"""Filesystem paths used throughout the app.

APP_SUPPORT_DIR is the runtime home for user state (config, logs, signed IPAs).
PLUMESIGN_BINARY resolves via NSBundle in bundle mode, via the repo's bin/
directory in dev mode.
PYMD3_PAIR_RECORDS / PLUMESIGN_STATE_DIR are external state directories owned
by the pymobiledevice3 and plumesign CLIs respectively — we only read them
during seeding and first-run detection, never write.
"""

from pathlib import Path

APP_SUPPORT_DIR = Path("~/Library/Application Support/Torch").expanduser()
CONFIG_FILE = APP_SUPPORT_DIR / "config.json"
LOG_DIR = APP_SUPPORT_DIR / "logs"
LOG_FILE = LOG_DIR / "torch.log"
IPAS_DIR = APP_SUPPORT_DIR / "ipas"
SIGNED_DIR = APP_SUPPORT_DIR / "signed"


def _resolve_plumesign_binary() -> Path:
    """Find the patched plumesign binary.

    Order:
      1. Inside the py2app bundle (Torch.app), check both
         Contents/Resources/plumesign (where py2app actually copies
         the file — it flattens the `resources: ["bin/plumesign"]`
         declaration in setup.py down to the file's basename) and
         Contents/Resources/bin/plumesign (in case setup.py is
         reconfigured to preserve the subdirectory via a tuple-form
         resource declaration later).
      2. <repo-root>/bin/plumesign — dev mode, `python3 -m torchapp`
         from a source tree with no bundle involved.

    We do NOT raise here if neither exists — refresh.py surfaces a
    clean error message to the user when it tries to use the binary
    and finds it missing.
    """
    try:
        from Foundation import NSBundle  # type: ignore[import-not-found]
        b = NSBundle.mainBundle()
        if b.bundleIdentifier() == "com.torch.app":
            rp = b.resourcePath()
            if rp:
                rp_path = Path(str(rp))
                for candidate in (rp_path / "plumesign", rp_path / "bin" / "plumesign"):
                    if candidate.exists():
                        return candidate
    except Exception:  # noqa: BLE001
        # Foundation import may fail during unit tests or if the
        # process has no Cocoa; fall through to dev-mode resolution.
        pass
    return Path(__file__).resolve().parent.parent.parent / "bin" / "plumesign"


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PLUMESIGN_BINARY = _resolve_plumesign_binary()
PROJECT_IPAS_DIR = PROJECT_ROOT / "ipas"
PROJECT_SIGNED_DIR = PROJECT_ROOT / "signed"

PYMD3_PAIR_RECORDS_DIR = Path("~/.pymobiledevice3").expanduser()
PLUMESIGN_STATE_DIR = Path("~/.config/PlumeImpactor").expanduser()
PLUMESIGN_ACCOUNTS_FILE = PLUMESIGN_STATE_DIR / "accounts.json"

TUNNELD_URL = "http://127.0.0.1:49151/"


def ensure_dirs() -> None:
    for d in (APP_SUPPORT_DIR, LOG_DIR, IPAS_DIR, SIGNED_DIR):
        d.mkdir(parents=True, exist_ok=True)
