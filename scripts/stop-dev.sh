#!/usr/bin/env bash

set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="$project_root/.runtime"
supervisor_pid_file="$runtime_dir/supervisor.pid"
ngrok_pid_file="$runtime_dir/ngrok.pid"

cd "$project_root"

stop_managed_process() {
  local label="$1"
  local pid_file="$2"
  local expected_command="$3"
  local pid
  local actual_command
  local attempt

  if [[ ! -f "$pid_file" ]]; then
    echo "$label was not started by scripts/start-dev.sh."
    return
  fi

  pid="$(tr -d '[:space:]' < "$pid_file")"
  if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
    echo "Ignoring invalid $label PID file: $pid_file" >&2
    rm -f "$pid_file"
    return
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "$label is already stopped."
    rm -f "$pid_file"
    return
  fi

  actual_command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ "$actual_command" != *"$expected_command"* ]]; then
    echo "Refusing to stop PID $pid: it is not the recorded $label process." >&2
    echo "Actual command: $actual_command" >&2
    return
  fi

  echo "Stopping ${label}…"
  kill "$pid"
  for ((attempt = 1; attempt <= 15; attempt++)); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "$label did not stop gracefully; terminating PID $pid." >&2
    kill -KILL "$pid"
  fi
  rm -f "$pid_file"
}

# Stop public ingress first, then let the supervisor shut down its API and worker children.
stop_managed_process "ngrok" "$ngrok_pid_file" "ngrok http 8000"
stop_managed_process \
  "development supervisor" \
  "$supervisor_pid_file" \
  "inventory_agent.dev_supervisor"

if command -v supabase >/dev/null 2>&1; then
  echo "Stopping local Supabase…"
  supabase stop
else
  echo "Supabase CLI is unavailable; local Supabase was not stopped." >&2
fi

echo "Inventory Agent development services are stopped."
