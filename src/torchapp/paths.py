"""Filesystem paths used throughout the app.

APP_SUPPORT_DIR is the runtime home for user state (config, logs, signed IPAs).
PROJECT_ROOT is where the source tree and bundled plumesign binary live.
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PLUMESIGN_BINARY = PROJECT_ROOT / "bin" / "plumesign"
PROJECT_IPAS_DIR = PROJECT_ROOT / "ipas"
PROJECT_SIGNED_DIR = PROJECT_ROOT / "signed"

PYMD3_PAIR_RECORDS_DIR = Path("~/.pymobiledevice3").expanduser()
PLUMESIGN_STATE_DIR = Path("~/.config/PlumeImpactor").expanduser()
PLUMESIGN_ACCOUNTS_FILE = PLUMESIGN_STATE_DIR / "accounts.json"

TUNNELD_URL = "http://127.0.0.1:49151/"


def ensure_dirs() -> None:
    for d in (APP_SUPPORT_DIR, LOG_DIR, IPAS_DIR, SIGNED_DIR):
        d.mkdir(parents=True, exist_ok=True)
