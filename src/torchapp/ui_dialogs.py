"""Modal input dialogs for the menubar app.

Every function in this module MUST be called on the Cocoa main thread.
Worker threads should marshal these calls through
`ui._run_on_main_and_wait(...)`, which dispatches to the main thread
via AppHelper.callAfter and blocks until the dialog returns.

Why its own module: rumps.Window + rumps.alert are Cocoa-main-thread-
only, and `ui.py` is already 1100+ lines. Keeping dialog plumbing here
means ui.py can stay focused on menu state and event wiring.

The password prompt uses a plain NSTextField (not a secure field).
rumps.Window doesn't expose NSSecureTextField, and the PyObjC code to
build one from scratch is more risk than it's worth for a flow the
user invokes at most a handful of times in the app's lifetime. The
prompt message makes this visible to the user.
"""

from __future__ import annotations

import rumps


def prompt_apple_id_email(default: str = "") -> str | None:
    """Show a modal asking for the Apple ID email. Returns None on cancel."""
    window = rumps.Window(
        message="Enter your Apple ID email.",
        title="Torch — Log in to Apple ID",
        default_text=default,
        ok="Next",
        cancel="Cancel",
        dimensions=(320, 24),
    )
    response = window.run()
    if response.clicked != 1:
        return None
    email = response.text.strip()
    return email or None


def prompt_apple_id_password() -> str | None:
    """Show a modal asking for the Apple ID password. Returns None on cancel.

    The text field is NOT masked. The message below tells the user so
    they can decide whether to look over their shoulder first.
    """
    window = rumps.Window(
        message=(
            "Enter your Apple ID password.\n\n"
            "Torch sends this to plumesign once to obtain a session "
            "token, then stores it in the macOS Keychain for session "
            "recovery only. It is never written to disk in plaintext.\n\n"
            "Note: this field does not mask input."
        ),
        title="Torch — Log in to Apple ID",
        default_text="",
        ok="Log in",
        cancel="Cancel",
        dimensions=(320, 24),
    )
    response = window.run()
    if response.clicked != 1:
        return None
    password = response.text
    return password or None


def prompt_2fa_code() -> str | None:
    """Show a modal asking for the 6-digit 2FA code. Returns None on cancel.

    Strips whitespace and any non-digit characters so the user can paste
    "123 456" or "123-456" from a message.
    """
    window = rumps.Window(
        message=(
            "Apple sent a 6-digit verification code to your trusted "
            "device.\n\nEnter it below."
        ),
        title="Torch — Two-factor code",
        default_text="",
        ok="Submit",
        cancel="Cancel",
        dimensions=(120, 24),
    )
    response = window.run()
    if response.clicked != 1:
        return None
    code = "".join(c for c in response.text if c.isdigit())
    return code or None


def prompt_pairing_pin(device_name: str) -> str | None:
    """Show a modal asking for the Apple TV pairing PIN. Returns None on cancel.

    pymobiledevice3 only prompts for a PIN when the peer is an Apple TV;
    iPhone/iPad pairing uses the device's own Trust dialog and never
    reaches this codepath. The PIN is a 6-digit code shown on the Apple
    TV pairing screen the user just opened.

    Strips whitespace and non-digit characters so the user can paste
    "123 456" or "123-456" if pymobiledevice3's display formats it that
    way on-device.
    """
    window = rumps.Window(
        message=(
            f"Apple TV {device_name!r} is showing a 6-digit pairing "
            f"code on screen.\n\nEnter it below."
        ),
        title="Torch — Pair Apple TV",
        default_text="",
        ok="Pair",
        cancel="Cancel",
        dimensions=(120, 24),
    )
    response = window.run()
    if response.clicked != 1:
        return None
    code = "".join(c for c in response.text if c.isdigit())
    return code or None
