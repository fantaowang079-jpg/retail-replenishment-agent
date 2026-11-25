from datetime import date
from enum import Enum
import re

from pydantic import BaseModel, Field, field_validator, model_validator


class Scenario(str, Enum):
    shortage = "shortage"
    slow = "slow"
    promotion = "promotion"


class AnalysisRequest(BaseModel):
    tenant: str = Field(default="demo", pattern=r"^[\w-]{1,50}$")
    warehouse: str = Field(default="WH-SH", pattern=r"^[\w-]{1,50}$")
    skus: list[str] = Field(default_factory=lambda: ["SKU-001", "SKU-002", "SKU-003"], min_length=1, max_length=30)
    start_date: date
    end_date: date
    scenario: Scenario = Scenario.shortage
    question: str = Field(default="分析库存风险并提供补货建议", max_length=1000)
    safety_days: int = Field(default=7, ge=0, le=90)
    review_days: int = Field(default=7, ge=1, le=90)
    promotion_multiplier: float = Field(default=1.5, ge=1, le=10, allow_inf_nan=False)

    @field_validator("skus")
    @classmethod
    def check_skus(cls, values):
        if any(not re.fullmatch(r"[\w-]{1,50}", v) for v in values):
            raise ValueError("商品编号仅允许文字、数字、下划线和短横线")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def check_dates(self):
        if self.start_date > self.end_date or (self.end_date - self.start_date).days > 365:
            raise ValueError("统计期需为正序且不超过 366 天")
        if self.end_date > date.today():
            raise ValueError("统计结束日期不能晚于今天")
        return self


class Gap(BaseModel):
    sku: str
    field: str
    message: str
    blocking: bool = True


class Evidence(BaseModel):
    tool: str
    table: str
    tenant: str
    warehouse: str
    period: str
    rows: int
    cached: bool = False
    fetched_at: str


class Recommendation(BaseModel):
    sku: str
    name: str
    sales_qty: float | None = None
    previous_sales_qty: float | None = None
    growth_percent: float | None = None
    daily_sales: float | None = None
    available_stock: float | None = None
    reported_available: float | None = None
    coverage_days: float | None = None
    incoming_qty: float | None = None
    eligible_incoming_qty: float | None = None
    lead_days: int | None = None
    pack_size: int | None = None
    target_stock: float | None = None
    suggested_qty: int | None = None
    risk: str
    formula: str


class Narrative(BaseModel):
    overview: str = Field(max_length=1000)
    reasoning: str = Field(max_length=1500)
    cautions: str = Field(max_length=1000)

    @field_validator("overview", "reasoning", "cautions")
    @classmethod
    def no_generated_numbers(cls, value):
        # 数值只从计算表展示。模型文本不允许夹带数字或中文数量。
        if re.search(r"\d|[零一二三四五六七八九十百千万两]+(?:个|件|天|日|周|月|成|倍|百分|千分)|百分之|千分之", value):
            raise ValueError("解读文本不允许生成数值")
        return value


class Report(BaseModel):
    task_id: str
    request: AnalysisRequest
    recommendations: list[Recommendation]
    evidence: list[Evidence]
    gaps: list[Gap]
    checks: list[str]
    narrative: Narrative
    llm_status: str
    generated_at: str
    status: str = "pending_review"
    review: dict | None = None


class Adjustment(BaseModel):
    sku: str
    quantity: int = Field(ge=0, le=10000000)
    reason: str = Field(min_length=1, max_length=500)


class ReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=80)
    decision: str = Field(pattern=r"^(approve|reject)$")
    note: str = Field(default="", max_length=2000)
    acknowledge_gaps: bool = False
    adjustments: list[Adjustment] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_skus(self):
        if len({a.sku for a in self.adjustments}) != len(self.adjustments):
            raise ValueError("同一商品不能重复调整")
        return self
