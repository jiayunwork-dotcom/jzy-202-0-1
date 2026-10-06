"""服务装配（依赖注入容器）与应用生命周期。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .calendar import CapacityService
from .config import settings
from .database import Database
from .planner import PlannerService
from .registry import Registry
from .results import ResultService
from .routing import RoutingService


@dataclass
class Services:
    db: Database
    registry: Registry
    routings: RoutingService
    capacities: CapacityService
    planner: PlannerService
    results: ResultService


@lru_cache(maxsize=1)
def get_services() -> Services:
    db = Database(path=settings.db_path, horizon=settings.horizon_weeks)
    registry = Registry(db)
    routings = RoutingService(db, registry)
    capacities = CapacityService(db, registry)
    results = ResultService(db)
    planner = PlannerService(db, registry, routings, capacities, results)
    svc = Services(db, registry, routings, capacities, planner, results)
    # 重启恢复：装载全部草稿，按数量表核对/修复负荷矩阵
    report = planner.bootstrap()
    return svc
