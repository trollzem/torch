# Devices

> **Maintainer note:** update this doc when (a) the tvOS pairing
> flow in `pair_helper.py` / `pairing.py` changes, (b) the iOS
> auto-detect worker in `ui.py:_auto_detect_ios_worker` changes,
> (c) the tunneld HTTP API contract changes (it's an external
> library — only changes when we pin a new pymobiledevice3),
> (d) the device class taxonomy ("tvOS"/"iOS"/"iPadOS"/"unknown")
> grows, or (e) reconciliation logic in `pymd3.reconcile_device`
> changes.

## What this is

The device subsystem covers everything between "a physical Apple
device on the user's network" and "an entry in `config.devices`
that the refresh loop can target." Three flows feed it:

1. **tvOS pairing** (user-driven, PIN-based) — runs once per Apple TV.
2. **iOS / iPadOS auto-detect** (background, 5s polling tick) —
   picks up devices the moment they're USB-trusted via a Bonjour
   path.
3. **Periodic reconciliation** — every refresh cycle re-asks tunneld
   for the device's current name / product type / OS version.

All three feed the same `Device` dataclass in `config.py`.

## Device dataclass

```
@dataclass
class Device:
    name: str                              # "Habibi TV" / "Hazem Eissa's iPad"
    pair_record_identifier: str            # the stable primary key
    udid: str | None                       # Apple-assigned device UDID
    device_class: str                      # "tvOS" | "iOS" | "iPadOS" | "unknown"
    paired_at: str                         # ISO8601 UTC
    pair_record_path: str | None           # path to remote_<uuid>.plist
    product_type: str | None               # "AppleTV14,1"
    product_version: str | None            # "26.4"
```

`pair_record_identifier` is the load-bearing key for tunneld
lookups. For tvOS devices it's a UUID generated during pairing
(e.g. `282C5ECA-...`). For iOS/iPadOS devices it's the device's
UDID itself (e.g. `00008150-001E1D8C0AF8401C`), because classic
usbmux pairing uses the UDID as both the pair-record filename in
`/var/db/lockdown/` and tunneld's lookup key.

`udid` is filled in at reconcile time from tunneld + lockdown
info, not at pairing time. Apple TVs surface their UDID via
lockdown; the value is what plumesign uses for `register_device`
and Apple uses to scope provisioning profiles.

## tunneld: the device discovery and routing service

`pymobiledevice3 remote tunneld --wifi` runs as a root
LaunchDaemon (`com.torch.tunneld`) and exposes
`http://127.0.0.1:49151/` returning JSON of the form:

```json
{
  "<pair_record_identifier>": [
    {
      "tunnel-address": "fd...::1",
      "tunnel-port": 63182,
      "interface": "usbmux-<UDID>-Network"
    }
  ]
}
```

Three interface naming patterns matter:

| Interface | What it means |
|---|---|
| `192.168.x.y` | Pure Wi-Fi RemotePairing tunnel (Apple TVs, paired via PIN) |
| `usbmux-<UDID>-USB` | Active USB cable connection (iOS devices currently plugged in) |
| `usbmux-<UDID>-Network` | Wi-Fi tunnel brokered via classic usbmuxd from old `/var/db/lockdown/` pair record (iOS devices on Wi-Fi, USB-trusted at some point) |

The `usbmux-...-Network` shape is specific to iOS / iPadOS. Apple
TVs always come through pure RemotePairing; iOS devices come
through the usbmuxd-bridged path because they have classic pair
records on the Mac.

The wrapper for this API lives in `pymd3.py`:

- `pymd3.tunneld_info(timeout=3.0)` — GET / and parse JSON
- `pymd3.tunnel_for_pair_id(pair_id)` — look up one device's
  tunnel; sorts non-usbmux interfaces first when both are
  available
- `pymd3.is_tunneld_up()` — non-raising health check
- `pymd3.all_tunneled_pair_ids()` — list of every pair_id tunneld
  knows about

Tunneld is long-running. After ~7 days of uptime its internal
mDNS state can rot (the user hit this 2026-06-20 at 44 days
uptime, where tunneld was alive but reporting `{}`). The watchdog
in `tunneld_keepalive.py` preempts this — see
[launchd.md#watchdog](launchd.md).

## tvOS pairing flow

Implemented in `pair_helper.py` (run as a subprocess) and driven
by `ui.py:_start_pairing_in_ui`. Apple TVs use the new
RemotePairing protocol (libimobiledevice's classic lockdown
pairing is non-functional on tvOS 26+, per CLAUDE.md dead-ends).

Sequence:

1. User clicks "Devices -> Add Apple TV..." in the menubar.
2. Torch shows an instructional alert: "On Apple TV: Settings ->
   General -> Remotes and Devices -> Remote App and Devices."
3. `_start_pairing_in_ui` spawns `pair_helper.py` as a subprocess
   under Homebrew python3.14 (pair_helper imports `pymobiledevice3`
   which is intentionally excluded from the py2app bundle to keep
   it small — see [packaging.md](packaging.md)).
4. pair_helper emits machine-readable `STATE:` lines on stdout
   that `ui.py:_pairing_worker` consumes:
   - `STATE: searching` — looking for Apple TVs over Bonjour
   - `STATE: awaiting_pin` — found device, ready for PIN
   - `STATE: pairing_complete` — pair record written to disk
   - `STATE: error <message>` — terminal failure
5. On `awaiting_pin`, the worker marshals a `rumps.Window` PIN
   prompt to the main thread via `_run_on_main_and_wait` and
   writes the entered code back to pair_helper's stdin.
6. On `pairing_complete`, pymobiledevice3 has written
   `~/.pymobiledevice3/remote_<UUID>.plist`. A polling timer
   (`_poll_for_new_pair_record`, 3s) sees the new file and
   triggers `_post_pair_reconcile` which adds the device to
   `cfg.devices`, registers the UDID with Apple's portal, and
   auto-targets compatible tracked IPAs.

The polling timer is belt-and-suspenders: even if the worker's
own success notification path has a bug, the pair record on disk
is the source of truth. The worker stops the timer early on
terminal error/cancel to avoid a spurious "Pairing timed out"
3 minutes later; success paths leave the timer running so the
pair-record file still gets picked up.

## iOS / iPadOS auto-detect

Implemented in `ui.py:_auto_detect_ios_worker`, kicked by
`_maybe_kick_auto_detect_worker` from `_on_config_watch_tick`
every 5 seconds. No PIN, no dialog — once an iOS device is
USB-trusted by the Mac (the system "Trust This Computer?" prompt,
handled by macOS itself), tunneld picks it up via Bonjour and the
worker auto-adds it on the next tick.

Flow:

1. Config watcher tick (every 5s on the main thread) calls
   `_maybe_kick_auto_detect_worker`.
2. If no worker is already in flight (guarded by
   `self._auto_add_in_flight`), spawn one.
3. Worker calls `pymd3.tunneld_info(timeout=2.0)`. If tunneld is
   down, return silently.
4. Diff the inventory against tracked pair_ids. For each new one:
   - Build a `Device` stub with `pair_record_identifier=pid`.
   - Call `pymd3.reconcile_device(stub)` to get the friendly
     name, UDID, device class, product type/version from lockdown.
   - Skip if `device_class` isn't `iOS` or `iPadOS`. tvOS
     pairing has its own PIN flow; if a tvOS pair record shows
     up here without going through that flow, leave it for the
     explicit pairing path to handle.
   - Re-check `cfg.device_by_pair_record(pid)` to defend against
     races between the worker and the tvOS post-pair path.
   - Append the device to `cfg.devices` in place.
   - For each tracked IPA, if `is_compatible(ipa.platform,
     device.device_class)`, append `pid` to `ipa.target_devices`.
   - `cfg.save()` (atomic temp+rename) before attempting the
     slow Apple portal call. If `register_device` hangs or fails,
     the device is already persisted and the next refresh will
     retry registration.
5. Notify the user via `_notify_async`. Rebuild the menu via
   `_rebuild_async`. Reset the in-flight flag.

The menubar item "Check for new iPhones/iPads now" just kicks
this same worker immediately rather than waiting for the next 5s
tick.

## reconcile_device

Called from the tvOS pairing path, the iOS auto-detect path, and
the periodic reconciliation in `refresh.reconcile_devices`. Reads
`DeviceClass`, `UniqueDeviceID`, `ProductType`, `ProductVersion`,
and `DeviceName` from `lockdown info` over the tunnel.

`reconcile_device` raises `TunnelNotFoundError` if the device
isn't currently in tunneld (offline / asleep). `reconcile_all`
swallows that and returns the device unchanged, so a device that
went offline keeps its last-known info instead of getting wiped.

`refresh.reconcile_devices` updates `cfg.devices` **in place**
(not via list reassignment) so the iOS auto-detect worker can
concurrently append a new device without the refresh loop's
reconcile blowing it away. The hourly cycle thus tolerates
mid-flight discovery from the watcher tick.

## Platform compatibility predicate

`config.platform_matches_device(ipa_platform, device_class)`
(re-exported as `refresh.is_compatible`):

```
tvOS IPA   matches  tvOS device only
iOS IPA    matches  iOS or iPadOS devices
iPadOS IPA matches  iOS or iPadOS devices
```

iOS and iPadOS are interchangeable because their provisioning
profile shapes are identical (both use `/QH65B2/ios/` with no
`subPlatform` body param). tvOS is strict — it needs the patched
`subPlatform: "tvOS"` body which forces a separate sign.

This predicate is the single source of truth, called at:

- `config._make_ipa_entry` — when discovering a new IPA in
  `ipas/`, only platform-compatible devices get added to its
  initial `target_devices` list
- `ui.py` menu rendering — the "Install on devices" submenu only
  lists devices whose class matches the IPA's platform
- `refresh.refresh_one` — the `compatible` filter that decides
  which devices a refresh cycle attempts

Keeping all three of these in lockstep is why the predicate
lives in `config.py` (so refresh.py can import it without a
circular import).

## Key files

- `src/torchapp/pymd3.py` — tunneld HTTP client + lockdown info wrapper
- `src/torchapp/pair_helper.py` — subprocess-driven tvOS PIN pairing
- `src/torchapp/pairing.py` — supporting code for pair_helper
- `src/torchapp/ui.py` `_start_pairing_in_ui`, `_pairing_worker`,
  `_post_pair_reconcile`, `_poll_for_new_pair_record` — menubar
  pairing flow
- `src/torchapp/ui.py` `_auto_detect_ios_worker`,
  `_maybe_kick_auto_detect_worker` — iOS auto-detect
- `src/torchapp/config.py` `Device`, `platform_matches_device`,
  `device_by_pair_record` — dataclass + lookups

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| tunneld returns `{}` despite devices on network | mDNS state rot at long uptime | watchdog auto-restarts every 7d; manual: `sudo launchctl kickstart -k system/com.torch.tunneld` |
| iOS device shown but with `device_class=unknown` | reconcile failed at first attempt | next refresh cycle re-reconciles; check tunneld is reachable for that pair_id |
| tvOS pairing stuck at "Searching..." | Apple TV not on the "Remote App and Devices" screen, or on different Wi-Fi | bring it to that screen on the TV; verify same SSID |
| iPad never auto-adds | USB trust prompt never accepted | plug it in via USB once and tap Trust on the device |
| iPhone disappears from tunneld every few minutes | iOS Wi-Fi power-save | expected; soft-failure pipeline tolerates it |

## Related docs

- [install.md](install.md) -- how a reconciled device gets an IPA installed
- [refresh.md](refresh.md) -- periodic reconciliation in the refresh loop
- [config.md](config.md) -- the on-disk Device record + iCloud backup
- [launchd.md](launchd.md) -- tunneld daemon + watchdog
- [ui.md](ui.md) -- the menubar threading rules the pairing/auto-detect workers obey
