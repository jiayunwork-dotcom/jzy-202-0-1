"""HTTP 接口测试：录入校验、逐格编辑（CAS）、批量导入、负荷查询与切片、
超负荷清单、BOM 未覆盖产品的提示。"""
from __future__ import annotations

from helpers import edit, publish, seed_bom, seed_calendar


# ---------------- 核对例子（端到端） ----------------
def test_reference_example_through_api(client):
    seed_bom(
        client,
        lines=[
            {"product": "P", "work_center": "W1", "hours_per_unit": 2.0, "lead_weeks": 1.0},
            {"product": "P", "work_center": "W2", "hours_per_unit": 0.5, "lead_weeks": 0.0},
        ],
    )
    seed_calendar(client)
    r = edit(client, "P", 3, 10)
    assert r.status_code == 200, r.text
    load = client.get("/api/draft/load").json()
    assert load["matrix"]["W1"]["weeks"][1] == 20.0  # 第 2 周
    assert load["matrix"]["W2"]["weeks"][2] == 5.0  # 第 3 周
    assert load["total_load"] == 25.0
    assert load["total_past_due"] == 0.0


# ---------------- 录入校验 ----------------
def test_bom_negative_values_rejected(client):
    r = client.post(
        "/api/bom-versions",
        json={"lines": [{"product": "P1", "work_center": "W1",
                         "hours_per_unit": -1.0, "lead_weeks": 0.0}]},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/bom-versions",
        json={"lines": [{"product": "P1", "work_center": "W1",
                         "hours_per_unit": 1.0, "lead_weeks": -0.5}]},
    )
    assert r.status_code == 422


def test_bom_duplicate_line_rejected(client):
    r = client.post(
        "/api/bom-versions",
        json={"lines": [
            {"product": "P1", "work_center": "W1", "hours_per_unit": 1.0, "lead_weeks": 0.0},
            {"product": "P1", "work_center": "W1", "hours_per_unit": 2.0, "lead_weeks": 1.0},
        ]},
    )
    assert r.status_code == 422
    assert "duplicate" in str(r.json()["errors"]).lower()


def test_bom_empty_rejected(client):
    assert client.post("/api/bom-versions", json={"lines": []}).status_code == 422


def test_calendar_validation(client):
    seed_bom(client)
    r = client.post("/api/calendar-versions", json={"entries": [
        {"work_center": "W1", "week": 3, "available_hours": -5.0}]})
    assert r.status_code == 422
    r = client.post("/api/calendar-versions", json={"entries": [
        {"work_center": "W1", "week": 0, "available_hours": 40.0}]})
    assert r.status_code == 422
    r = client.post("/api/calendar-versions", json={"entries": [
        {"work_center": "W1", "week": 21, "available_hours": 40.0}]})
    assert r.status_code == 422
    assert "horizon" in r.json()["errors"][0]["error"]
    r = client.post("/api/calendar-versions", json={"entries": [
        {"work_center": "W1", "week": 3, "available_hours": 40.0},
        {"work_center": "W1", "week": 3, "available_hours": 20.0}]})
    assert r.status_code == 422


def test_edit_unknown_product_rejected(client):
    seed_bom(client)
    r = edit(client, "NOPE", 3, 5)
    assert r.status_code == 422
    assert "unknown product" in r.json()["error"]


def test_edit_week_out_of_range(client):
    seed_bom(client)
    assert edit(client, "P1", 0, 5).status_code == 422
    r = edit(client, "P1", 21, 5)
    assert r.status_code == 422
    assert "horizon" in r.json()["error"]


def test_edit_bad_quantities(client):
    seed_bom(client)
    assert edit(client, "P1", 3, -1).status_code == 422
    r = client.put("/api/draft/cell",
                   json={"product": "P1", "week": 3, "qty": 2.5, "expected_old_qty": 0})
    assert r.status_code == 422
    # 整数值的浮点可以接受
    r = client.put("/api/draft/cell",
                   json={"product": "P1", "week": 3, "qty": 3.0, "expected_old_qty": 0})
    assert r.status_code == 200
    assert r.json()["new_qty"] == 3


# ---------------- 逐格编辑与 CAS ----------------
def test_same_cell_conflict_returns_current_value(client):
    seed_bom(client)
    assert edit(client, "P1", 5, 10).status_code == 200
    # 基于过期旧值的覆盖 → 409 + 当前值
    r = edit(client, "P1", 5, 99, expected_old_qty=0)
    assert r.status_code == 409
    assert r.json()["current_qty"] == 10
    # 基于正确旧值 → 生效
    r = edit(client, "P1", 5, 99, expected_old_qty=10)
    assert r.status_code == 200
    assert r.json()["old_qty"] == 10
    assert r.json()["new_qty"] == 99
    cells = client.get("/api/draft").json()["cells"]
    assert cells == [{"product": "P1", "week": 5, "qty": 99}]


def test_edit_zero_clears_cell(client):
    seed_bom(client)
    edit(client, "P1", 5, 10)
    r = edit(client, "P1", 5, 0, expected_old_qty=10)
    assert r.status_code == 200
    assert client.get("/api/draft").json()["cells"] == []
    load = client.get("/api/draft/load").json()
    assert load["matrix"] == {}


# ---------------- 批量导入 ----------------
def test_batch_atomic_rejection(client):
    seed_bom(client)
    r = client.post("/api/draft/batch", json={"cells": [
        {"product": "P1", "week": 3, "qty": 5},
        {"product": "GHOST", "week": 3, "qty": 5},
        {"product": "P2", "week": 30, "qty": 5},
        {"product": "P2", "week": 4, "qty": -2},
    ]})
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert [e["index"] for e in errors] == [1, 2, 3]
    # 整批未应用
    assert client.get("/api/draft").json()["cells"] == []


def test_batch_applies_in_order(client):
    seed_bom(client)
    r = client.post("/api/draft/batch", json={"cells": [
        {"product": "P1", "week": 3, "qty": 5},
        {"product": "P1", "week": 3, "qty": 8, "expected_old_qty": 5},  # 看到本批前一行的结果
        {"product": "P2", "week": 4, "qty": 7},
    ]})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 3
    cells = client.get("/api/draft").json()["cells"]
    assert {(c["product"], c["week"]): c["qty"] for c in cells} == {("P1", 3): 8, ("P2", 4): 7}


def test_batch_cas_mismatch_rejects_whole_batch(client):
    seed_bom(client)
    edit(client, "P1", 3, 5)
    r = client.post("/api/draft/batch", json={"cells": [
        {"product": "P2", "week": 4, "qty": 7},
        {"product": "P1", "week": 3, "qty": 9, "expected_old_qty": 1},  # 实际当前值是 5
    ]})
    assert r.status_code == 422
    assert r.json()["errors"][0]["current_qty"] == 5
    cells = client.get("/api/draft").json()["cells"]
    assert cells == [{"product": "P1", "week": 3, "qty": 5}]


# ---------------- BOM 未覆盖产品的提示 ----------------
def test_products_without_bom_warning(client):
    seed_bom(client)
    edit(client, "P1", 3, 5)
    edit(client, "P2", 3, 5)
    # BOM v2 去掉了 P2（P2 仍"存在"于 v1，所以不是未知产品，只是未被当前清单覆盖）
    seed_bom(client, lines=[
        {"product": "P1", "work_center": "W1", "hours_per_unit": 2.0, "lead_weeks": 1.0},
    ])
    load = client.get("/api/draft/load").json()
    assert load["warnings"]["products_without_bom"] == ["P2"]
    # P2 的负荷不再计入（W2 在 v2 中没有任何行）
    assert "W2" not in load["matrix"]
    assert load["matrix"]["W1"]["weeks"][1] == 10.0
    # 对 P2 的编辑仍然接受（它不是未知产品），但响应里带提示
    r = edit(client, "P2", 5, 3)
    assert r.status_code == 200
    assert r.json()["warnings"]["products_without_bom"] == ["P2"]


# ---------------- 负荷矩阵查询与切片 ----------------
def test_load_slicing(client):
    seed_bom(client)
    seed_calendar(client)
    edit(client, "P1", 3, 10)   # W1 第2周 +20；W2 第3周 +5
    edit(client, "P2", 4, 8)    # W1 提前1.5周：第3周 +4、第2周 +4；W2 第4周 +2

    by_wc = client.get("/api/draft/load", params={"work_center": "W1"}).json()
    assert by_wc["matrix"]["W1"]["weeks"][1] == 24.0
    assert by_wc["matrix"]["W1"]["weeks"][2] == 4.0
    assert "W2" not in by_wc["matrix"]

    by_week = client.get("/api/draft/load", params={"week": 2}).json()
    assert by_week["loads"] == {"W1": 24.0}

    cell = client.get("/api/draft/load", params={"work_center": "W2", "week": 4}).json()
    assert cell["load"] == 2.0

    assert client.get("/api/draft/load", params={"work_center": "NOPE"}).status_code == 404
    assert client.get("/api/draft/load", params={"week": 99}).status_code == 422


def test_fractional_lead_and_past_due_through_api(client):
    seed_bom(client)
    seed_calendar(client)
    # P2 在 W1 提前 1.5 周，第 1 周完工 10 件 → 第 1 周 5h（t-1=0 越界?）
    # t=1, L=1.5 → f=1, r=0.5：t-f=0 → 逾期 5h；t-f-1=-1 → 逾期 5h。全部逾期。
    edit(client, "P2", 1, 10)
    load = client.get("/api/draft/load").json()
    assert load["matrix"]["W1"]["past_due"] == 10.0
    assert load["total_past_due"] == 10.0
    # 守恒：总负荷（周 + 逾期）= 10 × (1.0 + 0.25) = 12.5
    assert load["total_load"] + load["total_past_due"] == 12.5


# ---------------- 超负荷清单 ----------------
def test_overload_list(client):
    seed_bom(client)
    seed_calendar(client, hours=40.0)
    edit(client, "P1", 3, 30)  # W1 第2周 = 60 > 40
    r = client.get("/api/draft/overloads").json()
    assert r["overloads"] == [
        {"work_center": "W1", "week": 2, "load": 60.0, "capacity": 40.0,
         "excess": 20.0, "ratio": 1.5}
    ]
    assert r["past_due"] == []


def test_overload_past_due_listed_separately(client):
    seed_bom(client)
    seed_calendar(client)
    edit(client, "P1", 1, 10)  # W1 提前 1 周 → 目标周 0 → 逾期 20h
    r = client.get("/api/draft/overloads").json()
    assert r["overloads"] == []
    assert r["past_due"] == [{"work_center": "W1", "past_due": 20.0}]


def test_overloads_require_calendar(client):
    seed_bom(client)
    edit(client, "P1", 3, 5)
    assert client.get("/api/draft/overloads").status_code == 409


# ---------------- 元信息 ----------------
def test_meta_and_registries(client):
    seed_bom(client)
    seed_calendar(client)
    edit(client, "P1", 3, 5)
    publish(client)
    meta = client.get("/api/meta").json()
    assert meta["weeks"] == 20
    assert meta["current_bom_version"] == 1
    assert meta["current_calendar_version"] == 1
    assert meta["products"] == ["P1", "P2"]
    assert meta["work_centers"] == ["W1", "W2"]
    assert meta["draft_cells"] == 1
    assert meta["plan_versions"] == [1]
    assert client.get("/api/products").json()["products"] == ["P1", "P2"]
    assert client.get("/api/work-centers").json()["work_centers"] == ["W1", "W2"]
