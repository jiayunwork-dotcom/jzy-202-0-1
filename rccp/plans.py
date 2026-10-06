"""计划草稿、已发布计划版本、负荷结果的持久化。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .bom import utcnow


@dataclass
class PlanVersionInfo:
    version: int
    bom_version: int
    calendar_version: int
    note: str
    created_at: str
    cell_count: int = 0


@dataclass
class LoadResultRecord:
    id: int
    kind: str  # 'publish' | 'recompute'
    plan_version: int
    bom_version: int
    calendar_version: int
    created_at: str
    matrix: dict  # {wc: {"weeks": [...], "past_due": x}}


class PlanStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------------- 草稿 ----------------
    def load_draft(self) -> dict[tuple[str, int], int]:
        return {
            (r["product"], r["week"]): r["qty"]
            for r in self.conn.execute("SELECT product, week, qty FROM draft_cells")
        }

    def _set_cell_tx(self, product: str, week: int, qty: int) -> None:
        if qty == 0:
            self.conn.execute(
                "DELETE FROM draft_cells WHERE product = ? AND week = ?", (product, week)
            )
        else:
            self.conn.execute(
                "INSERT INTO draft_cells(product, week, qty) VALUES (?, ?, ?)"
                " ON CONFLICT(product, week) DO UPDATE SET qty = excluded.qty",
                (product, week, qty),
            )

    def set_draft_cell(self, product: str, week: int, qty: int) -> None:
        with self.conn:
            self._set_cell_tx(product, week, qty)

    def set_draft_cells(self, changes: list[tuple[str, int, int]]) -> None:
        """批量导入：一个事务里按顺序应用全部修改。"""
        with self.conn:
            for product, week, qty in changes:
                self._set_cell_tx(product, week, qty)

    # ---------------- 已发布计划版本 ----------------
    def create_plan_version(
        self,
        cells: dict[tuple[str, int], int],
        bom_version: int,
        calendar_version: int,
        note: str,
    ) -> PlanVersionInfo:
        created_at = utcnow()
        nonzero = [(p, w, q) for (p, w), q in cells.items() if q > 0]
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO plan_versions(created_at, note, bom_version, calendar_version)"
                " VALUES (?, ?, ?, ?)",
                (created_at, note, bom_version, calendar_version),
            )
            version = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO plan_cells(plan_version, product, week, qty) VALUES (?, ?, ?, ?)",
                [(version, p, w, q) for p, w, q in nonzero],
            )
        return PlanVersionInfo(version, bom_version, calendar_version, note, created_at, len(nonzero))

    def get_plan_version(self, version: int) -> PlanVersionInfo | None:
        row = self.conn.execute(
            "SELECT v.version, v.bom_version, v.calendar_version, v.note, v.created_at,"
            " (SELECT COUNT(*) FROM plan_cells c WHERE c.plan_version = v.version) AS cell_count"
            " FROM plan_versions v WHERE v.version = ?",
            (version,),
        ).fetchone()
        if not row:
            return None
        return PlanVersionInfo(
            row["version"], row["bom_version"], row["calendar_version"],
            row["note"], row["created_at"], row["cell_count"],
        )

    def list_plan_versions(self) -> list[PlanVersionInfo]:
        return [
            PlanVersionInfo(
                r["version"], r["bom_version"], r["calendar_version"],
                r["note"], r["created_at"], r["cell_count"],
            )
            for r in self.conn.execute(
                "SELECT v.version, v.bom_version, v.calendar_version, v.note, v.created_at,"
                " (SELECT COUNT(*) FROM plan_cells c WHERE c.plan_version = v.version) AS cell_count"
                " FROM plan_versions v ORDER BY v.version"
            )
        ]

    def get_plan_cells(self, version: int) -> dict[tuple[str, int], int]:
        return {
            (r["product"], r["week"]): r["qty"]
            for r in self.conn.execute(
                "SELECT product, week, qty FROM plan_cells WHERE plan_version = ?", (version,)
            )
        }

    # ---------------- 负荷结果 ----------------
    def save_load_result(
        self,
        kind: str,
        plan_version: int,
        bom_version: int,
        calendar_version: int,
        matrix: dict,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO load_results(kind, plan_version, bom_version, calendar_version,"
                " created_at, matrix_json) VALUES (?, ?, ?, ?, ?, ?)",
                (kind, plan_version, bom_version, calendar_version, utcnow(), json.dumps(matrix)),
            )
        return cur.lastrowid

    def get_load_results(self, plan_version: int, kind: str | None = None) -> list[LoadResultRecord]:
        sql = ("SELECT id, kind, plan_version, bom_version, calendar_version, created_at,"
               " matrix_json FROM load_results WHERE plan_version = ?")
        args: list = [plan_version]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY id"
        return [
            LoadResultRecord(
                r["id"], r["kind"], r["plan_version"], r["bom_version"],
                r["calendar_version"], r["created_at"], json.loads(r["matrix_json"]),
            )
            for r in self.conn.execute(sql, args)
        ]

    def get_publish_result(self, plan_version: int) -> LoadResultRecord | None:
        records = self.get_load_results(plan_version, kind="publish")
        return records[0] if records else None
