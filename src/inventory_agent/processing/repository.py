"""Supabase adapters for atomic event claims, completion, and outbox handoff."""

from typing import Protocol
from uuid import UUID

import httpx

from inventory_agent.processing.models import (
    ProcessingOutcomeDraft,
    TelegramTextEventContext,
)


class SourceEventWorkRepository(Protocol):
    async def claim_text_event(self, event_id: UUID) -> TelegramTextEventContext | None:
        """Claim one received event, or return None when it is no longer claimable."""

    async def finish_event(
        self,
        *,
        event_id: UUID,
        success: bool,
        error_message: str | None = None,
    ) -> bool:
        """Mark a claimed event processed or failed."""


class ProcessingOutboxRepository(Protocol):
    async def enqueue(self, draft: ProcessingOutcomeDraft) -> UUID:
        """Idempotently enqueue an outcome for outbound delivery."""


class SupabaseSourceEventWorkRepository:
    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rest_url = f"{supabase_url.rstrip('/')}/rest/v1"
        self._headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def claim_text_event(self, event_id: UUID) -> TelegramTextEventContext | None:
        response = await self._post_rpc(
            "claim_telegram_text_event",
            {"p_event_id": str(event_id)},
        )
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("Supabase returned an invalid source event claim response")
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("Supabase returned more than one claimed source event")
        return TelegramTextEventContext.model_validate(rows[0])

    async def finish_event(
        self,
        *,
        event_id: UUID,
        success: bool,
        error_message: str | None = None,
    ) -> bool:
        response = await self._post_rpc(
            "finish_source_event",
            {
                "p_event_id": str(event_id),
                "p_success": success,
                "p_error_message": error_message,
            },
        )
        result = response.json()
        if not isinstance(result, bool):
            raise ValueError("Supabase returned an invalid source event completion response")
        return result

    async def _post_rpc(self, function_name: str, body: dict[str, object]) -> httpx.Response:
        async with httpx.AsyncClient(
            base_url=self._rest_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(f"/rpc/{function_name}", json=body)
        response.raise_for_status()
        return response


class SupabaseProcessingOutboxRepository:
    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rpc_url = f"{supabase_url.rstrip('/')}/rest/v1/rpc/enqueue_processing_outcome"
        self._headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def enqueue(self, draft: ProcessingOutcomeDraft) -> UUID:
        body = {
            "p_organization_id": str(draft.organization_id),
            "p_source_event_id": str(draft.source_event_id),
            "p_outcome_type": draft.outcome_type.value,
            "p_aggregate_id": str(draft.aggregate_id) if draft.aggregate_id else None,
            "p_chat_id": draft.chat_id,
            "p_payload": draft.payload,
        }
        async with httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(self._rpc_url, json=body)
        response.raise_for_status()
        outbox_id = response.json()
        if not isinstance(outbox_id, str):
            raise ValueError("Supabase returned an invalid processing outbox ID")
        return UUID(outbox_id)
