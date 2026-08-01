"""Authenticated Telegram webhook endpoint."""

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import ValidationError

from inventory_agent.config import Settings, get_settings
from inventory_agent.telegram.client import TelegramBotClient
from inventory_agent.telegram.dev_identity import (
    SupabaseTelegramDevIdentityRepository,
    TelegramDevIdentityRepository,
    TelegramDevIdentitySender,
    apply_dev_persona,
    handle_dev_user_command,
    parse_dev_user_command,
    telegram_chat_id,
)
from inventory_agent.telegram.group_activation import decide_group_activation
from inventory_agent.telegram.models import TelegramPayload, TelegramUpdate
from inventory_agent.telegram.registration import (
    RegistrationApplicant,
    RegistrationRepository,
    SupabaseRegistrationRepository,
    hash_invite_code,
    parse_registration_command,
)
from inventory_agent.telegram.repository import (
    SupabaseTelegramEventRepository,
    TelegramEventRepository,
)

router = APIRouter(prefix="/webhooks", tags=["telegram"])


def get_telegram_event_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> TelegramEventRepository | None:
    """Create the repository only when the webhook integration is called."""

    secret_key = _read_secret(settings.supabase_secret_key)
    if secret_key is None:
        return None
    return SupabaseTelegramEventRepository(
        supabase_url=settings.supabase_url,
        secret_key=secret_key,
    )


def get_registration_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> RegistrationRepository | None:
    """Create the registration boundary only when server credentials exist."""

    secret_key = _read_secret(settings.supabase_secret_key)
    if secret_key is None:
        return None
    return SupabaseRegistrationRepository(
        supabase_url=settings.supabase_url,
        secret_key=secret_key,
    )


def get_dev_identity_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> TelegramDevIdentityRepository | None:
    """Create the dev-only identity boundary only when explicitly enabled."""

    secret_key = _read_secret(settings.supabase_secret_key)
    if (
        settings.app_env != "development"
        or not settings.telegram_dev_user_simulation_enabled
        or secret_key is None
    ):
        return None
    return SupabaseTelegramDevIdentityRepository(
        supabase_url=settings.supabase_url,
        secret_key=secret_key,
    )


def get_telegram_command_sender(
    settings: Annotated[Settings, Depends(get_settings)],
) -> TelegramDevIdentitySender | None:
    """Send deterministic command notices through the configured real bot."""

    bot_token = _read_secret(settings.telegram_bot_token)
    if bot_token is None:
        return None
    return TelegramBotClient(bot_token=bot_token)


@router.post("/telegram")
async def receive_telegram_update(
    payload: TelegramPayload,
    settings: Annotated[Settings, Depends(get_settings)],
    repository: Annotated[
        TelegramEventRepository | None,
        Depends(get_telegram_event_repository),
    ],
    registrations: Annotated[
        RegistrationRepository | None,
        Depends(get_registration_repository),
    ],
    dev_identities: Annotated[
        TelegramDevIdentityRepository | None,
        Depends(get_dev_identity_repository),
    ],
    command_sender: Annotated[
        TelegramDevIdentitySender | None,
        Depends(get_telegram_command_sender),
    ],
    webhook_secret: Annotated[
        str | None,
        Header(alias="X-Telegram-Bot-Api-Secret-Token"),
    ] = None,
) -> dict[str, str]:
    """Authenticate, resolve tenancy, and persist a Telegram update exactly once."""

    expected_secret = _read_secret(settings.telegram_webhook_secret)
    if expected_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram webhook secret is not configured",
        )
    if webhook_secret is None or not secrets.compare_digest(webhook_secret, expected_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram webhook secret",
        )

    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase server credentials are not configured",
        )

    try:
        update = TelegramUpdate.model_validate(payload)
    except ValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error.errors(include_url=False),
        ) from error

    event_type = update.event_type
    controller_telegram_user_id = update.telegram_user_id
    if event_type is None or controller_telegram_user_id is None:
        return {"status": "ignored", "reason": "unsupported_update"}

    chat_id = telegram_chat_id(payload)
    dev_user_command = parse_dev_user_command(
        update.message.text if update.message is not None else None
    )
    if (
        dev_user_command is not None
        and settings.app_env == "development"
        and settings.telegram_dev_user_simulation_enabled
    ):
        if dev_identities is None or command_sender is None or chat_id is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Development Telegram user simulation is not fully configured",
            )
        command_status = await handle_dev_user_command(
            command=dev_user_command,
            controller_telegram_user_id=controller_telegram_user_id,
            chat_id=chat_id,
            repository=dev_identities,
            sender=command_sender,
            session_minutes=settings.telegram_dev_user_simulation_session_minutes,
        )
        return {"status": command_status}

    if (
        settings.app_env == "development"
        and settings.telegram_dev_user_simulation_enabled
        and dev_identities is not None
        and chat_id is not None
        and (
            persona := await dev_identities.resolve(
                controller_telegram_user_id=controller_telegram_user_id,
                chat_id=chat_id,
                session_minutes=settings.telegram_dev_user_simulation_session_minutes,
            )
        )
        is not None
    ):
        payload = apply_dev_persona(payload, persona=persona)
        update = TelegramUpdate.model_validate(payload)

    telegram_user_id = update.telegram_user_id
    if telegram_user_id is None:
        return {"status": "ignored", "reason": "unsupported_update"}

    registration_command = parse_registration_command(
        update.message.text if update.message is not None else None
    )
    if registration_command is not None:
        applicant = _registration_applicant(payload, telegram_user_id)
        if applicant is None:
            if command_sender is not None and chat_id is not None:
                await command_sender.send_message(
                    chat_id=chat_id,
                    text=(
                        "🔒 **Register in private chat**\n\n"
                        "I ignored and did not store this invite code. Because it was shared "
                        "in a group, ask an admin for a new code, then send `/register "
                        "INVITE_CODE` to me privately."
                    ),
                )
            return {"status": "ignored", "reason": "registration_requires_private_chat"}
        if registrations is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Registration storage is not configured",
            )
        registration_result = await registrations.submit_registration(
            invite_code_hash=hash_invite_code(registration_command.invite_code),
            applicant=applicant,
        )
        return {"status": registration_result.status}

    activation = decide_group_activation(
        payload,
        bot_username=settings.telegram_bot_username,
        bot_token=_read_secret(settings.telegram_bot_token),
    )
    if not activation.active:
        return {"status": "ignored", "reason": activation.reason}

    memberships = await repository.find_active_members(telegram_user_id)
    if not memberships:
        return {"status": "ignored", "reason": "unregistered_user"}
    if len(memberships) > 1:
        return {"status": "ignored", "reason": "organization_selection_required"}

    ingestion_result = await repository.ingest_event(
        member=memberships[0],
        update_id=update.update_id,
        event_type=event_type,
        payload=payload,
    )
    return {"status": ingestion_result.value}


def _registration_applicant(
    payload: TelegramPayload,
    telegram_user_id: int,
) -> RegistrationApplicant | None:
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or chat.get("type") != "private" or not isinstance(sender, dict):
        return None
    chat_id = chat.get("id")
    if not isinstance(chat_id, int) or chat_id <= 0:
        return None

    username_value = sender.get("username")
    username = username_value if isinstance(username_value, str) and username_value else None
    name_parts = [
        value.strip()
        for key in ("first_name", "last_name")
        if isinstance((value := sender.get(key)), str) and value.strip()
    ]
    display_name = " ".join(name_parts) or (
        f"@{username}" if username else f"Telegram {telegram_user_id}"
    )
    return RegistrationApplicant(
        telegram_user_id=telegram_user_id,
        telegram_username=username,
        display_name=display_name,
        private_chat_id=chat_id,
    )


def _read_secret(value: object) -> str | None:
    """Read a Pydantic SecretStr without accepting an empty configured value."""

    get_secret_value = getattr(value, "get_secret_value", None)
    if not callable(get_secret_value):
        return None
    secret_value = get_secret_value()
    if not isinstance(secret_value, str) or not secret_value:
        return None
    return secret_value
