"""Tests for the persisted Telegram text-event orchestration."""

from decimal import Decimal
from uuid import UUID

import pytest

from inventory_agent.catalog.interpreter import CatalogDetailsExtractionResult
from inventory_agent.catalog.models import (
    CatalogItemCreationView,
    ExtractedCatalogItemDetails,
)
from inventory_agent.extraction.interpreter import CommandExtractionResult
from inventory_agent.extraction.schema import (
    ExtractedCommandLine,
    ExtractedInventoryCommand,
    InventoryIntent,
)
from inventory_agent.matching.clarification import MatchClarificationView
from inventory_agent.matching.judge import CandidateJudgeOutput
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
REVERSAL_REQUEST_ID = UUID("70000000-0000-0000-0000-000000000004")
CATALOG_REQUEST_ID = UUID("71000000-0000-0000-0000-000000000004")
CLARIFICATION_REQUEST_ID = UUID("72000000-0000-0000-0000-000000000004")


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


class FakeReversals:
    def __init__(self, request_id: UUID | None = None) -> None:
        self.request_id = request_id
        self.reasons: list[tuple[UUID, UUID, int, str]] = []

    async def begin(
        self,
        *,
        transaction_id: UUID,
        actor_id: UUID,
        chat_id: int,
    ) -> UUID:
        raise AssertionError("text processor does not begin reversals")

    async def capture_reason(
        self,
        *,
        event_id: UUID,
        actor_id: UUID,
        chat_id: int,
        reason: str,
    ) -> UUID | None:
        self.reasons.append((event_id, actor_id, chat_id, reason))
        return self.request_id

    async def confirm(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        raise AssertionError("text processor does not confirm reversals")

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        raise AssertionError("text processor does not cancel reversals")


class FakeCatalog:
    def __init__(self, request_id: UUID | None = None) -> None:
        self.request_id = request_id
        self.drafts: list[ExtractedCatalogItemDetails] = []

    async def begin(self, *, line_id: UUID, actor_id: UUID, chat_id: int) -> UUID:
        raise AssertionError("text processor does not begin catalog creation")

    async def show_existing(self, *, line_id: UUID, actor_id: UUID) -> UUID:
        raise AssertionError("text processor does not show candidates")

    async def find_pending(self, *, actor_id: UUID, chat_id: int) -> UUID | None:
        return self.request_id

    async def get_view(self, *, request_id: UUID) -> CatalogItemCreationView:
        assert request_id == self.request_id
        return CatalogItemCreationView(
            request_id=request_id,
            status="awaiting_details",
            suggested_name="Purple Widget",
            suggested_sku=None,
            suggested_base_unit="each",
            suggested_tracking_mode="simple",
        )

    async def save_details(self, **kwargs: object) -> UUID:
        assert self.request_id is not None
        return self.request_id

    async def save_draft(
        self,
        *,
        request_id: UUID,
        event_id: UUID,
        actor_id: UUID,
        details: ExtractedCatalogItemDetails,
    ) -> UUID:
        self.drafts.append(details)
        return request_id

    async def confirm(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        raise AssertionError("text processor does not confirm catalog creation")

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        raise AssertionError("text processor does not cancel catalog creation")


class FakeCatalogInterpreter:
    def __init__(self, details: ExtractedCatalogItemDetails | None = None) -> None:
        self.details = details or ExtractedCatalogItemDetails(
            applies_to_pending_request=True,
            name=None,
            sku="ZX-999",
            base_unit=None,
            tracking_mode=None,
            attributes=[],
        )

    async def interpret(
        self,
        *,
        user_text: str,
        view: CatalogItemCreationView,
    ) -> CatalogDetailsExtractionResult:
        return CatalogDetailsExtractionResult(
            details=self.details,
            response_id="resp_catalog",
            model="gpt-test",
        )


class FakeClarifications:
    def __init__(self, request_id: UUID | None = None) -> None:
        self.request_id = request_id
        self.applied: list[tuple[UUID, str, CandidateJudgeOutput]] = []

    async def begin(
        self,
        *,
        proposal_id: UUID,
        actor_id: UUID,
        chat_id: int,
    ) -> int:
        return 1

    async def find_pending(self, *, actor_id: UUID, chat_id: int) -> UUID | None:
        return self.request_id

    async def get_view(self, *, request_id: UUID) -> MatchClarificationView:
        return MatchClarificationView(
            request_id=request_id,
            proposal_id=PROPOSAL_ID,
            proposal_line_id=UUID("41000000-0000-0000-0000-000000000004"),
            line=command().lines[0],
            question="Which colour is it?",
            accumulated_attributes={},
            clarification_replies=[],
            candidates=[candidate()],
        )

    async def apply(
        self,
        *,
        request_id: UUID,
        event_id: UUID,
        actor_id: UUID,
        user_reply: str,
        judgment: CandidateJudgeOutput,
    ) -> UUID:
        self.applied.append((request_id, user_reply, judgment))
        return PROPOSAL_ID


class FakeCandidateJudge:
    def __init__(self, judgment: CandidateJudgeOutput) -> None:
        self.judgment = judgment
        self.replies: list[str] = []

    async def judge(
        self,
        *,
        line: ExtractedCommandLine,
        candidates: list[InventoryCandidate],
        clarification_replies: list[str] | None = None,
        accumulated_attributes: dict[str, str] | None = None,
    ) -> CandidateJudgeOutput:
        self.replies = clarification_replies or []
        return self.judgment


def context(message_text: str = "received three AMOX-500") -> TelegramTextEventContext:
    return TelegramTextEventContext(
        event_id=EVENT_ID,
        organization_id=ORGANIZATION_ID,
        organization_user_id=MEMBER_ID,
        location_id=LOCATION_ID,
        external_event_id="70004",
        chat_id=-100123,
        telegram_user_id=100000001,
        message_text=message_text,
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
    reversal_request_id: UUID | None = None,
    catalog_request_id: UUID | None = None,
    catalog_details: ExtractedCatalogItemDetails | None = None,
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
            catalog_interpreter=FakeCatalogInterpreter(catalog_details),
            matcher=FakeMatcher(decision or fallback),
            proposals=proposals,
            outbox=outbox,
            reversals=FakeReversals(reversal_request_id),
            catalog=FakeCatalog(catalog_request_id),
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


async def test_pending_reversal_consumes_text_before_model_interpretation() -> None:
    events = FakeEvents(context())
    service, proposals, outbox = processor(
        events=events,
        interpreted=AssertionError("OpenAI must not run for a pending reversal reason"),
        reversal_request_id=REVERSAL_REQUEST_ID,
    )

    result = await service.process(EVENT_ID)

    assert result.status is TextEventProcessingStatus.REVERSAL_CONFIRMATION
    assert result.reversal_request_id == REVERSAL_REQUEST_ID
    assert proposals.drafts == []
    assert outbox.drafts[0].aggregate_id == REVERSAL_REQUEST_ID
    assert outbox.drafts[0].payload == {"reason": "received three AMOX-500"}
    assert events.finishes == [(EVENT_ID, True, None)]


async def test_pending_catalog_request_consumes_details_before_model() -> None:
    events = FakeEvents(context("Call it Purple Widget; use ZX-999 and count each one."))
    service, proposals, outbox = processor(
        events=events,
        interpreted=AssertionError("OpenAI must not run for catalog details"),
        catalog_request_id=CATALOG_REQUEST_ID,
    )

    result = await service.process_next()

    assert result is not None
    assert result.status is TextEventProcessingStatus.CATALOG_ITEM_CONFIRMATION
    assert result.catalog_request_id == CATALOG_REQUEST_ID
    assert proposals.drafts == []
    assert outbox.drafts[0].outcome_type.value == "catalog_item_confirmation"
    assert events.finishes == [(EVENT_ID, True, None)]


async def test_pending_catalog_request_asks_only_for_missing_information() -> None:
    events = FakeEvents(context("It is counted individually."))
    service, proposals, outbox = processor(
        events=events,
        interpreted=AssertionError("command extraction must not run"),
        catalog_request_id=CATALOG_REQUEST_ID,
        catalog_details=ExtractedCatalogItemDetails(
            applies_to_pending_request=True,
            name=None,
            sku=None,
            base_unit="each",
            tracking_mode=None,
            attributes=[],
        ),
    )

    result = await service.process_next()

    assert result is not None
    assert result.status is TextEventProcessingStatus.CLARIFICATION_REQUIRED
    assert proposals.drafts == []
    assert outbox.drafts[0].payload == {
        "message": (
            "❓ **Reply with the missing catalog information**\n"
            "Please send SKU or internal product code in any format."
        )
    }


async def test_pending_match_clarification_consumes_reply_and_resumes_proposal() -> None:
    events = FakeEvents(context("It is the red one."))
    proposals = FakeProposals()
    outbox = FakeOutbox()
    clarifications = FakeClarifications(CLARIFICATION_REQUEST_ID)
    judge = FakeCandidateJudge(
        CandidateJudgeOutput(
            action="SELECT",
            selected_candidate_id=VARIANT_ID,
            question=None,
            reason="The reply identifies the red variant.",
            resolved_attributes=[{"key": "colour", "value": "red"}],
        )
    )
    service = TelegramTextEventProcessor(
        events=events,
        interpreter=FakeInterpreter(
            AssertionError("command extraction must not run during clarification")
        ),
        catalog_interpreter=FakeCatalogInterpreter(),
        matcher=FakeMatcher(
            MatchDecision(
                status=MatchDecisionStatus.NOT_FOUND,
                selected=None,
                candidates=[],
                reason="unused",
            )
        ),
        proposals=proposals,
        outbox=outbox,
        reversals=FakeReversals(),
        catalog=FakeCatalog(),
        clarifications=clarifications,
        candidate_judge=judge,
    )

    result = await service.process_next()

    assert result is not None
    assert result.status is TextEventProcessingStatus.PROPOSAL_READY
    assert result.proposal_id == PROPOSAL_ID
    assert judge.replies == ["It is the red one."]
    assert clarifications.applied[0][1] == "It is the red one."
    assert outbox.drafts[0].aggregate_id == PROPOSAL_ID
    assert events.finishes == [(EVENT_ID, True, None)]
