"""Authenticated Telegram webhook endpoint."""

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import ValidationError

from inventory_agent.config import Settings, get_settings
from inventory_agent.telegram.models import TelegramPayload, TelegramUpdate
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


@router.post("/telegram")
async def receive_telegram_update(
    payload: TelegramPayload,
    settings: Annotated[Settings, Depends(get_settings)],
    repository: Annotated[
        TelegramEventRepository | None,
        Depends(get_telegram_event_repository),
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
    telegram_user_id = update.telegram_user_id
    if event_type is None or telegram_user_id is None:
        return {"status": "ignored", "reason": "unsupported_update"}

    memberships = await repository.find_active_members(telegram_user_id)
    if not memberships:
        return {"status": "ignored", "reason": "unregistered_user"}
    if len(memberships) > 1:
        return {"status": "ignored", "reason": "organization_selection_required"}

    result = await repository.ingest_event(
        member=memberships[0],
        update_id=update.update_id,
        event_type=event_type,
        payload=payload,
    )
    return {"status": result.value}


def _read_secret(value: object) -> str | None:
    """Read a Pydantic SecretStr without accepting an empty configured value."""

    get_secret_value = getattr(value, "get_secret_value", None)
    if not callable(get_secret_value):
        return None
    secret_value = get_secret_value()
    if not isinstance(secret_value, str) or not secret_value:
        return None
    return secret_value
