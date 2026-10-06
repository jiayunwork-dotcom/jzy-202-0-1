"""资源清单版本接口。"""

from __future__ import annotations

from fastapi import APIRouter

from ..models import RoutingReplace, RoutingVersionCreate
from ..services import get_services

router = APIRouter(prefix="/api/routings", tags=["routing"])


@router.post("/versions", status_code=201)
def create_routing_draft(body: RoutingVersionCreate):
    svc = get_services()
    version = svc.routings.create_draft(
        copy_from=body.copy_from, note=body.note, lines=body.lines
    )
    return svc.routings.get(version)


@router.get("/versions")
def list_routing_versions():
    return get_services().routings.list_versions()


@router.get("/versions/{version}")
def get_routing_version(version: int):
    return get_services().routings.get(version)


@router.put("/versions/{version}/lines")
def replace_routing_lines(version: int, body: RoutingReplace):
    svc = get_services()
    svc.routings.replace_lines(version, body.lines)
    return svc.routings.get(version)


@router.post("/versions/{version}/publish")
def publish_routing(version: int):
    return get_services().routings.publish(version)
