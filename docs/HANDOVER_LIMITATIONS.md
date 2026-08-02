# Prototype limitations and handover notes

Last reviewed: 2 August 2026

## How to read this document

This repository was deliberately built as a prototype. Its purpose was to prove the risky
parts of an inventory agent before investing in complete product coverage:

- unstructured Telegram input can be turned into reviewable inventory proposals;
- the model can be prevented from writing inventory directly;
- confirmations, ledger writes, reversals, tenant boundaries, retries, and audit records
  can be enforced deterministically outside the model; and
- matching and conversation behavior can be evaluated against a working end-to-end system.

Consequently, several reads are intentionally bounded, several workflows support only the
smallest useful case, and the local dashboard and supervisor are development tools. These
constraints made the prototype faster and safer to test. They must not be interpreted as
claims of production completeness or scalability.

The limitations below describe the current implementation, the prototype reason for the
constraint, and the work expected from the next owner.

## Safety boundaries to preserve

The following are intentional product invariants, not limitations to remove:

- The model never writes stock or catalog data directly.
- Stock changes, reversals, catalog creation, and catalog edits require deterministic
  authorization and explicit human confirmation.
- Applied inventory ledger rows are immutable. Corrections use compensating transactions.
- Database functions re-check organization scope, authorization, current state, and
  idempotency even when application code has already checked them.
- Model-selected database IDs must come from an organization-scoped read performed during
  the current turn.
- Telegram delivery is separated from processing through a durable outbox.

Future work should extend coverage and scale without weakening these boundaries.

## Highest-priority functional limitations

### 1. Transaction retrieval is limited to 20 records

**Current behavior**

- `TransactionReadArguments.limit` accepts at most 20.
- `read_inventory_agent_transactions` clamps every database request to 20.
- An unfiltered request such as “show all transactions” therefore means only the 20 most
  recent applied ledger transactions.
- A natural-language search returns ranked matching transactions and may add recent
  fallback records, but the combined result is still capped at 20.
- The result has no cursor, `next_cursor`, or reliable `has_more` field. The model cannot
  continue onto a second page and cannot know whether 20 means “all” or “the first 20.”
- An older transaction can still be found when the user supplies its exact UUID, but that
  is not a substitute for browsing or filtering complete history.
- Creator/confirmer attribution, date ranges, role-aware visibility, and structured
  type/status filters are not exposed by this tool.

**Why the prototype did this**

The first transaction-read slice was built to prove grounded reversal targeting without
placing an unbounded ledger in model context. Twenty records were sufficient for the demo
data and kept prompts, latency, and accidental reversal scope small.

**Required follow-up**

- Add stable cursor-based pagination with an opaque, organization-scoped cursor.
- Return `has_more` and `next_cursor`; make the agent explicitly say when a result is
  partial.
- Add database-side filters for transaction type, lifecycle status, creator, confirmer,
  item/SKU, and `occurred_after`/`occurred_before` timestamps.
- Expose creator and confirmer display information with an explicit privacy policy.
- Add count/aggregate queries that do not require placing every transaction in model
  context.
- Test records older than the first page, concurrent inserts between pages, cursor reuse,
  timezones, tenant isolation, and role-specific visibility.

### 2. Inventory and catalog-wide reads are not exhaustive

**Current behavior**

- The agent-facing `read_inventory` schema allows a requested limit up to 50, but the
  semantic and ordinary candidate-search database functions clamp candidate retrieval to
  20. Increasing the model argument does not make those paths exhaustive.
- Name searches return ranked candidates, not an authoritative filtered catalog listing.
- There is no cursor for the next page and no database aggregate tool.
- Questions such as “list all stock,” “how many products do we have?”, or broad category
  totals can omit variants once the relevant catalog is larger than one candidate page.
- The model is currently asked to inspect and total returned rows itself. That is unsafe
  for large catalogs and for quantities using incompatible units.

**Why the prototype did this**

Matching quality and safe proposal creation were the primary goals. A bounded candidate
set was sufficient to prove those flows and avoided treating a large model response as an
inventory reporting system.

**Required follow-up**

- Separate candidate search from authoritative catalog listing.
- Add stable cursor pagination for inventory listings.
- Add database-backed counts and grouped aggregates for item families, variants, in-stock
  variants, zero-stock variants, locations, categories, and configured attributes.
- Never sum incompatible units without an explicit conversion policy.
- Add large-catalog and concurrent-update tests.

### 3. Lot and serial tracking are not implemented in the agent workflow

**Current behavior**

The database contains lot/serial-oriented structures, but Telegram proposals and new
catalog creation are deliberately restricted to `simple` tracking. The agent does not
collect lot number, batch, expiry, or serial details. A business that needs traceability
cannot use the current conversational workflow safely for those products.

**Why the prototype did this**

Simple stock was the smallest end-to-end slice that could validate matching, confirmation,
atomic balances, ledger movements, and reversals. Lot allocation and serial uniqueness add
different validation and user-interface requirements.

**Required follow-up**

Design dedicated receive/issue/reversal flows for lots and serials, including expiry,
allocation policy, uniqueness, partial quantities, confirmation rendering, and database
tests. Do not enable non-simple tracking merely by removing the current validation.

### 4. Location selection is implicit

**Current behavior**

The application uses the organization's configured `default_location_id`; if that is not
usable, it chooses the first active location by code. It does not resolve a location from
message text, Telegram chat, member preference, or a follow-up selection. Reads and writes
can therefore target the wrong location in a multi-location company.

**Why the prototype did this**

One deterministic location kept the early proposal and ledger work tenant-safe while the
prototype focused on product matching.

**Required follow-up**

Add explicit location grounding, per-user/chat defaults, ambiguity handling, review text,
and authorization tests. Location IDs must be resolved server-side and shown before
confirmation.

### 5. Matching thresholds are prototype baselines

**Current behavior**

Semantic, fuzzy, and hybrid retrieval work, and the candidate judge is constrained to the
retrieved candidates. However, the confidence thresholds and margins were derived from a
small smoke dataset. Candidate retrieval is also bounded. A plausible result can be missed,
and similar products can still require unnecessary clarification or human selection.

**Why the prototype did this**

The goal was to establish a measurable, safe matching boundary—not claim universal SKU
resolution accuracy.

**Required follow-up**

Build labeled datasets from representative companies, product families, languages,
abbreviations, invoice quality, and true no-match examples. Calibrate retrieval and judge
behavior by business risk, measure false-match rates separately from no-match rates, and
retain deterministic confidence gates.

## Catalog and identity limitations

### 6. SKU and external identifier semantics are incomplete

The flow supports an optional deferred company SKU and later SKU editing. It does not yet
reliably distinguish a company SKU from a manufacturer part number, supplier part number,
or barcode during conversational creation. Real-world codes can therefore be stored in the
SKU field even when they belong in `item_identifiers` with manufacturer or supplier scope.

Add typed identifier extraction, organization policy for optional/generated SKUs,
supplier/manufacturer scoping, migration review for existing data, and duplicate-identifier
tests.

### 7. Canonical item-family and variant modeling is unfinished

Free-text product wording can create duplicate item families or inconsistent variant names.
The system does not yet fully normalize brand, product family, design, size, colour, and
other discriminator attributes into one canonical family plus consistently rendered
variants. Original wording should eventually be retained as source evidence or an alias,
not used as the only canonical identity.

### 8. Catalog maintenance covers only a safe first slice

Telegram managers/admins can confirm edits to names, SKU, description, and item/variant
attributes. The following remain unfinished:

- base-unit and unit-conversion changes;
- external identifier and alias maintenance;
- dashboard editing;
- duplicate-family or indistinguishable-variant checks;
- item/variant merge workflows; and
- organization policy for manager versus admin-only edits.

Base-unit changes are especially dangerous because existing balances and transaction lines
must not be reinterpreted. They need a dedicated audited migration/conversion workflow.

## Input and conversation limitations

### 9. Supported input types are narrow

- Text messages are supported.
- Invoice photos and JPEG/PNG/WebP documents are supported up to Telegram's hosted-bot
  20 MB download limit.
- PDFs and voice notes are retained as webhook events but are not interpreted.
- Multiple images are not combined into one inventory query.
- An image caption and image are sent together for invoice extraction, but invoice images
  use a separate structured-command pipeline rather than the LLM-led conversational agent.
  An image followed by a separate text message is therefore two separate events.

Voice transcription, PDF rendering/extraction, multi-image grouping, and unified
multimodal conversation all require explicit implementation and evaluation.

### 10. Conversation compaction is approximate

Active history defaults to seven days, approximately 30,000 tokens, and 300 Responses API
items. Token estimation is based on serialized history rather than the provider's exact
tokenizer. Older turns are summarized or discarded according to policy. Immutable audit
turns remain, but the model cannot automatically reconstruct every nuance from compacted
history.

This was sufficient to prove bounded durable context and reduce stale database facts.
Production use needs long-conversation evaluation, summary-quality monitoring, explicit
user-visible recovery when context is insufficient, and cost/latency calibration.

### 11. The fallback text processor has less capability

When `INVENTORY_AGENT_ENABLED=false`, the older Structured Outputs text path is restored.
That path can create inventory proposals but still responds that inventory queries are not
available. Disabling the main agent is therefore a compatibility fallback, not an
equivalent read/query experience.

### 12. Complex turns are deliberately bounded

The main agent stops after six model/tool rounds, permits at most one mutation proposal per
user message, and does not combine different mutation types into one atomic review. A
single add or deduct proposal can contain multiple lines, but a request that mixes stock,
catalog maintenance, and other actions must be split across confirmed turns.

These limits prevent runaway model loops and ambiguous multi-action confirmation in the
prototype. If richer workflows are added, preserve explicit per-action authorization,
review, idempotency, and failure semantics rather than only increasing the limits.

## Authorization and administration limitations

### 13. Membership and role workflows are incomplete

Invitation, registration, approval, and initial roles exist, but later role editing,
production-grade administrator accounts, and company selection for a Telegram user who
belongs to multiple organizations remain incomplete. Read visibility is not yet fully
role-specific; for example, the transaction-read backlog still needs a policy for workers'
own records versus company-wide manager access.

### 14. Unmatched-product behavior for workers needs a product decision

Catalog creation is manager/admin-only at the database boundary. A worker can still reach
a Telegram path that offers creation and then receives an authorization failure. The next
owner must choose whether to hide creation, create a manager-approval workflow, allow a
worker-authored draft, or make the permission organization-configurable. It must not be
solved by weakening the database authorization check.

### 15. The development dashboard is not a production admin console

The dashboard and process supervisor are loopback-focused development tools. Production
dashboard requests are intentionally unavailable, and the current Basic authentication is
not a production identity, session, CSRF, RBAC, or audit solution. A production console
needs authenticated organization administrators, scoped queries/actions, secure session
management, and deployment-specific access controls.

## Reliability, scale, and operations limitations

### 16. The default worker is deliberately low-throughput

One loop handles at most one callback, one image, one text event, one registration notice,
and one normal outbox delivery in sequence. Database claims are designed to be retryable,
but default operation has not been capacity-planned or validated as a horizontally scaled
production service. Slow image/model calls can delay later work in that worker.

Separate processor pools, concurrency limits, backpressure, queue-age alerts, graceful
shutdown testing, load tests, and deployment sizing remain.

### 17. Retry and dead-letter operations are basic

Source-event processing retries after a fixed delay and fails after three attempts.
Outbound delivery retries and eventually dead-letters. Dead-letter records are inspectable,
but there is no complete operator workflow for alerting, replay, selective quarantine, or
root-cause correlation across every processor.

### 18. Telegram delivery is at-least-once

If Telegram accepts a message and the process crashes before `sent` is recorded, lease
recovery can send the same visual message again. Proposal creation and database mutations
remain idempotent, but exactly-once Telegram display is not available at this boundary.
Product copy and support procedures should tolerate occasional duplicates.

### 19. Observability proves activity, not complete end-to-end health

The development dashboard exposes events, outcomes, prompts, context, and selected runtime
metrics. It does not yet provide production-grade per-processor health, oldest queue age,
backlog, dependency status, alerting, service-level objectives, tracing, or sanitized
failure correlation across Telegram, OpenAI, Supabase, and storage.

### 20. Artifact cleanup and retention are incomplete

An inventory reset removes source-artifact database records but does not delete the
corresponding objects from Supabase Storage. There is no complete retention, deletion,
legal-hold, export, or privacy policy for invoice images and agent audit history.

### 21. Prompt caching is an optimization, not a capacity guarantee

The main agent now uses stable cache keys and explicit cache breakpoints where supported,
and records cache reads/writes. Cache reuse depends on stable prefixes, model support,
traffic shape, and cache lifetime. It does not increase the model's context window or TPM
limits. Structured extraction, image interpretation, candidate judging, summarization, and
embedding calls do not all share the main agent's caching path.

Live cache-hit, expiry, latency, and cost behavior still needs calibration on representative
production traffic.

### 22. Deployment, backup, and disaster recovery are not productized

`scripts/start-dev.sh`, the local Supabase stack, ngrok, and the loopback supervisor are a
development experience. The repository does not constitute a production deployment plan.
The next owner must define hosting, secret management, database migration promotion,
backups, point-in-time recovery, storage durability, monitoring, scaling, incident response,
and rollback procedures.

### 23. Provider portability is unproven

The production construction path creates OpenAI-backed agent, extraction, clarification,
candidate-judge, summarization, and embedding clients. Although individual boundaries are
testable, an Ollama or other provider runtime has not been integrated or evaluated. Model
quality—especially tool adherence, image extraction, structured outputs, matching judgment,
and summarization—is part of application correctness and must be evaluated before changing
providers.

## Test and evaluation limitations

The repository has substantial unit, component, database, and stress-test coverage, but
passing tests does not establish production model quality:

- most Python tests use deterministic fakes and do not spend API credits;
- component tests require explicit opt-in and local Supabase infrastructure;
- the stress runner replaces real OpenAI and Telegram latency with simulators;
- matching thresholds have not been calibrated on a broad labeled SME corpus;
- there is no sustained production load, chaos, backup-restore, or disaster-recovery test;
  and
- real-model behavior can change with model versions even when schemas remain valid.

Maintain a versioned evaluation corpus of representative conversations, invoices,
catalogs, ambiguous matches, authorization failures, long histories, and adversarial tool
requests. Compare behavior, safety, latency, cache usage, and cost before changing models,
prompts, tools, matching thresholds, or context policy.

## Suggested handover priority

1. Add complete transaction pagination and filters; make every partial result explicit.
2. Add authoritative paginated inventory listing and database aggregate tools.
3. Calibrate matching and agent behavior on labeled, representative company data.
4. Complete role visibility, multi-company selection, unmatched-worker behavior, and a
   production admin authentication design.
5. Decide product scope for multiple locations, lots/serials, PDFs, voice, and unified
   multimodal turns; implement the chosen scope end to end.
6. Split and scale workers, add queue/dependency observability, and define dead-letter
   replay operations.
7. Define deployment, secrets, backup/restore, artifact retention, privacy, and incident
   response before production use.
8. Only then optimize or replace the model/harness, using the same safety boundaries and a
   versioned evaluation corpus.

The detailed implementation backlog remains in the README's **Build sequence**. This
document should stay short enough to read during handover and should be updated whenever a
listed limitation is removed or a new production constraint is discovered.

## Useful implementation entry points

- Transaction tool contract: [`agent/models.py`](../src/inventory_agent/agent/models.py)
- Transaction result assembly and grounding:
  [`agent/production_tools.py`](../src/inventory_agent/agent/production_tools.py)
- Current 20-row transaction database cap:
  [`202607240009_exact_transaction_id_and_status.sql`](../supabase/migrations/202607240009_exact_transaction_id_and_status.sql)
- Candidate retrieval cap:
  [`202607230019_semantic_item_matching.sql`](../supabase/migrations/202607230019_semantic_item_matching.sql)
- Agent loop and six-round limit: [`agent/runtime.py`](../src/inventory_agent/agent/runtime.py)
- Sequential worker construction and processing order:
  [`processing/worker.py`](../src/inventory_agent/processing/worker.py)
- Context compaction: [`agent/context.py`](../src/inventory_agent/agent/context.py)
- Image input boundary:
  [`extraction/image_interpreter.py`](../src/inventory_agent/extraction/image_interpreter.py)
- Product backlog and completed prototype slices: [README build sequence](../README.md#build-sequence)
