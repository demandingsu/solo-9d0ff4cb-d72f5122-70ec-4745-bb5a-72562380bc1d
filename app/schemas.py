"""请求/响应 Pydantic 模型（同时驱动 /docs 的 Schema 与示例）。"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field, model_validator


def _normalize_edition(value: Any) -> str:
    """版号按精确字符串比对；方便演示，也接受整数形式（自动转字符串）。"""
    if isinstance(value, bool):
        raise ValueError("版号不能是布尔值")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("版号必须是非空字符串或整数")


EditionNo = Annotated[str, BeforeValidator(_normalize_edition)]


# --------------------------------------------------------------------------- #
# 基础录入
# --------------------------------------------------------------------------- #
class BookCreate(BaseModel):
    title: str = Field("未命名图书", max_length=200, description="书名/资料名")
    page_versions: list[EditionNo] = Field(
        ...,
        min_length=1,
        description="全书逐页目标版号，按第 1 页…第 N 页顺序给出；数组长度即总页数",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "样书演示（6 页）",
                "page_versions": ["E1", "E1", "E2", "E2", "E3", "E3"],
            }
        }
    }


class SampleIn(BaseModel):
    code: str | None = Field(None, max_length=80, description="样书编号；不传则自动生成")
    bound_to_page: int = Field(
        ..., ge=0, description="该本样书已经配好的末页页码；0 表示一页都没配"
    )


class SampleBatchCreate(BaseModel):
    samples: list[SampleIn] = Field(..., min_length=1)

    model_config = {
        "json_schema_extra": {
            "example": {
                "samples": [
                    {"code": "YB-01", "bound_to_page": 0},
                    {"code": "YB-02", "bound_to_page": 2},
                    {"code": "YB-03", "bound_to_page": 2},
                ]
            }
        }
    }


class PileCreate(BaseModel):
    code: str | None = Field(None, max_length=80, description="垛号；不传则自动生成")
    start_page: int = Field(..., ge=1, description="本帖起始页码")
    page_versions: list[EditionNo] = Field(
        ..., min_length=1, description="本帖逐页版号，第 1 项对应 start_page"
    )
    copies: int = Field(..., ge=1, description="本垛相同散帖的份数")

    model_config = {
        "json_schema_extra": {
            "example": {
                "code": "D-36",
                "start_page": 3,
                "page_versions": ["E2", "E2", "E3", "E3"],
                "copies": 2,
            }
        }
    }


class PileBatchCreate(BaseModel):
    piles: list[PileCreate] = Field(..., min_length=1)


# --------------------------------------------------------------------------- #
# 试配
# --------------------------------------------------------------------------- #
class TrialRequest(BaseModel):
    sample_ids: list[int] = Field(
        default_factory=list,
        description="要试配的一批样书 ID；留空表示本书全部未领料样书",
    )

    model_config = {"json_schema_extra": {"example": {"sample_ids": []}}}


class PileLine(BaseModel):
    pile_id: int
    pile_code: str
    start_page: int
    end_page: int
    quantity: int = 1
    page_editions: list[str] = Field(default_factory=list)


class PickList(BaseModel):
    sample_id: int
    sample_code: str
    bound_to_page: int
    missing_from_page: int
    items: list[PileLine]


class SkippedSample(BaseModel):
    sample_id: int
    code: str
    reason: str
    detail: str | None = None


class UnselectedSample(BaseModel):
    sample_id: int
    code: str
    bound_to_page: int
    reason: str = Field(
        ...,
        description="no_valid_chain=无版号相符的连续帖链；out_of_stock=当前余量下单独也配不齐；"
        "contention=单独可配但在本批最大同时方案中被争用挤掉",
    )


class StockSnapshotItem(BaseModel):
    pile_id: int
    code: str
    copies_total: int
    copies_remaining: int


class PileDiagnostic(BaseModel):
    pile_id: int
    code: str
    usable: bool
    reason: str | None = None
    mismatched_pages: list[int] = Field(default_factory=list)


class ConfirmItemIn(BaseModel):
    pile_id: int = Field(..., description="散帖垛 ID")
    quantity: int = Field(1, ge=1, description="逐本取帖单每行恒为 1（一本书不会在同一条连续链里重复用同一帖）")


class ConfirmPickIn(BaseModel):
    sample_id: int
    items: list[ConfirmItemIn] = Field(..., min_length=1)


class ConfirmRequest(BaseModel):
    book_id: int
    request_id: str | None = Field(
        None, max_length=120, description="可选客户端幂等号；同号同单重发返回原领料单"
    )
    pick_lists: list[ConfirmPickIn] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _samples_unique(self) -> "ConfirmRequest":
        ids = [p.sample_id for p in self.pick_lists]
        if len(ids) != len(set(ids)):
            raise ValueError("同一张领料单里同一本样书只能出现一次")
        for pl in self.pick_lists:
            pids = [it.pile_id for it in pl.items]
            if len(pids) != len(set(pids)):
                raise ValueError(f"样书 {pl.sample_id} 的取帖单中同一散帖垛重复出现")
            if any(it.quantity != 1 for it in pl.items):
                raise ValueError(
                    f"样书 {pl.sample_id} 的逐本取帖单每行数量必须为 1；"
                    "同垛多本的汇总数量由服务端聚合"
                )
        return self

    model_config = {
        "json_schema_extra": {
            "example": {
                "book_id": 1,
                "request_id": "可选-客户端幂等号-0001",
                "pick_lists": [
                    {"sample_id": 2, "items": [{"pile_id": 2}, {"pile_id": 4}]},
                    {"sample_id": 3, "items": [{"pile_id": 3}]},
                ],
            }
        }
    }


class TrialResult(BaseModel):
    plan_id: str
    book_id: int
    computed_at: str
    candidate_count: int
    selected_count: int
    selected_sample_ids: list[int]
    pick_lists: list[PickList]
    skipped_samples: list[SkippedSample]
    unselected_samples: list[UnselectedSample]
    stock_snapshot: list[StockSnapshotItem]
    pile_diagnostics: list[PileDiagnostic]
    fingerprint: str = Field(..., description="方案规范化指纹，也是同方案重发的幂等依据")
    # 刻意用宽松 dict：0 本入选时 pick_lists 为空，无法通过“至少一单”的确认入参校验。
    confirm_request: dict[str, Any] = Field(
        ..., description="把这个对象原样 POST 到 /api/requisitions 即可确认领料（0 本入选时为空单，不能确认）"
    )


# --------------------------------------------------------------------------- #
# 领料单
# --------------------------------------------------------------------------- #
class AggregatedItem(BaseModel):
    pile_id: int
    pile_code: str
    quantity: int = Field(..., description="本单从该垛汇总领取的份数")
    copies_remaining_after: int


class OrderDetail(BaseModel):
    id: int
    order_no: str
    book_id: int
    request_id: str | None
    fingerprint: str
    status: str
    created_at: str
    sample_count: int
    total_pieces: int
    sample_ids: list[int]
    pick_lists: list[PickList]
    aggregated_items: list[AggregatedItem]


class ConfirmResponse(BaseModel):
    replayed: bool = False
    replay_reason: str | None = None
    order: OrderDetail
