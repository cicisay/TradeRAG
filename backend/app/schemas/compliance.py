"""合规接口的请求/响应模型。"""
from typing import Literal

from pydantic import BaseModel, Field


class ComplianceCheckIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


class ComplianceCheckOut(BaseModel):
    action: Literal["pass", "annotate", "block"]
    layer: str | None = None
    hits: list[dict] = []
    intents: list[dict] = []
    rule: str | None = None
    notice: str | None = None


class TermIn(BaseModel):
    term: str = Field(min_length=1, max_length=128)
    term_type: Literal["controlled_item", "sanctioned_country", "restricted_org", "evasion_phrase"]
    variants: list[str] = []
    severity: Literal["block", "annotate"] = "annotate"
    notice: str | None = None
    source: str | None = None


class TermOut(BaseModel):
    id: int
    term: str
    term_type: str
    variants: list[str]
    severity: str
    notice: str | None
    status: str
    version: int
    created_by: str | None
    approved_by: str | None


class RuleIn(BaseModel):
    rule_name: str = Field(min_length=1, max_length=128)
    countries: list[str] = ["*"]
    items: list[str] = ["*"]
    intents: list[str] = ["*"]
    action: Literal["block", "annotate"] = "annotate"
    notice: str | None = None
    priority: int = 100


class RuleOut(BaseModel):
    id: int
    rule_name: str
    countries: list[str]
    items: list[str]
    intents: list[str]
    action: str
    priority: int
    status: str
    version: int
    approved_by: str | None


class EventOut(BaseModel):
    id: int
    request_id: str | None
    user_id: int | None
    layer: str
    hit_terms: list[str]
    region_or_item: str | None
    action: str
    detail: str | None
    created_at: str
