#!/usr/bin/env python3
"""One-shot installer: builds Torch.app, writes launchd plists, bootstraps services.

Usage:
    python3 src/install.py              # install both (build bundle + both services)
    python3 src/install.py --agent      # LaunchAgent + bundle build, no sudo
    python3 src/install.py --daemon     # LaunchDaemon only (sudo)
    python3 src/install.py --agent --alias  # skip full build, use existing
                                            # dist/Torch.app alias build
                                            # (dev iteration — keeps live-source
                                            # reloading)

On first run:
  1. Builds Torch.app via py2app into dist/Torch.app, then copies it
     to /Applications/Torch.app. Running Torch as a real .app bundle
     (not `python3 -m torchapp`) is what gives the process a proper
     CFBundleIdentifier so notifications attribute to "Torch" instead
     of "Python".
  2. Installs com.torch.tunneld as a root LaunchDaemon so the
     WiFi tunnel to Apple TV / iOS devices starts at boot and
     respawns on crash. This is the single admin-password moment.
  3. Installs com.torch.app as a user LaunchAgent pointing at
     /Applications/Torch.app/Contents/MacOS/Torch, auto-starting
     at login and respawning on crash.

Any already-running pymobiledevice3 tunneld process or menubar app
instance is killed first so the new launchd-managed versions take
over cleanly.

Idempotent — re-running the script rebuilds the bundle, re-copies it
to /Applications, and re-bootstraps both services with the latest
plist contents.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

# Make `torchapp` importable when run from the project root without
# having to remember PYTHONPATH on the command line.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from torchapp import launchd, paths  # noqa: E402

PROJECT_ROOT = HERE.parent
DIST_APP = PROJECT_ROOT / "dist" / "Torch.app"
APPLICATIONS_APP = Path("/Applications/Torch.app")


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
    # Menubar app: kill any "Python -m torchapp" (legacy) OR bundle
    # launcher currently running.
    subprocess.run(
        ["pkill", "-9", "-f", "Python.*-m torchapp"],
        capture_output=True,
    )
    subprocess.run(
        ["pkill", "-9", "-f", "Torch.app/Contents/MacOS/Torch"],
        capture_output=True,
    )
    # Stray tunneld: pymobiledevice3 remote tunneld run without launchd
    subprocess.run(
        ["sudo", "-n", "pkill", "-9", "-f", "pymobiledevice3 remote tunneld"],
        capture_output=True,
    )


def _build_torch_app(*, alias: bool) -> None:
    """Run py2app to build dist/Torch.app.

    Alias mode (`setup.py py2app -A`) symlinks Resources back to the
    source tree — fast rebuild, live source reload, ~3 seconds. Dev
    use only.

    Full mode (`setup.py py2app`) copies everything into the bundle,
    including Python.framework. ~30–60 seconds, ~90 MB output. What
    we copy to /Applications for production.
    """
    cmd = [sys.executable, "setup.py", "py2app"]
    if alias:
        cmd.append("-A")
    print(f"[+] Building Torch.app ({'alias' if alias else 'full'} mode)")
    result = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # py2app prints a LOT on success; only dump stderr on failure.
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(
            f"py2app build failed with exit {result.returncode}"
        )
    if not DIST_APP.exists():
        raise RuntimeError(
            f"py2app build succeeded but {DIST_APP} is missing"
        )


def _install_torch_app_to_applications() -> None:
    """Copy dist/Torch.app to /Applications/Torch.app.

    /Applications is admin-group writable on default macOS, so no
    sudo prompt is needed for the copy itself (unlike the
    LaunchDaemon install, which writes to /Library/LaunchDaemons).

    Ad-hoc codesigns the installed copy. py2app already ad-hoc signs
    the dist/ output, but the copy invalidates the signature on
    Apple Silicon macOS 14+, so we re-sign at the destination.
    """
    if APPLICATIONS_APP.exists():
        print(f"[+] Removing previous {APPLICATIONS_APP}")
        shutil.rmtree(APPLICATIONS_APP)
    print(f"[+] Copying {DIST_APP} -> {APPLICATIONS_APP}")
    shutil.copytree(DIST_APP, APPLICATIONS_APP, symlinks=True)

    print(f"[+] Ad-hoc codesigning {APPLICATIONS_APP}")
    result = subprocess.run(
        [
            "codesign", "--force", "--deep", "--sign", "-",
            str(APPLICATIONS_APP),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(
            f"codesign failed with exit {result.returncode}"
        )


def main() -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description="Install Torch launchd services")
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
    parser.add_argument(
        "--alias",
        action="store_true",
        help=(
            "Build dist/Torch.app in alias mode (live source reloading) "
            "instead of doing a full build + /Applications install. For "
            "dev iteration only; leaves /Applications/Torch.app alone."
        ),
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help=(
            "Skip the py2app build step entirely. Use when Torch.app "
            "is already present at /Applications (full install) or "
            "dist/ (alias install)."
        ),
    )
    args = parser.parse_args()

    if not args.agent and not args.daemon:
        args.agent = args.daemon = True

    paths.ensure_dirs()
    _kill_stray_processes()

    if args.agent and not args.skip_build:
        try:
            _build_torch_app(alias=args.alias)
        except RuntimeError as e:
            print(f"[!] {e}", file=sys.stderr)
            return 2
        if args.alias:
            # Dev iteration: wipe /Applications/Torch.app so the
            # LaunchAgent falls back to the fresh alias build in dist/.
            if APPLICATIONS_APP.exists():
                print(
                    f"[+] Removing {APPLICATIONS_APP} so dist/ alias "
                    f"build takes precedence"
                )
                shutil.rmtree(APPLICATIONS_APP)
        else:
            try:
                _install_torch_app_to_applications()
            except RuntimeError as e:
                print(f"[!] {e}", file=sys.stderr)
                return 2
            # Full install complete: wipe dist/ so the LaunchAgent
            # unambiguously picks up /Applications. Keeping both
            # around leads to "which version is actually running?"
            # confusion.
            if DIST_APP.exists():
                shutil.rmtree(DIST_APP)

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
        print("[+] Installing Torch menubar LaunchAgent")
        try:
            launchd.install_launch_agent()
        except launchd.LaunchdError as e:
            print(f"[!] LaunchAgent install failed: {e}", file=sys.stderr)
            return 2
        print(
            f"[+] {launchd.APP_LABEL} is running — look for the 🔥 flame icon "
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
