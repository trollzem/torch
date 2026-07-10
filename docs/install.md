# Install

> **Maintainer note:** update this doc when (a) `installer.py`
> adds a new error class or changes the dispatch logic, (b) the
> DVT pre-kill logic in `pymd3.terminate_bundle_if_running`
> changes, (c) the reachability probes (`tcp_probe`,
> `_libimobile_*_available`) change, or (d) any pymd3 / ideviceinstaller
> error pattern we parse changes (the free-tier 3-app cap regex
> in particular).

## What this is

The install subsystem takes a signed IPA from
`~/Library/Application Support/Torch/signed/` and puts it on a
specific device. Implemented in `src/torchapp/installer.py` as a
device-class-aware dispatcher with three specific exception
classes that drive failure classification.

## The split: tvOS vs iOS/iPadOS

```
Device class        Install path
-----------------   --------------------------------------
tvOS                pymd3 apps install --rsd <tunnel>
iOS, iPadOS         ideviceinstaller -u <UDID> [-n] install
```

Why split: pymobiledevice3's `apps install --rsd` hangs
indefinitely mid-transfer when targeting iOS / iPadOS devices over
the `usbmux-<UDID>-Network` tunnel type. Symptoms: subprocess
sits at 0% CPU, no timeout fires, no error logged, blocked on an
async socket read. Small control commands over the same tunnel
work fine (`dvt kill`, `lockdown info`, `process-id-for-bundle-id`),
so the tunnel is alive — it's specifically the bulk AFC transfer
of `apps install` that gets stuck.

tvOS doesn't have this problem because its tunnels are pure
Wi-Fi RemotePairing (interface = device IP) rather than
usbmuxd-bridged. Classic libimobiledevice can't be used for tvOS
because Apple removed classic pairing in tvOS 26+.

Confirmed end-to-end on 2026-04-19. Full discovery context is in
CLAUDE.md discovery 6. The pragmatic conclusion: use whichever
tool works.

```
+-----------------+    tvOS?   yes  +--------------------+
| install_for_    |--------------->| pymd3 apps install |
| device(device,  |                | --rsd HOST PORT    |
| ipa_path)       |                +--------------------+
|                 |
|                 |    iOS or iPadOS?  yes
|                 |---------------+
+-----------------+               v
                           +--------------------+
                           | ideviceinstaller   |
                           | -u UDID install    |
                           | (USB if available, |
                           |  -n network else)  |
                           +--------------------+
```

The dispatch entry point is
`installer.install_for_device(device, ipa_path,
signed_bundle_id=...)`. Internally it routes to `_install_tvos`
or `_install_ios`. Both raise one of three specific exception
classes; nothing else escapes (other than `TunneldDownError`
which the caller propagates).

## Reachability probes

Each install path does a fast reachability check **before** the
expensive transfer. If the device isn't reachable, we raise
`DeviceOfflineError` immediately rather than waiting for a 60s+
async timeout deep inside pymd3 or ideviceinstaller.

### TCP probe (tvOS)

```python
def tcp_probe(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False
```

A successful TCP handshake means the tunnel listener is alive. If
it fails (Apple TV unplugged, network blip, stale tunnel entry
left over after the device moved off-network), the install is
skipped this cycle with a soft `DeviceOfflineError`.

### libimobiledevice probe (iOS / iPadOS)

```python
_libimobile_usb_available(udid)       # idevice_id -l (USB)
_libimobile_network_available(udid)   # idevice_id -n (Bonjour)
```

USB is preferred when available (faster, more reliable for large
transfers). If neither sees the device, soft fail.

## DVT pre-kill

Before any install (tvOS or iOS), we kill the target bundle if
it's currently running on the device. Without this, installd
hangs forever waiting for the frontmost app to exit. Detail:

- `pymd3.terminate_bundle_if_running(addr, port, bundle_id)`
- Calls `pymobiledevice3 developer dvt process-id-for-bundle-id` to
  find the PID, then `dvt kill <pid>` if found
- Best-effort: swallows `DvtError` (e.g. device without Developer
  Mode enabled — no DVT services available)
- Used on every install path, including the iOS classic-libimobile
  path. pymd3 DVT works over the same usbmux tunnel even though
  pymd3's `apps install` doesn't — the broken bit is specifically
  the bulk transfer service, not the control plane.

For iOS the DVT call is wrapped in a try/except that catches
anything (`except Exception`) and logs at debug. If the tunnel
isn't reachable for DVT, we proceed to the install anyway and
let ideviceinstaller handle whatever fallout there is.

CLAUDE.md discovery 4 has the historical context for the pre-kill
discovery (originally hit on tvOS 26.4 with YouTube running).

## Three failure classes

`installer.py` defines three subclasses of `InstallerError`. The
caller (`refresh.refresh_one`) dispatches on these to decide
whether the failure counts as a strike toward the freeze cap.

### `DeviceOfflineError` -- soft

Raised when:

- TCP probe to the tunnel address fails (tvOS)
- `idevice_id -l` and `idevice_id -n` both fail to see the UDID
  (iOS / iPadOS)
- `ideviceinstaller` install transfer stalled past the 240s
  timeout (iOS / iPadOS; treated as "iPhone Wi-Fi dozed
  mid-transfer," not as a real install failure)
- `ideviceinstaller` output matches a transient reachability error
  rather than a real install rejection:
  - an AFC transport-level error (`AFC Write error`, `AFC Read
    error`, or a partial `wrote only N of M` write) — the bulk
    transfer dropped mid-stream. Confirmed 2026-07-09: `AFC Write
    error: 30` / `wrote only 0 of 1048576` froze `YouTube-iOS.ipa`
    for a week even though the very next manual retry succeeded
    instantly.
  - `No device found with udid ...` — the pre-flight `idevice_id
    -n` Bonjour probe saw the device, but it had already dropped
    off Wi-Fi by the time `ideviceinstaller` connected. Confirmed
    2026-07-09, same session/IPA, the iPad target.

  `_TRANSIENT_IOS_ERR_RE` in `installer.py` recognizes both and
  raises `DeviceOfflineError` instead of `InstallFailedError`.

Result in `refresh.py`: `_record_soft_failure(ipa, "device-offline", ...)`
or `"partial"` if some siblings succeeded. **Does not bump
`consecutive_failures`.** The next hourly tick retries.

### `DeviceAtCapError` -- soft, user-action-required

Raised when Apple rejects with
`ApplicationVerificationFailed 0xe8008021: This device has reached
the maximum number of installed apps using a free developer
profile`. Apple's error message includes the list of bundle IDs
currently occupying the 3 slots — including any apps installed
by *other* tools (Sideloadly, AltStore, Xcode, TestFlight).
`_extract_capped_bundles` parses the bundle ID list out of the
plumesign / ideviceinstaller stderr and the exception carries it
as `external_bundle_ids`.

This matters because `refresh.count_active_apps_on_device` only
tracks IPAs Torch itself manages — it can't see what other
sideloaders installed. Without this parsing the user would see
"install-failed" with no actionable error and Torch would
otherwise think the device has 0 of 3 slots used.

Result in `refresh.py`: `_record_soft_failure(ipa, "device-at-cap", ...)`
with a message naming the external bundle IDs. **Does not bump
strikes** — hammering Apple won't free the slot, the user has to.

### `InstallFailedError` -- hard

Raised for everything else: unrecognized error patterns,
non-zero exit with empty stderr, `ideviceinstaller not installed`,
etc. — excluding the transient AFC transport errors carved out
above. Result in `refresh.py`: `_record_failure(ipa,
"install-failed", ...)`. **Bumps `consecutive_failures` by 1.**
After 3 such failures, `is_frozen()` returns True and the IPA is
skipped on subsequent ticks until something resets the counter
(re-login or a manual clear).

## ideviceinstaller subprocess interaction

```python
subprocess.run([
    "ideviceinstaller",
    "-u", device.udid,
    *(["-n"] if not usb else []),
    "install", str(ipa_path),
], capture_output=True, text=True,
   encoding="utf-8", errors="replace",
   timeout=_IDEVICEINSTALLER_TIMEOUT)
```

Notes:

- `encoding="utf-8"` is load-bearing — under launchd the
  bundle's subprocess inherits an empty `LANG`/`LC_ALL` and would
  default to ASCII, crashing on any non-ASCII byte in stderr
  (device names like "Hazem Eissa's iPad" contain U+2019). See
  CLAUDE.md "py2app gotchas" -> point about `PYTHONHOME`.
- `_IDEVICEINSTALLER_TIMEOUT = 240.0` (4 minutes). A 117 MB
  YouTube IPA over Wi-Fi finishes in ~70s; tvOS over Wi-Fi about
  30s. Real install errors fail much faster than the timeout
  itself, so hitting 240s almost always means the transfer
  stalled (iPhone slept).
- Success is detected by parsing `"Install: Complete"` or
  `"InstallComplete"` in the combined output; non-zero exit
  without that marker raises `InstallFailedError`.

## tvOS-specific tunnel handling

`_install_tvos` uses `pymd3.tunnel_for_pair_id(device.pair_record_identifier)`
to fetch the current `(addr, port)` from tunneld, then
`tcp_probe`s before calling `pymd3.install_ipa --rsd`. The
ordering matters:

```
tunneld_info()          ~50ms
tcp_probe(addr, port)   2s timeout
pymd3.apps install      can take 30-60s on a working tunnel
```

If `tcp_probe` returns False we save ~58s of waiting on pymd3's
async connect timeout, classify as soft, and move on.

## Key files

- `src/torchapp/installer.py` -- the whole subsystem
- `src/torchapp/pymd3.py` -- `install_ipa`, `terminate_bundle_if_running`,
  `tunnel_for_pair_id`, `tunneld_info`
- `src/torchapp/refresh.py` `refresh_one` -- dispatches on the
  three error classes after each device install

## Common failure modes

| Symptom | Class | Notes |
|---|---|---|
| tvOS install hangs ~60s then "Connect call failed" | offline (soft) | tcp_probe should have caught this in 2s — verify tunneld is up |
| iOS install hits 240s timeout | offline (soft) | iPhone slept mid-transfer; wake the device or wait for next tick |
| `ApplicationVerificationFailed 0xe8008021` | at-cap (soft) | user has 3 apps from this team on the device; surface external bundles |
| `ideviceinstaller not installed` | hard | `brew install ideviceinstaller`; also in bootstrap.sh |
| Empty stderr, exit=1, fast | hard | unusual; check `ideviceinstaller -d` (debug mode) by hand |

## Related docs

- [signing.md](signing.md) -- produces the signed IPA the install step consumes
- [refresh.md](refresh.md) -- the dispatcher that calls install_for_device and classifies outcomes
- [devices.md](devices.md) -- where devices come from + tunneld details
- [launchd.md](launchd.md) -- tunneld + watchdog (the install path depends on tunneld for tvOS and DVT pre-kill)
- CLAUDE.md discoveries 4, 6, 7 -- DVT pre-kill, pymd3 iOS hang, free-tier external apps
