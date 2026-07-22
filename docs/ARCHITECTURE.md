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

### Source artifact service

- Store original invoice images, PDFs, and voice notes in a private Supabase bucket.
- Store content type, checksum, Telegram file ID, and ownership metadata.
- Use short-lived signed URLs for review; never use a public bucket.
- Retain the transcript and exact source wording for audit and model evaluation.

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
      "attributes": {}
    }
  ],
  "notes": null
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
3. Normalized PostgreSQL full-text and trigram search.
4. Embedding retrieval for a small candidate set.
5. Model reranking for genuinely ambiguous candidates.
6. Human candidate selection when evidence remains insufficient.

The system must not treat a model's self-reported confidence as calibrated probability.
Match decisions use match method, identifier agreement, package and unit compatibility,
top-candidate margin, alias history, and results measured on labelled examples.

During the prototype, every inventory write requires confirmation. A low-confidence item
also requires explicit candidate selection before transaction confirmation.

### Transaction service

- Persist proposals separately from applied transactions.
- Validate authorization, units, conversions, tracking fields, and stock policy.
- Apply a multi-line transaction through one Postgres function.
- Lock affected balance rows in stable order.
- Insert immutable stock movements and update balances in the same database transaction.
- Return an already-applied result safely when the same confirmation is delivered twice.

Application code and workflow tools must not implement a fetch-and-update loop for
multi-line inventory changes.

### Reversal service

A reversal creates a new transaction with movement deltas opposite to the original.
It never deletes the original and never resets balances to historical `quantity_before`
values, because later valid movements may have occurred.

The prototype supports one complete reversal of an applied transaction. The schema will
permit partial reversal later. The negative-stock policy applies to reversals as it does
to other issues.

## Inventory model

### Relational core

Planned core tables:

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
| `inventory_transactions` | Transaction header and lifecycle |
| `transaction_lines` | Final item, unit, quantity, and matching evidence |
| `stock_movements` | Immutable inventory ledger |
| `item_aliases` | Human-confirmed supplier/name mappings |
| `source_events` | Telegram event and processing audit |
| `source_artifacts` | Private image, document, and audio metadata |

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
