"""List Telegram users from pending bot updates before a webhook is registered."""

import asyncio
import sys
from dataclasses import dataclass
from typing import Any, NoReturn, cast

import httpx
from pydantic import SecretStr

from inventory_agent.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class PendingTelegramUser:
    """A sender observed in Telegram's pending update queue."""

    id: int
    display_name: str
    username: str | None


async def discover_pending_users(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[PendingTelegramUser]:
    """Read, but do not acknowledge, pending updates returned by getUpdates."""

    bot_token = _required_secret(settings.telegram_bot_token, "TELEGRAM_BOT_TOKEN")
    api_url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        async with httpx.AsyncClient(timeout=15.0, transport=transport) as client:
            response = await client.get(api_url)
    except httpx.HTTPError as error:
        raise RuntimeError("Telegram user discovery failed; check network connectivity") from error

    if not response.is_success:
        raise RuntimeError(f"Telegram user discovery failed with HTTP {response.status_code}")
    response_body = response.json()
    if not isinstance(response_body, dict) or response_body.get("ok") is not True:
        description = response_body.get("description") if isinstance(response_body, dict) else None
        safe_description = description if isinstance(description, str) else "unknown API error"
        raise RuntimeError(f"Telegram rejected user discovery: {safe_description}")

    updates = response_body.get("result")
    if not isinstance(updates, list):
        raise RuntimeError("Telegram returned an invalid update list")

    users: dict[int, PendingTelegramUser] = {}
    for update in updates:
        raw_user = _extract_user(update)
        if raw_user is None:
            continue
        user_id = raw_user.get("id")
        if not isinstance(user_id, int):
            continue
        first_name = raw_user.get("first_name")
        last_name = raw_user.get("last_name")
        name_parts = [part for part in (first_name, last_name) if isinstance(part, str)]
        username = raw_user.get("username")
        users[user_id] = PendingTelegramUser(
            id=user_id,
            display_name=" ".join(name_parts) or "Unknown",
            username=username if isinstance(username, str) else None,
        )
    return sorted(users.values(), key=lambda user: user.id)


def _extract_user(update: object) -> dict[str, Any] | None:
    if not isinstance(update, dict):
        return None
    for event_key in ("message", "edited_message", "callback_query"):
        event = update.get(event_key)
        if isinstance(event, dict) and isinstance(event.get("from"), dict):
            return cast(dict[str, Any], event["from"])
    return None


def _required_secret(value: SecretStr | None, name: str) -> str:
    if value is None or not value.get_secret_value():
        raise ValueError(f"{name} is required")
    return value.get_secret_value()


def _exit_with_error(message: str) -> NoReturn:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    """Print pending sender IDs without printing the bot token."""

    try:
        users = asyncio.run(discover_pending_users(get_settings()))
    except (ValueError, RuntimeError) as error:
        _exit_with_error(str(error))

    if not users:
        print("No users found. Send the bot a message, then run this command again.")
        return
    for user in users:
        username = f" (@{user.username})" if user.username else ""
        print(f"{user.id}: {user.display_name}{username}")


if __name__ == "__main__":
    main()
