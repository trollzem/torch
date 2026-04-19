"""pymobiledevice3 wrapper.

Thin subprocess + HTTP layer over the pymobiledevice3 CLI and the
persistent tunneld HTTP API. Handles:

  - querying tunneld for paired devices and their tunnel addresses
  - resolving a pair_record_identifier to a live (addr, port) tunnel
  - fetching lockdown info through a tunnel (for reconciliation)
  - reconciling a seeded Device with real UDID / class / product info
  - installing a signed IPA through the tunnel
  - bonjour scanning for manual-pairing devices (pre-pair check)

Everything is synchronous — pymobiledevice3's async Python API is
available but subprocess is what we tested in the spike, and the
extra research cost is not worth it for this MVP.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import paths
from .config import Device

log = logging.getLogger(__name__)

# Default install timeout. The spike showed successful installs complete
# in 20-30 seconds for 50 MB IPAs, so 180s leaves headroom for slow WiFi
# while still catching hangs (e.g. installd waiting on a running app we
# failed to terminate).
_INSTALL_TIMEOUT = 180.0


class Pymd3Error(Exception):
    """Base for all pymobiledevice3 wrapper failures."""


class TunneldDownError(Pymd3Error):
    """tunneld HTTP API is not reachable on 127.0.0.1:49151."""


class TunnelNotFoundError(Pymd3Error):
    """Requested pair_record_identifier is not currently tunneled."""


class InstallError(Pymd3Error):
    """`pymobiledevice3 apps install` failed."""


class LockdownError(Pymd3Error):
    """`pymobiledevice3 lockdown info` failed."""


class DvtError(Pymd3Error):
    """DVT (process control) subcommand failed. Usually non-fatal — the caller
    can swallow this and continue if developer mode isn't enabled on the device."""


# --- tunneld HTTP API --------------------------------------------------------


def tunneld_info(timeout: float = 3.0) -> dict[str, list[dict[str, Any]]]:
    """GET http://127.0.0.1:49151/ and return parsed JSON.

    Raises TunneldDownError if the service isn't running or responds with
    something that isn't JSON.
    """
    try:
        with urllib.request.urlopen(paths.TUNNELD_URL, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError) as e:
        raise TunneldDownError(
            f"tunneld not reachable at {paths.TUNNELD_URL}: {e}"
        ) from e
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise TunneldDownError(f"tunneld returned non-JSON: {e}") from e
    if not isinstance(data, dict):
        raise TunneldDownError(f"unexpected tunneld response shape: {type(data).__name__}")
    return data


def is_tunneld_up() -> bool:
    """Non-raising check for tunneld health."""
    try:
        tunneld_info(timeout=1.5)
        return True
    except TunneldDownError:
        return False


def tunnel_for_pair_id(pair_id: str) -> tuple[str, int] | None:
    """Return (tunnel_address, tunnel_port) for a given pair_record_identifier.

    tunneld keys its response by the pair_record_identifier, mapping to a
    list of active tunnels (usually just one per device — the WiFi tunnel
    and any usbmux tunnel are distinct entries). We prefer the WiFi tunnel
    over USB when both are present.
    """
    info = tunneld_info()
    entries = info.get(pair_id)
    if not entries:
        return None

    # Prefer the WiFi interface if multiple are available. tunneld marks
    # WiFi interfaces as an IP address (192.168.x.x, fe80::x), and USB
    # interfaces as "usbmux-...". We sort non-usbmux first.
    def is_usb(entry: dict[str, Any]) -> bool:
        iface = entry.get("interface", "")
        return isinstance(iface, str) and iface.startswith("usbmux")

    sorted_entries = sorted(entries, key=is_usb)
    first = sorted_entries[0]
    addr = first.get("tunnel-address")
    port = first.get("tunnel-port")
    if not isinstance(addr, str) or not isinstance(port, int):
        raise TunneldDownError(f"malformed tunnel entry for {pair_id}: {first}")
    return addr, port


def all_tunneled_pair_ids() -> list[str]:
    """Return every pair_record_identifier that tunneld currently knows about."""
    return list(tunneld_info().keys())


# --- pymobiledevice3 CLI helpers ---------------------------------------------


def _run_pymd3(args: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """Run pymobiledevice3 with capturing, no shell."""
    cmd = ["pymobiledevice3", *args]
    log.debug("pymobiledevice3 run: %s", " ".join(args))
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        # launchd strips LANG/LC_ALL; without an explicit encoding Python
        # falls back to ASCII and any non-ASCII byte in stdout crashes
        # subprocess.communicate. Device names with curly apostrophes
        # (e.g. "Hazem's iPad") trip this — force UTF-8 decoding.
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def lockdown_info(tunnel_addr: str, tunnel_port: int) -> dict[str, Any]:
    """Return parsed lockdown info for the device at the given tunnel."""
    result = _run_pymd3(
        ["lockdown", "info", "--rsd", tunnel_addr, str(tunnel_port)]
    )
    if result.returncode != 0:
        raise LockdownError(
            f"lockdown info failed (exit={result.returncode}): "
            f"{result.stderr[-500:]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise LockdownError(f"lockdown info returned non-JSON: {e}") from e


def _device_class_from_lockdown(info: dict[str, Any]) -> str:
    """Normalize lockdown's DeviceClass to our config vocabulary."""
    raw = info.get("DeviceClass", "") or ""
    lower = raw.lower()
    if "appletv" in lower or lower == "appletv":
        return "tvOS"
    if "ipad" in lower:
        return "iPadOS"
    if "iphone" in lower or "ipod" in lower:
        return "iOS"
    return raw or "unknown"


def reconcile_device(device: Device) -> Device:
    """Fill in Device.udid / device_class / product_type / product_version
    / name by querying tunneld and lockdown info.

    Returns a new Device (dataclass is intended to be treated as immutable
    here — callers should swap the old reference). Raises TunnelNotFoundError
    if the device isn't currently in tunneld; in that case the caller can
    either skip the device or display "offline".
    """
    pair_id = device.pair_record_identifier
    tunnel = tunnel_for_pair_id(pair_id)
    if tunnel is None:
        raise TunnelNotFoundError(
            f"device {pair_id} not currently tunneled (not powered on "
            f"or tunneld hasn't connected yet)"
        )
    addr, port = tunnel
    info = lockdown_info(addr, port)

    udid = info.get("UniqueDeviceID") or device.udid
    device_class = _device_class_from_lockdown(info)
    product_type = info.get("ProductType")
    product_version = info.get("ProductVersion")
    # Prefer the friendly name from the device itself over the pair UUID
    # placeholder we stored during seeding.
    name = info.get("DeviceName") or device.name

    return replace(
        device,
        udid=udid,
        device_class=device_class,
        product_type=product_type,
        product_version=product_version,
        name=name,
    )


def reconcile_all(devices: list[Device]) -> list[Device]:
    """Reconcile every device, leaving offline ones with their old data.

    Never raises — devices that can't be reconciled are returned unchanged.
    Callers should log or surface the offline state if they care.
    """
    out: list[Device] = []
    for d in devices:
        try:
            out.append(reconcile_device(d))
        except (TunnelNotFoundError, LockdownError, TunneldDownError) as e:
            log.warning(
                "could not reconcile device %s: %s", d.pair_record_identifier, e
            )
            out.append(d)
    return out


# --- DVT process control (for pre-install kill) ------------------------------


def get_pid_for_bundle(
    tunnel_addr: str, tunnel_port: int, bundle_id: str
) -> int | None:
    """Return the PID of a running app via DVT, or None if not running.

    Requires Developer Mode + Developer Disk Image on the target device.
    Raises DvtError if the service is unreachable (typical on production
    iPhones without Developer Mode). tvOS 26+ devices generally have this
    working out of the box after the first tunneld connection.
    """
    result = _run_pymd3(
        [
            "developer",
            "dvt",
            "process-id-for-bundle-id",
            "--rsd",
            tunnel_addr,
            str(tunnel_port),
            bundle_id,
        ],
        timeout=30,
    )
    if result.returncode != 0:
        raise DvtError(
            f"process-id-for-bundle-id failed (exit={result.returncode}): "
            f"{result.stderr[-400:]}"
        )
    # DVT prints either a bare integer PID or "None" (or similar) for
    # not-running. Stdout may also be empty on some firmwares — in which
    # case we assume the app isn't running.
    stdout = (result.stdout or "").strip()
    if not stdout:
        return None
    # Strip any "PID:" prefix or quotes DVT might add
    for token in stdout.splitlines():
        token = token.strip().strip('"')
        if token.isdigit():
            return int(token)
    return None


def kill_process(tunnel_addr: str, tunnel_port: int, pid: int) -> None:
    """Kill a process by PID via DVT."""
    result = _run_pymd3(
        [
            "developer",
            "dvt",
            "kill",
            "--rsd",
            tunnel_addr,
            str(tunnel_port),
            str(pid),
        ],
        timeout=15,
    )
    if result.returncode != 0:
        raise DvtError(
            f"dvt kill failed (exit={result.returncode}): "
            f"{result.stderr[-400:]}"
        )


def terminate_bundle_if_running(
    tunnel_addr: str, tunnel_port: int, bundle_id: str
) -> bool:
    """Best-effort pre-install kill of a running app. Returns True if a
    running PID was found and killed, False otherwise.

    Never raises — if DVT is unavailable or the kill fails, we log and
    return False so the caller can decide whether to proceed anyway.
    """
    try:
        pid = get_pid_for_bundle(tunnel_addr, tunnel_port, bundle_id)
    except DvtError as e:
        log.debug("DVT unavailable for %s: %s", bundle_id, e)
        return False
    if pid is None:
        log.debug("bundle %s is not currently running", bundle_id)
        return False
    log.info("terminating running app %s (pid=%d) before install", bundle_id, pid)
    try:
        kill_process(tunnel_addr, tunnel_port, pid)
    except DvtError as e:
        log.warning("failed to kill %s pid=%d: %s", bundle_id, pid, e)
        return False
    # Give the device a moment for installd to release whatever lock it
    # was holding on the bundle's on-disk files before we re-upload.
    time.sleep(1.5)
    return True


# --- Install -----------------------------------------------------------------


def install_ipa(
    tunnel_addr: str,
    tunnel_port: int,
    ipa_path: Path,
    *,
    terminate_bundle_id: str | None = None,
) -> None:
    """Install a signed IPA through the given tunnel. Raises on any failure.

    If `terminate_bundle_id` is provided, we first try to kill any running
    instance of that bundle via DVT. This works around `installd` hanging
    indefinitely when asked to replace a currently-running app (we hit this
    with YouTube on tvOS 26.4 — `apps install` sits at 0% CPU waiting for
    the frontmost app to exit, and the wait has no timeout on its own).

    The pre-kill is best-effort; if DVT isn't available (no Developer Mode)
    we still attempt the install, relying on the subprocess timeout to
    surface the hang case instead of waiting forever.
    """
    if not ipa_path.exists():
        raise InstallError(f"IPA not found: {ipa_path}")

    if terminate_bundle_id:
        terminate_bundle_if_running(tunnel_addr, tunnel_port, terminate_bundle_id)

    log.info(
        "installing %s via tunnel %s:%d", ipa_path.name, tunnel_addr, tunnel_port
    )
    try:
        result = _run_pymd3(
            [
                "apps",
                "install",
                "--rsd",
                tunnel_addr,
                str(tunnel_port),
                str(ipa_path),
            ],
            timeout=_INSTALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise InstallError(
            f"install timed out after {_INSTALL_TIMEOUT:.0f}s — the target app "
            f"may be running on the device and DVT pre-kill did not succeed. "
            f"Close the app on the device and retry."
        ) from e

    if result.returncode != 0:
        tail = result.stderr[-800:] or result.stdout[-800:]
        raise InstallError(
            f"apps install failed (exit={result.returncode}):\n{tail}"
        )
    log.info("install of %s succeeded", ipa_path.name)


# --- Bonjour scan (for pairing wizard) ---------------------------------------


def scan_manual_pairing(timeout: float = 8.0) -> list[dict[str, Any]]:
    """Scan for devices currently advertising _remotepairing-manual-pairing._tcp.

    Used by the pairing wizard to verify the Apple TV / iPhone is actually
    in pairing mode before we kick off the PIN flow.
    """
    result = _run_pymd3(
        ["bonjour", "remotepairing-manual-pairing"], timeout=timeout + 2
    )
    if result.returncode != 0:
        raise Pymd3Error(
            f"bonjour scan failed (exit={result.returncode}): "
            f"{result.stderr[-400:]}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return data
