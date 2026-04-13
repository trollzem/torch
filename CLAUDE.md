# ATVLoader — macOS menubar sideloader for Apple TV, iPhone, iPad

A macOS menubar app that signs and installs IPAs onto Apple TV (tvOS 26+),
iPhone (iOS 17+), and iPad (iPadOS 17+) using a free Apple ID. Set it up
once, drop IPAs into a folder, auto-refreshes every 6 days before the
7-day free-tier profile expiry.

Supersedes the broken ATVLoadly Docker setup (which uses libimobiledevice
and cannot do RemotePairing — fatal on tvOS 26+).

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  ATVLoader menubar (rumps, user-level LaunchAgent)       │
│  ┌────────────┐  ┌──────────┐  ┌────────────────────┐    │
│  │ UI / state │  │ Refresh  │  │ Pairing            │    │
│  │ rumps.App  │  │ scheduler│  │ (Terminal handoff) │    │
│  └─────┬──────┘  └─────┬────┘  └─────────┬──────────┘    │
│        │               │                 │               │
│  ┌─────▼───────────────▼─────────────────▼────────────┐  │
│  │ plumesign wrapper (patched Rust binary)            │  │
│  │ PLUME_FORCE_TVOS=1 + PLUME_DELETE_AFTER_FINISHED=1 │  │
│  │ staging-dir scrape + manual re-zip                 │  │
│  └───────────────────┬─────────────────────────────────┘ │
│                      │                                    │
│  ┌───────────────────▼─────────────────────────────────┐  │
│  │ pymobiledevice3 client                              │  │
│  │ tunneld HTTP API + DVT pre-kill + apps install      │  │
│  └───────────────────┬─────────────────────────────────┘  │
└──────────────────────┼─────────────────────────────────────┘
                       │
              ┌────────▼────────────────────────┐
              │  pymobiledevice3 tunneld         │
              │  (root LaunchDaemon TODO,        │
              │   currently manual sudo run)     │
              │  HTTP API 127.0.0.1:49151        │
              └────────┬────────────────────────┘
                       │ RemotePairing tunnels (TUN ifaces)
              ┌────────▼─────────┐
              │  Paired devices  │
              │  • Habibi TV     │
              │  • Hazem iPhone  │
              │  • (future)      │
              └──────────────────┘
```

## Load-bearing discoveries

These are things we learned the hard way during the spike and first
product build. None of them are documented upstream.

### 1. tvOS provisioning via `subPlatform` body param

`plumesign account register-device --udid <tvOS-UDID>` hits
`/QH65B2/ios/addDevice.action` which is shared across platforms — Apple
auto-detects `device_class: "tvOS"` from the UDID chip prefix
(`00008110` = `AppleTV14,1`).

BUT `plumesign sign --apple-id` normally generates an iOS + xrOS +
visionOS profile, which Apple TV rejects with `ApplicationVerificationFailed:
A valid provisioning profile for this executable was not found`.

**Fix:** we patched `plume_core::developer::qh::profile::qh_get_profile`
to send `subPlatform: "tvOS"` in the POST body when
`PLUME_FORCE_TVOS=1` is set. The URL stays at `/QH65B2/ios/…` — only
the body parameter differs. This returns a real Apple-signed profile
with `Platform: [tvOS]` and `ProvisionedDevices` containing the
registered Apple TV UDID.

Patch: [vendor/impactor-tvos.patch](vendor/impactor-tvos.patch)
Built binary: [bin/plumesign](bin/plumesign) (arm64, ~9.2 MB)

### 2. plumesign archive bug (v2.2.3)

`plumesign sign --package X.ipa -o Y.ipa` copies the **input file** to
the output path instead of re-archiving the signed staging directory.
The actual signing work happens correctly in
`/var/folders/.../plume_stage_<uuid>/` but gets thrown away.

**Fix:** set `PLUME_DELETE_AFTER_FINISHED=1` to disable the cleanup,
scrape the staging path from stderr (`"writing signed main executable
to .../plume_stage_<uuid>/Payload/..."`), and re-zip `Payload/` ourselves
with `zip -r -y -q` (the `-y` preserves symlinks which some frameworks
need). See [src/atvloader/plumesign.py](src/atvloader/plumesign.py).

### 3. DVT pre-kill for running apps

`pymobiledevice3 apps install` hangs **indefinitely** on tvOS if the
target bundle is currently running (installd waits for the frontmost
app to exit). Lockdown info still works over the same tunnel, so it's
not a tunnel issue — it's a per-service wait.

**Fix:** before every install, call
`pymobiledevice3 developer dvt process-id-for-bundle-id <bundle-id>`
to check for a running PID, then `dvt kill <pid>` if found. Best-effort
(swallows DvtError for devices without Developer Mode). Also reduced
the install subprocess timeout from 600s to 180s so genuine hangs
surface as a clear error instead of silent freezes.

See [src/atvloader/pymd3.py](src/atvloader/pymd3.py) `terminate_bundle_if_running()`
and `install_ipa()`.

### 4. rumps is Cocoa-main-thread-only

Calling `rumps.notification`, `rumps.alert`, `rumps.Window`, or
mutating `self.menu` / `self.title` from a background thread **silently
kills the process**. No traceback, no output, just gone.

**Pattern:** every worker-thread UI touch goes through `_on_main_thread()`
(which wraps `PyObjCTools.AppHelper.callAfter`) or the
`_run_on_main_and_wait()` helper for modal dialogs that need a return
value. Periodic work uses `rumps.Timer` (NSTimer, fires on main thread),
not `threading.Timer`.

See [src/atvloader/ui.py](src/atvloader/ui.py).

### 5. iOS doesn't have a "pair device" screen

Unlike tvOS, iPhones/iPads don't expose a manual pairing flow in Settings.
The RemotePairing handshake happens automatically once the device is
USB-trusted (the one-time "Trust This Computer" prompt). From then on,
usbmuxd tunnels it over both USB and WiFi, and tunneld's HTTP API
surfaces it as a regular tunnel entry.

**UX consequence:** "Add Apple TV" uses a Terminal handoff for the
manual PIN dance. "Detect iPhone/iPad" just enumerates tunneld's current
list and offers to add anything that isn't already tracked.

## Why the old approach doesn't work

Kept for future reference — these are dead ends we ruled out.

- **libimobiledevice / ATVLoadly** — uses traditional lockdown pairing,
  which tvOS 26+ removed. RemoteXPC lockdown explicitly raises
  `NotImplementedError("RemoteXPC lockdown version does not support
  pairing operations")`.
- **Self-signed certs** — Apple TV rejects with
  `ApplicationVerificationFailed: No code signature found`. The profile
  must be signed by Apple's CA.
- **Writing our own Grand Slam / AuthKit auth in Python** — no maintained
  library exists; anisette generation is a real cryptographic barrier;
  plumesign uses an external anisette server (`ani.stikstore.app`).
- **plumesign's built-in install** — usbmux-only; Apple TV 4K has no
  USB port. We use plumesign for signing only; pymobiledevice3 does the
  install over the WiFi tunnel.
- **Xcode automatic tvOS provisioning** — Xcode 26 + tvOS 26.4 does not
  wirelessly see Apple TVs via CoreDevice. `xcrun xctrace list devices`
  and `xcrun devicectl list devices` both return only USB-connected
  iPhones on this setup.
- **/tvos/ endpoint family** — Apple has `/QH65B2/tvos/…` URLs in their
  dev portal but they return empty responses for free-tier accounts
  (`UnexpectedEndOfEventStream` when plumesign tries to parse them).
  The working mechanism is the `subPlatform` body param on the /ios/
  endpoint, not a different URL.

## Project layout

```
~/Desktop/Projects/ATVLoader/
├── src/
│   └── atvloader/
│       ├── __init__.py
│       ├── __main__.py       # `python3 -m atvloader` entry point
│       ├── paths.py          # Application Support + project + external dirs
│       ├── config.py         # dataclass schema + bootstrap + sync_ipas_folder
│       ├── keychain.py       # keyring wrapper for Apple ID credentials
│       ├── plumesign.py      # subprocess wrapper (login, register, sign)
│       ├── pymd3.py          # tunneld client + reconcile + install + DVT
│       ├── pairing.py        # pexpect-driven RemotePairing (for tvOS)
│       ├── refresh.py        # sign → install orchestrator + lock
│       └── ui.py             # rumps menubar app
├── bin/
│   └── plumesign             # our patched Rust binary (tracked in git)
├── vendor/
│   └── impactor-tvos.patch   # the plumesign source patch
├── ipas/
│   ├── YouTube.ipa           # source IPAs (copied into runtime dir on boot)
│   └── Streamer.ipa
├── signed/                   # project-local signed outputs (gitignored)
├── docs/
│   ├── SESSION_SUMMARY.md    # historical spike-era doc
│   └── plans/
│       └── 2026-04-12-atvloader-product-design.md
├── requirements.txt
└── README.md
```

Runtime state (not in repo):

```
~/Library/Application Support/ATVLoader/
  ipas/                # user-added + project-mirrored IPAs
  signed/              # plumesign output (per-IPA, per-platform)
  config.json          # tracked IPAs, devices, settings
  logs/atvloader.log
```

External state owned by other tools:

```
~/.config/PlumeImpactor/      # plumesign session (accounts.json, state.plist, keys/)
~/.pymobiledevice3/           # pair records (remote_<uuid>.plist)
```

## Dependencies

Python 3.14 (Homebrew):
- `rumps` — menubar
- `pyobjc-framework-Cocoa` — NSWorkspace wake notification + AppHelper.callAfter
- `pexpect` — driving plumesign + pymobiledevice3 remote pair interactive prompts
- `keyring` — macOS Keychain
- `pymobiledevice3 ≥ 9.9.1` — device communication (subprocess, not library)

Rebuilding plumesign from source (only needed if `bin/plumesign` is
missing or we bump Impactor upstream):
- `cargo` + `rustc` via `brew install rust`
- Clone `CLARATION/Impactor` at `v2.2.3`
- `git apply vendor/impactor-tvos.patch`
- `cargo build --release -p plumesign`
- Copy `target/release/plumesign` to `bin/plumesign`

## How to run

```bash
# One-time setup (on a fresh Mac)
pip3 install --break-system-packages -r requirements.txt
# + a manual `sudo pymobiledevice3 remote tunneld --wifi` in a terminal
#   until the launchd installer (step 8) exists

# Launch the menubar
cd ~/Desktop/Projects/ATVLoader
PYTHONPATH=src python3 -m atvloader
```

Logs: `~/Library/Application Support/ATVLoader/logs/atvloader.log`
or menubar → View Log.

## Apple TV / device details

- **Apple TV:** Habibi TV · AppleTV14,1 · tvOS 26.4 · build 23L243
  UDID `00008110-000E59EC3E41801E` · WiFi MAC `48:e1:5c:69:af:2b`
  Ethernet MAC `48:e1:5c:75:c5:91` · Currently 192.168.68.82
- **iPhone:** Hazem · iPhone18,4 · iOS 26.3
  UDID `00008150-001E1D8C0AF8401C`
  Already USB-trusted; visible in tunneld as `usbmux-00008150-...-Network`

## Apple ID

- **Email:** eissahazem@gmail.com
- **Team ID:** 3G6AP3U89B (Individual, Xcode Free Provisioning Program)
- plumesign session persisted at `~/.config/PlumeImpactor/`. No 2FA
  needed on subsequent runs — the cached session handles everything.

## Known gaps / TODO

- **LaunchDaemon for tunneld** — currently started manually via
  `sudo pymobiledevice3 remote tunneld --wifi` in a terminal. Needs to
  be installed as `/Library/LaunchDaemons/com.atvloader.tunneld.plist`
  running as root so it persists across reboots and doesn't depend on
  an open terminal. An install script in `src/install.py` is planned
  but not yet written.
- **LaunchAgent for the menubar app** — same story; currently launched
  manually via `python3 -m atvloader` for dev. Will become
  `~/Library/LaunchAgents/com.atvloader.app.plist` with `KeepAlive=true`.
- **First-run wizard** — designed in
  [docs/plans/2026-04-12-atvloader-product-design.md](docs/plans/2026-04-12-atvloader-product-design.md)
  but not yet implemented. For now the app seeds entirely from
  existing state on disk (plumesign session + pair records + project
  IPAs), which works because the spike left that state in place.
- **Xcode automatic signing fallback** — not implemented, probably
  not needed given the plumesign flow works.
- **py2app packaging** — out of scope for v1. Current invocation is
  `python3 -m atvloader`, which makes the dock show "Python" instead
  of "ATVLoader" — cosmetic only.

## Linux VM notes (historical)

The original spike work happened on a Linux VM at 192.168.68.75 (UTM,
Debian, user `hazem`, password `gameboy`). That VM is no longer the
primary execution environment — everything runs natively on the Mac
now. The VM still has:
- `pymobiledevice3-tunneld.service` (systemd)
- `atvloadly-mdns-bridge.service`
- ATVLoadly web UI at http://192.168.68.75/ (broken post tvOS 26)

Safe to leave running or shut down as desired.
