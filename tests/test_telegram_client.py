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


async def test_send_message_serializes_inline_keyboard_and_returns_message_id() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bottest-token/sendMessage"
        assert json.loads(request.content) == {
            "chat_id": -100123,
            "text": "Review stock receipt",
            "reply_markup": {
                "inline_keyboard": [[{"text": "Confirm", "callback_data": "confirm-data"}]]
            },
        }
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    client = TelegramBotClient(
        bot_token="test-token",
        transport=httpx.MockTransport(handle_request),
    )

    message_id = await client.send_message(
        chat_id=-100123,
        text="Review stock receipt",
        inline_keyboard=[[{"text": "Confirm", "callback_data": "confirm-data"}]],
    )

    assert message_id == 77


async def test_edit_message_text_can_replace_and_remove_keyboard() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bottest-token/editMessageText"
        assert json.loads(request.content) == {
            "chat_id": -100123,
            "message_id": 77,
            "text": "Inventory updated.",
            "reply_markup": {"inline_keyboard": []},
        }
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    client = TelegramBotClient(
        bot_token="test-token",
        transport=httpx.MockTransport(handle_request),
    )

    await client.edit_message_text(
        chat_id=-100123,
        message_id=77,
        text="Inventory updated.",
    )


async def test_edit_message_text_treats_already_applied_edit_as_success() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bottest-token/editMessageText"
        return httpx.Response(
            400,
            json={
                "ok": False,
                "description": "Bad Request: message is not modified",
            },
        )

    client = TelegramBotClient(
        bot_token="test-token",
        transport=httpx.MockTransport(handle_request),
    )

    await client.edit_message_text(
        chat_id=-100123,
        message_id=77,
        text="Inventory updated.",
    )
