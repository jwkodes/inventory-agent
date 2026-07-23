"""Minimal Telegram Bot API client for callback acknowledgements."""

import httpx


class TelegramBotClient:
    def __init__(
        self,
        *,
        bot_token: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._answer_url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        body: dict[str, str | bool] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text is not None:
            body["text"] = text
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(self._answer_url, json=body)
        except httpx.HTTPError as error:
            raise RuntimeError("Telegram callback acknowledgement failed") from error
        if not response.is_success:
            raise RuntimeError(
                f"Telegram callback acknowledgement failed with HTTP {response.status_code}"
            )
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Telegram rejected the callback acknowledgement")
