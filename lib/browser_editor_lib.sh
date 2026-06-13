#!/usr/bin/env bash
# Shared browser editor (code-server) install helpers. Source from provision scripts.

: "${BROWSER_EDITOR_PUBLIC_PORT:=8080}"
: "${BROWSER_EDITOR_UPSTREAM_BIND:=127.0.0.1:8081}"
: "${BROWSER_EDITOR_WAKE_BIND:=127.0.0.1:9082}"
: "${BROWSER_EDITOR_IDLE_SECONDS:=3600}"
: "${BROWSER_EDITOR_TLS_DIR:=/etc/hb-browser-editor/tls}"
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

browser_editor_lib_dir() {
  local lib_path
  lib_path="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || realpath "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
  dirname "$lib_path"
}

browser_editor_asset_dir() {
  echo "$(browser_editor_lib_dir)/hb-browser-editor"
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

browser_editor_systemctl() {
  local root_mnt="$1"
  shift
  if [[ -n "$root_mnt" ]]; then
    SYSTEMD_OFFLINE=1 systemctl --root "$root_mnt" "$@"
  else
    systemctl "$@"
  fi
}

browser_editor_target_has_caddy_group() {
  local root_mnt="$1"
  if [[ -n "$root_mnt" ]]; then
    grep -q '^caddy:' "${root_mnt}/etc/group" 2>/dev/null
  else
    getent group caddy >/dev/null 2>&1
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

browser_editor_install_caddy() {
  local root_mnt="$1"

  if [[ -n "$root_mnt" ]]; then
    if [[ -x "${root_mnt}/usr/bin/caddy" ]]; then
      log "caddy already installed in target rootfs."
      return 0
    fi
    local chroot_env=(/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root DEBIAN_FRONTEND=noninteractive)
    log "Installing caddy in target rootfs..."
    chroot "$root_mnt" "${chroot_env[@]}" /usr/bin/apt-get update --error-on=any \
      || die "Failed to apt-get update before caddy install."
    chroot "$root_mnt" "${chroot_env[@]}" /usr/bin/apt-get install -y --no-install-recommends caddy \
      || die "Failed to install caddy in target rootfs."
  else
    if command -v caddy >/dev/null 2>&1; then
      log "caddy already installed."
      return 0
    fi
    log "Installing caddy..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update --error-on=any || die "Failed to apt-get update before caddy install."
    apt-get install -y --no-install-recommends caddy || die "Failed to install caddy."
  fi
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
  local root_mnt="$1"
  local cert_cn="$2"
  local cert_dir_mnt san key_file crt_file

  [[ -n "$cert_cn" ]] || die "TLS certificate CN is required."
  cert_dir_mnt="$(browser_editor_path_prefix "$root_mnt" "$BROWSER_EDITOR_TLS_DIR")"
  san="$(browser_editor_cert_san "$cert_cn")"
  key_file="${cert_dir_mnt}/self.key"
  crt_file="${cert_dir_mnt}/self.crt"

  log "Generating self-signed TLS certificate (${BROWSER_EDITOR_CERT_DAYS}-day, CN=${cert_cn})..."
  install -d -m 0755 "$cert_dir_mnt"
  openssl req -x509 -nodes \
    -newkey rsa:2048 \
    -days "$BROWSER_EDITOR_CERT_DAYS" \
    -keyout "$key_file" \
    -out "$crt_file" \
    -subj "/CN=${cert_cn}" \
    -addext "subjectAltName=${san}" \
    2>/dev/null \
    || die "Failed to generate TLS certificate."

  if browser_editor_target_has_caddy_group "$root_mnt"; then
    # When provisioning a mounted rootfs, group names resolve against the host
    # /etc/group — use the target caddy GID so chown works before first boot.
    if [[ -n "$root_mnt" ]]; then
      local caddy_gid
      caddy_gid="$(awk -F: '$1 == "caddy" { print $3; exit }' "${root_mnt}/etc/group")"
      [[ -n "$caddy_gid" ]] || die "caddy group missing from ${root_mnt}/etc/group"
      chown "0:${caddy_gid}" "$key_file" "$crt_file"
    else
      chown root:caddy "$key_file" "$crt_file"
    fi
    chmod 640 "$key_file"
    chmod 644 "$crt_file"
  else
    chmod 600 "$key_file"
    chmod 644 "$crt_file"
  fi
}

browser_editor_write_config() {
  local conf_dir_mnt="$1"
  local conf_dir="$2"
  local password="$3"
  local target_uid="$4"
  local target_gid="$5"
  local i18n_file_mnt config_file_mnt

  log "Writing code-server config..."
  install -d -o "$target_uid" -g "$target_gid" "$conf_dir_mnt"

  i18n_file_mnt="${conf_dir_mnt}/login-i18n.json"
  cat >"$i18n_file_mnt" <<'EOF'
{
  "LOGIN_PASSWORD": "Enter the password set when provisioning this system. It is the same as the password used for SSH access."
}
EOF
  chown "${target_uid}:${target_gid}" "$i18n_file_mnt"
  chmod 644 "$i18n_file_mnt"

  config_file_mnt="${conf_dir_mnt}/config.yaml"
  cat >"$config_file_mnt" <<EOF
# code-server configuration
# Managed by browser_editor_lib.sh — edit carefully
bind-addr: ${BROWSER_EDITOR_UPSTREAM_BIND}
auth: password
password: $(browser_editor_yaml_single_quote "$password")
i18n: ${conf_dir}/login-i18n.json
EOF
  chown "${target_uid}:${target_gid}" "$config_file_mnt"
  chmod 600 "$config_file_mnt"
}

browser_editor_configure_code_server_dropin() {
  local root_mnt="$1"
  local target_user="$2"
  local workspace="$3"
  local dropin_dir override_file service_name

  service_name="code-server@${target_user}.service"
  dropin_dir="$(browser_editor_path_prefix "$root_mnt" "/etc/systemd/system/${service_name}.d")"
  override_file="${dropin_dir}/override.conf"

  log "Configuring code-server drop-in (workspace ${workspace}; not started at boot)..."
  install -d "$dropin_dir"
  cat >"$override_file" <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/code-server ${workspace}
EOF

  browser_editor_systemctl "$root_mnt" disable "$service_name" \
    || log "WARNING: Failed to disable ${service_name} at boot."
  browser_editor_systemctl "$root_mnt" stop "$service_name" 2>/dev/null || true
}

browser_editor_install_assets() {
  local root_mnt="$1"
  local asset_dir dest_dir

  asset_dir="$(browser_editor_asset_dir)"
  [[ -d "$asset_dir" ]] || die "Missing browser editor assets: $asset_dir"

  dest_dir="$(browser_editor_path_prefix "$root_mnt" "/usr/local/lib/hb-browser-editor")"
  install -d -m 0755 "$dest_dir"
  install -m 0755 "${asset_dir}/wake-server.py" "${dest_dir}/wake-server.py"
  install -m 0755 "${asset_dir}/idle-stop.sh" "${dest_dir}/idle-stop.sh"
}

browser_editor_write_unit_from_template() {
  local template="$1"
  local dest_mnt="$2"
  local target_user="$3"

  sed "s/__BROWSER_EDITOR_USER__/${target_user}/g" "$template" >"$dest_mnt"
}

browser_editor_install_proxy_stack() {
  local root_mnt="$1"
  local target_user="$2"
  local asset_dir etc_dir caddyfile_mnt caddy_dropin wake_unit idle_unit idle_timer
  local wake_unit_mnt idle_unit_mnt idle_timer_mnt caddy_dropin_mnt

  asset_dir="$(browser_editor_asset_dir)"
  etc_dir="$(browser_editor_path_prefix "$root_mnt" "/etc/hb-browser-editor")"
  caddyfile_mnt="${etc_dir}/Caddyfile"
  caddy_dropin_mnt="$(browser_editor_path_prefix "$root_mnt" "/etc/systemd/system/caddy.service.d/override.conf")"
  wake_unit_mnt="$(browser_editor_path_prefix "$root_mnt" "/etc/systemd/system/hb-browser-editor-wake.service")"
  idle_unit_mnt="$(browser_editor_path_prefix "$root_mnt" "/etc/systemd/system/hb-browser-editor-idle.service")"
  idle_timer_mnt="$(browser_editor_path_prefix "$root_mnt" "/etc/systemd/system/hb-browser-editor-idle.timer")"

  log "Installing lazy browser editor proxy stack (Caddy + wake + idle timer)..."
  browser_editor_install_assets "$root_mnt"

  install -d -m 0755 "$etc_dir"
  sed \
    -e "s|:__PUBLIC_PORT__|:${BROWSER_EDITOR_PUBLIC_PORT}|g" \
    -e "s|__TLS_DIR__|${BROWSER_EDITOR_TLS_DIR}|g" \
    -e "s|__WAKE_ADDR__|${BROWSER_EDITOR_WAKE_BIND}|g" \
    -e "s|__UPSTREAM__|${BROWSER_EDITOR_UPSTREAM_BIND}|g" \
    "${asset_dir}/Caddyfile.template" >"$caddyfile_mnt"

  install -d "$(dirname "$caddy_dropin_mnt")"
  cat >"$caddy_dropin_mnt" <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/caddy run --environ --config /etc/hb-browser-editor/Caddyfile
EOF

  browser_editor_write_unit_from_template \
    "${asset_dir}/hb-browser-editor-wake.service" "$wake_unit_mnt" "$target_user"
  browser_editor_write_unit_from_template \
    "${asset_dir}/hb-browser-editor-idle.service" "$idle_unit_mnt" "$target_user"
  install -m 0644 "${asset_dir}/hb-browser-editor-idle.timer" "$idle_timer_mnt"

  browser_editor_systemctl "$root_mnt" enable caddy.service \
    || log "WARNING: Failed to enable caddy.service."
  browser_editor_systemctl "$root_mnt" enable hb-browser-editor-wake.service \
    || log "WARNING: Failed to enable hb-browser-editor-wake.service."
  browser_editor_systemctl "$root_mnt" enable hb-browser-editor-idle.timer \
    || log "WARNING: Failed to enable hb-browser-editor-idle.timer."
}

browser_editor_activate_proxy_stack() {
  local root_mnt="$1"

  [[ -n "$root_mnt" ]] && return 0

  browser_editor_systemctl "" daemon-reload
  browser_editor_systemctl "" restart hb-browser-editor-wake.service \
    || log "WARNING: Failed to restart hb-browser-editor-wake.service."
  browser_editor_systemctl "" restart caddy.service \
    || log "WARNING: Failed to restart caddy.service."
  browser_editor_systemctl "" start hb-browser-editor-idle.timer \
    || log "WARNING: Failed to start hb-browser-editor-idle.timer."

  sleep 2
  if command -v ss >/dev/null 2>&1 && ss -tlnp | grep -q ":${BROWSER_EDITOR_PUBLIC_PORT} "; then
    log "Caddy is listening on :${BROWSER_EDITOR_PUBLIC_PORT} (code-server starts on first request)."
  else
    log "WARNING: Caddy does not appear to be listening on :${BROWSER_EDITOR_PUBLIC_PORT} yet."
  fi
}

# install_browser_editor ROOT_MNT TARGET_USER PASSWORD CERT_CN
# ROOT_MNT empty = live system; otherwise path to mounted target rootfs (chroot env must be mounted).
install_browser_editor() {
  local root_mnt="${1:-}"
  local target_user="$2"
  local password="$3"
  local cert_cn="${4:-}"
  local resolved home target_uid target_gid
  local workspace conf_dir
  local workspace_mnt conf_dir_mnt

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

  workspace="${home}/${BROWSER_EDITOR_WORKSPACE_SUBPATH}"
  conf_dir="${home}/.config/code-server"
  workspace_mnt="$(browser_editor_path_prefix "$root_mnt" "$workspace")"
  conf_dir_mnt="$(browser_editor_path_prefix "$root_mnt" "$conf_dir")"

  browser_editor_install_code_server "$root_mnt"
  browser_editor_install_caddy "$root_mnt"

  log "Ensuring workspace exists: ${workspace}"
  install -d -o "$target_uid" -g "$target_gid" "$workspace_mnt"
  browser_editor_download_docs "$workspace_mnt" "$target_user" "$target_uid" "$target_gid" "$root_mnt"
  browser_editor_write_copilot_instructions "$workspace_mnt" "$target_uid" "$target_gid"
  browser_editor_generate_tls_cert "$root_mnt" "$cert_cn"
  browser_editor_write_config "$conf_dir_mnt" "$conf_dir" "$password" "$target_uid" "$target_gid"
  browser_editor_configure_code_server_dropin "$root_mnt" "$target_user" "$workspace"
  browser_editor_install_proxy_stack "$root_mnt" "$target_user"
  browser_editor_activate_proxy_stack "$root_mnt"
}
