# google-fit-takeout-parser

Parse your **Google Fit Takeout** export into a lossless SQLite database, with CSV export and summary generation.

If you've collected years of health data through Google Fit — steps, heart rate, body composition, GPS workouts — and want to actually use it, this tool is for you. No information is lost in the process. You decide what to simplify later.

---

## What it does

Google Fit's Takeout export is a mess of thousands of JSON and TCX files across four folders. This script reads all of them and consolidates everything into a single **SQLite database** with clean indexes, preserving full nanosecond resolution.

| Source folder | Table(s) | Resolution |
|---|---|---|
| `All data/` | `fit_raw` + `fit_derived` | nanoseconds (maximum) |
| `All sessions/` | `fit_sessions` | milliseconds |
| `Activities/` | `fit_activities` | per trackpoint (GPS + HR + speed + cadence + power) |
| `Daily activity metrics/` | `fit_daily_aggregates` | ~15 min (already aggregated by Google, stored as-is) |

`fit_raw` and `fit_derived` are kept as separate tables so you can detect inconsistencies or corrections between what each app wrote and what Google merged.

---

## Requirements

- Python 3.10+
- [`orjson`](https://github.com/ijl/orjson) *(optional but recommended — ~5–10× faster JSON parsing)*

```bash
pip install orjson
```

All other dependencies are Python stdlib: `sqlite3`, `pathlib`, `csv`, `xml.etree.ElementTree`.

---

## Usage

### Interactive menu (recommended)

```bash
python fit_takeout_parser.py
```

You'll get a numbered menu:

```
  1)  Parse complete Takeout → SQLite
  2)  Export CSV from existing DB
  3)  Generate compact summary (JSON)
  4)  Exit
```

The script will ask for paths and filters step by step. Recommended order: **1 → 3 → 2**.

### CLI mode

```bash
# Parse the full Takeout
python fit_takeout_parser.py parse --input /path/to/Takeout/Fit

# Export selected types as CSV
python fit_takeout_parser.py export \
    --db fit_historical.db \
    --types body.fat.percentage weight heart_rate.bpm \
    --from-date 2023-01-01

# Generate a compact summary
python fit_takeout_parser.py summary \
    --db fit_historical.db \
    --output fit_summary.json
```

---

## Database schema

```sql
fit_raw               -- data written directly by each app (max fidelity)
fit_derived           -- data calculated/merged by Google
fit_sessions          -- workout session metadata
fit_activities        -- GPS trackpoints + HR per second (from TCX files)
fit_daily_aggregates  -- pre-aggregated by Google (~15 min, informational)
```

All tables are indexed on `(data_type, start_ns)` for fast time-series queries.

### Example queries

```python
import sqlite3
import pandas as pd

conn = sqlite3.connect("fit_historical.db")

# Body fat over time (from Xiaomi)
df = pd.read_sql("""
    SELECT start_dt, value_fp AS body_fat_pct
    FROM fit_raw
    WHERE data_type = 'body.fat.percentage'
    AND source_app  = 'com.xiaomi.hm.health'
    ORDER BY start_ns
""", conn)

# Heart rate during workouts
df_hr = pd.read_sql("""
    SELECT point_dt, hr_bpm, sport
    FROM fit_activities
    WHERE hr_bpm IS NOT NULL
    ORDER BY point_ts
""", conn)
```

---

## What gets discarded

Only three categories of records are skipped:
1. JSON files that cannot be parsed (logged as warnings during execution)
2. Data points where both `startTimeNanos` and `endTimeNanos` are `0` (explicit empty records from Google's API)
3. Data points with timestamps that cannot be parsed as integers

Nothing else is filtered, aggregated, or dropped.

---

## Progress bars

Each folder displays a real-time progress bar:

```
  [All data              ] ████████████░░░░░░░░░░░░░░░░░░  43.2%  8640/20000 | 1m23s elapsed | ETA 1m49s
```

---

## Getting your Takeout export

1. Go to [takeout.google.com](https://takeout.google.com)
2. Deselect all → select only **Fit**
3. Export and download the ZIP
4. Unzip → look for the `Fit/` folder inside `Takeout/`

---

## Updating your data in the future

The current scope of this tool is the **one-time historical parse**. Incremental update strategies (Health Connect backup → SQLite merge) are planned but not yet implemented.

---

## Credits

This script was designed by **Juan I. Peralta** ([@grapako](https://github.com/grapako)) and generated with the assistance of **Claude Sonnet 4.6** ([Anthropic](https://www.anthropic.com)), as part of a personal health data pipeline project. The architecture, requirements, and all design decisions are the author's own.

---

## License

MIT © Juan I. Peralta — see [LICENSE](LICENSE).
