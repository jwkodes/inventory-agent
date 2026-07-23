"""Tests for the Supabase PostgREST Telegram event adapter."""

import json
from uuid import UUID

import httpx

from inventory_agent.telegram.repository import (
    EventIngestionResult,
    OrganizationMember,
    SupabaseTelegramEventRepository,
)

MEMBER = OrganizationMember(
    id=UUID("11000000-0000-0000-0000-000000000001"),
    organization_id=UUID("10000000-0000-0000-0000-000000000001"),
)


async def test_active_membership_query_is_scoped_to_telegram_user() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/organization_users"
        assert request.url.params["telegram_user_id"] == "eq.100000001"
        assert request.url.params["active"] == "eq.true"
        assert request.headers["apikey"] == "test-secret"
        return httpx.Response(
            200,
            json=[{"id": str(MEMBER.id), "organization_id": str(MEMBER.organization_id)}],
        )

    repository = SupabaseTelegramEventRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    memberships = await repository.find_active_members(100000001)

    assert memberships == [MEMBER]


async def test_new_event_uses_conflict_safe_insert() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/source_events"
        assert request.url.params["on_conflict"] == "provider,external_event_id"
        assert "resolution=ignore-duplicates" in request.headers["Prefer"]
        assert json.loads(request.content) == {
            "organization_id": str(MEMBER.organization_id),
            "provider": "telegram",
            "external_event_id": "9001",
            "event_type": "message",
            "payload": {"update_id": 9001},
        }
        return httpx.Response(201, json=[{"id": "source-event-id"}])

    repository = SupabaseTelegramEventRepository(
        supabase_url="http://supabase.test/",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    result = await repository.ingest_event(
        member=MEMBER,
        update_id=9001,
        event_type="message",
        payload={"update_id": 9001},
    )

    assert result is EventIngestionResult.CREATED


async def test_conflicting_event_is_reported_as_duplicate() -> None:
    repository = SupabaseTelegramEventRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
    )

    result = await repository.ingest_event(
        member=MEMBER,
        update_id=9001,
        event_type="message",
        payload={"update_id": 9001},
    )

    assert result is EventIngestionResult.DUPLICATE
