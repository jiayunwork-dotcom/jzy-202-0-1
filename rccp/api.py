"""HTTP 接口层（FastAPI 路由）。所有路由只装配参数并委托给 service。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from .bom import BomLine
from .schemas import (
    BatchEditIn,
    BomVersionIn,
    CalendarVersionIn,
    CellEditIn,
    PublishIn,
    RecomputeIn,
)
from .service import BatchCell, RCCPService


def get_service(request: Request) -> RCCPService:
    return request.app.state.service


router = APIRouter(prefix="/api")


# ---------------- 元信息 / 注册表 ----------------
@router.get("/meta")
async def meta(svc: RCCPService = Depends(get_service)):
    return svc.meta()


@router.get("/products")
async def products(svc: RCCPService = Depends(get_service)):
    return {"products": sorted(svc.known_products())}


@router.get("/work-centers")
async def work_centers(svc: RCCPService = Depends(get_service)):
    return {"work_centers": sorted(svc.known_work_centers())}


# ---------------- 资源清单 ----------------
@router.post("/bom-versions")
async def create_bom(body: BomVersionIn, svc: RCCPService = Depends(get_service)):
    lines = [
        BomLine(l.product, l.work_center, l.hours_per_unit, l.lead_weeks) for l in body.lines
    ]
    return await svc.create_bom_version(lines, body.note)


@router.get("/bom-versions")
async def list_boms(svc: RCCPService = Depends(get_service)):
    return {"versions": svc.list_bom_versions()}


@router.get("/bom-versions/current")
async def current_bom(svc: RCCPService = Depends(get_service)):
    return svc.current_bom_payload()


@router.get("/bom-versions/{version}")
async def get_bom(version: int, svc: RCCPService = Depends(get_service)):
    return svc.bom_payload(version)


# ---------------- 能力日历 ----------------
@router.post("/calendar-versions")
async def create_calendar(body: CalendarVersionIn, svc: RCCPService = Depends(get_service)):
    entries = [(e.work_center, e.week, e.available_hours) for e in body.entries]
    return await svc.create_calendar_version(entries, body.note)


@router.get("/calendar-versions")
async def list_calendars(svc: RCCPService = Depends(get_service)):
    return {"versions": svc.list_calendar_versions()}


@router.get("/calendar-versions/current")
async def current_calendar(svc: RCCPService = Depends(get_service)):
    return svc.current_calendar_payload()


@router.get("/calendar-versions/{version}")
async def get_calendar(version: int, svc: RCCPService = Depends(get_service)):
    return svc.calendar_payload(version)


# ---------------- 计划草稿 ----------------
@router.get("/draft")
async def get_draft(svc: RCCPService = Depends(get_service)):
    return svc.get_draft()


@router.put("/draft/cell")
async def edit_cell(body: CellEditIn, svc: RCCPService = Depends(get_service)):
    return await svc.edit_cell(body.product, body.week, body.qty, body.expected_old_qty)


@router.post("/draft/batch")
async def batch_edit(body: BatchEditIn, svc: RCCPService = Depends(get_service)):
    cells = [BatchCell(c.product, c.week, c.qty, c.expected_old_qty) for c in body.cells]
    return await svc.batch_edit(cells)


@router.get("/draft/load")
async def draft_load(
    work_center: str | None = None,
    week: int | None = None,
    svc: RCCPService = Depends(get_service),
):
    return svc.draft_load(work_center, week)


@router.get("/draft/overloads")
async def draft_overloads(svc: RCCPService = Depends(get_service)):
    return svc.draft_overloads()


# ---------------- 计划版本 ----------------
@router.post("/plan-versions")
async def publish(body: PublishIn, svc: RCCPService = Depends(get_service)):
    return await svc.publish(body.note)


@router.get("/plan-versions")
async def list_plan_versions(svc: RCCPService = Depends(get_service)):
    return svc.list_plan_versions()


# 注意：/plan-versions/compare 必须注册在 /plan-versions/{version} 之前
@router.get("/plan-versions/compare")
async def compare_plan_versions(
    a: int = Query(...),
    b: int = Query(...),
    top: int = Query(10, ge=1, le=500),
    svc: RCCPService = Depends(get_service),
):
    return svc.compare_versions(a, b, top)


@router.get("/plan-versions/{version}")
async def get_plan_version(version: int, svc: RCCPService = Depends(get_service)):
    return svc.get_plan_version(version)


@router.get("/plan-versions/{version}/load")
async def plan_version_load(
    version: int,
    work_center: str | None = None,
    week: int | None = None,
    svc: RCCPService = Depends(get_service),
):
    return svc.version_load(version, work_center, week)


@router.get("/plan-versions/{version}/overloads")
async def plan_version_overloads(version: int, svc: RCCPService = Depends(get_service)):
    return svc.version_overloads(version)


@router.get("/plan-versions/{version}/results")
async def plan_version_results(version: int, svc: RCCPService = Depends(get_service)):
    return svc.version_results(version)


@router.post("/plan-versions/{version}/recompute")
async def recompute_plan_version(
    version: int, body: RecomputeIn, svc: RCCPService = Depends(get_service)
):
    return await svc.recompute_version(version, body.bom_version, body.calendar_version)
