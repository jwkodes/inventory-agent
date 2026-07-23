"""Register the configured public webhook with Telegram without exposing the bot token."""

import asyncio
import re
import sys
from typing import NoReturn
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from inventory_agent.config import Settings, get_settings

TELEGRAM_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


async def register_webhook(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Call setWebhook and return Telegram's non-secret success description."""

    bot_token = _required_secret(settings.telegram_bot_token, "TELEGRAM_BOT_TOKEN")
    webhook_secret = _required_secret(
        settings.telegram_webhook_secret,
        "TELEGRAM_WEBHOOK_SECRET",
    )
    if TELEGRAM_SECRET_PATTERN.fullmatch(webhook_secret) is None:
        raise ValueError(
            "TELEGRAM_WEBHOOK_SECRET must contain 1-256 letters, digits, underscores, or hyphens"
        )

    webhook_url = settings.telegram_webhook_url or ""
    parsed_url = urlparse(webhook_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError("TELEGRAM_WEBHOOK_URL must be a public HTTPS URL")
    if parsed_url.path != "/webhooks/telegram":
        raise ValueError("TELEGRAM_WEBHOOK_URL must end with /webhooks/telegram")

    api_url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    request_body = {
        "url": webhook_url,
        "secret_token": webhook_secret,
        "allowed_updates": ["message", "callback_query"],
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, transport=transport) as client:
            response = await client.post(api_url, json=request_body)
    except httpx.HTTPError as error:
        raise RuntimeError("Telegram webhook request failed; check network connectivity") from error

    if not response.is_success:
        raise RuntimeError(f"Telegram webhook registration failed with HTTP {response.status_code}")
    response_body = response.json()
    if not isinstance(response_body, dict) or response_body.get("ok") is not True:
        description = response_body.get("description") if isinstance(response_body, dict) else None
        safe_description = description if isinstance(description, str) else "unknown API error"
        raise RuntimeError(f"Telegram rejected the webhook: {safe_description}")

    description = response_body.get("description")
    return description if isinstance(description, str) else "Webhook registered"


def _required_secret(value: SecretStr | None, name: str) -> str:
    if value is None or not value.get_secret_value():
        raise ValueError(f"{name} is required")
    return value.get_secret_value()


def _exit_with_error(message: str) -> NoReturn:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    """Load .env and register the webhook."""

    try:
        description = asyncio.run(register_webhook(get_settings()))
    except (ValueError, RuntimeError) as error:
        _exit_with_error(str(error))
    print(description)


if __name__ == "__main__":
    main()
