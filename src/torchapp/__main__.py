"""Entry point for both `python3 -m torchapp` AND the py2app-bundled .app.

Uses absolute imports (`from torchapp import ...`) rather than relative
imports (`from . import ...`) so that both invocation modes work:
  - `python3 -m torchapp` sets __package__='torchapp' (relative works)
  - py2app's __boot__.py execs this file as a plain script with no
    package context (relative imports fail with "no known parent
    package"; only absolute imports resolve).

Adds `src/` to sys.path via __file__ navigation before importing
torchapp. This is required for py2app alias mode, where the bundle's
embedded Python has no knowledge of the project's src-layout; the
bundle's __boot__.py execs this file as a script, so `torchapp` is
not on sys.path until we put it there ourselves. In full-build mode
py2app copies the package into Contents/Resources/lib/python3.14/ so
torchapp is already importable — the added src/ path there refers to
a nonexistent directory inside the bundle and is silently ignored.
"""

from __future__ import annotations

import logging
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_THIS_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from torchapp import paths  # noqa: E402
from torchapp.ui import TorchApp  # noqa: E402


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


def _log_bundle_identity() -> None:
    """Log the process's NSBundle.mainBundle() identity at startup.

    Useful diagnostic for distinguishing "running inside the py2app
    Torch.app bundle" from "running as `python3 -m torchapp` from the
    source tree". Inside the bundle, bundle id should be com.torch.app;
    outside, it will be org.python.python (the hosting Python.app).
    """
    try:
        from AppKit import NSBundle  # type: ignore[import-not-found]
    except ImportError:
        return
    b = NSBundle.mainBundle()
    logging.getLogger("torch").info(
        "bundle identity: id=%s name=%s path=%s",
        b.bundleIdentifier(),
        b.infoDictionary().get("CFBundleName") if b.infoDictionary() else None,
        b.bundlePath(),
    )


def _scrub_python_env_for_subprocesses() -> None:
    """Remove PYTHONHOME / PYTHONPATH from os.environ post-boot.

    py2app's stub launcher sets both env vars before calling
    Py_Initialize so the bundle's embedded Python finds its own
    framework (Torch.app/Contents/Frameworks/Python.framework) and
    Resources/lib paths. Once the interpreter is running, these are
    dead weight — CPython reads them only during initialization.

    But they're still in the UNIX environment block, so any
    subprocess we spawn (pymobiledevice3, osascript → Terminal →
    python3, etc.) inherits them. When a child process is another
    Python interpreter — e.g. the /opt/homebrew/bin/pymobiledevice3
    CLI shim whose shebang is `python3.14` — its Python starts up
    with PYTHONHOME=<Torch.app path>, walks sys.path from there, and
    fails to find pymobiledevice3 (which lives in Homebrew's
    site-packages, not the bundle's).

    Fix: delete these env vars after the bundle's own Python has
    completed initialization. This is safe because Python only reads
    them at startup; deleting them mid-run has no effect on the
    current process. Every subprocess from this point on sees a clean
    environment and respects its own shebang's Python paths.
    """
    for var in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE"):
        os.environ.pop(var, None)


def main() -> None:
    _setup_logging()
    _scrub_python_env_for_subprocesses()
    _hide_dock_icon()
    _log_bundle_identity()
    app = TorchApp()
    app.run()


if __name__ == "__main__":
    main()
