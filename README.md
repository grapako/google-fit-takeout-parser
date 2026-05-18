# google-fit-takeout-parser

Parse your **Google Fit Takeout** export into a lossless SQLite database, with CSV export, summary generation, and an interactive data explorer.

If you've collected years of health data through Google Fit — steps, heart rate, body composition, GPS workouts — and want to actually use it, this tool is for you. No information is lost in the process. You decide what to simplify later.

---

## Two tools

### `fit_takeout_parser.py` — CLI parser

Converts the Takeout export into SQLite. Interactive menu + CLI mode.

```
  1)  Parse complete Takeout → SQLite
  2)  Export CSV from existing DB
  3)  Generate compact summary (JSON)
  4)  Create clean DB (remove flagged outliers permanently)
  5)  Exit
```

### `fit_explorer.py` — Streamlit dashboard

Interactive visualization of your health data.

**Data sources (switchable in the sidebar):**
- **SQLite database** — full functionality, persistent outlier exclusions
- **CSV files** — load CSVs exported by the parser or the explorer directly (session-only)

**Features:**
- Date range filter controls which metrics appear — no empty columns shown
- Multi-series overlay with shared or independent Y axes
- Line, Area, Bar, Scatter chart types
- Daily, weekly, monthly aggregation
- 7-day rolling trend overlay
- Statistics panel (n, mean, median, min, max per metric)
- Manual outlier flagging — hide points from plots without deleting from DB
- Exclusion management — restore flagged points at any time
- CSV export (excluded outliers omitted, clearly noted)
- Create clean DB — produce a new SQLite with excluded rows physically absent

---

## What gets parsed

| Source folder | Table(s) | Resolution |
|---|---|---|
| `All data/` | `fit_raw` + `fit_derived` | nanoseconds (maximum) |
| `All sessions/` | `fit_sessions` | milliseconds |
| `Activities/` | `fit_activities` | per trackpoint (GPS + HR + speed + cadence + power) |
| `Daily activity metrics/` | `fit_daily_aggregates` | ~15 min (aggregated by Google, stored as-is) |

`fit_raw` and `fit_derived` are kept separate so you can detect inconsistencies between what each app wrote and what Google merged.

---

## Requirements

Python 3.10+

```bash
# Parser
pip install orjson          # optional but ~5–10x faster JSON parsing

# Explorer
pip install streamlit plotly pandas
```

All parser dependencies except `orjson` are Python stdlib.

---

## Usage

### Parser

```bash
# Interactive menu (recommended)
python fit_takeout_parser.py

# CLI
python fit_takeout_parser.py parse   --input /path/to/Takeout/Fit
python fit_takeout_parser.py export  --db fit_historical.db --types body.fat.percentage weight
python fit_takeout_parser.py summary --db fit_historical.db --output fit_summary.json
```

### Explorer

```bash
streamlit run fit_explorer.py
```

---

## Recommended workflow

```
1. fit_takeout_parser.py → parse     → fit_historical.db
2. fit_takeout_parser.py → summary   → fit_summary.json   (inspect what's available)
3. fit_explorer.py       → explore, flag outliers
4. fit_explorer.py       → Create clean DB → fit_clean.db
5. fit_explorer.py       → export CSV for specific metrics / date ranges
```

---

## Database schema

```sql
fit_raw               -- data written directly by each app (max fidelity, nanoseconds)
fit_derived           -- data calculated/merged by Google
fit_sessions          -- workout session metadata
fit_activities        -- GPS trackpoints + HR per second (from TCX files)
fit_daily_aggregates  -- pre-aggregated by Google (~15 min, informational)
fit_excluded_points   -- outliers flagged in the explorer (created on first use)
```

All tables indexed on `(data_type, start_ns)` for fast time-series queries.

### Example queries

```python
import sqlite3, pandas as pd

conn = sqlite3.connect("fit_historical.db")

# Body fat over time (Xiaomi source)
df = pd.read_sql("""
    SELECT start_dt, COALESCE(value_fp, value_int) AS body_fat_pct
    FROM fit_raw
    WHERE data_type = 'body.fat.percentage'
      AND source_app = 'com.xiaomi.hm.health'
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
1. JSON files that cannot be parsed (logged as warnings)
2. Data points where both `startTimeNanos` and `endTimeNanos` are `0`
3. Data points with non-parseable timestamps

Nothing else is filtered, aggregated, or dropped.

---

## Getting your Takeout export

1. Go to [takeout.google.com](https://takeout.google.com)
2. Deselect all → select only **Fit**
3. Export and download the ZIP
4. Unzip → find the `Fit/` folder inside `Takeout/`

---

## Credits

Designed by **Juan I. Peralta** ([@grapako](https://github.com/grapako)) and generated with the assistance of **Claude Sonnet 4.6** ([Anthropic](https://www.anthropic.com)), as part of a personal health data pipeline project. Architecture, requirements, and design decisions are the author's own.

---

## License

MIT © Juan I. Peralta — see [LICENSE](LICENSE).
