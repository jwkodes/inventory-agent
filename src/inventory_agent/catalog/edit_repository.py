"""Durable, reviewable catalog metadata edit requests."""

from typing import Protocol
from uuid import UUID

import httpx

from inventory_agent.agent.models import CatalogItemEditArguments
from inventory_agent.catalog.models import CatalogItemEditView


class CatalogItemEditRepository(Protocol):
    async def begin(
        self,
        *,
        variant_id: UUID,
        actor_id: UUID,
        source_event_id: UUID,
        chat_id: int,
        changes: CatalogItemEditArguments,
    ) -> UUID:
        """Create an audited edit request without changing the catalog."""

    async def get_view(self, *, request_id: UUID) -> CatalogItemEditView:
        """Load the retained before/after review."""

    async def find_by_source_event(self, *, source_event_id: UUID) -> UUID | None:
        """Recover a request when replaying a saved agent turn."""

    async def confirm(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        """Apply a pending edit if its before snapshot is still current."""

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        """Cancel a pending edit without changing the catalog."""


class SupabaseCatalogItemEditRepository:
    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rest_url = f"{supabase_url.rstrip('/')}/rest/v1/rpc"
        self._headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def begin(
        self,
        *,
        variant_id: UUID,
        actor_id: UUID,
        source_event_id: UUID,
        chat_id: int,
        changes: CatalogItemEditArguments,
    ) -> UUID:
        result = await self._call(
            "begin_catalog_item_edit",
            {
                "p_item_variant_id": str(variant_id),
                "p_actor_id": str(actor_id),
                "p_source_event_id": str(source_event_id),
                "p_chat_id": chat_id,
                "p_item_name": changes.item_name,
                "p_variant_name": changes.variant_name,
                "p_sku": changes.sku,
                "p_description": changes.description,
                "p_clear_fields": changes.clear_fields,
                "p_item_attribute_changes": {
                    change.key: change.value for change in changes.item_attribute_changes
                },
                "p_variant_attribute_changes": {
                    change.key: change.value for change in changes.variant_attribute_changes
                },
                "p_reason": changes.reason,
            },
        )
        return _required_uuid(result, "catalog edit request")

    async def get_view(self, *, request_id: UUID) -> CatalogItemEditView:
        result = await self._call(
            "get_catalog_item_edit_view",
            {"p_request_id": str(request_id)},
        )
        if result is None:
            raise ValueError("Catalog edit request was not found")
        return CatalogItemEditView.model_validate(result)

    async def find_by_source_event(self, *, source_event_id: UUID) -> UUID | None:
        result = await self._call(
            "find_catalog_item_edit_by_source_event",
            {"p_source_event_id": str(source_event_id)},
        )
        if result is None:
            return None
        return _required_uuid(result, "catalog edit replay")

    async def confirm(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        return _required_uuid(
            await self._call(
                "confirm_catalog_item_edit",
                {"p_request_id": str(request_id), "p_actor_id": str(actor_id)},
            ),
            "confirmed catalog edit",
        )

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        return _required_uuid(
            await self._call(
                "cancel_catalog_item_edit",
                {"p_request_id": str(request_id), "p_actor_id": str(actor_id)},
            ),
            "cancelled catalog edit",
        )

    async def _call(self, function_name: str, body: dict[str, object]) -> object:
        async with httpx.AsyncClient(
            base_url=self._rest_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(f"/{function_name}", json=body)
        response.raise_for_status()
        return response.json()


def _required_uuid(result: object, operation: str) -> UUID:
    if not isinstance(result, str):
        raise ValueError(f"Supabase returned an invalid ID for {operation}")
    return UUID(result)


__all__ = ["CatalogItemEditRepository", "SupabaseCatalogItemEditRepository"]
