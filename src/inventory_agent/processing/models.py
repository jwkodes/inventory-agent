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


class TelegramCallbackEventContext(BaseModel):
    """Actor and source-message data resolved when a callback event is claimed."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    organization_id: UUID
    organization_user_id: UUID
    external_event_id: str
    callback_query_id: str
    callback_data: str
    chat_id: int
    telegram_message_id: int
    telegram_user_id: int


class ProcessingOutcomeType(StrEnum):
    """Durable handoffs understood by a later outbound-delivery worker."""

    PROPOSAL_READY = "proposal_ready"
    REVERSAL_CONFIRMATION = "reversal_confirmation"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNSUPPORTED_COMMAND = "unsupported_command"


class TextEventProcessingStatus(StrEnum):
    """Result returned to a worker invocation."""

    ALREADY_CLAIMED = "already_claimed"
    PROPOSAL_READY = "proposal_ready"
    REVERSAL_CONFIRMATION = "reversal_confirmation"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNSUPPORTED_COMMAND = "unsupported_command"


class TextEventProcessingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    status: TextEventProcessingStatus
    chat_id: int | None = None
    proposal_id: UUID | None = None
    reversal_request_id: UUID | None = None
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


class ClaimedProcessingOutcome(BaseModel):
    """One outbound outcome held by a delivery worker lease."""

    model_config = ConfigDict(extra="forbid")

    outbox_id: UUID
    organization_id: UUID
    source_event_id: UUID
    outcome_type: ProcessingOutcomeType
    aggregate_id: UUID | None = None
    chat_id: int
    payload: dict[str, Any]
    attempt_number: int


class OutboxCompletionStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class OutboxDeliveryStatus(StrEnum):
    IDLE = "idle"
    SENT = "sent"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"


class OutboxDeliveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OutboxDeliveryStatus
    outbox_id: UUID | None = None
    telegram_message_id: int | None = None
