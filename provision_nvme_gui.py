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
import re
import shlex
import shutil
import socket
import subprocess
import sys
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
FONT_INPUT = ("DejaVu Sans", 16)
FONT_BTN = ("DejaVu Sans", 14, "bold")

DEFAULT_WIFI_COUNTRY = "US"
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_LOCALE = "en_us"
DEFAULT_MONITOR_WIDTH_CM = "21.7"
DEFAULT_MONITOR_HEIGHT_CM = "13.6"
DEFAULT_MONITOR_DISTANCE_CM = "30.0"
DEFAULT_SCREEN_ROTATION = "0"
DEFAULT_OUTPUT = "/tmp/hb_provision_answers.json"
WIFI_SCAN_FILE = os.environ.get("HB_WIFI_SCAN_FILE", "/tmp/hb_wifi_scan_ssids.txt")
ACCESSORY_CHECK_ITEMS = [
    ("touchscreen", "Touchscreen"),
    ("juicer", "Juicer"),
    ("power_monitor", "Power monitor"),
    ("camera", "Camera"),
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


def scan_wifi_ssids():
    scan_file = Path(WIFI_SCAN_FILE)
    if scan_file.is_file():
        ssids = [line.strip() for line in scan_file.read_text(encoding="utf-8").splitlines()]
        return sorted({ssid for ssid in ssids if ssid})

    commands = [
        ["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", "--rescan", "yes"],
        ["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list"],
    ]
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.strip():
            ssids = [line.strip() for line in result.stdout.splitlines()]
            return sorted({ssid for ssid in ssids if ssid})
    return []


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
    restore_message = ""

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
        message = f"Connected to '{ssid}'."
        if not internet_ok:
            message += " Internet probe over Wi-Fi failed; Ethernet may still provide internet."

        return {
            "ok": True,
            "tested": True,
            "internet_reachable": internet_ok,
            "message": message + restore_message,
        }
    finally:
        if previous_connection and previous_connection != connection_name:
            result = nmcli(["-w", "20", "con", "up", previous_connection], timeout=25)
            if result.returncode != 0:
                restore_message = f" Could not restore previous connection '{previous_connection}'."
        nmcli(["con", "delete", connection_name], timeout=10)


class ProvisioningWizard(tk.Tk):
    def __init__(self, output_path=DEFAULT_OUTPUT):
        super().__init__()
        self.title("Device Provisioning")
        self.configure(bg=BG)

        # Fullscreen on the device; comment out for desktop testing.
        # self.attributes("-fullscreen", True)
        self.geometry("1280x800")

        self.output_path = output_path
        self._self_update_retry_needed = False
        self._maybe_self_update("startup")

        self.defaults_path = Path(os.environ.get("DEVICE_DEFAULTS_FILE", script_defaults_file()))
        self.config = load_defaults_config(self.defaults_path)
        self.groups = device_groups(self.config)
        self.wifi_ssids = scan_wifi_ssids()
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
            self._step_screen_height,
            self._step_screen_refresh_rate,
            self._step_screen_rotation,
            self._step_hostname,
            self._step_username,
            self._step_password,
            self._step_monitor_width,
            self._step_monitor_height,
            self._step_monitor_distance,
            self._step_review,
        ]
        self.step_index = 0

        self._build_layout()
        self._render_current_step()

    # ------------------------------------------------------------------
    # Layout: a top content frame and a nav frame kept above the area an
    # on-screen keyboard is likely to cover.
    # ------------------------------------------------------------------
    def _build_layout(self):
        self.content = tk.Frame(self, bg=BG, padx=40, pady=30)
        self.content.place(x=0, y=0, relwidth=1.0, height=380)

        self.nav = tk.Frame(self, bg=BG, padx=40, pady=10)
        self.nav.place(x=0, y=380, relwidth=1.0, height=80)

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

    def _clear_content(self):
        for child in self.content.winfo_children():
            child.destroy()

    def _render_current_step(self):
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
        self.destroy()

    def _confirm_destructive_provision(self):
        proceed = messagebox.askyesno(
            "Erase NVMe and provision?",
            "Provisioning will erase the selected NVMe disk and reboot this device when complete.\n\n"
            "Start provisioning now?",
        )
        if not proceed:
            return False

        self.answers["confirm_erase"] = "ERASE"
        return True

    def _launch_backend(self):
        backend = Path(__file__).resolve().parent / "provision_nvme.sh"
        if not backend.is_file():
            messagebox.showerror("Backend missing", f"Could not find provisioning backend:\n{backend}")
            return False

        launched_in_terminal = False
        terminal = shutil.which("x-terminal-emulator")
        if terminal:
            command = (
                f"cd {shlex.quote(str(backend.parent))} && "
                f"sudo {shlex.quote(str(backend))} --answers {shlex.quote(str(self.output_path))}; "
                "status=$?; echo; "
                "echo \"Provisioning exited with status ${status}. Press Enter to close.\"; "
                "read -r _; exit ${status}"
            )
            args = [terminal, "-e", "bash", "-lc", command]
            launched_in_terminal = True
        else:
            args = ["sudo", str(backend), "--answers", str(self.output_path)]

        try:
            subprocess.Popen(args)
        except OSError as exc:
            messagebox.showerror("Launch failed", f"Could not start provisioning backend:\n{exc}")
            return False

        messagebox.showinfo(
            "Provisioning started",
            "The NVMe provisioning backend is running"
            + (" in a terminal window." if launched_in_terminal else "."),
        )
        return True

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
        dialog.geometry("620x180+330+220")
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
        ).pack(anchor="w", padx=30, pady=(0, 25))
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
            print(f"Self-update: no internet during {phase}; will retry after Wi-Fi if available.")
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
            self._show_timed_message(
                "Update installed",
                "The provisioning GUI was updated. Restarting now so the newest defaults are used.",
                milliseconds=3000,
            )
            os.execv(sys.executable, [sys.executable, *sys.argv])

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
            ("Timezone", self.answers.get("timezone", "")),
            ("Locale", self.answers.get("locale", "")),
            ("Screen", self._screen_summary()),
            ("Wi-Fi SSID", self.answers.get("wifi_ssid", "(skipped)") or "(skipped)"),
            ("Wi-Fi test", self._wifi_test_summary()),
            ("Accessory checks", self._accessory_check_summary()),
            ("Hostname", self.answers.get("hostname", "")),
            ("Username", self.answers.get("username", "")),
            ("Password", self.answers.get("password", "")),
            ("Monitor", self._monitor_summary()),
        ]
        for label, value in rows:
            row = tk.Frame(review_frame, bg=ENTRY_BG)
            row.pack(fill="x", pady=2)
            tk.Label(
                row,
                text=f"{label}:",
                bg=ENTRY_BG,
                fg=FG,
                font=FONT_LABEL,
                width=14,
                anchor="w",
            ).pack(side="left")
            tk.Label(
                row,
                text=str(value),
                bg=ENTRY_BG,
                fg=ACCENT,
                font=FONT_LABEL,
                anchor="w",
            ).pack(side="left", fill="x", expand=True)

    def _screen_summary(self):
        width = self.answers.get("screen_pixels_width", "")
        height = self.answers.get("screen_pixels_height", "")
        rate = self.answers.get("screen_refresh_rate", "")
        rotation = self.answers.get("screen_rotation", "")
        if not (width and height and rate):
            return "(skipped)"
        return f"{width}x{height} @ {rate} Hz, rotation {rotation}"

    def _monitor_summary(self):
        width = self.answers.get("monitor_width_cm", "")
        height = self.answers.get("monitor_height_cm", "")
        distance = self.answers.get("monitor_distance_cm", "")
        return f"{width} x {height} cm, distance {distance} cm"

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
                        self._last_wifi_test_signature = test_signature
                        self._show_timed_message(
                            "Success!",
                            "Wi-Fi connected successfully!",
                            milliseconds=2000,
                        )
                        if result["tested"] and not result["internet_reachable"]:
                            messagebox.showwarning("Wi-Fi connected", result["message"])
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
