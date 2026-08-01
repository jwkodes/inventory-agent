"""Tests for retry-safe Telegram outbox delivery."""

from datetime import UTC, datetime
from uuid import UUID

from inventory_agent.catalog.models import CatalogBatchCreationView, CatalogItemCreationView
from inventory_agent.processing.delivery import TelegramOutboxDeliveryWorker
from inventory_agent.processing.models import (
    ClaimedProcessingOutcome,
    OutboxCompletionStatus,
    OutboxDeliveryStatus,
    ProcessingOutcomeType,
)
from inventory_agent.processing.repository import AppliedTransactionRecord
from inventory_agent.telegram.confirmation import ProposalConfirmationView

OUTBOX_ID = UUID("60000000-0000-0000-0000-000000000005")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000005")
ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000005")
TRANSACTION_ID = UUID("60000000-0000-0000-0000-000000000006")
LINE_ID = UUID("41000000-0000-0000-0000-000000000005")
VARIANT_ID = UUID("21000000-0000-0000-0000-000000000002")
CATALOG_REQUEST_ID = UUID("71000000-0000-0000-0000-000000000005")
CATALOG_BATCH_ID = UUID("72000000-0000-0000-0000-000000000005")
REVERSAL_REQUEST_ID = UUID("70000000-0000-0000-0000-000000000005")
APPLIED_AT = datetime(2026, 7, 24, 11, 42, 19, tzinfo=UTC)


class FakeRepository:
    def __init__(
        self,
        outcome: ClaimedProcessingOutcome | None,
        *,
        failure_completion: OutboxCompletionStatus = OutboxCompletionStatus.PENDING,
        catalog_status: str = "awaiting_confirmation",
        transaction_type: str = "receive",
    ) -> None:
        self.outcome = outcome
        self.failure_completion = failure_completion
        self.catalog_status = catalog_status
        self.transaction_type = transaction_type
        self.finishes: list[tuple[UUID, bool, str | None, int]] = []
        self.requested_proposals: list[UUID] = []
        self.requested_transactions: list[tuple[UUID, UUID]] = []
        self.requested_reversals: list[tuple[UUID, UUID]] = []

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

    async def get_catalog_batch_creation_view(self, batch_id: UUID) -> CatalogBatchCreationView:
        assert batch_id == CATALOG_BATCH_ID
        return CatalogBatchCreationView(
            batch_id=batch_id,
            proposal_id=PROPOSAL_ID,
            status=self.catalog_status,
            items=[
                {
                    "request_id": str(CATALOG_REQUEST_ID),
                    "line_number": 1,
                    "requested_quantity": "4",
                    "requested_unit": "PCS",
                    "suggested_name": "Purple Widget",
                    "suggested_sku": None,
                    "suggested_base_unit": "each",
                    "suggested_tracking_mode": "simple",
                    "name": "Purple Widget",
                    "sku": ("ZX-999" if self.catalog_status == "awaiting_confirmation" else None),
                    "base_unit": "each",
                    "tracking_mode": "simple",
                    "attributes": {},
                }
            ],
        )

    async def get_applied_transaction(
        self,
        *,
        organization_id: UUID,
        transaction_id: UUID,
    ) -> AppliedTransactionRecord:
        self.requested_transactions.append((organization_id, transaction_id))
        return AppliedTransactionRecord(
            transaction_id=transaction_id,
            transaction_type=self.transaction_type,
            applied_at=APPLIED_AT,
        )

    async def get_reversal_original_transaction(
        self,
        *,
        organization_id: UUID,
        request_id: UUID,
    ) -> AppliedTransactionRecord:
        self.requested_reversals.append((organization_id, request_id))
        return AppliedTransactionRecord(
            transaction_id=TRANSACTION_ID,
            transaction_type="issue",
            applied_at=APPLIED_AT,
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
            ProcessingOutcomeType.CATALOG_BATCH_DETAILS_REQUIRED: CATALOG_BATCH_ID,
            ProcessingOutcomeType.CATALOG_BATCH_CONFIRMATION: CATALOG_BATCH_ID,
            ProcessingOutcomeType.REVERSAL_REASON_REQUIRED: PROPOSAL_ID,
            ProcessingOutcomeType.REVERSAL_CONFIRMATION: REVERSAL_REQUEST_ID,
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
    assert sender.messages[0][1].startswith("🔎 **Choose a catalog match**")
    assert "Review stock addition" in sender.messages[0][1]
    assert "➕ ADD 3" in sender.messages[0][1]
    assert "💬 **Agent note**\nI found the exact catalog item." in sender.messages[0][1]
    assert sender.messages[0][2] is not None
    assert repository.finishes == [(OUTBOX_ID, True, None, 30)]


async def test_pending_proposal_replaces_premature_model_success_claim() -> None:
    repository = FakeRepository(
        outcome(
            ProcessingOutcomeType.PROPOSAL_READY,
            payload={"agent_reply": "Done! Inventory has been updated successfully."},
        )
    )
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    text = sender.messages[0][1]
    assert "Inventory has been updated successfully" not in text
    assert "prepared for review and has not been applied" in text


async def test_pending_proposal_removes_unbalanced_model_markdown() -> None:
    repository = FakeRepository(
        outcome(
            ProcessingOutcomeType.PROPOSAL_READY,
            payload={"agent_reply": "**Proposal prepared with malformed emphasis."},
        )
    )
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    text = sender.messages[0][1]
    assert text.count("**") % 2 == 0
    assert "Proposal prepared with malformed emphasis." in text


async def test_reversal_success_and_linked_replacement_are_delivered_together() -> None:
    repository = FakeRepository(
        outcome(
            ProcessingOutcomeType.PROPOSAL_READY,
            payload={"reversal_transaction_id": str(TRANSACTION_ID)},
        ),
        transaction_type="reversal",
    )
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    text = sender.messages[0][1]
    assert text.startswith("✅ **Transaction reversed**")
    assert f"Reversal transaction ID: `{TRANSACTION_ID}`" in text
    assert "Review stock addition:" in text
    assert "These are the best available matches" in text
    assert "choose one only if it is correct" in text
    assert repository.requested_transactions == [(ORGANIZATION_ID, TRANSACTION_ID)]
    assert sender.messages[0][2] is not None


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
    assert sender.messages == [
        (-100123, "❓ **Reply with the missing information**\nWhich item?", None)
    ]


async def test_delivers_one_bulk_catalog_prompt_with_retained_quantity() -> None:
    repository = FakeRepository(
        outcome(ProcessingOutcomeType.CATALOG_BATCH_DETAILS_REQUIRED),
        catalog_status="awaiting_details",
    )
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    assert "1. 4 PCS — Purple Widget — SKU needed" in sender.messages[0][1]
    assert "Quantities will not be changed" in sender.messages[0][1]


async def test_bulk_catalog_confirmation_reviews_creation_and_stock_together() -> None:
    repository = FakeRepository(
        outcome(ProcessingOutcomeType.CATALOG_BATCH_CONFIRMATION),
        catalog_status="awaiting_confirmation",
    )
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    text = sender.messages[0][1]
    assert text.startswith("📋 **Review and confirm new catalog products + stock addition**")
    assert "🆕 CREATE + ADD 3 each — Purple Widget · ZX-999" in text
    assert "Confirm once" in text
    assert repository.requested_proposals == [PROPOSAL_ID]


async def test_simulated_user_label_is_visible_on_every_outbound_result() -> None:
    repository = FakeRepository(
        outcome(
            ProcessingOutcomeType.AGENT_MESSAGE,
            payload={
                "message": "You have 10 switches.",
                "_dev_simulation": {"alias": "bob", "display_name": "Bob"},
            },
        )
    )
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    assert sender.messages == [(-100123, "🧪 **Simulating Bob**\n\nYou have 10 switches.", None)]


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
    assert sender.messages[0][1].startswith("✅ **Stock added**")
    assert f"Transaction ID: `{TRANSACTION_ID}`" in sender.messages[0][1]
    assert "24 Jul 2026, 07:42:19 PM (Asia/Singapore)" in sender.messages[0][1]
    assert repository.requested_transactions == [(ORGANIZATION_ID, TRANSACTION_ID)]
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
            "Please review, then choose **Create item** or **Cancel**.",
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
    assert "Please review, then choose **Create item** or **Cancel**." in sender.messages[0][1]
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
    assert sender.messages[0][1].startswith("📋 **Review and confirm transaction reversal**")
    assert f"Original transaction ID: `{TRANSACTION_ID}`" in sender.messages[0][1]
    assert "💬 **Agent note**\nI found the transaction to reverse." in sender.messages[0][1]
    assert "Wrong delivery was entered" in sender.messages[0][1]
    assert "Original transaction time: 24 Jul 2026, 07:42:19 PM" in sender.messages[0][1]
    assert repository.requested_reversals == [(ORGANIZATION_ID, REVERSAL_REQUEST_ID)]
    assert sender.messages[0][2] is not None


async def test_delivers_successful_reversal_with_timestamp_and_state_boundary() -> None:
    repository = FakeRepository(
        outcome(
            ProcessingOutcomeType.CALLBACK_NOTICE,
            payload={
                "message": "legacy fallback",
                "transaction_id": str(TRANSACTION_ID),
            },
        ),
        transaction_type="reversal",
    )
    sender = FakeSender()

    result = await TelegramOutboxDeliveryWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    assert sender.messages[0][1].startswith("✅ **Transaction reversed**")
    assert f"Reversal transaction ID: `{TRANSACTION_ID}`" in sender.messages[0][1]
    assert "24 Jul 2026, 07:42:19 PM (Asia/Singapore)" in sender.messages[0][1]
    assert "The original stock movement was reversed." in sender.messages[0][1]
    assert repository.requested_transactions == [(ORGANIZATION_ID, TRANSACTION_ID)]
    assert sender.messages[0][2] is None


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
