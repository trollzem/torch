# UI

> **Maintainer note:** update this doc when (a) a new menu item or
> top-level callback is added in `ui.py:_build_menu`, (b) the
> status icons in `_build_menu` or `_refresh_icon` change,
> (c) the threading rules around `_on_main_thread` /
> `_run_on_main_and_wait` change, (d) the watcher tick
> (`_on_config_watch_tick`) changes intervals or behavior, or
> (e) a new dialog is added in `ui_dialogs.py`.

## What this is

The UI is a `rumps`-based menubar app. `src/torchapp/ui.py`
defines `TorchApp(rumps.App)` and wires up:

- The menubar icon (SF Symbol PNG, rendered to disk on first
  launch)
- The menu structure (status header, Apps submenu with per-IPA
  targets, Devices submenu, Refresh / Pause / Open folders /
  Quit)
- Three NSTimer-backed periodic ticks (hourly refresh, 5s config
  watcher, one-shot initial kick)
- Wake-from-sleep observer via NSWorkspace
- Apple ID login flow (collecting credentials + 2FA via modal
  dialogs)
- Apple TV pairing flow (driving a pair_helper subprocess)
- iOS auto-detect worker
- All cross-thread marshaling for the above

## The non-negotiable threading rule

**rumps is Cocoa-main-thread-only.** Calling `rumps.notification`,
`rumps.alert`, `rumps.Window`, or mutating `self.menu` /
`self.title` / `self.icon` from a background thread *silently
kills the process* -- no traceback, no log line, Cocoa just
aborts and launchd respawns Torch.

Two helpers exist for crossing thread boundaries safely:

```python
def _on_main_thread(callable_) -> None:
    """Fire-and-forget: marshal a zero-arg callable to main."""
    AppHelper.callAfter(callable_)

def _run_on_main_and_wait(func, *args, **kwargs):
    """Synchronous: marshal to main, block until it returns the value."""
    # ... AppHelper.callAfter + threading.Event ...
```

Every worker-thread UI touch goes through one of these. Use the
synchronous form for modal dialogs that need a return value
(`prompt_apple_id_email`, `prompt_2fa_code`, etc.); use the
fire-and-forget form for notifications and menu rebuilds
(`_notify_async`, `_rebuild_async`, `_set_icon_async`).

Periodic work uses `rumps.Timer` (NSTimer under the hood; fires
on the main thread). **Never** `threading.Timer`.

CLAUDE.md discovery 5 has the historical context for this rule.

## Menu structure

```
+----------------------------------------------------------+
| Status: 3/3 apps fresh - 2h ago                          |
| Cert: 304 days left                                      |
| Apple ID: eissahazem@gmail.com (click to re-login)       |
| ---                                                      |
| Apps                                                     |
|   YouTube-iOS - iOS - 6d 23h left                        |
|     Refresh now                                          |
|     Remove from tracking                                 |
|     Install on devices                                   |
|       [v] Hazem (iPhone17,3)                             |
|       [v] Hazem Eissa's iPad (iPad15,4)                  |
|     ---                                                  |
|     Targets: Hazem, Hazem Eissa's iPad                   |
|     Bundle: com.google.ios.youtube                       |
|     Signed as: com.google.ios.youtube.3G6AP3U89B         |
|     Signed: 2h ago                                       |
|   ... (more IPAs)                                        |
|   ---                                                    |
|   Add IPA...                                             |
| Devices                                                  |
|   Habibi TV - tvOS - AppleTV14,1 26.4 - 2/3 apps         |
|   Hazem - iOS - iPhone17,3 26.4 - 1/3 apps               |
|   ---                                                    |
|   Add Apple TV...                                        |
|   Check for new iPhones/iPads now                        |
| ---                                                      |
| Refresh Now                                              |
| Pause Auto-Refresh                                       |
| ---                                                      |
| Open IPAs Folder                                         |
| View Log                                                 |
| Reveal Signed Folder                                     |
| ---                                                      |
| Quit Torch                                               |
+----------------------------------------------------------+
```

## Status icons

The menubar icon adapts to overall state. Four SF Symbol
templates are rendered to PNG at first launch into
`~/Library/Application Support/Torch/icons/`:

| State | SF Symbol | When |
|---|---|---|
| `idle` | `flame.fill` | every IPA is fresh and ok |
| `refreshing` | `arrow.triangle.2.circlepath` | a refresh worker is mid-flight |
| `stale` | `exclamationmark.triangle.fill` | at least one IPA needs refresh OR is in a soft-failure state |
| `error` | `xmark.octagon.fill` | cert expired/revoked/missing OR a hard-failure IPA exists |

`_refresh_icon` is the priority resolver:

```
if cert in (expired, revoked, missing): error
elif any ipa in hard-failure status: error
elif cert == expiring: stale
elif any ipa in (device-offline, device-at-cap, partial): stale
elif any ipa needs_refresh and not is_frozen: stale
else: idle
```

Soft-failure statuses promote to `stale` (warning), not `error`,
because they're retry-friendly and don't represent broken state.

Per-IPA status glyphs in the menu (rendered as the leading
character of the submenu title):

| Status | Glyph |
|---|---|
| ok | OK marker |
| pending | dot |
| sign-failed, install-failed, missing-source | X mark |
| auth-error, needs-login, app-id-limit, tunneld-down | warning |
| device-offline | sleep emoji |
| device-at-cap | no-entry sign |
| partial | half-filled circle |
| no-targets | dot |

(The exact characters are in `ui.py:_build_menu`'s `status_icon`
dict; using ASCII names here so the doc isn't full of emoji.)

## Three NSTimer ticks

```python
self._hourly_timer       = rumps.Timer(_on_hourly_tick, 3600.0)
self._config_watch_timer = rumps.Timer(_on_config_watch_tick, 5.0)
self._initial_kick       = rumps.Timer(_on_initial_kick, 2.0)   # one-shot
```

1. **Hourly tick** -- the main refresh trigger. Calls
   `_background_check(force=False)` which spawns a worker
   thread, which calls `refresh.refresh_all`.
2. **Config watcher tick (5s)** -- cheap on-disk reconciliation.
   Checks `config.json` mtime + IPAs folder contents. If either
   changed, reload `cfg` and rebuild the menu. Also kicks the
   iOS auto-detect worker via `_maybe_kick_auto_detect_worker`.
3. **Initial kick (2s, one-shot)** -- fires shortly after
   launch, kicks off a refresh check so the user doesn't wait
   an hour for the first cycle after a Mac restart.

## Wake-from-sleep observer

```python
self._install_wake_observer()
```

Registers an NSWorkspace observer for
`NSWorkspaceDidWakeNotification`. When the Mac wakes, fires
`_background_check(force=False)` immediately. Without this,
refreshes scheduled during sleep would never run (NSTimers
don't fire while the Mac is asleep) and the user could wake to
expired profiles.

## Apple ID login flow

User clicks the "Apple ID: ..." row -> `on_apple_id_login` ->
spawns `_apple_id_login_worker` on a worker thread.

The worker:

1. Prompts for email via `_run_on_main_and_wait(prompt_apple_id_email)`.
2. Prompts for password via
   `_run_on_main_and_wait(prompt_apple_id_password)`. The
   password field is a plain `NSTextField` (not secure) because
   `rumps.Window` doesn't expose secure fields; the prompt makes
   this visible to the user.
3. Calls `plumesign.login(email, password, tfa_callback)` which
   pexpect-drives the `plumesign account login` subprocess. The
   `tfa_callback` re-marshals to main via
   `_run_on_main_and_wait(prompt_2fa_code)` each time plumesign
   asks (up to 3 retries -- see [signing.md](signing.md)).
4. On success, store password in macOS Keychain via
   `keychain.set_password`, clear frozen state on every IPA,
   `_notify_async("Signed in")`, `_rebuild_async()`,
   auto-kick a refresh.

The auto-clear-on-relogin is what un-freezes IPAs that hit
`consec_fail=3` against a stale GSA session. Without it, a
correct re-login would still leave the IPAs frozen until the
user manually intervened.

## iOS auto-detect worker

Discussed in detail in [devices.md](devices.md). Key UI-side
piece: lives in `ui.py:_auto_detect_ios_worker`, kicked by
`_maybe_kick_auto_detect_worker` from `_on_config_watch_tick`.
Guarded by `self._auto_add_in_flight` to prevent multiple
concurrent workers.

## Adding IPAs

`on_add_ipa` opens an NSOpenPanel via PyObjC (filter:
`com.apple.application-bundle-archive` UTType). Selected files
get copied into `paths.IPAS_DIR`; the 5s watcher tick picks them
up via `sync_ipas_folder`.

The "Open IPAs Folder" menu item is the fallback for users who
want to drag-and-drop in Finder.

## Logs and ergonomics

- "View Log" opens `~/Library/Application Support/Torch/logs/torch.log`
  in Console.app.
- "Reveal Signed Folder" opens `~/Library/Application Support/Torch/signed/`
  in Finder. Used when a user wants to manually grab a signed
  IPA for offline install.
- "Open IPAs Folder" opens the source folder. Drag-and-drop works.

## Key files

- `src/torchapp/ui.py` -- `TorchApp`, all menu wiring, all timer
  ticks, all worker dispatching
- `src/torchapp/ui_dialogs.py` -- `prompt_apple_id_email`,
  `prompt_apple_id_password`, `prompt_2fa_code`,
  `prompt_pairing_pin`
- `src/torchapp/icons.py` -- SF Symbol PNG rendering at first
  launch
- `src/torchapp/keychain.py` -- macOS Keychain wrapper for the
  Apple ID password

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| App silently quits, launchd respawns | rumps API call from worker thread | grep for the offender; wrap in `_on_main_thread` |
| Menubar shows stale state | watcher tick stopped (rare) | restart Torch via `launchctl kickstart -k gui/$(id -u)/com.torch.app` |
| Login dialog never appears | login worker died before reaching `_run_on_main_and_wait` | check torch.log for traceback |
| Notification doesn't show | macOS Notification Center permission missing | System Settings -> Notifications -> Torch |
| "Add IPA..." opens nothing | NSOpenPanel failed silently | check the log; rare; restart Torch |

## Related docs

- [architecture.md](architecture.md) -- how UI fits into the bigger picture
- [signing.md](signing.md) -- the Apple ID login flow this drives
- [devices.md](devices.md) -- the pairing + auto-detect workers this hosts
- [refresh.md](refresh.md) -- the refresh triggers this fires
- [config.md](config.md) -- the on-disk state the menu reflects
- CLAUDE.md discovery 5 -- the rumps main-thread rule
