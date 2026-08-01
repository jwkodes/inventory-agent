"""Deterministic Telegram registration commands and durable notifications."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx

from inventory_agent.processing.models import OutboxDeliveryResult, OutboxDeliveryStatus

_REGISTER_COMMAND = re.compile(
    r"^/register(?:@[A-Za-z0-9_]{5,32})?(?:\s+(?P<code>\S.{0,199}))?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RegistrationCommand:
    """A private `/register` command with the raw invite code kept in memory only."""

    invite_code: str


@dataclass(frozen=True, slots=True)
class RegistrationApplicant:
    telegram_user_id: int
    telegram_username: str | None
    display_name: str
    private_chat_id: int


@dataclass(frozen=True, slots=True)
class RegistrationSubmission:
    status: str
    request_id: UUID | None = None
    organization_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ClaimedRegistrationNotification:
    id: UUID
    chat_id: int
    kind: str
    payload: dict[str, object]
    attempts: int


class RegistrationRepository(Protocol):
    async def submit_registration(
        self,
        *,
        invite_code_hash: str,
        applicant: RegistrationApplicant,
    ) -> RegistrationSubmission:
        """Validate an invite and create an approval request atomically."""


class RegistrationNotificationRepository(Protocol):
    async def claim_notification(self) -> ClaimedRegistrationNotification | None:
        """Lease one due registration notification."""

    async def complete_notification(
        self,
        *,
        notification_id: UUID,
        delivered: bool,
        error: str | None = None,
    ) -> str:
        """Delete a delivered notification or schedule its retry."""


class RegistrationNotificationSender(Protocol):
    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        """Send a new Telegram message."""


class SupabaseRegistrationRepository:
    """Call registration RPCs with the server-only Supabase credential."""

    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rest_url = f"{supabase_url.rstrip('/')}/rest/v1"
        self._headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def submit_registration(
        self,
        *,
        invite_code_hash: str,
        applicant: RegistrationApplicant,
    ) -> RegistrationSubmission:
        result = await self._rpc(
            "submit_organization_registration",
            {
                "p_code_hash": invite_code_hash,
                "p_telegram_user_id": applicant.telegram_user_id,
                "p_telegram_username": applicant.telegram_username,
                "p_display_name": applicant.display_name,
                "p_source_chat_id": applicant.private_chat_id,
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise ValueError("Supabase returned an invalid registration result")
        request_id = result.get("request_id")
        organization_id = result.get("organization_id")
        return RegistrationSubmission(
            status=result["status"],
            request_id=UUID(str(request_id)) if request_id is not None else None,
            organization_id=UUID(str(organization_id)) if organization_id is not None else None,
        )

    async def claim_notification(self) -> ClaimedRegistrationNotification | None:
        result = await self._rpc("claim_registration_telegram_notification", {})
        if result is None:
            return None
        if not isinstance(result, dict):
            raise ValueError("Supabase returned an invalid registration notification")
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Supabase returned an invalid registration notification payload")
        return ClaimedRegistrationNotification(
            id=UUID(str(result["id"])),
            chat_id=int(result["chat_id"]),
            kind=str(result["kind"]),
            payload=payload,
            attempts=int(result["attempts"]),
        )

    async def complete_notification(
        self,
        *,
        notification_id: UUID,
        delivered: bool,
        error: str | None = None,
    ) -> str:
        result = await self._rpc(
            "complete_registration_telegram_notification",
            {
                "p_notification_id": str(notification_id),
                "p_delivered": delivered,
                "p_error": error,
            },
        )
        if not isinstance(result, str):
            raise ValueError("Supabase returned an invalid notification completion result")
        return result

    async def _rpc(self, function_name: str, body: dict[str, object]) -> object:
        async with httpx.AsyncClient(
            base_url=self._rest_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(f"/rpc/{function_name}", json=body)
        response.raise_for_status()
        return response.json()


class TelegramRegistrationNotificationWorker:
    """Deliver registration state changes as new, restart-safe Telegram messages."""

    def __init__(
        self,
        *,
        repository: RegistrationNotificationRepository,
        sender: RegistrationNotificationSender,
    ) -> None:
        self._repository = repository
        self._sender = sender

    async def deliver_one(self) -> OutboxDeliveryResult:
        notification = await self._repository.claim_notification()
        if notification is None:
            return OutboxDeliveryResult(status=OutboxDeliveryStatus.IDLE)

        try:
            telegram_message_id = await self._sender.send_message(
                chat_id=notification.chat_id,
                text=render_registration_notification(notification),
            )
        except (RuntimeError, ValueError) as error:
            await self._repository.complete_notification(
                notification_id=notification.id,
                delivered=False,
                error=str(error),
            )
            return OutboxDeliveryResult(
                status=OutboxDeliveryStatus.RETRY_SCHEDULED,
                outbox_id=notification.id,
            )

        await self._repository.complete_notification(
            notification_id=notification.id,
            delivered=True,
        )
        return OutboxDeliveryResult(
            status=OutboxDeliveryStatus.SENT,
            outbox_id=notification.id,
            telegram_message_id=telegram_message_id,
        )


def parse_registration_command(text: str | None) -> RegistrationCommand | None:
    """Recognize only a complete Telegram registration command."""

    if text is None:
        return None
    match = _REGISTER_COMMAND.fullmatch(text.strip())
    if match is None:
        return None
    return RegistrationCommand(invite_code=(match.group("code") or "").strip())


def hash_invite_code(invite_code: str) -> str:
    """Produce the one-way value stored and compared by the database."""

    return hashlib.sha256(invite_code.encode("utf-8")).hexdigest()


def render_registration_notification(notification: ClaimedRegistrationNotification) -> str:
    organization_name = str(notification.payload.get("organization_name") or "the company")
    if notification.kind == "registration_pending":
        return (
            "🕒 **Registration submitted**\n\n"
            f"Your request to join **{organization_name}** is awaiting admin approval. "
            "I'll send a new message when a decision is made."
        )
    if notification.kind == "registration_approved":
        role = str(notification.payload.get("role") or "worker").replace("_", " ").title()
        return (
            "✅ **Registration approved**\n\n"
            f"You can now use the inventory bot for **{organization_name}** as **{role}**."
        )
    if notification.kind == "registration_rejected":
        return (
            "🚫 **Registration not approved**\n\n"
            "Your pending registration details have now been permanently removed."
        )
    if notification.kind == "registration_already_registered":
        return "✅ **Already registered**\n\nYou already have an active company membership."
    return (
        "⚠️ **Registration could not be submitted**\n\n"
        "That invite code is invalid, expired, or already used. "
        "Ask a company admin for a new code, then send `/register INVITE_CODE` here."
    )


__all__ = [
    "ClaimedRegistrationNotification",
    "RegistrationApplicant",
    "RegistrationCommand",
    "RegistrationNotificationRepository",
    "RegistrationRepository",
    "RegistrationSubmission",
    "SupabaseRegistrationRepository",
    "TelegramRegistrationNotificationWorker",
    "hash_invite_code",
    "parse_registration_command",
    "render_registration_notification",
]
