"""Authenticated, read-only development dashboard routes."""

from __future__ import annotations

import secrets
from importlib.resources import files
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

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


def _read_secret(value: object) -> str | None:
    get_secret_value = getattr(value, "get_secret_value", None)
    if not callable(get_secret_value):
        return None
    secret_value = get_secret_value()
    if not isinstance(secret_value, str) or not secret_value:
        return None
    return secret_value
