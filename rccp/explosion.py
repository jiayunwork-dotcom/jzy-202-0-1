"""负荷全量展开（纯函数）。

展开规则（详见 docs/DESIGN.md 第 1 节）：

1. 产品 p 在第 t 周完工 q 件，它在工作中心 w 上的负荷为 q × 单件工时 h，
   落在第 t − L 周（L 为该行的提前量，单位周）。
2. L 不是整数时（L = f + r，f 为整数部分、r 为小数部分），负荷按比例拆分
   到相邻两周：(1−r) 落在第 t−f 周，r 落在第 t−f−1 周。
3. 落到第 1 周之前的负荷不丢弃、也不并入第 1 周，而是累进该工作中心的
   "逾期负荷"桶（列下标 0），单独报告。这样总量严格守恒：
   所有周的负荷 + 逾期负荷 = Σ 数量 × 单件总工时。

负荷矩阵用 dict[工作中心, np.ndarray(weeks + 1)] 表示，下标 0 为逾期桶，
下标 1..weeks 对应第 1..weeks 周。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np

from .bom import BomLine

PAST_DUE = 0  # 逾期负荷所在的列下标


def split_lead(lead_weeks: float) -> tuple[int, float]:
    """把提前量拆成 (整数部分 f, 小数部分 r)，L = f + r，0 <= r < 1。"""
    whole = math.floor(lead_weeks)
    return whole, lead_weeks - whole


def add_load(arr: np.ndarray, week: int, amount: float, lead_weeks: float) -> None:
    """把 amount 的负荷按提前量规则累加进 arr（完工周为 week）。"""
    whole, frac = split_lead(lead_weeks)
    target = week - whole
    if frac > 0.0:
        _add_at(arr, target, amount * (1.0 - frac))
        _add_at(arr, target - 1, amount * frac)
    else:
        _add_at(arr, target, amount)


def _add_at(arr: np.ndarray, week: int, amount: float) -> None:
    # 提前量 >= 0 保证 week <= 计划期周数，只会向下越界（逾期）
    arr[week if week >= 1 else PAST_DUE] += amount


def explode(
    plan_cells: Mapping[tuple[str, int], int],
    by_product: Mapping[str, Sequence[BomLine]],
    weeks: int,
) -> dict[str, np.ndarray]:
    """把整份计划从头展开成负荷矩阵。

    plan_cells: {(product, week): qty}（qty <= 0 的格子忽略）
    by_product: {product: [BomLine, ...]}（当前 BOM 版本的索引）
    """
    loads: dict[str, np.ndarray] = {}
    for (product, week), qty in plan_cells.items():
        if qty <= 0:
            continue
        for ln in by_product.get(product, ()):
            arr = loads.get(ln.work_center)
            if arr is None:
                arr = np.zeros(weeks + 1, dtype=np.float64)
                loads[ln.work_center] = arr
            add_load(arr, week, qty * ln.hours_per_unit, ln.lead_weeks)
    return loads
