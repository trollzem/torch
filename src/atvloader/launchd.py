"""launchd plist generation + install/uninstall.

Two services:
  - com.atvloader.tunneld  System LaunchDaemon, runs as root.
                           `pymobiledevice3 remote tunneld --wifi`
                           Starts at boot, respawns if killed.
                           Installs to /Library/LaunchDaemons/
                           Requires sudo to install/uninstall.
  - com.atvloader.app      User LaunchAgent, runs as the logged-in user.
                           `python3 -m atvloader`
                           Starts at login, respawns if killed.
                           Installs to ~/Library/LaunchAgents/

The tunneld LaunchDaemon is what lets the user stop running
`sudo pymobiledevice3 remote tunneld --wifi` in a persistent terminal.
The menubar LaunchAgent is what auto-starts the app at login.

All sudo calls go through osascript `do shell script ... with administrator
privileges` so macOS prompts for admin password through its native dialog
instead of us capturing a password in our own process.
"""

from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

TUNNELD_LABEL = "com.atvloader.tunneld"
APP_LABEL = "com.atvloader.app"

SYSTEM_LAUNCHDAEMONS_DIR = Path("/Library/LaunchDaemons")
USER_LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

TUNNELD_PLIST_PATH = SYSTEM_LAUNCHDAEMONS_DIR / f"{TUNNELD_LABEL}.plist"
APP_PLIST_PATH = USER_LAUNCHAGENTS_DIR / f"{APP_LABEL}.plist"

TUNNELD_LOG_OUT = Path("/var/log/atvloader-tunneld.out")
TUNNELD_LOG_ERR = Path("/var/log/atvloader-tunneld.err")


class LaunchdError(Exception):
    """Base for launchd install/uninstall failures."""


def _resolve_binary(name: str) -> str:
    """Resolve a CLI name to an absolute path via `which`."""
    result = subprocess.run(
        ["/usr/bin/which", name], capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise LaunchdError(f"could not find {name} on PATH")
    return result.stdout.strip()


def tunneld_plist() -> dict:
    """Return the plist dict for the tunneld LaunchDaemon.

    A LaunchDaemon runs as root with an almost-empty environment. Two
    concerns to address:

    1. pymobiledevice3 reads pair records from ~/.pymobiledevice3/ which
       expands via HOME. Without setting HOME explicitly here, root's
       HOME defaults to /var/root and tunneld can't see the user's
       Apple TV pair records — it only picks up USB devices via
       usbmuxd (which is mediated by the kernel, independent of HOME).
       We bake the installing user's $HOME into the plist so root's
       tunneld reads from the same directory the manual-sudo version
       did during the spike.

    2. pymobiledevice3's pip-installed dependencies live in
       /opt/homebrew/lib/python3.14/site-packages. root's default
       PATH doesn't include /opt/homebrew/bin, which would also
       break subprocess resolution of the pymobiledevice3 CLI
       itself — but we're invoking it via absolute path already, so
       we only need PATH for any child subprocesses tunneld might
       spawn.
    """
    pymd3_bin = _resolve_binary("pymobiledevice3")
    user_home = str(Path.home())
    return {
        "Label": TUNNELD_LABEL,
        "ProgramArguments": [pymd3_bin, "remote", "tunneld", "--wifi"],
        "EnvironmentVariables": {
            "HOME": user_home,
            "PATH": os.environ.get(
                "PATH",
                "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            ),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(TUNNELD_LOG_OUT),
        "StandardErrorPath": str(TUNNELD_LOG_ERR),
        # Long-running service; no timeout. If it exits, launchd
        # respawns via KeepAlive.
        "ThrottleInterval": 30,
    }


def app_plist() -> dict:
    """Return the plist dict for the menubar LaunchAgent."""
    python_bin = _resolve_binary("python3")
    project_src = str(paths.PROJECT_ROOT / "src")
    logs_dir = paths.LOG_DIR
    logs_dir.mkdir(parents=True, exist_ok=True)
    return {
        "Label": APP_LABEL,
        "ProgramArguments": [python_bin, "-m", "atvloader"],
        "EnvironmentVariables": {
            "PYTHONPATH": project_src,
            "HOME": str(Path.home()),
            # Inherit PATH so subprocess calls to plumesign, pymobiledevice3,
            # zip, osascript, security, codesign, etc. all resolve the same
            # binaries as an interactive shell.
            "PATH": os.environ.get(
                "PATH",
                "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            ),
        },
        "WorkingDirectory": str(paths.PROJECT_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(logs_dir / "launchd.out"),
        "StandardErrorPath": str(logs_dir / "launchd.err"),
        "ProcessType": "Interactive",
    }


def _write_plist_bytes(data: dict) -> bytes:
    return plistlib.dumps(data, fmt=plistlib.FMT_XML)


# --- User LaunchAgent (no sudo needed) ---------------------------------------


def install_launch_agent() -> None:
    """Write + bootstrap the user LaunchAgent. No sudo required."""
    USER_LAUNCHAGENTS_DIR.mkdir(parents=True, exist_ok=True)
    data = _write_plist_bytes(app_plist())
    APP_PLIST_PATH.write_bytes(data)
    log.info("wrote %s", APP_PLIST_PATH)

    # Bootstrap into the gui domain for this user.
    uid = os.getuid()
    target = f"gui/{uid}"
    # Re-bootstrap is idempotent via bootout-then-bootstrap: bootout errors
    # if the service isn't loaded, which we ignore.
    subprocess.run(
        ["launchctl", "bootout", target, str(APP_PLIST_PATH)],
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", target, str(APP_PLIST_PATH)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise LaunchdError(
            f"launchctl bootstrap {target}: exit={result.returncode} "
            f"stderr={result.stderr}"
        )
    log.info("LaunchAgent %s is loaded", APP_LABEL)


def uninstall_launch_agent() -> None:
    """Bootout + remove the user LaunchAgent plist."""
    uid = os.getuid()
    target = f"gui/{uid}"
    subprocess.run(
        ["launchctl", "bootout", target, str(APP_PLIST_PATH)],
        capture_output=True,
        text=True,
    )
    if APP_PLIST_PATH.exists():
        APP_PLIST_PATH.unlink()
        log.info("removed %s", APP_PLIST_PATH)


# --- System LaunchDaemon (sudo via osascript) --------------------------------


def _run_as_admin(shell_script: str) -> None:
    """Run a shell script with administrator privileges via osascript.

    Uses macOS's native admin prompt; we never see the password.
    """
    # Escape the script for embedding in AppleScript:
    #   " -> \\"
    #   \ -> \\\\  (we don't currently use backslashes, but be safe)
    escaped = shell_script.replace("\\", "\\\\").replace('"', '\\"')
    full_script = f'do shell script "{escaped}" with administrator privileges'
    log.info("requesting admin privileges for: %s", shell_script)
    result = subprocess.run(
        ["osascript", "-e", full_script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise LaunchdError(
            f"admin run failed (exit={result.returncode}): {result.stderr}"
        )


def install_launch_daemon() -> None:
    """Write + bootstrap the tunneld LaunchDaemon as root via osascript.

    Writes the plist to a temp location we own, then uses osascript to
    chown it root:wheel, move it to /Library/LaunchDaemons/, launchctl
    bootout (ignoring error), launchctl bootstrap system, and touch the
    log files so launchd can open them without fighting SIP.
    """
    # Write the plist to a staging file owned by the user first.
    staging = paths.APP_SUPPORT_DIR / f"{TUNNELD_LABEL}.plist.staging"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(_write_plist_bytes(tunneld_plist()))

    # Compose the single admin-privileged shell script that does
    # everything atomically. launchctl bootout is best-effort (may
    # fail with "not loaded"); we chain with `|| true` so the
    # subsequent bootstrap runs regardless.
    target_plist = str(TUNNELD_PLIST_PATH)
    commands = [
        f"mv '{staging}' '{target_plist}'",
        f"chown root:wheel '{target_plist}'",
        f"chmod 644 '{target_plist}'",
        f"touch '{TUNNELD_LOG_OUT}' '{TUNNELD_LOG_ERR}'",
        f"chmod 644 '{TUNNELD_LOG_OUT}' '{TUNNELD_LOG_ERR}'",
        f"launchctl bootout system '{target_plist}' 2>/dev/null || true",
        f"launchctl bootstrap system '{target_plist}'",
    ]
    _run_as_admin(" && ".join(commands))
    log.info("LaunchDaemon %s is loaded", TUNNELD_LABEL)


def uninstall_launch_daemon() -> None:
    """Bootout + remove the tunneld LaunchDaemon as root."""
    target_plist = str(TUNNELD_PLIST_PATH)
    commands = [
        f"launchctl bootout system '{target_plist}' 2>/dev/null || true",
        f"rm -f '{target_plist}'",
    ]
    _run_as_admin(" && ".join(commands))
    log.info("LaunchDaemon %s removed", TUNNELD_LABEL)


# --- Status helpers ----------------------------------------------------------


def is_service_loaded(label: str, *, domain: str) -> bool:
    """Check whether a launchd service is currently loaded in a domain."""
    result = subprocess.run(
        ["launchctl", "print", f"{domain}/{label}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def is_tunneld_daemon_loaded() -> bool:
    return is_service_loaded(TUNNELD_LABEL, domain="system")


def is_app_agent_loaded() -> bool:
    uid = os.getuid()
    return is_service_loaded(APP_LABEL, domain=f"gui/{uid}")
