"""SQLite 连接与建表。

持久化的只有"源数据"：资源清单/能力日历的各版本、草稿单元格、
已发布计划版本及其负荷结果。草稿的负荷矩阵是派生状态，启动时重建
（见 docs/DESIGN.md 第 2 节）。
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS bom_versions (
    version     INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bom_lines (
    version         INTEGER NOT NULL REFERENCES bom_versions(version),
    product         TEXT NOT NULL,
    work_center     TEXT NOT NULL,
    hours_per_unit  REAL NOT NULL,
    lead_weeks      REAL NOT NULL,
    PRIMARY KEY (version, product, work_center)
);
CREATE TABLE IF NOT EXISTS calendar_versions (
    version     INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS calendar_entries (
    version          INTEGER NOT NULL REFERENCES calendar_versions(version),
    work_center      TEXT NOT NULL,
    week             INTEGER NOT NULL,
    available_hours  REAL NOT NULL,
    PRIMARY KEY (version, work_center, week)
);
CREATE TABLE IF NOT EXISTS draft_cells (
    product  TEXT NOT NULL,
    week     INTEGER NOT NULL,
    qty      INTEGER NOT NULL,
    PRIMARY KEY (product, week)
);
CREATE TABLE IF NOT EXISTS plan_versions (
    version           INTEGER PRIMARY KEY,
    created_at        TEXT NOT NULL,
    note              TEXT NOT NULL DEFAULT '',
    bom_version       INTEGER NOT NULL,
    calendar_version  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_cells (
    plan_version  INTEGER NOT NULL REFERENCES plan_versions(version),
    product       TEXT NOT NULL,
    week          INTEGER NOT NULL,
    qty           INTEGER NOT NULL,
    PRIMARY KEY (plan_version, product, week)
);
CREATE TABLE IF NOT EXISTS load_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL,              -- 'publish' | 'recompute'
    plan_version      INTEGER NOT NULL,
    bom_version       INTEGER NOT NULL,
    calendar_version  INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    matrix_json       TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL：已提交事务在崩溃/重启后不丢
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()
