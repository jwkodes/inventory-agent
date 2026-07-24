# Inventory Agent Architecture

Status: initial design, 22 July 2026

## Goal

Let SME workers query and modify inventory through Telegram text, invoice images, and
voice notes without giving an AI model direct control of inventory. Writes must be
reviewable, atomic, auditable, tenant-isolated, and reversible.

## System flow

```text
Telegram text / photo / voice
              |
              v
       Telegram gateway
  authentication + deduplication
              |
              v
      Persisted source event
       atomic worker claim
              |
              v
       Input interpretation
 text/image -> structured command
 voice -> transcript -> structured command
              |
              v
         Item matching
 identifier -> alias -> text search -> semantic reranking
              |
              v
       Transaction proposal
 resolved items + quantities + warnings
              |
              v
       Processing outbox
 durable Telegram-delivery handoff
              |
              v
      Telegram confirmation
       confirm / edit / cancel
              |
              v
      Atomic Postgres function
   ledger movements + balance update
              |
              v
        Applied transaction
              |
              +---- reverse -> compensating transaction
```

Inventory queries branch into a read-only query service after interpretation. They do not
enter the transaction application path.

## Component responsibilities

### Telegram gateway

- Receive webhook updates.
- Verify Telegram's webhook secret.
- Map Telegram users to an organization and role.
- Deduplicate Telegram update IDs.
- Store source message metadata before asynchronous processing.
- Put only opaque proposal IDs and actions in callback button data.

Telegram delivery is at-least-once. Every event and confirmation handler must therefore
be idempotent.

Callback queries are persisted by the same gateway. The runtime prioritizes one due
callback per cycle, resolves the active organization member again at claim time, then
acknowledges and dispatches only compact decoded actions. Selection, proposal confirmation,
cancellation, and the reversal lifecycle use idempotent database functions and edit the
original Telegram message. Expired callback acknowledgements do not block database work,
and a repeated identical message edit is accepted as an already-completed side effect.
Failed callback attempts retry after 30 seconds and the third failure is retained for
operations.

The text worker atomically claims a persisted event, resolves the organization member and
default active location, then first offers the text to a reversal request awaiting a reason.
A matching request consumes it without a model call and enqueues a final reversal review.
Other text continues through extraction, matching, and proposal creation. The worker writes
the outcome to `processing_outbox` before completing the source event. Proposal, reversal,
and outbox actions are idempotent, so a repeated processing attempt cannot create duplicate
business or delivery records. A 15-minute claim lease permits recovery after a worker
crash. Transient processing failures retry after 30 seconds and the third failure is
retained for operations. The same runtime loop owns a separate delivery component and retry
policy.

The image worker accepts Telegram photos and JPEG/PNG/WebP documents up to the hosted Bot
API's 20 MB download limit. It atomically claims the event, selects the largest Telegram
photo size, stores the original bytes and SHA-256 metadata in private Supabase Storage,
and passes a Base64 data URL to the vision interpreter. Text and image interpretation then
share the same organization-scoped matcher, proposal service, outbox, and confirmation
path. PDFs and voice notes remain separate future input adapters.

The delivery worker claims due outbox records with row locking and `SKIP LOCKED`, preventing
two healthy workers from delivering the same row concurrently. A claim abandoned for five
minutes can be recovered. Temporary failures return to `pending`; the fifth failed attempt
is retained as `failed` for operations rather than retried forever. Only outcomes belonging
to a `processed` source event are eligible.

This boundary provides at-least-once, not exactly-once, Telegram delivery. If Telegram
accepts `sendMessage` and the worker crashes before recording `sent`, lease recovery may
send a duplicate. Inventory application and proposal actions remain idempotent regardless.

### Source artifact service

- Store original invoice images, PDFs, and voice notes in a private Supabase bucket.
- Store content type, checksum, Telegram file ID, and ownership metadata.
- Use short-lived signed URLs for review; never use a public bucket.
- Retain the transcript and exact source wording for audit and model evaluation.

The implemented invoice-image slice stores original JPEG, PNG, and WebP bytes under a
deterministic tenant/event/checksum path. PDFs and audio are represented in the design but
are not processed yet.

### Input interpreter

The model converts unstructured content into a strict command schema. It may identify an
operation, source item reference, quantity, unit, and configured custom fields. It must
not choose database IDs, mutate stock, or invent a final match.

Example model output:

```json
{
  "schema_version": "1.0",
  "intent": "RECEIVE_STOCK",
  "location_hint": null,
  "lines": [
    {
      "source_text": "part ABC-123 and there are 3",
      "item_reference": {
        "type": "PART_NUMBER",
        "value": "ABC-123"
      },
      "description": null,
      "quantity": "3",
      "unit": null,
      "attributes": []
    }
  ],
  "notes": null,
  "needs_clarification": false,
  "clarification_question": null
}
```

Quantities are represented as decimal strings at the model boundary and converted to
Python `Decimal`; binary floating-point values must not be used for stock or money.

The first model role is routine extraction and routing, configured as
`gpt-5.6-luna` with reasoning effort `none`. Hard cases may later be routed to a stronger
model only after evaluations demonstrate a benefit.

### Item matcher

Resolve each extracted item using this ordered strategy:

1. Exact organization-scoped SKU, barcode, or part number.
2. Exact organization-and-supplier-scoped human-confirmed alias.
3. Normalized PostgreSQL trigram search.
4. Embedding retrieval for a small candidate set.
5. Model reranking for genuinely ambiguous candidates.
6. Human candidate selection when evidence remains insufficient.

The system must not treat a model's self-reported confidence as calibrated probability.
Match decisions use match method, identifier agreement, package and unit compatibility,
top-candidate margin, alias history, and results measured on labelled examples.

During the prototype, every inventory write requires confirmation. Proposal confirmation
is deterministic through either its Telegram button or an exact standalone `Confirm`
message bound to the conversation's active proposal; the conversational model cannot
apply stock. Exact `Cancel` is handled by the same boundary. A low-confidence item also
requires explicit candidate selection before transaction confirmation.

The implemented baseline accepts a fuzzy candidate only when its normalized score is at
least `0.72` and its margin over the runner-up is at least `0.12`. Exact identifiers and
confirmed aliases are trusted unless another trusted candidate is within `0.02`. These
values are configuration points for evaluation, not model-reported confidence or fixed
product promises.

### Transaction service

- Persist proposals separately from applied transactions.
- Validate authorization, units, conversions, tracking fields, and stock policy.
- Apply a multi-line transaction through one Postgres function.
- Lock affected balance rows in stable order.
- Insert immutable stock movements and update balances in the same database transaction.
- Return an already-applied result safely when the same confirmation is delivered twice.

Application code and workflow tools must not implement a fetch-and-update loop for
multi-line inventory changes.

Proposal creation is also a database function: it inserts the header and lines
idempotently, validates resolved variants, and derives base-unit deltas from organization
unit conversions. Ambiguous lines remain unresolved with a null delta. Adjustment intent
is disabled until the command contract distinguishes signed deltas from stocktake
assignments.

### Reversal service

A reversal creates a new transaction with movement deltas opposite to the original.
It never deletes the original and never resets balances to historical `quantity_before`
values, because later valid movements may have occurred.

The prototype supports one complete reversal of an applied transaction. The schema will
permit partial reversal later. The negative-stock policy applies to reversals as it does
to other issues.

The Telegram flow stores a `transaction_reversal_requests` state machine:

```text
awaiting_reason -> awaiting_confirmation -> completed
       |                    |
       +------ cancel ------+-------------> cancelled
```

Requests bind the original transaction, manager/admin actor, and Telegram chat. The reason
is linked to its immutable source event and must pass a second human confirmation before
the existing atomic reversal function runs. A completed request records the compensating
transaction ID. Every transition is replay-safe; only one complete reversal may exist for
an original transaction.

## Inventory model

### Relational core

Implemented core tables:

| Table | Responsibility |
|---|---|
| `organizations` | Company tenant |
| `organization_users` | Telegram identity, organization, and role |
| `locations` | Warehouse, shop, or storeroom |
| `items` | Product master and base unit |
| `item_variants` | Sellable variants such as colour and size |
| `item_identifiers` | SKU, barcode, and supplier/manufacturer part number |
| `item_unit_conversions` | Package conversion such as one box to 24 each |
| `inventory_lots` | Batch and expiry identity |
| `inventory_serials` | Individually tracked units |
| `inventory_balances` | Current quantity by stock identity and location |
| `transaction_proposals` | Resolved request awaiting action |
| `transaction_reversal_requests` | Durable reason and confirmation state for reversals |
| `inventory_transactions` | Transaction header and lifecycle |
| `transaction_lines` | Final item, unit, quantity, and matching evidence |
| `stock_movements` | Immutable inventory ledger |
| `item_aliases` | Human-confirmed supplier/name mappings |
| `source_events` | Telegram event and processing audit |
| `source_artifacts` | Private image, document, and audio metadata |
| `processing_outbox` | Durable outcomes awaiting outbound Telegram delivery |

All tenant-owned rows contain `organization_id`. Foreign keys, uniqueness constraints,
and Supabase Row Level Security provide tenant isolation.

### Different company requirements

Different attributes belong to different entity levels:

- Item: brand, material, strength.
- Variant: colour, size, voltage.
- Lot: batch number, manufacture date, expiry date.
- Serial: serial number, warranty end date.
- Transaction: supplier invoice number, delivery order number.

Company-defined fields use typed definitions:

```text
custom_field_definitions
- id
- organization_id
- entity_type: ITEM | VARIANT | LOT | SERIAL | TRANSACTION
- key
- label
- data_type: TEXT | NUMBER | DATE | BOOLEAN | ENUM
- required_on_receive
- required_on_issue
- searchable
- enum_options
- validation_rules
```

Values may initially live in a JSONB `attributes` column on the relevant entity, but the
application and database validate them against their definitions. Any field that affects
stock identity, matching, FIFO/FEFO selection, alerts, or reversal is promoted into the
relational model rather than left as unstructured metadata.

Each item has a tracking mode:

- `SIMPLE`: quantity per item/variant and location.
- `LOT`: quantity per item/variant, lot, and location.
- `SERIAL`: individually tracked units.

Onboarding starts from profiles such as general retail, clothing, food, pharmacy, and
electronics. Organizations can then modify the field definitions.

## Security boundaries

- OpenAI, Telegram, and Supabase secret keys exist only on the backend.
- Source artifacts are private and accessed through signed URLs.
- Telegram chat ID alone is not authorization; the Telegram user must be an active
  organization member with the required role.
- Database functions recheck authorization and transaction state.
- Raw user content is data, not an instruction that can bypass business rules.
- Applied ledger movements are immutable.

## Observability and evaluation

For each interpretation, store the provider response ID, model, reasoning setting, prompt
version, schema version, latency, token usage, parsed command, matching evidence, human
correction, and final outcome.

Before enabling automatic application, evaluate representative examples for:

- Intent accuracy.
- Quantity and unit accuracy.
- Invoice line-item recall and precision.
- Exact and semantic matching accuracy.
- False-positive rate at each proposed confidence threshold.
- End-to-end transaction correctness.

## Initial assumptions requiring validation

- The product is the system of record rather than a connector to another inventory system.
- The first prototype uses one organization and one location but all data is tenant-ready.
- All writes require confirmation.
- Negative stock is rejected by default.
- Complete reversal is supported before partial reversal.
- Voice notes use transcription followed by the same text pipeline; realtime voice is not
  required.
