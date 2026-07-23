"""Telegram text processing through the LLM-led inventory agent."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from inventory_agent.agent.production_tools import (
    AgentCatalogReader,
    ProductionInventoryAgentTools,
    ProductionToolContext,
)
from inventory_agent.agent.repository import (
    AgentConversation,
    AgentConversationRepository,
    AgentReadRepository,
)
from inventory_agent.agent.runtime import AgentModel, InventoryAgentSession
from inventory_agent.catalog.details import complete_catalog_item_details
from inventory_agent.catalog.interpreter import CatalogDetailsExtractionResult
from inventory_agent.catalog.models import CatalogItemCreationView
from inventory_agent.catalog.repository import CatalogItemCreationRepository
from inventory_agent.processing.models import (
    ProcessingOutcomeDraft,
    ProcessingOutcomeType,
    TelegramTextEventContext,
    TextEventProcessingResult,
    TextEventProcessingStatus,
)
from inventory_agent.processing.repository import (
    ProcessingOutboxRepository,
    SourceEventWorkRepository,
)
from inventory_agent.proposals.repository import ProposalRepository
from inventory_agent.reversals.repository import ReversalRepository


class CatalogDetailsInterpreter(Protocol):
    async def interpret(
        self,
        *,
        user_text: str,
        view: CatalogItemCreationView,
    ) -> CatalogDetailsExtractionResult:
        """Extract catalog fields from a natural user reply."""


class AgentTextEventProcessingError(RuntimeError):
    """A claimed agent event failed and was recorded for retry."""


class TelegramAgentTextEventProcessor:
    """Run one durable Telegram conversation turn through inventory tools."""

    def __init__(
        self,
        *,
        events: SourceEventWorkRepository,
        model: AgentModel,
        conversations: AgentConversationRepository,
        catalog_reader: AgentCatalogReader,
        reads: AgentReadRepository,
        proposals: ProposalRepository,
        outbox: ProcessingOutboxRepository,
        reversals: ReversalRepository,
        catalog: CatalogItemCreationRepository,
        catalog_interpreter: CatalogDetailsInterpreter,
    ) -> None:
        self._events = events
        self._model = model
        self._conversations = conversations
        self._catalog_reader = catalog_reader
        self._reads = reads
        self._proposals = proposals
        self._outbox = outbox
        self._reversals = reversals
        self._catalog = catalog
        self._catalog_interpreter = catalog_interpreter

    async def process(self, event_id: UUID) -> TextEventProcessingResult:
        context = await self._events.claim_text_event(event_id)
        if context is None:
            return TextEventProcessingResult(
                event_id=event_id,
                status=TextEventProcessingStatus.ALREADY_CLAIMED,
            )
        return await self._process_claimed(context)

    async def process_next(self) -> TextEventProcessingResult | None:
        context = await self._events.claim_next_text_event()
        if context is None:
            return None
        return await self._process_claimed(context)

    async def _process_claimed(
        self,
        context: TelegramTextEventContext,
    ) -> TextEventProcessingResult:
        try:
            pending = await self._handle_pending_deterministic_flow(context)
            if pending is not None:
                return pending

            conversation = await self._conversations.load(
                organization_id=context.organization_id,
                organization_user_id=context.organization_user_id,
                chat_id=context.chat_id,
            )
            if conversation.last_source_event_id == context.event_id:
                result = await self._enqueue_replayed_turn(context, conversation)
                await self._require_finish(context.event_id)
                return result

            tools = ProductionInventoryAgentTools(
                context=ProductionToolContext(
                    organization_id=context.organization_id,
                    organization_user_id=context.organization_user_id,
                    location_id=context.location_id,
                    source_event_id=context.event_id,
                    external_event_id=context.external_event_id,
                    chat_id=context.chat_id,
                ),
                catalog=self._catalog_reader,
                reads=self._reads,
                proposals=self._proposals,
                reversals=self._reversals,
                allowed_variant_ids=set(conversation.allowed_variant_ids),
                allowed_transaction_ids=set(conversation.allowed_transaction_ids),
            )
            session = InventoryAgentSession(
                model=self._model,
                tools=tools,
                history=conversation.history,
            )
            reply = await session.handle(context.message_text)
            await self._conversations.save(
                conversation_id=conversation.conversation_id,
                source_event_id=context.event_id,
                organization_user_id=context.organization_user_id,
                history=session.history,
                allowed_variant_ids=tools.allowed_variant_ids,
                allowed_transaction_ids=tools.allowed_transaction_ids,
                reply_text=reply.text,
                proposal_id=tools.stock_proposal_id,
                reversal_request_id=tools.reversal_request_id,
                reversal_reason=tools.reversal_reason,
                response_id=reply.response_id,
                model_name=reply.model,
            )
            result = await self._enqueue_agent_turn(
                context=context,
                reply_text=reply.text,
                proposal_id=tools.stock_proposal_id,
                reversal_request_id=tools.reversal_request_id,
                reversal_reason=tools.reversal_reason,
            )
            await self._require_finish(context.event_id)
            return result
        except Exception as error:
            failure = f"{type(error).__name__}: agent text event processing failed"
            try:
                await self._events.finish_event(
                    event_id=context.event_id,
                    success=False,
                    error_message=failure,
                )
            except Exception as finish_error:
                raise AgentTextEventProcessingError(
                    "Agent processing and failure recording both failed"
                ) from finish_error
            raise AgentTextEventProcessingError("Agent text event processing failed") from error

    async def _handle_pending_deterministic_flow(
        self,
        context: TelegramTextEventContext,
    ) -> TextEventProcessingResult | None:
        reversal_request_id = await self._reversals.capture_reason(
            event_id=context.event_id,
            actor_id=context.organization_user_id,
            chat_id=context.chat_id,
            reason=context.message_text,
        )
        if reversal_request_id is not None:
            outbox_id = await self._outbox.enqueue(
                ProcessingOutcomeDraft(
                    organization_id=context.organization_id,
                    source_event_id=context.event_id,
                    outcome_type=ProcessingOutcomeType.REVERSAL_CONFIRMATION,
                    aggregate_id=reversal_request_id,
                    chat_id=context.chat_id,
                    payload={"reason": context.message_text.strip()},
                )
            )
            await self._require_finish(context.event_id)
            return TextEventProcessingResult(
                event_id=context.event_id,
                status=TextEventProcessingStatus.REVERSAL_CONFIRMATION,
                chat_id=context.chat_id,
                reversal_request_id=reversal_request_id,
                outbox_id=outbox_id,
            )

        catalog_request_id = await self._catalog.find_pending(
            actor_id=context.organization_user_id,
            chat_id=context.chat_id,
        )
        if catalog_request_id is None:
            return None
        view = await self._catalog.get_view(request_id=catalog_request_id)
        extraction = await self._catalog_interpreter.interpret(
            user_text=context.message_text,
            view=view,
        )
        details, missing = complete_catalog_item_details(
            extracted=extraction.details,
            view=view,
        )
        if details is None:
            await self._catalog.save_draft(
                request_id=catalog_request_id,
                event_id=context.event_id,
                actor_id=context.organization_user_id,
                details=extraction.details,
            )
            outbox_id = await self._outbox.enqueue(
                ProcessingOutcomeDraft(
                    organization_id=context.organization_id,
                    source_event_id=context.event_id,
                    outcome_type=ProcessingOutcomeType.AGENT_MESSAGE,
                    chat_id=context.chat_id,
                    payload={
                        "message": (
                            f"I still need {_natural_list(missing)}. "
                            "Reply naturally with the missing information."
                        )
                    },
                )
            )
            await self._require_finish(context.event_id)
            return TextEventProcessingResult(
                event_id=context.event_id,
                status=TextEventProcessingStatus.AGENT_MESSAGE,
                chat_id=context.chat_id,
                catalog_request_id=catalog_request_id,
                outbox_id=outbox_id,
            )

        await self._catalog.save_details(
            request_id=catalog_request_id,
            event_id=context.event_id,
            actor_id=context.organization_user_id,
            details=details,
        )
        outbox_id = await self._outbox.enqueue(
            ProcessingOutcomeDraft(
                organization_id=context.organization_id,
                source_event_id=context.event_id,
                outcome_type=ProcessingOutcomeType.CATALOG_ITEM_CONFIRMATION,
                aggregate_id=catalog_request_id,
                chat_id=context.chat_id,
                payload={},
            )
        )
        await self._require_finish(context.event_id)
        return TextEventProcessingResult(
            event_id=context.event_id,
            status=TextEventProcessingStatus.CATALOG_ITEM_CONFIRMATION,
            chat_id=context.chat_id,
            catalog_request_id=catalog_request_id,
            outbox_id=outbox_id,
        )

    async def _enqueue_replayed_turn(
        self,
        context: TelegramTextEventContext,
        conversation: AgentConversation,
    ) -> TextEventProcessingResult:
        if conversation.last_reply_text is None:
            raise RuntimeError("Replayed agent turn is missing its saved reply")
        return await self._enqueue_agent_turn(
            context=context,
            reply_text=conversation.last_reply_text,
            proposal_id=conversation.last_proposal_id,
            reversal_request_id=conversation.last_reversal_request_id,
            reversal_reason=conversation.last_reversal_reason,
        )

    async def _enqueue_agent_turn(
        self,
        *,
        context: TelegramTextEventContext,
        reply_text: str,
        proposal_id: UUID | None,
        reversal_request_id: UUID | None,
        reversal_reason: str | None,
    ) -> TextEventProcessingResult:
        if proposal_id is not None:
            outcome_type = ProcessingOutcomeType.PROPOSAL_READY
            status = TextEventProcessingStatus.PROPOSAL_READY
            aggregate_id = proposal_id
            payload = {"proposal_id": str(proposal_id), "agent_reply": reply_text}
        elif reversal_request_id is not None:
            if reversal_reason is None:
                raise RuntimeError("Agent reversal is missing its reason")
            outcome_type = ProcessingOutcomeType.REVERSAL_CONFIRMATION
            status = TextEventProcessingStatus.REVERSAL_CONFIRMATION
            aggregate_id = reversal_request_id
            payload = {"reason": reversal_reason, "agent_reply": reply_text}
        else:
            outcome_type = ProcessingOutcomeType.AGENT_MESSAGE
            status = TextEventProcessingStatus.AGENT_MESSAGE
            aggregate_id = None
            payload = {"message": reply_text}
        outbox_id = await self._outbox.enqueue(
            ProcessingOutcomeDraft(
                organization_id=context.organization_id,
                source_event_id=context.event_id,
                outcome_type=outcome_type,
                aggregate_id=aggregate_id,
                chat_id=context.chat_id,
                payload=payload,
            )
        )
        return TextEventProcessingResult(
            event_id=context.event_id,
            status=status,
            chat_id=context.chat_id,
            proposal_id=proposal_id,
            reversal_request_id=reversal_request_id,
            outbox_id=outbox_id,
        )

    async def _require_finish(self, event_id: UUID) -> None:
        if not await self._events.finish_event(event_id=event_id, success=True):
            raise RuntimeError("Claimed source event could not be completed")


def _natural_list(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])}, and {values[-1]}"
