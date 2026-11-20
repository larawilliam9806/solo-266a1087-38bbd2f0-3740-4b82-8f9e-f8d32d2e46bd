"""SQLite 持久化层。

设计要点:
- 路网按版本注册,版本一旦创建即不可变;修订路网 = 注册新版本。
- 分析结果(匹配路径 + 缺口)按分析时的路网版本落库,
  因此路网修订后,旧分析仍可按原版本查询。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS networks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id  INTEGER NOT NULL REFERENCES networks(id),
    segment_id  TEXT NOT NULL,
    start_lat   REAL NOT NULL,
    start_lon   REAL NOT NULL,
    end_lat     REAL NOT NULL,
    end_lon     REAL NOT NULL,
    length_m    REAL NOT NULL,
    UNIQUE (network_id, segment_id)
);

CREATE TABLE IF NOT EXISTS trajectories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trajectory_points (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trajectory_id   INTEGER NOT NULL REFERENCES trajectories(id),
    seq             INTEGER NOT NULL,
    ts              TEXT NOT NULL,   -- UTC ISO8601
    lat             REAL NOT NULL,
    lon             REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trajectory_id    INTEGER NOT NULL REFERENCES trajectories(id),
    network_id       INTEGER NOT NULL REFERENCES networks(id),
    created_at       TEXT NOT NULL,
    total_points     INTEGER NOT NULL,
    matched_points   INTEGER NOT NULL,
    total_distance_m REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    seq         INTEGER NOT NULL,
    segment_id  TEXT NOT NULL,
    enter_time  TEXT NOT NULL,
    leave_time  TEXT NOT NULL,
    distance_m  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_gaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    seq         INTEGER NOT NULL,
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    reason      TEXT NOT NULL   -- out_of_range | disconnected
);

-- 逐采样点匹配明细:车距复核的依据,与分析在同一事务写入。
-- 缺口内未匹配点同样落一行(segment_id/offset_m/snap_dist_m 为 NULL),
-- 因此可凭该表是否存在判断旧分析能否复核。
CREATE TABLE IF NOT EXISTS analysis_point_matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    seq         INTEGER NOT NULL,
    ts          TEXT NOT NULL,             -- UTC ISO8601,采样时刻
    segment_id  TEXT,                      -- 匹配到的有向路段编号;未匹配为 NULL
    offset_m    REAL,                      -- 吸附点距所在路段起点的里程(米)
    snap_dist_m REAL,                      -- GPS 点到吸附点的距离(米)
    UNIQUE (analysis_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_apm_analysis
    ON analysis_point_matches(analysis_id, seq);
"""


def db_path() -> str:
    return os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
