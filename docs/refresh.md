# Refresh

> **Maintainer note:** update this doc when (a) `refresh_one` or
> `refresh_all` in `refresh.py` change shape, (b) a new status
> string is added (cross-reference [architecture.md](architecture.md)
> taxonomy), (c) `MAX_CONSECUTIVE_FAILURES` or
> `refresh_interval_days` change, (d) the cert rotation logic in
> `refresh_cert_status` changes, or (e) the freeze / soft-failure
> contract changes.

## What this is

The refresh orchestrator is the brain of Torch. It decides when
to re-sign each IPA, drives the sign + install pipeline for each,
classifies outcomes, and persists state. Lives in
`src/torchapp/refresh.py`.

It runs on three triggers, all of which converge on `refresh_all`:

- **Hourly NSTimer tick** from `ui.py:_on_hourly_tick`. Fires
  every 3600s on the main Cocoa thread, then dispatches the work
  onto a worker via `_background_check`.
- **Wake-from-sleep observer** registered in
  `_install_wake_observer`. When the Mac comes out of sleep,
  fire an immediate check.
- **Manual "Refresh Now"** from the menubar
  (`ui.on_refresh_now`). Same code path, force=True so the
  needs_refresh predicate is bypassed.

## The refresh interval is derived, not configured

The cadence comes from the provisioning profile's own
`TimeToLive`, read out of `embedded.mobileprovision` after every
sign and cached on the IPA as `profile_ttl_days` /
`profile_expires_at`. Apple issues **7-day** profiles on a free
Apple ID and **365-day** profiles on a paid Developer Program
membership, so the tier is self-evident from the artifact and
never has to be configured or guessed.

```python
def effective_interval_days(ipa: IPA, settings: Settings) -> int:
    ttl = ipa.profile_ttl_days
    if ttl is None or ttl <= 0:
        return settings.refresh_interval_days   # fallback, never signed yet
    safety = max(MIN_REFRESH_SAFETY_DAYS, round(ttl * REFRESH_SAFETY_FRACTION))
    return max(1, ttl - safety)
```

The reserve is `max(5% of TTL, 2 days)`, which reproduces the
historical free-tier behaviour exactly and scales sanely upward:

| Tier | Profile TTL | Reserve | Refresh at | Hourly retries in reserve |
|------|-------------|---------|------------|---------------------------|
| Free | 7 days      | 2 days  | day 5      | 48                        |
| Paid | 365 days    | 18 days | day 347    | 432                       |

```
Day 0    Day 5         Day 7          (free tier)
|--------|--------------|-->
sign     refresh_due    profile expires
         |
         |--- 48 hourly retries ---|
```

`Settings.refresh_interval_days` (default 5) is now only a
fallback for IPAs with no parsed profile yet — a freshly added
IPA signs immediately anyway (`last_signed_at is None`), so in
practice it rarely applies.

**Why this is derived rather than a setting.** Until 2026-08-10
the 7-day lifetime was hardcoded in three places (the cadence,
`device_expires_at`, and the 3-app cap). Upgrading the Apple ID
to a paid membership therefore left Torch re-signing every 5 days
against a 365-day profile — ~73x more often than needed — and,
combined with the all-targets install bug below, reinstalled
YouTube on the one reachable iPhone **every hour**, killing the
app mid-use each time via the installd pre-kill. Deriving from
the artifact means an upgrade *or* a lapse takes effect on the
next sign with no user action.

## Only stale devices get reinstalled

`any_target_device_stale()` fires the refresh when **any** target
lags, but `refresh_one()` must only install to the targets that
actually lag — `_devices_needing_install()` filters to devices
whose install is missing or older than the effective interval.

This split matters because the trigger is per-device while the
action used to be all-devices: one chronically-offline target
(an asleep iPad, a second phone) kept an IPA permanently "stale",
so every cycle re-signed and reinstalled on the healthy targets
too. Each of those reinstalls runs the DVT pre-kill
(`docs/install.md`, CLAUDE.md discovery #4), which terminates the
running app — the user-visible symptom was YouTube crashing
hourly, mid-video, on the only phone that was working.

`force=True` (the manual "Refresh Now" path) bypasses the filter
and reinstalls everywhere, which is what a user pressing that
button means.

## Free-tier-only restrictions

`FREE_TIER_DEVICE_APP_CAP = 3` applies to free Apple IDs only;
paid memberships have no per-device app ceiling. `refresh_one()`
skips the cap check entirely when `is_paid_tier(ipa)` — i.e. when
the profile TTL is at or above `PAID_TIER_TTL_THRESHOLD_DAYS`
(30). Gating on the observed TTL rather than a stored account
flag means the restriction re-arms automatically if a membership
lapses back to free.

## needs_refresh and is_frozen

```python
def needs_refresh(ipa: IPA, interval_days: int, now=None) -> bool:
    """True if the IPA has never been signed or was signed >= interval_days ago."""

def is_frozen(ipa: IPA) -> bool:
    return ipa.consecutive_failures >= MAX_CONSECUTIVE_FAILURES  # 3
```

`refresh_all` iterates `cfg.ipas` and runs `refresh_one(ipa)`
only when `needs_refresh AND not is_frozen` (or `force=True`).
The freeze gate is what stops Torch from hammering Apple
indefinitely when something is genuinely broken — sign errors
that don't have a soft-failure remediation will burn the strikes
and the IPA goes silent until intervention.

Frozen IPAs auto-recover in three ways:

1. **User re-logs in via menubar** (`ui.py:_apple_id_login_worker`).
   On successful login, every IPA's `consecutive_failures` is
   reset to 0 and `needs-login`/`sign-failed`/`auth-error`
   statuses are bumped back to `pending`. An immediate refresh
   tick is auto-kicked.
2. **Manual config edit** — set `consecutive_failures` to 0 in
   `config.json`. The 5s watcher tick reloads.
3. **Manual force-refresh** — clicking "Refresh now" on the
   individual IPA submenu calls `_refresh_one` with `force=True`,
   bypassing `is_frozen`.

There's deliberately no automatic decay-over-time today. A
strikes-decay rule was discussed (decrement by 1 every N quiet
hours) but not implemented; soft-failure classes cover the bulk
of "we want to keep retrying" cases without bumping strikes in
the first place.

## Soft vs hard failures

The full taxonomy is in [architecture.md](architecture.md). The
distinction that matters here is which `_record_*` helper sets
the status:

- `_record_failure(ipa, status, error)` -- bumps
  `consecutive_failures`. Use for genuine signing or install
  errors the user can't work around by waiting.
- `_record_soft_failure(ipa, status, error)` -- sets status +
  message but does NOT bump strikes. Use for transient /
  user-fixable states (device-offline, device-at-cap, partial,
  needs-login).
- `_record_success(ipa)` -- resets `consecutive_failures` to 0,
  clears `last_error`, sets `status="ok"`, updates
  `last_signed_at` and `last_installed_at`.

`refresh_one`'s outcome dispatch (after the per-device install
loop) classifies the cycle as one of:

- All successful -> `_record_success` (status="ok")
- Any hard fail -> `_record_failure(status="install-failed")`
- Any at-cap (no hard fails) ->
  `_record_soft_failure(status="device-at-cap")`
- Some success + some offline ->
  `_record_soft_failure(status="partial")` + updates
  `last_signed_at`
- All offline -> `_record_soft_failure(status="device-offline")`

## Cert rotation detection

`refresh_cert_status(cfg)` queries Apple's developer portal once
per refresh cycle and updates `cfg.cert_status` in place. Produces
one of:

| status | meaning |
|---|---|
| `ok` | cert exists, expires >`CERT_EXPIRY_WARNING_DAYS` away |
| `expiring` | cert exists, inside the warning window |
| `expired` | expiration_date in the past |
| `revoked` | cert exists but `status != "Issued"` |
| `missing` | Apple's portal has no issued cert for this team |
| `unknown` | query failed (network, auth, parse) -- previous snapshot is left in place |

Never raises -- the caller (`refresh_all`) calls it before the
per-IPA loop and continues regardless. The status drives:

- The menubar icon (`ui.py:_refresh_icon` -- expired/revoked/missing
  promote to ICON_ERROR; expiring promotes to ICON_STALE)
- The "Cert: X days left" header line in the menu
- The `Cert expired -- re-login required` notification on first
  detect of a bad state

Apple's developer cert lives 364 days. Re-login is what rotates
it -- the menubar Apple ID re-login flow refreshes the cert
along with the GSA session.

## Free-tier per-device app cap

Apple enforces a hard 3-apps-per-device limit on free Personal
Team profiles. `FREE_TIER_DEVICE_APP_CAP = 3`. The cap is checked
two places:

1. **At refresh time** -- `device_has_room(cfg, device, including=ipa)`
   counts how many tracked IPAs would land on this device after
   the next refresh. If projected count > 3, that device is
   filtered out of the install loop for this IPA. If ALL
   compatible devices would exceed the cap, the IPA gets
   `status="device-full"` (hard fail, bumps strikes -- this is
   correct because Apple will reject every retry until the user
   reduces the IPA set).

2. **At Apple install time** -- if the user has other sideloaders
   installed (Sideloadly, AltStore), Torch's tracker
   undercounts. Apple's `ApplicationVerificationFailed 0xe8008021`
   rejection at 80% install progress lists the bundle IDs
   currently occupying slots. `installer._extract_capped_bundles`
   parses that list and surfaces it via `DeviceAtCapError`
   (soft, no strikes -- the install will succeed automatically
   when the user frees a slot). See [install.md](install.md).

## reconcile_devices

Called once per refresh cycle (before the per-IPA loop) to
update `cfg.devices` with current `udid`, `device_class`, name,
etc. **Mutates in place** rather than reassigning
`cfg.devices = [...]`:

```python
for i in range(len(cfg.devices)):
    try:
        cfg.devices[i] = pymd3.reconcile_device(cfg.devices[i])
    except (TunnelNotFoundError, LockdownError, TunneldDownError) as e:
        log.warning(...)
        # leave entry unchanged
```

This avoids racing with the iOS auto-detect worker (which can
concurrently `append` to `cfg.devices` from a worker thread).
Reassigning the list would silently drop the auto-detect
worker's newly-added device.

Atomic save via temp+rename in `config.py:Config.save` covers
the same race at the disk-write layer.

## refresh_all locking

A module-level `_refresh_lock` prevents overlapping refresh
cycles. The hourly tick, the wake observer, the menubar "Refresh
Now" button, and the initial-kick timer all funnel through
`refresh_all` which acquires the lock at entry; concurrent
attempts log "refresh already in progress; skipping duplicate
call" and return. Without this two refreshes started a few
seconds apart could double-sign and double-install.

## tunneld-keepalive watchdog

Separate root LaunchDaemon, not part of refresh.py, but
operationally part of the refresh story. Detail in
[launchd.md](launchd.md). Summary:

- Runs `python3 -m torchapp.tunneld_keepalive` every 30 min
- Restarts tunneld via `launchctl kickstart -k system/com.torch.tunneld`
  if uptime > 7d OR (uptime > 6h AND inventory empty AND pair
  records on disk)
- Preempts the 44-day mDNS-state-rot failure we hit 2026-06-20

Without the watchdog, refresh would silently fail every cycle
once tunneld went stale (every device would show
`device-offline`). The watchdog is the load-bearing piece of
infrastructure that makes the "set it and forget it" promise
realistic.

## Key files

- `src/torchapp/refresh.py` -- the whole orchestrator
- `src/torchapp/installer.py` -- the install dispatcher
  refresh_one calls
- `src/torchapp/plumesign.py` -- the sign step refresh_one calls
- `src/torchapp/ui.py` `_background_check`, `_do_refresh_worker`,
  `_on_hourly_tick`, `_install_wake_observer` -- the trigger
  surface
- `src/torchapp/tunneld_keepalive.py` -- separate watchdog
  daemon

## Common failure modes

| Symptom | Likely cause | Notes |
|---|---|---|
| Every IPA stuck at `device-offline` | tunneld stale or empty | watchdog should catch within 30 min; manual kick: `sudo launchctl kickstart -k system/com.torch.tunneld` |
| Every IPA frozen at `consec_fail=3 needs-login` | plumesign session expired AND landed before the soft-fail fix | edit `config.json` to reset; future iterations classify needs-login as soft so this won't repeat |
| Every IPA frozen at `consec_fail=3 sign-failed` with anisette-like errors | iCloud signed out on the Mac | sign back in, then click Apple ID re-login (auto-clears strikes) |
| "refresh already in progress; skipping duplicate call" | overlap of hourly + wake + initial-kick | benign; one of the triggers fired while another was running |
| `refresh complete: 0 succeeded, N failed` for hours | check `cfg.cert_status` for `expired`/`revoked`/`missing` | re-login refreshes the cert |

## Related docs

- [architecture.md](architecture.md) -- full status taxonomy and end-to-end flow diagram
- [signing.md](signing.md) -- the sign step refresh drives
- [install.md](install.md) -- the install step refresh drives
- [devices.md](devices.md) -- the source of compatible target devices
- [ui.md](ui.md) -- the triggers (hourly timer, wake observer, manual button)
- [launchd.md](launchd.md) -- tunneld watchdog
