"""Typed boundaries for source-event processing and durable outcomes."""

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TelegramTextEventContext(BaseModel):
    """Tenant and message data resolved atomically when an event is claimed."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    organization_id: UUID
    organization_user_id: UUID
    location_id: UUID
    external_event_id: str
    chat_id: int
    telegram_user_id: int
    message_text: str


class ProcessingOutcomeType(StrEnum):
    """Durable handoffs understood by a later outbound-delivery worker."""

    PROPOSAL_READY = "proposal_ready"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNSUPPORTED_COMMAND = "unsupported_command"


class TextEventProcessingStatus(StrEnum):
    """Result returned to a worker invocation."""

    ALREADY_CLAIMED = "already_claimed"
    PROPOSAL_READY = "proposal_ready"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNSUPPORTED_COMMAND = "unsupported_command"


class TextEventProcessingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    status: TextEventProcessingStatus
    chat_id: int | None = None
    proposal_id: UUID | None = None
    outbox_id: UUID | None = None


class ProcessingOutcomeDraft(BaseModel):
    """One idempotent outbox record created after interpretation."""

    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    source_event_id: UUID
    outcome_type: ProcessingOutcomeType
    aggregate_id: UUID | None = None
    chat_id: int
    payload: dict[str, Any]
