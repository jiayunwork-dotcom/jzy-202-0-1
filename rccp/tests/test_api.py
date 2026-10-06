"""HTTP 端到端测试：录入、编辑、查询、409 冲突、发布、对比、重算、拒收码。"""

from __future__ import annotations


def _bootstrap_master(client):
    for code, kind in [("P1", "products"), ("P2", "products"),
                       ("INJ", "workcenters"), ("ASM", "workcenters")]:
        r = client.post(f"/api/{kind}", json={"code": code, "name": code})
        assert r.status_code == 201, r.text


def _publish_routing_and_capacity(client):
    r = client.post("/api/routings/versions", json={"lines": [
        {"product": "P1", "workcenter": "INJ", "hours_per_unit": 2.0, "lead_weeks": 1.0},
        {"product": "P1", "workcenter": "ASM", "hours_per_unit": 0.5, "lead_weeks": 0.0},
        {"product": "P2", "workcenter": "INJ", "hours_per_unit": 1.0, "lead_weeks": 1.0},
    ]})
    assert r.status_code == 201, r.text
    rv = r.json()["version"]
    assert client.post(f"/api/routings/versions/{rv}/publish").status_code == 200

    r = client.post("/api/capacities/versions", json={"weeks": [
        {"workcenter": "INJ", "week": w, "hours": 100.0} for w in range(1, 13)
    ] + [
        {"workcenter": "ASM", "week": w, "hours": 40.0} for w in range(1, 13)
    ]})
    cv = r.json()["version"]
    assert client.post(f"/api/capacities/versions/{cv}/publish").status_code == 200
    return rv, cv


def test_full_workflow(client):
    _bootstrap_master(client)
    rv, cv = _publish_routing_and_capacity(client)

    # 建草稿 + 逐格编辑
    r = client.post("/api/plans/drafts", json={"name": "w1"})
    assert r.status_code == 201, r.text
    draft_id = r.json()["id"]

    r = client.put(f"/api/plans/{draft_id}/cell",
                   json={"product": "P1", "week": 3, "quantity": 10})
    assert r.status_code == 200 and r.json()["changed"] is True

    load = client.get(f"/api/plans/{draft_id}/load").json()
    inj = next(row for row in load["rows"] if row["workcenter"] == "INJ")
    asm = next(row for row in load["rows"] if row["workcenter"] == "ASM")
    assert inj["load"][1] == 20.0   # 第2周 20h
    assert asm["load"][2] == 5.0    # 第3周 5h

    # 切片：按工作中心 + 周区间
    r = client.get(
        f"/api/plans/{draft_id}/load",
        params={"workcenter": "INJ", "week_from": 1, "week_to": 4},
    )
    assert r.status_code == 200
    body = r.json()
    assert [row["workcenter"] for row in body["rows"]] == ["INJ"]
    assert len(body["rows"][0]["load"]) == 4

    # 超负荷：P2 第3周 200 件 -> INJ 第2周 200h
    client.put(f"/api/plans/{draft_id}/cell",
               json={"product": "P2", "week": 3, "quantity": 200})
    overs = client.get(f"/api/plans/{draft_id}/overloads").json()["overloads"]
    # INJ 第2周：P1 提前1周贡献 20h + P2 贡献 200h = 220h，超 100h
    assert any(o["workcenter"] == "INJ" and o["week"] == 2 and o["excess"] == 120.0
               for o in overs)


def test_concurrent_conflict_returns_409_and_current_value(client):
    _bootstrap_master(client)
    _publish_routing_and_capacity(client)
    draft_id = client.post("/api/plans/drafts", json={}).json()["id"]
    client.put(f"/api/plans/{draft_id}/cell",
               json={"product": "P1", "week": 1, "quantity": 10})
    client.put(f"/api/plans/{draft_id}/cell",
               json={"product": "P1", "week": 1, "quantity": 15})
    r = client.put(
        f"/api/plans/{draft_id}/cell",
        json={"product": "P1", "week": 1, "quantity": 20, "expected_quantity": 10},
    )
    assert r.status_code == 409
    assert r.json()["details"]["current_quantity"] == 15.0

    # 带正确旧值成功
    r = client.put(
        f"/api/plans/{draft_id}/cell",
        json={"product": "P1", "week": 1, "quantity": 20, "expected_quantity": 15},
    )
    assert r.status_code == 200


def test_publish_compare_recalc_flow(client):
    _bootstrap_master(client)
    rv, cv = _publish_routing_and_capacity(client)

    def make_published(name, cells):
        did = client.post("/api/plans/drafts", json={"name": name}).json()["id"]
        r = client.post(f"/api/plans/{did}/import", json={"cells": cells})
        assert r.status_code == 200, r.text
        r = client.post(f"/api/plans/{did}/publish")
        assert r.status_code == 200, r.text
        return r.json()["plan_id"]

    p1 = make_published("a", [{"product": "P1", "week": 3, "quantity": 10}])
    p2 = make_published("b", [{"product": "P2", "week": 3, "quantity": 200}])

    rep = client.get(f"/api/plans/{p1}/compare/{p2}").json()
    assert rep["max_abs_delta"] > 0
    assert any(f["type"] == "became_overload" for f in rep["flips"])

    # 新清单：P2 工时翻倍，发布后已发布结果不变
    r = client.post("/api/routings/versions", json={"copy_from": rv})
    rv2 = r.json()["version"]
    client.put(f"/api/routings/versions/{rv2}/lines", json={"lines": [
        {"product": "P1", "workcenter": "INJ", "hours_per_unit": 2.0, "lead_weeks": 1.0},
        {"product": "P1", "workcenter": "ASM", "hours_per_unit": 0.5, "lead_weeks": 0.0},
        {"product": "P2", "workcenter": "INJ", "hours_per_unit": 2.0, "lead_weeks": 1.0},
    ]})
    client.post(f"/api/routings/versions/{rv2}/publish")

    before = client.get(f"/api/plans/{p2}/results").json()
    r = client.post(f"/api/plans/{p2}/recalc", json={"routing_version": rv2})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["routing_version"] == rv2
    assert out["old_result_id"] != out["new_result_id"]
    # INJ 第2周负荷 200 -> 400
    top = out["compare"]["top_deltas"]
    assert any(d["workcenter"] == "INJ" and d["week"] == 2 and d["delta"] == 200.0
               for d in top)
    # 原结果仍在、未变
    after = client.get(f"/api/plans/{p2}/results").json()
    assert len(after) == len(before) + 1

    # 新结果可单独查询，超负荷清单按新清单口径
    r = client.get(f"/api/plans/{p2}/overloads",
                   params={"result_id": out["new_result_id"]})
    assert r.status_code == 200
    assert r.json()["overloads"][0]["load"] == 400.0


def test_validation_rejections(client):
    _bootstrap_master(client)
    _publish_routing_and_capacity(client)
    did = client.post("/api/plans/drafts", json={}).json()["id"]

    # 非整数数量
    r = client.put(f"/api/plans/{did}/cell",
                   json={"product": "P1", "week": 1, "quantity": 1.5})
    assert r.status_code == 422
    # 周号超期（服务层）
    r = client.put(f"/api/plans/{did}/cell",
                   json={"product": "P1", "week": 99, "quantity": 1})
    assert r.status_code == 422
    # 不存在的产品
    r = client.put(f"/api/plans/{did}/cell",
                   json={"product": "X", "week": 1, "quantity": 1})
    assert r.status_code == 422 and "产品" in r.json()["message"]
    # 负能力 / 负工时 / 负提前量（pydantic 422）
    r = client.post("/api/capacities/versions", json={"weeks": [
        {"workcenter": "INJ", "week": 1, "hours": -5}
    ]})
    assert r.status_code == 422
    r = client.post("/api/routings/versions", json={"lines": [
        {"product": "P1", "workcenter": "INJ", "hours_per_unit": -1, "lead_weeks": 0}
    ]})
    assert r.status_code == 422
    r = client.post("/api/routings/versions", json={"lines": [
        {"product": "P1", "workcenter": "INJ", "hours_per_unit": 1, "lead_weeks": -1}
    ]})
    assert r.status_code == 422
    # 引用不存在工作中心
    r = client.post("/api/routings/versions", json={"lines": [
        {"product": "P1", "workcenter": "ZZ", "hours_per_unit": 1, "lead_weeks": 0}
    ]})
    assert r.status_code == 422


def test_uncovered_products_endpoint(client):
    _bootstrap_master(client)
    _publish_routing_and_capacity(client)
    client.post("/api/products", json={"code": "PX"})
    did = client.post("/api/plans/drafts", json={}).json()["id"]
    client.put(f"/api/plans/{did}/cell",
               json={"product": "PX", "week": 2, "quantity": 5})
    r = client.get(f"/api/plans/{did}/uncovered")
    assert r.status_code == 200
    assert r.json()["uncovered_products"] == ["PX"]


def test_published_plan_read_only(client):
    _bootstrap_master(client)
    _publish_routing_and_capacity(client)
    did = client.post("/api/plans/drafts", json={}).json()["id"]
    client.put(f"/api/plans/{did}/cell",
               json={"product": "P1", "week": 1, "quantity": 3})
    pid = client.post(f"/api/plans/{did}/publish").json()["plan_id"]
    r = client.put(f"/api/plans/{pid}/cell",
                   json={"product": "P1", "week": 1, "quantity": 4})
    assert r.status_code == 409


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["horizon_weeks"] == 12
