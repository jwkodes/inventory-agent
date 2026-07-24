"""Database actions invoked by authenticated Telegram callbacks."""

from typing import Protocol
from uuid import UUID

import httpx


class ProposalActionRejectedError(RuntimeError):
    """A deterministic proposal action was rejected without changing inventory."""


class ProposalActionRepository(Protocol):
    async def select_variant(self, *, line_id: UUID, variant_id: UUID, actor_id: UUID) -> UUID:
        """Resolve one proposal line and return its proposal ID."""

    async def confirm(self, *, proposal_id: UUID, actor_id: UUID) -> UUID:
        """Apply a proposal and return its inventory transaction ID."""

    async def cancel(self, *, proposal_id: UUID, actor_id: UUID) -> UUID:
        """Cancel a proposal and return its ID."""


class SupabaseProposalActionRepository:
    """Call security-definer proposal action functions through PostgREST."""

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

    async def select_variant(self, *, line_id: UUID, variant_id: UUID, actor_id: UUID) -> UUID:
        return await self._call(
            "resolve_proposal_line",
            {
                "p_proposal_line_id": str(line_id),
                "p_item_variant_id": str(variant_id),
                "p_actor_id": str(actor_id),
            },
        )

    async def confirm(self, *, proposal_id: UUID, actor_id: UUID) -> UUID:
        return await self._call(
            "apply_inventory_proposal",
            {"p_proposal_id": str(proposal_id), "p_actor_id": str(actor_id)},
        )

    async def cancel(self, *, proposal_id: UUID, actor_id: UUID) -> UUID:
        return await self._call(
            "cancel_inventory_proposal",
            {"p_proposal_id": str(proposal_id), "p_actor_id": str(actor_id)},
        )

    async def _call(self, function_name: str, body: dict[str, str]) -> UUID:
        async with httpx.AsyncClient(
            base_url=self._rest_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(f"/{function_name}", json=body)
        if response.status_code in {400, 404, 409}:
            raise ProposalActionRejectedError(f"Supabase rejected proposal action {function_name}")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, str):
            raise ValueError(f"Supabase returned an invalid result for {function_name}")
        return UUID(result)
