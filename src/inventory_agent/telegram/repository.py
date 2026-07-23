"""Persistence boundary for idempotent Telegram source-event ingestion."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import httpx

from inventory_agent.telegram.models import TelegramPayload


@dataclass(frozen=True, slots=True)
class OrganizationMember:
    """Active organization membership resolved from a Telegram user ID."""

    id: UUID
    organization_id: UUID


class EventIngestionResult(StrEnum):
    """Outcome of the insert protected by the event's unique provider key."""

    CREATED = "accepted"
    DUPLICATE = "duplicate"


class TelegramEventRepository(Protocol):
    """Operations the webhook needs without exposing database client details."""

    async def find_active_members(self, telegram_user_id: int) -> list[OrganizationMember]:
        """Find active memberships for a Telegram user."""

    async def ingest_event(
        self,
        *,
        member: OrganizationMember,
        update_id: int,
        event_type: str,
        payload: TelegramPayload,
    ) -> EventIngestionResult:
        """Insert an event once, returning duplicate for a previously seen update."""


class SupabaseTelegramEventRepository:
    """Use Supabase's PostgREST API with the server-only secret key."""

    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rest_url = f"{supabase_url.rstrip('/')}/rest/v1"
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._headers = {
            "apikey": secret_key,
            "Authorization": f"Bearer {secret_key}",
        }

    async def find_active_members(self, telegram_user_id: int) -> list[OrganizationMember]:
        """Return all active memberships instead of choosing an organization arbitrarily."""

        params = {
            "select": "id,organization_id",
            "telegram_user_id": f"eq.{telegram_user_id}",
            "active": "eq.true",
            "limit": "2",
        }
        async with self._client() as client:
            response = await client.get("/organization_users", params=params)
        response.raise_for_status()

        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("Supabase returned an invalid organization membership response")

        return [
            OrganizationMember(
                id=UUID(str(row["id"])),
                organization_id=UUID(str(row["organization_id"])),
            )
            for row in rows
        ]

    async def ingest_event(
        self,
        *,
        member: OrganizationMember,
        update_id: int,
        event_type: str,
        payload: TelegramPayload,
    ) -> EventIngestionResult:
        """Insert with ON CONFLICT DO NOTHING so concurrent retries remain safe."""

        params = {"on_conflict": "provider,external_event_id"}
        headers = {"Prefer": "resolution=ignore-duplicates,return=representation"}
        body = {
            "organization_id": str(member.organization_id),
            "provider": "telegram",
            "external_event_id": str(update_id),
            "event_type": event_type,
            "payload": payload,
        }
        async with self._client() as client:
            response = await client.post(
                "/source_events",
                params=params,
                headers=headers,
                json=body,
            )
        response.raise_for_status()

        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("Supabase returned an invalid source event response")
        return EventIngestionResult.CREATED if rows else EventIngestionResult.DUPLICATE

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._rest_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        )
