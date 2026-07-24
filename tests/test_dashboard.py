"""Authentication, rendering, and read-model tests for the development dashboard."""

from base64 import b64encode
from uuid import UUID

import httpx
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from inventory_agent.config import Settings, get_settings
from inventory_agent.dashboard.repository import DashboardRepository
from inventory_agent.dashboard.router import get_dashboard_repository
from inventory_agent.main import create_app

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000001")


class FakeDashboardRepository:
    async def list_organizations(self) -> list[dict[str, object]]:
        return [{"id": str(ORGANIZATION_ID), "name": "Demo SME", "inventory_profile": "general"}]

    async def list_events(
        self,
        *,
        organization_id: UUID,
        limit: int,
    ) -> list[dict[str, object]]:
        assert organization_id == ORGANIZATION_ID
        assert limit == 60
        return [{"id": str(EVENT_ID), "summary": "Received 3 widgets"}]

    async def get_flow(self, *, event_id: UUID) -> dict[str, object] | None:
        assert event_id == EVENT_ID
        return {"event": {"id": str(event_id), "summary": "Received 3 widgets"}}

    async def get_inventory(self, *, organization_id: UUID) -> dict[str, object]:
        assert organization_id == ORGANIZATION_ID
        return {"metrics": {"active_skus": 1}, "items": []}


def dashboard_settings(*, enabled: bool) -> Settings:
    return Settings(
        _env_file=None,
        app_env="development",
        dev_dashboard_enabled=enabled,
        dev_dashboard_username="inventory-dev",
        dev_dashboard_token=SecretStr("test-dashboard-token"),
    )


def basic_header() -> dict[str, str]:
    value = b64encode(b"inventory-dev:test-dashboard-token").decode()
    return {"Authorization": f"Basic {value}"}


async def test_dashboard_is_hidden_until_explicitly_enabled() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: dashboard_settings(enabled=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/dev")

    assert response.status_code == 404


async def test_dashboard_requires_basic_auth_and_serves_self_contained_ui() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: dashboard_settings(enabled=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/dev")
        response = await client.get("/dev", headers=basic_header())

    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == 'Basic realm="Inventory Agent Dev"'
    assert response.status_code == 200
    assert "Inventory Agent" in response.text
    assert "Flow inspector" in response.text
    assert "/dev/api/events" in response.text


async def test_dashboard_read_apis_are_authenticated_and_organization_scoped() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: dashboard_settings(enabled=True)
    app.dependency_overrides[get_dashboard_repository] = lambda: FakeDashboardRepository()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        organizations = await client.get("/dev/api/organizations", headers=basic_header())
        events = await client.get(
            "/dev/api/events",
            params={"organization_id": str(ORGANIZATION_ID)},
            headers=basic_header(),
        )
        flow = await client.get(f"/dev/api/events/{EVENT_ID}", headers=basic_header())
        inventory = await client.get(
            "/dev/api/inventory",
            params={"organization_id": str(ORGANIZATION_ID)},
            headers=basic_header(),
        )
        prompts = await client.get("/dev/api/prompts", headers=basic_header())

    assert organizations.json()["organizations"][0]["name"] == "Demo SME"
    assert events.json()["events"][0]["summary"] == "Received 3 widgets"
    assert flow.json()["event"]["id"] == str(EVENT_ID)
    assert inventory.json()["metrics"]["active_skus"] == 1
    assert {prompt["layer"] for prompt in prompts.json()["prompts"]} >= {
        "inventory_agent",
        "candidate_judge",
        "semantic_retrieval",
    }
    assert "secret" not in prompts.text.casefold()


async def test_dashboard_repository_builds_event_summaries_and_inventory_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/source_events"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": str(EVENT_ID),
                        "organization_id": str(ORGANIZATION_ID),
                        "status": "processed",
                        "payload": {
                            "message": {
                                "message_id": 12,
                                "text": "Received 3 widgets",
                            }
                        },
                    }
                ],
            )
        if path.endswith("/items"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "item-1",
                        "name": "Widget",
                        "base_unit": "each",
                        "tracking_mode": "simple",
                        "attributes": {},
                    }
                ],
            )
        if path.endswith("/item_variants"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "variant-1",
                        "item_id": "item-1",
                        "sku": "W-1",
                        "active": True,
                        "attributes": {"colour": "blue"},
                    }
                ],
            )
        if path.endswith("/inventory_balances"):
            return httpx.Response(
                200,
                json=[
                    {
                        "item_variant_id": "variant-1",
                        "location_id": "location-1",
                        "quantity": "12.5",
                    }
                ],
            )
        if path.endswith("/locations"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "location-1",
                        "code": "MAIN",
                        "name": "Main Store",
                        "active": True,
                        "attributes": {},
                    }
                ],
            )
        if path.endswith("/item_unit_conversions"):
            return httpx.Response(200, json=[])
        if path.endswith("/inventory_transactions"):
            return httpx.Response(200, json=[])
        if path.endswith("/transaction_lines"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected dashboard request: {request.url}")

    repository = DashboardRepository(
        supabase_url="https://example.supabase.co",
        secret_key="server-secret",
        transport=httpx.MockTransport(handler),
    )

    events = await repository.list_events(organization_id=ORGANIZATION_ID, limit=20)
    inventory = await repository.get_inventory(organization_id=ORGANIZATION_ID)

    assert events[0]["summary"] == "Received 3 widgets"
    assert events[0]["telegram_message_id"] == 12
    assert inventory["metrics"] == {
        "active_skus": 1,
        "total_on_hand": 12.5,
        "locations": 1,
        "transactions": 0,
    }
    rows = inventory["items"]
    assert isinstance(rows, list)
    assert rows[0]["on_hand"] == 12.5
    assert rows[0]["balances"][0]["location"]["name"] == "Main Store"
