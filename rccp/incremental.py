"""草稿负荷矩阵的增量维护。

设计取舍（详见 docs/DESIGN.md 第 2 节）：草稿的负荷矩阵常驻内存，
每次单元格编辑只加/减该产品各 BOM 行对应的差量（O(每产品 BOM 行数)），
而不是每次从头展开。矩阵本身不持久化——SQLite 里只存计划单元格，
服务启动时从持久化的草稿一次全量展开重建（毫秒级），
由测试保证增量维护的结果与全量展开逐格一致。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

from .bom import BomLine
from .explosion import PAST_DUE, add_load, explode


class LoadMatrix:
    """{工作中心: float64[weeks + 1]}，列 0 为逾期负荷。"""

    def __init__(self, weeks: int, loads: dict[str, np.ndarray] | None = None):
        self.weeks = weeks
        self.loads: dict[str, np.ndarray] = loads if loads is not None else {}

    @classmethod
    def recompute(
        cls,
        plan_cells: Mapping[tuple[str, int], int],
        by_product: Mapping[str, Sequence[BomLine]],
        weeks: int,
    ) -> "LoadMatrix":
        """全量展开。用于启动恢复、BOM 版本切换、发布时生成快照。"""
        return cls(weeks, explode(plan_cells, by_product, weeks))

    def apply_cell_delta(
        self,
        product: str,
        week: int,
        old_qty: int,
        new_qty: int,
        by_product: Mapping[str, Sequence[BomLine]],
    ) -> None:
        """单个计划格从 old_qty 改为 new_qty 的增量更新。"""
        delta = new_qty - old_qty
        if delta == 0:
            return
        for ln in by_product.get(product, ()):
            arr = self.loads.get(ln.work_center)
            if arr is None:
                arr = np.zeros(self.weeks + 1, dtype=np.float64)
                self.loads[ln.work_center] = arr
            add_load(arr, week, delta * ln.hours_per_unit, ln.lead_weeks)

    def row(self, work_center: str) -> np.ndarray:
        arr = self.loads.get(work_center)
        return arr if arr is not None else np.zeros(self.weeks + 1, dtype=np.float64)

    def to_matrix_dict(self, prune_zeros: bool = True) -> dict[str, dict]:
        """转成 JSON 友好的 {wc: {"weeks": [...], "past_due": x}} 形式。"""
        out: dict[str, dict] = {}
        for wc in sorted(self.loads):
            arr = self.loads[wc]
            if prune_zeros and not np.any(arr):
                continue
            out[wc] = {
                "weeks": [float(x) for x in arr[1:]],
                "past_due": float(arr[PAST_DUE]),
            }
        return out
