"""Supabase adapter for the database candidate-search function."""

from decimal import Decimal
from typing import Protocol
from uuid import UUID

import httpx

from inventory_agent.extraction.schema import ItemReferenceType
from inventory_agent.matching.models import InventoryCandidate


class InventoryCandidateRepository(Protocol):
    async def find_candidates(
        self,
        *,
        organization_id: UUID,
        query: str,
        reference_type: ItemReferenceType,
        supplier_scope: str | None = None,
        limit: int = 5,
    ) -> list[InventoryCandidate]:
        """Return ranked candidates from one organization's catalog."""


class SupabaseInventoryCandidateRepository:
    """Call the matching RPC through Supabase's server-side PostgREST API."""

    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rpc_url = f"{supabase_url.rstrip('/')}/rest/v1/rpc/find_inventory_candidates"
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._headers = {
            "apikey": secret_key,
            "Authorization": f"Bearer {secret_key}",
        }

    async def find_candidates(
        self,
        *,
        organization_id: UUID,
        query: str,
        reference_type: ItemReferenceType,
        supplier_scope: str | None = None,
        limit: int = 5,
    ) -> list[InventoryCandidate]:
        body = {
            "p_organization_id": str(organization_id),
            "p_query": query,
            "p_reference_type": reference_type.value,
            "p_supplier_scope": supplier_scope,
            "p_limit": limit,
        }
        async with httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(self._rpc_url, json=body)
        response.raise_for_status()

        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("Supabase returned an invalid inventory candidate response")
        return [
            InventoryCandidate.model_validate(
                {
                    **row,
                    "match_score": Decimal(str(row["match_score"])),
                }
            )
            for row in rows
        ]
