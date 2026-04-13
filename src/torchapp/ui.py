"""rumps menubar app.

State model:
  - On startup, bootstrap() loads or seeds config from existing state.
  - A background thread runs the hourly refresh check loop.
  - Wake-from-sleep notifications via NSWorkspace trigger an immediate
    check.
  - All refresh operations go through refresh_all() which holds a lock,
    so UI callbacks (Refresh Now button, hourly timer, wake handler)
    can't stomp on each other.

The menubar icon reflects the overall state via an SF Symbol template
image rendered into ~/Library/Application Support/Torch/icons/ at
first launch:
  idle         flame.fill                      all apps fresh
  refreshing   arrow.triangle.2.circlepath     sign/install in progress
  stale        exclamationmark.triangle.fill   at least one IPA needs refresh
  error        xmark.octagon.fill              tunneld down / cert dead / etc

See `icons.py` for the SF Symbol names and the rendering pipeline.

All text labels are updated by rebuilding the menu from config state
after every event that changes state (refresh done, config edited,
device added).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import rumps

try:
    from PyObjCTools import AppHelper  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    AppHelper = None  # type: ignore[assignment]

from . import config as cfgmod
from . import icons, pairing, paths, plumesign, pymd3, refresh
from .config import Config

log = logging.getLogger(__name__)


def _on_main_thread(callable_: "object") -> None:
    """Marshal a zero-arg callable onto the Cocoa main thread.

    rumps / NSUserNotificationCenter / NSMenu mutations must run on the
    main thread; calling them from a background worker can (and does)
    silently kill the process. We wrap every cross-thread UI touch in
    this so the worker threads doing long-running plumesign / pymd3
    work can safely signal progress back to the menu.

    Falls back to calling inline if PyObjC isn't available for some
    reason — which means we're already on the main thread (no Cocoa
    runloop), or in a degraded test scenario.
    """
    if AppHelper is None:
        callable_()  # type: ignore[misc]
        return
    AppHelper.callAfter(callable_)


def _run_on_main_and_wait(func, *args, **kwargs):  # type: ignore[no-untyped-def]
    """Dispatch a callable to the main thread and block until it returns.

    Used by worker threads that need modal dialogs (rumps.alert,
    rumps.Window) — those must run on the Cocoa main thread, but the
    worker needs the return value to continue its flow.

    Raises any exception the target callable raised.
    """
    if AppHelper is None:
        return func(*args, **kwargs)

    result: dict[str, object] = {}
    done = threading.Event()

    def _body() -> None:
        try:
            result["value"] = func(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001
            result["error"] = e
        finally:
            done.set()

    AppHelper.callAfter(_body)
    done.wait()
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result.get("value")


# State keys used by icons.ensure_menubar_icons(). These double as
# the string values we store in self._icon_state so we can detect
# "is the menubar currently showing refreshing?" in _refresh_icon.
ICON_IDLE = icons.STATE_IDLE
ICON_REFRESHING = icons.STATE_REFRESHING
ICON_STALE = icons.STATE_STALE
ICON_ERROR = icons.STATE_ERROR

# Emoji fallbacks used only if SF Symbol rendering failed at startup
# (e.g. on a macOS version that doesn't ship the expected symbols).
_EMOJI_FALLBACK: dict[str, str] = {
    ICON_IDLE: "🔥",
    ICON_REFRESHING: "🔥⏳",
    ICON_STALE: "🔥⚠️",
    ICON_ERROR: "🔥❌",
}

HOURLY_TICK_SECONDS = 3600.0
# How often to check for external config mutations (IPA file drops,
# config.json edits from another process). Cheap on-disk I/O only.
CONFIG_WATCH_SECONDS = 5.0

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


def _format_cert_expiry(iso: str | None) -> str:
    """Days until the developer certificate expires.

    Cert is valid for 364 days on free accounts — no need for the hour-
    level precision that profile expiry uses. Returns strings like:
      - "unknown"         (cert status not yet polled)
      - "304 days left"   (normal)
      - "3 days left"     (inside warning window)
      - "expired"
    """
    if not iso:
        return "unknown"
    try:
        exp = datetime.fromisoformat(iso)
    except ValueError:
        return "unknown"
    delta = exp - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return "expired"
    return f"{delta.days} days left"


class TorchApp(rumps.App):
    def __init__(self) -> None:
        # Render SF Symbol icons before super().__init__ so we can
        # pass the idle icon path directly to rumps.App. If symbol
        # rendering fails for any reason we fall back to emoji titles.
        self._icon_paths: dict[str, Path | None] = icons.ensure_menubar_icons()
        idle_path = self._icon_paths.get(ICON_IDLE)

        if idle_path is not None:
            super().__init__(
                "Torch",
                icon=str(idle_path),
                template=True,
                title=None,
                quit_button=None,
            )
        else:
            super().__init__(
                "Torch",
                title=_EMOJI_FALLBACK[ICON_IDLE],
                quit_button=None,
            )

        self.cfg: Config = cfgmod.bootstrap()
        self._state_lock = threading.Lock()
        self._icon_state: str = ICON_IDLE
        self._last_config_mtime = self._config_mtime()
        self._build_menu()
        # rumps.Timer runs its callback on the main thread, which is
        # exactly what we need for anything that touches self.menu /
        # self.title / rumps.notification. The timer itself schedules
        # at the given interval; calling .start() arms it.
        self._hourly_timer = rumps.Timer(self._on_hourly_tick, HOURLY_TICK_SECONDS)
        self._hourly_timer.start()
        # Cheap file-mtime watcher so external config mutations (another
        # process signing an IPA, a user dropping a file into the ipas/
        # folder) show up in the menu without having to trigger a refresh.
        self._config_watch_timer = rumps.Timer(
            self._on_config_watch_tick, CONFIG_WATCH_SECONDS
        )
        self._config_watch_timer.start()
        self._install_wake_observer()
        # Kick off an initial refresh check shortly after launch via a
        # one-shot timer so it runs on the main thread (which then
        # dispatches the actual signing work onto a worker via
        # _background_check).
        self._initial_kick = rumps.Timer(self._on_initial_kick, 2.0)
        self._initial_kick.start()

    def _config_mtime(self) -> float:
        try:
            return paths.CONFIG_FILE.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def _on_config_watch_tick(self, _timer: object) -> None:
        """Detect external changes to config.json or the IPAs folder.

        Runs on the main thread every CONFIG_WATCH_SECONDS. Cheap: a
        stat + optional directory scan + dict comparison. If anything
        changed, we reload config, run sync_ipas_folder (picks up new
        files dropped into the folder), save if needed, and rebuild
        the menu. No refresh is triggered — that's still only on the
        hourly timer or explicit user action.
        """
        current_mtime = self._config_mtime()
        config_changed = current_mtime != self._last_config_mtime

        # Also check for new/removed files in the IPAs folder even if
        # config.json itself hasn't been touched.
        on_disk = set()
        try:
            on_disk = {p.name for p in paths.IPAS_DIR.glob("*.ipa")}
        except OSError:
            pass
        tracked_set = {i.filename for i in self.cfg.ipas}
        folder_changed = on_disk != tracked_set

        if not config_changed and not folder_changed:
            return

        log.debug(
            "config watcher saw change (mtime=%s folder=%s); reloading",
            config_changed,
            folder_changed,
        )
        try:
            self.cfg = cfgmod.Config.load()
            if cfgmod.sync_ipas_folder(self.cfg):
                self.cfg.save()
            self._last_config_mtime = self._config_mtime()
            self._rebuild()
        except Exception as e:  # noqa: BLE001
            log.exception("config watcher rebuild failed: %s", e)

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

    def _cert_summary(self) -> str | None:
        """Return the cert-expiration status line, or None to hide it.

        Shows a countdown when the cert is healthy so the user has a
        calm always-visible signal that everything's ok. Adds an
        icon prefix when the cert is expiring / expired / revoked /
        missing so the problem is immediately obvious.
        """
        cs = self.cfg.cert_status
        if cs.status == "unknown" and not cs.expiration_date:
            return None
        label = _format_cert_expiry(cs.expiration_date)
        if cs.status == "ok":
            return f"Cert: {label}"
        if cs.status == "expiring":
            return f"⚠️ Cert expiring: {label}"
        if cs.status == "expired":
            return "❌ Cert expired — re-login required"
        if cs.status == "revoked":
            return "❌ Cert revoked — re-login required"
        if cs.status == "missing":
            return "❌ No developer cert — re-login required"
        return f"Cert: {label}"

    def _build_menu(self) -> None:
        self.menu.clear()

        # Header: current status + cert expiration line
        self.menu.add(rumps.MenuItem(self._status_summary(), callback=None))
        cert_line = self._cert_summary()
        if cert_line:
            self.menu.add(rumps.MenuItem(cert_line, callback=None))
        self.menu.add(rumps.separator)

        # Apps submenu — each IPA is a parent item with a sub-menu
        # that exposes a per-app "Refresh now" action and a tooltip-ish
        # line showing the original bundle ID.
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
                ipa_parent = rumps.MenuItem(label)

                # Capture filename in a default arg to avoid the usual
                # Python closure-over-loop-variable pitfall.
                def _refresh_cb(_sender, fn=ipa.filename):  # type: ignore[no-untyped-def]
                    self._refresh_one(fn)

                def _remove_cb(_sender, fn=ipa.filename):  # type: ignore[no-untyped-def]
                    self._remove_ipa(fn)

                ipa_parent.add(rumps.MenuItem("Refresh now", callback=_refresh_cb))
                ipa_parent.add(
                    rumps.MenuItem("Remove from tracking", callback=_remove_cb)
                )
                ipa_parent.add(rumps.separator)
                ipa_parent.add(
                    rumps.MenuItem(
                        f"Bundle: {ipa.original_bundle_id}", callback=None
                    )
                )
                if ipa.signed_bundle_id:
                    ipa_parent.add(
                        rumps.MenuItem(
                            f"Signed as: {ipa.signed_bundle_id}", callback=None
                        )
                    )
                if ipa.last_signed_at:
                    ipa_parent.add(
                        rumps.MenuItem(
                            f"Signed: {_format_age(ipa.last_signed_at)}",
                            callback=None,
                        )
                    )
                if ipa.status not in ("ok", "pending"):
                    ipa_parent.add(
                        rumps.MenuItem(f"Status: {ipa.status}", callback=None)
                    )
                    if ipa.last_error:
                        ipa_parent.add(
                            rumps.MenuItem(
                                f"Error: {ipa.last_error[:60]}…",
                                callback=None,
                            )
                        )
                apps_item.add(ipa_parent)
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
                # Free-tier per-device app cap display. 3 is Apple's
                # limit for Personal Team signed apps; we warn with ⚠️
                # at the cap so the user sees why a refresh might start
                # skipping the device.
                active = refresh.count_active_apps_on_device(self.cfg, device)
                cap = refresh.FREE_TIER_DEVICE_APP_CAP
                cap_marker = " ⚠️" if active >= cap else ""
                suffix += f" · {active}/{cap} apps{cap_marker}"
                label = f"{device.name}{suffix}"
                devices_item.add(rumps.MenuItem(label, callback=None))
        else:
            devices_item.add(rumps.MenuItem("No devices paired", callback=None))
        devices_item.add(rumps.separator)
        devices_item.add(
            rumps.MenuItem(
                "Add Apple TV (pair via Terminal)…",
                callback=self.on_add_device_tv,
            )
        )
        devices_item.add(
            rumps.MenuItem(
                "Detect iPhone/iPad (via USB trust)…",
                callback=self.on_add_device_ios,
            )
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
        self.menu.add(rumps.MenuItem("Quit Torch", callback=rumps.quit_application))

    def _set_icon(self, state: str) -> None:
        """Swap the menubar icon to the given state ('idle', 'refreshing',
        'stale', 'error'). Uses the rendered SF Symbol PNG if available,
        falls back to an emoji title if rendering failed at startup.
        """
        self._icon_state = state
        path = self._icon_paths.get(state) if self._icon_paths else None
        if path is not None:
            self.icon = str(path)
            self.title = None
        else:
            self.icon = None
            self.title = _EMOJI_FALLBACK.get(state, _EMOJI_FALLBACK[ICON_IDLE])

    def _rebuild(self, *, respect_refreshing: bool = False) -> None:
        """Rebuild menu + icon from current config.

        MUST be called on the main thread. Use _rebuild_async() from
        worker threads.

        When `respect_refreshing` is True, the icon is left at the
        refreshing-state symbol if a refresh is currently in progress.
        We use that during mid-refresh rebuilds so the progress
        indicator doesn't flicker away to idle and back.
        """
        try:
            self._build_menu()
            self._refresh_icon(respect_refreshing=respect_refreshing)
        except Exception as e:  # noqa: BLE001
            log.exception("menu rebuild failed: %s", e)

    def _rebuild_async(self) -> None:
        """Queue a rebuild to run on the main thread. Safe from any thread."""
        _on_main_thread(self._rebuild)

    def _notify_async(self, title: str, subtitle: str, message: str) -> None:
        """Queue a notification onto the main thread. Safe from any thread."""
        def _do() -> None:
            try:
                rumps.notification(title, subtitle, message)
            except Exception as e:  # noqa: BLE001
                log.warning("notification failed: %s", e)
        _on_main_thread(_do)

    def _set_icon_async(self, icon: str) -> None:
        """Queue an icon change onto the main thread. Safe from any thread."""
        def _do() -> None:
            self._set_icon(icon)
        _on_main_thread(_do)

    def _refresh_icon(self, *, respect_refreshing: bool = False) -> None:
        """Re-derive the menubar icon from current config state.

        When `respect_refreshing` is True, a refresh-in-progress icon is
        left alone so the menu rebuilds mid-refresh don't flicker the
        icon away from the refreshing symbol. When the refresh completes we call this
        with respect_refreshing=False to reset the icon to whatever the
        config now warrants.
        """
        if respect_refreshing and self._icon_state == ICON_REFRESHING:
            return
        # Cert problems are the most severe — they imply every refresh
        # will silently produce broken IPAs. Hoist them above per-IPA
        # errors in the icon priority.
        if self.cfg.cert_status.status in ("expired", "revoked", "missing"):
            self._set_icon(ICON_ERROR)
            return
        if any(i.status not in ("ok", "pending") for i in self.cfg.ipas):
            self._set_icon(ICON_ERROR)
            return
        if self.cfg.cert_status.status == "expiring":
            self._set_icon(ICON_STALE)
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
        # Callback is already on main thread — schedule worker immediately.
        self._background_check(force=True)

    def _refresh_one(self, filename: str) -> None:
        """Trigger a force-refresh of a single IPA by filename. Called from
        a main-thread menu callback; dispatches the actual work onto a
        worker."""
        log.info("per-app refresh requested: %s", filename)
        threading.Thread(
            target=self._do_refresh_worker,
            kwargs={"force": True, "only": [filename]},
            daemon=True,
            name=f"torch-refresh-{filename}",
        ).start()

    def _remove_ipa(self, filename: str) -> None:
        """Untrack an IPA (and delete the file from the runtime ipas/
        folder so it doesn't get re-added by sync_ipas_folder on the
        next startup). Main-thread callback."""
        log.info("removing IPA: %s", filename)
        if rumps.alert(
            title="Remove IPA?",
            message=(
                f"Stop tracking {filename} and delete it from the Torch "
                f"IPAs folder? The signed copy on your devices will not be "
                f"uninstalled."
            ),
            ok="Remove",
            cancel="Cancel",
        ) != 1:
            return

        # Delete source file from runtime dir + any signed variant
        try:
            source = paths.IPAS_DIR / filename
            if source.exists():
                source.unlink()
            signed_stem = Path(filename).stem
            for p in paths.SIGNED_DIR.glob(f"{signed_stem}-*.ipa"):
                p.unlink()
        except OSError as e:
            log.warning("cleanup during remove failed: %s", e)

        # Drop from config
        self.cfg.ipas = [i for i in self.cfg.ipas if i.filename != filename]
        self.cfg.save()
        self._rebuild()
        rumps.notification(
            "Torch", "IPA removed", f"{filename} is no longer tracked."
        )

    def on_toggle_pause(self, _sender: object) -> None:
        # Main-thread callback: safe to mutate config and rebuild directly.
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
        rumps.notification("Torch", "Settings", msg)

    def _on_hourly_tick(self, _timer: object) -> None:
        """rumps.Timer callback — runs on main thread, delegates to worker."""
        self._background_check(force=False)

    def _on_initial_kick(self, _timer: object) -> None:
        """One-shot timer to fire the initial refresh check after launch."""
        self._initial_kick.stop()
        self._background_check(force=False)

    def on_add_ipa(self, _sender: object) -> None:
        # We can't open an NSOpenPanel easily from rumps without PyObjC
        # incantations. For v1 we reveal the IPAs folder in Finder and
        # trust the user to drop files in. The polling watcher (hourly
        # tick, or the IPAs-folder sync at next app start) picks them up.
        subprocess.run(["open", str(paths.IPAS_DIR)])
        rumps.notification(
            "Torch",
            "Add an IPA",
            "Drop .ipa files into the folder that just opened, then click "
            "'Refresh Now' to pick them up.",
        )

    # --- device onboarding ---------------------------------------------------

    def on_add_device_tv(self, _sender: object) -> None:
        self._start_pairing_handoff(device_kind="tvOS")

    def on_add_device_ios(self, _sender: object) -> None:
        """Auto-detect a USB-trusted iPhone/iPad via tunneld and add it.

        iOS devices don't have a user-visible "pair with computer"
        screen the way tvOS does. Trust is established once via the
        "Trust This Computer" prompt when you first plug in the USB
        cable; from then on, usbmuxd handles the pairing transparently
        and exposes the device over both USB and WiFi. tunneld picks
        it up automatically. All we need to do here is enumerate
        tunneled devices that aren't already in our config and offer
        to add them.

        This runs entirely on the main thread (rumps menu callback),
        so rumps.alert() and config.save() are safe to call directly.
        """
        try:
            info = pymd3.tunneld_info()
        except pymd3.TunneldDownError:
            rumps.alert(
                title="Tunneld is down",
                message=(
                    "Couldn't reach the background tunnel service. "
                    "Make sure pymobiledevice3 tunneld is running."
                ),
            )
            return

        # Filter out devices we already track.
        tracked_ids = {d.pair_record_identifier for d in self.cfg.devices}
        candidates = [pid for pid in info.keys() if pid not in tracked_ids]

        if not candidates:
            rumps.alert(
                title="No new devices",
                message=(
                    "Tunneld doesn't see any devices that Torch "
                    "isn't already tracking.\n\n"
                    "If your iPhone or iPad isn't showing up, plug it "
                    "in with a USB cable and tap 'Trust This Computer' "
                    "on the device. After that it'll appear here "
                    "automatically."
                ),
            )
            return

        # For each candidate, try to reconcile against tunneld to get a
        # friendly name, UDID, and device class. Skip anything that
        # fails to reconcile (offline, lockdown failed, etc.).
        from .config import Device

        resolved: list[tuple[str, Device]] = []
        for pid in candidates:
            stub = Device(
                name=pid,
                pair_record_identifier=pid,
                udid=None,
                device_class="unknown",
                paired_at=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                pair_record_path=None,
            )
            try:
                reconciled = pymd3.reconcile_device(stub)
            except pymd3.Pymd3Error as e:
                log.warning("could not reconcile %s: %s", pid, e)
                continue
            resolved.append((pid, reconciled))

        if not resolved:
            rumps.alert(
                title="No reachable devices",
                message=(
                    "Tunneld knows about devices but none of them could "
                    "be queried. Make sure the target device is powered "
                    "on and on the same network."
                ),
            )
            return

        # Present each resolved device as a confirmation dialog. If the
        # user has multiple new devices we loop and ask one at a time.
        added_count = 0
        for pid, device in resolved:
            name = device.name or pid
            class_info = ""
            if device.device_class and device.device_class != "unknown":
                class_info = f" ({device.device_class}"
                if device.product_type:
                    class_info += f" · {device.product_type}"
                if device.product_version:
                    class_info += f" · {device.product_version}"
                class_info += ")"

            prompt = (
                f"Found: {name}{class_info}\n\n"
                f"Add this device to Torch? All tracked IPAs will "
                f"be auto-targeted at it."
            )
            if rumps.alert(
                title="Add device?", message=prompt, ok="Add", cancel="Skip"
            ) != 1:
                continue

            # Append to config, auto-target existing IPAs, save, rebuild.
            self.cfg.devices.append(device)
            for ipa in self.cfg.ipas:
                if pid not in ipa.target_devices:
                    ipa.target_devices.append(pid)

            # Best-effort register with Apple portal.
            if device.udid:
                try:
                    plumesign.register_device(device.udid, device.name)
                except plumesign.PlumesignError as e:
                    log.debug("register_device %s: %s", device.udid, e)

            added_count += 1

        if added_count > 0:
            self.cfg.save()
            self._rebuild()
            rumps.notification(
                "Torch",
                "Device added",
                f"{added_count} device{'s' if added_count != 1 else ''} "
                f"added. They'll be refreshed on the next cycle.",
            )

    def _start_pairing_handoff(self, *, device_kind: str) -> None:
        """Hand the pairing flow off to a Terminal window.

        Runs entirely on the main thread (no worker, no cross-thread
        modals). We open Terminal.app with the pairing command pre-
        filled, then start a rumps.Timer that polls
        ~/.pymobiledevice3/ every 3 seconds for up to 3 minutes
        looking for a new remote_*.plist file. When one appears,
        we reconcile it against tunneld, register with Apple, and
        update the menu.

        This is deliberately less fancy than a native modal flow
        because rumps + worker threads + Cocoa modals is a footgun
        that crashed the app the first time we tried it.
        """
        if device_kind == "tvOS":
            msg = (
                "On your Apple TV, go to:\n\n"
                "Settings → General → Remotes and Devices → "
                "Remote App and Devices\n\n"
                "Leave that screen open, then click OK. A Terminal "
                "window will open to collect the 6-digit PIN."
            )
        else:
            msg = (
                "On your iPhone or iPad (iOS 17+), go to:\n\n"
                "Settings → Privacy & Security → Developer Mode → "
                "Pair Device\n\n"
                "Leave that screen open, then click OK. A Terminal "
                "window will open to collect the 6-digit PIN."
            )

        if rumps.alert(
            title="Add device", message=msg, ok="OK", cancel="Cancel"
        ) != 1:
            return

        # Record the set of pair records present RIGHT NOW so we can
        # detect which one is new after the user finishes in Terminal.
        self._pairing_baseline: set[str] = {
            p.stem.removeprefix("remote_")
            for p in paths.PYMD3_PAIR_RECORDS_DIR.glob("remote_*.plist")
        } if paths.PYMD3_PAIR_RECORDS_DIR.exists() else set()

        # Open Terminal running the pairing command. Using osascript so
        # Terminal.app is brought to the foreground cleanly.
        #
        # We must use the absolute path to the venv's pymobiledevice3
        # binary rather than a bare command name. The menubar process
        # runs under a LaunchAgent with PATH pointing at the venv bin,
        # but when we ask Terminal.app to launch a new window, that
        # window inherits the user's normal shell PATH — which does
        # NOT include .venv/bin. A bare `pymobiledevice3` resolves to
        # "command not found" in the handoff terminal. The absolute
        # path dodges the PATH problem entirely.
        pymd3_bin = str(
            Path(sys.executable).parent / "pymobiledevice3"
        )
        script = (
            'tell application "Terminal" to activate\n'
            'tell application "Terminal" to do script '
            f'"{pymd3_bin} remote pair; '
            'echo; echo \\"Pairing finished. You can close this window.\\""'
        )
        subprocess.run(["osascript", "-e", script])

        rumps.notification(
            "Torch",
            "Pairing in progress",
            "Enter the 6-digit code in the Terminal window that just "
            "opened. I'll pick up the new device automatically.",
        )

        # Poll for a new pair record every 3 seconds for up to 3 minutes.
        self._pairing_deadline = time.monotonic() + 180
        self._pairing_timer = rumps.Timer(self._poll_for_new_pair_record, 3.0)
        self._pairing_timer.start()

    def _poll_for_new_pair_record(self, _timer: object) -> None:
        """rumps.Timer callback — main thread. Checks for a new pair record."""
        if time.monotonic() > self._pairing_deadline:
            log.info("pairing deadline reached; stopping poll")
            self._pairing_timer.stop()
            rumps.notification(
                "Torch",
                "Pairing timed out",
                "No new device appeared in the last 3 minutes. "
                "If you finished the pairing in Terminal, click "
                "'Refresh Now' to try again.",
            )
            return

        current = (
            {
                p.stem.removeprefix("remote_")
                for p in paths.PYMD3_PAIR_RECORDS_DIR.glob("remote_*.plist")
            }
            if paths.PYMD3_PAIR_RECORDS_DIR.exists()
            else set()
        )
        new_ids = current - self._pairing_baseline
        if not new_ids:
            return

        new_pair_id = next(iter(new_ids))
        log.info("discovered new pair record: %s", new_pair_id)
        self._pairing_timer.stop()
        # Kick the post-pair reconcile to a worker so we don't block
        # the main thread while tunneld connects to the new device.
        threading.Thread(
            target=self._post_pair_reconcile_worker,
            args=(new_pair_id,),
            daemon=True,
            name="torch-post-pair",
        ).start()

    def _post_pair_reconcile_worker(self, pair_id: str) -> None:
        """Worker-thread post-pair: reconcile + register + update config.
        All UI updates marshal back through _notify_async / _rebuild_async."""
        try:
            self._post_pair_reconcile(pair_id, fallback_name=pair_id)
        except Exception as e:  # noqa: BLE001
            log.exception("post-pair reconcile failed: %s", e)
            self._notify_async(
                "Torch",
                "Pairing incomplete",
                f"Pair record saved but registration failed: {e}",
            )
            return

        self._notify_async(
            "Torch",
            "Device added",
            "The new device is ready to receive app refreshes.",
        )
        self._rebuild_async()

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

    # --- worker threads ------------------------------------------------------

    def _background_check(self, *, force: bool = False) -> None:
        """Spawn a worker thread that runs a full refresh cycle.

        Only touches rumps / self.menu / self.title through the *_async()
        helpers, which marshal back to the main thread via AppHelper. This
        is critical: calling rumps APIs from worker threads silently kills
        the app in Cocoa-land.
        """

        def run() -> None:
            self._do_refresh_worker(force=force)

        t = threading.Thread(
            target=run, daemon=True, name="torch-refresh-worker"
        )
        t.start()

    def _do_refresh_worker(
        self, *, force: bool, only: list[str] | None = None
    ) -> None:
        """Full (or filtered) refresh cycle. Runs on a worker thread;
        MUST NOT touch rumps APIs directly — everything UI goes through
        *_async() helpers."""
        self._set_icon_async(ICON_REFRESHING)

        def progress(msg: str) -> None:
            log.debug("progress: %s", msg)

        try:
            cfg_snapshot = cfgmod.Config.load()
            succeeded, failed = refresh.refresh_all(
                cfg_snapshot, force=force, only=only, progress=progress
            )
        except refresh.RefreshAborted as e:
            log.warning("refresh aborted: %s", e)
            self._set_icon_async(ICON_ERROR)
            self._notify_async("Torch", "Refresh aborted", str(e))
            self._reload_and_rebuild_async()
            return
        except Exception as e:  # noqa: BLE001
            log.exception("unexpected refresh error: %s", e)
            self._set_icon_async(ICON_ERROR)
            self._notify_async("Torch", "Refresh failed", str(e))
            self._reload_and_rebuild_async()
            return

        if succeeded == 0 and failed == 0:
            # No-op refresh (nothing was stale). Silent.
            pass
        elif failed == 0:
            self._notify_async(
                "Torch",
                "Refresh complete",
                f"{succeeded} app{'s' if succeeded != 1 else ''} refreshed",
            )
        else:
            self._notify_async(
                "Torch",
                "Refresh partial",
                f"{succeeded} succeeded, {failed} failed",
            )
        self._reload_and_rebuild_async()

    def _reload_and_rebuild_async(self) -> None:
        """Reload config from disk and rebuild the menu, on the main thread.

        After a refresh cycle completes, we want the icon to return to its
        natural derived state (idle / stale / error), NOT stay at
        the refreshing symbol. Do that
        by rebuilding with respect_refreshing=False.
        """
        def _do() -> None:
            try:
                self.cfg = cfgmod.Config.load()
                self._rebuild(respect_refreshing=False)
            except Exception as e:  # noqa: BLE001
                log.exception("reload-and-rebuild failed: %s", e)
        _on_main_thread(_do)

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
                try:
                    app_ref._background_check(force=False)
                except Exception as e:  # noqa: BLE001
                    log.exception("wake handler failed: %s", e)

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
