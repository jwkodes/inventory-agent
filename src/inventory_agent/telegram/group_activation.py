"""Safe activation rules for Telegram group and supergroup messages."""

from __future__ import annotations

import re
from dataclasses import dataclass

from inventory_agent.telegram.models import TelegramPayload

GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})


@dataclass(frozen=True, slots=True)
class GroupActivationDecision:
    """Whether an authenticated Telegram update should enter inventory processing."""

    active: bool
    reason: str


def decide_group_activation(
    payload: TelegramPayload,
    *,
    bot_username: str | None,
    bot_token: str | None,
) -> GroupActivationDecision:
    """Require a direct bot reference before accepting an ordinary group message.

    Private messages, callbacks, and non-message updates retain their existing behavior.
    Telegram group privacy can therefore be disabled without sending unrelated group
    conversation to storage, workers, or OpenAI.
    """

    message = payload.get("message")
    if not isinstance(message, dict):
        return GroupActivationDecision(active=True, reason="not_group_message")

    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") not in GROUP_CHAT_TYPES:
        return GroupActivationDecision(active=True, reason="not_group_message")

    normalized_username = normalize_bot_username(bot_username)
    content = _message_content(message)
    if content is not None:
        command_target = _command_target(content)
        if command_target == "":
            return GroupActivationDecision(active=True, reason="bot_command")
        if normalized_username is not None and command_target == normalized_username.casefold():
            return GroupActivationDecision(active=True, reason="addressed_bot_command")
        if normalized_username is not None and _mentions_bot(content, normalized_username):
            return GroupActivationDecision(active=True, reason="bot_mention")

    if _replies_to_bot(
        message,
        bot_username=normalized_username,
        bot_id=_bot_id_from_token(bot_token),
    ):
        return GroupActivationDecision(active=True, reason="reply_to_bot")

    return GroupActivationDecision(active=False, reason="group_message_not_addressed")


def strip_bot_reference(text: str, *, bot_username: str | None) -> str:
    """Remove this bot's command suffix and ordinary mention before LLM processing."""

    normalized_username = normalize_bot_username(bot_username)
    if normalized_username is None:
        return text

    escaped_username = re.escape(normalized_username)
    cleaned = re.sub(
        rf"^(/[A-Za-z0-9_]+)@{escaped_username}(?=\s|$)",
        r"\1",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        rf"(?<![A-Za-z0-9_])@{escaped_username}(?![A-Za-z0-9_])",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    normalized = cleaned.strip()
    return normalized or text.strip()


def normalize_bot_username(bot_username: str | None) -> str | None:
    """Normalize a configured Telegram username without accepting malformed values."""

    if bot_username is None:
        return None
    normalized = bot_username.strip().removeprefix("@")
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", normalized) is None:
        return None
    return normalized


def _message_content(message: dict[object, object]) -> str | None:
    text = message.get("text")
    if isinstance(text, str):
        return text
    caption = message.get("caption")
    return caption if isinstance(caption, str) else None


def _command_target(text: str) -> str | None:
    match = re.match(
        r"^/[A-Za-z0-9_]+(?:@([A-Za-z0-9_]+))?(?=\s|$)",
        text.strip(),
    )
    if match is None:
        return None
    target = match.group(1)
    return "" if target is None else target.casefold()


def _mentions_bot(text: str, bot_username: str) -> bool:
    return (
        re.search(
            rf"(?<![A-Za-z0-9_])@{re.escape(bot_username)}(?![A-Za-z0-9_])",
            text,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _replies_to_bot(
    message: dict[object, object],
    *,
    bot_username: str | None,
    bot_id: int | None,
) -> bool:
    replied_to = message.get("reply_to_message")
    if not isinstance(replied_to, dict):
        return False
    sender = replied_to.get("from")
    if not isinstance(sender, dict):
        return False

    sender_id = sender.get("id")
    if bot_id is not None and sender_id == bot_id:
        return True

    sender_username = sender.get("username")
    return (
        bot_username is not None
        and isinstance(sender_username, str)
        and sender_username.casefold() == bot_username.casefold()
    )


def _bot_id_from_token(bot_token: str | None) -> int | None:
    if bot_token is None:
        return None
    identifier, separator, _secret = bot_token.partition(":")
    if not separator or not identifier.isdigit():
        return None
    return int(identifier)
