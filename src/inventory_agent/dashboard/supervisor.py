"""Authenticated client for the loopback-only development process supervisor."""

from __future__ import annotations

import httpx


class SupervisorClient:
    """Call only the supervisor's fixed status and lifecycle endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def status(self) -> dict[str, object]:
        return await self._request("GET", "/status")

    async def command(self, *, action: str, service: str) -> dict[str, object]:
        return await self._request(
            "POST",
            f"/{action}",
            json={"service": service},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.request(method, path, json=json)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Development supervisor returned an invalid response")
        return result
