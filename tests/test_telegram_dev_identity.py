"""Tests for development-only Telegram persona commands and payload overlays."""

from dataclasses import dataclass, field
from uuid import UUID

from inventory_agent.telegram.dev_identity import (
    DevUserCommandAction,
    TelegramDevPersona,
    apply_dev_persona,
    handle_dev_user_command,
    parse_dev_user_command,
    telegram_chat_id,
)

CONTROLLER_ID = 100000001
SYNTHETIC_ID = 4000000000000001
CHAT_ID = -100123
PERSONA = TelegramDevPersona(
    id=UUID("73000000-0000-0000-0000-000000000001"),
    controller_telegram_user_id=CONTROLLER_ID,
    alias="bob",
    synthetic_telegram_user_id=SYNTHETIC_ID,
    display_name="Bob",
    telegram_username="dev_bob",
    registered=False,
)


@dataclass
class FakeRepository:
    allowed: bool = True
    active: TelegramDevPersona | None = None
    personas: list[TelegramDevPersona] = field(default_factory=list)
    activations: list[dict[str, object]] = field(default_factory=list)
    clears: list[tuple[int, int]] = field(default_factory=list)

    async def controller_is_admin(self, controller_telegram_user_id: int) -> bool:
        assert controller_telegram_user_id == CONTROLLER_ID
        return self.allowed

    async def activate(self, **kwargs: object) -> TelegramDevPersona:
        self.activations.append(kwargs)
        self.active = PERSONA
        if PERSONA not in self.personas:
            self.personas.append(PERSONA)
        return PERSONA

    async def resolve(self, **kwargs: object) -> TelegramDevPersona | None:
        assert kwargs["controller_telegram_user_id"] == CONTROLLER_ID
        assert kwargs["chat_id"] == CHAT_ID
        return self.active

    async def clear(self, *, controller_telegram_user_id: int, chat_id: int) -> bool:
        self.clears.append((controller_telegram_user_id, chat_id))
        self.active = None
        return True

    async def list_personas(self, **kwargs: object) -> list[TelegramDevPersona]:
        assert kwargs == {
            "controller_telegram_user_id": CONTROLLER_ID,
            "chat_id": CHAT_ID,
        }
        return self.personas


@dataclass
class FakeSender:
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


def test_parses_only_complete_dev_user_commands() -> None:
    assert parse_dev_user_command("/user bob").action is DevUserCommandAction.SELECT
    assert parse_dev_user_command("/user bob").alias == "bob"
    assert parse_dev_user_command("/user@capybababot BOB").alias == "bob"
    assert parse_dev_user_command("/user Bob Lee").alias == "bob-lee"
    assert parse_dev_user_command("/user me").action is DevUserCommandAction.CLEAR
    assert parse_dev_user_command("/user").action is DevUserCommandAction.STATUS
    assert parse_dev_user_command("/users").action is DevUserCommandAction.LIST
    assert parse_dev_user_command("please /user bob") is None
    assert parse_dev_user_command("/users bob") is None


def test_payload_overlay_changes_sender_but_preserves_real_chat_and_audit_identity() -> None:
    payload = {
        "update_id": 1,
        "message": {
            "from": {"id": CONTROLLER_ID, "first_name": "JW"},
            "chat": {"id": CHAT_ID, "type": "supergroup"},
            "text": "@capybababot show stock",
        },
    }

    overlaid = apply_dev_persona(payload, persona=PERSONA)

    assert payload["message"]["from"]["id"] == CONTROLLER_ID
    assert overlaid["message"]["from"]["id"] == SYNTHETIC_ID
    assert overlaid["message"]["chat"]["id"] == CHAT_ID
    assert overlaid["_inventory_agent_dev_simulation"] == {
        "persona_id": str(PERSONA.id),
        "alias": "bob",
        "display_name": "Bob",
        "synthetic_telegram_user_id": SYNTHETIC_ID,
        "controller_telegram_user_id": CONTROLLER_ID,
    }
    assert telegram_chat_id(overlaid) == CHAT_ID


def test_callback_overlay_changes_the_button_actor() -> None:
    payload = {
        "update_id": 2,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": CONTROLLER_ID},
            "message": {"message_id": 5, "chat": {"id": CHAT_ID}},
            "data": "proposal:confirm:opaque",
        },
    }

    overlaid = apply_dev_persona(payload, persona=PERSONA)

    assert overlaid["callback_query"]["from"]["id"] == SYNTHETIC_ID
    assert telegram_chat_id(overlaid) == CHAT_ID


async def test_select_and_clear_commands_send_new_visible_messages() -> None:
    repository = FakeRepository()
    sender = FakeSender()

    selected = await handle_dev_user_command(
        command=parse_dev_user_command("/user bob"),
        controller_telegram_user_id=CONTROLLER_ID,
        chat_id=CHAT_ID,
        repository=repository,
        sender=sender,
        session_minutes=120,
    )
    cleared = await handle_dev_user_command(
        command=parse_dev_user_command("/user me"),
        controller_telegram_user_id=CONTROLLER_ID,
        chat_id=CHAT_ID,
        repository=repository,
        sender=sender,
        session_minutes=120,
    )

    assert selected == "dev_identity_selected"
    assert cleared == "dev_identity_cleared"
    assert repository.activations[0] == {
        "controller_telegram_user_id": CONTROLLER_ID,
        "chat_id": CHAT_ID,
        "alias": "bob",
        "display_name": "Bob",
        "session_minutes": 120,
    }
    assert repository.clears == [(CONTROLLER_ID, CHAT_ID)]
    assert sender.messages[0][1].startswith("🧪 **Simulating Bob**")
    assert "/register INVITE_CODE" in sender.messages[0][1]
    assert sender.messages[1][1].startswith("👤 **Using your real Telegram identity**")


async def test_non_admin_controller_cannot_select_a_persona() -> None:
    repository = FakeRepository(allowed=False)
    sender = FakeSender()

    result = await handle_dev_user_command(
        command=parse_dev_user_command("/user bob"),
        controller_telegram_user_id=CONTROLLER_ID,
        chat_id=CHAT_ID,
        repository=repository,
        sender=sender,
        session_minutes=120,
    )

    assert result == "dev_identity_forbidden"
    assert repository.activations == []
    assert "Only a real Telegram account" in sender.messages[0][1]
