"""产品与工作中心注册（主数据）。

数量、工时、能力里引用的产品/工作中心必须先在这里注册，否则拒收。
"""

from __future__ import annotations

import sqlite3

from .database import Database
from .errors import ConflictError, NotFoundError, ValidationError


class Registry:
    def __init__(self, db: Database):
        self.db = db

    # ---- 产品 ----

    def create_product(self, code: str, name: str = "") -> None:
        try:
            with self.db.write() as conn:
                conn.execute(
                    "INSERT INTO product(code, name) VALUES (?, ?)", (code, name)
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"产品 {code!r} 已存在") from exc

    def list_products(self) -> list[dict[str, str]]:
        rows = self.db.reader.execute(
            "SELECT code, name FROM product ORDER BY code"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 工作中心 ----

    def create_workcenter(self, code: str, name: str = "") -> None:
        try:
            with self.db.write() as conn:
                conn.execute(
                    "INSERT INTO workcenter(code, name) VALUES (?, ?)", (code, name)
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"工作中心 {code!r} 已存在") from exc

    def list_workcenters(self) -> list[dict[str, str]]:
        rows = self.db.reader.execute(
            "SELECT code, name FROM workcenter ORDER BY code"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 校验辅助 ----

    def require_products(self, codes) -> None:
        codes = list(codes)
        if not codes:
            return
        placeholders = ",".join("?" for _ in codes)
        found = {
            r["code"]
            for r in self.db.reader.execute(
                f"SELECT code FROM product WHERE code IN ({placeholders})", codes
            ).fetchall()
        }
        missing = sorted(set(codes) - found)
        if missing:
            raise ValidationError(
                "引用了不存在的产品", details={"products": missing}
            )

    def require_workcenters(self, codes) -> None:
        codes = list(codes)
        if not codes:
            return
        placeholders = ",".join("?" for _ in codes)
        found = {
            r["code"]
            for r in self.db.reader.execute(
                f"SELECT code FROM workcenter WHERE code IN ({placeholders})", codes
            ).fetchall()
        }
        missing = sorted(set(codes) - found)
        if missing:
            raise ValidationError(
                "引用了不存在的工作中心", details={"workcenters": missing}
            )

    def all_product_codes(self) -> list[str]:
        return [r["code"] for r in self.db.reader.execute(
            "SELECT code FROM product ORDER BY code"
        ).fetchall()]

    def all_workcenter_codes(self) -> list[str]:
        return [r["code"] for r in self.db.reader.execute(
            "SELECT code FROM workcenter ORDER BY code"
        ).fetchall()]

    def get_product(self, code: str) -> dict[str, str]:
        row = self.db.reader.execute(
            "SELECT code, name FROM product WHERE code=?", (code,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"产品 {code!r} 不存在")
        return dict(row)
