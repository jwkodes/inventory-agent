"""Bulk catalog creation preserves invoice facts and collects identifiers once."""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from inventory_agent.catalog.batch import (
    CatalogBatchExtractionResult,
    OpenAICatalogBatchDetailsInterpreter,
    merge_catalog_batch_details,
)
from inventory_agent.catalog.models import (
    CatalogBatchCreationView,
    ExtractedCatalogBatchDetails,
    ExtractedCatalogBatchItemDetails,
)
from inventory_agent.processing.catalog_batches import CatalogBatchReplyHandler
from inventory_agent.processing.models import (
    ProcessingOutcomeDraft,
    ProcessingOutcomeType,
    TelegramTextEventContext,
)
from inventory_agent.telegram.callbacks import CallbackAction, decode_callback
from inventory_agent.telegram.confirmation import (
    render_catalog_batch_confirmation,
    render_catalog_batch_details_prompt,
)

BATCH_ID = UUID("72000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000001")
REQUEST_1 = UUID("71000000-0000-0000-0000-000000000001")
REQUEST_2 = UUID("71000000-0000-0000-0000-000000000002")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000001")
ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
LOCATION_ID = UUID("30000000-0000-0000-0000-000000000001")
OUTBOX_ID = UUID("60000000-0000-0000-0000-000000000001")


def batch_view(*, complete: bool = False) -> CatalogBatchCreationView:
    return CatalogBatchCreationView(
        batch_id=BATCH_ID,
        proposal_id=PROPOSAL_ID,
        status="awaiting_confirmation" if complete else "awaiting_details",
        items=[
            {
                "request_id": str(REQUEST_1),
                "line_number": 1,
                "requested_quantity": "1",
                "requested_unit": "PCS",
                "suggested_name": '2W-10 DC24V N/C G3/8"',
                "suggested_sku": None,
                "suggested_base_unit": "each",
                "suggested_tracking_mode": "simple",
                "name": '2W-10 DC24V N/C G3/8"',
                "sku": "2W10-24-NC-38" if complete else None,
                "base_unit": "each",
                "tracking_mode": "simple",
                "attributes": {},
            },
            {
                "request_id": str(REQUEST_2),
                "line_number": 2,
                "requested_quantity": "4",
                "requested_unit": "PCS",
                "suggested_name": '2W-25 DC24V N/C G1"',
                "suggested_sku": None,
                "suggested_base_unit": "each",
                "suggested_tracking_mode": "simple",
                "name": '2W-25 DC24V N/C G1"',
                "sku": "2W25-24-NC-G1" if complete else None,
                "base_unit": "each",
                "tracking_mode": "simple",
                "attributes": {},
            },
        ],
    )


def test_batch_prompt_retains_quantities_and_requests_identifiers_once() -> None:
    message = render_catalog_batch_details_prompt(batch_view())

    assert "1. 1 PCS" in message.text
    assert "2. 4 PCS" in message.text
    assert message.text.count("SKU needed") == 2
    assert "Quantities will not be changed" in message.text
    assert (
        decode_callback(message.inline_keyboard[0][0].callback_data).action
        is CallbackAction.CANCEL_CATALOG_BATCH
    )


def test_batch_confirmation_creates_every_item_behind_one_button() -> None:
    message = render_catalog_batch_confirmation(batch_view(complete=True))

    assert "Create 2 new products" in message.text
    assert "2W10-24-NC-38" in message.text
    assert "2W25-24-NC-G1" in message.text
    actions = [
        decode_callback(button.callback_data).action for button in message.inline_keyboard[0]
    ]
    assert actions == [
        CallbackAction.CONFIRM_CATALOG_BATCH,
        CallbackAction.CANCEL_CATALOG_BATCH,
    ]


def test_batch_merge_keeps_invoice_quantities_outside_catalog_details() -> None:
    view = batch_view()
    extracted = ExtractedCatalogBatchDetails(
        applies_to_pending_request=True,
        items=[
            ExtractedCatalogBatchItemDetails(
                line_number=1,
                name=None,
                sku="2W10-24-NC-38",
                base_unit=None,
                tracking_mode=None,
                attributes=[],
            ),
            ExtractedCatalogBatchItemDetails(
                line_number=2,
                name=None,
                sku="2W25-24-NC-G1",
                base_unit=None,
                tracking_mode=None,
                attributes=[],
            ),
        ],
    )

    drafts, missing = merge_catalog_batch_details(extracted=extracted, view=view)

    assert missing == []
    assert [draft.sku for draft in drafts] == ["2W10-24-NC-38", "2W25-24-NC-G1"]
    assert [item.requested_quantity for item in view.items] == [
        Decimal("1"),
        Decimal("4"),
    ]


class FakeResponses:
    def __init__(self, parsed: ExtractedCatalogBatchDetails) -> None:
        self.parsed = parsed
        self.arguments: dict[str, Any] = {}

    async def parse(self, **kwargs: Any) -> object:
        self.arguments = kwargs
        return SimpleNamespace(
            output_parsed=self.parsed,
            output=[],
            id="batch-response",
            model="gpt-test",
        )


class FakeOpenAI:
    def __init__(self, parsed: ExtractedCatalogBatchDetails) -> None:
        self.responses = FakeResponses(parsed)


async def test_batch_interpreter_receives_all_retained_lines_in_one_call() -> None:
    parsed = ExtractedCatalogBatchDetails(
        applies_to_pending_request=True,
        items=[],
    )
    client = FakeOpenAI(parsed)
    interpreter = OpenAICatalogBatchDetailsInterpreter(
        client=client,  # type: ignore[arg-type]
        model="gpt-test",
    )

    result = await interpreter.interpret(
        user_text="Generate unique internal SKUs from the descriptions.",
        view=batch_view(),
    )

    assert result.details == parsed
    model_input = client.responses.arguments["input"]
    assert '"line_number":1' in model_input
    assert '"quantity_retained_by_inventory":"1"' in model_input
    assert '"line_number":2' in model_input


class FakeCatalog:
    def __init__(self) -> None:
        self.saved: list[object] = []
        self.complete = False

    async def find_pending_batch(self, *, actor_id: UUID, chat_id: int) -> UUID | None:
        return BATCH_ID

    async def get_batch_view(self, *, batch_id: UUID) -> CatalogBatchCreationView:
        return batch_view(complete=self.complete)

    async def save_batch_draft(self, **kwargs: object) -> UUID:
        self.saved.append(kwargs)
        self.complete = True
        return BATCH_ID


class CompletingInterpreter:
    async def interpret(
        self,
        *,
        user_text: str,
        view: CatalogBatchCreationView,
    ) -> CatalogBatchExtractionResult:
        return CatalogBatchExtractionResult(
            details=ExtractedCatalogBatchDetails(
                applies_to_pending_request=True,
                items=[
                    ExtractedCatalogBatchItemDetails(
                        line_number=1,
                        name=None,
                        sku="2W10-24-NC-38",
                        base_unit=None,
                        tracking_mode=None,
                        attributes=[],
                    ),
                    ExtractedCatalogBatchItemDetails(
                        line_number=2,
                        name=None,
                        sku="2W25-24-NC-G1",
                        base_unit=None,
                        tracking_mode=None,
                        attributes=[],
                    ),
                ],
            ),
            response_id="batch-response",
            model="gpt-test",
        )


class FakeOutbox:
    def __init__(self) -> None:
        self.drafts: list[ProcessingOutcomeDraft] = []

    async def enqueue(self, draft: ProcessingOutcomeDraft) -> UUID:
        self.drafts.append(draft)
        return OUTBOX_ID


async def test_batch_reply_produces_one_combined_confirmation() -> None:
    catalog = FakeCatalog()
    outbox = FakeOutbox()
    handler = CatalogBatchReplyHandler(
        catalog=catalog,  # type: ignore[arg-type]
        interpreter=CompletingInterpreter(),
        outbox=outbox,
    )
    context = TelegramTextEventContext(
        event_id=EVENT_ID,
        organization_id=ORGANIZATION_ID,
        organization_user_id=ACTOR_ID,
        location_id=LOCATION_ID,
        external_event_id="batch-reply",
        chat_id=-100123,
        telegram_user_id=123,
        message_text="Generate unique internal SKUs.",
    )

    result = await handler.handle_pending(context=context)

    assert result is not None
    assert result.catalog_batch_id == BATCH_ID
    assert outbox.drafts[0].outcome_type is ProcessingOutcomeType.CATALOG_BATCH_CONFIRMATION
    assert len(catalog.saved) == 1
