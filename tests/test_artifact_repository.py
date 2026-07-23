"""Supabase private source-artifact adapter tests."""

import json
from uuid import UUID

import httpx

from inventory_agent.artifacts.repository import (
    SourceArtifactDraft,
    SupabaseSourceArtifactRepository,
)

ARTIFACT_ID = UUID("80000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000001")
ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")


async def test_artifact_repository_uploads_private_bytes_then_upserts_metadata() -> None:
    requests: list[str] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.startswith("/storage/v1/object/"):
            assert request.headers["x-upsert"] == "true"
            assert request.headers["content-type"] == "image/png"
            assert request.content == b"png-data"
            return httpx.Response(200, json={"Key": "stored"})
        assert request.url.path == "/rest/v1/source_artifacts"
        body = json.loads(request.content)
        assert body["source_event_id"] == str(EVENT_ID)
        assert body["sha256"] == "abc123"
        assert request.url.params["on_conflict"] == "storage_bucket,storage_path"
        return httpx.Response(201, json=[{"id": str(ARTIFACT_ID)}])

    repository = SupabaseSourceArtifactRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        bucket="inventory-source-artifacts",
        transport=httpx.MockTransport(handle_request),
    )

    result = await repository.store(
        SourceArtifactDraft(
            organization_id=ORGANIZATION_ID,
            source_event_id=EVENT_ID,
            storage_path=f"{ORGANIZATION_ID}/{EVENT_ID}/abc123.png",
            media_type="image/png",
            sha256="abc123",
            telegram_file_id="telegram-file",
            data=b"png-data",
            metadata={"original_file_name": "invoice.png"},
        )
    )

    assert result == ARTIFACT_ID
    assert requests == [
        (f"/storage/v1/object/inventory-source-artifacts/{ORGANIZATION_ID}/{EVENT_ID}/abc123.png"),
        "/rest/v1/source_artifacts",
    ]
