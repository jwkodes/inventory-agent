"""Contract tests for Supabase catalog creation RPC calls."""

from uuid import UUID

import httpx

from inventory_agent.catalog.models import (
    CatalogItemCreationView,
    CatalogItemDetails,
    ExtractedCatalogItemDetails,
)
from inventory_agent.catalog.repository import SupabaseCatalogItemCreationRepository

ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
LINE_ID = UUID("41000000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("71000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000001")


async def test_catalog_repository_maps_resolution_and_creation_rpcs() -> None:
    responses = {
        "begin_catalog_item_creation": str(REQUEST_ID),
        "show_existing_inventory_candidates": str(PROPOSAL_ID),
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
        "confirm_catalog_item_creation": str(PROPOSAL_ID),
        "cancel_catalog_item_creation": str(REQUEST_ID),
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
