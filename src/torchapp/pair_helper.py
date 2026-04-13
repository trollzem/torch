"""Torch pair helper — device discovery + pairing, always run as a
subprocess from ui.py.

Replaces the bare `pymobiledevice3 remote pair` CLI with a filtered
version that:

  1. Deduplicates by device identifier (one row per Apple TV / iPhone
     / iPad, regardless of how many interfaces the mDNS scan saw).
  2. Prefers the IPv4 address of each device, falling back to a
     global IPv6 address, falling back to anything.
  3. Skips the interactive selection menu entirely when there's only
     one device in pairing mode — which is the usual case.

**This module imports pymobiledevice3 at top level.** pymobiledevice3
is excluded from the py2app Torch.app bundle (it's 150+ MB with its
transitive deps), so importing pair_helper from ui.py inside the
bundle raises ModuleNotFoundError. The menubar therefore runs this
module exclusively as a subprocess under Homebrew's python3.14, which
DOES have pymobiledevice3 in its site-packages. Two entry modes:

  - Plain CLI (Terminal handoff — legacy): `python3 -m torchapp.pair_helper`
    prints device-discovery output to stdout, pymobiledevice3 prompts
    for the PIN via `input()` on stdin from the user's terminal.
  - State-marker protocol (in-UI flow): `python3 pair_helper.py --state-markers`
    prints machine-parseable `STATE: <name>` lines to stdout and
    reads the PIN from stdin via a custom `pin_callback` that emits
    `STATE: awaiting_pin` before reading. The Torch menubar spawns
    this mode, watches stdout for `STATE: awaiting_pin`, shows a
    rumps.Window PIN dialog, and writes the PIN + newline back to
    the subprocess stdin.

pymobiledevice3's pairing code at
`pymobiledevice3/remote/tunnel_service.py::_request_pair_consent` does
`pin = input("Enter PIN: ")` when the device is an Apple TV. We
monkey-patch `builtins.input` for the duration of the connect() call
so that input() routes through our state-marker protocol instead of
writing a trailing-no-newline "Enter PIN: " prompt that can't be
reliably line-parsed by the parent.

pymobiledevice3 signals pairing success by RAISING
`RemotePairingCompletedError` as a flow-control exception; the state-
marker wrapper catches it and prints `STATE: pairing_complete` before
exiting 0.
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import ipaddress
import logging
import sys
from typing import Callable

from pymobiledevice3.bonjour import browse_remotepairing_manual_pairing
from pymobiledevice3.remote.tunnel_service import (
    RemotePairingManualPairingService,
)

log = logging.getLogger(__name__)


class PairHelperError(Exception):
    """Base class for pair_helper failures surfaced to the UI."""


class NoDeviceInPairingMode(PairHelperError):
    """No advertised device was found on the network."""


class NamedDeviceNotFound(PairHelperError):
    """`device_name` filter matched zero answers."""


class NoUsableAddress(PairHelperError):
    """The picked device had no IPv4/IPv6 address in the bonjour answer."""


def _is_ipv4(ip: str) -> bool:
    """True if `ip` parses as a bare IPv4 address (ignores %iface scope)."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
        return isinstance(addr, ipaddress.IPv4Address)
    except ValueError:
        return False


def _pick_best_address(addresses) -> str | None:
    """Pick the most-routable address from a bonjour answer's address list.

    Order:
      1. First IPv4 — always the simplest and most reliable on a LAN.
      2. First non-link-local IPv6 (ULA or global) — avoids scope-ID
         surprises that can make handshakes stall.
      3. First address of any kind (fallback).
    """
    ipv4 = next((a.full_ip for a in addresses if _is_ipv4(a.full_ip)), None)
    if ipv4:
        return ipv4

    global_v6 = next(
        (
            a.full_ip
            for a in addresses
            if not a.full_ip.lower().startswith("fe80:")
            and not _is_ipv4(a.full_ip)
        ),
        None,
    )
    if global_v6:
        return global_v6

    return addresses[0].full_ip if addresses else None


async def _discover_and_pick(device_name: str | None):
    """Find a device to pair with and return (answer, address, name).

    Shared helper for both the CLI entry point (`pair`) and the in-UI
    entry point (`pair_async`). Raises PairHelperError subclasses on
    failure so each caller can map them to an appropriate UX.
    """
    answers = await browse_remotepairing_manual_pairing()
    if not answers:
        raise NoDeviceInPairingMode(
            "No devices are currently advertising a pairing prompt on "
            "the network. On your Apple TV, go to Settings → General → "
            "Remotes and Devices → Remote App and Devices and leave that "
            "screen open."
        )

    if device_name:
        answers = [
            a for a in answers if a.properties.get("name") == device_name
        ]
        if not answers:
            raise NamedDeviceNotFound(
                f"No device named {device_name!r} in pairing mode right now."
            )

    # Collapse duplicates: mDNS can see the same device on multiple
    # interfaces and produce several answers with the same identifier.
    seen: set[str] = set()
    unique = []
    for answer in answers:
        ident = answer.properties.get("identifier")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        unique.append(answer)

    if not unique:
        raise NoDeviceInPairingMode(
            "No pairing-mode devices with valid identifiers."
        )

    picked = unique[0]
    address = _pick_best_address(picked.addresses)
    if address is None:
        name = picked.properties.get("name", "<unnamed>")
        raise NoUsableAddress(
            f"Device {name!r} has no usable IP address — aborting."
        )

    return picked, address, picked.properties.get("name", "<unnamed>"), unique


async def pair_async(
    device_name: str | None = None,
    pin_callback: Callable[[], str] | None = None,
) -> str:
    """Async pairing entry point used by the in-UI flow.

    Returns the paired device's identifier on success. Raises
    PairHelperError subclasses on discovery failures and
    RemotePairingCompletedError (from pymobiledevice3) on success —
    callers MUST catch RemotePairingCompletedError and treat it as
    success, since pymobiledevice3 uses it as flow control.

    When `pin_callback` is provided, it replaces `builtins.input` for
    the duration of the connect() call. This is the hook that lets the
    UI collect the 6-digit PIN via a rumps.Window instead of stdin.
    `pin_callback` is only invoked for Apple TV devices — iPhone/iPad
    pairing uses the device's own Trust dialog and never reaches the
    input() path.
    """
    picked, address, name, _unique = await _discover_and_pick(device_name)
    identifier = picked.properties["identifier"]
    log.info("pairing with %s at %s", name, address)

    async def _do_connect() -> None:
        async with RemotePairingManualPairingService(
            identifier, address, picked.port
        ) as service:
            await service.connect(autopair=True)

    if pin_callback is None:
        await _do_connect()
    else:
        # Scope the monkey-patch so it can't leak into unrelated code.
        original_input = builtins.input
        def _ui_input(prompt: str = "") -> str:
            # Ignore the "Enter PIN: " prompt text; the UI has its
            # own copy. Return whatever the callback gives us as a
            # string (pymobiledevice3 expects a str, strips whitespace
            # internally via SRP).
            return pin_callback()
        builtins.input = _ui_input  # type: ignore[assignment]
        try:
            await _do_connect()
        finally:
            builtins.input = original_input

    return identifier


async def pair(device_name: str | None = None) -> int:
    """CLI entry point. Preserves the original stdout-based UX."""
    try:
        picked, address, name, unique = await _discover_and_pick(device_name)
    except NoDeviceInPairingMode as e:
        print(str(e))
        return 2
    except NamedDeviceNotFound as e:
        print(str(e))
        return 2
    except NoUsableAddress as e:
        print(str(e))
        return 2

    if len(unique) > 1:
        print("Multiple devices in pairing mode:")
        for i, a in enumerate(unique, 1):
            dn = a.properties.get("name", "<unnamed>")
            print(f"  {i}. {dn}")
        print()
        print(
            "Pairing with the first one. Re-run with a name argument "
            "to target a specific device:"
        )
        print("  python3 -m torchapp.pair_helper 'Exact Device Name'")
        print()

    identifier = picked.properties["identifier"]
    print(f"Pairing with {name} at {address}")
    print()

    async with RemotePairingManualPairingService(
        identifier, address, picked.port
    ) as service:
        await service.connect(autopair=True)

    return 0


def _state_marker_pin_callback() -> str:
    """pin_callback for the --state-markers protocol.

    Emits `STATE: awaiting_pin` as a complete line on stdout and
    flushes, then blocks reading a single line from stdin. The
    parent process (ui.py worker thread) watches for this marker,
    shows a rumps.Window dialog, and writes the 6-digit PIN + newline
    back to this subprocess's stdin.
    """
    print("STATE: awaiting_pin", flush=True)
    line = sys.stdin.readline()
    return line.strip()


async def _run_state_marker(device_name: str | None) -> int:
    """Async entry point for the --state-markers protocol.

    Prints state transitions as parseable `STATE: <name>` lines on
    stdout. Catches PairHelperError subclasses and
    RemotePairingCompletedError (pymobiledevice3's success signal)
    and translates them into final state markers + exit codes.
    """
    print("STATE: searching", flush=True)
    try:
        await pair_async(
            device_name=device_name,
            pin_callback=_state_marker_pin_callback,
        )
    except NoDeviceInPairingMode as e:
        print(f"STATE: no_device: {e}", flush=True)
        return 2
    except NamedDeviceNotFound as e:
        print(f"STATE: no_device: {e}", flush=True)
        return 2
    except NoUsableAddress as e:
        print(f"STATE: error: {e}", flush=True)
        return 3
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "RemotePairingCompletedError":
            print("STATE: pairing_complete", flush=True)
            return 0
        print(f"STATE: error: {type(exc).__name__}: {exc}", flush=True)
        return 1
    # Didn't raise at all — treat as success to be safe.
    print("STATE: pairing_complete", flush=True)
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Torch pair helper")
    parser.add_argument(
        "--state-markers",
        action="store_true",
        help=(
            "Emit parseable STATE: lines on stdout and read PIN from "
            "stdin. Used by the Torch menubar's in-UI pairing flow."
        ),
    )
    parser.add_argument(
        "device_name",
        nargs="?",
        default=None,
        help="Optional exact device name to filter bonjour results.",
    )
    args = parser.parse_args()

    if args.state_markers:
        try:
            return asyncio.run(_run_state_marker(args.device_name))
        except KeyboardInterrupt:
            print("STATE: error: interrupted", flush=True)
            return 130

    # Classic CLI / Terminal-handoff mode — unchanged backwards-compat.
    try:
        return asyncio.run(pair(args.device_name))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        # pymobiledevice3 signals pairing success by raising
        # RemotePairingCompletedError as a flow-control exception.
        if type(exc).__name__ == "RemotePairingCompletedError":
            print()
            print("Pairing completed successfully.")
            return 0
        print(f"Pairing failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
