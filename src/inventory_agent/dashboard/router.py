"""Authenticated development dashboard routes."""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field

from inventory_agent.config import Settings, get_settings
from inventory_agent.dashboard.prompts import prompt_catalog
from inventory_agent.dashboard.repository import DashboardRepository
from inventory_agent.dashboard.supervisor import SupervisorClient
from inventory_agent.telegram.registration import hash_invite_code

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


class ProcessCommand(BaseModel):
    """Allowlisted development-process control command."""

    model_config = ConfigDict(extra="forbid")

    service: Literal["api", "worker", "all"]


class RegistrationInviteCreate(BaseModel):
    """Bounded invite lifetime and use count selected by an admin."""

    model_config = ConfigDict(extra="forbid")

    expires_in_hours: int = Field(default=72, ge=1, le=24 * 30)
    max_uses: int = Field(default=1, ge=1, le=1000)


class RegistrationApproval(BaseModel):
    """The role assigned by an admin during approval."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["worker", "manager", "admin"] = "worker"


class InventoryResetConfirmation(BaseModel):
    """Explicit destructive reset acknowledgement."""

    model_config = ConfigDict(extra="forbid")

    confirmation: str = Field(min_length=7, max_length=150)


def get_supervisor_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SupervisorClient | None:
    token = _read_secret(settings.dev_supervisor_token) or _read_secret(
        settings.dev_dashboard_token
    )
    if not settings.dev_supervisor_enabled or token is None:
        return None
    return SupervisorClient(base_url=settings.dev_supervisor_url, token=token)


SupervisorData = Annotated[SupervisorClient | None, Depends(get_supervisor_client)]


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


@router.post("/api/inventory/reset")
async def reset_inventory(
    organization_id: UUID,
    body: InventoryResetConfirmation,
    request: Request,
    _: DashboardAccess,
    repository: DashboardData,
    supervisor: SupervisorData,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Pause processing and atomically clear one company's operational test data."""

    _require_loopback_dashboard(request)
    _require_config_writes(settings)
    if supervisor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The development supervisor is required for a safe inventory reset",
        )
    worker_was_running = False
    try:
        snapshot = await supervisor.status()
        worker_was_running = _worker_running(snapshot)
        if worker_was_running:
            await supervisor.command(action="stop", service="worker")
            await _wait_for_worker(supervisor, running=False)
        result = await _require_repository(repository).reset_inventory_data(
            organization_id=organization_id,
            confirmation=body.confirmation,
        )
    except (httpx.HTTPError, ValueError) as exc:
        try:
            if worker_was_running:
                await supervisor.command(action="start", service="worker")
        finally:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Inventory reset could not be completed safely: {exc}",
            ) from exc

    if worker_was_running:
        try:
            await supervisor.command(action="start", service="worker")
            await _wait_for_worker(supervisor, running=True)
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Inventory data was reset, but the message processor could not be "
                    f"restarted: {exc}"
                ),
            ) from exc
    return {**result, "processor_restarted": worker_was_running}


@router.get("/api/memberships")
async def membership_administration(
    organization_id: UUID,
    _: DashboardAccess,
    repository: DashboardData,
) -> dict[str, object]:
    return await _require_repository(repository).get_membership_administration(
        organization_id=organization_id
    )


@router.post("/api/membership-invites", status_code=status.HTTP_201_CREATED)
async def create_membership_invite(
    organization_id: UUID,
    body: RegistrationInviteCreate,
    request: Request,
    _: DashboardAccess,
    repository: DashboardData,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _require_loopback_dashboard(request)
    _require_config_writes(settings)
    invite_code = f"INV-{secrets.token_urlsafe(12)}"
    expires_at = datetime.now(UTC) + timedelta(hours=body.expires_in_hours)
    invite = await _require_repository(repository).create_registration_invite(
        organization_id=organization_id,
        code_hash=hash_invite_code(invite_code),
        code_hint=invite_code[-6:],
        expires_at=expires_at.isoformat(),
        max_uses=body.max_uses,
    )
    return {
        "invite": invite,
        "invite_code": invite_code,
        "command": f"/register {invite_code}",
        "shown_once": True,
    }


@router.post("/api/registration-requests/{registration_request_id}/approve")
async def approve_registration(
    registration_request_id: UUID,
    organization_id: UUID,
    body: RegistrationApproval,
    request: Request,
    _: DashboardAccess,
    repository: DashboardData,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _require_loopback_dashboard(request)
    _require_config_writes(settings)
    return await _require_repository(repository).approve_registration(
        organization_id=organization_id,
        registration_request_id=registration_request_id,
        role=body.role,
    )


@router.post("/api/registration-requests/{registration_request_id}/reject")
async def reject_registration(
    registration_request_id: UUID,
    organization_id: UUID,
    request: Request,
    _: DashboardAccess,
    repository: DashboardData,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _require_loopback_dashboard(request)
    _require_config_writes(settings)
    return await _require_repository(repository).reject_registration(
        organization_id=organization_id,
        registration_request_id=registration_request_id,
    )


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
        "models": _model_configuration(settings),
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


@router.get("/api/system")
async def system_status(
    _: DashboardAccess,
    repository: DashboardData,
    supervisor: SupervisorData,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    components: list[dict[str, object]] = []
    supervisor_status: dict[str, object] | None = None
    supervisor_error: str | None = None
    if supervisor is None:
        supervisor_error = "Development supervisor is disabled or missing its token"
    else:
        try:
            supervisor_status = await supervisor.status()
        except (httpx.HTTPError, ValueError) as exc:
            supervisor_error = str(exc)
    components.append(
        _component(
            "supervisor",
            supervisor_status is not None,
            "Loopback control service is responding"
            if supervisor_status is not None
            else supervisor_error or "Supervisor unavailable",
        )
    )

    services = supervisor_status.get("services") if isinstance(supervisor_status, dict) else None
    service_rows = services if isinstance(services, dict) else {}
    api_service = service_rows.get("api")
    worker_service = service_rows.get("worker")
    api_managed = isinstance(api_service, dict) and api_service.get("running") is True
    components.append(
        _component(
            "api",
            True,
            "Dashboard API is responding"
            + (" and supervisor-managed" if api_managed else " (not supervisor-managed)"),
        )
    )
    components.append(
        _component(
            "worker",
            isinstance(worker_service, dict) and worker_service.get("running") is True,
            "Background worker process is running"
            if isinstance(worker_service, dict) and worker_service.get("running") is True
            else "Background worker is not running under the supervisor",
        )
    )

    try:
        organizations = await _require_repository(repository).list_organizations()
        components.append(
            _component(
                "supabase",
                True,
                f"Database API responded; {len(organizations)} organization(s) visible",
            )
        )
    except (httpx.HTTPError, ValueError) as exc:
        components.append(_component("supabase", False, str(exc)))

    tunnel_ok, tunnel_detail = await _telegram_tunnel_health(settings)
    components.append(_component("telegram tunnel", tunnel_ok, tunnel_detail))
    return {
        "controls_enabled": supervisor is not None,
        "components": components,
        "services": service_rows,
    }


@router.post("/api/system/{action}", status_code=status.HTTP_202_ACCEPTED)
async def system_command(
    action: Literal["start", "restart", "stop"],
    body: ProcessCommand,
    request: Request,
    _: DashboardAccess,
    supervisor: SupervisorData,
) -> dict[str, object]:
    _require_loopback_dashboard(request)
    if supervisor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Development supervisor is disabled or unavailable",
        )
    try:
        return await supervisor.command(action=action, service=body.service)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Development supervisor request failed: {exc}",
        ) from exc


@router.get("/api/prompts")
async def prompts(
    _: DashboardAccess,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return {
        "prompts": _current_prompt_catalog(settings),
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
        "telegram_dev_user_simulation_enabled": (settings.telegram_dev_user_simulation_enabled),
        "telegram_dev_user_simulation_session_minutes": (
            settings.telegram_dev_user_simulation_session_minutes
        ),
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


def _current_prompt_catalog(settings: Settings) -> list[dict[str, object]]:
    return prompt_catalog(
        agent_model=settings.inventory_agent_model,
        agent_reasoning_effort=settings.inventory_agent_reasoning_effort,
        extraction_model=settings.openai_model,
        extraction_reasoning_effort=settings.openai_reasoning_effort,
        embedding_model=settings.openai_embedding_model,
        agent_enabled=settings.inventory_agent_enabled,
        context_policy=settings.inventory_agent_context_policy,
        candidate_judging_enabled=settings.inventory_candidate_judging_enabled,
        matching_strategy=settings.inventory_matching_strategy,
    )


def _model_configuration(settings: Settings) -> list[dict[str, object]]:
    fields = (
        "layer",
        "label",
        "model",
        "reasoning_effort",
        "runtime_status",
        "when_called",
        "action",
    )
    return [
        {field: component[field] for field in fields}
        for component in _current_prompt_catalog(settings)
    ]


def _component(name: str, healthy: bool, detail: str) -> dict[str, object]:
    return {
        "name": name,
        "healthy": healthy,
        "status": "healthy" if healthy else "error",
        "detail": detail,
    }


async def _telegram_tunnel_health(settings: Settings) -> tuple[bool, str]:
    webhook_url = settings.telegram_webhook_url
    if not webhook_url:
        return False, "TELEGRAM_WEBHOOK_URL is not configured"
    try:
        url = httpx.URL(webhook_url).copy_with(path="/health", query=None)
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
            response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("status") == "ok":
            return True, f"Public tunnel reached {url.host}"
        return False, "Public tunnel returned an unexpected health response"
    except (httpx.HTTPError, ValueError) as exc:
        return False, f"Public tunnel health check failed: {exc}"


def _require_loopback_dashboard(request: Request) -> None:
    if request.url.hostname not in {"127.0.0.1", "localhost"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Process controls are available only through the local dashboard URL",
        )


def _worker_running(snapshot: dict[str, object]) -> bool:
    services = snapshot.get("services")
    if not isinstance(services, dict):
        raise ValueError("Development supervisor returned no service state")
    worker = services.get("worker")
    if not isinstance(worker, dict):
        raise ValueError("Development supervisor returned no worker state")
    return worker.get("running") is True


async def _wait_for_worker(
    supervisor: SupervisorClient,
    *,
    running: bool,
    attempts: int = 50,
) -> None:
    for _ in range(attempts):
        if _worker_running(await supervisor.status()) is running:
            return
        await asyncio.sleep(0.1)
    state = "start" if running else "stop"
    raise ValueError(f"Message processor did not {state} in time")


def _read_secret(value: object) -> str | None:
    get_secret_value = getattr(value, "get_secret_value", None)
    if not callable(get_secret_value):
        return None
    secret_value = get_secret_value()
    if not isinstance(secret_value, str) or not secret_value:
        return None
    return secret_value
