"""Entry point: `python3 -m torchapp` launches the menubar app."""

from __future__ import annotations

import logging
import sys

from . import paths
from .ui import TorchApp


def _hide_dock_icon() -> None:
    """Switch the process to an accessory app (no dock icon, no app menu).

    Equivalent to LSUIElement=true in an Info.plist, but done at runtime
    so we don't need a wrapper .app bundle. Must run before rumps enters
    its NSApplication run loop; NSApplication is a singleton so the
    policy set here is what rumps will inherit.
    """
    try:
        from AppKit import NSApplication  # type: ignore[import-not-found]
    except ImportError:
        return
    NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory


def _setup_logging() -> None:
    paths.ensure_dirs()
    handler = logging.FileHandler(paths.LOG_FILE)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(
        logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler, stderr_handler],
    )
    logging.getLogger("torch").info(
        "Torch starting; log file at %s", paths.LOG_FILE
    )


def main() -> None:
    _setup_logging()
    _hide_dock_icon()
    app = TorchApp()
    app.run()


if __name__ == "__main__":
    main()
