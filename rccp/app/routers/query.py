"""负荷查询：矩阵切片、超负荷清单、结果列表/对比。"""

from __future__ import annotations

import numpy as np

from fastapi import APIRouter

from ..errors import ValidationError
from ..services import get_services

router = APIRouter(prefix="/api", tags=["load-query"])


def _result_and_capacity(
    svc, plan_id: str, result_id: int | None
) -> tuple[dict, np.ndarray, int | None]:
    if result_id is not None:
        result = svc.results.get(result_id)
        if result["plan_id"] != plan_id:
            raise ValidationError("结果不属于该计划")
    else:
        result = svc.results.bound_result(plan_id)
    meta = svc.planner._get_meta(plan_id)
    # 用结果绑定的引擎坐标重建能力矩阵（published/recalc 与各自日历版本对齐）
    engine = svc.planner._build_engine(
        result["routing_version"], result["lead_split"], result["pre_horizon"]
    )
    capacity = engine.build_capacity(
        svc.capacities.sparse_of(result["capacity_version"])
    )
    return result, capacity, result["id"]


@router.get("/plans/{plan_id}/load")
def load_matrix(
    plan_id: str,
    workcenter: str | None = None,
    week_from: int | None = None,
    week_to: int | None = None,
    result_id: int | None = None,
):
    svc = get_services()
    result, capacity, rid = _result_and_capacity(svc, plan_id, result_id)
    body = svc.results.slice(
        result,
        capacity,
        workcenter=workcenter,
        week_from=week_from,
        week_to=week_to,
    )
    body["result_id"] = rid
    return body


@router.get("/plans/{plan_id}/overloads")
def overloads(plan_id: str, result_id: int | None = None):
    svc = get_services()
    result, capacity, rid = _result_and_capacity(svc, plan_id, result_id)
    return {
        "plan_id": plan_id,
        "result_id": rid,
        "overloads": svc.results.overloads(result, capacity),
    }


@router.get("/plans/{plan_id}/results")
def list_results(plan_id: str):
    get_services().planner._get_meta(plan_id)
    return get_services().results.list_for_plan(plan_id)


@router.get("/results/{result_id}")
def get_result(result_id: int):
    svc = get_services()
    result = svc.results.get(result_id)
    return {
        "id": result["id"],
        "plan_id": result["plan_id"],
        "policy": result["policy"],
        "routing_version": result["routing_version"],
        "capacity_version": result["capacity_version"],
        "lead_split": result["lead_split"],
        "pre_horizon": result["pre_horizon"],
        "workcenters": result["workcenters"],
        "created_at": result["created_at"],
        "recalc_of_result": result["recalc_of_result"],
        "total_load": float(result["load"].sum()),
        "total_overdue": float(result["overdue"].sum()),
    }


@router.get("/results/{left_id}/compare/{right_id}")
def compare_results(left_id: int, right_id: int, top_n: int = 20):
    svc = get_services()
    left = svc.results.get(left_id)
    right = svc.results.get(right_id)
    engine = svc.planner._build_engine(
        right["routing_version"], right["lead_split"], right["pre_horizon"]
    )
    cap = engine.build_capacity(
        svc.capacities.sparse_of(right["capacity_version"])
    )
    left_engine = svc.planner._build_engine(
        left["routing_version"], left["lead_split"], left["pre_horizon"]
    )
    left_cap = left_engine.build_capacity(
        svc.capacities.sparse_of(left["capacity_version"])
    )
    return svc.results.compare(
        left, right, cap, left_capacity=left_cap, top_n=top_n
    )
