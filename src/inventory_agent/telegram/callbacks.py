"""Compact opaque callback encoding within Telegram's 64-byte limit."""

import base64
import binascii
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

MAX_CALLBACK_BYTES = 64


class CallbackAction(StrEnum):
    CONFIRM_PROPOSAL = "c"
    CANCEL_PROPOSAL = "x"
    SELECT_VARIANT = "s"
    REVERSE_TRANSACTION = "r"
    CONFIRM_REVERSAL = "v"
    CANCEL_REVERSAL = "z"


@dataclass(frozen=True, slots=True)
class CallbackCommand:
    action: CallbackAction
    target_id: UUID
    choice_id: UUID | None = None


def encode_callback(command: CallbackCommand) -> str:
    parts = [command.action.value, _encode_uuid(command.target_id)]
    if command.choice_id is not None:
        parts.append(_encode_uuid(command.choice_id))
    value = ".".join(parts)
    if len(value.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError("Telegram callback data exceeds 64 bytes")
    return value


def decode_callback(value: str) -> CallbackCommand:
    if not value or len(value.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError("Invalid Telegram callback length")
    parts = value.split(".")
    if len(parts) not in (2, 3):
        raise ValueError("Invalid Telegram callback format")
    action = CallbackAction(parts[0])
    choice_id = _decode_uuid(parts[2]) if len(parts) == 3 else None
    if action is CallbackAction.SELECT_VARIANT and choice_id is None:
        raise ValueError("Variant selection requires a choice ID")
    if action is not CallbackAction.SELECT_VARIANT and choice_id is not None:
        raise ValueError("Only variant selection accepts a choice ID")
    return CallbackCommand(action=action, target_id=_decode_uuid(parts[1]), choice_id=choice_id)


def _encode_uuid(value: UUID) -> str:
    return base64.urlsafe_b64encode(value.bytes).decode("ascii").rstrip("=")


def _decode_uuid(value: str) -> UUID:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if len(raw) != 16:
            raise ValueError
        return UUID(bytes=raw)
    except (ValueError, binascii.Error) as error:
        raise ValueError("Invalid callback UUID") from error
