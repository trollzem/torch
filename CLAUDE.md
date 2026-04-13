# Torch — engineering notes

This file is for Claude Code (and anyone else reading the source cold).
It documents the load-bearing technical discoveries and the rationale
behind architectural choices. For user-facing install and usage
instructions, see [README.md](README.md).

## Goal

A macOS menubar app that signs and installs IPAs onto Apple TV
(tvOS 26+), iPhone (iOS 17+), and iPad (iPadOS 17+) using a free
Apple ID, and auto-refreshes every 6 days before the 7-day free-tier
provisioning profile expires.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Torch menubar (rumps, user-level LaunchAgent)           │
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
              │  (root LaunchDaemon installed    │
              │   by src/install.py)             │
              │  HTTP API 127.0.0.1:49151        │
              └────────┬────────────────────────┘
                       │ RemotePairing tunnels (TUN ifaces)
              ┌────────▼─────────┐
              │  Paired devices  │
              │  (tvOS + iOS +   │
              │   iPadOS)        │
              └──────────────────┘
```

Two long-running processes installed as launchd services:
- **`com.torch.tunneld`** — system LaunchDaemon, runs as root,
  wraps `pymobiledevice3 remote tunneld --wifi`. Starts at boot,
  exposes `http://127.0.0.1:49151/` for tunnel discovery.
- **`com.torch.app`** — user LaunchAgent, runs as the logged-in
  user, wraps `python3 -m torchapp`. Starts at login, KeepAlive=true.

## Load-bearing discoveries

The product depends on five non-obvious technical findings. If any
future engineer touches the sign / install / anisette paths, this
section is required reading.

### 1. tvOS provisioning via `subPlatform` body param

`plumesign account register-device --udid <tvOS-UDID>` hits
`/QH65B2/ios/addDevice.action` which is shared across platforms —
Apple auto-detects `device_class: "tvOS"` from the UDID chip prefix
(`0x8110` = `AppleTV14,1`, etc.).

BUT `plumesign sign --apple-id` on its default code path generates
an iOS + xrOS + visionOS profile, which Apple TV rejects with
`ApplicationVerificationFailed: A valid provisioning profile for
this executable was not found`.

**Fix:** our fork patches `plume_core::developer::qh::profile::qh_get_profile`
to send `subPlatform: "tvOS"` in the POST body when
`PLUME_FORCE_TVOS=1` is set. The URL stays at `/QH65B2/ios/…` —
only the body parameter differs. Apple then returns a real signed
profile with `Platform: [tvOS]` and the registered Apple TV UDID
in `ProvisionedDevices`.

This is not documented anywhere upstream. We found it by experiment.

Patch: [vendor/impactor-tvos.patch](vendor/impactor-tvos.patch)
Built binary: [bin/plumesign](bin/plumesign)

### 2. Native macOS anisette via AOSKit

plumesign's underlying `omnisette` crate has an `aos-kit` Cargo
feature that dlopen's Apple's private `AOSKit.framework` /
`AuthKit.framework` to generate anisette headers natively. Upstream
plume_core enables only the `remote-anisette-v3` feature, which
means every auth call proxies through `https://ani.stikstore.app` —
a third-party relay with a history of breaking when Apple rotates
GSA.

**Fix:** our patch enables both `remote-anisette-v3` AND `aos-kit`
in `crates/plume_core/Cargo.toml`. `omnisette::AnisetteHeaders::get_anisette_headers_provider`
tries aos_kit first on macOS, falls back to the remote provider
only if `AOSKitAnisetteProvider::new()` fails (which would mean
Apple removed or renamed the private framework). Result: zero
external anisette dependencies day-to-day, same safety net on
the edge.

See commits referencing `aos-kit` in the patch file.

### 3. plumesign v2.2.3 archive bug workaround

`plumesign sign --package X.ipa -o Y.ipa` copies the **input file**
to the output path instead of re-archiving the signed staging
directory. The actual signing work happens correctly in
`/var/folders/.../plume_stage_<uuid>/Payload/…` but gets thrown
away at end of run.

**Fix:** set `PLUME_DELETE_AFTER_FINISHED=1` to disable the
cleanup; scrape the staging path from stderr (`"writing signed
main executable to .../plume_stage_<uuid>/Payload/..."`); re-zip
`Payload/` ourselves with `zip -r -y -q` (the `-y` preserves
symlinks, which some frameworks need). See
[src/torchapp/plumesign.py](src/torchapp/plumesign.py).

### 4. DVT pre-kill for running apps

`pymobiledevice3 apps install` hangs **indefinitely** on tvOS (and
probably iOS) if the target bundle is currently running on the
device. installd waits for the frontmost app to exit with no
timeout. Other services through the same tunnel still work fine —
it's a per-service wait, not a tunnel issue.

**Fix:** before every install, call `pymobiledevice3 developer
dvt process-id-for-bundle-id <bundle-id>` to check for a running
PID, then `dvt kill <pid>` if found. Best-effort — swallows
`DvtError` for devices without Developer Mode — and the install
subprocess has a 180s timeout so genuine hangs surface as clear
errors instead of silent freezes.

See [src/torchapp/pymd3.py](src/torchapp/pymd3.py)
`terminate_bundle_if_running()` and `install_ipa()`.

### 5. rumps is Cocoa-main-thread-only

Calling `rumps.notification`, `rumps.alert`, `rumps.Window`, or
mutating `self.menu` / `self.title` / `self.icon` from a background
thread **silently kills the process**. No traceback, no output —
Cocoa just aborts.

**Pattern:** every worker-thread UI touch goes through
`_on_main_thread()` (which wraps `PyObjCTools.AppHelper.callAfter`)
or the `_run_on_main_and_wait()` helper for modal dialogs that need
a return value. Periodic work uses `rumps.Timer` (NSTimer, fires
on the main thread), not `threading.Timer`.

See [src/torchapp/ui.py](src/torchapp/ui.py) for `_on_main_thread`,
`_notify_async`, `_set_icon_async`, `_rebuild_async`.

## Dead ends (for future reference)

These approaches were explored and ruled out. Don't re-implement
them without reading this section.

- **libimobiledevice / classic lockdown pairing** — tvOS 26+
  removed the traditional pairing protocol. RemoteXPC lockdown
  explicitly raises `NotImplementedError("RemoteXPC lockdown
  version does not support pairing operations")`. libimobiledevice-
  based tools (ATVLoadly etc.) cannot talk to modern Apple TVs.
- **Self-signed certs** — Apple TV rejects with
  `ApplicationVerificationFailed: No code signature found`. The
  profile must be signed by Apple's CA.
- **Writing our own Grand Slam / AuthKit auth in Python** — no
  maintained library exists for the full GSA + anisette flow.
  Use plumesign's Rust-side implementation via subprocess.
- **plumesign's built-in install** — USB / usbmuxd only. Apple
  TV 4K has no USB port. We use plumesign for signing only;
  pymobiledevice3 does the WiFi install through the tunneld HTTP
  API.
- **Xcode automatic tvOS provisioning** — as of April 2026, Xcode
  + CoreDevice does not see Apple TVs wirelessly. `xcrun xctrace
  list devices` and `xcrun devicectl list devices` only surface
  USB-connected iPhones.
- **`/QH65B2/tvos/…` URL family** — Apple has parallel tvOS URLs
  in the developer portal but they return empty responses for
  free-tier accounts (`UnexpectedEndOfEventStream` when plumesign
  tries to parse them). The working mechanism is the `subPlatform`
  body parameter on the `/ios/` endpoint, not a different URL.
- **JIT attach for tvOS apps** — on A15+ / M2+ chips with TXM,
  the CS_DEBUGGED flag flip no longer yields RWX memory even
  though `debugserver vAttach` still works. No public TXM bypass
  exists as of April 2026. The DVT plumbing is already in place
  via our `pymd3.py` — if a bypass ships, JIT attach becomes a
  ~60-line addition, but until then it's dead code against zero
  targets.

## Project layout

```
torch/
├── bin/
│   └── plumesign             # patched Rust binary, tracked in git
├── bootstrap.sh              # one-command installer for fresh Macs
├── uninstall.sh              # one-command uninstaller
├── CLAUDE.md                 # this file — engineering notes
├── README.md                 # user-facing docs
├── requirements.txt
├── src/
│   ├── install.py            # launchd service installer (driven by bootstrap.sh)
│   ├── uninstall.py          # launchd service uninstaller
│   └── torchapp/
│       ├── __init__.py
│       ├── __main__.py       # `python3 -m torchapp` entry point
│       ├── paths.py          # Application Support + project + external dirs
│       ├── config.py         # dataclass schema + bootstrap + sync_ipas_folder
│       ├── keychain.py       # keyring wrapper for Apple ID credentials
│       ├── icons.py          # SF Symbol menubar icon renderer
│       ├── plumesign.py      # subprocess wrapper (login, register, sign)
│       ├── pymd3.py          # tunneld client + reconcile + install + DVT
│       ├── pairing.py        # pexpect-driven RemotePairing (tvOS)
│       ├── refresh.py        # sign → install orchestrator + lock + cert check
│       ├── launchd.py        # plist generators + install/uninstall helpers
│       └── ui.py             # rumps menubar app
└── vendor/
    └── impactor-tvos.patch   # source patch against CLARATION/Impactor v2.2.3
```

Runtime state, created on first launch (not in repo):

```
~/Library/Application Support/Torch/
  ipas/                # user-added IPAs (source)
  signed/              # plumesign output (per-IPA, per-platform)
  icons/               # rendered SF Symbol PNGs for the menubar
  config.json          # tracked IPAs, devices, cert status, settings
  logs/torch.log
```

External state owned by other tools (created by `plumesign account
login` and `pymobiledevice3 remote pair`, respectively):

```
~/.config/PlumeImpactor/      # plumesign session (accounts.json, state.plist, keys/)
~/.pymobiledevice3/           # pair records (remote_<uuid>.plist)
```

Pair records are mirrored to
`~/Library/Mobile Documents/com~apple~CloudDocs/Torch/backup/pair-records/`
on every app startup when iCloud Drive is available, so a Mac
reinstall doesn't force a re-pair of every device.

## Dependencies

Python 3.14 or newer (Homebrew `python@3.14`):

- `rumps` — menubar
- `pyobjc-framework-Cocoa` — NSWorkspace wake notification,
  AppHelper.callAfter, SF Symbol rendering
- `pexpect` — driving interactive prompts from plumesign account
  login and pymobiledevice3 remote pair
- `keyring` — macOS Keychain access
- `pymobiledevice3 ≥ 9.9.1` — device communication (we shell out
  to the CLI, we don't import the library)

Rebuilding `bin/plumesign` from source (only needed if the binary
is missing or we bump Impactor upstream):

- `cargo` + `rustc` via `brew install rust`
- Clone `CLARATION/Impactor` at tag `v2.2.3`
- `git apply <path-to>/vendor/impactor-tvos.patch`
- `cargo build --release -p plumesign`
- Copy `target/release/plumesign` to `bin/plumesign`

## Free Apple ID limits to budget against

- **10 new App IDs per 7 days** — team-level, enforced by the
  developer portal on `/addAppId.action`. Refreshing an existing
  bundle ID does NOT consume a slot; only new distinct bundle IDs
  do. Extensions count as separate app IDs against this cap.
- **3 apps per device** — enforced at install/launch time by the
  device. Installing a 4th signed bundle on the same device
  silently invalidates the oldest. We track this with
  `refresh.FREE_TIER_DEVICE_APP_CAP` and refuse the 4th install
  with a clear error.
- **Provisioning profile lifetime: 7 days.** We refresh on a
  6-day cadence (1-day buffer) via `HOURLY_TICK_SECONDS` + wake
  observer.
- **Developer certificate lifetime: 364 days.** We check via
  `refresh.refresh_cert_status()` on every refresh cycle and
  surface the countdown + expire/revoke state in the menubar.

## How to run (developer / debugging)

```bash
# One-time setup (matches what bootstrap.sh does)
pip3 install --break-system-packages -r requirements.txt

# Launch the menubar in-place (bypasses launchd)
PYTHONPATH=src python3 -m torchapp

# Install as a pair of launchd services
python3 src/install.py

# Uninstall launchd services (leaves user data alone)
python3 src/uninstall.py
```

Logs:
- Menubar app: `~/Library/Application Support/Torch/logs/torch.log`
- tunneld LaunchDaemon: `/var/log/torch-tunneld.{out,err}`

## Known gaps / non-goals (v1)

- **No py2app packaging.** The dock shows "Python" instead of
  "Torch" for the menubar process. Cosmetic only — the menubar
  icon is correct, logs are correct, everything else works.
- **No first-run wizard GUI.** The user runs the bootstrap
  script, enters their Apple ID in the Terminal once, and then
  everything else happens through the menubar.
- **No multi-Apple-ID support.** Single account per Mac.
- **No tweak injection.** Input IPAs are signed as-is. If you
  want tweaked apps, bring a pre-tweaked IPA.
- **No JIT attach.** See "dead ends" above — blocked by TXM on
  A15+ silicon until a public bypass ships.
- **No in-app IPA source browsing.** Drop-in-folder model only.
