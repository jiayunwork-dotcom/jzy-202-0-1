"""负荷展开规则的单元测试：核对例子、守恒、缩放、零提前量、
小数提前量拆分、计划期之前的逾期处理。"""
from __future__ import annotations

import random

from rccp.bom import BomLine
from rccp.explosion import PAST_DUE, explode

WEEKS = 20


def by_product(lines):
    out: dict[str, list[BomLine]] = {}
    for ln in lines:
        out.setdefault(ln.product, []).append(ln)
    return out


def test_reference_example():
    """题目核对例子：P 第 3 周完工 10 件；W1 每件 2h 提前 1 周 → W1 第 2 周 20h；
    W2 每件 0.5h 提前 0 周 → W2 第 3 周 5h。"""
    lines = [BomLine("P", "W1", 2.0, 1.0), BomLine("P", "W2", 0.5, 0.0)]
    loads = explode({("P", 3): 10}, by_product(lines), WEEKS)
    assert loads["W1"][2] == 20.0
    assert loads["W2"][3] == 5.0
    assert loads["W1"].sum() == 20.0
    assert loads["W2"].sum() == 5.0
    assert set(loads) == {"W1", "W2"}


def test_zero_lead_lands_on_completion_week():
    """提前量全为零时，负荷落在完工周。"""
    lines = [BomLine("P", "W1", 1.5, 0.0)]
    loads = explode({("P", 7): 8}, by_product(lines), WEEKS)
    assert loads["W1"][7] == 12.0
    assert loads["W1"][PAST_DUE] == 0.0
    assert loads["W1"].sum() == 12.0


def test_fractional_lead_splits_between_adjacent_weeks():
    """L=1.5：一半落在 t-1，一半落在 t-2。"""
    lines = [BomLine("P", "W1", 2.0, 1.5)]
    loads = explode({("P", 3): 10}, by_product(lines), WEEKS)
    assert loads["W1"][2] == 10.0
    assert loads["W1"][1] == 10.0
    assert loads["W1"].sum() == 20.0


def test_fractional_lead_quarter():
    """L=0.25：75% 落在完工周，25% 落在前一周。"""
    lines = [BomLine("P", "W1", 4.0, 0.25)]
    loads = explode({("P", 5): 3}, by_product(lines), WEEKS)
    assert loads["W1"][5] == 9.0
    assert loads["W1"][4] == 3.0


def test_pre_horizon_load_goes_to_past_due():
    """完工周 − 提前量 < 1：负荷全部进逾期桶，不丢弃、不并入第 1 周。"""
    lines = [BomLine("P", "W1", 2.0, 3.0)]
    loads = explode({("P", 2): 5}, by_product(lines), WEEKS)
    assert loads["W1"][PAST_DUE] == 10.0
    assert loads["W1"][1:].sum() == 0.0


def test_pre_horizon_partial_split():
    """L=1.5、t=2：50% 落在第 1 周，50% 落到第 0 周 → 逾期。"""
    lines = [BomLine("P", "W1", 2.0, 1.5)]
    loads = explode({("P", 2): 4}, by_product(lines), WEEKS)
    assert loads["W1"][1] == 4.0
    assert loads["W1"][PAST_DUE] == 4.0


def _random_bom(rng, products, work_centers):
    lines = []
    for p in products:
        for wc in rng.sample(work_centers, rng.randint(1, 4)):
            lines.append(
                BomLine(
                    p,
                    wc,
                    rng.choice([0.25, 0.5, 0.75, 1.0, 1.5, 2.0]),
                    rng.choice([0.0, 0.5, 1.0, 1.5, 2.0, 3.0]),
                )
            )
    return lines


def test_conservation_random():
    """所有工作中心所有周的负荷 + 逾期 = Σ 数量 × 单件总工时。

    数据全部取二进制有限小数，float64 下求和精确，用 == 断言。
    """
    rng = random.Random(7)
    products = [f"P{i}" for i in range(30)]
    work_centers = [f"W{j}" for j in range(8)]
    lines = _random_bom(rng, products, work_centers)
    bp = by_product(lines)
    plan = {
        (p, w): rng.randint(0, 40)
        for p in products
        for w in range(1, WEEKS + 1)
        if rng.random() < 0.5
    }
    loads = explode(plan, bp, WEEKS)
    total = sum(arr.sum() for arr in loads.values())
    expected = sum(
        qty * sum(ln.hours_per_unit for ln in bp[product])
        for (product, _w), qty in plan.items()
    )
    assert total == expected


def test_scaling():
    """全部计划数量乘以 k，负荷逐格乘以 k。"""
    rng = random.Random(11)
    products = [f"P{i}" for i in range(15)]
    work_centers = [f"W{j}" for j in range(5)]
    bp = by_product(_random_bom(rng, products, work_centers))
    plan = {
        (p, w): rng.randint(0, 20)
        for p in products
        for w in range(1, WEEKS + 1)
        if rng.random() < 0.6
    }
    k = 3
    base = explode(plan, bp, WEEKS)
    scaled = explode({key: q * k for key, q in plan.items()}, bp, WEEKS)
    assert set(base) == set(scaled)
    for wc in base:
        assert (scaled[wc] == base[wc] * k).all(), wc


def test_product_without_bom_contributes_no_load():
    """BOM 未覆盖的产品不产生负荷（由 API 层给出提示）。"""
    loads = explode({("GHOST", 3): 10}, by_product([BomLine("P", "W1", 1.0, 0.0)]), WEEKS)
    assert loads == {}
