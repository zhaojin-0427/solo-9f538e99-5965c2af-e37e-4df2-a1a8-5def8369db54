"""SQLite 存取层：连接管理、建表、批次事务性合并。

设计要点：
- events 表保存所有历史修订（审计留痕），主键 (event_id, revision)；
- current_events 视图只取每个事件的最高修订，行程/异常都基于它即时计算，
  因此更正卡号或时间后，旧卡与新卡的结果天然一起更新；
- 一个上传批次在一个事务内合并，任何冲突都整体回滚，
  查询侧只会看到合并前或合并后的完整状态，不会新旧混合。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

from processing import BatchError, EventRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT NOT NULL,
    uploaded_at   TEXT NOT NULL,              -- UTC 秒级 ISO
    status        TEXT NOT NULL,              -- 'applied' | 'rejected'
    message       TEXT,
    rows_total    INTEGER NOT NULL DEFAULT 0, -- 文件内去重后的有效行数
    rows_applied  INTEGER NOT NULL DEFAULT 0, -- 新写入的修订条数
    rows_stale    INTEGER NOT NULL DEFAULT 0, -- 低于已存最高修订而被忽略的条数
    rows_noop     INTEGER NOT NULL DEFAULT 0  -- 与已存内容一致 / 文件内重复的条数
);

CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    card_no    TEXT NOT NULL,
    event_time TEXT NOT NULL,                 -- UTC 秒级 ISO，定长可排序
    station    TEXT NOT NULL,
    direction  TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    upload_id  INTEGER NOT NULL REFERENCES uploads (id),
    PRIMARY KEY (event_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_events_card ON events (card_no, event_time);

-- 每个事件只采用最高修订
CREATE VIEW IF NOT EXISTS current_events AS
SELECT e.*
FROM events e
JOIN (
    SELECT event_id, MAX(revision) AS max_rev
    FROM events
    GROUP BY event_id
) m ON m.event_id = e.event_id AND m.max_rev = e.revision;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db = sqlite3.connect(current_app.config["DATABASE"], timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 15000")
        g.db = db
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def apply_batch(db: sqlite3.Connection, rows: list[EventRow], upload_id: int) -> tuple[int, int, int]:
    """在调用方开启的事务内，把一个批次合并进 events 表。

    合并规则（对每个 event_id 只保留最高修订）：
    - 批次修订低于库内最高修订      → 过期数据，忽略；
    - 批次修订等于库内同修订且内容一致 → 幂等重传，忽略；
    - 批次修订等于库内同修订但内容冲突 → BatchError，调用方整体回滚；
    - 批次修订更高（或事件不存在）  → 写入新修订。

    返回 (applied, stale, noop)。
    """
    applied = stale = noop = 0
    for row in rows:
        existing = db.execute(
            "SELECT revision, card_no, event_time, station, direction "
            "FROM events WHERE event_id = ? ORDER BY revision DESC LIMIT 1",
            (row.event_id,),
        ).fetchone()

        if existing is not None:
            if row.revision < existing["revision"]:
                stale += 1
                continue
            if row.revision == existing["revision"]:
                same = (
                    existing["card_no"] == row.card_no
                    and existing["event_time"] == row.event_time
                    and existing["station"] == row.station
                    and existing["direction"] == row.direction
                )
                if not same:
                    raise BatchError(
                        f"事件 {row.event_id} 修订 {row.revision} 与已存档内容冲突，整批拒绝")
                noop += 1
                continue

        db.execute(
            "INSERT INTO events (event_id, revision, card_no, event_time, station, direction, upload_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row.event_id, row.revision, row.card_no, row.event_time,
             row.station, row.direction, upload_id),
        )
        applied += 1
    return applied, stale, noop
