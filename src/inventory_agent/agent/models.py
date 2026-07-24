"""Typed contracts shared by the experimental agent and simulated tools."""

from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TrackingMode(StrEnum):
    SIMPLE = "simple"
    LOT = "lot"
    SERIAL = "serial"


class AttributeValue(BaseModel):
    """Portable custom field representation for strict tool schemas."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    value: str = Field(min_length=1)


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


class NewCatalogItemDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    sku: str | None
    base_unit: str = Field(min_length=1)
    tracking_mode: TrackingMode
    attributes: list[AttributeValue]


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
    occurred_at: str
    summary: str
    reversed: bool = False


class TransactionReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None
    limit: int = Field(ge=1, le=20)


class ReversalProposalArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1)
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
