"""领域异常。api 层把它们映射为对应的 HTTP 状态码。"""
from __future__ import annotations


class DomainError(Exception):
    status_code = 400

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


class ValidationFailed(DomainError):
    """请求数据违反领域规则（负数、越界周号、未知引用等）。"""

    status_code = 422


class NotFound(DomainError):
    status_code = 404


class Conflict(DomainError):
    """状态冲突：同格 CAS 失败（带当前值）、前置条件不满足等。"""

    status_code = 409
