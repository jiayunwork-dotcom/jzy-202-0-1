"""负荷展开引擎（NumPy）。

坐标约定
--------
* 周号对外是 1-based（第 1..H 周），引擎内部用 0-based 列索引。
* 计划矩阵 ``P`` 形状 ``(n_products, H)``；负荷矩阵 ``L`` 形状 ``(n_workcenters, H)``。
  行序固定为产品 / 工作中心 code 的字典序，因此不同版本之间的矩阵可以直接相减。
* 逾期负荷 ``overdue`` 形状 ``(n_workcenters,)``，表示落在第 1 周之前的部分
  （仅 ``pre_horizon='overdue'`` 策略下非零）。

展开规则
--------
某产品第 t 周（0-based）完工 q 件、在工作中心 w 上每件 h 工时、提前量 lead 周，
则负荷 q·h 的目标周为 ``center = t - lead``：

* ``lead_split='proportional'``（默认，中性）：center 非整数时，
  前一周取 floor 权重 frac、后一周取 ceil 权重 (1-frac)。
* ``lead_split='floor'``（整体归前一周，偏悲观）：负荷全部落在 floor(center)，
  一律提前，更早出现能力压力。
* ``lead_split='ceil'``（整体归后一周，偏乐观）：负荷全部落在 ceil(center)，
  一律推后，负荷图看起来更轻松。

目标周 < 0 的部分按 ``pre_horizon`` 处理：

* ``'overdue'``（默认）：单独累计到逾期负荷列，不污染任何计划周；
  保证 ``L 总和 + overdue 总和 = 数量×单件工时总和``。这是中性且可核对的口径。
* ``'accumulate'``（累到第一周，偏悲观）：全部压到第 1 周，
  第 1 周容易显示超负荷，推动计划员提前行动。
* ``'discard'``（丢弃，偏乐观）：直接不显示，负荷图最“好看”，
  但总量守恒被破坏（少掉的正是被丢弃的部分），仅建议在确认期初之前
  确无在制时使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

LeadSplit = Literal["proportional", "floor", "ceil"]
PreHorizon = Literal["overdue", "discard", "accumulate"]

LEAD_SPLITS: frozenset[str] = frozenset({"proportional", "floor", "ceil"})
PRE_HORIZON_POLICIES: frozenset[str] = frozenset({"overdue", "discard", "accumulate"})


@dataclass(frozen=True)
class RoutingLine:
    """资源清单中的一行：产品在某工作中心的单件工时与提前量（周）。"""

    product: str
    workcenter: str
    hours_per_unit: float
    lead_weeks: float


def _split_weights(
    centers: np.ndarray, lead_split: str
) -> tuple[np.ndarray, np.ndarray]:
    """返回 (前一周权重, 后一周权重)，形状与 centers 相同。

    center 为整数时后一周权重为 0，负荷只落一个桶。
    """
    lo = np.floor(centers)
    frac = centers - lo
    if lead_split == "proportional":
        w_lo = np.where(frac == 0.0, 1.0, frac)
    elif lead_split == "floor":
        w_lo = np.ones_like(frac)
    elif lead_split == "ceil":
        w_lo = np.where(frac == 0.0, 1.0, 0.0)
    else:  # pragma: no cover - 由入口校验拦截
        raise ValueError(f"未知提前量拆分规则: {lead_split!r}")
    w_hi = 1.0 - w_lo
    return w_lo, w_hi


class ExplosionEngine:
    """绑定一组产品/工作中心坐标、计划期、资源清单行与展开策略的展开器。

    清单行在构造时编入定长 ndarray；清单换版本时整体重建一个引擎即可
    （成本约为一次全量展开）。
    """

    def __init__(
        self,
        product_codes: list[str],
        workcenter_codes: list[str],
        horizon: int,
        lines: list[RoutingLine],
        lead_split: LeadSplit = "proportional",
        pre_horizon: PreHorizon = "overdue",
    ):
        if lead_split not in LEAD_SPLITS:
            raise ValueError(f"未知提前量拆分规则: {lead_split!r}")
        if pre_horizon not in PRE_HORIZON_POLICIES:
            raise ValueError(f"未知期初之前处理规则: {pre_horizon!r}")
        self.products = list(product_codes)
        self.workcenters = list(workcenter_codes)
        self.p_index = {c: i for i, c in enumerate(self.products)}
        self.w_index = {c: i for i, c in enumerate(self.workcenters)}
        self.horizon = int(horizon)
        self.lead_split = lead_split
        self.pre_horizon = pre_horizon

        # 按产品分组的行索引，增量更新时只取一个产品的行。
        self._prod_lines: dict[int, np.ndarray] = {}
        wc_idx: list[int] = []
        prod_idx: list[int] = []
        hours: list[float] = []
        leads: list[float] = []
        for line in lines:
            pi = self.p_index[line.product]
            wi = self.w_index[line.workcenter]
            wc_idx.append(wi)
            prod_idx.append(pi)
            hours.append(float(line.hours_per_unit))
            leads.append(float(line.lead_weeks))
        self.wc_arr = np.asarray(wc_idx, dtype=np.int64)
        self.prod_arr = np.asarray(prod_idx, dtype=np.int64)
        self.hours_arr = np.asarray(hours, dtype=np.float64)
        self.leads_arr = np.asarray(leads, dtype=np.float64)
        for pi in np.unique(self.prod_arr) if len(self.prod_arr) else []:
            self._prod_lines[int(pi)] = np.where(self.prod_arr == pi)[0]

        # 每个产品的单件总工时（跨所有工作中心），用于核对总量守恒。
        self.hours_per_product = np.zeros(len(self.products), dtype=np.float64)
        if len(self.hours_arr):
            np.add.at(self.hours_per_product, self.prod_arr, self.hours_arr)

        self._week_rows = np.arange(self.horizon, dtype=np.float64)

    # ---- 全量 / 增量 ----------------------------------------------------

    def explode_all(self, plan: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """全量展开 ``P (n_prod, H)`` -> ``(L (n_wc, H), overdue (n_wc,))``。

        向量化：每个源周 × 每条清单行最多产生两个贡献，用两次 bincount
        汇总到工作中心×周平面与逾期向量。几百产品 × 几十关键工作中心 × 20 周
        规模下为亚毫秒~毫秒级。
        """
        plan = np.asarray(plan, dtype=np.float64)
        if plan.shape != (len(self.products), self.horizon):
            raise ValueError(
                f"计划矩阵形状 {plan.shape} 与引擎坐标 "
                f"({len(self.products)}, {self.horizon}) 不一致"
            )
        if len(self.hours_arr) == 0:
            return (
                np.zeros((len(self.workcenters), self.horizon)),
                np.zeros(len(self.workcenters)),
            )
        # 每行清单对应的各周数量：(H, m)
        vals = plan[self.prod_arr, :].T * self.hours_arr[None, :]
        return self._scatter(vals, self.wc_arr, self.leads_arr)

    def explode_product(
        self, product: str, quantities: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """展开单个产品的数量向量，返回它对 L / overdue 的贡献。

        草稿单元格编辑时，用新值贡献减旧值贡献即可得到增量，复杂度只与
        该产品涉及的清单行数有关，与产品总数无关。
        """
        pi = self.p_index.get(product)
        if pi is None:
            raise KeyError(product)
        quantities = np.asarray(quantities, dtype=np.float64)
        if quantities.shape != (self.horizon,):
            raise ValueError(f"数量向量长度应为 {self.horizon}")
        lines = self._prod_lines.get(pi)
        load = np.zeros((len(self.workcenters), self.horizon))
        overdue = np.zeros(len(self.workcenters))
        if lines is None or not np.any(quantities):
            return load, overdue
        vals = quantities[:, None] * self.hours_arr[lines][None, :]
        return self._scatter(vals, self.wc_arr[lines], self.leads_arr[lines])

    # ---- 核心散射 -------------------------------------------------------

    def _scatter(
        self,
        vals: np.ndarray,
        wc_per_line: np.ndarray,
        leads: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """把各源周的工时值 vals(H, m) 散射到目标周。

        返回 (L(n_wc, H), overdue(n_wc,))。
        """
        H = self.horizon
        n_wc = len(self.workcenters)
        # centers[t, j] = t - lead_j  → 负荷目标周（0-based，可能为负）
        centers = self._week_rows[:, None] - leads[None, :]
        w_lo, w_hi = _split_weights(centers, self.lead_split)
        lo_idx = np.floor(centers).astype(np.int64)
        hi_idx = lo_idx + 1

        v_lo = vals * w_lo
        v_hi = vals * w_hi
        wc_grid = np.broadcast_to(wc_per_line[None, :], centers.shape)

        load = np.zeros(n_wc * H, dtype=np.float64)
        overdue = np.zeros(n_wc, dtype=np.float64)

        for idx, val in ((lo_idx, v_lo), (hi_idx, v_hi)):
            nonzero = val != 0.0
            week = idx
            if self.pre_horizon == "overdue":
                neg = nonzero & (week < 0)
                if np.any(neg):
                    overdue += np.bincount(
                        wc_grid[neg], weights=val[neg], minlength=n_wc
                    )
                valid = nonzero & (week >= 0) & (week < H)
            elif self.pre_horizon == "accumulate":
                week = np.where(week < 0, 0, week)
                valid = nonzero & (week < H)
            else:  # discard
                valid = nonzero & (week >= 0) & (week < H)
            if np.any(valid):
                flat = wc_grid[valid].astype(np.int64) * H + week[valid]
                load += np.bincount(flat, weights=val[valid], minlength=n_wc * H)

        return load.reshape(n_wc, H), overdue

    # ---- 核对与能力 -----------------------------------------------------

    def required_total_hours(self, plan: np.ndarray) -> float:
        """数量 × 单件总工时之和（守恒等式右侧，含所有产品所有工作中心）。"""
        plan = np.asarray(plan, dtype=np.float64)
        return float(np.sum(plan * self.hours_per_product[:, None]))

    def build_capacity(
        self, sparse: list[tuple[str, int, float]]
    ) -> np.ndarray:
        """把 (工作中心, 1-based 周号, 可用工时) 稀疏行拼成 (n_wc, H) 矩阵。

        未出现在日历中的工作中心/周按 0 工时处理（默认不可用，口径偏保守）。
        """
        cap = np.zeros((len(self.workcenters), self.horizon))
        for wc, week, hours in sparse:
            wi = self.w_index.get(wc)
            if wi is None:
                raise KeyError(wc)
            if not 1 <= week <= self.horizon:
                raise ValueError(f"能力周号超出计划期: {week}")
            cap[wi, week - 1] = float(hours)
        return cap


# ---- 对比分析 -------------------------------------------------------------


@dataclass(frozen=True)
class CellDelta:
    workcenter: str
    week: int
    old_load: float
    new_load: float

    @property
    def delta(self) -> float:
        return self.new_load - self.old_load


def compare_loads(
    old_load: np.ndarray,
    new_load: np.ndarray,
    workcenters: list[str],
    *,
    capacity: np.ndarray | None = None,
    old_capacity: np.ndarray | None = None,
    new_capacity: np.ndarray | None = None,
    top_n: int = 20,
) -> dict[str, object]:
    """对比两份同形状负荷矩阵。

    返回：
    * ``top_deltas``：绝对差异最大的格子（降序）；
    * ``flips``：列出在旧/新结果之间“超负荷状态翻转”的格子。
      超负荷定义为严格 ``load > capacity``。计划版本对比时两侧共用同一份
      能力（传 ``capacity``）；按新日历重算时两侧日历不同，分别传
      ``old_capacity`` / ``new_capacity``，此时“旧超负荷、新解除”这类
      纯由能力变化产生的翻转也会被识别，并标注 cause；
    * ``sum_abs_delta`` / ``max_abs_delta``：整体扰动度量。
    """
    if old_load.shape != new_load.shape:
        raise ValueError("两份负荷矩阵形状不同，无法对比")
    if capacity is not None:
        old_capacity = old_capacity if old_capacity is not None else capacity
        new_capacity = new_capacity if new_capacity is not None else capacity
    delta = new_load - old_load
    abs_delta = np.abs(delta)
    flat_order = np.argsort(-abs_delta.ravel(), kind="stable")
    top: list[CellDelta] = []
    for flat in flat_order:
        if abs_delta.ravel()[flat] == 0.0:
            break
        wi, ti = np.unravel_index(flat, delta.shape)
        top.append(
            CellDelta(
                workcenter=workcenters[int(wi)],
                week=int(ti) + 1,
                old_load=float(old_load[wi, ti]),
                new_load=float(new_load[wi, ti]),
            )
        )
        if len(top) >= top_n:
            break

    flips: list[dict[str, object]] = []
    if new_capacity is not None and old_capacity is not None:
        if old_capacity.shape != old_load.shape or new_capacity.shape != new_load.shape:
            raise ValueError("能力矩阵形状与负荷矩阵不一致")
        old_over = old_load > old_capacity
        new_over = new_load > new_capacity
        flip_mask = old_over != new_over
        for wi, ti in np.argwhere(flip_mask):
            same_load = old_load[wi, ti] == new_load[wi, ti]
            same_cap = old_capacity[wi, ti] == new_capacity[wi, ti]
            flips.append(
                {
                    "workcenter": workcenters[int(wi)],
                    "week": int(ti) + 1,
                    "type": "became_overload"
                    if new_over[wi, ti]
                    else "resolved_overload",
                    "old_load": float(old_load[wi, ti]),
                    "new_load": float(new_load[wi, ti]),
                    "old_capacity": float(old_capacity[wi, ti]),
                    "new_capacity": float(new_capacity[wi, ti]),
                    "cause": (
                        "capacity_changed"
                        if same_load and not same_cap
                        else ("load_changed" if same_cap else "both_changed")
                    ),
                }
            )
        flips.sort(key=lambda d: (d["week"], d["workcenter"]))

    return {
        "top_deltas": [
            {
                "workcenter": c.workcenter,
                "week": c.week,
                "old_load": c.old_load,
                "new_load": c.new_load,
                "delta": c.delta,
            }
            for c in top
        ],
        "flips": flips,
        "sum_abs_delta": float(abs_delta.sum()),
        "max_abs_delta": float(abs_delta.max(initial=0.0)),
    }


def overload_cells(
    load: np.ndarray,
    capacity: np.ndarray,
    workcenters: list[str],
) -> list[dict[str, object]]:
    """列出超负荷格子（严格超过能力），按超出量降序。"""
    over = load > capacity
    excess = np.where(over, load - capacity, 0.0)
    order = np.argsort(-excess.ravel(), kind="stable")
    result: list[dict[str, object]] = []
    for flat in order:
        if excess.ravel()[flat] == 0.0:
            break
        wi, ti = np.unravel_index(flat, load.shape)
        result.append(
            {
                "workcenter": workcenters[int(wi)],
                "week": int(ti) + 1,
                "load": float(load[wi, ti]),
                "capacity": float(capacity[wi, ti]),
                "excess": float(excess[wi, ti]),
            }
        )
    return result
