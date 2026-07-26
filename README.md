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
default and uses OpenAI embeddings cached in PostgreSQL with pgvector. A constrained
candidate judge then selects only from retrieved variants, asks a focused follow-up
question, or rejects every candidate. Its multi-turn conversation state is durable.
Original invoice
images are checksummed and stored in a private Supabase bucket before extraction. Text and
image events share matching, idempotent proposal creation, and the durable outbound-message
outbox. If an invoice needs a question answered before matching, the complete extracted
command and every line item are stored in a durable clarification record. The next natural
reply resumes that saved command before ordinary conversation processing, so unrelated chat
history cannot replace the invoice contents. The same worker processes stored Telegram
button callbacks, applies idempotent proposal actions, and sends a new Telegram message
after every successful action.

## Architecture principles

- The language model interprets unstructured input; it never writes inventory directly.
- Every write becomes a transaction proposal before it can be applied.
- Matching occurs before confirmation, with uncertain matches shown to the user.
- Applying a transaction updates balances and writes ledger movements atomically.
- Reversal creates a new, opposite movement. Applied ledger entries are never deleted.
- Core inventory facts are relational. Typed custom fields handle company-specific needs.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the component and data design.
See [docs/MODEL_AND_ACTION_FLOW.md](docs/MODEL_AND_ACTION_FLOW.md) for the current
model-switching flowchart, reasoning-effort assignments, tool calls, and deterministic
inventory actions.

An opt-in LLM-led text path now lives on `experiment/llm-inventory-agent`. It gives one
conversational model organization-scoped inventory and transaction reads plus tools that
create pending stock or reversal proposals. The model cannot apply inventory; the existing
Telegram confirmation buttons and atomic database functions remain the write boundary.
Conversation history and the database IDs retrieved by the model are durable across worker
restarts. See [docs/AGENT_SPIKE.md](docs/AGENT_SPIKE.md) for its safety boundary,
evaluation scenarios, and initial results.

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

The recommended local command starts a loopback-only supervisor, which starts both the
API and background worker and enables their dashboard controls. Generate one local
development password:

```bash
openssl rand -hex 32
```

Put it in `.env` and enable the local services:

```dotenv
DEV_DASHBOARD_ENABLED=true
DEV_DASHBOARD_CONFIG_WRITES_ENABLED=true
DEV_DASHBOARD_USERNAME=inventory-dev
DEV_DASHBOARD_TOKEN=PASTE_THE_GENERATED_VALUE_HERE
DEV_SUPERVISOR_ENABLED=true
DEV_SUPERVISOR_URL=http://127.0.0.1:8765
DEV_SUPERVISOR_PORT=8765
DEV_SUPERVISOR_TOKEN=${DEV_DASHBOARD_TOKEN}
```

Keep `DEV_SUPERVISOR_TOKEN` below `DEV_DASHBOARD_TOKEN` so dotenv can expand it. A
separately generated supervisor token can be used instead.

For normal local development, launch the complete stack from the repository root:

```bash
./scripts/start-dev.sh
```

The start script:

- starts Docker Desktop on macOS if Docker is not already ready;
- starts the project’s local Supabase stack;
- launches the API and worker through the loopback supervisor;
- launches ngrok for port 8000, or reuses an existing port-8000 tunnel;
- waits for the services to become healthy and prints their URLs; and
- writes process logs and ownership PID files under the gitignored `.runtime/` directory.

It is safe to run the command again: healthy existing services are reused rather than
duplicated. To stop the development stack:

```bash
./scripts/stop-dev.sh
```

The stop script shuts down only ngrok/supervisor processes recorded as owned by this
project, verifies each recorded command before signalling its PID, and then stops local
Supabase. It does not close Docker Desktop, delete local database volumes, or terminate
an ngrok/supervisor process that was started independently.

To run the supervisor directly instead of using the convenience script:

```bash
uv run python -m inventory_agent.dev_supervisor
```

The supervisor binds to `127.0.0.1:8765`, accepts only authenticated start, stop, restart,
and status operations for the fixed API and worker commands, and automatically stops its
children when it exits. It cannot run arbitrary shell commands.

To run only the API without process controls, use the manual alternative:

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
the API running and expose port 8000 through an HTTPS tunnel. A free authenticated ngrok
account provides one assigned development domain that remains the same across agent
restarts, so it is the preferred prototype setup.

Install ngrok once:

```bash
brew install ngrok/ngrok/ngrok
```

Create a free ngrok account, copy its authtoken from the ngrok dashboard, and authenticate
this computer once:

```bash
ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
ngrok config check
```

The authtoken is written to ngrok's user-level configuration outside this repository.
Never put it in `.env` or commit it. `./scripts/start-dev.sh` starts or reuses the ngrok
tunnel automatically. The following separate terminals are only the manual alternative.

Terminal 1 — API and worker supervisor:

```bash
uv run python -m inventory_agent.dev_supervisor
```

Terminal 2 — stable public tunnel:

```bash
ngrok http 8000
```

Copy the assigned `https://...ngrok-free.dev` hostname, append `/webhooks/telegram`, and
put the full URL in `.env` as `TELEGRAM_WEBHOOK_URL`. The assigned development hostname
persists, but the ngrok process and local API must both be running for Telegram delivery.
The free plan is suitable for development, not production availability.
Verify the public route before registering it:

```bash
curl https://YOUR-ASSIGNED-DOMAIN.ngrok-free.dev/health
```

It should return:

```json
{"status":"ok","service":"inventory-agent"}
```

Before registering a webhook, identify your Telegram numeric user ID:

1. Put the BotFather token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Put the bot username without its leading `@` in `.env`, for example
   `TELEGRAM_BOT_USERNAME=capybababot`.
3. Send the bot a private message in Telegram.
4. Run:

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
webhook is active. If the webhook URL or secret changes, update `.env` and rerun the
registration command. Ordinary restarts of the assigned ngrok domain do not require
webhook re-registration. Never paste a real bot token, ngrok authtoken, or webhook secret
into source files, terminal screenshots, issues, or chat.

Changing `TELEGRAM_BOT_TOKEN` requires both sides of the integration to be reloaded:

1. Update the token in `.env`.
2. Run `uv run python -m inventory_agent.telegram.setup_webhook` to register the webhook
   with the new bot.
3. Run `./scripts/stop-dev.sh` followed by `./scripts/start-dev.sh` so the supervisor and
   its worker inherit the new token.

The dashboard's application restart only recreates the supervisor's API and worker child
processes. It deliberately leaves the supervisor running, so it cannot reload values that
changed in `.env`; without the full restart, incoming messages can reach the new bot's
webhook while replies are still sent with the old bot token.

#### Safe Telegram group-chat activation

Telegram privacy mode delivers addressed commands and direct replies to a bot, but it does
not reliably deliver an ordinary natural-language `@botname` mention. To support natural
group requests while preventing every group message from reaching OpenAI:

1. Open `@BotFather` in Telegram and send `/setprivacy`.
2. Select the inventory bot and choose **Disable**.
3. Remove the bot from each existing group and add it again; Telegram applies the changed
   privacy setting when the bot rejoins.
4. Confirm that `.env` contains the exact username without `@`:

   ```dotenv
   TELEGRAM_BOT_USERNAME=capybababot
   ```

5. Fully reload `.env` with:

   ```bash
   ./scripts/stop-dev.sh
   ./scripts/start-dev.sh
   ```

In a group or supergroup, the webhook accepts a message only when it:

- contains the configured `@botname`;
- is a direct Telegram reply to a message sent by that bot; or
- starts with a bot command.

All other group messages are acknowledged and discarded before membership lookup,
`source_events` storage, worker processing, or an OpenAI request. Accepted messages still
require the sender's Telegram user ID to map to exactly one active organization membership.
The original Telegram payload is retained for audit, while the configured bot mention is
removed from the text passed to the model. Private-chat behavior is unchanged.

Agent conversation context is separate for each organization, organization user, and chat,
so a group does not inherit anyone's direct-message conversation. Inventory balances and
transactions are currently company-scoped, however: every active member of the same
organization can read the shared company ledger. Per-role and per-user transaction
visibility remains a production-hardening task in the build sequence below.

#### Simulate multiple Telegram users in development

One real Telegram account can exercise onboarding, roles, separate conversation history,
and confirmation buttons through stable simulated identities. This feature is unavailable
unless `APP_ENV=development`, is disabled by default, and only a real Telegram account with
an active company `admin` membership may use it.

Enable it in the local `.env`:

```dotenv
TELEGRAM_DEV_USER_SIMULATION_ENABLED=true
TELEGRAM_DEV_USER_SIMULATION_SESSION_MINUTES=120
```

Then fully restart so the webhook process reloads `.env`:

```bash
./scripts/stop-dev.sh
./scripts/start-dev.sh
```

Send these commands through the real bot:

```text
/user bob     create or select Bob in this chat
/user Bob Lee create or select Bob Lee as the stable alias `bob-lee`
/user         show the identity active in this chat
/users        list personas created by your real account
/user me      return to your real Telegram identity
```

Persona names are case-insensitive; spaces are normalized to hyphens, and aliases contain
at most 28 letters, numbers, underscores, or hyphens. Each alias retains a stable synthetic
Telegram ID across restarts. Selection is scoped to the real controller account and
current chat, so choosing Bob in a private chat does not change the same controller's
identity in a group. The session expires after the configured period of inactivity.

To test the complete new-member journey:

1. As the real admin, create an invite in **Members & registration**.
2. In the bot's private chat, send `/user bob`.
3. Send `/register INVITE_CODE`. The pending dashboard row is marked `🧪 simulated`.
4. Approve Bob and choose a role in the dashboard.
5. Continue sending inventory messages as Bob. Replies are headed
   `🧪 Simulating Bob`, and buttons use Bob as their actor.
6. Use `/user me` before returning to ordinary testing.

The webhook secret is authenticated before simulation is considered. `/user` commands are
deterministic, stored outside the inventory event stream, and never call OpenAI. For an
ordinary message or callback, only the logical sender is replaced; the real controller ID,
persona ID, alias, synthetic ID, and destination chat are retained in the source-event
audit record. Simulated members still go through the normal invite, approval, role,
membership, proposal, confirmation, and conversation boundaries.

Disable `TELEGRAM_DEV_USER_SIMULATION_ENABLED` and fully restart before a real multi-person
demo. In production the feature remains unavailable even if the setting is accidentally
enabled.

#### Registering company members

The prototype has a deterministic, company-scoped invitation and approval flow. It uses
the immutable numeric Telegram user ID as identity; Telegram `@username` and display name
are retained only to help the admin recognize a pending applicant.

1. An existing company admin creates an invite in the member-management dashboard. The
   invite has an expiry and use limit; the UI defaults to three days and one use.
2. The dashboard displays the random invite code once. The database stores only its
   SHA-256 hash and a short non-secret hint, never the reusable plaintext code.
3. The applicant opens a private chat with the bot and sends:

   ```text
   /register INVITE_CODE
   ```

   Registration codes must not be posted in a group. Group registration commands are
   discarded before the code reaches storage.
4. The webhook handles `/register` before ordinary membership resolution, validates and
   consumes one invite use atomically, and creates a pending request for the invite's
   company. It does not store a normal inventory `source_event`, call OpenAI, expose
   inventory, or create an active membership.
5. The company's admin sees the pending Telegram user in the dashboard and either approves
   them with a `worker`, `manager`, or `admin` role or rejects them.
6. Approval creates the active `organization_users` membership, records an immutable
   membership audit entry, and queues a new Telegram approval message. Rejection first
   queues and successfully sends a new Telegram notice, then permanently deletes the
   pending request, Telegram ID, username, display name, and private source-chat ID.
   Telegram failures are retried with backoff and do not delete the applicant prematurely.

The company’s first admin is bootstrapped during company creation. The development seed
creates `Demo Admin`; subsequent admins must join through approval and applicants cannot
grant their own role.

To use it:

1. Set `DEV_DASHBOARD_CONFIG_WRITES_ENABLED=true` in `.env` and start the app.
2. Open the local dashboard, select **Members**, and choose **Generate invite**.
3. Copy the displayed `/register ...` command immediately and send it privately to the
   intended person. Refresh **Members** after they submit it.
4. Choose their role, then press **Approve** or **Reject**. The worker sends the result as
   a new Telegram message. No OpenAI API call is made anywhere in registration.

For an existing local database that predates this feature, apply the new migration without
resetting inventory:

```bash
supabase migration up --local
./scripts/stop-dev.sh
./scripts/start-dev.sh
```

The restart is required because the receiver gains the `/register` route and the worker
gains registration-notification delivery. Future ordinary invite and approval operations
take effect without restarting.

The old manual `organization_users` insertion remains a development-only recovery path in
Supabase Studio, but normal onboarding should use an invite so approval and role assignment
are audited. Never identify a member by display name alone.

The current roles are:

- `worker`: read company inventory and transactions, prepare ordinary stock additions or
  deductions, and confirm them;
- `manager`: worker capabilities plus catalog-item creation and transaction reversal;
- `admin`: currently the same inventory permissions as manager, reserved for later company
  administration features.

All three roles can currently read the company's shared transaction ledger. Role-specific
transaction visibility is not implemented yet.

To remove access without deleting audit-linked history, deactivate the membership:

```sql
update public.organization_users as member
set active = false
from public.organizations as organization
where member.organization_id = organization.id
  and organization.slug = 'cabybaba-pte-ltd'
  and member.telegram_user_id = 123456789;
```

Do not give the same Telegram user active memberships in multiple companies yet. The
webhook deliberately returns `organization_selection_required` instead of guessing which
company an incoming message belongs to.

After restarting the computer:

1. Open a terminal in the repository.
2. Run `./scripts/start-dev.sh`.

When finished, run `./scripts/stop-dev.sh`. Local database contents remain in the
project’s Docker volume and are restored by the next start.

If you do not want an ngrok account, a Cloudflare Quick Tunnel remains a temporary
fallback:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

Its random hostname changes after restarts, so update `TELEGRAM_WEBHOOK_URL` and rerun the
webhook setup command each time.

In manual mode, press `Ctrl+C` in the supervisor or tunnel terminal to stop that process.

### 8. Run the background worker

The worker needs `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `SUPABASE_URL`, and
`SUPABASE_SECRET_KEY` in `.env`. The defaults use semantic matching, so the same OpenAI
key is used for command extraction, conversational catalog-detail extraction, and
embeddings. The recommended `./scripts/start-dev.sh` command in step 6 already runs it.
When using the manual API command instead, run the worker in a separate terminal:

```bash
uv run python -m inventory_agent.processing.worker --watch
```

Each cycle claims at most one button callback, one invoice image, one text event, and one
outbound result, in that order. Button actions are prioritized so confirmation stays
responsive; the remaining work still runs if one attempt fails. The worker polls every two
seconds when all four queues are idle. Use `--poll-seconds N` to choose an interval from
greater than zero through 60 seconds. Without `--watch`, it runs one complete cycle, which
is useful while debugging.

`--watch` means continuous queue polling; it is not a source-code hot reloader. Stop the
existing worker with `Ctrl+C` and start it again after changing code or `.env`. During
local development, keep exactly one worker running so an older process cannot handle an
event with stale code. To check before starting another terminal:

```bash
ps aux | grep '[i]nventory_agent.processing.worker'
```

To route Telegram text through the LLM-led agent, first apply the latest migrations, then
set the feature flag in `.env`:

```bash
supabase migration up --local
```

```dotenv
INVENTORY_AGENT_ENABLED=true
INVENTORY_AGENT_MODEL=gpt-5.6-sol
INVENTORY_AGENT_REASONING_EFFORT=low
INVENTORY_AGENT_CONTEXT_POLICY=summarize
INVENTORY_AGENT_CONTEXT_RETENTION_DAYS=7
INVENTORY_AGENT_CONTEXT_MAX_TOKENS=30000
INVENTORY_AGENT_CONTEXT_MAX_ITEMS=300
```

Restart the worker after changing `.env`. This switch affects Telegram text only. Invoice
images continue through the existing structured extraction and matching pipeline, while
button confirmations, cancellations, catalog creation, transaction application, and
reversal application continue through their existing deterministic handlers.

#### Conversation context retention

The application—not OpenAI—maintains a separate conversation for each organization,
organization user, and Telegram chat. OpenAI requests use `store=false`. For each agent
turn, the worker loads that conversation's active history from Supabase, appends the new
user/model/tool items, sends the resulting item list to the Responses API, and saves an
immutable timestamped turn in `inventory_agent_turns`.

Context limits are checked twice:

1. Immediately before an LLM call, so an oversized context is never knowingly sent.
2. After a successful turn, in a background task that does not delay the queued Telegram
   reply.

Only one compaction task runs for a given organization, user, and chat at a time. If that
same conversation receives another message before its task finishes, the new turn waits
for compaction and then reloads the latest durable history before calling the model.
Unrelated conversations continue independently. On an orderly worker shutdown, pending
compactions are allowed to finish before the OpenAI client closes.

A turn leaves active context when it is older than
`INVENTORY_AGENT_CONTEXT_RETENTION_DAYS`, or when retaining it would exceed either the
approximate token budget or item limit. The token estimate uses the serialized history
size and is deliberately conservative; `INVENTORY_AGENT_CONTEXT_MAX_ITEMS` also stays
below the database's hard 400-item ceiling.

`INVENTORY_AGENT_CONTEXT_POLICY` controls what replaces removed active history:

- `summarize` asks the configured inventory-agent model for an incremental rolling
  summary, then sends that summary with the exact recent turns. This can make an additional
  model call only when compaction is required.
- `discard` excludes the old turns without generating or retaining a rolling summary.

Both policies retain immutable raw turn rows for the development dashboard and audit.
They only remove turns from future model prompts; they do not delete Telegram source
events, proposals, catalog data, inventory transactions, or balances. Compaction clears
previously grounded variant and transaction IDs, forcing later mutations to retrieve
authoritative database state again. Summaries are explicitly non-authoritative and must
not be used as current stock or transaction state. Encrypted/private reasoning output is
retained in the immutable turn audit but removed from later model inputs and summaries at
the end of each turn.

The four application values above are defaults. The development dashboard can store a
complete override for one company in `organizations.settings.inventory_agent.context`.
The worker loads that override before each compaction check, so a saved override applies
on the next agent message without restarting the worker. Resetting the override makes the
company inherit `.env` again. Every save and reset is recorded in
`organization_setting_changes`; secrets and model credentials are never company settings.

### 9. Use the development dashboard

The FastAPI process includes a development console at
`http://127.0.0.1:8000/dev`. It is disabled by default, never available when
`APP_ENV=production`, and every page and API request requires HTTP Basic authentication.
The browser receives dashboard data but never receives the Supabase secret key.

Generate a dedicated local password:

```bash
openssl rand -hex 32
```

Add the dashboard settings to `.env`:

```dotenv
DEV_DASHBOARD_ENABLED=true
DEV_DASHBOARD_CONFIG_WRITES_ENABLED=true
DEV_DASHBOARD_USERNAME=inventory-dev
DEV_DASHBOARD_TOKEN=PASTE_THE_GENERATED_VALUE_HERE
DEV_SUPERVISOR_ENABLED=true
DEV_SUPERVISOR_URL=http://127.0.0.1:8765
DEV_SUPERVISOR_PORT=8765
DEV_SUPERVISOR_TOKEN=${DEV_DASHBOARD_TOKEN}
```

Leave `DEV_DASHBOARD_CONFIG_WRITES_ENABLED=false` if the console should be entirely
read-only. When enabled, it permits the validated, non-secret context settings and the
explicit development-admin actions shown in the dashboard, including registration
administration and the guarded inventory reset. It does not permit arbitrary
environment-variable, secret, model, or shell changes.

Restart the API after changing `.env`, then open:

```text
http://127.0.0.1:8000/dev
```

Use `inventory-dev` as the username and the generated token as the password. When the API
is exposed through ngrok, `/dev` is exposed too, so keep the generated token private and
do not put it in screenshots, source control, or shell history shared with others. The
dashboard settings page identifies which values are editable and whether a restart is
required.

The dashboard has seven views:

- **Flow inspector** lists Telegram source events and reconstructs the durable path through
  raw input, source artifacts, current conversation context, model/tool messages, proposal
  and matching evidence, catalog clarification, outbound delivery, and applied transaction.
  Expandable JSON preserves the exact stored records for debugging.
- **Inventory** shows every SKU, item and variant attributes, tracking mode, current
  location balances, unit conversions, and the recent immutable transaction ledger. Its
  danger zone contains the guarded test-data reset described below.
- **Members & registration** creates expiring invite codes, shows pending applicants, and
  lets a company admin approve a role or reject and remove an applicant.
- **App health** gives each live component a plainly named health card. Green means the
  local process controller, Telegram receiver/dashboard, inventory message processor,
  Supabase API, or Telegram public connection passed its current check; red shows the
  failure reason. Hover over a control for its scope. From the localhost URL only, the
  fixed controls can start or restart the application, restart only the receiver or
  processor, or pause message processing. The local process controller remains outside
  the receiver process it manages.
- **Conversations** lists company-scoped Telegram users and chats, then shows the rolling
  compacted summary, complete current stored history, active immutable turns, compacted
  immutable turns, and approximate active token usage separately.
- **Configuration** shows the effective company context limits, whether each value comes
  from `.env` or a company override, other non-secret runtime parameters, and the settings
  audit trail. It also lists each model-backed component, effective model, reasoning
  effort, runtime status, invocation condition, and action. Saving or resetting a context
  override applies on the next agent message.
- **Models & prompts** shows the active model configuration, complete current system
  instructions, prompt versions, and tool definitions for the main agent, structured
  extraction, invoice extraction, catalog detail extraction, candidate judgment, and
  semantic retrieval.

#### Reset one company's inventory test data

Use this only from the local dashboard while stress testing:

1. Select the company in the dashboard header.
2. Open **Inventory** and scroll to **Danger zone**.
3. Click **Reset inventory test data**.
4. Type the exact company-specific phrase shown, such as `RESET cabybaba-pte-ltd`.

The API pauses the inventory message processor, runs one atomic company-scoped database
reset, and starts the processor again if it was running beforehand. It deletes:

- catalog items, variants, identifiers, aliases, unit conversions, and embeddings;
- balances, lots, serials, immutable transaction ledger rows, and stock movements;
- pending proposals, matching/catalog/reversal requests, and outbound processing records;
- Telegram source-event and source-artifact database records; and
- agent conversations and their stored turn history.

It preserves the company, approved members and roles, locations, custom-field definitions,
company settings, registration records, and a durable reset audit containing the deleted
row counts. The reset is rejected unless dashboard writes and the local supervisor are
enabled, the request comes through `localhost`, an active company admin exists, and the
confirmation is exactly `RESET <selected-company-slug>`. Files previously uploaded to the
local Supabase Storage bucket are not deleted in this prototype; their application
database records are removed, so they no longer appear in the dashboard or agent flow.

For the most recent main-agent turn, the raw turn trace retains the model/tool exchange and
any exact UUID resolution used during that turn for dashboard auditing. The reusable
conversation snapshot deliberately excludes ephemeral transaction-selection context.
Older source events and deterministic callback events show the latest reusable snapshot
for the same Telegram chat as supporting context; the selected event's raw input,
proposal, matching evidence, outbox, and transaction records remain event-specific.

The agent can:

- read catalog variants and on-hand balances, using exact identifiers or the configured
  semantic/fuzzy/hybrid retrieval strategy;
- ask a natural follow-up question and continue from durable conversation history;
- create an add or deduct proposal using only variants refreshed by an inventory read
  during that user message;
- propose a new catalog item through the existing review and catalog-creation flow;
- read the transaction ledger and create a reversal request using only a returned
  transaction; and
- answer inventory questions while declining unrelated chat.

Name-based inventory reads return ranked candidates with their match method and score;
they are not presented as an exact filtered set. For a generic category count such as
“how many hairdryers,” the agent requests up to 50 candidates, inspects all results,
excludes incidental matches, and reports every relevant variant plus the total. A new
unqualified question is not narrowed by a colour or other qualifier from an earlier turn.
If a user disputes a reported balance, the agent must perform a broader fresh read before
asking whether stock should be added.

Filtered transaction reads use ranked token matching rather than one literal phrase. They
also include recent transactions as fallback evidence, so wording such as “red T-shirt
sale” can still retrieve a stored `issue` transaction whose summary uses different terms.
The agent may not conclude that a transaction is absent until it has inspected the recent
unfiltered ledger. A full transaction UUID takes a separate exact-match path and does not
append unrelated recent fallback rows. Every returned transaction explicitly includes
the database's `transaction_type` and `status`, plus its timestamp, summary, and derived
`reversed` flag. Agent replies must preserve those lifecycle terms rather than inventing
labels such as “active.”

Transaction UUIDs are display identifiers, not model-controlled mutation arguments. When
the user includes a UUID, the application extracts it from the raw Telegram message and
performs an exact organization-scoped lookup before the model runs. A found record becomes
a short current-turn reference such as `T1`; `propose_reversal` accepts only that reference.
Natural-language or positional selections such as “the first one” must call
`read_transactions` again during the current message and use the newly returned reference.
References expire at the end of the message. Old transaction tool outputs and assistant
paraphrases from transaction-read turns are removed from reusable model context while the
raw audit turn remains stored. UUIDs are never fuzzy-matched or silently repaired.

Every mutation remains pending until the user presses the Telegram confirmation button
or sends the exact standalone text command `Confirm` while that proposal is the active
proposal for their conversation. Exact standalone `Cancel` has the same effect as the
Cancel button. These commands bypass the model and use the same deterministic database
actions as the buttons; broader conversational uses of “confirm” or “cancel” remain
ordinary agent messages. If there is no active proposal, the bot refuses to guess among
older pending records and changes no stock. The agent reply and the rendered review are
sent together as a new Telegram message.
Turning `INVENTORY_AGENT_ENABLED` back to `false` restores the previous structured text
processor; no migration rollback is required.

For this testing phase, the demo catalog and agent assume `simple` tracking for every
item. The database retains its lot/serial design for later implementation, but the agent
must not request lot, batch, expiry, or serial details and new catalog items are constrained
to simple tracking. New items generally require only a product name and SKU or internal
code. An individually counted physical item silently defaults to canonical base unit
`each`; `unit`, `units`, `item`, and `items` are equivalent input and never require a user
choice. The agent asks about units only when packaging or measurement changes the inventory
meaning, such as boxes versus individual tablets, kilograms, or litres. The agent may
suggest useful custom attributes such as strength, colour, or size, but must present them
as optional unless the company has explicitly configured a field as required. Every
attribute question includes its reason: either the current
inventory tracks that field, the field distinguishes existing variants, or a similar
catalog item suggests it may be useful. Similar-item suggestions can be skipped and do not
become requirements. The agent does not ask for a value already established by an exact
SKU match. Attributes supplied by the user are retained in the item draft and stored when
the item is confirmed.

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
- Application component tests cross structured and LLM-led text processing,
  invoice-image processing, private Storage, matching, durable conversation storage,
  proposal creation, outbox delivery, stored callback claiming, and cancellation against
  real local Supabase while keeping OpenAI and Telegram fake. They are opt-in because they
  require running infrastructure and create temporary rows and objects:

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

### Randomized full-pipeline stress evaluation

The local stress harness runs at least 101 randomized user journeys through authenticated
Telegram webhook ingestion, durable event processing, the inventory-agent boundary,
catalog matching and creation, confirmations, cancellations, reversals, outbox delivery,
and the real local Supabase ledger. It simulates several users, group/private chats,
model responses, Telegram delivery, typos, duplicate updates, unsafe model output,
duplicate and already-used catalog SKUs, and transaction lookup by rough time, product,
actor identity, and relative position such as “four transactions ago.” Unsupported
capabilities are recorded as failures instead of being silently skipped. It does not call
OpenAI or Telegram, so the run costs no API credits and sends no real messages.

Stop the live worker before the run so it cannot claim the simulated source events, then
run:

```bash
uv run python -m inventory_agent.evaluation.pipeline_stress \
  --scenarios 150 \
  --users 24 \
  --seed 20260728
```

The harness deliberately retains its clearly named `Inventory Pipeline Stress <seed>`
organization and inventory for inspection in the development dashboard. Use a new seed
for an independent repeat. The Markdown report is written to
`docs/testing/PIPELINE_STRESS_REPORT.md`; use `--report PATH` to change it. Restart the
worker after the run. The report distinguishes application/database safety, correctness,
Telegram UX, injected model failures, and p50/p95/max stage timings.

This is a local text-pipeline evaluation, not a live-model language benchmark. Invoice
images remain covered by the application component tests, while speech transcription is
not implemented yet.

### Experimental LLM-led agent evaluation

The isolated agent spike has deterministic unit tests that use fake model responses:

```bash
uv run pytest tests/test_agent_tools.py tests/test_agent_runtime.py \
  tests/test_agent_simulator.py
```

Its live evaluation is an explicit, billable opt-in. It uses only an in-memory catalog,
ledger, and proposal store, so it cannot alter inventory:

```bash
uv run python -m inventory_agent.agent.simulator --live
```

Use `--scenario NAME` to run one case. The scenario names and expected behaviors are
documented in [docs/AGENT_SPIKE.md](docs/AGENT_SPIKE.md).

## Configuration

Configuration is read from environment variables and `.env` by
`inventory_agent.config.Settings`.

| Variable | Purpose | Local default |
|---|---|---|
| `APP_ENV` | Runtime environment | `development` |
| `LOG_LEVEL` | Application log level | `INFO` |
| `DEV_DASHBOARD_ENABLED` | Enable the authenticated `/dev` dashboard outside production | `false` |
| `DEV_DASHBOARD_CONFIG_WRITES_ENABLED` | Permit audited settings and guarded development-admin actions from the dashboard | `false` |
| `DEV_DASHBOARD_USERNAME` | HTTP Basic username for the development dashboard | `inventory-dev` |
| `DEV_DASHBOARD_TOKEN` | Dedicated password for the development dashboard | none |
| `DEV_SUPERVISOR_ENABLED` | Enable the loopback-only API/worker process supervisor and dashboard controls | `false` |
| `DEV_SUPERVISOR_URL` | Server-side URL used to reach the local supervisor | `http://127.0.0.1:8765` |
| `DEV_SUPERVISOR_PORT` | Loopback port on which the supervisor listens | `8765` |
| `DEV_SUPERVISOR_TOKEN` | Bearer token for API-to-supervisor calls; may reuse the local dashboard token | none |
| `OPENAI_API_KEY` | OpenAI Platform API key | none |
| `OPENAI_MODEL` | Extraction and intent model | `gpt-5.6-luna` |
| `OPENAI_REASONING_EFFORT` | Reasoning level for routine extraction | `none` |
| `INVENTORY_AGENT_MODEL` | LLM-led Telegram text and spike-evaluation model | `gpt-5.6-sol` |
| `INVENTORY_AGENT_REASONING_EFFORT` | Reasoning level for the LLM-led agent | `low` |
| `INVENTORY_AGENT_ENABLED` | Route Telegram text through the LLM-led agent | `false` |
| `INVENTORY_AGENT_CONTEXT_POLICY` | Old context handling: `summarize` or `discard` | `summarize` |
| `INVENTORY_AGENT_CONTEXT_RETENTION_DAYS` | Exact-turn retention window per user/chat | `7` |
| `INVENTORY_AGENT_CONTEXT_MAX_TOKENS` | Approximate active-context token ceiling | `30000` |
| `INVENTORY_AGENT_CONTEXT_MAX_ITEMS` | Active Responses API item ceiling; maximum `350` | `300` |
| `OPENAI_EMBEDDING_MODEL` | Semantic inventory embedding model | `text-embedding-3-small` |
| `OPENAI_EMBEDDING_DIMENSIONS` | pgvector embedding width; fixed by the current schema | `512` |
| `INVENTORY_MATCHING_STRATEGY` | Name matching: `semantic`, `fuzzy`, or `hybrid` | `semantic` |
| `INVENTORY_CANDIDATE_JUDGING_ENABLED` | Constrained LLM candidate judgment and follow-up questions | `true` |
| `INVENTORY_DISPLAY_TIMEZONE` | IANA timezone for user-facing transaction timestamps | `Asia/Singapore` |
| `TELEGRAM_BOT_TOKEN` | BotFather token | none |
| `TELEGRAM_BOT_USERNAME` | Username without `@`, used to activate and clean group requests | none |
| `TELEGRAM_DEV_USER_SIMULATION_ENABLED` | Enable admin-only `/user NAME` identity simulation in development | `false` |
| `TELEGRAM_DEV_USER_SIMULATION_SESSION_MINUTES` | Simulated identity inactivity window per controller and chat | `120` |
| `TELEGRAM_WEBHOOK_SECRET` | Verifies Telegram webhook requests | none |
| `TELEGRAM_WEBHOOK_URL` | Public HTTPS `/webhooks/telegram` endpoint | none |
| `SUPABASE_URL` | Supabase project API URL | local API URL |
| `SUPABASE_PUBLISHABLE_KEY` | Client-safe Supabase key | none |
| `SUPABASE_SECRET_KEY` | Server-only Supabase key | none |
| `SUPABASE_STORAGE_BUCKET` | Private source-artifact bucket | `inventory-source-artifacts` |

The default model is a configuration baseline, not a permanent product decision. Model
quality, latency, and cost will be measured against representative text and invoice cases.

### Runtime latency logs

With the default `LOG_LEVEL=INFO`, successful requests emit structured
`component_runtime` lines containing `duration_ms`. The supervisor's API and worker log
panels show these entries. Idle outbox polling is intentionally not logged, so useful
timings remain visible.

Timed components include Telegram webhook ingestion, total text-event processing,
conversation load/save and compaction, every inventory-agent model round and OpenAI
request, catalog search, embeddings, balance reads, agent tools, structured extractors,
outbox rendering, and Telegram delivery. To inspect local process logs:

```bash
rg "component_runtime" .runtime/logs/*.log
```

For one slow reply, compare `agent_model_round`, `agent_tool.*`,
`catalog_candidate_search`, `openai_embeddings`, and `telegram_api_send`. The largest
`duration_ms` is the dominant measured stage; `telegram_text_event_total` is the
worker-side total.

#### Live local benchmark

The opt-in benchmark exercises the complete agent path against local Supabase without
sending Telegram messages:

```bash
uv run python scripts/benchmark-agent.py
```

This command uses the configured OpenAI API key and therefore consumes API credits. It
also creates a real one-unit receipt and an exact-transaction reversal. Their net stock
change is zero, while both immutable inventory ledger entries remain as an audit trail.
The command refuses to run against a hosted Supabase URL.

On 25 July 2026, using `gpt-5.6-luna` at low reasoning effort, the original diagnostic
run measured approximately:

| Scenario | End-to-end runtime |
|---|---:|
| Semantic inventory query | 7.9 s |
| Recent transaction query | 7.3 s |
| Exact transaction UUID query | 2.1 s |
| Receipt proposal | 8.4 s |
| Exact UUID reversal proposal | 3.5 s |
| Database receipt/reversal confirmation | 24–27 ms |

OpenAI model rounds were the main cost. Semantic retrieval added about 2.3 seconds for
the embedding request, while ordinary local database operations took roughly 10–56 ms.
This test organization had an unusually low 1,000-token context override. It therefore
summarized after nearly every turn, and each extra OpenAI call added 1.8–2.4 seconds.
Raising or resetting the override toward the documented 30,000-token default makes those
summarization calls less frequent. The tradeoff is that retained conversations can then
grow larger, which may gradually increase the main model call's latency and token usage.
That diagnostic run preceded background post-turn compaction. Telegram delivery now
continues as soon as the reply is in the outbox; a post-turn summary no longer extends the
current reply's critical path.

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
needs—item name, SKU or internal code, any materially ambiguous package or measurement
unit, and optional company-specific attributes—but does not require the user to fill in
JSON or a fixed text form. Individually counted items default to `each`. The worker
extracts those facts from natural language, combines them with safe suggestions already
derived from the transaction, persists partial answers across clarification turns, and
asks only for fields that remain missing. A final Telegram confirmation is required before
the catalog item is created. The catalog interpreter also classifies whether a message
actually answers that pending item request; a separate receipt, deduction, query, or other
inventory command bypasses the stale request and reaches normal processing. The current
prototype creates simple-tracked items only; lot and serial creation require an additional
tracking-details flow.

Company SKUs are unique per inventory variant. Agent-suggested catalog drafts are checked
against current SKU ownership before the **Create item** confirmation is shown and checked
again when the button is pressed. If a code is already used, the bot sends a new,
explicit **Different SKU needed** message showing the existing and requested attributes,
retains the other draft details, and accepts a natural-language replacement SKU. Nothing
is created and the callback is completed rather than silently retried. A shared external
manufacturer part number should eventually be stored separately from the variant’s unique
company SKU, as described in the build sequence.

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

When the image contains usable line items but leaves the requested inventory action
ambiguous, the worker persists the exact structured extraction in
`command_clarification_requests` before asking one concise question. A reply such as
“Yes, all received stock” is merged into that saved extraction by a constrained Structured
Outputs call. Original identifiers, quantities, units, and attributes must be retained
unless the user explicitly corrects them. The resolved command then re-enters the normal
matching and proposal path; it does not pass through the ordinary conversational agent or
inherit an unrelated product from chat history. This state survives worker restarts and is
shown as **Input clarification** in the dashboard event flow.

## Item matching

Exact and fuzzy retrieval is implemented by the organization-scoped
`find_inventory_candidates` PostgreSQL function. Semantic retrieval uses
`list_inventory_embedding_documents`, an OpenAI embedding call, the
`inventory_variant_embeddings` cache, and `find_semantic_inventory_candidates`.
All database functions require an organization ID and never search across tenants.

Evidence is evaluated in this order:

1. Exact normalized SKU, barcode, manufacturer part number, or supplier part number.
2. Exact human-confirmed alias, optionally scoped to a supplier.
3. The configured name-matching strategy retrieves a short candidate list:
   - `semantic` (default): embedding cosine similarity across names, SKU, attributes, and
     confirmed aliases.
   - `fuzzy`: PostgreSQL trigram similarity across names, SKU, and aliases.
   - `hybrid`: a weighted semantic/fuzzy score for evaluation.

4. The Structured Outputs candidate judge sees only the retrieved IDs, names, SKUs,
   catalogue attributes, original wording, and company matching rules. It may return only
   `SELECT`, `ASK_USER`, or `NO_MATCH`; application code verifies that any selected UUID
   was actually offered.
5. [`policy.py`](src/inventory_agent/matching/policy.py) still gates automatic selection
   using retrieval score and margin. A model judgment cannot bypass that deterministic
   confidence policy or apply inventory.

Exact evidence is normally accepted, but
conflicting trusted results require human selection. A fuzzy result currently needs a
score of at least `0.72` and a lead of at least `0.12` over the next candidate. These are
prototype baselines to calibrate on labelled SME examples, not probabilities. Semantic
scores use their own initial threshold of `0.42` and top-two margin of `0.10`, based on a
small smoke test only; they must be calibrated on labelled SME examples before production.
Semantic retrieval and candidate judgment never bypass the user's transaction confirmation.

### Variant attributes and company-specific rules

Attributes that change which SKU or balance is being handled belong on
`item_variants`. For example, `Classic T-Shirt` is one item family, while Red / M and
Blue / L are separate variants with separate SKUs, attributes, and balances. Operational
facts such as medicine expiry date and batch number normally belong to a lot and do not
create a new catalogue variant.

Each company can classify an active item/variant custom field by putting
`matching_role` in `custom_field_definitions.validation_rules`:

```json
{"matching_role":"discriminator"}
```

The supported roles are:

- `discriminator`: identifies a variant; a contradiction rejects the candidate, and a
  missing value may trigger a question such as “Which colour is it?”
- `supporting`: useful matching evidence but not necessarily identity.
- `operational`: receipt/issue or lot information such as expiry and batch.
- `ignored`: excluded from matching.

The development seed marks `colour` and `size` as discriminators and `expiry_date` and
`batch_number` as operational. If no rule exists, the judge uses conservative domain
defaults and the result still passes through the deterministic confidence policy.

When the judge returns `ASK_USER`, the proposal and its candidates are stored first. The
bot sends the question as a new Telegram message. The next text from that member in that
chat is routed to the durable `match_clarification_requests` record before ordinary
command extraction. Answers may resolve the variant, establish that none match, or cause
one more focused question. Learned attributes and every source event remain attached to
the proposal; a restart does not lose the conversation.

If no candidate is confident, the bot explicitly offers **Add new item** and
**Choose existing**. Choosing existing displays fallback candidates in descending score
order. Adding an item starts the conversational detail flow described above. After item
creation, the original proposal line is linked to the new variant and a fresh proposal
review message is sent.

When any proposal—not only an invoice—contains several no-match lines, Telegram runs one
durable line-selection workflow before asking for catalog details. The worker resolves one
line at a time as **Add**, **Match**, or **Ignore**. Each callback removes the controls
from its source message without replacing the text, then sends a fresh review for the next
line. **Mark remaining N as new** is the shortcut when every remaining unmatched line is
valid.

Ignored lines remain in the proposal and keep the actor and timestamp in
`match_evidence`, but `apply_inventory_proposal` excludes them from transaction lines,
movements, and balances. Ignore is never interpreted as a catalog match, and the final
active line cannot be ignored.

Only after the line decisions are complete does the bot collect missing details. Multiple
new products enter one catalog batch that retains every extracted description, quantity,
and unit, then asks once for missing identifiers and optional attributes. The user can
reply naturally, approve suggested SKUs, correct selected lines, or explicitly request
unique internal SKUs generated from the product specifications. One combined catalog
and stock review creates the selected items and applies the complete receipt in one
database transaction. If either operation fails, neither catalog nor inventory changes.
Its Cancel action rejects both the catalog batch and stock proposal. If the decisions
leave only one new product, the ordinary single-item detail flow is used.

Proposal reviews and catalog drafts prefer the preserved source item phrase over a generic
normalized label. Consequently, lines such as several products normalized to `SOLENOID
VALVE` still show their voltage, open/closed state, connection size, or other
distinguishing source specifications.

## Transaction proposals and confirmation

`create_inventory_proposal` atomically stores a proposal and all of its lines. Repeated
processing with the same organization and idempotency key returns the existing proposal.
For resolved lines, PostgreSQL validates the variant and derives the signed base-unit
quantity using the configured unit conversion. Unresolved lines retain their candidate
evidence but have no stock delta, so they cannot be applied accidentally.

The explicitly generic words `each`, `unit`, `units`, `item`, `items`, `pc`, `pcs`,
`piece`, and `pieces` mean one unit of the matched SKU and receive a factor-one conversion.
For a new individually counted item, these words are stored canonically as `each` so
equivalent vocabulary cannot fragment the catalog. Package words such as `box`, `carton`,
and `case` still require an
organization-and-variant-specific conversion; the system does not guess package sizes.

Telegram confirmation rendering uses compact opaque callback data containing only action
codes and UUIDs. Variant-selection callbacks fit below Telegram's 64-byte limit. A fully
resolved proposal gets Confirm and Cancel buttons; an unresolved proposal gets candidate
buttons and cannot be confirmed. The outbox delivery worker renders and sends these
messages.

As a fallback when Telegram controls are temporarily unavailable, a user may send exactly
`Confirm` or `Cancel`. The text worker resolves that command only against
`inventory_agent_conversations.last_proposal_id`, which is scoped by organization member
and Telegram chat. It never selects a proposal merely because it is the newest database
row. Confirmation calls the same idempotent `apply_inventory_proposal` function; the
result is recorded as a deterministic conversation system turn and delivered using the
normal transaction-success message. A command with no active proposal produces an
explicit no-change notice and never reaches the LLM.

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
cancellation sends a status notice. The worker removes only the obsolete inline keyboard
from the originating message; it does not replace that message's text, so the original
review remains visible in the conversation. Replayed identical keyboard removals are
treated as success because Telegram may answer that the markup is already unchanged.
Callback failures retry after 30 seconds and become `failed` after the third unsuccessful
attempt, matching text-event handling.

Every successful stock addition, deduction, adjustment, or reversal message displays its
full copyable transaction UUID together with the localized transaction timestamp. A
pending reversal review repeats the original transaction UUID and timestamp. Users can
therefore identify an earlier transaction by ID when asking the agent to reverse or
correct it, while still being free to describe the transaction naturally.

When the LLM-led agent is enabled, completed proposal, catalog-item, and reversal
confirmations or cancellations also append a deterministic `system` item to that user's
durable conversation. Exact typed proposal controls do the same without a model call. The
next model turn therefore knows whether the preceding proposal was applied or cancelled,
while still being required to read the authoritative ledger before correcting or
reversing a transaction. These lifecycle records are retained and compacted under the
same conversation policy as agent turns.

System-generated Telegram messages use a small visual status vocabulary so the write
state is visible at a glance:

- `➕ ADD` marks every line that will increase stock.
- `➖ DEDUCT` marks every line that will decrease stock.
- `⏳` means a proposed catalog, stock, or reversal change is still pending confirmation.
- `✅` means the inventory transaction or reversal was applied successfully.
- `🚫` means the user cancelled the operation and no pending change was applied.
- `❓` means the system needs a natural-language reply before it can continue.
- `⚠️` means a proposal has unresolved lines and cannot yet be confirmed.
- `🔎` means no confident catalog match was found.

These headings are added by deterministic Telegram renderers rather than generated by the
model. Proposal and reversal messages explicitly state when inventory has not changed.
Applied-transaction receipts show the authoritative database `applied_at` timestamp.
Their success heading also states `Stock added`, `Stock deducted`, or `Stock adjusted`
from the stored transaction type rather than using a generic inventory-update message.
Final reversal review shows the original transaction timestamp, while a successful
reversal shows the compensating transaction timestamp. All are rendered using
`INVENTORY_DISPLAY_TIMEZONE`; use an IANA name such as `Asia/Singapore` or `Europe/London`.
Changing this `.env` value requires the full stop/start procedure described above.

Outbound Telegram text is HTML-escaped before delivery. The narrow `**text**` emphasis
syntax used by agent replies is translated to Telegram-safe bold text; arbitrary model
output is not passed directly to Telegram's markup parser.

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

Reversal currently applies to the complete transaction rather than selected lines. To
correct one line in a multi-line transaction, reverse the complete original transaction
and then create and confirm one corrected replacement transaction. When the corrected
quantities are already known, the agent retrieves their current variants and creates the
replacement proposal during the initial correction request. The reversal request stores a
validated link to that proposal. Confirming the reversal automatically sends the corrected
replacement review and its Confirm/Cancel buttons in the same new Telegram message; no
extra user text or second model call is required. A pure reversal has no linked
replacement. Cancelling the reversal rejects any hidden linked replacement.

The reversal and replacement remain two separately confirmed writes: confirming the
reversal restores all stock from the original transaction, while cancelling the
subsequent replacement leaves that restored stock unchanged. The agent is explicitly
instructed not to add a second deduction on top of the incorrect applied transaction.

`ADJUST_STOCK` proposal creation is intentionally rejected for now. Before enabling it we
must distinguish a signed delta ("add two") from a stocktake assignment ("set this to
two"), because those operations have different concurrency and reversal semantics.

## Background input processing

`claim_telegram_text_event` atomically changes one stored Telegram message from `received`
to `processing` and resolves its organization member and active inventory location. A
second worker cannot claim the same event. A claim abandoned for 15 minutes can be reclaimed
after a worker crash, with every attempt counted for operations and audit. The Python
processor first checks whether the same member and chat have a reversal or catalog flow
waiting for more details.

With `INVENTORY_AGENT_ENABLED=true`, ordinary text then enters a durable Responses API
tool loop. The model can retrieve inventory or transactions and can create one pending
proposal per message. PostgreSQL verifies organization membership, validates every
retrieved ID again when the conversation is saved, and rejects a proposal containing an
unread ID. Application tools additionally require current-turn retrieval evidence before
creating a resolved proposal, preventing stale conversation IDs from producing lines that
cannot be confirmed. The turn history, allowed IDs, final reply, proposal reference, provider
response ID, and model name are saved in `inventory_agent_conversations`. A retry of the
same source event reuses the saved turn rather than paying for or creating another model
response.

For reversals, persisted transaction IDs are audit metadata only and never authorize a
later model turn. Authorization comes from a server-managed `T1`, `T2`, and so on mapping
created by exact UUID resolution or `read_transactions` in the current message.

With the flag disabled, the structured processor checks candidate clarification before
ordinary command extraction. In both modes, a reversal reason is captured without a model
call and catalog replies use the dedicated catalog Structured Output extractor. The
structured path otherwise:

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

The seed data creates one demo organization, manager, warehouse, simple-tracked products,
and colour/size clothing variants. It retains an unused example lot record for database
lot-function tests. It is development data only and must never be loaded into production.

## Build sequence

1. Project setup and health endpoint — complete
2. Supabase schema, seed inventory, atomic apply, and reversal functions — complete
3. Telegram webhook authentication and idempotent event ingestion — complete
4. Text intent extraction using a strict structured schema — complete
5. Exact identifier, alias, configurable semantic/fuzzy/hybrid retrieval, constrained
   candidate judging, and durable follow-up questions — complete
6. Telegram confirmation, editing, cancellation, and complete reversal — complete
7. Invoice image extraction — complete for photos and JPEG/PNG/WebP documents
8. No-match catalog creation with conversational detail extraction — complete for simple
   tracking
9. Semantic and candidate-judge calibration on representative SME datasets
10. Voice-note transcription
11. Structured transaction retrieval for reliable reversal targeting:
    - exact `transaction_id` lookup with stored `transaction_type`, `status`, timestamp,
      summary, and derived reversal state — complete;
    - expose the already stored `created_by` and `confirmed_by` organization-user
      attribution to the agent, dashboard, and user-facing transaction details, including
      each person's display name, Telegram username when retained, and role at lookup
      time;
    - filter transactions by creator, confirmer, type, status, and date range;
    - add cursor-based pagination so the agent can traverse more than the current
      20-record result limit;
    - enforce role-based visibility, such as workers seeing their own transactions while
      managers can inspect the company-wide ledger;
    - support `occurred_after` and `occurred_before` timestamp filters with explicit
      timezone conversion;
    - retain type, item/SKU, and description filters for natural-language searches;
    - handle exact displayed times using a narrow time window and ask the user to choose
      when multiple transactions match; and
    - test transactions older than the current 20-record recent-history window.
12. Separate company SKUs from external catalog identifiers during item creation:
    - extend the agent and catalog-creation contracts to accept typed identifiers instead
      of mapping every supplied part number into `item_variants.sku`;
    - store manufacturer part numbers, supplier part numbers, and barcodes in
      `item_identifiers` under their actual identifier types;
    - scope supplier part numbers to their supplier and manufacturer part numbers to their
      manufacturer so coincidentally identical codes do not collide;
    - preserve a company-controlled SKU for each variant while allowing organization
      policy to require, derive, or automatically generate it when only an external
      identifier is provided;
    - let workers provide whichever real-world code appears on the product, delivery order,
      or invoice without needing to understand the identifier taxonomy; and
    - migrate or review catalog records whose current SKU was originally supplied as an
      external part number, with exact-matching and tenant-isolation tests.
13. Company member onboarding and authorization:
    - let an admin create a company-scoped, expiring, use-limited invitation in the
      dashboard; display the random plaintext code once and store only its cryptographic
      hash — complete;
    - accept `/register INVITE_CODE` only in a private bot chat, atomically consume an
      invite use, skip OpenAI and normal inventory event storage, retain the Telegram
      profile only while approval is pending, and show it in that company's admin
      dashboard — complete;
    - let an admin approve the applicant with a `worker`, `manager`, or `admin`
      role, then preserve the resulting membership and its role-change audit history —
      initial approval audit complete; later role editing remains;
    - on rejection, send the Telegram rejection notice first and then permanently delete
      the pending request, Telegram user ID, username, display name, and source-chat ID —
      complete;
    - when `/register INVITE_CODE` is sent in a group, continue refusing and avoiding
      persistence of the exposed code, but send a new Telegram reply explaining that
      registration must be completed in a private bot chat instead of failing silently —
      complete;
    - distinguish rejection from removing an approved member: approved memberships are
      deactivated rather than deleted because transactions and other audit records may
      reference them;
    - add company-selection handling for users who legitimately belong to more than one
      organization;
    - expose safe invite generation, approval, rejection, and role selection in the local
      admin dashboard — complete; authenticated production admin accounts remain; and
    - resolve the worker experience for unmatched products: the database correctly
      prevents a `worker` from creating catalog items, but Telegram can currently offer
      the new-item action and then fail without a useful explanation. Keep the policy
      decision open while evaluating these options:
      - hide or disable catalog-creation actions for workers and clearly ask them to
        contact a manager or admin;
      - create a durable manager-approval request and notify eligible approvers;
      - let workers prepare a pending catalog draft that a manager or admin must review
        before either the product or stock is created; or
      - make worker catalog creation an organization-level permission, with an appropriate
        approval and audit policy;
      regardless of the selected policy, never fail silently, never change inventory
      before authorization and confirmation, and provide workers with **Choose existing**
      and **Cancel** paths where applicable;
    - enforce role-specific read, proposal, confirmation, catalog, and reversal policies
      consistently in both application and database boundaries.
14. Component-level pipeline health and observability:
    - show the background worker separately from the overall system status, including
      whether it is running, its last successful cycle, last error, and queue backlog;
    - show health for each event processor—button callbacks, invoice images, text messages,
      registration notices, context compaction, and outbound Telegram delivery—with its
      last attempt, last success, last failure, recent latency, and oldest pending event;
    - report important dependency stages separately, including OpenAI extraction/agent
      calls, semantic embeddings, Supabase operations, and Telegram downloads/sends;
    - derive green, warning, and red states from recent successful work, failures, backlog
      age, and latency rather than treating process liveness as proof that the pipeline
      works end to end;
    - include concise hover help describing what each worker and processor does and what
      evidence determined its current status; and
    - retain recent sanitized failures and correlation IDs in the dashboard so a source
      event can be traced through extraction, matching, proposal creation, and delivery
      without exposing secrets or invoice contents unnecessarily.
