"""Tests for Telegram callback acknowledgement requests."""

import json

import httpx

from inventory_agent.telegram.client import TelegramBotClient


async def test_answer_callback_query_uses_bot_api() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bottest-token/answerCallbackQuery"
        assert json.loads(request.content) == {
            "callback_query_id": "callback-1",
            "show_alert": False,
        }
        return httpx.Response(200, json={"ok": True, "result": True})

    client = TelegramBotClient(
        bot_token="test-token",
        transport=httpx.MockTransport(handle_request),
    )

    await client.answer_callback_query(callback_query_id="callback-1")
