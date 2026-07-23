"""Orchestrate text extraction and durable inventory-command handling."""

from typing import Protocol
from uuid import UUID

from inventory_agent.extraction.interpreter import CommandExtractionResult
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


class CommandInterpreter(Protocol):
    async def interpret(self, user_text: str) -> CommandExtractionResult:
        """Extract a strict inventory command from one message."""


class TextEventProcessingError(RuntimeError):
    """A claimed event failed and was recorded as failed."""


class TelegramTextEventProcessor:
    """Turn one persisted Telegram text message into a reviewable proposal."""

    def __init__(
        self,
        *,
        events: SourceEventWorkRepository,
        interpreter: CommandInterpreter,
        matcher: ItemMatcher,
        proposals: ProposalRepository,
        outbox: ProcessingOutboxRepository,
        reversals: ReversalRepository,
    ) -> None:
        self._events = events
        self._interpreter = interpreter
        self._outbox = outbox
        self._reversals = reversals
        self._commands = InventoryCommandHandler(
            matcher=matcher,
            proposals=proposals,
            outbox=outbox,
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

            extraction = await self._interpreter.interpret(context.message_text)
            result = await self._commands.handle(context=context, extraction=extraction)
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
