"""Versioned model boundary for unstructured inventory commands."""

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class InventoryIntent(StrEnum):
    """Operations the interpreter may recognize before matching or authorization."""

    RECEIVE_STOCK = "RECEIVE_STOCK"
    ISSUE_STOCK = "ISSUE_STOCK"
    ADJUST_STOCK = "ADJUST_STOCK"
    QUERY_INVENTORY = "QUERY_INVENTORY"
    UNKNOWN = "UNKNOWN"


class ItemReferenceType(StrEnum):
    """Kind of source identifier stated by the user."""

    SKU = "SKU"
    BARCODE = "BARCODE"
    PART_NUMBER = "PART_NUMBER"
    NAME = "NAME"
    UNKNOWN = "UNKNOWN"


class ExtractedAttribute(BaseModel):
    """Company-defined field hint such as colour, batch, or expiry date."""

    model_config = ConfigDict(extra="forbid")

    key: str
    value: str


class ExtractedItemReference(BaseModel):
    """Reference copied from user input, never a database identifier."""

    model_config = ConfigDict(extra="forbid")

    type: ItemReferenceType
    value: str | None

    @model_validator(mode="after")
    def validate_reference_value(self) -> Self:
        if self.type is ItemReferenceType.UNKNOWN and self.value is not None:
            raise ValueError("unknown item references cannot have a value")
        if self.type is not ItemReferenceType.UNKNOWN and not self.value:
            raise ValueError("known item references require a value")
        return self


class ExtractedCommandLine(BaseModel):
    """One item mention before catalog matching and unit conversion."""

    model_config = ConfigDict(extra="forbid")

    source_text: str
    item_reference: ExtractedItemReference
    description: str | None
    quantity: str | None
    unit: str | None
    attributes: list[ExtractedAttribute]

    @field_validator("quantity")
    @classmethod
    def quantity_must_be_positive_decimal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            quantity = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("quantity must be a decimal string") from error
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("quantity must be greater than zero")
        return value


class ExtractedInventoryCommand(BaseModel):
    """Strict output produced by the language model."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    intent: InventoryIntent
    location_hint: str | None
    lines: list[ExtractedCommandLine]
    notes: str | None
    needs_clarification: bool
    clarification_question: str | None

    @model_validator(mode="after")
    def validate_command_state(self) -> Self:
        mutation_intents = {
            InventoryIntent.RECEIVE_STOCK,
            InventoryIntent.ISSUE_STOCK,
            InventoryIntent.ADJUST_STOCK,
        }
        if self.intent is InventoryIntent.UNKNOWN and not self.needs_clarification:
            raise ValueError("unknown intent requires clarification")
        if self.needs_clarification and not self.clarification_question:
            raise ValueError("clarification question is required")
        if not self.needs_clarification and self.clarification_question is not None:
            raise ValueError("clarification question must be null when clarification is not needed")
        if self.intent in mutation_intents:
            if not self.lines:
                raise ValueError("stock mutations require at least one line")
            if any(line.quantity is None for line in self.lines):
                raise ValueError("every stock mutation line requires a quantity")
        return self
