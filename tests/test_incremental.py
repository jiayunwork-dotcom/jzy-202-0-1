"""增量维护 vs 全量展开：随机修改序列下两者必须逐格相同；
数量改回原值后负荷必须恢复。"""
from __future__ import annotations

import random

import numpy as np

from rccp.bom import BomLine
from rccp.explosion import explode
from rccp.incremental import LoadMatrix

WEEKS = 20


def by_product(lines):
    out: dict[str, list[BomLine]] = {}
    for ln in lines:
        out.setdefault(ln.product, []).append(ln)
    return out


def assert_matrix_matches_full(matrix: LoadMatrix, plan, bp, weeks):
    """增量矩阵与从头展开逐格比较（缺失的工作中心按全零处理）。"""
    full = explode(plan, bp, weeks)
    for wc in set(matrix.loads) | set(full):
        a = matrix.loads.get(wc)
        b = full.get(wc)
        zeros = np.zeros(weeks + 1)
        actual = a if a is not None else zeros
        expected = b if b is not None else zeros
        assert np.array_equal(actual, expected), f"work center {wc} differs"


def random_bom(rng, products, work_centers):
    lines = []
    for p in products:
        for wc in rng.sample(work_centers, rng.randint(1, 4)):
            lines.append(
                BomLine(
                    p,
                    wc,
                    rng.choice([0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]),
                    rng.choice([0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0]),
                )
            )
    return lines


def test_random_edit_sequence_matches_full_recompute():
    """2000 次随机编辑（中途还切换一次 BOM 版本），增量矩阵始终与全量展开逐格相同。

    工时/提前量取二进制有限小数，float64 下所有中间和精确 → 用 array_equal 严格断言。
    """
    rng = random.Random(20261006)
    products = [f"P{i}" for i in range(25)]
    work_centers = [f"W{j}" for j in range(10)]
    bp = by_product(random_bom(rng, products, work_centers))
    plan: dict[tuple[str, int], int] = {}
    matrix = LoadMatrix.recompute(plan, bp, WEEKS)

    for i in range(2000):
        if i == 1000:
            # 模拟"资源清单发布新版本"：换一份 BOM，服务端的处理是全量重建
            bp = by_product(random_bom(rng, products, work_centers))
            matrix = LoadMatrix.recompute(plan, bp, WEEKS)
        product = rng.choice(products)
        week = rng.randint(1, WEEKS)
        qty = rng.randint(0, 50)
        old = plan.get((product, week), 0)
        matrix.apply_cell_delta(product, week, old, qty, bp)
        if qty:
            plan[(product, week)] = qty
        else:
            plan.pop((product, week), None)
        if i % 100 == 0 or i == 1999:
            assert_matrix_matches_full(matrix, plan, bp, WEEKS)

    # 顺带验证总量守恒（含逾期）
    total = sum(arr.sum() for arr in matrix.loads.values())
    expected = sum(
        qty * sum(ln.hours_per_unit for ln in bp[product])
        for (product, _w), qty in plan.items()
    )
    assert total == expected


def test_non_dyadic_values_match_within_float_tolerance():
    """非二进制小数（0.3、0.1…）时允许 1e-12 级浮点误差。"""
    rng = random.Random(99)
    products = [f"P{i}" for i in range(10)]
    work_centers = [f"W{j}" for j in range(4)]
    lines = [
        BomLine(p, wc, rng.choice([0.1, 0.3, 0.7, 1.1]), rng.choice([0.1, 0.3, 1.1, 1.7]))
        for p in products
        for wc in rng.sample(work_centers, 2)
    ]
    bp = by_product(lines)
    plan: dict[tuple[str, int], int] = {}
    matrix = LoadMatrix.recompute(plan, bp, WEEKS)
    for _ in range(500):
        product = rng.choice(products)
        week = rng.randint(1, WEEKS)
        qty = rng.randint(0, 30)
        old = plan.get((product, week), 0)
        matrix.apply_cell_delta(product, week, old, qty, bp)
        if qty:
            plan[(product, week)] = qty
        else:
            plan.pop((product, week), None)
    full = explode(plan, bp, WEEKS)
    for wc in set(matrix.loads) | set(full):
        a = matrix.loads.get(wc, np.zeros(WEEKS + 1))
        b = full.get(wc, np.zeros(WEEKS + 1))
        assert np.allclose(a, b, rtol=1e-12, atol=1e-9), wc


def test_revert_restores_load_exactly():
    """某产品数量改回原值，负荷恢复到修改前（逐格 bit 级一致）。"""
    lines = [
        BomLine("P1", "W1", 2.0, 1.0),
        BomLine("P1", "W2", 0.5, 1.5),
        BomLine("P2", "W1", 1.25, 0.75),
    ]
    bp = by_product(lines)
    plan = {("P1", 3): 10, ("P2", 8): 6}
    matrix = LoadMatrix.recompute(plan, bp, WEEKS)
    before = {wc: arr.copy() for wc, arr in matrix.loads.items()}

    # P1 第 3 周 10 → 25 → 10
    matrix.apply_cell_delta("P1", 3, 10, 25, bp)
    matrix.apply_cell_delta("P1", 3, 25, 10, bp)
    # P2 第 8 周 6 → 0 → 6（经过删除再恢复）
    matrix.apply_cell_delta("P2", 8, 6, 0, bp)
    matrix.apply_cell_delta("P2", 8, 0, 6, bp)

    for wc in set(before) | set(matrix.loads):
        a = before.get(wc, np.zeros(WEEKS + 1))
        b = matrix.loads.get(wc, np.zeros(WEEKS + 1))
        assert np.array_equal(a, b), wc
