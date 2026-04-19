"""Device-class-aware IPA install dispatcher.

tvOS installs go through pymobiledevice3 `apps install --rsd` (the
RemotePairing/RSD path) because tvOS 26+ removed classic lockdown
pairing.

iOS / iPadOS installs go through `ideviceinstaller` (the classic
libimobiledevice path) because pymobiledevice3's `apps install --rsd`
hangs indefinitely mid-transfer over the `usbmux-<UDID>-Network`
tunnels iOS devices use. This was confirmed 2026-04-19 by
back-to-back tests:

    pymd3 apps install --rsd <iPhone tunnel>  -> 0% CPU, hung forever
    ideviceinstaller -u <UDID> install <ipa>   -> ~30-70s, success

pymd3 DVT (process control) still works fine over the iOS tunnel —
only the large-file AFC transfer stage of `apps install` is broken.
So we still use pymd3 for the best-effort pre-install DVT kill of
the target bundle, and only swap the bulk-transfer step to
ideviceinstaller.

Each install raises one of:
  - DeviceOfflineError : device isn't reachable right now. Retry
                          next cycle; do NOT count as a failure
                          strike.
  - DeviceAtCapError   : Apple rejected with "max 3 apps per device"
                          and surfaced a list of the already-
                          installed bundle IDs (including ones not
                          tracked by Torch — e.g. apps installed via
                          Sideloadly, AltStore, Xcode). User must
                          free a slot; don't count as strike.
  - InstallFailedError : genuine install error. Counts as a strike
                          toward MAX_CONSECUTIVE_FAILURES.
"""

from __future__ import annotations

import logging
import re
import socket
import subprocess
from pathlib import Path

from . import pymd3
from .config import Device

log = logging.getLogger(__name__)

# Generous upper bound on ideviceinstaller install runtime. A 117 MB
# IPA over Wi-Fi measured ~70s end-to-end on a local test; larger
# IPAs or flaky Wi-Fi can push much higher. 4 minutes is enough for
# anything reasonable without letting the main refresh cycle hang
# forever if something goes wrong.
_IDEVICEINSTALLER_TIMEOUT = 240.0

# Pre-install TCP probe timeout. If the tunnel listener is alive it
# completes the TCP handshake in <100ms locally; if the tunnel is
# stale (iPhone dozed) the socket read sits until SYN timeout. We
# cap at 2s so a sleeping device classifies as offline quickly
# without the ~60s pymd3 asyncio timeout fallback.
_TCP_PROBE_TIMEOUT = 2.0


class InstallerError(Exception):
    """Base for errors raised by install_for_device."""


class DeviceOfflineError(InstallerError):
    """Device isn't reachable. Retry next cycle; do NOT bump strike count."""


class DeviceAtCapError(InstallerError):
    """Free-tier 3-app cap reached. Carries Apple's list of installed
    bundle IDs so the UI can surface exactly which apps are occupying
    the slots — helpful when slots are held by apps Torch doesn't
    track (Sideloadly / AltStore / Xcode / etc.)."""

    def __init__(self, external_bundle_ids: list[str]):
        self.external_bundle_ids = external_bundle_ids
        super().__init__(
            f"device at free-tier 3-app cap; installed bundles: "
            f"{', '.join(external_bundle_ids)}"
        )


class InstallFailedError(InstallerError):
    """Actual install error — count as a strike, retry next cycle."""


# Apple's "max apps" error:
#
#   ApplicationVerificationFailed ... 0xe8008021: This device has
#   reached the maximum number of installed apps using a free
#   developer profile: {(
#       "3G6AP3U89B.com.stossy11.MeloNX.3G6AP3U89B",
#       "3G6AP3U89B.me.oatmealdome.DolphiniOS-...",
#       "3G6AP3U89B.com.streamer.ios..."
#   )}
#
# The DOTALL + non-greedy body capture handles the multi-line shape.
_APP_CAP_ERR_RE = re.compile(
    r"maximum number of installed apps.*?\{\((?P<body>.*?)\)\}",
    re.DOTALL,
)


def tcp_probe(host: str, port: int, timeout: float = _TCP_PROBE_TIMEOUT) -> bool:
    """One-shot TCP handshake check against host:port.

    Returns True on handshake success, False on any socket error or
    timeout. Used as a pre-install reachability test on RSD tunnels —
    if the advertised tunnel is stale we want to know in ~1s, not
    after pymd3's ~60s async connect timeout.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _libimobile_usb_available(udid: str, timeout: float = 3.0) -> bool:
    """Is the device currently on the USB bus per usbmuxd?"""
    try:
        result = subprocess.run(
            ["idevice_id", "-l"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return udid in (result.stdout or "")


def _libimobile_network_available(udid: str, timeout: float = 3.0) -> bool:
    """Is the device advertising itself over Bonjour (`_apple-mobdev2`)?"""
    try:
        result = subprocess.run(
            ["idevice_id", "-n"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return udid in (result.stdout or "")


def install_for_device(
    device: Device,
    ipa_path: Path,
    *,
    signed_bundle_id: str | None = None,
) -> None:
    """Install an IPA on one device. Routes by device_class.

    Raises InstallerError (one of the three subclasses) on any
    problem. Caller decides how to classify — refresh.refresh_one
    treats Offline as a soft skip, AtCap as a user-fixable failure
    (don't burn strikes), and InstallFailed as a hard failure
    (do burn strikes).
    """
    if device.device_class == "tvOS":
        _install_tvos(device, ipa_path, signed_bundle_id=signed_bundle_id)
    elif device.device_class in ("iOS", "iPadOS"):
        _install_ios(device, ipa_path, signed_bundle_id=signed_bundle_id)
    else:
        raise InstallFailedError(
            f"{device.name}: unsupported device_class "
            f"{device.device_class!r}"
        )


def _install_tvos(
    device: Device,
    ipa_path: Path,
    *,
    signed_bundle_id: str | None,
) -> None:
    """tvOS install path — unchanged from pre-2026-04-19: pymd3
    apps install --rsd over the RemotePairing tunnel."""
    tunnel = pymd3.tunnel_for_pair_id(device.pair_record_identifier)
    if tunnel is None:
        raise DeviceOfflineError(
            f"{device.name}: not currently in tunneld"
        )
    addr, port = tunnel
    if not tcp_probe(addr, port):
        raise DeviceOfflineError(
            f"{device.name}: tunnel {addr}:{port} not responding "
            f"(device asleep / network drop)"
        )
    try:
        pymd3.install_ipa(
            addr, port, ipa_path, terminate_bundle_id=signed_bundle_id
        )
    except pymd3.InstallError as e:
        capped = _extract_capped_bundles(str(e))
        if capped:
            raise DeviceAtCapError(capped) from e
        raise InstallFailedError(str(e)) from e


def _install_ios(
    device: Device,
    ipa_path: Path,
    *,
    signed_bundle_id: str | None,
) -> None:
    """iOS / iPadOS install path — classic libimobiledevice.

    Prefers USB when the device is on the bus (faster, more reliable
    than Wi-Fi for large IPAs). Falls back to `-n` network mode via
    Bonjour/mDNS when not plugged in.
    """
    if not device.udid:
        raise InstallFailedError(
            f"{device.name}: no UDID reconciled yet"
        )

    usb = _libimobile_usb_available(device.udid)
    net = False
    if not usb:
        net = _libimobile_network_available(device.udid)
    if not usb and not net:
        raise DeviceOfflineError(
            f"{device.name}: not reachable over USB or Bonjour"
        )

    # Best-effort pre-kill of the running bundle via pymd3 DVT. Works
    # over the same RSD tunnel even though apps install --rsd itself
    # is broken for iOS. Without the pre-kill, installd on the device
    # can block the install indefinitely waiting for the old copy to
    # exit. Silently skip if tunnel/DeveloperMode unavailable.
    if signed_bundle_id:
        try:
            tunnel = pymd3.tunnel_for_pair_id(device.pair_record_identifier)
            if tunnel is not None and tcp_probe(tunnel[0], tunnel[1]):
                pymd3.terminate_bundle_if_running(
                    tunnel[0], tunnel[1], signed_bundle_id
                )
        except Exception as e:  # noqa: BLE001
            log.debug("DVT pre-kill skipped for %s: %s", device.name, e)

    transport = "USB" if usb else "network"
    log.info(
        "installing %s on %s via %s (classic libimobiledevice)",
        ipa_path.name,
        device.name,
        transport,
    )
    net_flag = [] if usb else ["-n"]
    try:
        result = subprocess.run(
            [
                "ideviceinstaller",
                "-u",
                device.udid,
                *net_flag,
                "install",
                str(ipa_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_IDEVICEINSTALLER_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise InstallFailedError(
            f"{device.name}: ideviceinstaller timed out after "
            f"{_IDEVICEINSTALLER_TIMEOUT:.0f}s"
        ) from e
    except FileNotFoundError as e:
        raise InstallFailedError(
            "ideviceinstaller not installed — run `brew install "
            "ideviceinstaller`"
        ) from e

    combined = (result.stdout or "") + (result.stderr or "")

    capped = _extract_capped_bundles(combined)
    if capped:
        raise DeviceAtCapError(capped)

    # ideviceinstaller prints "Install: Complete" / "InstallComplete"
    # on success regardless of exit code being 0, so check both.
    if result.returncode != 0 or (
        "Install: Complete" not in combined
        and "InstallComplete" not in combined
    ):
        raise InstallFailedError(
            f"{device.name}: ideviceinstaller exit="
            f"{result.returncode}: {combined[-600:].strip() or 'no output'}"
        )


def _extract_capped_bundles(text: str) -> list[str] | None:
    """Parse Apple's max-apps error into the list of bundle IDs.

    Returns None if the text isn't that error (so the caller can
    decide to raise a generic failure instead).
    """
    m = _APP_CAP_ERR_RE.search(text)
    if not m:
        return None
    body = m.group("body")
    return re.findall(r'"([^"]+)"', body)
