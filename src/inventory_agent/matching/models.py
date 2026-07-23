"""Typed candidate and decision models for inventory matching."""

from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TrackingMode(StrEnum):
    SIMPLE = "simple"
    LOT = "lot"
    SERIAL = "serial"


class CandidateMatchMethod(StrEnum):
    EXACT_IDENTIFIER = "exact_identifier"
    CONFIRMED_ALIAS = "confirmed_alias"
    TEXT_SEARCH = "text_search"
    SEMANTIC_RERANK = "semantic_rerank"
    HUMAN_SELECTED = "human_selected"


class InventoryCandidate(BaseModel):
    """One organization-scoped item variant returned by candidate retrieval."""

    model_config = ConfigDict(extra="forbid")

    item_variant_id: UUID
    item_id: UUID
    item_name: str
    variant_name: str | None
    sku: str
    base_unit: str
    tracking_mode: TrackingMode
    match_method: CandidateMatchMethod
    match_score: Decimal = Field(ge=0, le=1)
    match_evidence: dict[str, Any]

    @property
    def display_name(self) -> str:
        return self.variant_name or self.item_name


class MatchDecisionStatus(StrEnum):
    MATCHED = "matched"
    NEEDS_CONFIRMATION = "needs_confirmation"
    NOT_FOUND = "not_found"


class MatchDecision(BaseModel):
    """Policy outcome used by the proposal and Telegram confirmation layers."""

    model_config = ConfigDict(extra="forbid")

    status: MatchDecisionStatus
    selected: InventoryCandidate | None
    candidates: list[InventoryCandidate]
    reason: str
