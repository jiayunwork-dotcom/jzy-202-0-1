"""资源清单版本管理。

* 每个草稿是一张可变的行集合（同一产品在同一工作中心唯一一行）；
* 发布后不可变；新草稿可以基于任意已发布版本复制；
* 已发布计划不会因新版本而改变（计划版本绑定发布当时的清单版本号）。
"""

from __future__ import annotations

import sqlite3

from .database import Database
from .errors import ImmutableError, NotFoundError, ValidationError
from .models import RoutingLineIn
from .registry import Registry


class RoutingService:
    def __init__(self, db: Database, registry: Registry):
        self.db = db
        self.registry = registry

    # ---- 查询 ----

    def _get_row(self, version: int) -> sqlite3.Row:
        row = self.db.reader.execute(
            "SELECT * FROM routing_version WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"资源清单版本 {version} 不存在")
        return row

    def get(self, version: int) -> dict:
        row = self._get_row(version)
        lines = [
            {
                "product": r["product"],
                "workcenter": r["workcenter"],
                "hours_per_unit": r["hours_per_unit"],
                "lead_weeks": r["lead_weeks"],
            }
            for r in self.db.reader.execute(
                "SELECT product, workcenter, hours_per_unit, lead_weeks "
                "FROM routing_line WHERE routing_version=? "
                "ORDER BY product, workcenter",
                (version,),
            ).fetchall()
        ]
        return {
            "version": row["version"],
            "status": row["status"],
            "note": row["note"],
            "created_at": row["created_at"],
            "published_at": row["published_at"],
            "line_count": len(lines),
            "lines": lines,
        }

    def list_versions(self) -> list[dict]:
        rows = self.db.reader.execute(
            """SELECT v.*, (SELECT COUNT(*) FROM routing_line l
                           WHERE l.routing_version = v.version) AS line_count
               FROM routing_version v ORDER BY v.version"""
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_published(self) -> int:
        row = self.db.reader.execute(
            "SELECT MAX(version) AS v FROM routing_version WHERE status='published'"
        ).fetchone()
        if row["v"] is None:
            raise ValidationError("还没有已发布的资源清单版本")
        return int(row["v"])

    def lines_of(self, version: int):
        """返回已发布/草稿版本的行（RoutingLineIn 列表）。"""
        self._get_row(version)
        return [
            RoutingLineIn(
                product=r["product"],
                workcenter=r["workcenter"],
                hours_per_unit=r["hours_per_unit"],
                lead_weeks=r["lead_weeks"],
            )
            for r in self.db.reader.execute(
                "SELECT product, workcenter, hours_per_unit, lead_weeks "
                "FROM routing_line WHERE routing_version=? "
                "ORDER BY product, workcenter",
                (version,),
            ).fetchall()
        ]

    def covered_products(self, version: int) -> set[str]:
        return {
            r["product"]
            for r in self.db.reader.execute(
                "SELECT DISTINCT product FROM routing_line WHERE routing_version=?",
                (version,),
            ).fetchall()
        }

    # ---- 写入 ----

    def create_draft(
        self,
        copy_from: int | None = None,
        note: str = "",
        lines: list[RoutingLineIn] | None = None,
    ) -> int:
        if copy_from is not None:
            src = self._get_row(copy_from)
            if src["status"] != "published":
                raise ValidationError("只能复制已发布版本")
        with self.db.write() as conn:
            cur = conn.execute(
                "INSERT INTO routing_version(status, note) VALUES ('draft', ?)",
                (note,),
            )
            version = int(cur.lastrowid)
            if copy_from is not None:
                conn.execute(
                    "INSERT INTO routing_line"
                    "(routing_version, product, workcenter, hours_per_unit, lead_weeks) "
                    "SELECT ?, product, workcenter, hours_per_unit, lead_weeks "
                    "FROM routing_line WHERE routing_version=?",
                    (version, copy_from),
                )
        if lines is not None:
            self.replace_lines(version, lines)
        return version

    def replace_lines(self, version: int, lines: list[RoutingLineIn]) -> None:
        row = self._get_row(version)
        if row["status"] != "draft":
            raise ImmutableError("资源清单已发布，不可修改；请基于它新建草稿")
        self._validate_lines(lines)
        with self.db.write() as conn:
            conn.execute(
                "DELETE FROM routing_line WHERE routing_version=?", (version,)
            )
            conn.executemany(
                "INSERT INTO routing_line"
                "(routing_version, product, workcenter, hours_per_unit, lead_weeks) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        version,
                        ln.product,
                        ln.workcenter,
                        float(ln.hours_per_unit),
                        float(ln.lead_weeks),
                    )
                    for ln in lines
                ],
            )

    def _validate_lines(self, lines: list[RoutingLineIn]) -> None:
        seen: set[tuple[str, str]] = set()
        for ln in lines:
            key = (ln.product, ln.workcenter)
            if key in seen:
                raise ValidationError(
                    f"产品 {ln.product} 在工作中心 {ln.workcenter} 上存在重复行"
                )
            seen.add(key)
        self.registry.require_products({ln.product for ln in lines})
        self.registry.require_workcenters({ln.workcenter for ln in lines})

    def publish(self, version: int) -> dict:
        row = self._get_row(version)
        if row["status"] == "published":
            raise ImmutableError(f"资源清单版本 {version} 已是发布状态")
        with self.db.write() as conn:
            conn.execute(
                "UPDATE routing_version SET status='published', "
                "published_at=datetime('now') WHERE version=?",
                (version,),
            )
        return self.get(version)
