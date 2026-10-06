"""服务配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """weeks:   计划期长度（周），周号取值 1..weeks
    db_path: SQLite 库文件路径（部署时放在挂载卷上）
    """

    weeks: int = 20
    db_path: str = "/data/rccp.db"

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            weeks=int(os.environ.get("RCCP_WEEKS", "20")),
            db_path=os.environ.get("RCCP_DB_PATH", "/data/rccp.db"),
        )
