"""领域错误。所有 4xx 校验失败都抛 ``ValidationError``，由接口层统一转 422。"""

from __future__ import annotations

from typing import Any


class RccpError(Exception):
    """服务端错误基类。"""

    code = "rccp_error"
    status_code = 400

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return body


class ValidationError(RccpError):
    code = "validation_error"
    status_code = 422


class NotFoundError(RccpError):
    code = "not_found"
    status_code = 404


class ConflictError(RccpError):
    """乐观锁冲突（草稿同格并发覆盖）。返回 409 与当前值。"""

    code = "conflict"
    status_code = 409


class ImmutableError(RccpError):
    code = "immutable_version"
    status_code = 409
