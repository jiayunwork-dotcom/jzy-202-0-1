"""资源清单（BOM）：每个产品在哪些工作中心上占用多少工时（每件）、
相对完工周的提前量（可以是小数周）。版本创建即不可变，最新版本为当前版本。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class BomLine:
    product: str
    work_center: str
    hours_per_unit: float
    lead_weeks: float


@dataclass
class BomVersion:
    version: int
    lines: tuple[BomLine, ...]
    note: str = ""
    created_at: str = ""
    # 以下三个索引在 __post_init__ 中构建
    by_product: dict[str, list[BomLine]] = field(init=False)
    products: set[str] = field(init=False)
    work_centers: set[str] = field(init=False)

    def __post_init__(self) -> None:
        by_product: dict[str, list[BomLine]] = {}
        for ln in self.lines:
            by_product.setdefault(ln.product, []).append(ln)
        self.by_product = by_product
        self.products = set(by_product)
        self.work_centers = {ln.work_center for ln in self.lines}


class BomStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, lines: list[BomLine], note: str) -> BomVersion:
        created_at = utcnow()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO bom_versions(created_at, note) VALUES (?, ?)",
                (created_at, note),
            )
            version = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO bom_lines(version, product, work_center, hours_per_unit, lead_weeks)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (version, ln.product, ln.work_center, ln.hours_per_unit, ln.lead_weeks)
                    for ln in lines
                ],
            )
        return BomVersion(version, tuple(lines), note, created_at)

    def _load(self, row: sqlite3.Row) -> BomVersion:
        lines = tuple(
            BomLine(r["product"], r["work_center"], r["hours_per_unit"], r["lead_weeks"])
            for r in self.conn.execute(
                "SELECT product, work_center, hours_per_unit, lead_weeks"
                " FROM bom_lines WHERE version = ? ORDER BY product, work_center",
                (row["version"],),
            )
        )
        return BomVersion(row["version"], lines, row["note"], row["created_at"])

    def get(self, version: int) -> BomVersion | None:
        row = self.conn.execute(
            "SELECT version, created_at, note FROM bom_versions WHERE version = ?",
            (version,),
        ).fetchone()
        return self._load(row) if row else None

    def latest(self) -> BomVersion | None:
        row = self.conn.execute(
            "SELECT version, created_at, note FROM bom_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return self._load(row) if row else None

    def list(self) -> list[dict]:
        return [
            {
                "version": r["version"],
                "note": r["note"],
                "created_at": r["created_at"],
                "line_count": r["line_count"],
            }
            for r in self.conn.execute(
                "SELECT v.version, v.note, v.created_at,"
                " (SELECT COUNT(*) FROM bom_lines l WHERE l.version = v.version) AS line_count"
                " FROM bom_versions v ORDER BY v.version"
            )
        ]

    def all_products(self) -> set[str]:
        """在任何 BOM 版本中出现过的产品（用于"产品是否存在"的判定）。"""
        return {r[0] for r in self.conn.execute("SELECT DISTINCT product FROM bom_lines")}

    def all_work_centers(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT work_center FROM bom_lines")}
