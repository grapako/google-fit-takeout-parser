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
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Iterator, Optional

# ---------------------------------------------------------------------------
# PATH CONFIGURATION
# ---------------------------------------------------------------------------
# All user-specific file/folder paths are read from file_path_config.txt.
# The config file is expected next to this script. Relative paths are
# resolved relative to the directory containing the config file.
CONFIG_FILE = Path(__file__).resolve().with_name("file_path_config.txt")


CONFIG_FOLDER_KEYS = {
    "ALL_DATA_FOLDER",
    "ALL_SESSIONS_FOLDER",
    "ACTIVITIES_FOLDER",
    "DAILY_ACTIVITY_METRICS_FOLDER",
}


def load_path_config(config_path: Path = CONFIG_FILE) -> dict[str, Path | str]:
    """Read paths and Takeout subfolder names from file_path_config.txt.

    Path settings are resolved relative to the configuration file.
    The four Google Fit subfolder settings are deliberately kept as strings:
    their names are relative to FIT_FOLDER and may be localized by Google
    Takeout (for example when the Takeout language is not English).
    """
    if not config_path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            "Create file_path_config.txt next to this script."
        )

    config_dir = config_path.parent
    values: dict[str, Path] = {}

    with config_path.open(encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(
                    f"Invalid line {line_no} in {config_path.name}: {raw_line.rstrip()}"
                )

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or not value:
                raise ValueError(
                    f"Invalid line {line_no} in {config_path.name}: {raw_line.rstrip()}"
                )

            if key in CONFIG_FOLDER_KEYS:
                # These are names of subfolders *inside* FIT_FOLDER, not
                # filesystem paths relative to file_path_config.txt.
                values[key] = value
            else:
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = config_dir / path
                values[key] = path.resolve()

    required = {
        "FIT_FOLDER",
        "DATABASE",
        "CSV_EXPORT_DIR",
        "SUMMARY_JSON",
        "CLEAN_DATABASE",
        "ALL_DATA_FOLDER",
        "ALL_SESSIONS_FOLDER",
        "ACTIVITIES_FOLDER",
        "DAILY_ACTIVITY_METRICS_FOLDER",
    }
    missing = sorted(required - values.keys())
    if missing:
        raise ValueError(
            f"Missing configuration key(s) in {config_path.name}: "
            + ", ".join(missing)
        )
    return values


def get_config() -> dict[str, Path]:
    """Load and return the configured paths."""
    return load_path_config()

# ---------------------------------------------------------------------------
# JSON backend: orjson if available, stdlib fallback at the call level
# ---------------------------------------------------------------------------
try:
    import orjson as _orjson
    _HAS_ORJSON = True
except ImportError:
    _HAS_ORJSON = False

import json as _json_stdlib


def load_json(path: Path) -> dict:
    """
    Load a JSON file, attempting orjson first for speed.

    orjson is ~5–10x faster but has two failure modes on large files:
      1. Memory allocation error (very large files, e.g. heart_rate.bpm)
      2. Any other parse error

    In both cases we fall back transparently to stdlib json, which reads
    the file incrementally and does not require the full content in RAM.
    The fallback is per-call, so one large file does not disable orjson
    for the rest of the run.
    """
    if _HAS_ORJSON:
        try:
            return _orjson.loads(path.read_bytes())
        except Exception:
            # Fallback: stdlib json reads via file object (lower peak RAM)
            with open(path, encoding="utf-8") as f:
                return _json_stdlib.load(f)
    else:
        with open(path, encoding="utf-8") as f:
            return _json_stdlib.load(f)


JSON_BACKEND = "orjson (with stdlib fallback)" if _HAS_ORJSON else "json (stdlib)"


# ===========================================================================
# PROGRESS BAR (stdlib only, no tqdm)
# ===========================================================================

def _supports_carriage_return() -> bool:
    """
    Return True only when the terminal reliably overwrites lines with \\r.

    Windows terminals (CMD, PowerShell, VS Code, Windows Terminal) handle \\r
    inconsistently — it often starts a new visual line rather than overwriting.
    Rather than probing with ctypes (which can succeed yet still not work),
    we use a simple rule: on Windows always fall back to newline mode.

    To force overwrite mode on Windows (e.g. in a known-good terminal):
        set FIT_PARSER_CR=1  (CMD)
        $env:FIT_PARSER_CR="1"  (PowerShell)
    """
    if sys.platform == "win32":
        return os.environ.get("FIT_PARSER_CR") == "1"
    return sys.stdout.isatty()


_CR_SUPPORTED = _supports_carriage_return()


class ProgressBar:
    """
    Terminal progress bar using only stdlib.

    Two rendering modes (auto-detected at startup):
      • Overwrite mode  : uses \\r to update a single line in place (Unix / Windows ANSI).
      • Fallback mode   : prints a new line every 10% (any terminal, piped output).

    Shows: label | filled bar | percentage | current/total | elapsed | ETA.
    """
    BAR_WIDTH   = 30
    LINE_WIDTH  = 100   # fixed pad width prevents stale characters from showing

    def __init__(self, total: int, label: str) -> None:
        self.total    = max(total, 1)
        self.label    = label
        self.current  = 0
        self.t_start  = time.monotonic()
        self._last_render    = -1.0
        self._last_pct_print = -1      # for fallback mode: last % boundary printed
        self._print(force=True)

    def update(self, n: int = 1) -> None:
        """Advance the bar by n steps."""
        self.current = min(self.current + n, self.total)
        now = time.monotonic()

        if _CR_SUPPORTED:
            # Overwrite mode: throttle to ~10 fps
            if now - self._last_render >= 0.1 or self.current == self.total:
                self._print()
        else:
            # Fallback mode: print at every 10% boundary and at completion
            pct_bucket = int(100 * self.current / self.total) // 10
            if pct_bucket != self._last_pct_print or self.current == self.total:
                self._last_pct_print = pct_bucket
                self._print()

    def _fmt_seconds(self, s: float) -> str:
        s = int(s)
        if s < 60:   return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:   return f"{m}m{s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"

    def _build_line(self) -> str:
        elapsed = time.monotonic() - self.t_start
        pct     = self.current / self.total
        filled  = int(self.BAR_WIDTH * pct)
        bar     = "█" * filled + "░" * (self.BAR_WIDTH - filled)

        if self.current > 0 and elapsed > 0:
            rate    = self.current / elapsed
            eta     = (self.total - self.current) / rate if rate > 0 else 0
            eta_str = f"ETA {self._fmt_seconds(eta)}"
        else:
            eta_str = "ETA --"

        return (
            f"  [{self.label:<22}] {bar} {pct:5.1%} "
            f"{self.current:>{len(str(self.total))}}/{self.total} "
            f"| {self._fmt_seconds(elapsed)} elapsed | {eta_str}"
        )

    def _print(self, force: bool = False) -> None:
        line = self._build_line()
        if _CR_SUPPORTED:
            # Pad to fixed width so shorter lines fully overwrite longer ones
            sys.stdout.write(f"\r{line:<{self.LINE_WIDTH}}")
        else:
            sys.stdout.write(f"{line}\n")
        sys.stdout.flush()
        self._last_render = time.monotonic()

    def close(self, msg: str = "✓") -> None:
        """Print final status and move to a new line."""
        elapsed = time.monotonic() - self.t_start
        final   = (
            f"  [{self.label:<22}] done — {self.total:,} items "
            f"in {self._fmt_seconds(elapsed)} {msg}"
        )
        if _CR_SUPPORTED:
            sys.stdout.write(f"\r{final:<{self.LINE_WIDTH}}\n")
        else:
            sys.stdout.write(f"{final}\n")
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


def is_derived(origin_id: str, filename: str = "") -> bool:
    """
    Return True if the data point belongs in fit_derived.

    Two-level detection (in order of reliability):
      1. originDataSourceId field starts with "derived:" → definitive.
      2. Filename starts with "derived_" → fallback when the field is empty.
         Google Fit Takeout consistently names derived files "derived_*" and
         raw files "raw_*", even when originDataSourceId is an empty string.
    """
    if origin_id:
        return origin_id.startswith("derived:")
    # Field is empty: fall back to filename convention
    stem = Path(filename).name if filename else ""
    return stem.startswith("derived_")


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

            # Strip verbose "com.google." prefix — only from the start of the string.
            # Using removeprefix() avoids corrupting types that contain "com.google."
            # in a non-prefix position (which replace() would incorrectly modify).
            clean_type = data_type.removeprefix("com.google.")

            try:
                start_ns = int(start_ns_str)
                end_ns   = int(end_ns_str)
            except (ValueError, TypeError):
                continue  # no valid timestamp → useless record

            if start_ns == 0 and end_ns == 0:
                continue  # explicit empty record

            source_app = extract_source_app(origin_id)
            table = "fit_derived" if is_derived(origin_id, filepath.name) else "fit_raw"

            # --- Parse fitValue ---
            #
            # Google Fit Takeout JSON uses one of these structures:
            #
            # Key name: Takeout exports may use "fitValue" OR "value" at the
            #   point level. We try both.
            #
            # Structure A — mapVal with nested value dict:
            #   fitValue[i].mapVal[j] = {"key": "steps", "value": {"intVal": 523}}
            #
            # Structure B — mapVal with flat values (no "value" nesting):
            #   fitValue[i].mapVal[j] = {"key": "percentage", "fpVal": 17.8}
            #
            # Structure C — direct values (no mapVal):
            #   fitValue[i] = {"intVal": 523}           (simple types)
            #   fitValue[i] = {"fpVal": 17.8}
            #
            # All four combinations (A|B) × (fitValue|value) are handled below.

            fit_values = point.get("fitValue") or point.get("value") or []
            if not isinstance(fit_values, list):
                fit_values = []

            if not fit_values:
                # No value array but valid timestamp → store as event marker.
                # Some Google Fit types (e.g. activity.segment) are purely
                # temporal markers whose meaning is encoded in start/end time.
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
                    # Structures A and B: iterate mapVal entries
                    for mv in map_vals:
                        key = mv.get("key", "") or None

                        # Structure A: values nested under "value" dict
                        nested  = mv.get("value") or {}
                        fp_val  = nested.get("fpVal")
                        int_val = nested.get("intVal")

                        # Structure B fallback: values flat in the mapVal item
                        if fp_val  is None: fp_val  = mv.get("fpVal")
                        if int_val is None: int_val = mv.get("intVal")

                        yield (table, {
                            "start_ns":  start_ns, "end_ns": end_ns,
                            "start_dt":  ns_to_dt(start_ns), "end_dt": ns_to_dt(end_ns),
                            "data_type": clean_type, "source_app": source_app,
                            "value_key": key,
                            "value_fp":  float(fp_val)  if fp_val  is not None else None,
                            "value_int": int(int_val)   if int_val is not None else None,
                        })
                else:
                    # Structure C: no mapVal — single value directly in the fitValue item.
                    #
                    # Google Fit Takeout standard format (nested under "value"):
                    #   fitValue[i] = {"value": {"fpVal": 17.8}}
                    #   fitValue[i] = {"value": {"intVal": 523}}
                    #
                    # Defensive flat fallback (some third-party or legacy sources):
                    #   fitValue[i] = {"fpVal": 17.8}
                    #   fitValue[i] = {"intVal": 523}
                    #
                    # Note: the "value" nesting is NOT the same as the top-level
                    # point.get("value") fallback for the fitValue array itself.
                    # Here we're reading one item inside that array.
                    nested  = fv.get("value") or {}
                    fp_val  = nested.get("fpVal")
                    int_val = nested.get("intVal")
                    if fp_val  is None: fp_val  = fv.get("fpVal")
                    if int_val is None: int_val = fv.get("intVal")
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

    # Columns that are metadata, not measurements — exclude from the metrics table.
    # "Date" appears in Daily Summaries.csv as the date identifier column.
    TIME_COLS = {"Start time", "End time", "startTime", "endTime", "Date"}

    for filepath in files:
        bar.update()
        stem     = filepath.stem
        # Extract date from filename (YYYY-MM-DD.csv) if possible.
        # For aggregate files like "Daily Summaries.csv", date_str is None
        # and will be filled from start_dt at row level.
        date_from_filename = (
            stem if (len(stem) == 10 and stem[4] == "-" and stem[7] == "-")
            else None
        )

        try:
            with open(filepath, encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    start_dt = row.get("Start time") or row.get("startTime") or ""
                    end_dt   = row.get("End time")   or row.get("endTime")   or ""

                    # Resolve the date for this row:
                    # 1) from filename (most reliable, one date per file)
                    # 2) from start_dt (Daily Summaries.csv rows have it)
                    # 3) empty string as last resort
                    if date_from_filename:
                        date_str = date_from_filename
                    elif start_dt:
                        date_str = start_dt[:10]
                    else:
                        date_str = ""

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
    apply_exclusions: bool = True,
) -> None:
    """
    Export rows from fit_raw and/or fit_derived to CSV files.

    Optional filters:
      - data_types       : list of types (e.g. ["body.fat.percentage", "heart_rate.bpm"])
      - from_dt          : start date ISO8601 or YYYY-MM-DD
      - to_dt            : end date
      - tables           : ["fit_raw"] | ["fit_derived"] | ["fit_raw", "fit_derived"]
      - apply_exclusions : if True (default), rows flagged in fit_excluded_points
                           are omitted from the CSV. Set False to export everything.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = datetime.now().strftime("%Y%m%d_%H%M%S")

    target_tables = tables or ["fit_raw", "fit_derived"]

    # Load excluded row IDs per table (if the exclusion table exists)
    excluded_ids: dict[str, set] = {"fit_raw": set(), "fit_derived": set()}
    if apply_exclusions:
        excl_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='fit_excluded_points'"
        ).fetchone()
        if excl_exists:
            for tbl in ("fit_raw", "fit_derived"):
                rows = conn.execute(
                    "SELECT row_id FROM fit_excluded_points WHERE table_name=?", (tbl,)
                ).fetchall()
                excluded_ids[tbl] = {r[0] for r in rows}

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
            conditions.append("substr(start_dt, 1, 10) >= ?")
            params.append(from_dt[:10])
        if to_dt:
            conditions.append("substr(start_dt, 1, 10) <= ?")
            params.append(to_dt[:10])

        # Exclusions: use NOT IN only when there are excluded IDs
        excl = excluded_ids.get(table, set())
        if excl:
            ph_excl = ",".join(str(i) for i in excl)
            conditions.append(f"id NOT IN ({ph_excl})")
            print(f"  [CSV] {table}: excluding {len(excl):,} flagged point(s).")

        where  = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query  = f"SELECT * FROM {table} {where} ORDER BY start_ns"
        cursor = conn.execute(query, params)
        fieldnames = [d[0] for d in cursor.description]

        out_file = output_dir / f"{table}_{suffix}.csv"
        written  = 0
        with open(out_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            while True:
                chunk = cursor.fetchmany(10_000)
                if not chunk:
                    break
                writer.writerows([dict(zip(fieldnames, row)) for row in chunk])
                written += len(chunk)

        if written == 0:
            out_file.unlink(missing_ok=True)
            print(f"  [CSV] {table}: no data for the specified filters.")
            continue

        print(f"  [CSV] {table} → {out_file}  ({written:,} rows)")

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
        # Bug fix: use COALESCE(value_fp, value_int) so that integer-only types
        # (step_count, heart_rate.bpm, etc.) report meaningful statistics instead
        # of null. The parser correctly stores those values in value_int.
        rows = conn.execute(f"""
            SELECT
                data_type, source_app,
                COUNT(*)                                              AS n,
                MIN(start_dt)                                         AS first_dt,
                MAX(start_dt)                                         AS last_dt,
                AVG(COALESCE(value_fp, CAST(value_int AS REAL)))      AS mean_v,
                MIN(COALESCE(value_fp, CAST(value_int AS REAL)))      AS min_v,
                MAX(COALESCE(value_fp, CAST(value_int AS REAL)))      AS max_v,
                SUM(CASE WHEN value_fp  IS NOT NULL THEN 1 ELSE 0 END) AS n_fp,
                SUM(CASE WHEN value_int IS NOT NULL THEN 1 ELSE 0 END) AS n_int,
                SUM(CASE WHEN value_fp IS NULL AND value_int IS NULL
                          THEN 1 ELSE 0 END)                          AS n_no_value
            FROM {table}
            GROUP BY data_type, source_app
            ORDER BY data_type, n DESC
        """).fetchall()

        summary["tables"][table] = {
            "total_rows": total_rows,
            "data_types": [
                {
                    "data_type":   r[0],
                    "source_app":  r[1],
                    "n":           r[2],
                    "first_dt":    r[3],
                    "last_dt":     r[4],
                    "mean":        round(r[5], 4) if r[5] is not None else None,
                    "min":         r[6],
                    "max":         r[7],
                    "n_fp":        r[8],   # rows with float value
                    "n_int":       r[9],   # rows with integer value
                    "n_no_value":  r[10],  # rows with neither (event-only markers)
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
        SELECT
            column_name,
            COUNT(*)                     AS n,
            MIN(NULLIF(date, ''))        AS first_date,
            MAX(NULLIF(date, ''))        AS last_date
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
    """Coordinate all parsers and write the database.

    The actual Takeout subfolder names come from file_path_config.txt.
    This is important because Google localizes these folder names according
    to the language selected for the Takeout export.
    """
    try:
        cfg = get_config()
    except (FileNotFoundError, ValueError) as e:
        print(f"\n  ❌ Configuration error: {e}")
        return

    folders = {
        cfg["ALL_DATA_FOLDER"]:               fit_folder / str(cfg["ALL_DATA_FOLDER"]),
        cfg["ALL_SESSIONS_FOLDER"]:           fit_folder / str(cfg["ALL_SESSIONS_FOLDER"]),
        cfg["ACTIVITIES_FOLDER"]:             fit_folder / str(cfg["ACTIVITIES_FOLDER"]),
        cfg["DAILY_ACTIVITY_METRICS_FOLDER"]: fit_folder / str(cfg["DAILY_ACTIVITY_METRICS_FOLDER"]),
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
            if name == cfg["ALL_DATA_FOLDER"]:
                yield from parse_all_data(path)
            elif name == cfg["ALL_SESSIONS_FOLDER"]:
                yield from parse_sessions(path)
            elif name == cfg["ACTIVITIES_FOLDER"]:
                yield from parse_activities(path)
            elif name == cfg["DAILY_ACTIVITY_METRICS_FOLDER"]:
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

    try:
        cfg = get_config()
    except (FileNotFoundError, ValueError) as e:
        print(f"\n  ❌ Configuration error: {e}")
        return

#    fit_path = cfg["FIT_FOLDER"]
    fit_folder = cfg["FIT_FOLDER"]
    db_path = cfg["DATABASE"]
    print(f"  Fit folder : {fit_folder}")
    print(f"  Output DB  : {db_path}")

    if not fit_folder.exists():
        print(f"\n  ❌ Path does not exist: {fit_folder}")
        return

    if db_path.exists():
        ow = ask(f"  ⚠  {db_path} already exists. Overwrite? [y/N]", "N").upper()
        if ow != "Y":
            return
        db_path.unlink()
        print(f"  Deleted existing {db_path}")

    print()
    run_parse(fit_folder, db_path)


def menu_export_csv() -> None:
    print("\n" + "─" * 60)
    print("  EXPORT CSV FROM DATABASE")
    print("─" * 60)

    try:
        cfg = get_config()
    except (FileNotFoundError, ValueError) as e:
        print(f"\n  ❌ Configuration error: {e}")
        return

    db_path = cfg["DATABASE"]
    out_dir = cfg["CSV_EXPORT_DIR"]

    if not db_path.exists():
        print(f"\n  ❌ Does not exist: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    types_raw = [r[0] for r in conn.execute(
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
            indices = [int(x.strip()) - 1 for x in sel.split(",")]
            selected_types = [types_raw[i] for i in indices if 0 <= i < len(types_raw)]
            print(f"  Selected: {selected_types}")
        except (ValueError, IndexError):
            print("  ⚠  Invalid selection, exporting all.")

    from_dt = ask("From date (YYYY-MM-DD, or ENTER for no limit)") or None
    to_dt = ask("To date   (YYYY-MM-DD, or ENTER for no limit)") or None

    print("\n  Tables to export:")
    print("    1)  fit_raw only")
    print("    2)  fit_derived only")
    print("    3)  Both (default)")
    sel_t = ask("Option", "3")
    tables = {"1": ["fit_raw"], "2": ["fit_derived"]}.get(
        sel_t, ["fit_raw", "fit_derived"]
    )

    print(f"  Output directory: {out_dir}")
    apply_excl_str = ask("Apply outlier exclusions? [Y/n]", "Y").upper()
    apply_excl = apply_excl_str != "N"

    print()
    export_csv(
        db_path=db_path,
        output_dir=out_dir,
        data_types=selected_types,
        from_dt=from_dt,
        to_dt=to_dt,
        tables=tables,
        apply_exclusions=apply_excl,
    )


def menu_summary() -> None:
    print("\n" + "─" * 60)
    print("  GENERATE SUMMARY JSON")
    print("─" * 60)

    try:
        cfg = get_config()
    except (FileNotFoundError, ValueError) as e:
        print(f"\n  ❌ Configuration error: {e}")
        return

    db_path = cfg["DATABASE"]
    out_path = cfg["SUMMARY_JSON"]

    if not db_path.exists():
        print(f"\n  ❌ Does not exist: {db_path}")
        return

    print(f"  Database : {db_path}")
    print(f"  Summary  : {out_path}")
    generate_summary(db_path, out_path)


def create_clean_db(source_db: Path, output_db: Path) -> None:
    """
    Create a new SQLite database that is a physical copy of the source DB
    with all rows in fit_excluded_points permanently removed from the data
    tables. The exclusion table itself is NOT copied to the output.

    This is NOT a destructive operation on the source — source_db is never
    modified. The result is a clean, self-contained DB suitable for external
    analysis or sharing, where excluded points are simply absent.

    Steps:
      1. Read all excluded (table, row_id) pairs from source.
      2. Copy schema to output.
      3. Stream each table row by row, skipping excluded IDs.
      4. Rebuild indexes.
    """
    # Safety check FIRST: prevent accidental self-overwrite.
    # Must happen before unlink() so the source is never deleted.
    if source_db.resolve() == output_db.resolve():
        print("  ❌ Source and output paths are the same file. Aborting.")
        return

    if output_db.exists():
        print(f"  ⚠  Output file already exists: {output_db}")
        overwrite = ask("Overwrite? [y/N]", "N").upper()
        if overwrite != "Y":
            return
        output_db.unlink()

    src  = sqlite3.connect(str(source_db))
    dst  = sqlite3.connect(str(output_db))

    dst.execute("PRAGMA journal_mode=WAL")
    dst.execute("PRAGMA synchronous=NORMAL")
    dst.execute("PRAGMA cache_size=-65536")

    # Load excluded IDs per table
    excluded: dict[str, set] = {}
    try:
        rows = src.execute(
            "SELECT table_name, row_id FROM fit_excluded_points"
        ).fetchall()
        for tbl, rid in rows:
            excluded.setdefault(tbl, set()).add(rid)
        total_excl = sum(len(v) for v in excluded.values())
        print(f"\n  Excluded points to remove: {total_excl:,}")
    except sqlite3.OperationalError:
        print("  No fit_excluded_points table found — output will be identical to source.")

    # Tables to copy (skip the exclusion table itself)
    DATA_TABLES = [
        "fit_raw", "fit_derived", "fit_sessions",
        "fit_activities", "fit_daily_aggregates",
    ]

    dst.executescript(SCHEMA)  # create tables and indexes

    t0    = time.monotonic()
    total = 0

    for table in DATA_TABLES:
        exists = src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue

        excl_set = excluded.get(table, set())
        n_src    = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        n_skip   = len(excl_set)
        n_copy   = n_src - n_skip

        print(f"\n  → {table}: {n_src:,} rows  |  skipping {n_skip:,}  |  copying {n_copy:,}")
        bar = ProgressBar(max(n_src, 1), table)

        cursor = src.execute(f"SELECT * FROM {table}")
        cols   = [d[0] for d in cursor.description]
        ph     = ",".join("?" * len(cols))
        insert = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})"

        batch = []
        written = 0
        for row in cursor:
            bar.update()
            row_id = row[0]  # first column is always `id`
            if row_id in excl_set:
                continue
            batch.append(row)
            if len(batch) >= 5_000:
                dst.executemany(insert, batch)
                dst.commit()
                written += len(batch)
                batch = []

        if batch:
            dst.executemany(insert, batch)
            dst.commit()
            written += len(batch)

        bar.close(f"✓  ({written:,} written)")
        total += written

    elapsed = time.monotonic() - t0
    size_mb = output_db.stat().st_size / (1024 * 1024)
    src.close()
    dst.close()

    print(f"\n  ✅ Clean DB created.")
    print(f"     Rows written : {total:,}")
    print(f"     Time         : {elapsed:.1f}s")
    print(f"     Size         : {size_mb:.1f} MB")
    print(f"     Path         : {output_db.resolve()}")


def menu_clean_db() -> None:
    print("\n" + "─" * 60)
    print("  CREATE CLEAN DB (excluded outliers permanently removed)")
    print("─" * 60)
    print()
    print("  This creates a NEW database with flagged outliers physically")
    print("  absent. The source database is NOT modified.")
    print()

    try:
        cfg = get_config()
    except (FileNotFoundError, ValueError) as e:
        print(f"\n  ❌ Configuration error: {e}")
        return

    src_path = cfg["DATABASE"]
    dst_path = cfg["CLEAN_DATABASE"]

    if not src_path.exists():
        print(f"\n  ❌ Not found: {src_path}")
        return

    print(f"  Source DB : {src_path}")
    print(f"  Clean DB  : {dst_path}")
    create_clean_db(src_path, dst_path)


def run_menu() -> None:
    while True:
        print("\n" + "=" * 60)
        print("  Google Fit Takeout Parser")
        print(f"  Model: Claude Sonnet 4.6  |  Date: 2026-05-16")
        print("=" * 60)
        print()
        print("  1)  Parse complete Takeout → SQLite")
        print("  2)  Export CSV from existing DB")
        print("  3)  Generate compact summary (JSON)")
        print("  4)  Create clean DB (remove flagged outliers permanently)")
        print("  5)  Exit")
        print()

        choice = ask("Option [1–5]")

        if choice == "1":
            menu_parse()
        elif choice == "2":
            menu_export_csv()
        elif choice == "3":
            menu_summary()
        elif choice == "4":
            menu_clean_db()
        elif choice == "5":
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
  python fit_takeout_parser.py parse
  python fit_takeout_parser.py export  --types body.fat.percentage weight
  python fit_takeout_parser.py summary

  Paths are read from file_path_config.txt. CLI path options may override them.
        """,
    )
    sub = p.add_subparsers(dest="command")

    s_parse = sub.add_parser("parse", help="Parse Takeout → SQLite")
    s_parse.add_argument("--input", help="Override FIT_FOLDER from file_path_config.txt")
    s_parse.add_argument("--output", help="Override DATABASE from file_path_config.txt")

    s_export = sub.add_parser("export", help="Export CSV from DB")
    s_export.add_argument("--db",         help="Override DATABASE from file_path_config.txt")
    s_export.add_argument("--output-dir", help="Override CSV_EXPORT_DIR from file_path_config.txt")
    s_export.add_argument("--types",      nargs="+")
    s_export.add_argument("--from-date")
    s_export.add_argument("--to-date")
    s_export.add_argument(
        "--tables", nargs="+",
        choices=["fit_raw", "fit_derived"],
        default=["fit_raw", "fit_derived"]
    )

    s_sum = sub.add_parser("summary", help="Generate summary JSON")
    s_sum.add_argument("--db",     help="Override DATABASE from file_path_config.txt")
    s_sum.add_argument("--output", help="Override SUMMARY_JSON from file_path_config.txt")

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

        try:
            cfg = get_config()
        except (FileNotFoundError, ValueError) as e:
            print(f"\n❌ Configuration error: {e}")
            sys.exit(1)

        if args.command == "parse":
            fit_folder = Path(args.input).expanduser().resolve() if args.input else cfg["FIT_FOLDER"]
            db_path = Path(args.output).expanduser().resolve() if args.output else cfg["DATABASE"]
            run_parse(fit_folder, db_path)
        elif args.command == "export":
            db_path = Path(args.db).expanduser().resolve() if args.db else cfg["DATABASE"]
            output_dir = (
                Path(args.output_dir).expanduser().resolve()
                if args.output_dir else cfg["CSV_EXPORT_DIR"]
            )
            export_csv(
                db_path=db_path,
                output_dir=output_dir,
                data_types=args.types,
                from_dt=args.from_date,
                to_dt=args.to_date,
                tables=args.tables,
            )
        elif args.command == "summary":
            db_path = Path(args.db).expanduser().resolve() if args.db else cfg["DATABASE"]
            output_path = (
                Path(args.output).expanduser().resolve()
                if args.output else cfg["SUMMARY_JSON"]
            )
            generate_summary(db_path, output_path)
        else:
            build_arg_parser().print_help()
