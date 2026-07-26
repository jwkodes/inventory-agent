"""Supabase adapter for durable catalog resolution and item creation."""

from typing import Protocol
from uuid import UUID

import httpx

from inventory_agent.catalog.models import (
    CatalogBatchCreationView,
    CatalogBatchItemDraft,
    CatalogItemCreationView,
    CatalogItemDetails,
    ExtractedCatalogItemDetails,
)


class CatalogItemConfirmationConflict(ValueError):
    """The item draft was reopened because its SKU is already in use."""

    def __init__(self, *, request_id: UUID, message: str) -> None:
        super().__init__(message)
        self.request_id = request_id


class CatalogBatchConfirmationConflict(ValueError):
    """One or more batch SKUs conflict with another proposed or existing item."""

    def __init__(self, *, batch_id: UUID, message: str) -> None:
        super().__init__(message)
        self.batch_id = batch_id


class CatalogItemCreationRepository(Protocol):
    async def begin(self, *, line_id: UUID, actor_id: UUID, chat_id: int) -> UUID:
        """Begin or resume collection of a new catalog item's details."""

    async def show_existing(self, *, line_id: UUID, actor_id: UUID) -> UUID:
        """Expose ranked fallback candidates and return the proposal ID."""

    async def find_pending(self, *, actor_id: UUID, chat_id: int) -> UUID | None:
        """Find a detail form awaiting text from this actor and chat."""

    async def get_view(self, *, request_id: UUID) -> CatalogItemCreationView:
        """Return suggestions and captured fields for one catalog request."""

    async def save_details(
        self,
        *,
        request_id: UUID,
        event_id: UUID,
        actor_id: UUID,
        details: CatalogItemDetails,
    ) -> UUID:
        """Validate and retain submitted details for final confirmation."""

    async def save_draft(
        self,
        *,
        request_id: UUID,
        event_id: UUID,
        actor_id: UUID,
        details: ExtractedCatalogItemDetails,
    ) -> UUID:
        """Merge partial details while waiting for a clarification reply."""

    async def confirm(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        """Create the catalog item and return the resumed proposal ID."""

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        """Cancel catalog creation and return its request ID."""

    async def begin_batch(self, *, proposal_id: UUID, actor_id: UUID, chat_id: int) -> UUID:
        """Begin catalog creation for every unmatched line in one proposal."""

    async def find_pending_batch(self, *, actor_id: UUID, chat_id: int) -> UUID | None:
        """Find one bulk catalog request awaiting a natural-language reply."""

    async def get_batch_view(self, *, batch_id: UUID) -> CatalogBatchCreationView:
        """Load every retained proposal line and its catalog draft."""

    async def save_batch_draft(
        self,
        *,
        batch_id: UUID,
        event_id: UUID,
        actor_id: UUID,
        items: list[CatalogBatchItemDraft],
    ) -> UUID:
        """Merge one natural reply across all items in the batch."""

    async def confirm_batch(self, *, batch_id: UUID, actor_id: UUID) -> UUID:
        """Atomically create all batch items and return the resumed proposal ID."""

    async def cancel_batch(self, *, batch_id: UUID, actor_id: UUID) -> UUID:
        """Cancel a bulk catalog request."""


class SupabaseCatalogItemCreationRepository:
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

    async def begin(self, *, line_id: UUID, actor_id: UUID, chat_id: int) -> UUID:
        return _required_uuid(
            await self._call(
                "begin_catalog_item_creation",
                {
                    "p_proposal_line_id": str(line_id),
                    "p_actor_id": str(actor_id),
                    "p_chat_id": chat_id,
                },
            ),
            "catalog item request",
        )

    async def show_existing(self, *, line_id: UUID, actor_id: UUID) -> UUID:
        return _required_uuid(
            await self._call(
                "show_existing_inventory_candidates",
                {"p_proposal_line_id": str(line_id), "p_actor_id": str(actor_id)},
            ),
            "existing candidate proposal",
        )

    async def find_pending(self, *, actor_id: UUID, chat_id: int) -> UUID | None:
        result = await self._call(
            "find_pending_catalog_item_creation",
            {"p_actor_id": str(actor_id), "p_chat_id": chat_id},
        )
        if result is None:
            return None
        return _required_uuid(result, "pending catalog item request")

    async def get_view(self, *, request_id: UUID) -> CatalogItemCreationView:
        result = await self._call(
            "get_catalog_item_creation_view",
            {"p_request_id": str(request_id)},
        )
        return CatalogItemCreationView.model_validate(result)

    async def save_details(
        self,
        *,
        request_id: UUID,
        event_id: UUID,
        actor_id: UUID,
        details: CatalogItemDetails,
    ) -> UUID:
        return _required_uuid(
            await self._call(
                "save_catalog_item_creation_details",
                {
                    "p_request_id": str(request_id),
                    "p_event_id": str(event_id),
                    "p_actor_id": str(actor_id),
                    "p_name": details.name,
                    "p_sku": details.sku,
                    "p_base_unit": details.base_unit,
                    "p_tracking_mode": details.tracking_mode.value,
                    "p_attributes": details.attributes,
                },
            ),
            "catalog item details",
        )

    async def save_draft(
        self,
        *,
        request_id: UUID,
        event_id: UUID,
        actor_id: UUID,
        details: ExtractedCatalogItemDetails,
    ) -> UUID:
        return _required_uuid(
            await self._call(
                "save_catalog_item_creation_draft",
                {
                    "p_request_id": str(request_id),
                    "p_event_id": str(event_id),
                    "p_actor_id": str(actor_id),
                    "p_name": details.name,
                    "p_sku": details.sku,
                    "p_base_unit": details.base_unit,
                    "p_tracking_mode": (
                        details.tracking_mode.value if details.tracking_mode is not None else None
                    ),
                    "p_attributes": {
                        attribute.key: attribute.value for attribute in details.attributes
                    },
                },
            ),
            "catalog item draft",
        )

    async def confirm(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        preparation = await self._call(
            "prepare_catalog_item_creation_confirmation",
            {"p_request_id": str(request_id), "p_actor_id": str(actor_id)},
        )
        if not isinstance(preparation, dict) or not isinstance(preparation.get("ready"), bool):
            raise ValueError("Supabase returned an invalid catalog confirmation preparation")
        if preparation["ready"] is False:
            message = preparation.get("message")
            raise CatalogItemConfirmationConflict(
                request_id=request_id,
                message=(
                    message
                    if isinstance(message, str) and message.strip()
                    else "The proposed SKU is already in use. Reply with a different SKU."
                ),
            )
        return _required_uuid(
            await self._call(
                "confirm_catalog_item_creation",
                {"p_request_id": str(request_id), "p_actor_id": str(actor_id)},
            ),
            "resumed proposal",
        )

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        return _required_uuid(
            await self._call(
                "cancel_catalog_item_creation",
                {"p_request_id": str(request_id), "p_actor_id": str(actor_id)},
            ),
            "cancelled catalog item request",
        )

    async def begin_batch(self, *, proposal_id: UUID, actor_id: UUID, chat_id: int) -> UUID:
        return _required_uuid(
            await self._call(
                "begin_catalog_batch_creation",
                {
                    "p_proposal_id": str(proposal_id),
                    "p_actor_id": str(actor_id),
                    "p_chat_id": chat_id,
                },
            ),
            "catalog batch request",
        )

    async def find_pending_batch(self, *, actor_id: UUID, chat_id: int) -> UUID | None:
        result = await self._call(
            "find_pending_catalog_batch_creation",
            {"p_actor_id": str(actor_id), "p_chat_id": chat_id},
        )
        return UUID(result) if isinstance(result, str) else None

    async def get_batch_view(self, *, batch_id: UUID) -> CatalogBatchCreationView:
        result = await self._call(
            "get_catalog_batch_creation_view",
            {"p_batch_id": str(batch_id)},
        )
        return CatalogBatchCreationView.model_validate(result)

    async def save_batch_draft(
        self,
        *,
        batch_id: UUID,
        event_id: UUID,
        actor_id: UUID,
        items: list[CatalogBatchItemDraft],
    ) -> UUID:
        return _required_uuid(
            await self._call(
                "save_catalog_batch_creation_draft",
                {
                    "p_batch_id": str(batch_id),
                    "p_event_id": str(event_id),
                    "p_actor_id": str(actor_id),
                    "p_items": [item.model_dump(mode="json") for item in items],
                },
            ),
            "catalog batch draft",
        )

    async def confirm_batch(self, *, batch_id: UUID, actor_id: UUID) -> UUID:
        result = await self._call(
            "confirm_catalog_batch_creation",
            {"p_batch_id": str(batch_id), "p_actor_id": str(actor_id)},
        )
        if isinstance(result, dict) and result.get("ready") is False:
            message = result.get("message")
            raise CatalogBatchConfirmationConflict(
                batch_id=batch_id,
                message=(
                    message
                    if isinstance(message, str) and message.strip()
                    else "One or more proposed SKUs are unavailable."
                ),
            )
        if not isinstance(result, dict) or result.get("ready") is not True:
            raise ValueError("Supabase returned an invalid catalog batch confirmation")
        return _required_uuid(result.get("proposal_id"), "resumed catalog batch proposal")

    async def cancel_batch(self, *, batch_id: UUID, actor_id: UUID) -> UUID:
        return _required_uuid(
            await self._call(
                "cancel_catalog_batch_creation",
                {"p_batch_id": str(batch_id), "p_actor_id": str(actor_id)},
            ),
            "cancelled catalog batch request",
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


__all__ = [
    "CatalogBatchConfirmationConflict",
    "CatalogItemConfirmationConflict",
    "CatalogItemCreationRepository",
    "SupabaseCatalogItemCreationRepository",
]
