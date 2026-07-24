"""Telegram orchestration tests for the durable LLM-led agent path."""

from dataclasses import dataclass
from uuid import UUID

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

EVENT_ID = UUID("50000000-0000-0000-0000-000000000001")
ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
LOCATION_ID = UUID("12000000-0000-0000-0000-000000000001")
CONVERSATION_ID = UUID("65000000-0000-0000-0000-000000000001")
OUTBOX_ID = UUID("60000000-0000-0000-0000-000000000001")
CATALOG_REQUEST_ID = UUID("71000000-0000-0000-0000-000000000001")


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

    async def respond(
        self,
        *,
        input_items: list[dict[str, object]],
        instructions: str,
        tools: list[dict[str, object]],
    ) -> ModelTurn:
        self.calls += 1
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


def conversation(*, replay: bool = False) -> AgentConversation:
    return AgentConversation(
        conversation_id=CONVERSATION_ID,
        organization_id=ORGANIZATION_ID,
        organization_user_id=ACTOR_ID,
        chat_id=123,
        last_source_event_id=EVENT_ID if replay else None,
        last_reply_text="Previously saved reply." if replay else None,
    )


def processor(
    *,
    model: FakeModel,
    conversations: FakeConversations,
    outbox: FakeOutbox,
    events: FakeEvents,
    catalog: FakeCatalog | None = None,
    catalog_interpreter: object | None = None,
) -> TelegramAgentTextEventProcessor:
    return TelegramAgentTextEventProcessor(
        events=events,  # type: ignore[arg-type]
        model=model,
        conversations=conversations,
        catalog_reader=UnusedCatalogReader(),  # type: ignore[arg-type]
        reads=UnusedReads(),  # type: ignore[arg-type]
        proposals=UnusedProposals(),  # type: ignore[arg-type]
        outbox=outbox,
        reversals=FakeReversals(),
        catalog=catalog or FakeCatalog(),  # type: ignore[arg-type]
        catalog_interpreter=catalog_interpreter or UnusedCatalogInterpreter(),  # type: ignore[arg-type]
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
    assert outbox.drafts[0].outcome_type is ProcessingOutcomeType.AGENT_MESSAGE
    assert "inventory assistant" in outbox.drafts[0].payload["message"]
    assert events.finished == [(EVENT_ID, True)]


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
