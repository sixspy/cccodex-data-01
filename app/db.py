import sqlite3
from contextlib import contextmanager

from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def db_transaction():
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


_SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    seed_path TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'full' CHECK (mode IN ('full', 'incremental', 'backfill')),
    status TEXT NOT NULL DEFAULT 'draft',
    known_rev INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS crawl_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    item_code TEXT NOT NULL,
    title TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(job_id, item_key)
);
CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    rev_start INTEGER NOT NULL DEFAULT 0,
    rev_end INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    fail_summary TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS crawl_run_skips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    item_code TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
"""


def _migrate(db):
    """兼容旧库：补列 / 重建缺列的表（仅限简单情形）。"""
    job_cols = {r["name"] for r in db.execute("PRAGMA table_info(crawl_jobs)")}
    if job_cols and "mode" not in job_cols:
        db.execute("ALTER TABLE crawl_jobs ADD COLUMN mode TEXT NOT NULL DEFAULT 'full'")
    if job_cols and "known_rev" not in job_cols:
        db.execute("ALTER TABLE crawl_jobs ADD COLUMN known_rev INTEGER")

    rec_cols = {r["name"] for r in db.execute("PRAGMA table_info(crawl_records)")}
    if rec_cols and "item_code" not in rec_cols:
        db.execute("ALTER TABLE crawl_records RENAME TO crawl_records_old")
        db.executescript(_SCHEMA)
        db.execute(
            """
            INSERT INTO crawl_records (id, job_id, item_key, item_code, title,
                                       payload_json, fetched_at)
            SELECT id, job_id, item_key, item_key, title, payload_json, fetched_at
            FROM crawl_records_old
            """
        )
        db.execute("DROP TABLE crawl_records_old")


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(_SCHEMA)
        _migrate(db)
        db.commit()
