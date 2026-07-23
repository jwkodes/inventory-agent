"""Shared extracted-command matching and proposal orchestration."""

from decimal import Decimal
from typing import Protocol
from uuid import UUID

from inventory_agent.extraction.interpreter import CommandExtractionResult
from inventory_agent.extraction.schema import ExtractedCommandLine, InventoryIntent
from inventory_agent.matching.clarification import MatchClarificationRepository
from inventory_agent.matching.models import MatchDecision, MatchDecisionStatus
from inventory_agent.processing.models import (
    InventoryEventProcessingResult,
    InventoryEventProcessingStatus,
    ProcessingOutcomeDraft,
    ProcessingOutcomeType,
    TelegramInventoryEventContext,
)
from inventory_agent.processing.repository import ProcessingOutboxRepository
from inventory_agent.proposals.models import ProposalDraft, ProposalIntent, ProposalLineDraft
from inventory_agent.proposals.repository import ProposalRepository


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


class InventoryCommandHandler:
    """Match one extracted command and create its durable review outcome."""

    def __init__(
        self,
        *,
        matcher: ItemMatcher,
        proposals: ProposalRepository,
        outbox: ProcessingOutboxRepository,
        clarifications: MatchClarificationRepository | None = None,
    ) -> None:
        self._matcher = matcher
        self._proposals = proposals
        self._outbox = outbox
        self._clarifications = clarifications

    async def handle(
        self,
        *,
        context: TelegramInventoryEventContext,
        extraction: CommandExtractionResult,
    ) -> InventoryEventProcessingResult:
        command = extraction.command
        if command.needs_clarification or command.intent is InventoryIntent.UNKNOWN:
            return await self._with_message(
                context=context,
                outcome_type=ProcessingOutcomeType.CLARIFICATION_REQUIRED,
                message=command.clarification_question
                or "What inventory change would you like to make?",
            )
        if command.intent is InventoryIntent.QUERY_INVENTORY:
            return await self._with_message(
                context=context,
                outcome_type=ProcessingOutcomeType.UNSUPPORTED_COMMAND,
                message="Inventory queries are not available in this prototype yet.",
            )
        if command.intent is InventoryIntent.ADJUST_STOCK:
            return await self._with_message(
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
        if self._clarifications is not None and any(
            line.match_evidence.get("decision") == MatchDecisionStatus.CLARIFICATION_REQUIRED.value
            for line in proposal_lines
        ):
            await self._clarifications.begin(
                proposal_id=proposal_id,
                actor_id=context.organization_user_id,
                chat_id=context.chat_id,
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
        return InventoryEventProcessingResult(
            event_id=context.event_id,
            status=InventoryEventProcessingStatus.PROPOSAL_READY,
            chat_id=context.chat_id,
            proposal_id=proposal_id,
            outbox_id=outbox_id,
        )

    async def _with_message(
        self,
        *,
        context: TelegramInventoryEventContext,
        outcome_type: ProcessingOutcomeType,
        message: str,
    ) -> InventoryEventProcessingResult:
        outbox_id = await self._outbox.enqueue(
            ProcessingOutcomeDraft(
                organization_id=context.organization_id,
                source_event_id=context.event_id,
                outcome_type=outcome_type,
                chat_id=context.chat_id,
                payload={"message": message},
            )
        )
        return InventoryEventProcessingResult(
            event_id=context.event_id,
            status=InventoryEventProcessingStatus(outcome_type.value),
            chat_id=context.chat_id,
            outbox_id=outbox_id,
        )


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
    if decision.clarification_question is not None:
        evidence["clarification_question"] = decision.clarification_question
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
