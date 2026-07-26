"""Telegram orchestration tests for the durable LLM-led agent path."""

import asyncio
from dataclasses import dataclass
from uuid import UUID

from inventory_agent.agent.models import TransactionRecord
from inventory_agent.agent.repository import AgentConversation
from inventory_agent.agent.runtime import ModelTurn
from inventory_agent.catalog.interpreter import CatalogDetailsExtractionResult
from inventory_agent.catalog.models import (
    CatalogItemCreationView,
    ExtractedCatalogItemDetails,
)
from inventory_agent.processing.agent_text_events import TelegramAgentTextEventProcessor
from inventory_agent.processing.models import (
    ProcessingOutcomeDraft,
    ProcessingOutcomeType,
    TelegramTextEventContext,
    TextEventProcessingStatus,
)
from inventory_agent.proposals.actions import ProposalActionRejectedError

EVENT_ID = UUID("50000000-0000-0000-0000-000000000001")
ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
LOCATION_ID = UUID("12000000-0000-0000-0000-000000000001")
CONVERSATION_ID = UUID("65000000-0000-0000-0000-000000000001")
OUTBOX_ID = UUID("60000000-0000-0000-0000-000000000001")
CATALOG_REQUEST_ID = UUID("71000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("72000000-0000-0000-0000-000000000001")
TRANSACTION_ID = UUID("73000000-0000-0000-0000-000000000001")


class FakeEvents:
    def __init__(self, message_text: str = "tell me a joke") -> None:
        self.finished: list[tuple[UUID, bool]] = []
        self.context = context(message_text)

    async def claim_next_callback_event(self) -> None:
        return None

    async def claim_next_text_event(self) -> TelegramTextEventContext | None:
        return self.context

    async def claim_next_image_event(self) -> None:
        return None

    async def claim_text_event(self, event_id: UUID) -> TelegramTextEventContext | None:
        return self.context

    async def finish_event(
        self,
        *,
        event_id: UUID,
        success: bool,
        error_message: str | None = None,
    ) -> bool:
        self.finished.append((event_id, success))
        return True


@dataclass
class FakeModel:
    calls: int = 0
    last_input_items: list[dict[str, object]] | None = None

    async def respond(
        self,
        *,
        input_items: list[dict[str, object]],
        instructions: str,
        tools: list[dict[str, object]],
    ) -> ModelTurn:
        self.calls += 1
        self.last_input_items = input_items
        return ModelTurn(
            response_id="response-1",
            model="gpt-test",
            output_items=[
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                "I'm an inventory assistant and can only help with "
                                "inventory-related work."
                            ),
                        }
                    ],
                }
            ],
            output_text=(
                "I'm an inventory assistant and can only help with inventory-related work."
            ),
            function_calls=[],
        )


class FakeConversations:
    def __init__(self, conversation: AgentConversation) -> None:
        self.conversation = conversation
        self.saved: list[dict[str, object]] = []

    async def load(
        self,
        *,
        organization_id: UUID,
        organization_user_id: UUID,
        chat_id: int,
    ) -> AgentConversation:
        return self.conversation

    async def save(self, **kwargs: object) -> UUID:
        self.saved.append(kwargs)
        return CONVERSATION_ID


class FakeProposalActions:
    def __init__(self, *, reject: bool = False) -> None:
        self.confirmed: list[tuple[UUID, UUID]] = []
        self.cancelled: list[tuple[UUID, UUID]] = []
        self.reject = reject

    async def select_variant(
        self,
        *,
        line_id: UUID,
        variant_id: UUID,
        actor_id: UUID,
    ) -> UUID:
        raise AssertionError("not expected")

    async def confirm(self, *, proposal_id: UUID, actor_id: UUID) -> UUID:
        self.confirmed.append((proposal_id, actor_id))
        if self.reject:
            raise ProposalActionRejectedError("not confirmable")
        return TRANSACTION_ID

    async def cancel(self, *, proposal_id: UUID, actor_id: UUID) -> UUID:
        self.cancelled.append((proposal_id, actor_id))
        if self.reject:
            raise ProposalActionRejectedError("not cancellable")
        return proposal_id


class FakeContextManager:
    def __init__(self) -> None:
        self.calls = 0

    async def compact_if_needed(
        self,
        conversation: AgentConversation,
    ) -> AgentConversation:
        self.calls += 1
        return conversation


class BlockingPostTurnContextManager:
    def __init__(self) -> None:
        self.calls = 0
        self.background_started = asyncio.Event()
        self.release_background = asyncio.Event()

    async def compact_if_needed(
        self,
        conversation: AgentConversation,
    ) -> AgentConversation:
        self.calls += 1
        if self.calls == 2:
            self.background_started.set()
            await self.release_background.wait()
        return conversation


class FailingPostTurnContextManager:
    def __init__(self) -> None:
        self.calls = 0

    async def compact_if_needed(
        self,
        conversation: AgentConversation,
    ) -> AgentConversation:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("summary provider unavailable")
        return conversation


class FakeOutbox:
    def __init__(self) -> None:
        self.drafts: list[ProcessingOutcomeDraft] = []

    async def enqueue(self, draft: ProcessingOutcomeDraft) -> UUID:
        self.drafts.append(draft)
        return OUTBOX_ID


class FakeReversals:
    async def capture_reason(
        self,
        *,
        event_id: UUID,
        actor_id: UUID,
        chat_id: int,
        reason: str,
    ) -> UUID | None:
        return None

    async def begin(self, *, transaction_id: UUID, actor_id: UUID, chat_id: int) -> UUID:
        raise AssertionError("not expected")

    async def confirm(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        raise AssertionError("not expected")

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        raise AssertionError("not expected")


class FakeCatalog:
    def __init__(self, request_id: UUID | None = None) -> None:
        self.request_id = request_id
        self.saved = False

    async def find_pending(self, *, actor_id: UUID, chat_id: int) -> UUID | None:
        return self.request_id

    async def get_view(self, *, request_id: UUID) -> CatalogItemCreationView:
        return CatalogItemCreationView(
            request_id=request_id,
            status="awaiting_details",
            suggested_name="Gigablox Network Switch",
            suggested_sku="BB-08-01",
            suggested_base_unit="each",
            suggested_tracking_mode="simple",
        )

    async def save_draft(self, **kwargs: object) -> UUID:
        self.saved = True
        return CATALOG_REQUEST_ID

    async def save_details(self, **kwargs: object) -> UUID:
        self.saved = True
        return CATALOG_REQUEST_ID


class NewInventoryRequestCatalogInterpreter:
    def __init__(self) -> None:
        self.calls = 0

    async def interpret(
        self,
        *,
        user_text: str,
        view: CatalogItemCreationView,
    ) -> CatalogDetailsExtractionResult:
        self.calls += 1
        assert user_text == "I received 3 AMOX-500"
        return CatalogDetailsExtractionResult(
            details=ExtractedCatalogItemDetails(
                applies_to_pending_request=False,
                name=None,
                sku=None,
                base_unit=None,
                tracking_mode=None,
                attributes=[],
            ),
            response_id="catalog-routing-response",
            model="gpt-test",
        )


class UnusedCatalogReader:
    async def read(self, **kwargs: object) -> object:
        raise AssertionError("not expected")


class UnusedReads:
    async def get_variant_balances(self, **kwargs: object) -> object:
        raise AssertionError("not expected")

    async def read_transactions(self, **kwargs: object) -> object:
        raise AssertionError("not expected")


class ExactTransactionReads(UnusedReads):
    def __init__(self) -> None:
        self.queries: list[tuple[UUID, str | None, int]] = []

    async def read_transactions(
        self,
        *,
        organization_id: UUID,
        query: str | None,
        limit: int,
    ) -> list[TransactionRecord]:
        self.queries.append((organization_id, query, limit))
        if query != str(TRANSACTION_ID):
            return []
        return [
            TransactionRecord(
                transaction_id=str(TRANSACTION_ID),
                transaction_type="receive",
                status="applied",
                occurred_at="2026-07-25T10:00:00+08:00",
                summary="Receive: 100 each Classic T-Shirt [SHIRT-GREY-M]",
                reversed=False,
            )
        ]


class UnusedProposals:
    async def create(self, draft: object) -> UUID:
        raise AssertionError("not expected")


class UnusedCatalogInterpreter:
    async def interpret(self, **kwargs: object) -> object:
        raise AssertionError("not expected")


def context(message_text: str = "tell me a joke") -> TelegramTextEventContext:
    return TelegramTextEventContext(
        event_id=EVENT_ID,
        organization_id=ORGANIZATION_ID,
        organization_user_id=ACTOR_ID,
        location_id=LOCATION_ID,
        external_event_id="telegram-1",
        chat_id=123,
        telegram_user_id=456,
        message_text=message_text,
    )


def conversation(
    *,
    replay: bool = False,
    proposal_id: UUID | None = None,
) -> AgentConversation:
    return AgentConversation(
        conversation_id=CONVERSATION_ID,
        organization_id=ORGANIZATION_ID,
        organization_user_id=ACTOR_ID,
        chat_id=123,
        last_source_event_id=EVENT_ID if replay else None,
        last_reply_text="Previously saved reply." if replay else None,
        last_proposal_id=proposal_id,
    )


def processor(
    *,
    model: FakeModel,
    conversations: FakeConversations,
    outbox: FakeOutbox,
    events: FakeEvents,
    catalog: FakeCatalog | None = None,
    catalog_interpreter: object | None = None,
    context_manager: object | None = None,
    proposal_actions: FakeProposalActions | None = None,
    bot_username: str | None = None,
    reads: object | None = None,
) -> TelegramAgentTextEventProcessor:
    return TelegramAgentTextEventProcessor(
        events=events,  # type: ignore[arg-type]
        model=model,
        conversations=conversations,
        catalog_reader=UnusedCatalogReader(),  # type: ignore[arg-type]
        reads=reads or UnusedReads(),  # type: ignore[arg-type]
        proposals=UnusedProposals(),  # type: ignore[arg-type]
        proposal_actions=proposal_actions or FakeProposalActions(),
        outbox=outbox,
        reversals=FakeReversals(),
        catalog=catalog or FakeCatalog(),  # type: ignore[arg-type]
        catalog_interpreter=catalog_interpreter or UnusedCatalogInterpreter(),  # type: ignore[arg-type]
        context_manager=context_manager,  # type: ignore[arg-type]
        bot_username=bot_username,
    )


async def test_unrelated_telegram_message_saves_conversation_and_enqueues_new_message() -> None:
    model = FakeModel()
    conversations = FakeConversations(conversation())
    outbox = FakeOutbox()
    events = FakeEvents()

    result = await processor(
        model=model,
        conversations=conversations,
        outbox=outbox,
        events=events,
    ).process_next()

    assert result is not None
    assert result.status is TextEventProcessingStatus.AGENT_MESSAGE
    assert conversations.saved[0]["source_event_id"] == EVENT_ID
    assert conversations.saved[0]["turn_history"][0] == {
        "role": "user",
        "content": "tell me a joke",
    }
    assert conversations.saved[0]["estimated_tokens"] > 0
    assert outbox.drafts[0].outcome_type is ProcessingOutcomeType.AGENT_MESSAGE
    assert "inventory assistant" in outbox.drafts[0].payload["message"]
    assert events.finished == [(EVENT_ID, True)]


async def test_group_bot_mention_is_removed_before_model_and_history() -> None:
    model = FakeModel()
    conversations = FakeConversations(conversation())

    result = await processor(
        model=model,
        conversations=conversations,
        outbox=FakeOutbox(),
        events=FakeEvents("@capybababot  show my past transactions"),
        bot_username="capybababot",
    ).process_next()

    assert result is not None
    assert model.last_input_items is not None
    assert model.last_input_items[0] == {
        "role": "user",
        "content": "show my past transactions",
    }
    assert conversations.saved[0]["turn_history"][0] == {
        "role": "user",
        "content": "show my past transactions",
    }


async def test_user_supplied_uuid_is_resolved_before_the_model_with_current_turn_ref() -> None:
    model = FakeModel()
    stored = conversation().model_copy(
        update={
            "history": [
                {"role": "user", "content": "show the transaction"},
                {
                    "type": "function_call",
                    "call_id": "old-read",
                    "name": "read_transactions",
                    "arguments": '{"query":null,"limit":5}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "old-read",
                    "output": '{"transaction_id":"hallucinated-old-id"}',
                },
                {
                    "role": "assistant",
                    "content": "The transaction is hallucinated-old-id.",
                },
            ],
            "allowed_transaction_ids": [UUID("74000000-0000-0000-0000-000000000001")],
        }
    )
    conversations = FakeConversations(stored)
    reads = ExactTransactionReads()

    result = await processor(
        model=model,
        conversations=conversations,
        outbox=FakeOutbox(),
        events=FakeEvents(f"reverse transaction {TRANSACTION_ID}"),
        reads=reads,
    ).process_next()

    assert result is not None
    assert reads.queries == [(ORGANIZATION_ID, str(TRANSACTION_ID), 1)]
    assert model.last_input_items is not None
    model_input = str(model.last_input_items)
    assert '"transaction_ref":"T1"' in model_input
    assert str(TRANSACTION_ID) in model_input
    assert "hallucinated-old-id" not in model_input
    saved = conversations.saved[0]
    assert saved["allowed_transaction_ids"] == {TRANSACTION_ID}
    assert any(
        item.get("_ephemeral_agent_context") is True
        for item in saved["turn_history"]  # type: ignore[union-attr]
    )
    assert all(
        item.get("_ephemeral_agent_context") is not True
        for item in saved["history"]  # type: ignore[union-attr]
    )


async def test_context_is_checked_before_and_after_a_completed_agent_turn() -> None:
    context_manager = FakeContextManager()
    conversations = FakeConversations(conversation())
    event_processor = processor(
        model=FakeModel(),
        conversations=conversations,
        outbox=FakeOutbox(),
        events=FakeEvents(),
        context_manager=context_manager,
    )

    result = await event_processor.process_next()
    await event_processor.wait_for_background_compactions()

    assert result is not None
    assert context_manager.calls == 2


async def test_post_turn_compaction_does_not_block_reply_but_serializes_next_turn() -> None:
    context_manager = BlockingPostTurnContextManager()
    event_processor = processor(
        model=FakeModel(),
        conversations=FakeConversations(conversation()),
        outbox=FakeOutbox(),
        events=FakeEvents(),
        context_manager=context_manager,
    )

    first_result = await event_processor.process_next()
    assert first_result is not None
    await asyncio.wait_for(context_manager.background_started.wait(), timeout=1)

    second_turn = asyncio.create_task(event_processor.process_next())
    await asyncio.sleep(0)
    assert not second_turn.done()

    context_manager.release_background.set()
    second_result = await asyncio.wait_for(second_turn, timeout=1)
    await event_processor.wait_for_background_compactions()

    assert second_result is not None
    assert context_manager.calls == 4


async def test_background_compaction_failure_does_not_fail_or_poison_conversation() -> None:
    context_manager = FailingPostTurnContextManager()
    event_processor = processor(
        model=FakeModel(),
        conversations=FakeConversations(conversation()),
        outbox=FakeOutbox(),
        events=FakeEvents(),
        context_manager=context_manager,
    )

    first_result = await event_processor.process_next()
    await event_processor.wait_for_background_compactions()
    second_result = await event_processor.process_next()
    await event_processor.wait_for_background_compactions()

    assert first_result is not None
    assert second_result is not None
    assert context_manager.calls == 4


async def test_retried_saved_turn_reuses_reply_without_another_model_call() -> None:
    model = FakeModel()
    conversations = FakeConversations(conversation(replay=True))
    outbox = FakeOutbox()
    events = FakeEvents()

    result = await processor(
        model=model,
        conversations=conversations,
        outbox=outbox,
        events=events,
    ).process_next()

    assert result is not None
    assert result.status is TextEventProcessingStatus.AGENT_MESSAGE
    assert model.calls == 0
    assert conversations.saved == []
    assert outbox.drafts[0].payload == {"message": "Previously saved reply."}


async def test_new_inventory_request_bypasses_stale_catalog_details_flow() -> None:
    model = FakeModel()
    conversations = FakeConversations(conversation())
    outbox = FakeOutbox()
    events = FakeEvents("I received 3 AMOX-500")
    catalog = FakeCatalog(CATALOG_REQUEST_ID)
    catalog_interpreter = NewInventoryRequestCatalogInterpreter()

    result = await processor(
        model=model,
        conversations=conversations,
        outbox=outbox,
        events=events,
        catalog=catalog,
        catalog_interpreter=catalog_interpreter,
    ).process_next()

    assert result is not None
    assert result.status is TextEventProcessingStatus.AGENT_MESSAGE
    assert catalog_interpreter.calls == 1
    assert catalog.saved is False
    assert model.calls == 1
    assert conversations.saved[0]["source_event_id"] == EVENT_ID


async def test_exact_typed_confirm_applies_active_proposal_without_calling_model() -> None:
    model = FakeModel()
    conversations = FakeConversations(conversation(proposal_id=PROPOSAL_ID))
    outbox = FakeOutbox()
    events = FakeEvents("Confirm")
    proposal_actions = FakeProposalActions()
    context_manager = FakeContextManager()

    result = await processor(
        model=model,
        conversations=conversations,
        outbox=outbox,
        events=events,
        proposal_actions=proposal_actions,
        context_manager=context_manager,
    ).process_next()

    assert result is not None
    assert result.status is TextEventProcessingStatus.TRANSACTION_APPLIED
    assert result.proposal_id == PROPOSAL_ID
    assert model.calls == 0
    assert context_manager.calls == 0
    assert proposal_actions.confirmed == [(PROPOSAL_ID, ACTOR_ID)]
    assert proposal_actions.cancelled == []
    assert outbox.drafts[0].outcome_type is ProcessingOutcomeType.TRANSACTION_APPLIED
    assert outbox.drafts[0].aggregate_id == TRANSACTION_ID
    assert conversations.saved[0]["proposal_id"] is None
    assert TRANSACTION_ID in conversations.saved[0]["allowed_transaction_ids"]
    assert conversations.saved[0]["model_name"] == "deterministic-proposal-control"
    assert events.finished == [(EVENT_ID, True)]


async def test_exact_typed_cancel_rejects_active_proposal_without_calling_model() -> None:
    model = FakeModel()
    conversations = FakeConversations(conversation(proposal_id=PROPOSAL_ID))
    outbox = FakeOutbox()
    events = FakeEvents("  CANCEL  ")
    proposal_actions = FakeProposalActions()

    result = await processor(
        model=model,
        conversations=conversations,
        outbox=outbox,
        events=events,
        proposal_actions=proposal_actions,
    ).process_next()

    assert result is not None
    assert result.status is TextEventProcessingStatus.AGENT_MESSAGE
    assert model.calls == 0
    assert proposal_actions.cancelled == [(PROPOSAL_ID, ACTOR_ID)]
    assert outbox.drafts[0].outcome_type is ProcessingOutcomeType.CALLBACK_NOTICE
    assert outbox.drafts[0].aggregate_id is None
    assert conversations.saved[0]["proposal_id"] is None
    assert events.finished == [(EVENT_ID, True)]


async def test_exact_typed_confirm_without_active_proposal_refuses_to_guess() -> None:
    model = FakeModel()
    conversations = FakeConversations(conversation())
    outbox = FakeOutbox()
    events = FakeEvents("confirm")
    proposal_actions = FakeProposalActions()

    result = await processor(
        model=model,
        conversations=conversations,
        outbox=outbox,
        events=events,
        proposal_actions=proposal_actions,
    ).process_next()

    assert result is not None
    assert result.status is TextEventProcessingStatus.AGENT_MESSAGE
    assert model.calls == 0
    assert proposal_actions.confirmed == []
    assert proposal_actions.cancelled == []
    assert conversations.saved == []
    assert "nothing to confirm" in outbox.drafts[0].payload["message"]
    assert events.finished == [(EVENT_ID, True)]


async def test_non_exact_confirmation_language_remains_a_conversation_turn() -> None:
    model = FakeModel()
    conversations = FakeConversations(conversation(proposal_id=PROPOSAL_ID))
    outbox = FakeOutbox()
    events = FakeEvents("Can you confirm which quantities you used?")
    proposal_actions = FakeProposalActions()

    result = await processor(
        model=model,
        conversations=conversations,
        outbox=outbox,
        events=events,
        proposal_actions=proposal_actions,
    ).process_next()

    assert result is not None
    assert model.calls == 1
    assert proposal_actions.confirmed == []
    assert proposal_actions.cancelled == []


async def test_rejected_typed_confirmation_reports_no_change_without_model() -> None:
    model = FakeModel()
    conversations = FakeConversations(conversation(proposal_id=PROPOSAL_ID))
    outbox = FakeOutbox()
    events = FakeEvents("Confirm")
    proposal_actions = FakeProposalActions(reject=True)

    result = await processor(
        model=model,
        conversations=conversations,
        outbox=outbox,
        events=events,
        proposal_actions=proposal_actions,
    ).process_next()

    assert result is not None
    assert result.status is TextEventProcessingStatus.AGENT_MESSAGE
    assert model.calls == 0
    assert conversations.saved == []
    assert "incomplete or no longer pending" in outbox.drafts[0].payload["message"]
    assert events.finished == [(EVENT_ID, True)]
