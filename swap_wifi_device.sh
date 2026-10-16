#!/usr/bin/env bash
# Switch the running system's default Wi-Fi radio between the Pi built-in
# adapter and a USB adapter.
#
# "USB" disables the onboard radio (dtoverlay=disable-wifi) so saved
# NetworkManager profiles attach to the USB adapter, unless a profile is tied
# to a specific interface or MAC (the script warns about those). "Built-in"
# removes that overlay and restores the provision-time brcmfmac modprobe option
# if it is missing. Profiles are not rewritten.
set -euo pipefail

BRCMFMAC_CONF="/etc/modprobe.d/brcmfmac.conf"
BRCMFMAC_DEFAULT="options brcmfmac roamoff=1"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "$*" >&2
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "Run as root (e.g. sudo $0)"
  fi
}

wifi_iface_driver() {
  local iface="$1"
  local driver_link="/sys/class/net/${iface}/device/driver"
  if [[ -e "$driver_link" ]]; then
    basename "$(readlink -f "$driver_link" 2>/dev/null)"
    return 0
  fi
  echo "?"
}

# Keep in sync with provision_nvme.sh::classify_wifi_iface_bus.
classify_wifi_iface_bus() {
  local iface="$1"
  local iface_path="/sys/class/net/${iface}"
  [[ -d "${iface_path}/wireless" ]] || return 1

  local driver devpath
  driver="$(wifi_iface_driver "$iface")"
  devpath="$(readlink -f "${iface_path}/device" 2>/dev/null || echo "")"

  if [[ "$devpath" == *"/usb"* ]]; then
    echo "usb"
  elif [[ "$driver" == "brcmfmac" ]]; then
    echo "builtin"
  else
    echo "other"
  fi
}

# Prints "iface<TAB>driver<TAB>bus" for each wireless interface.
scan_wifi_adapters() {
  local iface_path iface bus driver
  shopt -s nullglob
  for iface_path in /sys/class/net/*; do
    iface="$(basename "$iface_path")"
    bus="$(classify_wifi_iface_bus "$iface" 2>/dev/null || true)"
    [[ -n "$bus" ]] || continue
    driver="$(wifi_iface_driver "$iface")"
    printf '%s\t%s\t%s\n' "$iface" "$driver" "$bus"
  done
}

find_boot_config() {
  local path
  for path in /boot/firmware/config.txt /boot/config.txt; do
    if [[ -f "$path" ]]; then
      printf '%s\n' "$path"
      return 0
    fi
  done
  return 1
}

# True when dtoverlay=disable-wifi applies to every board: the line sits
# before the first [section] filter, or inside [all]. A copy that only
# appears under [cm4], [cm5], [pi5], and similar does not count.
config_disables_builtin() {
  local cfg="$1" line section=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    if [[ "$line" =~ ^[[:space:]]*\[([^]]+)\][[:space:]]*(#.*)?$ ]]; then
      section="${BASH_REMATCH[1],,}"
      section="${section//[[:space:]]/}"
      continue
    fi
    if [[ "$line" =~ ^[[:space:]]*dtoverlay=disable-wifi[[:space:]]*$ ]]; then
      if [[ -z "$section" || "$section" == "all" ]]; then
        return 0
      fi
    fi
  done < "$cfg"
  return 1
}

config_has_disable_wifi() {
  local cfg="$1"
  grep -qE '^\s*dtoverlay=disable-wifi\s*$' "$cfg"
}

# True when lines appended to the end of config.txt apply to every board,
# i.e. the last section filter is [all] or there is none.
config_ends_in_all_section() {
  local cfg="$1" last
  last="$(grep -oE '^\s*\[[^]]*\]' "$cfg" | tail -n1 | tr -d '[:space:]' || true)"
  [[ -z "$last" || "${last,,}" == "[all]" ]]
}

backup_file() {
  local path="$1" backup
  backup="${path}.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$path" "$backup"
  log "Backed up $path to $backup"
}

print_saved_wifi() {
  local line uuid rest type name iface_bind mac_bind
  local -a names=() bound=()

  echo
  if ! have_cmd nmcli; then
    log "WARNING: nmcli is not available. Saved Wi-Fi networks were not listed."
    log "Existing NetworkManager profiles will be left unchanged."
    return 0
  fi

  # Escaping is off and UUID/TYPE never contain ':', so NAME is everything
  # after the second ':' even when the name itself contains colons.
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    uuid="${line%%:*}"
    rest="${line#*:}"
    type="${rest%%:*}"
    name="${rest#*:}"
    case "$type" in
      wifi|802-11-wireless) ;;
      *) continue ;;
    esac
    names+=("$name")
    iface_bind="$(nmcli -e no -g connection.interface-name connection show "$uuid" </dev/null 2>/dev/null || true)"
    mac_bind="$(nmcli -e no -g 802-11-wireless.mac-address connection show "$uuid" </dev/null 2>/dev/null || true)"
    if [[ -n "$iface_bind" ]]; then
      bound+=("${name}: interface ${iface_bind}")
    fi
    if [[ -n "$mac_bind" ]]; then
      bound+=("${name}: MAC ${mac_bind}")
    fi
  done < <(nmcli -e no -t -f UUID,TYPE,NAME connection show 2>/dev/null || true)

  if [[ ${#names[@]} -eq 0 ]]; then
    log "WARNING: No saved Wi-Fi profiles found."
    log "Existing NetworkManager profiles will be left unchanged."
    return 0
  fi

  echo "Saved Wi-Fi networks (these profiles will be reused):"
  for name in "${names[@]}"; do
    echo "  ${name}"
  done

  if [[ ${#bound[@]} -gt 0 ]]; then
    echo
    log "WARNING: These profiles are tied to one adapter and may not connect after a switch:"
    for line in "${bound[@]}"; do
      log "  ${line}"
    done
    log "To untie one: nmcli connection modify <name> connection.interface-name \"\" 802-11-wireless.mac-address \"\""
  fi
}

warn_if_ssh_over_wifi() {
  local peer dev a b c d e
  have_cmd ss && have_cmd ip || return 0

  # Older iproute2 prints "ESTAB recv send local peer". Newer versions drop
  # the state column when a state filter is set, leaving "recv send local peer".
  while read -r a b c d e _; do
    [[ -n "$a" ]] || continue
    if [[ "$a" =~ ^[0-9]+$ ]]; then
      peer="$d"
    else
      peer="$e"
    fi
    [[ -n "$peer" ]] || continue
    peer="${peer%:*}"
    peer="${peer#[}"
    peer="${peer%]}"
    peer="${peer#::ffff:}"
    if [[ "$peer" == *%* ]]; then
      dev="${peer#*%}"
    else
      dev="$(ip -o route get "$peer" 2>/dev/null \
        | awk '{ for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }' || true)"
    fi
    if [[ -n "$dev" && -d "/sys/class/net/${dev}/wireless" ]]; then
      echo
      log "WARNING: An SSH session is connected over Wi-Fi (${dev})."
      log "If Wi-Fi does not come back after the reboot, remote access over Wi-Fi will be lost."
      return 0
    fi
  done < <(ss -Htn state established '( sport = :ssh )' 2>/dev/null || true)
}

apply_usb() {
  local cfg="$1"
  if ! config_disables_builtin "$cfg"; then
    backup_file "$cfg"
    # Keep the new directive on its own line when config.txt has no trailing newline.
    if [[ -s "$cfg" ]] && [[ -n "$(tail -c1 "$cfg")" ]]; then
      printf '\n' >> "$cfg"
    fi
    # A line after a [pi4]/[cm5]/... filter would only apply to that model.
    if ! config_ends_in_all_section "$cfg"; then
      printf '%s\n' "[all]" >> "$cfg"
      log "Added [all] section header to $cfg"
    fi
    printf '%s\n' "dtoverlay=disable-wifi" >> "$cfg"
    log "Added dtoverlay=disable-wifi to $cfg"
  fi
}

apply_builtin() {
  local cfg="$1"
  backup_file "$cfg"
  sed -i -E '/^\s*dtoverlay=disable-wifi\s*$/d' "$cfg"
  log "Removed dtoverlay=disable-wifi from $cfg"
  if [[ ! -f "$BRCMFMAC_CONF" ]]; then
    mkdir -p /etc/modprobe.d
    printf '%s\n' "$BRCMFMAC_DEFAULT" > "$BRCMFMAC_CONF"
    log "Restored $BRCMFMAC_CONF"
  fi
}

prompt_reboot() {
  local choice
  echo
  echo "The change takes effect on the next boot."
  read -r -p "Reboot now? [y/N]: " choice || { echo; choice=""; }
  case "${choice,,}" in
    y|yes)
      log "Rebooting."
      reboot
      ;;
    *)
      log "Not rebooting. Reboot when you want this to take effect."
      ;;
  esac
}

main() {
  local cfg usb_default=0 choice line
  local iface driver bus kind
  local saw_usb=0 saw_builtin=0 usb_ifaces=""
  local -a adapters=()
  local current_usb="" current_builtin=""

  if [[ $# -gt 0 ]]; then
    die "This script takes no arguments."
  fi
  require_root
  if [[ ! -t 0 ]]; then
    die "This script is interactive. Run it in a terminal."
  fi

  cfg="$(find_boot_config)" || die "Could not find /boot/firmware/config.txt or /boot/config.txt."
  if config_disables_builtin "$cfg"; then
    usb_default=1
  fi

  mapfile -t adapters < <(scan_wifi_adapters)

  echo "Wi-Fi adapters:"
  if [[ ${#adapters[@]} -eq 0 ]]; then
    echo "  No Wi-Fi adapters are visible."
  else
    for line in "${adapters[@]}"; do
      IFS=$'\t' read -r iface driver bus <<<"$line"
      case "$bus" in
        usb)
          kind="USB"
          saw_usb=1
          if [[ -n "$usb_ifaces" ]]; then
            usb_ifaces+=", ${iface}"
          else
            usb_ifaces="$iface"
          fi
          ;;
        builtin)
          kind="built-in"
          saw_builtin=1
          ;;
        *)
          kind="other"
          ;;
      esac
      echo "  ${iface} (${driver}) ${kind}"
    done
  fi
  if [[ "$usb_default" -eq 1 && "$saw_builtin" -eq 0 ]]; then
    echo "  Built-in radio is hidden until it is re-enabled and the machine reboots."
  elif [[ "$usb_default" -eq 1 && "$saw_builtin" -eq 1 ]]; then
    echo "  Built-in radio is still running; dtoverlay=disable-wifi takes effect on the next reboot."
  fi

  if [[ "$usb_default" -eq 1 ]]; then
    echo
    echo "Configured default: USB"
    current_usb=" [current]"
  else
    echo
    echo "Configured default: built-in"
    current_builtin=" [current]"
    if config_has_disable_wifi "$cfg"; then
      echo "  dtoverlay=disable-wifi is present, but only inside a board filter, so it does not apply to every board."
      echo "  USB will add a copy under [all]. Built-in will remove the filtered copy."
    fi
  fi

  print_saved_wifi
  warn_if_ssh_over_wifi

  echo
  echo "Make which adapter the default?"
  if [[ -n "$usb_ifaces" ]]; then
    echo "  1) USB (${usb_ifaces}) — disable the built-in radio${current_usb}"
  else
    echo "  1) USB — disable the built-in radio${current_usb}"
    echo "     No USB Wi-Fi adapter found. This choice will be refused."
  fi
  echo "  2) Built-in — re-enable the built-in radio${current_builtin}"

  while true; do
    read -r -p "Choice [1/2]: " choice || { echo; die "No choice entered. No files changed."; }
    case "$choice" in
      1|2) break ;;
      *) echo "Enter 1 or 2." ;;
    esac
  done

  if [[ "$choice" == "1" && "$usb_default" -eq 1 ]]; then
    if [[ "$saw_builtin" -eq 1 ]]; then
      log "Configured default is already USB, but the built-in radio is still running until a reboot."
      prompt_reboot
    else
      log "Configured default is already USB. No files changed."
    fi
    exit 0
  fi
  if [[ "$choice" == "2" && "$usb_default" -eq 0 ]]; then
    if config_has_disable_wifi "$cfg"; then
      log "dtoverlay=disable-wifi is only inside a board filter. Removing it so the built-in radio stays enabled."
      apply_builtin "$cfg"
      prompt_reboot
    else
      log "Configured default is already built-in. No files changed."
    fi
    exit 0
  fi

  if [[ "$choice" == "1" && "$saw_usb" -eq 0 ]]; then
    die "No USB Wi-Fi adapter found. Plug one in and run this script again."
  fi

  if [[ "$choice" == "1" ]]; then
    apply_usb "$cfg"
  else
    apply_builtin "$cfg"
  fi

  prompt_reboot
}

main "$@"
