"""测试夹具：每个测试用独立临时库 + 独立服务容器。

做法：通过环境变量指向临时目录，重载 app.config / app.database，然后重载
依赖它们的服务层模块（pydantic 模型层不重载，避免 isinstance 身份漂移）。
"""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture
def svc(tmp_path):
    os.environ["RCCP_DATA_DIR"] = str(tmp_path)
    os.environ["RCCP_DB_NAME"] = "test.db"
    os.environ["RCCP_HORIZON_WEEKS"] = "12"

    import app.config as config
    importlib.reload(config)
    import app.database as database
    importlib.reload(database)
    import app.registry as registry
    importlib.reload(registry)
    import app.routing as routing
    importlib.reload(routing)
    import app.calendar as calendar
    importlib.reload(calendar)
    import app.results as results
    importlib.reload(results)
    import app.planner as planner
    importlib.reload(planner)
    import app.services as services
    importlib.reload(services)
    services.get_services.cache_clear()

    s = services.get_services()
    yield s
    services.get_services.cache_clear()


@pytest.fixture
def client(svc):
    from fastapi.testclient import TestClient

    import app.routers.masterdata as masterdata
    importlib.reload(masterdata)
    import app.routers.routing as routing_r
    importlib.reload(routing_r)
    import app.routers.calendar as calendar_r
    importlib.reload(calendar_r)
    import app.routers.plans as plans_r
    importlib.reload(plans_r)
    import app.routers.query as query_r
    importlib.reload(query_r)
    import app.api as api
    importlib.reload(api)
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def seeded(svc):
    """标准主数据 + 一个已发布资源清单与能力日历。"""
    from app.models import CapacityWeekIn, RoutingLineIn

    svc.registry.create_product("P1", "产品1")
    svc.registry.create_product("P2", "产品2")
    svc.registry.create_workcenter("INJ", "注塑")
    svc.registry.create_workcenter("ASM", "装配")
    svc.planner.bump_registry_epoch()

    rv = svc.routings.create_draft(lines=[
        RoutingLineIn(product="P1", workcenter="INJ", hours_per_unit=2.0, lead_weeks=1.5),
        RoutingLineIn(product="P1", workcenter="ASM", hours_per_unit=0.5, lead_weeks=0.0),
        RoutingLineIn(product="P2", workcenter="INJ", hours_per_unit=1.0, lead_weeks=1.0),
    ])
    svc.routings.publish(rv)
    cv = svc.capacities.create_draft(weeks=[
        CapacityWeekIn(workcenter="INJ", week=w, hours=100.0) for w in range(1, 13)
    ] + [
        CapacityWeekIn(workcenter="ASM", week=w, hours=40.0) for w in range(1, 13)
    ])
    svc.capacities.publish(cv)
    return {"routing": rv, "capacity": cv}
