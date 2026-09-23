import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        coin_count INTEGER DEFAULT 0,
        notes TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_runs_date ON runs(run_date, id)",

    """CREATE TABLE IF NOT EXISTS coins (
        coin_id TEXT PRIMARY KEY,
        symbol TEXT, name TEXT, image TEXT,
        categories TEXT,            -- json array
        github_repos TEXT,          -- json array of "owner/repo"
        contract_addresses TEXT,    -- json object platform -> address
        defillama_slug TEXT,
        homepage TEXT,
        exchange_count INTEGER,
        metadata_updated_at TEXT
    )""",

    """CREATE TABLE IF NOT EXISTS universe_snapshots (
        run_id INTEGER NOT NULL REFERENCES runs(id),
        coin_id TEXT NOT NULL,
        market_cap_rank INTEGER,
        market_cap REAL, price_usd REAL, total_volume REAL,
        symbol TEXT, name TEXT, image TEXT,
        included INTEGER NOT NULL DEFAULT 1,
        exclusion_reason TEXT,
        PRIMARY KEY (run_id, coin_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_univ_run ON universe_snapshots(run_id, included)",
    "CREATE INDEX IF NOT EXISTS ix_univ_coin ON universe_snapshots(coin_id, run_id)",

    """CREATE TABLE IF NOT EXISTS metrics (
        run_id INTEGER NOT NULL REFERENCES runs(id),
        coin_id TEXT NOT NULL,
        metric_key TEXT NOT NULL,
        current REAL, prior REAL, change_pct REAL,
        momentum REAL, rank_score REAL,
        PRIMARY KEY (run_id, coin_id, metric_key)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_metrics_run ON metrics(run_id, metric_key)",
    "CREATE INDEX IF NOT EXISTS ix_metrics_coin ON metrics(coin_id, metric_key, run_id)",

    """CREATE TABLE IF NOT EXISTS scores (
        run_id INTEGER NOT NULL REFERENCES runs(id),
        coin_id TEXT NOT NULL,
        score REAL,                 -- variance-shrunk composite: what the UI ranks on
        score_raw REAL,             -- the plain renormalised weighted average, kept for debugging
        score_prev REAL, score_change REAL,
        missing TEXT,               -- json array of metric keys
        weight_coverage REAL,
        PRIMARY KEY (run_id, coin_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_scores_run ON scores(run_id, score DESC)",
    "CREATE INDEX IF NOT EXISTS ix_scores_coin ON scores(coin_id, run_id)",

    """CREATE TABLE IF NOT EXISTS github_repos (
        repo TEXT PRIMARY KEY,      -- "owner/repo"
        coin_id TEXT NOT NULL,
        stars INTEGER,
        archived INTEGER DEFAULT 0,
        last_commit_at TEXT,        -- watermark: newest commit we have stored
        checked_at TEXT,
        error TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_repos_coin ON github_repos(coin_id)",

    """CREATE TABLE IF NOT EXISTS repo_commits (
        repo TEXT NOT NULL,
        sha TEXT NOT NULL,
        author TEXT,
        committed_at TEXT NOT NULL,
        PRIMARY KEY (repo, sha)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_commits_window ON repo_commits(repo, committed_at)",

    """CREATE TABLE IF NOT EXISTS api_usage (
        host TEXT NOT NULL,
        year_month TEXT NOT NULL,   -- "YYYY-MM", UTC
        calls INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (host, year_month)
    )""",

    """CREATE TABLE IF NOT EXISTS run_errors (
        run_id INTEGER NOT NULL REFERENCES runs(id),
        coin_id TEXT, stage TEXT, message TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_errors_run ON run_errors(run_id)",
]


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI serves sync endpoints from a threadpool.
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


# Columns added after the first release; ALTER on a DB that predates them.
ADDED_COLUMNS = [("scores", "score_raw", "REAL")]


def migrate(con):
    for stmt in SCHEMA:
        con.execute(stmt)
    for table, col, decl in ADDED_COLUMNS:
        if col not in {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    con.commit()
    return con


def open_db(path):
    return migrate(connect(path))


def jload(s, default=None):
    return json.loads(s) if s else (default if default is not None else [])


def month_key(when=None):
    return (when or datetime.now(timezone.utc)).strftime("%Y-%m")


def usage_this_month(con, host, when=None):
    row = con.execute("SELECT calls FROM api_usage WHERE host=? AND year_month=?",
                      (host, month_key(when))).fetchone()
    return row["calls"] if row else 0


def add_usage(con, host, calls, when=None):
    con.execute("INSERT INTO api_usage(host,year_month,calls) VALUES(?,?,?) "
                "ON CONFLICT(host,year_month) DO UPDATE SET calls=calls+excluded.calls",
                (host, month_key(when), calls))
