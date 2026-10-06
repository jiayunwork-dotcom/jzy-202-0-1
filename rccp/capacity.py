"""能力日历：每个工作中心每周的可用工时，随班次安排变化。版本创建即不可变。

日历中缺失的 (工作中心, 周) 一律按 0 可用工时处理（保守：宁可多报超负荷）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .bom import utcnow


@dataclass
class CalendarVersion:
    version: int
    entries: dict[tuple[str, int], float]  # (work_center, week) -> available_hours
    note: str = ""
    created_at: str = ""
    work_centers: set[str] = field(init=False)

    def __post_init__(self) -> None:
        self.work_centers = {wc for (wc, _w) in self.entries}

    def capacity(self, work_center: str, week: int) -> float:
        return self.entries.get((work_center, week), 0.0)


class CalendarStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, entries: list[tuple[str, int, float]], note: str) -> CalendarVersion:
        created_at = utcnow()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO calendar_versions(created_at, note) VALUES (?, ?)",
                (created_at, note),
            )
            version = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO calendar_entries(version, work_center, week, available_hours)"
                " VALUES (?, ?, ?, ?)",
                [(version, wc, week, hours) for wc, week, hours in entries],
            )
        return CalendarVersion(version, dict(((wc, w), h) for wc, w, h in entries), note, created_at)

    def _load(self, row: sqlite3.Row) -> CalendarVersion:
        entries = {
            (r["work_center"], r["week"]): r["available_hours"]
            for r in self.conn.execute(
                "SELECT work_center, week, available_hours FROM calendar_entries WHERE version = ?",
                (row["version"],),
            )
        }
        return CalendarVersion(row["version"], entries, row["note"], row["created_at"])

    def get(self, version: int) -> CalendarVersion | None:
        row = self.conn.execute(
            "SELECT version, created_at, note FROM calendar_versions WHERE version = ?",
            (version,),
        ).fetchone()
        return self._load(row) if row else None

    def latest(self) -> CalendarVersion | None:
        row = self.conn.execute(
            "SELECT version, created_at, note FROM calendar_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return self._load(row) if row else None

    def list(self) -> list[dict]:
        return [
            {
                "version": r["version"],
                "note": r["note"],
                "created_at": r["created_at"],
                "entry_count": r["entry_count"],
            }
            for r in self.conn.execute(
                "SELECT v.version, v.note, v.created_at,"
                " (SELECT COUNT(*) FROM calendar_entries e WHERE e.version = v.version) AS entry_count"
                " FROM calendar_versions v ORDER BY v.version"
            )
        ]

    def all_work_centers(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT work_center FROM calendar_entries")}
