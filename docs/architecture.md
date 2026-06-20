# Architecture

> **Maintainer note:** update this doc when (a) a new long-running
> service is added, (b) the top-level data flow between subsystems
> changes, (c) a new directory is added under `src/torchapp/`, or
> (d) the runtime data directory layout under
> `~/Library/Application Support/Torch/` changes. The TOC in
> CLAUDE.md points here; broken refs there mean this doc is stale.

## What Torch does at the highest level

Torch is a macOS menubar app that keeps free-tier sideloaded
IPAs from expiring on Apple TV / iPhone / iPad. It signs each IPA
on a 5-day cadence with the user's free Apple Developer
credentials, installs the freshly-signed copy over Wi-Fi, and
surfaces status (countdown to expiry, last error, frozen state) in
the menubar.

Three persistent processes run on the user's Mac. They communicate
through a single tunnel discovery HTTP API and shared on-disk state.

```
+--------------------------------------------------------------+
|  com.torch.app (user LaunchAgent)        Torch.app bundle    |
|  - rumps menubar + status icons                              |
|  - hourly refresh tick + wake-from-sleep observer            |
|  - signs via patched plumesign binary                        |
|  - installs via pymd3 (tvOS) or ideviceinstaller (iOS)       |
+----------+---------------------------------------------------+
           |
           | HTTP GET / -> {pair_id: tunnel_addr:port, ...}
           v
+--------------------------------------------------------------+
|  com.torch.tunneld (system LaunchDaemon, root)               |
|  - pymobiledevice3 remote tunneld --wifi                     |
|  - Exposes 127.0.0.1:49151                                   |
|  - Maintains RemotePairing tunnels per paired device         |
+--------------------------------------------------------------+
           ^
           | launchctl kickstart -k system/com.torch.tunneld
           |
+--------------------------------------------------------------+
|  com.torch.tunneld-keepalive (system LaunchDaemon, root)     |
|  - python3 -m torchapp.tunneld_keepalive (every 30 min)      |
|  - Restarts tunneld if uptime > 7d or inventory empty > 6h   |
|  - Preempts long-uptime mDNS state rot                       |
+--------------------------------------------------------------+
```

For full details on each service plist + install flow, see
[launchd.md](launchd.md). For the watchdog logic specifically, see
[refresh.md#tunneld-keepalive-watchdog](refresh.md).

## Subsystem responsibilities

The Python code under `src/torchapp/` splits along these lines.
Each subsystem has its own doc; this section is just the map.

| Module | Doc | Responsibility |
|---|---|---|
| `plumesign.py` | [signing.md](signing.md) | Subprocess wrapper around the patched `plumesign` Rust binary. Apple ID auth, anisette, sign step. |
| `pymd3.py` | [devices.md](devices.md) | Tunneld HTTP client, lockdown info, DVT pre-kill, tvOS RSD install path. |
| `installer.py` | [install.md](install.md) | Device-class-aware install dispatcher. tvOS -> pymd3, iOS -> ideviceinstaller. Soft/hard failure classification. |
| `pairing.py`, `pair_helper.py` | [devices.md](devices.md) | tvOS PIN pairing flow (pexpect-driven). |
| `refresh.py` | [refresh.md](refresh.md) | Orchestrator. Hourly tick, needs_refresh, sign + install + status update, freeze logic, cert rotation detection. |
| `config.py` | [config.md](config.md) | Dataclass schema, atomic save, IPAs folder sync, platform compatibility predicate. |
| `ui.py`, `ui_dialogs.py`, `icons.py` | [ui.md](ui.md) | rumps menubar, status icons, modal dialogs, threading rules, config watcher tick. |
| `launchd.py` | [launchd.md](launchd.md) | plist generation + install/uninstall for the three services. |
| `tunneld_keepalive.py` | [launchd.md](launchd.md) | Watchdog daemon entry point. |
| `keychain.py` | [signing.md](signing.md) | macOS Keychain wrapper for Apple ID password persistence. |
| `paths.py` | [config.md](config.md) | Path constants + NSBundle-aware `plumesign` binary resolver. |

The py2app bundle is described separately in
[packaging.md](packaging.md).

## Repository layout

```
torch/
  bin/
    plumesign                  # patched Rust binary, tracked in git
  bootstrap.sh                 # one-command installer for fresh Macs
  uninstall.sh                 # one-command uninstaller
  setup.py                     # py2app config (see packaging.md)
  CLAUDE.md                    # engineering notes (gitignored)
  README.md                    # user-facing docs
  requirements.txt
  docs/                        # this directory
    architecture.md
    signing.md
    devices.md
    install.md
    refresh.md
    config.md
    ui.md
    launchd.md
    packaging.md
  src/
    install.py                 # py2app build + launchd installer
    uninstall.py
    torchapp/
      __init__.py
      __main__.py              # entry point (scrubs PYTHONHOME)
      paths.py
      config.py
      keychain.py
      icons.py
      plumesign.py
      pymd3.py
      installer.py
      pairing.py
      pair_helper.py
      refresh.py
      launchd.py
      tunneld_keepalive.py
      ui_dialogs.py
      ui.py
  vendor/
    impactor-tvos.patch        # source patch against Impactor v2.2.3
```

## Runtime state directories

Created on first launch; not part of the repo.

```
~/Library/Application Support/Torch/
  config.json                  # tracked IPAs, devices, settings, cert status
  ipas/                        # user-added .ipa files (source)
  signed/                      # per-IPA per-platform signed outputs
  icons/                       # rendered SF Symbol PNGs for menubar
  logs/torch.log               # menubar app log
```

External state owned by other tools (read on first-run to seed
config; never written by Torch):

```
~/.config/PlumeImpactor/       # plumesign session (accounts.json, state.plist, keys/)
~/.pymobiledevice3/             # pymd3 RemotePairing pair records
/var/db/lockdown/              # classic usbmux pair records (system, root-owned)
```

Pair records get mirrored to
`~/Library/Mobile Documents/com~apple~CloudDocs/Torch/backup/pair-records/`
on every app startup when iCloud Drive is available. See
[config.md](config.md) for the backup mechanism.

System log files written by the three LaunchDaemons:

```
/var/log/torch-tunneld.out
/var/log/torch-tunneld.err
/var/log/torch-tunneld-keepalive.log
```

## How a refresh cycle flows end-to-end

A normal cycle, from the hourly NSTimer tick to a freshly-installed
IPA on the device:

```
ui.py:_on_hourly_tick (main thread)
  -> ui.py:_background_check (spawn worker)
       -> refresh.py:refresh_all (worker thread, holds _refresh_lock)
            -> refresh.py:refresh_cert_status      # check cert health
            -> refresh.py:reconcile_devices        # update device info via tunneld
            -> for each ipa where needs_refresh and not is_frozen:
                 refresh.py:refresh_one
                   -> plumesign.sign_ipa           # generate signed IPA on disk
                   -> for each compatible device:
                        installer.install_for_device
                          if tvOS:  pymd3.install_ipa --rsd
                          if iOS:   ideviceinstaller -u UDID install
            -> _record_success / _record_failure / _record_soft_failure
       -> ui.py:_rebuild_async                    # marshal back to main thread
```

The split by device class at the install step is the single biggest
behavioral fork in the code. tvOS goes through pymobiledevice3's
RSD path; iOS/iPadOS goes through classic libimobiledevice via
`ideviceinstaller`. The reason for the split is in [install.md](install.md)
and CLAUDE.md discovery 6.

## Failure-class taxonomy

Every refresh outcome lands in one of these statuses, defined
across `refresh.py` and `installer.py`:

| Status | Class | Bumps strikes? | Meaning |
|---|---|---|---|
| `ok` | success | No (reset to 0) | sign+install succeeded for every compatible device |
| `pending` | initial | No | never refreshed yet |
| `partial` | soft | No | signed; installed on some devices; others offline |
| `device-offline` | soft | No | every compatible device unreachable |
| `device-at-cap` | soft | No | Apple rejected with 3-apps cap, surfaces external bundle IDs |
| `needs-login` | soft | No | plumesign session expired (Apple Developer API 1100) |
| `app-id-limit` | soft | No | Apple weekly 10-new-bundle-IDs cap |
| `tunneld-down` | abort | No | the whole refresh cycle aborted; tunneld isn't reachable |
| `sign-failed` | hard | Yes | unknown plumesign failure |
| `install-failed` | hard | Yes | install step returned error other than offline/cap |
| `auth-error` | hard | Yes | SRP / authentication failure that wasn't a session expiry |
| `missing-source` | hard | Yes | source IPA file deleted from disk |

After 3 hard failures the IPA is `is_frozen()` and skipped by
`refresh_all` until either the user clears the strikes by hand or
the dedicated reset paths run (re-login auto-clears
`needs-login`/`sign-failed`/`auth-error`). The full table of which
exception types map to which status is in `refresh.refresh_one`.

For deeper detail on each soft-failure class and how the retry
loop tolerates it, see [refresh.md](refresh.md).

## Related docs

- [signing.md](signing.md) -- patched plumesign, anisette, Apple ID auth
- [devices.md](devices.md) -- pairing flows, tunneld discovery
- [install.md](install.md) -- split install pipeline, reachability probes
- [refresh.md](refresh.md) -- orchestrator, interval, freeze, cert rotation
- [config.md](config.md) -- schema, persistence, IPAs folder sync
- [ui.md](ui.md) -- menubar, threading, watcher tick
- [launchd.md](launchd.md) -- three services, sudo dance, watchdog
- [packaging.md](packaging.md) -- py2app bundle build
