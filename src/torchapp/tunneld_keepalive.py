"""Standalone watchdog that restarts the tunneld LaunchDaemon when
its long-uptime mDNS discovery has gone stale.

Runs as a root LaunchDaemon (com.torch.tunneld-keepalive) on a 30-min
interval. Lives outside the Torch.app bundle so it never gets killed
or restarted by the menubar lifecycle.

Decision logic each tick:

  1. Find the tunneld process (pgrep for `pymobiledevice3 remote tunneld`).
     If no process is running — leave it alone. The com.torch.tunneld
     LaunchDaemon itself has KeepAlive=true and will respawn it on
     crash; if it's not running, it's because the user uninstalled
     Torch or something is broken at a layer we shouldn't touch.

  2. If tunneld uptime exceeds MAX_TUNNELD_UPTIME (default 7 days),
     restart it unconditionally. This is the load-bearing case —
     observed 2026-06-20 with a tunneld running 44 days that had
     stopped reporting devices despite them being on the network.
     Long-uptime mDNS state rot is the root cause we're preempting.

  3. If tunneld uptime exceeds STALE_EMPTY_THRESHOLD (default 6 hours)
     AND tunneld's HTTP inventory is empty AND the local Mac has
     pair records on disk (i.e. we expect tunneld to see at least
     one device) — restart it. Catches the failure mode early
     without waiting for the 7-day uptime cap.

A restart is `launchctl kickstart -k system/com.torch.tunneld` —
launchd handles the actual respawn, we just trigger it.

Logs to /var/log/torch-tunneld-keepalive.log so the user can audit
what we did and when. Both checks AND restarts are logged.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

LOG_PATH = Path("/var/log/torch-tunneld-keepalive.log")
TUNNELD_LABEL = "com.torch.tunneld"
TUNNELD_URL = "http://127.0.0.1:49151/"

# Uptime past which we restart unconditionally — long-uptime mDNS
# state rot is a known tunneld pathology.
MAX_TUNNELD_UPTIME_SECONDS = 7 * 24 * 3600  # 7 days

# Uptime past which an empty inventory + expected pair records
# triggers a restart. Six hours gives normal cold-start / device-off
# windows plenty of room without sitting on a real stale state
# for half a day.
STALE_EMPTY_THRESHOLD_SECONDS = 6 * 3600  # 6 hours

# Where pymobiledevice3 stores the RemotePairing records that
# tunneld discovers iOS / tvOS devices from. We use the count of
# pair records as the "we expect at least one device" check.
PYMD3_DIR_CANDIDATES = [
    # When launchd sets HOME in the plist (we do for com.torch.tunneld),
    # root's HOME points at the installing user's home dir.
    Path(os.environ.get("HOME", "/")) / ".pymobiledevice3",
    # Fallbacks just in case HOME isn't set as expected.
    Path("/var/root/.pymobiledevice3"),
]


def _setup_logging() -> None:
    """Log to /var/log when running as root (LaunchDaemon); fall back
    to stderr when invoked by a non-root user (dev testing). Both
    formats match so the diagnostic value of the log lines is the
    same either way."""
    fmt = "%(asctime)s %(levelname)s %(message)s"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=str(LOG_PATH),
            level=logging.INFO,
            format=fmt,
        )
    except (PermissionError, OSError):
        logging.basicConfig(
            stream=sys.stderr,
            level=logging.INFO,
            format=fmt,
        )


def _tunneld_pid() -> int | None:
    """Return tunneld's PID or None if it's not running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "pymobiledevice3 remote tunneld"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    pids = [int(line) for line in result.stdout.split() if line.strip().isdigit()]
    return pids[0] if pids else None


def _process_uptime_seconds(pid: int) -> float | None:
    """Return how many seconds `pid` has been running, or None on error.

    macOS BSD ps doesn't support `etimes` (only Linux procps does), so
    we ask for the formatted `etime` and parse it ourselves. Format:
      MM:SS                 (< 1 hour)
      HH:MM:SS              (< 1 day)
      DD-HH:MM:SS           (>= 1 day)
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    days = 0
    if "-" in raw:
        d_str, raw = raw.split("-", 1)
        try:
            days = int(d_str)
        except ValueError:
            return None
    parts = raw.split(":")
    try:
        parts_int = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts_int) == 3:
        h, m, s = parts_int
    elif len(parts_int) == 2:
        h = 0
        m, s = parts_int
    else:
        return None
    return float(days * 86400 + h * 3600 + m * 60 + s)


def _tunneld_inventory_size() -> int | None:
    """Return the number of devices tunneld reports, or None on error."""
    try:
        with urllib.request.urlopen(TUNNELD_URL, timeout=3) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError):
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return len(data)


def _pair_record_count() -> int:
    """Count RemotePairing pair records on disk so we know whether
    tunneld is *supposed* to be seeing devices."""
    for d in PYMD3_DIR_CANDIDATES:
        if d.exists():
            try:
                return len(list(d.glob("remote_*.plist")))
            except OSError:
                continue
    return 0


def _kickstart_tunneld() -> bool:
    """Kick tunneld via launchctl. Returns True on success."""
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"system/{TUNNELD_LABEL}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        logging.error("launchctl kickstart timed out")
        return False
    if result.returncode != 0:
        logging.error(
            "launchctl kickstart failed (exit=%d): %s",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False
    return True


def main() -> int:
    _setup_logging()

    pid = _tunneld_pid()
    if pid is None:
        logging.info("tunneld not running; nothing to do")
        return 0

    uptime = _process_uptime_seconds(pid)
    if uptime is None:
        logging.warning("could not read tunneld (pid=%d) uptime; skipping", pid)
        return 0

    inv = _tunneld_inventory_size()
    pairs = _pair_record_count()

    logging.info(
        "tunneld pid=%d uptime=%.0fs inventory=%s pair_records=%d",
        pid,
        uptime,
        "?" if inv is None else inv,
        pairs,
    )

    reason: str | None = None
    if uptime > MAX_TUNNELD_UPTIME_SECONDS:
        reason = f"uptime {uptime/86400:.1f}d exceeds {MAX_TUNNELD_UPTIME_SECONDS/86400:.0f}d cap"
    elif (
        uptime > STALE_EMPTY_THRESHOLD_SECONDS
        and inv == 0
        and pairs > 0
    ):
        reason = (
            f"empty inventory for >{STALE_EMPTY_THRESHOLD_SECONDS/3600:.0f}h "
            f"with {pairs} pair record(s) on disk"
        )

    if reason is None:
        return 0

    logging.info("restarting tunneld: %s", reason)
    ok = _kickstart_tunneld()
    if ok:
        logging.info("tunneld restart succeeded")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
