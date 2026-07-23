"""Embedding-backed semantic inventory candidate retrieval."""

import json
from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict

from inventory_agent.matching.models import InventoryCandidate


class InventoryEmbeddingDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_variant_id: UUID
    search_text: str
    content_hash: str
    stored_content_hash: str | None = None
    stored_embedding_model: str | None = None
    stored_embedding_dimensions: int | None = None


class EmbeddingProvider(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts in input order."""


class SemanticCandidateRepository(Protocol):
    async def find_candidates(
        self,
        *,
        organization_id: UUID,
        query: str,
        limit: int = 5,
    ) -> list[InventoryCandidate]:
        """Return organization-scoped candidates ranked by semantic similarity."""


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        model: str = "text-embedding-3-small",
        dimensions: int = 512,
    ) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.embeddings.create(
            model=self._model,
            input=list(texts),
            dimensions=self._dimensions,
            encoding_format="float",
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in ordered]


class SupabaseSemanticCandidateRepository:
    """Refresh cached catalog vectors and query pgvector cosine similarity."""

    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        embeddings: EmbeddingProvider,
        embedding_model: str = "text-embedding-3-small",
        embedding_dimensions: int = 512,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if embedding_dimensions != 512:
            raise ValueError("This schema currently requires 512 embedding dimensions")
        self._rest_url = f"{supabase_url.rstrip('/')}/rest/v1/rpc"
        self._headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
        self._embeddings = embeddings
        self._embedding_model = embedding_model
        self._embedding_dimensions = embedding_dimensions
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def find_candidates(
        self,
        *,
        organization_id: UUID,
        query: str,
        limit: int = 5,
    ) -> list[InventoryCandidate]:
        documents = await self._documents(organization_id)
        stale = [
            document
            for document in documents
            if document.stored_content_hash != document.content_hash
            or document.stored_embedding_model != self._embedding_model
            or document.stored_embedding_dimensions != self._embedding_dimensions
        ]
        query_vectors = await self._embeddings.embed([query])
        if len(query_vectors) != 1:
            raise ValueError("Embedding provider returned an unexpected query vector count")
        query_vector = query_vectors[0]
        _validate_vector(query_vector, self._embedding_dimensions)

        for batch_start in range(0, len(stale), 128):
            batch = stale[batch_start : batch_start + 128]
            vectors = await self._embeddings.embed([document.search_text for document in batch])
            if len(vectors) != len(batch):
                raise ValueError("Embedding provider returned an unexpected catalog vector count")
            records = []
            for document, vector in zip(batch, vectors, strict=True):
                _validate_vector(vector, self._embedding_dimensions)
                records.append(
                    {
                        "item_variant_id": str(document.item_variant_id),
                        "content_hash": document.content_hash,
                        "embedding": vector,
                    }
                )
            await self._call(
                "upsert_inventory_variant_embeddings",
                {
                    "p_organization_id": str(organization_id),
                    "p_embedding_model": self._embedding_model,
                    "p_embedding_dimensions": self._embedding_dimensions,
                    "p_records": records,
                },
            )

        rows = await self._call(
            "find_semantic_inventory_candidates",
            {
                "p_organization_id": str(organization_id),
                "p_query_embedding": json.dumps(query_vector, separators=(",", ":")),
                "p_embedding_model": self._embedding_model,
                "p_embedding_dimensions": self._embedding_dimensions,
                "p_limit": limit,
            },
        )
        if not isinstance(rows, list):
            raise ValueError("Supabase returned an invalid semantic candidate response")
        candidates = [InventoryCandidate.model_validate(row) for row in rows]
        contexts = await self._call(
            "get_inventory_candidate_context",
            {
                "p_organization_id": str(organization_id),
                "p_item_variant_ids": [str(candidate.item_variant_id) for candidate in candidates],
            },
        )
        if not isinstance(contexts, list):
            raise ValueError("Supabase returned invalid inventory candidate context")
        context_by_id = {
            str(context["item_variant_id"]): context
            for context in contexts
            if isinstance(context, dict) and "item_variant_id" in context
        }
        return [
            candidate.model_copy(
                update={
                    "match_evidence": {
                        **candidate.match_evidence,
                        **{
                            key: value
                            for key, value in context_by_id.get(
                                str(candidate.item_variant_id), {}
                            ).items()
                            if key != "item_variant_id"
                        },
                    }
                }
            )
            for candidate in candidates
        ]

    async def _documents(self, organization_id: UUID) -> list[InventoryEmbeddingDocument]:
        rows = await self._call(
            "list_inventory_embedding_documents",
            {"p_organization_id": str(organization_id)},
        )
        if not isinstance(rows, list):
            raise ValueError("Supabase returned invalid inventory embedding documents")
        return [InventoryEmbeddingDocument.model_validate(row) for row in rows]

    async def _call(self, function_name: str, body: Mapping[str, object]) -> object:
        async with httpx.AsyncClient(
            base_url=self._rest_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(f"/{function_name}", json=body)
        response.raise_for_status()
        return response.json()


def _validate_vector(vector: Sequence[float], dimensions: int) -> None:
    if len(vector) != dimensions:
        raise ValueError(f"Embedding must contain exactly {dimensions} dimensions")
