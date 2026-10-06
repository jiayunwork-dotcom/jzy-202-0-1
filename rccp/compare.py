"""超负荷分析与两个负荷结果的对比。

负荷结果用 JSON 友好的 dict 表示：{wc: {"weeks": [w1..wN], "past_due": x}}。
"""
from __future__ import annotations

from .capacity import CalendarVersion


def overload_cells(matrix: dict, calendar: CalendarVersion, weeks: int) -> list[dict]:
    """负荷 > 能力的格子，按超出量降序。

    日历中缺失的 (工作中心, 周) 能力按 0 处理（保守）。能力为 0 且负荷 > 0 时
    ratio 无法表示，置为 None。
    """
    out: list[dict] = []
    for wc in sorted(matrix):
        for i, load in enumerate(matrix[wc]["weeks"], start=1):
            capacity = calendar.capacity(wc, i)
            if load > capacity:
                out.append(
                    {
                        "work_center": wc,
                        "week": i,
                        "load": load,
                        "capacity": capacity,
                        "excess": load - capacity,
                        "ratio": load / capacity if capacity > 0 else None,
                    }
                )
    out.sort(key=lambda d: (-d["excess"], d["work_center"], d["week"]))
    return out


def past_due_rows(matrix: dict) -> list[dict]:
    """各工作中心的逾期负荷（非零才列出）。"""
    return [
        {"work_center": wc, "past_due": row["past_due"]}
        for wc, row in sorted(matrix.items())
        if row["past_due"] != 0.0
    ]


def compare_results(
    matrix_a: dict,
    matrix_b: dict,
    cal_a: CalendarVersion,
    cal_b: CalendarVersion,
    weeks: int,
    top: int = 10,
) -> dict:
    """对比两个负荷结果；超负荷状态各自用其绑定的能力日历判定。

    返回：差异最大的格子（按 |delta| 降序，最多 top 条）、
    超负荷状态翻转的格子（正常↔超负荷）、逾期负荷变化、总量。
    注意：即使负荷没变，能力日历变化也会导致状态翻转。
    """
    zeros = [0.0] * weeks
    diffs: list[dict] = []
    transitions: list[dict] = []
    past_changes: list[dict] = []
    total_a = total_b = past_a = past_b = 0.0

    for wc in sorted(set(matrix_a) | set(matrix_b)):
        row_a = matrix_a.get(wc)
        row_b = matrix_b.get(wc)
        weeks_a = row_a["weeks"] if row_a else zeros
        weeks_b = row_b["weeks"] if row_b else zeros
        pd_a = row_a["past_due"] if row_a else 0.0
        pd_b = row_b["past_due"] if row_b else 0.0
        total_a += sum(weeks_a)
        total_b += sum(weeks_b)
        past_a += pd_a
        past_b += pd_b
        if pd_a != pd_b:
            past_changes.append(
                {"work_center": wc, "past_due_a": pd_a, "past_due_b": pd_b, "delta": pd_b - pd_a}
            )
        for i in range(weeks):
            la, lb = weeks_a[i], weeks_b[i]
            if la != lb:
                diffs.append(
                    {"work_center": wc, "week": i + 1, "load_a": la, "load_b": lb, "delta": lb - la}
                )
            cap_a = cal_a.capacity(wc, i + 1)
            cap_b = cal_b.capacity(wc, i + 1)
            over_a = la > cap_a
            over_b = lb > cap_b
            if over_a != over_b:
                transitions.append(
                    {
                        "work_center": wc,
                        "week": i + 1,
                        "from": "overload" if over_a else "normal",
                        "to": "overload" if over_b else "normal",
                        "load_a": la,
                        "capacity_a": cap_a,
                        "load_b": lb,
                        "capacity_b": cap_b,
                    }
                )
    diffs.sort(key=lambda d: (-abs(d["delta"]), d["work_center"], d["week"]))
    return {
        "top_differences": diffs[:top],
        "overload_transitions": transitions,
        "past_due_changes": past_changes,
        "total_load_a": total_a,
        "total_load_b": total_b,
        "total_past_due_a": past_a,
        "total_past_due_b": past_b,
    }
