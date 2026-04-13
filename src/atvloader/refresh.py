"""Refresh orchestrator.

The functional heart of the app. Given a Config, refreshes every tracked
IPA for every compatible target device by:

  1. Reconciling seeded devices against tunneld (fills real UDIDs / class)
  2. Ensuring each target device UDID is registered with the Apple portal
  3. Signing each IPA once per platform it targets
  4. Installing the signed result to each compatible target via the tunnel
  5. Updating config.json with last_signed_at / last_installed_at / status

Thread-safe: a module-level lock serializes refresh runs so the hourly
timer and the manual "Refresh Now" button can't collide.

Error classification:
  - PlumesignNotLoggedInError  -> status "needs-login", stop refresh entirely
  - PlumesignAppIdLimitError   -> status "app-id-limit", skip IPA, notify
  - PlumesignAuthError         -> status "auth-error", retry at next tick
  - PlumesignSignError         -> status "sign-failed", increment failures
  - TunneldDownError           -> status "tunneld-down", stop refresh entirely
  - InstallError               -> status "install-failed", increment failures

Three strikes and the IPA is frozen until the user intervenes.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import paths, plumesign, pymd3
from .config import Config, Device, IPA

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

_refresh_lock = threading.Lock()

MAX_CONSECUTIVE_FAILURES = 3


class RefreshAborted(Exception):
    """Raised when a refresh stops for a reason that applies to the whole run
    (tunneld down, not logged in) — distinct from per-IPA errors that let the
    refresh continue with other IPAs."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# --- Platform compatibility --------------------------------------------------


def is_compatible(ipa_platform: str, device_class: str) -> bool:
    """Can this IPA run on this device class?

    tvOS is strict: only tvOS devices. iOS and iPadOS are treated as
    interchangeable because the provisioning profile shape is the same
    (both use the default /ios/ endpoint with no subPlatform).
    """
    if ipa_platform == "tvOS":
        return device_class == "tvOS"
    return device_class in ("iOS", "iPadOS")


def force_tvos_flag(ipa_platform: str) -> bool:
    """Whether to pass PLUME_FORCE_TVOS=1 when signing this IPA."""
    return ipa_platform == "tvOS"


# --- Refresh freshness -------------------------------------------------------


def needs_refresh(ipa: IPA, interval_days: int, now: datetime | None = None) -> bool:
    """True if the IPA has never been signed or was signed >= interval_days ago."""
    if ipa.last_signed_at is None:
        return True
    signed_at = _parse_iso(ipa.last_signed_at)
    if signed_at is None:
        return True
    if now is None:
        now = _now()
    age = now - signed_at
    return age >= timedelta(days=interval_days)


def is_frozen(ipa: IPA) -> bool:
    """True if the IPA has hit the retry cap and should be skipped until
    the user intervenes."""
    return ipa.consecutive_failures >= MAX_CONSECUTIVE_FAILURES


# --- Device reconciliation ---------------------------------------------------


def reconcile_devices(cfg: Config) -> Config:
    """Reconcile all devices against tunneld. Persists the result to config.json.

    Failures (device offline, tunneld down for one device) are logged and
    leave that device's record unchanged.
    """
    cfg.devices = pymd3.reconcile_all(cfg.devices)
    cfg.save()
    return cfg


# --- Core refresh logic ------------------------------------------------------


def _signed_ipa_path(ipa: IPA) -> Path:
    """Where to write the signed output for an IPA."""
    stem = Path(ipa.filename).stem
    return paths.SIGNED_DIR / f"{stem}-{ipa.platform}.ipa"


def _source_ipa_path(ipa: IPA) -> Path:
    return paths.IPAS_DIR / ipa.filename


def _compatible_devices(
    ipa: IPA, cfg: Config
) -> list[Device]:
    """Return the subset of this IPA's target devices that are both
    reconciled (have a UDID) and compatible with the IPA's platform."""
    out: list[Device] = []
    for pair_id in ipa.target_devices:
        device = cfg.device_by_pair_record(pair_id)
        if device is None:
            log.warning(
                "IPA %s targets unknown device %s", ipa.filename, pair_id
            )
            continue
        if device.udid is None or device.device_class == "unknown":
            log.warning(
                "IPA %s targets unreconciled device %s (offline?)",
                ipa.filename,
                pair_id,
            )
            continue
        if not is_compatible(ipa.platform, device.device_class):
            log.debug(
                "skipping %s on %s: IPA platform %s is not compatible "
                "with device class %s",
                ipa.filename,
                device.name,
                ipa.platform,
                device.device_class,
            )
            continue
        out.append(device)
    return out


def _ensure_devices_registered(devices: list[Device]) -> None:
    """Register each device's UDID with Apple. Idempotent-ish: we swallow
    errors so "already registered" doesn't abort the run. If a device was
    never registered and the call truly failed, signing will surface the
    real problem downstream with a clearer message."""
    for d in devices:
        if not d.udid:
            continue
        try:
            plumesign.register_device(d.udid, d.name)
        except plumesign.PlumesignError as e:
            log.debug(
                "register_device(%s, %s) returned error (likely already "
                "registered): %s",
                d.udid,
                d.name,
                e,
            )


def _record_success(ipa: IPA) -> None:
    ipa.last_signed_at = _now_iso()
    ipa.last_installed_at = _now_iso()
    ipa.status = "ok"
    ipa.consecutive_failures = 0
    ipa.last_error = None


def _record_failure(ipa: IPA, status: str, error: str) -> None:
    ipa.status = status
    ipa.consecutive_failures += 1
    ipa.last_error = error[:500]


def refresh_one(
    ipa: IPA,
    cfg: Config,
    *,
    progress: ProgressCallback | None = None,
) -> bool:
    """Sign + install a single IPA. Returns True on success, False on failure.

    Callers must hold no lock; this function is designed to be called from
    refresh_all() which already owns _refresh_lock.
    """
    def emit(msg: str) -> None:
        log.info("[%s] %s", ipa.filename, msg)
        if progress is not None:
            progress(f"{ipa.filename}: {msg}")

    compatible = _compatible_devices(ipa, cfg)
    if not compatible:
        _record_failure(ipa, "no-targets", "no compatible target devices")
        emit("no compatible targets; skipping")
        return False

    source = _source_ipa_path(ipa)
    if not source.exists():
        _record_failure(ipa, "missing-source", f"source IPA not found: {source}")
        emit(f"source IPA missing at {source}")
        return False

    # 1. Register every target device with the Apple portal (idempotent).
    emit(f"registering {len(compatible)} target device(s)")
    _ensure_devices_registered(compatible)

    # 2. Sign the IPA once for its platform.
    signed_path = _signed_ipa_path(ipa)
    try:
        emit(f"signing ({ipa.platform})")
        plumesign.sign_ipa(
            source,
            signed_path,
            force_tvos=force_tvos_flag(ipa.platform),
        )
        ipa.signed_bundle_id = _read_signed_bundle_id(signed_path)
    except plumesign.PlumesignNotLoggedInError as e:
        _record_failure(ipa, "needs-login", str(e))
        emit("not logged in; aborting run")
        raise RefreshAborted("plumesign session missing") from e
    except plumesign.PlumesignAppIdLimitError as e:
        _record_failure(ipa, "app-id-limit", str(e))
        emit("Apple app-ID weekly limit reached")
        return False
    except plumesign.PlumesignAuthError as e:
        _record_failure(ipa, "auth-error", str(e))
        emit(f"auth error: {e}")
        return False
    except plumesign.PlumesignError as e:
        _record_failure(ipa, "sign-failed", str(e))
        emit(f"sign failed: {e}")
        return False

    # 3. Install to every compatible target.
    all_installs_ok = True
    for device in compatible:
        try:
            tunnel = pymd3.tunnel_for_pair_id(device.pair_record_identifier)
        except pymd3.TunneldDownError as e:
            _record_failure(ipa, "tunneld-down", str(e))
            emit("tunneld went down mid-refresh")
            raise RefreshAborted("tunneld down") from e
        if tunnel is None:
            log.warning(
                "target %s has no active tunnel; skipping install",
                device.name,
            )
            all_installs_ok = False
            continue
        addr, port = tunnel
        try:
            emit(f"installing to {device.name}")
            pymd3.install_ipa(
                addr,
                port,
                signed_path,
                terminate_bundle_id=ipa.signed_bundle_id,
            )
        except pymd3.InstallError as e:
            _record_failure(ipa, "install-failed", str(e))
            emit(f"install to {device.name} failed: {e}")
            all_installs_ok = False

    if all_installs_ok:
        _record_success(ipa)
        emit("done")
        return True
    return False


def _read_signed_bundle_id(ipa_path: Path) -> str | None:
    """Read CFBundleIdentifier from the signed IPA's Info.plist."""
    import plistlib
    import zipfile

    try:
        with zipfile.ZipFile(ipa_path) as zf:
            info_name = next(
                (
                    n
                    for n in zf.namelist()
                    if n.startswith("Payload/")
                    and n.endswith(".app/Info.plist")
                    and n.count("/") == 2
                ),
                None,
            )
            if not info_name:
                return None
            with zf.open(info_name) as f:
                return plistlib.load(f).get("CFBundleIdentifier")
    except (zipfile.BadZipFile, KeyError, plistlib.InvalidFileException):
        return None


# --- Top-level orchestrator --------------------------------------------------


def refresh_all(
    cfg: Config,
    *,
    force: bool = False,
    only: list[str] | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[int, int]:
    """Refresh every tracked IPA that needs it. Returns (succeeded, failed).

    If `only` is a list of filenames, only those IPAs are considered —
    useful for the per-app "Refresh now" button. `force=True` bypasses
    the freshness check so a manually-requested refresh always runs
    (subject to the frozen-after-3-failures cap).

    Only one refresh runs at a time. Concurrent calls (e.g. hourly timer
    fires while a manual Refresh Now is mid-run) no-op on the second
    entry and return (0, 0).
    """
    if not _refresh_lock.acquire(blocking=False):
        log.info("refresh already in progress; skipping duplicate call")
        return (0, 0)

    try:
        return _refresh_all_locked(
            cfg, force=force, only=only, progress=progress
        )
    finally:
        _refresh_lock.release()


def _refresh_all_locked(
    cfg: Config,
    *,
    force: bool,
    only: list[str] | None,
    progress: ProgressCallback | None,
) -> tuple[int, int]:
    def emit(msg: str) -> None:
        log.info(msg)
        if progress is not None:
            progress(msg)

    # Guard rails
    if cfg.settings.auto_refresh_paused and not force:
        emit("auto-refresh is paused; skipping")
        return (0, 0)

    if not plumesign.is_logged_in():
        emit("plumesign not logged in; cannot refresh")
        raise RefreshAborted("not logged in")

    if not pymd3.is_tunneld_up():
        emit("tunneld is down; cannot refresh")
        raise RefreshAborted("tunneld down")

    # Reconcile devices so we know platform/UDID of every target.
    emit("reconciling devices against tunneld")
    cfg = reconcile_devices(cfg)

    interval = cfg.settings.refresh_interval_days
    only_set = set(only) if only is not None else None
    candidates = [
        ipa
        for ipa in cfg.ipas
        if (only_set is None or ipa.filename in only_set)
        and not is_frozen(ipa)
        and (force or needs_refresh(ipa, interval))
    ]
    if not candidates:
        emit("nothing to refresh")
        return (0, 0)

    emit(f"refreshing {len(candidates)} IPA(s)")

    succeeded = 0
    failed = 0
    try:
        for ipa in candidates:
            try:
                ok = refresh_one(ipa, cfg, progress=progress)
            except RefreshAborted:
                # Whole-run abort — stop processing further IPAs but save
                # whatever we managed to record on the current one.
                cfg.save()
                raise
            if ok:
                succeeded += 1
            else:
                failed += 1
            cfg.save()  # persist status after each IPA
    finally:
        cfg.save()

    emit(f"refresh complete: {succeeded} succeeded, {failed} failed")
    return (succeeded, failed)
