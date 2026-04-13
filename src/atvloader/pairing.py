"""Device pairing via `pymobiledevice3 remote pair` driven by pexpect.

The pairing flow is:
  1. Device is put into the "Remote App and Devices" screen on tvOS or
     "Developer Mode > Pair Device" screen on iOS 17+. This makes the
     device advertise _remotepairing-manual-pairing._tcp on bonjour.
  2. We spawn `pymobiledevice3 remote pair --name <name>`, which
     performs the SRP handshake and triggers a 6-digit PIN to display
     on the target device.
  3. pexpect captures the "Enter PIN:" prompt, the caller supplies the
     PIN via the callback, we pipe it to stdin.
  4. On success, pymobiledevice3 raises RemotePairingCompletedError
     (which is flow-control, not an error) and exits non-zero with a
     traceback. We treat that specific exception as success.
  5. The pair record is written to ~/.pymobiledevice3/remote_<UUID>.plist.
     We scan the directory for any file that wasn't there before the
     pair to identify the new record's identifier.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import pexpect

from . import paths

log = logging.getLogger(__name__)

PinCallback = Callable[[], str]


class PairingError(Exception):
    """Base class for pairing flow failures."""


class PairingCancelledError(PairingError):
    """User clicked Cancel on the PIN dialog."""


class PairingTimeoutError(PairingError):
    """Pairing handshake exceeded the deadline."""


def _existing_pair_record_ids() -> set[str]:
    """Return the set of pair_record_identifiers currently on disk."""
    if not paths.PYMD3_PAIR_RECORDS_DIR.exists():
        return set()
    return {
        p.stem.removeprefix("remote_")
        for p in paths.PYMD3_PAIR_RECORDS_DIR.glob("remote_*.plist")
    }


def pair_device(
    device_name: str,
    pin_callback: PinCallback,
    *,
    timeout: float = 120.0,
) -> str:
    """Run the RemotePairing handshake for a device.

    Returns the pair_record_identifier of the newly-created pair record.

    Raises:
      PairingCancelledError - if pin_callback raises it
      PairingTimeoutError   - if the handshake hangs past `timeout`
      PairingError          - for any other failure
    """
    before = _existing_pair_record_ids()
    log.info("starting pairing handshake for %s", device_name)

    child = pexpect.spawn(
        "pymobiledevice3",
        ["remote", "pair", "--name", device_name],
        encoding="utf-8",
        timeout=timeout,
    )

    try:
        # pymobiledevice3 prints the PIN prompt to stdout.
        # We match leniently to survive wording changes.
        idx = child.expect(
            [
                r"Enter PIN",
                r"RemotePairingCompletedError",  # in case it auto-completes without asking
                pexpect.EOF,
                pexpect.TIMEOUT,
            ]
        )
        if idx == 0:
            pin = pin_callback().strip()
            if not pin:
                raise PairingError("empty PIN supplied")
            log.info("sending PIN to pymobiledevice3")
            child.sendline(pin)
            # Wait for the RemotePairingCompletedError (= success) or EOF.
            child.expect(
                [
                    r"RemotePairingCompletedError",
                    pexpect.EOF,
                    pexpect.TIMEOUT,
                ]
            )
        elif idx == 1:
            # Already completed before the PIN prompt fired (unusual).
            pass
        elif idx == 2:
            raise PairingError(
                f"pymobiledevice3 exited before asking for PIN: "
                f"{(child.before or '')[-500:]}"
            )
        else:
            raise PairingTimeoutError(
                f"timed out waiting for PIN prompt from {device_name}"
            )
    except PairingCancelledError:
        raise
    finally:
        try:
            child.close()
        except Exception:  # noqa: BLE001
            pass

    # Find the new pair record. Give tunneld / pymobiledevice3 a moment
    # to finish writing the file before we scan.
    time.sleep(0.5)
    after = _existing_pair_record_ids()
    new_ids = after - before
    if not new_ids:
        raise PairingError(
            "pairing handshake completed but no new pair record appeared "
            "on disk"
        )
    if len(new_ids) > 1:
        log.warning(
            "multiple new pair records appeared after pairing; picking one: %s",
            new_ids,
        )
    pair_id = next(iter(new_ids))
    log.info("pairing complete, new pair record: %s", pair_id)
    return pair_id
