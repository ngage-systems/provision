#!/usr/bin/env bash
# Shared browser editor (code-server) install helpers. Source from provision scripts.

: "${BROWSER_EDITOR_CODE_BIND:=0.0.0.0:8080}"
: "${BROWSER_EDITOR_WORKSPACE_SUBPATH:=systems}"
: "${BROWSER_EDITOR_DOCS_REPO:=https://github.com/ngage-systems/trainer_docs/archive/refs/heads/main.tar.gz}"
: "${BROWSER_EDITOR_DOCS_STRIP:=trainer_docs-main/docs/agentic-coding}"
: "${BROWSER_EDITOR_CERT_DAYS:=3650}"

if ! declare -F log >/dev/null 2>&1; then
  log() { echo "$*" >&2; }
fi

if ! declare -F die >/dev/null 2>&1; then
  die() { log "ERROR: $*"; exit 1; }
fi

browser_editor_yaml_single_quote() {
  printf "'%s'" "${1//\'/\'\'}"
}

browser_editor_resolve_user() {
  local root_mnt="$1"
  local target_user="$2"
  local passwd_file home uid gid

  if [[ -n "$root_mnt" ]]; then
    passwd_file="${root_mnt}/etc/passwd"
    [[ -r "$passwd_file" ]] || die "Cannot read ${passwd_file}"
    home="$(awk -F: -v u="$target_user" '$1 == u { print $6; exit }' "$passwd_file")"
    uid="$(awk -F: -v u="$target_user" '$1 == u { print $3; exit }' "$passwd_file")"
    gid="$(awk -F: -v u="$target_user" '$1 == u { print $4; exit }' "$passwd_file")"
  else
    home="$(getent passwd "$target_user" | cut -d: -f6)"
    uid="$(getent passwd "$target_user" | cut -d: -f3)"
    gid="$(getent passwd "$target_user" | cut -d: -f4)"
  fi

  [[ -n "$home" && -n "$uid" && -n "$gid" ]] \
    || die "Could not resolve home/uid/gid for user '$target_user'"
  printf '%s\n' "$home" "$uid" "$gid"
}

browser_editor_path_prefix() {
  local root_mnt="$1"
  local path="$2"
  if [[ -n "$root_mnt" ]]; then
    printf '%s%s' "$root_mnt" "$path"
  else
    printf '%s' "$path"
  fi
}

browser_editor_install_code_server() {
  local root_mnt="$1"
  local code_server_bin

  code_server_bin="$(browser_editor_path_prefix "$root_mnt" "/usr/bin/code-server")"
  if [[ -x "$code_server_bin" ]]; then
    log "code-server already installed in target rootfs."
    return 0
  fi

  log "Installing code-server..."
  if [[ -n "$root_mnt" ]]; then
    local chroot_env=(/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root DEBIAN_FRONTEND=noninteractive)
    chroot "$root_mnt" "${chroot_env[@]}" /bin/bash -c \
      'curl -fsSL https://code-server.dev/install.sh | sh' \
      || die "Failed to install code-server in target rootfs."
  else
    curl -fsSL https://code-server.dev/install.sh | sh \
      || die "Failed to install code-server."
  fi

  [[ -x "$code_server_bin" ]] || die "code-server binary not found after install: $code_server_bin"
}

browser_editor_download_docs() {
  local workspace="$1"
  local target_user="$2"
  local target_uid="$3"
  local target_gid="$4"
  local root_mnt="$5"

  if [[ -d "$workspace/agentic-coding" ]]; then
    log "agentic-coding already exists — skipping download."
    return 0
  fi

  log "Pulling agentic-coding docs into ${workspace}/agentic-coding ..."
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  if ! (
    set -euo pipefail
    if [[ -n "$root_mnt" ]]; then
      wget -qO- "$BROWSER_EDITOR_DOCS_REPO" \
        | tar -xz -C "$tmp_dir" --strip-components=2 "$BROWSER_EDITOR_DOCS_STRIP"
    else
      sudo -u "$target_user" wget -qO- "$BROWSER_EDITOR_DOCS_REPO" \
        | sudo -u "$target_user" tar -xz -C "$tmp_dir" --strip-components=2 "$BROWSER_EDITOR_DOCS_STRIP"
    fi
    if [[ -d "$tmp_dir/agentic-coding" ]]; then
      mv "$tmp_dir/agentic-coding" "$workspace/agentic-coding"
    else
      mv "$tmp_dir"/* "$workspace/" 2>/dev/null || true
    fi
  ); then
    rm -rf "$tmp_dir"
    log "WARNING: Failed to download agentic-coding docs; continuing without them."
    return 0
  fi
  rm -rf "$tmp_dir"

  if [[ -d "$workspace/agentic-coding" ]]; then
    chown -R "${target_uid}:${target_gid}" "$workspace/agentic-coding" 2>/dev/null || true
    log "Agentic-coding docs installed."
  fi
}

browser_editor_write_copilot_instructions() {
  local workspace="$1"
  local target_uid="$2"
  local target_gid="$3"
  local github_dir instructions_file

  github_dir="${workspace}/.github"
  instructions_file="${github_dir}/copilot-instructions.md"
  install -d -o "$target_uid" -g "$target_gid" "$github_dir"
  cat >"$instructions_file" <<'EOF'
Before responding, read all files in the `agentic-coding/` folder of this workspace. This folder contains documentation about the system you are helping to modify.
EOF
  chown "${target_uid}:${target_gid}" "$instructions_file"
}

browser_editor_cert_san() {
  local cert_cn="$1"
  if [[ "$cert_cn" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf 'IP:%s,IP:127.0.0.1' "$cert_cn"
  else
    printf 'DNS:%s,IP:127.0.0.1' "$cert_cn"
  fi
}

browser_editor_generate_tls_cert() {
  local cert_dir="$1"
  local cert_cn="$2"
  local target_uid="$3"
  local target_gid="$4"
  local san key_file crt_file

  [[ -n "$cert_cn" ]] || die "TLS certificate CN is required."
  san="$(browser_editor_cert_san "$cert_cn")"
  key_file="${cert_dir}/self.key"
  crt_file="${cert_dir}/self.crt"

  log "Generating self-signed TLS certificate (${BROWSER_EDITOR_CERT_DAYS}-day, CN=${cert_cn})..."
  install -d -o "$target_uid" -g "$target_gid" "$cert_dir"
  openssl req -x509 -nodes \
    -newkey rsa:2048 \
    -days "$BROWSER_EDITOR_CERT_DAYS" \
    -keyout "$key_file" \
    -out "$crt_file" \
    -subj "/CN=${cert_cn}" \
    -addext "subjectAltName=${san}" \
    2>/dev/null \
    || die "Failed to generate TLS certificate."
  chown -R "${target_uid}:${target_gid}" "$cert_dir"
  chmod 600 "$key_file"
}

browser_editor_write_config() {
  local conf_dir="$1"
  local cert_dir="$2"
  local workspace="$3"
  local password="$4"
  local target_uid="$5"
  local target_gid="$6"
  local i18n_file config_file

  log "Writing code-server config..."
  install -d -o "$target_uid" -g "$target_gid" "$conf_dir"

  i18n_file="${conf_dir}/login-i18n.json"
  cat >"$i18n_file" <<'EOF'
{
  "LOGIN_PASSWORD": "Enter the password set when provisioning this system. It is the same as the password used for SSH access."
}
EOF
  chown "${target_uid}:${target_gid}" "$i18n_file"
  chmod 644 "$i18n_file"

  config_file="${conf_dir}/config.yaml"
  cat >"$config_file" <<EOF
# code-server configuration
# Managed by browser_editor_lib.sh — edit carefully
bind-addr: ${BROWSER_EDITOR_CODE_BIND}
auth: password
password: $(browser_editor_yaml_single_quote "$password")
i18n: ${i18n_file}
cert: ${cert_dir}/self.crt
cert-key: ${cert_dir}/self.key
EOF
  chown "${target_uid}:${target_gid}" "$config_file"
  chmod 600 "$config_file"
}

browser_editor_configure_systemd() {
  local root_mnt="$1"
  local target_user="$2"
  local workspace="$3"
  local dropin_dir override_file service_name

  service_name="code-server@${target_user}.service"
  dropin_dir="$(browser_editor_path_prefix "$root_mnt" "/etc/systemd/system/${service_name}.d")"
  override_file="${dropin_dir}/override.conf"

  log "Configuring systemd service to open ${workspace}..."
  install -d "$dropin_dir"
  cat >"$override_file" <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/code-server ${workspace}
EOF

  if [[ -n "$root_mnt" ]]; then
    SYSTEMD_OFFLINE=1 systemctl --root "$root_mnt" enable "$service_name" \
      || log "WARNING: Failed to enable ${service_name} in target rootfs."
    return 0
  fi

  systemctl daemon-reload
  systemctl enable "$service_name"
  systemctl restart "$service_name"
  log "code-server service enabled and restarted."

  sleep 2
  if command -v ss >/dev/null 2>&1 && ss -tlnp | grep -q ':8080'; then
    log "code-server is listening on :8080."
  else
    log "WARNING: code-server does not appear to be listening on :8080 yet."
  fi
}

# install_browser_editor ROOT_MNT TARGET_USER PASSWORD CERT_CN
# ROOT_MNT empty = live system; otherwise path to mounted target rootfs (chroot env must be mounted).
install_browser_editor() {
  local root_mnt="${1:-}"
  local target_user="$2"
  local password="$3"
  local cert_cn="${4:-}"
  local resolved home target_uid target_gid workspace conf_dir cert_dir

  [[ -n "$target_user" ]] || die "install_browser_editor: target user is required."
  [[ -n "$password" ]] || die "install_browser_editor: password is required."

  if [[ -z "$cert_cn" && -z "$root_mnt" ]]; then
    cert_cn="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{print $7; exit}' || true)"
    cert_cn="${cert_cn:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
  fi
  [[ -n "$cert_cn" ]] || die "install_browser_editor: certificate CN is required."

  mapfile -t resolved < <(browser_editor_resolve_user "$root_mnt" "$target_user")
  home="${resolved[0]}"
  target_uid="${resolved[1]}"
  target_gid="${resolved[2]}"

  workspace="$(browser_editor_path_prefix "$root_mnt" "${home}/${BROWSER_EDITOR_WORKSPACE_SUBPATH}")"
  conf_dir="$(browser_editor_path_prefix "$root_mnt" "${home}/.config/code-server")"
  cert_dir="${conf_dir}/tls"

  browser_editor_install_code_server "$root_mnt"

  log "Ensuring workspace exists: ${workspace}"
  install -d -o "$target_uid" -g "$target_gid" "$workspace"
  browser_editor_download_docs "$workspace" "$target_user" "$target_uid" "$target_gid" "$root_mnt"
  browser_editor_write_copilot_instructions "$workspace" "$target_uid" "$target_gid"
  browser_editor_generate_tls_cert "$cert_dir" "$cert_cn" "$target_uid" "$target_gid"
  browser_editor_write_config "$conf_dir" "$cert_dir" "$workspace" "$password" "$target_uid" "$target_gid"
  browser_editor_configure_systemd "$root_mnt" "$target_user" "$workspace"
}
