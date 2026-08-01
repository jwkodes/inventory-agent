"""Contract tests for audited catalog edit RPC calls."""

from uuid import UUID

import httpx

from inventory_agent.agent.models import CatalogItemEditArguments
from inventory_agent.catalog.edit_repository import SupabaseCatalogItemEditRepository
from inventory_agent.catalog.models import CatalogItemEditView

ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
VARIANT_ID = UUID("21000000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("73000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000901")


async def test_catalog_edit_repository_maps_review_lifecycle() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    view = {
        "request_id": str(REQUEST_ID),
        "item_variant_id": str(VARIANT_ID),
        "status": "awaiting_confirmation",
        "reason": "Correct the product code and add its brand",
        "before_values": {
            "item_name": "Milo 500g",
            "variant_name": None,
            "sku": "8873",
            "description": None,
            "item_attributes": {},
            "variant_attributes": {},
        },
        "after_values": {
            "item_name": "Milo Chocolate Malt 500g",
            "variant_name": None,
            "sku": "MILO-500",
            "description": "Chocolate malt drink powder",
            "item_attributes": {"brand": "Milo"},
            "variant_attributes": {},
        },
    }
    responses: dict[str, object] = {
        "begin_catalog_item_edit": str(REQUEST_ID),
        "get_catalog_item_edit_view": view,
        "find_catalog_item_edit_by_source_event": str(REQUEST_ID),
        "confirm_catalog_item_edit": str(REQUEST_ID),
        "cancel_catalog_item_edit": str(REQUEST_ID),
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        body = __import__("json").loads(request.content)
        calls.append((name, body))
        return httpx.Response(200, json=responses[name])

    repository = SupabaseCatalogItemEditRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )
    changes = CatalogItemEditArguments(
        variant_id=str(VARIANT_ID),
        item_name="Milo Chocolate Malt 500g",
        variant_name=None,
        sku="MILO-500",
        description="Chocolate malt drink powder",
        clear_fields=[],
        item_attribute_changes=[{"key": "brand", "value": "Milo"}],
        variant_attribute_changes=[{"key": "obsolete", "value": None}],
        reason="Correct the product code and add its brand",
    )

    assert (
        await repository.begin(
            variant_id=VARIANT_ID,
            actor_id=ACTOR_ID,
            source_event_id=EVENT_ID,
            chat_id=123,
            changes=changes,
        )
        == REQUEST_ID
    )
    assert await repository.get_view(request_id=REQUEST_ID) == CatalogItemEditView.model_validate(
        view
    )
    assert await repository.find_by_source_event(source_event_id=EVENT_ID) == REQUEST_ID
    assert await repository.confirm(request_id=REQUEST_ID, actor_id=ACTOR_ID) == REQUEST_ID
    assert await repository.cancel(request_id=REQUEST_ID, actor_id=ACTOR_ID) == REQUEST_ID

    begin_body = calls[0][1]
    assert begin_body["p_item_attribute_changes"] == {"brand": "Milo"}
    assert begin_body["p_variant_attribute_changes"] == {"obsolete": None}


async def test_catalog_edit_replay_lookup_can_return_none() -> None:
    repository = SupabaseCatalogItemEditRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"null")),
    )

    assert await repository.find_by_source_event(source_event_id=EVENT_ID) is None
