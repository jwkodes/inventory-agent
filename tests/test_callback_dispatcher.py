"""Tests for acknowledgement-first callback dispatch."""

from uuid import UUID

from inventory_agent.telegram.callback_dispatcher import (
    CallbackOutcomeStatus,
    TelegramCallbackDispatcher,
)
from inventory_agent.telegram.callbacks import CallbackAction, CallbackCommand, encode_callback

ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000001")
LINE_ID = UUID("41000000-0000-0000-0000-000000000001")
VARIANT_ID = UUID("21000000-0000-0000-0000-000000000001")


class RecordingAnswerer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.alert = False

    async def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        self.events.append("ack")
        self.alert = show_alert


class RecordingActions:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def select_variant(self, *, line_id: UUID, variant_id: UUID, actor_id: UUID) -> UUID:
        assert (line_id, variant_id, actor_id) == (LINE_ID, VARIANT_ID, ACTOR_ID)
        self.events.append("select")
        return PROPOSAL_ID

    async def confirm(self, *, proposal_id: UUID, actor_id: UUID) -> UUID:
        self.events.append("confirm")
        return UUID("60000000-0000-0000-0000-000000000001")

    async def cancel(self, *, proposal_id: UUID, actor_id: UUID) -> UUID:
        self.events.append("cancel")
        return proposal_id


async def test_selection_is_acknowledged_before_database_action() -> None:
    events: list[str] = []
    dispatcher = TelegramCallbackDispatcher(
        answerer=RecordingAnswerer(events), repository=RecordingActions(events)
    )
    data = encode_callback(CallbackCommand(CallbackAction.SELECT_VARIANT, LINE_ID, VARIANT_ID))

    outcome = await dispatcher.dispatch(
        callback_query_id="callback-1", callback_data=data, actor_id=ACTOR_ID
    )

    assert events == ["ack", "select"]
    assert outcome.status is CallbackOutcomeStatus.COMPLETED
    assert outcome.result_id == PROPOSAL_ID


async def test_malformed_callback_alerts_without_database_action() -> None:
    events: list[str] = []
    answerer = RecordingAnswerer(events)
    dispatcher = TelegramCallbackDispatcher(answerer=answerer, repository=RecordingActions(events))

    outcome = await dispatcher.dispatch(
        callback_query_id="callback-2", callback_data="forged", actor_id=ACTOR_ID
    )

    assert events == ["ack"]
    assert answerer.alert is True
    assert outcome.status is CallbackOutcomeStatus.INVALID


async def test_confirm_and_cancel_route_to_distinct_actions() -> None:
    events: list[str] = []
    dispatcher = TelegramCallbackDispatcher(
        answerer=RecordingAnswerer(events), repository=RecordingActions(events)
    )
    for action in (CallbackAction.CONFIRM_PROPOSAL, CallbackAction.CANCEL_PROPOSAL):
        await dispatcher.dispatch(
            callback_query_id=f"callback-{action}",
            callback_data=encode_callback(CallbackCommand(action, PROPOSAL_ID)),
            actor_id=ACTOR_ID,
        )

    assert events == ["ack", "confirm", "ack", "cancel"]
