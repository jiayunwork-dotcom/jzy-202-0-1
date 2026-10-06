"""重启恢复：服务重启后草稿内容与负荷状态保持一致，已发布版本不受影响。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from helpers import edit, publish, seed_bom, seed_calendar
from rccp.config import Settings
from rccp.main import create_app


def test_restart_recovers_draft_and_load(tmp_path):
    db_path = str(tmp_path / "rccp.db")

    app1 = create_app(Settings(weeks=20, db_path=db_path))
    with TestClient(app1) as c1:
        seed_bom(c1)
        seed_calendar(c1)
        edit(c1, "P1", 3, 10)   # W1 第2周 20h；W2 第3周 5h
        edit(c1, "P2", 2, 8)    # W1 提前1.5周：第1周 4h + 逾期 4h；W2 第2周 2h
        draft_before = c1.get("/api/draft").json()
        load_before = c1.get("/api/draft/load").json()
        overloads_before = c1.get("/api/draft/overloads").json()
        publish(c1, note="重启前发布")
        v1_load_before = c1.get("/api/plan-versions/1/load").json()

    # 模拟重启：同一库文件新建服务实例
    app2 = create_app(Settings(weeks=20, db_path=db_path))
    with TestClient(app2) as c2:
        assert c2.get("/api/draft").json() == draft_before
        assert c2.get("/api/draft/load").json() == load_before
        assert c2.get("/api/draft/overloads").json() == overloads_before
        assert c2.get("/api/plan-versions/1/load").json() == v1_load_before
        meta = c2.get("/api/meta").json()
        assert meta["current_bom_version"] == 1
        assert meta["current_calendar_version"] == 1
        assert meta["plan_versions"] == [1]

        # 重启后继续编辑，增量维护仍然正确
        r = edit(c2, "P1", 3, 0, expected_old_qty=10)
        assert r.status_code == 200
        load = c2.get("/api/draft/load").json()
        assert "W1" in load["matrix"]  # P2 的 4h 还在第 1 周
        assert load["matrix"]["W1"]["weeks"][0] == 4.0
        assert load["matrix"]["W1"]["past_due"] == 4.0
        assert load["matrix"]["W2"]["weeks"][1] == 2.0
        assert load["matrix"]["W2"]["weeks"][2] == 0.0  # P1 已清零，第 3 周的 5h 消失


def test_restart_after_bom_version_change(tmp_path):
    db_path = str(tmp_path / "rccp.db")

    app1 = create_app(Settings(weeks=20, db_path=db_path))
    with TestClient(app1) as c1:
        seed_bom(c1)
        seed_calendar(c1)
        edit(c1, "P1", 3, 10)
        # BOM v2：P1 在 W1 的工时改为 3 → 草稿负荷按新清单重建
        seed_bom(c1, lines=[
            {"product": "P1", "work_center": "W1", "hours_per_unit": 3.0, "lead_weeks": 1.0},
        ])
        load_before = c1.get("/api/draft/load").json()
        assert load_before["matrix"]["W1"]["weeks"][1] == 30.0
        assert load_before["warnings"]["products_without_bom"] == []

    app2 = create_app(Settings(weeks=20, db_path=db_path))
    with TestClient(app2) as c2:
        assert c2.get("/api/draft/load").json() == load_before
        assert c2.get("/api/bom-versions/current").json()["version"] == 2
