"""Minimal Telegram Bot API client for callbacks and outbound messages."""

import re
from dataclasses import dataclass
from html import escape

import httpx

TELEGRAM_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024
_BOLD_MARKDOWN = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", flags=re.DOTALL)
_ITALIC_ASTERISK_MARKDOWN = re.compile(
    r"(?<!\*)\*(?!\*)(?=\S)(.+?)(?<=\S)(?<!\*)\*(?!\*)",
    flags=re.DOTALL,
)
_ITALIC_UNDERSCORE_MARKDOWN = re.compile(
    r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])",
    flags=re.DOTALL,
)
_FENCED_CODE_BLOCK = re.compile(
    r"```[ \t]*(?:[A-Za-z0-9_+.-]+)?[ \t]*\r?\n"
    r"(?P<code>.*?)"
    r"(?:\r?\n)?```",
    flags=re.DOTALL,
)
_TABLE_DELIMITER_CELL = re.compile(r"^:?-{3,}:?$")


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
    """Render inline Markdown and turn unsupported GFM tables into aligned text."""

    lines = text.splitlines(keepends=True)
    rendered: list[str] = []
    plain_lines: list[str] = []
    index = 0
    while index < len(lines):
        header = _split_markdown_table_row(lines[index].rstrip("\r\n"))
        alignments = (
            _table_alignments(lines[index + 1].rstrip("\r\n"), len(header))
            if header is not None and index + 1 < len(lines)
            else None
        )
        if header is None or alignments is None:
            plain_lines.append(lines[index])
            index += 1
            continue

        if plain_lines:
            rendered.append(_render_inline_telegram_html("".join(plain_lines)))
            plain_lines.clear()

        table_rows = [header]
        row_index = index + 2
        while row_index < len(lines):
            row = _split_markdown_table_row(lines[row_index].rstrip("\r\n"))
            if row is None or len(row) != len(header):
                break
            table_rows.append(row)
            row_index += 1

        rendered.append(
            f"<pre>{escape(_format_plain_text_table(table_rows, alignments), quote=False)}</pre>"
        )
        if lines[row_index - 1].endswith(("\n", "\r")):
            rendered.append("\n")
        index = row_index

    if plain_lines:
        rendered.append(_render_inline_telegram_html("".join(plain_lines)))
    return "".join(rendered)


def _render_inline_telegram_html(text: str) -> str:
    escaped = escape(text, quote=False)
    rendered = _BOLD_MARKDOWN.sub(r"<b>\1</b>", escaped)
    rendered = _ITALIC_ASTERISK_MARKDOWN.sub(r"<i>\1</i>", rendered)
    return _ITALIC_UNDERSCORE_MARKDOWN.sub(r"<i>\1</i>", rendered)


def _split_markdown_table_row(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = tuple(cell.strip() for cell in stripped.split("|"))
    return cells if len(cells) >= 2 else None


def _table_alignments(line: str, column_count: int) -> tuple[str, ...] | None:
    cells = _split_markdown_table_row(line)
    if cells is None or len(cells) != column_count:
        return None
    if any(_TABLE_DELIMITER_CELL.fullmatch(cell) is None for cell in cells):
        return None
    return tuple(
        "center"
        if cell.startswith(":") and cell.endswith(":")
        else "right"
        if cell.endswith(":")
        else "left"
        for cell in cells
    )


def _format_plain_text_table(
    rows: list[tuple[str, ...]],
    alignments: tuple[str, ...],
) -> str:
    widths = [max(len(row[column]) for row in rows) for column in range(len(alignments))]

    def format_row(row: tuple[str, ...], *, header: bool = False) -> str:
        cells: list[str] = []
        for value, width, alignment in zip(row, widths, alignments, strict=True):
            if header or alignment == "left":
                cells.append(value.ljust(width))
            elif alignment == "right":
                cells.append(value.rjust(width))
            else:
                cells.append(value.center(width))
        return " | ".join(cells).rstrip()

    separator = "-+-".join("-" * width for width in widths)
    return "\n".join(
        [
            format_row(rows[0], header=True),
            separator,
            *(format_row(row) for row in rows[1:]),
        ]
    )
