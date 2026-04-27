#!/usr/bin/env bash
set -euo pipefail

# Provision fallback boot media (eMMC, microSD, or USB mass storage) with
# Raspberry Pi OS (desktop) and set it up to auto-run NVMe provisioning on every boot.
#
# Intended to be run while booted from NVMe or microSD.

LOG_PREFIX=""

DEFAULTS_FILE=""

# Used by EXIT trap for cleanup (must not be local vars).
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

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

detect_touchscreen() {
  local usb_output="$1"
  echo "$usb_output" | grep -Ei 'ID (0eef:c002|222a:0001)' | sed -n '1p'
}

detect_juicer() {
  local usb_output="$1"
  echo "$usb_output" | grep -i 'juicer' | sed -n '1p'
}

detect_power_monitor() {
  compgen -G "/dev/serial/by-id/usb-Homebase_power_monitor_*-if00" | sed -n '1p'
}

detect_camera() {
  local camera_output="$1"
  echo "$camera_output" | grep -E '^[[:space:]]*[0-9]+[[:space:]]*:' | sed -n '1p'
}

print_accessory_result() {
  local label="$1"
  local detail="$2"
  local missing_detail="$3"

  if [[ -n "$detail" ]]; then
    printf '  %-15s detected (%s)\n' "${label}:" "$detail" >&2
  else
    printf '  %-15s not detected (%s)\n' "${label}:" "$missing_detail" >&2
  fi
}

check_accessories_and_acknowledge() {
  local usb_output=""
  local touchscreen="" juicer="" power_monitor="" camera=""
  local touchscreen_missing="USB touchscreen controller 0eef:c002 or 222a:0001 not found"
  local juicer_missing="USB device containing 'juicer' not found"
  local power_monitor_missing="/dev/serial/by-id/usb-Homebase_power_monitor_*-if00 not found"
  local camera_missing="No cameras reported by rpicam-hello --list-cameras"

  if have_cmd lsusb; then
    usb_output="$(lsusb 2>/dev/null || true)"
    touchscreen="$(detect_touchscreen "$usb_output" || true)"
    juicer="$(detect_juicer "$usb_output" || true)"
  else
    touchscreen_missing="Missing command: lsusb"
    juicer_missing="Missing command: lsusb"
  fi

  power_monitor="$(detect_power_monitor || true)"

  if have_cmd rpicam-hello; then
    local camera_output
    camera_output="$(rpicam-hello --list-cameras 2>&1 || true)"
    camera="$(detect_camera "$camera_output" || true)"
    if [[ -z "$camera" && -n "$camera_output" ]]; then
      camera_missing="$(echo "$camera_output" | sed -n '1p')"
    fi
  else
    camera_missing="Missing command: rpicam-hello"
  fi

  printf '\nAccessory checks:\n' >&2
  print_accessory_result "Touchscreen" "$touchscreen" "$touchscreen_missing"
  print_accessory_result "Juicer" "$juicer" "$juicer_missing"
  print_accessory_result "Power monitor" "$power_monitor" "$power_monitor_missing"
  print_accessory_result "Camera" "$camera" "$camera_missing"
  printf '\n' >&2
  read -r -p "Press Enter to continue after reviewing accessory checks." _
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

ini_list_orgs() {
  local file="$1"
  ini_list_sections "$file" | awk -F. 'NF>=2 {print $1}' | sort -u
}

ini_list_groups_for_org() {
  local file="$1"
  local org="$2"
  ini_list_sections "$file" | awk -F. -v o="$org" 'NF>=2 && $1==o {print $2}' | sort -u
}

prompt_org_group() {
  local script_path script_dir defaults_file
  script_path="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"
  script_dir="$(cd "$(dirname "$script_path")" && pwd -P)"
  defaults_file="${DEVICE_DEFAULTS_FILE:-${script_dir}/device_defaults.ini}"

  local org=""
  local group=""
  local orgs groups choice

  if [[ -r "$defaults_file" ]]; then
    orgs="$(ini_list_orgs "$defaults_file")"
  fi

  while true; do
    if [[ -n "$orgs" ]]; then
      log "Available organizations:"
      mapfile -t _orgs_list < <(printf '%s\n' "$orgs" | sed '/^$/d')
      local i
      for i in "${!_orgs_list[@]}"; do
        printf '  [%d] %s\n' "$i" "${_orgs_list[$i]}" >&2
      done
      read -r -p "Select organization by number, or type name: " choice
      if [[ "$choice" =~ ^[0-9]+$ ]] && [[ "$choice" -ge 0 && "$choice" -lt "${#_orgs_list[@]}" ]]; then
        org="${_orgs_list[$choice]}"
      else
        org="$choice"
      fi
      if printf '%s\n' "${_orgs_list[@]}" | grep -Fxq "$org"; then
        break
      fi
      log "Invalid organization '$org'."
    else
      read -r -p "Enter organization: " org
      [[ -n "$org" ]] && break
    fi
  done

  if [[ -r "$defaults_file" ]]; then
    groups="$(ini_list_groups_for_org "$defaults_file" "$org")"
  fi

  while true; do
    if [[ -n "$groups" ]]; then
      log "Available groups for ${org}:"
      mapfile -t _groups_list < <(printf '%s\n' "$groups" | sed '/^$/d')
      local i
      for i in "${!_groups_list[@]}"; do
        printf '  [%d] %s\n' "$i" "${_groups_list[$i]}" >&2
      done
      read -r -p "Select group by number, or type name: " choice
      if [[ "$choice" =~ ^[0-9]+$ ]] && [[ "$choice" -ge 0 && "$choice" -lt "${#_groups_list[@]}" ]]; then
        group="${_groups_list[$choice]}"
      else
        group="$choice"
      fi
      if printf '%s\n' "${_groups_list[@]}" | grep -Fxq "$group"; then
        break
      fi
      log "Invalid group '$group'."
    else
      read -r -p "Enter group for ${org}: " group
      [[ -n "$group" ]] && break
    fi
  done

  echo "${org}.${group}"
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

have_internet() {
  # Best-effort connectivity check without requiring curl/ping.
  if have_cmd timeout; then
    timeout 3 bash -c 'cat < /dev/null > /dev/tcp/1.1.1.1/443' >/dev/null 2>&1 && return 0
  else
    bash -c 'cat < /dev/null > /dev/tcp/1.1.1.1/443' >/dev/null 2>&1 && return 0
  fi
  return 1
}

nmcli_connected() {
  have_cmd nmcli || return 1
  nmcli -t -f STATE g 2>/dev/null | grep -q '^connected$'
}

wifi_iface() {
  # Returns the first connected wifi interface (e.g. wlan0), or empty.
  have_cmd nmcli || { echo ""; return 0; }
  nmcli -t -f DEVICE,TYPE,STATE dev status 2>/dev/null \
    | awk -F: '$2=="wifi" && $3=="connected"{print $1; exit}'
}

connected_wifi_ssid() {
  # Returns the SSID currently in use (best-effort), or empty.
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

  have_cmd nmcli || die "nmcli not found. Install NetworkManager or connect networking manually, then re-run."

  log "Attempting to connect current system to Wi-Fi via NetworkManager (nmcli)..."
  nmcli radio wifi on >/dev/null 2>&1 || true
  nmcli dev wifi rescan >/dev/null 2>&1 || true

  local iface
  iface="$(wifi_iface)"
  if [[ -z "$iface" ]]; then
    iface="$(nmcli -t -f DEVICE,TYPE dev status 2>/dev/null | awk -F: '$2=="wifi"{print $1; exit}')"
  fi
  [[ -n "$iface" ]] || die "No Wi-Fi interface found (nmcli shows no wifi devices)."

  local con_name="hb-wifi-${ssid//[^A-Za-z0-9_.-]/_}-$RANDOM"
  nmcli_cleanup_temp_connection "$con_name"
  trap 'nmcli_cleanup_temp_connection "'"$con_name"'"' RETURN

  nmcli -w 5 dev disconnect "$iface" >/dev/null 2>&1 || true

  nmcli -w 30 con add type wifi ifname "$iface" con-name "$con_name" ssid "$ssid" >/dev/null 2>&1 \
    || die "Failed to create temporary Wi-Fi connection for SSID '$ssid'."
  nmcli -w 30 con modify "$con_name" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$pass" >/dev/null 2>&1 \
    || die "Failed to apply Wi-Fi password for SSID '$ssid' (nmcli rejected it)."
  nmcli -w 60 con up "$con_name" ifname "$iface" >/dev/null 2>&1 \
    || die "Failed to connect to Wi-Fi SSID '$ssid' (auth may have failed)."

  local got_ssid
  got_ssid="$(connected_wifi_ssid)"
  if [[ "$got_ssid" != "$ssid" ]]; then
    die "Connected Wi-Fi SSID mismatch. Expected '$ssid', got '${got_ssid:-<none>}'"
  fi

  if ! nmcli_connected; then
    die "NetworkManager did not reach connected state after Wi-Fi connect."
  fi

  if wait_for_ipv4 "$iface" 60; then
    :
  else
    die "Wi-Fi connected to '$ssid' on '$iface' but no IPv4 address was acquired within 60s."
  fi
}

wifi_scan_ssids() {
  local ssids=""
  if command -v nmcli >/dev/null 2>&1; then
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

prompt_wifi() {
  local ssids ssid pass choice
  log "Scanning for Wi-Fi SSIDs..."
  ssids="$(wifi_scan_ssids)"

  if [[ -n "$ssids" ]]; then
    log "Discovered Wi-Fi SSIDs:"
    mapfile -t _ssids_list < <(printf '%s\n' "$ssids")
    local i
    for i in "${!_ssids_list[@]}"; do
      printf '  [%d] %s\n' "$i" "${_ssids_list[$i]}" >&2
    done
    echo >&2
    read -r -p "Select Wi-Fi by number, or type an SSID (leave blank to skip Wi-Fi): " choice
    if [[ -z "$choice" ]]; then
      echo ""
      echo ""
      return 0
    fi
    if [[ "$choice" =~ ^[0-9]+$ ]] && [[ "$choice" -ge 0 && "$choice" -lt "${#_ssids_list[@]}" ]]; then
      ssid="${_ssids_list[$choice]}"
    else
      ssid="$choice"
    fi
  else
    log "WARNING: Could not scan Wi-Fi SSIDs."
    read -r -p "Enter Wi-Fi SSID to use (leave blank to skip Wi-Fi): " ssid
    if [[ -z "$ssid" ]]; then
      echo ""
      echo ""
      return 0
    fi
  fi

  [[ "$ssid" != *$'\n'* && "$ssid" != *$'\r'* ]] || die "SSID contains newline characters; refusing."
  read -r -s -p "Enter Wi-Fi password for '$ssid': " pass
  echo >&2
  [[ -n "$pass" ]] || die "Empty Wi-Fi password not allowed."
  [[ "$pass" != *$'\n'* && "$pass" != *$'\r'* ]] || die "Wi-Fi password contains newline characters; refusing."
  echo "$ssid"
  echo "$pass"
}

prompt_hostname_fallback() {
  local hn
  while true; do
    read -r -p "Enter desired hostname for the fallback OS: " hn
    hn="${hn,,}"
    if [[ "$hn" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
      echo "$hn"
      return 0
    fi
    log "Invalid hostname '$hn' (use a-z, 0-9, and '-', max 63 chars)."
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

classify_fallback_device() {
  local dev="$1"
  local name="${dev##*/}"
  local tran rm
  tran="$(lsblk -dn -o TRAN "$dev" 2>/dev/null | head -n1 || true)"
  rm="$(lsblk -dn -o RM "$dev" 2>/dev/null | head -n1 || true)"

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

list_fallback_candidates() {
  # Output lines: dev|size|model|class|transport|removable
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
      loop*|zram*|ram*|sr*|nvme*)
        continue
        ;;
    esac

    if [[ ! "$name" =~ ^mmcblk[0-9]+$ && ! "$name" =~ ^sd[a-z]+$ ]]; then
      continue
    fi

    dev="/dev/${name}"
    if [[ -n "$root_dev" && "$dev" == "$root_dev" ]]; then
      continue
    fi

    class="$(classify_fallback_device "$dev")"
    echo "${dev}|${size}|${model}|${class}|${tran}|${rm}"
  done < <(lsblk -dn -P -o NAME,TYPE,SIZE,MODEL,TRAN,RM)
}

pick_fallback_device() {
  local root_dev="$1"
  local entries=()
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    entries+=("$line")
  done < <(list_fallback_candidates "$root_dev")

  [[ "${#entries[@]}" -gt 0 ]] || die "No fallback disks found (expected eMMC/microSD or USB mass storage)."

  if [[ "${#entries[@]}" -eq 1 ]]; then
    local one_dev one_size one_model one_class
    IFS="|" read -r one_dev one_size one_model one_class _ <<< "${entries[0]}"
    log "Detected one fallback target: ${one_dev} (${one_class}, ${one_size:-unknown}${one_model:+, ${one_model}})"
    echo "${entries[0]%%|*}"
    return 0
  fi

  log "Multiple fallback disks found:"
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
  local idx
  read -r -p "Select fallback disk index to ERASE and provision: " idx
  [[ "$idx" =~ ^[0-9]+$ ]] || die "Invalid index."
  [[ "$idx" -ge 0 && "$idx" -lt "${#entries[@]}" ]] || die "Index out of range."
  echo "${entries[$idx]%%|*}"
}

confirm_erase_device() {
  local dev="$1"
  log "About to ERASE and overwrite the entire disk: $dev"
  log "This is destructive. All data on $dev will be lost."
  read -r -p "Type ERASE to continue: " answer
  [[ "$answer" == "ERASE" ]] || die "User did not confirm ERASE."
}

install_packages() {
  need_cmd apt-get
  log "Installing required packages..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    wget xz-utils openssl ca-certificates \
    util-linux coreutils gawk grep sed \
    parted \
    dosfstools e2fsprogs \
    iw network-manager \
    git \
    rpi-eeprom
}

download_image_xz() {
  local out_xz="$1"
  local url="https://downloads.raspberrypi.org/raspios_arm64_latest"
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

  log "Downloading latest Raspberry Pi OS desktop arm64 image (.xz) from $url ..."
  wget -O "$out_xz" "$url"

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

write_image_to_fallback() {
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

find_fallback_partition() {
  local dev="$1"
  local want="$2" # "boot" or "root"
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

mount_fallback_partitions_for_config() {
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

set_eeprom_boot_to_fallback() {
  # Best-effort non-interactive edit using EDITOR trick; falls back with instructions if it fails.
  need_cmd rpi-eeprom-update
  need_cmd rpi-eeprom-config

  log "Updating EEPROM package + applying latest EEPROM update (if available)..."
  rpi-eeprom-update -a || true

  log "Setting EEPROM BOOT_ORDER to prefer fallback media first (SD/eMMC then USB; BOOT_ORDER=0xf41) ..."

  local editor="/tmp/hb_rpi_eeprom_editor_emmc.sh"
  cat > "$editor" <<'EOF'
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

ensure_kv "BOOT_ORDER" "0xf41"
EOF
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
    log "  BOOT_ORDER=0xf41"
  fi
}

write_fallback_config() {
  local boot_mnt="$1"
  local root_mnt="$2"
  local hostname="$3"
  local defaults_group="$4"

  log "Configuring fallback OS (user/autostart/sudo)..."

  local username="provision"
  local password="provision"

  # Create user on first boot via userconf.txt.
  local pw_hash
  pw_hash="$(openssl passwd -6 "$password")"
  printf '%s:%s\n' "$username" "$pw_hash" > "${boot_mnt}/userconf.txt"

  # Enable SSH on first boot.
  : > "${boot_mnt}/ssh"

  # Optionally set 180-degree rotation via KMS cmdline (HDMI-A-1).
  # Use an explicit mode because some panels ignore rotate without it.
  local cmdline_rotate=""
  if [[ -f "${boot_mnt}/cmdline.txt" ]]; then
    cmdline_rotate="${boot_mnt}/cmdline.txt"
  elif [[ -f "${boot_mnt}/firmware/cmdline.txt" ]]; then
    cmdline_rotate="${boot_mnt}/firmware/cmdline.txt"
  fi
  local rotate_choice="${HB_ROTATE_180:-}"
  if [[ -z "$rotate_choice" ]]; then
    local ans=""
    read -r -p "Rotate display 180 degrees at 1280x800? [y/N]: " ans
    [[ "$ans" == "y" || "$ans" == "Y" ]] && rotate_choice="yes" || rotate_choice="no"
  fi
  if [[ "$rotate_choice" == "yes" ]]; then
    if [[ -n "$cmdline_rotate" ]]; then
      if ! grep -qE '(^|[[:space:]])video=HDMI-A-1:.*rotate=180([[:space:]]|$)' "$cmdline_rotate"; then
        sed -i -e "1 s/$/ video=HDMI-A-1:1280x800M@60,rotate=180/" "$cmdline_rotate"
      fi
    else
      log "WARNING: Could not find cmdline.txt on boot partition to set display rotation."
    fi
  fi

  # Ensure PCIe is enabled so NVMe is visible when booted from eMMC.
  local cfg="${boot_mnt}/config.txt"
  if [[ -f "$cfg" ]]; then
    if ! grep -qE '^\s*dtparam=pciex1(=on)?\s*$' "$cfg"; then
      echo "dtparam=pciex1=on" >> "$cfg"
    fi
  else
    log "WARNING: Did not find config.txt on boot partition ($cfg)."
  fi

  # Prefer antenna 2 if supported.
  if [[ -f "$cfg" ]]; then
    if ! grep -qE '^\s*dtparam=ant2\s*$' "$cfg"; then
      echo "dtparam=ant2" >> "$cfg"
    fi
  fi

  # Passwordless sudo for provision user.
  mkdir -p "${root_mnt}/etc/sudoers.d"
  printf '%s\n' "${username} ALL=(ALL) NOPASSWD:ALL" > "${root_mnt}/etc/sudoers.d/010-${username}-nopasswd"
  chmod 0440 "${root_mnt}/etc/sudoers.d/010-${username}-nopasswd"

  # Auto-login to desktop (best-effort; depends on display manager).
  if [[ -d "${root_mnt}/etc/lightdm" ]]; then
    mkdir -p "${root_mnt}/etc/lightdm/lightdm.conf.d"
    cat > "${root_mnt}/etc/lightdm/lightdm.conf.d/50-hb-autologin.conf" <<EOF
[Seat:*]
autologin-user=${username}
autologin-user-timeout=0
EOF
  fi

  # Create home directory skeleton + autostart entry.
  local home_dir="${root_mnt}/home/${username}"
  mkdir -p "${home_dir}/.config/autostart"

  cat > "${home_dir}/.config/autostart/hb-provision-nvme.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Homebase NVMe Provisioning
Comment=Run NVMe provisioning on boot
Exec=bash -lc "cd /home/provision/homebase_setup/hb-clients/provision && DEVICE_DEFAULTS_GROUP=${defaults_group} ./provision_nvme_gui.py"
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

  # Clone repo onto the eMMC image.
  local repo_dir="${home_dir}/homebase_setup"
  rm -rf "$repo_dir"
  git clone --depth 1 https://github.com/ngage-systems/provision.git "$repo_dir"

  # Ensure script is executable and ownership is correct.
  chmod +x "${repo_dir}/hb-clients/provision/full_provision_nvme.sh" || true
  chmod +x "${repo_dir}/hb-clients/provision/provision_nvme.sh" || true
  chmod +x "${repo_dir}/hb-clients/provision/provision_nvme_gui.py" || true
  chown -R 1000:1000 "$home_dir"

  if [[ "$rotate_choice" == "yes" ]]; then
    # Rotate touchscreen input 180 degrees for the known controllers.
    mkdir -p "${root_mnt}/etc/udev/rules.d"
    cat > "${root_mnt}/etc/udev/rules.d/99-touchscreen-rotate.rules" <<'EOF'
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{idVendor}=="0eef", ATTRS{idProduct}=="c002", ENV{LIBINPUT_CALIBRATION_MATRIX}="-1 0 1 0 -1 1"
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{idVendor}=="222a", ATTRS{idProduct}=="0001", ENV{ID_INPUT_TOUCHSCREEN}=="1", ENV{LIBINPUT_CALIBRATION_MATRIX}="-1 0 1 0 -1 1"
EOF
  fi

  # Hostname for the installed system.
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
}

main() {
  require_root
  need_cmd lsblk
  need_cmd awk
  need_cmd grep
  need_cmd sed

  check_bookworm_or_later

  # Ensure we have internet for downloads.
  local wifi_ssid="" wifi_pass=""
  log "Wi-Fi selection (optional) for downloads."
  {
    read -r wifi_ssid
    read -r wifi_pass
  } < <(prompt_wifi)

  if [[ -n "$wifi_ssid" ]]; then
    connect_wifi_current "$wifi_ssid" "$wifi_pass"
  fi

  if ! have_internet; then
    die "No internet connectivity. Connect Ethernet or provide Wi-Fi credentials and re-run."
  fi
  log "Internet connectivity verified."

  check_accessories_and_acknowledge

  local root_src root_dev
  root_src="$(root_source)"
  root_dev="$(strip_partition_suffix "$root_src")"
  log "Current root device: $root_dev"

  local fallback_dev
  fallback_dev="$(pick_fallback_device "$root_dev")"
  [[ -b "$fallback_dev" ]] || die "Not a block device: $fallback_dev"
  if [[ "$fallback_dev" == "$root_dev" ]]; then
    die "Refusing to overwrite the current root device ($root_dev). Boot from NVMe or SD and retry."
  fi

  confirm_erase_device "$fallback_dev"

  install_packages

  local xz_path="/tmp/raspios_arm64_latest.img.xz"
  download_image_xz "$xz_path"
  write_image_to_fallback "$xz_path" "$fallback_dev"

  wait_for_partitions "$fallback_dev"
  local boot_part root_part
  boot_part="$(find_fallback_partition "$fallback_dev" boot)"
  root_part="$(find_fallback_partition "$fallback_dev" root)"

  [[ -b "$boot_part" ]] || die "Boot partition not found: $boot_part"
  [[ -b "$root_part" ]] || die "Root partition not found: $root_part"

  log "Fallback boot partition: $boot_part"
  log "Fallback root partition: $root_part"

  HB_BOOT_MNT="/mnt/hb_fallback_boot"
  HB_ROOT_MNT="/mnt/hb_fallback_root"
  trap 'cleanup_mounts "${HB_BOOT_MNT:-}" "${HB_ROOT_MNT:-}"' EXIT
  mount_fallback_partitions_for_config "$boot_part" "$root_part" "$HB_BOOT_MNT" "$HB_ROOT_MNT"

  local defaults_group
  defaults_group="$(prompt_org_group)"

  local hostname
  hostname="$(prompt_hostname_fallback)"
  write_fallback_config "$HB_BOOT_MNT" "$HB_ROOT_MNT" "$hostname" "$defaults_group"

  cleanup_mounts "$HB_BOOT_MNT" "$HB_ROOT_MNT"
  trap - EXIT

  set_eeprom_boot_to_fallback

  log "Fallback provisioning complete."
}

main
