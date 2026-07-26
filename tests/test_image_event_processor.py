"""Tests for durable invoice-image to proposal orchestration."""

from decimal import Decimal
from uuid import UUID

import pytest

from inventory_agent.artifacts.repository import SourceArtifactDraft
from inventory_agent.extraction.interpreter import CommandExtractionResult
from inventory_agent.extraction.schema import (
    ExtractedCommandLine,
    ExtractedInventoryCommand,
    InventoryIntent,
)
from inventory_agent.matching.models import MatchDecision, MatchDecisionStatus
from inventory_agent.processing.commands import InventoryCommandHandler
from inventory_agent.processing.image_events import (
    ImageEventProcessingError,
    TelegramImageEventProcessor,
)
from inventory_agent.processing.models import (
    ImageEventProcessingStatus,
    ProcessingOutcomeDraft,
    TelegramImageEventContext,
)
from inventory_agent.proposals.models import ProposalDraft
from inventory_agent.telegram.client import DownloadedTelegramFile

EVENT_ID = UUID("50000000-0000-0000-0000-000000000011")
ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
MEMBER_ID = UUID("11000000-0000-0000-0000-000000000001")
LOCATION_ID = UUID("12000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000011")
OUTBOX_ID = UUID("60000000-0000-0000-0000-000000000011")
ARTIFACT_ID = UUID("80000000-0000-0000-0000-000000000011")


class FakeEvents:
    def __init__(self, context: TelegramImageEventContext | None) -> None:
        self.context = context
        self.finishes: list[tuple[UUID, bool, str | None]] = []

    async def claim_next_image_event(self) -> TelegramImageEventContext | None:
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


class FakeDownloader:
    async def download_file(
        self,
        *,
        file_id: str,
        expected_size: int | None = None,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> DownloadedTelegramFile:
        assert file_id == "telegram-photo"
        assert expected_size == 9
        return DownloadedTelegramFile(b"jpeg-data", "photos/invoice.jpg")


class FakeArtifacts:
    def __init__(self) -> None:
        self.drafts: list[SourceArtifactDraft] = []

    async def store(self, draft: SourceArtifactDraft) -> UUID:
        self.drafts.append(draft)
        return ARTIFACT_ID


class FakeInterpreter:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure

    async def interpret(
        self,
        *,
        image_bytes: bytes,
        media_type: str,
        caption: str | None = None,
    ) -> CommandExtractionResult:
        if self.failure is not None:
            raise self.failure
        assert image_bytes == b"jpeg-data"
        assert media_type == "image/jpeg"
        assert caption == "delivery"
        return CommandExtractionResult(
            command=ExtractedInventoryCommand.model_validate(
                {
                    "schema_version": "1.0",
                    "intent": "RECEIVE_STOCK",
                    "location_hint": None,
                    "lines": [
                        {
                            "source_text": "ABC-123 3",
                            "item_reference": {
                                "type": "PART_NUMBER",
                                "value": "ABC-123",
                            },
                            "description": "Widget",
                            "quantity": "3",
                            "unit": "box",
                            "attributes": [{"key": "colour", "value": "blue"}],
                        }
                    ],
                    "notes": "invoice",
                    "needs_clarification": False,
                    "clarification_question": None,
                }
            ),
            response_id="resp-image",
            model="gpt-test",
            prompt_version="inventory-invoice-image-v1",
        )


class AmbiguousInvoiceInterpreter(FakeInterpreter):
    async def interpret(
        self,
        *,
        image_bytes: bytes,
        media_type: str,
        caption: str | None = None,
    ) -> CommandExtractionResult:
        result = await super().interpret(
            image_bytes=image_bytes,
            media_type=media_type,
            caption=caption,
        )
        return CommandExtractionResult(
            command=result.command.model_copy(
                update={
                    "intent": InventoryIntent.UNKNOWN,
                    "needs_clarification": True,
                    "clarification_question": (
                        "Should these invoice line items be recorded as received stock?"
                    ),
                }
            ),
            response_id=result.response_id,
            model=result.model,
            prompt_version=result.prompt_version,
        )


class FakeCommandClarifications:
    def __init__(self) -> None:
        self.begun: list[dict[str, object]] = []

    async def begin(self, **kwargs: object) -> UUID:
        self.begun.append(kwargs)
        return UUID("90000000-0000-0000-0000-000000000011")


class FakeMatcher:
    async def match_line(
        self,
        *,
        organization_id: UUID,
        line: ExtractedCommandLine,
        supplier_scope: str | None = None,
        limit: int = 5,
    ) -> MatchDecision:
        assert organization_id == ORGANIZATION_ID
        return MatchDecision(
            status=MatchDecisionStatus.NOT_FOUND,
            selected=None,
            candidates=[],
            reason="not found",
        )


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


def context() -> TelegramImageEventContext:
    return TelegramImageEventContext(
        event_id=EVENT_ID,
        organization_id=ORGANIZATION_ID,
        organization_user_id=MEMBER_ID,
        location_id=LOCATION_ID,
        external_event_id="70011",
        chat_id=100000001,
        telegram_user_id=100000001,
        telegram_file_id="telegram-photo",
        telegram_file_unique_id="unique-photo",
        media_type="image/jpeg",
        file_size=9,
        width=900,
        height=1200,
        caption="delivery",
    )


def processor(
    events: FakeEvents,
    *,
    interpreter: FakeInterpreter | None = None,
    command_clarifications: object | None = None,
) -> tuple[TelegramImageEventProcessor, FakeArtifacts, FakeProposals]:
    artifacts = FakeArtifacts()
    proposals = FakeProposals()
    service = TelegramImageEventProcessor(
        events=events,  # type: ignore[arg-type]
        downloader=FakeDownloader(),
        artifacts=artifacts,
        interpreter=interpreter or FakeInterpreter(),
        commands=InventoryCommandHandler(
            matcher=FakeMatcher(),
            proposals=proposals,
            outbox=FakeOutbox(),
            command_clarifications=command_clarifications,  # type: ignore[arg-type]
        ),
    )
    return service, artifacts, proposals


async def test_image_event_stores_original_then_creates_reviewable_proposal() -> None:
    events = FakeEvents(context())
    service, artifacts, proposals = processor(events)

    result = await service.process_next()

    assert result is not None
    assert result.status is ImageEventProcessingStatus.PROPOSAL_READY
    assert result.proposal_id == PROPOSAL_ID
    assert artifacts.drafts[0].data == b"jpeg-data"
    assert artifacts.drafts[0].storage_path.endswith(".jpg")
    assert len(artifacts.drafts[0].sha256) == 64
    assert proposals.drafts[0].prompt_version == "inventory-invoice-image-v1"
    assert proposals.drafts[0].lines[0].requested_quantity == Decimal("3")
    assert proposals.drafts[0].lines[0].attributes == {"colour": "blue"}
    assert events.finishes == [(EVENT_ID, True, None)]


async def test_ambiguous_invoice_persists_extracted_lines_before_asking_question() -> None:
    events = FakeEvents(context())
    clarifications = FakeCommandClarifications()
    service, _, proposals = processor(
        events,
        interpreter=AmbiguousInvoiceInterpreter(),
        command_clarifications=clarifications,
    )

    result = await service.process_next()

    assert result is not None
    assert result.status is ImageEventProcessingStatus.CLARIFICATION_REQUIRED
    assert proposals.drafts == []
    begun = clarifications.begun[0]
    extraction = begun["extraction"]
    assert isinstance(extraction, CommandExtractionResult)
    assert extraction.command.lines[0].item_reference.value == "ABC-123"
    assert extraction.command.lines[0].quantity == "3"
    assert begun["question"] == ("Should these invoice line items be recorded as received stock?")
    assert events.finishes == [(EVENT_ID, True, None)]


async def test_image_failure_is_sanitized_and_recorded_for_retry() -> None:
    events = FakeEvents(context())
    service, artifacts, proposals = processor(
        events,
        interpreter=FakeInterpreter(RuntimeError("provider secret")),
    )

    with pytest.raises(ImageEventProcessingError, match="processing failed"):
        await service.process_next()

    assert artifacts.drafts
    assert proposals.drafts == []
    assert events.finishes == [(EVENT_ID, False, "RuntimeError: image event processing failed")]


async def test_image_processor_returns_none_when_idle() -> None:
    service, artifacts, proposals = processor(FakeEvents(None))

    assert await service.process_next() is None
    assert artifacts.drafts == []
    assert proposals.drafts == []
