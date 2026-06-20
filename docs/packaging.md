# Packaging

> **Maintainer note:** update this doc when (a) `setup.py` options
> change (especially `packages`, `excludes`, `resources`), (b) the
> py2app version pinned in requirements changes, (c) the
> `__main__.py` bootstrap (especially the `PYTHONHOME` scrub or
> the `sys.path` self-bootstrap) changes, or (d) the bundle ID /
> `LSUIElement` config changes.

## What this is

Torch ships as a py2app-built `.app` bundle at
`/Applications/Torch.app`. Bundling is load-bearing -- without
it the process has no CFBundleIdentifier of its own and macOS
attributes notifications, LaunchServices lookups, Keychain ACL
prompts, etc. to "Python" (the hosting interpreter). Inside the
bundle the process's `NSBundle.mainBundle().bundleIdentifier()`
returns `com.torch.app`, so the user sees "Torch."

Build config lives in `setup.py`. Build orchestration lives in
`src/install.py`.

## Build modes

```bash
# Full build (~30-60s, ~36 MB output) -- production install
python3 setup.py py2app

# Alias build (~3s) -- dev iteration
python3 setup.py py2app -A
```

### Full mode

Copies everything into `dist/Torch.app/`: Python.framework,
torchapp/, every dependency from `packages=[...]`, plumesign,
the rendered SF Symbol PNGs, etc. Self-contained -- the bundle
runs even if the source tree is gone. `src/install.py` copies
this to `/Applications/Torch.app` and ad-hoc codesigns it.

### Alias mode

Symlinks `Contents/Resources/` back to the source tree.
Iteration is instant: edit `src/torchapp/*.py`, then
`launchctl kickstart -k gui/$(id -u)/com.torch.app`. The bundle
re-imports the changed code on next launch.

Dev-only -- the alias bundle can't be moved to `/Applications`
because the symlinks break. `src/install.py --agent --alias`
wipes any existing `/Applications/Torch.app` and bootstraps the
LaunchAgent against `dist/Torch.app`.

## setup.py: four load-bearing options

```python
APP = ["src/torchapp/__main__.py"]
OPTIONS = {
    "argv_emulation": False,             # (1)
    "packages": [                        # (2)
        "torchapp", "rumps", "keyring", "pexpect",
    ],
    "excludes": [                        # (3)
        "pymobiledevice3", "cryptography", "construct",
        "fastapi", "qh3", "hyperframe", "opack", "pyimg4",
        "nest_asyncio", "starlette",
    ],
    "resources": ["bin/plumesign"],      # (4)
    "plist": {
        "CFBundleIdentifier": "com.torch.app",
        "CFBundleName": "Torch",
        "LSUIElement": True,             # menubar app, no Dock icon
        # ...
    },
}
```

### (1) `argv_emulation=False`

True would inject a Carbon event-loop wrapper that silently
fights `LSUIElement` and brings the Dock icon back. We don't
need argv emulation -- Torch never receives drag-and-drop events
at the process level.

### (2) `packages` (not `includes`)

`packages` copies the full package tree including
`importlib.metadata` entry points. `includes` would break
`keyring` at runtime because its macOS backend is discovered via
entry points; without the metadata, `keyring.set_password`
raises `NoKeyringError`.

`torchapp`, `rumps`, `keyring`, `pexpect` are the four packages
the menubar app imports. PyObjC frameworks (Cocoa, AppKit,
Foundation) get auto-included by py2app's hooks.

### (3) `excludes`

`pymobiledevice3` brings ~10 transitive dependencies that we
don't need bundled because **we shell out to the
`pymobiledevice3` CLI via subprocess; we never import the
library**. Without aggressive exclusion the bundle balloons from
~36 MB to 250+ MB.

The same applies to the DVT pre-kill calls (we shell out, not
import) and to `ideviceinstaller` (separate binary, not Python).

### (4) `resources=["bin/plumesign"]`

py2app flattens this to `Contents/Resources/plumesign` (not
`Contents/Resources/bin/plumesign`). `paths._resolve_plumesign_binary()`
probes both locations plus the repo's `bin/` fallback so dev
mode works without rebuilding. See [config.md](config.md) for
the resolver.

## __main__.py: three non-obvious bootstraps

Living in `src/torchapp/__main__.py`. Each fixes a py2app gotcha
we hit during the original packaging spike.

### 1. Absolute imports (not relative)

```python
from torchapp import paths
# NOT: from . import paths
```

py2app's `__boot__.py` does
`exec(compile(source, script, "exec"), globals(), globals())`
with no `__package__` set. A relative import (`from . import
paths`) raises `ImportError: attempted relative import with no
known parent package`. Absolute imports work in both modes.

### 2. sys.path self-bootstrap

```python
import sys, pathlib
src_dir = pathlib.Path(__file__).resolve().parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))
```

In alias mode, py2app doesn't symlink packages into
`Contents/Resources/lib`, and we don't `pip install -e .` into
system Python. So bare `from torchapp import paths` raises
`ModuleNotFoundError`. The self-bootstrap inserts `<repo>/src/`
into sys.path so the absolute import works.

Harmless in full-build mode: the computed src directory doesn't
exist inside the bundle, and the insert is a no-op relative to
py2app's own sys.path configuration.

### 3. PYTHONHOME / PYTHONPATH scrub

```python
for var in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE"):
    os.environ.pop(var, None)
```

py2app's stub launcher sets `PYTHONHOME` and `PYTHONPATH` at
process start so the bundle's embedded Python finds its
framework. CPython reads them only during `Py_Initialize`, but
they stay in the UNIX env block -- so any subprocess we spawn
inherits them.

When the child is another Python interpreter (e.g.
`/opt/homebrew/bin/pymobiledevice3`, whose shebang runs
Homebrew `python3.14`), the child's interpreter starts up with
`PYTHONHOME=<Torch.app path>`, computes `sys.path` from that,
and can't find `pymobiledevice3` (which lives in Homebrew's
site-packages, not the bundle's). Symptom:
`ModuleNotFoundError: No module named 'pymobiledevice3'` on
every lockdown-info subprocess.

The scrub deletes these vars immediately after logging setup.
Deleting them mid-run is safe (Python only reads them at
startup); every subprocess from that point on sees a clean env
and respects its own shebang's Python paths. This is a single-
point fix -- don't try to scrub per-subprocess-call-site.

## Bundle structure (full build)

```
Torch.app/
  Contents/
    Info.plist                     # CFBundleIdentifier=com.torch.app, LSUIElement=True
    MacOS/
      Torch                        # py2app stub launcher (the entry point)
    Resources/
      __boot__.py                  # py2app bootstrap (sets PYTHONHOME)
      __error__.py                 # py2app error display
      icon.icns
      plumesign                    # flattened from setup.py resources
      lib/
        python3.14/
          torchapp/                # our package
          rumps/
          keyring/
          pexpect/
          PyObjC bindings/
      Python.framework/            # embedded Python interpreter
```

The LaunchAgent plist points at `Contents/MacOS/Torch`, which
is the py2app stub. It in turn execs the embedded Python which
runs `Resources/__boot__.py` which exec's
`torchapp/__main__.py` which runs `ui.TorchApp().run()`.

## Bundle ID and dock-icon hiding

```python
"CFBundleIdentifier": "com.torch.app",
"LSUIElement": True,
```

- `CFBundleIdentifier` is what NSBundle reports. Notifications
  attribute to this identifier; LaunchServices uses it for
  default-app preferences; the Keychain ACL prompt names it.
  We register `com.torch.app` to keep all of these labeled
  "Torch."
- `LSUIElement: True` is the "menubar accessory" flag. Suppresses
  the Dock icon, doesn't accept normal keyboard focus, doesn't
  appear in Cmd-Tab. Without it Torch would behave like a
  normal app with a Dock entry and a hidden window.

## install.py orchestration

```bash
python3 src/install.py [--agent|--daemon|--alias|--skip-build]
```

Full install flow (no flags):

1. Set up logging.
2. Kill any orphaned tunneld / Torch.app processes
   (`_kill_stray_processes`) so the launchd bootstrap doesn't
   race against manually-started copies.
3. If `--agent` (or no flag): build the bundle.
   - Full mode: `python3 setup.py py2app`, then copy `dist/Torch.app`
     to `/Applications/Torch.app`, then ad-hoc codesign (because
     copying invalidates the original signature on macOS 14+).
   - Alias mode: `python3 setup.py py2app -A`, then wipe
     `/Applications/Torch.app` so the LaunchAgent falls back to
     `dist/Torch.app`.
4. If `--daemon` (or no flag): osascript-prompt for admin
   password and bootstrap both LaunchDaemons (tunneld +
   keepalive).
5. If `--agent` (or no flag): bootstrap the LaunchAgent
   pointing at the just-built bundle.
6. Print log paths for the user to check.

## Ad-hoc codesigning

```python
subprocess.run(["codesign", "--force", "--deep", "--sign", "-", APPLICATIONS_APP])
```

The bundle is ad-hoc signed at the destination (after copy to
`/Applications/`) because copying invalidates the original
signature on Apple Silicon macOS 14+. Without re-signing, the
bundle won't launch (Gatekeeper rejects).

We sign with `-` (ad-hoc, no developer certificate). This is
fine for personal use and works around the need for a paid
Apple Developer ID. If you want a notarized build you'd:

- Sign with a real Developer ID Application certificate
- Run `xcrun notarytool` against the signed bundle
- Staple the notarization with `xcrun stapler`

None of that is needed for Torch's current personal-use scope.

## Key files

- `setup.py` -- py2app options
- `src/torchapp/__main__.py` -- bundle entry point with the
  three load-bearing bootstraps
- `src/install.py` -- build + install orchestration
- `src/torchapp/paths.py` `_resolve_plumesign_binary` -- finds
  the flattened plumesign in either bundle or dev mode

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Notifications attribute to "Python" | Torch running as `python3 -m torchapp`, not the bundle | install the bundle via `python3 src/install.py --agent` |
| `ModuleNotFoundError: pymobiledevice3` on first subprocess call | PYTHONHOME scrub didn't fire | check `__main__.py` ordering |
| Dock icon appears alongside the menubar icon | `LSUIElement` overridden somehow, OR `argv_emulation=True` | check Info.plist + setup.py |
| `NoKeyringError` on first password save | `keyring` brought in via `includes` instead of `packages` | check setup.py |
| `keychain` ACL prompt names "Python" not "Torch" | bundle ID not being read | verify `CFBundleIdentifier="com.torch.app"` and the bundle is actually running |
| Bundle won't launch ("damaged or incomplete") | copy to `/Applications/` invalidated signature, ad-hoc resign skipped | check `_install_torch_app_to_applications` codesign call ran |

## Related docs

- [architecture.md](architecture.md) -- where the bundle fits in the bigger picture
- [launchd.md](launchd.md) -- the LaunchAgent that runs the bundle
- [signing.md](signing.md) -- the plumesign binary bundled as a resource
- [config.md](config.md) -- the resolver that finds it at runtime
- [ui.md](ui.md) -- what runs inside the bundle
