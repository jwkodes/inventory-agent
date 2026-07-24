"""Minimal Telegram Bot API client for callbacks and outbound messages."""

import re
from dataclasses import dataclass
from html import escape

import httpx

TELEGRAM_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024
_BOLD_MARKDOWN = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", flags=re.DOTALL)
_FENCED_CODE_BLOCK = re.compile(
    r"```[ \t]*(?:[A-Za-z0-9_+.-]+)?[ \t]*\r?\n"
    r"(?P<code>.*?)"
    r"(?:\r?\n)?```",
    flags=re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class DownloadedTelegramFile:
    data: bytes
    file_path: str


class TelegramBotClient:
    def __init__(
        self,
        *,
        bot_token: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._file_base_url = f"https://api.telegram.org/file/bot{bot_token}"
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def download_file(
        self,
        *,
        file_id: str,
        expected_size: int | None = None,
        max_bytes: int = TELEGRAM_DOWNLOAD_LIMIT_BYTES,
    ) -> DownloadedTelegramFile:
        """Resolve and download one Telegram file while enforcing a bounded size."""

        if not file_id.strip():
            raise ValueError("Telegram file ID must not be empty")
        if max_bytes <= 0 or max_bytes > TELEGRAM_DOWNLOAD_LIMIT_BYTES:
            raise ValueError("Telegram download limit must be between 1 byte and 20 MB")
        if expected_size is not None and expected_size > max_bytes:
            raise ValueError("Telegram file exceeds the configured download limit")

        response = await self._post("getFile", {"file_id": file_id}, "file lookup")
        result = response.get("result")
        if response.get("ok") is not True or not isinstance(result, dict):
            raise RuntimeError("Telegram rejected the file lookup")
        file_path = result.get("file_path")
        file_size = result.get("file_size")
        if (
            not isinstance(file_path, str)
            or not file_path
            or any(character in file_path for character in ("\r", "\n", "\\"))
        ):
            raise RuntimeError("Telegram returned an invalid file path")
        if isinstance(file_size, int) and file_size > max_bytes:
            raise ValueError("Telegram file exceeds the configured download limit")

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                download = await client.get(f"{self._file_base_url}/{file_path}")
                download.raise_for_status()
        except httpx.HTTPError as error:
            raise RuntimeError("Telegram file download failed") from error
        if len(download.content) > max_bytes:
            raise ValueError("Telegram file exceeds the configured download limit")
        return DownloadedTelegramFile(data=download.content, file_path=file_path)

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
        body: dict[str, object] = {
            "chat_id": chat_id,
            "text": _render_telegram_html(text),
            "parse_mode": "HTML",
        }
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

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """Replace a bot message and its inline keyboard after a callback action."""

        if not text.strip():
            raise ValueError("Telegram message text must not be empty")
        body: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": _render_telegram_html(text),
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": inline_keyboard or []},
        }
        response = await self._post(
            "editMessageText",
            body,
            "message edit",
            acceptable_error_descriptions=("message is not modified",),
        )
        if response.get("ok") is not True:
            raise RuntimeError("Telegram rejected the message edit")

    async def remove_inline_keyboard(self, *, chat_id: int, message_id: int) -> None:
        """Remove stale controls without replacing the message text."""

        body: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        }
        response = await self._post(
            "editMessageReplyMarkup",
            body,
            "reply-markup edit",
            acceptable_error_descriptions=("message is not modified",),
        )
        if response.get("ok") is not True:
            raise RuntimeError("Telegram rejected the reply-markup edit")

    async def _post(
        self,
        method: str,
        body: dict[str, object],
        operation: str,
        *,
        acceptable_error_descriptions: tuple[str, ...] = (),
    ) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(f"{self._base_url}/{method}", json=body)
        except httpx.HTTPError as error:
            raise RuntimeError(f"Telegram {operation} failed") from error
        try:
            result = response.json()
        except ValueError as error:
            raise RuntimeError(f"Telegram returned an invalid {operation} response") from error
        if not isinstance(result, dict):
            raise RuntimeError(f"Telegram returned an invalid {operation} response")
        description = result.get("description")
        if not response.is_success:
            if isinstance(description, str) and any(
                accepted.casefold() in description.casefold()
                for accepted in acceptable_error_descriptions
            ):
                return {"ok": True, "result": True}
            raise RuntimeError(f"Telegram {operation} failed with HTTP {response.status_code}")
        return result


def _render_telegram_html(text: str) -> str:
    """Translate supported Markdown while keeping all model text safe for Telegram HTML."""

    rendered: list[str] = []
    cursor = 0
    for match in _FENCED_CODE_BLOCK.finditer(text):
        rendered.append(_render_plain_telegram_html(text[cursor : match.start()]))
        rendered.append(f"<pre>{escape(match.group('code'), quote=False)}</pre>")
        cursor = match.end()
    rendered.append(_render_plain_telegram_html(text[cursor:]))
    return "".join(rendered)


def _render_plain_telegram_html(text: str) -> str:
    escaped = escape(text, quote=False)
    return _BOLD_MARKDOWN.sub(r"<b>\1</b>", escaped)
