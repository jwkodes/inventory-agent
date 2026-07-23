# Inventory Agent

An inventory assistant for SMEs that turns Telegram text, invoice images, and voice
notes into reviewable inventory transactions. Inventory writes are confirmed by a
human, applied atomically, recorded in an immutable ledger, and reversible through
compensating transactions.

This repository is in the prototype stage. It currently includes the application
foundation, a health endpoint, the first Supabase inventory schema, atomic stock
application, immutable movements, compensating reversals, and authenticated,
idempotent Telegram webhook ingestion. Versioned text and invoice-image Structured
Outputs interpreters run inside the continuous processing worker.
Organization-scoped catalog matching resolves exact identifiers and confirmed aliases,
then uses configurable semantic, fuzzy, or hybrid name matching. Semantic matching is the
default and uses OpenAI embeddings cached in PostgreSQL with pgvector. Original invoice
images are checksummed and stored in a private Supabase bucket before extraction. Text and
image events share matching, idempotent proposal creation, and the durable outbound-message
outbox. The same worker processes stored Telegram button callbacks, applies idempotent
proposal actions, and sends a new Telegram message after every successful action.

## Architecture principles

- The language model interprets unstructured input; it never writes inventory directly.
- Every write becomes a transaction proposal before it can be applied.
- Matching occurs before confirmation, with uncertain matches shown to the user.
- Applying a transaction updates balances and writes ledger movements atomically.
- Reversal creates a new, opposite movement. Applied ledger entries are never deleted.
- Core inventory facts are relational. Typed custom fields handle company-specific needs.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the component and data design.

## Technology stack

- Python 3.12 or 3.13
- FastAPI and Uvicorn
- OpenAI Responses API with Structured Outputs
- Supabase: PostgreSQL, pgvector, Storage, Row Level Security, and database functions
- Telegram Bot API using webhooks
- `uv` for Python versions, virtual environments, dependencies, and lockfiles
- Ruff, mypy, and pytest for validation

## Set up a development machine

These instructions are the canonical setup path. Keep them current whenever the setup
changes.

### 1. Install prerequisites

Install:

- [Git](https://git-scm.com/downloads)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Docker Desktop](https://docs.docker.com/get-docker/) or another Docker-compatible runtime
- [Supabase CLI](https://supabase.com/docs/guides/local-development/cli/getting-started)

On macOS with Homebrew:

```bash
brew install uv supabase
```

Install and start Docker Desktop separately. Verify the tools:

```bash
git --version
uv --version
docker --version
supabase --version
```

### 2. Get the source code

```bash
git clone git@github.com:jwkodes/inventory-agent.git inventory-agent-v2
cd inventory-agent-v2
```

This command uses GitHub SSH authentication. If SSH is not configured, use
`git clone https://github.com/jwkodes/inventory-agent.git inventory-agent-v2` instead.
When working from an existing local checkout, just change into its directory.

### 3. Install Python and project dependencies

```bash
uv sync --all-groups
```

`uv` installs a compatible Python version when needed, creates `.venv`, and installs the
exact dependency versions recorded in `uv.lock`.

### 4. Configure environment variables

```bash
cp .env.example .env
```

For the health endpoint and unit tests, the blank secret values are acceptable. OpenAI,
Telegram, and Supabase features will require their corresponding values as those features
are enabled.

Never commit `.env`, Telegram bot tokens, OpenAI API keys, or Supabase secret keys.
An API key authenticates requests but does not itself include an API balance. Before a
live run, check the OpenAI Platform usage and limits pages and add billing or prepaid
credit only if that API organization has no available balance. Unit, database, and
component tests do not call OpenAI.

### 5. Start local Supabase

Make sure Docker Desktop or another Docker-compatible engine is running first. The Docker
CLI being installed is not sufficient; `docker info` must be able to reach the server.

Start the local Supabase services:

```bash
supabase start
supabase db reset
```

Copy the local Project URL, publishable key, and secret key printed by `supabase start`
into `.env`. Local Supabase Studio is normally available at
<http://127.0.0.1:54323>.

The local stack is for development only. Do not expose it to external traffic.

The first `supabase start` downloads several container images and can take a few minutes.
If it appears to wait indefinitely without output, confirm that Docker Desktop has fully
started, then run the command again.

### 6. Run the API

```bash
uv run uvicorn inventory_agent.main:app --reload
```

Verify it in another terminal:

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok","service":"inventory-agent"}
```

Interactive API documentation is available at <http://127.0.0.1:8000/docs>.

### 7. Connect the Telegram bot

Telegram can deliver webhooks only to a public HTTPS URL. For local development, keep
the API running and expose port 8000 through an HTTPS tunnel. One development-only
option is a Cloudflare Quick Tunnel. It generates a temporary HTTPS hostname without
requiring a domain or Cloudflare account.

Install `cloudflared` once:

```bash
brew install cloudflared
```

Then keep the following processes open in separate terminals.

Terminal 1 — API:

```bash
uv run uvicorn inventory_agent.main:app --reload
```

Terminal 2 — public tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

You can use another tunnel provider instead. Copy the generated HTTPS hostname, append
`/webhooks/telegram`, and put the full URL in `.env` as `TELEGRAM_WEBHOOK_URL`.
Quick Tunnel hostnames change when restarted and are not suitable for production.
Verify the public route before registering it:

```bash
curl https://YOUR-GENERATED-HOSTNAME.trycloudflare.com/health
```

It should return:

```json
{"status":"ok","service":"inventory-agent"}
```

Before registering a webhook, identify your Telegram numeric user ID:

1. Put the BotFather token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send the bot a private message in Telegram.
3. Run:

```bash
uv run python -m inventory_agent.telegram.discover_users
```

In local Supabase Studio at <http://127.0.0.1:54323>, open the SQL editor and replace
the demo manager's placeholder Telegram ID with the ID printed above:

```sql
update public.organization_users
set telegram_user_id = 123456789
where id = '11000000-0000-0000-0000-000000000001';
```

Replace `123456789` with your actual ID. Then generate the webhook secret:

```bash
openssl rand -hex 32
```

Put the result in `.env` as `TELEGRAM_WEBHOOK_SECRET`, set the tunnel URL in
`TELEGRAM_WEBHOOK_URL`, and register it from a third terminal:

```bash
uv run python -m inventory_agent.telegram.setup_webhook
```

The registration helper sends Telegram only `message` and `callback_query` updates.
The API verifies Telegram's secret header, resolves the sender to exactly one active
organization, and stores each Telegram update ID once in `source_events`. It acknowledges
duplicates safely because Telegram retries webhook deliveries after non-2xx responses.

User discovery uses Telegram's `getUpdates` endpoint, which is unavailable after a
webhook is active. If the webhook URL changes, update `TELEGRAM_WEBHOOK_URL` and rerun the
registration command. Never paste a real bot token or webhook secret into source files,
terminal screenshots, issues, or chat.

After restarting the computer or stopping the Quick Tunnel:

1. Start Docker Desktop and run `supabase start`.
2. Restart the API in terminal 1.
3. Restart `cloudflared` in terminal 2.
4. Copy its new hostname into `TELEGRAM_WEBHOOK_URL`.
5. Rerun `uv run python -m inventory_agent.telegram.setup_webhook`.
6. Start the background worker described below.

Press `Ctrl+C` in the API or tunnel terminal to stop that process. Closing Codex or a
terminal session may also stop processes launched from it.

### 8. Run the background worker

The worker needs `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `SUPABASE_URL`, and
`SUPABASE_SECRET_KEY` in `.env`. The defaults use semantic matching, so the same OpenAI
key is used for command extraction, conversational catalog-detail extraction, and
embeddings. Run it in a separate terminal:

```bash
uv run python -m inventory_agent.processing.worker --watch
```

Each cycle claims at most one button callback, one invoice image, one text event, and one
outbound result, in that order. Button actions are prioritized so confirmation stays
responsive; the remaining work still runs if one attempt fails. The worker polls every two
seconds when all four queues are idle. Use `--poll-seconds N` to choose an interval from
greater than zero through 60 seconds. Without `--watch`, it runs one complete cycle, which
is useful while debugging.

Send an invoice either as a normal Telegram photo or as a JPEG, PNG, or WebP document.
The hosted Telegram Bot API limits bot downloads to 20 MB, which the worker checks before
downloading. The prototype deliberately leaves PDFs and voice notes for later slices;
their webhooks are retained but are not sent through the image interpreter.

On the first non-exact name match, the worker creates embeddings for catalog variants
whose searchable content has not been indexed yet. It batches and caches those vectors in
local PostgreSQL. Later requests normally embed only the user's query. Changing an item
name, variant name, SKU, attributes, or confirmed alias changes its content hash and
causes that variant to be refreshed automatically.

## Development checks

Run these before committing a change:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
supabase test db
supabase db lint --local --schema public --fail-on error
```

To apply safe formatting changes:

```bash
uv run ruff format .
```

### Testing levels

The test suite deliberately separates these levels:

- Unit and contract tests run Python components with fake OpenAI, Telegram, and Supabase
  boundaries. They are fast, deterministic, and run with `uv run pytest`.
- Database component tests run migrations, constraints, and functions against the real
  local Supabase PostgreSQL using `supabase test db`.
- Application component tests cross text and invoice-image processing, private Storage,
  matching, proposal creation, outbox delivery, stored callback claiming, and cancellation
  against real local Supabase while keeping OpenAI and Telegram fake. They are opt-in
  because they require running infrastructure and create temporary rows and objects:

```bash
RUN_COMPONENT_TESTS=1 uv run pytest -m component
```

Before running that command, start and reset local Supabase and copy its local secret key
into `.env` as `SUPABASE_SECRET_KEY`. The component tests refuse any Supabase URL whose host
is not `127.0.0.1` or `localhost`, and delete their temporary events, proposals, and outbox
rows.

Live OpenAI and Telegram end-to-end tests have not been automated yet. Those will use a
dedicated test bot, test organization, and explicit opt-in so ordinary test runs cannot
spend API credits or message real users.

## Configuration

Configuration is read from environment variables and `.env` by
`inventory_agent.config.Settings`.

| Variable | Purpose | Local default |
|---|---|---|
| `APP_ENV` | Runtime environment | `development` |
| `LOG_LEVEL` | Application log level | `INFO` |
| `OPENAI_API_KEY` | OpenAI Platform API key | none |
| `OPENAI_MODEL` | Extraction and intent model | `gpt-5.6-luna` |
| `OPENAI_REASONING_EFFORT` | Reasoning level for routine extraction | `none` |
| `OPENAI_EMBEDDING_MODEL` | Semantic inventory embedding model | `text-embedding-3-small` |
| `OPENAI_EMBEDDING_DIMENSIONS` | pgvector embedding width; fixed by the current schema | `512` |
| `INVENTORY_MATCHING_STRATEGY` | Name matching: `semantic`, `fuzzy`, or `hybrid` | `semantic` |
| `TELEGRAM_BOT_TOKEN` | BotFather token | none |
| `TELEGRAM_WEBHOOK_SECRET` | Verifies Telegram webhook requests | none |
| `TELEGRAM_WEBHOOK_URL` | Public HTTPS `/webhooks/telegram` endpoint | none |
| `SUPABASE_URL` | Supabase project API URL | local API URL |
| `SUPABASE_PUBLISHABLE_KEY` | Client-safe Supabase key | none |
| `SUPABASE_SECRET_KEY` | Server-only Supabase key | none |
| `SUPABASE_STORAGE_BUCKET` | Private source-artifact bucket | `inventory-source-artifacts` |

The default model is a configuration baseline, not a permanent product decision. Model
quality, latency, and cost will be measured against representative text and invoice cases.

## Structured command extraction

[`schema.py`](src/inventory_agent/extraction/schema.py) is the single source of truth for
model output. The interpreter uses the OpenAI Responses API's native Pydantic Structured
Outputs support; it does not parse ad-hoc JSON. The schema permits receive, issue,
adjustment, query, and unknown intents. It carries source item references, positive
decimal-string quantities, units, and a list of company-specific attribute hints.

The model cannot provide database IDs or apply an item match. Unclear and unrelated input
has an explicit clarification state, and refusals are raised separately. Provider response
ID, model, prompt version, and token usage are returned for later persistence and
evaluation. The invoice interpreter supplies a Base64 image input at `high` detail and
uses the same schema and matching path as text. Automated tests use local fake responses
and never spend API credits.

Catalog creation has its own smaller Structured Output schema. The bot states the facts it
needs—item name, SKU or internal code, base unit, and optional company-specific
attributes—but does not require the user to fill in JSON or a fixed text form. The worker
extracts those facts from natural language, combines them with safe suggestions already
derived from the transaction, persists partial answers across clarification turns, and
asks only for fields that remain missing. A final Telegram confirmation is required before
the catalog item is created. The current prototype creates simple-tracked items only; lot
and serial creation require an additional tracking-details flow.

## Invoice image processing

The webhook classifies Telegram photos and JPEG/PNG/WebP documents as `invoice_image`
events while preserving the original document name and MIME type. A database function
atomically claims the event, chooses Telegram's largest photo size, and resolves the
organization member and inventory location.

The worker obtains a temporary download path through Telegram `getFile`, enforces the
20 MB maximum, and stores the original bytes under a deterministic
`organization/event/SHA-256` path in the private `inventory-source-artifacts` bucket. It
then sends those in-memory bytes to the OpenAI Responses API with `store=False`; no public
artifact URL is created. Retries safely overwrite the same storage path and reuse the
proposal and outbox idempotency keys. Extraction failures keep only a sanitized error,
retry after 30 seconds, and become failed after the third attempt.

## Item matching

Exact and fuzzy retrieval is implemented by the organization-scoped
`find_inventory_candidates` PostgreSQL function. Semantic retrieval uses
`list_inventory_embedding_documents`, an OpenAI embedding call, the
`inventory_variant_embeddings` cache, and `find_semantic_inventory_candidates`.
All database functions require an organization ID and never search across tenants.

Evidence is evaluated in this order:

1. Exact normalized SKU, barcode, manufacturer part number, or supplier part number.
2. Exact human-confirmed alias, optionally scoped to a supplier.
3. The configured name-matching strategy:
   - `semantic` (default): embedding cosine similarity across names, SKU, attributes, and
     confirmed aliases.
   - `fuzzy`: PostgreSQL trigram similarity across names, SKU, and aliases.
   - `hybrid`: a weighted semantic/fuzzy score for evaluation.

[`policy.py`](src/inventory_agent/matching/policy.py) converts ranked candidates into
`matched`, `needs_confirmation`, or `not_found`. Exact evidence is normally accepted, but
conflicting trusted results require human selection. A fuzzy result currently needs a
score of at least `0.72` and a lead of at least `0.12` over the next candidate. These are
prototype baselines to calibrate on labelled SME examples, not probabilities. Semantic
scores use their own initial threshold of `0.42` and top-two margin of `0.10`, based on a
small smoke test only; they must be calibrated on labelled SME examples before production.
Semantic retrieval never bypasses this policy or the user's transaction confirmation.

If no candidate is confident, the bot explicitly offers **Add new item** and
**Choose existing**. Choosing existing displays fallback candidates in descending score
order. Adding an item starts the conversational detail flow described above. After item
creation, the original proposal line is linked to the new variant and a fresh proposal
review message is sent.

## Transaction proposals and confirmation

`create_inventory_proposal` atomically stores a proposal and all of its lines. Repeated
processing with the same organization and idempotency key returns the existing proposal.
For resolved lines, PostgreSQL validates the variant and derives the signed base-unit
quantity using the configured unit conversion. Unresolved lines retain their candidate
evidence but have no stock delta, so they cannot be applied accidentally.

The explicitly generic words `unit`, `units`, `item`, and `items` mean one unit of the
matched SKU and receive a factor-one conversion. Package words such as `box`, `carton`,
and `case` still require an organization-and-variant-specific conversion; the system does
not guess package sizes.

Telegram confirmation rendering uses compact opaque callback data containing only action
codes and UUIDs. Variant-selection callbacks fit below Telegram's 64-byte limit. A fully
resolved proposal gets Confirm and Cancel buttons; an unresolved proposal gets candidate
buttons and cannot be confirmed. The outbox delivery worker renders and sends these
messages.

Callback webhooks are stored before processing. The callback worker atomically claims the
oldest due event, resolves its active organization member, and routes decoded selection,
proposal, and reversal actions to separate database functions. It acknowledges valid
Telegram button presses before database work when possible; an expired acknowledgement
does not prevent the durable action. Malformed callback data is acknowledged with an alert
and never reaches Supabase. Proposal confirmation uses the existing atomic
`apply_inventory_proposal` function, so duplicate confirmations remain safe at the
database boundary.

Every successful button action enqueues a separate outbox-backed Telegram message so the
user receives a new-message notification. Variant selection sends a fresh proposal,
confirmation sends the inventory result with its next action, and proposal or reversal
cancellation sends a status notice. The worker still edits the originating message to
remove obsolete buttons, preventing stale controls from remaining active. Replayed
identical edits are treated as success because Telegram may answer that the message is
already unchanged. Callback failures retry after 30 seconds and become `failed` after the
third unsuccessful attempt, matching text-event handling.

Variant selection rechecks organization membership, verifies that the variant was actually
offered, derives its base-unit delta, and records `human_selected` evidence. Lot- and
serial-tracked selections remain blocked until their required tracking-detail prompts are
implemented.

## Complete transaction reversal

After inventory is updated, the Telegram message offers a Reverse transaction button.
Only an active manager or admin can start the flow. Pressing it creates or resumes a
durable `transaction_reversal_requests` record. It enqueues and sends a separate Telegram
message asking the same user, in the same chat, to reply with a reason; editing only the
existing transaction message would not notify the user. The next claimed text message is
consumed as that reason before OpenAI interpretation, retained with its source event, and
delivered back with final Confirm reversal and Cancel buttons.

Final confirmation calls `reverse_inventory_transaction` through an idempotent request
function. PostgreSQL creates a new opposite transaction and movements atomically; it never
edits or deletes the original ledger. The request retains its reason and compensating
transaction ID. Cancellation changes no stock. Request creation, reason capture, final
confirmation, cancellation, Telegram message edits, and all callback-notification
enqueueing are safe to replay after a worker crash.

`ADJUST_STOCK` proposal creation is intentionally rejected for now. Before enabling it we
must distinguish a signed delta ("add two") from a stocktake assignment ("set this to
two"), because those operations have different concurrency and reversal semantics.

## Background input processing

`claim_telegram_text_event` atomically changes one stored Telegram message from `received`
to `processing` and resolves its organization member and active inventory location. A
second worker cannot claim the same event. A claim abandoned for 15 minutes can be reclaimed
after a worker crash, with every attempt counted for operations and audit. The Python
processor first checks whether the same member and chat have a reversal waiting for a
reason, then whether catalog creation is waiting for more details. A reversal reason is
captured without a model call. A catalog reply uses the dedicated catalog Structured
Output extractor. Otherwise it:

1. Extracts the strict, versioned command.
2. Matches each mutation line within the organization's catalog.
3. Stores an idempotent proposal with either a resolved variant or selectable candidates.
4. Enqueues a `proposal_ready`, `clarification_required`, or `unsupported_command` outcome.
5. Marks the source event `processed`; failures retain only a sanitized error and retry
   after 30 seconds, becoming `failed` after the third unsuccessful attempt.

Button results enqueue `proposal_ready`, `transaction_applied`, `callback_notice`, or
`reversal_reason_required` outcomes as appropriate. A captured reversal reason later
enqueues a `reversal_confirmation` outcome through the same durable outbox.

Invoice-image events use the same claim lease, matching, proposal, and outbox path. Their
original bytes and audit metadata are stored before model extraction.

The `processing_outbox` is the durable boundary between interpretation and Telegram
delivery. This matters because a Telegram outage must not cause the OpenAI call or proposal
creation to run again. Outcome insertion and proposal creation each have database-level
idempotency keys. The delivery worker uses skip-locked claims, a five-minute abandoned-claim
lease, delayed retry, and a dead-letter state after five unsuccessful attempts. It sends
only outcomes whose source event finished processing.

Telegram delivery is at-least-once: a process crash after Telegram accepts a message but
before the database records `sent` can result in a duplicate after lease recovery. The
message and inventory records remain idempotent, but exactly-once visual delivery is not
available from the Bot API boundary.

The prototype selects the organization's configured `settings.default_location_id` when
it names an active location; otherwise it deterministically uses the first active location
by code. Location selection from message hints and per-user defaults is not implemented yet.
Dead-lettered events remain available for audit and manual investigation.

## Repository layout

```text
.
├── docs/                  Architecture and engineering decisions
├── supabase/              Local config, migrations, seed data, and database tests
├── src/inventory_agent/   Python application, including private artifact handling
├── tests/                 Automated tests
├── .env.example           Safe configuration template
├── pyproject.toml         Project metadata and dependency declarations
└── uv.lock                Fully resolved Python dependencies
```

The committed `supabase/` directory contains local configuration, ordered SQL migrations,
deterministic development seed data, and pgTAP database tests.

## Local database workflow

After pulling database changes, rebuild the local database and run its tests:

```bash
supabase start
supabase db reset
supabase test db
```

Useful commands:

```bash
supabase status                 # Show local URLs and keys
supabase migration new <name>   # Create the next migration
supabase db reset               # Replay migrations and seed data locally
supabase stop                   # Stop services and preserve local data
```

Write schema changes as new files under `supabase/migrations/`; do not edit a migration
that has already been shared or deployed. `supabase db reset` targets the local database
by default. Never add `--linked` unless you intentionally mean to destroy and rebuild a
remote development environment.

The seed data creates one demo organization, manager, warehouse, ordinary products, a
medicine lot with an expiry date, and colour/size clothing variants. It is development
data only and must never be loaded into production.

## Build sequence

1. Project setup and health endpoint — complete
2. Supabase schema, seed inventory, atomic apply, and reversal functions — complete
3. Telegram webhook authentication and idempotent event ingestion — complete
4. Text intent extraction using a strict structured schema — complete
5. Exact identifier, alias, and configurable semantic/fuzzy/hybrid matching — complete
6. Telegram confirmation, editing, cancellation, and complete reversal — complete
7. Invoice image extraction — complete for photos and JPEG/PNG/WebP documents
8. No-match catalog creation with conversational detail extraction — complete for simple
   tracking
9. Semantic confidence calibration on representative SME datasets
10. Voice-note transcription
