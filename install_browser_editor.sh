#!/usr/bin/env bash
#
# install_browser_editor.sh
# -------------------------------------------------------------------
# Installs a browser-based VS Code editor with GitHub Copilot Chat
# (BYOK-ready) on a Raspberry Pi / Debian system.
#
# What it does:
#   1. Installs code-server (browser VS Code, ARM64-compatible)
#   2. Creates ~/systems/, pulls agentic-coding docs, writes copilot-instructions.md
#   3. Generates a self-signed TLS cert so crypto.subtle works
#      (required for Copilot Chat webview)
#   4. Configures code-server: HTTPS, 0.0.0.0:8080, auth:password, login i18n
#   5. Creates a systemd drop-in to open ~/systems at startup
#   6. Enables and starts the code-server service
#
# Usage:
#   sudo ./install_browser_editor.sh
#
# After install:
#   https://<pi-ip>:8080   — accept cert warning, then enter login password
#
# -------------------------------------------------------------------

set -euo pipefail

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

script_path="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"
script_dir="$(cd "$(dirname "$script_path")" && pwd -P)"
lib="${script_dir}/lib/browser_editor_lib.sh"
[[ -r "$lib" ]] || die "Missing shared library: $lib"
# shellcheck source=lib/browser_editor_lib.sh
source "$lib"

prompt_code_server_password() {
  local pass pass2
  [[ -r /dev/tty ]] || die "No TTY available — run interactively: sudo $0"

  log "Set the code-server login password (often same as your SSH password)"
  while true; do
    printf 'Enter password: ' >/dev/tty
    read -rs pass </dev/tty
    printf '\n' >/dev/tty
    [[ -n "$pass" ]] || { warn "Password cannot be empty."; continue; }

    printf 'Confirm password: ' >/dev/tty
    read -rs pass2 </dev/tty
    printf '\n' >/dev/tty
    [[ "$pass" = "$pass2" ]] || { warn "Passwords do not match."; continue; }

    CODE_SERVER_PASSWORD="$pass"
    break
  done
}

[[ "$(id -u)" -eq 0 ]] || die "Please run with sudo: sudo $0"
TARGET_USER="${SUDO_USER:-}"
[[ -n "$TARGET_USER" ]] || die "Run via sudo as a normal user, not as root directly. Try: sudo $0"

log "Target user : $TARGET_USER"
prompt_code_server_password
install_browser_editor "" "$TARGET_USER" "$CODE_SERVER_PASSWORD" ""
unset CODE_SERVER_PASSWORD
