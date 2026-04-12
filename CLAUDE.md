# ATVLoader - Apple TV Sideloader for macOS

## Project Goal
A macOS menubar app that signs and installs IPAs on Apple TV (tvOS 26+), replacing the broken ATVLoadly Docker setup. Uses `pymobiledevice3` for device communication and `zsign` for IPA signing.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  ATVLoader (macOS menubar app)                  │
│  ┌───────────┐  ┌──────────┐  ┌─────────────┐  │
│  │ rumps UI  │  │ Signing  │  │ Installer   │  │
│  │ (menubar) │  │ (zsign)  │  │ (pymd3)     │  │
│  └───────────┘  └──────────┘  └─────────────┘  │
│        │              │              │          │
│        └──────────────┼──────────────┘          │
│                       │                         │
│            ┌──────────▼──────────┐              │
│            │ pymobiledevice3     │              │
│            │ tunneld (WiFi)      │              │
│            └──────────┬──────────┘              │
│                       │                         │
└───────────────────────┼─────────────────────────┘
                        │ RemotePairing tunnel
                ┌───────▼───────┐
                │  Apple TV     │
                │  Habibi TV    │
                │  tvOS 26.4    │
                │  192.168.68.82│
                └───────────────┘
```

## Key Technical Constraints

### Why ATVLoadly Broke (and why this project exists)
- tvOS 26+ changed the device discovery protocol from `_apple-pairable._tcp` to `_remotepairing-manual-pairing._tcp`
- tvOS 26+ deprecated traditional lockdown pairing; only RemotePairing (RemoteXPC) works
- ATVLoadly's `libimobiledevice` cannot do RemotePairing — this is a fundamental protocol incompatibility
- `pymobiledevice3` fully supports the new RemotePairing protocol and can install apps through it
- The RemoteXPC lockdown explicitly raises `NotImplementedError("RemoteXPC lockdown version does not support pairing operations")` — there is NO way to create a traditional lockdown pair record through the tunnel

### The RemotePairing Flow
1. `pymobiledevice3 remote pair` — discovers Apple TV via `_remotepairing-manual-pairing._tcp`, performs SRP handshake with PIN displayed on Apple TV, saves pair record to `~/.pymobiledevice3/remote_<identifier>.plist`
2. `pymobiledevice3 remote tunneld --wifi` — maintains a persistent TCP tunnel to the paired Apple TV, exposes RSD (Remote Service Discovery) at `127.0.0.1:49151`
3. Through the tunnel, ALL Apple TV services are accessible without traditional pairing: lockdown info, app install, provisioning, etc.

### IPA Signing Requirements
- Free Apple ID certs expire every 7 days — must auto-refresh
- Signing requires: developer certificate + private key + provisioning profile matching device UDID
- `zsign` (installed via `brew install zsign`) handles the actual binary re-signing
- The HARD PART is obtaining the developer cert + provisioning profile from Apple's servers using a free Apple ID. This is what AltStore/SideStore/PlumeImpactor implement.

## Current State (What's Done)

### On the Linux VM (192.168.68.75)
- ATVLoadly upgraded to v0.3.7 (but still can't connect due to protocol change)
- `pymobiledevice3` installed and RemotePairing completed with Apple TV
- `pymobiledevice3-tunneld.service` — systemd service maintaining WiFi tunnel to Apple TV
- `atvloadly-mdns-bridge.service` — re-publishes Apple TV's new mDNS service under old name (partial fix)
- Pairing record: `~hazem/.pymobiledevice3/remote_155263B4-89DB-4F83-B237-170E0E8A6817.plist`

### On the Mac (this project)
- `pymobiledevice3` 9.9.1 installed (system-wide via pip --break-system-packages)
- `zsign` 1.0.4 installed via Homebrew
- `rumps` installed for menubar UI
- Basic menubar app skeleton (`src/atvloader.py`)
- Basic signing setup script (`src/setup_signing.py`)
- YouTube.ipa and Streamer.ipa copied to `ipas/`

## What Needs To Be Done

### Phase 1: Pairing from Mac (CRITICAL)
The Mac needs its own RemotePairing with the Apple TV:
1. Run `sudo pymobiledevice3 remote pair` from the Mac
2. Apple TV must be in pairing mode (Settings > Remotes and Devices > Remote App and Devices)
3. Enter the 6-digit PIN displayed on Apple TV
4. This creates `~/.pymobiledevice3/remote_<id>.plist`

### Phase 2: Tunnel from Mac
1. Run `sudo pymobiledevice3 remote tunneld --wifi` as a persistent service (LaunchDaemon)
2. Verify with: `curl http://127.0.0.1:49151/`
3. Test device access: `sudo pymobiledevice3 lockdown info --rsd <addr> <port>`

### Phase 3: IPA Signing (THE HARD PART)
Self-signed certs do NOT work — Apple TV rejects them with "No code signature found" or "ApplicationVerificationFailed". Need a real Apple-issued developer certificate.

**Options (in order of feasibility):**

1. **Use AltSign/SideServer protocol** — Implement the Apple ID → developer cert flow that AltStore uses. Python libraries to research:
   - `altserver-linux` (has the protocol implementation in C++)
   - `SideJITServer` (Python, may have relevant auth code)
   - Apple's AuthKit/Grand Slam protocol for authentication
   - Apple's developer provisioning API for cert/profile creation

2. **Use Xcode automatic signing** — If Xcode is signed into the same Apple ID, use `xcodebuild` to generate the cert and profile, then extract them for zsign.

3. **Use an existing tool** — `ios-deploy`, `ideviceinstaller`, or `cfgutil` with Xcode signing

4. **Manual cert export** — Have user sign in to Xcode → create a dummy tvOS project → export the cert+key+profile → place in `signing/` directory

**Recommended: Start with option 4 (manual) for MVP, then automate with option 1 or 2.**

### Phase 4: Complete Menubar App
- Wire up the full sign → install pipeline
- Add auto-refresh timer (every 6 days)
- Add app management (add/remove IPAs)
- Proper error handling and notifications
- LaunchAgent for auto-start on login

### Phase 5: Polish
- App icon (Apple TV icon in menubar)
- py2app or PyInstaller packaging
- First-run wizard for Apple ID setup
- Progress indicators during sign/install

## Apple TV Details
- **Name:** Habibi TV
- **Model:** AppleTV14,1 (J255AP)
- **tvOS:** 26.4 (build 23L243)
- **IP:** 192.168.68.82
- **UDID:** 00008110-000E59EC3E41801E
- **WiFi MAC:** 48:e1:5c:69:af:2b
- **Ethernet MAC:** 48:e1:5c:75:c5:91

## Apple ID
- **Email:** eissahazem@gmail.com
- **Previous ATVLoadly password stored in DB:** (check session summary)

## Key Commands

```bash
# Pair with Apple TV (requires sudo, Apple TV in pairing mode)
sudo pymobiledevice3 remote pair

# Start persistent tunnel
sudo pymobiledevice3 remote tunneld --wifi

# Check tunnel
curl http://127.0.0.1:49151/

# Get device info through tunnel
sudo pymobiledevice3 lockdown info --rsd <TUNNEL_ADDR> <TUNNEL_PORT>

# Install a signed IPA
sudo pymobiledevice3 apps install --rsd <TUNNEL_ADDR> <TUNNEL_PORT> signed.ipa

# Sign an IPA with zsign
zsign -k key.pem -c cert.pem -m profile.mobileprovision -o signed.ipa input.ipa

# List apps on Apple TV
sudo pymobiledevice3 apps list --rsd <TUNNEL_ADDR> <TUNNEL_PORT>
```

## Dependencies
- Python 3.14+ (system)
- pymobiledevice3 9.9.1 (`pip3 install pymobiledevice3 --break-system-packages`)
- rumps 0.4.0 (`pip3 install rumps --break-system-packages`)
- zsign 1.0.4 (`brew install zsign`)

## Linux VM SSH Access
- **Host:** 192.168.68.75 (UTM VM running Debian)
- **User:** hazem
- **Password:** gameboy
- **SSH key:** was set up at /tmp/atv_key (may need to regenerate)
- **ATVLoadly web UI:** http://192.168.68.75/
