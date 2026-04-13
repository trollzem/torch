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
from .config import CertStatus, Config, Device, IPA

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

_refresh_lock = threading.Lock()

MAX_CONSECUTIVE_FAILURES = 3

# Free Apple ID per-device capacity. Apple limits a free Personal Team
# certificate to 3 simultaneously-installed apps on a single device; when
# a 4th is installed, the oldest gets silently invalidated (developer
# apps get "untrusted" on the device and stop launching). We refuse to
# install the 4th rather than trip this invisible edge.
FREE_TIER_DEVICE_APP_CAP = 3

# Warn the user when the dev cert is within this many days of expiring.
# Certs are valid for 364 days on free accounts; we start alerting at
# 14 days left so there's a comfortable window to re-login and rotate.
CERT_EXPIRY_WARNING_DAYS = 14


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


def refresh_cert_status(cfg: Config) -> CertStatus:
    """Query Apple's developer portal for the current cert and update
    cfg.cert_status in place.

    Runs once per refresh cycle (cheap — one HTTP call). Produces one of:
      - "ok"       : cert exists and expires more than CERT_EXPIRY_WARNING_DAYS away
      - "expiring" : cert exists but expires within the warning window
      - "expired"  : cert exists but expiration_date is in the past
      - "revoked"  : cert exists but status != "Issued"
      - "missing"  : Apple's portal has no issued cert for this team
      - "unknown"  : query failed (network, auth, parsing) — leaves the
                     previous snapshot in place so the menubar keeps
                     showing stale-but-useful data

    Never raises. The caller decides what to do with the status (the
    refresh orchestrator soft-fails on "expired" and "missing" since
    the next sign would produce a broken IPA).
    """
    try:
        cert = plumesign.current_cert()
    except plumesign.PlumesignError as e:
        log.warning("cert status query failed: %s", e)
        return cfg.cert_status  # preserve last-known
    except Exception as e:  # noqa: BLE001
        log.exception("unexpected error in cert status query: %s", e)
        return cfg.cert_status

    now = _now()
    checked_at = _now_iso()

    if cert is None:
        cfg.cert_status = CertStatus(
            certificate_id=None,
            name=None,
            expiration_date=None,
            status="missing",
            checked_at=checked_at,
        )
        return cfg.cert_status

    if cert.status.lower() != "issued":
        state = "revoked"
    elif cert.expiration_date <= now:
        state = "expired"
    elif (cert.expiration_date - now).days < CERT_EXPIRY_WARNING_DAYS:
        state = "expiring"
    else:
        state = "ok"

    cfg.cert_status = CertStatus(
        certificate_id=cert.certificate_id,
        name=cert.name,
        expiration_date=cert.expiration_date.isoformat(),
        status=state,
        checked_at=checked_at,
    )
    return cfg.cert_status


def count_active_apps_on_device(cfg: Config, device: Device) -> int:
    """How many tracked IPAs are currently targeted at (and platform-
    compatible with) the given device.

    This approximates "apps the free Personal Team cert is signing on
    this device" — the number Apple caps at FREE_TIER_DEVICE_APP_CAP.
    Counts only IPAs that are compatible with the device's platform.
    """
    return sum(
        1
        for ipa in cfg.ipas
        if device.pair_record_identifier in ipa.target_devices
        and is_compatible(ipa.platform, device.device_class)
    )


def device_has_room(cfg: Config, device: Device, *, including: IPA) -> bool:
    """True if installing `including` on `device` would stay within the
    free-tier 3-app cap. If `including` is already tracked for this
    device, a refresh of it doesn't count toward the cap (we're not
    adding a new slot).
    """
    existing = count_active_apps_on_device(cfg, device)
    already_tracked = (
        device.pair_record_identifier in including.target_devices
        and is_compatible(including.platform, device.device_class)
    )
    projected = existing if already_tracked else existing + 1
    return projected <= FREE_TIER_DEVICE_APP_CAP


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

    # Enforce the free Apple ID 3-apps-per-device cap. This is separate
    # from the 10-app-IDs-per-week team-level limit — it's a per-device
    # ceiling on simultaneously-trusted free-signed apps. Filter out any
    # device that would go over 3 if we installed this IPA. If ALL
    # targets get filtered out, treat it as a soft failure with a
    # distinct status so the user knows to remove something rather than
    # chasing a different problem.
    over_cap: list[str] = []
    within_cap: list[Device] = []
    for d in compatible:
        if device_has_room(cfg, d, including=ipa):
            within_cap.append(d)
        else:
            over_cap.append(d.name)

    if over_cap and not within_cap:
        msg = (
            f"device {over_cap[0]} already has "
            f"{FREE_TIER_DEVICE_APP_CAP} apps (free-tier cap); "
            f"remove one from tracking before adding this IPA"
        )
        _record_failure(ipa, "device-full", msg)
        emit(msg)
        return False
    if over_cap:
        emit(
            f"skipping {', '.join(over_cap)} (would exceed 3-app cap); "
            f"refreshing on {', '.join(d.name for d in within_cap)}"
        )
    compatible = within_cap

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

    # Check the dev cert on Apple's portal. If it's expired or revoked,
    # stop the whole run — the next sign would produce an IPA that
    # installs cleanly but gets rejected by the device at launch time.
    emit("checking developer certificate status")
    status = refresh_cert_status(cfg)
    if status.status in ("expired", "revoked", "missing"):
        msg = (
            f"developer certificate is {status.status}; refresh aborted. "
            f"Re-run `plumesign account login` to rotate."
        )
        emit(msg)
        cfg.save()
        raise RefreshAborted(msg)
    if status.status == "expiring":
        emit(
            f"⚠️ dev cert expires {status.expiration_date} (status=expiring)"
        )

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
