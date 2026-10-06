"""测试公共辅助：标准 BOM / 日历种子数据、编辑快捷方式。

标准种子（对应题目里的核对例子的结构）：
- P1: W1 每件 2h 提前 1 周；W2 每件 0.5h 提前 0 周
- P2: W1 每件 1h 提前 1.5 周；W2 每件 0.25h 提前 0 周
- 日历：W1、W2 每周 40h
"""
from __future__ import annotations

WEEKS = 20

DEFAULT_BOM_LINES = [
    {"product": "P1", "work_center": "W1", "hours_per_unit": 2.0, "lead_weeks": 1.0},
    {"product": "P1", "work_center": "W2", "hours_per_unit": 0.5, "lead_weeks": 0.0},
    {"product": "P2", "work_center": "W1", "hours_per_unit": 1.0, "lead_weeks": 1.5},
    {"product": "P2", "work_center": "W2", "hours_per_unit": 0.25, "lead_weeks": 0.0},
]


def seed_bom(client, lines=None, note=""):
    lines = lines if lines is not None else DEFAULT_BOM_LINES
    r = client.post("/api/bom-versions", json={"lines": lines, "note": note})
    assert r.status_code == 200, r.text
    return r.json()


def seed_calendar(client, hours=40.0, work_centers=("W1", "W2"), weeks=WEEKS):
    entries = [
        {"work_center": wc, "week": w, "available_hours": hours}
        for wc in work_centers
        for w in range(1, weeks + 1)
    ]
    r = client.post("/api/calendar-versions", json={"entries": entries})
    assert r.status_code == 200, r.text
    return r.json()


def edit(client, product, week, qty, expected_old_qty=0):
    return client.put(
        "/api/draft/cell",
        json={
            "product": product,
            "week": week,
            "qty": qty,
            "expected_old_qty": expected_old_qty,
        },
    )


def publish(client, note=""):
    r = client.post("/api/plan-versions", json={"note": note})
    assert r.status_code == 200, r.text
    return r.json()
