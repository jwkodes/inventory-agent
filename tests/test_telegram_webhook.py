"""Tests for authenticated, idempotent Telegram webhook ingestion."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from httpx import ASGITransport, AsyncClient, Response

from inventory_agent.config import Settings, get_settings
from inventory_agent.main import create_app
from inventory_agent.telegram.models import TelegramPayload
from inventory_agent.telegram.repository import (
    EventIngestionResult,
    OrganizationMember,
    TelegramEventRepository,
)
from inventory_agent.telegram.router import get_telegram_event_repository

WEBHOOK_SECRET = "test_webhook_secret"
MEMBER = OrganizationMember(
    id=UUID("11000000-0000-0000-0000-000000000001"),
    organization_id=UUID("10000000-0000-0000-0000-000000000001"),
)
MESSAGE_UPDATE: TelegramPayload = {
    "update_id": 9001,
    "message": {
        "message_id": 42,
        "from": {"id": 100000001, "first_name": "Demo"},
        "chat": {"id": 100000001, "type": "private"},
        "date": 1784678400,
        "text": "Received 3 units of milk",
    },
}


@dataclass
class FakeTelegramEventRepository:
    """Record calls while emulating the repository's two possible insert outcomes."""

    memberships: list[OrganizationMember] = field(default_factory=lambda: [MEMBER])
    result: EventIngestionResult = EventIngestionResult.CREATED
    looked_up_user_ids: list[int] = field(default_factory=list)
    ingested_events: list[dict[str, Any]] = field(default_factory=list)

    async def find_active_members(self, telegram_user_id: int) -> list[OrganizationMember]:
        self.looked_up_user_ids.append(telegram_user_id)
        return self.memberships

    async def ingest_event(
        self,
        *,
        member: OrganizationMember,
        update_id: int,
        event_type: str,
        payload: TelegramPayload,
    ) -> EventIngestionResult:
        self.ingested_events.append(
            {
                "member": member,
                "update_id": update_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        return self.result


async def post_update(
    *,
    repository: TelegramEventRepository | None,
    payload: TelegramPayload,
    header_secret: str | None = WEBHOOK_SECRET,
    configured_secret: str | None = WEBHOOK_SECRET,
) -> Response:
    """Create an isolated dependency graph for one webhook request."""

    test_app = create_app()
    settings = Settings(
        _env_file=None,
        app_env="test",
        telegram_webhook_secret=configured_secret,
        supabase_secret_key="test-supabase-secret",
    )
    test_app.dependency_overrides[get_settings] = lambda: settings
    test_app.dependency_overrides[get_telegram_event_repository] = lambda: repository
    headers = {}
    if header_secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = header_secret

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhooks/telegram", json=payload, headers=headers)


async def test_valid_message_is_ingested() -> None:
    repository = FakeTelegramEventRepository()

    response = await post_update(repository=repository, payload=MESSAGE_UPDATE)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert repository.looked_up_user_ids == [100000001]
    assert repository.ingested_events == [
        {
            "member": MEMBER,
            "update_id": 9001,
            "event_type": "message",
            "payload": MESSAGE_UPDATE,
        }
    ]


async def test_invoice_photo_is_classified_for_image_processing() -> None:
    repository = FakeTelegramEventRepository()
    payload: TelegramPayload = {
        "update_id": 9010,
        "message": {
            "message_id": 43,
            "from": {"id": 100000001},
            "chat": {"id": 100000001},
            "caption": "Supplier delivery",
            "photo": [
                {
                    "file_id": "small",
                    "file_unique_id": "photo-1",
                    "width": 90,
                    "height": 120,
                    "file_size": 1000,
                },
                {
                    "file_id": "large",
                    "file_unique_id": "photo-1",
                    "width": 900,
                    "height": 1200,
                    "file_size": 10000,
                },
            ],
        },
    }

    response = await post_update(repository=repository, payload=payload)

    assert response.status_code == 200
    assert repository.ingested_events[0]["event_type"] == "invoice_image"


async def test_supported_image_document_is_classified_for_image_processing() -> None:
    repository = FakeTelegramEventRepository()
    payload: TelegramPayload = {
        "update_id": 9011,
        "message": {
            "from": {"id": 100000001},
            "chat": {"id": 100000001},
            "document": {
                "file_id": "png-document",
                "file_name": "invoice.png",
                "mime_type": "image/png",
            },
        },
    }

    await post_update(repository=repository, payload=payload)

    assert repository.ingested_events[0]["event_type"] == "invoice_image"


async def test_pdf_is_retained_as_unsupported_document_for_a_later_slice() -> None:
    repository = FakeTelegramEventRepository()
    payload: TelegramPayload = {
        "update_id": 9012,
        "message": {
            "from": {"id": 100000001},
            "chat": {"id": 100000001},
            "document": {
                "file_id": "pdf-document",
                "file_name": "invoice.pdf",
                "mime_type": "application/pdf",
            },
        },
    }

    await post_update(repository=repository, payload=payload)

    assert repository.ingested_events[0]["event_type"] == "unsupported_document"


async def test_duplicate_update_returns_success_without_claiming_a_new_event() -> None:
    repository = FakeTelegramEventRepository(result=EventIngestionResult.DUPLICATE)

    response = await post_update(repository=repository, payload=MESSAGE_UPDATE)

    assert response.status_code == 200
    assert response.json() == {"status": "duplicate"}


async def test_invalid_webhook_secret_is_rejected_before_database_access() -> None:
    repository = FakeTelegramEventRepository()

    response = await post_update(
        repository=repository,
        payload=MESSAGE_UPDATE,
        header_secret="wrong-secret",
    )

    assert response.status_code == 401
    assert repository.looked_up_user_ids == []


async def test_missing_webhook_secret_configuration_returns_unavailable() -> None:
    response = await post_update(
        repository=FakeTelegramEventRepository(),
        payload=MESSAGE_UPDATE,
        configured_secret=None,
    )

    assert response.status_code == 503


async def test_missing_supabase_configuration_returns_unavailable_after_authentication() -> None:
    response = await post_update(repository=None, payload=MESSAGE_UPDATE)

    assert response.status_code == 503


async def test_malformed_update_is_rejected() -> None:
    response = await post_update(
        repository=FakeTelegramEventRepository(),
        payload={"message": {"from": {"id": 100000001}}},
    )

    assert response.status_code == 422


async def test_unsupported_update_is_acknowledged_without_database_access() -> None:
    repository = FakeTelegramEventRepository()

    response = await post_update(
        repository=repository,
        payload={"update_id": 9002, "poll": {"id": "poll-1"}},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "unsupported_update"}
    assert repository.looked_up_user_ids == []


async def test_unregistered_user_is_acknowledged_without_ingestion() -> None:
    repository = FakeTelegramEventRepository(memberships=[])

    response = await post_update(repository=repository, payload=MESSAGE_UPDATE)

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "unregistered_user"}
    assert repository.ingested_events == []


async def test_multi_company_user_requires_organization_selection() -> None:
    second_membership = OrganizationMember(
        id=UUID("11000000-0000-0000-0000-000000000002"),
        organization_id=UUID("10000000-0000-0000-0000-000000000002"),
    )
    repository = FakeTelegramEventRepository(memberships=[MEMBER, second_membership])

    response = await post_update(repository=repository, payload=MESSAGE_UPDATE)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "organization_selection_required",
    }
    assert repository.ingested_events == []


async def test_callback_query_uses_callback_sender() -> None:
    repository = FakeTelegramEventRepository()
    payload: TelegramPayload = {
        "update_id": 9003,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": 100000001},
            "data": "proposal:confirm:opaque-id",
        },
    }

    response = await post_update(repository=repository, payload=payload)

    assert response.status_code == 200
    assert repository.ingested_events[0]["event_type"] == "callback_query"
