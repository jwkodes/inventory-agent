"""Tests for registration command parsing and durable Telegram delivery."""

from dataclasses import dataclass, field
from uuid import UUID

from inventory_agent.processing.models import OutboxDeliveryStatus
from inventory_agent.telegram.registration import (
    ClaimedRegistrationNotification,
    TelegramRegistrationNotificationWorker,
    hash_invite_code,
    parse_registration_command,
)

NOTIFICATION_ID = UUID("71000000-0000-0000-0000-000000000001")


@dataclass
class FakeNotificationRepository:
    notification: ClaimedRegistrationNotification | None
    completions: list[dict[str, object]] = field(default_factory=list)

    async def claim_notification(self) -> ClaimedRegistrationNotification | None:
        return self.notification

    async def complete_notification(
        self,
        *,
        notification_id: UUID,
        delivered: bool,
        error: str | None = None,
    ) -> str:
        self.completions.append(
            {
                "notification_id": notification_id,
                "delivered": delivered,
                "error": error,
            }
        )
        return "delivered" if delivered else "retry_scheduled"


@dataclass
class FakeSender:
    error: Exception | None = None
    messages: list[dict[str, object]] = field(default_factory=list)

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        if self.error is not None:
            raise self.error
        self.messages.append({"chat_id": chat_id, "text": text, "inline_keyboard": inline_keyboard})
        return 808


def test_registration_command_supports_bot_suffix_and_never_changes_code_case() -> None:
    command = parse_registration_command("/register@capybababot INV-AbC_123")

    assert command is not None
    assert command.invite_code == "INV-AbC_123"
    assert hash_invite_code(command.invite_code) == (
        "38157c876b0807314b9df8c91afd6d824b4302e2fdfb8ac271d302edf6f875bb"
    )


def test_non_registration_text_is_not_intercepted() -> None:
    assert parse_registration_command("please register these goods") is None


async def test_approval_notification_is_sent_as_a_new_message_and_completed() -> None:
    repository = FakeNotificationRepository(
        ClaimedRegistrationNotification(
            id=NOTIFICATION_ID,
            chat_id=222333444,
            kind="registration_approved",
            payload={"organization_name": "Cabybaba Pte Ltd", "role": "manager"},
            attempts=1,
        )
    )
    sender = FakeSender()

    result = await TelegramRegistrationNotificationWorker(
        repository=repository,
        sender=sender,
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.SENT
    assert result.telegram_message_id == 808
    assert sender.messages[0]["chat_id"] == 222333444
    assert "Registration approved" in str(sender.messages[0]["text"])
    assert "Manager" in str(sender.messages[0]["text"])
    assert repository.completions == [
        {
            "notification_id": NOTIFICATION_ID,
            "delivered": True,
            "error": None,
        }
    ]


async def test_rejected_applicant_is_not_completed_when_telegram_delivery_fails() -> None:
    repository = FakeNotificationRepository(
        ClaimedRegistrationNotification(
            id=NOTIFICATION_ID,
            chat_id=222333444,
            kind="registration_rejected",
            payload={},
            attempts=1,
        )
    )

    result = await TelegramRegistrationNotificationWorker(
        repository=repository,
        sender=FakeSender(error=RuntimeError("Telegram unavailable")),
    ).deliver_one()

    assert result.status is OutboxDeliveryStatus.RETRY_SCHEDULED
    assert repository.completions == [
        {
            "notification_id": NOTIFICATION_ID,
            "delivered": False,
            "error": "Telegram unavailable",
        }
    ]
