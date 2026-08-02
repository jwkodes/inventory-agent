#!/usr/bin/env bash

set -eo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="$project_root/.runtime"
log_dir="$runtime_dir/logs"
supervisor_pid_file="$runtime_dir/supervisor.pid"
ngrok_pid_file="$runtime_dir/ngrok.pid"
python_bin="$project_root/.venv/bin/python"

cd "$project_root"
mkdir -p "$log_dir"

for command_name in docker supabase uv ngrok curl jq; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name" >&2
    exit 1
  fi
done

if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example to .env and populate it first." >&2
  exit 1
fi

if [[ ! -x "$python_bin" ]]; then
  echo "Project environment is missing; running uv sync…"
  uv sync --locked
fi

# Load the same local values as the Python processes. Keep nounset disabled while
# dotenv-style variable references are expanded.
set -a
# shellcheck disable=SC1091
source .env
set +a
set -u

control_token="${DEV_SUPERVISOR_TOKEN:-${DEV_DASHBOARD_TOKEN:-}}"
supervisor_url="${DEV_SUPERVISOR_URL:-http://127.0.0.1:8765}"

if [[ -z "$control_token" ]]; then
  echo "Set DEV_SUPERVISOR_TOKEN or DEV_DASHBOARD_TOKEN in .env." >&2
  exit 1
fi

missing_worker_settings=()
for setting_name in SUPABASE_SECRET_KEY TELEGRAM_BOT_TOKEN OPENAI_API_KEY; do
  if [[ -z "${!setting_name:-}" ]]; then
    missing_worker_settings+=("$setting_name")
  fi
done
if (( ${#missing_worker_settings[@]} > 0 )); then
  echo "The full development stack cannot start until these .env values are set:" >&2
  for setting_name in "${missing_worker_settings[@]}"; do
    echo "  - $setting_name" >&2
  done
  echo "Run the API-only command from README.md if you do not need the worker yet." >&2
  exit 1
fi

if ! "$python_bin" -c "from inventory_agent.config import Settings; Settings()"; then
  echo "The values in .env are invalid. Correct the validation error above and try again." >&2
  exit 1
fi

pid_is_running() {
  local pid_file="$1"
  local pid
  [[ -f "$pid_file" ]] || return 1
  pid="$(tr -d '[:space:]' < "$pid_file")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

wait_for_url() {
  local url="$1"
  local attempts="$2"
  local auth_header="${3:-}"
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if [[ -n "$auth_header" ]]; then
      curl -fsS -H "$auth_header" "$url" >/dev/null 2>&1 && return 0
    else
      curl -fsS "$url" >/dev/null 2>&1 && return 0
    fi
    sleep 1
  done
  return 1
}

if ! docker info >/dev/null 2>&1; then
  if [[ "$(uname -s)" == "Darwin" ]] && [[ -d /Applications/Docker.app ]]; then
    echo "Starting Docker Desktop…"
    open -a Docker
    for _ in {1..60}; do
      docker info >/dev/null 2>&1 && break
      sleep 2
    done
  fi
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not ready. Start Docker Desktop and run this script again." >&2
  exit 1
fi

echo "Starting local Supabase…"
supabase start >/dev/null

if curl -fsS \
  -H "Authorization: Bearer $control_token" \
  "$supervisor_url/status" >/dev/null 2>&1; then
  echo "Development supervisor is already running."
elif pid_is_running "$supervisor_pid_file"; then
  echo "Supervisor process exists but its health endpoint is unavailable." >&2
  echo "See $log_dir/supervisor.log" >&2
  exit 1
else
  rm -f "$supervisor_pid_file"
  echo "Starting API and worker supervisor…"
  DEV_SUPERVISOR_ENABLED=true \
    nohup "$python_bin" -m inventory_agent.dev_supervisor \
    >>"$log_dir/supervisor.log" 2>&1 &
  supervisor_pid="$!"
  echo "$supervisor_pid" >"$supervisor_pid_file"
  if ! wait_for_url \
    "$supervisor_url/status" \
    20 \
    "Authorization: Bearer $control_token"; then
    echo "Supervisor failed to become healthy. Recent log output:" >&2
    tail -n 30 "$log_dir/supervisor.log" >&2
    exit 1
  fi
fi

if ! wait_for_url "http://127.0.0.1:8000/health" 20; then
  echo "The API failed to become healthy. See $log_dir/supervisor.log" >&2
  exit 1
fi

existing_tunnel="$(
  curl -fsS http://127.0.0.1:4040/api/tunnels 2>/dev/null \
    | jq -r '.tunnels[]? | select(.config.addr | endswith(":8000")) | .public_url' \
    | head -n 1 \
    || true
)"
if [[ -n "$existing_tunnel" ]]; then
  echo "ngrok tunnel is already running: $existing_tunnel"
elif pid_is_running "$ngrok_pid_file"; then
  echo "ngrok process exists but its local API is unavailable." >&2
  echo "See $log_dir/ngrok.log" >&2
  exit 1
else
  rm -f "$ngrok_pid_file"
  echo "Starting ngrok tunnel…"
  nohup ngrok http 8000 --log=stdout >>"$log_dir/ngrok.log" 2>&1 &
  ngrok_pid="$!"
  echo "$ngrok_pid" >"$ngrok_pid_file"
  if ! wait_for_url "http://127.0.0.1:4040/api/tunnels" 20; then
    echo "ngrok failed to become healthy. Recent log output:" >&2
    tail -n 30 "$log_dir/ngrok.log" >&2
    exit 1
  fi
  existing_tunnel="$(
    curl -fsS http://127.0.0.1:4040/api/tunnels \
      | jq -r '.tunnels[]? | select(.config.addr | endswith(":8000")) | .public_url' \
      | head -n 1 \
      || true
  )"
fi

echo
echo "Inventory Agent is running."
echo "Dashboard:  http://127.0.0.1:8000/dev"
echo "API health: http://127.0.0.1:8000/health"
[[ -n "$existing_tunnel" ]] && echo "Public URL: $existing_tunnel"
echo "Logs:       $log_dir"
