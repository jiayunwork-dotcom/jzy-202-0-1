"""接口层 Pydantic 模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LeadSplitType = Literal["proportional", "floor", "ceil"]
PreHorizonType = Literal["overdue", "discard", "accumulate"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- 基础数据 ---------------------------------------------------------------


class ProductIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = ""


class WorkcenterIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = ""


# ---- 资源清单 ---------------------------------------------------------------


class RoutingLineIn(BaseModel):
    product: str
    workcenter: str
    hours_per_unit: float = Field(ge=0)
    lead_weeks: float = Field(ge=0)


class RoutingVersionCreate(BaseModel):
    # 为空表示创建空草稿；给 published 版本号则复制该版本作为起点
    copy_from: int | None = None
    note: str = ""
    lines: list[RoutingLineIn] | None = None  # 给了就整体替换草稿行


class RoutingReplace(BaseModel):
    lines: list[RoutingLineIn]


class RoutingVersionOut(BaseModel):
    version: int
    status: str
    note: str
    created_at: str
    published_at: str | None
    line_count: int


class RoutingVersionDetail(RoutingVersionOut):
    lines: list[RoutingLineIn]


# ---- 能力日历 ---------------------------------------------------------------


class CapacityWeekIn(BaseModel):
    workcenter: str
    week: int = Field(ge=1)
    hours: float = Field(ge=0)


class CapacityVersionCreate(BaseModel):
    copy_from: int | None = None
    note: str = ""
    weeks: list[CapacityWeekIn] | None = None


class CapacityReplace(BaseModel):
    weeks: list[CapacityWeekIn]


class CapacityVersionOut(BaseModel):
    version: int
    status: str
    note: str
    created_at: str
    published_at: str | None
    week_count: int


class CapacityVersionDetail(CapacityVersionOut):
    weeks: list[CapacityWeekIn]


class PublishedRef(BaseModel):
    routing_version: int | None = None
    capacity_version: int | None = None


# ---- 计划草稿 ---------------------------------------------------------------


class DraftCreate(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = ""
    routing_version: int | None = None   # None -> 当前最新已发布清单
    capacity_version: int | None = None  # None -> 当前最新已发布能力
    lead_split: LeadSplitType = "proportional"
    pre_horizon: PreHorizonType = "overdue"
    cells: list["PlanCellIn"] | None = None  # 创建时批量导入


class PlanCellIn(BaseModel):
    product: str
    week: int = Field(ge=1)
    quantity: float = Field(ge=0)


class BulkImport(BaseModel):
    mode: Literal["merge", "replace"] = "merge"
    # merge：按 (product, week) upsert，其余格子保留
    # replace：先清空草稿再整体写入
    cells: list[PlanCellIn]


class CellEdit(BaseModel):
    product: str
    week: int = Field(ge=1)
    quantity: float = Field(ge=0)
    expected_quantity: float | None = None  # 乐观锁：调用方认为的旧值


class CurrentValue(BaseModel):
    product: str
    week: int
    current_quantity: float


# ---- 查询 / 结果 ------------------------------------------------------------


class LoadSlice(BaseModel):
    plan_id: str
    routing_version: int
    capacity_version: int
    lead_split: str
    pre_horizon: str
    horizon: int
    workcenters: list[str]
    # rows：每个工作中心一行，weeks 为从 week_from 起的周负荷
    rows: list["LoadRow"]
    overdue: dict[str, float]


class LoadRow(BaseModel):
    workcenter: str
    week_from: int
    load: list[float]
    capacity: list[float]
    utilization: list[float | None]


class OverloadItem(BaseModel):
    workcenter: str
    week: int
    load: float
    capacity: float
    excess: float
    utilization: float


class PublishOut(BaseModel):
    draft_id: str
    plan_id: str
    version: int
    routing_version: int
    capacity_version: int
    result_id: int


class DeltaItem(BaseModel):
    workcenter: str
    week: int
    old_load: float
    new_load: float
    delta: float


class FlipItem(BaseModel):
    workcenter: str
    week: int
    type: str
    old_load: float
    new_load: float
    capacity: float


class CompareOut(BaseModel):
    left: str
    right: str
    sum_abs_delta: float
    max_abs_delta: float
    top_deltas: list[DeltaItem]
    flips: list[FlipItem]
    overdue_delta: dict[str, float]


class RecalcRequest(BaseModel):
    routing_version: int | None = None
    capacity_version: int | None = None


class RecalcOut(BaseModel):
    plan_id: str
    old_result_id: int
    new_result_id: int
    routing_version: int
    capacity_version: int
    compare: CompareOut


class UncoveredReport(BaseModel):
    plan_id: str
    uncovered_products: list[str]
    message: str


class DraftSummary(BaseModel):
    id: str
    name: str
    status: str
    routing_version: int
    capacity_version: int
    lead_split: str
    pre_horizon: str
    cell_count: int
    uncovered_products: list[str]


DraftCreate.model_rebuild()
LoadSlice.model_rebuild()
