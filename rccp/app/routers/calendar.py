"""能力日历版本接口。"""

from __future__ import annotations

from fastapi import APIRouter

from ..models import CapacityReplace, CapacityVersionCreate
from ..services import get_services

router = APIRouter(prefix="/api/capacities", tags=["capacity"])


@router.post("/versions", status_code=201)
def create_capacity_draft(body: CapacityVersionCreate):
    svc = get_services()
    version = svc.capacities.create_draft(
        copy_from=body.copy_from, note=body.note, weeks=body.weeks
    )
    return svc.capacities.get(version)


@router.get("/versions")
def list_capacity_versions():
    return get_services().capacities.list_versions()


@router.get("/versions/{version}")
def get_capacity_version(version: int):
    return get_services().capacities.get(version)


@router.put("/versions/{version}/weeks")
def replace_capacity_weeks(version: int, body: CapacityReplace):
    svc = get_services()
    svc.capacities.replace_weeks(version, body.weeks)
    return svc.capacities.get(version)


@router.post("/versions/{version}/publish")
def publish_capacity(version: int):
    return get_services().capacities.publish(version)
