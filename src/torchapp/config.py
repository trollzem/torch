"""config.json schema, load/save, and first-run seeding from existing state."""

from __future__ import annotations

import hashlib
import json
import logging
import plistlib
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

CONFIG_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Device:
    name: str
    # UUID of the pair record on disk (remote_<this>.plist). This is also
    # the key tunneld uses to address the device. Stable across runs.
    pair_record_identifier: str
    # Apple-assigned UDID like "00008110-XXXXXXXXXXXXXXXX". Not present in
    # the pair record file itself — must be discovered at runtime by
    # querying `pymobiledevice3 lockdown info` through the tunnel. Starts
    # as None on fresh seeding and is backfilled during reconciliation.
    udid: str | None
    device_class: str  # "tvOS" | "iOS" | "iPadOS" | "unknown"
    paired_at: str
    pair_record_path: str | None = None
    product_type: str | None = None     # e.g. "AppleTV14,1"
    product_version: str | None = None  # e.g. "26.4"


@dataclass
class IPA:
    filename: str
    sha256: str
    original_bundle_id: str
    platform: str  # "tvOS", "iOS", "iPadOS"
    added_at: str
    # List of Device.pair_record_identifier values — the stable primary
    # key for devices in this app. At runtime we resolve each entry to a
    # Device object and use its .udid for Apple portal calls and its
    # .pair_record_identifier for tunneld lookups.
    target_devices: list[str] = field(default_factory=list)
    last_signed_at: str | None = None
    last_installed_at: str | None = None
    signed_bundle_id: str | None = None
    status: str = "pending"  # pending | ok | sign-failed | install-failed | app-id-limit
    consecutive_failures: int = 0
    last_error: str | None = None


@dataclass
class Settings:
    # 5-day cadence (not 6) gives us 48 hourly retry attempts before the
    # 7-day free-tier profile expires. The reason: iOS devices have
    # aggressive Wi-Fi power-save — the iPhone/iPad can be unreachable
    # for stretches of minutes-to-hours at a time even when "awake and
    # on Wi-Fi". A single refresh attempt can legitimately find every
    # iOS target offline; we need enough retry runway to eventually
    # catch all targets in a reachable state before the profile dies.
    refresh_interval_days: int = 5
    auto_refresh_paused: bool = False
    start_at_login: bool = True


@dataclass
class CertStatus:
    """Cached snapshot of the dev cert plumesign is using.

    This is a *display* value — the authoritative cert state lives in
    Apple's developer portal and is queried by refresh.py at the top of
    every refresh cycle. We persist the last-known result so the menubar
    can show a countdown without hitting the portal on every menu redraw.
    """
    certificate_id: str | None = None
    name: str | None = None
    expiration_date: str | None = None  # ISO8601 UTC string
    status: str = "unknown"              # ok | expiring | expired | revoked | missing | unknown
    checked_at: str | None = None        # when we last refreshed this snapshot


@dataclass
class Config:
    version: int = CONFIG_VERSION
    apple_id_email: str | None = None
    devices: list[Device] = field(default_factory=list)
    ipas: list[IPA] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    cert_status: CertStatus = field(default_factory=CertStatus)

    @classmethod
    def load(cls) -> Config:
        if not paths.CONFIG_FILE.exists():
            return cls()
        data = json.loads(paths.CONFIG_FILE.read_text())
        return cls._from_dict(data)

    def save(self) -> None:
        """Write config to disk atomically.

        Writes to a sibling temp file then renames over the target. On
        POSIX `os.rename` is atomic within the same directory, so
        readers never see a half-written file even if two threads save
        concurrently. Without this, concurrent saves (hourly refresh +
        iOS auto-detect worker) could interleave and corrupt the JSON.
        """
        paths.ensure_dirs()
        tmp = paths.CONFIG_FILE.with_name(paths.CONFIG_FILE.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, default=str))
        tmp.replace(paths.CONFIG_FILE)

    @classmethod
    def _from_dict(cls, data: dict) -> Config:
        devices = [Device(**d) for d in data.get("devices", [])]
        ipas = [IPA(**i) for i in data.get("ipas", [])]
        settings = Settings(**data.get("settings", {}))
        cert_status = CertStatus(**data.get("cert_status", {}))
        return cls(
            version=data.get("version", CONFIG_VERSION),
            apple_id_email=data.get("apple_id_email"),
            devices=devices,
            ipas=ipas,
            settings=settings,
            cert_status=cert_status,
        )

    def device_by_pair_record(self, pair_record_id: str) -> Device | None:
        return next(
            (d for d in self.devices if d.pair_record_identifier == pair_record_id),
            None,
        )

    def device_by_udid(self, udid: str) -> Device | None:
        return next((d for d in self.devices if d.udid == udid), None)

    def ipa_by_filename(self, filename: str) -> IPA | None:
        return next((i for i in self.ipas if i.filename == filename), None)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def plumesign_is_logged_in() -> str | None:
    """Return the email of the plumesign-saved account, or None if not logged in.

    plumesign's accounts.json looks like:
        {
          "selected_account": "user@example.com",
          "accounts": {"user@example.com": {"email": "...", "team_id": "..."}}
        }
    We return `selected_account` as the authoritative email.
    """
    if not paths.PLUMESIGN_ACCOUNTS_FILE.exists():
        return None
    try:
        data = json.loads(paths.PLUMESIGN_ACCOUNTS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    selected = data.get("selected_account")
    if isinstance(selected, str) and selected:
        return selected
    # Fallback: first key in the accounts dict
    accounts = data.get("accounts")
    if isinstance(accounts, dict) and accounts:
        return next(iter(accounts))
    return None


def plumesign_team_id() -> str | None:
    """Return the team_id of the currently-selected plumesign account."""
    if not paths.PLUMESIGN_ACCOUNTS_FILE.exists():
        return None
    try:
        data = json.loads(paths.PLUMESIGN_ACCOUNTS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    email = data.get("selected_account")
    accounts = data.get("accounts") or {}
    if isinstance(accounts, dict) and email in accounts:
        account = accounts[email]
        if isinstance(account, dict):
            return account.get("team_id")
    return None


def _preferred_backup_dir() -> Path:
    """Return the directory where we mirror pair records + config.

    Prefers iCloud Drive if it exists (so a Mac restore puts everything
    back); otherwise falls back to ~/Documents/Torch-Backup/. Pair
    records are the single hardest-to-recover piece of Torch state
    (the Apple TV won't issue a new PIN without physically cycling its
    pairing screen), so protecting them across Mac loss is worth the
    5 minutes of work.
    """
    icloud = (
        Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    )
    if icloud.exists() and icloud.is_dir():
        return icloud / "Torch" / "backup"
    return Path.home() / "Documents" / "Torch-Backup"


def backup_pair_records() -> int:
    """Copy ~/.pymobiledevice3/remote_*.plist to the backup directory.

    Returns the number of pair records copied. Idempotent: existing
    backups get overwritten only if the source mtime is newer. Swallows
    per-file errors so a single bad pair record doesn't abort the run.
    """
    if not paths.PYMD3_PAIR_RECORDS_DIR.exists():
        return 0
    dest_dir = _preferred_backup_dir() / "pair-records"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("could not create pair-record backup dir %s: %s", dest_dir, e)
        return 0

    copied = 0
    for src in paths.PYMD3_PAIR_RECORDS_DIR.glob("remote_*.plist"):
        dest = dest_dir / src.name
        try:
            if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
                continue
            shutil.copy2(src, dest)
            copied += 1
        except OSError as e:
            log.warning("failed to back up %s: %s", src.name, e)
    if copied:
        log.info("backed up %d pair record(s) to %s", copied, dest_dir)
    return copied


def seed_devices_from_pair_records() -> list[Device]:
    """Scan ~/.pymobiledevice3/ for existing pair records and return Devices."""
    if not paths.PYMD3_PAIR_RECORDS_DIR.exists():
        return []
    out: list[Device] = []
    for plist_path in sorted(paths.PYMD3_PAIR_RECORDS_DIR.glob("remote_*.plist")):
        # The pair record file only contains crypto material (private_key,
        # public_key) — no device UDID or friendly name. Everything useful
        # has to come from tunneld at runtime. For seeding, we capture the
        # identifier from the filename (which is the same key tunneld uses
        # to address the device) and leave udid/device_class to be filled
        # in later by the reconciliation pass in the pymd3 wrapper.
        pair_record_id = plist_path.stem.removeprefix("remote_")
        out.append(
            Device(
                name=pair_record_id,  # placeholder; replaced at reconciliation
                pair_record_identifier=pair_record_id,
                udid=None,
                device_class="unknown",
                paired_at=_now_iso(),
                pair_record_path=str(plist_path),
            )
        )
    return out


def _detect_ipa_platform(ipa_path: Path) -> tuple[str, str]:
    """Return (platform, original_bundle_id) by inspecting the IPA.

    Platform is one of "tvOS", "iOS", "iPadOS". Detection:
    - Unzip just Info.plist from the .app inside Payload/
    - CFBundleSupportedPlatforms contains "AppleTVOS" -> tvOS
    - UIDeviceFamily contains 3 (Apple TV) -> tvOS
    - UIDeviceFamily contains 2 (iPad) only -> iPadOS
    - Default -> iOS
    """
    import zipfile

    with zipfile.ZipFile(ipa_path) as zf:
        info_plist_name = next(
            (
                n
                for n in zf.namelist()
                if n.startswith("Payload/")
                and n.endswith(".app/Info.plist")
                and n.count("/") == 2
            ),
            None,
        )
        if not info_plist_name:
            raise ValueError(f"no Info.plist found in {ipa_path.name}")
        with zf.open(info_plist_name) as f:
            info = plistlib.load(f)

    bundle_id = info.get("CFBundleIdentifier", "unknown")
    supported = info.get("CFBundleSupportedPlatforms") or []
    family = info.get("UIDeviceFamily") or []

    if any("AppleTV" in p or "TVOS" in p.upper() for p in supported):
        return "tvOS", bundle_id
    if 3 in family:
        return "tvOS", bundle_id
    if family == [2]:
        return "iPadOS", bundle_id
    return "iOS", bundle_id


def copy_project_ipas_into_runtime() -> None:
    """Copy project-level ipas/*.ipa into the runtime Application Support
    folder. Idempotent — skips files that are already present. Runs on
    every startup, not just first run, so dropping a new IPA into the
    project's ipas/ folder (for dev workflow) picks it up too.
    """
    if not paths.PROJECT_IPAS_DIR.exists():
        return
    paths.IPAS_DIR.mkdir(parents=True, exist_ok=True)
    for src in sorted(paths.PROJECT_IPAS_DIR.glob("*.ipa")):
        dest = paths.IPAS_DIR / src.name
        if not dest.exists():
            log.info("copying project IPA %s -> %s", src, dest)
            shutil.copy2(src, dest)


def platform_matches_device(ipa_platform: str, device_class: str) -> bool:
    """Whether an IPA's platform is compatible with a device's class.

    Lives here (not refresh.py) because config-time auto-targeting needs
    it and refresh imports config — keeping the predicate here avoids a
    circular import. refresh.is_compatible delegates to this.
    """
    if ipa_platform == "tvOS":
        return device_class == "tvOS"
    return device_class in ("iOS", "iPadOS")


def _make_ipa_entry(ipa_file: Path, devices: list[Device]) -> IPA | None:
    """Create a new IPA entry for a freshly discovered file.

    Auto-targets every platform-compatible device. tvOS IPAs only target
    Apple TVs; iOS/iPadOS IPAs only target iPhones/iPads. Earlier versions
    targeted every device and relied on the refresh-time platform filter,
    but that left iPhones in tvOS IPAs' target lists, which confused both
    the UI and anyone reading config.json.
    """
    try:
        platform, bundle_id = _detect_ipa_platform(ipa_file)
    except Exception as e:  # noqa: BLE001
        log.warning("failed to inspect %s: %s", ipa_file, e)
        return None
    return IPA(
        filename=ipa_file.name,
        sha256=sha256_file(ipa_file),
        original_bundle_id=bundle_id,
        platform=platform,
        added_at=_now_iso(),
        target_devices=[
            d.pair_record_identifier
            for d in devices
            if platform_matches_device(platform, d.device_class)
        ],
    )


def sync_ipas_folder(cfg: Config) -> bool:
    """Reconcile cfg.ipas against what's actually in the runtime ipas/ folder.

    - Any .ipa file in the folder that isn't tracked gets a new IPA entry.
    - Any tracked IPA whose file has disappeared is removed from cfg.ipas
      (the signed cache is left alone; the refresh module will notice
      "missing source" if someone tries to refresh a deleted entry).

    Returns True if anything changed.
    """
    paths.IPAS_DIR.mkdir(parents=True, exist_ok=True)
    files_on_disk: dict[str, Path] = {
        p.name: p for p in paths.IPAS_DIR.glob("*.ipa")
    }
    tracked: dict[str, IPA] = {i.filename: i for i in cfg.ipas}

    changed = False

    # Remove tracked IPAs whose source file is gone.
    for name in list(tracked):
        if name not in files_on_disk:
            log.info("IPA %s no longer present on disk; untracking", name)
            tracked.pop(name)
            changed = True

    # Add new files.
    for name, path in files_on_disk.items():
        if name in tracked:
            continue
        entry = _make_ipa_entry(path, cfg.devices)
        if entry is None:
            continue
        log.info(
            "discovered new IPA %s (platform=%s, bundle=%s)",
            name,
            entry.platform,
            entry.original_bundle_id,
        )
        tracked[name] = entry
        changed = True

    if changed:
        cfg.ipas = sorted(tracked.values(), key=lambda i: i.filename)
    return changed


def bootstrap() -> Config:
    """Load config (or create a fresh one), seed devices / creds from
    existing Mac state, sync the IPAs folder with what's on disk, and
    return the resulting Config.

    Called on every app launch. Idempotent:
      - If config.json exists, we load it.
      - If it doesn't exist, we create a new one and seed apple_id +
        devices from the plumesign + pymobiledevice3 state directories.
      - Regardless of which path we took, we then sync_ipas_folder()
        against the runtime ipas/ directory so that any IPA files
        dropped in since the last run appear as tracked apps. This
        also recovers if config.json lost entries for any reason.

    We also copy project-level ipas/*.ipa into the runtime location on
    every startup so the dev workflow (stash .ipa files in the repo,
    run from source) keeps working without the user having to manually
    copy files.
    """
    paths.ensure_dirs()

    copy_project_ipas_into_runtime()

    if paths.CONFIG_FILE.exists():
        cfg = Config.load()
    else:
        log.info("no config found, creating new one")
        cfg = Config()

    # Seed / refresh the Apple ID email from plumesign state every run
    # so a logout-from-CLI is reflected without the user having to
    # edit config.json.
    plumesign_email = plumesign_is_logged_in()
    if plumesign_email:
        cfg.apple_id_email = plumesign_email

    # Seed devices from pair records IF the config has no devices yet.
    # On subsequent runs we leave the stored devices alone — their
    # identifiers are stable, and the pymd3 reconciliation pass will
    # update name / udid / device_class / product info at runtime.
    if not cfg.devices:
        cfg.devices = seed_devices_from_pair_records()
        log.info("seeded %d device(s) from pair records", len(cfg.devices))

    # Sync IPAs from the runtime folder every run.
    if sync_ipas_folder(cfg):
        log.info("IPAs folder changed; config updated")

    # Mirror pair records to iCloud Drive (or ~/Documents) so a Mac
    # loss / reinstall doesn't force you to re-pair every device from
    # scratch. Best-effort; errors are logged but don't block startup.
    try:
        backup_pair_records()
    except Exception as e:  # noqa: BLE001
        log.warning("pair record backup failed: %s", e)

    cfg.save()
    return cfg
