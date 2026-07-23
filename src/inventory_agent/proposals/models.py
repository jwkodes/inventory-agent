"""Typed proposal drafts passed to the atomic database function."""

from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from inventory_agent.matching.models import CandidateMatchMethod


class ProposalIntent(StrEnum):
    RECEIVE_STOCK = "receive_stock"
    ISSUE_STOCK = "issue_stock"


class ProposalLineDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: int = Field(gt=0)
    source_text: str
    extracted_description: str | None = None
    requested_quantity: Decimal = Field(gt=0)
    requested_unit: str | None = None
    item_variant_id: UUID | None = None
    lot_id: UUID | None = None
    serial_id: UUID | None = None
    match_method: CandidateMatchMethod | None = None
    match_score: Decimal | None = Field(default=None, ge=0, le=1)
    match_evidence: dict[str, Any] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)


class ProposalDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    location_id: UUID
    source_event_id: UUID
    created_by: UUID
    intent: ProposalIntent
    idempotency_key: str
    raw_command: dict[str, Any]
    model_name: str | None = None
    model_response_id: str | None = None
    prompt_version: str | None = None
    notes: str | None = None
    lines: list[ProposalLineDraft] = Field(min_length=1)
