#!/usr/bin/env bash
#
# Torch uninstaller.
#
# Usage (already have the repo):
#   cd ~/torch && ./uninstall.sh
#
# Usage (remote one-liner, if you don't have the repo cloned):
#   curl -fsSL https://raw.githubusercontent.com/trollzem/torch/main/uninstall.sh | bash
#
# What it does:
#   1. Stops the menubar app (if running)
#   2. Removes both launchd services (asks for admin password once,
#      via macOS's native dialog — we never see it)
#   3. Interactively offers to remove the user data dir
#      (~/Library/Application Support/Torch/) — your tracked IPAs,
#      signed outputs, config.json, logs
#   4. Interactively offers to remove the Apple ID entry from the
#      macOS Keychain (com.torch.appleid)
#   5. Leaves ~/.config/PlumeImpactor/ and ~/.pymobiledevice3/ alone
#      unless you pass --purge-all
#
# Flags:
#   --yes          Don't ask for confirmation on any step (for scripts)
#   --purge-all    Also remove plumesign session and pymobiledevice3
#                  pair records. Destructive — you will need to re-pair
#                  every device and re-login to your Apple ID after.
#   --help         Show this message
#
# Safe to re-run. Each step is a no-op if that component isn't installed.

set -euo pipefail

# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mXXX\033[0m %s\n' "$*" >&2; exit 1; }

YES=0
PURGE_ALL=0

for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=1 ;;
        --purge-all) PURGE_ALL=1 ;;
        --help|-h)
            head -32 "$0" | sed 's/^#//'
            exit 0
            ;;
        *)
            warn "unknown flag: $arg"
            ;;
    esac
done

confirm() {
    local prompt="$1"
    if [ "$YES" -eq 1 ]; then
        return 0
    fi
    read -r -p "$prompt [y/N] " reply < /dev/tty || reply=""
    [[ "$reply" =~ ^[Yy]$ ]]
}

# ---------------------------------------------------------------------------
#  Locate the repo (for `python3 src/uninstall.py`)
# ---------------------------------------------------------------------------

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]:-$0}" )" && pwd )" 2>/dev/null || SCRIPT_DIR=""
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/src/uninstall.py" ]; then
    REPO_DIR="$SCRIPT_DIR"
elif [ -d "$HOME/torch" ] && [ -f "$HOME/torch/src/uninstall.py" ]; then
    REPO_DIR="$HOME/torch"
else
    # Running from curl | bash without a local repo. Clone just enough
    # to run the Python uninstaller, or fall back to manual launchctl.
    REPO_DIR=""
fi

# ---------------------------------------------------------------------------
#  Step 1: Stop menubar app
# ---------------------------------------------------------------------------

log "Stopping Torch menubar (if running)"
pkill -9 -f "Python.*-m torchapp" 2>/dev/null || true

# ---------------------------------------------------------------------------
#  Step 2: Remove launchd services
# ---------------------------------------------------------------------------

log "Removing launchd services (will prompt for admin password once)"
if [ -n "$REPO_DIR" ]; then
    (cd "$REPO_DIR" && python3 src/uninstall.py) || \
        warn "python3 src/uninstall.py failed; falling back to launchctl"
fi

# Fallback / best-effort teardown via launchctl directly, even if the
# Python helper worked — this catches stale plist files that python3
# src/uninstall.py might miss (e.g. partial previous installs).
USER_AGENT_PLIST="$HOME/Library/LaunchAgents/com.torch.app.plist"
SYSTEM_DAEMON_PLIST="/Library/LaunchDaemons/com.torch.tunneld.plist"

if [ -f "$USER_AGENT_PLIST" ]; then
    launchctl bootout "gui/$(id -u)" "$USER_AGENT_PLIST" 2>/dev/null || true
    rm -f "$USER_AGENT_PLIST"
fi

if [ -f "$SYSTEM_DAEMON_PLIST" ]; then
    osascript -e "do shell script \"launchctl bootout system '$SYSTEM_DAEMON_PLIST' 2>/dev/null || true && rm -f '$SYSTEM_DAEMON_PLIST' && rm -f /var/log/torch-tunneld.out /var/log/torch-tunneld.err\" with administrator privileges" 2>/dev/null || \
        warn "could not remove system LaunchDaemon — you may need to delete $SYSTEM_DAEMON_PLIST manually"
fi

log "launchd services removed"

# ---------------------------------------------------------------------------
#  Step 3: User data dir (interactive)
# ---------------------------------------------------------------------------

APP_SUPPORT="$HOME/Library/Application Support/Torch"
if [ -d "$APP_SUPPORT" ]; then
    log "Found user data at: $APP_SUPPORT"
    log "  Contains: tracked IPAs, signed outputs, config.json, logs"
    if confirm "  Delete this directory?"; then
        rm -rf "$APP_SUPPORT"
        log "  Deleted"
    else
        log "  Left in place — delete manually with: rm -rf '$APP_SUPPORT'"
    fi
fi

# ---------------------------------------------------------------------------
#  Step 4: Keychain entry (interactive)
# ---------------------------------------------------------------------------

log "Checking macOS Keychain for Torch Apple ID entry"
if security find-generic-password -s "com.torch.appleid" >/dev/null 2>&1; then
    log "  Found Keychain entry for service 'com.torch.appleid'"
    if confirm "  Remove it?"; then
        security delete-generic-password -s "com.torch.appleid" >/dev/null 2>&1 || true
        log "  Removed"
    else
        log "  Left in place"
    fi
else
    log "  No Keychain entry to remove"
fi

# ---------------------------------------------------------------------------
#  Step 5: External state (plumesign session + pymobiledevice3 pair records)
# ---------------------------------------------------------------------------

if [ "$PURGE_ALL" -eq 1 ]; then
    log "Purging plumesign session (~/.config/PlumeImpactor)"
    rm -rf "$HOME/.config/PlumeImpactor"
    log "Purging pymobiledevice3 pair records (~/.pymobiledevice3)"
    rm -rf "$HOME/.pymobiledevice3"
    warn "You will need to re-login with plumesign and re-pair every device if you reinstall."
else
    log "Leaving plumesign session (~/.config/PlumeImpactor) and pair records (~/.pymobiledevice3) alone."
    log "Pass --purge-all if you want those removed too."
fi

# ---------------------------------------------------------------------------
#  Done
# ---------------------------------------------------------------------------

echo
log "Torch uninstalled."
if [ -n "$REPO_DIR" ]; then
    log "Repo still on disk at: $REPO_DIR"
    log "Delete it with: rm -rf '$REPO_DIR'"
fi
