"""Telegram text processing through the LLM-led inventory agent."""

from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from inventory_agent.agent.context import (
    AgentContextManager,
    durable_history_items,
    estimate_history_tokens,
)
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
from inventory_agent.proposals.actions import (
    ProposalActionRejectedError,
    ProposalActionRepository,
)
from inventory_agent.proposals.repository import ProposalRepository
from inventory_agent.reversals.repository import ReversalRepository

logger = logging.getLogger(__name__)


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
        proposal_actions: ProposalActionRepository,
        outbox: ProcessingOutboxRepository,
        reversals: ReversalRepository,
        catalog: CatalogItemCreationRepository,
        catalog_interpreter: CatalogDetailsInterpreter,
        context_manager: AgentContextManager | None = None,
    ) -> None:
        self._events = events
        self._model = model
        self._conversations = conversations
        self._catalog_reader = catalog_reader
        self._reads = reads
        self._proposals = proposals
        self._proposal_actions = proposal_actions
        self._outbox = outbox
        self._reversals = reversals
        self._catalog = catalog
        self._catalog_interpreter = catalog_interpreter
        self._context_manager = context_manager

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

            proposal_control = await self._handle_typed_proposal_control(
                context,
                conversation,
            )
            if proposal_control is not None:
                return proposal_control

            if self._context_manager is not None:
                conversation = await self._context_manager.compact_if_needed(conversation)

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
            active_history = durable_history_items(conversation.history)
            session = InventoryAgentSession(
                model=self._model,
                tools=tools,
                history=active_history,
                summary=conversation.summary,
            )
            history_start = len(active_history)
            reply = await session.handle(context.message_text)
            raw_turn_history = session.history[history_start:]
            turn_history = durable_history_items(raw_turn_history)
            persisted_history = active_history + turn_history
            await self._conversations.save(
                conversation_id=conversation.conversation_id,
                source_event_id=context.event_id,
                organization_user_id=context.organization_user_id,
                history=persisted_history,
                turn_history=raw_turn_history,
                estimated_tokens=estimate_history_tokens(turn_history),
                allowed_variant_ids=tools.allowed_variant_ids,
                allowed_transaction_ids=tools.allowed_transaction_ids,
                reply_text=reply.text,
                proposal_id=tools.stock_proposal_id,
                reversal_request_id=tools.reversal_request_id,
                reversal_reason=tools.reversal_reason,
                response_id=reply.response_id,
                model_name=reply.model,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                total_tokens=reply.total_tokens,
            )
            result = await self._enqueue_agent_turn(
                context=context,
                reply_text=reply.text,
                proposal_id=tools.stock_proposal_id,
                reversal_request_id=tools.reversal_request_id,
                reversal_reason=tools.reversal_reason,
            )
            await self._require_finish(context.event_id)
            await self._compact_after_turn(context)
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

    async def _compact_after_turn(self, context: TelegramTextEventContext) -> None:
        if self._context_manager is None:
            return
        try:
            conversation = await self._conversations.load(
                organization_id=context.organization_id,
                organization_user_id=context.organization_user_id,
                chat_id=context.chat_id,
            )
            await self._context_manager.compact_if_needed(conversation)
        except Exception:
            logger.exception(
                "agent_context_compaction status=failed conversation_chat_id=%s",
                context.chat_id,
            )

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
        if not extraction.details.applies_to_pending_request:
            return None
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
                            "❓ **More information needed**\n"
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

    async def _handle_typed_proposal_control(
        self,
        context: TelegramTextEventContext,
        conversation: AgentConversation,
    ) -> TextEventProcessingResult | None:
        action = _typed_proposal_control(context.message_text)
        if action is None:
            return None
        if conversation.last_proposal_id is None:
            outbox_id = await self._outbox.enqueue(
                ProcessingOutcomeDraft(
                    organization_id=context.organization_id,
                    source_event_id=context.event_id,
                    outcome_type=ProcessingOutcomeType.AGENT_MESSAGE,
                    chat_id=context.chat_id,
                    payload={
                        "message": (
                            "⚠️ **No active proposal selected**\n"
                            f"I did not {action} anything because this conversation is "
                            "not currently pointing to a pending stock proposal. Ask me "
                            "to prepare or show the intended change again."
                        )
                    },
                )
            )
            await self._require_finish(context.event_id)
            return TextEventProcessingResult(
                event_id=context.event_id,
                status=TextEventProcessingStatus.AGENT_MESSAGE,
                chat_id=context.chat_id,
                outbox_id=outbox_id,
            )

        proposal_id = conversation.last_proposal_id
        try:
            result_id = await (
                self._proposal_actions.confirm(
                    proposal_id=proposal_id,
                    actor_id=context.organization_user_id,
                )
                if action == "confirm"
                else self._proposal_actions.cancel(
                    proposal_id=proposal_id,
                    actor_id=context.organization_user_id,
                )
            )
        except ProposalActionRejectedError:
            outbox_id = await self._outbox.enqueue(
                ProcessingOutcomeDraft(
                    organization_id=context.organization_id,
                    source_event_id=context.event_id,
                    outcome_type=ProcessingOutcomeType.AGENT_MESSAGE,
                    chat_id=context.chat_id,
                    payload={
                        "message": (
                            "⚠️ **Proposal action not applied**\n"
                            f"I could not {action} the active proposal. It may still "
                            "need an item match, may no longer be pending, or may fail an "
                            "inventory validation. No additional stock change was made. "
                            "Ask me to show or prepare the intended change again."
                        )
                    },
                )
            )
            await self._require_finish(context.event_id)
            return TextEventProcessingResult(
                event_id=context.event_id,
                status=TextEventProcessingStatus.AGENT_MESSAGE,
                chat_id=context.chat_id,
                proposal_id=proposal_id,
                outbox_id=outbox_id,
            )

        if action == "confirm":
            outcome_type = ProcessingOutcomeType.TRANSACTION_APPLIED
            status = TextEventProcessingStatus.TRANSACTION_APPLIED
            aggregate_id: UUID | None = result_id
            payload: dict[str, object] = {}
            lifecycle_message = (
                "Inventory system event: The user typed the exact Confirm command for "
                f"stock proposal {proposal_id}. Inventory transaction {result_id} was "
                "applied successfully. The proposal is no longer pending. Read "
                "authoritative transactions before correcting or reversing it."
            )
            allowed_transaction_ids = set(conversation.allowed_transaction_ids) | {result_id}
        else:
            outcome_type = ProcessingOutcomeType.CALLBACK_NOTICE
            status = TextEventProcessingStatus.AGENT_MESSAGE
            aggregate_id = None
            payload = {"message": "🚫 **Proposal cancelled**\nNo inventory changes were applied."}
            lifecycle_message = (
                "Inventory system event: The user typed the exact Cancel command for "
                f"stock proposal {result_id}. It was cancelled and no inventory change "
                "resulted from that proposal."
            )
            allowed_transaction_ids = set(conversation.allowed_transaction_ids)

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
        history_item: dict[str, object] = {
            "role": "system",
            "content": lifecycle_message,
        }
        await self._conversations.save(
            conversation_id=conversation.conversation_id,
            source_event_id=context.event_id,
            organization_user_id=context.organization_user_id,
            history=[*conversation.history, history_item],
            turn_history=[history_item],
            estimated_tokens=estimate_history_tokens([history_item]),
            allowed_variant_ids=set(conversation.allowed_variant_ids),
            allowed_transaction_ids=allowed_transaction_ids,
            reply_text=lifecycle_message,
            proposal_id=None,
            reversal_request_id=None,
            reversal_reason=None,
            response_id=f"deterministic-proposal-{action}-{context.event_id}",
            model_name="deterministic-proposal-control",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
        )
        await self._require_finish(context.event_id)
        return TextEventProcessingResult(
            event_id=context.event_id,
            status=status,
            chat_id=context.chat_id,
            proposal_id=proposal_id,
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


def _typed_proposal_control(message_text: str) -> str | None:
    normalized = message_text.strip().casefold()
    if normalized in {"confirm", "cancel"}:
        return normalized
    return None
