"""rumps menubar app.

State model:
  - On startup, bootstrap() loads or seeds config from existing state.
  - A background thread runs the hourly refresh check loop.
  - Wake-from-sleep notifications via NSWorkspace trigger an immediate
    check.
  - All refresh operations go through refresh_all() which holds a lock,
    so UI callbacks (Refresh Now button, hourly timer, wake handler)
    can't stomp on each other.

The menubar title icon reflects the overall state:
  📺 idle - all apps fresh
  📺⏳ refreshing right now
  📺⚠️ stale (at least one app needs refresh and isn't frozen)
  📺❌ error (last run failed or tunneld is down)

All text labels are updated by rebuilding the menu from config state
after every event that changes state (refresh done, config edited,
device added).
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import rumps

from . import config as cfgmod
from . import pairing, paths, plumesign, pymd3, refresh
from .config import Config

log = logging.getLogger(__name__)


ICON_IDLE = "📺"
ICON_REFRESHING = "📺⏳"
ICON_STALE = "📺⚠️"
ICON_ERROR = "📺❌"

HOURLY_TICK_SECONDS = 3600.0

# Free Apple ID provisioning profiles live for 7 days from the moment
# the profile is issued (which is also the moment we sign the IPA).
FREE_TIER_PROFILE_LIFETIME = timedelta(days=7)


def _format_age(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return "unknown"
    delta = datetime.now(timezone.utc) - dt
    total_minutes = int(delta.total_seconds() / 60)
    if total_minutes < 1:
        return "just now"
    if total_minutes < 60:
        return f"{total_minutes}m ago"
    hours = total_minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _format_expiry(iso: str | None) -> str:
    """Human-friendly time until profile expiry.

    Profile expires 7 days after last_signed_at. Returns strings like:
      - "not signed yet"  (never signed)
      - "6d 23h left"     (> 24 hours remaining)
      - "5h 12m left"     (< 24 hours remaining)
      - "expired"         (past the 7-day mark)
    """
    if not iso:
        return "not signed yet"
    try:
        signed_at = datetime.fromisoformat(iso)
    except ValueError:
        return "unknown"
    expires_at = signed_at + FREE_TIER_PROFILE_LIFETIME
    delta = expires_at - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return "expired"
    total_seconds = int(delta.total_seconds())
    days = total_seconds // 86400
    remaining_hours = (total_seconds % 86400) // 3600
    if days > 0:
        return f"{days}d {remaining_hours}h left"
    minutes = (total_seconds % 3600) // 60
    return f"{remaining_hours}h {minutes}m left"


class ATVLoaderApp(rumps.App):
    def __init__(self) -> None:
        super().__init__(
            "ATVLoader",
            title=ICON_IDLE,
            quit_button=None,
        )
        self.cfg: Config = cfgmod.bootstrap()
        self._state_lock = threading.Lock()
        self._icon_state: str = ICON_IDLE
        self._build_menu()
        self._start_scheduler_thread()
        self._install_wake_observer()
        # Kick off an initial refresh check shortly after launch so the
        # user sees activity if anything is stale on cold start. Non-
        # blocking — runs on a worker thread.
        self._background_check(delay=2.0)

    # --- menu wiring ---------------------------------------------------------

    def _status_summary(self) -> str:
        if not self.cfg.ipas:
            return "No apps tracked"
        ok_count = sum(1 for i in self.cfg.ipas if i.status == "ok")
        total = len(self.cfg.ipas)
        stale_count = sum(
            1
            for i in self.cfg.ipas
            if refresh.needs_refresh(i, self.cfg.settings.refresh_interval_days)
        )
        if self._icon_state == ICON_REFRESHING:
            return "Refreshing…"
        if stale_count > 0:
            return f"{stale_count}/{total} apps stale"
        freshest = max(
            (i.last_signed_at for i in self.cfg.ipas if i.last_signed_at),
            default=None,
        )
        return f"{ok_count}/{total} apps fresh · {_format_age(freshest)}"

    def _build_menu(self) -> None:
        self.menu.clear()

        # Header: current status
        self.menu.add(rumps.MenuItem(self._status_summary(), callback=None))
        self.menu.add(rumps.separator)

        # Apps submenu
        apps_item = rumps.MenuItem("Apps")
        if self.cfg.ipas:
            for ipa in sorted(self.cfg.ipas, key=lambda i: i.filename):
                status_icon = {
                    "ok": "✓",
                    "pending": "·",
                    "sign-failed": "❌",
                    "install-failed": "❌",
                    "auth-error": "⚠️",
                    "needs-login": "⚠️",
                    "app-id-limit": "⚠️",
                    "missing-source": "❌",
                    "no-targets": "·",
                    "tunneld-down": "⚠️",
                }.get(ipa.status, "·")
                label = (
                    f"{status_icon} {Path(ipa.filename).stem} · "
                    f"{ipa.platform} · {_format_expiry(ipa.last_signed_at)}"
                )
                apps_item.add(rumps.MenuItem(label, callback=None))
        else:
            apps_item.add(rumps.MenuItem("No IPAs yet", callback=None))
        apps_item.add(rumps.separator)
        apps_item.add(rumps.MenuItem("Add IPA…", callback=self.on_add_ipa))
        self.menu.add(apps_item)

        # Devices submenu
        devices_item = rumps.MenuItem("Devices")
        if self.cfg.devices:
            for device in self.cfg.devices:
                suffix = ""
                if device.device_class and device.device_class != "unknown":
                    suffix = f" · {device.device_class}"
                if device.product_type:
                    suffix += f" · {device.product_type}"
                if device.product_version:
                    suffix += f" {device.product_version}"
                label = f"{device.name}{suffix}"
                devices_item.add(rumps.MenuItem(label, callback=None))
        else:
            devices_item.add(rumps.MenuItem("No devices paired", callback=None))
        devices_item.add(rumps.separator)
        devices_item.add(
            rumps.MenuItem("Add device (Apple TV)…", callback=self.on_add_device_tv)
        )
        devices_item.add(
            rumps.MenuItem("Add device (iPhone/iPad)…", callback=self.on_add_device_ios)
        )
        self.menu.add(devices_item)

        self.menu.add(rumps.separator)

        # Actions
        self.menu.add(rumps.MenuItem("Refresh Now", callback=self.on_refresh_now))
        pause_label = (
            "Resume Auto-Refresh"
            if self.cfg.settings.auto_refresh_paused
            else "Pause Auto-Refresh"
        )
        self.menu.add(rumps.MenuItem(pause_label, callback=self.on_toggle_pause))

        self.menu.add(rumps.separator)

        # Utility
        self.menu.add(
            rumps.MenuItem("Open IPAs Folder", callback=self.on_open_ipas_folder)
        )
        self.menu.add(rumps.MenuItem("View Log", callback=self.on_view_log))
        self.menu.add(
            rumps.MenuItem("Reveal Signed Folder", callback=self.on_open_signed_folder)
        )

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Quit ATVLoader", callback=rumps.quit_application))

    def _set_icon(self, icon: str) -> None:
        self._icon_state = icon
        self.title = icon

    def _rebuild(self) -> None:
        """Rebuild menu + icon from current config. Safe to call from any thread."""
        try:
            self._build_menu()
            self._refresh_icon()
        except Exception as e:  # noqa: BLE001
            log.exception("menu rebuild failed: %s", e)

    def _refresh_icon(self) -> None:
        if self._icon_state == ICON_REFRESHING:
            return
        if any(i.status not in ("ok", "pending") for i in self.cfg.ipas):
            self._set_icon(ICON_ERROR)
            return
        if any(
            refresh.needs_refresh(i, self.cfg.settings.refresh_interval_days)
            and not refresh.is_frozen(i)
            for i in self.cfg.ipas
        ):
            self._set_icon(ICON_STALE)
            return
        self._set_icon(ICON_IDLE)

    # --- menu callbacks ------------------------------------------------------

    def on_refresh_now(self, _sender: object) -> None:
        self._background_check(force=True)

    def on_toggle_pause(self, _sender: object) -> None:
        with self._state_lock:
            self.cfg.settings.auto_refresh_paused = (
                not self.cfg.settings.auto_refresh_paused
            )
            self.cfg.save()
        self._rebuild()
        msg = (
            "Auto-refresh paused"
            if self.cfg.settings.auto_refresh_paused
            else "Auto-refresh resumed"
        )
        rumps.notification("ATVLoader", "Settings", msg)

    def on_add_ipa(self, _sender: object) -> None:
        # We can't open an NSOpenPanel easily from rumps without PyObjC
        # incantations. For v1 we reveal the IPAs folder in Finder and
        # trust the user to drop files in. The polling watcher (hourly
        # tick, or the IPAs-folder sync at next app start) picks them up.
        subprocess.run(["open", str(paths.IPAS_DIR)])
        rumps.notification(
            "ATVLoader",
            "Add an IPA",
            "Drop .ipa files into the folder that just opened, then click "
            "'Refresh Now' to pick them up.",
        )

    # --- device onboarding ---------------------------------------------------

    def on_add_device_tv(self, _sender: object) -> None:
        threading.Thread(
            target=self._add_device_flow,
            args=("tvOS",),
            daemon=True,
            name="atvloader-add-tv",
        ).start()

    def on_add_device_ios(self, _sender: object) -> None:
        threading.Thread(
            target=self._add_device_flow,
            args=("iOS",),
            daemon=True,
            name="atvloader-add-ios",
        ).start()

    def _add_device_flow(self, device_kind: str) -> None:
        """Interactive device pairing. Runs on a worker thread.

        device_kind is "tvOS" or "iOS" — used only to customize the
        instructions shown to the user. The pairing protocol itself is
        the same RemotePairing handshake for both.
        """
        # Step 1: instructions. rumps.alert() is a modal NSAlert which
        # returns 1 for OK, 0 for Cancel.
        if device_kind == "tvOS":
            msg = (
                "On your Apple TV, go to:\n\n"
                "Settings → General → Remotes and Devices → "
                "Remote App and Devices\n\n"
                "Leave that screen open, then click OK to start the scan."
            )
        else:
            msg = (
                "On your iPhone or iPad (iOS 17+), go to:\n\n"
                "Settings → Privacy & Security → Developer Mode → "
                "Pair Device\n\n"
                "Leave that screen open, then click OK to start the scan."
            )
        if rumps.alert(title="Add device", message=msg, ok="OK", cancel="Cancel") != 1:
            return

        # Step 2: bonjour scan for devices in pairing mode.
        try:
            candidates = pymd3.scan_manual_pairing(timeout=10.0)
        except pymd3.Pymd3Error as e:
            rumps.alert(
                title="Scan failed",
                message=f"Could not scan for devices: {e}",
            )
            return

        if not candidates:
            rumps.alert(
                title="No device found",
                message=(
                    "Nothing is advertising a pairing prompt on the network. "
                    "Make sure the device is on the same WiFi and the "
                    "pairing screen is still open."
                ),
            )
            return

        # For v1 we pair with the first advertising device. If there are
        # multiple (rare — you'd have to have two Apple TVs in pairing mode
        # simultaneously) we take the first.
        target = candidates[0]
        target_name = target.get("name") or "unknown device"

        # Step 3: spawn the pairing handshake and collect the PIN.
        def pin_prompt() -> str:
            result = rumps.Window(
                message=(
                    f"Enter the 6-digit pairing code shown on "
                    f"{target_name}."
                ),
                title="Enter pairing code",
                default_text="",
                ok="Pair",
                cancel="Cancel",
                dimensions=(160, 20),
            ).run()
            if not result.clicked:
                raise pairing.PairingCancelledError("user cancelled PIN dialog")
            return result.text.strip()

        try:
            pair_id = pairing.pair_device(target_name, pin_prompt)
        except pairing.PairingCancelledError:
            return
        except pairing.PairingError as e:
            rumps.alert(title="Pairing failed", message=str(e))
            return

        # Step 4: reconcile the new device so we get its real UDID, then
        # register with the Apple portal and save config.
        try:
            self._post_pair_reconcile(pair_id, fallback_name=target_name)
        except Exception as e:  # noqa: BLE001
            log.exception("post-pair reconcile failed: %s", e)
            rumps.alert(
                title="Pairing recorded but not complete",
                message=(
                    f"The pairing file was saved but we couldn't finish "
                    f"registering the device: {e}"
                ),
            )
            return

        rumps.notification(
            "ATVLoader",
            "Device added",
            f"{target_name} is ready to receive app refreshes.",
        )

    def _post_pair_reconcile(self, pair_id: str, *, fallback_name: str) -> None:
        """After a successful pair, create the Device entry, reconcile it
        against tunneld (to get the real UDID), register it with the Apple
        portal, and persist config."""
        from .config import Device

        pair_record_path = paths.PYMD3_PAIR_RECORDS_DIR / f"remote_{pair_id}.plist"
        new_device = Device(
            name=fallback_name,
            pair_record_identifier=pair_id,
            udid=None,
            device_class="unknown",
            paired_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            pair_record_path=str(pair_record_path),
        )

        # Re-load config to avoid clobbering any changes made during pairing.
        self.cfg = cfgmod.Config.load()
        if self.cfg.device_by_pair_record(pair_id) is None:
            self.cfg.devices.append(new_device)

        # Reconcile against tunneld (may take a few seconds for tunneld
        # to notice the new pair record and establish a tunnel).
        deadline = time.monotonic() + 30
        reconciled = None
        while time.monotonic() < deadline:
            try:
                reconciled = pymd3.reconcile_device(new_device)
                if reconciled.udid:
                    break
            except pymd3.TunnelNotFoundError:
                time.sleep(2)
                continue
            except pymd3.Pymd3Error:
                break

        if reconciled and reconciled.udid:
            # Swap the new entry for the reconciled one
            for idx, d in enumerate(self.cfg.devices):
                if d.pair_record_identifier == pair_id:
                    self.cfg.devices[idx] = reconciled
                    break
            # Register with Apple's portal (swallows "already exists")
            try:
                plumesign.register_device(reconciled.udid, reconciled.name)
            except plumesign.PlumesignError as e:
                log.warning(
                    "register_device failed for %s: %s", reconciled.udid, e
                )

        # Auto-target existing IPAs at the new device.
        for ipa in self.cfg.ipas:
            if pair_id not in ipa.target_devices:
                ipa.target_devices.append(pair_id)

        self.cfg.save()
        self._rebuild()

    def on_open_ipas_folder(self, _sender: object) -> None:
        subprocess.run(["open", str(paths.IPAS_DIR)])

    def on_open_signed_folder(self, _sender: object) -> None:
        subprocess.run(["open", str(paths.SIGNED_DIR)])

    def on_view_log(self, _sender: object) -> None:
        subprocess.run(["open", "-a", "Console", str(paths.LOG_FILE)])

    # --- scheduler / worker threads ------------------------------------------

    def _background_check(
        self, *, force: bool = False, delay: float = 0.0
    ) -> None:
        """Run a refresh check on a worker thread. Multiple calls collapse
        into one because refresh_all() is lock-guarded."""

        def run() -> None:
            if delay > 0:
                time.sleep(delay)
            self._do_refresh(force=force)

        t = threading.Thread(target=run, daemon=True, name="atvloader-refresh")
        t.start()

    def _do_refresh(self, *, force: bool) -> None:
        previous_icon = self._icon_state
        self._set_icon(ICON_REFRESHING)
        self._rebuild()

        def progress(msg: str) -> None:
            log.debug("progress: %s", msg)

        try:
            self.cfg = cfgmod.Config.load()
            succeeded, failed = refresh.refresh_all(
                self.cfg, force=force, progress=progress
            )
        except refresh.RefreshAborted as e:
            self._set_icon(ICON_ERROR)
            rumps.notification(
                "ATVLoader",
                "Refresh aborted",
                str(e),
            )
            self._rebuild()
            return
        except Exception as e:  # noqa: BLE001
            log.exception("unexpected refresh error: %s", e)
            self._set_icon(ICON_ERROR)
            rumps.notification("ATVLoader", "Refresh failed", str(e))
            self._rebuild()
            return

        # Restore state from disk so we have the latest timestamps
        self.cfg = cfgmod.Config.load()
        self._set_icon(previous_icon)  # will be re-derived by _refresh_icon
        if succeeded == 0 and failed == 0:
            # Nothing needed refreshing
            pass
        elif failed == 0:
            rumps.notification(
                "ATVLoader",
                "Refresh complete",
                f"{succeeded} app{'s' if succeeded != 1 else ''} refreshed",
            )
        else:
            rumps.notification(
                "ATVLoader",
                "Refresh partial",
                f"{succeeded} succeeded, {failed} failed",
            )
        self._rebuild()

    def _start_scheduler_thread(self) -> None:
        """Hourly tick that calls the refresh check. The check itself
        decides whether any IPAs actually need work (needs_refresh), so
        this thread just keeps the clock running."""

        def loop() -> None:
            while True:
                time.sleep(HOURLY_TICK_SECONDS)
                try:
                    self._do_refresh(force=False)
                except Exception as e:  # noqa: BLE001
                    log.exception("hourly refresh tick failed: %s", e)

        t = threading.Thread(target=loop, daemon=True, name="atvloader-hourly")
        t.start()

    # --- wake-from-sleep hook ------------------------------------------------

    def _install_wake_observer(self) -> None:
        """Subscribe to NSWorkspace.didWakeNotification so we re-check
        immediately when the Mac wakes from sleep. Without this, a
        long-sleeping Mac would only notice stale apps on the next
        hourly tick (up to an hour late)."""
        try:
            from AppKit import NSWorkspace
            from Foundation import NSObject
            from PyObjCTools import AppHelper  # noqa: F401 (import side effect)
        except ImportError:
            log.warning("PyObjC unavailable; wake hook disabled")
            return

        app_ref = self

        class _WakeObserver(NSObject):  # type: ignore[misc]
            def didWake_(self, _note: object) -> None:
                log.info("system wake detected; kicking refresh check")
                app_ref._background_check(force=False, delay=2.0)

        observer = _WakeObserver.alloc().init()
        center = NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(
            observer,
            b"didWake:",
            "NSWorkspaceDidWakeNotification",
            None,
        )
        # Hold a strong reference so the observer isn't GC'd.
        self._wake_observer = observer
