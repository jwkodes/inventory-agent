"""Typed catalog creation views and submitted details."""

from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from inventory_agent.units import canonicalize_base_unit


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

    @field_validator("base_unit")
    @classmethod
    def normalize_base_unit(cls, value: str) -> str:
        return canonicalize_base_unit(value)


class ExtractedCatalogAttribute(BaseModel):
    """One user-provided custom item field extracted from natural language."""

    model_config = ConfigDict(extra="forbid")

    key: str
    value: str


class ExtractedCatalogItemDetails(BaseModel):
    """Nullable Structured Output for a free-form catalog-details reply."""

    model_config = ConfigDict(extra="forbid")

    applies_to_pending_request: bool
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
    details_reason: str | None = None
    line_number: int | None = None
    requested_quantity: Decimal | None = None
    requested_unit: str | None = None


class CatalogPreviewCreationResult(BaseModel):
    """Outcome from creating an agent-proposed catalog item in one transaction."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "awaiting_details"]
    result_id: UUID
    message: str | None = None


class CatalogBatchItemView(BaseModel):
    """One unmatched proposal line inside a bulk catalog-creation request."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    line_number: int = Field(ge=1)
    requested_quantity: Decimal
    requested_unit: str | None = None
    suggested_name: str | None = None
    suggested_sku: str | None = None
    suggested_base_unit: str
    suggested_tracking_mode: CatalogTrackingMode
    name: str | None = None
    sku: str | None = None
    base_unit: str | None = None
    tracking_mode: CatalogTrackingMode | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    details_reason: str | None = None


class CatalogBatchCreationView(BaseModel):
    """All new catalog items being reviewed for one inventory proposal."""

    model_config = ConfigDict(extra="forbid")

    batch_id: UUID
    proposal_id: UUID
    status: str
    items: list[CatalogBatchItemView]


class ExtractedCatalogBatchItemDetails(BaseModel):
    """Natural-language catalog facts for one numbered batch line."""

    model_config = ConfigDict(extra="forbid")

    line_number: int = Field(ge=1)
    name: str | None
    sku: str | None
    base_unit: str | None
    tracking_mode: CatalogTrackingMode | None
    attributes: list[ExtractedCatalogAttribute]


class ExtractedCatalogBatchDetails(BaseModel):
    """Structured result for one reply covering a catalog batch."""

    model_config = ConfigDict(extra="forbid")

    applies_to_pending_request: bool
    items: list[ExtractedCatalogBatchItemDetails]


class CatalogBatchItemDraft(BaseModel):
    """Merged, possibly incomplete details saved for one batch item."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    name: str | None = None
    sku: str | None = None
    base_unit: str | None = None
    tracking_mode: CatalogTrackingMode | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
