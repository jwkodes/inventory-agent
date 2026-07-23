# LLM-led inventory agent spike

Status: experimental, no-write evaluation  
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

Nothing in this package is connected to the production Telegram worker, Supabase proposal
functions, or stock application functions.

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
                         simulated proposal only
                         no commit capability
```

The runtime sends tool results back to the model and permits another model/tool round.
Every model output item is retained in the in-memory history, including function calls and
reasoning items. A later natural-language reply therefore continues the same conversation.

## Safety properties enforced by code

- Mutation-named tools create `awaiting_confirmation` proposals only.
- There is no confirmation or commit tool in the spike.
- A proposed existing variant ID must have been returned by `read_inventory` earlier in
  the same session.
- A proposed transaction reversal ID must have been returned by `read_transactions`
  earlier in the same session.
- Deductions cannot create catalog items.
- Quantities are positive `Decimal` values at the application boundary.
- Repeated tool-call IDs return the original result instead of creating another proposal.
- Tool arguments use strict JSON schemas.
- The session stops after a configured tool-round budget.

These checks are independent of the system prompt. A model instruction cannot bypass them.

## Run deterministic tests

Install the normal development dependencies, then run:

```bash
uv run pytest tests/test_agent_tools.py tests/test_agent_runtime.py \
  tests/test_agent_simulator.py
```

These tests use a fake model and in-memory data. They do not call OpenAI, Telegram, or
Supabase and do not spend API credits.

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

The result does not establish production reliability, cost, latency, tenant isolation, or
safe database integration.

## Gate before replacing the current pipeline

Do not connect this agent to live writes until all of the following exist:

1. Organization-scoped Supabase read adapters with pagination and bounded results.
2. Durable application-owned conversation records and pending proposal references.
3. Existing atomic proposal, confirmation, ledger, and reversal functions behind the
   tool boundary.
4. Telegram callback confirmation outside the model loop.
5. Tool-call, prompt, response, token, latency, and correction audit records.
6. Prompt-injection and cross-tenant tests.
7. A larger labelled evaluation set covering reads, additions, deductions, new catalog
   items, variants, lots, serials, units, negative stock, reversals, and ambiguous replies.
8. Side-by-side quality, latency, and cost measurements against the current pipeline.

The next implementation phase should replace only the conversational orchestration. The
existing authentication, source-event ingestion, outbox, atomic proposal application,
immutable ledger, confirmation callbacks, and compensating reversal functions remain.
