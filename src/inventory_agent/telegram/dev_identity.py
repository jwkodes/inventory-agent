"""Development-only Telegram identity simulation through real bot chats."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import httpx

from inventory_agent.telegram.models import TelegramPayload

_USER_COMMAND = re.compile(
    r"^/user(?:@[A-Za-z0-9_]{5,32})?(?:\s+(?P<alias>.+?))?\s*$",
    re.IGNORECASE,
)
_USERS_COMMAND = re.compile(
    r"^/users(?:@[A-Za-z0-9_]{5,32})?\s*$",
    re.IGNORECASE,
)
_ALIAS = re.compile(r"^[a-z][a-z0-9_-]{0,27}$")


class DevUserCommandAction(StrEnum):
    STATUS = "status"
    SELECT = "select"
    CLEAR = "clear"
    LIST = "list"


@dataclass(frozen=True, slots=True)
class DevUserCommand:
    action: DevUserCommandAction
    alias: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramDevPersona:
    id: UUID
    controller_telegram_user_id: int
    alias: str
    synthetic_telegram_user_id: int
    display_name: str
    telegram_username: str
    registered: bool
    organization_name: str | None = None
    role: str | None = None
    active: bool = True
    expires_at: str | None = None


class TelegramDevIdentityRepository(Protocol):
    async def controller_is_admin(self, controller_telegram_user_id: int) -> bool:
        """Return whether the real Telegram account may simulate users."""

    async def activate(
        self,
        *,
        controller_telegram_user_id: int,
        chat_id: int,
        alias: str,
        display_name: str,
        session_minutes: int,
    ) -> TelegramDevPersona:
        """Create or select a stable persona in one Telegram chat."""

    async def resolve(
        self,
        *,
        controller_telegram_user_id: int,
        chat_id: int,
        session_minutes: int,
    ) -> TelegramDevPersona | None:
        """Resolve and extend the active session for one controller and chat."""

    async def clear(self, *, controller_telegram_user_id: int, chat_id: int) -> bool:
        """Return the chat to the controller's real Telegram identity."""

    async def list_personas(
        self,
        *,
        controller_telegram_user_id: int,
        chat_id: int,
    ) -> list[TelegramDevPersona]:
        """List stable personas created by the controller."""


class TelegramDevIdentitySender(Protocol):
    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        """Send a new Telegram message for a deterministic dev command."""


class SupabaseTelegramDevIdentityRepository:
    """Call the development-persona RPC boundary with server credentials."""

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

    async def controller_is_admin(self, controller_telegram_user_id: int) -> bool:
        result = await self._rpc(
            "telegram_dev_controller_is_admin",
            {"p_controller_telegram_user_id": controller_telegram_user_id},
        )
        if not isinstance(result, bool):
            raise ValueError("Supabase returned an invalid dev-controller result")
        return result

    async def activate(
        self,
        *,
        controller_telegram_user_id: int,
        chat_id: int,
        alias: str,
        display_name: str,
        session_minutes: int,
    ) -> TelegramDevPersona:
        result = await self._rpc(
            "activate_telegram_dev_persona",
            {
                "p_controller_telegram_user_id": controller_telegram_user_id,
                "p_chat_id": chat_id,
                "p_alias": alias,
                "p_display_name": display_name,
                "p_session_minutes": session_minutes,
            },
        )
        return _parse_persona(result)

    async def resolve(
        self,
        *,
        controller_telegram_user_id: int,
        chat_id: int,
        session_minutes: int,
    ) -> TelegramDevPersona | None:
        result = await self._rpc(
            "resolve_telegram_dev_persona",
            {
                "p_controller_telegram_user_id": controller_telegram_user_id,
                "p_chat_id": chat_id,
                "p_session_minutes": session_minutes,
            },
        )
        return None if result is None else _parse_persona(result)

    async def clear(self, *, controller_telegram_user_id: int, chat_id: int) -> bool:
        result = await self._rpc(
            "clear_telegram_dev_persona",
            {
                "p_controller_telegram_user_id": controller_telegram_user_id,
                "p_chat_id": chat_id,
            },
        )
        if not isinstance(result, bool):
            raise ValueError("Supabase returned an invalid dev-persona clear result")
        return result

    async def list_personas(
        self,
        *,
        controller_telegram_user_id: int,
        chat_id: int,
    ) -> list[TelegramDevPersona]:
        result = await self._rpc(
            "list_telegram_dev_personas",
            {
                "p_controller_telegram_user_id": controller_telegram_user_id,
                "p_chat_id": chat_id,
            },
        )
        if not isinstance(result, list):
            raise ValueError("Supabase returned an invalid dev-persona list")
        return [_parse_persona(row) for row in result]

    async def _rpc(self, function_name: str, body: dict[str, object]) -> object:
        async with httpx.AsyncClient(
            base_url=self._rest_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(f"/rpc/{function_name}", json=body)
        response.raise_for_status()
        return response.json()


def parse_dev_user_command(text: str | None) -> DevUserCommand | None:
    """Recognize complete `/user` and `/users` commands without intercepting prose."""

    if text is None:
        return None
    stripped = text.strip()
    if _USERS_COMMAND.fullmatch(stripped):
        return DevUserCommand(action=DevUserCommandAction.LIST)
    match = _USER_COMMAND.fullmatch(stripped)
    if match is None:
        return None
    alias = re.sub(r"\s+", "-", (match.group("alias") or "").strip().casefold())
    if not alias:
        return DevUserCommand(action=DevUserCommandAction.STATUS)
    if alias == "me":
        return DevUserCommand(action=DevUserCommandAction.CLEAR)
    return DevUserCommand(action=DevUserCommandAction.SELECT, alias=alias)


async def handle_dev_user_command(
    *,
    command: DevUserCommand,
    controller_telegram_user_id: int,
    chat_id: int,
    repository: TelegramDevIdentityRepository,
    sender: TelegramDevIdentitySender,
    session_minutes: int,
) -> str:
    """Apply one deterministic command and notify the real Telegram chat."""

    if not await repository.controller_is_admin(controller_telegram_user_id):
        message = (
            "🚫 **User simulation unavailable**\n\n"
            "Only a real Telegram account with an active company admin membership can "
            "simulate users."
        )
        await sender.send_message(chat_id=chat_id, text=message)
        return "dev_identity_forbidden"

    if command.action is DevUserCommandAction.CLEAR:
        await repository.clear(
            controller_telegram_user_id=controller_telegram_user_id,
            chat_id=chat_id,
        )
        message = (
            "👤 **Using your real Telegram identity**\n\n"
            "Development user simulation is off for this chat."
        )
        await sender.send_message(chat_id=chat_id, text=message)
        return "dev_identity_cleared"

    if command.action is DevUserCommandAction.LIST:
        personas = await repository.list_personas(
            controller_telegram_user_id=controller_telegram_user_id,
            chat_id=chat_id,
        )
        if not personas:
            message = (
                "🧪 **Simulated users**\n\n"
                "No personas exist yet. Send `/user bob` to create and select Bob."
            )
        else:
            lines = [
                (
                    f"- {'➡️ ' if persona.active else ''}**{persona.display_name}** "
                    f"(`/user {persona.alias}`) — {_persona_membership(persona)}"
                )
                for persona in personas
            ]
            message = (
                "🧪 **Simulated users**\n\n"
                + "\n".join(lines)
                + "\n\nSend `/user me` to return to your real identity."
            )
        await sender.send_message(chat_id=chat_id, text=message)
        return "dev_identity_listed"

    if command.action is DevUserCommandAction.STATUS:
        persona = await repository.resolve(
            controller_telegram_user_id=controller_telegram_user_id,
            chat_id=chat_id,
            session_minutes=session_minutes,
        )
        if persona is None:
            message = (
                "👤 **Using your real Telegram identity**\n\n"
                "Send `/user bob` to create or select a simulated user."
            )
        else:
            message = _selected_message(persona)
        await sender.send_message(chat_id=chat_id, text=message)
        return "dev_identity_status"

    alias = command.alias or ""
    if not _ALIAS.fullmatch(alias) or alias == "me":
        message = (
            "⚠️ **Invalid simulated user name**\n\n"
            "Use 1-28 letters, numbers, underscores, or hyphens, starting with a letter. "
            "For example: `/user bob`."
        )
        await sender.send_message(chat_id=chat_id, text=message)
        return "dev_identity_invalid_alias"

    persona = await repository.activate(
        controller_telegram_user_id=controller_telegram_user_id,
        chat_id=chat_id,
        alias=alias,
        display_name=_display_name(alias),
        session_minutes=session_minutes,
    )
    await sender.send_message(chat_id=chat_id, text=_selected_message(persona))
    return "dev_identity_selected"


def telegram_chat_id(payload: TelegramPayload) -> int | None:
    """Return the destination chat for supported message and callback updates."""

    for key in ("message", "edited_message"):
        value = payload.get(key)
        if isinstance(value, dict):
            chat = value.get("chat")
            if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                return int(chat["id"])
    callback = payload.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message")
        if isinstance(message, dict):
            chat = message.get("chat")
            if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                return int(chat["id"])
    return None


def apply_dev_persona(
    payload: TelegramPayload,
    *,
    persona: TelegramDevPersona,
) -> TelegramPayload:
    """Copy an authenticated update and replace only its logical sender identity."""

    overlaid = copy.deepcopy(payload)
    sender: dict[str, object] = {
        "id": persona.synthetic_telegram_user_id,
        "is_bot": False,
        "first_name": persona.display_name,
        "username": persona.telegram_username,
    }
    for key in ("message", "edited_message"):
        value = overlaid.get(key)
        if isinstance(value, dict) and isinstance(value.get("from"), dict):
            value["from"] = sender
    callback = overlaid.get("callback_query")
    if isinstance(callback, dict) and isinstance(callback.get("from"), dict):
        callback["from"] = sender
    overlaid["_inventory_agent_dev_simulation"] = {
        "persona_id": str(persona.id),
        "alias": persona.alias,
        "display_name": persona.display_name,
        "synthetic_telegram_user_id": persona.synthetic_telegram_user_id,
        "controller_telegram_user_id": persona.controller_telegram_user_id,
    }
    return overlaid


def _parse_persona(value: object) -> TelegramDevPersona:
    if not isinstance(value, dict):
        raise ValueError("Supabase returned an invalid dev persona")
    return TelegramDevPersona(
        id=UUID(str(value["id"])),
        controller_telegram_user_id=int(value["controller_telegram_user_id"]),
        alias=str(value["alias"]),
        synthetic_telegram_user_id=int(value["synthetic_telegram_user_id"]),
        display_name=str(value["display_name"]),
        telegram_username=str(value["telegram_username"]),
        registered=value.get("registered") is True,
        organization_name=(
            str(value["organization_name"]) if value.get("organization_name") is not None else None
        ),
        role=str(value["role"]) if value.get("role") is not None else None,
        active=value.get("active", True) is True,
        expires_at=str(value["expires_at"]) if value.get("expires_at") is not None else None,
    )


def _display_name(alias: str) -> str:
    return " ".join(word.capitalize() for word in re.split(r"[-_]+", alias) if word)


def _persona_membership(persona: TelegramDevPersona) -> str:
    if not persona.registered:
        return "not registered"
    role = (persona.role or "worker").replace("_", " ")
    return f"{role} at {persona.organization_name or 'a company'}"


def _selected_message(persona: TelegramDevPersona) -> str:
    membership = _persona_membership(persona)
    next_step = (
        "Send inventory messages normally. New messages and button presses in this chat "
        "will use this identity."
        if persona.registered
        else (
            "This is a new user. Create an invite in the dashboard, then send "
            "`/register INVITE_CODE` while this persona is selected."
        )
    )
    return (
        f"🧪 **Simulating {persona.display_name}**\n\n"
        f"Identity: `{persona.alias}` · {membership}\n\n"
        f"{next_step}\n\n"
        "Use `/user me` to return to your real identity."
    )


__all__ = [
    "DevUserCommand",
    "DevUserCommandAction",
    "SupabaseTelegramDevIdentityRepository",
    "TelegramDevIdentityRepository",
    "TelegramDevIdentitySender",
    "TelegramDevPersona",
    "apply_dev_persona",
    "handle_dev_user_command",
    "parse_dev_user_command",
    "telegram_chat_id",
]
