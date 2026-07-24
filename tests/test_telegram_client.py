"""Tests for Telegram callback acknowledgement requests."""

import json

import httpx
import pytest

from inventory_agent.telegram.client import TELEGRAM_DOWNLOAD_LIMIT_BYTES, TelegramBotClient


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
            "parse_mode": "HTML",
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
            "parse_mode": "HTML",
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


async def test_send_message_safely_renders_bold_markdown_as_telegram_html() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "chat_id": -100123,
            "text": "Is <b>AMOX-502</b> new &amp; ready?",
            "parse_mode": "HTML",
        }
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 78}})

    client = TelegramBotClient(
        bot_token="test-token",
        transport=httpx.MockTransport(handle_request),
    )

    await client.send_message(
        chat_id=-100123,
        text="Is **AMOX-502** new & ready?",
    )


async def test_remove_inline_keyboard_preserves_message_text() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bottest-token/editMessageReplyMarkup"
        assert json.loads(request.content) == {
            "chat_id": -100123,
            "message_id": 77,
            "reply_markup": {"inline_keyboard": []},
        }
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    client = TelegramBotClient(
        bot_token="test-token",
        transport=httpx.MockTransport(handle_request),
    )

    await client.remove_inline_keyboard(chat_id=-100123, message_id=77)


async def test_download_file_resolves_path_and_returns_bounded_bytes() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bottest-token/getFile":
            assert json.loads(request.content) == {"file_id": "invoice-file"}
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"file_path": "photos/invoice.jpg", "file_size": 7},
                },
            )
        assert request.url.path == "/file/bottest-token/photos/invoice.jpg"
        return httpx.Response(200, content=b"invoice")

    client = TelegramBotClient(
        bot_token="test-token",
        transport=httpx.MockTransport(handle_request),
    )

    downloaded = await client.download_file(file_id="invoice-file", expected_size=7)

    assert downloaded.data == b"invoice"
    assert downloaded.file_path == "photos/invoice.jpg"


async def test_download_file_rejects_known_oversized_input_before_network() -> None:
    client = TelegramBotClient(
        bot_token="test-token",
        transport=httpx.MockTransport(
            lambda request: pytest.fail("oversized file must not make a request")
        ),
    )

    with pytest.raises(ValueError, match="download limit"):
        await client.download_file(
            file_id="too-large",
            expected_size=TELEGRAM_DOWNLOAD_LIMIT_BYTES + 1,
        )
