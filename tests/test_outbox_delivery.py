"""Tests for retry-safe Telegram outbox delivery."""

from uuid import UUID

from inventory_agent.processing.delivery import TelegramOutboxDeliveryWorker
from inventory_agent.processing.models import (
    ClaimedProcessingOutcome,
    OutboxCompletionStatus,
    OutboxDeliveryStatus,
    ProcessingOutcomeType,
)
from inventory_agent.telegram.confirmation import ProposalConfirmationView

OUTBOX_ID = UUID("60000000-0000-0000-0000-000000000005")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000005")
ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000005")
LINE_ID = UUID("41000000-0000-0000-0000-000000000005")
VARIANT_ID = UUID("21000000-0000-0000-0000-000000000002")


class FakeRepository:
    def __init__(
        self,
        outcome: ClaimedProcessingOutcome | None,
        *,
        failure_completion: OutboxCompletionStatus = OutboxCompletionStatus.PENDING,
    ) -> None:
        self.outcome = outcome
        self.failure_completion = failure_completion
        self.finishes: list[tuple[UUID, bool, str | None, int]] = []
        self.requested_proposals: list[UUID] = []

    async def claim(self, outbox_id: UUID | None = None) -> ClaimedProcessingOutcome | None:
        return self.outcome

    async def finish(
        self,
        *,
        outbox_id: UUID,
        success: bool,
        error_message: str | None = None,
        retry_delay_seconds: int = 30,
    ) -> OutboxCompletionStatus | None:
        self.finishes.append((outbox_id, success, error_message, retry_delay_seconds))
        return OutboxCompletionStatus.SENT if success else self.failure_completion

    async def get_proposal_view(self, proposal_id: UUID) -> ProposalConfirmationView:
        self.requested_proposals.append(proposal_id)
        return ProposalConfirmationView(
            proposal_id=proposal_id,
            intent="receive_stock",
            lines=[
                {
                    "proposal_line_id": str(LINE_ID),
                    "description": "Full Cream Milk 1L",
                    "quantity": "3",
                    "unit": "each",
                    "matched_label": None,
                    "candidate_choices": [
                        {
                            "item_variant_id": str(VARIANT_ID),
                            "label": "Full Cream Milk 1L",
                        }
                    ],
                }
            ],
        )


class FakeSender:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.messages: list[tuple[int, str, list[list[dict[str, str]]] | None]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        self.messages.append((chat_id, text, inline_keyboard))
        if self.error is not None:
            raise self.error
        return 77


def outcome(
    outcome_type: ProcessingOutcomeType,
    *,
    payload: dict[str, object] | None = None,
) -> ClaimedProcessingOutcome:
    return ClaimedProcessingOutcome(
        outbox_id=OUTBOX_ID,
        organization_id=ORGANIZATION_ID,
        source_event_id=EVENT_ID,
        outcome_type=outcome_type,
        aggregate_id=PROPOSAL_ID if outcome_type is ProcessingOutcomeType.PROPOSAL_READY else None,
        chat_id=-100123,
        payload=payload or {},
        attempt_number=1,
    )


async def test_delivers_rendered_proposal_with_selection_keyboard() -> None:
    repository = FakeRepository(outcome(ProcessingOutcomeType.PROPOSAL_READY))
    sender = FakeSender()
    worker = TelegramOutboxDeliveryWorker(repository=repository, sender=sender)

    result = await worker.deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    assert result.telegram_message_id == 77
    assert repository.requested_proposals == [PROPOSAL_ID]
    assert "Review stock receipt" in sender.messages[0][1]
    assert sender.messages[0][2] is not None
    assert repository.finishes == [(OUTBOX_ID, True, None, 30)]


async def test_delivers_plain_clarification_message() -> None:
    repository = FakeRepository(
        outcome(
            ProcessingOutcomeType.CLARIFICATION_REQUIRED,
            payload={"message": "Which item?"},
        )
    )
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one(OUTBOX_ID)

    assert result.status is OutboxDeliveryStatus.SENT
    assert sender.messages == [(-100123, "Which item?", None)]


async def test_transient_failure_is_sanitized_and_scheduled_for_retry() -> None:
    repository = FakeRepository(
        outcome(
            ProcessingOutcomeType.CLARIFICATION_REQUIRED,
            payload={"message": "Which item?"},
        )
    )
    sender = FakeSender(RuntimeError("secret Telegram response"))

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
        retry_delay_seconds=10,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.RETRY_SCHEDULED
    assert repository.finishes == [(OUTBOX_ID, False, "RuntimeError: Telegram delivery failed", 10)]


async def test_fifth_failure_is_dead_lettered() -> None:
    repository = FakeRepository(
        outcome(
            ProcessingOutcomeType.CLARIFICATION_REQUIRED,
            payload={"message": "Which item?"},
        ),
        failure_completion=OutboxCompletionStatus.FAILED,
    )
    sender = FakeSender(RuntimeError("unavailable"))

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.DEAD_LETTERED


async def test_no_due_outcome_is_idle() -> None:
    repository = FakeRepository(None)
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.IDLE
    assert sender.messages == []
