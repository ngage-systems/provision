#!/usr/bin/env bash
set -euo pipefail

# Provision an NVMe boot drive while running from eMMC/microSD (Bookworm+),
# install stim2/dserv/dlsh/ess into the NVMe rootfs, and configure kiosk defaults.
# This script is intended to fully provision the NVMe target (not run from NVMe).
#
# Flow:
# - Self-update from git when internet is available (disable with --no-self-update or HB_PROVISION_NO_SELF_UPDATE=1)
# - Read GUI-collected inputs from JSON (with defaults from device_defaults.ini)
# - Configure Wi-Fi (optional) and verify internet connectivity
# - Flash Raspberry Pi OS Lite arm64 to NVMe and expand the root filesystem
# - Configure headless settings: SSH, user, hostname, Wi-Fi, timezone, locale
# - Configure display mode/rotation and monitor geometry
# - Install dserv stack + ESS repo in NVMe rootfs
# - Copy /etc/dserv/trial_ingest_secret from the fallback host rootfs when present (written by provision_emmc_for_nvme_fallback.sh)
# - Enable services, kiosk settings, and seatd (with stim2 startup delay)
# - Save full log to /var/log/provision/provision_nvme_YYYYMMDD_HHMMSS.log on NVMe rootfs
# - Optional persistent swap file on host rootfs (eMMC when / is eMMC): HB_EMMC_SWAP_MB (default 2048; 0=off)
# - Configure EEPROM boot order to prefer NVMe
# - Wait for the GUI to request reboot

LOG_PREFIX=""
HB_WIFI_SCAN_FILE="/tmp/hb_wifi_scan_ssids.txt"
HB_SELFUPDATED="${HB_SELFUPDATED:-0}"
HB_POST_UPDATE_ATTEMPTED="${HB_POST_UPDATE_ATTEMPTED:-0}"
HB_SELFUPDATE_NO_INTERNET="${HB_SELFUPDATE_NO_INTERNET:-0}"
HB_PROVISION_NO_SELF_UPDATE="${HB_PROVISION_NO_SELF_UPDATE:-0}"
HB_LOG_FILE=""
ANSWERS_FILE="${HB_PROVISION_ANSWERS:-/tmp/hb_provision_answers.json}"
HB_REBOOT_REQUEST_FILE="${HB_PROVISION_REBOOT_REQUEST_FILE:-/tmp/hb_provision_reboot_requested}"
HB_PROVISION_COMPLETE_MARKER="Provisioning complete. Waiting for GUI reboot request."

# Persistent swap on host rootfs (eMMC when booting from eMMC). Adds /etc/fstab entry. HB_EMMC_SWAP_MB=0 disables.
HB_EMMC_SWAP_MB="${HB_EMMC_SWAP_MB:-2048}"
HB_EMMC_SWAP_PATH="${HB_EMMC_SWAP_PATH:-/var/swap/hb_provision.swap}"

# Same path on fallback rootfs (after boot) and on NVMe target after provisioning.
HB_TRIAL_INGEST_SECRET="/etc/dserv/trial_ingest_secret"

ANSWER_DEFAULTS_GROUP=""
ANSWER_DEFAULTS_DEVICE_TYPE=""
ANSWER_DEFAULTS_SECTION=""
ANSWER_WIFI_COUNTRY=""
ANSWER_WIFI_SSID=""
ANSWER_WIFI_PASSWORD=""
ANSWER_WIFI_HIDDEN=""
ANSWER_TIMEZONE=""
ANSWER_LOCALE=""
ANSWER_SCREEN_PIXELS_WIDTH=""
ANSWER_SCREEN_PIXELS_HEIGHT=""
ANSWER_SCREEN_REFRESH_RATE=""
ANSWER_SCREEN_ROTATION=""
ANSWER_HOSTNAME=""
ANSWER_USERNAME=""
ANSWER_PASSWORD=""
ANSWER_MONITOR_WIDTH_CM=""
ANSWER_MONITOR_HEIGHT_CM=""
ANSWER_MONITOR_DISTANCE_CM=""
ANSWER_CONFIRM_ERASE=""
ANSWER_NVME_DEVICE=""
ANSWER_BOOT_TARGET_DEVICE=""
ANSWER_ALLOW_POSSIBLE_SD=""
ANSWER_CONNECTIVITY_CONTINUE_ANYWAY=""
ANSWER_ACCESSORY_CHECKS_JSON=""

DEFAULTS_FILE=""
DEFAULTS_SECTION=""
DEFAULT_USERNAME=""
DEFAULT_TIMEZONE="America/New_York"
DEFAULT_LOCALE="en_us"
DEFAULT_WIFI_COUNTRY="US"
DEFAULT_SCREEN_PIXELS_WIDTH=""
DEFAULT_SCREEN_PIXELS_HEIGHT=""
DEFAULT_SCREEN_REFRESH_RATE=""
DEFAULT_SCREEN_ROTATION=""
DEFAULT_MESH_HOST=""
DEFAULT_MESH_WORKGROUP=""
MONITOR_WIDTH_CM_DEFAULT="21.7"
MONITOR_HEIGHT_CM_DEFAULT="13.6"
MONITOR_DISTANCE_CM_DEFAULT="30.0"
ESS_SOURCE_DEFAULT="https://github.com/homebase-sheinberg/ess.git"
ESS_SOURCE="$ESS_SOURCE_DEFAULT"

# Used by EXIT trap for cleanup (must not be local vars, because traps can run after scope exits).
HB_BOOT_MNT=""
HB_ROOT_MNT=""

die() {
  if [[ -n "$LOG_PREFIX" ]]; then
    echo "$LOG_PREFIX ERROR: $*" >&2
  else
    echo "ERROR: $*" >&2
  fi
  exit 1
}

log() {
  # Logs go to stderr so functions that "return data" via stdout can be safely captured.
  if [[ -n "$LOG_PREFIX" ]]; then
    echo "$LOG_PREFIX $*" >&2
  else
    echo "$*" >&2
  fi
}

setup_logging() {
  if [[ -n "$HB_LOG_FILE" ]]; then
    return 0
  fi

  HB_LOG_FILE="$(mktemp -p /tmp provision_nvme.XXXXXX.log)"
  exec > >(tee -a "$HB_LOG_FILE") 2>&1
  log "Logging to $HB_LOG_FILE"
}

save_provision_log() {
  if [[ -z "$HB_LOG_FILE" || ! -f "$HB_LOG_FILE" ]]; then
    log "WARNING: Provision log file not found; skipping copy to NVMe."
    return 0
  fi
  if [[ -z "$HB_ROOT_MNT" || ! -d "$HB_ROOT_MNT" ]]; then
    log "WARNING: NVMe root mount not available; skipping log copy."
    return 0
  fi

  local log_dir log_date log_dest
  log_dir="$HB_ROOT_MNT/var/log/provision"
  if ! mkdir -p "$log_dir"; then
    log "WARNING: Failed to create $log_dir; skipping log copy."
    return 0
  fi

  log_date="$(date +%Y%m%d_%H%M%S)"
  log_dest="$log_dir/provision_nvme_${log_date}.log"
  if ! cp -f "$HB_LOG_FILE" "$log_dest"; then
    log "WARNING: Failed to copy provision log to $log_dest"
    return 0
  fi
  log "Saved provision log to $log_dest"
}
need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

apt_output_has_lock_error() {
  local output_file="$1"
  grep -Eiq 'Could not get lock|Unable to lock directory|Resource temporarily unavailable|Waiting for cache lock|Unable to acquire the dpkg frontend lock|Could not open lock file' "$output_file"
}

run_with_apt_lock_retry() {
  local description="$1"
  shift

  local retry_seconds="${APT_LOCK_RETRY_SECONDS:-300}"
  local sleep_seconds="${APT_LOCK_RETRY_SLEEP_SECONDS:-10}"
  local start now elapsed attempt status output_file
  start="$(date +%s)"
  attempt=1
  output_file="$(mktemp -p /tmp provision_apt.XXXXXX.log)"

  while true; do
    : > "$output_file"
    if "$@" > >(tee "$output_file") 2>&1; then
      rm -f "$output_file"
      return 0
    else
      status=$?
    fi

    if ! apt_output_has_lock_error "$output_file"; then
      rm -f "$output_file"
      return "$status"
    fi

    now="$(date +%s)"
    elapsed=$((now - start))
    if (( elapsed >= retry_seconds )); then
      log "ERROR: ${description} failed because apt/dpkg remained locked after ${elapsed}s."
      rm -f "$output_file"
      return "$status"
    fi

    log "apt/dpkg lock detected during ${description}; waiting ${sleep_seconds}s before retry ${attempt}..."
    sleep "$sleep_seconds"
    attempt=$((attempt + 1))
  done
}

ini_list_sections() {
  local file="$1"
  awk '
    /^[[:space:]]*\[[^]]+\][[:space:]]*$/ {
      line=$0
      sub(/^[[:space:]]*\[/, "", line)
      sub(/\][[:space:]]*$/, "", line)
      print line
    }' "$file"
}

ini_list_device_sections() {
  local file="$1"
  ini_list_sections "$file" | awk -F. 'NF>=3' || true
}

ini_list_groups() {
  local file="$1"
  ini_list_device_sections "$file" | awk -F. '
    NF>=2 {
      group=$1
      for (i=2; i<NF; i++) group=group "." $i
      print group
    }' | sort -u
}

ini_list_device_types_for_group() {
  local file="$1"
  local group="$2"
  ini_list_device_sections "$file" | awk -F. -v g="$group" '
    NF>=2 {
      grp=$1
      for (i=2; i<NF; i++) grp=grp "." $i
      if (grp==g) print $NF
    }' | sort -u
}

ini_section_exists() {
  local file="$1"
  local section="$2"
  ini_list_sections "$file" | grep -Fxq "$section"
}

ini_get() {
  local file="$1"
  local section="$2"
  local key="$3"
  awk -v section="$section" -v key="$key" '
    /^[[:space:]]*[#;]/ {next}
    /^[[:space:]]*\[/ {
      line=$0
      sub(/^[[:space:]]*\[/, "", line)
      sub(/\][[:space:]]*$/, "", line)
      in_section=(line==section)
      next
    }
    in_section {
      split($0, a, "=")
      k=a[1]
      sub(/^[[:space:]]+/, "", k); sub(/[[:space:]]+$/, "", k)
      if (k==key) {
        v=substr($0, index($0, "=")+1)
        sub(/^[[:space:]]+/, "", v); sub(/[[:space:]]+$/, "", v)
        print v
        exit
      }
    }
  ' "$file"
}

cloud_ingest_url_for_defaults_group() {
  local group="$1"
  local file="$2"
  ini_get "$file" "$group" "cloud_ingest"
}

select_defaults_section() {
  local file="$1"
  local section="${DEVICE_DEFAULTS_SECTION:-}"
  local group="${DEVICE_DEFAULTS_GROUP:-}"
  local subgroup="${DEVICE_DEFAULTS_SUBGROUP:-}"

  if [[ -n "$section" ]] && ini_section_exists "$file" "$section"; then
    echo "$section"
    return 0
  fi

  if [[ -n "$group" && -n "$subgroup" ]]; then
    section="${group}.${subgroup}"
    if ini_section_exists "$file" "$section"; then
      echo "$section"
      return 0
    fi
  fi

  local groups group_choice
  groups="$(ini_list_groups "$file")"
  if [[ -z "$groups" ]]; then
    return 0
  fi

  log "Available groups:"
  mapfile -t _groups_list < <(printf '%s\n' "$groups" | sed '/^$/d')
  local i
  for i in "${!_groups_list[@]}"; do
    printf '  [%d] %s\n' "$i" "${_groups_list[$i]}" >&2
  done
  read -r -p "Select group by number, or type name (leave blank to skip defaults): " group_choice
  if [[ -z "$group_choice" ]]; then
    echo ""
    return 0
  fi
  if [[ "$group_choice" =~ ^[0-9]+$ ]] && [[ "$group_choice" -ge 0 && "$group_choice" -lt "${#_groups_list[@]}" ]]; then
    group="${_groups_list[$group_choice]}"
  else
    group="$group_choice"
  fi

  local types type_choice
  types="$(ini_list_device_types_for_group "$file" "$group")"
  if [[ -z "$types" ]]; then
    die "No device types found for group '$group'."
  fi

  log "Available device types for ${group}:"
  mapfile -t _types_list < <(printf '%s\n' "$types" | sed '/^$/d')
  for i in "${!_types_list[@]}"; do
    printf '  [%d] %s\n' "$i" "${_types_list[$i]}" >&2
  done
  read -r -p "Select device type by number, or type name (leave blank to skip defaults): " type_choice
  if [[ -z "$type_choice" ]]; then
    echo ""
    return 0
  fi
  if [[ "$type_choice" =~ ^[0-9]+$ ]] && [[ "$type_choice" -ge 0 && "$type_choice" -lt "${#_types_list[@]}" ]]; then
    subgroup="${_types_list[$type_choice]}"
  else
    subgroup="$type_choice"
  fi

  section="${group}.${subgroup}"
  if ini_section_exists "$file" "$section"; then
    echo "$section"
    return 0
  fi
  die "Defaults section '$section' not found in $file"
}

load_defaults() {
  local script_path script_dir
  script_path="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"
  script_dir="$(cd "$(dirname "$script_path")" && pwd -P)"
  DEFAULTS_FILE="${DEVICE_DEFAULTS_FILE:-${script_dir}/device_defaults.ini}"

  if [[ ! -r "$DEFAULTS_FILE" ]]; then
    log "WARNING: Defaults file not found at $DEFAULTS_FILE; using built-in defaults."
    return 0
  fi

  if [[ -n "${DEVICE_DEFAULTS_SECTION:-}" ]]; then
    DEFAULTS_SECTION="$DEVICE_DEFAULTS_SECTION"
    ini_section_exists "$DEFAULTS_FILE" "$DEFAULTS_SECTION" || die "Defaults section '$DEFAULTS_SECTION' not found in $DEFAULTS_FILE"
  elif [[ -n "${DEVICE_DEFAULTS_GROUP:-}" && -n "${DEVICE_DEFAULTS_SUBGROUP:-}" ]]; then
    DEFAULTS_SECTION="${DEVICE_DEFAULTS_GROUP}.${DEVICE_DEFAULTS_SUBGROUP}"
    ini_section_exists "$DEFAULTS_FILE" "$DEFAULTS_SECTION" || die "Defaults section '$DEFAULTS_SECTION' not found in $DEFAULTS_FILE"
  else
    log "No defaults selected; using built-in defaults."
    return 0
  fi
  if [[ -z "$DEFAULTS_SECTION" ]]; then
    log "No defaults selected; using built-in defaults."
    return 0
  fi
  log "Using defaults section: $DEFAULTS_SECTION"

  local group="${DEFAULTS_SECTION%.*}"
  local meta="${group}"
  if ini_section_exists "$DEFAULTS_FILE" "$meta"; then
    local ess_source
    ess_source="$(ini_get "$DEFAULTS_FILE" "$meta" "ess_source")"
    [[ -n "$ess_source" ]] && ESS_SOURCE="$ess_source"
  fi

  local mesh_host mesh_workgroup
  mesh_host="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "mesh_host")"
  mesh_workgroup="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "mesh_workgroup")"
  if [[ -z "$mesh_host" && -n "$meta" ]] && ini_section_exists "$DEFAULTS_FILE" "$meta"; then
    mesh_host="$(ini_get "$DEFAULTS_FILE" "$meta" "mesh_host")"
  fi
  if [[ -z "$mesh_workgroup" && -n "$meta" ]] && ini_section_exists "$DEFAULTS_FILE" "$meta"; then
    mesh_workgroup="$(ini_get "$DEFAULTS_FILE" "$meta" "mesh_workgroup")"
  fi
  [[ -n "$mesh_host" ]] && DEFAULT_MESH_HOST="$mesh_host"
  [[ -n "$mesh_workgroup" ]] && DEFAULT_MESH_WORKGROUP="$mesh_workgroup"

  local val
  val="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "username")"
  [[ -n "$val" ]] && DEFAULT_USERNAME="$val"
  val="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "timezone")"
  [[ -n "$val" ]] && DEFAULT_TIMEZONE="$val"
  val="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "locale")"
  [[ -n "$val" ]] && DEFAULT_LOCALE="$val"
  val="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "wifi_country")"
  [[ -n "$val" ]] && DEFAULT_WIFI_COUNTRY="$val"
  val="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "screen_pixels_width")"
  [[ -n "$val" ]] && DEFAULT_SCREEN_PIXELS_WIDTH="$val"
  val="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "screen_pixels_height")"
  [[ -n "$val" ]] && DEFAULT_SCREEN_PIXELS_HEIGHT="$val"
  val="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "screen_refresh_rate")"
  [[ -n "$val" ]] && DEFAULT_SCREEN_REFRESH_RATE="$val"
  val="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "screen_rotation")"
  [[ -n "$val" ]] && DEFAULT_SCREEN_ROTATION="$val"
  val="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "monitor_width_cm")"
  [[ -n "$val" ]] && MONITOR_WIDTH_CM_DEFAULT="$val"
  val="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "monitor_height_cm")"
  [[ -n "$val" ]] && MONITOR_HEIGHT_CM_DEFAULT="$val"
  val="$(ini_get "$DEFAULTS_FILE" "$DEFAULTS_SECTION" "monitor_distance_cm")"
  [[ -n "$val" ]] && MONITOR_DISTANCE_CM_DEFAULT="$val"
}

usage() {
  cat >&2 <<'EOF'
Usage: sudo ./provision_nvme.sh [--answers PATH] [--no-self-update]

Provision an NVMe target using answers collected by provision_nvme_gui.py.

Options:
  --answers PATH      JSON answers file (default: /tmp/hb_provision_answers.json)
  --no-self-update    Do not git fetch/reset this repo to update the script (also: HB_PROVISION_NO_SELF_UPDATE=1)
  -h, --help          Show this help
EOF
}

parse_args() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --answers)
        [[ "$#" -ge 2 ]] || die "--answers requires a path"
        ANSWERS_FILE="$2"
        shift 2
        ;;
      --answers=*)
        ANSWERS_FILE="${1#--answers=}"
        shift
        ;;
      --no-self-update)
        HB_PROVISION_NO_SELF_UPDATE=1
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

load_answers_json() {
  local answers_file="$1"
  [[ -r "$answers_file" ]] || die "Answers JSON not readable: $answers_file"
  need_cmd python3

  local assignments
  assignments="$(ANSWERS_FILE="$answers_file" python3 - <<'PY'
import json
import os
import shlex
import sys

path = os.environ["ANSWERS_FILE"]
try:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
except Exception as exc:
    print(f"echo {shlex.quote('ERROR: Failed to parse answers JSON: ' + str(exc))} >&2")
    print("exit 1")
    sys.exit(0)

if not isinstance(data, dict):
    print("echo 'ERROR: Answers JSON must be an object.' >&2")
    print("exit 1")
    sys.exit(0)

keys = {
    "ANSWER_DEFAULTS_GROUP": "defaults_group",
    "ANSWER_DEFAULTS_DEVICE_TYPE": "defaults_device_type",
    "ANSWER_DEFAULTS_SECTION": "defaults_section",
    "ANSWER_WIFI_COUNTRY": "wifi_country",
    "ANSWER_WIFI_SSID": "wifi_ssid",
    "ANSWER_WIFI_PASSWORD": "wifi_password",
    "ANSWER_WIFI_HIDDEN": "wifi_hidden",
    "ANSWER_TIMEZONE": "timezone",
    "ANSWER_LOCALE": "locale",
    "ANSWER_SCREEN_PIXELS_WIDTH": "screen_pixels_width",
    "ANSWER_SCREEN_PIXELS_HEIGHT": "screen_pixels_height",
    "ANSWER_SCREEN_REFRESH_RATE": "screen_refresh_rate",
    "ANSWER_SCREEN_ROTATION": "screen_rotation",
    "ANSWER_HOSTNAME": "hostname",
    "ANSWER_USERNAME": "username",
    "ANSWER_PASSWORD": "password",
    "ANSWER_MONITOR_WIDTH_CM": "monitor_width_cm",
    "ANSWER_MONITOR_HEIGHT_CM": "monitor_height_cm",
    "ANSWER_MONITOR_DISTANCE_CM": "monitor_distance_cm",
    "ANSWER_CONFIRM_ERASE": "confirm_erase",
    "ANSWER_NVME_DEVICE": "nvme_device",
    "ANSWER_BOOT_TARGET_DEVICE": "boot_target_device",
    "ANSWER_ALLOW_POSSIBLE_SD": "allow_possible_sd",
    "ANSWER_CONNECTIVITY_CONTINUE_ANYWAY": "connectivity_continue_anyway",
    "ANSWER_CLOUD_TRIAL_INGEST": "cloud_trial_ingest",
}

for shell_name, json_name in keys.items():
    value = data.get(json_name, "")
    if value is None:
        value = ""
    elif isinstance(value, bool):
        value = "true" if value else "false"
    else:
        value = str(value)
    print(f"{shell_name}={shlex.quote(value)}")

accessory_checks = data.get("accessory_checks", {})
if not isinstance(accessory_checks, dict):
    accessory_checks = {}
print(f"ANSWER_ACCESSORY_CHECKS_JSON={shlex.quote(json.dumps(accessory_checks, sort_keys=True))}")

wifi_networks = data.get("wifi_networks")
if not isinstance(wifi_networks, list):
    wifi_networks = []
print(f"ANSWER_WIFI_NETWORKS_JSON={shlex.quote(json.dumps(wifi_networks, sort_keys=True))}")
PY
)"
  eval "$assignments"
}

apply_answer_defaults_env() {
  if [[ -n "$ANSWER_DEFAULTS_SECTION" ]]; then
    export DEVICE_DEFAULTS_SECTION="$ANSWER_DEFAULTS_SECTION"
  elif [[ -n "$ANSWER_DEFAULTS_GROUP" && -n "$ANSWER_DEFAULTS_DEVICE_TYPE" ]]; then
    export DEVICE_DEFAULTS_GROUP="$ANSWER_DEFAULTS_GROUP"
    export DEVICE_DEFAULTS_SUBGROUP="$ANSWER_DEFAULTS_DEVICE_TYPE"
  fi
}

require_answer() {
  local name="$1"
  local value="$2"
  [[ -n "$value" ]] || die "Missing required answer: $name"
}

validate_positive_number() {
  local name="$1"
  local value="$2"
  python3 - "$name" "$value" <<'PY'
import sys

name, value = sys.argv[1], sys.argv[2]
try:
    number = float(value)
except ValueError:
    print(f"ERROR: {name} must be a number.", file=sys.stderr)
    sys.exit(1)
if number <= 0:
    print(f"ERROR: {name} must be greater than zero.", file=sys.stderr)
    sys.exit(1)
PY
}

validate_int_range() {
  local name="$1"
  local value="$2"
  local min="$3"
  local max="$4"
  [[ "$value" =~ ^[0-9]+$ ]] || die "$name must be a whole number."
  (( value >= min && value <= max )) || die "$name must be between $min and $max."
}

validate_wifi_answers() {
  need_cmd python3
  ANSWER_WIFI_NETWORKS_JSON="${ANSWER_WIFI_NETWORKS_JSON:-[]}" \
    ANSWER_WIFI_SSID="${ANSWER_WIFI_SSID:-}" \
    ANSWER_WIFI_PASSWORD="${ANSWER_WIFI_PASSWORD:-}" \
    python3 - <<'PY'
import json
import os
import sys

raw = os.environ.get("ANSWER_WIFI_NETWORKS_JSON", "[]")
try:
    arr = json.loads(raw)
except json.JSONDecodeError:
    print("ERROR: wifi_networks in answers is not valid JSON.", file=sys.stderr)
    sys.exit(1)
if not isinstance(arr, list):
    print("ERROR: wifi_networks must be a JSON array.", file=sys.stderr)
    sys.exit(1)

def bad_line(s):
    return "\n" in s or "\r" in s

if arr:
    for i, item in enumerate(arr):
        if not isinstance(item, dict):
            print("ERROR: Each wifi_networks entry must be an object.", file=sys.stderr)
            sys.exit(1)
        ssid = str(item.get("ssid", "")).strip()
        pw = str(item.get("password", ""))
        if not ssid:
            print("ERROR: wifi_networks entries must include a non-empty ssid.", file=sys.stderr)
            sys.exit(1)
        if not pw:
            print("ERROR: wifi_networks entries must include a password.", file=sys.stderr)
            sys.exit(1)
        if bad_line(ssid) or bad_line(pw):
            print("ERROR: Wi-Fi SSID and password cannot contain newline characters.", file=sys.stderr)
            sys.exit(1)
else:
    ssid = os.environ.get("ANSWER_WIFI_SSID", "").strip()
    if ssid:
        pw = os.environ.get("ANSWER_WIFI_PASSWORD", "")
        if not pw:
            print("ERROR: Missing required answer: wifi_password", file=sys.stderr)
            sys.exit(1)
        if bad_line(ssid) or bad_line(pw):
            print("ERROR: Wi-Fi SSID and password cannot contain newline characters.", file=sys.stderr)
            sys.exit(1)
sys.exit(0)
PY
}

validate_answers() {
  require_answer "wifi_country" "$ANSWER_WIFI_COUNTRY"
  ANSWER_WIFI_COUNTRY="${ANSWER_WIFI_COUNTRY^^}"
  [[ "$ANSWER_WIFI_COUNTRY" =~ ^[A-Z]{2}$ ]] || die "Invalid Wi-Fi country '$ANSWER_WIFI_COUNTRY'."

  validate_wifi_answers || die "Wi-Fi answers validation failed."

  require_answer "timezone" "$ANSWER_TIMEZONE"
  [[ -f "/usr/share/zoneinfo/${ANSWER_TIMEZONE}" ]] || die "Invalid timezone '$ANSWER_TIMEZONE'."

  require_answer "locale" "$ANSWER_LOCALE"
  if [[ "$ANSWER_LOCALE" =~ ^[a-z]{2}_[a-z]{2}$ ]]; then
    ANSWER_LOCALE="${ANSWER_LOCALE%_*}_$(echo "${ANSWER_LOCALE#*_}" | tr 'a-z' 'A-Z').UTF-8"
  fi
  [[ "$ANSWER_LOCALE" =~ ^[a-z]{2}_[A-Z]{2}\.UTF-8$ ]] || die "Invalid locale '$ANSWER_LOCALE'."
  [[ -f "/usr/share/i18n/locales/${ANSWER_LOCALE%.UTF-8}" ]] || die "Locale not found on this system: $ANSWER_LOCALE"

  local screen_count=0
  [[ -n "$ANSWER_SCREEN_PIXELS_WIDTH" ]] && screen_count=$((screen_count + 1))
  [[ -n "$ANSWER_SCREEN_PIXELS_HEIGHT" ]] && screen_count=$((screen_count + 1))
  [[ -n "$ANSWER_SCREEN_REFRESH_RATE" ]] && screen_count=$((screen_count + 1))
  if [[ "$screen_count" -gt 0 && "$screen_count" -lt 3 ]]; then
    die "Screen width, height, and refresh rate must be provided together, or all left blank."
  fi
  if [[ "$screen_count" -eq 3 ]]; then
    validate_int_range "screen_pixels_width" "$ANSWER_SCREEN_PIXELS_WIDTH" 320 7680
    validate_int_range "screen_pixels_height" "$ANSWER_SCREEN_PIXELS_HEIGHT" 240 4320
    validate_int_range "screen_refresh_rate" "$ANSWER_SCREEN_REFRESH_RATE" 1 360
  else
    ANSWER_SCREEN_ROTATION=""
  fi
  if [[ -n "$ANSWER_SCREEN_ROTATION" ]]; then
    case "$ANSWER_SCREEN_ROTATION" in
      0|90|180|270) ;;
      *) die "screen_rotation must be 0, 90, 180, or 270." ;;
    esac
  fi

  require_answer "hostname" "$ANSWER_HOSTNAME"
  ANSWER_HOSTNAME="${ANSWER_HOSTNAME,,}"
  [[ "$ANSWER_HOSTNAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || die "Invalid hostname '$ANSWER_HOSTNAME'."

  require_answer "username" "$ANSWER_USERNAME"
  [[ "$ANSWER_USERNAME" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "Invalid username '$ANSWER_USERNAME'."
  require_answer "password" "$ANSWER_PASSWORD"

  require_answer "monitor_width_cm" "$ANSWER_MONITOR_WIDTH_CM"
  require_answer "monitor_height_cm" "$ANSWER_MONITOR_HEIGHT_CM"
  require_answer "monitor_distance_cm" "$ANSWER_MONITOR_DISTANCE_CM"
  validate_positive_number "monitor_width_cm" "$ANSWER_MONITOR_WIDTH_CM"
  validate_positive_number "monitor_height_cm" "$ANSWER_MONITOR_HEIGHT_CM"
  validate_positive_number "monitor_distance_cm" "$ANSWER_MONITOR_DISTANCE_CM"

  local root_dev candidate_count=0 line
  root_dev="$(strip_partition_suffix "$(root_source)")"
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    candidate_count=$((candidate_count + 1))
  done < <(list_boot_target_candidates "$root_dev")
  if [[ "$candidate_count" -gt 1 ]]; then
    [[ "$ANSWER_CONFIRM_ERASE" == "ERASE" ]] || die "Missing destructive confirmation. Expected confirm_erase to equal ERASE."
  fi
  if [[ -n "$ANSWER_ALLOW_POSSIBLE_SD" ]]; then
    [[ "$ANSWER_ALLOW_POSSIBLE_SD" == "YES" || "$ANSWER_ALLOW_POSSIBLE_SD" == "true" ]] || die "allow_possible_sd must be YES or true when provided."
  fi
  if [[ -n "$ANSWER_CONNECTIVITY_CONTINUE_ANYWAY" ]]; then
    [[ "$ANSWER_CONNECTIVITY_CONTINUE_ANYWAY" =~ ^(true|false|YES|NO)$ ]] \
      || die "connectivity_continue_anyway must be true/false or YES/NO when provided."
  fi
}

log_gui_accessory_checks() {
  if [[ -z "$ANSWER_ACCESSORY_CHECKS_JSON" || "$ANSWER_ACCESSORY_CHECKS_JSON" == "{}" ]]; then
    log "Accessory checks (GUI): not reported."
    return 0
  fi

  local output
  output="$(ACCESSORY_CHECKS_JSON="$ANSWER_ACCESSORY_CHECKS_JSON" python3 - <<'PY' 2>/dev/null || true
import json
import os

labels = [
    ("touchscreen", "Touchscreen"),
    ("juicer", "Juicer"),
    ("power_monitor", "Power monitor"),
    ("camera", "Camera"),
]

try:
    data = json.loads(os.environ.get("ACCESSORY_CHECKS_JSON", "{}"))
except json.JSONDecodeError:
    data = {}
if not isinstance(data, dict):
    data = {}

detected = 0
missing = []
for key, label in labels:
    result = data.get(key, {})
    if not isinstance(result, dict):
        result = {}
    is_detected = result.get("detected") is True
    detail = str(result.get("detail", "")).strip()
    status = "detected" if is_detected else "not detected"
    if is_detected:
        detected += 1
    else:
        missing.append(label)
    suffix = f" ({detail})" if detail else ""
    print(f"Accessory checks (GUI): {label}: {status}{suffix}")

summary = f"Accessory checks (GUI): {detected}/{len(labels)} detected"
if missing:
    summary += "; missing: " + ", ".join(missing)
print(summary)
PY
)"
  if [[ -z "$output" ]]; then
    log "Accessory checks (GUI): could not parse reported results."
    return 0
  fi
  while IFS= read -r line; do
    log "$line"
  done <<< "$output"
}

log_live_accessory_checks() {
  local usb_output=""
  local touchscreen_status="not detected"
  local touchscreen_detail="USB touchscreen controller 0eef:c002 or 222a:0001 not found"
  local juicer_status="not detected"
  local juicer_detail="USB device containing 'juicer' not found"
  local power_status="not detected"
  local power_detail="/dev/serial/by-id/usb-Homebase_power_monitor_*-if00 not found"
  local camera_status="not detected"
  local camera_detail="No cameras reported by rpicam-hello --list-cameras"

  if have_cmd lsusb; then
    usb_output="$(lsusb 2>/dev/null || true)"
    if echo "$usb_output" | grep -Eiq 'ID (0eef:c002|222a:0001)'; then
      touchscreen_status="detected"
      touchscreen_detail="$(echo "$usb_output" | grep -Ei 'ID (0eef:c002|222a:0001)' | sed -n '1p')"
    fi
    if echo "$usb_output" | grep -iq 'juicer'; then
      juicer_status="detected"
      juicer_detail="$(echo "$usb_output" | grep -i 'juicer' | sed -n '1p')"
    fi
  else
    touchscreen_detail="Missing command: lsusb"
    juicer_detail="Missing command: lsusb"
  fi

  if compgen -G "/dev/serial/by-id/usb-Homebase_power_monitor_*-if00" >/dev/null; then
    power_status="detected"
    power_detail="$(compgen -G "/dev/serial/by-id/usb-Homebase_power_monitor_*-if00" | sed -n '1p')"
  fi

  if have_cmd rpicam-hello; then
    local camera_output
    camera_output="$(rpicam-hello --list-cameras 2>&1 || true)"
    if echo "$camera_output" | grep -Eq '^[[:space:]]*[0-9]+[[:space:]]*:'; then
      camera_status="detected"
      camera_detail="$(echo "$camera_output" | grep -E '^[[:space:]]*[0-9]+[[:space:]]*:' | sed -n '1p')"
    elif [[ -n "$camera_output" ]]; then
      camera_detail="$(echo "$camera_output" | sed -n '1p')"
    fi
  else
    camera_detail="Missing command: rpicam-hello"
  fi

  log "Accessory checks (live): Touchscreen: ${touchscreen_status} (${touchscreen_detail})"
  log "Accessory checks (live): Juicer: ${juicer_status} (${juicer_detail})"
  log "Accessory checks (live): Power monitor: ${power_status} (${power_detail})"
  log "Accessory checks (live): Camera: ${camera_status} (${camera_detail})"
}

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "Run as root (e.g. sudo $0)"
  fi
}

read_os_codename() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "${VERSION_CODENAME:-}"
  else
    echo ""
  fi
}

read_os_codename_from_root() {
  local root_mnt="$1"
  local file="${root_mnt}/etc/os-release"
  if [[ -r "$file" ]]; then
    awk -F= '/^VERSION_CODENAME=/{print $2}' "$file" | tr -d '"' | tr -d '\r'
  else
    echo ""
  fi
}

check_bookworm_or_later() {
  local codename
  codename="$(read_os_codename)"
  if [[ -z "$codename" ]]; then
    log "WARNING: Could not read OS codename from /etc/os-release; continuing."
    return 0
  fi
  case "$codename" in
    bookworm|trixie|forky|sid)
      return 0
      ;;
    *)
      die "Expected Raspberry Pi OS Bookworm or later, got VERSION_CODENAME='$codename'"
      ;;
  esac
}

mesh_registry_tcp_target_line() {
  # host:port derived from DEFAULT_MESH_HOST (mirrors provision_nvme_gui.py::parse_mesh_host_for_probe).
  MH="${DEFAULT_MESH_HOST:-}" python3 - <<'PY'
import os
from urllib.parse import urlparse

default_h = "dserv.net"
raw = (os.environ.get("MH") or "").strip()
if not raw:
    raw = "https://dserv.net"
if "://" not in raw:
    raw = "https://" + raw
parsed = urlparse(raw)
host = (parsed.hostname or "").strip() or default_h
port = parsed.port or 443
print(f"{host}:{port}")
PY
}

internet_probe_baseline_targets() {
  printf '%s\n' \
    "1.1.1.1:443" \
    "1.0.0.1:443" \
    "93.184.216.34:80"
}

internet_probe_service_targets() {
  printf '%s\n' \
    "$(mesh_registry_tcp_target_line)" \
    "downloads.raspberrypi.org:443" \
    "github.com:443" \
    "api.github.com:443" \
    "objects.githubusercontent.com:443"
}

internet_probe_targets() {
  # Combined list for diagnostics; have_internet treats baseline vs services differently.
  internet_probe_baseline_targets
  internet_probe_service_targets
}

internet_probe_targets_text() {
  local joined=""
  local target
  while IFS= read -r target; do
    if [[ -n "$joined" ]]; then
      joined+=", "
    fi
    joined+="$target"
  done < <(internet_probe_targets)
  printf '%s\n' "$joined"
}

probe_tcp_target() {
  local target="$1"
  local host="${target%:*}"
  local port="${target##*:}"

  [[ -n "$host" && -n "$port" && "$host" != "$port" ]] || return 1

  if have_cmd timeout; then
    timeout 3 bash -c 'cat < /dev/null > /dev/tcp/$1/$2' _ "$host" "$port" >/dev/null 2>&1
  else
    bash -c 'cat < /dev/null > /dev/tcp/$1/$2' _ "$host" "$port" >/dev/null 2>&1
  fi
}

have_internet() {
  # At least one baseline target, then every registry + service hostname must answer.
  local target baseline_ok="false"
  while IFS= read -r target; do
    if probe_tcp_target "$target"; then
      baseline_ok="true"
      break
    fi
  done < <(internet_probe_baseline_targets)
  [[ "$baseline_ok" == "true" ]] || return 1

  while IFS= read -r target; do
    probe_tcp_target "$target" || return 1
  done < <(internet_probe_service_targets)
  return 0
}

# Hostnames that must resolve for provisioning (apt, image, GitHub, registry). Mirrors
# provision_nvme_gui.py::required_dns_hostnames.
provision_critical_dns_hosts() {
  local reg_host
  reg_host="$(mesh_registry_tcp_target_line)"
  reg_host="${reg_host%:*}"
  printf '%s\n' \
    deb.debian.org \
    archive.raspberrypi.com \
    downloads.raspberrypi.org \
    github.com \
    api.github.com \
    objects.githubusercontent.com \
    "$reg_host" | sort -u
}

warn_if_provision_critical_dns_fails() {
  local h failed=()

  if ! have_cmd getent; then
    log "WARNING: getent not available; skipping provisioning DNS host check."
    return 0
  fi
  while IFS= read -r h; do
    [[ -n "$h" ]] || continue
    if ! getent ahosts "$h" >/dev/null 2>&1; then
      failed+=("$h")
    fi
  done < <(provision_critical_dns_hosts)

  [[ "${#failed[@]}" -eq 0 ]] && return 0

  local joined="" f
  for f in "${failed[@]}"; do
    [[ -n "$joined" ]] && joined+=", "
    joined+="$f"
  done
  log "WARNING: DNS lookup failed for provisioning hostname(s): $joined"
  log "WARNING: apt, image downloads, GitHub, or registry steps may fail. Check DNS (/etc/resolv.conf) and your network."
}

wait_for_internet() {
  local timeout_s="${1:-30}"
  local sleep_s="${2:-3}"
  local waited=0

  while true; do
    have_internet && return 0
    if (( waited >= timeout_s )); then
      return 1
    fi
    log "Internet probe failed for $(internet_probe_targets_text); retrying in ${sleep_s}s..."
    sleep "$sleep_s"
    waited=$((waited + sleep_s))
  done
}

update_self_if_possible() {
  local phase="$1"
  local script_path script_dir repo_root origin_head target_ref before after

  if [[ "${HB_PROVISION_NO_SELF_UPDATE:-0}" == "1" ]]; then
    log "Self-update: disabled for this run (--no-self-update or HB_PROVISION_NO_SELF_UPDATE=1)."
    return 0
  fi

  log "Self-update: checking for updates (${phase} phase)..."

  git_cmd() {
    if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
      sudo -u "$SUDO_USER" git -C "$repo_root" "$@"
    else
      git -C "$repo_root" "$@"
    fi
  }

  if [[ "$HB_SELFUPDATED" == "1" ]]; then
    return 0
  fi
  if [[ "$phase" == "post" && "$HB_POST_UPDATE_ATTEMPTED" == "1" ]]; then
    return 0
  fi
  if ! have_internet; then
    HB_SELFUPDATE_NO_INTERNET=1
    log "Self-update: no internet after checking $(internet_probe_targets_text); skipping ${phase} update."
    return 1
  fi
  if ! have_cmd git; then
    log "WARNING: git not available; skipping ${phase}-Wi-Fi self-update."
    return 1
  fi

  script_path="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"
  script_dir="$(cd "$(dirname "$script_path")" && pwd -P)"
  repo_root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null || true)"
  if [[ -z "$repo_root" ]]; then
    log "WARNING: Could not determine git repo root; skipping ${phase}-Wi-Fi self-update."
    return 1
  fi

  origin_head="$(git_cmd symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [[ -z "$origin_head" ]]; then
    origin_head="origin/main"
  fi

  before="$(git_cmd rev-parse HEAD 2>/dev/null || true)"
  if ! git_cmd fetch --prune; then
    log "WARNING: git fetch failed; skipping ${phase}-Wi-Fi self-update."
    return 1
  fi
  target_ref="$(git_cmd rev-parse "$origin_head" 2>/dev/null || true)"
  if [[ -z "$target_ref" ]]; then
    log "WARNING: Could not resolve ${origin_head}; skipping ${phase}-Wi-Fi self-update."
    return 1
  fi
  if ! git_cmd reset --hard "$origin_head"; then
    log "WARNING: git reset failed; skipping ${phase}-Wi-Fi self-update."
    return 1
  fi
  after="$(git_cmd rev-parse HEAD 2>/dev/null || true)"

  if [[ -n "$before" && "$before" != "$after" ]]; then
    local updated_script="${repo_root}/provision_nvme.sh"
    if [[ -r "$updated_script" ]]; then
      log "Self-update: updated script detected; restarting..."
      log "Provisioning script updated. Restarting setup now so the newest provisioning steps are used."
      log "Restarting with the same answers file: $ANSWERS_FILE"
      sleep 3
      exec sudo env HB_SELFUPDATED=1 HB_POST_UPDATE_ATTEMPTED=1 HB_PROVISION_NO_SELF_UPDATE="${HB_PROVISION_NO_SELF_UPDATE:-0}" \
        bash "$updated_script" --answers "$ANSWERS_FILE"
    else
      log "WARNING: Updated script not found at ${updated_script}; continuing."
    fi
  fi

  if [[ "$phase" == "post" ]]; then
    HB_POST_UPDATE_ATTEMPTED=1
  fi
  return 0
}

have_internet_via_iface() {
  # Verifies connectivity over a specific interface (SO_BINDTODEVICE). Matches have_internet semantics.
  local iface="$1"
  [[ -n "$iface" ]] || return 1

  if have_cmd python3; then
    IFACE="$iface" MH="${DEFAULT_MESH_HOST:-}" python3 - <<'PY' >/dev/null 2>&1
import os
import socket
from urllib.parse import urlparse

iface = os.environ.get("IFACE", "")
if not iface:
    raise SystemExit(2)

opt = iface.encode("utf-8", errors="strict")
if not opt.endswith(b"\0"):
    opt += b"\0"


def tcp(h, p):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    s.setsockopt(socket.SOL_SOCKET, 25, opt)
    try:
        s.connect((h, int(p)))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


baseline = [
    ("1.1.1.1", 443),
    ("1.0.0.1", 443),
    ("93.184.216.34", 80),
]
if not any(tcp(h, p) for h, p in baseline):
    raise SystemExit(1)

raw = (os.environ.get("MH") or "").strip()
if not raw:
    raw = "https://dserv.net"
if "://" not in raw:
    raw = "https://" + raw
pr = urlparse(raw)
rh = (pr.hostname or "").strip() or "dserv.net"
rp = pr.port or 443
extras = [
    (rh, rp),
    ("downloads.raspberrypi.org", 443),
    ("github.com", 443),
    ("api.github.com", 443),
    ("objects.githubusercontent.com", 443),
]
for h, port in extras:
    if not tcp(h, port):
        raise SystemExit(1)
raise SystemExit(0)
PY
    return $?
  fi

  if have_cmd ping; then
    ping -I "$iface" -c 1 -W 3 1.1.1.1 >/dev/null 2>&1 && return 0
  fi

  return 1
}

nmcli_connected() {
  have_cmd nmcli || return 1
  nmcli -t -f STATE g 2>/dev/null | grep -q '^connected'
}

wifi_iface() {
  have_cmd nmcli || { echo ""; return 0; }
  nmcli -t -f DEVICE,TYPE,STATE dev status 2>/dev/null \
    | awk -F: '$2=="wifi" && $3=="connected"{print $1; exit}'
}

connected_wifi_ssid() {
  have_cmd nmcli || { echo ""; return 0; }
  nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2; exit}'
}

nmcli_cleanup_temp_connection() {
  local con_name="$1"
  have_cmd nmcli || return 0
  [[ -n "$con_name" ]] || return 0
  nmcli -w 5 con delete "$con_name" >/dev/null 2>&1 || true
}

iface_has_ipv4() {
  local iface="$1"
  [[ -n "$iface" ]] || return 1
  have_cmd ip || return 1
  ip -4 addr show dev "$iface" 2>/dev/null | grep -qE '^\s*inet\s+'
}

wait_for_ipv4() {
  local iface="$1"
  local timeout_s="${2:-45}"
  local waited=0

  while [[ "$waited" -lt "$timeout_s" ]]; do
    if iface_has_ipv4 "$iface"; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  return 1
}

connect_wifi_current() {
  local ssid="$1"
  local pass="$2"

  if ! have_cmd nmcli; then
    log "ERROR: nmcli not found. Install NetworkManager or connect networking manually, then re-run."
    return 1
  fi

  log "Attempting to connect current system to Wi-Fi via NetworkManager (nmcli)..."
  nmcli radio wifi on >/dev/null 2>&1 || true
  nmcli dev wifi rescan >/dev/null 2>&1 || true

  local iface
  iface="$(wifi_iface)"
  if [[ -z "$iface" ]]; then
    iface="$(nmcli -t -f DEVICE,TYPE dev status 2>/dev/null | awk -F: '$2=="wifi"{print $1; exit}')"
  fi
  if [[ -z "$iface" ]]; then
    log "ERROR: No Wi-Fi interface found (nmcli shows no wifi devices)."
    return 1
  fi

  local prev_con=""
  prev_con="$(nmcli -t -f NAME,DEVICE con show --active 2>/dev/null | awk -F: -v d="$iface" '$2==d{print $1; exit}')"

  local con_name="hb-wifi-${ssid//[^A-Za-z0-9_.-]/_}-$RANDOM"
  nmcli_cleanup_temp_connection "$con_name"
  local cleanup_temp="yes"
  if [[ -z "$prev_con" ]]; then
    cleanup_temp="no"
  fi
  trap 'if [[ "'"$cleanup_temp"'" == "yes" ]]; then nmcli_cleanup_temp_connection "'"$con_name"'"; fi' RETURN

  nmcli -w 5 dev disconnect "$iface" >/dev/null 2>&1 || true

  if ! nmcli -w 30 con add type wifi ifname "$iface" con-name "$con_name" ssid "$ssid" >/dev/null 2>&1; then
    log "ERROR: Failed to create temporary Wi-Fi connection for SSID '$ssid'."
    return 1
  fi
  if ! nmcli -w 30 con modify "$con_name" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$pass" >/dev/null 2>&1; then
    log "ERROR: Failed to apply Wi-Fi password for SSID '$ssid' (nmcli rejected it)."
    return 1
  fi
  if ! nmcli -w 60 con up "$con_name" ifname "$iface" >/dev/null 2>&1; then
    log "ERROR: Failed to connect to Wi-Fi SSID '$ssid' (auth may have failed)."
    return 1
  fi

  local got_ssid
  got_ssid="$(connected_wifi_ssid)"
  if [[ "$got_ssid" != "$ssid" ]]; then
    log "ERROR: Connected Wi-Fi SSID mismatch. Expected '$ssid', got '${got_ssid:-<none>}'"
    return 1
  fi

  if ! nmcli_connected; then
    log "ERROR: NetworkManager did not reach connected state after Wi-Fi connect."
    return 1
  fi

  if ! wait_for_ipv4 "$iface" 120; then
    log "ERROR: Wi-Fi connected to '$ssid' on '$iface' but no IPv4 address was acquired within 120s (DHCP may have failed)."
    return 1
  fi

  if have_internet_via_iface "$iface"; then
    log "Wi-Fi connected to '$ssid' and internet is reachable via Wi-Fi."
  else
    log "WARNING: Wi-Fi connected to '$ssid' but internet probe via Wi-Fi failed (captive portal/firewall?)."
  fi

  if [[ -n "$prev_con" && "$prev_con" != "$con_name" ]]; then
    if ! nmcli -w 20 con up "$prev_con" >/dev/null 2>&1; then
      log "WARNING: Failed to restore previous connection '$prev_con' after Wi-Fi validation."
    fi
  fi
}

prompt_monitor_settings() {
  local input

  log "Configure stim2 monitor settings (press Enter to accept defaults)."

  read -r -p "Screen width cm [${MONITOR_WIDTH_CM_DEFAULT}]: " input
  MONITOR_WIDTH_CM="${input:-$MONITOR_WIDTH_CM_DEFAULT}"

  read -r -p "Screen height cm [${MONITOR_HEIGHT_CM_DEFAULT}]: " input
  MONITOR_HEIGHT_CM="${input:-$MONITOR_HEIGHT_CM_DEFAULT}"

  read -r -p "Distance to monitor cm [${MONITOR_DISTANCE_CM_DEFAULT}]: " input
  MONITOR_DISTANCE_CM="${input:-$MONITOR_DISTANCE_CM_DEFAULT}"
}

prompt_username_password() {
  local default_username="${1:-}"
  local username password input
  while true; do
    if [[ -n "$default_username" ]]; then
      read -r -p "Enter username [${default_username}]: " input
      username="${input:-$default_username}"
    else
      read -r -p "Enter username: " username
    fi
    if [[ ! "$username" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
      log "Invalid username '$username' (use a-z, 0-9, '_' or '-', must start with a letter or '_')."
      continue
    fi
    read -r -p "Enter password for '$username' (shown): " password
    if [[ -z "$password" ]]; then
      log "Empty password not allowed. Please try again."
      continue
    fi
    echo "$username"
    echo "$password"
    return 0
  done
}

prompt_hostname() {
  local hn default_hn="${1:-}"
  while true; do
    if [[ -n "$default_hn" ]]; then
      read -r -p "Enter hostname (default: ${default_hn}): " hn
      hn="${hn:-$default_hn}"
    else
      read -r -p "Enter hostname: " hn
    fi
    hn="${hn,,}"
    if [[ "$hn" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
      echo "$hn"
      return 0
    fi
    log "Invalid hostname '$hn' (use a-z, 0-9, and '-', max 63 chars)."
  done
}

prompt_wifi_country() {
  local cc default_cc="${1:-}"
  while true; do
    if [[ -n "$default_cc" ]]; then
      read -r -p "Enter Wi-Fi country code (2 letters, e.g. US, CA, GB, DE, FR, JP). Default: ${default_cc}: " cc
      cc="${cc:-$default_cc}"
    else
      read -r -p "Enter Wi-Fi country code (2 letters, e.g. US, CA, GB, DE, FR, JP). Default: US: " cc
      cc="${cc:-US}"
    fi
    cc="${cc^^}"
    if [[ "$cc" =~ ^[A-Z]{2}$ ]]; then
      echo "$cc"
      return 0
    fi
    log "Invalid country code '$cc'. Please enter 2 letters like US."
  done
}

prompt_timezone() {
  local tz default_tz="${1:-}"
  while true; do
    if [[ -n "$default_tz" ]]; then
      read -r -p "Enter timezone (default: ${default_tz}): " tz
      tz="${tz:-$default_tz}"
    else
      read -r -p "Enter timezone (default: America/New_York): " tz
      tz="${tz:-America/New_York}"
    fi
    if [[ -f "/usr/share/zoneinfo/${tz}" ]]; then
      echo "$tz"
      return 0
    fi
    log "Invalid timezone '${tz}'. Example: America/Los_Angeles, Europe/London, Asia/Tokyo."
  done
}

prompt_locale() {
  local loc base default_loc="${1:-}"
  while true; do
    if [[ -n "$default_loc" ]]; then
      read -r -p "Enter locale (default: ${default_loc}): " loc
      loc="${loc:-$default_loc}"
    else
      read -r -p "Enter locale (default: en_us): " loc
      loc="${loc:-en_us}"
    fi
    loc="$(echo "$loc" | tr 'A-Z' 'a-z')"
    if [[ ! "$loc" =~ ^[a-z]{2}_[a-z]{2}$ ]]; then
      log "Invalid locale '${loc}'. Example: en_us, en_gb, fr_fr, de_de."
      continue
    fi
    base="${loc%_*}_$(echo "${loc#*_}" | tr 'a-z' 'A-Z')"
    if [[ -f "/usr/share/i18n/locales/${base}" ]]; then
      echo "${base}.UTF-8"
      return 0
    fi
    log "Locale '${loc}' not found on this system. Example: en_us, en_gb, fr_fr, de_de."
  done
}

prompt_screen_settings() {
  local w h r rot input

  if [[ -n "$DEFAULT_SCREEN_PIXELS_WIDTH" ]]; then
    read -r -p "Enter screen pixel width (default: ${DEFAULT_SCREEN_PIXELS_WIDTH}): " input
    w="${input:-$DEFAULT_SCREEN_PIXELS_WIDTH}"
  else
    read -r -p "Enter screen pixel width (leave blank to skip): " w
  fi

  if [[ -n "$DEFAULT_SCREEN_PIXELS_HEIGHT" ]]; then
    read -r -p "Enter screen pixel height (default: ${DEFAULT_SCREEN_PIXELS_HEIGHT}): " input
    h="${input:-$DEFAULT_SCREEN_PIXELS_HEIGHT}"
  else
    read -r -p "Enter screen pixel height (leave blank to skip): " h
  fi

  if [[ -n "$DEFAULT_SCREEN_REFRESH_RATE" ]]; then
    read -r -p "Enter screen refresh rate Hz (default: ${DEFAULT_SCREEN_REFRESH_RATE}): " input
    r="${input:-$DEFAULT_SCREEN_REFRESH_RATE}"
  else
    read -r -p "Enter screen refresh rate Hz (leave blank to skip): " r
  fi

  if [[ -n "$DEFAULT_SCREEN_ROTATION" ]]; then
    read -r -p "Enter screen rotation degrees (0/90/180/270). Default: ${DEFAULT_SCREEN_ROTATION}: " input
    rot="${input:-$DEFAULT_SCREEN_ROTATION}"
  else
    read -r -p "Enter screen rotation degrees (0/90/180/270). Default: 0: " rot
    rot="${rot:-0}"
  fi

  [[ -n "$w" && -n "$h" && -n "$r" ]] || { echo ""; echo ""; echo ""; echo ""; return 0; }
  echo "$w"
  echo "$h"
  echo "$r"
  echo "$rot"
}

wifi_scan_ssids() {
  if [[ -n "${HB_WIFI_SCAN_FILE:-}" && -s "$HB_WIFI_SCAN_FILE" ]]; then
    cat "$HB_WIFI_SCAN_FILE"
    return 0
  fi
  local ssids=""
  if command -v nmcli >/dev/null 2>&1; then
    if command -v rfkill >/dev/null 2>&1; then
      rfkill unblock wifi >/dev/null 2>&1 || true
    fi
    nmcli radio wifi on >/dev/null 2>&1 || true
    nmcli dev wifi rescan >/dev/null 2>&1 || true
    sleep 2
    ssids="$(
      nmcli -t -f SSID dev wifi list --rescan yes 2>/dev/null \
        | sed '/^$/d' \
        | sort -u \
        || nmcli -t -f SSID dev wifi list 2>/dev/null | sed '/^$/d' | sort -u \
        || true
    )"
  fi
  if [[ -z "$ssids" ]] && command -v iw >/dev/null 2>&1; then
    local iface
    iface="$(iw dev 2>/dev/null | awk '/Interface/{print $2; exit}' || true)"
    if [[ -n "$iface" ]]; then
      ssids="$(iw dev "$iface" scan 2>/dev/null | grep -E '^\s*SSID:' | sed 's/^\s*SSID:\s*//' | sed '/^$/d' | sort -u || true)"
    fi
  fi
  echo "$ssids"
}

start_wifi_scan_background() {
  have_cmd nmcli || return 0
  local out="${HB_WIFI_SCAN_FILE:-/tmp/hb_wifi_scan_ssids.txt}"
  rm -f "$out" 2>/dev/null || true
  (
    nmcli radio wifi on >/dev/null 2>&1 || true
    nmcli dev wifi rescan >/dev/null 2>&1 || true
    sleep 2
    nmcli -t -f SSID dev wifi list --rescan yes 2>/dev/null \
      | sed '/^$/d' \
      | sort -u \
      > "$out" \
      || true
  ) >/dev/null 2>&1 &
}

prompt_wifi() {
  local ssids ssid pass choice
  log "Scanning for Wi-Fi SSIDs..."
  ssids="$(wifi_scan_ssids)"

  if [[ -n "$ssids" ]]; then
    log "Discovered Wi-Fi SSIDs from the current system:"
    mapfile -t _ssids_list < <(printf '%s\n' "$ssids")
    local i
    for i in "${!_ssids_list[@]}"; do
      printf '  [%d] %s\n' "$i" "${_ssids_list[$i]}" >&2
    done
    echo >&2
    while true; do
      read -r -p "Select Wi-Fi by number, or type an SSID (leave blank to skip Wi-Fi): " choice
      if [[ -z "$choice" ]]; then
        echo ""
        echo ""
        return 0
      fi
      if [[ "$choice" =~ ^[0-9]+$ ]]; then
        if [[ "$choice" -ge 0 && "$choice" -lt "${#_ssids_list[@]}" ]]; then
          ssid="${_ssids_list[$choice]}"
          break
        fi
        log "Invalid selection '$choice'. Please choose one of the listed numbers."
        continue
      fi
      ssid="$choice"
      break
    done
  else
    log "WARNING: Could not scan Wi-Fi SSIDs (no scan results)."
    if command -v nmcli >/dev/null 2>&1; then
      log "nmcli diagnostics:"
      nmcli -t -f WIFI g 2>/dev/null >&2 || true
      nmcli -t -f DEVICE,TYPE,STATE dev status 2>/dev/null >&2 || true
    fi
    read -r -p "Enter Wi-Fi SSID to use (leave blank to skip Wi-Fi): " ssid
    if [[ -z "$ssid" ]]; then
      echo ""
      echo ""
      return 0
    fi
  fi

  while true; do
    if [[ -z "$ssid" ]]; then
      echo ""
      echo ""
      return 0
    fi
    if [[ "$ssid" == *$'\n'* || "$ssid" == *$'\r'* ]]; then
      log "SSID contains newline characters. Please re-enter."
      read -r -p "Enter Wi-Fi SSID to use (leave blank to skip Wi-Fi): " ssid
      if [[ -z "$ssid" ]]; then
        echo ""
        echo ""
        return 0
      fi
      continue
    fi
    read -r -p "Enter Wi-Fi password for '$ssid' (shown): " pass
    if [[ -z "$pass" ]]; then
      log "Empty Wi-Fi password not allowed. Please try again."
      continue
    fi
    if [[ "$pass" == *$'\n'* || "$pass" == *$'\r'* ]]; then
      log "Wi-Fi password contains newline characters. Please try again."
      continue
    fi
    echo "$ssid"
    echo "$pass"
    return 0
  done
}

root_source() {
  need_cmd findmnt
  local src
  src="$(findmnt -n -o SOURCE /)"
  if [[ "$src" == /dev/* ]]; then
    echo "$src"
    return 0
  fi
  if command -v blkid >/dev/null 2>&1; then
    case "$src" in
      PARTUUID=*|UUID=*|LABEL=*)
        local dev
        dev="$(blkid -t "$src" -o device 2>/dev/null | head -n1 || true)"
        if [[ -n "$dev" ]]; then
          echo "$dev"
          return 0
        fi
        ;;
    esac
  fi
  echo "$src"
}

strip_partition_suffix() {
  local src="$1"
  if [[ "$src" =~ ^/dev/mmcblk[0-9]+p[0-9]+$ ]]; then
    echo "${src%p*}"
  elif [[ "$src" =~ ^/dev/nvme[0-9]+n[0-9]+p[0-9]+$ ]]; then
    echo "${src%p*}"
  else
    echo "${src%[0-9]*}"
  fi
}

# Boot-order helpers — keep in sync with provision_emmc_for_nvme_fallback.sh
device_boot_nibble() {
  local dev="$1"
  case "$dev" in
    /dev/nvme*)
      echo 6
      return 0
      ;;
    /dev/mmcblk*)
      echo 1
      return 0
      ;;
    /dev/sd*)
      local tran rm
      tran="$(lsblk -dn -o TRAN "$dev" 2>/dev/null | head -n1 || true)"
      rm="$(lsblk -dn -o RM "$dev" 2>/dev/null | head -n1 || true)"
      if [[ "$tran" == "usb" || "$rm" == "1" ]]; then
        echo 4
        return 0
      fi
      die "Unknown boot mode for block device: $dev"
      ;;
    *)
      die "Unsupported block device for boot order: $dev"
      ;;
  esac
}

boot_order_label() {
  case "$1" in
    1) echo "microSD/eMMC" ;;
    4) echo "USB" ;;
    6) echo "NVMe" ;;
    *) echo "unknown($1)" ;;
  esac
}

boot_order_for_devices() {
  local primary_dev="$1"
  local fallback_dev="$2"
  local primary_nibble fallback_nibble

  [[ "$primary_dev" != "$fallback_dev" ]] || die "Primary and fallback devices must differ: $primary_dev"
  primary_nibble="$(device_boot_nibble "$primary_dev")"
  fallback_nibble="$(device_boot_nibble "$fallback_dev")"
  printf '0x%x' $((0xf00 | (fallback_nibble << 4) | primary_nibble))
}

set_eeprom_boot_order() {
  local primary_dev="$1"
  local fallback_dev="$2"
  local boot_order primary_nibble fallback_nibble need_pcie=0
  local primary_label fallback_label editor

  boot_order="$(boot_order_for_devices "$primary_dev" "$fallback_dev")"
  primary_nibble="$(device_boot_nibble "$primary_dev")"
  fallback_nibble="$(device_boot_nibble "$fallback_dev")"
  primary_label="$(boot_order_label "$primary_nibble")"
  fallback_label="$(boot_order_label "$fallback_nibble")"
  [[ "$primary_nibble" == "6" || "$fallback_nibble" == "6" ]] && need_pcie=1

  need_cmd rpi-eeprom-update
  need_cmd rpi-eeprom-config

  log "Updating EEPROM package + applying latest EEPROM update (if available)..."
  rpi-eeprom-update -a || true

  log "Setting EEPROM BOOT_ORDER to ${boot_order} (${primary_label} first, ${fallback_label} fallback)..."

  editor="/tmp/hb_rpi_eeprom_editor.sh"
  {
    cat <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
f="$1"

ensure_kv() {
  local key="$1" val="$2"
  if grep -qE "^${key}=" "$f"; then
    sed -i -E "s/^${key}=.*/${key}=${val}/" "$f"
  else
    printf '%s=%s\n' "$key" "$val" >> "$f"
  fi
}
EOF
    printf 'ensure_kv "BOOT_ORDER" "%s"\n' "$boot_order"
    if [[ "$need_pcie" == "1" ]]; then
      echo 'ensure_kv "PCIE_PROBE" "1"'
    fi
  } > "$editor"
  chmod +x "$editor"

  if EDITOR="$editor" rpi-eeprom-config --edit >/dev/null 2>&1; then
    :
  elif EDITOR="$editor" rpi-eeprom-config -e >/dev/null 2>&1; then
    :
  else
    log "WARNING: Could not non-interactively edit EEPROM config."
    log "You can run manually:"
    log "  sudo rpi-eeprom-config -e"
    log "and set:"
    log "  BOOT_ORDER=${boot_order}"
    if [[ "$need_pcie" == "1" ]]; then
      log "  PCIE_PROBE=1"
    fi
  fi
}

classify_boot_target_device() {
  local dev="$1"
  local name="${dev##*/}"
  local tran rm
  tran="$(lsblk -dn -o TRAN "$dev" 2>/dev/null | head -n1 || true)"
  rm="$(lsblk -dn -o RM "$dev" 2>/dev/null | head -n1 || true)"

  if [[ "$name" =~ ^nvme ]]; then
    echo "NVMe"
    return 0
  fi
  if compgen -G "${dev}boot0" >/dev/null; then
    echo "eMMC"
    return 0
  fi
  if [[ "$name" =~ ^mmcblk[0-9]+$ ]]; then
    echo "microSD/MMC"
    return 0
  fi
  if [[ "$tran" == "usb" || "$rm" == "1" ]]; then
    echo "USB"
    return 0
  fi
  echo "block"
}

list_boot_target_candidates() {
  local root_dev="$1"
  local line name type size model tran rm dev class
  while read -r line; do
    [[ -n "$line" ]] || continue
    NAME=""; TYPE=""; SIZE=""; MODEL=""; TRAN=""; RM=""
    eval "$line"

    name="${NAME:-}"
    type="${TYPE:-}"
    size="${SIZE:-}"
    model="${MODEL:-}"
    tran="${TRAN:-}"
    rm="${RM:-}"
    [[ "$type" == "disk" ]] || continue
    [[ -n "$name" ]] || continue

    case "$name" in
      loop*|zram*|ram*|sr*)
        continue
        ;;
    esac

    if [[ ! "$name" =~ ^mmcblk[0-9]+$ && ! "$name" =~ ^sd[a-z]+$ && ! "$name" =~ ^nvme ]]; then
      continue
    fi

    dev="/dev/${name}"
    if [[ -n "$root_dev" && "$dev" == "$root_dev" ]]; then
      continue
    fi

    class="$(classify_boot_target_device "$dev")"
    echo "${dev}|${size}|${model}|${class}|${tran}|${rm}"
  done < <(lsblk -dn -P -o NAME,TYPE,SIZE,MODEL,TRAN,RM)
}

configured_boot_target_device() {
  if [[ -n "$ANSWER_BOOT_TARGET_DEVICE" ]]; then
    echo "$ANSWER_BOOT_TARGET_DEVICE"
    return 0
  fi
  if [[ -n "$ANSWER_NVME_DEVICE" ]]; then
    echo "$ANSWER_NVME_DEVICE"
    return 0
  fi
  echo ""
}

check_root_on_fallback_and_target_present() {
  local root_dev="$1"
  local root_src

  root_src="$(root_source)"
  [[ -n "$root_dev" ]] || root_dev="$(strip_partition_suffix "$root_src")"

  log "Root filesystem source: $root_src"
  log "Root block device: $root_dev"

  case "$root_dev" in
    /dev/mmcblk*)
      if ! compgen -G "/dev/mmcblk*boot0" >/dev/null; then
        log "WARNING: Could not find /dev/mmcblk*boot0; this may be microSD rather than eMMC."
        [[ "$ANSWER_ALLOW_POSSIBLE_SD" == "YES" || "$ANSWER_ALLOW_POSSIBLE_SD" == "true" ]] \
          || die "Aborting due to non-eMMC heuristic. Set allow_possible_sd to YES in answers JSON to proceed."
      fi
      ;;
    /dev/sd*)
      log "Running from ${root_dev} (USB/SCSI block device) as fallback source."
      ;;
    *)
      die "Root is not on a supported fallback device (expected eMMC/microSD or USB block device). Root device: $root_dev"
      ;;
  esac

  local candidates=()
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    candidates+=("${line%%|*}")
  done < <(list_boot_target_candidates "$root_dev")

  [[ "${#candidates[@]}" -gt 0 ]] || die "No boot target disks found (expected NVMe, microSD/eMMC, or USB mass storage)."
}

pick_boot_target_device() {
  local root_dev="$1"
  local entries=() configured candidate
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    entries+=("$line")
  done < <(list_boot_target_candidates "$root_dev")

  [[ "${#entries[@]}" -gt 0 ]] || die "No boot target disks found."

  configured="$(configured_boot_target_device)"
  if [[ -n "$configured" ]]; then
    [[ -b "$configured" ]] || die "Configured boot target is not a block device: $configured"
    if [[ "$configured" == "$root_dev" ]]; then
      die "Configured boot target '$configured' is the current root device; pick a different drive."
    fi
    for candidate in "${entries[@]}"; do
      if [[ "${candidate%%|*}" == "$configured" ]]; then
        echo "$configured"
        return 0
      fi
    done
    die "Configured boot target '$configured' is not one of the detected boot target disks."
  fi

  if [[ "${#entries[@]}" -eq 1 ]]; then
    local one_dev one_size one_model one_class
    IFS="|" read -r one_dev one_size one_model one_class _ <<< "${entries[0]}"
    log "Detected one boot target: ${one_dev} (${one_class}, ${one_size:-unknown}${one_model:+, ${one_model}})"
    echo "$one_dev"
    return 0
  fi

  log "Multiple boot target disks found:"
  local i line dev size model class tran rm
  for i in "${!entries[@]}"; do
    line="${entries[$i]}"
    IFS="|" read -r dev size model class tran rm <<< "$line"
    printf '  [%d] %s (%s, %s%s%s)\n' \
      "$i" \
      "$dev" \
      "${class:-block}" \
      "${size:-unknown size}" \
      "${model:+, ${model}}" \
      "${tran:+, transport=${tran}}" >&2
  done
  die "Multiple boot target disks were detected. Add boot_target_device to the answers JSON."
}

confirm_erase_device() {
  local dev="$1"
  local root_dev="$2"
  log "About to ERASE and overwrite the entire disk: $dev"
  log "This is destructive. All data on $dev will be lost."
  if [[ "$ANSWER_CONFIRM_ERASE" == "ERASE" ]]; then
    return 0
  fi

  local candidate_count=0 line
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    candidate_count=$((candidate_count + 1))
  done < <(list_boot_target_candidates "$root_dev")
  if [[ "$candidate_count" -le 1 ]]; then
    log "Single boot target detected; proceeding without explicit ERASE confirmation."
    ANSWER_CONFIRM_ERASE="ERASE"
    return 0
  fi

  die "Answers JSON did not confirm ERASE."
}

install_packages_host() {
  need_cmd apt-get
  log "Installing required packages on host..."
  export DEBIAN_FRONTEND=noninteractive
  run_with_apt_lock_retry "host apt-get update" apt-get update --error-on=any
  run_with_apt_lock_retry "host apt-get install" apt-get install -y --no-install-recommends \
    wget xz-utils openssl ca-certificates \
    util-linux coreutils gawk grep sed \
    parted \
    dosfstools e2fsprogs \
    iw network-manager \
    rpi-eeprom
}

download_image_xz() {
  local out_xz="$1"
  local url="https://downloads.raspberrypi.org/raspios_lite_arm64_latest"
  local meta="${out_xz}.meta"

  remote_meta() {
    wget -S --spider --max-redirect=20 "$url" 2>&1 | awk '
      BEGIN{etag=""; lm=""; len=""}
      {
        line=$0
        sub(/^[[:space:]]+/, "", line)
        if (tolower(substr(line,1,5))=="etag:") {
          sub(/^ETag:[[:space:]]*/, "", line)
          etag=line
        } else if (tolower(substr(line,1,14))=="last-modified:") {
          sub(/^Last-Modified:[[:space:]]*/, "", line)
          lm=line
        } else if (tolower(substr(line,1,7))=="length:") {
          n=split(line, a, /[[:space:]]+/)
          if (n>=2) len=a[2]
        }
      }
      END{
        print etag
        print lm
        print len
      }'
  }

  if [[ -f "$out_xz" && -f "$meta" ]]; then
    local old_etag old_lm old_len
    old_etag="$(sed -n '1p' "$meta" 2>/dev/null || true)"
    old_lm="$(sed -n '2p' "$meta" 2>/dev/null || true)"
    old_len="$(sed -n '3p' "$meta" 2>/dev/null || true)"

    local new_etag new_lm new_len
    {
      read -r new_etag
      read -r new_lm
      read -r new_len
    } < <(remote_meta || true)

    local local_len=""
    if command -v stat >/dev/null 2>&1; then
      local_len="$(stat -c%s "$out_xz" 2>/dev/null || true)"
    fi

    if [[ -n "$new_len" && -n "$old_len" && "$new_len" == "$old_len" && "$local_len" == "$new_len" ]] \
      && [[ -n "$new_etag" && -n "$old_etag" && "$new_etag" == "$old_etag" ]] \
      && [[ -n "$new_lm" && -n "$old_lm" && "$new_lm" == "$old_lm" ]]; then
      log "Local image is already the latest (ETag/Last-Modified/Length match). Skipping download."
      return 0
    fi
  fi

  log "Downloading latest Raspberry Pi OS Lite arm64 image (.xz) from $url ..."
  wget --progress=bar:force:noscroll -O "$out_xz" "$url"

  remote_meta > "$meta" 2>/dev/null || true
}

unmount_device_partitions() {
  local dev="$1"
  local mounts
  mounts="$(lsblk -nr -o MOUNTPOINT "$dev" 2>/dev/null || true)"
  if echo "$mounts" | grep -qE '.+'; then
    log "Unmounting any mounted partitions on $dev ..."
    while read -r mp; do
      [[ -n "$mp" ]] || continue
      umount "$mp" || true
    done < <(lsblk -nr -o MOUNTPOINT "$dev" | awk 'NF')
  fi
}

write_image_to_nvme() {
  local xz_path="$1"
  local dev="$2"

  need_cmd dd
  need_cmd xzcat
  unmount_device_partitions "$dev"

  log "Flashing image to $dev (this can take a while)..."
  xzcat "$xz_path" | dd of="$dev" bs=4M conv=fsync status=progress
  sync
}

wait_for_partitions() {
  local dev="$1"
  need_cmd udevadm
  need_cmd partprobe

  partprobe "$dev" || true
  udevadm settle || true

  local tries=40
  while [[ "$tries" -gt 0 ]]; do
    if lsblk -pn -o NAME "$dev" | grep -qE "${dev}p?[0-9]+"; then
      return 0
    fi
    sleep 0.25
    tries=$((tries-1))
  done
  die "Timed out waiting for partitions to appear on $dev"
}

expand_nvme_root_partition() {
  local dev="$1"
  local root_part="$2"
  need_cmd parted
  need_cmd partprobe
  need_cmd udevadm
  need_cmd e2fsck
  need_cmd resize2fs

  log "Expanding NVMe root partition to fill disk..."
  parted -s "$dev" resizepart 2 100% || die "Failed to resize partition 2 on $dev"
  partprobe "$dev" || true
  udevadm settle || true

  e2fsck -fy "$root_part" || die "Filesystem check failed on $root_part"
  resize2fs "$root_part" || die "Failed to resize filesystem on $root_part"
}

fsck_nvme_partitions() {
  local boot_part="$1"
  local root_part="$2"
  if [[ -b "$root_part" ]]; then
    if command -v e2fsck >/dev/null 2>&1; then
      e2fsck -fy "$root_part" >/dev/null 2>&1 || die "Filesystem check failed on $root_part"
    fi
  fi
  if [[ -b "$boot_part" ]]; then
    if command -v fsck.vfat >/dev/null 2>&1; then
      fsck.vfat -a "$boot_part" >/dev/null 2>&1 || true
    fi
  fi
}

find_nvme_partition() {
  local dev="$1"
  local want="$2"
  local out=""

  while read -r line; do
    [[ -n "$line" ]] || continue
    local NAME="" LABEL="" PARTLABEL=""
    eval "$line"
    local name="${NAME:-}" label="${LABEL:-}" partlabel="${PARTLABEL:-}"
    [[ -n "$name" ]] || continue
    case "$want" in
      boot)
        if [[ "$label" == "bootfs" || "$partlabel" == "bootfs" || "$label" == "boot" || "$partlabel" == "boot" ]]; then
          out="$name"; break
        fi
        ;;
      root)
        if [[ "$label" == "rootfs" || "$partlabel" == "rootfs" ]]; then
          out="$name"; break
        fi
        ;;
      *)
        die "Unknown partition type requested: $want"
        ;;
    esac
  done < <(lsblk -pn -P -o NAME,LABEL,PARTLABEL "$dev")

  if [[ -n "$out" ]]; then
    echo "$out"
    return 0
  fi

  case "$want" in
    boot) partition_path_for_disk "$dev" 1 ;;
    root) partition_path_for_disk "$dev" 2 ;;
  esac
}

partition_path_for_disk() {
  local dev="$1"
  local part_num="$2"
  case "$dev" in
    /dev/mmcblk*|/dev/nvme*)
      echo "${dev}p${part_num}"
      ;;
    *)
      echo "${dev}${part_num}"
      ;;
  esac
}

write_nm_wifi_profiles() {
  local root_mnt="$1"
  local answers_file="${2:-}"
  local legacy_ssid="${3:-}"
  local legacy_pass="${4:-}"
  local legacy_hidden="${5:-}"
  need_cmd python3
  ROOT_MNT="$root_mnt" ANSWERS_FILE="$answers_file" \
    LEGACY_SSID="$legacy_ssid" LEGACY_PASS="$legacy_pass" LEGACY_HIDDEN="$legacy_hidden" \
    python3 - <<'PY'
import json
import os
import pathlib

root = pathlib.Path(os.environ["ROOT_MNT"])
nm_dir = root / "etc/NetworkManager/system-connections"
nm_dir.mkdir(parents=True, exist_ok=True)


def hidden_bool(raw):
    if isinstance(raw, bool):
        return raw
    s = str(raw or "").strip().lower()
    return s in ("true", "yes", "1")


def load_networks():
    path = (os.environ.get("ANSWERS_FILE") or "").strip()
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            wn = data.get("wifi_networks")
            if isinstance(wn, list) and wn:
                out = []
                for item in wn:
                    if not isinstance(item, dict):
                        continue
                    ssid = str(item.get("ssid", "")).strip()
                    if not ssid:
                        continue
                    out.append(
                        (
                            ssid,
                            str(item.get("password") or ""),
                            hidden_bool(item.get("hidden")),
                        )
                    )
                if out:
                    return out
    ssid = (os.environ.get("LEGACY_SSID") or "").strip()
    if not ssid:
        return []
    return [
        (
            ssid,
            os.environ.get("LEGACY_PASS") or "",
            hidden_bool(os.environ.get("LEGACY_HIDDEN")),
        )
    ]


def sanitize_filename(stem, idx):
    safe = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in stem)
    safe = "_".join(safe.split()) or "wifi"
    return f"hb-wifi-{idx}-{safe}"


for idx, (ssid, pw, is_hidden) in enumerate(load_networks(), start=1):
    fname = sanitize_filename(ssid, idx) + ".nmconnection"
    nm_path = nm_dir / fname
    lines = [
        "[connection]",
        f"id={ssid}",
        "type=wifi",
        "autoconnect=true",
        "",
        "[wifi]",
        "mode=infrastructure",
        f"ssid={ssid}",
    ]
    if is_hidden:
        lines.append("hidden=true")
    lines.extend(
        [
            "",
            "[wifi-security]",
            "key-mgmt=wpa-psk",
            f"psk={pw}",
            "",
            "[ipv4]",
            "method=auto",
            "",
            "[ipv6]",
            "method=auto",
            "",
        ]
    )
    nm_path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(nm_path, 0o600)
    os.chown(nm_path, 0, 0)
PY
}

write_headless_config() {
  local boot_mnt="$1"
  local root_mnt="$2"
  local username="$3"
  local password="$4"
  local wifi_ssid="$5"
  local wifi_pass="$6"
  local wifi_hidden_raw="$7"
  local hostname="$8"
  local wifi_country="$9"
  local timezone="${10}"
  local locale="${11}"
  local screen_w="${12}"
  local screen_h="${13}"
  local screen_r="${14}"
  local screen_rot="${15}"
  local answers_file="${16:-}"

  log "Configuring NVMe OS (SSH/user/Wi-Fi)..."

  : > "${boot_mnt}/ssh"

  local pw_hash
  pw_hash="$(openssl passwd -6 "$password")"
  printf '%s:%s\n' "$username" "$pw_hash" > "${boot_mnt}/userconf.txt"

  local cfg="${boot_mnt}/config.txt"
  if [[ -f "$cfg" ]]; then
    if ! grep -qE '^\s*dtparam=pciex1(=on)?\s*$' "$cfg"; then
      echo "dtparam=pciex1=on" >> "$cfg"
    fi
  else
    log "WARNING: Did not find config.txt on boot partition ($cfg)."
  fi

  if [[ -f "$cfg" ]]; then
    if ! grep -qE '^\s*dtparam=ant2\s*$' "$cfg"; then
      echo "dtparam=ant2" >> "$cfg"
    fi
  fi

  if [[ -f "$cfg" ]]; then
    if grep -qE '^\s*camera_auto_detect=' "$cfg"; then
      sed -i -E 's/^\s*camera_auto_detect=.*/camera_auto_detect=0/' "$cfg"
    else
      echo "camera_auto_detect=0" >> "$cfg"
    fi
    if ! grep -qE '^\s*dtoverlay=imx708\s*$' "$cfg"; then
      if grep -qE '^\s*\[all\]\s*$' "$cfg"; then
        sed -i -E '/^\s*\[all\]\s*$/a dtoverlay=imx708' "$cfg"
      else
        echo "[all]" >> "$cfg"
        echo "dtoverlay=imx708" >> "$cfg"
      fi
    fi
  fi

  if [[ -n "$wifi_country" ]]; then
    local cmdline=""
    if [[ -f "${boot_mnt}/cmdline.txt" ]]; then
      cmdline="${boot_mnt}/cmdline.txt"
    elif [[ -f "${boot_mnt}/firmware/cmdline.txt" ]]; then
      cmdline="${boot_mnt}/firmware/cmdline.txt"
    fi

    if [[ -n "$cmdline" ]]; then
      if grep -qE '(^|[[:space:]])cfg80211\.ieee80211_regdom=[A-Z]{2}([[:space:]]|$)' "$cmdline"; then
        sed -i -E "s/(^|[[:space:]])cfg80211\\.ieee80211_regdom=[A-Z]{2}([[:space:]]|$)/\\1cfg80211.ieee80211_regdom=${wifi_country}\\2/" "$cmdline"
      else
        sed -i -e "1 s/$/ cfg80211.ieee80211_regdom=${wifi_country}/" "$cmdline"
      fi
    else
      log "WARNING: Could not find cmdline.txt on boot partition to set Wi-Fi country code."
    fi
  fi

  local cmdline_rotate=""
  if [[ -f "${boot_mnt}/cmdline.txt" ]]; then
    cmdline_rotate="${boot_mnt}/cmdline.txt"
  elif [[ -f "${boot_mnt}/firmware/cmdline.txt" ]]; then
    cmdline_rotate="${boot_mnt}/firmware/cmdline.txt"
  fi
  if [[ -n "$cmdline_rotate" && -n "$screen_w" && -n "$screen_h" && -n "$screen_r" ]]; then
    local rotate_token="video=HDMI-A-1:${screen_w}x${screen_h}M@${screen_r},rotate=${screen_rot:-0}"
    if grep -qE '(^|[[:space:]])video=HDMI-A-1:[^[:space:]]+' "$cmdline_rotate"; then
      sed -i -E "s/(^|[[:space:]])video=HDMI-A-1:[^[:space:]]+/\1${rotate_token}/" "$cmdline_rotate"
    else
      sed -i -e "1 s/$/ ${rotate_token}/" "$cmdline_rotate"
    fi
  elif [[ -z "$cmdline_rotate" ]]; then
    log "WARNING: Could not find cmdline.txt on boot partition to set display mode."
  fi

  write_nm_wifi_profiles "$root_mnt" "${16:-}" "$wifi_ssid" "$wifi_pass" "$wifi_hidden_raw"

  local nm_state_dir="${root_mnt}/var/lib/NetworkManager"
  mkdir -p "$nm_state_dir"
  cat > "${nm_state_dir}/NetworkManager.state" <<'EOF'
[main]
NetworkingEnabled=true
WirelessEnabled=true
WWANEnabled=true
EOF
  chmod 600 "${nm_state_dir}/NetworkManager.state"
  chown root:root "${nm_state_dir}/NetworkManager.state"

  if [[ -f /etc/udev/rules.d/99-touchscreen-rotate.rules ]]; then
    mkdir -p "${root_mnt}/etc/udev/rules.d"
    cp /etc/udev/rules.d/99-touchscreen-rotate.rules "${root_mnt}/etc/udev/rules.d/99-touchscreen-rotate.rules"
  fi

  if [[ -n "$hostname" ]]; then
    echo "$hostname" > "${root_mnt}/etc/hostname"
    if [[ -f "${root_mnt}/etc/hosts" ]]; then
      if grep -qE '^\s*127\.0\.1\.1\s+' "${root_mnt}/etc/hosts"; then
        sed -i -E "s/^\s*127\.0\.1\.1\s+.*/127.0.1.1\t${hostname}/" "${root_mnt}/etc/hosts"
      else
        printf '\n127.0.1.1\t%s\n' "$hostname" >> "${root_mnt}/etc/hosts"
      fi
    else
      log "WARNING: ${root_mnt}/etc/hosts not found; hostname may not fully apply."
    fi
  fi

  if [[ -n "$timezone" ]]; then
    echo "$timezone" > "${root_mnt}/etc/timezone"
    ln -sfn "/usr/share/zoneinfo/${timezone}" "${root_mnt}/etc/localtime"
  fi

  if [[ -n "$locale" ]]; then
    local locale_gen="${root_mnt}/etc/locale.gen"
    echo "${locale} UTF-8" > "$locale_gen"
    echo "LANG=${locale}" > "${root_mnt}/etc/default/locale"
  fi

  if [[ -n "$locale" ]]; then
    local kb_layout=""
    case "$locale" in
      en_US.UTF-8) kb_layout="us" ;;
      en_GB.UTF-8) kb_layout="gb" ;;
    esac
    if [[ -n "$kb_layout" ]]; then
      local kb_file="${root_mnt}/etc/default/keyboard"
      if [[ -f "$kb_file" ]]; then
        if grep -qE '^XKBLAYOUT=' "$kb_file"; then
          sed -i -E "s/^XKBLAYOUT=.*/XKBLAYOUT=\"${kb_layout}\"/" "$kb_file"
        else
          echo "XKBLAYOUT=\"${kb_layout}\"" >> "$kb_file"
        fi
      else
        cat > "$kb_file" <<EOF
XKBLAYOUT="${kb_layout}"
EOF
      fi
    fi
  fi
}

ensure_user_exists_root() {
  local root_mnt="$1"
  local username="$2"
  local password="$3"

  [[ -n "$username" && -n "$password" ]] || return 0

  mount_chroot_env "$root_mnt"
  local chroot_env=(/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root DEBIAN_FRONTEND=noninteractive)
  if ! chroot "$root_mnt" "${chroot_env[@]}" /usr/bin/id -u "$username" >/dev/null 2>&1; then
    chroot "$root_mnt" "${chroot_env[@]}" /usr/sbin/useradd -m -s /bin/bash "$username" || true
    chroot "$root_mnt" "${chroot_env[@]}" /usr/sbin/usermod -aG sudo "$username" || true
    printf '%s:%s\n' "$username" "$password" \
      | chroot "$root_mnt" "${chroot_env[@]}" /usr/sbin/chpasswd || true
  fi
  unmount_chroot_env "$root_mnt"
}

mount_nvme_partitions_for_config() {
  local boot_part="$1"
  local root_part="$2"
  local boot_mnt="$3"
  local root_mnt="$4"

  mkdir -p "$boot_mnt" "$root_mnt"
  mount "$root_part" "$root_mnt"
  mount "$boot_part" "$boot_mnt"
}

cleanup_mounts() {
  local boot_mnt="$1"
  local root_mnt="$2"

  sync || true
  if [[ -n "${boot_mnt:-}" ]]; then
    umount "$boot_mnt" 2>/dev/null || true
  fi
  if [[ -n "${root_mnt:-}" ]]; then
    umount "$root_mnt" 2>/dev/null || true
  fi
}

mount_chroot_env() {
  local root_mnt="$1"
  local chroot_resolv="${root_mnt}/etc/resolv.conf"
  local systemd_real="/run/systemd/resolve/resolv.conf"
  local flattened=0

  mount --bind /dev "${root_mnt}/dev"
  mount --bind /dev/pts "${root_mnt}/dev/pts"
  mount -t proc proc "${root_mnt}/proc"
  mount -t sysfs sys "${root_mnt}/sys"

  # Prefer a flattened resolv.conf: real upstream nameservers. Host /etc/resolv.conf often
  # points at 127.0.0.53 or symlinks into /run/systemd/resolve/; the chroot has a different
  # /run tree, so bind-mounting the stub symlink can break DNS. systemd publishes upstreams
  # in /run/systemd/resolve/resolv.conf when systemd-resolved is active.
  if [[ -r "$systemd_real" ]] \
    && awk '/^nameserver[[:space:]]+/ && $2 != "127.0.0.53" { ok=1 } END { exit !ok }' "$systemd_real" 2>/dev/null; then
    if cp -- "$systemd_real" "$chroot_resolv" 2>/dev/null; then
      chmod 644 "$chroot_resolv" || true
      log "Chroot DNS: using flattened resolv.conf from $systemd_real."
      flattened=1
    else
      log "WARNING: Could not copy $systemd_real to $chroot_resolv; will try bind-mounting host /etc/resolv.conf."
    fi
  fi

  if [[ "$flattened" -eq 0 ]]; then
    if [[ -f /etc/resolv.conf ]] || [[ -L /etc/resolv.conf ]]; then
      if ! mount --bind /etc/resolv.conf "$chroot_resolv"; then
        log "WARNING: Could not bind-mount host /etc/resolv.conf into chroot; apt/git inside chroot may fail DNS resolution."
      fi
    else
      log "WARNING: Host has no /etc/resolv.conf; chroot DNS may fail (no bind mount)."
    fi
  fi
}

unmount_chroot_env() {
  local root_mnt="$1"
  umount "${root_mnt}/etc/resolv.conf" 2>/dev/null || true
  umount "${root_mnt}/sys" 2>/dev/null || true
  umount "${root_mnt}/proc" 2>/dev/null || true
  umount "${root_mnt}/dev/pts" 2>/dev/null || true
  umount "${root_mnt}/dev" 2>/dev/null || true
}

# Swap file on the running system's root filesystem (typically eMMC during NVMe provisioning).
# Stacks with zram; helps heavy host/chroot apt. Appends to /etc/fstab so swap persists after reboot.
ensure_host_persistent_swap() {
  local swap_mb="${HB_EMMC_SWAP_MB:-2048}"
  local swap_path="${HB_EMMC_SWAP_PATH:-/var/swap/hb_provision.swap}"
  local swap_dir free_kb need_kb fstab="/etc/fstab"

  [[ "${swap_mb:-0}" -gt 0 ]] || return 0
  need_cmd swapon
  need_cmd mkswap

  if swapon --show 2>/dev/null | awk -v p="$swap_path" 'NR > 1 && $1 == p { found = 1 } END { exit !found }'; then
    log "Host swap file already active: $swap_path"
    return 0
  fi

  swap_dir="$(dirname "$swap_path")"
  mkdir -p "$swap_dir" || {
    log "WARNING: Could not create directory for host swap: $swap_dir"
    return 0
  }

  need_kb=$((swap_mb * 1024 + 256 * 1024))
  free_kb="$(df -Pk "$swap_dir" 2>/dev/null | awk 'NR==2 {print $4}')"
  if [[ "${free_kb:-0}" -lt "$need_kb" ]]; then
    log "WARNING: Not enough disk space under $swap_dir for ${swap_mb}MiB host swap (need ~$((need_kb / 1024))MiB free; have ${free_kb:-0}KiB)."
    return 0
  fi

  if [[ ! -f "$swap_path" ]]; then
    log "Creating ${swap_mb}MiB host swap file at $swap_path (rootfs; typically eMMC)..."
    if ! fallocate -l "${swap_mb}M" "$swap_path" 2>/dev/null; then
      dd if=/dev/zero of="$swap_path" bs=1M count="$swap_mb" status=none || {
        log "WARNING: Failed to allocate host swap file $swap_path."
        rm -f "$swap_path" 2>/dev/null || true
        return 0
      }
    fi
    chmod 600 "$swap_path" || true
  fi

  if ! mkswap "$swap_path" >/dev/null 2>&1; then
    log "WARNING: mkswap failed for $swap_path."
    return 0
  fi
  if ! swapon "$swap_path" 2>/dev/null; then
    log "WARNING: swapon failed for $swap_path."
    return 0
  fi
  log "Host swap file active: $swap_path (${swap_mb}MiB)."

  if [[ -f "$fstab" ]] && ! awk -v p="$swap_path" '$1 == p { exit 0 } END { exit 1 }' "$fstab"; then
    printf '%s none swap sw 0 0\n' "$swap_path" >>"$fstab"
    log "Recorded host swap in /etc/fstab (persists after reboot)."
  fi
}

# Temporary swap for the host during chroot apt: full-upgrade + labwc/wlroots can spike RAM
# and the child apt/dpkg process is often SIGKILL'd by the OOM killer ("Killed" in logs).
# Override with e.g. HB_CHROOT_APT_MIN_MEM_KB=999999 to always add swap, or HB_CHROOT_APT_SWAP_MB=0 to disable.
HB_CHROOT_APT_MIN_MEM_KB="${HB_CHROOT_APT_MIN_MEM_KB:-1800000}"
HB_CHROOT_APT_SWAP_MB="${HB_CHROOT_APT_SWAP_MB:-2048}"
HB_CHROOT_APT_SWAP_PATH="${HB_CHROOT_APT_SWAP_PATH:-/var/tmp/hb_nvme_chroot_apt.swap}"
HB_HOST_PROVISION_SWAP_ON=0

host_provision_swap_deactivate() {
  if [[ "${HB_HOST_PROVISION_SWAP_ON:-0}" != "1" ]]; then
    return 0
  fi
  local p="${HB_CHROOT_APT_SWAP_PATH:-}"
  [[ -n "$p" ]] || return 0
  log "Removing temporary provisioning swap: $p"
  swapoff "$p" 2>/dev/null || true
  rm -f "$p" 2>/dev/null || true
  HB_HOST_PROVISION_SWAP_ON=0
}

host_provision_swap_activate_if_needed() {
  local min_kb="${HB_CHROOT_APT_MIN_MEM_KB:-1800000}"
  local swap_mb="${HB_CHROOT_APT_SWAP_MB:-2048}"
  local swap_path="${HB_CHROOT_APT_SWAP_PATH:-/var/tmp/hb_nvme_chroot_apt.swap}"
  local avail_kb free_kb need_kb

  [[ "${swap_mb:-0}" -gt 0 ]] || return 0
  [[ -r /proc/meminfo ]] || return 0
  avail_kb="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0)"
  if [[ "${avail_kb:-0}" -ge "$min_kb" ]]; then
    return 0
  fi

  if [[ -f "$swap_path" ]]; then
    if swapon "$swap_path" 2>/dev/null; then
      HB_HOST_PROVISION_SWAP_ON=1
      log "Enabled existing provisioning swap $swap_path (MemAvailable ${avail_kb}KiB < ${min_kb}KiB)."
      return 0
    fi
    rm -f "$swap_path" 2>/dev/null || true
  fi

  need_kb=$((swap_mb * 1024 + 512 * 1024))
  free_kb="$(df -Pk "$(dirname "$swap_path")" 2>/dev/null | awk 'NR==2 {print $4}')"
  if [[ "${free_kb:-0}" -lt "$need_kb" ]]; then
    log "WARNING: Not enough free disk under $(dirname "$swap_path") for ${swap_mb}MiB swap (need ~$((need_kb / 1024))MiB; have ${free_kb:-0}KiB). Chroot apt may be killed on low-memory hosts."
    return 0
  fi

  log "Host MemAvailable is ${avail_kb}KiB (below ${min_kb}KiB). Creating ${swap_mb}MiB swap at $swap_path for chroot apt..."
  if ! fallocate -l "${swap_mb}M" "$swap_path" 2>/dev/null; then
    dd if=/dev/zero of="$swap_path" bs=1M count="$swap_mb" status=none || {
      log "WARNING: Failed to allocate swap file $swap_path."
      return 0
    }
  fi
  chmod 600 "$swap_path" || true
  if ! mkswap "$swap_path" >/dev/null 2>&1 || ! swapon "$swap_path" 2>/dev/null; then
    log "WARNING: Failed to mkswap/swapon $swap_path."
    rm -f "$swap_path" 2>/dev/null || true
    return 0
  fi
  HB_HOST_PROVISION_SWAP_ON=1
  log "Temporary provisioning swap active."
}

configure_nvme_packages_and_services() {
  local root_mnt="$1"
  local locale="$2"
  log "Configuring packages/services in NVMe OS (apt upgrade, dev packages, disable bluetooth)..."
  mount_chroot_env "$root_mnt"
  trap 'host_provision_swap_deactivate; unmount_chroot_env "'"$root_mnt"'"' RETURN
  local chroot_env=(/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root DEBIAN_FRONTEND=noninteractive)

  chroot_cmd() {
    chroot "$root_mnt" "${chroot_env[@]}" "$@"
  }

  chroot_apt_get() {
    run_with_apt_lock_retry "NVMe rootfs apt-get $*" chroot_cmd /usr/bin/apt-get \
      -o APT::Install-Recommends=false \
      "$@"
  }

  host_provision_swap_activate_if_needed

  if ! chroot_apt_get update --error-on=any \
    || ! chroot_apt_get -y full-upgrade \
    || ! chroot_apt_get -y clean; then
    log "WARNING: apt full-upgrade failed in NVMe rootfs. Attempting recovery..."
    chroot_cmd /usr/bin/dpkg --configure -a || true
    chroot_apt_get -y -f install || true
    chroot_apt_get update --error-on=any \
      && chroot_apt_get -y full-upgrade \
      && chroot_apt_get -y clean \
      || die "Failed to run apt update/full-upgrade in NVMe rootfs."
  fi

  # Install in batches to lower peak memory (wlroots/labwc unpack is heavy).
  chroot_apt_get install -y \
    locales ca-certificates curl jq unzip wget git screen \
    || die "Failed to install base packages in NVMe rootfs."
  chroot_apt_get install -y \
    build-essential cmake libevdev-dev libpq-dev \
    || die "Failed to install build packages in NVMe rootfs."
  chroot_apt_get install -y \
    libcamera-apps libtcl9.0 raspi-config lightdm seatd cage labwc \
    || die "Failed to install desktop/kiosk packages in NVMe rootfs."

  if [[ -n "$locale" ]]; then
    if ! chroot_cmd /usr/sbin/locale-gen "$locale"; then
      log "WARNING: Failed to generate locale '$locale' in NVMe rootfs."
    else
      chroot_cmd /usr/sbin/update-locale "LANG=${locale}" || log "WARNING: Failed to update locale in NVMe rootfs."
    fi
  fi

  if [[ -x "${root_mnt}/bin/systemctl" ]]; then
    chroot_cmd /bin/systemctl disable bluetooth || log "WARNING: Failed to disable bluetooth in NVMe rootfs."
    chroot_cmd /bin/systemctl stop bluetooth || log "WARNING: Failed to stop bluetooth in NVMe rootfs."
    SYSTEMD_OFFLINE=1 systemctl --root "$root_mnt" enable seatd.service || log "WARNING: Failed to enable seatd in NVMe rootfs."
  fi

  host_provision_swap_deactivate
  unmount_chroot_env "$root_mnt"
  trap - RETURN
}

enable_systemd_service_root() {
  local root_mnt="$1"
  local rel_path="$2"
  local source="${root_mnt}${rel_path}"
  local service_name
  service_name="$(basename "$rel_path")"

  if [[ ! -f "$source" ]]; then
    log "WARNING: Missing service file in NVMe rootfs: $rel_path"
    return 0
  fi

  install -m 0644 "$source" "${root_mnt}/etc/systemd/system/${service_name}"
  if command -v systemctl >/dev/null 2>&1; then
    SYSTEMD_OFFLINE=1 systemctl --root "$root_mnt" enable "$service_name" || true
  fi
}

write_stim2_service_override_root() {
  local root_mnt="$1"
  local override_dir="${root_mnt}/etc/systemd/system/stim2.service.d"
  local override_file="${override_dir}/override.conf"

  mkdir -p "$override_dir"
  cat > "$override_file" <<'EOF'
[Unit]
After=systemd-logind.service seatd.service
Wants=seatd.service

[Service]
Environment=LIBSEAT_BACKEND=seatd
ExecStartPre=/bin/bash -c 'for i in $(seq 1 30); do [ -e /dev/dri/card0 ] && exit 0; sleep 1; done; exit 1'
EOF
}

write_dserv_agent_override_root() {
  local root_mnt="$1"
  [[ -n "$DEFAULT_MESH_HOST" && -n "$DEFAULT_MESH_WORKGROUP" ]] || return 0

  local registry_url="${DEFAULT_MESH_HOST%/}"
  local workgroup="${DEFAULT_MESH_WORKGROUP//./-}"
  local override_dir="${root_mnt}/etc/systemd/system/dserv-agent.service.d"
  local override_file="${override_dir}/override.conf"

  mkdir -p "$override_dir"
  cat > "$override_file" <<EOF
[Service]
ExecStart=
ExecStart=/usr/local/bin/dserv-agent --no-tls -allow-reboot -components /usr/local/dserv/config/components.json --registry ${registry_url} --workgroup ${workgroup}
EOF
}

write_monitor_tcl_root() {
  local root_mnt="$1"
  local monitor_dir monitor_file
  monitor_dir="${root_mnt}/usr/local/stim2/local"
  monitor_file="${monitor_dir}/monitor.tcl"

  mkdir -p "$monitor_dir"
  cat >"$monitor_file" <<EOF
# Monitor-specific settings
screen_set ScreenWidthCm       ${MONITOR_WIDTH_CM}
screen_set ScreenHeightCm      ${MONITOR_HEIGHT_CM}
screen_set DistanceToMonitor   ${MONITOR_DISTANCE_CM}
EOF
}

configure_dserv_local_tcl_root() {
  local root_mnt="$1"

  if [[ -f "${root_mnt}/usr/local/dserv/local/post-pins.tcl.EXAMPLE" ]]; then
    cp -n "${root_mnt}/usr/local/dserv/local/post-pins.tcl.EXAMPLE" "${root_mnt}/usr/local/dserv/local/post-pins.tcl" || true
  fi
  if [[ -f "${root_mnt}/usr/local/dserv/local/sound.tcl.EXAMPLE" ]]; then
    cp -n "${root_mnt}/usr/local/dserv/local/sound.tcl.EXAMPLE" "${root_mnt}/usr/local/dserv/local/sound.tcl" || true
  fi
  if [[ -f "${root_mnt}/usr/local/dserv/local/mesh.tcl.EXAMPLE" ]]; then
    local mesh_target="${root_mnt}/usr/local/dserv/local/mesh.tcl"
    cp -n "${root_mnt}/usr/local/dserv/local/mesh.tcl.EXAMPLE" "$mesh_target" || true
    if [[ -n "$DEFAULT_MESH_HOST" && -n "$DEFAULT_MESH_WORKGROUP" && -f "$mesh_target" ]]; then
      local mesh_workgroup="${DEFAULT_MESH_WORKGROUP//./-}"
      sed -i -E "s|^mesh_configure[[:space:]]+\"[^\"]*\"[[:space:]]+\"[^\"]*\"|mesh_configure \"${DEFAULT_MESH_HOST}\" \"${mesh_workgroup}\"|" "$mesh_target"
    fi
  fi
  if [[ -f "${root_mnt}/usr/local/dserv/local/pre-registry.tcl.EXAMPLE" ]]; then
    local pre_registry_target="${root_mnt}/usr/local/dserv/local/pre-registry.tcl"
    cp -n "${root_mnt}/usr/local/dserv/local/pre-registry.tcl.EXAMPLE" "$pre_registry_target" || true
    if [[ -n "$DEFAULT_MESH_HOST" && -n "$DEFAULT_MESH_WORKGROUP" && -f "$pre_registry_target" ]]; then
      local pre_registry_workgroup="${DEFAULT_MESH_WORKGROUP//./-}"
      sed -i -E "s|^set env\\(ESS_REGISTRY_URL\\)[[:space:]]+.*|set env(ESS_REGISTRY_URL) ${DEFAULT_MESH_HOST}|" "$pre_registry_target"
      sed -i -E "s|^set env\\(ESS_WORKGROUP\\)[[:space:]]+.*|set env(ESS_WORKGROUP)    ${pre_registry_workgroup}|" "$pre_registry_target"
    fi
  fi
}

install_dserv_stack_root() {
  local root_mnt="$1"
  local registry_url="${DEFAULT_MESH_HOST:-https://dserv.net}"
  local workgroup="${DEFAULT_MESH_WORKGROUP:-brown-sheinberg}"
  local bootstrap_workgroup="${workgroup//./-}"
  registry_url="${registry_url%/}"

  log "Installing dserv stack from ${registry_url}/setup for workgroup '${bootstrap_workgroup}'..."

  mount_chroot_env "$root_mnt"
  trap 'unmount_chroot_env "'"$root_mnt"'"' RETURN
  local chroot_env=(/usr/bin/env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root DEBIAN_FRONTEND=noninteractive DSERV_BOOTSTRAP_FORCE=1)
  chroot "$root_mnt" "${chroot_env[@]}" /bin/bash -c \
    'curl -sSL "$1" | bash -s -- --workgroup "$2"' \
    _ "${registry_url}/setup" "$bootstrap_workgroup" \
    || die "Failed to install dserv stack in NVMe rootfs."
  unmount_chroot_env "$root_mnt"
  trap - RETURN

  configure_dserv_local_tcl_root "$root_mnt"
}

sync_trial_ingest_secret_to_nvme_root() {
  local root_mnt="$1"
  local host_file="$HB_TRIAL_INGEST_SECRET"
  local dest="${root_mnt}${HB_TRIAL_INGEST_SECRET}"

  if [[ ! -f "$host_file" ]]; then
    log "WARNING: Host trial ingest secret not found at ${host_file}; skipping copy to NVMe rootfs."
    return 0
  fi

  local line tmp
  line="$(head -n1 "$host_file" | tr -d '\r')"
  if [[ -z "$line" ]]; then
    log "WARNING: Host trial ingest secret at ${host_file} is empty; skipping copy to NVMe rootfs."
    return 0
  fi

  tmp="$(mktemp -p /tmp hb_trial_secret.XXXXXX)"
  chmod 0600 "$tmp" || true
  printf '%s\n' "$line" > "$tmp"

  install -d -m 0755 -o root -g root "${root_mnt}/etc/dserv"
  install -m 0600 -o root -g root "$tmp" "$dest"
  rm -f "$tmp"
  log "Installed trial ingest secret from host into NVMe rootfs."
}

configure_trial_ingest_pre_remoteservers_root() {
  local root_mnt="$1"
  local dir="${root_mnt}/usr/local/dserv/local"
  local target="${dir}/pre-remoteservers.tcl"
  local example="${dir}/pre-remoteservers.tcl.EXAMPLE"

  if [[ "${ANSWER_CLOUD_TRIAL_INGEST:-false}" != "true" ]]; then
    return 0
  fi

  install -d -m 0755 -o root -g root "$dir" || true

  if [[ ! -f "$target" ]]; then
    if [[ -f "$example" ]]; then
      if ! cp "$example" "$target"; then
        log "WARNING: Could not copy ${example} to ${target}; skipping trial ingest URL config."
        return 0
      fi
    else
      log "WARNING: ${example} not found; creating empty ${target} for trial ingest line."
      : >"$target" || true
    fi
  fi

  local defaults_file="${DEFAULTS_FILE:-}"
  if [[ -z "$defaults_file" || ! -r "$defaults_file" ]]; then
    local script_path script_dir
    script_path="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"
    script_dir="$(cd "$(dirname "$script_path")" && pwd -P)"
    defaults_file="${script_dir}/device_defaults.ini"
  fi

  local group="${ANSWER_DEFAULTS_GROUP:-}"
  if [[ -z "$group" && -n "${DEFAULTS_SECTION:-}" ]]; then
    group="${DEFAULTS_SECTION%.*}"
  fi
  if [[ -z "$group" ]]; then
    log "WARNING: No defaults group available; skipping trial ingest URL in pre-remoteservers.tcl."
    return 0
  fi

  local cloud_ingest_url
  cloud_ingest_url="$(cloud_ingest_url_for_defaults_group "$group" "$defaults_file")"
  if [[ -z "$cloud_ingest_url" ]]; then
    log "WARNING: cloud_ingest not set for section ${group} in ${defaults_file}; skipping trial ingest URL in pre-remoteservers.tcl."
    return 0
  fi

  if grep -qE '^[[:space:]]*dservSet[[:space:]]+configs/trial_ingest_base_url' "$target" 2>/dev/null; then
    return 0
  fi

  if [[ -s "$target" ]] && [[ "$(tail -c1 "$target" 2>/dev/null)" != $'\n' ]]; then
    printf '\n' >>"$target" || true
  fi
  cat >>"$target" <<EOF
dservSet configs/trial_ingest_base_url {${cloud_ingest_url}}
EOF
  log "Appended trial ingest base URL (${cloud_ingest_url}) to pre-remoteservers.tcl"
}

install_ess_repo_root() {
  local root_mnt="$1"
  local username="$2"
  local systems_dir="/home/${username}/systems"
  local ess_data_dir="/usr/data/essdat"
  local ess_converted_dir="/usr/data/converted"

  mkdir -p "${root_mnt}${systems_dir}"
  mount_chroot_env "$root_mnt"
  local chroot_env=(/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root DEBIAN_FRONTEND=noninteractive)
  chroot "$root_mnt" "${chroot_env[@]}" /usr/bin/git -C "$systems_dir" clone "$ESS_SOURCE" ess || true
  chroot "$root_mnt" "${chroot_env[@]}" /usr/bin/git config --system --add safe.directory "${systems_dir}/ess" || true
  # Ensure the lab user owns the home directory for normal git usage.
  chroot "$root_mnt" "${chroot_env[@]}" /bin/chown -R "${username}:${username}" "/home/${username}" || true
  unmount_chroot_env "$root_mnt"

  mkdir -p "${root_mnt}/usr/local/dserv/local"
  cat > "${root_mnt}/usr/local/dserv/local/pre-systemdir.tcl" <<EOF
set env(ESS_SYSTEM_PATH) ${systems_dir%/}
set env(ESS_DATA_DIR)    ${ess_data_dir}
set env(ESS_EXPORT_PATH) ${ess_converted_dir}
EOF
}

configure_raspi_config_root() {
  local root_mnt="$1"
  if [[ ! -x "${root_mnt}/usr/bin/raspi-config" ]]; then
    log "WARNING: raspi-config not found in NVMe rootfs; skipping console/autologin/wayland setup."
    return 0
  fi

  mount_chroot_env "$root_mnt"
  local chroot_env=(/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root DEBIAN_FRONTEND=noninteractive)
  if ! chroot "$root_mnt" "${chroot_env[@]}" /usr/bin/raspi-config nonint do_boot_behaviour B2; then
    log "WARNING: raspi-config boot behaviour failed in NVMe rootfs."
  fi
  if ! chroot "$root_mnt" "${chroot_env[@]}" /usr/bin/raspi-config nonint do_wayland W1; then
    log "WARNING: raspi-config do_wayland W1 failed in NVMe rootfs."
  fi
  unmount_chroot_env "$root_mnt"
}

wait_for_gui_reboot_request() {
  rm -f "$HB_REBOOT_REQUEST_FILE"
  log "$HB_PROVISION_COMPLETE_MARKER"
  log "Click Reboot in the GUI to finish setup and start the newly installed system."

  while [[ ! -e "$HB_REBOOT_REQUEST_FILE" ]]; do
    sleep 1
  done

  rm -f "$HB_REBOOT_REQUEST_FILE"
  log "Reboot requested from GUI. Rebooting now..."
  reboot || die "Reboot command failed after GUI request."
}

main() {
  setup_logging
  parse_args "$@"
  require_root
  rm -f "$HB_REBOOT_REQUEST_FILE"
  load_answers_json "$ANSWERS_FILE"
  apply_answer_defaults_env
  load_defaults
  validate_answers

  ensure_host_persistent_swap

  HB_SELFUPDATE_NO_INTERNET=0
  if ! update_self_if_possible "pre"; then
    if [[ "$HB_SELFUPDATE_NO_INTERNET" == "1" ]]; then
      log "Self-update: will retry after internet connectivity is verified."
    fi
  fi

  local wifi_country timezone locale screen_w screen_h screen_r screen_rot
  local wifi_ssid="" wifi_pass="" wifi_hidden=""
  local hostname username password
  local root_src root_dev target_dev
  local monitor_width monitor_height monitor_distance

  wifi_country="$ANSWER_WIFI_COUNTRY"
  wifi_ssid="$ANSWER_WIFI_SSID"
  wifi_pass="$ANSWER_WIFI_PASSWORD"
  wifi_hidden="$ANSWER_WIFI_HIDDEN"

  if [[ "${ANSWER_CONNECTIVITY_CONTINUE_ANYWAY:-}" == "true" || "${ANSWER_CONNECTIVITY_CONTINUE_ANYWAY:-}" == "YES" ]]; then
    log "WARNING: Answers set connectivity_continue_anyway: failures from wait_for_internet will not abort this script."
    if ! wait_for_internet 30 3; then
      log "WARNING: Strict internet probes did not succeed within timeout; continuing anyway."
    fi
  elif ! wait_for_internet 30 3; then
    die "No internet connectivity after checking $(internet_probe_targets_text). Wi-Fi may be connected but blocked by a captive portal, firewall, or a route/DNS issue."
  fi
  log "Internet connectivity verified."
  warn_if_provision_critical_dns_fails

  if [[ "$HB_SELFUPDATE_NO_INTERNET" == "1" ]]; then
    log "Self-update: retrying now that internet connectivity is verified..."
    log "If an update is found, setup will restart so the newest provisioning script is used."
  fi
  update_self_if_possible "post" || true

  log_gui_accessory_checks
  log_live_accessory_checks

  timezone="$ANSWER_TIMEZONE"
  locale="$ANSWER_LOCALE"
  screen_w="$ANSWER_SCREEN_PIXELS_WIDTH"
  screen_h="$ANSWER_SCREEN_PIXELS_HEIGHT"
  screen_r="$ANSWER_SCREEN_REFRESH_RATE"
  screen_rot="$ANSWER_SCREEN_ROTATION"
  hostname="$ANSWER_HOSTNAME"
  username="$ANSWER_USERNAME"
  password="$ANSWER_PASSWORD"
  monitor_width="$ANSWER_MONITOR_WIDTH_CM"
  monitor_height="$ANSWER_MONITOR_HEIGHT_CM"
  monitor_distance="$ANSWER_MONITOR_DISTANCE_CM"

  check_bookworm_or_later
  root_src="$(root_source)"
  root_dev="$(strip_partition_suffix "$root_src")"
  check_root_on_fallback_and_target_present "$root_dev"
  target_dev="$(pick_boot_target_device "$root_dev")"
  [[ -b "$target_dev" ]] || die "Not a block device: $target_dev"
  if [[ "$target_dev" == "$root_dev" ]]; then
    die "Refusing to overwrite the current root device ($root_dev)."
  fi
  confirm_erase_device "$target_dev" "$root_dev"

  install_packages_host

  local xz_path="/tmp/raspios_lite_arm64_latest.img.xz"
  download_image_xz "$xz_path"
  write_image_to_nvme "$xz_path" "$target_dev"

  wait_for_partitions "$target_dev"
  local boot_part root_part
  boot_part="$(find_nvme_partition "$target_dev" boot)"
  root_part="$(find_nvme_partition "$target_dev" root)"
  [[ -b "$boot_part" ]] || die "Boot partition not found: $boot_part"
  [[ -b "$root_part" ]] || die "Root partition not found: $root_part"

  log "Target boot partition: $boot_part"
  log "Target root partition: $root_part"

  expand_nvme_root_partition "$target_dev" "$root_part"
  fsck_nvme_partitions "$boot_part" "$root_part"

  HB_BOOT_MNT="/mnt/hb_nvme_boot"
  HB_ROOT_MNT="/mnt/hb_nvme_root"
  trap 'cleanup_mounts "${HB_BOOT_MNT:-}" "${HB_ROOT_MNT:-}"' EXIT
  mount_nvme_partitions_for_config "$boot_part" "$root_part" "$HB_BOOT_MNT" "$HB_ROOT_MNT"

  MONITOR_WIDTH_CM="$monitor_width"
  MONITOR_HEIGHT_CM="$monitor_height"
  MONITOR_DISTANCE_CM="$monitor_distance"

  write_headless_config "$HB_BOOT_MNT" "$HB_ROOT_MNT" "$username" "$password" "$wifi_ssid" "$wifi_pass" "$wifi_hidden" "$hostname" "$wifi_country" "$timezone" "$locale" "$screen_w" "$screen_h" "$screen_r" "$screen_rot" "$ANSWERS_FILE"
  ensure_user_exists_root "$HB_ROOT_MNT" "$username" "$password"

  configure_nvme_packages_and_services "$HB_ROOT_MNT" "$locale"
  install_dserv_stack_root "$HB_ROOT_MNT"
  sync_trial_ingest_secret_to_nvme_root "$HB_ROOT_MNT"
  configure_trial_ingest_pre_remoteservers_root "$HB_ROOT_MNT"
  install_ess_repo_root "$HB_ROOT_MNT" "$username"
  write_monitor_tcl_root "$HB_ROOT_MNT"

  enable_systemd_service_root "$HB_ROOT_MNT" "/usr/local/stim2/systemd/stim2.service"
  enable_systemd_service_root "$HB_ROOT_MNT" "/usr/local/dserv/systemd/dserv.service"
  enable_systemd_service_root "$HB_ROOT_MNT" "/usr/local/dserv/systemd/dserv-agent.service"
  write_stim2_service_override_root "$HB_ROOT_MNT"
  write_dserv_agent_override_root "$HB_ROOT_MNT"

  configure_raspi_config_root "$HB_ROOT_MNT"

  save_provision_log

  cleanup_mounts "$HB_BOOT_MNT" "$HB_ROOT_MNT"
  trap - EXIT

  set_eeprom_boot_order "$target_dev" "$root_dev"

  wait_for_gui_reboot_request
}

main "$@"
