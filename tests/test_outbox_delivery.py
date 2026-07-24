"""Tests for retry-safe Telegram outbox delivery."""

from uuid import UUID

from inventory_agent.catalog.models import CatalogItemCreationView
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
TRANSACTION_ID = UUID("60000000-0000-0000-0000-000000000006")
LINE_ID = UUID("41000000-0000-0000-0000-000000000005")
VARIANT_ID = UUID("21000000-0000-0000-0000-000000000002")
CATALOG_REQUEST_ID = UUID("71000000-0000-0000-0000-000000000005")


class FakeRepository:
    def __init__(
        self,
        outcome: ClaimedProcessingOutcome | None,
        *,
        failure_completion: OutboxCompletionStatus = OutboxCompletionStatus.PENDING,
        catalog_status: str = "awaiting_confirmation",
    ) -> None:
        self.outcome = outcome
        self.failure_completion = failure_completion
        self.catalog_status = catalog_status
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

    async def get_catalog_item_creation_view(self, request_id: UUID) -> CatalogItemCreationView:
        assert request_id == CATALOG_REQUEST_ID
        return CatalogItemCreationView(
            request_id=request_id,
            status=self.catalog_status,
            suggested_name="Purple Widget",
            suggested_sku="ZX-999",
            suggested_base_unit="each",
            suggested_tracking_mode="simple",
            name="Purple Widget" if self.catalog_status == "awaiting_confirmation" else None,
            sku="ZX-999" if self.catalog_status == "awaiting_confirmation" else None,
            base_unit="each" if self.catalog_status == "awaiting_confirmation" else None,
            tracking_mode=("simple" if self.catalog_status == "awaiting_confirmation" else None),
            attributes=(
                {"colour": "purple"} if self.catalog_status == "awaiting_confirmation" else {}
            ),
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
        aggregate_id={
            ProcessingOutcomeType.PROPOSAL_READY: PROPOSAL_ID,
            ProcessingOutcomeType.TRANSACTION_APPLIED: TRANSACTION_ID,
            ProcessingOutcomeType.CATALOG_ITEM_DETAILS_REQUIRED: CATALOG_REQUEST_ID,
            ProcessingOutcomeType.CATALOG_ITEM_CONFIRMATION: CATALOG_REQUEST_ID,
            ProcessingOutcomeType.REVERSAL_REASON_REQUIRED: PROPOSAL_ID,
            ProcessingOutcomeType.REVERSAL_CONFIRMATION: PROPOSAL_ID,
        }.get(outcome_type),
        chat_id=-100123,
        payload=payload or {},
        attempt_number=1,
    )


async def test_delivers_rendered_proposal_with_selection_keyboard() -> None:
    repository = FakeRepository(
        outcome(
            ProcessingOutcomeType.PROPOSAL_READY,
            payload={"agent_reply": "I found the exact catalog item."},
        )
    )
    sender = FakeSender()
    worker = TelegramOutboxDeliveryWorker(repository=repository, sender=sender)

    result = await worker.deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    assert result.telegram_message_id == 77
    assert repository.requested_proposals == [PROPOSAL_ID]
    assert sender.messages[0][1].startswith("⚠️ **Action needed**")
    assert "Review stock receipt" in sender.messages[0][1]
    assert "💬 **Agent note**\nI found the exact catalog item." in sender.messages[0][1]
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
    assert sender.messages == [(-100123, "❓ **More information needed**\nWhich item?", None)]


async def test_delivers_callback_notice_as_a_new_message() -> None:
    repository = FakeRepository(
        outcome(
            ProcessingOutcomeType.CALLBACK_NOTICE,
            payload={"message": "Proposal cancelled."},
        )
    )
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    assert sender.messages == [(-100123, "Proposal cancelled.", None)]


async def test_delivers_applied_transaction_with_reversal_button() -> None:
    repository = FakeRepository(outcome(ProcessingOutcomeType.TRANSACTION_APPLIED))
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    assert sender.messages[0][1].startswith("✅ **Inventory updated**")
    assert sender.messages[0][2] is not None


async def test_delivers_catalog_detail_prompt_and_confirmation_as_new_messages() -> None:
    for outcome_type, catalog_status, expected_text in [
        (
            ProcessingOutcomeType.CATALOG_ITEM_DETAILS_REQUIRED,
            "awaiting_details",
            "Reply naturally",
        ),
        (
            ProcessingOutcomeType.CATALOG_ITEM_CONFIRMATION,
            "awaiting_confirmation",
            "Create this catalog item?",
        ),
    ]:
        repository = FakeRepository(outcome(outcome_type), catalog_status=catalog_status)
        sender = FakeSender()

        result = await TelegramOutboxDeliveryWorker(
            repository=repository,
            sender=sender,
        ).deliver_one()

        assert result.status is OutboxDeliveryStatus.SENT
        assert expected_text in sender.messages[0][1]
        assert sender.messages[0][2] is not None


async def test_catalog_add_action_confirms_complete_agent_draft_without_reasking() -> None:
    repository = FakeRepository(
        outcome(ProcessingOutcomeType.CATALOG_ITEM_DETAILS_REQUIRED),
        catalog_status="awaiting_confirmation",
    )
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    assert "Create this catalog item?" in sender.messages[0][1]
    assert "colour" in sender.messages[0][1]


async def test_delivers_reversal_confirmation_with_reason_and_buttons() -> None:
    repository = FakeRepository(
        outcome(
            ProcessingOutcomeType.REVERSAL_CONFIRMATION,
            payload={
                "reason": "Wrong delivery was entered",
                "agent_reply": "I found the transaction to reverse.",
            },
        )
    )
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    assert sender.messages[0][1].startswith("⏳ **Pending reversal confirmation**")
    assert "💬 **Agent note**\nI found the transaction to reverse." in sender.messages[0][1]
    assert "Wrong delivery was entered" in sender.messages[0][1]
    assert sender.messages[0][2] is not None


async def test_delivers_reversal_reason_as_separate_message_with_cancel_button() -> None:
    repository = FakeRepository(outcome(ProcessingOutcomeType.REVERSAL_REASON_REQUIRED))
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    assert "Reply with the reason" in sender.messages[0][1]
    assert sender.messages[0][2] is not None


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
