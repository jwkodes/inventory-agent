"""Minimal Telegram Bot API client for callbacks and outbound messages."""

import httpx


class TelegramBotClient:
    def __init__(
        self,
        *,
        bot_token: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        body: dict[str, object] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text is not None:
            body["text"] = text
        result = await self._post("answerCallbackQuery", body, "callback acknowledgement")
        if result.get("ok") is not True:
            raise RuntimeError("Telegram rejected the callback acknowledgement")

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        """Send text with an optional inline keyboard and return Telegram's message ID."""

        if not text.strip():
            raise ValueError("Telegram message text must not be empty")
        body: dict[str, object] = {"chat_id": chat_id, "text": text}
        if inline_keyboard is not None:
            body["reply_markup"] = {"inline_keyboard": inline_keyboard}
        response = await self._post("sendMessage", body, "message delivery")
        result = response.get("result")
        if response.get("ok") is not True or not isinstance(result, dict):
            raise RuntimeError("Telegram rejected the message")
        message_id = result.get("message_id")
        if not isinstance(message_id, int):
            raise RuntimeError("Telegram returned an invalid message ID")
        return message_id

    async def _post(
        self,
        method: str,
        body: dict[str, object],
        operation: str,
    ) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(f"{self._base_url}/{method}", json=body)
        except httpx.HTTPError as error:
            raise RuntimeError(f"Telegram {operation} failed") from error
        if not response.is_success:
            raise RuntimeError(f"Telegram {operation} failed with HTTP {response.status_code}")
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError(f"Telegram returned an invalid {operation} response")
        return result
