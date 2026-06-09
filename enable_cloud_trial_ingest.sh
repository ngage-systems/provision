#!/usr/bin/env bash
set -euo pipefail

# Enable cloud trial ingest (remote logging) on an already-provisioned trainer device.
# Run on the live Pi as root, on whatever drive is currently booted.

LOG_PREFIX=""
HB_OTHER_ROOT_MNT="/mnt/hb_other_root"
HB_TRIAL_INGEST_SECRET="/etc/dserv/trial_ingest_secret"

DEFAULTS_GROUP="${DEVICE_DEFAULTS_GROUP:-}"
DEFAULTS_FILE=""
HOSTNAME_OVERRIDE=""
NO_RESTART=0
OTHER_ROOT_MOUNTED=0

die() {
  if [[ -n "$LOG_PREFIX" ]]; then
    echo "$LOG_PREFIX ERROR: $*" >&2
  else
    echo "ERROR: $*" >&2
  fi
  exit 1
}

log() {
  if [[ -n "$LOG_PREFIX" ]]; then
    echo "$LOG_PREFIX $*" >&2
  else
    echo "$*" >&2
  fi
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

require_root() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "Run as root (sudo)."
}

resolve_defaults_file() {
  local script_path script_dir
  script_path="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"
  script_dir="$(cd "$(dirname "$script_path")" && pwd -P)"
  echo "${DEVICE_DEFAULTS_FILE:-${script_dir}/device_defaults.ini}"
}

cleanup_other_root() {
  if [[ "$OTHER_ROOT_MOUNTED" -eq 1 ]]; then
    trial_ingest_unmount_other_root "$HB_OTHER_ROOT_MNT"
    OTHER_ROOT_MOUNTED=0
  fi
}

usage() {
  cat <<EOF
Usage: sudo $0 [OPTIONS]

Enable cloud trial ingest (remote logging) on an already-provisioned device.

Options:
  --defaults-group GROUP   Defaults group from device_defaults.ini (or DEVICE_DEFAULTS_GROUP)
  --defaults-file PATH     Path to device_defaults.ini (default: beside this script)
  --hostname NAME          Hostname for cloud registry user (default: /etc/hostname)
  --no-restart             Skip systemctl restart dserv
  -h, --help               Show this help

If /etc/dserv/trial_ingest_secret already exists, this script exits without changes.
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --defaults-group)
        [[ $# -ge 2 ]] || die "--defaults-group requires a value"
        DEFAULTS_GROUP="$2"
        shift 2
        ;;
      --defaults-group=*)
        DEFAULTS_GROUP="${1#--defaults-group=}"
        shift
        ;;
      --defaults-file)
        [[ $# -ge 2 ]] || die "--defaults-file requires a path"
        DEFAULTS_FILE="$2"
        shift 2
        ;;
      --defaults-file=*)
        DEFAULTS_FILE="${1#--defaults-file=}"
        shift
        ;;
      --hostname)
        [[ $# -ge 2 ]] || die "--hostname requires a value"
        HOSTNAME_OVERRIDE="$2"
        shift 2
        ;;
      --hostname=*)
        HOSTNAME_OVERRIDE="${1#--hostname=}"
        shift
        ;;
      --no-restart)
        NO_RESTART=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
  done
}

prompt_defaults_group() {
  local defaults_file="$1"
  local groups group_choice
  groups="$(trial_ingest_ini_list_groups "$defaults_file")"

  [[ -n "$groups" ]] || die "No defaults groups found in ${defaults_file}."

  log "Available defaults groups:"
  mapfile -t _groups_list < <(printf '%s\n' "$groups" | sed '/^$/d')
  local i
  for i in "${!_groups_list[@]}"; do
    printf '  [%d] %s\n' "$i" "${_groups_list[$i]}" >&2
  done
  read -r -p "Select defaults group by number or name: " group_choice
  [[ -n "$group_choice" ]] || die "Defaults group is required."

  if [[ "$group_choice" =~ ^[0-9]+$ ]] && [[ "$group_choice" -ge 0 && "$group_choice" -lt "${#_groups_list[@]}" ]]; then
    DEFAULTS_GROUP="${_groups_list[$group_choice]}"
  else
    DEFAULTS_GROUP="$group_choice"
  fi
}

resolve_hostname() {
  if [[ -n "$HOSTNAME_OVERRIDE" ]]; then
    echo "$HOSTNAME_OVERRIDE"
    return 0
  fi
  if [[ -f /etc/hostname ]]; then
    tr -d '\r\n' </etc/hostname
    return 0
  fi
  die "Could not determine hostname; use --hostname."
}

source_lib() {
  local script_path script_dir lib
  script_path="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"
  script_dir="$(cd "$(dirname "$script_path")" && pwd -P)"
  lib="${script_dir}/trial_ingest_lib.sh"
  [[ -r "$lib" ]] || die "Missing shared library: $lib"
  # shellcheck source=trial_ingest_lib.sh
  source "$lib"
}

main() {
  parse_args "$@"
  require_root
  source_lib

  need_cmd openssl
  need_cmd mount
  need_cmd umount
  need_cmd install

  trap cleanup_other_root EXIT

  if read_trial_ingest_secret "" >/dev/null 2>&1; then
    log "Trial ingest secret already present at ${HB_TRIAL_INGEST_SECRET}; nothing to do."
    exit 0
  fi

  [[ -d /usr/local/dserv/local ]] || die "dserv does not appear to be installed (/usr/local/dserv/local missing)."

  [[ -n "$DEFAULTS_FILE" ]] || DEFAULTS_FILE="$(resolve_defaults_file)"
  [[ -r "$DEFAULTS_FILE" ]] || die "Defaults file not readable: ${DEFAULTS_FILE}"

  if [[ -z "$DEFAULTS_GROUP" ]]; then
    prompt_defaults_group "$DEFAULTS_FILE"
  fi

  if ! cloud_data_store_enabled_for_group "$DEFAULTS_GROUP" "$DEFAULTS_FILE"; then
    log "WARNING: cloud_data_store is not enabled for group ${DEFAULTS_GROUP} in ${DEFAULTS_FILE}."
  fi

  local hostname secret="" secret_source="" other_dev="" other_mnt="" register=0
  hostname="$(resolve_hostname)"

  local root_src root_dev
  root_src="$(trial_ingest_root_source)"
  root_dev="$(trial_ingest_strip_partition_suffix "$root_src")"
  log "Current root: ${root_src} (${root_dev})"

  if other_dev="$(trial_ingest_pick_other_boot_disk "$root_dev" 2>/dev/null || true)"; then
    log "Other boot disk detected: ${other_dev}"
    if other_mnt="$(trial_ingest_mount_other_root "$other_dev" "$HB_OTHER_ROOT_MNT" ro)"; then
      OTHER_ROOT_MOUNTED=1
      if secret="$(read_trial_ingest_secret "$other_mnt" 2>/dev/null)"; then
        log "Found trial ingest secret on other drive; copying to live system."
        write_trial_ingest_secret "$secret" ""
        secret_source="other_drive"
      fi
    else
      log "WARNING: Could not mount root partition on ${other_dev}; skipping other-drive secret check."
    fi
  else
    log "No other boot disk detected."
  fi

  if [[ -z "$secret" ]]; then
    need_cmd openssl
    secret="$(openssl rand -hex 8)"
    log "Generated new trial ingest secret."
    write_trial_ingest_secret "$secret" ""
    secret_source="generated"
    register=1

    if [[ -n "$other_dev" ]]; then
      if [[ "$OTHER_ROOT_MOUNTED" -eq 1 ]]; then
        if trial_ingest_remount_other_root_rw "$HB_OTHER_ROOT_MNT"; then
          write_trial_ingest_secret "$secret" "$HB_OTHER_ROOT_MNT"
          log "Copied trial ingest secret to other drive at ${HB_OTHER_ROOT_MNT}${HB_TRIAL_INGEST_SECRET}."
        else
          if other_mnt="$(trial_ingest_mount_other_root "$other_dev" "$HB_OTHER_ROOT_MNT" rw)"; then
            OTHER_ROOT_MOUNTED=1
            write_trial_ingest_secret "$secret" "$HB_OTHER_ROOT_MNT"
            log "Copied trial ingest secret to other drive at ${HB_OTHER_ROOT_MNT}${HB_TRIAL_INGEST_SECRET}."
          else
            log "WARNING: Could not mount ${other_dev} read-write; secret not copied to other drive."
          fi
        fi
      else
        if other_mnt="$(trial_ingest_mount_other_root "$other_dev" "$HB_OTHER_ROOT_MNT" rw)"; then
          OTHER_ROOT_MOUNTED=1
          write_trial_ingest_secret "$secret" "$HB_OTHER_ROOT_MNT"
          log "Copied trial ingest secret to other drive at ${HB_OTHER_ROOT_MNT}${HB_TRIAL_INGEST_SECRET}."
        else
          log "WARNING: Could not mount ${other_dev}; secret not copied to other drive."
        fi
      fi
    fi
  fi

  configure_trial_ingest_pre_remoteservers "" "$DEFAULTS_GROUP" "$DEFAULTS_FILE"

  if [[ "$register" -eq 1 ]]; then
    local mesh_workgroup cloud_registry_url
    mesh_workgroup="$(mesh_workgroup_for_defaults_group "$DEFAULTS_GROUP" "$DEFAULTS_FILE")"
    cloud_registry_url="$(cloud_registry_url_for_defaults_group "$DEFAULTS_GROUP" "$DEFAULTS_FILE")"
    if [[ -z "$cloud_registry_url" ]]; then
      log "WARNING: cloud_registry not set for section ${DEFAULTS_GROUP} in ${DEFAULTS_FILE}"
      print_trial_ingest_secret_banner "$secret" "$HB_TRIAL_INGEST_SECRET"
    elif ! trial_ingest_have_internet; then
      log "WARNING: No internet connectivity; skipping cloud registry registration."
      print_trial_ingest_secret_banner "$secret" "$HB_TRIAL_INGEST_SECRET"
    elif register_trial_ingest_writer "$cloud_registry_url" "$mesh_workgroup" "$hostname" "$secret"; then
      log "Writer is inactive until activated in MySQL; contact ngage-systems for data access."
    else
      print_trial_ingest_secret_banner "$secret" "$HB_TRIAL_INGEST_SECRET"
    fi
  elif [[ "$secret_source" == "other_drive" ]]; then
    log "Skipping cloud registry registration (secret copied from other drive)."
  fi

  if [[ "$NO_RESTART" -eq 0 ]]; then
    if have_cmd systemctl && systemctl list-unit-files dserv.service >/dev/null 2>&1; then
      log "Restarting dserv..."
      systemctl restart dserv
    else
      log "WARNING: dserv.service not found; skipping restart."
    fi
  else
    log "Skipping dserv restart (--no-restart)."
  fi

  log "Cloud trial ingest enabled (secret source: ${secret_source})."
}

main "$@"
