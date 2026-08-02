"""Typed contracts shared by the experimental agent and simulated tools."""

from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from inventory_agent.units import canonicalize_base_unit


class TrackingMode(StrEnum):
    SIMPLE = "simple"
    LOT = "lot"
    SERIAL = "serial"


class AttributeValue(BaseModel):
    """Portable custom field representation for strict tool schemas."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    value: str = Field(min_length=1)


class CatalogAttributeChange(BaseModel):
    """Set one catalog attribute, or remove it when value is null."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=100)
    value: str | None = Field(max_length=1000)


class CatalogVariant(BaseModel):
    """Compact inventory record returned to the model."""

    model_config = ConfigDict(extra="forbid")

    variant_id: str
    item_name: str
    variant_name: str | None = None
    sku: str | None = None
    base_unit: str = "each"
    tracking_mode: TrackingMode = TrackingMode.SIMPLE
    attributes: list[AttributeValue] = Field(default_factory=list)
    on_hand: Decimal = Decimal("0")


class InventoryReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None
    sku: str | None
    attributes: list[AttributeValue]
    include_zero_stock: bool
    limit: int = Field(ge=1, le=50)

    @field_validator("query", "sku")
    @classmethod
    def normalize_optional_search_term(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class CatalogItemEditArguments(BaseModel):
    """Safe catalog-only changes that always require an external confirmation."""

    model_config = ConfigDict(extra="forbid")

    variant_id: str
    item_name: str | None = Field(default=None, min_length=1, max_length=200)
    variant_name: str | None = Field(default=None, min_length=1, max_length=200)
    sku: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    clear_fields: list[Literal["variant_name", "description"]]
    item_attribute_changes: list[CatalogAttributeChange]
    variant_attribute_changes: list[CatalogAttributeChange]
    reason: str = Field(min_length=1, max_length=1000)


class NewCatalogItemDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    sku: str | None
    sku_deferred: bool = False
    base_unit: str = Field(min_length=1)
    tracking_mode: TrackingMode
    attributes: list[AttributeValue]

    @field_validator("base_unit")
    @classmethod
    def normalize_base_unit(cls, value: str) -> str:
        return canonicalize_base_unit(value)

    @model_validator(mode="after")
    def validate_sku_deferral(self) -> Self:
        if self.sku is not None and self.sku_deferred:
            raise ValueError("sku_deferred must be false when an SKU is supplied")
        return self


class StockProposalLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: str | None
    new_item: NewCatalogItemDraft | None
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1)
    attributes: list[AttributeValue]

    @model_validator(mode="after")
    def require_exactly_one_item_source(self) -> Self:
        if (self.variant_id is None) == (self.new_item is None):
            raise ValueError("provide exactly one of variant_id or new_item")
        return self


class StockProposalArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[StockProposalLine] = Field(min_length=1)
    reason: str = Field(min_length=1)


class TransactionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: str
    transaction_type: str
    status: str
    occurred_at: str
    summary: str
    reversed: bool = False


class TransactionReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None
    limit: int = Field(ge=1, le=20)


class ReversalProposalArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_ref: str = Field(pattern=r"^T[1-9][0-9]*$")
    reason: str = Field(min_length=1)
    replacement: "CorrectionReplacementArguments | None"


class CorrectionReplacementArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["ADD", "DEDUCT"]
    lines: list[StockProposalLine] = Field(min_length=1)
    reason: str = Field(min_length=1)


class SimulationProposal(BaseModel):
    """A recorded proposal that deliberately has no commit operation."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    operation: str
    payload: dict[str, object]
    status: str = "awaiting_confirmation"
