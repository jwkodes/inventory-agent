"""Retention-policy tests for bounded, auditable agent context."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from inventory_agent.agent.context import (
    AgentContextManager,
    ContextRetentionPolicy,
    ContextRetentionSettings,
    durable_history_items,
    model_history_items,
)
from inventory_agent.agent.repository import AgentConversation, AgentConversationTurn

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
CONVERSATION_ID = UUID("65000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


@dataclass
class FakeConversationRepository:
    compacted: list[dict[str, object]] = field(default_factory=list)
    context_settings: dict[str, object] | None = None

    async def load(self, **kwargs: object) -> AgentConversation:
        raise AssertionError("not expected")

    async def save(self, **kwargs: object) -> UUID:
        raise AssertionError("not expected")

    async def compact(self, **kwargs: object) -> UUID:
        self.compacted.append(kwargs)
        return CONVERSATION_ID

    async def load_context_settings(
        self,
        *,
        organization_id: UUID,
    ) -> dict[str, object] | None:
        assert organization_id == ORGANIZATION_ID
        return self.context_settings


@dataclass
class FakeSummarizer:
    result: str
    calls: list[dict[str, object]] = field(default_factory=list)

    async def summarize(
        self,
        *,
        existing_summary: str | None,
        history: list[dict[str, object]],
    ) -> str:
        self.calls.append(
            {
                "existing_summary": existing_summary,
                "history": history,
            }
        )
        return self.result


def turn(number: int, *, age_days: int, estimated_tokens: int = 10) -> AgentConversationTurn:
    return AgentConversationTurn(
        turn_id=UUID(f"66000000-0000-0000-0000-{number:012d}"),
        source_event_id=UUID(f"50000000-0000-0000-0000-{number:012d}"),
        history=[
            {"role": "user", "content": f"request {number}"},
            {"role": "assistant", "content": f"reply {number}"},
        ],
        estimated_tokens=estimated_tokens,
        created_at=NOW - timedelta(days=age_days),
    )


def conversation(turns: list[AgentConversationTurn]) -> AgentConversation:
    return AgentConversation(
        conversation_id=CONVERSATION_ID,
        organization_id=ORGANIZATION_ID,
        organization_user_id=ACTOR_ID,
        chat_id=123,
        history=[item for conversation_turn in turns for item in conversation_turn.history],
        active_turns=turns,
        allowed_variant_ids=[UUID("21000000-0000-0000-0000-000000000001")],
        allowed_transaction_ids=[UUID("40000000-0000-0000-0000-000000000001")],
    )


async def test_discard_policy_removes_old_turns_only_from_active_context() -> None:
    old = turn(1, age_days=8)
    recent = turn(2, age_days=1)
    repository = FakeConversationRepository()
    manager = AgentContextManager(
        conversations=repository,
        defaults=ContextRetentionSettings(
            policy=ContextRetentionPolicy.DISCARD,
            retention_days=7,
            max_tokens=30_000,
            max_items=300,
        ),
    )

    compacted = await manager.compact_if_needed(conversation([old, recent]), now=NOW)

    assert repository.compacted[0]["turn_ids"] == [old.turn_id]
    assert repository.compacted[0]["policy"] == "discard"
    assert repository.compacted[0]["summary"] is None
    assert compacted.history == recent.history
    assert compacted.active_turns == [recent]
    assert compacted.allowed_variant_ids == []
    assert compacted.allowed_transaction_ids == []


async def test_summary_policy_compacts_oldest_turns_when_token_budget_is_reached() -> None:
    turns = [
        turn(1, age_days=2, estimated_tokens=20),
        turn(2, age_days=1, estimated_tokens=20),
        turn(3, age_days=0, estimated_tokens=20),
    ]
    repository = FakeConversationRepository()
    summarizer = FakeSummarizer(result="The user previously discussed requests 1 and 2.")
    manager = AgentContextManager(
        conversations=repository,
        defaults=ContextRetentionSettings(
            policy=ContextRetentionPolicy.SUMMARIZE,
            retention_days=7,
            max_tokens=30,
            max_items=300,
        ),
        summarizer=summarizer,
    )
    source = conversation(turns).model_copy(update={"summary": "Earlier summary."})

    compacted = await manager.compact_if_needed(source, now=NOW)

    assert repository.compacted[0]["turn_ids"] == [turns[0].turn_id, turns[1].turn_id]
    assert summarizer.calls[0]["existing_summary"] == "Earlier summary."
    assert summarizer.calls[0]["history"] == turns[0].history + turns[1].history
    assert compacted.summary == "The user previously discussed requests 1 and 2."
    assert compacted.history == turns[2].history


async def test_item_limit_protects_the_database_history_ceiling() -> None:
    turns = [turn(number, age_days=0, estimated_tokens=1) for number in range(1, 5)]
    repository = FakeConversationRepository()
    manager = AgentContextManager(
        conversations=repository,
        defaults=ContextRetentionSettings(
            policy=ContextRetentionPolicy.DISCARD,
            retention_days=7,
            max_tokens=30_000,
            max_items=4,
        ),
    )

    compacted = await manager.compact_if_needed(conversation(turns), now=NOW)

    assert repository.compacted[0]["turn_ids"] == [turns[0].turn_id, turns[1].turn_id]
    assert compacted.active_turns == turns[2:]


async def test_organization_override_replaces_application_context_defaults() -> None:
    turns = [turn(number, age_days=0, estimated_tokens=10) for number in range(1, 4)]
    repository = FakeConversationRepository(
        context_settings={
            "policy": "discard",
            "retention_days": 7,
            "max_tokens": 10,
            "max_items": 300,
        }
    )
    manager = AgentContextManager(
        conversations=repository,
        defaults=ContextRetentionSettings(
            policy=ContextRetentionPolicy.DISCARD,
            retention_days=30,
            max_tokens=30_000,
            max_items=300,
        ),
        settings_provider=repository,
    )

    compacted = await manager.compact_if_needed(conversation(turns), now=NOW)

    assert repository.compacted[0]["turn_ids"] == [turns[0].turn_id, turns[1].turn_id]
    assert compacted.active_turns == [turns[2]]


def test_private_reasoning_is_excluded_from_future_active_context() -> None:
    history = [
        {"role": "user", "content": "show stock"},
        {"type": "reasoning", "encrypted_content": "private-payload"},
        {"role": "assistant", "content": "There are 3 boxes."},
    ]

    assert durable_history_items(history) == [history[0], history[2]]


def test_ephemeral_authoritative_context_is_kept_out_of_durable_history() -> None:
    history = [
        {
            "role": "system",
            "content": "current-turn transaction resolution",
            "_ephemeral_agent_context": True,
        },
        {"role": "user", "content": "reverse that transaction"},
    ]

    assert durable_history_items(history) == [history[1]]


def test_model_history_removes_stale_transaction_results_but_keeps_user_intent() -> None:
    history = [
        {"role": "user", "content": "show my last transactions"},
        {
            "type": "function_call",
            "call_id": "read-transactions-1",
            "name": "read_transactions",
            "arguments": '{"query":null,"limit":5}',
        },
        {
            "type": "function_call_output",
            "call_id": "read-transactions-1",
            "output": '{"transactions":[{"transaction_id":"wrong-old-id"}]}',
        },
        {
            "role": "assistant",
            "content": "The first transaction is wrong-old-id.",
        },
        {"role": "user", "content": "the first one is wrong"},
    ]

    sanitized = model_history_items(history)

    assert sanitized[0] == history[0]
    assert sanitized[-1] == history[-1]
    assert all("wrong-old-id" not in str(item) for item in sanitized)
    assert "must read the transaction ledger again" in str(sanitized[1])
