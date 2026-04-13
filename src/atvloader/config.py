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
    # Apple-assigned UDID like "00008110-000E59EC3E41801E". Not present in
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
    refresh_interval_days: int = 6
    auto_refresh_paused: bool = False
    start_at_login: bool = True


@dataclass
class Config:
    version: int = CONFIG_VERSION
    apple_id_email: str | None = None
    devices: list[Device] = field(default_factory=list)
    ipas: list[IPA] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)

    @classmethod
    def load(cls) -> Config:
        if not paths.CONFIG_FILE.exists():
            return cls()
        data = json.loads(paths.CONFIG_FILE.read_text())
        return cls._from_dict(data)

    def save(self) -> None:
        paths.ensure_dirs()
        paths.CONFIG_FILE.write_text(
            json.dumps(asdict(self), indent=2, default=str)
        )

    @classmethod
    def _from_dict(cls, data: dict) -> Config:
        devices = [Device(**d) for d in data.get("devices", [])]
        ipas = [IPA(**i) for i in data.get("ipas", [])]
        settings = Settings(**data.get("settings", {}))
        return cls(
            version=data.get("version", CONFIG_VERSION),
            apple_id_email=data.get("apple_id_email"),
            devices=devices,
            ipas=ipas,
            settings=settings,
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


def seed_ipas_from_project_dir() -> list[IPA]:
    """Copy project-level ipas/ files into Application Support and return IPAs.

    This runs on first app launch so the spike's YouTube/Streamer show up
    as tracked apps without the user having to re-add them.
    """
    if not paths.PROJECT_IPAS_DIR.exists():
        return []
    paths.IPAS_DIR.mkdir(parents=True, exist_ok=True)

    out: list[IPA] = []
    for src in sorted(paths.PROJECT_IPAS_DIR.glob("*.ipa")):
        dest = paths.IPAS_DIR / src.name
        if not dest.exists():
            log.info("seeding IPA %s -> %s", src, dest)
            shutil.copy2(src, dest)
        try:
            platform, bundle_id = _detect_ipa_platform(dest)
        except Exception as e:  # noqa: BLE001
            log.warning("failed to inspect %s: %s", dest, e)
            continue
        out.append(
            IPA(
                filename=dest.name,
                sha256=sha256_file(dest),
                original_bundle_id=bundle_id,
                platform=platform,
                added_at=_now_iso(),
            )
        )
    return out


def bootstrap() -> Config:
    """Load config, or create + seed one from existing Mac state.

    Called on every app launch. Idempotent. If config.json already exists,
    we load it and return unchanged — seeding only runs on a truly empty
    state (fresh install on a Mac that already has plumesign + pair records
    set up from the spike, which is exactly the user's situation today).
    """
    paths.ensure_dirs()

    if paths.CONFIG_FILE.exists():
        return Config.load()

    log.info("no config found, bootstrapping from existing state")
    cfg = Config()

    plumesign_email = plumesign_is_logged_in()
    if plumesign_email:
        log.info("detected plumesign session for %s", plumesign_email)
        cfg.apple_id_email = plumesign_email

    cfg.devices = seed_devices_from_pair_records()
    log.info("seeded %d device(s) from pair records", len(cfg.devices))

    cfg.ipas = seed_ipas_from_project_dir()
    log.info("seeded %d IPA(s) from project directory", len(cfg.ipas))

    # Auto-target every seeded IPA at every seeded device.
    for ipa in cfg.ipas:
        ipa.target_devices = [d.pair_record_identifier for d in cfg.devices]

    cfg.save()
    return cfg
