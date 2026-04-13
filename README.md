# Torch 🔥

**A macOS menubar app that signs and auto-refreshes sideloaded IPAs on Apple TV, iPhone, and iPad — so your apps never expire.**

Set it up once with your free Apple ID, drop IPAs into a folder, and Torch re-signs and reinstalls them on a 6-day cycle (one day before Apple's 7-day free-tier profile expiry). No more manual re-signing in Sideloadly every week. No more broken apps on your Apple TV.

> [!WARNING]
> Torch is unofficial. It uses Apple's free developer provisioning APIs the same way Xcode, AltStore, SideStore, Sideloadly, and Impactor do. It doesn't bypass anything, jailbreak anything, or do anything Apple hasn't already allowed for personal development.

---

## Why Torch exists

Every existing Apple TV sideloader in 2026 is either broken on tvOS 26+ (because libimobiledevice can't speak the new RemotePairing protocol) or iOS-only (AltStore, SideStore, Sideloadly's wireless mode, Impactor). Torch is the first menubar tool that handles the full sign → install → auto-refresh loop for **tvOS 26+, iOS 17+, and iPadOS 17+ in one place**, using only a free Apple ID.

The big technical wins:

- **tvOS provisioning that actually works** on free accounts. We patched `plumesign` to send a `subPlatform: "tvOS"` parameter that nobody else uses, which makes Apple return proper tvOS profiles instead of rejecting the Apple TV as an "unsupported platform".
- **Native macOS anisette** via Apple's own `AOSKit.framework`. No dependency on third-party anisette relays (`ani.stikstore.app` etc.) that break every time Apple rotates Grand Slam Authentication.
- **Reliable installs over running apps.** We pre-kill the running bundle via `pymobiledevice3 developer dvt kill` before each install, so installd doesn't hang indefinitely waiting for the frontmost app to exit.
- **6-day auto-refresh** with wake-from-sleep detection, so the profile never expires even if your Mac was asleep over the weekend.
- **Per-device app-slot tracking** so you know before you hit Apple's "3 simultaneously-signed apps per device" cap.
- **Developer certificate rotation detection** so you're not silently producing broken IPAs when Apple rotates the 364-day cert.

---

## Features

- 🔥 Minimal menubar app with an SF Symbol icon
- 🔐 Apple ID signing using a free developer account (**zero cost**)
- 📺 tvOS 26+ support (Apple TV 4K)
- 📱 iOS 17+ / iPadOS 17+ support (iPhone and iPad)
- 🔁 Fully automatic 6-day refresh before the 7-day free-tier profile expires
- 🛌 Wake-from-sleep detection — refreshes fire immediately when your Mac comes back, not hours later
- 🚫 Pre-kill running apps before install so installd never hangs
- 📊 Expiration countdown for each app and for the developer certificate
- 🪪 Free Apple ID "3 apps per device" cap tracking — no silent invalidation
- 💾 Pair records auto-backed up to iCloud Drive so a Mac restore never loses paired devices
- 🧹 One-command install and uninstall

---

## Requirements

- **A Mac** running macOS 13 Ventura or newer (tested on macOS 26)
- **A free Apple ID** (no paid developer account required)
- **At least one Apple TV, iPhone, or iPad** to sideload to
- **Homebrew** — the bootstrap script installs it if missing

That's it. Python, Rust, `pymobiledevice3`, and every other dependency is installed automatically.

---

## Install

### One-command install on a fresh Mac

```bash
curl -fsSL https://raw.githubusercontent.com/trollzem/torch/main/bootstrap.sh | bash
```

That's the whole install. The script will:

1. Install Homebrew if it's not already there
2. Install Python 3.14 via Homebrew
3. Clone Torch to `~/torch`
4. Install the Python dependencies (`rumps`, `keyring`, `pexpect`, `pyobjc`, `pymobiledevice3`)
5. Verify the bundled patched `plumesign` binary is present
6. Prompt for your Apple ID email, password, and a 2FA code (**one-time only**, the session is cached forever after)
7. Install the launchd services (**macOS will ask for your admin password once**, via the native authorization dialog — the script never sees your password)
8. Tell you how to pair your first Apple TV / iPhone

After it finishes, a flame icon appears in your menubar. That's it — you're done.

### If you want to read the script before running it (you should)

```bash
curl -fsSL https://raw.githubusercontent.com/trollzem/torch/main/bootstrap.sh | less
```

Or just clone and review:

```bash
git clone https://github.com/trollzem/torch.git ~/torch
cd ~/torch
less bootstrap.sh
./bootstrap.sh
```

---

## Usage

### Adding an Apple TV

1. Click the 🔥 flame icon in your menubar → **Devices** → **Add Apple TV (pair via Terminal)…**
2. On the Apple TV, navigate to **Settings → General → Remotes and Devices → Remote App and Devices** and leave that screen open.
3. A Terminal window will open running `pymobiledevice3 remote pair`. Enter the 6-digit PIN that appears on the Apple TV screen when it prompts.
4. Torch detects the new pair record within a few seconds, registers the device with your Apple developer account, and adds it to the tracked devices list.

### Adding an iPhone or iPad

iPhones and iPads don't have a manual pairing screen — the first time you plug an iPhone into your Mac via USB, iOS shows "Trust This Computer?". Tap Trust. After that:

1. Click the 🔥 flame icon → **Devices** → **Detect iPhone/iPad (via USB trust)…**
2. Torch shows any USB-trusted devices that aren't yet tracked, and offers to add them.

### Adding an IPA

1. Click the 🔥 flame icon → **Apps** → **Add IPA…** (which opens the runtime IPAs folder in Finder)
2. Drop `.ipa` files into that folder.
3. Torch picks them up within 5 seconds (config watcher), detects the platform (tvOS / iOS / iPadOS), and auto-targets the new IPA at every compatible device.
4. Click **Refresh Now** to sign and install immediately, or wait for the next 6-day auto-refresh cycle.

### Status at a glance

The menubar dropdown always shows:

```
3/3 apps fresh · 2h ago
Cert: 304 days left
─────────────────────────
Apps
  ▸ YouTube · iOS · 6d 23h left
  ▸ Streamer · tvOS · 6d 23h left
  ...
─────────────────────────
Devices
  ▸ Apple TV · tvOS · AppleTV14,1 26.4 · 2/3 apps
  ▸ iPhone · iOS · iPhone18,4 26.3 · 1/3 apps
  ...
─────────────────────────
Refresh Now
Pause Auto-Refresh
...
```

The icon itself adapts to state:

- 🔥 **flame** — idle, everything fresh
- ↻ **refresh arrow** — signing/installing right now
- ⚠ **warning triangle** — at least one app is stale or the cert is expiring
- ✕ **error mark** — tunneld is down, login expired, or a refresh cycle failed

Every icon is rendered from an Apple SF Symbol as a template image, so it adapts to your menubar's light/dark theme automatically.

---

## How it works (brief)

Torch is a thin Python menubar app (~3,500 lines) that orchestrates two external components:

1. **[plumesign](https://github.com/CLARATION/Impactor)** — a Rust CLI from the Impactor project that talks to Apple's `developerservices2.apple.com` endpoints using your free Apple ID. It creates developer certificates, registers devices, generates provisioning profiles, and signs IPAs. Torch ships a **patched** build of plumesign in `bin/` with two modifications: native anisette via AOSKit (no third-party relay), and `subPlatform: "tvOS"` support (so tvOS profiles actually work for free accounts).
2. **[pymobiledevice3](https://github.com/doronz88/pymobiledevice3)** — Python tools for talking to iOS / tvOS / iPadOS devices over the modern RemotePairing protocol (replaces libimobiledevice, which is broken on tvOS 26+). Torch uses pymobiledevice3 for pairing, the persistent WiFi tunnel service, and app installation.

The orchestration (refresh scheduler, menubar UI, config management, device reconciliation, certificate rotation detection, pair record backup) is all in `src/torchapp/`.

Every file is small, well-commented, and designed to be read top-to-bottom. Start at `src/torchapp/__main__.py` and follow the imports.

---

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/trollzem/torch/main/uninstall.sh | bash
```

Or locally:

```bash
cd ~/torch
./uninstall.sh
```

The uninstaller will:

1. Stop the menubar app
2. Remove both launchd services (admin password prompt once)
3. Optionally remove `~/Library/Application Support/Torch/` (config, tracked IPAs, logs)
4. Optionally remove your Apple ID Keychain entry
5. Leave `~/.config/PlumeImpactor/` and `~/.pymobiledevice3/` alone unless you pass `--purge-all`

If you also want to delete the repo itself, `rm -rf ~/torch`.

---

## Free Apple ID limits

Torch tracks all of these and surfaces them in the UI, but you should know about them upfront:

| Limit | Scope | Impact |
|---|---|---|
| **10 new App IDs per 7 days** | Team-level | Only new distinct bundle IDs count. Refreshing existing IPAs is free. |
| **3 apps per device** | Per-device | Installing a 4th signed app on a single device silently invalidates the oldest. Torch refuses the 4th install with a clear error. |
| **7-day provisioning profile** | Per-app | Torch re-signs every 6 days to keep a 1-day buffer. |
| **364-day developer certificate** | Team-level | Torch detects expiry and expiring states and surfaces them in the menubar. Re-login required on expiry. |

Apple does not officially document most of these limits. They're reverse-engineered from Xcode error messages and community knowledge. Torch's enforcement matches what AltStore, SideStore, and Sideloadly have converged on.

---

## What Torch does NOT do

- **Jailbreak anything.** Torch uses Apple's official free developer provisioning. If Apple lets Xcode do it, Torch does it the same way.
- **Bypass code signing.** Every IPA is signed by Apple's CA via a real developer certificate issued to your Apple ID. The same way Xcode does it for personal development.
- **Install apps from the App Store.** Torch takes IPAs you already have and signs them for your own devices.
- **JIT for emulators on tvOS 26.** Apple's TXM (Trusted Execution Monitor) on A15+ chips blocks the `CS_DEBUGGED` JIT trick, so apps like iCube (the tvOS Dolphin fork) run in interpreter mode only. This is an Apple-side restriction that no sideloader can work around today. Torch will ship JIT attach the moment a public bypass exists.
- **Tweak injection.** Torch signs IPAs as-is. Bring your own pre-tweaked IPAs (YouTube Plus, uYouEnhanced, iCube, etc.).

---

## Credits

Torch stands on the shoulders of several open-source projects:

- **[CLARATION/Impactor](https://github.com/CLARATION/Impactor)** — the Rust sideloading core. Our patched `plumesign` binary is built from an Impactor fork with tvOS and AOSKit modifications.
- **[doronz88/pymobiledevice3](https://github.com/doronz88/pymobiledevice3)** — Python toolkit for modern iOS/tvOS device communication (RemotePairing, tunneld, DVT).
- **[jaredks/rumps](https://github.com/jaredks/rumps)** — Ridiculously Uncomplicated macOS Python Statusbar apps.
- **[SideJITServer](https://github.com/nythepegasus/SideJITServer)** — reference implementation of the JIT attach flow (which Torch doesn't need yet, but read for the DVT plumbing).
- **[AltStore](https://altstore.io) / [SideStore](https://sidestore.io)** — pioneered the "refresh your own sideloaded apps from a Mac" UX.

---

## License

MIT. See [LICENSE](LICENSE).

Torch is a personal project released for anyone who wants to use it. No warranty, no support guarantees — if it breaks, you keep both pieces.
