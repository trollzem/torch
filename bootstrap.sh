#!/usr/bin/env bash
#
# Torch bootstrap — one-command install for a fresh macOS machine.
#
# Usage (new Mac, never seen this repo):
#   curl -fsSL https://raw.githubusercontent.com/trollzem/torch/main/bootstrap.sh | bash
#
# Usage (already have the repo cloned somewhere):
#   cd /path/to/torch && ./bootstrap.sh
#
# What this does:
#   1. Installs Homebrew if missing.
#   2. Installs python@3.14 via brew.
#   3. Clones Torch to ~/torch (or uses an existing checkout).
#   4. Installs Python dependencies (rumps, keyring, pexpect, pyobjc,
#      pymobiledevice3, py2app).
#   5. Verifies the bundled patched plumesign binary is present (or rebuilds from source).
#   6. Builds Torch.app via py2app and copies it to /Applications/Torch.app.
#      Bundling is what gives the process a proper CFBundleIdentifier so
#      notifications attribute to "Torch" instead of "Python".
#   7. Installs the launchd services (LaunchDaemon for tunneld, LaunchAgent for menubar).
#      The LaunchAgent runs /Applications/Torch.app/Contents/MacOS/Torch directly.
#      This is the single sudo moment — macOS's native admin dialog will ask once
#      for the LaunchDaemon (tunneld); the LaunchAgent and bundle install don't need it.
#   8. Tells you how to log in and pair your first device from the menubar.
#
# The script is idempotent — running it again on an existing install does a
# `git pull` and re-bootstraps everything, skipping steps that are already done.
#
# After this completes:
#   - 🔥 flame icon appears in your menubar
#   - tunneld runs at boot as a root LaunchDaemon
#   - menubar auto-starts at login
#   - drop IPAs into ~/Library/Application Support/Torch/ipas/ (or click
#     "Add IPA..." in the menubar) and they refresh automatically every 6 days

set -euo pipefail

# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------

REPO_URL="${TORCH_REPO:-https://github.com/trollzem/torch.git}"
TARGET_DIR="${TORCH_DIR:-$HOME/torch}"

# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mXXX\033[0m %s\n' "$*" >&2; exit 1; }

has_cmd() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
#  Step 1: Figure out where to put the repo
# ---------------------------------------------------------------------------

# If this script is being run from inside an existing checkout (the user cd'd
# into the repo and ran ./bootstrap.sh), use that instead of cloning again.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]:-$0}" )" && pwd )" 2>/dev/null || SCRIPT_DIR=""
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/bootstrap.sh" ] && [ -d "$SCRIPT_DIR/src/torchapp" ]; then
    TARGET_DIR="$SCRIPT_DIR"
    log "Using existing checkout at $TARGET_DIR"
fi

# ---------------------------------------------------------------------------
#  Step 2: Homebrew (on a vanilla Mac, this is the first install)
# ---------------------------------------------------------------------------

if ! has_cmd brew; then
    log "Installing Homebrew (will prompt for your password)"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Homebrew's installer puts brew on PATH by writing to ~/.zprofile but
    # that only applies to new shells. Eval its shellenv for this session.
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
else
    log "Homebrew already installed"
fi

# ---------------------------------------------------------------------------
#  Step 3: System dependencies
# ---------------------------------------------------------------------------

log "Ensuring python@3.14 is installed"
brew list python@3.14 >/dev/null 2>&1 || brew install python@3.14

# We bundle the prebuilt plumesign binary in the repo, so rust is only
# needed if someone wants to rebuild from vendor/impactor-tvos.patch. We
# don't install it proactively — the build step below handles the missing
# binary case by calling out clearly.

# ---------------------------------------------------------------------------
#  Step 4: Clone / update the repo
# ---------------------------------------------------------------------------

if [ ! -d "$TARGET_DIR" ]; then
    log "Cloning Torch to $TARGET_DIR"
    git clone "$REPO_URL" "$TARGET_DIR"
elif [ -d "$TARGET_DIR/.git" ]; then
    log "Updating existing checkout"
    (cd "$TARGET_DIR" && git pull --ff-only 2>/dev/null) || \
        warn "git pull failed — continuing with existing state"
fi

cd "$TARGET_DIR"

# ---------------------------------------------------------------------------
#  Step 5: Python dependencies
# ---------------------------------------------------------------------------

# Create a dedicated virtualenv inside the repo rather than installing
# into Homebrew-managed system site-packages. Two reasons:
#   1. Homebrew-installed pip has no RECORD file, so pip cannot
#      uninstall-to-upgrade itself when building packages in isolated
#      build environments. On Homebrew python@3.14 shipping pip 26+,
#      even `pip install -r requirements.txt` silently triggers this
#      and fails with "Cannot uninstall pip X.Y".
#   2. Isolating Torch's dependencies from system Python means we
#      never collide with anything else you've installed and we can
#      pin versions without polluting the rest of your machine.
VENV_DIR="$TARGET_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"

if [ ! -x "$VENV_PYTHON" ]; then
    log "Creating Python virtualenv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

log "Installing Python packages into the virtualenv"
"$VENV_PIP" install --quiet --upgrade pip setuptools wheel
"$VENV_PIP" install --quiet -r requirements.txt
"$VENV_PIP" install --quiet pymobiledevice3

# Sanity check the imports via the venv's Python — a broken install
# would break everything downstream in confusing ways.
"$VENV_PYTHON" - <<'PY' || die "Python dependency import failed. Check the error above."
import rumps, keyring, pexpect, pymobiledevice3
from PyObjCTools import AppHelper
print(f"  rumps {rumps.__version__}")
print(f"  pymobiledevice3 ok")
PY

# ---------------------------------------------------------------------------
#  Step 6: Verify patched plumesign binary
# ---------------------------------------------------------------------------

if [ ! -x "bin/plumesign" ]; then
    warn "bin/plumesign is missing or not executable."
    warn "This happens if you cloned with git-lfs but didn't pull LFS objects,"
    warn "or if you're building from a source export."
    warn ""
    warn "To rebuild from source, run these four commands:"
    warn "  brew install rust"
    warn "  git clone https://github.com/CLARATION/Impactor.git /tmp/Impactor"
    warn "  (cd /tmp/Impactor && git checkout v2.2.3 && git apply $TARGET_DIR/vendor/impactor-tvos.patch && cargo build --release -p plumesign)"
    warn "  cp /tmp/Impactor/target/release/plumesign $TARGET_DIR/bin/plumesign"
    die "Cannot continue without bin/plumesign"
fi

log "Patched plumesign binary is present"

# Pre-create ~/.pymobiledevice3 with user ownership BEFORE the launchd
# step runs. The tunneld LaunchDaemon starts as root with HOME set to
# the user's home, and on first start pymobiledevice3's pair_records
# module does `Path.home() / ".pymobiledevice3"` + mkdir(exist_ok=True),
# which creates the directory owned by root. After that the user's
# own pair command can't write to it and fails with EACCES. Creating
# it first under user ownership means root just uses the existing
# directory.
mkdir -p "$HOME/.pymobiledevice3"

# Note: Apple ID login no longer happens in this script. On a fresh
# install the menubar app starts in a "not logged in" state; the user
# clicks the 🔥 flame icon, clicks the "Apple ID: not logged in" menu
# header, and enters their credentials in a native dialog. The 2FA code
# flow runs through another native dialog. All of this is done from
# the menubar so there's nothing left that needs a terminal after this
# script exits.

# ---------------------------------------------------------------------------
#  Step 6: Install launchd services
# ---------------------------------------------------------------------------

log "Installing launchd services (tunneld LaunchDaemon + menubar LaunchAgent)"
log "macOS will prompt you for your admin password in a native dialog — this"
log "is the only admin-password moment. After this, nothing else needs sudo."
echo
# Use the venv's Python so that launchd.py resolves sys.executable to
# the venv interpreter and bakes that path into the launchd plists
# (instead of pointing them at system Python, which would then crash
# at import time with ModuleNotFoundError: rumps).
"$VENV_PYTHON" src/install.py

# ---------------------------------------------------------------------------
#  Step 7: Next-step guidance
# ---------------------------------------------------------------------------

pair_count=0
if [ -d "$HOME/.pymobiledevice3" ]; then
    pair_count=$(find "$HOME/.pymobiledevice3" -name 'remote_*.plist' 2>/dev/null | wc -l | tr -d ' ')
fi

logged_in="no"
if [ -f "$HOME/.config/PlumeImpactor/accounts.json" ]; then
    logged_in="yes"
fi

echo
log "Done. Look for the 🔥 flame icon in your menubar."
echo

if [ "$logged_in" = "no" ]; then
    cat <<'EOF'
First: log in to your Apple ID.

  1. Click the 🔥 flame menubar icon.
  2. Click the "⚠️ Apple ID: not logged in" row at the top of the menu.
  3. Enter your Apple ID email, password, and the 6-digit 2FA code that
     Apple sends to your trusted device. All three prompts are native
     macOS dialogs — nothing else needs a terminal after this.

EOF
fi

if [ "$pair_count" -eq 0 ]; then
    cat <<'EOF'
No devices are paired yet. To add your first one:

  Apple TV:
    1. On the Apple TV: Settings → General → Remotes and Devices →
       Remote App and Devices. Leave that screen open.
    2. Click the 🔥 flame menubar icon → Devices → Add Apple TV
       (pair via Terminal). Follow the 6-digit PIN prompt.

  iPhone / iPad:
    1. Plug the device in via USB cable. Tap "Trust This Computer"
       when it appears on the device screen.
    2. Click the 🔥 flame menubar icon → Devices → Detect iPhone/iPad
       (via USB trust). Accept the "Add this device?" dialog.

Then drop IPAs into ~/Library/Application Support/Torch/ipas/
(or click the 🔥 flame → Apps → Add IPA...) and they'll auto-refresh every 6 days.
EOF
else
    log "$pair_count device(s) already paired — nothing else to do."
fi
