# ATVLoader — Product Design

**Date:** 2026-04-12
**Status:** approved, ready for implementation
**Supersedes:** `docs/SESSION_SUMMARY.md`, `CLAUDE.md` phases 1–5

## Goal

A macOS menubar app that signs and installs IPAs onto Apple TV (tvOS 26+), iPhone (iOS 26+), and iPad (iPadOS 26+) using a free Apple ID. Set it up once, drop IPAs into a folder, and they get auto-refreshed every 6 days before the free-tier 7-day profile expiry. Think Sideloadly but menubar-only and tvOS-aware.

## Non-goals (v1)

- Main window / full GUI beyond the menubar dropdown.
- Multi-Apple-ID accounts.
- Cloud sync of config.
- Signing `.app` directories. `.ipa` input only.
- `.app` bundle packaging / py2app / code signing. Plain Python scripts managed by launchd; we can repackage later.
- Ports beyond macOS.
- App store / discovery / browsing.

---

## Spike findings (what we proved before designing this)

These are load-bearing discoveries from the 2026-04-12 spike. They explain *why* the design looks the way it does.

### What works

- **Apple ID auth via `plumesign account login`** handles SRP, 2FA, and anisette via `ani.stikstore.app`. Session persists in `~/.config/PlumeImpactor/accounts.json` + `state.plist` and survives indefinitely across runs.
- **Mac ↔ Apple TV pairing via `pymobiledevice3 remote pair`** works on tvOS 26.4. The spike paired Habibi TV (`00008110-000E59EC3E41801E`) and wrote `~/.pymobiledevice3/remote_282C5ECA-2B41-4E94-A97A-0692031F7123.plist`. `RemotePairingCompletedError` is thrown on success, not failure.
- **`pymobiledevice3 remote tunneld --wifi`** creates a persistent WiFi tunnel. Verified via `curl http://127.0.0.1:49151/` returning tunnel metadata, and `pymobiledevice3 lockdown info --rsd <addr> <port>` returning full device info through the tunnel.
- **`plumesign account register-device --udid <tvOS-UDID>`** succeeds against the `/QH65B2/ios/addDevice.action` endpoint. Apple auto-detects `device_class: "tvOS"` from the UDID chip prefix (`00008110`). There is only one device registry — iOS and tvOS share it.
- **Apple-issued dev cert** is created on the first `plumesign sign --apple-id` call and stored at `~/.config/PlumeImpactor/keys/<team-id>/key.pem`. Labelled `"iPhone Developer: eissahazem@gmail.com (77V9722KZH)"` — the "iPhone Developer" label is a historical naming; the cert signs all platforms on free tier.
- **tvOS provisioning profile** is obtained by sending `subPlatform: "tvOS"` in the body of a POST to `/QH65B2/ios/downloadTeamProvisioningProfile.action`. The URL stays at `/ios/`. The response contains a real Apple-signed profile with `Platform: [tvOS]` and `ProvisionedDevices: [<our-UDID>]`. **This was not documented anywhere; we discovered it by experiment.**
- **Install via tunnel** with `pymobiledevice3 apps install --rsd <addr> <port> <signed.ipa>` works for tvOS 26 and the signed app launches and runs.

### What's broken in plumesign v2.2.3 (and our workarounds)

1. **Archive-step bug.** `plumesign sign --package X.ipa -o Y.ipa` copies the *input file* to the output path instead of re-archiving the signed staging directory. The actual signing work happens in `/var/folders/.../plume_stage_<uuid>/` and is thrown away at end of run. Source: [`crates/plume_utils/src/package.rs` `get_archive_based_on_path`](vendor/impactor-tvos.patch).

   **Workaround:** set `PLUME_DELETE_AFTER_FINISHED=1` to skip staging cleanup (plumesign already has this escape hatch for debugging), then re-zip `<staging>/Payload` ourselves into the output IPA.

2. **Hardcoded `/ios/` endpoints.** All of plumesign's developer-portal calls hit `/QH65B2/ios/...`. There is no tvOS code path. The profile endpoint needed `subPlatform: "tvOS"` in the body; that's a one-line patch.

   **Workaround:** fork Impactor, patch `crates/plume_core/src/developer/qh/profile.rs::qh_get_profile` to add the `subPlatform` field when `PLUME_FORCE_TVOS=1`, rebuild. Our patch is preserved at `vendor/impactor-tvos.patch` and the built binary at `bin/plumesign`.

3. **`plumesign device ...` and `plumesign sign --register-and-install`** are USB-only because the underlying `idevice` Rust crate doesn't implement RemotePairing transport. Apple TV 4K has no USB port, so the built-in install path is dead for us. **We never use plumesign for installation** — pymobiledevice3 does that over the WiFi tunnel.

4. **`plumesign account devices --platform tvos`** is a cosmetic lie: the `--platform` flag is only a client-side filter; plumesign still hits `/ios/listDevices.action`. Not a blocker for us (we use `account register-device` which works).

### What we learned about Apple's API shape

- Cert, AppID, and Device registries are **shared across platforms**. There is no `/tvos/addAppId`, no `/tvos/listDevices`, no `/tvos/listAllDevelopmentCerts`. Requests to those URLs return `UnexpectedEndOfEventStream`.
- The **provisioning profile endpoint** is the only one that differentiates by platform, and it does so via a body parameter (`subPlatform`), not by URL path.
- Free Apple ID memberships have `platform: "ios"` but can still obtain tvOS profiles via `subPlatform`.
- Free tier has a **10-app-ID-per-7-days limit** that we have to budget around. We spent 2 slots during the spike (YouTube and Streamer).
- Profiles expire in **7 days**. Certs expire in **364 days**.

### What doesn't work (dead paths we ruled out)

- Writing our own Grand Slam / AuthKit auth in Python: dead. No maintained library; anisette is a real cryptographic barrier.
- Patching `/QH65B2/ios/` → `/QH65B2/tvos/` in the URL: all non-profile endpoints return empty. Only the profile endpoint needs touching.
- Using Xcode automatic signing: Xcode does not see Habibi TV (`xcrun xctrace list devices` shows only the iPhone). tvOS 26.4 and Xcode's CoreDevice don't cooperate for wireless tvOS pairing.
- Using self-signed certs with a hand-crafted provisioning profile: Apple TV rejects with `ApplicationVerificationFailed: No code signature found`.
- `ATVLoadly` / `libimobiledevice`: fundamentally incompatible with tvOS 26+ RemotePairing. The underlying lockdown pairing protocol was removed. No patch can bring it back.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  ATVLoader menubar (rumps, user-level LaunchAgent)       │
│  ┌────────────┐  ┌──────────┐  ┌────────────────────┐    │
│  │ UI / state │  │ Refresh  │  │ Pairing wizard     │    │
│  │ rumps.App  │  │ scheduler│  │ pexpect + PIN UI   │    │
│  └─────┬──────┘  └─────┬────┘  └─────────┬──────────┘    │
│        │               │                 │               │
│  ┌─────▼───────────────▼─────────────────▼────────────┐  │
│  │ Plumesign wrapper (subprocess + staging scrape +    │  │
│  │ pexpect 2FA + PLUME_FORCE_TVOS env var routing)     │  │
│  └───────────────────┬─────────────────────────────────┘  │
│                      │                                    │
│  ┌───────────────────▼─────────────────────────────────┐  │
│  │ pymobiledevice3 client                              │  │
│  │ - bonjour scan for manual-pairing                   │  │
│  │ - remote pair (pexpect for PIN)                     │  │
│  │ - apps install via tunneld HTTP API (127.0.0.1:49151)│  │
│  └───────────────────┬─────────────────────────────────┘  │
└──────────────────────┼─────────────────────────────────────┘
                       │
              ┌────────▼────────────────────────┐
              │  pymobiledevice3 tunneld         │
              │  (root LaunchDaemon,             │
              │   `remote tunneld --wifi`)       │
              │  HTTP API on 127.0.0.1:49151     │
              └────────┬────────────────────────┘
                       │ RemotePairing tunnels (TUN ifaces)
              ┌────────▼─────────┐
              │  Paired devices  │
              │  • Habibi TV     │
              │  • Hazem iPhone  │
              │  • (future)      │
              └──────────────────┘
```

Two long-running processes:
- **System LaunchDaemon `com.atvloader.tunneld`** — root. Runs `pymobiledevice3 remote tunneld --wifi`. Started at boot. Provides `http://127.0.0.1:49151/` to user-level processes.
- **User LaunchAgent `com.atvloader.app`** — user. Runs the Python menubar app. `KeepAlive=true`, starts at login, restarted if it crashes.

One external subprocess as needed:
- **`bin/plumesign`** — our patched Rust binary. Invoked per-signing-job; not long-running.

## Credentials and auth

| Secret                                | Where                                             | Who writes                   | Who reads                  |
| ------------------------------------- | ------------------------------------------------- | ---------------------------- | -------------------------- |
| Apple ID email                        | `config.json` (display) + Keychain                | First-run wizard             | First-run + expired-login recovery |
| Apple ID password                     | Keychain only (service `com.atvloader.appleid`)   | First-run wizard             | First-run + recovery       |
| plumesign anisette + session          | `~/.config/PlumeImpactor/`                        | `plumesign account login`    | plumesign itself           |
| Apple dev cert private key            | `~/.config/PlumeImpactor/keys/<team>/key.pem`     | plumesign on first sign      | plumesign on sign          |
| `~/.pymobiledevice3/remote_*.plist`   | per-device pair records                           | `pymobiledevice3 remote pair`| tunneld, `apps install`    |

**First-run Apple ID flow:** email dialog → password dialog → store in Keychain → `pexpect` spawns `plumesign account login -u <email> -p <password>` → watches for 2FA prompt → pops 6-digit input dialog → pipes code to stdin → success → updates `config.json` with email.

**Expired-session recovery:** any plumesign call that fails with auth-related error → `pexpect` re-login using Keychain-stored password → only 2FA re-prompt needed.

## Device pairing UX

Install-time sudo moment (happens exactly once, at `install.py`): write `/Library/LaunchDaemons/com.atvloader.tunneld.plist`, `chown root:wheel`, `launchctl bootstrap`. All via `osascript -e 'do shell script "..." with administrator privileges'`. After this, no sudo is needed for any normal operation.

**Add a tvOS device:**
1. User: menubar → Devices → Add device… → Apple TV
2. App shows: *"On your Apple TV go to Settings → General → Remotes and Devices → Remote App and Devices, then click Continue."*
3. On Continue, app scans `_remotepairing-manual-pairing._tcp` via `pymobiledevice3 bonjour remotepairing-manual-pairing` for 10 seconds.
4. If found, spawn `pymobiledevice3 remote pair --name "<device-name>"` via `pexpect`, watch for `"Enter PIN:"`, pop a 6-digit `rumps.Window`, pipe response.
5. On `RemotePairingCompletedError` (= success), read the new pair record file to extract the UDID, call `plumesign account register-device --udid <UDID> --name "<name>"`, append to `config.json['devices']`.

**Add a USB-trusted iOS device:** auto-detected via polling the tunneld HTTP API. A new identifier appearing with interface `usbmux-*` triggers a "Add this iPhone?" prompt — no PIN needed (trust was established on first cable connection).

**Add a WiFi iOS device (never trusted):** same flow as tvOS. Settings → Developer → Pair Device on iOS, same `pexpect` + PIN dance.

**Seeding from existing state:** on first app launch, enumerate `~/.pymobiledevice3/remote_*.plist` and auto-register those identifiers as devices (the spike already paired Habibi TV).

**Unpair:** removes the local pair record, drops from `config.json`. We do not call a portal-side remove — plumesign doesn't expose one, and free-tier devices count against a 100-device cap that we're nowhere near.

## IPA pipeline

### Tracked-IPA schema in `config.json`

```json
{
  "ipas": [
    {
      "filename": "YouTube.ipa",
      "sha256": "75ef11273f53...",
      "original_bundle_id": "com.google.ios.youtube.unstable",
      "platform": "tvOS",
      "added_at": "2026-04-12T19:00:00Z",
      "target_device_udids": ["00008110-000E59EC3E41801E"],
      "last_signed_at": "2026-04-12T19:56:10Z",
      "last_installed_at": "2026-04-12T19:57:23Z",
      "signed_bundle_id": "com.google.ios.youtube.3G6AP3U89B",
      "status": "ok",
      "consecutive_failures": 0
    }
  ]
}
```

`platform` is detected on first add by extracting the main binary and inspecting `LC_BUILD_VERSION` via `otool -l`. Possible values: `"tvOS"`, `"iOS"`, `"iPadOS"` (iPadOS and iOS share one binary in practice; we treat them as one).

### Sign → install pipeline (one pass per platform)

```
for platform in needed_platforms:
    env = {"PLUME_DELETE_AFTER_FINISHED": "1"}
    if platform == "tvOS":
        env["PLUME_FORCE_TVOS"] = "1"
    run: ./bin/plumesign sign --package <ipa> --apple-id -o /tmp/throwaway.ipa
    scrape stderr for "plume_stage_<uuid>" path
    re-zip <staging>/Payload into signed/<stem>-<platform>.ipa using zipfile.ZIP_DEFLATED
    delete staging dir
    for udid in target_udids_for_platform:
        resolve (tunnel_addr, tunnel_port) via http://127.0.0.1:49151/
        run: pymobiledevice3 apps install --rsd <addr> <port> signed/<stem>-<platform>.ipa
```

**Staging-directory scrape:** parse plumesign's stderr for the line `writing signed main executable to /var/folders/.../plume_stage_<uuid>/Payload/<app>/<exe>`. Fallback if parsing fails: glob `/var/folders/*/*/T/plume_stage_*` for the newest directory modified in the last 30 seconds.

**Re-zip:** Python's `zipfile` with `ZIP_DEFLATED` and manual handling of symlinks via `ZipInfo.external_attr` (some frameworks contain symlinks and break if you use naive zipping).

**App-ID budget tracking:** `plumesign account app-ids` returns all current app IDs for the team. We parse the count on every refresh cycle and surface it as "7 / 10 used this week" in the menubar. If we project that a refresh cycle would exceed 10, we skip the refresh and notify the user to wait until the oldest app ID ages out.

**Error classification and retry:**
- `plumesign` auth error → run recovery flow (re-login with stored password + 2FA prompt).
- `plumesign` "app ID limit reached" → mark IPA `status: "app-id-limit"`, notify, skip.
- `plumesign` signing error → mark `status: "sign-failed"`, log plumesign stderr, notify.
- `pymobiledevice3 apps install` fail → retry once after 30s, then mark `status: "install-failed"`.
- 3 consecutive failures → freeze the IPA's retry loop until user intervention.

## Refresh scheduler

**Cadence:** 6 days (1-day headroom before 7-day free-tier profile expiry).

**Trigger paths:**
1. **Hourly timer** — `rumps.Timer(3600)` calls `check_refresh_needed()`.
2. **App launch** — immediate `check_refresh_needed()` on every startup.
3. **Wake from sleep** — PyObjC-registered `NSWorkspace.didWakeNotification` observer calls `check_refresh_needed()`.

**Opportunistic batching:** if any tracked IPA is ≥ 6 days stale, we refresh *all* IPAs in one run so the schedule stays coherent and Apple API calls are minimized.

**Single-refresh lock:** a `threading.Lock()` on `refresh_all()` guarantees only one refresh runs at a time; second calls no-op.

**Three-strike freeze:** `consecutive_failures >= 3` removes the IPA from the auto-refresh loop until the user clicks "Refresh Now" or the app restarts.

**Manual controls:**
- "Refresh Now" button in menu
- "Refresh <app>" in per-IPA submenu
- "Pause auto-refresh" toggle (persists in `config.json`)

## UI

```
📺 ATVLoader (menubar icon shows state: 📺 ✅ / 📺 ⏳ / 📺 ⚠️ / 📺 ❌)

─────────────────────────────
  All apps fresh · 2h ago     (disabled header)
─────────────────────────────
  Apps
    ▸ YouTube        ✓ 2h ago
        ▸ Refresh now
        ▸ Remove
        ▸ Target: Habibi TV [✓]
    ▸ Streamer       ✓ 2h ago
        ▸ (same submenu)
    ▸ Add IPA…
─────────────────────────────
  Devices
    ▸ Habibi TV · tvOS 26.4 · paired
    ▸ Hazem · iOS 26.3 · paired (USB)
    ▸ Add device…
─────────────────────────────
  Refresh Now
  Pause auto-refresh
─────────────────────────────
  Open IPAs folder
  View log
  Settings…
─────────────────────────────
  About
  Quit ATVLoader
```

**Settings dialog** (single `rumps.Window`): Apple ID email (display), "Change account…" button, refresh interval slider (3–6 days), start-at-login toggle, app-ID usage `"7 / 10 used this week"`, version, GitHub link.

**First-run wizard:** a sequence of `rumps.Window` dialogs — welcome → email → password → 2FA → install tunneld (sudo moment) → add first device → add first IPA → ready.

**Notifications:** `rumps.notification()` wraps `UNUserNotificationCenter`. Success, stale warning, tunneld down, session expired.

## Project layout

```
~/Desktop/Projects/ATVLoader/
├── src/
│   ├── atvloader/
│   │   ├── __init__.py
│   │   ├── __main__.py      entry point
│   │   ├── config.py        config.json load/save
│   │   ├── keychain.py      keyring wrapper
│   │   ├── plumesign.py     subprocess + staging scrape + pexpect
│   │   ├── pymd3.py         tunneld HTTP client + install
│   │   ├── pairing.py       remote pair + PIN UI
│   │   ├── refresh.py       scheduler + lock + wake hook
│   │   ├── ui.py            rumps.App subclass, menu builders
│   │   ├── wizards.py       first-run wizard
│   │   ├── launchd.py       plist install/uninstall
│   │   └── ipa.py           IPA inspection (LC_BUILD_VERSION)
│   ├── install.py           one-shot installer
│   └── uninstall.py         cleanup
├── bin/
│   └── plumesign            patched binary (tracked in git; rebuildable)
├── vendor/
│   └── impactor-tvos.patch  the source patch for rebuilding plumesign
├── docs/
│   ├── SESSION_SUMMARY.md
│   └── plans/
│       └── 2026-04-12-atvloader-product-design.md   ← this file
├── requirements.txt
└── README.md
```

**Runtime data** (not in repo):
```
~/Library/Application Support/ATVLoader/
  ipas/                IPAs added by the user
  signed/              cached signed outputs per (ipa, platform)
  config.json
  logs/atvloader.log
```

## Dependencies

- Python 3.14 (system, via Homebrew)
- `rumps` — menubar
- `pyobjc-framework-Cocoa` — for `NSWorkspace` wake notification
- `pexpect` — driving plumesign's interactive prompts
- `keyring` — macOS Keychain access
- `pymobiledevice3` — device communication (already installed)
- `cargo` / `rustc` — only required to rebuild `bin/plumesign` from `vendor/impactor-tvos.patch`. Not needed for day-to-day use.

## Distribution

Private tool. "Install" = clone the repo, `pip install -r requirements.txt --break-system-packages`, (optionally `cargo build` if the bundled binary is missing), `python3 src/install.py`. The installer runs the first-run wizard, writes both launchd plists, asks for sudo once via osascript, and starts the menubar.

No py2app, no notarization, no Homebrew formula, no auto-updater. `git pull && python3 src/install.py --reinstall` for upgrades.

## Known tradeoffs

1. **Dock shows "Python" instead of "ATVLoader"** for the menubar process because we're not using a `.app` bundle. Cosmetic; can be fixed by py2app later.
2. **Refresh can interrupt a running app on the TV** when it reinstalls. ~1-minute window every 6 days. Acceptable for v1.
3. **plumesign archive bug** is worked around via staging scrape — fragile against plumesign log format changes. We pin to our built binary and control the upgrade cadence manually.
4. **10-app-ID/week limit** on free Apple IDs caps how many distinct bundles we can refresh in a week. We surface usage in the menubar; no workaround exists short of a paid account.
5. **Anisette server dependency** (`ani.stikstore.app`) is a third-party service plumesign uses to bypass the Apple-machine-specific `X-Apple-I-MD` header. If it goes down, new logins fail; existing sessions keep working. Risk is low but non-zero.
