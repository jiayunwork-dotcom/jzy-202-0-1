"""版本测试：发布冻结与绑定、版本对比、按新清单重算、日历绑定。"""
from __future__ import annotations

from helpers import edit, publish, seed_bom, seed_calendar


def test_publish_freezes_draft_and_binds_versions(client):
    seed_bom(client)
    seed_calendar(client)
    edit(client, "P1", 3, 10)
    pub = publish(client, note="第 40 周计划")
    assert pub["version"] == 1
    assert pub["bom_version"] == 1
    assert pub["calendar_version"] == 1
    assert pub["cell_count"] == 1

    # 发布后草稿继续改，已发布版本不变
    assert edit(client, "P1", 3, 0, expected_old_qty=10).status_code == 200
    edit(client, "P1", 5, 50)
    v1 = client.get("/api/plan-versions/1/load").json()
    assert v1["plan_version"] == 1
    assert v1["bom_version"] == 1
    assert v1["matrix"]["W1"]["weeks"][1] == 20.0
    draft = client.get("/api/draft/load").json()
    assert draft["matrix"]["W1"]["weeks"][3] == 100.0  # 第 5 周完工、提前 1 周 → 第 4 周
    assert draft["matrix"]["W2"]["weeks"][4] == 25.0  # 50 件 × 0.5h，提前 0 周

    # 版本内容只读：没有修改已发布版本的接口，版本详情可查询
    detail = client.get("/api/plan-versions/1").json()
    assert detail["cells"] == [{"product": "P1", "week": 3, "qty": 10}]
    assert client.get("/api/plan-versions").json()["versions"][0]["note"] == "第 40 周计划"


def test_publish_requires_bom_and_calendar(client):
    assert client.post("/api/plan-versions", json={}).status_code == 409
    seed_bom(client)
    assert client.post("/api/plan-versions", json={}).status_code == 409
    seed_calendar(client)
    assert client.post("/api/plan-versions", json={}).status_code == 200


def test_compare_versions_finds_diffs_and_overload_flips(client):
    seed_bom(client)
    seed_calendar(client, hours=40.0)
    edit(client, "P1", 3, 10)  # W1 第2周 = 20 ≤ 40
    publish(client)
    edit(client, "P1", 3, 30, expected_old_qty=10)  # W1 第2周 = 60 > 40
    publish(client)

    cmp = client.get("/api/plan-versions/compare", params={"a": 1, "b": 2}).json()
    assert cmp["a"] == {"plan_version": 1, "bom_version": 1, "calendar_version": 1}
    assert cmp["b"] == {"plan_version": 2, "bom_version": 1, "calendar_version": 1}
    top = cmp["top_differences"]
    assert top[0] == {
        "work_center": "W1", "week": 2, "load_a": 20.0, "load_b": 60.0, "delta": 40.0,
    }
    # W2 第3周 5 → 15 也在差异里
    assert {("W2", 3): (5.0, 15.0)} == {
        (d["work_center"], d["week"]): (d["load_a"], d["load_b"])
        for d in top
        if d["work_center"] == "W2"
    }
    assert cmp["overload_transitions"] == [
        {
            "work_center": "W1", "week": 2, "from": "normal", "to": "overload",
            "load_a": 20.0, "capacity_a": 40.0, "load_b": 60.0, "capacity_b": 40.0,
        }
    ]
    assert cmp["total_load_a"] == 25.0
    assert cmp["total_load_b"] == 75.0


def test_compare_reports_reverse_flip(client):
    seed_bom(client)
    seed_calendar(client, hours=40.0)
    edit(client, "P1", 3, 30)  # 超负荷
    publish(client)
    edit(client, "P1", 3, 10, expected_old_qty=30)  # 回到正常
    publish(client)
    cmp = client.get("/api/plan-versions/compare", params={"a": 1, "b": 2}).json()
    assert cmp["overload_transitions"][0]["from"] == "overload"
    assert cmp["overload_transitions"][0]["to"] == "normal"


def test_compare_unknown_version_404(client):
    seed_bom(client)
    seed_calendar(client)
    publish(client)
    assert client.get("/api/plan-versions/compare", params={"a": 1, "b": 9}).status_code == 404


def test_recompute_with_new_bom(client):
    seed_bom(client)
    seed_calendar(client)
    edit(client, "P1", 3, 10)  # W1 第2周 = 20（h=2）
    publish(client)

    # BOM v2：P1 在 W1 的单件工时 2 → 3
    lines = [
        {"product": "P1", "work_center": "W1", "hours_per_unit": 3.0, "lead_weeks": 1.0},
        {"product": "P1", "work_center": "W2", "hours_per_unit": 0.5, "lead_weeks": 0.0},
        {"product": "P2", "work_center": "W1", "hours_per_unit": 1.0, "lead_weeks": 1.5},
        {"product": "P2", "work_center": "W2", "hours_per_unit": 0.25, "lead_weeks": 0.0},
    ]
    seed_bom(client, lines=lines)

    # 已发布计划的负荷不自动改变
    v1 = client.get("/api/plan-versions/1/load").json()
    assert v1["bom_version"] == 1
    assert v1["matrix"]["W1"]["weeks"][1] == 20.0

    # 按新清单重算（缺省用当前 BOM / 当前日历）
    r = client.post("/api/plan-versions/1/recompute", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["kind"] == "recompute"
    assert body["result"]["bom_version"] == 2
    assert body["result"]["matrix"]["W1"]["weeks"][1] == 30.0
    cmp = body["comparison_vs_publish"]
    assert cmp["a"]["bom_version"] == 1
    assert cmp["b"]["bom_version"] == 2
    assert cmp["top_differences"][0] == {
        "work_center": "W1", "week": 2, "load_a": 20.0, "load_b": 30.0, "delta": 10.0,
    }

    # 原结果仍在，重算结果也持久化
    v1b = client.get("/api/plan-versions/1/load").json()
    assert v1b["matrix"]["W1"]["weeks"][1] == 20.0
    results = client.get("/api/plan-versions/1/results").json()["results"]
    assert [x["kind"] for x in results] == ["publish", "recompute"]
    assert results[1]["bom_version"] == 2


def test_recompute_with_explicit_versions_and_404(client):
    seed_bom(client)
    seed_calendar(client)
    publish(client)
    assert client.post("/api/plan-versions/1/recompute", json={"bom_version": 9}).status_code == 404
    assert client.post("/api/plan-versions/9/recompute", json={}).status_code == 404
    r = client.post("/api/plan-versions/1/recompute", json={"bom_version": 1, "calendar_version": 1})
    assert r.status_code == 200
    # 用同一清单重算 → 无差异
    assert r.json()["comparison_vs_publish"]["top_differences"] == []


def test_published_version_keeps_bound_calendar(client):
    seed_bom(client)
    seed_calendar(client, hours=40.0)
    edit(client, "P1", 3, 25)  # W1 第2周 = 50 > 40 → 超负荷
    publish(client)

    seed_calendar(client, hours=60.0)  # 新日历：能力提高

    ov = client.get("/api/plan-versions/1/overloads").json()
    assert ov["calendar_version"] == 1  # 已发布版本仍用绑定的旧日历
    assert len(ov["overloads"]) == 1
    assert ov["overloads"][0]["excess"] == 10.0

    draft_ov = client.get("/api/draft/overloads").json()
    assert draft_ov["calendar_version"] == 2  # 草稿用当前日历
    assert draft_ov["overloads"] == []


def test_version_load_slicing(client):
    seed_bom(client)
    seed_calendar(client)
    edit(client, "P1", 3, 10)
    edit(client, "P2", 4, 8)
    publish(client)
    by_wc = client.get("/api/plan-versions/1/load", params={"work_center": "W1"}).json()
    assert by_wc["matrix"]["W1"]["weeks"][1] == 24.0  # 20 + 8×1 的 50%
    by_week = client.get("/api/plan-versions/1/load", params={"week": 4}).json()
    assert by_week["loads"] == {"W2": 2.0}
    cell = client.get(
        "/api/plan-versions/1/load", params={"work_center": "W1", "week": 3}
    ).json()
    assert cell["load"] == 4.0
    assert client.get("/api/plan-versions/1/load", params={"work_center": "NOPE"}).status_code == 404
    assert client.get("/api/plan-versions/9/load").status_code == 404
