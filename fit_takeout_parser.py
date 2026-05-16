#!/usr/bin/env python3
"""
fit_takeout_parser.py
=====================
Lossless parser for Google Fit Takeout exports → SQLite database,
with CSV export and compact JSON summary generation.

Author         : Juan I. Peralta (https://github.com/grapako)
Generated with : Claude Sonnet 4.6  (https://claude.ai)
Date           : 2026-05-13
License        : MIT

Sources parsed (NO information loss):
  - Takeout/Fit/All data/               → tables: fit_raw, fit_derived
                                          Maximum resolution (nanoseconds).
                                          raw     = written directly by each app.
                                          derived = calculated/merged by Google.
  - Takeout/Fit/All sessions/           → table: fit_sessions
                                          Session metadata (name, type, duration).
  - Takeout/Fit/Activities/             → table: fit_activities
                                          GPS trackpoints + HR per second (TCX files).
  - Takeout/Fit/Daily activity metrics/ → table: fit_daily_aggregates
                                          ⚠ Already aggregated by Google (~15 min res.).
                                          Stored intact for reference; fit_raw is the
                                          primary high-resolution source.

Discarded (only):
  - Timestamps both equal to 0 (empty records)
  - JSON files that cannot be parsed (logged as warnings)
  - Data points with non-parseable timestamps

Dependencies:
  - orjson (pip install orjson)  → JSON parsing ~5–10x faster than stdlib.
                                   Falls back to stdlib json if not installed.
  - Everything else: sqlite3, pathlib, csv, xml.etree.ElementTree → stdlib only.

Usage:
  # Interactive menu (recommended):
  python fit_takeout_parser.py

  # CLI mode:
  python fit_takeout_parser.py parse   --input /path/to/Fit --output fit_historical.db
  python fit_takeout_parser.py export  --db fit_historical.db --types body.fat.percentage
  python fit_takeout_parser.py summary --db fit_historical.db --output fit_summary.json
"""

import argparse
import csv
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Iterator, Optional

# ---------------------------------------------------------------------------
# JSON backend: orjson if available, fallback to stdlib
# ---------------------------------------------------------------------------
try:
    import orjson

    def load_json(path: Path) -> dict:
        """Load a JSON file using orjson (faster)."""
        return orjson.loads(path.read_bytes())

    JSON_BACKEND = "orjson"
except ImportError:
    import json as _json_stdlib

    def load_json(path: Path) -> dict:
        """Load a JSON file using stdlib json (fallback)."""
        with open(path, encoding="utf-8") as f:
            return _json_stdlib.load(f)

    JSON_BACKEND = "json (stdlib — install orjson for faster parsing)"


# ===========================================================================
# PROGRESS BAR (stdlib only, no tqdm)
# ===========================================================================

class ProgressBar:
    """
    Terminal progress bar using only stdlib.
    Renders on a single line using carriage return (\\r).
    Shows: label | filled bar | percentage | current/total | elapsed | ETA.
    """
    BAR_WIDTH = 30

    def __init__(self, total: int, label: str) -> None:
        self.total    = max(total, 1)
        self.label    = label
        self.current  = 0
        self.t_start  = time.monotonic()
        self._last_render = -1.0
        self._print(force=True)

    def update(self, n: int = 1) -> None:
        """Advance the bar by n steps."""
        self.current = min(self.current + n, self.total)
        now = time.monotonic()
        # Throttle rendering to ~10 fps to avoid I/O bottleneck
        if now - self._last_render >= 0.1 or self.current == self.total:
            self._print()

    def _fmt_seconds(self, s: float) -> str:
        s = int(s)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m}m{s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"

    def _print(self, force: bool = False) -> None:
        now      = time.monotonic()
        elapsed  = now - self.t_start
        pct      = self.current / self.total
        filled   = int(self.BAR_WIDTH * pct)
        bar      = "█" * filled + "░" * (self.BAR_WIDTH - filled)

        if self.current > 0 and elapsed > 0:
            rate = self.current / elapsed
            eta  = (self.total - self.current) / rate if rate > 0 else 0
            eta_str = f"ETA {self._fmt_seconds(eta)}"
        else:
            eta_str = "ETA --"

        line = (
            f"  [{self.label:<22}] {bar} {pct:5.1%} "
            f"{self.current:>{len(str(self.total))}}/{self.total} "
            f"| {self._fmt_seconds(elapsed)} elapsed | {eta_str}"
        )
        sys.stdout.write(f"\r{line}")
        sys.stdout.flush()
        self._last_render = now

    def close(self, msg: str = "✓") -> None:
        """Print final status and move to a new line."""
        elapsed = time.monotonic() - self.t_start
        sys.stdout.write(
            f"\r  [{self.label:<22}] done — {self.total:,} items "
            f"in {self._fmt_seconds(elapsed)} {msg}\n"
        )
        sys.stdout.flush()


# ===========================================================================
# DATABASE SCHEMA
# ===========================================================================

SCHEMA = """
-- -----------------------------------------------------------------------
-- fit_raw: data points written directly by each app (maximum fidelity).
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fit_raw (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ns    INTEGER NOT NULL,   -- start timestamp in nanoseconds since Unix epoch
    end_ns      INTEGER NOT NULL,   -- end timestamp
    start_dt    TEXT    NOT NULL,   -- human-readable ISO8601 UTC ("2024-03-15T14:32:00+00:00")
    end_dt      TEXT    NOT NULL,
    data_type   TEXT    NOT NULL,   -- clean type ("body.fat.percentage", "heart_rate.bpm", ...)
    source_app  TEXT,               -- origin app ("com.mi.health", "com.samsung.shealth", ...)
    value_key   TEXT,               -- field name ("percentage", "bpm", "steps", ...)
    value_fp    REAL,               -- float value (when applicable)
    value_int   INTEGER             -- integer value (when applicable)
);

-- -----------------------------------------------------------------------
-- fit_derived: data points calculated/merged by Google.
-- Useful for detecting corrections or inconsistencies vs fit_raw.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fit_derived (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ns    INTEGER NOT NULL,
    end_ns      INTEGER NOT NULL,
    start_dt    TEXT    NOT NULL,
    end_dt      TEXT    NOT NULL,
    data_type   TEXT    NOT NULL,
    source_app  TEXT,
    value_key   TEXT,
    value_fp    REAL,
    value_int   INTEGER
);

-- -----------------------------------------------------------------------
-- fit_sessions: workout session metadata.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fit_sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ms           INTEGER NOT NULL,  -- milliseconds since epoch
    end_ms             INTEGER NOT NULL,
    start_dt           TEXT    NOT NULL,
    end_dt             TEXT    NOT NULL,
    name               TEXT,
    activity_type      INTEGER,           -- Google Fit numeric activity code
    activity_type_name TEXT,              -- human-readable ("Running", "Strength training", ...)
    source_app         TEXT,
    description        TEXT
);

-- -----------------------------------------------------------------------
-- fit_activities: GPS trackpoints + HR per second from TCX files.
-- Each row = one point in time during a workout.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fit_activities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id  INTEGER NOT NULL,  -- internal ID per TCX file
    sport        TEXT,              -- declared sport ("Running", "Biking", ...)
    lap_start_dt TEXT,
    point_dt     TEXT    NOT NULL,  -- ISO8601 of the trackpoint
    point_ts     INTEGER NOT NULL,  -- unix timestamp (seconds)
    lat          REAL,              -- latitude in decimal degrees
    lon          REAL,              -- longitude in decimal degrees
    altitude_m   REAL,
    hr_bpm       INTEGER,
    speed_ms     REAL,              -- speed in m/s
    cadence      INTEGER,           -- steps/min (running) or rpm (cycling)
    power_w      REAL,
    filename     TEXT               -- source TCX filename (for traceability)
);

-- -----------------------------------------------------------------------
-- fit_daily_aggregates: data pre-aggregated by Google (~15 min resolution).
-- NOTE: informational only. fit_raw is the primary high-resolution source.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fit_daily_aggregates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT    NOT NULL,  -- YYYY-MM-DD (extracted from filename)
    start_dt     TEXT,
    end_dt       TEXT,
    column_name  TEXT    NOT NULL,  -- original CSV column name
    value_text   TEXT,              -- raw value as text (preserves original)
    value_fp     REAL,              -- value as float (when parseable)
    source_file  TEXT               -- source CSV filename
);

-- -----------------------------------------------------------------------
-- Indexes for efficient time-series queries over years of data.
-- -----------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_raw_type_time      ON fit_raw     (data_type, start_ns);
CREATE INDEX IF NOT EXISTS idx_raw_time           ON fit_raw     (start_ns);
CREATE INDEX IF NOT EXISTS idx_raw_app_type       ON fit_raw     (source_app, data_type);
CREATE INDEX IF NOT EXISTS idx_derived_type_time  ON fit_derived (data_type, start_ns);
CREATE INDEX IF NOT EXISTS idx_derived_time       ON fit_derived (start_ns);
CREATE INDEX IF NOT EXISTS idx_sessions_time      ON fit_sessions (start_ms);
CREATE INDEX IF NOT EXISTS idx_sessions_type      ON fit_sessions (activity_type);
CREATE INDEX IF NOT EXISTS idx_activities_ts      ON fit_activities (point_ts);
CREATE INDEX IF NOT EXISTS idx_activities_act     ON fit_activities (activity_id);
CREATE INDEX IF NOT EXISTS idx_daily_date         ON fit_daily_aggregates (date);
"""

# ===========================================================================
# GOOGLE FIT ACTIVITY TYPE MAP
# ===========================================================================
ACTIVITY_TYPES = {
    0: "In vehicle", 1: "Biking", 2: "On foot", 3: "Still", 4: "Unknown",
    5: "Tilting", 7: "Walking", 8: "Running", 9: "Aerobics", 10: "Badminton",
    11: "Baseball", 12: "Basketball", 13: "Biathlon", 14: "Hand biking",
    15: "Mountain biking", 16: "Road biking", 17: "Spinning", 18: "Stationary biking",
    19: "Utility biking", 20: "Boxing", 21: "Calisthenics", 22: "Circuit training",
    23: "Cricket", 24: "Cross training", 25: "Curling", 26: "Dancing",
    27: "Diving", 28: "Elliptical", 29: "Ergometer", 30: "Fencing",
    31: "Football American", 32: "Football Australian", 33: "Football soccer",
    34: "Frisbee", 35: "Gardening", 36: "Golf", 37: "Gymnastics",
    38: "Handball", 39: "Hiking", 40: "Hockey", 41: "Horseback riding",
    42: "Housework", 43: "Ice skating", 44: "In vehicle", 45: "Jumping rope",
    46: "Kayaking", 47: "Kettlebell training", 48: "Kickboxing", 49: "Kitesurfing",
    50: "Martial arts", 51: "Meditation", 52: "Mixed martial arts", 53: "P90X exercises",
    54: "Paragliding", 55: "Pilates", 56: "Polo", 57: "Racquetball",
    58: "Rock climbing", 59: "Rowing", 60: "Rowing machine", 61: "Rugby",
    62: "Jogging", 63: "Running on sand", 64: "Running treadmill", 65: "Sailing",
    66: "Scuba diving", 67: "Skateboarding", 68: "Skating", 69: "Cross skating",
    70: "Indoor inline skating", 71: "Skiing", 72: "Back-country skiing",
    73: "Cross-country skiing", 74: "Downhill skiing", 75: "Kite skiing",
    76: "Roller skiing", 77: "Sledding", 78: "Sleeping", 79: "Light sleep",
    80: "Deep sleep", 81: "REM sleep", 82: "Awake (during sleep)",
    83: "Snowboarding", 84: "Snowmobile", 85: "Snowshoeing",
    86: "Squash", 87: "Stair climbing", 88: "Stair climbing machine",
    89: "Stand up paddleboarding", 90: "Strength training", 91: "Surfing",
    92: "Swimming open water", 93: "Swimming pool", 94: "Table tennis",
    95: "Team sports", 96: "Tennis", 97: "Treadmill", 98: "Volleyball",
    99: "Volleyball beach", 100: "Volleyball indoor", 101: "Wakeboarding",
    102: "Walking fitness", 103: "Nordic walking", 104: "Treadmill walking",
    105: "Waterpolo", 106: "Weightlifting", 107: "Wheelchair", 108: "Windsurfing",
    109: "Yoga", 110: "Zumba", 111: "Diving",
}

# ===========================================================================
# UTILITIES
# ===========================================================================

def ns_to_dt(ns: int) -> str:
    """Convert nanoseconds since Unix epoch to ISO8601 UTC string."""
    try:
        return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return "INVALID"


def ms_to_dt(ms: int) -> str:
    """Convert milliseconds since Unix epoch to ISO8601 UTC string."""
    try:
        return datetime.fromtimestamp(ms / 1_000, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return "INVALID"


def extract_source_app(origin_id: str) -> str:
    """
    Extract the source package name from originDataSourceId.

    Format: '<layer>:<producer_pkg>:<source_pkg>:<device_uid>:...'
    Examples:
      'raw:com.google.android.gms:com.xiaomi.hm.health:...' → 'com.xiaomi.hm.health'
      'derived:com.google.android.gms:com.google.android.gms:...' → 'com.google.android.gms'
    """
    if not origin_id:
        return ""
    parts = origin_id.split(":")
    if len(parts) >= 3:
        return parts[2]
    elif len(parts) >= 2:
        return parts[1]
    return origin_id


def is_derived(origin_id: str) -> bool:
    """Return True if the data point was calculated/merged by Google."""
    return origin_id.startswith("derived:")


# ===========================================================================
# PARSER: All data/ → fit_raw + fit_derived
# ===========================================================================

def parse_all_data(folder: Path) -> Generator[tuple, None, None]:
    """
    Parse all JSON files in Takeout/Fit/All data/.

    Each file contains a list of 'Data Points' with nanosecond timestamps
    and values in a fitValue[].mapVal[] structure.
    Automatically separates raw vs derived based on originDataSourceId.

    Discards only:
      - JSON files that cannot be parsed (logged as warnings)
      - Points with both timestamps equal to 0 (explicit empty records)
      - Points with non-parseable timestamps

    Yields: ("fit_raw" | "fit_derived", row_dict)
    """
    files = sorted(folder.glob("*.json"))
    total = len(files)
    bar   = ProgressBar(total, "All data")

    for filepath in files:
        bar.update()
        try:
            data = load_json(filepath)
        except Exception as e:
            sys.stdout.write(f"\n  ⚠  Cannot read {filepath.name}: {e}\n")
            continue

        points = data.get("Data Points", [])
        if not points:
            continue

        for point in points:
            start_ns_str = point.get("startTimeNanos", "")
            end_ns_str   = point.get("endTimeNanos",   "")
            origin_id    = point.get("originDataSourceId", "")
            data_type    = point.get("dataTypeName", "")

            # Strip verbose "com.google." prefix for readability
            clean_type = data_type.replace("com.google.", "")

            try:
                start_ns = int(start_ns_str)
                end_ns   = int(end_ns_str)
            except (ValueError, TypeError):
                continue  # no valid timestamp → useless record

            if start_ns == 0 and end_ns == 0:
                continue  # explicit empty record

            source_app = extract_source_app(origin_id)
            table = "fit_derived" if is_derived(origin_id) else "fit_raw"

            # --- Parse fitValue ---
            # Structure A: fitValue[].mapVal[].{key, value.{fpVal|intVal}}
            # Structure B: fitValue[].{fpVal|intVal}  (no mapVal)
            fit_values = point.get("fitValue", [])

            if not fit_values:
                # No value but valid timestamp → store (marks event presence)
                yield (table, {
                    "start_ns": start_ns, "end_ns": end_ns,
                    "start_dt": ns_to_dt(start_ns), "end_dt": ns_to_dt(end_ns),
                    "data_type": clean_type, "source_app": source_app,
                    "value_key": None, "value_fp": None, "value_int": None,
                })
                continue

            for fv in fit_values:
                map_vals = fv.get("mapVal", [])
                if map_vals:
                    # Structure A
                    for mv in map_vals:
                        key      = mv.get("key", "") or None
                        val_dict = mv.get("value", {})
                        fp_val   = val_dict.get("fpVal")
                        int_val  = val_dict.get("intVal")
                        yield (table, {
                            "start_ns":  start_ns, "end_ns": end_ns,
                            "start_dt":  ns_to_dt(start_ns), "end_dt": ns_to_dt(end_ns),
                            "data_type": clean_type, "source_app": source_app,
                            "value_key": key,
                            "value_fp":  float(fp_val)  if fp_val  is not None else None,
                            "value_int": int(int_val)   if int_val is not None else None,
                        })
                else:
                    # Structure B
                    fp_val  = fv.get("fpVal")
                    int_val = fv.get("intVal")
                    yield (table, {
                        "start_ns":  start_ns, "end_ns": end_ns,
                        "start_dt":  ns_to_dt(start_ns), "end_dt": ns_to_dt(end_ns),
                        "data_type": clean_type, "source_app": source_app,
                        "value_key": None,
                        "value_fp":  float(fp_val)  if fp_val  is not None else None,
                        "value_int": int(int_val)   if int_val is not None else None,
                    })

    bar.close()


# ===========================================================================
# PARSER: All sessions/ → fit_sessions
# ===========================================================================

def parse_sessions(folder: Path) -> Generator[tuple, None, None]:
    """
    Parse all JSON files in Takeout/Fit/All sessions/.

    Each JSON may contain a list of sessions under the 'sessions' key.
    Stores name, activity type, source app, and description.

    Yields: ("fit_sessions", row_dict)
    """
    files = sorted(folder.glob("*.json"))
    total = len(files)
    bar   = ProgressBar(total, "All sessions")
    count = 0

    for filepath in files:
        bar.update()
        try:
            data = load_json(filepath)
        except Exception as e:
            sys.stdout.write(f"\n  ⚠  Cannot read {filepath.name}: {e}\n")
            continue

        for sess in data.get("sessions", []):
            try:
                start_ms = int(sess.get("startTimeMillis", 0))
                end_ms   = int(sess.get("endTimeMillis",   0))
            except (ValueError, TypeError):
                continue

            if start_ms == 0:
                continue

            act_type   = sess.get("activityType", -1)
            app_dict   = sess.get("application", {}) or {}
            source_app = app_dict.get("packageName", "")

            count += 1
            yield ("fit_sessions", {
                "start_ms":           start_ms,
                "end_ms":             end_ms,
                "start_dt":           ms_to_dt(start_ms),
                "end_dt":             ms_to_dt(end_ms),
                "name":               sess.get("name", "") or "",
                "activity_type":      act_type,
                "activity_type_name": ACTIVITY_TYPES.get(act_type, f"Unknown ({act_type})"),
                "source_app":         source_app,
                "description":        sess.get("description", "") or "",
            })

    bar.close(f"✓ ({count} sessions)")


# ===========================================================================
# PARSER: Activities/ → fit_activities  (TCX)
# ===========================================================================

def _find_tag(elem: ET.Element, tag: str, ns: str) -> Optional[ET.Element]:
    """Find a child element by tag, with namespace fallback."""
    result = elem.find(f"{{{ns}}}{tag}") if ns else None
    if result is None:
        result = elem.find(tag)
    return result


def _findall_tag(elem: ET.Element, tag: str, ns: str) -> list:
    result = elem.findall(f"{{{ns}}}{tag}") if ns else []
    if not result:
        result = elem.findall(tag)
    return result


def _find_in_extensions(tp: ET.Element, tag: str) -> Optional[ET.Element]:
    """Search for a tag in any namespace within the extensions subtree."""
    for child in tp.iter():
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if local == tag:
            return child
    return None


def parse_activities(folder: Path) -> Generator[tuple, None, None]:
    """
    Parse all TCX files in Takeout/Fit/Activities/.

    Each TCX may have multiple laps with multiple trackpoints.
    Extracts: time, lat/lon, altitude, HR, speed, cadence, power.
    Stores source filename for full traceability.

    Yields: ("fit_activities", row_dict)
    """
    files = sorted(folder.glob("*.tcx"))
    total = len(files)
    bar   = ProgressBar(total, "Activities (TCX)")

    for act_id, filepath in enumerate(files, 1):
        bar.update()
        try:
            tree = ET.parse(filepath)
            root = tree.getroot()
        except ET.ParseError as e:
            sys.stdout.write(f"\n  ⚠  XML error in {filepath.name}: {e}\n")
            continue

        # Detect namespace (may vary between files)
        ns = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""

        activities_elem = _find_tag(root, "Activities", ns)
        if activities_elem is None:
            continue

        for act_elem in _findall_tag(activities_elem, "Activity", ns):
            sport = act_elem.get("Sport", "")

            for lap_elem in _findall_tag(act_elem, "Lap", ns):
                lap_start_dt = lap_elem.get("StartTime", "")
                track_elem   = _find_tag(lap_elem, "Track", ns)
                if track_elem is None:
                    continue

                for tp in _findall_tag(track_elem, "Trackpoint", ns):
                    time_elem = _find_tag(tp, "Time", ns)
                    if time_elem is None or not time_elem.text:
                        continue

                    point_dt = time_elem.text.strip()
                    try:
                        point_ts = int(
                            datetime.fromisoformat(
                                point_dt.replace("Z", "+00:00")
                            ).timestamp()
                        )
                    except ValueError:
                        point_ts = 0

                    # GPS position
                    lat = lon = None
                    pos_elem = _find_tag(tp, "Position", ns)
                    if pos_elem is not None:
                        lat_e = _find_tag(pos_elem, "LatitudeDegrees",  ns)
                        lon_e = _find_tag(pos_elem, "LongitudeDegrees", ns)
                        try:
                            lat = float(lat_e.text) if lat_e is not None and lat_e.text else None
                            lon = float(lon_e.text) if lon_e is not None and lon_e.text else None
                        except ValueError:
                            pass

                    # Altitude
                    alt   = None
                    alt_e = _find_tag(tp, "AltitudeMeters", ns)
                    if alt_e is not None and alt_e.text:
                        try: alt = float(alt_e.text)
                        except ValueError: pass

                    # Heart rate
                    hr_bpm = None
                    hr_e   = _find_tag(tp, "HeartRateBpm", ns)
                    if hr_e is not None:
                        val_e = _find_tag(hr_e, "Value", ns)
                        if val_e is not None and val_e.text:
                            try: hr_bpm = int(val_e.text)
                            except ValueError: pass

                    # Extensions: speed, cadence, power
                    speed = cadence = power = None
                    for tag, dtype, attr in [
                        ("Speed",      float, "speed"),
                        ("RunCadence", int,   "cadence"),
                        ("Watts",      float, "power"),
                    ]:
                        elem = _find_in_extensions(tp, tag)
                        if elem is not None and elem.text:
                            try:
                                val = dtype(elem.text)
                                if attr == "speed":   speed   = val
                                if attr == "cadence": cadence = val
                                if attr == "power":   power   = val
                            except ValueError:
                                pass

                    yield ("fit_activities", {
                        "activity_id":  act_id,
                        "sport":        sport,
                        "lap_start_dt": lap_start_dt,
                        "point_dt":     point_dt,
                        "point_ts":     point_ts,
                        "lat":          lat,
                        "lon":          lon,
                        "altitude_m":   alt,
                        "hr_bpm":       hr_bpm,
                        "speed_ms":     speed,
                        "cadence":      cadence,
                        "power_w":      power,
                        "filename":     filepath.name,
                    })

    bar.close()


# ===========================================================================
# PARSER: Daily activity metrics/ → fit_daily_aggregates
# ===========================================================================

def parse_daily_metrics(folder: Path) -> Generator[tuple, None, None]:
    """
    Parse all CSV files in Takeout/Fit/Daily activity metrics/.

    ⚠ These are AGGREGATED data (not raw). Stored intact without any loss
    relative to the original CSV. Each cell is stored as a separate row to
    maintain a generic schema that survives future Google column changes.

    Yields: ("fit_daily_aggregates", row_dict)
    """
    files = sorted(folder.glob("*.csv"))
    total = len(files)
    bar   = ProgressBar(total, "Daily metrics (CSV)")
    count = 0

    TIME_COLS = {"Start time", "End time", "startTime", "endTime"}

    for filepath in files:
        bar.update()
        stem     = filepath.stem
        date_str = stem if (len(stem) == 10 and stem[4] == "-" and stem[7] == "-") else ""

        try:
            with open(filepath, encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    start_dt = row.get("Start time") or row.get("startTime") or ""
                    end_dt   = row.get("End time")   or row.get("endTime")   or ""

                    for col, val in row.items():
                        if col in TIME_COLS:
                            continue
                        if val is None or (isinstance(val, str) and val.strip() == ""):
                            continue

                        fp_val = None
                        try:
                            fp_val = float(val)
                        except (ValueError, AttributeError):
                            pass

                        count += 1
                        yield ("fit_daily_aggregates", {
                            "date":        date_str,
                            "start_dt":    start_dt,
                            "end_dt":      end_dt,
                            "column_name": col,
                            "value_text":  str(val),
                            "value_fp":    fp_val,
                            "source_file": filepath.name,
                        })
        except Exception as e:
            sys.stdout.write(f"\n  ⚠  Error reading {filepath.name}: {e}\n")
            continue

    bar.close(f"✓ ({count:,} cells)")


# ===========================================================================
# SQLITE WRITER (batch inserts for efficiency)
# ===========================================================================

INSERT_QUERIES = {
    "fit_raw": """
        INSERT INTO fit_raw
            (start_ns, end_ns, start_dt, end_dt, data_type, source_app, value_key, value_fp, value_int)
        VALUES
            (:start_ns, :end_ns, :start_dt, :end_dt, :data_type, :source_app, :value_key, :value_fp, :value_int)
    """,
    "fit_derived": """
        INSERT INTO fit_derived
            (start_ns, end_ns, start_dt, end_dt, data_type, source_app, value_key, value_fp, value_int)
        VALUES
            (:start_ns, :end_ns, :start_dt, :end_dt, :data_type, :source_app, :value_key, :value_fp, :value_int)
    """,
    "fit_sessions": """
        INSERT INTO fit_sessions
            (start_ms, end_ms, start_dt, end_dt, name, activity_type, activity_type_name, source_app, description)
        VALUES
            (:start_ms, :end_ms, :start_dt, :end_dt, :name, :activity_type, :activity_type_name, :source_app, :description)
    """,
    "fit_activities": """
        INSERT INTO fit_activities
            (activity_id, sport, lap_start_dt, point_dt, point_ts, lat, lon, altitude_m, hr_bpm, speed_ms, cadence, power_w, filename)
        VALUES
            (:activity_id, :sport, :lap_start_dt, :point_dt, :point_ts, :lat, :lon, :altitude_m, :hr_bpm, :speed_ms, :cadence, :power_w, :filename)
    """,
    "fit_daily_aggregates": """
        INSERT INTO fit_daily_aggregates
            (date, start_dt, end_dt, column_name, value_text, value_fp, source_file)
        VALUES
            (:date, :start_dt, :end_dt, :column_name, :value_text, :value_fp, :source_file)
    """,
}

BATCH_SIZE = 5_000  # rows per commit; increase if RAM allows


def write_to_db(db_path: Path, generator: Iterator) -> int:
    """
    Write all rows from the generator into the SQLite database.

    Uses WAL journal mode and batch commits for maximum throughput.
    Returns total rows inserted.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-65536")  # 64 MB RAM cache
    conn.executescript(SCHEMA)

    total = 0
    batch: dict[str, list] = {t: [] for t in INSERT_QUERIES}

    for table, row in generator:
        batch[table].append(row)
        total += 1

        if sum(len(v) for v in batch.values()) >= BATCH_SIZE:
            for t, rows in batch.items():
                if rows:
                    conn.executemany(INSERT_QUERIES[t], rows)
            conn.commit()
            batch = {t: [] for t in INSERT_QUERIES}

    # Final flush
    for t, rows in batch.items():
        if rows:
            conn.executemany(INSERT_QUERIES[t], rows)
    conn.commit()
    conn.close()

    return total


# ===========================================================================
# CSV EXPORT
# ===========================================================================

def export_csv(
    db_path: Path,
    output_dir: Path,
    data_types: Optional[list] = None,
    from_dt: Optional[str] = None,
    to_dt: Optional[str] = None,
    tables: Optional[list] = None,
) -> None:
    """
    Export rows from fit_raw and/or fit_derived to CSV files.

    Optional filters:
      - data_types : list of types (e.g. ["body.fat.percentage", "heart_rate.bpm"])
      - from_dt    : start date ISO8601 or YYYY-MM-DD
      - to_dt      : end date
      - tables     : ["fit_raw"] | ["fit_derived"] | ["fit_raw", "fit_derived"]
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = datetime.now().strftime("%Y%m%d_%H%M%S")

    target_tables = tables or ["fit_raw", "fit_derived"]

    for table in target_tables:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            print(f"  [CSV] ⚠  Table {table} not found.")
            continue

        conditions, params = [], []
        if data_types:
            ph = ",".join("?" * len(data_types))
            conditions.append(f"data_type IN ({ph})")
            params.extend(data_types)
        if from_dt:
            conditions.append("start_dt >= ?")
            params.append(from_dt)
        if to_dt:
            conditions.append("start_dt <= ?")
            params.append(to_dt + "T23:59:59" if len(to_dt) == 10 else to_dt)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM {table} {where} ORDER BY start_ns"
        rows  = conn.execute(query, params).fetchall()

        if not rows:
            print(f"  [CSV] {table}: no data for the specified filters.")
            continue

        out_file = output_dir / f"{table}_{suffix}.csv"
        with open(out_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows([dict(r) for r in rows])

        print(f"  [CSV] {table} → {out_file}  ({len(rows):,} rows)")

    conn.close()


# ===========================================================================
# SUMMARY
# ===========================================================================

def generate_summary(db_path: Path, output_path: Optional[Path] = None) -> dict:
    """
    Generate a compact JSON with per-type statistics across all tables.

    This summary does NOT reduce or alter the database in any way.
    It is a statistical view intended for LLM context or quick inspection.
    The raw source remains intact in the database.

    Includes per data_type: count, date range, mean/min/max of numeric values.
    Includes per activity: session count, trackpoint count, date range.
    """
    import json as _json

    conn    = sqlite3.connect(str(db_path))
    summary: dict = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "db_path":      str(db_path.resolve()),
        "tables":       {},
    }

    for table in ("fit_raw", "fit_derived"):
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue

        total_rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        rows = conn.execute(f"""
            SELECT
                data_type, source_app,
                COUNT(*)      AS n,
                MIN(start_dt) AS first_dt,
                MAX(start_dt) AS last_dt,
                AVG(value_fp) AS mean_fp,
                MIN(value_fp) AS min_fp,
                MAX(value_fp) AS max_fp
            FROM {table}
            GROUP BY data_type, source_app
            ORDER BY data_type, n DESC
        """).fetchall()

        summary["tables"][table] = {
            "total_rows": total_rows,
            "data_types": [
                {
                    "data_type":  r[0], "source_app": r[1],
                    "n":          r[2], "first_dt": r[3], "last_dt": r[4],
                    "mean_fp":    round(r[5], 4) if r[5] is not None else None,
                    "min_fp":     r[6], "max_fp": r[7],
                }
                for r in rows
            ],
        }

    # Sessions
    sess_count = conn.execute("SELECT COUNT(*) FROM fit_sessions").fetchone()[0]
    sess_types = conn.execute("""
        SELECT activity_type_name, COUNT(*) AS n, MIN(start_dt), MAX(start_dt)
        FROM fit_sessions
        GROUP BY activity_type_name
        ORDER BY n DESC
    """).fetchall()
    summary["tables"]["fit_sessions"] = {
        "total_rows": sess_count,
        "by_activity": [
            {"activity": r[0], "n": r[1], "first_dt": r[2], "last_dt": r[3]}
            for r in sess_types
        ],
    }

    # Activities (TCX trackpoints)
    act_count  = conn.execute("SELECT COUNT(*) FROM fit_activities").fetchone()[0]
    act_sports = conn.execute("""
        SELECT sport,
               COUNT(DISTINCT activity_id) AS n_workouts,
               COUNT(*)                    AS n_trackpoints,
               MIN(point_dt)               AS first_dt,
               MAX(point_dt)               AS last_dt
        FROM fit_activities
        GROUP BY sport
        ORDER BY n_workouts DESC
    """).fetchall()
    summary["tables"]["fit_activities"] = {
        "total_trackpoints": act_count,
        "by_sport": [
            {"sport": r[0], "workouts": r[1], "trackpoints": r[2],
             "first_dt": r[3], "last_dt": r[4]}
            for r in act_sports
        ],
    }

    # Daily aggregates
    daily_count = conn.execute("SELECT COUNT(*) FROM fit_daily_aggregates").fetchone()[0]
    daily_cols  = conn.execute("""
        SELECT column_name, COUNT(*) AS n, MIN(date), MAX(date)
        FROM fit_daily_aggregates
        GROUP BY column_name
        ORDER BY n DESC
    """).fetchall()
    summary["tables"]["fit_daily_aggregates"] = {
        "total_rows": daily_count,
        "columns": [
            {"column": r[0], "n": r[1], "first_date": r[2], "last_date": r[3]}
            for r in daily_cols
        ],
    }

    conn.close()

    summary_json = _json.dumps(summary, indent=2, ensure_ascii=False)
    if output_path:
        output_path.write_text(summary_json, encoding="utf-8")
        print(f"  [Summary] → {output_path.resolve()}")
    else:
        print(summary_json)

    return summary


# ===========================================================================
# CORE PARSE LOGIC (shared by menu and CLI)
# ===========================================================================

def run_parse(fit_folder: Path, db_path: Path) -> None:
    """Coordinate all parsers and write the database."""
    folders = {
        "All data":               fit_folder / "All data",
        "All sessions":           fit_folder / "All sessions",
        "Activities":             fit_folder / "Activities",
        "Daily activity metrics": fit_folder / "Daily activity metrics",
    }

    print(f"\n  JSON backend : {JSON_BACKEND}")
    print(f"  Fit folder   : {fit_folder.resolve()}")
    print(f"  Output DB    : {db_path.resolve()}\n")

    found_any = False
    for name, path in folders.items():
        if path.exists():
            n = len(list(path.iterdir()))
            print(f"  ✓  {name:<30}  ({n:,} files)")
            found_any = True
        else:
            print(f"  ✗  {name:<30}  (not found, skipping)")

    if not found_any:
        print("\n  ❌ No expected subfolders found. Check the path.")
        return

    def all_generators():
        for name, path in folders.items():
            if not path.exists():
                continue
            print(f"\n  → Parsing: {name}/")
            if name == "All data":
                yield from parse_all_data(path)
            elif name == "All sessions":
                yield from parse_sessions(path)
            elif name == "Activities":
                yield from parse_activities(path)
            elif name == "Daily activity metrics":
                yield from parse_daily_metrics(path)

    print("\n  Starting parse (this may take several minutes)...\n")
    t0      = time.monotonic()
    total   = write_to_db(db_path, all_generators())
    elapsed = time.monotonic() - t0

    size_mb = db_path.stat().st_size / (1024 * 1024)
    print(f"\n  ✅ Done.")
    print(f"     Rows inserted : {total:,}")
    print(f"     Total time    : {elapsed:.1f}s")
    print(f"     DB size       : {size_mb:.1f} MB")
    print(f"     Path          : {db_path.resolve()}")


# ===========================================================================
# INTERACTIVE MENU
# ===========================================================================

def ask(prompt: str, default: str = "") -> str:
    shown  = f" [{default}]" if default else ""
    result = input(f"  {prompt}{shown}: ").strip()
    return result if result else default


def menu_parse() -> None:
    print("\n" + "─" * 60)
    print("  PARSE COMPLETE TAKEOUT → SQLite")
    print("─" * 60)

    fit_str = ask("Path to the 'Fit' folder from Takeout")
    if not fit_str:
        print("  Empty path, returning to menu.")
        return

    db_str  = ask("Output DB filename", "fit_historical.db")
    fit_path = Path(fit_str)
    db_path  = Path(db_str)

    if not fit_path.exists():
        print(f"\n  ❌ Path does not exist: {fit_str}")
        return

    if db_path.exists():
        ow = ask(f"  ⚠  {db_str} already exists. Overwrite? [y/N]", "N").upper()
        if ow != "Y":
            return

    print()
    run_parse(fit_path, db_path)


def menu_export_csv() -> None:
    print("\n" + "─" * 60)
    print("  EXPORT CSV FROM DATABASE")
    print("─" * 60)

    db_str  = ask("Path to DB", "fit_historical.db")
    db_path = Path(db_str)

    if not db_path.exists():
        print(f"\n  ❌ Does not exist: {db_str}")
        return

    conn       = sqlite3.connect(str(db_path))
    types_raw  = [r[0] for r in conn.execute(
        "SELECT DISTINCT data_type FROM fit_raw ORDER BY data_type"
    ).fetchall()]
    conn.close()

    if not types_raw:
        print("  ⚠  fit_raw is empty.")
        return

    print(f"\n  Available types in fit_raw ({len(types_raw)}):")
    for i, t in enumerate(types_raw, 1):
        print(f"    {i:3d})  {t}")

    print()
    sel = ask("Types to export (comma-separated numbers, or ENTER for all)")
    selected_types = None
    if sel:
        try:
            indices        = [int(x.strip()) - 1 for x in sel.split(",")]
            selected_types = [types_raw[i] for i in indices if 0 <= i < len(types_raw)]
            print(f"  Selected: {selected_types}")
        except (ValueError, IndexError):
            print("  ⚠  Invalid selection, exporting all.")

    from_dt = ask("From date (YYYY-MM-DD, or ENTER for no limit)") or None
    to_dt   = ask("To date   (YYYY-MM-DD, or ENTER for no limit)") or None

    print("\n  Tables to export:")
    print("    1)  fit_raw only")
    print("    2)  fit_derived only")
    print("    3)  Both (default)")
    sel_t  = ask("Option", "3")
    tables = {"1": ["fit_raw"], "2": ["fit_derived"]}.get(sel_t, ["fit_raw", "fit_derived"])

    out_dir = ask("Output directory", "./csv_export")

    print()
    export_csv(
        db_path=db_path,
        output_dir=Path(out_dir),
        data_types=selected_types,
        from_dt=from_dt,
        to_dt=to_dt,
        tables=tables,
    )


def menu_summary() -> None:
    print("\n" + "─" * 60)
    print("  GENERATE SUMMARY JSON")
    print("─" * 60)

    db_str  = ask("Path to DB", "fit_historical.db")
    db_path = Path(db_str)

    if not db_path.exists():
        print(f"\n  ❌ Does not exist: {db_str}")
        return

    out_str = ask("Output file", "fit_summary.json")
    generate_summary(db_path, Path(out_str))


def run_menu() -> None:
    while True:
        print("\n" + "=" * 60)
        print("  Google Fit Takeout Parser")
        print(f"  Model: Claude Sonnet 4.6  |  Date: 2026-05-13")
        print("=" * 60)
        print()
        print("  1)  Parse complete Takeout → SQLite")
        print("  2)  Export CSV from existing DB")
        print("  3)  Generate compact summary (JSON)")
        print("  4)  Exit")
        print()

        choice = ask("Option [1–4]")

        if choice == "1":
            menu_parse()
        elif choice == "2":
            menu_export_csv()
        elif choice == "3":
            menu_summary()
        elif choice == "4":
            print("\n  Goodbye.\n")
            sys.exit(0)
        else:
            print("  Invalid option.")

        input("\n  [ENTER to return to menu]")


# ===========================================================================
# CLI
# ===========================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fit_takeout_parser",
        description="Google Fit Takeout → lossless SQLite + CSV export",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python fit_takeout_parser.py                                  # interactive menu
  python fit_takeout_parser.py parse   --input ~/Takeout/Fit
  python fit_takeout_parser.py export  --db fit_historical.db --types body.fat.percentage weight
  python fit_takeout_parser.py summary --db fit_historical.db --output fit_summary.json
        """,
    )
    sub = p.add_subparsers(dest="command")

    s_parse = sub.add_parser("parse", help="Parse Takeout → SQLite")
    s_parse.add_argument("--input",  required=True, help="Path to Fit folder")
    s_parse.add_argument("--output", default="fit_historical.db")

    s_export = sub.add_parser("export", help="Export CSV from DB")
    s_export.add_argument("--db",         required=True)
    s_export.add_argument("--output-dir", default="./csv_export")
    s_export.add_argument("--types",      nargs="+")
    s_export.add_argument("--from-date")
    s_export.add_argument("--to-date")
    s_export.add_argument(
        "--tables", nargs="+",
        choices=["fit_raw", "fit_derived"],
        default=["fit_raw", "fit_derived"]
    )

    s_sum = sub.add_parser("summary", help="Generate summary JSON")
    s_sum.add_argument("--db",     required=True)
    s_sum.add_argument("--output", default="fit_summary.json")

    return p


# ===========================================================================
# ENTRY POINT
# ===========================================================================

if __name__ == "__main__":
    if len(sys.argv) == 1:
        run_menu()
    else:
        parser = build_arg_parser()
        args   = parser.parse_args()

        if args.command == "parse":
            run_parse(Path(args.input), Path(args.output))
        elif args.command == "export":
            export_csv(
                db_path=Path(args.db),
                output_dir=Path(args.output_dir),
                data_types=args.types,
                from_dt=args.from_date,
                to_dt=args.to_date,
                tables=args.tables,
            )
        elif args.command == "summary":
            generate_summary(Path(args.db), Path(args.output))
        else:
            build_arg_parser().print_help()
