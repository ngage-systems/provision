#!/usr/bin/env bash
# Shared trial ingest / remote logging helpers. Source from provision scripts.

: "${HB_TRIAL_INGEST_SECRET:=/etc/dserv/trial_ingest_secret}"
: "${HB_OTHER_ROOT_MNT:=/mnt/hb_other_root}"

if ! declare -F log >/dev/null 2>&1; then
  log() { echo "$*" >&2; }
fi

if ! declare -F die >/dev/null 2>&1; then
  die() { log "ERROR: $*"; exit 1; }
fi

if ! declare -F need_cmd >/dev/null 2>&1; then
  need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
  }
fi

trial_ingest_ini_list_sections() {
  local file="$1"
  [[ -n "$file" ]] || die "trial_ingest_ini_list_sections: defaults file path is empty"
  [[ -r "$file" ]] || die "trial_ingest_ini_list_sections: cannot read defaults file: $file"
  awk '
    /^[[:space:]]*\[/ {
      line=$0
      sub(/^[[:space:]]*\[/, "", line)
      sub(/\][[:space:]]*$/, "", line)
      print line
    }' "$file"
}

trial_ingest_ini_list_groups() {
  local file="$1"
  trial_ingest_ini_list_sections "$file" | awk -F. '
    NF>=3 {
      group=$1
      for (i=2; i<NF; i++) group=group "." $i
      print group
    }' | sort -u
}

trial_ingest_ini_get() {
  local file="$1"
  local section="$2"
  local key="$3"
  [[ -n "$file" ]] || die "trial_ingest_ini_get: defaults file path is empty"
  [[ -r "$file" ]] || die "trial_ingest_ini_get: cannot read defaults file: $file"
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

mesh_workgroup_for_defaults_group() {
  local group="$1"
  local file="$2"
  local wg
  wg="$(trial_ingest_ini_get "$file" "$group" "mesh_workgroup")"
  if [[ -n "$wg" ]]; then
    echo "${wg//./-}"
    return 0
  fi
  echo "${group//./-}"
}

cloud_registry_url_for_defaults_group() {
  local group="$1"
  local file="$2"
  trial_ingest_ini_get "$file" "$group" "cloud_registry"
}

cloud_ingest_url_for_defaults_group() {
  local group="$1"
  local file="$2"
  trial_ingest_ini_get "$file" "$group" "cloud_ingest"
}

cloud_data_store_enabled_for_group() {
  local group="$1"
  local file="$2"
  local raw
  raw="$(trial_ingest_ini_get "$file" "$group" "cloud_data_store")"
  [[ "${raw,,}" == "yes" || "${raw,,}" == "true" || "${raw}" == "1" ]]
}

print_trial_ingest_secret_banner() {
  local secret="$1"
  local location="${2:-${HB_TRIAL_INGEST_SECRET}}"
  {
    echo ""
    echo "================================================================"
    echo "WARNING: Cloud registry registration failed."
    echo "Create the writer row manually using this passkey:"
    echo "$secret"
    echo "Stored at: ${location}"
    echo "================================================================"
    echo ""
  } >&2
}

register_trial_ingest_writer() {
  local registry_url="$1"
  local mesh_workgroup="$2"
  local hostname="$3"
  local passkey="$4"

  need_cmd curl
  need_cmd python3

  log "Registering trial ingest writer with cloud registry..."

  local json_body
  json_body="$(python3 -c '
import json, sys
print(json.dumps({
    "workgroup": sys.argv[1],
    "user": sys.argv[2],
    "pass": sys.argv[3],
    "role": "writer",
}))
' "$mesh_workgroup" "$hostname" "$passkey")"

  local tmp_response http_code response
  tmp_response="$(mktemp)"
  if ! http_code="$(curl -sS --connect-timeout 10 --max-time 30 -o "$tmp_response" -w '%{http_code}' -X POST "$registry_url" \
    -H 'Content-Type: application/json' \
    --data "$json_body")"; then
    rm -f "$tmp_response"
    log "WARNING: Cloud registry request failed (network, DNS, or TLS error)."
    return 1
  fi
  response="$(cat "$tmp_response")"
  rm -f "$tmp_response"

  local parse_result
  parse_result="$(RESPONSE="$response" HTTP_CODE="$http_code" python3 -c '
import json, os, sys

raw = os.environ.get("RESPONSE", "")
code = os.environ.get("HTTP_CODE", "")
try:
    body = json.loads(raw) if raw.strip() else {}
except json.JSONDecodeError:
    snippet = raw[:200].replace("\n", " ")
    print(f"ERROR|Cloud registry returned non-JSON (HTTP {code}): {snippet}")
    sys.exit(0)
if body.get("ok"):
    writer_id = body.get("writer_id", "?")
    workgroup = body.get("workgroup", "?")
    print(f"OK|writer_id={writer_id} workgroup={workgroup}")
else:
    err = body.get("error", "unknown")
    msg = body.get("message", raw[:200])
    print(f"ERROR|HTTP {code}: {err}: {msg}")
')"

  if [[ "${parse_result%%|*}" == "OK" ]]; then
    log "Trial ingest writer registered (${parse_result#*|}; inactive until activated in MySQL)."
    return 0
  fi
  log "WARNING: ${parse_result#*|}"
  return 1
}

read_trial_ingest_secret() {
  local root_mnt="${1:-}"
  local file
  if [[ -n "$root_mnt" ]]; then
    file="${root_mnt}${HB_TRIAL_INGEST_SECRET}"
  else
    file="$HB_TRIAL_INGEST_SECRET"
  fi
  if [[ ! -f "$file" ]]; then
    return 1
  fi
  local line
  line="$(head -n1 "$file" | tr -d '\r')"
  if [[ -z "$line" ]]; then
    return 1
  fi
  printf '%s' "$line"
}

write_trial_ingest_secret() {
  local secret="$1"
  local root_mnt="${2:-}"
  local dest dir tmp
  if [[ -n "$root_mnt" ]]; then
    dest="${root_mnt}${HB_TRIAL_INGEST_SECRET}"
    dir="${root_mnt}/etc/dserv"
  else
    dest="$HB_TRIAL_INGEST_SECRET"
    dir="/etc/dserv"
  fi
  install -d -m 0755 -o root -g root "$dir"
  tmp="$(mktemp -p /tmp hb_trial_secret.XXXXXX)"
  chmod 0600 "$tmp"
  printf '%s\n' "$secret" > "$tmp"
  install -m 0600 -o root -g root "$tmp" "$dest"
  rm -f "$tmp"
}

configure_trial_ingest_pre_remoteservers() {
  local root_mnt="${1:-}"
  local group="$2"
  local defaults_file="$3"
  local dir target example
  dir="${root_mnt}/usr/local/dserv/local"
  target="${dir}/pre-remoteservers.tcl"
  example="${dir}/pre-remoteservers.tcl.EXAMPLE"

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

trial_ingest_root_source() {
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

trial_ingest_strip_partition_suffix() {
  local src="$1"
  if [[ "$src" =~ ^/dev/mmcblk[0-9]+p[0-9]+$ ]]; then
    echo "${src%p*}"
  elif [[ "$src" =~ ^/dev/nvme[0-9]+n[0-9]+p[0-9]+$ ]]; then
    echo "${src%p*}"
  else
    echo "${src%[0-9]*}"
  fi
}

trial_ingest_partition_path_for_disk() {
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

trial_ingest_find_root_partition() {
  local dev="$1"
  local out=""
  local line NAME="" LABEL="" PARTLABEL="" name="" label="" partlabel=""

  while read -r line; do
    [[ -n "$line" ]] || continue
    NAME=""; LABEL=""; PARTLABEL=""
    eval "$line"
    name="${NAME:-}"
    label="${LABEL:-}"
    partlabel="${PARTLABEL:-}"
    [[ -n "$name" ]] || continue
    if [[ "$label" == "rootfs" || "$partlabel" == "rootfs" ]]; then
      out="$name"
      break
    fi
  done < <(lsblk -pn -P -o NAME,LABEL,PARTLABEL "$dev" 2>/dev/null || true)

  if [[ -n "$out" ]]; then
    echo "$out"
    return 0
  fi

  trial_ingest_partition_path_for_disk "$dev" 2
}

trial_ingest_list_other_boot_disks() {
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

    echo "$dev"
  done < <(lsblk -dn -P -o NAME,TYPE,SIZE,MODEL,TRAN,RM)
}

trial_ingest_pick_other_boot_disk() {
  local root_dev="$1"
  local candidates=() dev picked=""

  while IFS= read -r dev; do
    [[ -n "$dev" ]] || continue
    candidates+=("$dev")
  done < <(trial_ingest_list_other_boot_disks "$root_dev")

  ((${#candidates[@]} == 0)) && return 1

  if ((${#candidates[@]} == 1)); then
    echo "${candidates[0]}"
    return 0
  fi

  for dev in "${candidates[@]}"; do
    if compgen -G "${dev}boot0" >/dev/null; then
      log "Multiple other boot disks found; using eMMC provisioning drive ${dev}."
      echo "$dev"
      return 0
    fi
  done

  for dev in "${candidates[@]}"; do
    if [[ "$dev" =~ ^/dev/mmcblk ]]; then
      log "Multiple other boot disks found; using ${dev}."
      echo "$dev"
      return 0
    fi
  done

  picked="${candidates[0]}"
  log "Multiple other boot disks found; using ${picked}."
  echo "$picked"
}

trial_ingest_mount_other_root() {
  local dev="$1"
  local mnt="${2:-$HB_OTHER_ROOT_MNT}"
  local mode="${3:-ro}"
  local root_part

  root_part="$(trial_ingest_find_root_partition "$dev")"
  [[ -n "$root_part" && -b "$root_part" ]] || return 1

  mkdir -p "$mnt"
  if mount -o "$mode" "$root_part" "$mnt" 2>/dev/null; then
    echo "$mnt"
    return 0
  fi
  return 1
}

trial_ingest_unmount_other_root() {
  local mnt="${1:-$HB_OTHER_ROOT_MNT}"
  if mountpoint -q "$mnt" 2>/dev/null; then
    umount "$mnt" || true
  fi
}

trial_ingest_remount_other_root_rw() {
  local mnt="${1:-$HB_OTHER_ROOT_MNT}"
  if mountpoint -q "$mnt" 2>/dev/null; then
    mount -o remount,rw "$mnt" 2>/dev/null && return 0
    trial_ingest_unmount_other_root "$mnt"
  fi
  return 1
}

trial_ingest_have_internet() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 3 bash -c 'cat < /dev/null > /dev/tcp/1.1.1.1/443' >/dev/null 2>&1 && return 0
  else
    bash -c 'cat < /dev/null > /dev/tcp/1.1.1.1/443' >/dev/null 2>&1 && return 0
  fi
  return 1
}
