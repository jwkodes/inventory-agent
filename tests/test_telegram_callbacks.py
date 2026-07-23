"""Tests for compact and opaque Telegram callback data."""

from uuid import UUID

import pytest

from inventory_agent.telegram.callbacks import (
    MAX_CALLBACK_BYTES,
    CallbackAction,
    CallbackCommand,
    decode_callback,
    encode_callback,
)


def test_variant_selection_round_trips_within_telegram_limit() -> None:
    command = CallbackCommand(
        action=CallbackAction.SELECT_VARIANT,
        target_id=UUID("41000000-0000-0000-0000-000000000001"),
        choice_id=UUID("21000000-0000-0000-0000-000000000001"),
    )

    encoded = encode_callback(command)

    assert len(encoded.encode()) <= MAX_CALLBACK_BYTES
    assert decode_callback(encoded) == command


def test_malformed_callback_is_rejected() -> None:
    with pytest.raises(ValueError, match="format"):
        decode_callback("not-a-valid-callback")
