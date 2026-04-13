"""macOS Keychain wrapper for Apple ID credentials.

Apple ID password is never persisted to disk. It's read only during the
first-run login flow and during session-expired recovery. plumesign's own
session token (~/.config/PlumeImpactor/) handles all subsequent sign calls
without touching the password.
"""

from __future__ import annotations

import logging

import keyring
from keyring.errors import PasswordDeleteError

log = logging.getLogger(__name__)

SERVICE_NAME = "com.atvloader.appleid"


def set_password(email: str, password: str) -> None:
    keyring.set_password(SERVICE_NAME, email, password)


def get_password(email: str) -> str | None:
    return keyring.get_password(SERVICE_NAME, email)


def delete_password(email: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, email)
    except PasswordDeleteError:
        log.debug("no keychain entry to delete for %s", email)
