"""领域模型（Pydantic v2）。

命名刻意回避原项目：journey / spot / taste 而非 trip / poi / preference。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ─── 账号 ───
class AccountCreate(BaseModel):
    handle: str | None = None
    username: str | None = None
    email: str | None = None
    password: str = Field(min_length=1, max_length=128)


class AccountOut(BaseModel):
    id: str
    handle: str
    email: str | None = None
    created_at: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AuthOut(BaseModel):
    token: str
    username: str
    access_token: str | None = None
    token_type: str = "bearer"


# ─── 旅行偏好 ───
TasteCategory = Literal["节拍", "景点类型", "饮食", "交通", "预算"]


class TasteItem(BaseModel):
    category: TasteCategory
    value: str
    note: str | None = None


class TasteUpsert(BaseModel):
    items: list[TasteItem]


# ─── 行程 ───
class PlanRequest(BaseModel):
    need: str
    destination: str | None = None
    start_date: str | None = None
    days: int | None = None


class SpotOut(BaseModel):
    name: str
    category: str
    address: str | None = None
    lon: float | None = None
    lat: float | None = None
    arrive: str | None = None
    leave: str | None = None
    tip: str | None = None
    source: str = "builtin"


class DayOut(BaseModel):
    index: int
    date: str | None = None
    theme: str | None = None
    spots: list[SpotOut]


class PlanOut(BaseModel):
    journey_id: str
    destination: str
    days_count: int
    days: list[DayOut]
    warnings: list[str] = Field(default_factory=list)
    created_at: str


class JourneyBrief(BaseModel):
    id: str
    title: str | None
    destination: str | None
    days_count: int | None
    status: str
    created_at: str
