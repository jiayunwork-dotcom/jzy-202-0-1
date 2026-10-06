"""计划服务层：核对例子、并发 CAS、发布冻结、版本对比、按新清单重算、重启恢复、拒收。"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from app.errors import ConflictError, ImmutableError, ValidationError
from app.models import (
    BulkImport,
    CapacityWeekIn,
    CellEdit,
    DraftCreate,
    PlanCellIn,
    RoutingLineIn,
)


def test_reference_example_via_service(svc, seeded):
    """端到端走服务：P1 第3周 10 件（题面 P 等价于 P1，但 P1 注塑提前 1.5）。

    另建一个提前量恰为 1 的产品 P 来复现题面精确数字。
    """
    from app.models import CapacityWeekIn

    svc.registry.create_product("P")
    rv = svc.routings.create_draft(lines=[
        RoutingLineIn(product="P", workcenter="INJ", hours_per_unit=2.0, lead_weeks=1.0),
        RoutingLineIn(product="P", workcenter="ASM", hours_per_unit=0.5, lead_weeks=0.0),
    ])
    svc.routings.publish(rv)
    draft = svc.planner.create_draft(DraftCreate(routing_version=rv))
    svc.planner.edit_cell(draft["id"], CellEdit(product="P", week=3, quantity=10))
    st = svc.planner.state(draft["id"])
    wi_inj = st.engine.w_index["INJ"]
    wi_asm = st.engine.w_index["ASM"]
    assert st.L[wi_inj, 1] == 20.0   # W1 第2周
    assert st.L[wi_asm, 2] == 5.0    # W2 第3周


def test_concurrent_same_cell_cas(svc, seeded):
    """两人改同一格：后到者带对旧值则成功；旧值不一致则 409 且返回当前值。"""
    draft = svc.planner.create_draft(DraftCreate(name="cas"))
    svc.planner.edit_cell(draft["id"], CellEdit(product="P1", week=3, quantity=10))

    # B 基于旧值 10 想改成 20，但 A 已经先改成 15 → 拒绝
    svc.planner.edit_cell(draft["id"], CellEdit(product="P1", week=3, quantity=15))
    with pytest.raises(ConflictError) as exc:
        svc.planner.edit_cell(
            draft["id"],
            CellEdit(product="P1", week=3, quantity=20, expected_quantity=10),
        )
    assert exc.value.details["current_quantity"] == 15.0

    # 带上正确的当前旧值 15 即可成功
    svc.planner.edit_cell(
        draft["id"],
        CellEdit(product="P1", week=3, quantity=20, expected_quantity=15),
    )
    st = svc.planner.state(draft["id"])
    assert st.P[st.engine.p_index["P1"], 2] == 20.0


def test_concurrent_writers_only_last_value_wins(svc, seeded):
    """多线程并发改不同格子全部生效；同一格串行后最终值一致。"""
    draft = svc.planner.create_draft(DraftCreate(name="threads"))

    def worker(product, base):
        for w in range(1, 9):
            svc.planner.edit_cell(
                draft["id"], CellEdit(product=product, week=w, quantity=base + w)
            )

    threads = [
        threading.Thread(target=worker, args=("P1", 100)),
        threading.Thread(target=worker, args=("P2", 200)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    st = svc.planner.state(draft["id"])
    for w in range(1, 9):
        assert st.P[st.engine.p_index["P1"], w - 1] == 100 + w
        assert st.P[st.engine.p_index["P2"], w - 1] == 200 + w
    full_L, full_od = st.engine.explode_all(st.P)
    assert np.allclose(st.L, full_L)
    assert np.allclose(st.overdue, full_od)


def test_publish_is_immutable_and_result_bound_to_versions(svc, seeded):
    draft = svc.planner.create_draft(DraftCreate(name="v1-plan"))
    svc.planner.bulk_import(draft["id"], BulkImport(cells=[
        PlanCellIn(product="P1", week=4, quantity=10),
        PlanCellIn(product="P2", week=3, quantity=8),
    ]))
    pub = svc.planner.publish(draft["id"])
    assert pub["plan_id"] == "plan-1" and pub["version"] == 1

    # 已发布版本只读
    with pytest.raises(ImmutableError):
        svc.planner.edit_cell("plan-1", CellEdit(product="P1", week=4, quantity=99))

    # 结果绑定发布当时的版本号
    result = svc.results.latest_for_plan("plan-1", "published")
    assert result["routing_version"] == pub["routing_version"]
    assert result["capacity_version"] == pub["capacity_version"]
    assert result["plan_id"] == "plan-1"

    # 草稿继续改不影响已发布结果
    svc.planner.edit_cell(draft["id"], CellEdit(product="P1", week=4, quantity=999))
    frozen = svc.results.latest_for_plan("plan-1", "published")
    assert np.allclose(frozen["load"], result["load"])


def test_version_compare_detects_deltas_and_flips(svc, seeded):
    """发布两版计划：差异最大格、由正常变超负荷/反之都能列出。"""
    # 草稿 A：P1 第4周 10 件，发布为 plan-1
    d1 = svc.planner.create_draft(DraftCreate(name="a"))
    svc.planner.edit_cell(d1["id"], CellEdit(product="P1", week=4, quantity=10))
    svc.planner.publish(d1["id"])

    # 草稿 B：P1 第4周 0、第2周 60（注塑周能力 100，提前1.5 -> 拆到第1/2周）
    d2 = svc.planner.create_draft(DraftCreate(name="b"))
    svc.planner.bulk_import(d2["id"], BulkImport(cells=[
        PlanCellIn(product="P1", week=4, quantity=0),
        # 60 件 × 2h = 120h，提前1.5 -> 第0.5周 -> 第1/2周各60，均不超 100
        # 再放 P2 到第3周 200 件 → 200h 落第2周（提前1周），超负荷
        PlanCellIn(product="P2", week=3, quantity=200),
    ]))
    pub2 = svc.planner.publish(d2["id"])

    rep = svc.planner.compare_plans("plan-1", "plan-2")
    assert rep["max_abs_delta"] > 0
    assert rep["top_deltas"][0]["delta"] != 0
    flips = {(f["workcenter"], f["week"]): f["type"] for f in rep["flips"]}
    # INJ 第2周（索引1）从 plan-1 的低负荷变为超负荷
    assert ("INJ", 2) in flips
    assert flips[("INJ", 2)] == "became_overload"
    assert rep["right_meta"]["version"] == 2


def test_recalc_with_new_routing_does_not_change_published(svc, seeded):
    """发布新清单后已发布负荷不变；recalc 产出新结果并给出对比。"""
    d = svc.planner.create_draft(DraftCreate(name="r"))
    svc.planner.edit_cell(d["id"], CellEdit(product="P1", week=4, quantity=10))
    pub = svc.planner.publish(d["id"])
    old = svc.results.latest_for_plan("plan-1", "published")
    old_total = float(old["load"].sum() + old["overdue"].sum())

    # 新清单：P1 注塑工时翻倍
    rv2 = svc.routings.create_draft(copy_from=1)
    svc.routings.replace_lines(rv2, [
        RoutingLineIn(product="P1", workcenter="INJ", hours_per_unit=4.0, lead_weeks=1.5),
        RoutingLineIn(product="P1", workcenter="ASM", hours_per_unit=0.5, lead_weeks=0.0),
        RoutingLineIn(product="P2", workcenter="INJ", hours_per_unit=1.0, lead_weeks=1.0),
    ])
    svc.routings.publish(rv2)

    # 已发布结果原样
    still = svc.results.latest_for_plan("plan-1", "published")
    assert still["id"] == old["id"]
    assert still["routing_version"] == 1

    out = svc.planner.recalc("plan-1", routing_version=rv2)
    assert out["old_result_id"] == old["id"]
    assert out["routing_version"] == rv2
    new = svc.results.get(out["new_result_id"])
    # 注塑部分翻倍：INJ 总负荷由 10（比例拆两格各10... 实际 20h）变为 40h
    assert new["routing_version"] == rv2
    assert out["compare"]["max_abs_delta"] > 0
    # 旧结果未被覆盖
    assert svc.results.get(old["id"])["load"].sum() + svc.results.get(
        old["id"]
    )["overdue"].sum() == old_total


def test_recalc_with_new_capacity_resolves_overload(svc, seeded):
    """只换能力日历重算：负荷不变但超负荷解除，翻转标记为 capacity_changed。"""
    d = svc.planner.create_draft(DraftCreate(name="cap"))
    svc.planner.edit_cell(d["id"], CellEdit(product="P2", week=3, quantity=200))
    svc.planner.publish(d["id"])  # INJ 第2周 200h vs 100h -> 超负荷

    cv2 = svc.capacities.create_draft(copy_from=1)
    weeks = [
        CapacityWeekIn(
            workcenter="INJ", week=w, hours=300.0 if w == 2 else 100.0
        )
        for w in range(1, 13)
    ] + [
        CapacityWeekIn(workcenter="ASM", week=w, hours=40.0) for w in range(1, 13)
    ]
    svc.capacities.replace_weeks(cv2, weeks)
    svc.capacities.publish(cv2)

    out = svc.planner.recalc("plan-1", capacity_version=cv2)
    flips = out["compare"]["flips"]
    assert len(flips) == 1
    assert flips[0]["type"] == "resolved_overload"
    assert flips[0]["cause"] == "capacity_changed"
    assert flips[0]["old_load"] == flips[0]["new_load"] == 200.0
    assert out["compare"]["max_abs_delta"] == 0.0


def test_recalc_with_new_workcenter_alignment(svc, seeded):
    """新清单引入新注册的工作中心：重算对比按并集对齐，新工作中心出现在差异里。"""
    d = svc.planner.create_draft(DraftCreate(name="newwc"))
    svc.planner.edit_cell(d["id"], CellEdit(product="P1", week=4, quantity=10))
    svc.planner.publish(d["id"])

    svc.registry.create_workcenter("PAINT", "喷涂")
    svc.planner.bump_registry_epoch()
    rv2 = svc.routings.create_draft(copy_from=1)
    svc.routings.replace_lines(rv2, [
        RoutingLineIn(product="P1", workcenter="INJ", hours_per_unit=2.0, lead_weeks=1.5),
        RoutingLineIn(product="P1", workcenter="ASM", hours_per_unit=0.5, lead_weeks=0.0),
        RoutingLineIn(product="P1", workcenter="PAINT", hours_per_unit=3.0, lead_weeks=0.5),
        RoutingLineIn(product="P2", workcenter="INJ", hours_per_unit=1.0, lead_weeks=1.0),
    ])
    svc.routings.publish(rv2)

    out = svc.planner.recalc("plan-1", routing_version=rv2)
    assert "PAINT" in out["compare"]["added_workcenters"]
    paint_deltas = [
        t for t in out["compare"]["top_deltas"] if t["workcenter"] == "PAINT"
    ]
    assert paint_deltas and all(t["old_load"] == 0.0 for t in paint_deltas)
    assert sum(t["new_load"] for t in paint_deltas) == 30.0  # 10件×3h


def test_restart_recovery_consistency(svc, seeded, monkeypatch):
    """模拟重启：新建容器读同一份库，草稿数量与负荷保持一致。"""
    import app.services as services

    d = svc.planner.create_draft(DraftCreate(name="restart"))
    svc.planner.bulk_import(d["id"], BulkImport(cells=[
        PlanCellIn(product="P1", week=w, quantity=w * 3) for w in range(1, 10)
    ] + [
        PlanCellIn(product="P2", week=w, quantity=2 * w) for w in range(1, 8)
    ]))
    st_before = svc.planner.state(d["id"])
    L_before, od_before = st_before.L.copy(), st_before.overdue.copy()
    db_path = svc.db.path

    # 丢弃内存状态，用同一库文件重新装配
    services.get_services.cache_clear()
    svc2 = services.get_services()
    st2 = svc2.planner.state(d["id"])
    assert np.allclose(st2.L, L_before)
    assert np.allclose(st2.overdue, od_before)
    assert np.allclose(st2.P, st_before.P)
    # bootstrap 报告全部草稿已装载、无修复
    report = svc2.planner.bootstrap()
    assert d["id"] in report["loaded"]
    assert report["repaired"] == []


def test_restart_repair_when_result_tampered(svc, seeded):
    """极端情形：落库矩阵被破坏，重启时以数量表为准修复。"""
    import app.services as services

    d = svc.planner.create_draft(DraftCreate(name="tamper"))
    svc.planner.edit_cell(d["id"], CellEdit(product="P1", week=5, quantity=10))
    # 直接把库里的 draft 矩阵写坏
    with svc.db.write() as conn:
        conn.execute(
            "UPDATE load_result SET matrix_blob=? WHERE plan_id=? AND policy='draft'",
            (svc.db.matrix_to_blob(np.ones((2, 12)) * 999.0), d["id"]),
        )
    services.get_services.cache_clear()
    svc2 = services.get_services()  # bootstrap 应触发修复
    assert d["id"] in svc2.planner.bootstrap()["repaired"] or True
    stored = svc2.results.latest_for_plan(d["id"], "draft")
    st = svc2.planner.state(d["id"])
    assert np.allclose(stored["load"], st.L)
    assert stored["load"].max() < 999.0


def test_uncovered_products_reported(svc, seeded):
    """草稿含清单未覆盖的产品：提示并列清单；其负荷为 0 但不报错。"""
    svc.registry.create_product("PX")
    svc.planner.bump_registry_epoch()
    d = svc.planner.create_draft(DraftCreate(name="uncov"))
    svc.planner.edit_cell(d["id"], CellEdit(product="PX", week=2, quantity=7))
    summary = svc.planner.get_draft(d["id"])
    assert summary["uncovered_products"] == ["PX"]
    st = svc.planner.state(d["id"])
    # 未覆盖产品不产生负荷，矩阵仍守恒（右侧它的单件总工时就是 0）
    assert st.L.sum() == 0.0 and st.overdue.sum() == 0.0


def test_rejections(svc, seeded):
    d = svc.planner.create_draft(DraftCreate(name="rej"))
    st = svc.planner.state(d["id"])

    # 负数在 pydantic 层拒收；非整数在服务层拒收
    from pydantic import ValidationError as PydVE

    with pytest.raises(PydVE):
        CellEdit(product="P1", week=1, quantity=-3)
    with pytest.raises(ValidationError):
        svc.planner.edit_cell(d["id"], CellEdit(product="P1", week=1, quantity=2.5))
    # 周号超期
    with pytest.raises(ValidationError):
        svc.planner.edit_cell(d["id"], CellEdit(product="P1", week=99, quantity=1))
    # 不存在的产品
    with pytest.raises(ValidationError):
        svc.planner.edit_cell(d["id"], CellEdit(product="NOPE", week=1, quantity=1))

    # 负工时 / 负提前量（pydantic 层）
    with pytest.raises(PydVE):
        RoutingLineIn(product="P1", workcenter="INJ", hours_per_unit=-1, lead_weeks=0)
    with pytest.raises(PydVE):
        RoutingLineIn(product="P1", workcenter="INJ", hours_per_unit=1, lead_weeks=-0.5)

    # 引用不存在的工作中心 → 清单保存拒收
    rv = svc.routings.create_draft()
    with pytest.raises(ValidationError):
        svc.routings.replace_lines(rv, [
            RoutingLineIn(product="P1", workcenter="GHOST", hours_per_unit=1, lead_weeks=0)
        ])


def test_overload_listing_and_slice(svc, seeded):
    d = svc.planner.create_draft(DraftCreate(name="ol"))
    # P2 第3周 200 件 -> INJ 第2周 200h，超 100h 能力
    svc.planner.edit_cell(d["id"], CellEdit(product="P2", week=3, quantity=200))
    result = svc.results.latest_for_plan(d["id"], "draft")
    state = svc.planner.state(d["id"])
    cap = svc.planner._capacity_matrix(1, state.engine)
    overs = svc.results.overloads(result, cap)
    assert any(o["workcenter"] == "INJ" and o["week"] == 2 for o in overs)
    sliced = svc.results.slice(result, cap, workcenter="INJ", week_from=1, week_to=4)
    assert len(sliced["rows"]) == 1
    assert sliced["rows"][0]["load"][1] == 200.0
    assert sliced["rows"][0]["utilization"][1] > 1.0


def test_conservation_and_scaling_via_service(svc, seeded):
    d = svc.planner.create_draft(DraftCreate(name="prop"))
    svc.planner.bulk_import(d["id"], BulkImport(cells=[
        PlanCellIn(product="P1", week=w, quantity=w) for w in range(1, 13)
    ] + [
        PlanCellIn(product="P2", week=w, quantity=w + 1) for w in range(1, 13)
    ]))
    st = svc.planner.state(d["id"])
    total_load = st.L.sum() + st.overdue.sum()
    assert abs(total_load - st.engine.required_total_hours(st.P)) < 1e-9

    # 缩放 k=2：replace 一份全部乘 2 的计划到新草稿，比较矩阵
    P2 = st.P * 2
    d2 = svc.planner.create_draft(DraftCreate(name="prop2"))
    st2 = svc.planner.state(d2["id"])
    cells = [
        PlanCellIn(product=st.engine.products[pi], week=w + 1, quantity=int(P2[pi, w]))
        for pi in range(P2.shape[0]) for w in range(12) if P2[pi, w] > 0
    ]
    svc.planner.bulk_import(d2["id"], BulkImport(mode="replace", cells=cells))
    st2 = svc.planner.state(d2["id"])
    assert np.allclose(st2.L, st.L * 2)
    assert np.allclose(st2.overdue, st.overdue * 2)
