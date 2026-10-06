"""全局配置。

计划期长度 ``HORIZON_WEEKS`` 在库初始化时写入 meta 表，之后所有版本共用同一
计划期，保证负荷矩阵的列坐标（第 1..H 周）在任意版本之间可以直接相减。
周号约定为 1-based 整数。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} 必须为正整数，收到 {value!r}")
    return value


@dataclass(frozen=True)
class Settings:
    # SQLite 库文件所在目录（挂载卷）。
    data_dir: str = os.environ.get("RCCP_DATA_DIR", "/data")
    db_name: str = os.environ.get("RCCP_DB_NAME", "rccp.db")
    horizon_weeks: int = _env_int("RCCP_HORIZON_WEEKS", 20)
    # 小数提前量的默认拆分规则：proportional | floor | ceil
    lead_split: str = os.environ.get("RCCP_LEAD_SPLIT", "proportional")
    # 计划期开始之前负荷的默认处理：overdue | discard | accumulate
    pre_horizon: str = os.environ.get("RCCP_PRE_HORIZON", "overdue")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, self.db_name)


settings = Settings()
