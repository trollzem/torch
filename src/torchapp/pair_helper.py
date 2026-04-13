"""Torch pair helper — called from the menubar's "Add Apple TV" handoff.

Replaces the bare `pymobiledevice3 remote pair` CLI with a filtered
version that:

  1. Deduplicates by device identifier (one row per Apple TV / iPhone
     / iPad, regardless of how many interfaces the mDNS scan saw).
  2. Prefers the IPv4 address of each device, falling back to a
     global IPv6 address, falling back to anything.
  3. Skips the interactive selection menu entirely when there's only
     one device in pairing mode — which is the usual case.

The default `pymobiledevice3 remote pair` CLI builds one menu row per
(device, interface-address) pair. On a Mac with IPv4 + IPv6 ULA + link-
local, that produces 6-8 visually-identical entries for a single
Apple TV, which is confusing. This helper collapses them.

Usage:
    python3 -m torchapp.pair_helper
    python3 -m torchapp.pair_helper "Living Room"

On success prints a human-readable confirmation and exits 0. On any
failure prints a short explanation and exits non-zero. pymobiledevice3
signals pairing success by RAISING `RemotePairingCompletedError` (as a
flow-control exception, not an error), and we translate that back into
exit 0 here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import sys

from pymobiledevice3.bonjour import browse_remotepairing_manual_pairing
from pymobiledevice3.remote.tunnel_service import (
    RemotePairingManualPairingService,
)

log = logging.getLogger(__name__)


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


async def pair(device_name: str | None = None) -> int:
    answers = await browse_remotepairing_manual_pairing()
    if not answers:
        print(
            "No devices are currently advertising a pairing prompt on "
            "the network."
        )
        print()
        print("Make sure your Apple TV is on the pairing screen:")
        print(
            "  Settings → General → Remotes and Devices → "
            "Remote App and Devices"
        )
        return 2

    if device_name:
        answers = [
            a for a in answers if a.properties.get("name") == device_name
        ]
        if not answers:
            print(
                f"No device named {device_name!r} in pairing mode right now."
            )
            return 2

    # Collapse duplicates: mDNS can see the same device on multiple
    # interfaces and produce several answers with the same identifier.
    # Keep only the first answer per identifier.
    seen: set[str] = set()
    unique = []
    for answer in answers:
        ident = answer.properties.get("identifier")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        unique.append(answer)

    if len(unique) == 0:
        print("No pairing-mode devices with valid identifiers.")
        return 2

    if len(unique) > 1:
        print("Multiple devices in pairing mode:")
        for i, a in enumerate(unique, 1):
            name = a.properties.get("name", "<unnamed>")
            print(f"  {i}. {name}")
        print()
        print(
            "Pairing with the first one. Re-run with a name argument "
            "to target a specific device:"
        )
        print(f"  python3 -m torchapp.pair_helper 'Exact Device Name'")
        print()

    picked = unique[0]
    address = _pick_best_address(picked.addresses)
    if address is None:
        name = picked.properties.get("name", "<unnamed>")
        print(f"Device {name!r} has no usable IP address — aborting.")
        return 2

    name = picked.properties.get("name", "<unnamed>")
    identifier = picked.properties["identifier"]
    print(f"Pairing with {name} at {address}")
    print()

    async with RemotePairingManualPairingService(
        identifier, address, picked.port
    ) as service:
        await service.connect(autopair=True)

    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    device_name = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        return asyncio.run(pair(device_name))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        # pymobiledevice3 signals pairing success by raising
        # RemotePairingCompletedError as a flow-control exception.
        # Translate that back into exit 0 here.
        if type(exc).__name__ == "RemotePairingCompletedError":
            print()
            print("Pairing completed successfully.")
            return 0
        print(f"Pairing failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
