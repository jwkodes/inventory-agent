"""Tests for the Supabase inventory candidate RPC adapter."""

import json
from decimal import Decimal
from uuid import UUID

import httpx

from inventory_agent.extraction.schema import ItemReferenceType
from inventory_agent.matching.models import CandidateMatchMethod
from inventory_agent.matching.repository import SupabaseInventoryCandidateRepository

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")


async def test_repository_calls_rpc_and_parses_decimal_score() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_inventory_candidate_context"):
            assert json.loads(request.content)["p_item_variant_ids"] == [
                "21000000-0000-0000-0000-000000000003"
            ]
            return httpx.Response(
                200,
                json=[
                    {
                        "item_variant_id": "21000000-0000-0000-0000-000000000003",
                        "item_attributes": {"strength": "500mg"},
                        "variant_attributes": {"form": "capsule"},
                        "attribute_matching_roles": {"strength": "discriminator"},
                    }
                ],
            )
        assert request.url.path == "/rest/v1/rpc/find_inventory_candidates"
        assert request.headers["apikey"] == "test-secret"
        assert json.loads(request.content) == {
            "p_organization_id": str(ORGANIZATION_ID),
            "p_query": "AMOX-500",
            "p_reference_type": "PART_NUMBER",
            "p_supplier_scope": None,
            "p_limit": 5,
        }
        return httpx.Response(
            200,
            json=[
                {
                    "item_variant_id": "21000000-0000-0000-0000-000000000003",
                    "item_id": "20000000-0000-0000-0000-000000000003",
                    "item_name": "Amoxicillin 500mg",
                    "variant_name": None,
                    "sku": "MED-AMOX-500",
                    "base_unit": "box",
                    "tracking_mode": "lot",
                    "match_method": "exact_identifier",
                    "match_score": "1.0000000",
                    "match_evidence": {"source": "item_identifier"},
                }
            ],
        )

    repository = SupabaseInventoryCandidateRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    candidates = await repository.find_candidates(
        organization_id=ORGANIZATION_ID,
        query="AMOX-500",
        reference_type=ItemReferenceType.PART_NUMBER,
    )

    assert len(candidates) == 1
    assert candidates[0].match_method is CandidateMatchMethod.EXACT_IDENTIFIER
    assert candidates[0].match_score == Decimal("1.0000000")
    assert candidates[0].display_name == "Amoxicillin 500mg"
    assert candidates[0].item_attributes == {"strength": "500mg"}
    assert candidates[0].variant_attributes == {"form": "capsule"}


async def test_repository_calls_unfiltered_fallback_browse_rpc() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_inventory_candidate_context"):
            return httpx.Response(
                200,
                json=[
                    {
                        "item_variant_id": "21000000-0000-0000-0000-000000000001",
                        "item_attributes": {},
                        "variant_attributes": {},
                        "attribute_matching_roles": {},
                    }
                ],
            )
        assert request.url.path == "/rest/v1/rpc/browse_inventory_candidates"
        assert json.loads(request.content) == {
            "p_organization_id": str(ORGANIZATION_ID),
            "p_query": "purple widget",
            "p_limit": 5,
        }
        return httpx.Response(
            200,
            json=[
                {
                    "item_variant_id": "21000000-0000-0000-0000-000000000001",
                    "item_id": "20000000-0000-0000-0000-000000000001",
                    "item_name": "Anchor Butter 500g",
                    "variant_name": None,
                    "sku": None,
                    "base_unit": "each",
                    "tracking_mode": "simple",
                    "match_method": "text_search",
                    "match_score": "0.0100000",
                    "match_evidence": {"source": "fallback_trigram"},
                }
            ],
        )

    repository = SupabaseInventoryCandidateRepository(
        supabase_url="http://supabase.test",
        secret_key="test-secret",
        transport=httpx.MockTransport(handle_request),
    )

    candidates = await repository.browse_candidates(
        organization_id=ORGANIZATION_ID,
        query="purple widget",
    )

    assert candidates[0].match_score == Decimal("0.0100000")
    assert candidates[0].sku is None
