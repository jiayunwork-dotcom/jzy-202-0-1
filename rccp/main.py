"""应用工厂。uvicorn 入口在 rccp.asgi（避免 import 时创建数据库的副作用）。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api import router
from .config import Settings
from .errors import DomainError
from .service import RCCPService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = RCCPService(settings)
    app = FastAPI(title="RCCP — 粗能力计划服务", version="1.0.0")
    app.state.service = service

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"error": exc.message, **exc.extra}
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "invalid request", "details": jsonable_encoder(exc.errors())},
        )

    app.include_router(router)
    return app
