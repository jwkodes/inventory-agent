# LLM-led inventory agent spike

Status: experimental, opt-in Telegram text integration
Branch: `experiment/llm-inventory-agent`  
Started: 23 July 2026

## Why this branch exists

The first prototype separated extraction, matching, candidate judgment, catalog
clarification, and transaction clarification into application-managed stages. That
architecture kept writes safe, but natural conversations accumulated brittle routing and
state-machine branches.

This spike tests a different boundary:

> The model decides what the user means, what evidence to retrieve, what to ask, and what
> action to propose. Application code decides whether that proposal is legal and whether it
> may change inventory.

The first no-write simulator passed its initial five-scenario gate. The branch now also
contains an opt-in Telegram text integration backed by Supabase. Its mutation tools create
the same pending proposals and reversal requests as the existing application; they do not
apply stock. Telegram callbacks and atomic database functions remain the only path from a
pending proposal to an inventory change.

## Spike flow

```text
user message
    |
    v
Responses API inventory agent
    |
    +-- ordinary unrelated message --> inventory-only scope response
    |
    +-- missing fact --> one natural follow-up question
    |
    +-- tool call --> strict application-owned tool
                         |
                         +-- read_inventory
                         +-- propose_add_inventory
                         +-- propose_deduct_inventory
                         +-- read_transactions
                         +-- propose_reversal
                                  |
                                  v
                         pending proposal only
                         no apply capability
```

The runtime sends tool results back to the model and permits another model/tool round.
Every model output item is retained in history, including function calls and reasoning
items. The simulator keeps that history in memory. The Telegram integration stores it in
`inventory_agent_conversations`, together with grounded variant/transaction IDs and replay
metadata, so a later natural-language reply continues after a worker restart.

## Safety properties enforced by code

- Mutation-named tools create `awaiting_confirmation` proposals only.
- There is no confirmation, apply, or commit tool in the agent.
- Telegram buttons and exact standalone `Confirm`/`Cancel` messages are handled before the
  conversational model. Typed controls target only the conversation's active proposal and
  refuse to guess when none is attached.
- A proposed existing variant ID must be refreshed by `read_inventory` during the current
  user message. Durable history can guide the query but cannot substitute for current
  grounding evidence.
- A proposed transaction reversal ID must have been returned by `read_transactions`
  earlier in the same session.
- Deductions cannot create catalog items.
- Quantities are positive `Decimal` values at the application boundary.
- Repeated tool-call IDs return the original result during a turn; database proposal
  idempotency also covers event replay.
- Tool arguments use strict JSON schemas.
- The session stops after a configured tool-round budget.
- PostgreSQL revalidates active organization membership and every persisted variant and
  transaction ID.
- Only one stock or reversal proposal can be created for one user message.

These checks are independent of the system prompt. A model instruction cannot bypass them.

## Run deterministic tests

Install the normal development dependencies, then run:

```bash
uv run pytest tests/test_agent_tools.py tests/test_agent_runtime.py \
  tests/test_agent_simulator.py
```

These tests use a fake model and in-memory data. They do not call OpenAI, Telegram, or
Supabase and do not spend API credits.

The production adapters, durable conversation RPCs, and Telegram orchestration also have
unit, database, and local-Supabase component coverage. See the canonical commands in the
README.

## Run the billable live evaluation

Put `OPENAI_API_KEY` in `.env`. The experiment has separate model settings:

```dotenv
INVENTORY_AGENT_MODEL=gpt-5.6-sol
INVENTORY_AGENT_REASONING_EFFORT=low
```

Run all scenarios:

```bash
uv run python -m inventory_agent.agent.simulator --live
```

Run one scenario:

```bash
uv run python -m inventory_agent.agent.simulator \
  --live \
  --scenario multi_turn_variant_split
```

`--live` is deliberately required because these commands make billable OpenAI API calls.
The catalog, balances, transactions, and proposals are all in memory. The command cannot
update local or hosted inventory.

Available scenario names:

- `unrelated_chat`
- `exact_receipt`
- `different_product_generation`
- `multi_turn_variant_split`
- `transaction_reversal`

The CLI prints every user message, tool call, tool result, assistant response, simulated
proposal, token count, and deterministic verdict.

## Initial result

On 23 July 2026, each scenario was run independently using the default experimental
configuration.

| Scenario | Result | Observed behavior | Total tokens |
|---|---:|---|---:|
| Unrelated chat | Pass | Declined without calling a tool | 1,277 |
| Exact receipt | Pass | Read exact SKU and proposed quantity 3 | 4,354 |
| Different product generation | Pass | Rejected Switch 2 as a match for first-generation Switch | 4,537 |
| Multi-turn variant split | Pass | Asked once, retained context, proposed 2 L and 2 XS | 6,631 |
| Transaction reversal | Pass | Read the ledger before proposing a compensating reversal | 4,332 |

Total measured usage: 21,131 tokens. This is a small qualitative gate, not a statistically
meaningful production evaluation.

## What this result establishes

The tool-loop design can handle the exact conversational failures that the handcrafted
pipeline handled poorly:

- It can reason about a product distinction that semantic similarity alone would hide.
- It can carry unresolved facts across ordinary messages without a custom clarification
  state for each attribute combination.
- It can split a total quantity across variants after a follow-up.
- It can select and compose multiple reads and proposals.
- It can remain inside the inventory domain.

The result did not by itself establish production reliability, cost, latency, tenant
isolation, or safe database integration. The subsequent integration adds organization
scoping, durable state, ID grounding, proposal-only writes, and component coverage, but it
is still a prototype rather than a production-readiness claim.

## Integration gate

Completed on this branch:

1. Organization-scoped Supabase read adapters with bounded results.
2. Durable application-owned conversation records and pending proposal references.
3. Existing atomic proposal, confirmation, ledger, and reversal functions behind the
   tool boundary.
4. Telegram callback confirmation outside the model loop.
5. Deterministic unit and database tests plus a component test covering Telegram event,
   grounded read, proposal creation, conversation persistence, and outbox enqueueing.

Still required before a production rollout:

1. Complete tool-call, prompt, token, latency, correction, and operator audit reporting.
2. A dedicated prompt-injection and adversarial cross-tenant test suite.
3. A larger labelled evaluation set covering reads, additions, deductions, new catalog
   items, variants, lots, serials, units, negative stock, reversals, and ambiguous replies.
4. Side-by-side quality, latency, and cost measurements against the structured pipeline.
5. Explicit pagination or a user-facing continuation flow for catalogs larger than one
   bounded read.

The feature flag replaces only text conversational orchestration. Existing authentication,
source-event ingestion, invoice processing, outbox delivery, atomic proposal application,
immutable ledger, confirmation callbacks, catalog creation, and compensating reversal
functions remain.
