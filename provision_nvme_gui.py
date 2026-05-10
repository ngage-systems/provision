#!/usr/bin/env python3
"""
Provisioning wizard - Tkinter GUI for collecting NVMe provisioning answers.

This replaces the old interactive shell questions with a touch-friendly flow.
It collects answers, validates Wi-Fi when requested, writes JSON output, and
launches provision_nvme.sh after the user confirms the destructive erase step.

Disable automatic git fetch/merge to refresh this repo before the wizard runs with
``--no-self-update`` or environment ``HB_PROVISION_NO_SELF_UPDATE=1`` (same as
provision_nvme.sh).
"""

import argparse
import configparser
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
from urllib.parse import urlparse
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
import uuid

try:
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    _DBUS_AVAILABLE = True
except Exception:
    _DBUS_AVAILABLE = False


# ---- Theme / sizing ----
BG = "#1e1e2e"
FG = "#cdd6f4"
ACCENT = "#89b4fa"
ACCENT_ACTIVE = "#74a0e8"
ENTRY_BG = "#313244"
ERROR = "#f38ba8"
SUCCESS = "#a6e3a1"
MUTED = "#a6adc8"

FONT_TITLE = ("DejaVu Sans", 22, "bold")
FONT_LABEL = ("DejaVu Sans", 14)
FONT_REVIEW = ("DejaVu Sans", 12)
FONT_INPUT = ("DejaVu Sans", 16)
FONT_BTN = ("DejaVu Sans", 14, "bold")


def _keyboard_scale(n):
    """Scale on-screen keyboard dimensions ~10% larger (x and y spacing/size)."""
    return max(1, int(math.ceil(float(n) * 1.1 - 1e-9)))


FONT_KEYBOARD = ("DejaVu Sans", _keyboard_scale(15))

KEYBOARD_FRAME_PADX = _keyboard_scale(20)
KEYBOARD_FRAME_PADY = _keyboard_scale(11)
KEYBOARD_ROW_INDENT_UNIT = _keyboard_scale(20)
KEYBOARD_KEY_PADX = _keyboard_scale(3)
KEYBOARD_KEY_PADY = _keyboard_scale(7)
KEYBOARD_ROW_PADY = _keyboard_scale(3)
KEYBOARD_CONTROLS_PADY = (_keyboard_scale(5), 0)
KEYBOARD_CONTROL_PADX = _keyboard_scale(4)
KEYBOARD_KEY_CHAR_WIDTH = _keyboard_scale(4)
KEYBOARD_CTRL_SHIFT_W = _keyboard_scale(7)
KEYBOARD_CTRL_SPACE_W = _keyboard_scale(18)
KEYBOARD_CTRL_BACKSPACE_W = _keyboard_scale(10)
KEYBOARD_CTRL_SMALL_W = _keyboard_scale(7)

DEFAULT_WIFI_COUNTRY = "US"
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_LOCALE = "en_us"
DEFAULT_MONITOR_WIDTH_CM = "21.7"
DEFAULT_MONITOR_HEIGHT_CM = "13.6"
DEFAULT_MONITOR_DISTANCE_CM = "30.0"
DEFAULT_SCREEN_ROTATION = "0"
DEFAULT_OUTPUT = "/tmp/hb_provision_answers.json"
WIFI_SCAN_FILE = os.environ.get("HB_WIFI_SCAN_FILE", "/tmp/hb_wifi_scan_ssids.txt")
REBOOT_REQUEST_FILE = os.environ.get("HB_PROVISION_REBOOT_REQUEST_FILE", "/tmp/hb_provision_reboot_requested")
PROVISION_COMPLETE_MARKER = "Provisioning complete. Waiting for GUI reboot request."
RESUME_STATE_VERSION = 2
RESUME_STATE_FILE = os.environ.get("HB_PROVISION_GUI_RESUME_FILE", "/tmp/hb_provision_gui_resume.json")
RESUME_STATE_MAX_AGE_SECONDS = 60 * 60
ACCESSORY_CHECK_ITEMS = [
    ("touchscreen", "Touchscreen"),
    ("juicer", "Juicer"),
    ("power_monitor", "Power monitor"),
    ("camera", "Camera"),
]
US_ANSI_KEY_ROWS = [
    {
        "pad": 0,
        "keys": [
            ("`", "~"),
            ("1", "!"),
            ("2", "@"),
            ("3", "#"),
            ("4", "$"),
            ("5", "%"),
            ("6", "^"),
            ("7", "&"),
            ("8", "*"),
            ("9", "("),
            ("0", ")"),
            ("-", "_"),
            ("=", "+"),
        ],
    },
    {
        "pad": 2,
        "keys": [
            ("q", "Q"),
            ("w", "W"),
            ("e", "E"),
            ("r", "R"),
            ("t", "T"),
            ("y", "Y"),
            ("u", "U"),
            ("i", "I"),
            ("o", "O"),
            ("p", "P"),
            ("[", "{"),
            ("]", "}"),
            ("\\", "|"),
        ],
    },
    {
        "pad": 4,
        "keys": [
            ("a", "A"),
            ("s", "S"),
            ("d", "D"),
            ("f", "F"),
            ("g", "G"),
            ("h", "H"),
            ("j", "J"),
            ("k", "K"),
            ("l", "L"),
            (";", ":"),
            ("'", "\""),
        ],
    },
    {
        "pad": 6,
        "keys": [
            ("z", "Z"),
            ("x", "X"),
            ("c", "C"),
            ("v", "V"),
            ("b", "B"),
            ("n", "N"),
            ("m", "M"),
            (",", "<"),
            (".", ">"),
            ("/", "?"),
        ],
    },
]


def script_defaults_file():
    return Path(__file__).resolve().parent / "device_defaults.ini"


def load_defaults_config(path):
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    if path.is_file():
        config.read(path)
    return config


def device_groups(config):
    groups = set()
    for section in config.sections():
        parts = section.split(".")
        if len(parts) >= 3:
            groups.add(".".join(parts[:-1]))
    return sorted(groups)


def device_types_for_group(config, group):
    types = []
    prefix = f"{group}."
    for section in config.sections():
        if section.startswith(prefix) and len(section.split(".")) >= 3:
            types.append(section[len(prefix):])
    return sorted(types)


def read_hostname_default():
    try:
        return Path("/etc/hostname").read_text(encoding="utf-8").strip()
    except OSError:
        return socket.gethostname()


def resume_state_path():
    return Path(RESUME_STATE_FILE)


def delete_resume_state(path=None):
    path = Path(path or resume_state_path())
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"Resume state: could not delete {path}: {exc}")


def write_resume_state(payload, path=None):
    path = Path(path or resume_state_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def read_resume_state(path=None):
    path = Path(path or resume_state_path())
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Resume state: ignoring unreadable state at {path}: {exc}")
        delete_resume_state(path)
        return None

    if not isinstance(payload, dict):
        print("Resume state: ignoring unsupported state file.")
        delete_resume_state(path)
        return None
    version = payload.get("version")
    if version not in (1, RESUME_STATE_VERSION):
        print("Resume state: ignoring unsupported state file.")
        delete_resume_state(path)
        return None
    if version == 1 and payload.get("target_step") == "_step_wifi_ssid":
        payload["target_step"] = "_step_wifi_ssid_pick"

    created_at = payload.get("created_at")
    if not isinstance(created_at, (int, float)) or time.time() - created_at > RESUME_STATE_MAX_AGE_SECONDS:
        print("Resume state: ignoring stale state file.")
        delete_resume_state(path)
        return None

    if not isinstance(payload.get("answers"), dict) or not isinstance(payload.get("target_step"), str):
        print("Resume state: ignoring malformed state file.")
        delete_resume_state(path)
        return None

    return payload


def quick_command(cmd, timeout=10):
    try:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"Missing command: {cmd[0]}")
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd,
            124,
            exc.stdout or "",
            exc.stderr or f"Timed out running: {' '.join(cmd)}",
        )


def parse_ssids(lines):
    return sorted({line.strip() for line in lines if line and line.strip()})


def wifi_interfaces_from_nmcli():
    result = quick_command(["nmcli", "-t", "-f", "DEVICE,TYPE", "dev", "status"], timeout=8)
    if result.returncode != 0:
        return []
    interfaces = []
    for line in result.stdout.splitlines():
        device, sep, dev_type = line.partition(":")
        if sep and dev_type == "wifi" and device:
            interfaces.append(device)
    return interfaces


def wifi_interfaces_from_iw():
    result = quick_command(["iw", "dev"], timeout=8)
    if result.returncode != 0:
        return []
    interfaces = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*Interface\s+(.+)$", line)
        if match:
            interfaces.append(match.group(1).strip())
    return interfaces


def scan_wifi_ssids(wifi_country=""):
    scan_file = Path(WIFI_SCAN_FILE)
    if scan_file.is_file():
        ssids = parse_ssids(scan_file.read_text(encoding="utf-8").splitlines())
        if ssids:
            return ssids, f"Loaded {len(ssids)} SSID(s) from {scan_file}."

    if shutil.which("rfkill"):
        quick_command(["rfkill", "unblock", "wifi"], timeout=5)

    if shutil.which("nmcli"):
        quick_command(["nmcli", "radio", "wifi", "on"], timeout=8)

    wifi_country = (wifi_country or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}", wifi_country) and shutil.which("iw"):
        quick_command(["sudo", "-n", "iw", "reg", "set", wifi_country], timeout=5)

    time.sleep(1)

    commands = [
        ["nmcli", "--escape", "no", "-t", "-f", "SSID", "dev", "wifi", "list", "--rescan", "yes"],
        ["nmcli", "--escape", "no", "-t", "-f", "SSID", "dev", "wifi", "list"],
    ]
    diagnostics = []
    for cmd in commands:
        result = quick_command(cmd, timeout=15)
        if result.returncode == 0:
            ssids = parse_ssids(result.stdout.splitlines())
            if ssids:
                return ssids, f"Found {len(ssids)} SSID(s) with nmcli."
        elif result.stderr.strip():
            diagnostics.append(result.stderr.strip())

    interfaces = wifi_interfaces_from_nmcli() or wifi_interfaces_from_iw()
    for iface in interfaces:
        for cmd in (["sudo", "-n", "iw", "dev", iface, "scan"], ["iw", "dev", iface, "scan"]):
            result = quick_command(cmd, timeout=20)
            if result.returncode != 0:
                if result.stderr.strip():
                    diagnostics.append(result.stderr.strip())
                continue
            ssids = []
            for line in result.stdout.splitlines():
                match = re.match(r"\s*SSID:\s*(.*)$", line)
                if match:
                    ssids.append(match.group(1))
            ssids = parse_ssids(ssids)
            if ssids:
                return ssids, f"Found {len(ssids)} SSID(s) with iw on {iface}."

    if not shutil.which("nmcli") and not shutil.which("iw"):
        return [], "Neither nmcli nor iw is available for Wi-Fi scanning."
    if not interfaces:
        return [], "No Wi-Fi interface was reported by nmcli or iw."
    if diagnostics:
        return [], diagnostics[-1]
    return [], "No Wi-Fi SSIDs were found after enabling radio and rescanning."


def run_command(cmd, timeout=30, env=None):
    try:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"Missing command: {cmd[0]}")
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd,
            124,
            exc.stdout or "",
            exc.stderr or f"Timed out running: {' '.join(cmd)}",
        )


def accessory_result(detected, detail):
    return {"detected": bool(detected), "detail": detail}


def detect_touchscreen(lsusb_output):
    for line in lsusb_output.splitlines():
        if re.search(r"\bID\s+(0eef:c002|222a:0001)\b", line, re.IGNORECASE):
            return accessory_result(True, line.strip())
    return accessory_result(False, "USB touchscreen controller 0eef:c002 or 222a:0001 not found.")


def detect_juicer(lsusb_output):
    for line in lsusb_output.splitlines():
        if re.search(r"juicer", line, re.IGNORECASE):
            return accessory_result(True, line.strip())
    return accessory_result(False, "USB device containing 'juicer' not found.")


def detect_power_monitor():
    serial_dir = Path("/dev/serial/by-id")
    matches = sorted(serial_dir.glob("usb-Homebase_power_monitor_*-if00")) if serial_dir.is_dir() else []
    if matches:
        return accessory_result(True, str(matches[0]))
    return accessory_result(False, "/dev/serial/by-id/usb-Homebase_power_monitor_*-if00 not found.")


def detect_camera():
    result = run_command(["rpicam-hello", "--list-cameras"], timeout=10)
    output = "\n".join(part.strip() for part in [result.stdout, result.stderr] if part and part.strip())
    if result.returncode == 127:
        return accessory_result(False, result.stderr.strip())

    camera_lines = [
        line.strip()
        for line in output.splitlines()
        if re.match(r"^\s*\d+\s*:", line)
    ]
    if result.returncode == 0 and camera_lines:
        return accessory_result(True, camera_lines[0])
    if output:
        detail = output.splitlines()[0].strip()
    else:
        detail = "No cameras reported by rpicam-hello --list-cameras."
    return accessory_result(False, detail)


def check_accessories():
    lsusb_result = run_command(["lsusb"], timeout=5)
    lsusb_output = lsusb_result.stdout if lsusb_result.returncode == 0 else ""
    missing_lsusb = lsusb_result.returncode == 127

    if missing_lsusb:
        touchscreen = accessory_result(False, lsusb_result.stderr.strip())
        juicer = accessory_result(False, lsusb_result.stderr.strip())
    else:
        touchscreen = detect_touchscreen(lsusb_output)
        juicer = detect_juicer(lsusb_output)

    return {
        "touchscreen": touchscreen,
        "juicer": juicer,
        "power_monitor": detect_power_monitor(),
        "camera": detect_camera(),
    }


DEFAULT_REGISTRY_PROBE_HOST = "dserv.net"

# Baseline reachability: succeed if any of these TCP connects (captive / partial paths).
# Host:port — keep in sync with provision_nvme.sh::internet_probe_targets first three lines.
CONNECTIVITY_BASELINE_TCP = [
    ("1.1.1.1", 443),
    ("1.0.0.1", 443),
    ("93.184.216.34", 80),
]

# Required services (host, port) beyond registry — order matches plan / shell.
FIXED_SERVICE_PROBE_TCP = [
    ("downloads.raspberrypi.org", 443),
    ("github.com", 443),
    ("api.github.com", 443),
    ("objects.githubusercontent.com", 443),
]


def parse_mesh_host_for_probe(mesh_host):
    """Return (hostname, port) for connectivity probes from device_defaults mesh_host URL or host string."""
    raw = (mesh_host or "").strip()
    if not raw:
        return DEFAULT_REGISTRY_PROBE_HOST, 443
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").strip()
    if not host:
        return DEFAULT_REGISTRY_PROBE_HOST, 443
    port = parsed.port or 443
    return host, port


def required_dns_hostnames(registry_hostname):
    hosts = [
        "deb.debian.org",
        "archive.raspberrypi.com",
        "downloads.raspberrypi.org",
        "github.com",
        "api.github.com",
        "objects.githubusercontent.com",
        (registry_hostname or DEFAULT_REGISTRY_PROBE_HOST).strip() or DEFAULT_REGISTRY_PROBE_HOST,
    ]
    return tuple(dict.fromkeys(h for h in hosts if h))


def _tcp_connect_probe(host, port, bind_iface=None, timeout_s=3):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        if bind_iface:
            opt = bind_iface.encode("utf-8")
            if not opt.endswith(b"\0"):
                opt += b"\0"
            sock.setsockopt(socket.SOL_SOCKET, 25, opt)  # SO_BINDTODEVICE
        sock.connect((host, int(port)))
        return True, ""
    except OSError as exc:
        err = getattr(exc, "strerror", None) or str(exc)
        return False, err
    finally:
        sock.close()


def connectivity_checks_report(registry_host, registry_port, bind_iface=None):
    """Ordered checklist rows: baseline, DNS per host, then TCP to each required endpoint.

    bind_iface: wireless interface name for SO_BINDTODEVICE, or None for default routing.
    Each row: dict with keys key, title, ok, detail (str).
    """
    rows = []
    reg_host = registry_host or DEFAULT_REGISTRY_PROBE_HOST
    reg_port = int(registry_port or 443)

    hits = []
    for host, port in CONNECTIVITY_BASELINE_TCP:
        ok, detail = _tcp_connect_probe(host, port, bind_iface=bind_iface)
        if ok:
            hits.append(f"{host}:{port}")
            break
    rows.append(
        {
            "key": "baseline_tcp",
            "title": "Baseline internet (Cloudflare / example.com)",
            "ok": bool(hits),
            "detail": ("OK via " + hits[0]) if hits else "Could not reach any baseline address",
        }
    )

    for hostname in required_dns_hostnames(reg_host):
        try:
            socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            err = getattr(exc, "strerror", None) or str(exc)
            rows.append(
                {
                    "key": f"dns:{hostname}",
                    "title": f"DNS: {hostname}",
                    "ok": False,
                    "detail": err,
                }
            )
        else:
            rows.append(
                {
                    "key": f"dns:{hostname}",
                    "title": f"DNS: {hostname}",
                    "ok": True,
                    "detail": "",
                }
            )

    service_targets = [(reg_host, reg_port, f"Registry ({reg_host}:{reg_port})")]
    for host, port in FIXED_SERVICE_PROBE_TCP:
        service_targets.append((host, port, f"TCP: {host}:{port}"))

    for host, port, title in service_targets:
        ok, detail = _tcp_connect_probe(host, port, bind_iface=bind_iface)
        rows.append(
            {
                "key": f"tcp:{host}:{port}",
                "title": title,
                "ok": ok,
                "detail": detail if not ok else "",
            }
        )

    return rows


def connectivity_report_all_ok(rows):
    return bool(rows) and all(r.get("ok") for r in rows)


def summarize_connectivity_rows(rows):
    lines = []
    for r in rows:
        label = r.get("title", r.get("key", "?"))
        status = "OK" if r.get("ok") else "FAIL"
        extra = r.get("detail") or ""
        if extra:
            lines.append(f"  [{status}] {label} — {extra}")
        else:
            lines.append(f"  [{status}] {label}")
    return "\n".join(lines)


def have_internet(registry_hostname=None, registry_port=None):
    """True only if baseline reachable and all DNS + required TCP probes pass (default routing)."""
    reg_host = (registry_hostname or DEFAULT_REGISTRY_PROBE_HOST).strip() or DEFAULT_REGISTRY_PROBE_HOST
    reg_p = registry_port if registry_port is not None else 443
    rows = connectivity_checks_report(reg_host, reg_p, bind_iface=None)
    return connectivity_report_all_ok(rows)


# Hostnames for DNS resolution — mirrors provision_nvme.sh::provision_critical_dns_hosts
def provision_critical_dns_failures(registry_hostname=None):
    failed = []
    reg_host = (registry_hostname or DEFAULT_REGISTRY_PROBE_HOST).strip() or DEFAULT_REGISTRY_PROBE_HOST
    for host in required_dns_hostnames(reg_host):
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            err = getattr(exc, "strerror", None) or str(exc)
            failed.append((host, err))
    return failed


def warn_critical_dns_if_needed(parent=None, registry_hostname=None):
    if parent is not None and hasattr(parent, "_warn_critical_dns_if_needed"):
        parent._warn_critical_dns_if_needed(registry_hostname=registry_hostname)
        return
    failures = provision_critical_dns_failures(registry_hostname)
    if not failures:
        return
    detail = "\n".join(f"  • {host}: {err}" for host, err in failures)
    messagebox.showwarning(
        "DNS check for provisioning servers",
        "Could not resolve one or more host names required for downloads, apt, GitHub, or registry:\n\n"
        f"{detail}\n\n"
        "Provisioning may fail unless DNS works. "
        "If you use Wi-Fi or a strict network, check router DNS settings or /etc/resolv.conf.",
        parent=parent,
    )


def git_command(repo_root, args, timeout=45):
    return run_command(["git", "-C", str(repo_root), *args], timeout=timeout)


def update_current_repo_if_needed(script_path):
    if shutil.which("git") is None:
        return {"ok": False, "updated": False, "message": "git is not available."}

    script_dir = Path(script_path).resolve().parent
    result = run_command(["git", "-C", str(script_dir), "rev-parse", "--show-toplevel"], timeout=10)
    if result.returncode != 0:
        return {
            "ok": False,
            "updated": False,
            "message": f"Could not determine repository root: {result.stderr.strip() or result.stdout.strip()}",
        }
    repo_root = Path(result.stdout.strip())

    origin_head_result = git_command(repo_root, ["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], timeout=10)
    origin_head = origin_head_result.stdout.strip() if origin_head_result.returncode == 0 else "origin/main"

    before_result = git_command(repo_root, ["rev-parse", "HEAD"], timeout=10)
    if before_result.returncode != 0:
        return {"ok": False, "updated": False, "message": "Could not read current git revision."}
    before = before_result.stdout.strip()

    fetch_result = git_command(repo_root, ["fetch", "--prune"], timeout=90)
    if fetch_result.returncode != 0:
        return {
            "ok": False,
            "updated": False,
            "message": f"git fetch failed: {fetch_result.stderr.strip() or fetch_result.stdout.strip()}",
        }

    target_result = git_command(repo_root, ["rev-parse", origin_head], timeout=10)
    if target_result.returncode != 0:
        return {"ok": False, "updated": False, "message": f"Could not resolve {origin_head}."}
    target = target_result.stdout.strip()
    if before == target:
        return {"ok": True, "updated": False, "message": "Already up to date."}

    dirty_result = git_command(repo_root, ["status", "--porcelain"], timeout=10)
    if dirty_result.returncode == 0 and dirty_result.stdout.strip():
        return {
            "ok": False,
            "updated": False,
            "message": "The local checkout has uncommitted changes, so the GUI did not update automatically.",
        }

    merge_result = git_command(repo_root, ["merge", "--ff-only", origin_head], timeout=90)
    if merge_result.returncode != 0:
        return {
            "ok": False,
            "updated": False,
            "message": f"git update failed: {merge_result.stderr.strip() or merge_result.stdout.strip()}",
        }

    after_result = git_command(repo_root, ["rev-parse", "HEAD"], timeout=10)
    after = after_result.stdout.strip() if after_result.returncode == 0 else ""
    return {
        "ok": True,
        "updated": bool(after and after != before),
        "message": f"Updated from {before[:7]} to {after[:7] or target[:7]}.",
    }


def nmcli(args, timeout=30):
    env = os.environ.copy()
    env["NM_CLI_SECRET_AGENT"] = "0"
    return run_command(["nmcli", *args], timeout=timeout, env=env)


def wifi_interface():
    result = nmcli(["-t", "-f", "DEVICE,TYPE,STATE", "dev", "status"], timeout=10)
    if result.returncode != 0:
        return ""

    fallback = ""
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        device, dev_type, state = parts[:3]
        if dev_type == "wifi" and state == "connected":
            return device
        if dev_type == "wifi" and not fallback:
            fallback = device
    return fallback


def active_connection_for_iface(iface):
    result = nmcli(["-t", "-f", "NAME,DEVICE", "con", "show", "--active"], timeout=10)
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        name, sep, device = line.rpartition(":")
        if sep and device == iface:
            return name
    return ""


def connected_wifi_ssid():
    result = nmcli(["-t", "-f", "ACTIVE,SSID", "dev", "wifi"], timeout=10)
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        active, sep, ssid = line.partition(":")
        if sep and active == "yes":
            return ssid
    return ""


def iface_has_ipv4(iface):
    result = run_command(["ip", "-4", "addr", "show", "dev", iface], timeout=5)
    return result.returncode == 0 and re.search(r"^\s*inet\s+", result.stdout, re.MULTILINE)


def wait_for_ipv4(iface, timeout_s=90):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if iface_has_ipv4(iface):
            return True
        time.sleep(1)


def safe_connection_name(ssid):
    """Strip SSID for use as nmcli Wi-Fi profile name fragment."""
    if ssid is None:
        return ""
    val = str(ssid).strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    return val.strip()


def _strip_nmcli_secret_value(raw):
    """Normalize a secret string from nmcli -g or tabular output."""
    if raw is None:
        return ""
    val = raw.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    return val.strip()


def read_connection_wifi_psk(connection_name):
    """Read the WPA PSK NetworkManager has for this connection (best-effort; needs nmcli -s)."""
    for field in ("wifi-sec.psk", "802-11-wireless-security.psk"):
        result = nmcli(["-s", "-g", field, "con", "show", connection_name], timeout=10)
        if result.returncode != 0 or not (result.stdout or "").strip():
            continue
        line = (result.stdout or "").splitlines()[0]
        val = _strip_nmcli_secret_value(line)
        if not val or val == "--" or val.lower() in ("<hidden>", "****"):
            continue
        if re.fullmatch(r"\*+", val):
            continue
        return val

    result = nmcli(["-s", "con", "show", connection_name], timeout=15)
    if result.returncode != 0:
        return None
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        m = re.match(r"(?:802-11-wireless-security|wifi-sec)\.psk:\s*(.*)$", line)
        if not m:
            continue
        val = _strip_nmcli_secret_value(m.group(1))
        if not val or val == "--" or val.lower() in ("<hidden>", "****"):
            continue
        if re.fullmatch(r"\*+", val):
            continue
        return val
    return None


NM_AGENT_IFACE = "org.freedesktop.NetworkManager.SecretAgent"
NM_AGENT_PATH = "/org/freedesktop/NetworkManager/SecretAgent"
NM_AGENT_MGR_IFACE = "org.freedesktop.NetworkManager.AgentManager"
NM_AGENT_MGR_PATH = "/org/freedesktop/NetworkManager/AgentManager"
NM_BUS_NAME = "org.freedesktop.NetworkManager"
NM_NO_SECRETS_ERROR = "org.freedesktop.NetworkManager.SecretManager.NoSecrets"


if _DBUS_AVAILABLE:

    class HBSecretAgent(dbus.service.Object):
        """Temporary NetworkManager secret agent used during the Wi-Fi test.

        Returns the user-typed PSK on the first GetSecrets call. On any subsequent
        call (or any call carrying the REQUEST_NEW flag, which means NM has
        already determined the previous secret was wrong) it raises NoSecrets so
        NetworkManager gives up immediately. This prevents the desktop secret
        agent (nm-applet et al.) from popping its own dialog and silently
        substituting a different password than what the user typed in our GUI.
        """

        def __init__(self, bus, typed_psk):
            super().__init__(bus, NM_AGENT_PATH)
            self._psk = typed_psk
            self._answered = False

        @dbus.service.method(
            NM_AGENT_IFACE,
            in_signature="a{sa{sv}}osasu",
            out_signature="a{sa{sv}}",
        )
        def GetSecrets(self, connection, conn_path, setting_name, hints, flags):
            request_new = bool(flags & 0x2)
            if self._answered or request_new:
                raise dbus.DBusException("No secrets available", name=NM_NO_SECRETS_ERROR)
            self._answered = True
            if str(setting_name) == "802-11-wireless-security":
                return {
                    "802-11-wireless-security": {
                        "psk": dbus.String(self._psk),
                    }
                }
            raise dbus.DBusException("No secrets available", name=NM_NO_SECRETS_ERROR)

        @dbus.service.method(NM_AGENT_IFACE, in_signature="os", out_signature="")
        def CancelGetSecrets(self, conn_path, setting_name):
            return None

        @dbus.service.method(NM_AGENT_IFACE, in_signature="a{sa{sv}}o", out_signature="")
        def SaveSecrets(self, connection, conn_path):
            return None

        @dbus.service.method(NM_AGENT_IFACE, in_signature="a{sa{sv}}o", out_signature="")
        def DeleteSecrets(self, connection, conn_path):
            return None


@contextmanager
def hb_secret_agent(typed_psk):
    """Register a transient NM secret agent for the duration of the with-block.

    Falls back silently (yields None) if dbus / pygobject / NetworkManager are
    not available. Callers should not depend on the yielded value; the agent
    works purely as a side-effect on the system bus.
    """
    if not _DBUS_AVAILABLE:
        yield None
        return

    bus = None
    agent = None
    mgr = None
    loop = None
    thread = None
    registered = False
    try:
        DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()
        agent = HBSecretAgent(bus, typed_psk)
        mgr = bus.get_object(NM_BUS_NAME, NM_AGENT_MGR_PATH)
        mgr.Register("com.homebase.provision", dbus_interface=NM_AGENT_MGR_IFACE)
        registered = True
        loop = GLib.MainLoop()
        thread = threading.Thread(target=loop.run, daemon=True)
        thread.start()
    except Exception:
        if registered and mgr is not None:
            try:
                mgr.Unregister(dbus_interface=NM_AGENT_MGR_IFACE)
            except Exception:
                pass
        if loop is not None:
            try:
                loop.quit()
            except Exception:
                pass
        if agent is not None:
            try:
                agent.remove_from_connection()
            except Exception:
                pass
        yield None
        return

    try:
        yield agent
    finally:
        if registered and mgr is not None:
            try:
                mgr.Unregister(dbus_interface=NM_AGENT_MGR_IFACE)
            except Exception:
                pass
        if loop is not None:
            try:
                loop.quit()
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=2)
        if agent is not None:
            try:
                agent.remove_from_connection()
            except Exception:
                pass


def test_wifi_connection(
    ssid,
    password,
    *,
    hidden=False,
    registry_host=None,
    registry_port=None,
    on_connected=None,
):
    if not ssid:
        return {"ok": True, "tested": False, "internet_reachable": False, "message": "Wi-Fi skipped.", "connectivity_report": []}

    reg_h = (registry_host or DEFAULT_REGISTRY_PROBE_HOST).strip() or DEFAULT_REGISTRY_PROBE_HOST
    reg_p = registry_port if registry_port is not None else 443

    if shutil.which("nmcli") is None:
        return {
            "ok": False,
            "tested": False,
            "internet_reachable": False,
            "message": "nmcli is not available. Install NetworkManager or skip Wi-Fi.",
            "connectivity_report": [],
        }

    nmcli(["radio", "wifi", "on"], timeout=10)
    nmcli(["dev", "wifi", "rescan"], timeout=15)

    iface = wifi_interface()
    if not iface:
        return {
            "ok": False,
            "tested": False,
            "internet_reachable": False,
            "message": "No Wi-Fi interface was found.",
            "connectivity_report": [],
        }

    previous_connection = active_connection_for_iface(iface)
    connection_name = safe_connection_name(ssid)
    created_connection = False
    connected_connection = False

    def cleanup_test_connection():
        if created_connection:
            nmcli(["con", "delete", connection_name], timeout=10)

    def restore_previous_connection():
        if not previous_connection or previous_connection == connection_name:
            return ""
        result = nmcli(["-w", "20", "con", "up", previous_connection], timeout=25)
        if result.returncode == 0:
            cleanup_test_connection()
            return f" Restored previous Wi-Fi connection '{previous_connection}'."
        return f" Could not restore previous Wi-Fi connection '{previous_connection}'; staying on '{ssid}'."

    try:
        nmcli(["con", "delete", connection_name], timeout=10)
        nmcli(["-w", "5", "dev", "disconnect", iface], timeout=10)

        result = nmcli(
            ["-w", "30", "con", "add", "type", "wifi", "ifname", iface, "con-name", connection_name, "ssid", ssid],
            timeout=35,
        )
        if result.returncode != 0:
            return {
                "ok": False,
                "tested": True,
                "internet_reachable": False,
                "message": f"Failed to create a temporary Wi-Fi connection for '{ssid}'.",
                "connectivity_report": [],
            }
        created_connection = True

        modify_args = [
            "-w",
            "30",
            "con",
            "modify",
            connection_name,
            "connection.autoconnect",
            "no",
            "wifi-sec.key-mgmt",
            "wpa-psk",
            "wifi-sec.psk",
            password,
            "wifi-sec.psk-flags",
            "0",
        ]
        if hidden:
            modify_args.extend(["wifi.hidden", "yes"])

        result = nmcli(modify_args, timeout=35)
        if result.returncode != 0:
            return {
                "ok": False,
                "tested": True,
                "internet_reachable": False,
                "message": f"NetworkManager rejected the password settings for '{ssid}'.",
                "connectivity_report": [],
            }

        with hb_secret_agent(password):
            result = nmcli(["-w", "60", "con", "up", connection_name, "ifname", iface], timeout=70)
            if result.returncode != 0:
                return {
                    "ok": False,
                    "tested": True,
                    "internet_reachable": False,
                    "message": f"Failed to connect to '{ssid}'. Check the password and try again.",
                    "connectivity_report": [],
                }
            connected_connection = True

            got_ssid = connected_wifi_ssid()
            if got_ssid != ssid:
                return {
                    "ok": False,
                    "tested": True,
                    "internet_reachable": False,
                    "message": f"Connected Wi-Fi mismatch. Expected '{ssid}', got '{got_ssid or '<none>'}'.",
                    "connectivity_report": [],
                }

            if not wait_for_ipv4(iface):
                return {
                    "ok": False,
                    "tested": True,
                    "internet_reachable": False,
                    "message": f"Connected to '{ssid}', but no IPv4 address was acquired.",
                    "connectivity_report": [],
                }

            actual_password = read_connection_wifi_psk(connection_name)
            if not actual_password:
                actual_password = password

        if callable(on_connected):
            try:
                on_connected(iface)
            except Exception:
                pass

        connectivity_report = connectivity_checks_report(reg_h, reg_p, bind_iface=iface)
        internet_ok = connectivity_report_all_ok(connectivity_report)
        restore_message = restore_previous_connection()
        message = f"Connected to '{ssid}'."
        if previous_connection:
            message += restore_message
        else:
            message += " Leaving this Wi-Fi connected for provisioning."
        if not internet_ok:
            message += " Required connectivity checks over Wi-Fi did not all pass."

        return {
            "ok": True,
            "tested": True,
            "internet_reachable": internet_ok,
            "message": message,
            "actual_password": actual_password,
            "connectivity_report": connectivity_report,
        }
    finally:
        if not connected_connection:
            if previous_connection and previous_connection != connection_name:
                nmcli(["-w", "20", "con", "up", previous_connection], timeout=25)
            cleanup_test_connection()


class ProvisioningWizard(tk.Tk):
    def __init__(self, output_path=DEFAULT_OUTPUT):
        super().__init__()
        self.title("Device Provisioning")
        self.configure(bg=BG)

        self._configure_window_size()

        self.output_path = output_path
        self._self_update_retry_needed = False
        self._loaded_resume_state = False

        self.defaults_path = Path(os.environ.get("DEVICE_DEFAULTS_FILE", script_defaults_file()))
        self.config = load_defaults_config(self.defaults_path)
        self.groups = device_groups(self.config)
        self.wifi_ssids = []
        self.wifi_scan_message = ""
        self._focused_entry = None
        self._keyboard_shift = False
        self._keyboard_rows_frame = None
        self._last_wifi_test_signature = None
        self._wifi_ssid_manual_flow = False

        self.answers = {
            "wifi_country": DEFAULT_WIFI_COUNTRY,
            "wifi_hidden": False,
            "wifi_networks": [],
            "timezone": DEFAULT_TIMEZONE,
            "locale": DEFAULT_LOCALE,
            "screen_rotation": DEFAULT_SCREEN_ROTATION,
            "hostname": read_hostname_default(),
            "monitor_width_cm": DEFAULT_MONITOR_WIDTH_CM,
            "monitor_height_cm": DEFAULT_MONITOR_HEIGHT_CM,
            "monitor_distance_cm": DEFAULT_MONITOR_DISTANCE_CM,
        }
        self._apply_initial_defaults_from_env()

        self.steps = [
            self._step_defaults_group,
            self._step_defaults_device_type,
            self._step_wifi_country,
            self._step_wifi_ssid_pick,
            self._step_wifi_ssid_manual,
            self._step_wifi_password,
            self._step_accessory_checks,
            self._step_timezone,
            self._step_locale,
            self._step_screen_width,
            self._step_monitor_width,
            self._step_screen_height,
            self._step_monitor_height,
            self._step_monitor_distance,
            self._step_screen_refresh_rate,
            self._step_screen_rotation,
            self._step_hostname,
            self._step_username,
            self._step_password,
            self._step_review,
        ]
        self.step_index = 0
        self._restore_resume_state()
        self._maybe_self_update("startup")

        self._build_layout()
        self._render_current_step()
        self.focus_force()

    def _configure_window_size(self):
        screen_w = max(1, self.winfo_screenwidth())
        screen_h = max(1, self.winfo_screenheight())
        margin_x = 20 if screen_w > 800 else 0
        margin_y = 60 if screen_h > 480 else 0
        width = min(1280, max(640, screen_w - margin_x))
        height = min(800, max(420, screen_h - margin_y))
        self.geometry(f"{width}x{height}+10+10")
        self.minsize(min(760, width), min(420, height))

    def _step_index_for_name(self, step_name):
        for index, step in enumerate(self.steps):
            if step.__name__ == step_name:
                return index
        return None

    def _restore_resume_state(self):
        payload = read_resume_state()
        if not payload:
            return

        target_step = payload["target_step"]
        target_index = self._step_index_for_name(target_step)
        if target_index is None:
            print(f"Resume state: unknown target step {target_step!r}.")
            delete_resume_state()
            return

        self.answers.update(payload["answers"])
        wn = self.answers.get("wifi_networks")
        if not isinstance(wn, list):
            self.answers["wifi_networks"] = []
        self.step_index = target_index
        self._wifi_ssid_manual_flow = target_step == "_step_wifi_ssid_manual"
        self._loaded_resume_state = True

        ssid = self.answers.get("wifi_ssid", "")
        password = self.answers.get("wifi_password", "")
        hidden = bool(self.answers.get("wifi_hidden"))
        if (
            ssid
            and password
            and self.answers.get("wifi_tested") is True
            and self.answers.get("wifi_test_ssid") == ssid
            and bool(self.answers.get("wifi_test_hidden")) == hidden
        ):
            self._last_wifi_test_signature = (ssid, password, hidden)
        print(f"Resume state: restored wizard at {payload['target_step']}.")

    def _current_resume_target_step_name(self):
        if not self.steps:
            return ""
        return self.steps[self.step_index].__name__

    def _save_resume_state(self):
        target_step = self._current_resume_target_step_name()
        if not target_step:
            return
        payload = {
            "version": RESUME_STATE_VERSION,
            "created_at": time.time(),
            "target_step": target_step,
            "answers": self.answers,
        }
        write_resume_state(payload)
        print(f"Resume state: saved wizard state for {target_step}.")

    # ------------------------------------------------------------------
    # Layout: an outer frame separates keyboard (root-level, side=bottom)
    # from nav+content (inside outer).  This gives nav and content their
    # own non-overlapping geometry regions so z-order between them is
    # irrelevant — nav is always side=bottom of outer, content fills the
    # rest above it.  Keyboard appearing/disappearing only resizes outer;
    # it can never cover nav because they live at different hierarchy
    # levels and different screen positions.
    # ------------------------------------------------------------------
    def _build_layout(self):
        self.keyboard = tk.Frame(
            self, bg=ENTRY_BG, padx=KEYBOARD_FRAME_PADX, pady=KEYBOARD_FRAME_PADY
        )
        self._build_touch_keyboard()

        # outer fills everything above the keyboard (which packs side=bottom
        # in self when visible).  nav and content both live inside outer.
        outer = tk.Frame(self, bg=BG)
        outer.pack(side="top", fill="both", expand=True)

        self.nav = tk.Frame(outer, bg=BG, padx=40, pady=10)
        self.nav.pack(side="bottom", fill="x")

        self.progress_label = tk.Label(
            self.nav, text="", bg=BG, fg=FG, font=FONT_LABEL
        )
        self.progress_label.pack(side="top", fill="x", pady=(0, 4))

        nav_row = tk.Frame(self.nav, bg=BG)
        nav_row.pack(side="top", fill="x")

        self.btn_back = self._make_button(nav_row, "< Back", self._on_back)
        self.btn_back.pack(side="left")

        self.nav_right = tk.Frame(nav_row, bg=BG)
        self.nav_right.pack(side="right")

        self.btn_recheck_accessories = self._make_button(
            self.nav_right, "Recheck Accessories", self._recheck_accessories
        )

        self.btn_next = self._make_button(self.nav_right, "Next >", self._on_next, primary=True)
        self.btn_next.pack(side="left")

        self.nav.update_idletasks()

        self.content = tk.Frame(outer, bg=BG, padx=40, pady=30)
        self.content.pack(side="top", fill="both", expand=True)

    def _make_button(self, parent, text, command, primary=False):
        bg = ACCENT if primary else ENTRY_BG
        active = ACCENT_ACTIVE if primary else "#45475a"
        invoked_at = {"time": 0.0}

        def invoke_once():
            now = time.monotonic()
            if now - invoked_at["time"] < 0.25:
                return
            invoked_at["time"] = now
            command()

        button = tk.Button(
            parent,
            text=text,
            command=invoke_once,
            bg=bg,
            fg=FG,
            activebackground=active,
            activeforeground=FG,
            font=FONT_BTN,
            relief="flat",
            padx=30,
            pady=12,
            borderwidth=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=0,
        )
        self._bind_touch_release(button, invoke_once)
        return button

    def _make_keyboard_button(self, parent, text, command, width=None):
        if width is None:
            width = KEYBOARD_KEY_CHAR_WIDTH
        invoked_at = {"time": 0.0}

        def invoke_once():
            now = time.monotonic()
            if now - invoked_at["time"] < 0.25:
                return
            invoked_at["time"] = now
            command()

        button = tk.Button(
            parent,
            text=text,
            command=invoke_once,
            bg=BG,
            fg=FG,
            activebackground="#45475a",
            activeforeground=FG,
            font=FONT_KEYBOARD,
            relief="flat",
            width=width,
            pady=KEYBOARD_KEY_PADY,
            borderwidth=0,
            highlightthickness=0,
            takefocus=0,
        )
        self._bind_touch_release(button, invoke_once)
        return button

    def _bind_touch_release(self, button, command):
        def on_release(event):
            if str(button.cget("state")) == tk.DISABLED:
                return None
            if button.winfo_containing(event.x_root, event.y_root) is button:
                command()
                return "break"
            return None

        button.bind("<ButtonRelease-1>", on_release, add="+")

    def _finalize_modal(self, dialog, focus_widget=None, parent=None, geometry=None):
        if parent is not None:
            dialog.transient(parent)
        if geometry is not None:
            dialog.geometry(geometry)
        dialog.update_idletasks()
        try:
            dialog.wait_visibility()
        except tk.TclError:
            pass
        try:
            dialog.lift()
            dialog.grab_set()
        except tk.TclError:
            pass
        if focus_widget is not None and focus_widget.winfo_exists():
            focus_widget.focus_force()
        else:
            try:
                dialog.focus_force()
            except tk.TclError:
                pass

    def _fit_modal_to_screen(self, dialog, max_width=900, min_height=200):
        """Size and center a modal so all packed content fits (avoids clipping buttons on small displays)."""
        dialog.update_idletasks()
        sw = max(1, dialog.winfo_screenwidth())
        sh = max(1, dialog.winfo_screenheight())
        margin = 16
        req_w = dialog.winfo_reqwidth()
        req_h = dialog.winfo_reqheight()
        w = min(max(req_w + 8, 400), min(max_width, sw - 2 * margin))
        h = min(max(req_h + 8, min_height), sh - 2 * margin)
        x = max(margin, (sw - w) // 2)
        y = max(margin, (sh - h) // 2)
        dialog.geometry(f"{w}x{h}+{x}+{y}")

    def _build_touch_keyboard(self):
        self._keyboard_rows_frame = tk.Frame(self.keyboard, bg=ENTRY_BG)
        self._keyboard_rows_frame.pack(anchor="center", fill="x")
        self._render_touch_keyboard()

    def _render_touch_keyboard(self):
        for child in self._keyboard_rows_frame.winfo_children():
            child.destroy()

        for spec in US_ANSI_KEY_ROWS:
            row = tk.Frame(self._keyboard_rows_frame, bg=ENTRY_BG)
            row.pack(anchor="center", pady=KEYBOARD_ROW_PADY)
            if spec.get("pad", 0):
                tk.Frame(
                    row, width=spec["pad"] * KEYBOARD_ROW_INDENT_UNIT, bg=ENTRY_BG
                ).pack(side="left")
            for key, shifted in spec["keys"]:
                value = shifted if self._keyboard_shift else key
                self._make_keyboard_button(
                    row, value, lambda insert_value=value: self._keyboard_insert(insert_value)
                ).pack(side="left", padx=KEYBOARD_KEY_PADX)

        controls = tk.Frame(self._keyboard_rows_frame, bg=ENTRY_BG)
        controls.pack(anchor="center", pady=KEYBOARD_CONTROLS_PADY)

        shift_text = "Shift" if not self._keyboard_shift else "SHIFT"
        self._make_keyboard_button(
            controls, shift_text, self._keyboard_toggle_shift, width=KEYBOARD_CTRL_SHIFT_W
        ).pack(side="left", padx=KEYBOARD_CONTROL_PADX)
        self._make_keyboard_button(
            controls, "Space", lambda: self._keyboard_insert(" "), width=KEYBOARD_CTRL_SPACE_W
        ).pack(side="left", padx=KEYBOARD_CONTROL_PADX)
        self._make_keyboard_button(
            controls, "Backspace", self._keyboard_backspace, width=KEYBOARD_CTRL_BACKSPACE_W
        ).pack(side="left", padx=KEYBOARD_CONTROL_PADX)
        self._make_keyboard_button(
            controls, "Clear", self._keyboard_clear, width=KEYBOARD_CTRL_SMALL_W
        ).pack(side="left", padx=KEYBOARD_CONTROL_PADX)
        self._make_keyboard_button(
            controls, "Hide", self._hide_touch_keyboard, width=KEYBOARD_CTRL_SMALL_W
        ).pack(side="left", padx=KEYBOARD_CONTROL_PADX)

    def _show_touch_keyboard(self, entry):
        self._focused_entry = entry
        if not self.keyboard.winfo_ismapped():
            # keyboard packs into the root window (self), which is also the
            # parent of outer.  keyboard side=bottom sits below outer, so it
            # can never overlap nav or content inside outer — no lift needed.
            self.keyboard.pack(side="bottom", fill="x")

    def _hide_touch_keyboard(self):
        if self.keyboard.winfo_manager():
            self.keyboard.pack_forget()
            # Flush pending layout: pack_forget queues a resize of outer as an
            # idle task.  Without this, the new step renders while outer is still
            # sized as if the keyboard is present, leaving nav mispositioned.
            self.update_idletasks()

    def _keyboard_toggle_shift(self):
        self._keyboard_shift = not self._keyboard_shift
        self._render_touch_keyboard()

    def _keyboard_insert(self, value):
        entry = self._focused_entry
        if not entry or not entry.winfo_exists():
            return
        entry.insert(tk.INSERT, value)
        entry.focus_set()

    def _keyboard_backspace(self):
        entry = self._focused_entry
        if not entry or not entry.winfo_exists():
            return
        try:
            start = entry.index("sel.first")
            end = entry.index("sel.last")
            entry.delete(start, end)
        except tk.TclError:
            cursor = entry.index(tk.INSERT)
            if cursor > 0:
                entry.delete(cursor - 1)
        entry.focus_set()

    def _keyboard_clear(self):
        entry = self._focused_entry
        if not entry or not entry.winfo_exists():
            return
        entry.delete(0, tk.END)
        entry.focus_set()

    def _clear_content(self):
        for child in self.content.winfo_children():
            child.destroy()

    def _render_current_step(self):
        self._hide_touch_keyboard()
        self._clear_content()
        step_name = self.steps[self.step_index].__name__
        self.progress_label.config(
            text=f"Step {self.step_index + 1} of {len(self.steps)}"
        )
        self.btn_back.config(state="normal" if self.step_index > 0 else "disabled")
        self.btn_next.config(
            text="Finish" if self.step_index == len(self.steps) - 1 else "Next >"
        )
        if step_name == "_step_accessory_checks":
            self.btn_recheck_accessories.pack(
                side="left", padx=(0, 15), before=self.btn_next
            )
        else:
            self.btn_recheck_accessories.pack_forget()
        self.steps[self.step_index]()

    def _next_index(self, index):
        next_index = index + 1
        while next_index < len(self.steps):
            name = self.steps[next_index].__name__
            if name == "_step_wifi_ssid_manual" and not self._wifi_ssid_manual_flow:
                next_index += 1
                continue
            if name == "_step_wifi_password" and not self.answers.get("wifi_ssid"):
                next_index += 1
                continue
            break
        return next_index

    def _previous_index(self, index):
        previous_index = index - 1
        while previous_index >= 0:
            name = self.steps[previous_index].__name__
            if name == "_step_wifi_ssid_manual" and not self._wifi_ssid_manual_flow:
                previous_index -= 1
                continue
            if name == "_step_wifi_password" and not self.answers.get("wifi_ssid"):
                previous_index -= 1
                continue
            break
        return previous_index

    def _on_next(self):
        if not self._validate_current_step():
            return
        if self._maybe_branch_wifi_network_collection_after_password():
            return
        if self.step_index < len(self.steps) - 1:
            self.step_index = self._next_index(self.step_index)
            self._render_current_step()
        else:
            self._on_finish()

    def _on_back(self):
        if self.step_index > 0:
            self.step_index = self._previous_index(self.step_index)
            self._render_current_step()

    def _recheck_accessories(self):
        if self.steps[self.step_index].__name__ == "_step_accessory_checks":
            self._render_current_step()

    def _refresh_wifi_scan(self):
        dialog, _body = self._show_busy_dialog(
            "Scanning Wi-Fi",
            "Enabling Wi-Fi radio, applying country settings, and scanning for nearby networks.",
        )
        try:
            self.update_idletasks()
            country = self.answers.get("wifi_country", DEFAULT_WIFI_COUNTRY)
            self.wifi_ssids, self.wifi_scan_message = scan_wifi_ssids(country)
        finally:
            dialog.grab_release()
            dialog.destroy()
            self.update_idletasks()

    def _normalize_wifi_networks_for_export(self):
        raw = self.answers.get("wifi_networks")
        if not isinstance(raw, list):
            raw = []
        if not raw and (self.answers.get("wifi_ssid") or "").strip():
            raw = [
                {
                    "ssid": (self.answers.get("wifi_ssid") or "").strip(),
                    "password": self.answers.get("wifi_password") or "",
                    "hidden": bool(self.answers.get("wifi_hidden")),
                }
            ]
        self.answers["wifi_networks"] = raw
        if raw:
            first = raw[0]
            self.answers["wifi_ssid"] = (first.get("ssid") or "").strip()
            self.answers["wifi_password"] = first.get("password") or ""
            self.answers["wifi_hidden"] = bool(first.get("hidden"))
        else:
            self.answers["wifi_ssid"] = ""
            self.answers["wifi_password"] = ""
            self.answers["wifi_hidden"] = False

    def _sync_wifi_flat_from_primary_network(self):
        nets = self.answers.get("wifi_networks")
        if not isinstance(nets, list) or not nets:
            return
        first = nets[0]
        self.answers["wifi_ssid"] = (first.get("ssid") or "").strip()
        self.answers["wifi_password"] = first.get("password") or ""
        self.answers["wifi_hidden"] = bool(first.get("hidden"))

    def _clear_draft_wifi_for_additional_network(self):
        self.answers["wifi_ssid"] = ""
        self.answers["wifi_password"] = ""
        self.answers["wifi_hidden"] = False
        self.answers.pop("wifi_tested", None)
        self.answers.pop("wifi_test_ssid", None)
        self.answers.pop("wifi_test_hidden", None)
        self.answers.pop("wifi_test_passed", None)
        self.answers.pop("wifi_continue_anyway", None)
        self.answers.pop("wifi_internet_reachable", None)
        self.answers.pop("wifi_test_message", None)
        self.answers.pop("connectivity_continue_anyway", None)
        self.answers.pop("connectivity_checks_last_report", None)
        self._last_wifi_test_signature = None
        self._wifi_ssid_manual_flow = False

    def _maybe_branch_wifi_network_collection_after_password(self):
        if self.steps[self.step_index].__name__ != "_step_wifi_password":
            return False
        ssid = (self.answers.get("wifi_ssid") or "").strip()
        if not ssid:
            return False
        pw = self.answers.get("wifi_password") or ""
        hidden = bool(self.answers.get("wifi_hidden"))
        nets = self.answers.setdefault("wifi_networks", [])
        if not isinstance(nets, list):
            nets = []
            self.answers["wifi_networks"] = nets
        if any((n.get("ssid") or "").strip() == ssid for n in nets):
            self._show_styled_error_modal(
                "Duplicate Wi-Fi",
                "This network is already in your saved list. Choose a different SSID or go Back.",
            )
            return True
        nets.append({"ssid": ssid, "password": pw, "hidden": hidden})
        if self._ask_add_another_wifi_network():
            self._clear_draft_wifi_for_additional_network()
            self.step_index = self.steps.index(self._step_wifi_ssid_pick)
            try:
                self.grab_release()
            except tk.TclError:
                pass
            # Run render next tick so nav.lift() wins over pending WM/focus events.
            self.after(0, self._render_current_step)
            return True
        self._sync_wifi_flat_from_primary_network()
        return False

    def _on_finish(self):
        if not self._confirm_destructive_provision():
            return

        self._normalize_wifi_networks_for_export()

        try:
            output_path = Path(self.output_path)
            output_path.write_text(
                json.dumps(self.answers, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(output_path, 0o600)
        except OSError as exc:
            self._show_styled_error_modal(
                "Save failed",
                f"Could not write {self.output_path}:\n{exc}",
            )
            return

        print(json.dumps(self.answers, indent=2, sort_keys=True))
        reg_host, _reg_port = self._registry_probe_target()
        self._warn_critical_dns_if_needed(registry_hostname=reg_host)
        if not self._launch_backend():
            return
        self.withdraw()

    def _ask_add_another_wifi_network(self):
        """Styled modal matching the wizard. Returns True to add another network."""
        dialog = tk.Toplevel(self)
        dialog.title("Add another network?")
        dialog.configure(bg=BG)
        result = tk.StringVar(value="no")

        def choose(value):
            result.set(value)
            dialog.destroy()

        tk.Label(
            dialog,
            text="Add another Wi-Fi network?",
            bg=BG,
            fg=FG,
            font=FONT_TITLE,
        ).pack(anchor="w", padx=40, pady=(30, 15))
        tk.Label(
            dialog,
            text=(
                "Each saved network will be written to the new system so it can connect "
                "wherever those networks are in range.\n\n"
                "Do you want to add another Wi-Fi network now?"
            ),
            bg=BG,
            fg=FG,
            font=FONT_LABEL,
            wraplength=620,
            justify="left",
        ).pack(anchor="w", padx=40, pady=(0, 30))

        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(fill="x", padx=40, pady=(0, 35))
        no_button = self._make_button(buttons, "No", lambda: choose("no"))
        no_button.config(padx=60, pady=24, width=8)
        no_button.pack(side="left")
        yes_button = self._make_button(buttons, "Yes", lambda: choose("yes"), primary=True)
        yes_button.config(padx=60, pady=24, width=8)
        yes_button.pack(side="right")

        dialog.protocol("WM_DELETE_WINDOW", lambda: choose("no"))
        self._fit_modal_to_screen(dialog, max_width=720, min_height=220)
        self._finalize_modal(dialog, focus_widget=yes_button, parent=self, geometry=None)
        dialog.wait_window()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.focus_force()
        return result.get() == "yes"

    def _confirm_destructive_provision(self):
        dialog = tk.Toplevel(self)
        dialog.title("Install new system?")
        dialog.configure(bg=BG)

        result = tk.BooleanVar(value=False)

        def choose(value):
            result.set(value)
            dialog.destroy()

        tk.Label(
            dialog,
            text="Install new system?",
            bg=BG,
            fg=FG,
            font=FONT_TITLE,
        ).pack(anchor="w", padx=40, pady=(30, 15))
        tk.Label(
            dialog,
            text=(
                "This will erase the device's internal storage drive and install a fresh system on it. "
                "That erase step is expected: it clears the target drive so the new setup can be written.\n\n"
                "If this is a new system, there is probably nothing on that drive to lose. "
                "If the drive already has data you care about, stop now because that data will be lost.\n\n"
                "Start provisioning now?"
            ),
            bg=BG,
            fg=FG,
            font=FONT_LABEL,
            wraplength=620,
            justify="left",
        ).pack(anchor="w", padx=40, pady=(0, 30))

        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(fill="x", padx=40, pady=(0, 35))
        no_button = self._make_button(buttons, "No", lambda: choose(False))
        no_button.config(padx=60, pady=24, width=8)
        no_button.pack(side="left")
        yes_button = self._make_button(buttons, "Yes", lambda: choose(True), primary=True)
        yes_button.config(padx=60, pady=24, width=8)
        yes_button.pack(side="right")

        dialog.protocol("WM_DELETE_WINDOW", lambda: choose(False))
        dialog.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dialog.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dialog.winfo_height()) // 2)
        self._finalize_modal(dialog, focus_widget=yes_button, parent=self, geometry=f"+{x}+{y}")
        dialog.wait_window()
        self.focus_force()

        if not result.get():
            return False

        self.answers["confirm_erase"] = "ERASE"
        return True

    def _launch_backend(self):
        backend = Path(__file__).resolve().parent / "provision_nvme.sh"
        if not backend.is_file():
            self._show_styled_error_modal(
                "Backend missing",
                f"Could not find provisioning backend:\n{backend}",
            )
            return False

        provision_wrapper = Path("/usr/local/sbin/hb-provision-nvme")
        if provision_wrapper.is_file():
            backend_args = ["sudo", "-n", str(provision_wrapper)]
        else:
            backend_args = ["sudo", "bash", str(backend), "--answers", str(self.output_path)]

        dialog, status_label, log_text, close_button = self._show_provision_log_window()
        output_queue = queue.Queue()
        completion_shown = False

        try:
            process = subprocess.Popen(
                backend_args,
                cwd=str(backend.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            dialog.destroy()
            self._show_styled_error_modal(
                "Launch failed",
                f"Could not start provisioning backend:\n{exc}",
            )
            return False

        self._append_provision_log(
            log_text,
            "Starting NVMe provisioning backend...\n"
            f"$ {' '.join(backend_args)}\n\n",
        )

        def read_output():
            try:
                if process.stdout is not None:
                    for line in process.stdout:
                        output_queue.put(("line", line))
            except OSError as exc:
                output_queue.put(("line", f"\nError reading provisioning output: {exc}\n"))
            output_queue.put(("done", process.wait()))

        def close_after_exit():
            if dialog.winfo_exists():
                dialog.destroy()
            self.destroy()

        def ignore_close_while_running():
            status_label.config(
                text="Provisioning is still running. This window will close after reboot or when the backend exits."
            )

        def poll_output():
            nonlocal completion_shown
            finished = False
            exit_status = None
            while True:
                try:
                    kind, payload = output_queue.get_nowait()
                except queue.Empty:
                    break

                if kind == "line":
                    self._append_provision_log(log_text, payload)
                    if not completion_shown and PROVISION_COMPLETE_MARKER in payload:
                        completion_shown = True
                        status_label.config(
                            text="Provisioning complete. Read the final instructions and click Reboot when ready."
                        )
                        close_button.config(state="disabled")
                        try:
                            dialog.grab_release()
                        except tk.TclError:
                            pass
                        self._show_provision_complete_dialog(dialog)
                elif kind == "done":
                    finished = True
                    exit_status = payload

            if finished:
                if completion_shown and exit_status == 0:
                    return

                if exit_status == 0:
                    self._append_provision_log(log_text, "\nProvisioning complete.\n")
                    status_label.config(
                        text="Provisioning complete. Read the final instructions and click Reboot when ready."
                    )
                    close_button.config(state="disabled")
                    try:
                        dialog.grab_release()
                    except tk.TclError:
                        pass
                    self._show_provision_complete_dialog(dialog)
                    return

                self._append_provision_log(
                    log_text,
                    f"\nProvisioning exited with status {exit_status}.\n",
                )
                status_label.config(
                    text=f"Provisioning exited with status {exit_status}. Review the log before closing."
                )
                close_button.config(state="normal")
                dialog.protocol("WM_DELETE_WINDOW", close_after_exit)
                try:
                    dialog.grab_release()
                except tk.TclError:
                    pass
                return

            if dialog.winfo_exists():
                self.after(100, poll_output)

        dialog.protocol("WM_DELETE_WINDOW", ignore_close_while_running)
        threading.Thread(target=read_output, daemon=True).start()
        self.after(100, poll_output)
        return True

    def _selected_mesh_workgroup(self):
        candidates = []
        section = self.answers.get("defaults_section", "").strip()
        group = self.answers.get("defaults_group", "").strip()

        if section:
            candidates.append(section)
            if "." in section:
                candidates.append(section.rsplit(".", 1)[0])
        if group:
            candidates.append(group)

        for candidate in dict.fromkeys(candidates):
            if not self.config.has_section(candidate):
                continue
            mesh_workgroup = self.config.get(candidate, "mesh_workgroup", fallback="").strip()
            if mesh_workgroup:
                return mesh_workgroup.replace(".", "-")

        return group.replace(".", "-") if group else ""

    def _provision_running_message(self):
        message = (
            "This will take about 5-10 mins. "
            "When it is complete, you will be asked to reboot this device."
        )
        mesh_workgroup = self._selected_mesh_workgroup()
        if mesh_workgroup:
            message += f" Visit dserv.net/w/{mesh_workgroup} after reboot "
            hostname = self.answers.get("hostname", "").strip()
            if hostname:
                message += f" and select {hostname} when it appears."
        return message

    def _provision_complete_web_message(self):
        mesh_workgroup = self._selected_mesh_workgroup()
        if mesh_workgroup:
            return f"From a separate computer on the same network, visit dserv.net/w/{mesh_workgroup}."
        return "From a separate computer on the same network, visit the dserv webpage for this system."

    def _provision_complete_device_message(self):
        hostname = self.answers.get("hostname", "").strip()
        if hostname:
            return f"After reboot, the newly provisioned device '{hostname}' should appear on that page soon."
        return "After reboot, the newly provisioned device should appear on that page soon."

    def _show_provision_complete_dialog(self, parent):
        dialog = tk.Toplevel(self)
        dialog.title("Provisioning complete")
        dialog.configure(bg=BG)

        tk.Label(
            dialog,
            text="Provisioning complete",
            bg=BG,
            fg=SUCCESS,
            font=FONT_TITLE,
        ).pack(anchor="w", padx=35, pady=(30, 12))

        message = (
            "The new system has been installed on the device's internal drive.\n\n"
            f"{self._provision_complete_web_message()}\n\n"
            "Click Reboot to finish setup and start from the newly installed system. "
            f"{self._provision_complete_device_message()}\n\n"
            "When the device finishes rebooting, its local screen may stay black. "
            "That is expected for this setup."
        )
        tk.Label(
            dialog,
            text=message,
            bg=BG,
            fg=FG,
            font=FONT_LABEL,
            justify="left",
            wraplength=780,
        ).pack(anchor="w", fill="x", padx=35, pady=(0, 25))

        footer = tk.Frame(dialog, bg=BG)
        footer.pack(fill="x", padx=35, pady=(0, 30))
        reboot_button = self._make_button(
            footer,
            "Reboot",
            lambda: self._request_reboot_from_completion(dialog, reboot_button),
            primary=True,
        )
        reboot_button.config(padx=60, pady=24)
        reboot_button.pack(side="right")

        dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        self._finalize_modal(
            dialog,
            focus_widget=reboot_button,
            parent=parent,
            geometry="860x430+210+120",
        )

    def _request_reboot_from_completion(self, dialog, reboot_button):
        reboot_button.config(state="disabled", text="Rebooting...")
        dialog.update_idletasks()
        try:
            Path(REBOOT_REQUEST_FILE).write_text(
                f"reboot requested by GUI pid {os.getpid()} at {time.time()}\n",
                encoding="utf-8",
            )
        except OSError as exc:
            reboot_button.config(state="normal", text="Reboot")
            self._show_styled_error_modal(
                "Reboot failed",
                "Could not request reboot from the provisioning backend.\n\n"
                f"{exc}",
            )
            return

        tk.Label(
            dialog,
            text="Reboot requested. The device should restart momentarily.",
            bg=BG,
            fg=SUCCESS,
            font=FONT_LABEL,
        ).pack(anchor="w", padx=35, pady=(0, 20))

    def _show_provision_log_window(self):
        dialog = tk.Toplevel(self)
        dialog.title("NVMe provisioning log")
        dialog.configure(bg=BG)
        dialog.minsize(800, 500)

        tk.Label(
            dialog,
            text="Provisioning System",
            bg=BG,
            fg=FG,
            font=FONT_TITLE,
        ).pack(anchor="w", padx=30, pady=(25, 8))

        status_label = tk.Label(
            dialog,
            text=self._provision_running_message(),
            bg=BG,
            fg=MUTED,
            font=FONT_LABEL,
            justify="left",
            wraplength=1040,
        )
        status_label.pack(anchor="w", fill="x", padx=30, pady=(0, 16))

        log_frame = tk.Frame(dialog, bg=ENTRY_BG, padx=2, pady=2)
        log_frame.pack(fill="both", expand=True, padx=30, pady=(0, 18))

        log_text = tk.Text(
            log_frame,
            bg="#11111b",
            fg=FG,
            insertbackground=FG,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("DejaVu Sans Mono", 11),
            wrap="word",
            padx=12,
            pady=10,
            state="disabled",
            takefocus=0,
            cursor="arrow",
        )
        log_text.pack(side="left", fill="both", expand=True)
        log_text.bind("<FocusIn>", lambda _event: dialog.focus_set(), add="+")
        log_text.bind("<Button-1>", lambda _event: "break", add="+")

        scrollbar = tk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
        scrollbar.pack(side="right", fill="y")
        log_text.config(yscrollcommand=scrollbar.set)

        footer = tk.Frame(dialog, bg=BG)
        footer.pack(fill="x", padx=30, pady=(0, 25))
        close_button = self._make_button(footer, "Close", lambda: self.destroy())
        close_button.config(state="disabled")
        close_button.pack(side="right")

        self._finalize_modal(dialog, geometry="1120x720+80+50")
        return dialog, status_label, log_text, close_button

    def _append_provision_log(self, log_text, text):
        if not log_text.winfo_exists():
            return
        log_text.config(state="normal")
        log_text.insert("end", text)
        log_text.see("end")
        log_text.config(state="disabled")

    # ------------------------------------------------------------------
    # Helpers for building consistent step UIs
    # ------------------------------------------------------------------
    def _add_title(self, text):
        tk.Label(
            self.content, text=text, bg=BG, fg=FG, font=FONT_TITLE
        ).pack(anchor="w", pady=(0, 20))

    def _add_label(self, text, fg=FG):
        tk.Label(
            self.content,
            text=text,
            bg=BG,
            fg=fg,
            font=FONT_LABEL,
            justify="left",
            wraplength=1160,
        ).pack(anchor="w", pady=(10, 5))

    def _add_entry(self, initial=""):
        var = tk.StringVar(value=initial)
        entry = tk.Entry(
            self.content,
            textvariable=var,
            font=FONT_INPUT,
            bg=ENTRY_BG,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            highlightthickness=2,
            highlightbackground=ENTRY_BG,
            highlightcolor=ACCENT,
        )
        entry.pack(fill="x", ipady=8, pady=5)
        entry.bind("<FocusIn>", lambda _event, widget=entry: self._show_touch_keyboard(widget), add="+")
        entry.bind("<Button-1>", lambda _event, widget=entry: self._show_touch_keyboard(widget), add="+")
        return var, entry

    def _add_listbox(
        self,
        entries,
        selected_value="",
        *,
        max_visible_rows=5,
        parent=None,
        list_frame_pack=None,
        clamp_height_to_entries=True,
    ):
        var = tk.StringVar(value=selected_value)
        list_parent = parent if parent is not None else self.content
        pack_kw = list_frame_pack if list_frame_pack is not None else {"fill": "x", "pady": 5}
        list_frame = tk.Frame(list_parent, bg=BG)
        list_frame.pack(**pack_kw)

        if clamp_height_to_entries:
            list_height = min(max_visible_rows, max(1, len(entries)))
        else:
            list_height = max(1, max_visible_rows)

        listbox = tk.Listbox(
            list_frame,
            font=FONT_INPUT,
            bg=ENTRY_BG,
            fg=FG,
            selectbackground=ACCENT,
            selectforeground=BG,
            relief="flat",
            highlightthickness=2,
            highlightbackground=ENTRY_BG,
            highlightcolor=ACCENT,
            height=list_height,
            activestyle="none",
        )
        for item in entries:
            listbox.insert("end", item)
        listbox.pack(side="left", fill="x", expand=True)

        scrollbar = tk.Scrollbar(list_frame, command=listbox.yview)
        scrollbar.pack(side="right", fill="y")
        listbox.config(yscrollcommand=scrollbar.set)

        if selected_value in entries:
            idx = entries.index(selected_value)
            listbox.selection_set(idx)
            listbox.see(idx)

        def on_select(_event):
            sel = listbox.curselection()
            if sel:
                var.set(listbox.get(sel[0]))

        listbox.bind("<<ListboxSelect>>", on_select)
        return var, listbox

    def _show_busy_dialog(self, title, text):
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.configure(bg=BG)
        dialog.minsize(640, 200)
        tk.Label(
            dialog,
            text=title,
            bg=BG,
            fg=FG,
            font=FONT_TITLE,
        ).pack(anchor="w", padx=30, pady=(25, 10))
        body = tk.Label(
            dialog,
            text=text,
            bg=BG,
            fg=FG,
            font=FONT_LABEL,
            justify="left",
            wraplength=680,
        )
        body.pack(anchor="w", fill="x", padx=30, pady=(0, 25))
        self._finalize_modal(dialog, parent=self, geometry="760x220+260+180")
        return dialog, body

    def _show_timed_message(self, title, text, milliseconds=2000):
        dialog, _body = self._show_busy_dialog(title, text)
        dialog.after(milliseconds, dialog.destroy)
        self.wait_window(dialog)

    def _show_styled_alert_modal(self, title, body_text, *, kind="error", wraplength=620):
        """Themed alert: kind is 'error' (rose title) or 'warning' (accent title)."""
        title_fg = ACCENT if kind == "warning" else ERROR
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.configure(bg=BG)
        tk.Label(
            dialog,
            text=title,
            bg=BG,
            fg=title_fg,
            font=FONT_TITLE,
        ).pack(anchor="w", padx=40, pady=(30, 15))
        tk.Label(
            dialog,
            text=body_text,
            bg=BG,
            fg=FG,
            font=FONT_LABEL,
            justify="left",
            wraplength=wraplength,
        ).pack(anchor="w", padx=40, pady=(0, 30))
        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(fill="x", padx=40, pady=(0, 35))
        ok_button = self._make_button(buttons, "OK", dialog.destroy, primary=True)
        ok_button.config(padx=60, pady=20, width=10)
        ok_button.pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self._fit_modal_to_screen(dialog, max_width=720, min_height=200)
        self._finalize_modal(dialog, focus_widget=ok_button, parent=self, geometry=None)
        dialog.wait_window()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.focus_force()

    def _show_styled_error_modal(self, title, body_text, **kw):
        self._show_styled_alert_modal(title, body_text, kind="error", **kw)

    def _show_styled_warning_modal(self, title, body_text, **kw):
        self._show_styled_alert_modal(title, body_text, kind="warning", **kw)

    def _ask_styled_ok_cancel(self, title, body_text, *, wraplength=620):
        """Same look as other modals; returns True for OK, False for Cancel or close."""
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.configure(bg=BG)
        result = tk.BooleanVar(value=False)

        def ok():
            result.set(True)
            dialog.destroy()

        def cancel():
            result.set(False)
            dialog.destroy()

        tk.Label(
            dialog,
            text=title,
            bg=BG,
            fg=ACCENT,
            font=FONT_TITLE,
        ).pack(anchor="w", padx=40, pady=(30, 15))
        tk.Label(
            dialog,
            text=body_text,
            bg=BG,
            fg=FG,
            font=FONT_LABEL,
            justify="left",
            wraplength=wraplength,
        ).pack(anchor="w", padx=40, pady=(0, 30))
        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(fill="x", padx=40, pady=(0, 35))
        cancel_button = self._make_button(buttons, "Cancel", cancel)
        cancel_button.config(padx=50, pady=20, width=10)
        cancel_button.pack(side="left")
        ok_button = self._make_button(buttons, "OK", ok, primary=True)
        ok_button.config(padx=50, pady=20, width=10)
        ok_button.pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        self._fit_modal_to_screen(dialog, max_width=720, min_height=220)
        self._finalize_modal(dialog, focus_widget=ok_button, parent=self, geometry=None)
        dialog.wait_window()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.focus_force()
        return bool(result.get())

    def _warn_critical_dns_if_needed(self, registry_hostname=None):
        failures = provision_critical_dns_failures(registry_hostname)
        if not failures:
            return
        detail = "\n".join(f"  • {host}: {err}" for host, err in failures)
        self._show_styled_warning_modal(
            "DNS check for provisioning servers",
            "Could not resolve one or more host names required for downloads, apt, GitHub, or registry:\n\n"
            f"{detail}\n\n"
            "Provisioning may fail unless DNS works. "
            "If you use Wi-Fi or a strict network, check router DNS settings or /etc/resolv.conf.",
        )

    def _registry_probe_target(self):
        """(hostname, port) for mesh_host from the selected defaults section."""
        section = (self.answers.get("defaults_section") or "").strip()
        mesh = ""
        if section and self.config.has_section(section):
            mesh = self.config.get(section, "mesh_host", fallback="").strip()
        return parse_mesh_host_for_probe(mesh)

    def _confirm_connectivity_bypass(self):
        return self._ask_styled_ok_cancel(
            "Continue anyway?",
            "Provisioning expects every connectivity check to pass. If you continue anyway, "
            "the install may still fail during downloads, apt, GitHub, or the registry step.\n\n"
            "Continue anyway?",
        )

    def _connectivity_checklist_modal(self, rows, *, allow_redo_wifi=False):
        """Show per-check results.

        Returns one of retry | back | continue, or redo_wifi when allow_redo_wifi is True.
        Window close acts as Back (never implies a silent full Wi‑Fi retest).
        """
        valid_choices = {"retry", "back", "continue"}
        if allow_redo_wifi:
            valid_choices = valid_choices | {"redo_wifi"}

        dialog = tk.Toplevel(self)
        dialog.title("Connectivity check")
        dialog.configure(bg=BG)

        sw = max(480, self.winfo_screenwidth())
        wrap_px = max(280, min(760, sw - 80))

        tk.Label(
            dialog,
            text="Some connectivity checks failed",
            bg=BG,
            fg=ERROR,
            font=FONT_TITLE,
        ).pack(anchor="w", padx=30, pady=(18, 8))

        tk.Label(
            dialog,
            text="Each line must pass for provisioning to reliably reach downloads, apt, GitHub, and the mesh registry.",
            bg=BG,
            fg=FG,
            font=FONT_LABEL,
            justify="left",
            wraplength=wrap_px,
        ).pack(anchor="w", padx=30, pady=(0, 8))

        action = tk.StringVar(value="")

        dialog.protocol("WM_DELETE_WINDOW", lambda: action.set("back"))

        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(side="bottom", fill="x", padx=30, pady=(0, 20))
        retry_button = self._make_button(
            buttons, "Retry checks", lambda: action.set("retry"), primary=True
        )
        retry_button.pack(side="left")
        self._make_button(buttons, "Back", lambda: action.set("back")).pack(side="left", padx=15)
        if allow_redo_wifi:
            self._make_button(buttons, "Test Wi‑Fi again", lambda: action.set("redo_wifi")).pack(
                side="left", padx=15
            )
        self._make_button(buttons, "Continue anyway", lambda: action.set("continue")).pack(side="right")

        body_lines = summarize_connectivity_rows(rows)
        sh = max(360, self.winfo_screenheight())
        text_lines = max(8, min(22, (sh - 340) // 20))

        msg_frame = tk.Frame(dialog, bg=BG)
        msg_frame.pack(side="top", fill="both", expand=True, padx=30, pady=(0, 12))

        msg_widget = tk.Text(
            msg_frame,
            wrap="word",
            font=FONT_LABEL,
            bg=ENTRY_BG,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            highlightthickness=0,
            padx=12,
            pady=12,
            height=text_lines,
            width=max(36, wrap_px // 10),
            state="normal",
        )
        msg_widget.insert("1.0", body_lines or "(no detail)")
        msg_widget.configure(state="disabled")

        scroll = tk.Scrollbar(msg_frame, command=msg_widget.yview, bg=ENTRY_BG, troughcolor=BG)
        msg_widget.configure(yscrollcommand=scroll.set)
        msg_widget.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._fit_modal_to_screen(dialog, max_width=min(900, sw))
        self._finalize_modal(
            dialog,
            focus_widget=retry_button,
            parent=self,
            geometry=None,
        )
        dialog.wait_variable(action)
        choice = action.get()
        dialog.grab_release()
        dialog.destroy()
        self.focus_force()
        return choice if choice in valid_choices else "back"

    def _run_connectivity_gate(self, bind_iface=None):
        """Prompt until checks pass or user bypasses (Ethernet / default route path)."""
        reg_h, reg_p = self._registry_probe_target()
        while True:
            rows = connectivity_checks_report(reg_h, reg_p, bind_iface)
            if connectivity_report_all_ok(rows):
                self.answers["connectivity_continue_anyway"] = False
                self.answers.pop("connectivity_checks_last_report", None)
                return True

            self.answers["connectivity_checks_last_report"] = [dict(r) for r in rows]
            choice = self._connectivity_checklist_modal(rows)
            if choice == "retry":
                continue
            if choice == "back":
                return False
            if choice == "continue" and self._confirm_connectivity_bypass():
                self.answers["connectivity_continue_anyway"] = True
                return True
        return False

    def _connectivity_review_summary(self):
        if self.answers.get("connectivity_continue_anyway"):
            return "Bypassed — install may fail network steps"
        if self.answers.get("wifi_ssid") or self._wifi_saved_networks_list():
            if self.answers.get("wifi_internet_reachable"):
                return "All checks passed (Wi‑Fi path)"
            return "Incomplete"
        return "All checks passed (Ethernet)"

    def _ask_wifi_failure_action(self, message):
        dialog = tk.Toplevel(self)
        dialog.title("Wi-Fi test failed")
        dialog.configure(bg=BG)
        dialog.minsize(400, 220)

        sw = max(480, self.winfo_screenwidth())
        wrap_px = max(280, min(760, sw - 80))

        tk.Label(
            dialog,
            text="Wi-Fi test failed",
            bg=BG,
            fg=ERROR,
            font=FONT_TITLE,
        ).pack(anchor="w", padx=30, pady=(18, 8))

        action = tk.StringVar(value="")
        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(side="bottom", fill="x", padx=30, pady=(0, 20))
        retry_button = self._make_button(buttons, "Try Again", lambda: action.set("retry"), primary=True)
        retry_button.pack(side="left")
        self._make_button(buttons, "Edit Wi-Fi", lambda: action.set("edit")).pack(side="left", padx=15)
        self._make_button(buttons, "Continue Anyway", lambda: action.set("continue")).pack(side="right")

        body = (
            "We could not connect to this Wi-Fi network from the current location. "
            "The password may be wrong, or this device may be using Wi-Fi settings "
            "for another site.\n\n"
            f"{message}\n\n"
            "If you continue anyway, the password you entered will be written to the "
            "device as-is. If it is wrong, the device will not connect to Wi-Fi after restarting."
        )

        msg_frame = tk.Frame(dialog, bg=BG)
        msg_frame.pack(side="top", fill="both", expand=True, padx=30, pady=(0, 8))

        sh = max(360, self.winfo_screenheight())
        text_lines = max(5, min(16, (sh - 320) // 22))

        msg_widget = tk.Text(
            msg_frame,
            wrap="word",
            font=FONT_LABEL,
            bg=ENTRY_BG,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            highlightthickness=0,
            padx=12,
            pady=12,
            height=text_lines,
            width=max(36, wrap_px // 11),
            state="normal",
        )
        msg_widget.insert("1.0", body)
        msg_widget.configure(state="disabled")

        scroll = tk.Scrollbar(msg_frame, command=msg_widget.yview, bg=ENTRY_BG, troughcolor=BG)
        msg_widget.configure(yscrollcommand=scroll.set)
        msg_widget.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._fit_modal_to_screen(dialog, max_width=min(900, sw))
        self._finalize_modal(
            dialog,
            focus_widget=retry_button,
            parent=self,
            geometry=None,
        )
        dialog.wait_variable(action)
        choice = action.get()
        dialog.grab_release()
        dialog.destroy()
        return choice

    def _maybe_self_update(self, phase):
        if os.environ.get("HB_PROVISION_NO_SELF_UPDATE", "0").strip() == "1":
            print(
                "Self-update: disabled for this run (--no-self-update or HB_PROVISION_NO_SELF_UPDATE=1)."
            )
            self._self_update_retry_needed = False
            return

        reg_host, reg_port = self._registry_probe_target()
        if not have_internet(reg_host, reg_port):
            self._self_update_retry_needed = True
            print(f"Self-update: no internet during {phase}; will retry after internet is available.")
            return

        dialog, _body = self._show_busy_dialog(
            "Checking for updates",
            "Checking GitHub for the latest provisioning GUI and defaults.",
        )
        try:
            result = update_current_repo_if_needed(__file__)
        finally:
            dialog.grab_release()
            dialog.destroy()
            self.update_idletasks()

        if not result["ok"]:
            print(f"Self-update failed: {result['message']}")
            self._show_styled_warning_modal(
                "Update check failed",
                f"Could not update the provisioning GUI automatically.\n\n{result['message']}",
            )
            return

        self._self_update_retry_needed = False
        print(f"Self-update: {result['message']}")
        if result["updated"]:
            try:
                self._save_resume_state()
            except (OSError, TypeError) as exc:
                print(f"Resume state: could not save before restart: {exc}")
                self._show_styled_warning_modal(
                    "Resume save failed",
                    "The provisioning GUI was updated, but it could not save the current answers "
                    f"before restarting.\n\n{exc}",
                )
            self._show_timed_message(
                "Update installed",
                "The provisioning GUI was updated. Restarting now so the newest defaults are used.",
                milliseconds=3000,
            )
            os.execv(sys.executable, [sys.executable, *sys.argv])
        delete_resume_state()

    def _show_default_hint(self, key):
        value = self.answers.get(key, "")
        if value not in ("", None):
            self._add_label(f"Default: {value}", fg=MUTED)

    def _apply_initial_defaults_from_env(self):
        section = os.environ.get("DEVICE_DEFAULTS_SECTION", "").strip()
        group = os.environ.get("DEVICE_DEFAULTS_GROUP", "").strip()
        subgroup = os.environ.get("DEVICE_DEFAULTS_SUBGROUP", "").strip()

        if section and self.config.has_section(section):
            parts = section.split(".")
            if len(parts) >= 3:
                self.answers["defaults_group"] = ".".join(parts[:-1])
                self.answers["defaults_device_type"] = parts[-1]
                self.answers["defaults_section"] = section
                self._apply_defaults_section(section)
            return

        if group in self.groups:
            self.answers["defaults_group"] = group
            if subgroup:
                candidate = f"{group}.{subgroup}"
                if self.config.has_section(candidate):
                    self.answers["defaults_device_type"] = subgroup
                    self.answers["defaults_section"] = candidate
                    self._apply_defaults_section(candidate)

    def _apply_defaults_section(self, section):
        if not section or not self.config.has_section(section):
            return

        key_map = {
            "username": "username",
            "timezone": "timezone",
            "locale": "locale",
            "wifi_country": "wifi_country",
            "screen_pixels_width": "screen_pixels_width",
            "screen_pixels_height": "screen_pixels_height",
            "screen_refresh_rate": "screen_refresh_rate",
            "screen_rotation": "screen_rotation",
            "monitor_width_cm": "monitor_width_cm",
            "monitor_height_cm": "monitor_height_cm",
            "monitor_distance_cm": "monitor_distance_cm",
        }
        for ini_key, answer_key in key_map.items():
            value = self.config.get(section, ini_key, fallback="").strip()
            if value:
                self.answers[answer_key] = value

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    def _step_defaults_group(self):
        self._add_title("Choose a device profile")
        if not self.groups:
            self._add_label(
                f"No device defaults found at {self.defaults_path}. Built-in defaults will be used."
            )
            self._defaults_group_var = tk.StringVar(value="")
            return

        self._add_label("Pick the lab/device defaults to pre-fill the setup.")
        options = self.groups
        selected = self.answers.get("defaults_group") or self.groups[0]
        self._defaults_group_var, _ = self._add_listbox(options, selected)

    def _step_defaults_device_type(self):
        group = self.answers.get("defaults_group", "")
        if not group:
            self._add_title("Defaults skipped")
            self._add_label("No device defaults will be applied.")
            self._defaults_type_var = tk.StringVar(value="")
            return

        self._add_title("Choose the device type")
        self._add_label("This narrows the profile to the exact setup you are provisioning.")
        types = device_types_for_group(self.config, group)
        self._defaults_type_options = types
        self._defaults_type_var, _ = self._add_listbox(
            types,
            self.answers.get("defaults_device_type", types[0] if types else ""),
        )

    def _step_wifi_country(self):
        self._add_title("Wi-Fi country")
        self._add_label("Enter Wi-Fi country code (2 letters, e.g. US, CA, GB, DE, FR, JP).")
        self._show_default_hint("wifi_country")
        self._wifi_country_var, entry = self._add_entry(
            self.answers.get("wifi_country", DEFAULT_WIFI_COUNTRY)
        )
        entry.focus_set()

    def _step_timezone(self):
        self._add_title("Timezone")
        self._add_label("Enter timezone, e.g. America/New_York or Europe/London.")
        self._show_default_hint("timezone")
        self._timezone_var, entry = self._add_entry(
            self.answers.get("timezone", DEFAULT_TIMEZONE)
        )
        entry.focus_set()

    def _step_locale(self):
        self._add_title("Locale")
        self._add_label("Enter locale, e.g. en_us, en_gb, fr_fr, or de_de.")
        self._show_default_hint("locale")
        locale = self.answers.get("locale", DEFAULT_LOCALE)
        if locale.endswith(".UTF-8"):
            locale = locale[:-6].lower()
        self._locale_var, entry = self._add_entry(locale)
        entry.focus_set()

    def _step_screen_width(self):
        self._add_title("Display width")
        self._add_label("Enter the screen width in pixels.")
        self._show_default_hint("screen_pixels_width")
        self._screen_width_var, entry = self._add_entry(
            self.answers.get("screen_pixels_width", "")
        )
        entry.focus_set()

    def _step_screen_height(self):
        self._add_title("Display height")
        self._add_label("Pixels tall.")
        self._show_default_hint("screen_pixels_height")
        self._screen_height_var, entry = self._add_entry(
            self.answers.get("screen_pixels_height", "")
        )
        entry.focus_set()

    def _step_screen_refresh_rate(self):
        self._add_title("Display refresh rate")
        self._add_label("Enter the refresh rate in Hz.")
        self._show_default_hint("screen_refresh_rate")
        self._screen_refresh_var, entry = self._add_entry(
            self.answers.get("screen_refresh_rate", "")
        )
        entry.focus_set()

    def _step_screen_rotation(self):
        self._add_title("Display orientation correction")
        self._add_label(
            "This compensates for how the physical screen is mounted. If the default is 180, "
            "that usually means the monitor is intentionally mounted upside down and the "
            "software rotates the image so it appears upright."
        )
        rotation = self.answers.get("screen_rotation", DEFAULT_SCREEN_ROTATION)
        self._add_label(f"Default: {rotation} (recommended for this device profile)", fg=MUTED)
        self._screen_rotation_var, entry = self._add_entry(
            rotation
        )
        entry.focus_set()

    def _go_wifi_manual_ssid(self):
        self._wifi_ssid_manual_flow = True
        self.step_index = self.steps.index(self._step_wifi_ssid_manual)
        self._render_current_step()

    def _step_wifi_ssid_pick(self):
        self._wifi_ssid_pick_listbox = None
        self._add_title("Choose Wi-Fi network")
        self._add_label(
            "Select a network from the list and tap Next, or tap Next with nothing selected to use Ethernet. "
            "If your network does not appear after a rescan, use the button on the right."
        )
        self._refresh_wifi_scan()

        scan_row = tk.Frame(self.content, bg=BG)
        scan_row.pack(fill="x", pady=(0, 10))
        self._make_button(scan_row, "Rescan Wi-Fi", self._render_current_step).pack(side="left")
        if self.wifi_scan_message:
            tk.Label(
                scan_row,
                text=self.wifi_scan_message,
                bg=BG,
                fg=MUTED,
                font=FONT_LABEL,
                justify="left",
                wraplength=760,
            ).pack(side="left", padx=15)

        list_btn_row = tk.Frame(self.content, bg=BG)
        list_btn_row.pack(fill="x", pady=(0, 10))
        list_col = tk.Frame(list_btn_row, bg=BG)
        list_col.pack(side="left", fill="both", expand=True)

        if self.wifi_ssids:
            selected = self.answers.get("wifi_ssid", "")
            self._ssid_list_var, listbox = self._add_listbox(
                self.wifi_ssids,
                selected,
                max_visible_rows=10,
                parent=list_col,
                list_frame_pack={"fill": "both", "expand": True},
                clamp_height_to_entries=False,
            )
            self._wifi_ssid_pick_listbox = listbox
            listbox.bind("<<ListboxSelect>>", self._on_ssid_list_select, add="+")
        else:
            tk.Label(
                list_col,
                text="No scanned Wi-Fi networks found.",
                bg=BG,
                fg=MUTED,
                font=FONT_LABEL,
                justify="left",
                wraplength=560,
            ).pack(anchor="w", pady=(0, 8))
            self._ssid_list_var = tk.StringVar(value="")

        self._make_button(
            list_btn_row,
            "Specify SSID not on this list",
            self._go_wifi_manual_ssid,
        ).pack(side="right", padx=(12, 0), anchor="n")

    def _step_wifi_ssid_manual(self):
        self._add_title("Enter Wi-Fi network name")
        self._add_label("Type the SSID exactly. Use the checkbox if the network does not broadcast its name.")
        self._wifi_ssid_var, entry = self._add_entry(self.answers.get("wifi_ssid", ""))
        self._wifi_hidden_var = tk.BooleanVar(value=bool(self.answers.get("wifi_hidden")))
        hid_row = tk.Frame(self.content, bg=BG)
        hid_row.pack(fill="x", pady=(8, 0))
        tk.Checkbutton(
            hid_row,
            text="Hidden network (SSID not broadcast)",
            variable=self._wifi_hidden_var,
            bg=BG,
            fg=FG,
            selectcolor=ENTRY_BG,
            activebackground=BG,
            activeforeground=FG,
            font=FONT_LABEL,
        ).pack(anchor="w")
        entry.focus_set()

    def _on_ssid_list_select(self, _event):
        self.btn_next.focus_set()

    def _step_wifi_password(self):
        ssid = self.answers.get("wifi_ssid", "")
        self._add_title("Wi-Fi password")
        self._add_label(f"Password for {ssid} (shown). The connection will be tested before continuing.")
        self._wifi_password_var, entry = self._add_entry(
            self.answers.get("wifi_password", "")
        )
        entry.focus_set()

    def _step_accessory_checks(self):
        self._add_title("Accessory checks")
        self._add_label(
            "These checks confirm the expected accessories are visible from the current system. "
            "Missing accessories are shown as warnings and do not block provisioning."
        )
        self._run_accessory_checks()

    def _step_hostname(self):
        self._add_title("Name this device")
        self._add_label(
            "This name identifies the device on the network, in control tools, and when connecting with SSH."
        )
        hostname = self.answers.get("hostname", "")
        if hostname:
            self._add_label(f"Suggested: {hostname}", fg=MUTED)
        self._hostname_var, entry = self._add_entry(self.answers.get("hostname", ""))
        entry.focus_set()

    def _step_username(self):
        self._add_title("Create login user")
        self._add_label("Enter the username for signing in and SSH access.")
        self._show_default_hint("username")
        self._username_var, entry = self._add_entry(self.answers.get("username", ""))
        entry.focus_set()

    def _step_password(self):
        username = self.answers.get("username", "the user")
        self._add_title("Set login password")
        self._add_label(f"Password for {username} (shown). This will also be used for SSH.")
        self._password_var, entry = self._add_entry(self.answers.get("password", ""))
        entry.focus_set()

    def _step_monitor_width(self):
        self._add_title("Physical screen width")
        self._add_label("Enter the visible screen width in centimeters.")
        self._show_default_hint("monitor_width_cm")
        self._monitor_width_var, entry = self._add_entry(
            self.answers.get("monitor_width_cm", DEFAULT_MONITOR_WIDTH_CM)
        )
        entry.focus_set()

    def _step_monitor_height(self):
        self._add_title("Screen height (cm)")
        self._add_label("Visible height.")
        self._show_default_hint("monitor_height_cm")
        self._monitor_height_var, entry = self._add_entry(
            self.answers.get("monitor_height_cm", DEFAULT_MONITOR_HEIGHT_CM)
        )
        entry.focus_set()

    def _step_monitor_distance(self):
        self._add_title("Viewing distance")
        self._add_label("Enter the typical distance from the animal to the screen, in centimeters.")
        self._show_default_hint("monitor_distance_cm")
        self._monitor_distance_var, entry = self._add_entry(
            self.answers.get("monitor_distance_cm", DEFAULT_MONITOR_DISTANCE_CM)
        )
        entry.focus_set()

    def _run_accessory_checks(self):
        dialog, _body = self._show_busy_dialog(
            "Checking accessories",
            "Looking for touchscreen, juicer, power monitor, and camera.",
        )
        try:
            self.answers["accessory_checks"] = check_accessories()
        finally:
            dialog.grab_release()
            dialog.destroy()
            self.update_idletasks()
        self._render_accessory_results()

    def _render_accessory_results(self):
        results = self.answers.get("accessory_checks", {})
        scroll_shell = tk.Frame(self.content, bg=ENTRY_BG)
        scroll_shell.pack(fill="both", expand=True, pady=(10, 0))

        canvas = tk.Canvas(
            scroll_shell,
            bg=ENTRY_BG,
            highlightthickness=0,
            height=230,
        )
        scrollbar = tk.Scrollbar(scroll_shell, orient="vertical", command=canvas.yview)
        rows = tk.Frame(canvas, bg=ENTRY_BG, padx=20, pady=15)

        rows_window = canvas.create_window((0, 0), window=rows, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def update_scroll_region(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def update_inner_width(event):
            canvas.itemconfigure(rows_window, width=event.width)

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        rows.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", update_inner_width)
        canvas.bind("<MouseWheel>", on_mousewheel)
        canvas.bind("<Button-4>", lambda _event: canvas.yview_scroll(-1, "units"))
        canvas.bind("<Button-5>", lambda _event: canvas.yview_scroll(1, "units"))

        for key, label in ACCESSORY_CHECK_ITEMS:
            result = results.get(key, {})
            detected = bool(result.get("detected"))
            detail = result.get("detail", "")
            row = tk.Frame(rows, bg=ENTRY_BG)
            row.pack(fill="x", pady=4)
            tk.Label(
                row,
                text=label,
                bg=ENTRY_BG,
                fg=FG,
                font=FONT_LABEL,
                width=18,
                anchor="w",
            ).pack(side="left")
            tk.Label(
                row,
                text="Detected" if detected else "Not detected",
                bg=ENTRY_BG,
                fg=SUCCESS if detected else ERROR,
                font=FONT_LABEL,
                width=14,
                anchor="w",
            ).pack(side="left")
            tk.Label(
                row,
                text=detail,
                bg=ENTRY_BG,
                fg=MUTED,
                font=FONT_LABEL,
                anchor="w",
                wraplength=760,
                justify="left",
            ).pack(side="left", fill="x", expand=True)

    def _step_review(self):
        self._add_title("Review setup")
        self._add_label("Check these settings before starting provisioning.")

        scroll_shell = tk.Frame(self.content, bg=ENTRY_BG)
        scroll_shell.pack(fill="both", expand=True, pady=(5, 0))

        canvas = tk.Canvas(
            scroll_shell,
            bg=ENTRY_BG,
            highlightthickness=0,
            height=230,
        )
        scrollbar = tk.Scrollbar(scroll_shell, orient="vertical", command=canvas.yview)
        review_frame = tk.Frame(canvas, bg=ENTRY_BG, padx=20, pady=15)

        review_window = canvas.create_window((0, 0), window=review_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def update_scroll_region(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def update_inner_width(event):
            canvas.itemconfigure(review_window, width=event.width)

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        review_frame.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", update_inner_width)
        canvas.bind("<MouseWheel>", on_mousewheel)
        canvas.bind("<Button-4>", lambda _event: canvas.yview_scroll(-1, "units"))
        canvas.bind("<Button-5>", lambda _event: canvas.yview_scroll(1, "units"))

        rows = [
            ("Defaults", self.answers.get("defaults_section", "(skipped)")),
            ("Wi-Fi country", self.answers.get("wifi_country", "")),
            ("Wi-Fi SSID(s)", self._wifi_review_saved_ssids_text()),
            ("Wi-Fi hidden SSID", self._wifi_review_saved_hidden_text()),
            ("Wi-Fi test", self._wifi_test_summary()),
            ("Network / connectivity", self._connectivity_review_summary()),
            ("Accessory checks", self._accessory_check_summary()),
            ("Timezone", self.answers.get("timezone", "")),
            ("Locale", self.answers.get("locale", "")),
            ("Screen width (px)", self.answers.get("screen_pixels_width", "")),
            ("Monitor width (cm)", self.answers.get("monitor_width_cm", "")),
            ("Screen height (px)", self.answers.get("screen_pixels_height", "")),
            ("Monitor height (cm)", self.answers.get("monitor_height_cm", "")),
            ("Viewing distance (cm)", self.answers.get("monitor_distance_cm", "")),
            ("Refresh rate (Hz)", self.answers.get("screen_refresh_rate", "")),
            ("Screen rotation", self.answers.get("screen_rotation", "")),
            ("Hostname", self.answers.get("hostname", "")),
            ("Username", self.answers.get("username", "")),
            ("Password", self.answers.get("password", "")),
        ]
        for label, value in rows:
            row = tk.Frame(review_frame, bg=ENTRY_BG)
            row.pack(fill="x", pady=2)
            tk.Label(
                row,
                text=f"{label}:",
                bg=ENTRY_BG,
                fg=FG,
                font=FONT_REVIEW,
                width=22,
                anchor="w",
            ).pack(side="left")
            tk.Label(
                row,
                text=str(value),
                bg=ENTRY_BG,
                fg=ACCENT,
                font=FONT_REVIEW,
                anchor="w",
            ).pack(side="left", fill="x", expand=True)

    def _wifi_saved_networks_list(self):
        nets = self.answers.get("wifi_networks")
        if isinstance(nets, list) and nets:
            return nets
        return []

    def _wifi_review_saved_ssids_text(self):
        nets = self._wifi_saved_networks_list()
        if nets:
            return "; ".join((n.get("ssid") or "") for n in nets)
        return self.answers.get("wifi_ssid", "(skipped)") or "(skipped)"

    def _wifi_review_saved_hidden_text(self):
        nets = self._wifi_saved_networks_list()
        if nets:
            return "; ".join("Yes" if n.get("hidden") else "No" for n in nets)
        return "Yes" if self.answers.get("wifi_hidden") else "No"

    def _wifi_test_summary(self):
        if not self._wifi_saved_networks_list() and not self.answers.get("wifi_ssid"):
            return "(skipped)"
        if self.answers.get("wifi_continue_anyway"):
            return "Failed, continuing anyway (password / association)"
        if self.answers.get("connectivity_continue_anyway") and self.answers.get("wifi_test_passed"):
            return "Connected; connectivity bypassed"
        if self.answers.get("wifi_test_passed"):
            if self.answers.get("wifi_internet_reachable"):
                return "Connected, all checks passed"
            return "Connected, checks failed or bypassed"
        if not self.answers.get("wifi_tested"):
            return "Not tested"
        return "Failed"

    def _accessory_check_summary(self):
        checks = self.answers.get("accessory_checks", {})
        if not checks:
            return "Not checked"
        detected = [
            label
            for key, label in ACCESSORY_CHECK_ITEMS
            if checks.get(key, {}).get("detected")
        ]
        missing = [
            label
            for key, label in ACCESSORY_CHECK_ITEMS
            if not checks.get(key, {}).get("detected")
        ]
        summary = f"{len(detected)}/{len(ACCESSORY_CHECK_ITEMS)} detected"
        if missing:
            summary += f"; missing: {', '.join(missing)}"
        return summary

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _validate_current_step(self):
        step_name = self.steps[self.step_index].__name__

        if step_name == "_step_defaults_group":
            value = self._defaults_group_var.get()
            if not value:
                self._show_styled_error_modal("Required", "Please select a device profile.")
                return False
            self.answers["defaults_group"] = value
            self.answers.pop("defaults_device_type", None)
            self.answers.pop("defaults_section", None)

        elif step_name == "_step_defaults_device_type":
            group = self.answers.get("defaults_group", "")
            if not group:
                return True
            device_type = self._defaults_type_var.get().strip()
            if not device_type:
                self._show_styled_error_modal("Required", "Please select a device type.")
                return False
            section = f"{group}.{device_type}"
            if not self.config.has_section(section):
                self._show_styled_error_modal("Invalid", f"Defaults section not found: {section}")
                return False
            self.answers["defaults_device_type"] = device_type
            self.answers["defaults_section"] = section
            self._apply_defaults_section(section)

        elif step_name == "_step_wifi_country":
            value = self._wifi_country_var.get().strip().upper() or DEFAULT_WIFI_COUNTRY
            if not re.fullmatch(r"[A-Z]{2}", value):
                self._show_styled_error_modal("Invalid", "Wi-Fi country must be 2 letters like US.")
                return False
            self.answers["wifi_country"] = value

        elif step_name == "_step_timezone":
            value = self._timezone_var.get().strip() or DEFAULT_TIMEZONE
            if not Path("/usr/share/zoneinfo", value).is_file():
                self._show_styled_error_modal(
                    "Invalid",
                    "Timezone not found. Example: America/Los_Angeles, Europe/London, Asia/Tokyo.",
                )
                return False
            self.answers["timezone"] = value

        elif step_name == "_step_locale":
            value = self._locale_var.get().strip().lower() or DEFAULT_LOCALE
            if not re.fullmatch(r"[a-z]{2}_[a-z]{2}", value):
                self._show_styled_error_modal("Invalid", "Locale must look like en_us, en_gb, fr_fr, or de_de.")
                return False
            base = f"{value[:2]}_{value[3:].upper()}"
            if not Path("/usr/share/i18n/locales", base).is_file():
                self._show_styled_error_modal("Invalid", f"Locale not found on this system: {value}")
                return False
            self.answers["locale"] = f"{base}.UTF-8"

        elif step_name == "_step_screen_width":
            value = self._screen_width_var.get().strip()
            if value and not self._valid_int(value, 320, 7680):
                self._show_styled_error_modal("Invalid", "Screen width must be a number between 320 and 7680.")
                return False
            self.answers["screen_pixels_width"] = value

        elif step_name == "_step_screen_height":
            value = self._screen_height_var.get().strip()
            if value and not self._valid_int(value, 240, 4320):
                self._show_styled_error_modal("Invalid", "Screen height must be a number between 240 and 4320.")
                return False
            self.answers["screen_pixels_height"] = value

        elif step_name == "_step_screen_refresh_rate":
            value = self._screen_refresh_var.get().strip()
            if value and not self._valid_int(value, 1, 360):
                self._show_styled_error_modal("Invalid", "Refresh rate must be a number between 1 and 360.")
                return False
            self.answers["screen_refresh_rate"] = value

        elif step_name == "_step_screen_rotation":
            value = self._screen_rotation_var.get().strip() or DEFAULT_SCREEN_ROTATION
            if value not in {"0", "90", "180", "270"}:
                self._show_styled_error_modal("Invalid", "Screen rotation must be 0, 90, 180, or 270.")
                return False
            self.answers["screen_rotation"] = value

        elif step_name == "_step_wifi_ssid_pick":
            lb = getattr(self, "_wifi_ssid_pick_listbox", None)
            if lb is not None:
                sel = lb.curselection()
                value = lb.get(sel[0]).strip() if sel else ""
            else:
                value = ""
            hidden = False
            if "\n" in value or "\r" in value:
                self._show_styled_error_modal("Invalid", "Wi-Fi SSID cannot contain newline characters.")
                return False
            prev_hidden = bool(self.answers.get("wifi_hidden"))
            if value != self.answers.get("wifi_ssid") or hidden != prev_hidden:
                self._last_wifi_test_signature = None
                self.answers.pop("wifi_tested", None)
                self.answers.pop("wifi_test_ssid", None)
                self.answers.pop("wifi_test_hidden", None)
                self.answers.pop("wifi_test_passed", None)
                self.answers.pop("wifi_continue_anyway", None)
                self.answers.pop("wifi_internet_reachable", None)
                self.answers.pop("wifi_test_message", None)
                self.answers.pop("connectivity_continue_anyway", None)
                self.answers.pop("connectivity_checks_last_report", None)
            self.answers["wifi_ssid"] = value
            self.answers["wifi_hidden"] = False
            self._wifi_ssid_manual_flow = False
            if not value:
                if self.answers.get("wifi_networks"):
                    self._show_styled_error_modal(
                        "Wi-Fi required",
                        "Select a Wi-Fi network from the list, use “Specify SSID not on this list”, or go Back.",
                    )
                    return False
                self.answers["wifi_password"] = ""
                self.answers["wifi_hidden"] = False
                self.answers["wifi_tested"] = False
                self.answers["wifi_test_passed"] = False
                self.answers["wifi_continue_anyway"] = False
                self.answers["wifi_internet_reachable"] = False
                self.answers["connectivity_continue_anyway"] = False
                self.answers.pop("connectivity_checks_last_report", None)
                self._last_wifi_test_signature = None
                if not self._run_connectivity_gate(bind_iface=None):
                    return False
                self.answers["wifi_internet_reachable"] = not self.answers.get(
                    "connectivity_continue_anyway", False
                )
            return True

        elif step_name == "_step_wifi_ssid_manual":
            value = self._wifi_ssid_var.get().strip()
            hidden = bool(self._wifi_hidden_var.get())
            if not value:
                self._show_styled_error_modal(
                    "Required",
                    "Enter the Wi-Fi network name (SSID), or go Back to choose from the list.",
                )
                return False
            if "\n" in value or "\r" in value:
                self._show_styled_error_modal("Invalid", "Wi-Fi SSID cannot contain newline characters.")
                return False
            prev_hidden = bool(self.answers.get("wifi_hidden"))
            if value != self.answers.get("wifi_ssid") or hidden != prev_hidden:
                self._last_wifi_test_signature = None
                self.answers.pop("wifi_tested", None)
                self.answers.pop("wifi_test_ssid", None)
                self.answers.pop("wifi_test_hidden", None)
                self.answers.pop("wifi_test_passed", None)
                self.answers.pop("wifi_continue_anyway", None)
                self.answers.pop("wifi_internet_reachable", None)
                self.answers.pop("wifi_test_message", None)
                self.answers.pop("connectivity_continue_anyway", None)
                self.answers.pop("connectivity_checks_last_report", None)
            self.answers["wifi_ssid"] = value
            self.answers["wifi_hidden"] = hidden
            return True

        elif step_name == "_step_wifi_password":
            value = self._wifi_password_var.get()
            if not value:
                self._show_styled_error_modal("Required", "Wi-Fi password cannot be empty. Go Back to skip Wi-Fi.")
                return False
            if "\n" in value or "\r" in value:
                self._show_styled_error_modal("Invalid", "Wi-Fi password cannot contain newline characters.")
                return False
            self.answers["wifi_password"] = value
            ssid = self.answers.get("wifi_ssid", "")
            hidden_now = bool(self.answers.get("wifi_hidden"))
            test_signature = (ssid, value, hidden_now)
            already_tested = (
                self.answers.get("wifi_tested") is True
                and self.answers.get("wifi_test_ssid") == ssid
                and bool(self.answers.get("wifi_test_hidden")) == hidden_now
                and self._last_wifi_test_signature == test_signature
            )
            if already_tested:
                return True
            reg_h, reg_p = self._registry_probe_target()
            busy_phase1 = (
                "Connecting briefly to verify the password.\n"
                "The current Wi-Fi network will be restored afterwards."
            )
            busy_phase2 = (
                "Checking required sites and downloads over this Wi‑Fi:\n\n"
                "• Mesh / registry host\n"
                "• Raspberry Pi OS image downloads\n"
                "• GitHub (clone / updates)\n"
                "• Debian and Raspberry Pi mirrors (DNS + TCP)\n\n"
                "Please wait…"
            )
            while True:
                dialog, body = self._show_busy_dialog("Testing Wi-Fi", busy_phase1)

                def _on_connected(_iface):
                    body.config(text=busy_phase2)
                    dialog.update_idletasks()

                try:
                    result = test_wifi_connection(
                        ssid,
                        value,
                        hidden=hidden_now,
                        registry_host=reg_h,
                        registry_port=reg_p,
                        on_connected=_on_connected,
                    )
                finally:
                    dialog.grab_release()
                    dialog.destroy()
                    self.update_idletasks()

                self.answers["wifi_tested"] = result["tested"]
                self.answers["wifi_test_ssid"] = ssid
                self.answers["wifi_test_hidden"] = hidden_now
                self.answers["wifi_test_passed"] = result["ok"]
                self.answers["wifi_continue_anyway"] = False
                self.answers["wifi_internet_reachable"] = result["internet_reachable"]
                self.answers["wifi_test_message"] = result["message"]

                if result["ok"]:
                    # Do not gate on NM secret readback. Association already used the passphrase from
                    # hb_secret_agent; nmcli may expose WPA2 PMK hex, WPA3 blobs, masking, etc., so a
                    # string compare falsely fails and wedges this step despite a successful connect.

                    redo_full_wifi = False
                    while True:
                        wifi_probe_ok = bool(result.get("internet_reachable"))
                        try:
                            post_rows = connectivity_checks_report(reg_h, reg_p, bind_iface=None)
                        except Exception as exc:
                            print(f"Connectivity check (post-wifi, default route): {exc}")
                            self._show_styled_error_modal(
                                "Connectivity check",
                                f"Unexpected error while checking network reachability:\n{exc}\n\n"
                                "Use Retry checks to try again, or fix the issue from a terminal.",
                            )
                            continue

                        post_probe_ok = connectivity_report_all_ok(post_rows)
                        failure_rows = list(result.get("connectivity_report") or [])
                        if wifi_probe_ok and not post_probe_ok:
                            failure_rows = post_rows

                        if wifi_probe_ok and post_probe_ok:
                            self.answers["connectivity_continue_anyway"] = False
                            self.answers.pop("connectivity_checks_last_report", None)
                            self.answers["wifi_internet_reachable"] = True
                            self._last_wifi_test_signature = test_signature
                            self._show_timed_message(
                                "Success!",
                                "Required sites and downloads are reachable.",
                                milliseconds=2000,
                            )
                            if self._self_update_retry_needed:
                                self._maybe_self_update("post-wifi")
                            break

                        self.answers["connectivity_checks_last_report"] = [dict(r) for r in failure_rows]
                        choice = self._connectivity_checklist_modal(
                            failure_rows, allow_redo_wifi=True
                        )
                        if choice == "retry":
                            continue
                        if choice == "redo_wifi":
                            redo_full_wifi = True
                            break
                        if choice == "back":
                            self.step_index = self.steps.index(self._step_wifi_ssid_pick)
                            self._render_current_step()
                            return False
                        if choice == "continue":
                            if self._confirm_connectivity_bypass():
                                self.answers["connectivity_continue_anyway"] = True
                                self.answers["wifi_internet_reachable"] = False
                                self.answers["wifi_test_passed"] = True
                                self._last_wifi_test_signature = test_signature
                                break
                            continue

                    if redo_full_wifi:
                        continue

                    break

                if not result["ok"]:
                    self._last_wifi_test_signature = None
                    action = self._ask_wifi_failure_action(result["message"])
                    if action == "retry":
                        continue
                    if action == "edit":
                        self.step_index = self.steps.index(self._step_wifi_ssid_pick)
                        self._render_current_step()
                        return False

                    self.answers["wifi_continue_anyway"] = True
                    self.answers["wifi_test_passed"] = False
                    self._last_wifi_test_signature = test_signature
                    break

            return True

        elif step_name == "_step_hostname":
            value = self._hostname_var.get().strip().lower()
            if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", value):
                self._show_styled_error_modal("Invalid", "Hostname must use a-z, 0-9, and hyphen, max 63 chars.")
                return False
            self.answers["hostname"] = value

        elif step_name == "_step_username":
            value = self._username_var.get().strip()
            if not re.fullmatch(r"[a-z_][a-z0-9_-]*", value):
                self._show_styled_error_modal(
                    "Invalid",
                    "Username must use a-z, 0-9, '_' or '-', and start with a letter or '_'.",
                )
                return False
            self.answers["username"] = value

        elif step_name == "_step_password":
            value = self._password_var.get()
            if not value:
                self._show_styled_error_modal("Required", "Empty password is not allowed.")
                return False
            self.answers["password"] = value

        elif step_name == "_step_monitor_width":
            return self._validate_float_step("monitor_width_cm", self._monitor_width_var)

        elif step_name == "_step_monitor_height":
            return self._validate_float_step("monitor_height_cm", self._monitor_height_var)

        elif step_name == "_step_monitor_distance":
            return self._validate_float_step("monitor_distance_cm", self._monitor_distance_var)

        return True

    def _validate_float_step(self, key, var):
        value = var.get().strip()
        try:
            number = float(value)
        except ValueError:
            self._show_styled_error_modal("Invalid", "Please enter a number.")
            return False
        if number <= 0:
            self._show_styled_error_modal("Invalid", "Value must be greater than zero.")
            return False
        self.answers[key] = value
        return True

    def _valid_int(self, value, low, high):
        try:
            number = int(value)
        except ValueError:
            return False
        return low <= number <= high


def parse_args():
    parser = argparse.ArgumentParser(description="Collect NVMe provisioning answers in a Tkinter GUI.")
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Path to write JSON answers (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--no-self-update",
        action="store_true",
        help="Do not git fetch/merge to update this repo before running (also HB_PROVISION_NO_SELF_UPDATE=1).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.no_self_update:
        os.environ["HB_PROVISION_NO_SELF_UPDATE"] = "1"
    app = ProvisioningWizard(output_path=args.output)
    app.mainloop()