"""Supabase adapter for atomic, idempotent proposal creation."""

from typing import Protocol
from uuid import UUID

import httpx

from inventory_agent.proposals.models import ProposalDraft


class ProposalRepository(Protocol):
    async def create(self, draft: ProposalDraft) -> UUID:
        """Create or return an idempotent transaction proposal."""


class SupabaseProposalRepository:
    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rpc_url = f"{supabase_url.rstrip('/')}/rest/v1/rpc/create_inventory_proposal"
        self._headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def create(self, draft: ProposalDraft) -> UUID:
        body = {
            "p_organization_id": str(draft.organization_id),
            "p_location_id": str(draft.location_id),
            "p_source_event_id": str(draft.source_event_id),
            "p_created_by": str(draft.created_by),
            "p_intent": draft.intent.value,
            "p_idempotency_key": draft.idempotency_key,
            "p_raw_command": draft.raw_command,
            "p_model_name": draft.model_name,
            "p_model_response_id": draft.model_response_id,
            "p_prompt_version": draft.prompt_version,
            "p_notes": draft.notes,
            "p_lines": [line.model_dump(mode="json") for line in draft.lines],
        }
        async with httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(self._rpc_url, json=body)
        response.raise_for_status()
        proposal_id = response.json()
        if not isinstance(proposal_id, str):
            raise ValueError("Supabase returned an invalid proposal ID")
        return UUID(proposal_id)
