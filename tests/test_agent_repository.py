"""HTTP contract tests for durable agent persistence and reads."""

from decimal import Decimal
from uuid import UUID

import httpx

from inventory_agent.agent.repository import SupabaseAgentRepository

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
LOCATION_ID = UUID("12000000-0000-0000-0000-000000000001")
VARIANT_ID = UUID("21000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000001")
CONVERSATION_ID = UUID("65000000-0000-0000-0000-000000000001")


async def test_load_and_save_agent_conversation_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/load_inventory_agent_conversation"):
            return httpx.Response(
                200,
                json={
                    "conversation_id": str(CONVERSATION_ID),
                    "organization_id": str(ORGANIZATION_ID),
                    "organization_user_id": str(ACTOR_ID),
                    "chat_id": 123,
                    "history": [],
                    "allowed_variant_ids": [],
                    "allowed_transaction_ids": [],
                    "last_source_event_id": None,
                    "last_reply_text": None,
                    "last_proposal_id": None,
                    "last_reversal_request_id": None,
                    "last_reversal_reason": None,
                    "last_response_id": None,
                    "model_name": None,
                },
            )
        if request.url.path.endswith("/load_organization_agent_context_settings"):
            return httpx.Response(
                200,
                json={
                    "policy": "summarize",
                    "retention_days": 7,
                    "max_tokens": 30000,
                    "max_items": 300,
                },
            )
        return httpx.Response(200, json=str(CONVERSATION_ID))

    repository = SupabaseAgentRepository(
        supabase_url="https://example.supabase.co",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )

    conversation = await repository.load(
        organization_id=ORGANIZATION_ID,
        organization_user_id=ACTOR_ID,
        chat_id=123,
    )
    saved = await repository.save(
        conversation_id=conversation.conversation_id,
        source_event_id=EVENT_ID,
        organization_user_id=ACTOR_ID,
        history=[{"role": "user", "content": "show stock"}],
        turn_history=[{"role": "user", "content": "show stock"}],
        estimated_tokens=12,
        allowed_variant_ids={VARIANT_ID},
        allowed_transaction_ids=set(),
        reply_text="Here is the stock.",
        proposal_id=None,
        reversal_request_id=None,
        reversal_reason=None,
        response_id="resp-1",
        model_name="gpt-test",
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
    )
    compacted = await repository.compact(
        conversation_id=CONVERSATION_ID,
        organization_user_id=ACTOR_ID,
        turn_ids=[EVENT_ID],
        policy="discard",
        summary=None,
    )
    context_settings = await repository.load_context_settings(organization_id=ORGANIZATION_ID)
    callback_conversation = await repository.record_callback_outcome(
        organization_id=ORGANIZATION_ID,
        organization_user_id=ACTOR_ID,
        chat_id=123,
        source_event_id=EVENT_ID,
        action="confirm_proposal",
        result_id=EVENT_ID,
    )

    assert saved == CONVERSATION_ID
    assert compacted == CONVERSATION_ID
    load_body = requests[0].read().decode()
    assert str(ORGANIZATION_ID) in load_body
    save_body = requests[1].read().decode()
    assert requests[1].url.path.endswith("/save_inventory_agent_conversation_turn")
    assert str(VARIANT_ID) in save_body
    assert "Here is the stock." in save_body
    assert '"p_estimated_tokens":12' in save_body
    assert requests[2].url.path.endswith("/compact_inventory_agent_conversation")
    compact_body = requests[2].read().decode()
    assert '"p_policy":"discard"' in compact_body
    assert str(EVENT_ID) in compact_body
    assert requests[3].url.path.endswith("/load_organization_agent_context_settings")
    assert context_settings == {
        "policy": "summarize",
        "retention_days": 7,
        "max_tokens": 30000,
        "max_items": 300,
    }
    assert callback_conversation == CONVERSATION_ID
    assert requests[4].url.path.endswith("/record_inventory_agent_callback_outcome")
    callback_body = requests[4].read().decode()
    assert '"p_action":"confirm_proposal"' in callback_body
    assert f'"p_result_id":"{EVENT_ID}"' in callback_body


async def test_agent_balance_and_transaction_read_contracts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_inventory_agent_variant_balances"):
            return httpx.Response(
                200,
                json=[{"item_variant_id": str(VARIANT_ID), "on_hand": 12.5}],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "transaction_id": "40000000-0000-0000-0000-000000000001",
                    "transaction_type": "receipt",
                    "occurred_at": "2026-07-23 09:00:00+08",
                    "summary": "Receipt: 3 each Widget [ABC-123]",
                    "reversed": False,
                }
            ],
        )

    repository = SupabaseAgentRepository(
        supabase_url="https://example.supabase.co",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )

    balances = await repository.get_variant_balances(
        organization_id=ORGANIZATION_ID,
        location_id=LOCATION_ID,
        variant_ids=[VARIANT_ID],
    )
    transactions = await repository.read_transactions(
        organization_id=ORGANIZATION_ID,
        query="receipt",
        limit=5,
    )

    assert balances == {VARIANT_ID: Decimal("12.5")}
    assert transactions[0].transaction_type == "receipt"
