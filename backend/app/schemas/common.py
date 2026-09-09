"""Shared schema bases."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

Source = Literal["static", "dynamic"]
Status = Literal["active", "inactive", "error"]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegistryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str
    source: Source
    status: Status
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
