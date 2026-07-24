"""Loopback-only supervisor for the local API and worker processes."""

from __future__ import annotations

import asyncio
import logging
import secrets
import sys
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict

from inventory_agent.config import Settings, get_settings

logger = logging.getLogger(__name__)


class ServiceName(StrEnum):
    API = "api"
    WORKER = "worker"
    ALL = "all"


class LifecycleAction(StrEnum):
    START = "start"
    RESTART = "restart"
    STOP = "stop"


class ServiceCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: ServiceName


@dataclass
class ManagedService:
    name: ServiceName
    argv: tuple[str, ...]
    process: asyncio.subprocess.Process | None = None
    started_at: datetime | None = None
    restart_count: int = 0
    last_exit_code: int | None = None
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=120))
    drain_task: asyncio.Task[None] | None = None


class ProcessSupervisor:
    """Manage an exact allowlist of child-process argument vectors."""

    def __init__(self, services: list[ManagedService]) -> None:
        self._services = {service.name: service for service in services}
        self._lock = asyncio.Lock()

    async def start_all(self) -> None:
        for service in self._services:
            await self.start(service)

    async def stop_all(self) -> None:
        for service in reversed(tuple(self._services)):
            await self.stop(service)

    async def start(self, service_name: ServiceName) -> None:
        async with self._lock:
            service = self._services[service_name]
            if service.process is not None and service.process.returncode is None:
                return
            process = await asyncio.create_subprocess_exec(
                *service.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            service.process = process
            service.started_at = datetime.now(UTC)
            service.last_exit_code = None
            service.logs.append(f"[supervisor] started pid {process.pid}")
            service.drain_task = asyncio.create_task(self._drain(service))

    async def stop(self, service_name: ServiceName) -> None:
        async with self._lock:
            service = self._services[service_name]
            process = service.process
            if process is None or process.returncode is not None:
                return
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()
        service.last_exit_code = process.returncode
        service.logs.append(f"[supervisor] stopped with code {process.returncode}")

    async def restart(self, service_name: ServiceName) -> None:
        await self.stop(service_name)
        self._services[service_name].restart_count += 1
        await self.start(service_name)

    async def apply(self, action: LifecycleAction, service: ServiceName) -> None:
        targets = list(self._services) if service is ServiceName.ALL else [service]
        for target in targets:
            if action is LifecycleAction.START:
                await self.start(target)
            elif action is LifecycleAction.RESTART:
                await self.restart(target)
            else:
                await self.stop(target)

    def snapshot(self) -> dict[str, object]:
        services: dict[str, object] = {}
        for name, service in self._services.items():
            process = service.process
            running = process is not None and process.returncode is None
            services[name.value] = {
                "running": running,
                "pid": (
                    process.pid if process is not None and process.returncode is None else None
                ),
                "started_at": service.started_at.isoformat() if service.started_at else None,
                "restart_count": service.restart_count,
                "last_exit_code": service.last_exit_code,
                "logs": list(service.logs)[-40:],
            }
        return {"services": services}

    async def _drain(self, service: ManagedService) -> None:
        process = service.process
        if process is None or process.stdout is None:
            return
        while line := await process.stdout.readline():
            service.logs.append(line.decode(errors="replace").rstrip())
        await process.wait()
        service.last_exit_code = process.returncode


def default_supervisor() -> ProcessSupervisor:
    executable = sys.executable
    return ProcessSupervisor(
        [
            ManagedService(
                name=ServiceName.API,
                argv=(
                    executable,
                    "-m",
                    "uvicorn",
                    "inventory_agent.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8000",
                ),
            ),
            ManagedService(
                name=ServiceName.WORKER,
                argv=(
                    executable,
                    "-m",
                    "inventory_agent.processing.worker",
                    "--watch",
                ),
            ),
        ]
    )


def create_supervisor_app(
    *,
    manager: ProcessSupervisor,
    token: str,
    auto_start: bool = True,
) -> FastAPI:
    pending_tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if auto_start:
            await manager.start_all()
        yield
        await manager.stop_all()

    app = FastAPI(
        title="Inventory Agent Development Supervisor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def require_token(
        authorization: str | None = Header(default=None),
    ) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    @app.get("/status")
    async def process_status(_: None = Depends(require_token)) -> dict[str, object]:
        return manager.snapshot()

    async def schedule(
        action: LifecycleAction,
        body: ServiceCommand,
    ) -> dict[str, object]:
        task = asyncio.create_task(manager.apply(action, body.service))
        pending_tasks.add(task)
        task.add_done_callback(pending_tasks.discard)
        return {
            "accepted": True,
            "action": action.value,
            "service": body.service.value,
        }

    @app.post("/start", status_code=status.HTTP_202_ACCEPTED)
    async def start(
        body: ServiceCommand,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        return await schedule(LifecycleAction.START, body)

    @app.post("/restart", status_code=status.HTTP_202_ACCEPTED)
    async def restart(
        body: ServiceCommand,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        return await schedule(LifecycleAction.RESTART, body)

    @app.post("/stop", status_code=status.HTTP_202_ACCEPTED)
    async def stop(
        body: ServiceCommand,
        _: None = Depends(require_token),
    ) -> dict[str, object]:
        return await schedule(LifecycleAction.STOP, body)

    return app


def main() -> None:
    settings: Settings = get_settings()
    token = _secret(settings.dev_supervisor_token) or _secret(settings.dev_dashboard_token)
    if not settings.dev_supervisor_enabled or token is None:
        raise SystemExit(
            "Set DEV_SUPERVISOR_ENABLED=true and DEV_SUPERVISOR_TOKEN before starting "
            "the development supervisor."
        )
    logging.basicConfig(level=settings.log_level)
    uvicorn.run(
        create_supervisor_app(manager=default_supervisor(), token=token),
        host="127.0.0.1",
        port=settings.dev_supervisor_port,
        log_level=settings.log_level.casefold(),
    )


def _secret(value: object) -> str | None:
    get_secret_value = getattr(value, "get_secret_value", None)
    if not callable(get_secret_value):
        return None
    result = get_secret_value()
    return result if isinstance(result, str) and result else None


if __name__ == "__main__":
    main()
