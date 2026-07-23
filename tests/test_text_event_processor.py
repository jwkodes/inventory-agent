"""Tests for the persisted Telegram text-event orchestration."""

from decimal import Decimal
from uuid import UUID

import pytest

from inventory_agent.extraction.interpreter import CommandExtractionResult
from inventory_agent.extraction.schema import (
    ExtractedCommandLine,
    ExtractedInventoryCommand,
    InventoryIntent,
)
from inventory_agent.matching.models import (
    CandidateMatchMethod,
    InventoryCandidate,
    MatchDecision,
    MatchDecisionStatus,
    TrackingMode,
)
from inventory_agent.processing.models import (
    ProcessingOutcomeDraft,
    TelegramTextEventContext,
    TextEventProcessingStatus,
)
from inventory_agent.processing.text_events import (
    TelegramTextEventProcessor,
    TextEventProcessingError,
)
from inventory_agent.proposals.models import ProposalDraft

EVENT_ID = UUID("50000000-0000-0000-0000-000000000004")
ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
MEMBER_ID = UUID("11000000-0000-0000-0000-000000000001")
LOCATION_ID = UUID("12000000-0000-0000-0000-000000000001")
VARIANT_ID = UUID("21000000-0000-0000-0000-000000000003")
ITEM_ID = UUID("20000000-0000-0000-0000-000000000003")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000004")
OUTBOX_ID = UUID("60000000-0000-0000-0000-000000000004")


class FakeEvents:
    def __init__(self, context: TelegramTextEventContext | None = None) -> None:
        self.context = context
        self.finishes: list[tuple[UUID, bool, str | None]] = []

    async def claim_next_text_event(self) -> TelegramTextEventContext | None:
        return self.context

    async def claim_text_event(self, event_id: UUID) -> TelegramTextEventContext | None:
        assert event_id == EVENT_ID
        return self.context

    async def finish_event(
        self,
        *,
        event_id: UUID,
        success: bool,
        error_message: str | None = None,
    ) -> bool:
        self.finishes.append((event_id, success, error_message))
        return True


class FakeInterpreter:
    def __init__(self, command: ExtractedInventoryCommand | Exception) -> None:
        self.command = command

    async def interpret(self, user_text: str) -> CommandExtractionResult:
        assert user_text == "received three AMOX-500"
        if isinstance(self.command, Exception):
            raise self.command
        return CommandExtractionResult(
            command=self.command,
            response_id="resp_test",
            model="gpt-test",
        )


class FakeMatcher:
    def __init__(self, decision: MatchDecision) -> None:
        self.decision = decision

    async def match_line(
        self,
        *,
        organization_id: UUID,
        line: ExtractedCommandLine,
        supplier_scope: str | None = None,
        limit: int = 5,
    ) -> MatchDecision:
        assert organization_id == ORGANIZATION_ID
        assert line.item_reference.value == "AMOX-500"
        return self.decision


class FakeProposals:
    def __init__(self) -> None:
        self.drafts: list[ProposalDraft] = []

    async def create(self, draft: ProposalDraft) -> UUID:
        self.drafts.append(draft)
        return PROPOSAL_ID


class FakeOutbox:
    def __init__(self) -> None:
        self.drafts: list[ProcessingOutcomeDraft] = []

    async def enqueue(self, draft: ProcessingOutcomeDraft) -> UUID:
        self.drafts.append(draft)
        return OUTBOX_ID


def context() -> TelegramTextEventContext:
    return TelegramTextEventContext(
        event_id=EVENT_ID,
        organization_id=ORGANIZATION_ID,
        organization_user_id=MEMBER_ID,
        location_id=LOCATION_ID,
        external_event_id="70004",
        chat_id=-100123,
        telegram_user_id=100000001,
        message_text="received three AMOX-500",
    )


def command(
    *,
    intent: InventoryIntent = InventoryIntent.RECEIVE_STOCK,
    needs_clarification: bool = False,
) -> ExtractedInventoryCommand:
    lines = []
    if intent in {
        InventoryIntent.RECEIVE_STOCK,
        InventoryIntent.ISSUE_STOCK,
        InventoryIntent.ADJUST_STOCK,
    }:
        lines = [
            {
                "source_text": "three AMOX-500",
                "item_reference": {"type": "PART_NUMBER", "value": "AMOX-500"},
                "description": "amoxicillin",
                "quantity": "3",
                "unit": "box",
                "attributes": [{"key": "expiry_date", "value": "2027-06-30"}],
            }
        ]
    return ExtractedInventoryCommand.model_validate(
        {
            "schema_version": "1.0",
            "intent": intent.value,
            "location_hint": None,
            "lines": lines,
            "notes": "delivery",
            "needs_clarification": needs_clarification,
            "clarification_question": "Which item?" if needs_clarification else None,
        }
    )


def candidate() -> InventoryCandidate:
    return InventoryCandidate(
        item_variant_id=VARIANT_ID,
        item_id=ITEM_ID,
        item_name="Amoxicillin 500mg",
        variant_name=None,
        sku="MED-AMOX-500",
        base_unit="box",
        tracking_mode=TrackingMode.LOT,
        match_method=CandidateMatchMethod.EXACT_IDENTIFIER,
        match_score=Decimal("1"),
        match_evidence={"identifier_type": "manufacturer_part_number"},
    )


def processor(
    *,
    events: FakeEvents,
    interpreted: ExtractedInventoryCommand | Exception,
    decision: MatchDecision | None = None,
) -> tuple[TelegramTextEventProcessor, FakeProposals, FakeOutbox]:
    proposals = FakeProposals()
    outbox = FakeOutbox()
    fallback = MatchDecision(
        status=MatchDecisionStatus.NOT_FOUND,
        selected=None,
        candidates=[],
        reason="not found",
    )
    return (
        TelegramTextEventProcessor(
            events=events,
            interpreter=FakeInterpreter(interpreted),
            matcher=FakeMatcher(decision or fallback),
            proposals=proposals,
            outbox=outbox,
        ),
        proposals,
        outbox,
    )


async def test_matched_receive_creates_resolved_proposal_and_ready_outcome() -> None:
    matched = candidate()
    events = FakeEvents(context())
    service, proposals, outbox = processor(
        events=events,
        interpreted=command(),
        decision=MatchDecision(
            status=MatchDecisionStatus.MATCHED,
            selected=matched,
            candidates=[matched],
            reason="exact identifier",
        ),
    )

    result = await service.process(EVENT_ID)

    assert result.status is TextEventProcessingStatus.PROPOSAL_READY
    assert result.proposal_id == PROPOSAL_ID
    draft = proposals.drafts[0]
    assert draft.idempotency_key == "telegram:70004:command:1.0"
    assert draft.lines[0].item_variant_id == VARIANT_ID
    assert draft.lines[0].requested_quantity == Decimal("3")
    assert draft.lines[0].attributes == {"expiry_date": "2027-06-30"}
    assert outbox.drafts[0].aggregate_id == PROPOSAL_ID
    assert events.finishes == [(EVENT_ID, True, None)]


async def test_ambiguous_match_persists_candidate_evidence_without_selecting_item() -> None:
    offered = candidate()
    events = FakeEvents(context())
    service, proposals, _ = processor(
        events=events,
        interpreted=command(),
        decision=MatchDecision(
            status=MatchDecisionStatus.NEEDS_CONFIRMATION,
            selected=None,
            candidates=[offered],
            reason="weak fuzzy evidence",
        ),
    )

    await service.process(EVENT_ID)

    line = proposals.drafts[0].lines[0]
    assert line.item_variant_id is None
    assert line.match_method is None
    assert line.match_evidence["decision"] == "needs_confirmation"
    assert line.match_evidence["candidates"][0]["item_variant_id"] == str(VARIANT_ID)


@pytest.mark.parametrize(
    ("interpreted", "expected_message"),
    [
        (command(intent=InventoryIntent.UNKNOWN, needs_clarification=True), "Which item?"),
        (
            command(intent=InventoryIntent.ADJUST_STOCK),
            "Should I add or remove this quantity, or set the current on-hand "
            "quantity to this number?",
        ),
    ],
)
async def test_unclear_or_adjustment_command_requests_clarification_without_proposal(
    interpreted: ExtractedInventoryCommand,
    expected_message: str,
) -> None:
    events = FakeEvents(context())
    service, proposals, outbox = processor(events=events, interpreted=interpreted)

    result = await service.process(EVENT_ID)

    assert result.status is TextEventProcessingStatus.CLARIFICATION_REQUIRED
    assert proposals.drafts == []
    assert outbox.drafts[0].payload == {"message": expected_message}
    assert events.finishes == [(EVENT_ID, True, None)]


async def test_already_claimed_event_does_not_repeat_work() -> None:
    events = FakeEvents(None)
    service, proposals, outbox = processor(events=events, interpreted=command())

    result = await service.process(EVENT_ID)

    assert result.status is TextEventProcessingStatus.ALREADY_CLAIMED
    assert proposals.drafts == []
    assert outbox.drafts == []
    assert events.finishes == []


async def test_process_next_returns_none_when_worker_is_idle() -> None:
    events = FakeEvents(None)
    service, proposals, outbox = processor(events=events, interpreted=command())

    result = await service.process_next()

    assert result is None
    assert proposals.drafts == []
    assert outbox.drafts == []


async def test_processing_failure_is_recorded_without_provider_error_details() -> None:
    events = FakeEvents(context())
    service, _, _ = processor(
        events=events,
        interpreted=RuntimeError("secret provider response"),
    )

    with pytest.raises(TextEventProcessingError, match="processing failed"):
        await service.process(EVENT_ID)

    assert events.finishes == [(EVENT_ID, False, "RuntimeError: text event processing failed")]
