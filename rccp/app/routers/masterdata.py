"""主数据：产品与工作中心。"""

from __future__ import annotations

from fastapi import APIRouter

from ..models import ProductIn, WorkcenterIn
from ..services import get_services

router = APIRouter(prefix="/api", tags=["master-data"])


@router.post("/products", status_code=201)
def create_product(body: ProductIn):
    svc = get_services()
    svc.registry.create_product(body.code, body.name)
    svc.planner.bump_registry_epoch()
    return {"code": body.code, "name": body.name}


@router.get("/products")
def list_products():
    return get_services().registry.list_products()


@router.post("/workcenters", status_code=201)
def create_workcenter(body: WorkcenterIn):
    svc = get_services()
    svc.registry.create_workcenter(body.code, body.name)
    svc.planner.bump_registry_epoch()
    return {"code": body.code, "name": body.name}


@router.get("/workcenters")
def list_workcenters():
    return get_services().registry.list_workcenters()
