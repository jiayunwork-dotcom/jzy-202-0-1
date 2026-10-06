"""并发测试：多名计划员同时编辑同一份草稿。

- 不同格子：全部生效，按到达顺序应用；
- 同一格子：携带相同旧值的并发覆盖只有一个成功，其余 409 并拿到当前值。
"""
from __future__ import annotations

import asyncio

import httpx

from rccp.config import Settings
from rccp.main import create_app

BOM = {
    "lines": [
        {"product": "P1", "work_center": "W1", "hours_per_unit": 2.0, "lead_weeks": 1.0},
        {"product": "P2", "work_center": "W1", "hours_per_unit": 1.0, "lead_weeks": 0.0},
    ]
}


def make_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_concurrent_edits_on_distinct_cells_all_apply(tmp_path):
    app = create_app(Settings(weeks=20, db_path=str(tmp_path / "c.db")))
    async with make_client(app) as client:
        await client.post("/api/bom-versions", json=BOM)
        responses = await asyncio.gather(
            *(
                client.put(
                    "/api/draft/cell",
                    json={"product": "P1", "week": w, "qty": w, "expected_old_qty": 0},
                )
                for w in range(1, 21)
            )
        )
        assert all(r.status_code == 200 for r in responses)
        draft = (await client.get("/api/draft")).json()
        assert {(c["product"], c["week"]): c["qty"] for c in draft["cells"]} == {
            ("P1", w): w for w in range(1, 21)
        }
        # 负荷总量 = Σ w × 2h（提前 1 周，第 1 周的 2h 落逾期）
        load = (await client.get("/api/draft/load")).json()
        assert load["total_load"] + load["total_past_due"] == sum(range(1, 21)) * 2.0


async def test_concurrent_same_cell_only_one_wins(tmp_path):
    app = create_app(Settings(weeks=20, db_path=str(tmp_path / "c.db")))
    async with make_client(app) as client:
        await client.post("/api/bom-versions", json=BOM)
        await client.put(
            "/api/draft/cell",
            json={"product": "P1", "week": 5, "qty": 3, "expected_old_qty": 0},
        )
        # 两个计划员都基于旧值 3 同时改同一格
        responses = await asyncio.gather(
            client.put(
                "/api/draft/cell",
                json={"product": "P1", "week": 5, "qty": 10, "expected_old_qty": 3},
            ),
            client.put(
                "/api/draft/cell",
                json={"product": "P1", "week": 5, "qty": 20, "expected_old_qty": 3},
            ),
        )
        codes = sorted(r.status_code for r in responses)
        assert codes == [200, 409]
        winner = next(r for r in responses if r.status_code == 200).json()
        loser = next(r for r in responses if r.status_code == 409).json()
        # 后到者拿到当前值（即先到者写入的值）
        assert loser["current_qty"] == winner["new_qty"]
        draft = (await client.get("/api/draft")).json()
        assert draft["cells"] == [
            {"product": "P1", "week": 5, "qty": winner["new_qty"]}
        ]


async def test_concurrent_same_cell_many_writers(tmp_path):
    app = create_app(Settings(weeks=20, db_path=str(tmp_path / "c.db")))
    async with make_client(app) as client:
        await client.post("/api/bom-versions", json=BOM)
        n = 8
        responses = await asyncio.gather(
            *(
                client.put(
                    "/api/draft/cell",
                    json={"product": "P1", "week": 7, "qty": 100 + i, "expected_old_qty": 0},
                )
                for i in range(n)
            )
        )
        codes = [r.status_code for r in responses]
        assert codes.count(200) == 1
        assert codes.count(409) == n - 1
        final = (await client.get("/api/draft")).json()["cells"]
        winner_qty = next(r.json()["new_qty"] for r in responses if r.status_code == 200)
        assert final == [{"product": "P1", "week": 7, "qty": winner_qty}]
        # 负荷与最终数量一致
        load = (await client.get("/api/draft/load")).json()
        assert load["matrix"]["W1"]["weeks"][5] == winner_qty * 2.0
