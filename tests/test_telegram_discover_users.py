"""Tests for Telegram user ID discovery during local setup."""

import httpx

from inventory_agent.config import Settings
from inventory_agent.telegram.discover_users import (
    PendingTelegramUser,
    discover_pending_users,
)


async def test_discover_pending_users_extracts_and_deduplicates_senders() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "from": {
                                "id": 12345,
                                "first_name": "Jane",
                                "last_name": "Doe",
                                "username": "jane",
                            }
                        },
                    },
                    {
                        "update_id": 2,
                        "callback_query": {"from": {"id": 12345, "first_name": "Jane"}},
                    },
                ],
            },
        )
    )
    settings = Settings(
        _env_file=None,
        app_env="test",
        telegram_bot_token="123456:test-token",
    )

    users = await discover_pending_users(settings, transport=transport)

    assert users == [PendingTelegramUser(id=12345, display_name="Jane", username=None)]
