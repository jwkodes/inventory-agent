"""Orchestrate extraction, matching, proposal creation, and durable handoff."""

from decimal import Decimal
from typing import Protocol
from uuid import UUID

from inventory_agent.extraction.interpreter import CommandExtractionResult
from inventory_agent.extraction.schema import (
    ExtractedCommandLine,
    InventoryIntent,
)
from inventory_agent.matching.models import MatchDecision, MatchDecisionStatus
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
from inventory_agent.proposals.models import ProposalDraft, ProposalIntent, ProposalLineDraft
from inventory_agent.proposals.repository import ProposalRepository
from inventory_agent.reversals.repository import ReversalRepository


class CommandInterpreter(Protocol):
    async def interpret(self, user_text: str) -> CommandExtractionResult:
        """Extract a strict inventory command from one message."""


class ItemMatcher(Protocol):
    async def match_line(
        self,
        *,
        organization_id: UUID,
        line: ExtractedCommandLine,
        supplier_scope: str | None = None,
        limit: int = 5,
    ) -> MatchDecision:
        """Resolve or offer candidates for one extracted line."""


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
        self._matcher = matcher
        self._proposals = proposals
        self._outbox = outbox
        self._reversals = reversals

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
            command = extraction.command

            if command.needs_clarification or command.intent is InventoryIntent.UNKNOWN:
                return await self._finish_with_message(
                    context=context,
                    outcome_type=ProcessingOutcomeType.CLARIFICATION_REQUIRED,
                    message=command.clarification_question
                    or "What inventory change would you like to make?",
                )

            if command.intent is InventoryIntent.QUERY_INVENTORY:
                return await self._finish_with_message(
                    context=context,
                    outcome_type=ProcessingOutcomeType.UNSUPPORTED_COMMAND,
                    message="Inventory queries are not available in this prototype yet.",
                )

            if command.intent is InventoryIntent.ADJUST_STOCK:
                return await self._finish_with_message(
                    context=context,
                    outcome_type=ProcessingOutcomeType.CLARIFICATION_REQUIRED,
                    message=(
                        "Should I add or remove this quantity, or set the current on-hand "
                        "quantity to this number?"
                    ),
                )

            proposal_lines = []
            for line_number, line in enumerate(command.lines, start=1):
                decision = await self._matcher.match_line(
                    organization_id=context.organization_id,
                    line=line,
                )
                proposal_lines.append(_proposal_line(line_number, line, decision))

            intent = {
                InventoryIntent.RECEIVE_STOCK: ProposalIntent.RECEIVE_STOCK,
                InventoryIntent.ISSUE_STOCK: ProposalIntent.ISSUE_STOCK,
            }[command.intent]
            proposal_id = await self._proposals.create(
                ProposalDraft(
                    organization_id=context.organization_id,
                    location_id=context.location_id,
                    source_event_id=context.event_id,
                    created_by=context.organization_user_id,
                    intent=intent,
                    idempotency_key=(
                        f"telegram:{context.external_event_id}:command:{command.schema_version}"
                    ),
                    raw_command=command.model_dump(mode="json"),
                    model_name=extraction.model,
                    model_response_id=extraction.response_id,
                    prompt_version=extraction.prompt_version,
                    notes=command.notes,
                    lines=proposal_lines,
                )
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

    async def _finish_with_message(
        self,
        *,
        context: TelegramTextEventContext,
        outcome_type: ProcessingOutcomeType,
        message: str,
    ) -> TextEventProcessingResult:
        outbox_id = await self._outbox.enqueue(
            ProcessingOutcomeDraft(
                organization_id=context.organization_id,
                source_event_id=context.event_id,
                outcome_type=outcome_type,
                chat_id=context.chat_id,
                payload={"message": message},
            )
        )
        await self._require_finish(context.event_id)
        status = TextEventProcessingStatus(outcome_type.value)
        return TextEventProcessingResult(
            event_id=context.event_id,
            status=status,
            chat_id=context.chat_id,
            outbox_id=outbox_id,
        )

    async def _require_finish(self, event_id: UUID) -> None:
        if not await self._events.finish_event(event_id=event_id, success=True):
            raise RuntimeError("Claimed source event could not be completed")


def _proposal_line(
    line_number: int,
    line: ExtractedCommandLine,
    decision: MatchDecision,
) -> ProposalLineDraft:
    if line.quantity is None:
        raise ValueError("Mutation line is missing a quantity")

    selected = decision.selected if decision.status is MatchDecisionStatus.MATCHED else None
    evidence = {
        "decision": decision.status.value,
        "reason": decision.reason,
        "candidates": [candidate.model_dump(mode="json") for candidate in decision.candidates],
    }
    return ProposalLineDraft(
        line_number=line_number,
        source_text=line.source_text,
        extracted_description=line.description,
        requested_quantity=Decimal(line.quantity),
        requested_unit=line.unit,
        item_variant_id=selected.item_variant_id if selected else None,
        match_method=selected.match_method if selected else None,
        match_score=selected.match_score if selected else None,
        match_evidence=evidence,
        attributes={attribute.key: attribute.value for attribute in line.attributes},
    )
