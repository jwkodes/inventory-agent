"""Supabase adapter for reversal conversation state and actions."""

from typing import Protocol
from uuid import UUID

import httpx


class ReversalRepository(Protocol):
    async def begin(
        self,
        *,
        transaction_id: UUID,
        actor_id: UUID,
        chat_id: int,
    ) -> UUID:
        """Start reason collection and return the durable request ID."""

    async def capture_reason(
        self,
        *,
        event_id: UUID,
        actor_id: UUID,
        chat_id: int,
        reason: str,
    ) -> UUID | None:
        """Consume text as a pending reason, or return None when no request is waiting."""

    async def confirm(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        """Apply a pending request and return the compensating transaction ID."""

    async def attach_replacement(
        self,
        *,
        request_id: UUID,
        proposal_id: UUID,
        actor_id: UUID,
    ) -> UUID:
        """Link a grounded corrected proposal to a pending reversal."""

    async def get_completed_replacement(
        self,
        *,
        request_id: UUID,
        actor_id: UUID,
    ) -> UUID | None:
        """Return the pending replacement after its reversal completes."""

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        """Cancel a pending request and return its ID."""


class SupabaseReversalRepository:
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
        transaction_id: UUID,
        actor_id: UUID,
        chat_id: int,
    ) -> UUID:
        result = await self._call(
            "begin_transaction_reversal_request",
            {
                "p_transaction_id": str(transaction_id),
                "p_actor_id": str(actor_id),
                "p_chat_id": chat_id,
            },
        )
        return _required_uuid(result, "reversal request")

    async def capture_reason(
        self,
        *,
        event_id: UUID,
        actor_id: UUID,
        chat_id: int,
        reason: str,
    ) -> UUID | None:
        result = await self._call(
            "capture_transaction_reversal_reason",
            {
                "p_event_id": str(event_id),
                "p_actor_id": str(actor_id),
                "p_chat_id": chat_id,
                "p_reason": reason,
            },
        )
        if result is None:
            return None
        return _required_uuid(result, "reversal reason")

    async def confirm(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        result = await self._call(
            "confirm_transaction_reversal_request",
            {
                "p_request_id": str(request_id),
                "p_actor_id": str(actor_id),
            },
        )
        return _required_uuid(result, "reversal transaction")

    async def attach_replacement(
        self,
        *,
        request_id: UUID,
        proposal_id: UUID,
        actor_id: UUID,
    ) -> UUID:
        result = await self._call(
            "attach_transaction_reversal_replacement",
            {
                "p_request_id": str(request_id),
                "p_proposal_id": str(proposal_id),
                "p_actor_id": str(actor_id),
            },
        )
        return _required_uuid(result, "reversal replacement proposal")

    async def get_completed_replacement(
        self,
        *,
        request_id: UUID,
        actor_id: UUID,
    ) -> UUID | None:
        result = await self._call(
            "get_completed_reversal_replacement",
            {
                "p_request_id": str(request_id),
                "p_actor_id": str(actor_id),
            },
        )
        if result is None:
            return None
        return _required_uuid(result, "completed reversal replacement proposal")

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> UUID:
        result = await self._call(
            "cancel_transaction_reversal_request",
            {
                "p_request_id": str(request_id),
                "p_actor_id": str(actor_id),
            },
        )
        return _required_uuid(result, "cancelled reversal request")

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
