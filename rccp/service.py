"""应用服务层：把各模块装配在一起，持有草稿的内存态负荷矩阵。

并发模型：所有写操作在单个 asyncio.Lock 下串行执行，按到达顺序生效；
同一计划格的冲突用 expected_old_qty 做 CAS——调用方必须带上它基于的旧值，
不一致则拒绝并返回当前值。读操作直接读内存快照。

重启恢复：SQLite 只存源数据（BOM/日历版本、草稿单元格、已发布版本及其
负荷结果）；草稿的负荷矩阵是派生状态，启动时一次全量展开重建。
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import asdict, dataclass

from .bom import BomLine, BomStore, BomVersion
from .capacity import CalendarStore, CalendarVersion
from .compare import compare_results, overload_cells, past_due_rows
from .config import Settings
from .db import Database
from .errors import Conflict, NotFound, ValidationFailed
from .incremental import LoadMatrix
from .plans import PlanStore


@dataclass
class BatchCell:
    """批量导入的一行。字段是原始输入，由 batch_edit 逐行校验并规范化。"""

    product: object
    week: object
    qty: object
    expected_old_qty: object = None


class RCCPService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.boms = BomStore(self.db.conn)
        self.calendars = CalendarStore(self.db.conn)
        self.plans = PlanStore(self.db.conn)
        self.lock = asyncio.Lock()
        self._reload()

    # ---------------- 内部 ----------------
    def _reload(self) -> None:
        """从 SQLite 恢复全部状态；草稿负荷矩阵全量重建。"""
        self.current_bom: BomVersion | None = self.boms.latest()
        self.current_calendar: CalendarVersion | None = self.calendars.latest()
        self.draft: dict[tuple[str, int], int] = self.plans.load_draft()
        self.matrix = LoadMatrix.recompute(self.draft, self._by_product(), self.settings.weeks)

    def _by_product(self) -> dict:
        return self.current_bom.by_product if self.current_bom else {}

    def _warnings(self) -> dict:
        return {"products_without_bom": self.products_without_bom()}

    def products_without_bom(self) -> list[str]:
        """草稿中用到、但当前 BOM 版本没有覆盖的产品（负荷按 0 计，仅提示）。"""
        covered = self.current_bom.products if self.current_bom else set()
        used = {product for (product, _week) in self.draft}
        return sorted(used - covered)

    def known_products(self) -> set[str]:
        return self.boms.all_products()

    def known_work_centers(self) -> set[str]:
        return self.boms.all_work_centers() | self.calendars.all_work_centers()

    def _check_week(self, week: int) -> None:
        if not (1 <= week <= self.settings.weeks):
            raise ValidationFailed(
                f"week {week} out of planning horizon 1..{self.settings.weeks}", week=week
            )

    def _check_product(self, product: str) -> None:
        if product not in self.known_products():
            raise ValidationFailed(f"unknown product: {product}", product=product)

    def _check_work_center(self, work_center: str) -> None:
        if work_center not in self.known_work_centers():
            raise NotFound(f"unknown work center: {work_center}", work_center=work_center)

    @staticmethod
    def _check_qty(qty: int) -> None:
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
            raise ValidationFailed(f"quantity must be a non-negative integer, got {qty!r}")

    @staticmethod
    def _normalize_qty(value) -> int | None:
        """批量导入用：把输入规范化为非负整数，不合法返回 None。"""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, float) and value.is_integer() and value >= 0:
            return int(value)
        return None

    @staticmethod
    def _totals(matrix: dict) -> tuple[float, float]:
        total_load = sum(sum(row["weeks"]) for row in matrix.values())
        total_past_due = sum(row["past_due"] for row in matrix.values())
        return total_load, total_past_due

    # ---------------- 元信息 ----------------
    def meta(self) -> dict:
        return {
            "weeks": self.settings.weeks,
            "current_bom_version": self.current_bom.version if self.current_bom else None,
            "current_calendar_version": (
                self.current_calendar.version if self.current_calendar else None
            ),
            "products": sorted(self.known_products()),
            "work_centers": sorted(self.known_work_centers()),
            "draft_cells": len(self.draft),
            "plan_versions": [info.version for info in self.plans.list_plan_versions()],
        }

    # ---------------- 资源清单 ----------------
    async def create_bom_version(self, lines: list[BomLine], note: str = "") -> dict:
        errors: list[dict] = []
        if not lines:
            errors.append({"error": "at least one BOM line is required"})
        seen: set[tuple[str, str]] = set()
        for i, ln in enumerate(lines):
            if not ln.product or not ln.work_center:
                errors.append({"index": i, "error": "product and work_center must be non-empty"})
            if not math.isfinite(ln.hours_per_unit) or ln.hours_per_unit < 0:
                errors.append({"index": i, "error": "hours_per_unit must be a finite number >= 0"})
            if not math.isfinite(ln.lead_weeks) or ln.lead_weeks < 0:
                errors.append({"index": i, "error": "lead_weeks must be a finite number >= 0"})
            key = (ln.product, ln.work_center)
            if key in seen:
                errors.append(
                    {
                        "index": i,
                        "error": f"duplicate BOM line for product={ln.product} "
                        f"work_center={ln.work_center}",
                    }
                )
            seen.add(key)
        if errors:
            raise ValidationFailed("invalid BOM version", errors=errors)
        async with self.lock:
            version = self.boms.create(lines, note)
            self.current_bom = version
            # BOM 是负荷展开的结构输入，换版本后草稿负荷矩阵全量重建
            self.matrix = LoadMatrix.recompute(self.draft, self._by_product(), self.settings.weeks)
        return self._bom_payload(version)

    def _bom_payload(self, version: BomVersion) -> dict:
        return {
            "version": version.version,
            "note": version.note,
            "created_at": version.created_at,
            "lines": [
                {
                    "product": ln.product,
                    "work_center": ln.work_center,
                    "hours_per_unit": ln.hours_per_unit,
                    "lead_weeks": ln.lead_weeks,
                }
                for ln in version.lines
            ],
            "products": sorted(version.products),
            "work_centers": sorted(version.work_centers),
            "warnings": self._warnings(),
        }

    def list_bom_versions(self) -> list[dict]:
        return self.boms.list()

    def bom_payload(self, version: int) -> dict:
        bom = self.boms.get(version)
        if bom is None:
            raise NotFound(f"BOM version {version} not found", bom_version=version)
        return self._bom_payload(bom)

    def current_bom_payload(self) -> dict:
        if self.current_bom is None:
            raise NotFound("no BOM version exists")
        return self._bom_payload(self.current_bom)

    # ---------------- 能力日历 ----------------
    async def create_calendar_version(
        self, entries: list[tuple[str, int, float]], note: str = ""
    ) -> dict:
        errors: list[dict] = []
        if not entries:
            errors.append({"error": "at least one calendar entry is required"})
        seen: set[tuple[str, int]] = set()
        for i, (wc, week, hours) in enumerate(entries):
            if not wc:
                errors.append({"index": i, "error": "work_center must be non-empty"})
            if not (1 <= week <= self.settings.weeks):
                errors.append(
                    {
                        "index": i,
                        "work_center": wc,
                        "week": week,
                        "error": f"week out of planning horizon 1..{self.settings.weeks}",
                    }
                )
            if not math.isfinite(hours) or hours < 0:
                errors.append({"index": i, "error": "available_hours must be a finite number >= 0"})
            if (wc, week) in seen:
                errors.append(
                    {"index": i, "error": f"duplicate calendar entry for {wc} week {week}"}
                )
            seen.add((wc, week))
        if errors:
            raise ValidationFailed("invalid calendar version", errors=errors)
        async with self.lock:
            version = self.calendars.create(entries, note)
            self.current_calendar = version
            # 日历只影响能力对比，不影响负荷矩阵，无需重建
        return self._calendar_payload(version)

    def _calendar_payload(self, version: CalendarVersion) -> dict:
        return {
            "version": version.version,
            "note": version.note,
            "created_at": version.created_at,
            "entries": [
                {"work_center": wc, "week": w, "available_hours": h}
                for (wc, w), h in sorted(version.entries.items())
            ],
            "work_centers": sorted(version.work_centers),
        }

    def list_calendar_versions(self) -> list[dict]:
        return self.calendars.list()

    def calendar_payload(self, version: int) -> dict:
        cal = self.calendars.get(version)
        if cal is None:
            raise NotFound(f"calendar version {version} not found", calendar_version=version)
        return self._calendar_payload(cal)

    def current_calendar_payload(self) -> dict:
        if self.current_calendar is None:
            raise NotFound("no calendar version exists")
        return self._calendar_payload(self.current_calendar)

    # ---------------- 草稿 ----------------
    def get_draft(self) -> dict:
        cells = [
            {"product": p, "week": w, "qty": q}
            for (p, w), q in sorted(self.draft.items())
        ]
        return {"cells": cells, "warnings": self._warnings()}

    def _apply_cell(self, product: str, week: int, qty: int) -> None:
        old = self.draft.get((product, week), 0)
        if qty == 0:
            self.draft.pop((product, week), None)
        else:
            self.draft[(product, week)] = qty
        self.matrix.apply_cell_delta(product, week, old, qty, self._by_product())

    async def edit_cell(
        self, product: str, week: int, qty: int, expected_old_qty: int | None
    ) -> dict:
        self._check_product(product)
        self._check_week(week)
        self._check_qty(qty)
        async with self.lock:
            old = self.draft.get((product, week), 0)
            if expected_old_qty is not None and expected_old_qty != old:
                raise Conflict(
                    f"cell ({product}, week {week}) was changed by someone else",
                    product=product,
                    week=week,
                    current_qty=old,
                )
            self._apply_cell(product, week, qty)
            self.plans.set_draft_cell(product, week, qty)
        return {
            "product": product,
            "week": week,
            "old_qty": old,
            "new_qty": qty,
            "warnings": self._warnings(),
        }

    async def batch_edit(self, cells: list[BatchCell]) -> dict:
        """批量导入：整批逐行校验，任一失败则整批不应用（错误按行号汇总）；
        批内按到达顺序生效，后一行能看到前一行的结果。"""
        errors: list[dict] = []
        known = self.known_products()
        normalized: list[BatchCell] = []
        for i, c in enumerate(cells):
            ok = True
            if not isinstance(c.product, str) or not c.product.strip():
                errors.append({"index": i, "error": "product must be a non-empty string"})
                ok = False
            elif c.product not in known:
                errors.append(
                    {"index": i, "product": c.product, "error": f"unknown product: {c.product}"}
                )
                ok = False
            if (
                isinstance(c.week, bool)
                or not isinstance(c.week, int)
                or not (1 <= c.week <= self.settings.weeks)
            ):
                errors.append(
                    {
                        "index": i,
                        "product": c.product,
                        "week": c.week,
                        "error": f"week must be an integer in 1..{self.settings.weeks}",
                    }
                )
                ok = False
            qty = self._normalize_qty(c.qty)
            if qty is None:
                errors.append(
                    {"index": i, "product": c.product, "error": "quantity must be a non-negative integer"}
                )
                ok = False
            expected = None
            if c.expected_old_qty is not None:
                expected = self._normalize_qty(c.expected_old_qty)
                if expected is None:
                    errors.append(
                        {
                            "index": i,
                            "product": c.product,
                            "error": "expected_old_qty must be a non-negative integer",
                        }
                    )
                    ok = False
            if ok:
                normalized.append(BatchCell(c.product, c.week, qty, expected))
        if errors:
            raise ValidationFailed("batch rejected; nothing was applied", errors=errors)
        async with self.lock:
            # 先在副本上按顺序模拟，校验每行的 CAS 条件（后一行能看到前一行的结果）
            sim = dict(self.draft)
            cas_errors: list[dict] = []
            for i, c in enumerate(normalized):
                if c.expected_old_qty is not None:
                    current = sim.get((c.product, c.week), 0)
                    if c.expected_old_qty != current:
                        cas_errors.append(
                            {
                                "index": i,
                                "product": c.product,
                                "week": c.week,
                                "error": "expected_old_qty mismatch",
                                "current_qty": current,
                            }
                        )
                if c.qty == 0:
                    sim.pop((c.product, c.week), None)
                else:
                    sim[(c.product, c.week)] = c.qty
            if cas_errors:
                raise ValidationFailed("batch rejected; nothing was applied", errors=cas_errors)
            for c in normalized:
                self._apply_cell(c.product, c.week, c.qty)
            self.plans.set_draft_cells([(c.product, c.week, c.qty) for c in normalized])
        return {"applied": len(normalized), "warnings": self._warnings()}

    # ---------------- 负荷查询 ----------------
    def _load_payload(
        self,
        source: str,
        matrix: dict,
        bom_version: int | None,
        calendar_version: int | None,
        work_center: str | None,
        week: int | None,
        plan_version: int | None = None,
    ) -> dict:
        if work_center is not None:
            self._check_work_center(work_center)
        if week is not None:
            self._check_week(week)
        payload: dict = {
            "source": source,
            "bom_version": bom_version,
            "calendar_version": calendar_version,
            "weeks": self.settings.weeks,
        }
        if plan_version is not None:
            payload["plan_version"] = plan_version
        if work_center is not None and week is not None:
            row = matrix.get(work_center)
            payload["work_center"] = work_center
            payload["week"] = week
            payload["load"] = row["weeks"][week - 1] if row else 0.0
            return payload
        if week is not None:
            payload["week"] = week
            payload["loads"] = {
                wc: row["weeks"][week - 1]
                for wc, row in matrix.items()
                if row["weeks"][week - 1] != 0.0
            }
            return payload
        if work_center is not None:
            row = matrix.get(
                work_center, {"weeks": [0.0] * self.settings.weeks, "past_due": 0.0}
            )
            payload["matrix"] = {work_center: row}
            return payload
        total_load, total_past_due = self._totals(matrix)
        payload["matrix"] = matrix
        payload["total_load"] = total_load
        payload["total_past_due"] = total_past_due
        return payload

    def draft_load(self, work_center: str | None = None, week: int | None = None) -> dict:
        payload = self._load_payload(
            "draft",
            self.matrix.to_matrix_dict(),
            self.current_bom.version if self.current_bom else None,
            self.current_calendar.version if self.current_calendar else None,
            work_center,
            week,
        )
        payload["warnings"] = self._warnings()
        return payload

    def draft_overloads(self) -> dict:
        if self.current_calendar is None:
            raise Conflict("no calendar version exists; cannot evaluate overloads")
        matrix = self.matrix.to_matrix_dict()
        return {
            "source": "draft",
            "calendar_version": self.current_calendar.version,
            "overloads": overload_cells(matrix, self.current_calendar, self.settings.weeks),
            "past_due": past_due_rows(matrix),
            "warnings": self._warnings(),
        }

    # ---------------- 发布与版本 ----------------
    async def publish(self, note: str = "") -> dict:
        if self.current_bom is None:
            raise Conflict("cannot publish: no BOM version exists")
        if self.current_calendar is None:
            raise Conflict("cannot publish: no calendar version exists")
        async with self.lock:
            cells = dict(self.draft)
            info = self.plans.create_plan_version(
                cells, self.current_bom.version, self.current_calendar.version, note
            )
            # 发布快照用全量展开生成（历史记录，绑定当前 BOM/日历版本）
            matrix = LoadMatrix.recompute(
                cells, self._by_product(), self.settings.weeks
            ).to_matrix_dict()
            self.plans.save_load_result(
                "publish", info.version, info.bom_version, info.calendar_version, matrix
            )
        total_load, total_past_due = self._totals(matrix)
        return {
            "version": info.version,
            "bom_version": info.bom_version,
            "calendar_version": info.calendar_version,
            "cell_count": info.cell_count,
            "total_load": total_load,
            "total_past_due": total_past_due,
            "warnings": self._warnings(),
        }

    def list_plan_versions(self) -> dict:
        return {"versions": [asdict(info) for info in self.plans.list_plan_versions()]}

    def get_plan_version(self, version: int) -> dict:
        info = self.plans.get_plan_version(version)
        if info is None:
            raise NotFound(f"plan version {version} not found", plan_version=version)
        cells = self.plans.get_plan_cells(version)
        return {
            **asdict(info),
            "cells": [
                {"product": p, "week": w, "qty": q} for (p, w), q in sorted(cells.items())
            ],
        }

    def _publish_result(self, version: int):
        record = self.plans.get_publish_result(version)
        if record is None:
            raise NotFound(f"plan version {version} not found", plan_version=version)
        return record

    def version_load(
        self, version: int, work_center: str | None = None, week: int | None = None
    ) -> dict:
        record = self._publish_result(version)
        return self._load_payload(
            "plan_version",
            record.matrix,
            record.bom_version,
            record.calendar_version,
            work_center,
            week,
            plan_version=version,
        )

    def version_overloads(self, version: int) -> dict:
        record = self._publish_result(version)
        calendar = self.calendars.get(record.calendar_version)
        return {
            "source": "plan_version",
            "plan_version": version,
            "calendar_version": record.calendar_version,
            "overloads": overload_cells(record.matrix, calendar, self.settings.weeks),
            "past_due": past_due_rows(record.matrix),
        }

    def version_results(self, version: int) -> dict:
        self._publish_result(version)  # 不存在则 404
        results = []
        for r in self.plans.get_load_results(version):
            total_load, total_past_due = self._totals(r.matrix)
            results.append(
                {
                    "result_id": r.id,
                    "kind": r.kind,
                    "plan_version": r.plan_version,
                    "bom_version": r.bom_version,
                    "calendar_version": r.calendar_version,
                    "created_at": r.created_at,
                    "total_load": total_load,
                    "total_past_due": total_past_due,
                }
            )
        return {"results": results}

    def compare_versions(self, a: int, b: int, top: int = 10) -> dict:
        ra = self._publish_result(a)
        rb = self._publish_result(b)
        cal_a = self.calendars.get(ra.calendar_version)
        cal_b = self.calendars.get(rb.calendar_version)
        comparison = compare_results(ra.matrix, rb.matrix, cal_a, cal_b, self.settings.weeks, top)
        return {
            "a": {
                "plan_version": a,
                "bom_version": ra.bom_version,
                "calendar_version": ra.calendar_version,
            },
            "b": {
                "plan_version": b,
                "bom_version": rb.bom_version,
                "calendar_version": rb.calendar_version,
            },
            **comparison,
        }

    async def recompute_version(
        self, version: int, bom_version: int | None, calendar_version: int | None
    ) -> dict:
        """按新清单（和/或新日历）重算已发布计划的负荷，存为新结果并与原结果对比。"""
        original = self._publish_result(version)
        bom = self.boms.get(bom_version) if bom_version is not None else self.current_bom
        if bom is None:
            raise NotFound(
                f"BOM version {bom_version} not found"
                if bom_version is not None
                else "no BOM version exists"
            )
        calendar = (
            self.calendars.get(calendar_version)
            if calendar_version is not None
            else self.current_calendar
        )
        if calendar is None:
            raise NotFound(
                f"calendar version {calendar_version} not found"
                if calendar_version is not None
                else "no calendar version exists"
            )
        cells = self.plans.get_plan_cells(version)
        async with self.lock:
            matrix = LoadMatrix.recompute(cells, bom.by_product, self.settings.weeks).to_matrix_dict()
            result_id = self.plans.save_load_result(
                "recompute", version, bom.version, calendar.version, matrix
            )
        cal_original = self.calendars.get(original.calendar_version)
        comparison = compare_results(
            original.matrix, matrix, cal_original, calendar, self.settings.weeks, top=10
        )
        total_load, total_past_due = self._totals(matrix)
        return {
            "result": {
                "result_id": result_id,
                "kind": "recompute",
                "plan_version": version,
                "bom_version": bom.version,
                "calendar_version": calendar.version,
                "matrix": matrix,
                "total_load": total_load,
                "total_past_due": total_past_due,
            },
            "comparison_vs_publish": {
                "a": {
                    "plan_version": version,
                    "bom_version": original.bom_version,
                    "calendar_version": original.calendar_version,
                },
                "b": {
                    "plan_version": version,
                    "bom_version": bom.version,
                    "calendar_version": calendar.version,
                },
                **comparison,
            },
        }
