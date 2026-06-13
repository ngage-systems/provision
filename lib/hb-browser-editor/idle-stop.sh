#!/usr/bin/env bash
# Stop code-server when no browser editor requests for BROWSER_EDITOR_IDLE_SECONDS.
set -euo pipefail

USER="${BROWSER_EDITOR_USER:-lab}"
IDLE="${BROWSER_EDITOR_IDLE_SECONDS:-3600}"
STAMP="${BROWSER_EDITOR_STAMP:-/run/hb-browser-editor/last-request}"
SERVICE="code-server@${USER}.service"

if ! systemctl is-active --quiet "$SERVICE"; then
  exit 0
fi

if [[ ! -f "$STAMP" ]]; then
  exit 0
fi

now="$(date +%s)"
last="$(stat -c %Y "$STAMP")"
if (( now - last >= IDLE )); then
  systemctl stop "$SERVICE"
fi
