"""SQLite 持久层。

刻意不引入 ORM，用最薄的仓储封装，方便后续换 Postgres。
表结构全部自研，与原项目无任何字段对应。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          TEXT PRIMARY KEY,
    handle      TEXT UNIQUE NOT NULL,
    email       TEXT,
    pw_hash     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tastes (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    category    TEXT NOT NULL,
    value       TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(user_id, category, value)
);

CREATE TABLE IF NOT EXISTS journeys (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    title       TEXT,
    destination TEXT,
    start_date  TEXT,
    days_count  INTEGER,
    raw_need    TEXT,
    status      TEXT DEFAULT 'draft',
    plan_json   TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    category    TEXT NOT NULL,
    polarity    TEXT NOT NULL DEFAULT 'prefer',
    value_text  TEXT NOT NULL,
    scope_type  TEXT NOT NULL DEFAULT 'global',
    scope_key   TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'active',
    source_kind TEXT NOT NULL DEFAULT 'manual',
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_meta (
    user_id   TEXT PRIMARY KEY,
    revision  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS conversations (
    id                    TEXT PRIMARY KEY,
    owner_id              TEXT NOT NULL,
    title                 TEXT,
    status                TEXT NOT NULL DEFAULT 'active',
    related_itinerary_id  TEXT,
    finalization_status   TEXT,
    memory_error_code     TEXT,
    memory_created        INTEGER,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id                    TEXT PRIMARY KEY,
    conversation_id       TEXT NOT NULL,
    role                  TEXT NOT NULL,
    content               TEXT NOT NULL DEFAULT '',
    sequence              REAL NOT NULL,
    artifacts             TEXT NOT NULL DEFAULT '[]',
    related_itinerary_id  TEXT,
    related_run_id        TEXT,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS planning_briefs (
    id               TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL,
    owner_id         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'collecting',
    data             TEXT NOT NULL DEFAULT '{}',
    missing_fields   TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_owner ON conversations(owner_id);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON conversation_messages(conversation_id, sequence);
CREATE INDEX IF NOT EXISTS idx_brief_conv ON planning_briefs(conversation_id);

CREATE TABLE IF NOT EXISTS spot_tips (
    name        TEXT PRIMARY KEY,
    tip         TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spot_times (
    name        TEXT PRIMARY KEY,
    period      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _new_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _new_conn() as conn:
        conn.executescript(_SCHEMA)


@contextmanager
def connection():
    conn = _new_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
