# Inventory Agent

An inventory assistant for SMEs that turns Telegram text, invoice images, and voice
notes into reviewable inventory transactions. Inventory writes are confirmed by a
human, applied atomically, recorded in an immutable ledger, and reversible through
compensating transactions.

This repository is in the foundation stage. The current application exposes a health
endpoint and establishes the project structure, configuration, dependency management,
and architecture that subsequent features will use.

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
git clone <repository-url> inventory-agent-v2
cd inventory-agent-v2
```

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

The Supabase project files and database migrations will live under `supabase/`. The
database scaffold is the next build phase; until `supabase/config.toml` exists, skip this
step. Once it has been added, start the local stack with:

```bash
supabase start
supabase db reset
```

Copy the local Project URL, publishable key, and secret key printed by `supabase start`
into `.env`. Local Supabase Studio is normally available at
<http://127.0.0.1:54323>.

The local stack is for development only. Do not expose it to external traffic.

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

## Development checks

Run these before committing a change:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

To apply safe formatting changes:

```bash
uv run ruff format .
```

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
| `SUPABASE_URL` | Supabase project API URL | local API URL |
| `SUPABASE_PUBLISHABLE_KEY` | Client-safe Supabase key | none |
| `SUPABASE_SECRET_KEY` | Server-only Supabase key | none |
| `SUPABASE_STORAGE_BUCKET` | Private source-artifact bucket | `inventory-source-artifacts` |

The default model is a configuration baseline, not a permanent product decision. Model
quality, latency, and cost will be measured against representative text and invoice cases.

## Repository layout

```text
.
├── docs/                  Architecture and engineering decisions
├── src/inventory_agent/   Python application package
├── tests/                 Automated tests
├── .env.example           Safe configuration template
├── pyproject.toml         Project metadata and dependency declarations
└── uv.lock                Fully resolved Python dependencies
```

The `supabase/` directory will be added with the first database migration and will contain
local configuration, migrations, seed data, and database tests.

## Build sequence

1. Project setup and health endpoint — current
2. Supabase schema, seed inventory, atomic apply, and reversal functions
3. Telegram webhook authentication and idempotent event ingestion
4. Text intent extraction using a strict structured schema
5. Exact identifier, alias, and fuzzy name matching
6. Telegram confirmation, editing, cancellation, and reversal
7. Invoice image extraction
8. Voice-note transcription
9. Semantic candidate retrieval and calibrated confidence policies
