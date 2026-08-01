"""Tests for safe Telegram group activation and LLM-facing text cleanup."""

from inventory_agent.telegram.group_activation import (
    decide_group_activation,
    strip_bot_reference,
)
from inventory_agent.telegram.models import TelegramPayload

BOT_USERNAME = "capybababot"
BOT_TOKEN = "8992832449:test-secret"


def group_message(text: str, **message_fields: object) -> TelegramPayload:
    return {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "from": {"id": 307432432},
            "chat": {"id": -5338267069, "type": "group"},
            "text": text,
            **message_fields,
        },
    }


def test_private_message_does_not_require_activation() -> None:
    payload = group_message("show inventory")
    payload["message"]["chat"]["type"] = "private"  # type: ignore[index]

    decision = decide_group_activation(
        payload,
        bot_username=BOT_USERNAME,
        bot_token=BOT_TOKEN,
    )

    assert decision.active is True
    assert decision.reason == "not_group_message"


def test_unaddressed_group_message_is_ignored() -> None:
    decision = decide_group_activation(
        group_message("show inventory"),
        bot_username=BOT_USERNAME,
        bot_token=BOT_TOKEN,
    )

    assert decision.active is False
    assert decision.reason == "group_message_not_addressed"


def test_exact_bot_mention_activates_group_message() -> None:
    for text in (
        "@capybababot show inventory",
        "show inventory @capybababot",
        "Can you help, @CapyBabaBot?",
    ):
        decision = decide_group_activation(
            group_message(text),
            bot_username=BOT_USERNAME,
            bot_token=BOT_TOKEN,
        )

        assert decision.active is True
        assert decision.reason == "bot_mention"


def test_other_username_does_not_activate_group_message() -> None:
    decision = decide_group_activation(
        group_message("@differentbot show inventory"),
        bot_username=BOT_USERNAME,
        bot_token=BOT_TOKEN,
    )

    assert decision.active is False


def test_bot_commands_activate_group_message() -> None:
    bare = decide_group_activation(
        group_message("/start"),
        bot_username=BOT_USERNAME,
        bot_token=BOT_TOKEN,
    )
    addressed = decide_group_activation(
        group_message("/inventory@capybababot show stock"),
        bot_username=BOT_USERNAME,
        bot_token=BOT_TOKEN,
    )
    other = decide_group_activation(
        group_message("/inventory@differentbot show stock"),
        bot_username=BOT_USERNAME,
        bot_token=BOT_TOKEN,
    )

    assert bare.active is True
    assert bare.reason == "bot_command"
    assert addressed.active is True
    assert addressed.reason == "addressed_bot_command"
    assert other.active is False


def test_reply_to_this_bot_activates_group_message() -> None:
    payload = group_message(
        "show transactions",
        reply_to_message={
            "message_id": 9,
            "from": {
                "id": 8992832449,
                "is_bot": True,
                "username": BOT_USERNAME,
            },
        },
    )

    decision = decide_group_activation(
        payload,
        bot_username=BOT_USERNAME,
        bot_token=BOT_TOKEN,
    )

    assert decision.active is True
    assert decision.reason == "reply_to_bot"


def test_reply_to_another_bot_does_not_activate_group_message() -> None:
    payload = group_message(
        "show transactions",
        reply_to_message={
            "message_id": 9,
            "from": {
                "id": 123456,
                "is_bot": True,
                "username": "differentbot",
            },
        },
    )

    decision = decide_group_activation(
        payload,
        bot_username=BOT_USERNAME,
        bot_token=BOT_TOKEN,
    )

    assert decision.active is False


def test_image_caption_can_activate_group_message() -> None:
    payload = group_message("")
    message = payload["message"]
    assert isinstance(message, dict)
    message.pop("text")
    message["caption"] = "@capybababot receive this invoice"
    message["photo"] = [{"file_id": "photo", "width": 100, "height": 100}]

    decision = decide_group_activation(
        payload,
        bot_username=BOT_USERNAME,
        bot_token=BOT_TOKEN,
    )

    assert decision.active is True
    assert decision.reason == "bot_mention"


def test_bot_reference_is_removed_before_model_processing() -> None:
    assert (
        strip_bot_reference(
            "@capybababot  can you retrieve my past transactions",
            bot_username=BOT_USERNAME,
        )
        == "can you retrieve my past transactions"
    )
    assert (
        strip_bot_reference(
            "can you retrieve my past transactions @capybababot",
            bot_username=f"@{BOT_USERNAME}",
        )
        == "can you retrieve my past transactions"
    )
    assert (
        strip_bot_reference(
            "/inventory@capybababot show stock",
            bot_username=BOT_USERNAME,
        )
        == "/inventory show stock"
    )


def test_mention_only_text_is_not_reduced_to_an_empty_request() -> None:
    assert (
        strip_bot_reference(
            "@capybababot",
            bot_username=BOT_USERNAME,
        )
        == "@capybababot"
    )
