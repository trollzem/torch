"""py2app build config for Torch.

Usage:
    # Dev iteration — symlinks Contents/Resources back to src/, fast rebuild
    python3 setup.py py2app -A

    # Production build — copies everything, bundles Python.framework
    python3 setup.py py2app

Why py2app: without a real .app hosting bundle, the process's
NSBundle.mainBundle() resolves to Python.app (because libpython walks
up from its executable path during Py_Initialize), which means
notifications, LaunchServices attribution, and Keychain ACL prompts
all credit "Python" as the sender. py2app generates a tiny C stub at
Contents/MacOS/Torch that sets PYTHONHOME + PYTHONPATH correctly
before initializing libpython, so mainBundle() points at Torch.app.

Build options are chosen with specific reasons (see comments inline).
Don't loosen `argv_emulation` or drop the `keyring` package entry
without re-reading those comments.
"""

from __future__ import annotations

import sys
from pathlib import Path

from setuptools import setup

# py2app's modulegraph scans APP's imports, which begin at
# src/torchapp/__main__.py -> `from .ui import TorchApp`. That
# relative import needs `src/` on sys.path during the build so
# `torchapp` resolves as a package. Prepending here is the
# documented pattern for src-layout projects + py2app.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

APP = ["src/torchapp/__main__.py"]

OPTIONS = {
    # MUST be False for LSUIElement apps. When True, py2app inserts a
    # Carbon event-loop wrapper that fights the activation-policy
    # setting and drags the dock icon back on launch. We already call
    # setActivationPolicy_(1) in __main__.py, but the Info.plist
    # LSUIElement=True below is the authoritative flag and argv
    # emulation would silently override it.
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "Torch",
        "CFBundleDisplayName": "Torch",
        "CFBundleIdentifier": "com.torch.app",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1",
        "CFBundleExecutable": "Torch",
        "CFBundlePackageType": "APPL",
        # Menubar-only: no dock icon, no main menu, no app switcher.
        "LSUIElement": True,
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        # Required because we drive Terminal.app via osascript for the
        # pairing handoff (until phase 4 replaces it with native UI).
        "NSAppleEventsUsageDescription":
            "Torch uses AppleScript to drive Terminal for device pairing.",
    },
    # `packages` copies the full package tree, which preserves
    # importlib.metadata entry points. This matters for `keyring`,
    # which discovers its macOS backend via entry points — if we used
    # `includes=["keyring"]` instead, entry-point metadata would be
    # missing and keyring.get_password would raise NoKeyringError at
    # runtime. py2app 0.28+ handles rumps and pexpect correctly via
    # either mode; `packages` is the safer default.
    "packages": [
        "torchapp",
        "rumps",
        "keyring",
        "pexpect",
    ],
    # Aggressively exclude pymobiledevice3 and its transitive deps.
    # We shell out to the CLI at runtime (see pymd3.py), we never
    # import the library. If py2app pulls it in, the bundle balloons
    # from ~90 MB to 250+ MB (cryptography, construct, fastapi,
    # asyncclick, qh3, hyperframe, opack, pyimg4, nest_asyncio,
    # starlette). Every entry here is something modulegraph might
    # follow from a stray import.
    "excludes": [
        "pymobiledevice3",
        "construct",
        "cryptography",
        "fastapi",
        "starlette",
        "asyncclick",
        "qh3",
        "hyperframe",
        "opack",
        "nest_asyncio",
        "pyimg4",
        # General bloat
        "tkinter",
        "PIL",
        "numpy",
        "scipy",
        "matplotlib",
        "pytest",
        "wheel",
        "test",
        "unittest",
    ],
    # Copy the patched plumesign binary into
    # Torch.app/Contents/Resources/bin/plumesign. The runtime resolver
    # in paths.py will prefer this location when NSBundle.mainBundle()
    # looks like a real Torch bundle, and fall back to <repo>/bin/
    # for dev mode (python3 -m torchapp from a source tree).
    "resources": ["bin/plumesign"],
    # Semi-standalone skips bundling Python.framework (saves ~70 MB)
    # but hard-codes the Homebrew python3.14 path into the stub
    # launcher's dyld loads — a Homebrew upgrade to python 3.15
    # would break the bundle at launch. Not worth the fragility.
    "semi_standalone": False,
    "site_packages": True,
}

setup(
    app=APP,
    name="Torch",
    options={"py2app": OPTIONS},
    setup_requires=["py2app>=0.28.10"],
)
