# Config

> **Maintainer note:** update this doc when (a) any field in
> `Config` / `Device` / `IPA` / `Settings` / `CertStatus` is
> added/removed/renamed, (b) `CONFIG_VERSION` is bumped, (c)
> the IPAs-folder sync logic in `sync_ipas_folder` changes,
> (d) the iCloud Drive pair-record backup path changes, or (e)
> `platform_matches_device` semantics change (this is the single
> source of truth for IPA <-> device compatibility).

## What this is

The config subsystem is the on-disk source of truth for what
Torch is tracking: which IPAs, which devices, which device gets
which IPA, when each IPA was last refreshed, what the cert
status is, and what the user's preferences are. Lives in
`src/torchapp/config.py` and serializes to
`~/Library/Application Support/Torch/config.json`.

## Schema

```python
@dataclass
class Config:
    version: int = CONFIG_VERSION                  # 1
    apple_id_email: str | None = None
    devices: list[Device] = []
    ipas: list[IPA] = []
    settings: Settings = ...
    cert_status: CertStatus = ...

@dataclass
class Device:
    name: str                                       # "Habibi TV"
    pair_record_identifier: str                     # stable primary key
    udid: str | None                                # filled by reconcile_device
    device_class: str                               # "tvOS"/"iOS"/"iPadOS"/"unknown"
    paired_at: str                                  # ISO8601 UTC
    pair_record_path: str | None = None
    product_type: str | None = None                 # "AppleTV14,1"
    product_version: str | None = None              # "26.4"
    approved_for_install: bool = True               # False for auto-detected
                                                    # devices until user opts in

@dataclass
class IPA:
    filename: str                                   # "YouTube-iOS.ipa"
    sha256: str
    original_bundle_id: str                         # "com.google.ios.youtube"
    platform: str                                   # "tvOS"/"iOS"/"iPadOS"
    added_at: str
    target_devices: list[str] = []                  # pair_record_identifier values
    last_signed_at: str | None = None
    last_installed_at: str | None = None
    signed_bundle_id: str | None = None
    status: str = "pending"                         # see status taxonomy
    consecutive_failures: int = 0
    last_error: str | None = None
    installs: dict[str, str] = {}                   # pair_record_id -> ISO install time
    expiry_notified: dict[str, str] = {}            # pair_record_id -> ISO date warned
    profile_expires_at: str | None = None           # from embedded.mobileprovision
    profile_ttl_days: int | None = None             # 7 = free tier, 365 = paid

@dataclass
class Settings:
    refresh_interval_days: int = 5                  # FALLBACK only; real cadence is
                                                    # derived from profile_ttl_days
    auto_refresh_paused: bool = False
    start_at_login: bool = True

@dataclass
class CertStatus:
    certificate_id: str | None = None
    name: str | None = None
    expiration_date: str | None = None              # ISO8601
    status: str = "unknown"                         # see refresh.refresh_cert_status
    checked_at: str | None = None
```

Field-level notes:

- `IPA.target_devices` stores `Device.pair_record_identifier`
  values (not UDIDs). The pair_record_identifier is what tunneld
  uses to address tunnels, so this lookup is what the install
  loop needs.
- `IPA.signed_bundle_id` differs from `original_bundle_id` --
  plumesign rewrites the bundle ID to include the team ID
  (e.g. `com.google.ios.youtube` becomes
  `com.google.ios.youtube.3G6AP3U89B`). The signed value is what
  DVT pre-kill and `installer` use to talk to the on-device app.
- `last_signed_at` and `last_installed_at` are only updated on
  `_record_success`. A `partial` cycle (some installed, some
  offline) does update `last_signed_at` but leaves
  `last_installed_at` at the previous value.

## Atomic save

```python
def save(self) -> None:
    tmp = paths.CONFIG_FILE.with_name(paths.CONFIG_FILE.name + ".tmp")
    tmp.write_text(json.dumps(asdict(self), indent=2, default=str))
    tmp.replace(paths.CONFIG_FILE)
```

Writes to a sibling temp file, then `os.rename` (atomic within
the same directory on POSIX). Without this, concurrent saves
from the hourly refresh worker + the iOS auto-detect worker could
interleave and corrupt the JSON. The race window is small but
real -- the iOS auto-detect saves immediately after appending a
new device, and the refresh cycle saves at the end. Atomic
rename guarantees readers always see a complete, valid file.

## Tolerant load (`_known_fields`)

`_from_dict` filters every raw dict down to the keys its
dataclass actually declares before constructing it:

```python
allowed = {f.name for f in fields(cls_)}
return {k: v for k, v in raw.items() if k in allowed}
```

Plain `Cls(**raw)` raises `TypeError` on any unexpected key,
which turns config.json into a one-way door: the moment a newer
Torch writes a new field, an older bundle reading the same file
dies on **every** load. That happened for real on 2026-08-10 —
`profile_expires_at` was written while the previous build was
still running, and its config-watcher tick threw on each fire
until the new bundle landed.

Unknown keys are dropped, not preserved. They'd be lost on the
next `save()` regardless (which serializes from the dataclass),
and silently round-tripping fields this version can't reason
about is worse than forgetting them. Adding a field is therefore
always safe; **renaming or repurposing** one still needs a real
migration.

## sync_ipas_folder

Called from `bootstrap()` on startup and from the 5s watcher
tick in `ui.py:_on_config_watch_tick`. Reconciles `cfg.ipas`
against the contents of `~/Library/Application Support/Torch/ipas/`:

- Any `.ipa` file in the folder not already in `cfg.ipas` ->
  call `_make_ipa_entry` to build a new IPA record, which:
  - Reads the IPA's Info.plist to determine platform (`tvOS` /
    `iOS` / `iPadOS`) via `_detect_ipa_platform`
  - Computes SHA-256 of the file
  - Reads the original bundle ID
  - Auto-targets every platform-compatible device via
    `platform_matches_device` (was previously every device with
    a refresh-time filter, but that polluted target_devices with
    incompatible entries that confused readers of config.json)
- Any IPA in `cfg.ipas` whose source file no longer exists ->
  remove from `cfg.ipas` (the signed cache in `signed/` is left
  alone; the refresh module surfaces "missing source" if anyone
  tries to refresh it before removal propagates)

Returns True if anything changed; the caller is responsible for
`save()`.

## platform_matches_device

The single source of truth for IPA <-> device compatibility:

```python
def platform_matches_device(ipa_platform: str, device_class: str) -> bool:
    if ipa_platform == "tvOS":
        return device_class == "tvOS"
    return device_class in ("iOS", "iPadOS")
```

Lives here (not in `refresh.py`) because `_make_ipa_entry` needs
it at config-time AND `refresh.is_compatible` needs it at
install-time. Putting it in `config.py` avoids a circular import
(refresh imports config, but config doesn't import refresh).
`refresh.is_compatible` is a thin re-export that delegates here.

Three call sites stay in lockstep through this predicate:

1. `config._make_ipa_entry` -- newly-discovered IPA gets only
   platform-compatible devices in its initial `target_devices`
2. `ui.py` per-IPA "Install on devices" submenu -- only lists
   compatible devices
3. `refresh.refresh_one` -- the `compatible` filter that decides
   the install loop

## Pair record iCloud Drive backup

On every app startup `bootstrap()` mirrors
`~/.pymobiledevice3/remote_*.plist` to
`~/Library/Mobile Documents/com~apple~CloudDocs/Torch/backup/pair-records/`
when iCloud Drive is available. The pair records are tiny
(~400 bytes each) and re-pairing every device after a Mac
reinstall is painful, so we make the records cloud-backed.

On first-launch reseeding, `bootstrap()` also reads any pair
records found locally OR in the iCloud backup to seed
`cfg.devices` for a returning user. Apple TV UDIDs aren't
in the pair record itself -- the actual UDID is filled in by
`reconcile_device` once tunneld establishes a tunnel.

The classic usbmux pair records at `/var/db/lockdown/*.plist`
are NOT mirrored: they're owned by `_usbmuxd:_usbmuxd` and we
don't have permission to read them as the user. Apple's
`/var/db/lockdown/` survives a Mac restore via Migration
Assistant anyway.

## Runtime path constants

```python
APP_SUPPORT_DIR = ~/Library/Application Support/Torch
CONFIG_FILE     = APP_SUPPORT_DIR / config.json
LOG_DIR         = APP_SUPPORT_DIR / logs
LOG_FILE        = LOG_DIR / torch.log
IPAS_DIR        = APP_SUPPORT_DIR / ipas              # user drops .ipa here
SIGNED_DIR      = APP_SUPPORT_DIR / signed            # plumesign outputs land here

PROJECT_ROOT       = <repo root>
PROJECT_IPAS_DIR   = PROJECT_ROOT / ipas              # dev: copy these to runtime
PROJECT_SIGNED_DIR = PROJECT_ROOT / signed

PYMD3_PAIR_RECORDS_DIR = ~/.pymobiledevice3
PLUMESIGN_STATE_DIR    = ~/.config/PlumeImpactor
PLUMESIGN_ACCOUNTS_FILE = ~/.config/PlumeImpactor/accounts.json

TUNNELD_URL = http://127.0.0.1:49151/
```

`PLUMESIGN_BINARY` resolves through `_resolve_plumesign_binary()`
which probes (in order):

1. `NSBundle.mainBundle().resourcePath() / "plumesign"` -- when
   running inside the py2app bundle, this is where py2app
   flattens the `resources=["bin/plumesign"]` declaration
2. Same as 1 but `/bin/plumesign` subdir -- safety net if setup.py
   uses tuple-form resource declaration in the future
3. `<repo>/bin/plumesign` -- dev mode, running `python3 -m torchapp`
   from a source tree

The NSBundle probe is wrapped in a try/except so unit tests
running outside Cocoa fall through cleanly.

## Status taxonomy

Defined informally across `refresh.py` (which sets statuses) and
`ui.py` (which renders status icons). The full table lives in
[architecture.md](architecture.md#failure-class-taxonomy). The
canonical strings are:

```
ok, pending, partial, device-offline, device-at-cap,
needs-login, app-id-limit, tunneld-down, sign-failed,
install-failed, auth-error, missing-source
```

Soft statuses don't bump `consecutive_failures`; hard statuses
do. See [refresh.md](refresh.md) for the contract.

## Key files

- `src/torchapp/config.py` -- dataclasses + load/save + bootstrap
  + sync_ipas_folder + platform_matches_device
- `src/torchapp/paths.py` -- path constants + plumesign binary
  resolver
- `src/torchapp/refresh.py` `_record_success` / `_record_failure`
  / `_record_soft_failure` -- the writers
- `src/torchapp/ui.py` `_on_config_watch_tick` -- the 5s reader
  + saver that keeps the menu in sync

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `config.json` parse error on launch | concurrent save without atomic rename | shouldn't happen now; check `config.json.tmp` is gone |
| Menu shows IPA but file isn't on disk | manual edit, then sync didn't run | the 5s watcher reconciles within a tick |
| New IPA not showing up | not a valid `.ipa` (missing `Payload/`) | check `Info.plist` reads via `_detect_ipa_platform` |
| `target_devices` has UDIDs not pair_record_identifiers | bad manual edit | refresh will fail to find the device; correct with the pair_record_identifier (`config.json` `devices[]` entries) |

## Related docs

- [architecture.md](architecture.md) -- file layout + status taxonomy
- [refresh.md](refresh.md) -- the writers of IPA.last_*, status, consecutive_failures
- [devices.md](devices.md) -- the readers/writers of `cfg.devices`
- [install.md](install.md) -- consumes `IPA.target_devices` for the install loop
- [ui.md](ui.md) -- the 5s watcher tick that drives sync_ipas_folder and config reloads
