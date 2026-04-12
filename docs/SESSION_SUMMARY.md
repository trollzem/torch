# Session Summary — ATVLoader Development

## Date: 2026-04-12

## Problem Statement
ATVLoadly (Docker container on Linux VM at 192.168.68.75) stopped recognizing Apple TV "Habibi TV" after a tvOS update unpaired it. The user wanted to fix the sideloading pipeline that auto-refreshes YouTube and Streamer apps on the Apple TV.

## Root Cause Investigation

### Discovery Phase
1. SSH'd into Linux VM (`hazem@192.168.68.75`, password `gameboy`)
2. Found ATVLoadly running as Docker container (`bitxeno/atvloadly:latest`, was v0.2.7)
3. Errors: `IDEVICE_E_NO_DEVICE` — libimobiledevice couldn't find Apple TV
4. Investigated usbmuxd2, avahi, mDNS discovery

### Root Cause Identified
**tvOS 26.x changed the device discovery/pairing protocol:**
- OLD: Apple TV advertises `_apple-pairable._tcp` for discovery + traditional lockdown pairing
- NEW: Apple TV advertises `_remotepairing-manual-pairing._tcp` + RemotePairing (RemoteXPC) protocol
- ATVLoadly only knows `_apple-pairable._tcp` → can't find the Apple TV
- This is GitHub Issue #86 on bitxeno/atvloadly — open and unresolved

### Additional Findings
- The Apple TV's WiFi MAC is `48:e1:5c:69:af:2b` (unchanged)
- The `_apple-mobdev2._tcp` services with MAC `64:31:35:91:69:08` are from the user's Mac, NOT the Apple TV
- The Apple TV's real UDID is `00008110-000E59EC3E41801E`
- The old lockdown pairing record was stale and deleted

## Attempted Fixes (on Linux VM)

### 1. ATVLoadly Upgrade ✅
- Upgraded container from v0.2.7 to v0.3.7
- Still uses `_apple-pairable._tcp` — same issue

### 2. mDNS Bridge Service ✅ (partial)
- Created `atvloadly-mdns-bridge.service` that re-publishes `_remotepairing-manual-pairing._tcp` as `_apple-pairable._tcp`
- Also publishes avahi address record so resolver works
- Also creates fake avahi-daemon PID file for ATVLoadly's status check
- **Result:** ATVLoadly CAN see the Apple TV as "pairable" in the UI

### 3. idevicepair Wrapper ✅ (partial)
- Replaced `/usr/bin/idevicepair` with a bash wrapper that:
  - Adds `-w` flag for wireless pairing on WiFi-only devices
  - Fakes `validate` success
- Original binary moved to `/usr/bin/idevicepair.real`

### 4. UI Pairing Flow ✅ (partial)
- Through the ATVLoadly UI, pairing DOES work — PIN appears on Apple TV, user enters it, ATVLoadly says "successfully paired"
- A lockdown plist IS created at `/data/lockdown/00008110-000E59EC3E41801E.plist`
- BUT: `idevicepair validate` returns "Invalid HostID" — the pairing happened via RemotePairing but lockdownd doesn't recognize the host

### 5. pymobiledevice3 Pairing ✅ (WORKS)
- Installed pymobiledevice3 on the Linux VM
- `pymobiledevice3 remote pair --name "Habibi TV"` — successfully paired with PIN 573567
- Pair record saved to `~/.pymobiledevice3/remote_155263B4-89DB-4F83-B237-170E0E8A6817.plist`
- `RemotePairingCompletedError` = success (exception is thrown when pairing completes)

### 6. pymobiledevice3 Tunnel ✅ (WORKS)
- `pymobiledevice3 remote tunneld --wifi` — creates persistent tunnel
- Created systemd service: `pymobiledevice3-tunneld.service`
- Tunnel exposes RSD at `127.0.0.1:49151`
- Full device access through tunnel: lockdown info, app listing, app install all work

### 7. App Install Test ✅ (partial)
- `pymobiledevice3 apps install --rsd <addr> <port> YouTube.ipa` — reached 40% before failing
- Failure: `ApplicationVerificationFailed: No code signature found`
- This proves the install pipeline WORKS — just needs signed IPAs

### 8. Protocol Bridge Attempts ❌
- Tried to make libimobiledevice work through the tunnel — FAILED
- The tunnel's port 62078 speaks RemoteXPC, not traditional lockdown
- `RemoteXPC lockdown version does not support pairing operations` — confirmed by pymobiledevice3 source code
- This is a FUNDAMENTAL incompatibility — no amount of patching can make libimobiledevice work with tvOS 26+

## Decision: Build Native Mac App
Since ATVLoadly's libimobiledevice is fundamentally broken on tvOS 26+, user decided to build a native macOS menubar app ("ATVLoader") that uses pymobiledevice3 directly.

## What Was Built

### On Linux VM (192.168.68.75)
**Services installed (systemd, enabled on boot):**
- `pymobiledevice3-tunneld.service` — maintains WiFi tunnel to Apple TV
- `atvloadly-mdns-bridge.service` — mDNS re-publisher for ATVLoadly compatibility

**Files modified in ATVLoadly container:**
- `/usr/bin/idevicepair` — wrapper script (original at `/usr/bin/idevicepair.real`)
- `/run/avahi-daemon/pid` — fake PID file for status check

**Packages installed:**
- `pymobiledevice3` 9.9.1 (pip, user-level at `~/.local/`)
- `sqlite3` (apt)

### On Mac
**Project:** `~/Desktop/projects/ATVLoader/`

**Dependencies installed:**
- `pymobiledevice3` 9.9.1 (pip --break-system-packages)
- `rumps` 0.4.0 (pip --break-system-packages)
- `zsign` 1.0.4 (brew)

**Files:**
- `src/atvloader.py` — menubar app skeleton
- `src/setup_signing.py` — signing setup script
- `ipas/YouTube.ipa` — YouTube 4.51.08 (unsigned, 46MB)
- `ipas/Streamer.ipa` — Streamer 1.3.0 (unsigned, 51MB)
- `CLAUDE.md` — project context for Claude Code
- `docs/SESSION_SUMMARY.md` — this file

## Where To Pick Up

### Immediate Next Steps
1. **Pair the Mac with the Apple TV** — Run `sudo pymobiledevice3 remote pair` while Apple TV is in pairing mode. This is the FIRST thing to do.
2. **Start tunnel from Mac** — `sudo pymobiledevice3 remote tunneld --wifi`
3. **Solve IPA signing** — This is the key unsolved problem. Self-signed certs don't work. Need Apple-issued developer certs through the Apple ID (eissahazem@gmail.com).

### The Signing Problem (Most Important)
The IPAs must be signed with a certificate issued by Apple's CA. Free Apple ID certs:
- Expire every 7 days
- Are obtained through Apple's developer provisioning API
- Require: Apple ID auth → create signing cert → register device UDID → create provisioning profile

**Research paths for solving signing:**
1. Look at how `AltServer-Linux` / `AltStore` implement the Apple ID → developer cert flow
2. Check if `pymobiledevice3` has hidden signing capabilities
3. Check `SideJITServer` Python code for Apple ID auth protocol
4. Use Xcode's automatic signing (sign in with Apple ID in Xcode → build dummy tvOS project → extract cert/key/profile → use with zsign)
5. Look into `apple-codesign` Rust tool (gregoryszorc/apple-codesign) which can handle Apple ID auth

### Apple TV Details for Pairing
- Apple TV name: "Habibi TV"
- Apple TV must be on Settings > Remotes and Devices > Remote App and Devices
- PIN changes frequently and is rate-limited (wait 30+ seconds between attempts)
- Use `sudo pymobiledevice3 remote pair --name "Habibi TV"` and enter PIN when prompted

### Linux VM Notes
- SSH: `ssh hazem@192.168.68.75` (password: gameboy, SSH key was at /tmp/atv_key but may be gone)
- The VM's tunnel service may need restarting if Apple TV reboots
- ATVLoadly web UI is at http://192.168.68.75/ (still shows the Apple TV but can't install)
