"""FastAPI 应用入口：``uvicorn app.main:app``。对外只暴露 HTTP。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import include_routers
from .errors import RccpError

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="RCCP 粗能力计划服务",
    version="1.0.0",
    description=(
        "维护资源清单/能力日历/主生产计划的版本，向量化展开工作中心周负荷，"
        "草稿增量维护，发布冻结，支持版本对比与按新清单重算。"
    ),
)


@app.exception_handler(RccpError)
async def rccp_error_handler(request: Request, exc: RccpError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


include_routers(app)


@app.get("/health", tags=["meta"])
def health():
    from .services import get_services

    svc = get_services()
    return {"status": "ok", "horizon_weeks": svc.db.get_horizon()}
