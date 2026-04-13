#!/usr/bin/env python3
"""Uninstaller: bootout + remove both launchd plists.

Usage:
    python3 src/uninstall.py           # remove both
    python3 src/uninstall.py --agent   # remove LaunchAgent only
    python3 src/uninstall.py --daemon  # remove LaunchDaemon only (sudo)

Does NOT touch:
  - ~/Library/Application Support/ATVLoader/   (config, IPAs, logs)
  - ~/.config/PlumeImpactor/                   (plumesign session)
  - ~/.pymobiledevice3/                        (pair records)
  - the macOS Keychain entry for com.atvloader.appleid

Use `rm -rf` on those yourself if you want a truly clean slate.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from atvloader import launchd  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Uninstall ATVLoader launchd services")
    parser.add_argument(
        "--agent", action="store_true", help="Only remove the user LaunchAgent"
    )
    parser.add_argument(
        "--daemon", action="store_true", help="Only remove the system LaunchDaemon"
    )
    args = parser.parse_args()

    if not args.agent and not args.daemon:
        args.agent = args.daemon = True

    if args.agent:
        print("[+] Removing menubar LaunchAgent")
        try:
            launchd.uninstall_launch_agent()
        except launchd.LaunchdError as e:
            print(f"[!] {e}", file=sys.stderr)

    if args.daemon:
        print("[+] Removing tunneld LaunchDaemon (requires admin password)")
        try:
            launchd.uninstall_launch_daemon()
        except launchd.LaunchdError as e:
            print(f"[!] {e}", file=sys.stderr)
            return 2

    print("Done. User data in ~/Library/Application Support/ATVLoader/ is untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
