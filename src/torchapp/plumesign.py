"""plumesign subprocess wrapper.

Handles:
  - interactive login with 2FA via pexpect
  - non-interactive signing with the staging-directory scrape workaround
    for plumesign v2.2.3's archive bug
  - device registration and app-ID listing for budget tracking
  - platform routing via the PLUME_FORCE_TVOS env var (our patch)

All plumesign invocations go through `_run_plumesign()` or
`_spawn_plumesign_interactive()` so we have one place to set PATH and
env vars consistently.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import pexpect

from . import paths

log = logging.getLogger(__name__)

# Environment variable toggles we use
ENV_PLUME_FORCE_TVOS = "PLUME_FORCE_TVOS"
ENV_PLUME_DELETE_AFTER_FINISHED = "PLUME_DELETE_AFTER_FINISHED"


# --- Exceptions --------------------------------------------------------------


class PlumesignError(Exception):
    """Base class for all plumesign wrapper failures."""


class PlumesignNotLoggedInError(PlumesignError):
    """No saved plumesign account; login is needed."""


class PlumesignAuthError(PlumesignError):
    """Apple ID authentication failed (bad password, 2FA rejected, anisette down, etc.)."""


class PlumesignAppIdLimitError(PlumesignError):
    """Apple's 10-app-ID-per-7-days free-tier limit reached."""


class PlumesignSignError(PlumesignError):
    """Generic signing failure (bundle inspection error, cert missing, etc.)."""


class PlumesignInstallError(PlumesignError):
    """plumesign binary itself is missing or unusable."""


# --- Data shapes -------------------------------------------------------------


@dataclass
class RegisteredDevice:
    """A device as reported by Apple's developer portal (plumesign account devices)."""
    device_id: str          # Apple's internal ID (e.g. "2LGWFCR2DX")
    name: str
    udid: str               # "00008110-XXXXXXXXXXXXXXXX"
    device_platform: str    # "ios" (always, even for tvOS devices)
    device_class: str       # "tvOS" | "iPhone" | "iPad" | etc.


@dataclass
class AppIdInfo:
    """An app ID as reported by Apple's developer portal."""
    app_id_id: str
    identifier: str         # "com.google.ios.youtube.TEAMID"
    name: str


@dataclass
class CertInfo:
    """A development certificate as reported by Apple's portal."""
    certificate_id: str           # e.g. "G24N6XD9U6"
    name: str                     # e.g. "iOS Development: your-apple-id@example.com"
    serial_number: str            # hex
    status: str                   # "Issued" | "Revoked" | ...
    expiration_date: datetime     # UTC
    machine_name: str | None      # e.g. "AltStore"


# --- Core subprocess helpers -------------------------------------------------


def _ensure_binary() -> Path:
    if not paths.PLUMESIGN_BINARY.exists():
        raise PlumesignInstallError(
            f"plumesign binary not found at {paths.PLUMESIGN_BINARY}. "
            f"Rebuild it from vendor/impactor-tvos.patch."
        )
    if not os.access(paths.PLUMESIGN_BINARY, os.X_OK):
        raise PlumesignInstallError(
            f"plumesign binary at {paths.PLUMESIGN_BINARY} is not executable."
        )
    return paths.PLUMESIGN_BINARY


def _env(*, force_tvos: bool = False, preserve_staging: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    if force_tvos:
        env[ENV_PLUME_FORCE_TVOS] = "1"
    if preserve_staging:
        env[ENV_PLUME_DELETE_AFTER_FINISHED] = "1"
    return env


def _run_plumesign(
    args: list[str],
    *,
    force_tvos: bool = False,
    preserve_staging: bool = False,
    timeout: float = 120.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run plumesign non-interactively and return the completed process."""
    binary = _ensure_binary()
    cmd = [str(binary), *args]
    log.debug("plumesign run: %s (force_tvos=%s)", " ".join(args), force_tvos)
    result = subprocess.run(
        cmd,
        env=_env(force_tvos=force_tvos, preserve_staging=preserve_staging),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        log.error(
            "plumesign failed (exit=%d): args=%s\nstderr tail:\n%s",
            result.returncode,
            args,
            _tail(result.stderr, 40),
        )
        _classify_failure_and_raise(result.stderr)
        # _classify always raises, but fall through for safety
        raise PlumesignError(f"plumesign {args[0]} failed (exit={result.returncode})")
    return result


def _tail(text: str, lines: int) -> str:
    parts = text.splitlines()
    return "\n".join(parts[-lines:])


def _classify_failure_and_raise(stderr: str) -> None:
    """Inspect stderr and raise the most specific exception we can."""
    lower = stderr.lower()
    if "maximum" in lower and "app id" in lower:
        raise PlumesignAppIdLimitError(_tail(stderr, 10))
    if "no accounts" in lower or "no account selected" in lower:
        raise PlumesignNotLoggedInError(_tail(stderr, 10))
    if "authentication" in lower or "unauthorized" in lower or "srp" in lower:
        raise PlumesignAuthError(_tail(stderr, 10))
    if "unexpectedendofeventstream" in lower or "plist error" in lower:
        # Apple returned empty / non-plist — usually means session drift
        # or an endpoint that doesn't exist for this account tier.
        raise PlumesignAuthError(_tail(stderr, 10))
    raise PlumesignSignError(_tail(stderr, 20))


# --- High-level API ----------------------------------------------------------


def is_logged_in() -> bool:
    """Return True if plumesign has a saved session on disk."""
    return paths.PLUMESIGN_ACCOUNTS_FILE.exists()


def login(
    email: str,
    password: str,
    tfa_callback: Callable[[], str],
    *,
    timeout: float = 120.0,
) -> None:
    """Run `plumesign account login` interactively, calling tfa_callback when
    plumesign prompts for a 2FA code.

    `tfa_callback` is called exactly once when the "Enter 2FA code: " prompt
    appears. It should return the 6-digit code as a string (whitespace is
    stripped automatically).

    Raises PlumesignAuthError on any login failure, PlumesignInstallError
    if the binary is missing.
    """
    binary = _ensure_binary()
    log.info("starting plumesign account login for %s", email)

    child = pexpect.spawn(
        str(binary),
        ["account", "login", "-u", email, "-p", password],
        env=_env(),
        encoding="utf-8",
        timeout=timeout,
    )

    # plumesign uses env_logger which goes to stderr with timestamped
    # INFO prefixes. pexpect.spawn merges stdout and stderr by default.
    patterns = [
        r"Enter 2FA code:",           # 0 - prompt for code
        r"Successfully logged in",    # 1 - success
        pexpect.EOF,                  # 2 - died without success marker
        pexpect.TIMEOUT,              # 3 - stuck
    ]

    try:
        idx = child.expect(patterns)
        if idx == 0:
            code = tfa_callback().strip()
            if not code:
                raise PlumesignAuthError("empty 2FA code provided")
            log.info("sending 2FA code to plumesign")
            child.sendline(code)
            # After 2FA, wait for the success marker or EOF.
            inner = child.expect(
                [r"Successfully logged in", pexpect.EOF, pexpect.TIMEOUT]
            )
            if inner != 0:
                raise PlumesignAuthError(
                    f"2FA rejected or login never completed: "
                    f"{(child.before or '')[-500:]}"
                )
        elif idx == 1:
            # Logged in without ever asking for 2FA (already-trusted session reuse).
            pass
        elif idx == 2:
            raise PlumesignAuthError(
                f"plumesign exited before 2FA prompt: {(child.before or '')[-500:]}"
            )
        else:
            raise PlumesignAuthError("timed out waiting for 2FA prompt")
    finally:
        try:
            child.close()
        except Exception:  # noqa: BLE001
            pass

    log.info("plumesign login succeeded")


def register_device(udid: str, name: str) -> None:
    """Register a device UDID with the Apple developer portal.

    Idempotent on Apple's side — re-registering an existing device returns
    the same record. Apple auto-detects device_class from the UDID's chip
    prefix (e.g. 00008110 -> tvOS), so this works for both iOS and tvOS
    devices via the /ios/addDevice endpoint.
    """
    log.info("registering device %s (%s) with Apple portal", name, udid)
    _run_plumesign(
        ["account", "register-device", "--udid", udid, "--name", name],
        timeout=60,
    )


def list_app_ids() -> list[AppIdInfo]:
    """Return the list of app IDs currently in the team.

    plumesign's `account app-ids` command uses the new /v1/bundleIds API
    and prints a Rust Debug-formatted Vec<AppID> to stderr. The format is:

        AppID {
            id: "89Q828849P",
            attributes: AppIDAttributes {
                identifier: "com.google.ios.youtube.TEAMID",
                ...
                name: "YouTubeUnstable",
                ...
            },
        },

    We regex-match each AppID block and pull out id, identifier, name.
    Caller uses the count for the 10-per-week budget display; individual
    identifiers are only used for debugging.
    """
    result = _run_plumesign(["account", "app-ids"], check=False)
    if result.returncode != 0:
        _classify_failure_and_raise(result.stderr)
    text = result.stdout + result.stderr

    records: list[AppIdInfo] = []
    # Each record starts with "AppID {" and contains nested attributes.
    # Non-greedy match between "AppID {" and the next "AppID {" or end.
    block_re = re.compile(
        r'AppID\s*\{\s*id:\s*"(?P<id>[^"]+)"'
        r'[^{]*attributes:\s*AppIDAttributes\s*\{'
        r'[^}]*?identifier:\s*"(?P<identifier>[^"]+)"'
        r'[^}]*?name:\s*"(?P<name>[^"]+)"',
        re.DOTALL,
    )
    for match in block_re.finditer(text):
        records.append(
            AppIdInfo(
                app_id_id=match.group("id"),
                identifier=match.group("identifier"),
                name=match.group("name"),
            )
        )
    log.debug("parsed %d app IDs from plumesign output", len(records))
    return records


def list_certs() -> list[CertInfo]:
    """Return development certificates on Apple's developer portal.

    Parses plumesign's `account certificates` Rust-Debug output. Records
    with unparseable expiration dates or missing required fields are
    skipped (logged at debug).
    """
    result = _run_plumesign(["account", "certificates"], check=False)
    if result.returncode != 0:
        _classify_failure_and_raise(result.stderr)
    text = result.stdout + result.stderr

    # Example debug-printed record fragment:
    #   name: "iOS Development: Hazem Eissa",
    #   certificate_id: "G24N6XD9U6",
    #   serial_number: "3E8019E8...",
    #   status: "Issued",
    #   expiration_date: 2027-04-12T23:21:56Z,
    #   ...
    #   machine_name: Some(
    #       "AltStore",
    #   ),
    block_re = re.compile(
        r'name:\s*"(?P<name>[^"]+)"\s*,'
        r'[^}]*?certificate_id:\s*"(?P<cid>[^"]+)"\s*,'
        r'[^}]*?serial_number:\s*"(?P<serial>[^"]+)"\s*,'
        r'[^}]*?status:\s*"(?P<status>[^"]+)"\s*,'
        r'[^}]*?expiration_date:\s*(?P<exp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)'
        r'[^}]*?(?:machine_name:\s*(?:None|Some\(\s*"(?P<machine>[^"]+)"))?',
        re.DOTALL,
    )
    out: list[CertInfo] = []
    for m in block_re.finditer(text):
        try:
            exp = datetime.strptime(m.group("exp"), "%Y-%m-%dT%H:%M:%SZ")
            # Apple returns naive UTC; mark it as such.
            from datetime import timezone as _tz
            exp = exp.replace(tzinfo=_tz.utc)
        except ValueError as e:
            log.debug("failed to parse cert expiration %r: %s", m.group("exp"), e)
            continue
        out.append(
            CertInfo(
                certificate_id=m.group("cid"),
                name=m.group("name"),
                serial_number=m.group("serial"),
                status=m.group("status"),
                expiration_date=exp,
                machine_name=m.group("machine"),
            )
        )
    log.debug("parsed %d certs from plumesign output", len(out))
    return out


def current_cert() -> CertInfo | None:
    """Return the cert plumesign is likely to use for signing.

    Heuristic: the most-distant expiration among issued (non-revoked)
    certificates. This matches plumesign's own internal selection, which
    prefers the newest valid cert. Returns None if there are no issued
    certificates on the account (which means the next sign will either
    create one or fail noisily).
    """
    certs = list_certs()
    issued = [c for c in certs if c.status.lower() == "issued"]
    if not issued:
        return None
    return max(issued, key=lambda c: c.expiration_date)


# --- Signing pipeline --------------------------------------------------------


# Regex for plumesign's "writing signed main executable to ..." log line.
# The path looks like:
#   /var/folders/zt/fy.../T/plume_stage_<UUID>/Payload/<App>.app/<exe>
# We capture up to plume_stage_<UUID> (the staging root).
_STAGING_DIR_RE = re.compile(
    r"writing signed main executable to "
    r"(?P<stage>/var/folders/[^\s]+?/plume_stage_[0-9A-Fa-f-]+)/Payload/"
)


def _find_staging_dir_from_stderr(stderr: str) -> Path | None:
    match = _STAGING_DIR_RE.search(stderr)
    if match:
        return Path(match.group("stage"))
    return None


def _find_staging_dir_by_glob(max_age_seconds: float = 60.0) -> Path | None:
    """Fallback: find the newest plume_stage_* dir modified recently."""
    candidates: list[tuple[float, Path]] = []
    root = Path("/var/folders")
    if not root.exists():
        return None
    # /var/folders/<2>/<rest>/T/plume_stage_*
    for user_dir in root.iterdir():
        for sub in user_dir.iterdir() if user_dir.is_dir() else []:
            tmp_dir = sub / "T"
            if not tmp_dir.exists():
                continue
            for stage in tmp_dir.glob("plume_stage_*"):
                try:
                    mtime = stage.stat().st_mtime
                except OSError:
                    continue
                if time.time() - mtime <= max_age_seconds:
                    candidates.append((mtime, stage))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _rezip_staging_to_ipa(stage_dir: Path, output_ipa: Path) -> None:
    """Zip <stage_dir>/Payload into output_ipa using the `zip` CLI.

    We use `zip -r -y -q` rather than Python's zipfile because:
      1. zipfile does not preserve symlinks natively — some frameworks
         need this and silently break otherwise.
      2. The spike validated `zip -y` produces IPAs Apple TV accepts.
      3. Less code, less surface area.
    """
    payload = stage_dir / "Payload"
    if not payload.exists():
        raise PlumesignSignError(f"staging dir has no Payload: {stage_dir}")

    # Absolute output path — we chdir into the staging dir so that "Payload"
    # in the zip is a top-level name rather than a long absolute prefix.
    absolute_output = output_ipa.resolve()
    absolute_output.parent.mkdir(parents=True, exist_ok=True)
    if absolute_output.exists():
        absolute_output.unlink()

    result = subprocess.run(
        ["zip", "-r", "-y", "-q", str(absolute_output), "Payload"],
        cwd=str(stage_dir),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise PlumesignSignError(
            f"zip failed (exit={result.returncode}): {_tail(result.stderr, 20)}"
        )


def _verify_signed_bundle(stage_dir: Path) -> None:
    """Run `codesign --verify --deep --strict` on the signed .app in
    staging before we re-archive it into the output IPA.

    Defensive gate: plumesign's sign step occasionally produces malformed
    bundles (v2.2.3 had that happen to us once — the binary lacked an
    LC_CODE_SIGNATURE slot because the staging-scrape workaround was
    disabled in an early iteration). If the bundle is broken we'd rather
    fail loudly here than ship an unsigned IPA to the device and hit a
    cryptic installd error minutes later.

    Uses `codesign` from macOS's built-in Security framework, not the
    Rust apple-codesign library — the system binary has the authoritative
    verification logic Apple's own installd uses.
    """
    payload = stage_dir / "Payload"
    app_dirs = [p for p in payload.iterdir() if p.is_dir() and p.suffix == ".app"]
    if not app_dirs:
        raise PlumesignSignError(
            f"staging dir has no .app inside Payload: {stage_dir}"
        )
    app = app_dirs[0]
    log.info("verifying code signature on %s", app.name)
    result = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        combined = (result.stderr or result.stdout or "").strip()
        raise PlumesignSignError(
            f"codesign verification failed for {app.name}:\n{combined[-800:]}"
        )
    log.debug("codesign verify ok: %s", (result.stderr or result.stdout).strip())


def sign_ipa(
    ipa_path: Path,
    output_ipa: Path,
    *,
    force_tvos: bool = False,
) -> Path:
    """Sign an IPA with plumesign and write the real signed bytes to
    output_ipa, working around plumesign v2.2.3's archive bug.

    Steps:
      1. Run plumesign sign with PLUME_DELETE_AFTER_FINISHED=1 so the
         staging directory isn't cleaned up at the end.
      2. Scrape the staging directory path from plumesign's stderr.
      3. Fall back to globbing for a fresh plume_stage_* if scraping fails.
      4. Re-zip <staging>/Payload into output_ipa using the `zip` CLI.
      5. Remove the staging directory.

    Returns output_ipa on success. Raises PlumesignSignError on any
    failure, including an empty/missing staging directory.
    """
    _ensure_binary()
    if not ipa_path.exists():
        raise PlumesignSignError(f"source IPA not found: {ipa_path}")

    # Throwaway output path — plumesign's --output copies the unchanged
    # input here due to the archive bug. We never read it.
    throwaway_out = Path("/tmp") / f"plume_throwaway_{os.getpid()}.ipa"

    result = _run_plumesign(
        [
            "sign",
            "--package", str(ipa_path),
            "--apple-id",
            "-o", str(throwaway_out),
        ],
        force_tvos=force_tvos,
        preserve_staging=True,
        timeout=300,
        check=False,
    )
    try:
        throwaway_out.unlink(missing_ok=True)
    except OSError:
        pass

    if result.returncode != 0:
        _classify_failure_and_raise(result.stderr)

    stage_dir = _find_staging_dir_from_stderr(result.stderr)
    if stage_dir is None or not stage_dir.exists():
        log.warning(
            "could not scrape staging dir from stderr, falling back to glob"
        )
        stage_dir = _find_staging_dir_by_glob()
    if stage_dir is None or not stage_dir.exists():
        raise PlumesignSignError(
            "plumesign reported success but we could not locate its "
            "staging directory. plume_stage_* dirs in /var/folders/ may "
            "have been cleaned up externally."
        )

    log.info("using plumesign staging dir: %s", stage_dir)
    try:
        # Defensive gate: validate the signature on the .app in staging
        # BEFORE we re-archive it. If the sign step produced garbage
        # (which has happened historically with plumesign v2.2.3), we
        # want to fail here rather than ship a broken IPA downstream.
        _verify_signed_bundle(stage_dir)
        _rezip_staging_to_ipa(stage_dir, output_ipa)
    finally:
        # Clean up staging now that we have the IPA. Swallow errors;
        # leftover temp directories aren't fatal.
        try:
            shutil.rmtree(stage_dir, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            log.debug("staging cleanup failed: %s", e)

    log.info("signed IPA written to %s", output_ipa)
    return output_ipa
