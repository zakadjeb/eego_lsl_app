from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from dataclasses import dataclass
from tkinter import messagebox


APP_RULE_BASE_NAME = "eego LSL App"


@dataclass
class FirewallResult:
    ok: bool
    message: str
    changed: bool = False


def is_windows() -> bool:
    return os.name == "nt"


def is_admin() -> bool:
    if not is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def executable_path() -> str:
    """Return the executable Windows Firewall should allow.

    During development this is python.exe. After PyInstaller packaging this is
    the packaged eego .exe, which is the preferred production behaviour.
    """
    return os.path.abspath(sys.executable)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_powershell(script: str, *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        check=check,
        shell=False,
    )


def firewall_rules_exist(app_name: str = APP_RULE_BASE_NAME) -> bool:
    if not is_windows():
        return True
    script = (
        f"$rules = Get-NetFirewallRule -DisplayName {_ps_quote(app_name + ' *')} "
        "-ErrorAction SilentlyContinue; "
        "if ($rules) { 'FOUND' }"
    )
    try:
        result = _run_powershell(script)
        return "FOUND" in result.stdout
    except Exception:
        return False


def add_firewall_rules(app_name: str = APP_RULE_BASE_NAME) -> FirewallResult:
    """Add inbound and outbound Windows Firewall allow rules for this app."""
    if not is_windows():
        return FirewallResult(True, "Firewall configuration is only needed on Windows.", changed=False)

    exe = executable_path()
    if firewall_rules_exist(app_name):
        return FirewallResult(True, "Firewall rules already exist.", changed=False)

    if not is_admin():
        return FirewallResult(False, "Administrator permission is required to add firewall rules.", changed=False)

    exe_q = _ps_quote(exe)
    inbound_name_q = _ps_quote(app_name + " Inbound")
    outbound_name_q = _ps_quote(app_name + " Outbound")

    script = f"""
$exe = {exe_q}
if (-not (Test-Path $exe)) {{ throw "Executable not found: $exe" }}
New-NetFirewallRule -DisplayName {inbound_name_q} -Direction Inbound -Program $exe -Action Allow -Profile Private,Domain -Enabled True | Out-Null
New-NetFirewallRule -DisplayName {outbound_name_q} -Direction Outbound -Program $exe -Action Allow -Profile Private,Domain -Enabled True | Out-Null
"""
    try:
        result = _run_powershell(script, check=True)
        return FirewallResult(True, "Firewall rules added for Private and Domain networks.", changed=True)
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        return FirewallResult(False, f"Could not add firewall rules:\n{msg}", changed=False)
    except Exception as exc:
        return FirewallResult(False, f"Could not add firewall rules:\n{exc}", changed=False)


def relaunch_as_admin_with_firewall_flag() -> bool:
    """Ask Windows UAC to relaunch the current app/script with --add-firewall-rule."""
    if not is_windows():
        return False

    if getattr(sys, "frozen", False):
        executable = sys.executable
        params = " --add-firewall-rule"
    else:
        executable = sys.executable
        script_path = os.path.abspath(sys.argv[0])
        params = f'"{script_path}" --add-firewall-rule'

    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
        return rc > 32
    except Exception:
        return False


def ensure_firewall_rule_interactive(parent=None, *, force_prompt: bool = False) -> None:
    """Interactive startup check.

    If no rule exists, the user is asked whether to add one. Adding the rule
    requires admin permission, so a UAC prompt may appear.
    """
    if not is_windows():
        return

    if firewall_rules_exist() and not force_prompt:
        return

    exe = executable_path()
    answer = messagebox.askyesno(
        "Windows Firewall",
        "No Windows Firewall rule was found for this app.\n\n"
        "LSL streams may not be visible to other computers unless this app is allowed through Windows Firewall.\n\n"
        f"Program to allow:\n{exe}\n\n"
        "Add inbound and outbound firewall rules for Private/Domain networks now?\n\n"
        "Windows may ask for administrator permission.",
        parent=parent,
    )
    if not answer:
        return

    if is_admin():
        result = add_firewall_rules()
        if result.ok:
            messagebox.showinfo("Windows Firewall", result.message, parent=parent)
        else:
            messagebox.showerror("Windows Firewall", result.message, parent=parent)
        return

    launched = relaunch_as_admin_with_firewall_flag()
    if launched:
        messagebox.showinfo(
            "Windows Firewall",
            "A Windows administrator permission window should appear.\n\n"
            "After the firewall rule has been added, restart this app.",
            parent=parent,
        )
    else:
        messagebox.showerror(
            "Windows Firewall",
            "Could not open the administrator permission prompt.",
            parent=parent,
        )
