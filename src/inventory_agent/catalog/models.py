"""Typed catalog creation views and submitted details."""

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CatalogTrackingMode(StrEnum):
    SIMPLE = "simple"
    LOT = "lot"
    SERIAL = "serial"


class CatalogItemDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    sku: str = Field(min_length=1, max_length=100)
    base_unit: str = Field(min_length=1, max_length=50)
    tracking_mode: CatalogTrackingMode
    attributes: dict[str, Any] = Field(default_factory=dict)


class ExtractedCatalogAttribute(BaseModel):
    """One user-provided custom item field extracted from natural language."""

    model_config = ConfigDict(extra="forbid")

    key: str
    value: str


class ExtractedCatalogItemDetails(BaseModel):
    """Nullable Structured Output for a free-form catalog-details reply."""

    model_config = ConfigDict(extra="forbid")

    name: str | None
    sku: str | None
    base_unit: str | None
    tracking_mode: CatalogTrackingMode | None
    attributes: list[ExtractedCatalogAttribute]


class CatalogItemCreationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    status: str
    suggested_name: str | None = None
    suggested_sku: str | None = None
    suggested_base_unit: str
    suggested_tracking_mode: CatalogTrackingMode
    name: str | None = None
    sku: str | None = None
    base_unit: str | None = None
    tracking_mode: CatalogTrackingMode | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
