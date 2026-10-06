"""主生产计划：草稿、逐格编辑、批量导入、发布、重算、版本对比。"""

from __future__ import annotations

from fastapi import APIRouter

from ..models import (
    BulkImport,
    CellEdit,
    DraftCreate,
    RecalcRequest,
)
from ..services import get_services

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.post("/drafts", status_code=201)
def create_draft(body: DraftCreate):
    return get_services().planner.create_draft(body)


@router.get("")
def list_plans(status: str | None = None):
    return get_services().planner.list_plans(status)


@router.get("/{plan_id}")
def get_plan(plan_id: str):
    return get_services().planner.get_draft(plan_id)


@router.get("/{plan_id}/cells")
def list_cells(plan_id: str):
    return get_services().planner.list_cells(plan_id)


@router.get("/{plan_id}/uncovered")
def uncovered(plan_id: str):
    summary = get_services().planner.get_draft(plan_id)
    missing = summary["uncovered_products"]
    return {
        "plan_id": plan_id,
        "uncovered_products": missing,
        "message": (
            "以下产品在当前资源清单版本中没有任何工作中心行，其负荷计为 0："
            + ", ".join(missing)
            if missing
            else "草稿内所有产品都被资源清单覆盖"
        ),
    }


@router.put("/{plan_id}/cell")
def edit_cell(plan_id: str, body: CellEdit):
    return get_services().planner.edit_cell(plan_id, body)


@router.post("/{plan_id}/import")
def bulk_import(plan_id: str, body: BulkImport):
    return get_services().planner.bulk_import(plan_id, body)


@router.post("/{draft_id}/publish")
def publish(draft_id: str):
    return get_services().planner.publish(draft_id)


@router.post("/{plan_id}/recalc")
def recalc(plan_id: str, body: RecalcRequest):
    return get_services().planner.recalc(
        plan_id,
        routing_version=body.routing_version,
        capacity_version=body.capacity_version,
    )


@router.get("/{left_id}/compare/{right_id}")
def compare_plans(left_id: str, right_id: str, top_n: int = 20):
    return get_services().planner.compare_plans(left_id, right_id, top_n=top_n)
