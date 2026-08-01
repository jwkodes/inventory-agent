"""Orchestrate text extraction and durable inventory-command handling."""

from typing import Protocol
from uuid import UUID

from inventory_agent.catalog.details import complete_catalog_item_details
from inventory_agent.catalog.interpreter import CatalogDetailsExtractionResult
from inventory_agent.catalog.models import CatalogItemCreationView
from inventory_agent.catalog.repository import CatalogItemCreationRepository
from inventory_agent.extraction.interpreter import CommandExtractionResult
from inventory_agent.matching.clarification import MatchClarificationRepository
from inventory_agent.matching.judge import CandidateJudge
from inventory_agent.processing.catalog_batches import CatalogBatchReplyHandler
from inventory_agent.processing.commands import InventoryCommandHandler, ItemMatcher
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
from inventory_agent.telegram.group_activation import strip_bot_reference


class CommandInterpreter(Protocol):
    async def interpret(self, user_text: str) -> CommandExtractionResult:
        """Extract a strict inventory command from one message."""


class CatalogDetailsInterpreter(Protocol):
    async def interpret(
        self,
        *,
        user_text: str,
        view: CatalogItemCreationView,
    ) -> CatalogDetailsExtractionResult:
        """Extract catalog fields from a free-form user reply."""


class TextEventProcessingError(RuntimeError):
    """A claimed event failed and was recorded as failed."""


class TelegramTextEventProcessor:
    """Turn one persisted Telegram text message into a reviewable proposal."""

    def __init__(
        self,
        *,
        events: SourceEventWorkRepository,
        interpreter: CommandInterpreter,
        catalog_interpreter: CatalogDetailsInterpreter,
        matcher: ItemMatcher,
        proposals: ProposalRepository,
        outbox: ProcessingOutboxRepository,
        reversals: ReversalRepository,
        catalog: CatalogItemCreationRepository,
        clarifications: MatchClarificationRepository | None = None,
        candidate_judge: CandidateJudge | None = None,
        catalog_batches: CatalogBatchReplyHandler | None = None,
        bot_username: str | None = None,
    ) -> None:
        self._events = events
        self._interpreter = interpreter
        self._catalog_interpreter = catalog_interpreter
        self._outbox = outbox
        self._reversals = reversals
        self._catalog = catalog
        self._clarifications = clarifications
        self._candidate_judge = candidate_judge
        self._catalog_batches = catalog_batches
        self._bot_username = bot_username
        self._commands = InventoryCommandHandler(
            matcher=matcher,
            proposals=proposals,
            outbox=outbox,
            clarifications=clarifications,
        )

    async def process(self, event_id: UUID) -> TextEventProcessingResult:
        context = await self._events.claim_text_event(event_id)
        if context is None:
            return TextEventProcessingResult(
                event_id=event_id,
                status=TextEventProcessingStatus.ALREADY_CLAIMED,
            )
        return await self._process_claimed(context)

    async def process_next(self) -> TextEventProcessingResult | None:
        """Claim and process the oldest eligible event, or return None when idle."""

        context = await self._events.claim_next_text_event()
        if context is None:
            return None
        return await self._process_claimed(context)

    async def _process_claimed(
        self,
        context: TelegramTextEventContext,
    ) -> TextEventProcessingResult:
        try:
            context = context.model_copy(
                update={
                    "message_text": strip_bot_reference(
                        context.message_text,
                        bot_username=self._bot_username,
                    )
                }
            )
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

            if self._clarifications is not None and self._candidate_judge is not None:
                clarification_id = await self._clarifications.find_pending(
                    actor_id=context.organization_user_id,
                    chat_id=context.chat_id,
                )
                if clarification_id is not None:
                    view = await self._clarifications.get_view(request_id=clarification_id)
                    judgment = await self._candidate_judge.judge(
                        line=view.line,
                        candidates=view.candidates,
                        clarification_replies=[
                            *view.clarification_replies,
                            context.message_text,
                        ],
                        accumulated_attributes=view.accumulated_attributes,
                    )
                    proposal_id = await self._clarifications.apply(
                        request_id=clarification_id,
                        event_id=context.event_id,
                        actor_id=context.organization_user_id,
                        user_reply=context.message_text,
                        judgment=judgment,
                    )
                    outbox_id = await self._outbox.enqueue(
                        ProcessingOutcomeDraft(
                            organization_id=context.organization_id,
                            source_event_id=context.event_id,
                            outcome_type=ProcessingOutcomeType.PROPOSAL_READY,
                            aggregate_id=proposal_id,
                            chat_id=context.chat_id,
                            payload={"proposal_id": str(proposal_id)},
                        )
                    )
                    await self._require_finish(context.event_id)
                    return TextEventProcessingResult(
                        event_id=context.event_id,
                        status=TextEventProcessingStatus.PROPOSAL_READY,
                        chat_id=context.chat_id,
                        proposal_id=proposal_id,
                        outbox_id=outbox_id,
                    )

            catalog_request_id = await self._catalog.find_pending(
                actor_id=context.organization_user_id,
                chat_id=context.chat_id,
            )
            if catalog_request_id is not None:
                catalog_view = await self._catalog.get_view(request_id=catalog_request_id)
                catalog_extraction = await self._catalog_interpreter.interpret(
                    user_text=context.message_text,
                    view=catalog_view,
                )
                if catalog_extraction.details.applies_to_pending_request:
                    details, missing = complete_catalog_item_details(
                        extracted=catalog_extraction.details,
                        view=catalog_view,
                    )
                    if details is None:
                        await self._catalog.save_draft(
                            request_id=catalog_request_id,
                            event_id=context.event_id,
                            actor_id=context.organization_user_id,
                            details=catalog_extraction.details,
                        )
                        outbox_id = await self._outbox.enqueue(
                            ProcessingOutcomeDraft(
                                organization_id=context.organization_id,
                                source_event_id=context.event_id,
                                outcome_type=ProcessingOutcomeType.CALLBACK_NOTICE,
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
                            status=TextEventProcessingStatus.CLARIFICATION_REQUIRED,
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

            command_extraction = await self._interpreter.interpret(context.message_text)
            result = await self._commands.handle(
                context=context,
                extraction=command_extraction,
            )
            await self._require_finish(context.event_id)
            return result
        except Exception as error:
            failure = f"{type(error).__name__}: text event processing failed"
            try:
                await self._events.finish_event(
                    event_id=context.event_id,
                    success=False,
                    error_message=failure,
                )
            except Exception as finish_error:
                raise TextEventProcessingError(
                    "Text event processing and failure recording both failed"
                ) from finish_error
            raise TextEventProcessingError("Text event processing failed") from error

    async def _require_finish(self, event_id: UUID) -> None:
        if not await self._events.finish_event(event_id=event_id, success=True):
            raise RuntimeError("Claimed source event could not be completed")


def _natural_list(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])}, and {values[-1]}"
