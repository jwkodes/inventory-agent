"""Telegram text processing through the LLM-led inventory agent."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from functools import partial
from time import perf_counter
from typing import Protocol
from uuid import UUID

from inventory_agent.agent.context import (
    AgentContextManager,
    durable_history_items,
    estimate_history_tokens,
    model_history_items,
)
from inventory_agent.agent.models import TransactionRecord
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
from inventory_agent.agent.runtime import (
    AgentModel,
    InventoryAgentSession,
    build_prompt_cache_key,
)
from inventory_agent.catalog.details import complete_catalog_item_details
from inventory_agent.catalog.interpreter import CatalogDetailsExtractionResult
from inventory_agent.catalog.models import CatalogItemCreationView
from inventory_agent.catalog.repository import CatalogItemCreationRepository
from inventory_agent.extraction.clarification import (
    CommandClarificationInterpreter,
    CommandClarificationRepository,
)
from inventory_agent.extraction.interpreter import CommandExtractionResult
from inventory_agent.extraction.schema import InventoryIntent
from inventory_agent.processing.catalog_batches import CatalogBatchReplyHandler
from inventory_agent.processing.models import (
    InventoryEventProcessingResult,
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
from inventory_agent.telegram.group_activation import strip_bot_reference

logger = logging.getLogger(__name__)
TRANSACTION_UUID_PATTERN = re.compile(
    r"(?<![0-9a-fA-F])"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"(?![0-9a-fA-F])"
)


class CatalogDetailsInterpreter(Protocol):
    async def interpret(
        self,
        *,
        user_text: str,
        view: CatalogItemCreationView,
    ) -> CatalogDetailsExtractionResult:
        """Extract catalog fields from a natural user reply."""


class ExtractedCommandHandler(Protocol):
    async def handle(
        self,
        *,
        context: TelegramTextEventContext,
        extraction: CommandExtractionResult,
    ) -> InventoryEventProcessingResult:
        """Resume matching and proposal creation from a clarified command."""


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
        command_clarifications: CommandClarificationRepository | None = None,
        command_clarification_interpreter: CommandClarificationInterpreter | None = None,
        command_handler: ExtractedCommandHandler | None = None,
        catalog_batches: CatalogBatchReplyHandler | None = None,
        bot_username: str | None = None,
    ) -> None:
        command_flow_dependencies = (
            command_clarifications,
            command_clarification_interpreter,
            command_handler,
        )
        if any(value is not None for value in command_flow_dependencies) and not all(
            value is not None for value in command_flow_dependencies
        ):
            raise ValueError(
                "command clarification repository, interpreter, and handler are all required"
            )
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
        self._command_clarifications = command_clarifications
        self._command_clarification_interpreter = command_clarification_interpreter
        self._command_handler = command_handler
        self._catalog_batches = catalog_batches
        self._bot_username = bot_username
        self._background_compactions: dict[tuple[UUID, UUID, int], asyncio.Task[None]] = {}

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
        processing_started = perf_counter()
        try:
            context = context.model_copy(
                update={
                    "message_text": strip_bot_reference(
                        context.message_text,
                        bot_username=self._bot_username,
                    )
                }
            )
            pending = await self._handle_pending_deterministic_flow(context)
            if pending is not None:
                return pending

            await self._wait_for_background_compaction(context)
            conversation_load_started = perf_counter()
            conversation = await self._conversations.load(
                organization_id=context.organization_id,
                organization_user_id=context.organization_user_id,
                chat_id=context.chat_id,
            )
            self._log_runtime(
                component="conversation_load",
                started=conversation_load_started,
                context=context,
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
                compaction_started = perf_counter()
                conversation = await self._context_manager.compact_if_needed(conversation)
                self._log_runtime(
                    component="context_compaction_before_turn",
                    started=compaction_started,
                    context=context,
                )

            exact_lookup_started = perf_counter()
            preselected_transactions, turn_context = await self._resolve_explicit_transactions(
                context
            )
            self._log_runtime(
                component="explicit_transaction_resolution",
                started=exact_lookup_started,
                context=context,
                requested=len(_transaction_ids_in_text(context.message_text)),
                found=len(preselected_transactions),
            )
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
                preselected_transactions=preselected_transactions,
            )
            active_history = model_history_items(conversation.history)
            session = InventoryAgentSession(
                model=self._model,
                tools=tools,
                history=active_history,
                summary=conversation.summary,
                prompt_cache_key=build_prompt_cache_key(conversation.conversation_id),
            )
            history_start = len(active_history)
            reply = await session.handle(context.message_text, turn_context=turn_context)
            raw_turn_history = session.history[history_start:]
            turn_history = durable_history_items(raw_turn_history)
            persisted_history = active_history + turn_history
            save_started = perf_counter()
            await self._conversations.save(
                conversation_id=conversation.conversation_id,
                source_event_id=context.event_id,
                organization_user_id=context.organization_user_id,
                history=persisted_history,
                turn_history=raw_turn_history,
                estimated_tokens=estimate_history_tokens(model_history_items(turn_history)),
                allowed_variant_ids=tools.allowed_variant_ids,
                allowed_transaction_ids=tools.allowed_transaction_ids,
                reply_text=reply.text,
                proposal_id=tools.stock_proposal_id,
                reversal_request_id=tools.reversal_request_id,
                reversal_reason=tools.reversal_reason,
                response_id=reply.response_id,
                model_name=reply.model,
                input_tokens=reply.input_tokens,
                cached_input_tokens=reply.cached_input_tokens,
                cache_write_tokens=reply.cache_write_tokens,
                output_tokens=reply.output_tokens,
                total_tokens=reply.total_tokens,
            )
            self._log_runtime(
                component="conversation_save",
                started=save_started,
                context=context,
            )
            enqueue_started = perf_counter()
            result = await self._enqueue_agent_turn(
                context=context,
                reply_text=reply.text,
                proposal_id=tools.stock_proposal_id,
                reversal_request_id=tools.reversal_request_id,
                reversal_reason=tools.reversal_reason,
            )
            self._log_runtime(
                component="outbox_enqueue_agent_turn",
                started=enqueue_started,
                context=context,
            )
            await self._require_finish(context.event_id)
            self._schedule_background_compaction(context)
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
        finally:
            self._log_runtime(
                component="telegram_text_event_total",
                started=processing_started,
                context=context,
            )

    async def _resolve_explicit_transactions(
        self,
        context: TelegramTextEventContext,
    ) -> tuple[list[TransactionRecord], list[dict[str, object]]]:
        requested = _transaction_ids_in_text(context.message_text)
        if not requested:
            return [], []
        found: list[TransactionRecord] = []
        missing: list[str] = []
        for transaction_id in requested:
            records = await self._reads.read_transactions(
                organization_id=context.organization_id,
                query=str(transaction_id),
                limit=1,
            )
            exact = next(
                (record for record in records if UUID(record.transaction_id) == transaction_id),
                None,
            )
            if exact is None:
                missing.append(str(transaction_id))
            else:
                found.append(exact)
        resolved = [
            {
                **transaction.model_dump(mode="json"),
                "transaction_ref": f"T{index}",
            }
            for index, transaction in enumerate(found, start=1)
        ]
        context_payload = {
            "authoritative_current_turn_transaction_resolution": {
                "resolved": resolved,
                "not_found": missing,
                "instruction": (
                    "Use transaction_ref for reversal proposals. UUID text is display-only. "
                    "Do not fuzzy-match or repair a UUID that was not found."
                ),
            }
        }
        return found, [
            {
                "role": "system",
                "content": json.dumps(context_payload, separators=(",", ":"), default=str),
            }
        ]

    @staticmethod
    def _log_runtime(
        *,
        component: str,
        started: float,
        context: TelegramTextEventContext,
        **fields: object,
    ) -> None:
        details = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
        logger.info(
            "component_runtime component=%s duration_ms=%.2f organization_id=%s "
            "source_event_id=%s chat_id=%s%s",
            component,
            (perf_counter() - started) * 1000,
            context.organization_id,
            context.event_id,
            context.chat_id,
            f" {details}" if details else "",
        )

    async def wait_for_background_compactions(self) -> None:
        """Wait for scheduled compactions during tests or graceful shutdown."""

        while tasks := tuple(self._background_compactions.values()):
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))
            for key, task in tuple(self._background_compactions.items()):
                if task.done():
                    self._background_compactions.pop(key, None)

    async def _wait_for_background_compaction(
        self,
        context: TelegramTextEventContext,
    ) -> None:
        task = self._background_compactions.get(self._compaction_key(context))
        if task is None:
            return
        started = perf_counter()
        await asyncio.shield(task)
        self._log_runtime(
            component="context_compaction_wait_before_turn",
            started=started,
            context=context,
        )

    def _schedule_background_compaction(
        self,
        context: TelegramTextEventContext,
    ) -> None:
        if self._context_manager is None:
            return
        key = self._compaction_key(context)
        existing = self._background_compactions.get(key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._compact_after_turn(context),
            name=f"compact-agent-context-{context.chat_id}",
        )
        self._background_compactions[key] = task
        task.add_done_callback(partial(self._forget_background_compaction, key))

    async def _compact_after_turn(self, context: TelegramTextEventContext) -> None:
        if self._context_manager is None:  # pragma: no cover - scheduling invariant
            return
        started = perf_counter()
        try:
            conversation = await self._conversations.load(
                organization_id=context.organization_id,
                organization_user_id=context.organization_user_id,
                chat_id=context.chat_id,
            )
            await self._context_manager.compact_if_needed(conversation)
            self._log_runtime(
                component="context_compaction_background",
                started=started,
                context=context,
                status="completed",
            )
        except Exception:
            logger.exception(
                "agent_context_compaction status=failed conversation_chat_id=%s",
                context.chat_id,
            )
            self._log_runtime(
                component="context_compaction_background",
                started=started,
                context=context,
                status="failed",
            )

    @staticmethod
    def _compaction_key(
        context: TelegramTextEventContext,
    ) -> tuple[UUID, UUID, int]:
        return (
            context.organization_id,
            context.organization_user_id,
            context.chat_id,
        )

    def _forget_background_compaction(
        self,
        key: tuple[UUID, UUID, int],
        task: asyncio.Future[None],
    ) -> None:
        if self._background_compactions.get(key) is task:
            self._background_compactions.pop(key, None)

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

        if self._catalog_batches is not None:
            batch_result = await self._catalog_batches.handle_pending(context=context)
            if batch_result is not None:
                await self._require_finish(context.event_id)
                return batch_result

        if (
            self._command_clarifications is not None
            and self._command_clarification_interpreter is not None
            and self._command_handler is not None
        ):
            command_request_id = await self._command_clarifications.find_pending(
                actor_id=context.organization_user_id,
                chat_id=context.chat_id,
            )
            if command_request_id is not None:
                if _is_standalone_clarification_cancel(context.message_text):
                    await self._command_clarifications.cancel(
                        request_id=command_request_id,
                        event_id=context.event_id,
                        actor_id=context.organization_user_id,
                    )
                    outbox_id = await self._outbox.enqueue(
                        ProcessingOutcomeDraft(
                            organization_id=context.organization_id,
                            source_event_id=context.event_id,
                            outcome_type=ProcessingOutcomeType.AGENT_MESSAGE,
                            chat_id=context.chat_id,
                            payload={
                                "message": (
                                    "🚫 **Request cancelled**\n"
                                    "I stopped asking about the earlier image or command."
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
                command_view = await self._command_clarifications.get_view(
                    request_id=command_request_id
                )
                clarification = await self._command_clarification_interpreter.resolve(
                    view=command_view,
                    user_reply=context.message_text,
                )
                if (
                    clarification.cancel_pending_request
                    or not clarification.applies_to_pending_request
                ):
                    await self._command_clarifications.cancel(
                        request_id=command_request_id,
                        event_id=context.event_id,
                        actor_id=context.organization_user_id,
                    )
                else:
                    command_extraction = clarification.extraction
                    command = command_extraction.command
                    if command.needs_clarification or command.intent in {
                        InventoryIntent.UNKNOWN,
                        InventoryIntent.ADJUST_STOCK,
                    }:
                        question = command.clarification_question or (
                            "Should I add or remove these quantities?"
                            if command.intent is InventoryIntent.ADJUST_STOCK
                            else "What inventory change should I make?"
                        )
                        await self._command_clarifications.continue_request(
                            request_id=command_request_id,
                            event_id=context.event_id,
                            actor_id=context.organization_user_id,
                            user_reply=context.message_text,
                            question=question,
                            extraction=command_extraction,
                        )
                        outbox_id = await self._outbox.enqueue(
                            ProcessingOutcomeDraft(
                                organization_id=context.organization_id,
                                source_event_id=context.event_id,
                                outcome_type=ProcessingOutcomeType.CLARIFICATION_REQUIRED,
                                chat_id=context.chat_id,
                                payload={
                                    "message": (
                                        f"❓ **Reply with the missing information**\n{question}"
                                    )
                                },
                            )
                        )
                        await self._require_finish(context.event_id)
                        return TextEventProcessingResult(
                            event_id=context.event_id,
                            status=TextEventProcessingStatus.CLARIFICATION_REQUIRED,
                            chat_id=context.chat_id,
                            outbox_id=outbox_id,
                        )

                    result = await self._command_handler.handle(
                        context=context,
                        extraction=command_extraction,
                    )
                    await self._command_clarifications.resolve(
                        request_id=command_request_id,
                        event_id=context.event_id,
                        actor_id=context.organization_user_id,
                        user_reply=context.message_text,
                        extraction=command_extraction,
                        proposal_id=result.proposal_id,
                    )
                    await self._require_finish(context.event_id)
                    return result

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
                            "❓ **Reply with the missing catalog information**\n"
                            f"Please send {_natural_list(missing)} in any format."
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
                            "⚠️ **No pending proposal**\n"
                            f"There is nothing to {action}. Ask me to prepare the change first."
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
                            "⚠️ **Proposal not updated**\n"
                            f"I couldn't {action} it because it is incomplete or no longer "
                            "pending. Ask me to prepare the change again."
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
            payload = {"message": "🚫 **Proposal cancelled**"}
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
            cached_input_tokens=0,
            cache_write_tokens=0,
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


def _is_standalone_clarification_cancel(message_text: str) -> bool:
    normalized = re.sub(r"[^\w/]+", " ", message_text.casefold()).strip()
    return normalized in {
        "/cancel",
        "/reset",
        "cancel",
        "cancel it",
        "cancel that",
        "cancel this",
        "discard it",
        "drop it",
        "forget it",
        "forget that",
        "forget that image",
        "never mind",
        "nevermind",
        "reset",
        "stop",
        "stop asking",
    }


def _transaction_ids_in_text(message_text: str) -> list[UUID]:
    transaction_ids: list[UUID] = []
    seen: set[UUID] = set()
    for match in TRANSACTION_UUID_PATTERN.finditer(message_text):
        transaction_id = UUID(match.group())
        if transaction_id in seen:
            continue
        seen.add(transaction_id)
        transaction_ids.append(transaction_id)
    return transaction_ids
