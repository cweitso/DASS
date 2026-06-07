from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

ConcurrencyPolicy = Literal["allow", "forbid", "replace"]
ActionType = Literal["http", "shell"]


class HttpActionConfig(BaseModel):
    method: str = Field(default="GET")
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | dict[str, Any] | None = None
    timeout_seconds: int = Field(default=30, ge=1)


class ShellActionConfig(BaseModel):
    command: str
    timeout_seconds: int = Field(default=30, ge=1)


class JobBase(BaseModel):
    name: str
    cron_expression: str
    action_type: ActionType
    action_config: dict[str, Any]
    enabled: bool = True
    concurrency_policy: ConcurrencyPolicy = "allow"
    max_retries: int = Field(default=0, ge=0)

    @field_validator("action_config")
    @classmethod
    def validate_action_config(cls, value: dict[str, Any], info):
        action_type = info.data.get("action_type")
        if action_type == "http":
            HttpActionConfig.model_validate(value)
        elif action_type == "shell":
            ShellActionConfig.model_validate(value)
        return value


class JobCreate(JobBase):
    # 沒帶 cron → 建成 job_type='normal'（即時/手動任務，scheduler 不碰，只能 /trigger）。
    # 有帶 cron → scheduled。覆寫成 optional 才能表達「即時任務不需要 cron」。
    cron_expression: str | None = None


class JobUpdate(BaseModel):
    name: str | None = None
    cron_expression: str | None = None
    action_type: ActionType | None = None
    action_config: dict[str, Any] | None = None
    enabled: bool | None = None
    concurrency_policy: ConcurrencyPolicy | None = None
    max_retries: int | None = Field(default=None, ge=0)


class JobRead(JobBase):
    id: UUID
    # normal job 沒有 cron / 下次執行時間，兩者都允許為 null 才能正確序列化回傳。
    cron_expression: str | None = None
    next_fire_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class JobListItem(BaseModel):
    id: UUID
    name: str
    cron_expression: str | None = None  # normal job 無 cron
    action_type: str
    action_config: dict[str, Any]
    enabled: bool
    concurrency_policy: str
    max_retries: int
    next_fire_at: datetime | None = None  # normal job 無下次執行時間
    created_at: datetime
    updated_at: datetime


class JobListResponse(BaseModel):
    items: list[JobListItem]
    page: int
    page_size: int
    total: int
    total_pages: int


class TriggerResponse(BaseModel):
    task_id: str
    status: str
