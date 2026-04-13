"""Entry point: `python3 -m torchapp` launches the menubar app."""

from __future__ import annotations

import logging
import sys

from . import paths
from .ui import TorchApp


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
    app = TorchApp()
    app.run()


if __name__ == "__main__":
    main()
