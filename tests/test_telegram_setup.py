"""Tests for safe Telegram webhook registration."""

import json

import httpx
import pytest

from inventory_agent.config import Settings
from inventory_agent.telegram.setup_webhook import register_webhook


def telegram_settings(**overrides: str) -> Settings:
    values = {
        "app_env": "test",
        "telegram_bot_token": "123456:test-token",
        "telegram_webhook_secret": "test_webhook_secret",
        "telegram_webhook_url": "https://inventory.example.com/webhooks/telegram",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


async def test_register_webhook_sends_secret_and_supported_updates() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bot123456:test-token/setWebhook"
        assert json.loads(request.content) == {
            "url": "https://inventory.example.com/webhooks/telegram",
            "secret_token": "test_webhook_secret",
            "allowed_updates": ["message", "callback_query"],
        }
        return httpx.Response(200, json={"ok": True, "description": "Webhook was set"})

    description = await register_webhook(
        telegram_settings(),
        transport=httpx.MockTransport(handle_request),
    )

    assert description == "Webhook was set"


async def test_register_webhook_requires_public_https_url() -> None:
    with pytest.raises(ValueError, match="public HTTPS URL"):
        await register_webhook(
            telegram_settings(telegram_webhook_url="http://127.0.0.1:8000/webhooks/telegram")
        )


async def test_register_webhook_validates_telegram_secret_format() -> None:
    with pytest.raises(ValueError, match="letters, digits"):
        await register_webhook(
            telegram_settings(telegram_webhook_secret="invalid secret with spaces")
        )


async def test_registration_failure_does_not_include_bot_token() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500))

    with pytest.raises(RuntimeError) as captured_error:
        await register_webhook(telegram_settings(), transport=transport)

    assert "123456:test-token" not in str(captured_error.value)


async def test_network_failure_does_not_include_bot_token() -> None:
    def fail_request(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    with pytest.raises(RuntimeError) as captured_error:
        await register_webhook(
            telegram_settings(),
            transport=httpx.MockTransport(fail_request),
        )

    assert "123456:test-token" not in str(captured_error.value)
