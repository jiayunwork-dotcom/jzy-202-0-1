"""随机一连串修改下，增量维护的负荷矩阵与从头展开逐格相同。

每一步随机执行：
* 改某个格子（置新数量）；
* 批量 merge 若干格；
* 批量 replace 整张数量表。

每步之后同时要求：
1. 内存增量矩阵 L/overdue 与对当前数量表全量 explode_all 逐格相同；
2. 落库的 draft 结果行反序列化后也与全量结果一致（持久化层面一致）。
"""

from __future__ import annotations

import numpy as np

from app.models import BulkImport, CellEdit, DraftCreate, PlanCellIn


def _random_state(rng, n_products, horizon):
    return rng.integers(0, 40, size=(n_products, horizon)).astype(float)


def test_incremental_matches_full_random_chain(svc, seeded):
    n_products = 2
    horizon = 12
    draft = svc.planner.create_draft(DraftCreate(name="d"))
    st = svc.planner.state(draft["id"])

    rng = np.random.default_rng(20261006)
    truth = np.zeros((n_products, horizon))
    products = ["P1", "P2"]

    for step in range(400):
        kind = rng.choice(["cell", "merge", "replace"], p=[0.5, 0.3, 0.2])
        if kind == "cell":
            p = products[rng.integers(n_products)]
            week = int(rng.integers(1, horizon + 1))
            q = int(rng.integers(0, 40))
            svc.planner.edit_cell(
                draft["id"],
                CellEdit(product=p, week=week, quantity=q),
            )
            truth[products.index(p), week - 1] = q
        elif kind == "merge":
            picked: dict[tuple[str, int], int] = {}
            for _ in range(rng.integers(1, 6)):
                p = products[rng.integers(n_products)]
                week = int(rng.integers(1, horizon + 1))
                q = int(rng.integers(0, 40))
                picked[(p, week)] = q  # 同格去重，后写覆盖
            cells = [
                PlanCellIn(product=p, week=w, quantity=q)
                for (p, w), q in picked.items()
            ]
            for (p, w), q in picked.items():
                truth[products.index(p), w - 1] = q
            svc.planner.bulk_import(
                draft["id"], BulkImport(mode="merge", cells=cells)
            )
        else:
            new_table = _random_state(rng, n_products, horizon)
            truth = new_table
            cells = [
                PlanCellIn(product=products[pi], week=w + 1, quantity=int(new_table[pi, w]))
                for pi in range(n_products)
                for w in range(horizon)
                if new_table[pi, w] > 0
            ]
            svc.planner.bulk_import(
                draft["id"], BulkImport(mode="replace", cells=cells)
            )

        st = svc.planner.state(draft["id"])
        full_L, full_overdue = st.engine.explode_all(st.P)
        assert np.allclose(st.L, full_L, atol=1e-9), f"step {step} L 不一致"
        assert np.allclose(st.overdue, full_overdue, atol=1e-9), (
            f"step {step} overdue 不一致"
        )
        assert np.allclose(st.P, truth), f"step {step} 数量矩阵与操作真值不一致"

        stored = svc.results.latest_for_plan(draft["id"], "draft")
        assert np.allclose(stored["load"], full_L, atol=1e-9), (
            f"step {step} 落库矩阵不一致"
        )
        assert np.allclose(stored["overdue"], full_overdue, atol=1e-9)

    # 收尾：守恒
    assert abs(
        st.L.sum() + st.overdue.sum() - st.engine.required_total_hours(st.P)
    ) < 1e-9


def test_edit_back_restores_load(svc, seeded):
    """改数量再改回原值，负荷恢复到修改前。"""
    draft = svc.planner.create_draft(DraftCreate(name="d"))
    st = svc.planner.state(draft["id"])
    svc.planner.edit_cell(draft["id"], CellEdit(product="P1", week=4, quantity=10))
    L_after = st.L.copy()
    od_after = st.overdue.copy()
    svc.planner.edit_cell(draft["id"], CellEdit(product="P1", week=4, quantity=18))
    assert not np.allclose(st.L, L_after)
    svc.planner.edit_cell(draft["id"], CellEdit(product="P1", week=4, quantity=10))
    assert np.allclose(st.L, L_after)
    assert np.allclose(st.overdue, od_after)
    # 改回零等价于从未存在
    svc.planner.edit_cell(draft["id"], CellEdit(product="P1", week=4, quantity=10))
    svc.planner.edit_cell(draft["id"], CellEdit(product="P1", week=4, quantity=0))
    full_L, full_od = st.engine.explode_all(st.P)
    assert np.allclose(st.L, full_L)
    assert st.L.sum() == 0.0
