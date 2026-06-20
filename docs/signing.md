# Signing

> **Maintainer note:** update this doc when (a) the patched plumesign
> binary is rebuilt against a new Impactor version, (b) the Apple ID
> login flow changes in `ui.py:_apple_id_login_worker` or
> `plumesign.login`, (c) a new exception class is added to
> `plumesign.py`, (d) the anisette source (AOSKit vs remote)
> changes, or (e) any Apple GSA error code shape we parse changes.

## What this is

The signing subsystem turns an unsigned `.ipa` from
`~/Library/Application Support/Torch/ipas/` into a signed copy in
`signed/`, using the user's free Apple ID via Apple's developer
provisioning APIs. Everything runs through a single patched Rust
binary (`bin/plumesign`) wrapped by `src/torchapp/plumesign.py`.

## How it works

### The patched plumesign binary

Upstream: [CLARATION/Impactor](https://github.com/CLARATION/Impactor)
at tag `v2.2.3`. Our fork lives in `vendor/impactor-tvos.patch`. The
patch enables two non-default Cargo features and a runtime tweak:

1. **`subPlatform: "tvOS"` body parameter** on
   `/QH65B2/ios/addDevice.action`. Without this, Apple returns an
   iOS+xrOS+visionOS profile that Apple TV rejects with
   `ApplicationVerificationFailed: A valid provisioning profile for
   this executable was not found`. With it, Apple returns a real
   tvOS profile. Triggered by `PLUME_FORCE_TVOS=1` env var which
   `refresh.py` sets when signing tvOS IPAs.

2. **Native macOS anisette via AOSKit**. Upstream omnisette enables
   only `remote-anisette-v3` which proxies auth through
   `ani.stikstore.app`. The patch enables `aos-kit` so omnisette
   dlopens `/System/Library/PrivateFrameworks/AOSKit.framework`
   and generates anisette headers from the local macOS iCloud
   session. Remote anisette is kept as a fallback if
   `AOSKitAnisetteProvider::new()` fails at construction time.

3. **`PLUME_DELETE_AFTER_FINISHED=1`** disables the staging-dir
   cleanup so we can scrape the signed bundle out before plumesign
   tears it down (workaround for the upstream archive bug below).

Full discovery context for each patch lives in CLAUDE.md
discoveries 1, 2, and 3.

### Sign step (`plumesign.sign_ipa`)

The wrapper invokes `plumesign sign --apple-id eissahazem@gmail.com
--package <in.ipa> -o <out.ipa>` and works around plumesign v2.2.3's
output bug: the binary copies the *input* file to the output path
instead of re-archiving the signed staging dir. To recover the
real signed bundle we:

1. Set `PLUME_DELETE_AFTER_FINISHED=1` so the staging dir survives.
2. Scrape the staging path from stderr by matching
   `writing signed main executable to .../plume_stage_<uuid>/Payload/...`.
3. Re-zip `Payload/` ourselves with `zip -r -y -q`. The `-y` flag
   preserves symlinks which some frameworks need.
4. Run `codesign --verify --deep --strict` on the staged `.app`
   before re-archiving; defensive gate against malformed signed
   bundles that occasionally come out of plumesign v2.2.3.

The re-zip + verify dance lives in `plumesign.py`. The verify gate
is at `_verify_signed_bundle`.

### Apple ID login flow (`plumesign.login`)

Interactive login via `pexpect.spawn`. Plumesign prompts at stdin
when it needs the 2FA code; the wrapper expect()s on
`"Enter 2FA code:"` and `"Successfully logged in"`. Three retry
attempts cover the case where Apple rejects the first code and
plumesign re-prompts. After 3 rejections we surface
`Apple rejected 2FA code 3 times in a row` to the user.

The matching menubar flow is `ui.py:_apple_id_login_worker`. It
collects email/password/2FA through `rumps.Window` dialogs (via
`ui_dialogs.py`), all marshaled to the main Cocoa thread by
`_run_on_main_and_wait`. On success it stores the password in the
macOS Keychain (`keychain.set_password`) and *clears all frozen
state on every tracked IPA*: a successful re-login resets
`consecutive_failures` to 0 and bumps statuses out of
`needs-login`/`sign-failed`/`auth-error` back to `pending`, then
auto-kicks a refresh.

### Error classification (`_classify_failure_and_raise`)

Plumesign exits non-zero with a Rust traceback on stderr. We
inspect the stderr text and raise one of:

| Match | Exception | What it means |
|---|---|---|
| "maximum" + "app id" | `PlumesignAppIdLimitError` | Apple's 10-new-bundle-IDs-per-week cap hit |
| "no accounts" / "no account selected" | `PlumesignNotLoggedInError` | session was never established |
| "session has expired" / "please log in" | `PlumesignNotLoggedInError` | GSA session token rotated (Apple Dev API 1100) |
| "authentication" / "unauthorized" / "srp" | `PlumesignAuthError` | SRP password verification failure (often anisette, not actual password) |
| "unexpectedendofeventstream" / "plist error" | `PlumesignAuthError` | Apple returned empty body, usually session drift |
| anything else | `PlumesignSignError` | generic signing failure |

The session-expired branch maps to a soft failure in `refresh.py`
so a stale session doesn't burn the 3-strike freeze counter. See
[refresh.md](refresh.md) for the soft-vs-hard classification.

### register_device (`plumesign.register_device`)

Idempotent on Apple's side; "already exists" errors are expected
when re-registering a device. Called by `refresh._ensure_devices_registered`
which swallows `PlumesignError` so a successful re-registration
doesn't tank the run.

## Anisette dependency on iCloud sign-in

**Load-bearing for the AOSKit path.** AOSKit reads the device's
iCloud session from the Mac's keychain to generate the
`X-Apple-I-MD` / `X-Apple-I-MD-M` anisette headers. When the user
is signed out of iCloud on the Mac:

- `AOSKitAnisetteProvider::new()` succeeds (the framework still
  loads) but generates empty/invalid headers.
- Apple's GSA rejects the auth with SRP error `-22406`
  (`"Enter the correct password for this Apple Account."`) which
  reads as "wrong password" but actually means "invalid
  anisette."
- Because construction succeeded, our remote-anisette-v3 fallback
  never triggers.

**Symptom-to-root-cause cheat sheet:**

- "Wrong password" but icloud.com accepts the same password ->
  not signed into iCloud on the Mac -> System Settings -> Apple ID
  -> Sign In.
- All sign attempts fail with status `needs-login` after a known
  good GSA session was working last week -> session token
  rotated -> click the Apple ID row in Torch's menubar to
  re-login.

Both states are non-strike-bumping soft failures, so the IPA
auto-recovers as soon as the underlying issue is fixed.

## Free Apple ID limits we budget against

- **10 new App IDs per 7 days** -- team-level, enforced on
  `/addAppId.action`. Refreshing an existing bundle ID does NOT
  consume a slot; only new distinct bundle IDs do. Extensions
  count as separate App IDs.
- **3 apps per device** -- per-device install-time check by the
  device. Detailed in [install.md](install.md).
- **7-day provisioning profile** -- per-app expiry; refresh
  cycle compensates. See [refresh.md](refresh.md).
- **364-day developer certificate** -- team-level, detected in
  `refresh.refresh_cert_status`.

## Key files

- `src/torchapp/plumesign.py` -- subprocess wrapper
- `vendor/impactor-tvos.patch` -- source patch against Impactor v2.2.3
- `bin/plumesign` -- built binary tracked in git
- `src/torchapp/ui.py` `_apple_id_login_worker` -- menubar login flow
- `src/torchapp/ui_dialogs.py` -- email/password/2FA `rumps.Window` prompts
- `src/torchapp/keychain.py` -- Keychain wrapper for password persistence

## Rebuilding plumesign from source

Only needed if the binary disappears or we bump Impactor upstream:

```bash
brew install rust
git clone https://github.com/CLARATION/Impactor.git /tmp/Impactor
cd /tmp/Impactor && git checkout v2.2.3
git apply /Volumes/T7/Projects/ATVLoader/vendor/impactor-tvos.patch
cargo build --release -p plumesign
cp target/release/plumesign /Volumes/T7/Projects/ATVLoader/bin/plumesign
```

After rebuilding, run `python3 src/install.py --agent` to copy the
new binary into the bundle's `Contents/Resources/`.

## Related docs

- [refresh.md](refresh.md) -- how sign errors become soft/hard failure states
- [install.md](install.md) -- the install step the sign step feeds
- [config.md](config.md) -- where signed IPAs land on disk
- [ui.md](ui.md) -- the menubar login flow + status display
- CLAUDE.md discoveries 1-3 -- deep dives on each plumesign patch
