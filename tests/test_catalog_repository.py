"""Contract tests for Supabase catalog creation RPC calls."""

from uuid import UUID

import httpx

from inventory_agent.catalog.models import (
    CatalogBatchCreationView,
    CatalogBatchItemDraft,
    CatalogItemCreationView,
    CatalogItemDetails,
    ExtractedCatalogItemDetails,
)
from inventory_agent.catalog.repository import (
    CatalogItemConfirmationConflict,
    SupabaseCatalogItemCreationRepository,
)

ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
LINE_ID = UUID("41000000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("71000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000001")
BATCH_ID = UUID("72000000-0000-0000-0000-000000000001")
TRANSACTION_ID = UUID("60000000-0000-0000-0000-000000000001")


async def test_catalog_repository_maps_resolution_and_creation_rpcs() -> None:
    responses = {
        "begin_catalog_item_creation": str(REQUEST_ID),
        "show_existing_inventory_candidates": str(PROPOSAL_ID),
        "create_catalog_item_from_agent_preview": {
            "status": "completed",
            "result_id": str(PROPOSAL_ID),
        },
        "find_pending_catalog_item_creation": str(REQUEST_ID),
        "get_catalog_item_creation_view": {
            "request_id": str(REQUEST_ID),
            "status": "awaiting_details",
            "suggested_name": "Purple Widget",
            "suggested_sku": None,
            "suggested_base_unit": "each",
            "suggested_tracking_mode": "simple",
            "name": None,
            "sku": None,
            "base_unit": None,
            "tracking_mode": None,
            "attributes": {},
        },
        "save_catalog_item_creation_draft": str(REQUEST_ID),
        "save_catalog_item_creation_details": str(REQUEST_ID),
        "prepare_catalog_item_creation_confirmation": {"ready": True},
        "confirm_catalog_item_creation": str(PROPOSAL_ID),
        "cancel_catalog_item_creation": str(REQUEST_ID),
        "begin_catalog_batch_creation": str(BATCH_ID),
        "find_pending_catalog_batch_creation": str(BATCH_ID),
        "get_catalog_batch_creation_view": {
            "batch_id": str(BATCH_ID),
            "proposal_id": str(PROPOSAL_ID),
            "status": "awaiting_details",
            "items": [],
        },
        "save_catalog_batch_creation_draft": str(BATCH_ID),
        "confirm_catalog_batch_and_apply_inventory": {
            "ready": True,
            "proposal_id": str(PROPOSAL_ID),
            "transaction_id": str(TRANSACTION_ID),
        },
        "cancel_catalog_batch_and_proposal": str(BATCH_ID),
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.path.rsplit("/", 1)[-1]])

    repository = SupabaseCatalogItemCreationRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    assert await repository.begin(line_id=LINE_ID, actor_id=ACTOR_ID, chat_id=-100123) == REQUEST_ID
    assert await repository.show_existing(line_id=LINE_ID, actor_id=ACTOR_ID) == PROPOSAL_ID
    preview_result = await repository.create_from_preview(
        line_id=LINE_ID,
        actor_id=ACTOR_ID,
        chat_id=-100123,
    )
    assert preview_result.status == "completed"
    assert preview_result.result_id == PROPOSAL_ID
    assert await repository.find_pending(actor_id=ACTOR_ID, chat_id=-100123) == REQUEST_ID
    assert await repository.get_view(
        request_id=REQUEST_ID
    ) == CatalogItemCreationView.model_validate(responses["get_catalog_item_creation_view"])
    assert (
        await repository.save_draft(
            request_id=REQUEST_ID,
            event_id=EVENT_ID,
            actor_id=ACTOR_ID,
            details=ExtractedCatalogItemDetails(
                applies_to_pending_request=True,
                name="Purple Widget",
                sku=None,
                base_unit="each",
                tracking_mode=None,
                attributes=[],
            ),
        )
        == REQUEST_ID
    )
    assert (
        await repository.save_details(
            request_id=REQUEST_ID,
            event_id=EVENT_ID,
            actor_id=ACTOR_ID,
            details=CatalogItemDetails(
                name="Purple Widget",
                sku="ZX-999",
                base_unit="each",
                tracking_mode="simple",
                attributes={"colour": "purple"},
            ),
        )
        == REQUEST_ID
    )
    assert await repository.confirm(request_id=REQUEST_ID, actor_id=ACTOR_ID) == PROPOSAL_ID
    assert await repository.cancel(request_id=REQUEST_ID, actor_id=ACTOR_ID) == REQUEST_ID
    assert (
        await repository.begin_batch(
            proposal_id=PROPOSAL_ID,
            actor_id=ACTOR_ID,
            chat_id=-100123,
        )
        == BATCH_ID
    )
    assert (
        await repository.find_pending_batch(
            actor_id=ACTOR_ID,
            chat_id=-100123,
        )
        == BATCH_ID
    )
    assert await repository.get_batch_view(
        batch_id=BATCH_ID
    ) == CatalogBatchCreationView.model_validate(responses["get_catalog_batch_creation_view"])
    assert (
        await repository.save_batch_draft(
            batch_id=BATCH_ID,
            event_id=EVENT_ID,
            actor_id=ACTOR_ID,
            items=[
                CatalogBatchItemDraft(
                    request_id=REQUEST_ID,
                    name="Purple Widget",
                    sku="ZX-999",
                    base_unit="each",
                    tracking_mode="simple",
                )
            ],
        )
        == BATCH_ID
    )
    assert (
        await repository.confirm_batch(
            batch_id=BATCH_ID,
            actor_id=ACTOR_ID,
        )
        == TRANSACTION_ID
    )
    assert (
        await repository.cancel_batch(
            batch_id=BATCH_ID,
            actor_id=ACTOR_ID,
        )
        == BATCH_ID
    )


async def test_catalog_repository_surfaces_recoverable_duplicate_sku_conflict() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/prepare_catalog_item_creation_confirmation")
        return httpx.Response(
            200,
            json={
                "ready": False,
                "request_id": str(REQUEST_ID),
                "message": "SKU HAC-001 is already used by the blue variant.",
            },
        )

    repository = SupabaseCatalogItemCreationRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    try:
        await repository.confirm(request_id=REQUEST_ID, actor_id=ACTOR_ID)
    except CatalogItemConfirmationConflict as error:
        assert error.request_id == REQUEST_ID
        assert "HAC-001" in str(error)
    else:
        raise AssertionError("duplicate SKU should reopen catalog detail collection")
