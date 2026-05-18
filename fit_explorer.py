"""
fit_explorer.py
===============
Interactive dashboard for exploring Google Fit health data.

Supports two data sources:
  1. SQLite database  (fit_historical.db or fit_clean.db)
     Full functionality including persistent outlier exclusions.
  2. CSV files exported by fit_takeout_parser or the explorer itself.
     Loaded into an in-memory SQLite for the session. Outlier exclusions
     exist for the session duration only (not persisted to disk).

Author         : Juan I. Peralta (https://github.com/grapako)
Generated with : Claude Sonnet 4.6  (https://www.anthropic.com)
Date           : 2026-05-17

Usage:
    pip install streamlit plotly pandas
    streamlit run fit_explorer.py
"""

import hashlib
import io
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Fit Explorer",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    section[data-testid="stSidebar"] { min-width: 310px; max-width: 350px; }
    div[data-testid="metric-container"] {
        background: #1e1e2e; border: 1px solid #313244;
        border-radius: 8px; padding: 10px 14px;
    }
    .section-title {
        font-size: 0.70rem; font-weight: 600;
        letter-spacing: 0.12em; text-transform: uppercase;
        color: #6c7086; margin-bottom: 2px;
    }
    .outlier-note { font-size: 0.78rem; color: #f38ba8; font-style: italic; }
    .csv-note     { font-size: 0.78rem; color: #f9e2af; font-style: italic; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# CONNECTION MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

# Minimal schema needed for the in-memory DB and for fit_excluded_points.
# Tables are created by pandas to_sql when loading CSVs; only the exclusion
# table and its index are created manually.
_EXCL_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS fit_excluded_points (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name   TEXT NOT NULL,
    row_id       INTEGER NOT NULL,
    data_type    TEXT,
    point_dt     TEXT,
    value        REAL,
    reason       TEXT,
    excluded_at  TEXT DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_excl_unique
    ON fit_excluded_points (table_name, row_id);
CREATE INDEX IF NOT EXISTS idx_excl_table_row
    ON fit_excluded_points (table_name, row_id);
"""

# Known CSV column fingerprints → table name.
# Checked in order; first match wins.
_CSV_SIGNATURES: list[tuple[set, str]] = [
    ({"start_ns", "data_type", "source_app"}, "fit_raw"),        # default for ambiguous
    ({"column_name", "value_text", "source_file"}, "fit_daily_aggregates"),
    ({"sport", "point_dt", "lat"},              "fit_activities"),
    ({"activity_type_name", "start_ms"},        "fit_sessions"),
]


def _detect_csv_table(columns: list[str]) -> str | None:
    """Infer the destination table from CSV column names."""
    col_set = set(columns)
    for required, table in _CSV_SIGNATURES:
        if required.issubset(col_set):
            return table
    return None


def _table_from_filename(name: str, detected: str) -> str:
    """Refine fit_raw vs fit_derived based on the filename convention."""
    stem = Path(name).stem.lower()
    if detected == "fit_raw":
        if stem.startswith("fit_derived"):
            return "fit_derived"
    return detected


@st.cache_resource
def get_db_conn(db_path: str) -> sqlite3.Connection:
    """Open and cache a connection to a SQLite file database."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA cache_size=-32768")
    conn.executescript(_EXCL_TABLE_DDL)
    conn.commit()
    return conn


def build_csv_conn(uploaded_files) -> tuple[sqlite3.Connection, list[dict]]:
    """
    Load uploaded CSV files into an in-memory SQLite database.

    Returns the connection and a list of load-result dicts for display.
    The connection is stored in st.session_state so it survives reruns
    without re-reading the files.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(_EXCL_TABLE_DDL)

    results = []
    for uf in uploaded_files:
        try:
            df = pd.read_csv(io.StringIO(uf.getvalue().decode("utf-8")))
        except Exception as e:
            results.append({"file": uf.name, "table": "—", "rows": 0,
                            "status": f"❌ {e}"})
            continue

        detected = _detect_csv_table(df.columns.tolist())
        if detected is None:
            results.append({"file": uf.name, "table": "unknown", "rows": len(df),
                            "status": "⚠ Could not detect table type"})
            continue

        table = _table_from_filename(uf.name, detected)
        try:
            # Keep the 'id' column if present — fetch queries use SELECT id, ...
            # and need it to exist in the table. Duplicate IDs across multiple
            # CSV files for the same table are acceptable since to_sql doesn't
            # create a UNIQUE constraint by default.
            df.to_sql(table, conn, if_exists="append", index=False)
            conn.commit()
            results.append({"file": uf.name, "table": table,
                            "rows": len(df), "status": "✅"})
        except Exception as e:
            results.append({"file": uf.name, "table": table, "rows": 0,
                            "status": f"❌ {e}"})

    return conn, results


# ══════════════════════════════════════════════════════════════════════════════
# QUERY LAYER
# conn_id is a string that uniquely identifies the active data source.
# It is passed as a plain arg (not prefixed with _) so Streamlit uses it as
# part of the cache key. This ensures cache invalidation when the user
# switches between DB and CSV, or loads different CSV files.
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=60)
def run_query(_conn, sql: str, params: tuple = (), conn_id: str = "") -> pd.DataFrame:
    return pd.read_sql(sql, _conn, params=params)


VALUE_COL = "COALESCE(value_fp, CAST(value_int AS REAL))"

AGGREGATION_OPTIONS = {
    "Raw (no aggregation)": None,
    "Daily mean":           "1D",
    "Weekly mean":          "1W",
    "Monthly mean":         "1ME",
}

CHART_COLORS = [
    "#89b4fa", "#a6e3a1", "#fab387", "#f38ba8",
    "#cba6f7", "#94e2d5", "#f9e2af", "#89dceb",
]


def unit_for(col: str) -> str:
    c = col.lower()
    if "bpm"   in c: return "bpm"
    if "kcal"  in c: return "kcal"
    if "(kg)"  in c: return "kg"
    if "(%)"   in c: return "%"
    if "(m/s)" in c: return "m/s"
    if "(m)"   in c and "mmhg" not in c: return "m"
    if "mmhg"  in c: return "mmHg"
    if "(ms)"  in c: return "hours"
    if "deg"   in c: return "°"
    if "l/min" in c: return "L/min"
    return ""


def ms_to_hours(series: pd.Series) -> pd.Series:
    return (series / 3_600_000).round(3)


@st.cache_data(ttl=300)
def get_daily_columns(_conn, from_str: str, to_str: str, conn_id: str) -> list:
    """
    Columns in fit_daily_aggregates that have at least one numeric value
    within the selected date range, sorted alphabetically.
    Only columns with data in the chosen period appear in the selector.
    """
    df = run_query(_conn, """
        SELECT column_name, COUNT(*) AS n
        FROM fit_daily_aggregates
        WHERE (value_fp IS NOT NULL
               OR (value_text IS NOT NULL AND value_text != ''))
          AND column_name NOT IN ('Date', 'Start time', 'End time')
          AND COALESCE(NULLIF(date,''), substr(start_dt,1,10)) BETWEEN ? AND ?
        GROUP BY column_name
        HAVING n > 0
        ORDER BY column_name
    """, (from_str, to_str), conn_id=conn_id)
    return df["column_name"].tolist()


@st.cache_data(ttl=300)
def get_raw_types(_conn, from_str: str, to_str: str, conn_id: str) -> list:
    """
    Data types in fit_raw with at least one numeric value in the selected
    date range, sorted alphabetically.
    """
    try:
        df = run_query(_conn, f"""
            SELECT data_type
            FROM fit_raw
            WHERE {VALUE_COL} IS NOT NULL
              AND substr(start_dt, 1, 10) BETWEEN ? AND ?
            GROUP BY data_type
            HAVING COUNT(*) > 0
            ORDER BY data_type
        """, (from_str, to_str), conn_id=conn_id)
        return df["data_type"].tolist()
    except Exception:
        return []


@st.cache_data(ttl=30)
def get_excluded_ids(_conn, table: str, conn_id: str) -> frozenset:
    """
    Row IDs flagged as outliers for a given table.
    Cached for 30 s; conn_id ensures invalidation on source switch.
    """
    try:
        rows = _conn.execute(
            "SELECT row_id FROM fit_excluded_points WHERE table_name = ?", (table,)
        ).fetchall()
        return frozenset(r[0] for r in rows)
    except Exception:
        return frozenset()


# ══════════════════════════════════════════════════════════════════════════════
# DATA FETCHERS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_daily(conn, col: str, from_str: str, to_str: str, conn_id: str) -> pd.DataFrame:
    excluded = get_excluded_ids(conn, "fit_daily_aggregates", conn_id)
    excl_cl  = f"AND id NOT IN ({','.join(str(i) for i in excluded)})" if excluded else ""
    return run_query(conn, f"""
        SELECT
            id,
            COALESCE(NULLIF(date,''), substr(start_dt,1,10)) AS day,
            COALESCE(value_fp, CAST(value_text AS REAL))     AS value
        FROM fit_daily_aggregates
        WHERE column_name = ?
          AND COALESCE(value_fp, CAST(value_text AS REAL)) IS NOT NULL
          AND COALESCE(NULLIF(date,''), substr(start_dt,1,10)) BETWEEN ? AND ?
          {excl_cl}
        ORDER BY day
    """, (col, from_str, to_str), conn_id=conn_id + col)


@st.cache_data(ttl=300)
def has_start_ns(_conn, conn_id: str) -> bool:
    """Return True if fit_raw has a start_ns column (DB-sourced, not CSV-loaded)."""
    try:
        cols = [r[1] for r in _conn.execute(
            "PRAGMA table_info(fit_raw)"
        ).fetchall()]
        return "start_ns" in cols
    except Exception:
        return False


@st.cache_data(ttl=300)
def get_source_apps(_conn, data_type: str, from_str: str, to_str: str,
                    conn_id: str) -> list:
    """Return sorted list of source apps for a given data type and date range."""
    try:
        df = run_query(_conn, """
            SELECT DISTINCT source_app FROM fit_raw
            WHERE data_type = ? AND source_app != ''
              AND substr(start_dt,1,10) BETWEEN ? AND ?
            ORDER BY source_app
        """, (data_type, from_str, to_str), conn_id=conn_id + data_type)
        return df["source_app"].tolist()
    except Exception:
        return []


def fetch_raw(conn, data_type: str, from_str: str, to_str: str,
              source_app: str, value_key: str, conn_id: str) -> pd.DataFrame:
    excluded = get_excluded_ids(conn, "fit_raw", conn_id)
    excl_cl  = f"AND id NOT IN ({','.join(str(i) for i in excluded)})" if excluded else ""
    app_cl   = "AND source_app = ?" if source_app != "All" else ""
    app_p    = (source_app,)        if source_app != "All" else ()
    key_cl   = "AND value_key = ?"  if value_key  != "All" else ""
    key_p    = (value_key,)         if value_key  != "All" else ()
    order_by = "start_ns" if has_start_ns(conn, conn_id) else "day"

    return run_query(conn, f"""
        SELECT
            id,
            substr(start_dt, 1, 10) AS day,
            start_dt,
            {VALUE_COL}             AS value,
            value_key,
            source_app
        FROM fit_raw
        WHERE data_type = ?
          AND {VALUE_COL} IS NOT NULL
          AND substr(start_dt, 1, 10) BETWEEN ? AND ?
          {app_cl} {key_cl} {excl_cl}
        ORDER BY {order_by}
        LIMIT 200000
    """, (data_type, from_str, to_str) + app_p + key_p,
    conn_id=conn_id + data_type + source_app)


# ══════════════════════════════════════════════════════════════════════════════
# CHART
# ══════════════════════════════════════════════════════════════════════════════

LAYOUT_BASE = dict(
    template="plotly_dark",
    plot_bgcolor="#1e1e2e",
    paper_bgcolor="#1e1e2e",
    margin=dict(l=0, r=0, t=40, b=0),
    legend=dict(orientation="h", y=1.10, x=0),
    height=450,
)


def make_trace(x, y, name, color, chart_type, yaxis="y"):
    base = dict(x=x, y=y, name=name, yaxis=yaxis)
    if chart_type == "Bar":
        return go.Bar(**base, marker_color=color)
    kw = dict(**base, marker_color=color, line_color=color)
    if chart_type == "Area":
        return go.Scatter(**kw, mode="lines", fill="tozeroy",
                          line=dict(color=color, width=1.8), opacity=0.55)
    if chart_type == "Scatter":
        return go.Scatter(**kw, mode="markers",
                          marker=dict(size=4, color=color, opacity=0.55))
    return go.Scatter(**kw, mode="lines", line=dict(color=color, width=1.8))


def render_chart(series_list, chart_type, agg_freq, shared_y):
    fig          = go.Figure()
    layout_extra = {}
    has_data     = False

    for i, s in enumerate(series_list):
        df   = s["df"].copy()
        x, y = s["x_col"], s["y_col"]
        if df.empty:
            continue

        if agg_freq:
            try:
                df[x] = pd.to_datetime(df[x], errors="coerce")
                df     = df.dropna(subset=[x])
                df     = (df.set_index(x)[y]
                           .resample(agg_freq).mean().dropna()
                           .reset_index())
                df.columns = [x, y]
            except Exception:
                pass

        if s.get("unit") == "hours":
            df[y] = ms_to_hours(df[y])

        if df.empty:
            continue

        has_data = True
        yaxis_id = "y" if (shared_y or i == 0) else f"y{i+1}"
        label    = f"{s['label']}  [{s['unit']}]" if s.get("unit") else s["label"]

        try:
            fig.add_trace(make_trace(df[x], df[y], label, s["color"],
                                     chart_type, yaxis_id))
        except Exception as e:
            st.warning(f"Trace `{s['label']}` ({chart_type}): {e}")
            continue

        if not shared_y and i > 0:
            layout_extra[f"yaxis{i+1}"] = dict(
                title=s.get("unit", ""), overlaying="y",
                side="right", showgrid=False, color=s["color"],
            )

    if not has_data:
        st.info("No data to plot for the current selection and date range.")
        return

    fig.update_layout(**LAYOUT_BASE,
                      xaxis=dict(gridcolor="#313244"),
                      yaxis=dict(gridcolor="#313244"),
                      **layout_extra)
    st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# OUTLIER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def render_outlier_panel(conn, df, table, data_type, csv_mode: bool):
    with st.expander("🚩 Flag outliers (manual exclusion)", expanded=False):
        if csv_mode:
            st.markdown(
                '<p class="csv-note">Session-only: exclusions will be lost when '
                "you close the browser tab.</p>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<p class="outlier-note">Flagged rows are hidden from plots but '
                "never deleted from the database. Restore them any time.</p>",
                unsafe_allow_html=True,
            )
        if df.empty:
            st.info("No data to flag.")
            return

        x_col = "day" if "day" in df.columns else "start_dt"
        sort_asc = st.radio(
            "Sort",
            ["Descending (high outliers)", "Ascending (low outliers)"],
            horizontal=True, label_visibility="collapsed",
        )
        display = (df[[x_col, "value", "id"]]
                   .sort_values("value", ascending=sort_asc.startswith("Ascending"))
                   .head(50))

        st.caption("Top 50 rows by value. Check to flag:")
        selected = []
        for _, row in display.iterrows():
            c1, c2 = st.columns([0.07, 0.93])
            if c1.checkbox("", key=f"flag_{row['id']}", label_visibility="collapsed"):
                selected.append(int(row["id"]))
            c2.write(f"`{row[x_col]}` — **{round(row['value'], 4)}**")

        reason = st.text_input("Reason (optional)",
                               placeholder="e.g. measurement error, sync artefact")
        if st.button("Exclude selected", disabled=not selected):
            for rid in selected:
                conn.execute(
                    "INSERT OR IGNORE INTO fit_excluded_points "
                    "(table_name, row_id, data_type, reason) VALUES (?,?,?,?)",
                    (table, rid, data_type, reason or None),
                )
            conn.commit()
            st.cache_data.clear()
            st.success(f"Flagged {len(selected)} row(s).")
            st.rerun()


def render_manage_exclusions(conn, csv_mode: bool):
    with st.expander("🗑 Manage exclusions", expanded=False):
        try:
            df = pd.read_sql(
                "SELECT * FROM fit_excluded_points ORDER BY excluded_at DESC", conn
            )
        except Exception:
            st.info("No exclusions yet.")
            return

        if df.empty:
            st.info("No exclusions yet.")
            return

        st.dataframe(df, use_container_width=True, height=220)
        to_restore = st.multiselect(
            "Select IDs to restore", df["id"].tolist(),
            format_func=lambda i: (
                f"#{i} — {df.loc[df['id']==i,'data_type'].values[0]}"
            ),
        )
        if st.button("Restore", disabled=not to_restore):
            conn.executemany("DELETE FROM fit_excluded_points WHERE id=?",
                             [(i,) for i in to_restore])
            conn.commit()
            st.cache_data.clear()
            st.rerun()


def render_create_clean_db(conn, db_path: Path, csv_mode: bool):
    """
    Create a new SQLite file that is a copy of the current DB with all
    flagged outlier rows permanently deleted.

    Not available in CSV mode (no source file to copy from).
    Logically belongs here because outlier exclusions originate in the explorer.
    """
    with st.expander("💾 Create clean DB (outliers permanently removed)",
                     expanded=False):
        if csv_mode:
            st.info("Only available when working with a SQLite database, not CSVs.")
            return

        st.markdown(
            "Creates a **new** database file with flagged rows physically absent. "
            "The source database is **never modified**."
        )
        dst_input = st.text_input("Output path", value="fit_clean.db")
        if st.button("Create clean DB"):
            dst_path = Path(dst_input)
            if dst_path.resolve() == db_path.resolve():
                st.error("Output path is the same as the source. Choose a different name.")
                return
            try:
                # Use SQLite's backup API — safe even with open WAL connections.
                dst_conn = sqlite3.connect(str(dst_path))
                conn.backup(dst_conn)
                dst_conn.close()

                # Open the copy and delete excluded rows from each data table.
                dst_conn = sqlite3.connect(str(dst_path))
                excl = dst_conn.execute(
                    "SELECT table_name, row_id FROM fit_excluded_points"
                ).fetchall()
                n_removed = 0
                for tbl, rid in excl:
                    try:
                        dst_conn.execute(f"DELETE FROM {tbl} WHERE id=?", (rid,))
                        n_removed += 1
                    except Exception:
                        pass
                dst_conn.execute("DELETE FROM fit_excluded_points")
                dst_conn.commit()
                dst_conn.execute("VACUUM")
                dst_conn.close()

                size_mb = dst_path.stat().st_size / (1024 * 1024)
                st.success(
                    f"✅ Clean DB created: `{dst_path.resolve()}`  "
                    f"({n_removed} rows removed, {size_mb:.1f} MB)"
                )
            except Exception as e:
                st.error(f"Failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("## 📂 Data source")

    src_mode = st.radio(
        "Input type",
        ["SQLite database", "CSV file(s)"],
        label_visibility="collapsed",
    )
    csv_mode = src_mode == "CSV file(s)"
    conn_id  = ""   # set below based on active source

    st.divider()

    if not csv_mode:
        # ── SQLite mode ───────────────────────────────────────────────────────
        db_path_input = st.text_input("Path to .db file", value="fit_historical.db")
        db_path = Path(db_path_input)
        if not db_path.exists():
            st.error(f"Not found: `{db_path_input}`")
            st.stop()
        conn    = get_db_conn(str(db_path))
        conn_id = str(db_path.resolve())
        st.success("Connected ✓")

    else:
        # ── CSV mode ──────────────────────────────────────────────────────────
        st.markdown(
            '<p class="csv-note">Exclusions are session-only and will not '
            "be saved when you close the browser tab.</p>",
            unsafe_allow_html=True,
        )
        uploaded = st.file_uploader(
            "Upload CSV files (fit_raw, fit_derived, fit_daily_aggregates, …)",
            type=["csv"],
            accept_multiple_files=True,
        )
        if not uploaded:
            st.info("Upload one or more CSV files exported by the parser or explorer.")
            st.stop()

        # Compute a stable key from file names + sizes to detect changes.
        file_key = "_".join(
            sorted(f"{uf.name}:{uf.size}" for uf in uploaded)
        )
        conn_id = hashlib.md5(file_key.encode()).hexdigest()[:12]

        # Rebuild the in-memory DB only when the file set changes.
        if st.session_state.get("csv_conn_id") != conn_id:
            csv_conn, load_results = build_csv_conn(uploaded)
            st.session_state["csv_conn"]    = csv_conn
            st.session_state["csv_conn_id"] = conn_id
            st.session_state["csv_load_log"] = load_results

        conn     = st.session_state["csv_conn"]
        db_path  = Path("(in-memory)")

        # Show load summary
        log = st.session_state.get("csv_load_log", [])
        for entry in log:
            color = "#a6e3a1" if entry["status"].startswith("✅") else "#f38ba8"
            st.markdown(
                f"<span style='color:{color}'>{entry['status']}</span> "
                f"`{entry['file']}` → **{entry['table']}** ({entry['rows']:,} rows)",
                unsafe_allow_html=True,
            )

    st.divider()

    # ── Data table ────────────────────────────────────────────────────────────
    st.markdown('<p class="section-title">Data table</p>', unsafe_allow_html=True)
    use_daily = st.radio(
        "Table",
        ["Daily aggregates (cleaner)", "Raw data (max resolution)"],
        label_visibility="collapsed",
    ).startswith("Daily")
    st.divider()

    # ── Date range (before metrics — controls which metrics are available) ────
    st.markdown('<p class="section-title">Date range</p>', unsafe_allow_html=True)
    presets = {
        "Last 30 days": 30, "Last 90 days": 90,
        "Last 6 months": 180, "Last year": 365,
        "All time": 0, "Custom": -1,
    }
    preset = st.selectbox("Quick range", list(presets.keys()), index=2)
    today  = datetime.today().date()
    if preset == "Custom":
        from_date = st.date_input("From", value=today - timedelta(days=180))
        to_date   = st.date_input("To",   value=today)
    elif presets[preset] == 0:
        from_date, to_date = today - timedelta(days=3650), today
    else:
        from_date, to_date = today - timedelta(days=presets[preset]), today

    from_str = from_date.strftime("%Y-%m-%d")
    to_str   = to_date.strftime("%Y-%m-%d")
    st.divider()

    # ── Metrics (only those with data in the selected date range) ─────────────
    st.markdown('<p class="section-title">Metrics — select 1 or more</p>',
                unsafe_allow_html=True)

    selected_apps = ["All (merged)"]
    selected_key  = "All"

    if use_daily:
        all_cols = get_daily_columns(conn, from_str, to_str, conn_id)
        if not all_cols:
            st.error(
                "No numeric columns found in the selected date range.\n\n"
                "Try expanding the range or switching to 'All time'."
            )
            st.stop()
        selected_metrics = st.multiselect(
            "Columns", all_cols,
            default=[all_cols[0]] if all_cols else [],
            help=f"{len(all_cols)} columns with data in this range.",
        )
    else:
        all_types = get_raw_types(conn, from_str, to_str, conn_id)
        if not all_types:
            st.warning(
                "No numeric data found in fit_raw for this date range.\n\n"
                "Try 'All time', or re-generate the database with the latest parser."
            )
            st.stop()
        selected_metrics = st.multiselect(
            "Data types", all_types,
            default=[all_types[0]] if all_types else [],
            help=f"{len(all_types)} types with data in this range.",
        )
        if selected_metrics:
            apps = get_source_apps(conn, selected_metrics[0], from_str, to_str, conn_id)
            ALL_MERGED = "All (merged)"
            selected_apps = st.multiselect(
                "Source apps",
                [ALL_MERGED] + apps,
                default=[ALL_MERGED],
                help=(
                    f"'{ALL_MERGED}' plots all sources as one series. "
                    "Select individual apps to plot each as a separate series."
                ),
            )
            if not selected_apps:
                selected_apps = [ALL_MERGED]

            keys = run_query(conn, """
                SELECT DISTINCT value_key FROM fit_raw
                WHERE data_type = ? AND value_key IS NOT NULL
                  AND substr(start_dt,1,10) BETWEEN ? AND ?
                ORDER BY value_key
            """, (selected_metrics[0], from_str, to_str),
            conn_id=conn_id)["value_key"].tolist()
            selected_key = (st.selectbox("Value key", ["All"] + keys)
                            if keys else "All")

    st.divider()

    # ── Chart options ─────────────────────────────────────────────────────────
    st.markdown('<p class="section-title">Chart options</p>', unsafe_allow_html=True)
    chart_type = st.selectbox("Type", ["Line", "Area", "Bar", "Scatter"])
    agg_label  = st.selectbox("Aggregation", list(AGGREGATION_OPTIONS.keys()), index=1)
    agg_freq   = AGGREGATION_OPTIONS[agg_label]
    shared_y   = True
    if len(selected_metrics) > 1:
        shared_y = st.checkbox(
            "Shared Y axis", value=True,
            help="Uncheck for independent Y axes — useful when mixing units.",
        )
    show_stats     = st.checkbox("Statistics",      value=True)
    show_raw_table = st.checkbox("Data table",      value=False)
    show_ma        = st.checkbox("Moving average",  value=False)
    ma_window      = 7
    if show_ma:
        MA_PRESETS = {"7": 7, "24": 24, "30": 30, "100": 100, "Custom": -1}
        ma_preset  = st.selectbox("Window (points)", list(MA_PRESETS.keys()), index=0,
                                  help="N-point moving average, like MA20/MA55 in trading. "
                                       "Window = number of consecutive data points, "
                                       "independent of time gaps between them.")
        if ma_preset == "Custom":
            ma_window = st.number_input("Custom window", min_value=2,
                                        max_value=500, value=7, step=1)
        else:
            ma_window = MA_PRESETS[ma_preset]


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AREA
# ══════════════════════════════════════════════════════════════════════════════

if not selected_metrics:
    st.info("Select at least one metric in the sidebar.")
    st.stop()

# Banner for CSV mode
if csv_mode:
    st.info(
        "📄 **CSV mode** — data loaded from uploaded file(s). "
        "Outlier exclusions are session-only and will not be saved to disk."
    )

# Build series list
# In daily mode: one series per metric.
# In raw mode: one series per (metric × source_app), unless "All (merged)" is selected
# in which case all sources for that metric are collapsed into one series.
ALL_MERGED = "All (merged)"
series_list  = []
color_cursor = 0  # advances globally so colors never repeat across metric+app combos

for metric in selected_metrics:
    if use_daily:
        df   = fetch_daily(conn, metric, from_str, to_str, conn_id)
        unit = unit_for(metric)
        tbl  = "fit_daily_aggregates"
        series_list.append(dict(
            df=df, x_col="day", y_col="value",
            label=metric, unit=unit,
            color=CHART_COLORS[color_cursor % len(CHART_COLORS)],
            table=tbl, data_type=metric,
        ))
        color_cursor += 1
    else:
        # Expand source apps into separate series when individual apps are selected.
        if ALL_MERGED in selected_apps or not selected_apps:
            # Merged: fetch all apps together (source_app = "All")
            df = fetch_raw(conn, metric, from_str, to_str,
                           "All", selected_key, conn_id)
            series_list.append(dict(
                df=df, x_col="day", y_col="value",
                label=metric, unit="",
                color=CHART_COLORS[color_cursor % len(CHART_COLORS)],
                table="fit_raw", data_type=metric,
            ))
            color_cursor += 1
        else:
            # One series per selected app
            for app in selected_apps:
                df = fetch_raw(conn, metric, from_str, to_str,
                               app, selected_key, conn_id)
                # Short label: metric + last segment of app package name
                app_short = app.split(".")[-1] if "." in app else app
                label = f"{metric} [{app_short}]"
                series_list.append(dict(
                    df=df, x_col="day", y_col="value",
                    label=label, unit="",
                    color=CHART_COLORS[color_cursor % len(CHART_COLORS)],
                    table="fit_raw", data_type=metric,
                ))
                color_cursor += 1

# Title
title = " + ".join(selected_metrics[:3])
if len(selected_metrics) > 3:
    title += f" +{len(selected_metrics)-3} more"
st.markdown(f"### {title}  ·  {from_str} → {to_str}")

# Statistics
if show_stats:
    stat_cols = st.columns(min(len(series_list), 4))
    for i, s in enumerate(series_list):
        df  = s["df"]
        col = stat_cols[i % len(stat_cols)]
        if df.empty:
            col.metric(s["label"][:30], "no data")
        else:
            v = df["value"]
            col.metric(
                s["label"][:30],
                f"{v.mean():.2f} {s['unit']}",
                help=(f"n={len(df):,} | min={v.min():.2f} | "
                      f"max={v.max():.2f} | median={v.median():.2f}"),
            )
    st.write("")

# Main chart
render_chart(series_list, chart_type, agg_freq, shared_y)

# Row limit warning (raw mode)
if not use_daily:
    for s in series_list:
        if len(s["df"]) == 200_000:
            st.warning(
                f"⚠ 200k row limit for `{s['label']}`. Narrow the date range."
            )

# Moving average overlay
if show_ma:
    with st.expander(f"Moving average (MA{ma_window})", expanded=True):
        fig_ma   = go.Figure()
        has_data = False

        for s in series_list:
            df = s["df"].copy()
            if df.empty:
                continue

            x_col = "day" if "day" in df.columns else "start_dt"

            try:
                # Sort by time, then apply integer rolling — same logic as
                # MA20/MA55/MA200 in trading: window = N consecutive observations,
                # regardless of time gaps between them. No DatetimeIndex needed.
                df = df.sort_values(x_col).reset_index(drop=True)
                df["ma"] = df["value"].rolling(ma_window, min_periods=1).mean()
            except Exception as e:
                st.warning(f"MA failed for `{s['label']}`: {e}")
                continue

            if s.get("unit") == "hours":
                df["value"] = ms_to_hours(df["value"])
                df["ma"]    = ms_to_hours(df["ma"])

            has_data = True
            # Raw points: low-opacity scatter
            fig_ma.add_trace(go.Scatter(
                x=df[x_col], y=df["value"],
                mode="markers",
                name=f"{s['label']} (raw)",
                marker=dict(size=3, color=s["color"], opacity=0.25),
                showlegend=True,
            ))
            # MA line
            fig_ma.add_trace(go.Scatter(
                x=df[x_col], y=df["ma"],
                mode="lines",
                name=f"{s['label']} MA{ma_window}",
                line=dict(color=s["color"], width=2),
            ))

        if has_data:
            fig_ma.update_layout(
                **LAYOUT_BASE, height=400,
                xaxis=dict(gridcolor="#313244"),
                yaxis=dict(gridcolor="#313244"),
            )
            st.plotly_chart(fig_ma, use_container_width=True)
        else:
            st.info("No data for moving average in the selected range.")

# Data table + CSV download
if show_raw_table:
    st.divider()
    for s in series_list:
        df = s["df"]
        if df.empty:
            continue
        st.markdown(f'<p class="section-title">{s["label"]}</p>',
                    unsafe_allow_html=True)
        display = df.copy()
        display["value"] = display["value"].round(4)
        st.dataframe(display, use_container_width=True, height=240)

        # Count exclusions specific to this data_type
        try:
            excl_n = conn.execute(
                "SELECT COUNT(*) FROM fit_excluded_points "
                "WHERE table_name=? AND data_type=?",
                (s["table"], s["data_type"]),
            ).fetchone()[0]
        except Exception:
            excl_n = 0

        note = f" *(outliers excluded: {excl_n})*" if excl_n else ""
        st.caption(f"Rows: {len(display):,}{note}")
        st.download_button(
            f"⬇ CSV — {s['label']}" + (" (excl. outliers)" if excl_n else ""),
            data=display.to_csv(index=False).encode("utf-8"),
            file_name=(f"fit_{s['label'].replace(' ','_')}_"
                       f"{datetime.now():%Y%m%d_%H%M%S}.csv"),
            mime="text/csv",
        )

# Outlier panels
st.divider()
if len(series_list) == 1:
    s = series_list[0]
    render_outlier_panel(conn, s["df"], s["table"], s["data_type"], csv_mode)
else:
    choice = st.selectbox("Flag outliers in:", [s["label"] for s in series_list])
    s = next(x for x in series_list if x["label"] == choice)
    render_outlier_panel(conn, s["df"], s["table"], s["data_type"], csv_mode)

render_manage_exclusions(conn, csv_mode)
render_create_clean_db(conn, db_path, csv_mode)

st.divider()
st.markdown(
    '<p style="color:#6c7086;font-size:0.74rem;">'
    'fit_explorer.py — Juan I. Peralta · Claude Sonnet 4.6 · 2026-05-17'
    '</p>',
    unsafe_allow_html=True,
)
