"""展开引擎：核对例子、四条性质、小数提前量与期初之前各策略。"""

from __future__ import annotations

import numpy as np

from app.explosion import (
    ExplosionEngine,
    RoutingLine,
    compare_loads,
    overload_cells,
)


def make_engine(horizon=6, lead_split="proportional", pre_horizon="overdue",
                lines=None):
    if lines is None:
        lines = [
            RoutingLine("P1", "W1", 2.0, 1.0),
            RoutingLine("P1", "W2", 0.5, 0.0),
            RoutingLine("P2", "W1", 1.0, 1.5),
        ]
    return ExplosionEngine(
        product_codes=["P1", "P2"],
        workcenter_codes=["W1", "W2"],
        horizon=horizon,
        lines=lines,
        lead_split=lead_split,
        pre_horizon=pre_horizon,
    )


def test_reference_example():
    """题面核对例子：P 第3周 10 件 → W1 第2周 20h，W2 第3周 5h。"""
    eng = ExplosionEngine(
        product_codes=["P"],
        workcenter_codes=["W1", "W2"],
        horizon=5,
        lines=[
            RoutingLine("P", "W1", 2.0, 1.0),
            RoutingLine("P", "W2", 0.5, 0.0),
        ],
    )
    P = np.zeros((1, 5))
    P[0, 2] = 10
    L, overdue = eng.explode_all(P)
    assert L[0, 1] == 20.0
    assert L[1, 2] == 5.0
    assert L.sum() == 25.0
    assert overdue.sum() == 0.0


def test_fractional_lead_proportional_split():
    """提前 1.5 周、第3周完工 → 目标周 0.5（内部），按比例拆到第1/2周各一半。"""
    eng = ExplosionEngine(
        ["P"], ["W"], 4, [RoutingLine("P", "W", 2.0, 1.5)]
    )
    P = np.zeros((1, 4))
    P[0, 2] = 10
    L, overdue = eng.explode_all(P)
    assert np.allclose(L[0], [10, 10, 0, 0])
    assert overdue[0] == 0.0


def test_fractional_lead_pre_horizon_overdue():
    """第2周完工、提前1.5周 → 一半落在第1周之前，进逾期负荷。"""
    eng = ExplosionEngine(
        ["P"], ["W"], 4, [RoutingLine("P", "W", 2.0, 1.5)]
    )
    P = np.zeros((1, 4))
    P[0, 1] = 10
    L, overdue = eng.explode_all(P)
    assert np.allclose(L[0], [10, 0, 0, 0])
    assert overdue[0] == 10.0


def test_pre_horizon_accumulate_is_pessimistic():
    eng = ExplosionEngine(
        ["P"], ["W"], 4, [RoutingLine("P", "W", 2.0, 1.5)],
        pre_horizon="accumulate",
    )
    P = np.zeros((1, 4))
    P[0, 1] = 10
    L, overdue = eng.explode_all(P)
    # 逾期 10h 全部压到第 1 周，连同原本在第1周的 10h
    assert L[0, 0] == 20.0
    assert overdue[0] == 0.0


def test_pre_horizon_discard_is_optimistic_and_breaks_conservation():
    eng = ExplosionEngine(
        ["P"], ["W"], 4, [RoutingLine("P", "W", 2.0, 1.5)],
        pre_horizon="discard",
    )
    P = np.zeros((1, 4))
    P[0, 1] = 10
    L, overdue = eng.explode_all(P)
    assert L[0, 0] == 10.0
    assert overdue[0] == 0.0
    # 被丢弃的 10h 让负荷图看起来偏乐观
    assert L.sum() + overdue.sum() == 10.0
    assert eng.required_total_hours(P) == 20.0


def test_floor_and_ceil_split_bias():
    P = np.zeros((1, 6))
    P[0, 3] = 8  # 第4周完工
    # 内部目标周 1.5：floor 落数组下标 1（第2周），ceil 落下标 2（第3周）
    for policy, expected_idx in [("floor", 1), ("ceil", 2)]:
        eng = ExplosionEngine(
            ["P"], ["W"], 6, [RoutingLine("P", "W", 3.0, 1.5)],
            lead_split=policy,
        )
        L, _ = eng.explode_all(P)
        assert L[0, expected_idx] == 24.0
        assert L.sum() == 24.0


def test_property_total_conservation():
    """所有工作中心所有周负荷 + 逾期 = 数量×单件总工时。"""
    eng = make_engine(horizon=6)
    rng = np.random.default_rng(42)
    P = rng.integers(0, 50, size=(2, 6)).astype(float)
    L, overdue = eng.explode_all(P)
    assert abs(L.sum() + overdue.sum() - eng.required_total_hours(P)) < 1e-9


def test_property_scaling():
    """全部计划数量乘以 k，负荷（含逾期）逐格乘以 k。"""
    eng = make_engine(horizon=6)
    rng = np.random.default_rng(7)
    P = rng.integers(0, 30, size=(2, 6)).astype(float)
    L1, o1 = eng.explode_all(P)
    k = 2.5
    L2, o2 = eng.explode_all(P * k)
    assert np.allclose(L2, L1 * k)
    assert np.allclose(o2, o1 * k)


def test_property_zero_lead_lands_in_completion_week():
    eng = ExplosionEngine(
        ["P"], ["W"], 5, [RoutingLine("P", "W", 4.0, 0.0)]
    )
    P = np.zeros((1, 5))
    P[0, [0, 2, 4]] = [3, 5, 7]
    L, _ = eng.explode_all(P)
    assert np.allclose(L[0], [12, 0, 20, 0, 28])


def test_incremental_product_contribution_matches_full():
    """单产品增量贡献与全量展开中该产品的贡献一致。"""
    eng = make_engine(horizon=6)
    rng = np.random.default_rng(11)
    P = rng.integers(0, 40, size=(2, 6)).astype(float)
    L, overdue = eng.explode_all(P)
    l1, o1 = eng.explode_product("P1", P[0])
    l2, o2 = eng.explode_product("P2", P[1])
    assert np.allclose(l1 + l2, L)
    assert np.allclose(o1 + o2, overdue)


def test_overload_and_compare_flips():
    old = np.array([[10.0, 10.0], [50.0, 50.0]])
    new = np.array([[30.0, 10.0], [50.0, 20.0]])
    cap = np.array([[20.0, 20.0], [40.0, 40.0]])
    wcs = ["W1", "W2"]
    rep = compare_loads(old, new, wcs, capacity=cap)
    types = {(f["workcenter"], f["week"]): f["type"] for f in rep["flips"]}
    assert types[("W1", 1)] == "became_overload"
    assert types[("W2", 2)] == "resolved_overload"
    assert rep["top_deltas"][0]["workcenter"] in {"W1", "W2"}
    over = overload_cells(new, cap, wcs)
    assert {(o["workcenter"], o["week"]) for o in over} == {
        ("W1", 1),
        ("W2", 1),
    }
