# Inventory Agent

An inventory assistant for SMEs that turns Telegram text, invoice images, and voice
notes into reviewable inventory transactions. Inventory writes are confirmed by a
human, applied atomically, recorded in an immutable ledger, and reversible through
compensating transactions.

This repository is in the prototype stage. It currently includes the application
foundation, a health endpoint, the first Supabase inventory schema, atomic stock
application, immutable movements, compensating reversals, and authenticated,
idempotent Telegram webhook ingestion. A versioned Structured Outputs contract and
OpenAI Responses API interpreter run inside the continuous text-processing worker.
Organization-scoped catalog matching resolves exact identifiers and confirmed aliases,
then falls back to typo-tolerant PostgreSQL trigram candidates. The persisted text-event
processor now joins extraction, matching, and idempotent proposal creation, and records
its result in a durable outbound-message outbox. The same worker processes stored
Telegram button callbacks, applies idempotent proposal actions, and edits the original
confirmation message.

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
- Supabase: PostgreSQL, Storage, Row Level Security, and database functions
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
option is a Cloudflare Quick Tunnel:

```bash
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8000
```

You can use another tunnel provider instead. Copy the generated HTTPS hostname, append
`/webhooks/telegram`, and put the full URL in `.env` as `TELEGRAM_WEBHOOK_URL`.
Quick Tunnel hostnames change when restarted and are not suitable for production.

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
`TELEGRAM_WEBHOOK_URL`, and register it:

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

### 8. Run the background worker

The worker needs `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `SUPABASE_URL`, and
`SUPABASE_SECRET_KEY` in `.env`. Run it in a separate terminal:

```bash
uv run python -m inventory_agent.processing.worker --watch
```

Each cycle claims at most one button callback, one text event, and one outbound result, in
that order. Button actions are prioritized so confirmation stays responsive; the remaining
work still runs if one callback attempt fails. The worker polls every two seconds when all
three queues are idle. Use `--poll-seconds N` to choose an interval from greater than zero
through 60 seconds. Without `--watch`, it runs one complete cycle, which is useful while
debugging.

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
- Application component tests cross text processing, matching, proposal creation, outbox
  delivery, stored callback claiming, and cancellation against real local Supabase while
  keeping OpenAI and Telegram fake. They are opt-in because they require running
  infrastructure and create temporary rows:

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
evaluation. Automated tests use local fake responses and never spend API credits.

## Item matching

Candidate retrieval is implemented by the organization-scoped
`find_inventory_candidates` PostgreSQL function. It ranks evidence in this order:

1. Exact normalized SKU, barcode, manufacturer part number, or supplier part number.
2. Exact human-confirmed alias, optionally scoped to a supplier.
3. Trigram similarity across item names, variant names, SKUs, and confirmed aliases.

[`policy.py`](src/inventory_agent/matching/policy.py) converts ranked candidates into
`matched`, `needs_confirmation`, or `not_found`. Exact evidence is normally accepted, but
conflicting trusted results require human selection. A fuzzy result currently needs a
score of at least `0.72` and a lead of at least `0.12` over the next candidate. These are
prototype baselines to calibrate on labelled SME examples, not probabilities. Semantic
retrieval may later rerank the small candidate set; it will not bypass this policy or the
user's transaction confirmation.

## Transaction proposals and confirmation

`create_inventory_proposal` atomically stores a proposal and all of its lines. Repeated
processing with the same organization and idempotency key returns the existing proposal.
For resolved lines, PostgreSQL validates the variant and derives the signed base-unit
quantity using the configured unit conversion. Unresolved lines retain their candidate
evidence but have no stock delta, so they cannot be applied accidentally.

Telegram confirmation rendering uses compact opaque callback data containing only action
codes and UUIDs. Variant-selection callbacks fit below Telegram's 64-byte limit. A fully
resolved proposal gets Confirm and Cancel buttons; an unresolved proposal gets candidate
buttons and cannot be confirmed. The outbox delivery worker renders and sends these
messages.

Callback webhooks are stored before processing. The callback worker atomically claims the
oldest due event, resolves its active organization member, and routes decoded Select,
Confirm, and Cancel actions to separate database functions. It acknowledges valid Telegram
button presses before database work when possible; an expired acknowledgement does not
prevent the durable action. Malformed callback data is acknowledged with an alert and
never reaches Supabase. Confirm uses the existing atomic `apply_inventory_proposal`
function, so duplicate confirmations remain safe at the database boundary.

After an action, the worker edits the original Telegram message: selection refreshes the
proposal and its buttons, confirmation reports that inventory was updated, and cancellation
removes the buttons. Replayed identical edits are treated as success because Telegram may
answer that the message is already unchanged. Callback failures retry after 30 seconds and
become `failed` after the third unsuccessful attempt, matching text-event handling.

Variant selection rechecks organization membership, verifies that the variant was actually
offered, derives its base-unit delta, and records `human_selected` evidence. Lot- and
serial-tracked selections remain blocked until their required tracking-detail prompts are
implemented. Reversal buttons do not execute immediately because the system must collect
and retain a reason first.

`ADJUST_STOCK` proposal creation is intentionally rejected for now. Before enabling it we
must distinguish a signed delta ("add two") from a stocktake assignment ("set this to
two"), because those operations have different concurrency and reversal semantics.

## Background text processing

`claim_telegram_text_event` atomically changes one stored Telegram message from `received`
to `processing` and resolves its organization member and active inventory location. A
second worker cannot claim the same event. A claim abandoned for 15 minutes can be reclaimed
after a worker crash, with every attempt counted for operations and audit. The Python
processor then:

1. Extracts the strict, versioned command.
2. Matches each mutation line within the organization's catalog.
3. Stores an idempotent proposal with either a resolved variant or selectable candidates.
4. Enqueues a `proposal_ready`, `clarification_required`, or `unsupported_command` outcome.
5. Marks the source event `processed`; failures retain only a sanitized error and retry
   after 30 seconds, becoming `failed` after the third unsuccessful attempt.

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
├── src/inventory_agent/   Python application package
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
5. Exact identifier, alias, and fuzzy name matching — complete
6. Telegram confirmation, editing, cancellation, and reversal — in progress
7. Invoice image extraction
8. Voice-note transcription
9. Semantic candidate retrieval and calibrated confidence policies
