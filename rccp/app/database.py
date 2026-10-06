"""SQLite 存储层。

库文件放在挂载卷（``RCCP_DATA_DIR``，默认 /data）。单文件库，WAL 模式：

* 所有写入在同一进程内由一把进程锁串行化（服务方法再按草稿 id 加细粒度锁）；
* 读连接与写连接分开，``check_same_thread=False`` 配合短事务使用；
* 草稿与版本的数量、能力按行存储（可读、可审计），负荷矩阵以原始字节
  （float64, C-order）存储并带形状头；重启时直接反序列化，与数量表
  重新展开的结果做一次校验后投入使用（见 service.bootstrap）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import numpy as np

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workcenter (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 资源清单版本（草稿与已发布共用一张表，status 区分）
CREATE TABLE IF NOT EXISTS routing_version (
    version     INTEGER PRIMARY KEY AUTOINCREMENT,
    status      TEXT NOT NULL CHECK (status IN ('draft','published')),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS routing_line (
    routing_version INTEGER NOT NULL REFERENCES routing_version(version),
    product         TEXT NOT NULL REFERENCES product(code),
    workcenter      TEXT NOT NULL REFERENCES workcenter(code),
    hours_per_unit  REAL NOT NULL CHECK (hours_per_unit >= 0),
    lead_weeks      REAL NOT NULL CHECK (lead_weeks >= 0),
    PRIMARY KEY (routing_version, product, workcenter)
);

-- 能力日历版本
CREATE TABLE IF NOT EXISTS capacity_version (
    version     INTEGER PRIMARY KEY AUTOINCREMENT,
    status      TEXT NOT NULL CHECK (status IN ('draft','published')),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS capacity_week (
    capacity_version INTEGER NOT NULL REFERENCES capacity_version(version),
    workcenter       TEXT NOT NULL REFERENCES workcenter(code),
    week             INTEGER NOT NULL CHECK (week >= 1),
    hours            REAL NOT NULL CHECK (hours >= 0),
    PRIMARY KEY (capacity_version, workcenter, week)
);

-- 主生产计划：草稿（status='draft'）与已发布版本（status='published'）
CREATE TABLE IF NOT EXISTS plan_version (
    id               TEXT PRIMARY KEY,           -- 草稿用业务 id；发布后另起 published 行
    status           TEXT NOT NULL CHECK (status IN ('draft','published')),
    name             TEXT NOT NULL DEFAULT '',
    routing_version  INTEGER NOT NULL REFERENCES routing_version(version),
    capacity_version INTEGER NOT NULL REFERENCES capacity_version(version),
    lead_split       TEXT NOT NULL,
    pre_horizon      TEXT NOT NULL,
    parent_draft_id  TEXT REFERENCES plan_version(id),
    published_from   TEXT,
    version          INTEGER,                    -- 已发布版本的单调序号
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    published_at     TEXT
);
CREATE TABLE IF NOT EXISTS plan_cell (
    plan_id  TEXT NOT NULL REFERENCES plan_version(id) ON DELETE CASCADE,
    product  TEXT NOT NULL REFERENCES product(code),
    week     INTEGER NOT NULL CHECK (week >= 1),
    quantity REAL NOT NULL CHECK (quantity >= 0),
    PRIMARY KEY (plan_id, product, week)
);

-- 负荷结果：草稿实时结果 + 已发布结果 + 按新清单重算的结果。
-- policy 取值：draft / published / recalc。
CREATE TABLE IF NOT EXISTS load_result (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id          TEXT NOT NULL REFERENCES plan_version(id) ON DELETE CASCADE,
    policy           TEXT NOT NULL,
    routing_version  INTEGER NOT NULL REFERENCES routing_version(version),
    capacity_version INTEGER NOT NULL REFERENCES capacity_version(version),
    lead_split       TEXT NOT NULL,
    pre_horizon      TEXT NOT NULL,
    n_workcenters    INTEGER NOT NULL,
    horizon          INTEGER NOT NULL,
    matrix_blob      BLOB NOT NULL,              -- n_workcenters x horizon float64
    overdue_blob     BLOB NOT NULL,              -- n_workcenters float64
    wc_codes_json    TEXT NOT NULL,              -- 行序 = 工作中心 code 排序
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    recalc_of_result INTEGER REFERENCES load_result(id)
);
"""


class Database:
    """SQLite 连接管理。写操作全局串行，读操作可并发。"""

    def __init__(self, path: str | None = None, horizon: int | None = None):
        self.path = path or settings.db_path
        self.horizon = horizon or settings.horizon_weeks
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._write_lock = threading.RLock()
        self._txn_lock = threading.RLock()
        self._writer = self._connect()
        self._read_local = threading.local()
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @property
    def reader(self) -> sqlite3.Connection:
        conn = getattr(self._read_local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._read_local.conn = conn
        return conn

    def init_schema(self) -> None:
        with self._write_lock:
            self._writer.executescript(SCHEMA)
            self._writer.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('horizon_weeks', ?)",
                (str(self.horizon),),
            )
            self._writer.commit()

    def get_horizon(self) -> int:
        row = self.reader.execute(
            "SELECT value FROM meta WHERE key='horizon_weeks'"
        ).fetchone()
        return int(row["value"])

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """串行化的写事务。异常回滚，正常提交。"""
        with self._write_lock:
            try:
                yield self._writer
                self._writer.commit()
            except Exception:
                self._writer.rollback()
                raise

    # ---- numpy 序列化 -------------------------------------------------

    @staticmethod
    def matrix_to_blob(matrix: np.ndarray) -> bytes:
        return np.ascontiguousarray(matrix, dtype=np.float64).tobytes()

    @staticmethod
    def blob_to_matrix(blob: bytes, n_rows: int, n_cols: int) -> np.ndarray:
        arr = np.frombuffer(blob, dtype=np.float64)
        return arr.reshape(n_rows, n_cols).copy()

    @staticmethod
    def vector_to_blob(vector: np.ndarray) -> bytes:
        return np.ascontiguousarray(vector, dtype=np.float64).tobytes()

    @staticmethod
    def blob_to_vector(blob: bytes, n: int) -> np.ndarray:
        arr = np.frombuffer(blob, dtype=np.float64)
        return arr.reshape(n).copy()

    @staticmethod
    def dumps(obj: object) -> str:
        return json.dumps(obj, ensure_ascii=False)
