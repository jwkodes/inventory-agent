"""Tests for acknowledgement-first callback dispatch."""

from uuid import UUID

from inventory_agent.catalog.models import CatalogBatchCreationView, CatalogItemCreationView
from inventory_agent.catalog.repository import CatalogItemConfirmationConflict
from inventory_agent.telegram.callback_dispatcher import (
    CallbackOutcomeStatus,
    TelegramCallbackDispatcher,
)
from inventory_agent.telegram.callbacks import CallbackAction, CallbackCommand, encode_callback

ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000001")
LINE_ID = UUID("41000000-0000-0000-0000-000000000001")
VARIANT_ID = UUID("21000000-0000-0000-0000-000000000001")
TRANSACTION_ID = UUID("60000000-0000-0000-0000-000000000001")
REVERSAL_REQUEST_ID = UUID("70000000-0000-0000-0000-000000000001")
REVERSAL_TRANSACTION_ID = UUID("60000000-0000-0000-0000-000000000002")
CATALOG_REQUEST_ID = UUID("71000000-0000-0000-0000-000000000001")


class RecordingAnswerer:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.alert = False
        self.fail = fail

    async def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        self.events.append("ack")
        self.alert = show_alert
        if self.fail:
            raise RuntimeError("Telegram acknowledgement expired")


class RecordingActions:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def select_variant(self, *, line_id: UUID, variant_id: UUID, actor_id: UUID) -> UUID:
        assert (line_id, variant_id, actor_id) == (LINE_ID, VARIANT_ID, ACTOR_ID)
        self.events.append("select")
        return PROPOSAL_ID

    async def mark_new_item(self, *, line_id: UUID, actor_id: UUID) -> UUID:
        assert (line_id, actor_id) == (LINE_ID, ACTOR_ID)
        self.events.append("mark_new")
        return PROPOSAL_ID

    async def ignore_line(self, *, line_id: UUID, actor_id: UUID) -> UUID:
        assert (line_id, actor_id) == (LINE_ID, ACTOR_ID)
        self.events.append("ignore_line")
        return PROPOSAL_ID

    async def mark_all_new_items(self, *, proposal_id: UUID, actor_id: UUID) -> UUID:
        assert (proposal_id, actor_id) == (PROPOSAL_ID, ACTOR_ID)
        self.events.append("mark_all_new")
        return proposal_id

    async def confirm(self, *, proposal_id: UUID, actor_id: UUID) -> UUID:
        self.events.append("confirm")
        return UUID("60000000-0000-0000-0000-000000000001")

    async def cancel(self, *, proposal_id: UUID, actor_id: UUID) -> UUID:
        self.events.append("cancel")
        return proposal_id


class RecordingReversals:
    def __init__(self, events: list[str], replacement_proposal_id: UUID | None = None) -> None:
        self.events = events
        self.replacement_proposal_id = replacement_proposal_id

    async def begin(
        self,
        *,
        transaction_id: UUID,
        actor_id: UUID,
        chat_id: int,
    ) -> UUID:
        assert (transaction_id, actor_id, chat_id) == (TRANSACTION_ID, ACTOR_ID, -100123)
        self.events.append("begin_reversal")
        return REVERSAL_REQUEST_ID

    async def capture_reason(
        self,
        *,
        event_id: UUID,
        actor_id: UUID,
        chat_id: int,
        reason: str,
    ) -> UUID | None:
        raise AssertionError("dispatcher does not capture text reasons")

    async def confirm(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        assert (request_id, actor_id) == (REVERSAL_REQUEST_ID, ACTOR_ID)
        self.events.append("confirm_reversal")
        return REVERSAL_TRANSACTION_ID

    async def attach_replacement(
        self,
        *,
        request_id: UUID,
        proposal_id: UUID,
        actor_id: UUID,
    ) -> UUID:
        raise AssertionError("dispatcher does not attach replacement proposals")

    async def get_completed_replacement(
        self,
        *,
        request_id: UUID,
        actor_id: UUID,
    ) -> UUID | None:
        assert (request_id, actor_id) == (REVERSAL_REQUEST_ID, ACTOR_ID)
        self.events.append("get_replacement")
        return self.replacement_proposal_id

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        assert (request_id, actor_id) == (REVERSAL_REQUEST_ID, ACTOR_ID)
        self.events.append("cancel_reversal")
        return request_id


class RecordingCatalog:
    def __init__(
        self,
        events: list[str],
        *,
        status: str = "awaiting_confirmation",
        confirmation_conflict: bool = False,
    ) -> None:
        self.events = events
        self.status = status
        self.confirmation_conflict = confirmation_conflict

    async def begin(self, *, line_id: UUID, actor_id: UUID, chat_id: int) -> UUID:
        self.events.append("begin_catalog")
        return CATALOG_REQUEST_ID

    async def show_existing(self, *, line_id: UUID, actor_id: UUID) -> UUID:
        self.events.append("show_existing")
        return PROPOSAL_ID

    async def find_pending(self, *, actor_id: UUID, chat_id: int) -> UUID | None:
        raise AssertionError("dispatcher does not capture catalog details")

    async def get_view(self, *, request_id: UUID) -> CatalogItemCreationView:
        self.events.append("get_catalog_view")
        return CatalogItemCreationView(
            request_id=request_id,
            status=self.status,
            suggested_name="AMOX-505",
            suggested_sku="AMOX-505",
            suggested_base_unit="unit",
            suggested_tracking_mode="simple",
            name="AMOX-505",
            sku="AMOX-505",
            base_unit="unit",
            tracking_mode="simple",
            attributes={"pack size": "10 units per box"},
        )

    async def save_details(self, **kwargs: object) -> UUID:
        raise AssertionError("dispatcher does not capture catalog details")

    async def confirm(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        self.events.append("confirm_catalog")
        if self.confirmation_conflict:
            raise CatalogItemConfirmationConflict(
                request_id=request_id,
                message="SKU HAC-001 is already used by the blue variant.",
            )
        return PROPOSAL_ID

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        self.events.append("cancel_catalog")
        return CATALOG_REQUEST_ID

    async def begin_batch(self, *, proposal_id: UUID, actor_id: UUID, chat_id: int) -> UUID:
        self.events.append("begin_catalog_batch")
        return CATALOG_REQUEST_ID

    async def get_batch_view(self, *, batch_id: UUID) -> CatalogBatchCreationView:
        self.events.append("get_catalog_batch_view")
        return CatalogBatchCreationView(
            batch_id=batch_id,
            proposal_id=PROPOSAL_ID,
            status=self.status,
            items=[],
        )

    async def confirm_batch(self, *, batch_id: UUID, actor_id: UUID) -> UUID:
        self.events.append("confirm_catalog_batch")
        return TRANSACTION_ID

    async def cancel_batch(self, *, batch_id: UUID, actor_id: UUID) -> UUID:
        self.events.append("cancel_catalog_batch")
        return batch_id


def dispatcher(
    events: list[str],
    *,
    answerer_fails: bool = False,
    replacement_proposal_id: UUID | None = None,
) -> TelegramCallbackDispatcher:
    return TelegramCallbackDispatcher(
        answerer=RecordingAnswerer(events, fail=answerer_fails),
        repository=RecordingActions(events),
        reversals=RecordingReversals(events, replacement_proposal_id),
        catalog=RecordingCatalog(events),
    )


async def test_selection_is_acknowledged_before_database_action() -> None:
    events: list[str] = []
    callback_dispatcher = dispatcher(events)
    data = encode_callback(CallbackCommand(CallbackAction.SELECT_VARIANT, LINE_ID, VARIANT_ID))

    outcome = await callback_dispatcher.dispatch(
        callback_query_id="callback-1",
        callback_data=data,
        actor_id=ACTOR_ID,
        chat_id=-100123,
    )

    assert events == ["ack", "select"]
    assert outcome.status is CallbackOutcomeStatus.COMPLETED
    assert outcome.result_id == PROPOSAL_ID


async def test_malformed_callback_alerts_without_database_action() -> None:
    events: list[str] = []
    answerer = RecordingAnswerer(events)
    callback_dispatcher = TelegramCallbackDispatcher(
        answerer=answerer,
        repository=RecordingActions(events),
        reversals=RecordingReversals(events),
        catalog=RecordingCatalog(events),
    )

    outcome = await callback_dispatcher.dispatch(
        callback_query_id="callback-2",
        callback_data="forged",
        actor_id=ACTOR_ID,
        chat_id=-100123,
    )

    assert events == ["ack"]
    assert answerer.alert is True
    assert outcome.status is CallbackOutcomeStatus.INVALID


async def test_confirm_and_cancel_route_to_distinct_actions() -> None:
    events: list[str] = []
    callback_dispatcher = dispatcher(events)
    for action in (CallbackAction.CONFIRM_PROPOSAL, CallbackAction.CANCEL_PROPOSAL):
        await callback_dispatcher.dispatch(
            callback_query_id=f"callback-{action}",
            callback_data=encode_callback(CallbackCommand(action, PROPOSAL_ID)),
            actor_id=ACTOR_ID,
            chat_id=-100123,
        )

    assert events == ["ack", "confirm", "ack", "cancel"]


async def test_expired_acknowledgement_does_not_block_idempotent_database_action() -> None:
    events: list[str] = []
    callback_dispatcher = dispatcher(events, answerer_fails=True)

    outcome = await callback_dispatcher.dispatch(
        callback_query_id="expired-callback",
        callback_data=encode_callback(
            CallbackCommand(CallbackAction.CONFIRM_PROPOSAL, PROPOSAL_ID)
        ),
        actor_id=ACTOR_ID,
        chat_id=-100123,
    )

    assert events == ["ack", "confirm"]
    assert outcome.status is CallbackOutcomeStatus.COMPLETED


async def test_reversal_actions_route_through_durable_request_lifecycle() -> None:
    events: list[str] = []
    callback_dispatcher = dispatcher(events)
    cases = [
        (CallbackAction.REVERSE_TRANSACTION, TRANSACTION_ID, REVERSAL_REQUEST_ID),
        (CallbackAction.CONFIRM_REVERSAL, REVERSAL_REQUEST_ID, REVERSAL_TRANSACTION_ID),
        (CallbackAction.CANCEL_REVERSAL, REVERSAL_REQUEST_ID, REVERSAL_REQUEST_ID),
    ]

    for action, target_id, expected_result in cases:
        outcome = await callback_dispatcher.dispatch(
            callback_query_id=f"callback-{action}",
            callback_data=encode_callback(CallbackCommand(action, target_id)),
            actor_id=ACTOR_ID,
            chat_id=-100123,
        )
        assert outcome.status is CallbackOutcomeStatus.COMPLETED
        assert outcome.result_id == expected_result

    assert events == [
        "ack",
        "begin_reversal",
        "ack",
        "confirm_reversal",
        "get_replacement",
        "ack",
        "cancel_reversal",
    ]


async def test_confirmed_reversal_returns_linked_replacement_proposal() -> None:
    events: list[str] = []
    callback_dispatcher = dispatcher(events, replacement_proposal_id=PROPOSAL_ID)

    outcome = await callback_dispatcher.dispatch(
        callback_query_id="callback-linked-correction",
        callback_data=encode_callback(
            CallbackCommand(CallbackAction.CONFIRM_REVERSAL, REVERSAL_REQUEST_ID)
        ),
        actor_id=ACTOR_ID,
        chat_id=-100123,
    )

    assert outcome.result_id == REVERSAL_TRANSACTION_ID
    assert outcome.replacement_proposal_id == PROPOSAL_ID
    assert events == ["ack", "confirm_reversal", "get_replacement"]


async def test_catalog_actions_route_through_durable_resolution_lifecycle() -> None:
    events: list[str] = []
    callback_dispatcher = dispatcher(events)
    cases = [
        (CallbackAction.ADD_NEW_ITEM, LINE_ID, CATALOG_REQUEST_ID),
        (CallbackAction.SHOW_EXISTING_ITEMS, LINE_ID, PROPOSAL_ID),
        (CallbackAction.CONFIRM_NEW_ITEM, CATALOG_REQUEST_ID, PROPOSAL_ID),
        (CallbackAction.CANCEL_NEW_ITEM, CATALOG_REQUEST_ID, CATALOG_REQUEST_ID),
    ]

    for action, target_id, expected_result in cases:
        outcome = await callback_dispatcher.dispatch(
            callback_query_id=f"callback-{action}",
            callback_data=encode_callback(CallbackCommand(action, target_id)),
            actor_id=ACTOR_ID,
            chat_id=-100123,
        )
        assert outcome.status is CallbackOutcomeStatus.COMPLETED
        assert outcome.result_id == expected_result
        if action is CallbackAction.ADD_NEW_ITEM:
            assert outcome.catalog_status == "awaiting_confirmation"

    assert events == [
        "ack",
        "mark_new",
        "begin_catalog",
        "get_catalog_view",
        "ack",
        "show_existing",
        "ack",
        "confirm_catalog",
        "ack",
        "cancel_catalog",
    ]


async def test_bulk_catalog_actions_use_one_batch_lifecycle() -> None:
    events: list[str] = []
    callback_dispatcher = dispatcher(events)
    cases = [
        (CallbackAction.ADD_ALL_NEW_ITEMS, PROPOSAL_ID, CATALOG_REQUEST_ID),
        (CallbackAction.CONFIRM_CATALOG_BATCH, CATALOG_REQUEST_ID, TRANSACTION_ID),
        (CallbackAction.CANCEL_CATALOG_BATCH, CATALOG_REQUEST_ID, CATALOG_REQUEST_ID),
    ]

    for action, target_id, expected_result in cases:
        outcome = await callback_dispatcher.dispatch(
            callback_query_id=f"callback-{action}",
            callback_data=encode_callback(CallbackCommand(action, target_id)),
            actor_id=ACTOR_ID,
            chat_id=-100123,
        )
        assert outcome.status is CallbackOutcomeStatus.COMPLETED
        assert outcome.result_id == expected_result

    assert events == [
        "ack",
        "mark_all_new",
        "begin_catalog_batch",
        "get_catalog_batch_view",
        "ack",
        "confirm_catalog_batch",
        "ack",
        "cancel_catalog_batch",
    ]


async def test_multi_line_decisions_return_the_proposal_for_a_fresh_review() -> None:
    events: list[str] = []
    callback_dispatcher = dispatcher(events)

    for action in (CallbackAction.MARK_NEW_ITEM, CallbackAction.IGNORE_PROPOSAL_LINE):
        outcome = await callback_dispatcher.dispatch(
            callback_query_id=f"callback-{action}",
            callback_data=encode_callback(CallbackCommand(action, LINE_ID)),
            actor_id=ACTOR_ID,
            chat_id=-100123,
        )
        assert outcome.status is CallbackOutcomeStatus.COMPLETED
        assert outcome.result_id == PROPOSAL_ID

    assert events == ["ack", "mark_new", "ack", "ignore_line"]


async def test_duplicate_catalog_sku_reopens_details_and_alerts_immediately() -> None:
    events: list[str] = []
    answerer = RecordingAnswerer(events)
    callback_dispatcher = TelegramCallbackDispatcher(
        answerer=answerer,
        repository=RecordingActions(events),
        reversals=RecordingReversals(events),
        catalog=RecordingCatalog(events, confirmation_conflict=True),
    )

    outcome = await callback_dispatcher.dispatch(
        callback_query_id="duplicate-sku",
        callback_data=encode_callback(
            CallbackCommand(CallbackAction.CONFIRM_NEW_ITEM, CATALOG_REQUEST_ID)
        ),
        actor_id=ACTOR_ID,
        chat_id=-100123,
    )

    assert outcome.status is CallbackOutcomeStatus.COMPLETED
    assert outcome.result_id == CATALOG_REQUEST_ID
    assert outcome.catalog_status == "awaiting_details"
    assert answerer.alert is True
    assert events == ["ack", "confirm_catalog", "ack"]
