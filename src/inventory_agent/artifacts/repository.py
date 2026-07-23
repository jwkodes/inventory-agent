"""Private Supabase Storage persistence for original source images."""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol
from uuid import UUID

import httpx


@dataclass(frozen=True, slots=True)
class SourceArtifactDraft:
    organization_id: UUID
    source_event_id: UUID
    storage_path: str
    media_type: str
    sha256: str
    telegram_file_id: str
    data: bytes
    metadata: dict[str, Any]


class SourceArtifactRepository(Protocol):
    async def store(self, draft: SourceArtifactDraft) -> UUID:
        """Idempotently store private bytes and their audit metadata."""


class SupabaseSourceArtifactRepository:
    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        bucket: str,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not bucket or "/" in bucket:
            raise ValueError("Supabase Storage bucket must be a simple nonempty name")
        self._supabase_url = supabase_url.rstrip("/")
        self._bucket = bucket
        self._headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def store(self, draft: SourceArtifactDraft) -> UUID:
        path = PurePosixPath(draft.storage_path)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("Source artifact storage path must be relative and safe")

        async with httpx.AsyncClient(
            base_url=self._supabase_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            upload = await client.post(
                f"/storage/v1/object/{self._bucket}/{path.as_posix()}",
                headers={"content-type": draft.media_type, "x-upsert": "true"},
                content=draft.data,
            )
            upload.raise_for_status()
            metadata = await client.post(
                "/rest/v1/source_artifacts",
                params={"on_conflict": "storage_bucket,storage_path"},
                headers={"Prefer": "resolution=merge-duplicates,return=representation"},
                json={
                    "organization_id": str(draft.organization_id),
                    "source_event_id": str(draft.source_event_id),
                    "storage_bucket": self._bucket,
                    "storage_path": path.as_posix(),
                    "media_type": draft.media_type,
                    "sha256": draft.sha256,
                    "telegram_file_id": draft.telegram_file_id,
                    "metadata": draft.metadata,
                },
            )
            metadata.raise_for_status()

        rows = metadata.json()
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError("Supabase returned an invalid source artifact response")
        artifact_id = rows[0].get("id")
        if not isinstance(artifact_id, str):
            raise ValueError("Supabase returned an invalid source artifact ID")
        return UUID(artifact_id)
