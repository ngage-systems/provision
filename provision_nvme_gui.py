#!/usr/bin/env python3
"""
Provisioning wizard - Tkinter GUI for collecting NVMe provisioning answers.

This replaces the old interactive shell questions with a touch-friendly flow.
It collects answers, validates Wi-Fi when requested, writes JSON output, and
launches provision_nvme.sh after the user confirms the destructive erase step.
"""

import argparse
import configparser
import json
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
import uuid


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
FONT_KEYBOARD = ("DejaVu Sans", 15)

KEYBOARD_FRAME_PADX = 20
KEYBOARD_FRAME_PADY = 11
KEYBOARD_ROW_INDENT_UNIT = 20
KEYBOARD_KEY_PADX = 3
KEYBOARD_KEY_PADY = 7
KEYBOARD_ROW_PADY = 3
KEYBOARD_CONTROLS_PADY = (5, 0)
KEYBOARD_CONTROL_PADX = 4

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
RESUME_STATE_VERSION = 1
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

    if not isinstance(payload, dict) or payload.get("version") != RESUME_STATE_VERSION:
        print("Resume state: ignoring unsupported state file.")
        delete_resume_state(path)
        return None

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


def have_internet():
    for host, port in [
        ("1.1.1.1", 443),
        ("1.0.0.1", 443),
        ("93.184.216.34", 80),
    ]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            continue
        finally:
            sock.close()
    return False


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
    return False


def internet_reachable_via_iface(iface):
    targets = [
        ("1.1.1.1", 443),
        ("1.0.0.1", 443),
        ("93.184.216.34", 443),
        ("93.184.216.34", 80),
    ]
    iface_opt = iface.encode("utf-8")
    if not iface_opt.endswith(b"\0"):
        iface_opt += b"\0"

    for host, port in targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        try:
            sock.setsockopt(socket.SOL_SOCKET, 25, iface_opt)  # SO_BINDTODEVICE
            sock.connect((host, port))
            return True
        except OSError:
            continue
        finally:
            sock.close()
    return False


def safe_connection_name(ssid):
    safe_ssid = re.sub(r"[^A-Za-z0-9_.-]+", "_", ssid).strip("_")
    return f"hb-wifi-{safe_ssid or 'network'}-{uuid.uuid4().hex[:8]}"


def test_wifi_connection(ssid, password):
    if not ssid:
        return {"ok": True, "tested": False, "internet_reachable": False, "message": "Wi-Fi skipped."}

    if shutil.which("nmcli") is None:
        return {
            "ok": False,
            "tested": False,
            "internet_reachable": False,
            "message": "nmcli is not available. Install NetworkManager or skip Wi-Fi.",
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
            }
        created_connection = True

        result = nmcli(
            [
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
            ],
            timeout=35,
        )
        if result.returncode != 0:
            return {
                "ok": False,
                "tested": True,
                "internet_reachable": False,
                "message": f"NetworkManager rejected the password settings for '{ssid}'.",
            }

        result = nmcli(["-w", "60", "con", "up", connection_name, "ifname", iface], timeout=70)
        if result.returncode != 0:
            return {
                "ok": False,
                "tested": True,
                "internet_reachable": False,
                "message": f"Failed to connect to '{ssid}'. Check the password and try again.",
            }
        connected_connection = True

        got_ssid = connected_wifi_ssid()
        if got_ssid != ssid:
            return {
                "ok": False,
                "tested": True,
                "internet_reachable": False,
                "message": f"Connected Wi-Fi mismatch. Expected '{ssid}', got '{got_ssid or '<none>'}'.",
            }

        if not wait_for_ipv4(iface):
            return {
                "ok": False,
                "tested": True,
                "internet_reachable": False,
                "message": f"Connected to '{ssid}', but no IPv4 address was acquired.",
            }

        internet_ok = internet_reachable_via_iface(iface)
        restore_message = restore_previous_connection()
        message = f"Connected to '{ssid}'."
        if previous_connection:
            message += restore_message
        else:
            message += " Leaving this Wi-Fi connected for provisioning."
        if not internet_ok:
            message += " Internet probe over Wi-Fi failed; Ethernet may still provide internet."

        return {
            "ok": True,
            "tested": True,
            "internet_reachable": internet_ok,
            "message": message,
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

        self.answers = {
            "wifi_country": DEFAULT_WIFI_COUNTRY,
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
            self._step_wifi_ssid,
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

        target_index = self._step_index_for_name(payload["target_step"])
        if target_index is None:
            print(f"Resume state: unknown target step {payload['target_step']!r}.")
            delete_resume_state()
            return

        self.answers.update(payload["answers"])
        self.step_index = target_index
        self._loaded_resume_state = True

        ssid = self.answers.get("wifi_ssid", "")
        password = self.answers.get("wifi_password", "")
        if (
            ssid
            and password
            and self.answers.get("wifi_tested") is True
            and self.answers.get("wifi_test_ssid") == ssid
        ):
            self._last_wifi_test_signature = (ssid, password)
        print(f"Resume state: restored wizard at {payload['target_step']}.")

    def _current_resume_target_step_name(self):
        if not self.steps:
            return ""
        step_name = self.steps[self.step_index].__name__
        if step_name == "_step_wifi_password" and self.answers.get("wifi_tested") is True:
            return self.steps[self._next_index(self.step_index)].__name__
        return step_name

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
    # Layout: content expands, the touch keyboard appears above nav, and
    # navigation stays pinned to the bottom of the window.
    # ------------------------------------------------------------------
    def _build_layout(self):
        self.keyboard = tk.Frame(
            self, bg=ENTRY_BG, padx=KEYBOARD_FRAME_PADX, pady=KEYBOARD_FRAME_PADY
        )
        self._build_touch_keyboard()

        self.nav = tk.Frame(self, bg=BG, padx=40, pady=10)
        self.nav.pack(side="bottom", fill="x")

        self.btn_back = self._make_button(self.nav, "< Back", self._on_back)
        self.btn_back.pack(side="left")

        self.nav_right = tk.Frame(self.nav, bg=BG)
        self.nav_right.pack(side="right")

        self.btn_recheck_accessories = self._make_button(
            self.nav_right, "Recheck Accessories", self._recheck_accessories
        )

        self.btn_next = self._make_button(self.nav_right, "Next >", self._on_next, primary=True)
        self.btn_next.pack(side="left")

        self.progress_label = tk.Label(
            self.nav, text="", bg=BG, fg=FG, font=FONT_LABEL
        )
        self.progress_label.pack(side="top", pady=5)
        self.nav.update_idletasks()

        self.content = tk.Frame(self, bg=BG, padx=40, pady=30)
        self.content.pack(side="top", fill="both", expand=True)

    def _make_button(self, parent, text, command, primary=False):
        bg = ACCENT if primary else ENTRY_BG
        active = ACCENT_ACTIVE if primary else "#45475a"
        return tk.Button(
            parent,
            text=text,
            command=command,
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
        )

    def _make_keyboard_button(self, parent, text, command, width=4):
        return tk.Button(
            parent,
            text=text,
            command=command,
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
            controls, shift_text, self._keyboard_toggle_shift, width=7
        ).pack(side="left", padx=KEYBOARD_CONTROL_PADX)
        self._make_keyboard_button(
            controls, "Space", lambda: self._keyboard_insert(" "), width=18
        ).pack(side="left", padx=KEYBOARD_CONTROL_PADX)
        self._make_keyboard_button(
            controls, "Backspace", self._keyboard_backspace, width=10
        ).pack(side="left", padx=KEYBOARD_CONTROL_PADX)
        self._make_keyboard_button(
            controls, "Clear", self._keyboard_clear, width=7
        ).pack(side="left", padx=KEYBOARD_CONTROL_PADX)
        self._make_keyboard_button(
            controls, "Hide", self._hide_touch_keyboard, width=7
        ).pack(side="left", padx=KEYBOARD_CONTROL_PADX)

    def _show_touch_keyboard(self, entry):
        self._focused_entry = entry
        if not self.keyboard.winfo_ismapped():
            self.keyboard.pack(side="bottom", fill="x", before=self.nav)

    def _hide_touch_keyboard(self):
        if self.keyboard.winfo_manager():
            self.keyboard.pack_forget()

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
        if (
            next_index < len(self.steps)
            and self.steps[next_index].__name__ == "_step_wifi_password"
            and not self.answers.get("wifi_ssid")
        ):
            next_index += 1
        return next_index

    def _previous_index(self, index):
        previous_index = index - 1
        if (
            previous_index >= 0
            and self.steps[previous_index].__name__ == "_step_wifi_password"
            and not self.answers.get("wifi_ssid")
        ):
            previous_index -= 1
        return previous_index

    def _on_next(self):
        if not self._validate_current_step():
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
        dialog = self._show_busy_dialog(
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

    def _on_finish(self):
        if not self._confirm_destructive_provision():
            return

        try:
            output_path = Path(self.output_path)
            output_path.write_text(
                json.dumps(self.answers, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(output_path, 0o600)
        except OSError as exc:
            messagebox.showerror("Save failed", f"Could not write {self.output_path}:\n{exc}")
            return

        print(json.dumps(self.answers, indent=2, sort_keys=True))
        if not self._launch_backend():
            return
        self.withdraw()

    def _confirm_destructive_provision(self):
        dialog = tk.Toplevel(self)
        dialog.title("Install new system?")
        dialog.configure(bg=BG)
        dialog.transient(self)
        dialog.grab_set()

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
        dialog.geometry(f"+{x}+{y}")
        yes_button.focus_set()
        dialog.wait_window()

        if not result.get():
            return False

        self.answers["confirm_erase"] = "ERASE"
        return True

    def _launch_backend(self):
        backend = Path(__file__).resolve().parent / "provision_nvme.sh"
        if not backend.is_file():
            messagebox.showerror("Backend missing", f"Could not find provisioning backend:\n{backend}")
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
            messagebox.showerror("Launch failed", f"Could not start provisioning backend:\n{exc}")
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
            "Provisioning new system. This will take about 5-10 mins. "
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
        dialog.transient(parent)
        dialog.grab_set()
        dialog.geometry("860x430+210+120")

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
        dialog.update_idletasks()
        reboot_button.focus_set()

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
            messagebox.showerror(
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
        dialog.geometry("1120x720+80+50")
        dialog.minsize(800, 500)
        dialog.grab_set()

        tk.Label(
            dialog,
            text="Provisioning NVMe",
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

        dialog.update_idletasks()
        dialog.focus_set()
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

    def _add_listbox(self, entries, selected_value=""):
        var = tk.StringVar(value=selected_value)
        list_frame = tk.Frame(self.content, bg=BG)
        list_frame.pack(fill="x", pady=5)

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
            height=min(5, max(1, len(entries))),
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
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("760x220+260+180")
        dialog.minsize(640, 200)
        tk.Label(
            dialog,
            text=title,
            bg=BG,
            fg=FG,
            font=FONT_TITLE,
        ).pack(anchor="w", padx=30, pady=(25, 10))
        tk.Label(
            dialog,
            text=text,
            bg=BG,
            fg=FG,
            font=FONT_LABEL,
            justify="left",
            wraplength=680,
        ).pack(anchor="w", fill="x", padx=30, pady=(0, 25))
        dialog.update_idletasks()
        return dialog

    def _show_timed_message(self, title, text, milliseconds=2000):
        dialog = self._show_busy_dialog(title, text)
        dialog.after(milliseconds, dialog.destroy)
        self.wait_window(dialog)

    def _ask_wifi_failure_action(self, message):
        dialog = tk.Toplevel(self)
        dialog.title("Wi-Fi test failed")
        dialog.configure(bg=BG)
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("820x300+230+180")

        tk.Label(
            dialog,
            text="Wi-Fi test failed",
            bg=BG,
            fg=ERROR,
            font=FONT_TITLE,
        ).pack(anchor="w", padx=30, pady=(25, 10))
        tk.Label(
            dialog,
            text=(
                "We could not connect to this Wi-Fi network from the current location. "
                "The password may be wrong, or this device may be using Wi-Fi settings "
                "for another site.\n\n"
                f"{message}"
            ),
            bg=BG,
            fg=FG,
            font=FONT_LABEL,
            justify="left",
            wraplength=760,
        ).pack(anchor="w", padx=30, pady=(0, 20))

        action = tk.StringVar(value="")
        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(fill="x", padx=30, pady=(0, 25))
        self._make_button(buttons, "Try Again", lambda: action.set("retry"), primary=True).pack(side="left")
        self._make_button(buttons, "Edit Wi-Fi", lambda: action.set("edit")).pack(side="left", padx=15)
        self._make_button(buttons, "Continue Anyway", lambda: action.set("continue")).pack(side="right")

        dialog.wait_variable(action)
        choice = action.get()
        dialog.grab_release()
        dialog.destroy()
        return choice

    def _maybe_self_update(self, phase):
        if not have_internet():
            self._self_update_retry_needed = True
            print(f"Self-update: no internet during {phase}; will retry after internet is available.")
            return

        dialog = self._show_busy_dialog(
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
            messagebox.showwarning(
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
                messagebox.showwarning(
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

    def _step_wifi_ssid(self):
        self._add_title("Choose Wi-Fi network")
        self._add_label("Select a network, type a network name, or leave this blank if using Ethernet.")
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

        if self.wifi_ssids:
            selected = self.answers.get("wifi_ssid", "")
            self._ssid_list_var, listbox = self._add_listbox(self.wifi_ssids, selected)
            listbox.bind("<<ListboxSelect>>", self._on_ssid_list_select, add="+")
        else:
            self._add_label("No scanned Wi-Fi networks found. You can type the SSID manually.", fg=MUTED)
            self._ssid_list_var = tk.StringVar(value="")

        self._wifi_ssid_var, entry = self._add_entry(self.answers.get("wifi_ssid", ""))
        entry.focus_set()

    def _on_ssid_list_select(self, _event):
        self._wifi_ssid_var.set(self._ssid_list_var.get())
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
        dialog = self._show_busy_dialog(
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
            ("Wi-Fi SSID", self.answers.get("wifi_ssid", "(skipped)") or "(skipped)"),
            ("Wi-Fi test", self._wifi_test_summary()),
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

    def _wifi_test_summary(self):
        if not self.answers.get("wifi_ssid"):
            return "(skipped)"
        if self.answers.get("wifi_continue_anyway"):
            return "Failed, continuing anyway"
        if self.answers.get("wifi_test_passed"):
            if self.answers.get("wifi_internet_reachable"):
                return "Connected, internet reachable"
            return "Connected, internet not confirmed"
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
                messagebox.showerror("Required", "Please select a device profile.")
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
                messagebox.showerror("Required", "Please select a device type.")
                return False
            section = f"{group}.{device_type}"
            if not self.config.has_section(section):
                messagebox.showerror("Invalid", f"Defaults section not found: {section}")
                return False
            self.answers["defaults_device_type"] = device_type
            self.answers["defaults_section"] = section
            self._apply_defaults_section(section)

        elif step_name == "_step_wifi_country":
            value = self._wifi_country_var.get().strip().upper() or DEFAULT_WIFI_COUNTRY
            if not re.fullmatch(r"[A-Z]{2}", value):
                messagebox.showerror("Invalid", "Wi-Fi country must be 2 letters like US.")
                return False
            self.answers["wifi_country"] = value

        elif step_name == "_step_timezone":
            value = self._timezone_var.get().strip() or DEFAULT_TIMEZONE
            if not Path("/usr/share/zoneinfo", value).is_file():
                messagebox.showerror(
                    "Invalid",
                    "Timezone not found. Example: America/Los_Angeles, Europe/London, Asia/Tokyo.",
                )
                return False
            self.answers["timezone"] = value

        elif step_name == "_step_locale":
            value = self._locale_var.get().strip().lower() or DEFAULT_LOCALE
            if not re.fullmatch(r"[a-z]{2}_[a-z]{2}", value):
                messagebox.showerror("Invalid", "Locale must look like en_us, en_gb, fr_fr, or de_de.")
                return False
            base = f"{value[:2]}_{value[3:].upper()}"
            if not Path("/usr/share/i18n/locales", base).is_file():
                messagebox.showerror("Invalid", f"Locale not found on this system: {value}")
                return False
            self.answers["locale"] = f"{base}.UTF-8"

        elif step_name == "_step_screen_width":
            value = self._screen_width_var.get().strip()
            if value and not self._valid_int(value, 320, 7680):
                messagebox.showerror("Invalid", "Screen width must be a number between 320 and 7680.")
                return False
            self.answers["screen_pixels_width"] = value

        elif step_name == "_step_screen_height":
            value = self._screen_height_var.get().strip()
            if value and not self._valid_int(value, 240, 4320):
                messagebox.showerror("Invalid", "Screen height must be a number between 240 and 4320.")
                return False
            self.answers["screen_pixels_height"] = value

        elif step_name == "_step_screen_refresh_rate":
            value = self._screen_refresh_var.get().strip()
            if value and not self._valid_int(value, 1, 360):
                messagebox.showerror("Invalid", "Refresh rate must be a number between 1 and 360.")
                return False
            self.answers["screen_refresh_rate"] = value

        elif step_name == "_step_screen_rotation":
            value = self._screen_rotation_var.get().strip() or DEFAULT_SCREEN_ROTATION
            if value not in {"0", "90", "180", "270"}:
                messagebox.showerror("Invalid", "Screen rotation must be 0, 90, 180, or 270.")
                return False
            self.answers["screen_rotation"] = value

        elif step_name == "_step_wifi_ssid":
            value = self._wifi_ssid_var.get().strip()
            if "\n" in value or "\r" in value:
                messagebox.showerror("Invalid", "Wi-Fi SSID cannot contain newline characters.")
                return False
            if value != self.answers.get("wifi_ssid"):
                self._last_wifi_test_signature = None
                self.answers.pop("wifi_tested", None)
                self.answers.pop("wifi_test_ssid", None)
                self.answers.pop("wifi_test_passed", None)
                self.answers.pop("wifi_continue_anyway", None)
                self.answers.pop("wifi_internet_reachable", None)
                self.answers.pop("wifi_test_message", None)
            self.answers["wifi_ssid"] = value
            if not value:
                self.answers["wifi_password"] = ""
                self.answers["wifi_tested"] = False
                self.answers["wifi_test_passed"] = False
                self.answers["wifi_continue_anyway"] = False
                self.answers["wifi_internet_reachable"] = False
                self._last_wifi_test_signature = None
                if not have_internet():
                    messagebox.showerror(
                        "Internet required",
                        "No Wi-Fi SSID was selected, and this device does not currently have internet. "
                        "Connect Ethernet or go Back and enter Wi-Fi credentials before continuing.",
                    )
                    return False

        elif step_name == "_step_wifi_password":
            value = self._wifi_password_var.get()
            if not value:
                messagebox.showerror("Required", "Wi-Fi password cannot be empty. Go Back to skip Wi-Fi.")
                return False
            if "\n" in value or "\r" in value:
                messagebox.showerror("Invalid", "Wi-Fi password cannot contain newline characters.")
                return False
            self.answers["wifi_password"] = value
            ssid = self.answers.get("wifi_ssid", "")
            test_signature = (ssid, value)
            already_tested = (
                self.answers.get("wifi_tested") is True
                and self.answers.get("wifi_test_ssid") == ssid
                and self._last_wifi_test_signature == test_signature
            )
            if not already_tested:
                while True:
                    dialog = self._show_busy_dialog(
                        "Testing Wi-Fi",
                        "Connecting briefly to verify the password.\n"
                        "The current Wi-Fi network will be restored afterwards.",
                    )
                    try:
                        result = test_wifi_connection(ssid, value)
                    finally:
                        dialog.grab_release()
                        dialog.destroy()
                        self.update_idletasks()

                    self.answers["wifi_tested"] = result["tested"]
                    self.answers["wifi_test_ssid"] = ssid
                    self.answers["wifi_test_passed"] = result["ok"]
                    self.answers["wifi_continue_anyway"] = False
                    self.answers["wifi_internet_reachable"] = result["internet_reachable"]
                    self.answers["wifi_test_message"] = result["message"]

                    if result["ok"]:
                        internet_ok = have_internet()
                        self.answers["wifi_internet_reachable"] = internet_ok
                        if not internet_ok:
                            messagebox.showerror(
                                "Internet required",
                                "The Wi-Fi credentials were accepted, but this device still does not have internet. "
                                "Provisioning needs internet for downloads and repository updates.",
                            )
                            return False
                        self._last_wifi_test_signature = test_signature
                        self._show_timed_message(
                            "Success!",
                            "Internet connection verified!",
                            milliseconds=2000,
                        )
                        if self._self_update_retry_needed:
                            self._maybe_self_update("post-wifi")
                        break

                    self._last_wifi_test_signature = None
                    action = self._ask_wifi_failure_action(result["message"])
                    if action == "retry":
                        continue
                    if action == "edit":
                        self.step_index = self.steps.index(self._step_wifi_ssid)
                        self._render_current_step()
                        return False

                    self.answers["wifi_continue_anyway"] = True
                    self.answers["wifi_test_passed"] = False
                    self._last_wifi_test_signature = test_signature
                    break

        elif step_name == "_step_hostname":
            value = self._hostname_var.get().strip().lower()
            if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", value):
                messagebox.showerror("Invalid", "Hostname must use a-z, 0-9, and hyphen, max 63 chars.")
                return False
            self.answers["hostname"] = value

        elif step_name == "_step_username":
            value = self._username_var.get().strip()
            if not re.fullmatch(r"[a-z_][a-z0-9_-]*", value):
                messagebox.showerror(
                    "Invalid",
                    "Username must use a-z, 0-9, '_' or '-', and start with a letter or '_'.",
                )
                return False
            self.answers["username"] = value

        elif step_name == "_step_password":
            value = self._password_var.get()
            if not value:
                messagebox.showerror("Required", "Empty password is not allowed.")
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
            messagebox.showerror("Invalid", "Please enter a number.")
            return False
        if number <= 0:
            messagebox.showerror("Invalid", "Value must be greater than zero.")
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
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app = ProvisioningWizard(output_path=args.output)
    app.mainloop()
