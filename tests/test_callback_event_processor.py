"""Tests for durable Telegram callback-event processing."""

from uuid import UUID

import pytest

from inventory_agent.processing.callback_events import (
    CallbackEventProcessingError,
    TelegramCallbackEventProcessor,
)
from inventory_agent.processing.models import (
    ProcessingOutcomeDraft,
    TelegramCallbackEventContext,
)
from inventory_agent.telegram.callback_dispatcher import (
    CallbackOutcome,
    CallbackOutcomeStatus,
)
from inventory_agent.telegram.callbacks import CallbackAction

EVENT_ID = UUID("50000000-0000-0000-0000-000000000010")
ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000010")
TRANSACTION_ID = UUID("60000000-0000-0000-0000-000000000010")
OUTBOX_ID = UUID("60000000-0000-0000-0000-000000000011")


class FakeEvents:
    def __init__(self, context: TelegramCallbackEventContext | None) -> None:
        self.context = context
        self.finishes: list[tuple[UUID, bool, str | None]] = []

    async def claim_next_callback_event(self) -> TelegramCallbackEventContext | None:
        return self.context

    async def finish_event(
        self,
        *,
        event_id: UUID,
        success: bool,
        error_message: str | None = None,
    ) -> bool:
        self.finishes.append((event_id, success, error_message))
        return True


class FakeDispatcher:
    def __init__(self, outcome: CallbackOutcome) -> None:
        self.outcome = outcome

    async def dispatch(
        self,
        *,
        callback_query_id: str,
        callback_data: str,
        actor_id: UUID,
        chat_id: int,
    ) -> CallbackOutcome:
        assert callback_query_id == "callback-query-10"
        assert callback_data == "opaque-callback-data"
        assert actor_id == ACTOR_ID
        assert chat_id == -100123
        return self.outcome


class RecordingEditor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.removed_keyboards: list[tuple[int, int]] = []

    async def remove_inline_keyboard(self, *, chat_id: int, message_id: int) -> None:
        self.removed_keyboards.append((chat_id, message_id))
        if self.error is not None:
            raise self.error


class RecordingOutbox:
    def __init__(self) -> None:
        self.drafts: list[ProcessingOutcomeDraft] = []

    async def enqueue(self, draft: ProcessingOutcomeDraft) -> UUID:
        self.drafts.append(draft)
        return OUTBOX_ID


def context() -> TelegramCallbackEventContext:
    return TelegramCallbackEventContext(
        event_id=EVENT_ID,
        organization_id=ORGANIZATION_ID,
        organization_user_id=ACTOR_ID,
        external_event_id="9100",
        callback_query_id="callback-query-10",
        callback_data="opaque-callback-data",
        chat_id=-100123,
        telegram_message_id=77,
        telegram_user_id=100000001,
    )


async def test_variant_selection_refreshes_proposal_with_confirmation_buttons() -> None:
    events = FakeEvents(context())
    editor = RecordingEditor()
    outbox = RecordingOutbox()
    processor = TelegramCallbackEventProcessor(
        events=events,
        dispatcher=FakeDispatcher(
            CallbackOutcome(
                CallbackOutcomeStatus.COMPLETED,
                CallbackAction.SELECT_VARIANT,
                PROPOSAL_ID,
                "Item selected",
            )
        ),
        message_editor=editor,
        outbox=outbox,
    )

    result = await processor.process_next()

    assert result is not None
    assert editor.removed_keyboards == [(-100123, 77)]
    assert outbox.drafts[0].outcome_type.value == "proposal_ready"
    assert outbox.drafts[0].aggregate_id == PROPOSAL_ID
    assert events.finishes == [(EVENT_ID, True, None)]


async def test_confirmation_offers_a_reversal_button() -> None:
    events = FakeEvents(context())
    editor = RecordingEditor()
    outbox = RecordingOutbox()
    processor = TelegramCallbackEventProcessor(
        events=events,
        dispatcher=FakeDispatcher(
            CallbackOutcome(
                CallbackOutcomeStatus.COMPLETED,
                CallbackAction.CONFIRM_PROPOSAL,
                TRANSACTION_ID,
                "Inventory updated",
            )
        ),
        message_editor=editor,
        outbox=outbox,
    )

    await processor.process_next()

    assert editor.removed_keyboards == [(-100123, 77)]
    assert outbox.drafts[0].outcome_type.value == "transaction_applied"
    assert outbox.drafts[0].aggregate_id == TRANSACTION_ID
    assert events.finishes == [(EVENT_ID, True, None)]


async def test_cancelled_proposal_sends_a_new_notice() -> None:
    outbox = RecordingOutbox()
    processor = TelegramCallbackEventProcessor(
        events=FakeEvents(context()),
        dispatcher=FakeDispatcher(
            CallbackOutcome(
                CallbackOutcomeStatus.COMPLETED,
                CallbackAction.CANCEL_PROPOSAL,
                PROPOSAL_ID,
                "Proposal cancelled",
            )
        ),
        message_editor=RecordingEditor(),
        outbox=outbox,
    )

    await processor.process_next()

    assert outbox.drafts[0].outcome_type.value == "callback_notice"
    assert outbox.drafts[0].aggregate_id is None
    assert outbox.drafts[0].payload == {
        "message": "🚫 **Proposal cancelled**\nNo inventory changes were applied."
    }


async def test_reversal_request_prompts_for_reason_with_cancel_button() -> None:
    events = FakeEvents(context())
    editor = RecordingEditor()
    outbox = RecordingOutbox()
    processor = TelegramCallbackEventProcessor(
        events=events,
        dispatcher=FakeDispatcher(
            CallbackOutcome(
                CallbackOutcomeStatus.COMPLETED,
                CallbackAction.REVERSE_TRANSACTION,
                PROPOSAL_ID,
                "Reversal reason required",
            )
        ),
        message_editor=editor,
        outbox=outbox,
    )

    await processor.process_next()

    assert editor.removed_keyboards == [(-100123, 77)]
    assert len(outbox.drafts) == 1
    draft = outbox.drafts[0]
    assert draft.source_event_id == EVENT_ID
    assert draft.aggregate_id == PROPOSAL_ID
    assert draft.outcome_type.value == "reversal_reason_required"


async def test_confirmed_reversal_removes_buttons() -> None:
    editor = RecordingEditor()
    outbox = RecordingOutbox()
    processor = TelegramCallbackEventProcessor(
        events=FakeEvents(context()),
        dispatcher=FakeDispatcher(
            CallbackOutcome(
                CallbackOutcomeStatus.COMPLETED,
                CallbackAction.CONFIRM_REVERSAL,
                TRANSACTION_ID,
                "Transaction reversed",
            )
        ),
        message_editor=editor,
        outbox=outbox,
    )

    await processor.process_next()

    assert editor.removed_keyboards == [(-100123, 77)]
    assert outbox.drafts[0].outcome_type.value == "callback_notice"
    assert outbox.drafts[0].payload == {
        "message": (
            "✅ **Transaction reversed**\n"
            "The opposite inventory transaction was applied successfully."
        )
    }


async def test_cancelled_reversal_sends_a_new_notice() -> None:
    outbox = RecordingOutbox()
    processor = TelegramCallbackEventProcessor(
        events=FakeEvents(context()),
        dispatcher=FakeDispatcher(
            CallbackOutcome(
                CallbackOutcomeStatus.COMPLETED,
                CallbackAction.CANCEL_REVERSAL,
                PROPOSAL_ID,
                "Reversal cancelled",
            )
        ),
        message_editor=RecordingEditor(),
        outbox=outbox,
    )

    await processor.process_next()

    assert outbox.drafts[0].outcome_type.value == "callback_notice"
    assert outbox.drafts[0].payload == {
        "message": "🚫 **Reversal cancelled**\nNo inventory changes were applied."
    }


@pytest.mark.parametrize(
    ("action", "result_id", "catalog_status", "outcome_type"),
    [
        (
            CallbackAction.ADD_NEW_ITEM,
            PROPOSAL_ID,
            "awaiting_details",
            "catalog_item_details_required",
        ),
        (
            CallbackAction.ADD_NEW_ITEM,
            PROPOSAL_ID,
            "awaiting_confirmation",
            "catalog_item_confirmation",
        ),
        (CallbackAction.SHOW_EXISTING_ITEMS, PROPOSAL_ID, None, "proposal_ready"),
        (CallbackAction.CONFIRM_NEW_ITEM, PROPOSAL_ID, None, "proposal_ready"),
        (CallbackAction.CANCEL_NEW_ITEM, PROPOSAL_ID, None, "callback_notice"),
    ],
)
async def test_catalog_actions_send_new_outbox_messages(
    action: CallbackAction,
    result_id: UUID,
    catalog_status: str | None,
    outcome_type: str,
) -> None:
    outbox = RecordingOutbox()
    processor = TelegramCallbackEventProcessor(
        events=FakeEvents(context()),
        dispatcher=FakeDispatcher(
            CallbackOutcome(
                CallbackOutcomeStatus.COMPLETED,
                action,
                result_id,
                "Catalog action completed",
                catalog_status,
            )
        ),
        message_editor=RecordingEditor(),
        outbox=outbox,
    )

    await processor.process_next()

    assert outbox.drafts[0].outcome_type.value == outcome_type


async def test_invalid_callback_is_completed_without_editing_message() -> None:
    events = FakeEvents(context())
    editor = RecordingEditor()
    processor = TelegramCallbackEventProcessor(
        events=events,
        dispatcher=FakeDispatcher(
            CallbackOutcome(
                CallbackOutcomeStatus.INVALID,
                None,
                None,
                "Malformed callback data",
            )
        ),
        message_editor=editor,
        outbox=RecordingOutbox(),
    )

    await processor.process_next()

    assert editor.removed_keyboards == []
    assert events.finishes == [(EVENT_ID, True, None)]


async def test_edit_failure_is_sanitized_and_returned_to_event_retry_queue() -> None:
    events = FakeEvents(context())
    processor = TelegramCallbackEventProcessor(
        events=events,
        dispatcher=FakeDispatcher(
            CallbackOutcome(
                CallbackOutcomeStatus.COMPLETED,
                CallbackAction.CONFIRM_PROPOSAL,
                TRANSACTION_ID,
                "Inventory updated",
            )
        ),
        message_editor=RecordingEditor(RuntimeError("secret Telegram response")),
        outbox=RecordingOutbox(),
    )

    with pytest.raises(CallbackEventProcessingError, match="processing failed"):
        await processor.process_next()

    assert events.finishes == [(EVENT_ID, False, "RuntimeError: callback event processing failed")]


async def test_no_callback_event_is_idle() -> None:
    processor = TelegramCallbackEventProcessor(
        events=FakeEvents(None),
        dispatcher=FakeDispatcher(
            CallbackOutcome(CallbackOutcomeStatus.INVALID, None, None, "unused")
        ),
        message_editor=RecordingEditor(),
        outbox=RecordingOutbox(),
    )

    assert await processor.process_next() is None
