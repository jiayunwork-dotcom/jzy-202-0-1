"""API 请求模型（形状与基础校验；领域规则在 service 层校验）。

批量导入（BatchCellIn）故意放宽为 Any：来自表格导入的行可能带各种脏数据，
由 service 层逐行校验并一次性返回全部错误（哪一行、什么问题），
而不是让 pydantic 在第一个坏行就中断。
"""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints


def _reject_bool(v):
    if isinstance(v, bool):
        raise ValueError("boolean is not a valid number")
    return v


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
NonNegFloat = Annotated[
    float, BeforeValidator(_reject_bool), Field(ge=0.0, allow_inf_nan=False)
]
WeekNo = Annotated[int, BeforeValidator(_reject_bool), Field(ge=1)]


def _qty(v):
    """数量必须是非负整数；整数值的浮点（如 3.0）接受并转换。"""
    _reject_bool(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    raise ValueError("quantity must be an integer")


Qty = Annotated[int, BeforeValidator(_qty), Field(ge=0)]


class BomLineIn(BaseModel):
    product: Name
    work_center: Name
    hours_per_unit: NonNegFloat
    lead_weeks: NonNegFloat


class BomVersionIn(BaseModel):
    lines: list[BomLineIn] = Field(min_length=1)
    note: str = ""


class CalendarEntryIn(BaseModel):
    work_center: Name
    week: WeekNo
    available_hours: NonNegFloat


class CalendarVersionIn(BaseModel):
    entries: list[CalendarEntryIn] = Field(min_length=1)
    note: str = ""


class CellEditIn(BaseModel):
    """逐格编辑。expected_old_qty 必填（CAS）：调用方必须带上它基于的旧值，
    与当前值不一致时拒绝并返回当前值。新格子的旧值为 0。"""

    product: Name
    week: WeekNo
    qty: Qty
    expected_old_qty: Qty


class BatchCellIn(BaseModel):
    """批量导入的行。字段值由 service 层逐行校验（错误按行号汇总返回）。
    expected_old_qty 可选：带了就校验，不带则无条件覆盖。"""

    product: Any = None
    week: Any = None
    qty: Any = None
    expected_old_qty: Any = None


class BatchEditIn(BaseModel):
    cells: list[BatchCellIn] = Field(min_length=1)


class PublishIn(BaseModel):
    note: str = ""


class RecomputeIn(BaseModel):
    """按新清单重算：缺省用当前 BOM / 当前日历。"""

    bom_version: int | None = None
    calendar_version: int | None = None
