"""Menubar icon generation via Apple SF Symbols.

Replaces the old emoji title with proper vector icons rendered from
Apple's SF Symbols framework (shipped with macOS 11+). The symbols
render as template images that adapt to light/dark menubar themes
automatically — same treatment every native Apple menubar app gets.

Icons are rendered to PNG on the first run and cached in the app
support dir. rumps's menubar hook takes a filesystem path, so we
dump the symbol to a file and point rumps at it; at runtime the
active icon swaps by assigning a different path to `self.icon`.

Four states mirror the four menubar states used in `ui.py`:

    idle        flame.fill                      steady-state, everything fresh
    refreshing  arrow.triangle.2.circlepath     sign/install in progress
    stale       exclamationmark.triangle.fill   at least one IPA stale
    error       xmark.octagon.fill              tunneld down / cert dead / etc

All four symbols ship with SF Symbols 1 and are stable across every
macOS release we care about. Rendering uses the Cocoa runtime via
PyObjC — no external dependencies beyond what the menubar already
needs to run.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)


# SF Symbol identifiers, one per menubar state.
SYMBOL_IDLE = "flame.fill"
SYMBOL_REFRESHING = "arrow.triangle.2.circlepath"
SYMBOL_STALE = "exclamationmark.triangle.fill"
SYMBOL_ERROR = "xmark.octagon.fill"

STATE_IDLE = "idle"
STATE_REFRESHING = "refreshing"
STATE_STALE = "stale"
STATE_ERROR = "error"

_STATE_SYMBOLS: dict[str, str] = {
    STATE_IDLE: SYMBOL_IDLE,
    STATE_REFRESHING: SYMBOL_REFRESHING,
    STATE_STALE: SYMBOL_STALE,
    STATE_ERROR: SYMBOL_ERROR,
}

# macOS menubar icons are typically 18-22pt tall. We render slightly
# oversized and let rumps / NSStatusBar downsample, which gives us a
# clean image on both standard and Retina displays.
_POINT_SIZE = 18

# NSBitmapImageFileType.png == 4. We import the constant when available
# and fall back to the integer otherwise — PyObjC's constant surface
# varies across versions.
try:  # pragma: no cover - platform specific import
    from AppKit import NSPNGFileType as _NS_PNG_FILE_TYPE  # type: ignore
except ImportError:  # pragma: no cover
    _NS_PNG_FILE_TYPE = 4


def _cache_dir() -> Path:
    d = paths.APP_SUPPORT_DIR / "icons"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _icon_path_for_state(state: str) -> Path:
    return _cache_dir() / f"menubar-{state}.png"


def _render_symbol_to_png(symbol_name: str, dest: Path) -> bool:
    """Render an SF Symbol to `dest` as a template PNG.

    Returns True on success, False if anything went wrong (SF Symbol
    unavailable, PyObjC missing, write failed). The caller should
    fall back to an emoji title when this returns False.
    """
    try:
        from AppKit import NSBitmapImageRep, NSImage  # type: ignore
    except ImportError:
        log.warning("PyObjC AppKit not available; cannot render SF Symbol")
        return False

    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
        symbol_name, f"Torch menubar icon ({symbol_name})"
    )
    if img is None:
        log.warning("SF Symbol not available: %s", symbol_name)
        return False

    # Template images adapt to the menubar's light/dark background
    # the same way NSStatusItem's own images do.
    img.setTemplate_(True)
    img.setSize_((_POINT_SIZE, _POINT_SIZE))

    tiff = img.TIFFRepresentation()
    if tiff is None:
        log.warning("failed to get TIFF data for %s", symbol_name)
        return False

    rep = NSBitmapImageRep.imageRepWithData_(tiff)
    if rep is None:
        log.warning("failed to build bitmap rep for %s", symbol_name)
        return False

    png = rep.representationUsingType_properties_(_NS_PNG_FILE_TYPE, {})
    if png is None:
        log.warning("PNG encode failed for %s", symbol_name)
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    png.writeToFile_atomically_(str(dest), True)
    log.debug("rendered %s -> %s", symbol_name, dest)
    return True


def ensure_menubar_icons(*, rerender: bool = False) -> dict[str, Path | None]:
    """Ensure all four menubar icon PNGs exist in the cache.

    Returns a map `{state: Path | None}` where None means that
    specific render failed — the caller should fall back to an emoji
    for that state. Idempotent: existing cached files are left alone
    unless `rerender=True`.
    """
    result: dict[str, Path | None] = {}
    for state, symbol in _STATE_SYMBOLS.items():
        dest = _icon_path_for_state(state)
        if dest.exists() and not rerender:
            result[state] = dest
            continue
        if _render_symbol_to_png(symbol, dest):
            result[state] = dest
        else:
            result[state] = None
    return result
