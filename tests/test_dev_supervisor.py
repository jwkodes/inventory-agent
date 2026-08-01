"""Authentication and allowlisted-command tests for the development supervisor."""

import asyncio

from httpx import ASGITransport, AsyncClient

from inventory_agent.dev_supervisor import (
    LifecycleAction,
    ServiceName,
    create_supervisor_app,
)


class FakeProcessSupervisor:
    calls: list[tuple[LifecycleAction, ServiceName]]

    def __init__(self) -> None:
        self.calls = []

    def snapshot(self) -> dict[str, object]:
        return {"services": {"api": {"running": True}, "worker": {"running": True}}}

    async def apply(self, action: LifecycleAction, service: ServiceName) -> None:
        self.calls.append((action, service))

    async def start_all(self) -> None:
        raise AssertionError("auto start is disabled")

    async def stop_all(self) -> None:
        raise AssertionError("lifespan is not entered by this transport")


async def test_supervisor_requires_token_and_accepts_only_typed_commands() -> None:
    manager = FakeProcessSupervisor()
    app = create_supervisor_app(  # type: ignore[arg-type]
        manager=manager,
        token="supervisor-test-token",
        auto_start=False,
    )
    headers = {"Authorization": "Bearer supervisor-test-token"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/status")
        process_status = await client.get("/status", headers=headers)
        command = await client.post(
            "/restart",
            headers=headers,
            json={"service": "worker"},
        )
        invalid = await client.post(
            "/restart",
            headers=headers,
            json={"service": "database"},
        )
        await asyncio.sleep(0)

    assert unauthorized.status_code == 401, unauthorized.text
    assert process_status.json()["services"]["api"]["running"] is True
    assert command.status_code == 202
    assert invalid.status_code == 422
    assert manager.calls == [(LifecycleAction.RESTART, ServiceName.WORKER)]
