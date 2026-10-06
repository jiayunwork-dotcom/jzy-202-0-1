"""能力日历版本管理。结构与资源清单版本对称。"""

from __future__ import annotations

from .database import Database
from .errors import ImmutableError, NotFoundError, ValidationError
from .models import CapacityWeekIn
from .registry import Registry


class CapacityService:
    def __init__(self, db: Database, registry: Registry):
        self.db = db
        self.registry = registry

    def _get_row(self, version: int):
        row = self.db.reader.execute(
            "SELECT * FROM capacity_version WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"能力日历版本 {version} 不存在")
        return row

    def get(self, version: int) -> dict:
        row = self._get_row(version)
        horizon = self.db.get_horizon()
        weeks = [
            {
                "workcenter": r["workcenter"],
                "week": r["week"],
                "hours": r["hours"],
            }
            for r in self.db.reader.execute(
                "SELECT workcenter, week, hours FROM capacity_week "
                "WHERE capacity_version=? ORDER BY week, workcenter",
                (version,),
            ).fetchall()
        ]
        return {
            "version": row["version"],
            "status": row["status"],
            "note": row["note"],
            "created_at": row["created_at"],
            "published_at": row["published_at"],
            "week_count": len(weeks),
            "horizon": horizon,
            "weeks": weeks,
        }

    def list_versions(self) -> list[dict]:
        rows = self.db.reader.execute(
            """SELECT v.*, (SELECT COUNT(*) FROM capacity_week c
                           WHERE c.capacity_version = v.version) AS week_count
               FROM capacity_version v ORDER BY v.version"""
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_published(self) -> int:
        row = self.db.reader.execute(
            "SELECT MAX(version) AS v FROM capacity_version WHERE status='published'"
        ).fetchone()
        if row["v"] is None:
            raise ValidationError("还没有已发布的能力日历版本")
        return int(row["v"])

    def sparse_of(self, version: int) -> list[tuple[str, int, float]]:
        self._get_row(version)
        return [
            (r["workcenter"], int(r["week"]), float(r["hours"]))
            for r in self.db.reader.execute(
                "SELECT workcenter, week, hours FROM capacity_week "
                "WHERE capacity_version=?",
                (version,),
            ).fetchall()
        ]

    def create_draft(
        self,
        copy_from: int | None = None,
        note: str = "",
        weeks: list[CapacityWeekIn] | None = None,
    ) -> int:
        if copy_from is not None:
            src = self._get_row(copy_from)
            if src["status"] != "published":
                raise ValidationError("只能复制已发布版本")
        with self.db.write() as conn:
            cur = conn.execute(
                "INSERT INTO capacity_version(status, note) VALUES ('draft', ?)",
                (note,),
            )
            version = int(cur.lastrowid)
            if copy_from is not None:
                conn.execute(
                    "INSERT INTO capacity_week(capacity_version, workcenter, week, hours) "
                    "SELECT ?, workcenter, week, hours FROM capacity_week "
                    "WHERE capacity_version=?",
                    (version, copy_from),
                )
        if weeks is not None:
            self.replace_weeks(version, weeks)
        return version

    def replace_weeks(self, version: int, weeks: list[CapacityWeekIn]) -> None:
        row = self._get_row(version)
        if row["status"] != "draft":
            raise ImmutableError("能力日历已发布，不可修改；请基于它新建草稿")
        self._validate(weeks)
        with self.db.write() as conn:
            conn.execute(
                "DELETE FROM capacity_week WHERE capacity_version=?", (version,)
            )
            conn.executemany(
                "INSERT INTO capacity_week(capacity_version, workcenter, week, hours) "
                "VALUES (?, ?, ?, ?)",
                [
                    (version, w.workcenter, int(w.week), float(w.hours))
                    for w in weeks
                ],
            )

    def _validate(self, weeks: list[CapacityWeekIn]) -> None:
        horizon = self.db.get_horizon()
        seen: set[tuple[str, int]] = set()
        for w in weeks:
            if w.week > horizon:
                raise ValidationError(
                    f"能力周号 {w.week} 超出计划期（1..{horizon}）",
                    details={"week": w.week, "horizon": horizon},
                )
            key = (w.workcenter, w.week)
            if key in seen:
                raise ValidationError(
                    f"工作中心 {w.workcenter} 第 {w.week} 周能力重复"
                )
            seen.add(key)
        self.registry.require_workcenters({w.workcenter for w in weeks})

    def publish(self, version: int) -> dict:
        row = self._get_row(version)
        if row["status"] == "published":
            raise ImmutableError(f"能力日历版本 {version} 已是发布状态")
        with self.db.write() as conn:
            conn.execute(
                "UPDATE capacity_version SET status='published', "
                "published_at=datetime('now') WHERE version=?",
                (version,),
            )
        return self.get(version)
