"""Tests for durable Telegram callback-event processing."""

from uuid import UUID

import pytest

from inventory_agent.processing.callback_events import (
    CallbackEventProcessingError,
    TelegramCallbackEventProcessor,
)
from inventory_agent.processing.models import TelegramCallbackEventContext
from inventory_agent.telegram.callback_dispatcher import (
    CallbackOutcome,
    CallbackOutcomeStatus,
)
from inventory_agent.telegram.callbacks import CallbackAction
from inventory_agent.telegram.confirmation import ProposalConfirmationView

EVENT_ID = UUID("50000000-0000-0000-0000-000000000010")
ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000010")
TRANSACTION_ID = UUID("60000000-0000-0000-0000-000000000010")
LINE_ID = UUID("41000000-0000-0000-0000-000000000010")


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
    ) -> CallbackOutcome:
        assert callback_query_id == "callback-query-10"
        assert callback_data == "opaque-callback-data"
        assert actor_id == ACTOR_ID
        return self.outcome


class FakeProposalViews:
    def __init__(self) -> None:
        self.requested: list[UUID] = []

    async def get_proposal_view(self, proposal_id: UUID) -> ProposalConfirmationView:
        self.requested.append(proposal_id)
        return ProposalConfirmationView(
            proposal_id=proposal_id,
            intent="receive_stock",
            lines=[
                {
                    "proposal_line_id": str(LINE_ID),
                    "description": "Full Cream Milk 1L",
                    "quantity": "3",
                    "unit": "each",
                    "matched_label": "Full Cream Milk 1L",
                    "candidate_choices": [],
                }
            ],
        )


class RecordingEditor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.edits: list[tuple[int, int, str, list[list[dict[str, str]]] | None]] = []

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        self.edits.append((chat_id, message_id, text, inline_keyboard))
        if self.error is not None:
            raise self.error


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
    views = FakeProposalViews()
    editor = RecordingEditor()
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
        proposal_views=views,
        message_editor=editor,
    )

    result = await processor.process_next()

    assert result is not None
    assert views.requested == [PROPOSAL_ID]
    assert "Review stock receipt" in editor.edits[0][2]
    assert editor.edits[0][3] is not None
    assert events.finishes == [(EVENT_ID, True, None)]


async def test_confirmation_replaces_buttons_with_success_message() -> None:
    events = FakeEvents(context())
    editor = RecordingEditor()
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
        proposal_views=FakeProposalViews(),
        message_editor=editor,
    )

    await processor.process_next()

    assert editor.edits == [(-100123, 77, "Inventory updated.", None)]
    assert events.finishes == [(EVENT_ID, True, None)]


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
        proposal_views=FakeProposalViews(),
        message_editor=editor,
    )

    await processor.process_next()

    assert editor.edits == []
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
        proposal_views=FakeProposalViews(),
        message_editor=RecordingEditor(RuntimeError("secret Telegram response")),
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
        proposal_views=FakeProposalViews(),
        message_editor=RecordingEditor(),
    )

    assert await processor.process_next() is None
