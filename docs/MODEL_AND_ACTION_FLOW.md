# Model and Action Flow

This document describes the model routing and inventory actions implemented by the
current prototype. It is intentionally a description of the code as it exists today,
including the older structured pipeline that remains available alongside the LLM-led text
agent.

## Current model assignments

| Component | Current model | Reasoning effort | When it is called |
|---|---|---:|---|
| Inventory agent | `gpt-5.6-sol` | `low` | An ordinary Telegram text message when no deterministic follow-up flow is pending and `INVENTORY_AGENT_ENABLED=true` |
| Structured text extraction | `gpt-5.6-luna` | `none` | Ordinary Telegram text only when `INVENTORY_AGENT_ENABLED=false`; this is currently the standby legacy path |
| Conversation context summary | `gpt-5.6-sol` | `low` | Only when active history exceeds its age, estimated-token, or item limit and the policy is `summarize` |
| Invoice image extraction | `gpt-5.6-luna` | `none` | Every supported Telegram invoice image |
| Input clarification resolution | `gpt-5.6-luna` | `none` | A natural-language reply to an ambiguous saved invoice or legacy structured command |
| Catalog detail extraction | `gpt-5.6-luna` | `none` | A natural-language reply while a new catalog item is waiting for required details |
| Bulk catalog detail extraction | `gpt-5.6-luna` | `none` | One natural-language reply supplies or explicitly generates missing identifiers for selected new products in any multi-line proposal |
| Candidate judge | `gpt-5.6-luna` | `none` | Retrieved candidates in the invoice/structured pipeline need a constrained select, ask-user, or no-match decision |
| Semantic retrieval | `text-embedding-3-small` | Not applicable | A name-based catalog search when the configured strategy is `semantic` or `hybrid`; exact SKU reads bypass it |

These are configuration defaults from `.env`. The development dashboard shows the
effective runtime values. Embeddings perform retrieval rather than conversational
reasoning, so reasoning effort does not apply to them.

## End-to-end switching flow

```mermaid
flowchart TD
    USER["Telegram user"] --> TG["Telegram Bot API"]
    TG --> TUNNEL["ngrok HTTPS tunnel"]
    TUNNEL --> API["Telegram receiver and dashboard<br/>No model"]
    API --> EVENT[("Supabase source_events<br/>Durable ingress")]
    EVENT --> WORKER{"Message processor<br/>Select event type"}

    WORKER -->|Button callback| CALLBACK["Deterministic callback handler<br/>No model"]
    CALLBACK -->|Confirm proposal| APPLY["Atomic balance update<br/>Write immutable transaction"]
    CALLBACK -->|Cancel| CANCEL["Reject proposal<br/>No inventory write"]
    CALLBACK -->|Select match or add item| RESUME["Update pending workflow<br/>No model"]
    CALLBACK -->|Confirm reversal| REVERSE["Apply compensating transaction"]
    REVERSE -->|Linked correction exists| REPLACEMENT["Send corrected replacement review automatically"]
    REPLACEMENT --> CALLBACK
    CALLBACK -->|Completed lifecycle action| CONTEXT_EVENT[("Append authoritative system turn<br/>Agent context · no model")]

    WORKER -->|Text| PENDING{"Pending deterministic flow?"}
    PENDING -->|Reversal reason| REASON["Store reason and request confirmation<br/>No model"]
    PENDING -->|Saved input clarification| INPUT_RESOLVE["Merge reply into preserved command<br/>gpt-5.6-luna · effort none"]
    INPUT_RESOLVE -->|Still ambiguous| INPUT_CLARIFY["Update saved extraction and ask one question"]
    INPUT_RESOLVE -->|Resolved| STRUCTURED_MATCH
    PENDING -->|New-item details| CATALOG["Catalog detail extraction<br/>gpt-5.6-luna · effort none"]
    CATALOG --> CATALOG_VALID{"Required details complete?"}
    CATALOG_VALID -->|No| ASK_MORE["Ask only for missing details"]
    CATALOG_VALID -->|Yes| ITEM_REVIEW["Create catalog-item review"]

    PENDING -->|No pending flow| MODE{"Inventory agent enabled?"}
    MODE -->|Yes| LIMITS{"Context exceeds configured limit?"}
    LIMITS -->|Yes, summarize policy| SUMMARY["Context summary<br/>gpt-5.6-sol · effort low"]
    LIMITS -->|No| AGENT
    SUMMARY --> AGENT["Inventory agent tool loop<br/>gpt-5.6-sol · effort low"]

    AGENT -->|Exact SKU read| EXACT["Exact catalog/database lookup<br/>No model"]
    AGENT -->|Name-based read| EMBED["Semantic query embedding<br/>text-embedding-3-small"]
    EMBED --> SEARCH["pgvector candidate retrieval"]
    EXACT --> AGENT
    SEARCH --> AGENT
    AGENT -->|Read ledger| LEDGER["Token-ranked transaction search<br/>plus recent fallback · no model"]
    LEDGER --> AGENT
    AGENT -->|ADD or DEDUCT| PROPOSAL["Create pending stock proposal<br/>No inventory write"]
    AGENT -->|New catalog item| ITEM_REVIEW
    AGENT -->|Reverse transaction| REVERSAL_REVIEW["Create pending reversal request"]
    AGENT -->|Read-only request| RESPONSE["Prepare grounded reply"]

    MODE -->|No: legacy mode| TEXT_EXTRACT["Structured text extraction<br/>gpt-5.6-luna · effort none"]
    TEXT_EXTRACT --> STRUCTURED_MATCH["Structured matching pipeline"]

    WORKER -->|Invoice image| DOWNLOAD["Download, hash and privately store image<br/>No model"]
    DOWNLOAD --> IMAGE["Invoice image extraction<br/>gpt-5.6-luna · effort none"]
    IMAGE --> INPUT_VALID{"Inventory action clear?"}
    INPUT_VALID -->|No| INPUT_SAVE["Persist complete extraction<br/>and clarification state"]
    INPUT_SAVE --> OUTBOX
    INPUT_VALID -->|Yes| STRUCTURED_MATCH
    STRUCTURED_MATCH --> MATCH_TYPE{"Trusted exact match?"}
    MATCH_TYPE -->|Yes, no conflicting attributes| PROPOSAL
    MATCH_TYPE -->|No| SEMANTIC["Semantic candidate retrieval<br/>text-embedding-3-small"]
    SEMANTIC --> CANDIDATES{"Candidates returned?"}
    CANDIDATES -->|Yes| JUDGE["Candidate judge<br/>gpt-5.6-luna · effort none"]
    CANDIDATES -->|No| NO_MATCH["Offer add-new, match-existing,<br/>or ignore flow"]
    JUDGE -->|Select with sufficient confidence| PROPOSAL
    JUDGE -->|Ask user| CLARIFY["Store clarification and ask one question"]
    JUDGE -->|No match| NO_MATCH
    NO_MATCH -->|Several unmatched lines| LINE_DECISION{"Resolve next line"}
    LINE_DECISION -->|Add new| LINE_SAVE["Store add-new decision<br/>Send fresh review"]
    LINE_DECISION -->|Match existing| CANDIDATE_PICK["Show grounded candidates"]
    CANDIDATE_PICK -->|Select| LINE_SAVE
    LINE_DECISION -->|Ignore extraction mistake| LINE_IGNORE["Keep audit evidence<br/>Exclude from stock"]
    LINE_IGNORE --> LINE_DECISION
    LINE_SAVE --> LINE_DECISION
    LINE_DECISION -->|All decisions complete<br/>2+ new products| BULK_START["Start catalog batch<br/>Preserve every quantity"]
    LINE_DECISION -->|All decisions complete<br/>1 new product| ITEM_REVIEW
    LINE_DECISION -->|All active lines matched| REVIEW
    BULK_START --> BULK_DETAILS["Collect all missing SKUs in one reply<br/>gpt-5.6-luna · effort none"]
    BULK_DETAILS --> BULK_REVIEW["One combined catalog review"]
    BULK_REVIEW -->|Confirm once| BULK_CREATE["Create all catalog items atomically<br/>Resolve original proposal lines"]
    BULK_CREATE --> REVIEW

    PROPOSAL --> REVIEW["Render proposal review and confirmation buttons"]
    ITEM_REVIEW --> OUTBOX[("Supabase processing_outbox")]
    BULK_REVIEW --> OUTBOX
    REVERSAL_REVIEW --> OUTBOX
    RESPONSE --> OUTBOX
    REVIEW --> OUTBOX
    ASK_MORE --> OUTBOX
    INPUT_CLARIFY --> OUTBOX
    CLARIFY --> OUTBOX
    AGENT --> POST_LIMITS{"Post-turn context exceeds limit?"}
    POST_LIMITS -->|Yes, summarize policy| BG_SUMMARY["Background context summary<br/>does not block delivery"]
    POST_LIMITS -->|No or discard policy| BG_DONE["Background compaction complete"]
    BG_SUMMARY --> BG_DONE
    NO_MATCH --> OUTBOX
    REASON --> OUTBOX
    APPLY --> OUTBOX
    CANCEL --> OUTBOX
    RESUME --> OUTBOX
    REVERSE --> OUTBOX
    OUTBOX --> SEND["Send a new Telegram message<br/>No model"]
    SEND --> USER

    classDef primary fill:#243225,stroke:#9dcc68,color:#f6fff0;
    classDef routine fill:#202c35,stroke:#6fb6d9,color:#f1f8fc;
    classDef embed fill:#322d1f,stroke:#d7b85b,color:#fff9e7;
    classDef deterministic fill:#282a2d,stroke:#7d838b,color:#f5f5f5;
    classDef storage fill:#28243a,stroke:#8d7dd1,color:#f8f4ff;

    class AGENT,SUMMARY,BG_SUMMARY primary;
    class CATALOG,BULK_DETAILS,TEXT_EXTRACT,IMAGE,INPUT_RESOLVE,JUDGE routine;
    class EMBED,SEMANTIC embed;
    class EVENT,OUTBOX,CONTEXT_EVENT storage;
    class API,CALLBACK,APPLY,CANCEL,RESUME,REVERSE,REASON,INPUT_VALID,INPUT_SAVE,INPUT_CLARIFY,CATALOG_VALID,ASK_MORE,ITEM_REVIEW,BULK_START,BULK_REVIEW,BULK_CREATE,LIMITS,POST_LIMITS,BG_DONE,EXACT,SEARCH,LEDGER,PROPOSAL,REVERSAL_REVIEW,RESPONSE,DOWNLOAD,STRUCTURED_MATCH,MATCH_TYPE,CANDIDATES,NO_MATCH,CLARIFY,REVIEW,SEND deterministic;
```

The input-clarification record is deliberately separate from ordinary conversation
history. It keeps the original structured line items and model audit metadata, scopes the
pending question to one organization member and Telegram chat, and routes the member's
next reply to a constrained resolver before the inventory agent. Once resolved, the saved
command continues through the same exact, semantic, candidate-judgment, and proposal
stages as an unambiguous invoice.

## Ordinary text-agent tool loop

One user message does not necessarily equal one OpenAI request. The inventory agent may
alternate between model responses and deterministic tool executions:

```mermaid
sequenceDiagram
    actor User
    participant API as Telegram receiver
    participant DB as Supabase
    participant Worker as Message processor
    participant Agent as gpt-5.6-sol (low)
    participant Search as Exact/semantic catalog search

    User->>API: "Received 100 blue small shirts"
    API->>DB: Store source event
    Worker->>DB: Claim event and load conversation
    Worker->>Agent: History + current message + tool definitions
    Agent-->>Worker: read_inventory(name/attributes)
    Worker->>Search: Exact or semantic catalog search
    Search-->>Worker: Grounded variants, IDs and balances
    Worker->>Agent: Tool result
    Agent-->>Worker: propose_add(lines)
    Worker->>DB: Store pending proposal
    Worker->>Agent: Proposal tool result
    Agent-->>Worker: User-facing explanation
    Worker->>DB: Save turn and enqueue review
    Worker-->>User: New Telegram message with review buttons
```

The agent loop permits at most six model/tool rounds. A typical mutation uses multiple
`gpt-5.6-sol` calls: one to request inventory data, another to request a proposal, and a
final one to explain the prepared proposal. Tool execution itself does not call the
conversational model, although a name-based `read_inventory` tool can call the embedding
model for semantic retrieval.

`read_inventory` labels name-query results as ranked candidates and includes each
candidate's match method and score. For an unqualified category-wide quantity question,
the agent requests the 50-result ceiling, evaluates every candidate, excludes incidental
matches, and returns a per-variant breakdown and total. If the user challenges that
answer, the agent rereads with broader criteria instead of treating the challenge as a
request to receive stock.

## Confirmation and mutation boundary

Models can read authoritative data and create pending proposals, but they cannot apply
inventory changes. Telegram buttons and the exact standalone `Confirm`/`Cancel` fallback
commands use deterministic code:

```mermaid
flowchart LR
    P["Active pending proposal"] --> U{"Button or exact text control"}
    U -->|Confirm| F["Security-definer database function"]
    F --> B["Lock and update balance"]
    F --> L["Append immutable transaction and lines"]
    U -->|Cancel| C["Mark proposal rejected"]
    N["No active proposal"] --> R["Refuse to guess<br/>No stock change"]
    B --> M["Send new status message"]
    L --> M
    C --> M
```

A reversal follows the same rule: the system creates a new opposite transaction. It never
edits or deletes the original applied transaction.

Transaction selection is deterministic even though the model conducts the conversation:

```mermaid
flowchart LR
    U["Raw user message"] --> X{"Contains full UUID?"}
    X -->|Yes| E["Application extracts UUID<br/>Exact company lookup"]
    X -->|No / natural description| R["Model calls read_transactions"]
    E --> T["Server maps result to T1/T2"]
    R --> T
    T --> M["Model proposes reversal using current-turn ref"]
    M --> V["Server resolves ref to exact UUID"]
    V --> P["Pending reversal confirmation"]
    E -->|No exact row| N["Ask user to recopy or list transactions"]
```

The displayed UUID remains useful to workers and auditors, but it is not accepted as a
model-supplied reversal argument. Current-turn references expire after each user message.
When a user says “the first one” later, the agent rereads the ledger and receives a fresh
mapping. Reusable conversation context retains user intent but strips old transaction
tool results and their assistant paraphrases; raw event traces remain in the dashboard.

## Runtime timing

Every model boundary and major deterministic stage emits a structured
`component_runtime` log with `duration_ms`. This covers webhook ingestion, conversation
storage, compaction, model rounds, OpenAI calls, embeddings, catalog and balance reads,
tools, outbox rendering, Telegram delivery, and total text-event duration. Timings are
emitted when real work happens; they are diagnostic measurements rather than heartbeats.
Post-turn compaction is scheduled after the outbox record is created and is logged as
`context_compaction_background`; it is not included in the current
`telegram_text_event_total`. A following turn in the same conversation logs
`context_compaction_wait_before_turn` if it must wait for that task before loading history.

## Important routing notes

- The API never calls OpenAI. It authenticates and stores Telegram updates quickly.
- The worker owns all model calls, matching, proposal creation and outbound delivery.
- The inventory agent is the active ordinary-text path because
  `INVENTORY_AGENT_ENABLED=true`.
- Structured text extraction remains implemented but is not called for ordinary text
  while the inventory agent is enabled.
- Candidate judgment remains part of invoice and legacy structured matching. The ordinary
  text agent examines catalog results itself.
- Exact SKU reads query the database directly. Semantic embeddings are used for
  name-based retrieval, not for exact identifiers or broad inventory listings.
- Context summarisation is conditional housekeeping, not an automatic call on every turn.
- Post-turn context compaction runs in the background, so Telegram delivery does not wait
  for a summary. A subsequent turn in the same user/chat is serialized behind that task
  and reloads durable context afterward.
- Button callbacks and exact standalone proposal controls do not call a model.
- Successful confirmation and cancellation actions append a deterministic system turn to
  active agent context. The next agent turn can see that lifecycle result but must still
  read the authoritative transaction ledger before a correction or reversal.
- Typed proposal controls target only the proposal currently attached to that actor and
  chat. They never infer a target from old pending rows.
- Filtered ledger reads rank normalized query tokens and automatically include recent
  fallback transactions. A failed literal phrase can no longer establish that no
  transaction exists.
- Reversal applies to a complete transaction. Correcting one line requires reversing the
  original transaction and then confirming a corrected replacement.
- Voice-note transcription is not yet implemented in the current worker and is therefore
  not shown as an active branch.

## Auditing a real message

Use the development dashboard to compare this design with a real execution:

1. **Flow inspector** shows the Telegram event, model/tool history, matching evidence,
   proposal, outbox and transaction.
2. **Conversations** shows active history, compacted turns and the rolling summary.
3. **Models & prompts** shows the exact prompt and tool contract for every model component.
4. **Configuration** shows the effective model names, efforts and matching settings.

The immutable records in Supabase remain authoritative when a dashboard trace and this
architecture document differ.
