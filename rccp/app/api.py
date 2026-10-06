"""FastAPI 路由与异常处理。"""

from __future__ import annotations

from .routers import calendar, masterdata, plans, query, routing


def include_routers(app) -> None:
    app.include_router(masterdata.router)
    app.include_router(routing.router)
    app.include_router(calendar.router)
    app.include_router(plans.router)
    app.include_router(query.router)
