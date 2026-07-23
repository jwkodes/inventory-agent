"""Contract tests for OpenAI embeddings cached and searched through Supabase."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import httpx

from inventory_agent.matching.models import CandidateMatchMethod
from inventory_agent.matching.semantic import SupabaseSemanticCandidateRepository

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
VARIANT_ID = UUID("21000000-0000-0000-0000-000000000001")


class FakeEmbeddings:
    def __init__(self) -> None:
        self.inputs: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.inputs.append(list(texts))
        return [[float(index == 0)] * 512 for index, _ in enumerate(texts)]


async def test_semantic_repository_refreshes_stale_vectors_then_searches() -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("/list_inventory_embedding_documents"):
            return httpx.Response(
                200,
                json=[
                    {
                        "item_variant_id": str(VARIANT_ID),
                        "search_text": "Nintendo Switch 2 wireless controller",
                        "content_hash": "a" * 64,
                        "stored_content_hash": None,
                        "stored_embedding_model": None,
                        "stored_embedding_dimensions": None,
                    }
                ],
            )
        if request.url.path.endswith("/upsert_inventory_variant_embeddings"):
            return httpx.Response(200, json=1)
        return httpx.Response(
            200,
            json=[
                {
                    "item_variant_id": str(VARIANT_ID),
                    "item_id": "20000000-0000-0000-0000-000000000001",
                    "item_name": "Nintendo Switch 2 Controller",
                    "variant_name": None,
                    "sku": "SW2-CONTROLLER",
                    "base_unit": "each",
                    "tracking_mode": "simple",
                    "match_method": "semantic_rerank",
                    "match_score": "0.91",
                    "match_evidence": {"source": "embedding_cosine"},
                }
            ],
        )

    embeddings = FakeEmbeddings()
    repository = SupabaseSemanticCandidateRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        embeddings=embeddings,
        transport=httpx.MockTransport(handle_request),
    )

    candidates = await repository.find_candidates(
        organization_id=ORGANIZATION_ID,
        query="switch2 controller",
    )

    assert embeddings.inputs == [
        ["switch2 controller"],
        ["Nintendo Switch 2 wireless controller"],
    ]
    assert [path.rsplit("/", 1)[-1] for path, _ in requests] == [
        "list_inventory_embedding_documents",
        "upsert_inventory_variant_embeddings",
        "find_semantic_inventory_candidates",
    ]
    assert len(requests[1][1]["p_records"][0]["embedding"]) == 512
    assert candidates[0].match_method is CandidateMatchMethod.SEMANTIC_RERANK
