#!/usr/bin/env python3
"""One-shot installer: writes launchd plists and bootstraps both services.

Usage:
    python3 src/install.py           # install both
    python3 src/install.py --agent   # install LaunchAgent only (no sudo)
    python3 src/install.py --daemon  # install LaunchDaemon only (sudo)

On first run:
  1. Installs com.atvloader.tunneld as a root LaunchDaemon so the
     WiFi tunnel to Apple TV / iOS devices starts at boot and
     respawns on crash. This is the single admin-password moment.
  2. Installs com.atvloader.app as a user LaunchAgent so the
     menubar app auto-starts at login and respawns on crash.

Any already-running pymobiledevice3 tunneld process or python3 -m
atvloader instance is killed first so the new launchd-managed
versions take over cleanly.

Idempotent — re-running the script bootout + re-bootstraps both
services with the latest plist contents.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

# Make `atvloader` importable when run from the project root without
# having to remember PYTHONPATH on the command line.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from atvloader import launchd, paths  # noqa: E402


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _kill_stray_processes() -> None:
    """Kill any orphaned tunneld + menubar app so launchd can own them.

    We do this before install so the launchd bootstrap doesn't race
    against a manually-started copy that would bind the same sockets.
    """
    # Menubar app: kill any "Python -m atvloader" we find
    subprocess.run(
        ["pkill", "-9", "-f", "Python.*-m atvloader"],
        capture_output=True,
    )
    # Stray tunneld: pymobiledevice3 remote tunneld run without launchd
    subprocess.run(
        ["sudo", "-n", "pkill", "-9", "-f", "pymobiledevice3 remote tunneld"],
        capture_output=True,
    )


def main() -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description="Install ATVLoader launchd services")
    parser.add_argument(
        "--agent",
        action="store_true",
        help="Install only the user LaunchAgent (menubar app)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Install only the system LaunchDaemon (tunneld)",
    )
    args = parser.parse_args()

    if not args.agent and not args.daemon:
        args.agent = args.daemon = True

    paths.ensure_dirs()
    _kill_stray_processes()

    if args.daemon:
        print(
            "[+] Installing tunneld LaunchDaemon (requires admin password — "
            "macOS will prompt in a native dialog)"
        )
        try:
            launchd.install_launch_daemon()
        except launchd.LaunchdError as e:
            print(f"[!] LaunchDaemon install failed: {e}", file=sys.stderr)
            return 2
        print(f"[+] {launchd.TUNNELD_LABEL} is running as a system service")

    if args.agent:
        print("[+] Installing ATVLoader menubar LaunchAgent")
        try:
            launchd.install_launch_agent()
        except launchd.LaunchdError as e:
            print(f"[!] LaunchAgent install failed: {e}", file=sys.stderr)
            return 2
        print(
            f"[+] {launchd.APP_LABEL} is running — look for the 📺 icon "
            f"in your menubar"
        )

    print()
    print("Done. Both services are set to auto-start on login/boot.")
    print("Logs:")
    print(f"  tunneld: {launchd.TUNNELD_LOG_OUT}")
    print(f"  app:     {paths.LOG_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
