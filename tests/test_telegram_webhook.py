"""Tests for authenticated, idempotent Telegram webhook ingestion."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from httpx import ASGITransport, AsyncClient, Response

from inventory_agent.config import Settings, get_settings
from inventory_agent.main import create_app
from inventory_agent.telegram.dev_identity import TelegramDevPersona
from inventory_agent.telegram.models import TelegramPayload
from inventory_agent.telegram.registration import (
    RegistrationApplicant,
    RegistrationRepository,
    RegistrationSubmission,
    hash_invite_code,
)
from inventory_agent.telegram.repository import (
    EventIngestionResult,
    OrganizationMember,
    TelegramEventRepository,
)
from inventory_agent.telegram.router import (
    get_dev_identity_repository,
    get_registration_repository,
    get_telegram_command_sender,
    get_telegram_event_repository,
)

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


@dataclass
class FakeRegistrationRepository:
    result: RegistrationSubmission = field(
        default_factory=lambda: RegistrationSubmission(status="pending")
    )
    submissions: list[dict[str, object]] = field(default_factory=list)

    async def submit_registration(
        self,
        *,
        invite_code_hash: str,
        applicant: RegistrationApplicant,
    ) -> RegistrationSubmission:
        self.submissions.append({"invite_code_hash": invite_code_hash, "applicant": applicant})
        return self.result


@dataclass
class FakeDevIdentityRepository:
    active: TelegramDevPersona | None = None
    allowed: bool = True
    activations: list[dict[str, object]] = field(default_factory=list)

    async def controller_is_admin(self, controller_telegram_user_id: int) -> bool:
        assert controller_telegram_user_id == 100000001
        return self.allowed

    async def activate(self, **kwargs: object) -> TelegramDevPersona:
        self.activations.append(kwargs)
        assert self.active is not None
        return self.active

    async def resolve(self, **kwargs: object) -> TelegramDevPersona | None:
        assert kwargs["controller_telegram_user_id"] == 100000001
        return self.active

    async def clear(self, **kwargs: object) -> bool:
        self.active = None
        return True

    async def list_personas(self, **kwargs: object) -> list[TelegramDevPersona]:
        return [self.active] if self.active is not None else []


@dataclass
class FakeDevIdentitySender:
    messages: list[tuple[int, str]] = field(default_factory=list)

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        assert inline_keyboard is None
        self.messages.append((chat_id, text))
        return 77


async def post_update(
    *,
    repository: TelegramEventRepository | None,
    payload: TelegramPayload,
    header_secret: str | None = WEBHOOK_SECRET,
    configured_secret: str | None = WEBHOOK_SECRET,
    bot_username: str | None = None,
    bot_token: str | None = None,
    registrations: RegistrationRepository | None = None,
    dev_simulation_enabled: bool = False,
    dev_identities: FakeDevIdentityRepository | None = None,
    command_sender: FakeDevIdentitySender | None = None,
) -> Response:
    """Create an isolated dependency graph for one webhook request."""

    test_app = create_app()
    settings = Settings(
        _env_file=None,
        app_env="development" if dev_simulation_enabled else "test",
        telegram_webhook_secret=configured_secret,
        telegram_bot_username=bot_username,
        telegram_bot_token=bot_token,
        telegram_dev_user_simulation_enabled=dev_simulation_enabled,
        supabase_secret_key="test-supabase-secret",
    )
    test_app.dependency_overrides[get_settings] = lambda: settings
    test_app.dependency_overrides[get_telegram_event_repository] = lambda: repository
    test_app.dependency_overrides[get_registration_repository] = lambda: registrations
    test_app.dependency_overrides[get_dev_identity_repository] = lambda: dev_identities
    test_app.dependency_overrides[get_telegram_command_sender] = lambda: command_sender
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


async def test_dev_user_command_selects_persona_without_source_event_or_openai_work() -> None:
    persona = TelegramDevPersona(
        id=UUID("73000000-0000-0000-0000-000000000001"),
        controller_telegram_user_id=100000001,
        alias="bob",
        synthetic_telegram_user_id=4000000000000001,
        display_name="Bob",
        telegram_username="dev_bob",
        registered=False,
    )
    identities = FakeDevIdentityRepository(active=persona)
    sender = FakeDevIdentitySender()
    events = FakeTelegramEventRepository()
    payload: TelegramPayload = {
        "update_id": 9040,
        "message": {
            "message_id": 70,
            "from": {"id": 100000001, "first_name": "JW"},
            "chat": {"id": 100000001, "type": "private"},
            "text": "/user bob",
        },
    }

    response = await post_update(
        repository=events,
        payload=payload,
        dev_simulation_enabled=True,
        dev_identities=identities,
        command_sender=sender,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "dev_identity_selected"}
    assert events.looked_up_user_ids == []
    assert events.ingested_events == []
    assert identities.activations[0]["alias"] == "bob"
    assert sender.messages[0][1].startswith("🧪 **Simulating Bob**")


async def test_active_dev_persona_drives_membership_and_preserves_controller_audit() -> None:
    persona = TelegramDevPersona(
        id=UUID("73000000-0000-0000-0000-000000000001"),
        controller_telegram_user_id=100000001,
        alias="bob",
        synthetic_telegram_user_id=4000000000000001,
        display_name="Bob",
        telegram_username="dev_bob",
        registered=True,
        organization_name="Cabybaba Pte Ltd",
        role="worker",
    )
    identities = FakeDevIdentityRepository(active=persona)
    events = FakeTelegramEventRepository()

    response = await post_update(
        repository=events,
        payload=MESSAGE_UPDATE,
        dev_simulation_enabled=True,
        dev_identities=identities,
        command_sender=FakeDevIdentitySender(),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert events.looked_up_user_ids == [4000000000000001]
    stored_payload = events.ingested_events[0]["payload"]
    assert stored_payload["message"]["from"]["id"] == 4000000000000001
    assert stored_payload["message"]["chat"]["id"] == 100000001
    assert stored_payload["_inventory_agent_dev_simulation"]["controller_telegram_user_id"] == (
        100000001
    )


async def test_dev_persona_can_register_through_the_real_private_chat() -> None:
    persona = TelegramDevPersona(
        id=UUID("73000000-0000-0000-0000-000000000001"),
        controller_telegram_user_id=100000001,
        alias="bob",
        synthetic_telegram_user_id=4000000000000001,
        display_name="Bob",
        telegram_username="dev_bob",
        registered=False,
    )
    registrations = FakeRegistrationRepository()
    events = FakeTelegramEventRepository(memberships=[])
    payload: TelegramPayload = {
        "update_id": 9041,
        "message": {
            "message_id": 71,
            "from": {"id": 100000001, "first_name": "JW"},
            "chat": {"id": 100000001, "type": "private"},
            "text": "/register INV-bob-code",
        },
    }

    response = await post_update(
        repository=events,
        registrations=registrations,
        payload=payload,
        dev_simulation_enabled=True,
        dev_identities=FakeDevIdentityRepository(active=persona),
        command_sender=FakeDevIdentitySender(),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "pending"}
    assert registrations.submissions[0]["applicant"] == RegistrationApplicant(
        telegram_user_id=4000000000000001,
        telegram_username="dev_bob",
        display_name="Bob",
        private_chat_id=100000001,
    )
    assert events.ingested_events == []


async def test_private_register_command_creates_pending_request_without_source_event() -> None:
    events = FakeTelegramEventRepository(memberships=[])
    registrations = FakeRegistrationRepository()
    payload: TelegramPayload = {
        "update_id": 9030,
        "message": {
            "message_id": 60,
            "from": {
                "id": 222333444,
                "username": "new_worker",
                "first_name": "New",
                "last_name": "Worker",
            },
            "chat": {"id": 222333444, "type": "private"},
            "text": "/register INV-secret-code",
        },
    }

    response = await post_update(
        repository=events,
        registrations=registrations,
        payload=payload,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "pending"}
    assert events.looked_up_user_ids == []
    assert events.ingested_events == []
    assert registrations.submissions == [
        {
            "invite_code_hash": hash_invite_code("INV-secret-code"),
            "applicant": RegistrationApplicant(
                telegram_user_id=222333444,
                telegram_username="new_worker",
                display_name="New Worker",
                private_chat_id=222333444,
            ),
        }
    ]


async def test_register_command_is_rejected_in_a_group_before_code_is_stored() -> None:
    events = FakeTelegramEventRepository(memberships=[])
    registrations = FakeRegistrationRepository()
    sender = FakeDevIdentitySender()
    payload: TelegramPayload = {
        "update_id": 9031,
        "message": {
            "message_id": 61,
            "from": {"id": 222333444, "first_name": "New"},
            "chat": {"id": -100123456789, "type": "supergroup"},
            "text": "/register INV-secret-code",
        },
    }

    response = await post_update(
        repository=events,
        registrations=registrations,
        payload=payload,
        command_sender=sender,
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "registration_requires_private_chat",
    }
    assert registrations.submissions == []
    assert events.looked_up_user_ids == []
    assert sender.messages[0][0] == -100123456789
    assert sender.messages[0][1].startswith("🔒 **Register in private chat**")
    assert "INV-secret-code" not in sender.messages[0][1]


async def test_unaddressed_group_message_is_ignored_before_membership_lookup() -> None:
    repository = FakeTelegramEventRepository()
    payload: TelegramPayload = {
        "update_id": 9020,
        "message": {
            "message_id": 50,
            "from": {"id": 100000001},
            "chat": {"id": -100123456789, "type": "supergroup"},
            "text": "unrelated group conversation",
        },
    }

    response = await post_update(
        repository=repository,
        payload=payload,
        bot_username="capybababot",
        bot_token="8992832449:test-secret",
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "group_message_not_addressed",
    }
    assert repository.looked_up_user_ids == []
    assert repository.ingested_events == []


async def test_addressed_group_message_is_ingested_with_original_payload() -> None:
    repository = FakeTelegramEventRepository()
    payload: TelegramPayload = {
        "update_id": 9021,
        "message": {
            "message_id": 51,
            "from": {"id": 100000001},
            "chat": {"id": -100123456789, "type": "supergroup"},
            "text": "@capybababot show the last five transactions",
        },
    }

    response = await post_update(
        repository=repository,
        payload=payload,
        bot_username="capybababot",
        bot_token="8992832449:test-secret",
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert repository.looked_up_user_ids == [100000001]
    assert repository.ingested_events[0]["payload"] == payload


async def test_group_reply_to_bot_is_ingested() -> None:
    repository = FakeTelegramEventRepository()
    payload: TelegramPayload = {
        "update_id": 9022,
        "message": {
            "message_id": 52,
            "from": {"id": 100000001},
            "chat": {"id": -100123456789, "type": "group"},
            "text": "show transactions",
            "reply_to_message": {
                "message_id": 49,
                "from": {
                    "id": 8992832449,
                    "is_bot": True,
                    "username": "capybababot",
                },
            },
        },
    }

    response = await post_update(
        repository=repository,
        payload=payload,
        bot_username="capybababot",
        bot_token="8992832449:test-secret",
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert repository.ingested_events[0]["event_type"] == "message"


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


async def test_callback_button_uses_the_active_dev_persona() -> None:
    persona = TelegramDevPersona(
        id=UUID("73000000-0000-0000-0000-000000000001"),
        controller_telegram_user_id=100000001,
        alias="bob",
        synthetic_telegram_user_id=4000000000000001,
        display_name="Bob",
        telegram_username="dev_bob",
        registered=True,
        organization_name="Cabybaba Pte Ltd",
        role="worker",
    )
    repository = FakeTelegramEventRepository()
    payload: TelegramPayload = {
        "update_id": 9042,
        "callback_query": {
            "id": "callback-dev-bob",
            "from": {"id": 100000001, "first_name": "JW"},
            "message": {
                "message_id": 72,
                "chat": {"id": 100000001, "type": "private"},
            },
            "data": "proposal:confirm:opaque-id",
        },
    }

    response = await post_update(
        repository=repository,
        payload=payload,
        dev_simulation_enabled=True,
        dev_identities=FakeDevIdentityRepository(active=persona),
        command_sender=FakeDevIdentitySender(),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert repository.looked_up_user_ids == [4000000000000001]
    assert repository.ingested_events[0]["payload"]["callback_query"]["from"]["id"] == (
        4000000000000001
    )
