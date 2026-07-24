"""Authenticated development dashboard routes."""

from __future__ import annotations

import secrets
from importlib.resources import files
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field

from inventory_agent.config import Settings, get_settings
from inventory_agent.dashboard.prompts import prompt_catalog
from inventory_agent.dashboard.repository import DashboardRepository

router = APIRouter(prefix="/dev", include_in_schema=False)
_basic = HTTPBasic(auto_error=False)


def get_dashboard_repository(
    settings: Annotated[Settings, Depends(get_settings)],
) -> DashboardRepository | None:
    secret_key = _read_secret(settings.supabase_secret_key)
    if secret_key is None:
        return None
    return DashboardRepository(
        supabase_url=settings.supabase_url,
        secret_key=secret_key,
    )


def require_dashboard_access(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> None:
    """Hide the dashboard unless explicitly enabled and authenticate every request."""

    if not settings.dev_dashboard_enabled or settings.app_env == "production":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    expected_token = _read_secret(settings.dev_dashboard_token)
    if expected_token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Development dashboard token is not configured",
        )
    valid = (
        credentials is not None
        and secrets.compare_digest(credentials.username, settings.dev_dashboard_username)
        and secrets.compare_digest(credentials.password, expected_token)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid development dashboard credentials",
            headers={"WWW-Authenticate": 'Basic realm="Inventory Agent Dev"'},
        )


DashboardAccess = Annotated[None, Depends(require_dashboard_access)]
DashboardData = Annotated[
    DashboardRepository | None,
    Depends(get_dashboard_repository),
]


class ContextSettingsUpdate(BaseModel):
    """Allowlisted, non-secret organization context settings."""

    model_config = ConfigDict(extra="forbid")

    policy: Literal["discard", "summarize"]
    retention_days: int = Field(ge=1)
    max_tokens: int = Field(ge=1)
    max_items: int = Field(ge=1, le=350)


@router.get("", response_class=HTMLResponse)
async def dashboard(_: DashboardAccess) -> HTMLResponse:
    html = files("inventory_agent.dashboard").joinpath("index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@router.get("/api/organizations")
async def organizations(
    _: DashboardAccess,
    repository: DashboardData,
) -> dict[str, object]:
    return {"organizations": await _require_repository(repository).list_organizations()}


@router.get("/api/events")
async def events(
    organization_id: UUID,
    _: DashboardAccess,
    repository: DashboardData,
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
) -> dict[str, object]:
    rows = await _require_repository(repository).list_events(
        organization_id=organization_id,
        limit=limit,
    )
    return {"events": rows}


@router.get("/api/events/{event_id}")
async def flow(
    event_id: UUID,
    _: DashboardAccess,
    repository: DashboardData,
) -> dict[str, object]:
    result = await _require_repository(repository).get_flow(event_id=event_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return result


@router.get("/api/inventory")
async def inventory(
    organization_id: UUID,
    _: DashboardAccess,
    repository: DashboardData,
) -> dict[str, object]:
    return await _require_repository(repository).get_inventory(organization_id=organization_id)


@router.get("/api/configuration")
async def configuration(
    organization_id: UUID,
    _: DashboardAccess,
    repository: DashboardData,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    data = _require_repository(repository)
    override = await data.get_context_settings(organization_id=organization_id)
    defaults: dict[str, object] = {
        "policy": settings.inventory_agent_context_policy,
        "retention_days": settings.inventory_agent_context_retention_days,
        "max_tokens": settings.inventory_agent_context_max_tokens,
        "max_items": settings.inventory_agent_context_max_items,
    }
    return {
        "context": {
            "defaults": defaults,
            "override": override,
            "effective": override or defaults,
            "source": "organization override" if override is not None else "application default",
            "applies": "next agent message",
            "restart_required": False,
        },
        "writes_enabled": settings.dev_dashboard_config_writes_enabled,
        "changes": await data.list_setting_changes(organization_id=organization_id),
        "runtime": _runtime_configuration(settings),
    }


@router.put("/api/configuration/context")
async def update_context_configuration(
    organization_id: UUID,
    body: ContextSettingsUpdate,
    _: DashboardAccess,
    repository: DashboardData,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _require_config_writes(settings)
    saved = await _require_repository(repository).set_context_settings(
        organization_id=organization_id,
        policy=body.policy,
        retention_days=body.retention_days,
        max_tokens=body.max_tokens,
        max_items=body.max_items,
        changed_by=f"dashboard:{settings.dev_dashboard_username}",
    )
    return {"context": saved, "applies": "next agent message", "restart_required": False}


@router.delete("/api/configuration/context")
async def reset_context_configuration(
    organization_id: UUID,
    _: DashboardAccess,
    repository: DashboardData,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _require_config_writes(settings)
    cleared = await _require_repository(repository).clear_context_settings(
        organization_id=organization_id,
        changed_by=f"dashboard:{settings.dev_dashboard_username}",
    )
    return {"cleared": cleared is not None, "restart_required": False}


@router.get("/api/conversations")
async def conversations(
    organization_id: UUID,
    _: DashboardAccess,
    repository: DashboardData,
) -> dict[str, object]:
    rows = await _require_repository(repository).list_conversations(organization_id=organization_id)
    return {"conversations": rows}


@router.get("/api/conversations/{conversation_id}")
async def conversation(
    conversation_id: UUID,
    organization_id: UUID,
    _: DashboardAccess,
    repository: DashboardData,
) -> dict[str, object]:
    result = await _require_repository(repository).get_conversation(
        organization_id=organization_id,
        conversation_id=conversation_id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )
    return result


@router.get("/api/prompts")
async def prompts(
    _: DashboardAccess,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return {
        "prompts": prompt_catalog(
            agent_model=settings.inventory_agent_model,
            extraction_model=settings.openai_model,
            embedding_model=settings.openai_embedding_model,
        ),
        "configuration": {
            "agent_enabled": settings.inventory_agent_enabled,
            "agent_reasoning_effort": settings.inventory_agent_reasoning_effort,
            "agent_context_policy": settings.inventory_agent_context_policy,
            "agent_context_retention_days": settings.inventory_agent_context_retention_days,
            "agent_context_max_tokens": settings.inventory_agent_context_max_tokens,
            "agent_context_max_items": settings.inventory_agent_context_max_items,
            "extraction_reasoning_effort": settings.openai_reasoning_effort,
            "matching_strategy": settings.inventory_matching_strategy,
            "candidate_judging_enabled": settings.inventory_candidate_judging_enabled,
            "embedding_dimensions": settings.openai_embedding_dimensions,
        },
    }


def _require_repository(repository: DashboardRepository | None) -> DashboardRepository:
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase server credentials are not configured",
        )
    return repository


def _require_config_writes(settings: Settings) -> None:
    if not settings.dev_dashboard_config_writes_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Development dashboard configuration writes are disabled",
        )


def _runtime_configuration(settings: Settings) -> list[dict[str, object]]:
    """Expose useful non-secret runtime values without offering unsafe mutation."""

    values = {
        "app_env": settings.app_env,
        "log_level": settings.log_level,
        "inventory_agent_enabled": settings.inventory_agent_enabled,
        "inventory_agent_model": settings.inventory_agent_model,
        "inventory_agent_reasoning_effort": settings.inventory_agent_reasoning_effort,
        "extraction_model": settings.openai_model,
        "extraction_reasoning_effort": settings.openai_reasoning_effort,
        "embedding_model": settings.openai_embedding_model,
        "embedding_dimensions": settings.openai_embedding_dimensions,
        "matching_strategy": settings.inventory_matching_strategy,
        "candidate_judging_enabled": settings.inventory_candidate_judging_enabled,
        "storage_bucket": settings.supabase_storage_bucket,
    }
    return [
        {
            "key": key,
            "value": value,
            "source": "application environment",
            "editable": False,
            "restart_required": True,
        }
        for key, value in values.items()
    ]


def _read_secret(value: object) -> str | None:
    get_secret_value = getattr(value, "get_secret_value", None)
    if not callable(get_secret_value):
        return None
    secret_value = get_secret_value()
    if not isinstance(secret_value, str) or not secret_value:
        return None
    return secret_value
