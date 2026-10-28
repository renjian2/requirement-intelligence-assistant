"""SQLite connection and schema. Raw uploaded files are NOT stored - only extracted text."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    session_id          TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    document_id         TEXT NOT NULL,
    file_name           TEXT NOT NULL,
    extension           TEXT NOT NULL,
    document_type       TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL,
    status              TEXT NOT NULL,
    detected_transcript INTEGER NOT NULL DEFAULT 0,
    stats_json          TEXT NOT NULL,
    warnings_json       TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (session_id, document_id)
);

CREATE TABLE IF NOT EXISTS segments (
    session_id      TEXT NOT NULL,
    segment_id      TEXT NOT NULL,
    document_id     TEXT NOT NULL,
    ordinal         INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    text            TEXT NOT NULL,
    heading         TEXT,
    page_number     INTEGER,
    speaker         TEXT,
    timestamp_start TEXT,
    timestamp_end   TEXT,
    is_split_part   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, segment_id),
    FOREIGN KEY (session_id, document_id)
        REFERENCES documents(session_id, document_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL,
    stage       TEXT NOT NULL,
    message     TEXT NOT NULL
);
-- ---- Phase 2 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS glossary (
    session_id  TEXT NOT NULL,
    term        TEXT NOT NULL,
    meaning     TEXT,
    PRIMARY KEY (session_id, term)
);

CREATE TABLE IF NOT EXISTS transcription_suggestions (
    session_id   TEXT NOT NULL,
    original     TEXT NOT NULL,
    suggestion   TEXT NOT NULL,
    score        REAL NOT NULL,
    segment_ids  TEXT NOT NULL,
    example      TEXT NOT NULL,
    status       TEXT NOT NULL,
    PRIMARY KEY (session_id, original, suggestion)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id      TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS understandings (
    run_id       TEXT NOT NULL,
    document_id  TEXT NOT NULL,
    json         TEXT NOT NULL,
    PRIMARY KEY (run_id, document_id)
);

CREATE TABLE IF NOT EXISTS statements (
    run_id        TEXT NOT NULL,
    statement_id  TEXT NOT NULL,
    document_id   TEXT NOT NULL,
    json          TEXT NOT NULL,
    PRIMARY KEY (run_id, statement_id)
);

CREATE TABLE IF NOT EXISTS requirements (
    run_id          TEXT NOT NULL,
    requirement_id  TEXT NOT NULL,
    json            TEXT NOT NULL,
    PRIMARY KEY (run_id, requirement_id)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    ts             TEXT NOT NULL,
    stage          TEXT NOT NULL,
    ref            TEXT,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    estimated      INTEGER NOT NULL DEFAULT 1,
    latency_ms     INTEGER NOT NULL DEFAULT 0,
    cached         INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL,
    message        TEXT
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key      TEXT PRIMARY KEY,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    raw_text       TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);

-- ---- Phase 3 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS findings (
    run_id TEXT NOT NULL, item_id TEXT NOT NULL, json TEXT NOT NULL, PRIMARY KEY (run_id, item_id));
CREATE TABLE IF NOT EXISTS stories (
    run_id TEXT NOT NULL, item_id TEXT NOT NULL, json TEXT NOT NULL, PRIMARY KEY (run_id, item_id));
CREATE TABLE IF NOT EXISTS scenarios (
    run_id TEXT NOT NULL, item_id TEXT NOT NULL, json TEXT NOT NULL, PRIMARY KEY (run_id, item_id));
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ts         TEXT NOT NULL,
    item_type  TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    action     TEXT NOT NULL,
    field      TEXT,
    old_value  TEXT,
    new_value  TEXT,
    reviewer   TEXT,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_session ON analysis_runs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_calls_run ON llm_calls(run_id, id);
CREATE INDEX IF NOT EXISTS idx_segments_doc ON segments(session_id, document_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_log_session ON run_log(session_id, id);
"""


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)  # parallel AI calls may write at the same time
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade databases created by earlier versions (Phase 1 -> Phase 2)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "input_mode" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN input_mode TEXT")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
