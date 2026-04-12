#!/usr/bin/env python3
"""
ATVLoader - Apple TV Sideloader for macOS
Menubar app that signs and installs IPAs on Apple TV via pymobiledevice3.
"""

import asyncio
import json
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import rumps

# ── Configuration ────────────────────────────────────────────────────────────

APP_DIR = Path.home() / "atvloader"
IPA_DIR = APP_DIR / "ipas"
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "atvloader.log"
SIGNED_DIR = APP_DIR / "signed"
REFRESH_DAYS = 6  # re-sign before 7-day expiry

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("atvloader")


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {
        "apple_id": "",
        "apps": [],
        "last_refresh": None,
        "apple_tv_name": "Habibi TV",
    }


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, default=str))


# ── Apple TV Discovery & Install ─────────────────────────────────────────────


async def find_apple_tv(name="Habibi TV"):
    """Find Apple TV via RemotePairing mDNS."""
    from pymobiledevice3.bonjour import browse_remotepairing

    results = await browse_remotepairing()
    for r in results:
        if r.properties.get("name") == name:
            for addr in r.addresses:
                if ":" not in addr.full_ip or addr.full_ip.startswith("192."):
                    return addr.full_ip, r.port
    # Fallback: try any result
    if results:
        r = results[0]
        for addr in r.addresses:
            if addr.full_ip.startswith("192.") or addr.full_ip.startswith("fe80"):
                return addr.full_ip, r.port
    return None, None


async def get_tunnel_info():
    """Get tunnel info from local tunneld (if running)."""
    import urllib.request

    try:
        resp = urllib.request.urlopen("http://127.0.0.1:49151/", timeout=3)
        tunnels = json.loads(resp.read())
        for identifier, entries in tunnels.items():
            if entries:
                return entries[0]["tunnel-address"], entries[0]["tunnel-port"]
    except Exception:
        pass
    return None, None


async def ensure_paired(tv_name="Habibi TV"):
    """Ensure we have a RemotePairing record for the Apple TV."""
    from pymobiledevice3.pair_records import (
        get_remote_pairing_record_filename,
        create_pairing_records_cache_folder,
        iter_remote_paired_identifiers,
    )

    cache = create_pairing_records_cache_folder()
    paired_ids = list(iter_remote_paired_identifiers())
    if paired_ids:
        log.info(f"Already paired with {len(paired_ids)} device(s)")
        return True

    log.info("No pairing found. Need to pair with Apple TV.")
    return False


async def install_ipa(ipa_path, rsd_addr=None, rsd_port=None):
    """Install a signed IPA on the Apple TV via pymobiledevice3."""
    if not rsd_addr:
        rsd_addr, rsd_port = await get_tunnel_info()
    if not rsd_addr:
        raise RuntimeError("No tunnel available. Start pymobiledevice3 tunneld first.")

    log.info(f"Installing {ipa_path} via tunnel {rsd_addr}:{rsd_port}")
    result = subprocess.run(
        [
            sys.executable, "-m", "pymobiledevice3",
            "apps", "install",
            "--rsd", rsd_addr, str(rsd_port),
            str(ipa_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Install failed: {result.stderr}")
    log.info(f"Installed {ipa_path.name} successfully")
    return True


# ── IPA Signing ──────────────────────────────────────────────────────────────


async def get_device_udid():
    """Get Apple TV UDID via tunnel."""
    rsd_addr, rsd_port = await get_tunnel_info()
    if not rsd_addr:
        return None
    result = subprocess.run(
        [
            sys.executable, "-m", "pymobiledevice3",
            "lockdown", "info", "--rsd", rsd_addr, str(rsd_port),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0:
        info = json.loads(result.stdout)
        return info.get("UniqueDeviceID")
    return None


def sign_ipa(ipa_path, cert_path, key_path, prov_path, output_path, bundle_id=None):
    """Sign an IPA using zsign."""
    cmd = [
        "zsign",
        "-k", str(key_path),
        "-c", str(cert_path),
        "-m", str(prov_path),
        "-o", str(output_path),
        "-z", "5",
    ]
    if bundle_id:
        cmd.extend(["-b", bundle_id])
    cmd.append(str(ipa_path))

    log.info(f"Signing {ipa_path.name}...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"zsign failed: {result.stderr}")
    log.info(f"Signed {ipa_path.name} -> {output_path.name}")
    return output_path


# ── Apple ID Developer Cert Management ───────────────────────────────────────

# This uses Apple's AuthKit/Grand Slam protocol to obtain a free dev cert,
# similar to what AltStore does. For the MVP, we'll use Xcode's built-in
# free signing or guide the user to export a cert.


def check_signing_materials():
    """Check if we have cert + key + provisioning profile."""
    cert = APP_DIR / "signing" / "cert.pem"
    key = APP_DIR / "signing" / "key.pem"
    prov = APP_DIR / "signing" / "profile.mobileprovision"
    return cert.exists() and key.exists() and prov.exists()


def get_signing_paths():
    signing_dir = APP_DIR / "signing"
    return (
        signing_dir / "cert.pem",
        signing_dir / "key.pem",
        signing_dir / "profile.mobileprovision",
    )


# ── Refresh Logic ────────────────────────────────────────────────────────────


async def refresh_all_apps():
    """Sign and install all tracked IPAs."""
    if not check_signing_materials():
        log.error("Missing signing materials. Run setup first.")
        return False, "Missing signing materials"

    cert, key, prov = get_signing_paths()
    rsd_addr, rsd_port = await get_tunnel_info()
    if not rsd_addr:
        log.error("No tunnel available")
        return False, "No tunnel to Apple TV"

    SIGNED_DIR.mkdir(exist_ok=True)
    results = []
    for ipa_file in IPA_DIR.glob("*.ipa"):
        try:
            signed = SIGNED_DIR / f"signed_{ipa_file.name}"
            sign_ipa(ipa_file, cert, key, prov, signed)
            await install_ipa(signed, rsd_addr, rsd_port)
            results.append((ipa_file.name, True, None))
        except Exception as e:
            log.error(f"Failed {ipa_file.name}: {e}")
            results.append((ipa_file.name, False, str(e)))

    cfg = load_config()
    cfg["last_refresh"] = datetime.now().isoformat()
    save_config(cfg)

    failures = [r for r in results if not r[1]]
    if failures:
        return False, f"{len(failures)}/{len(results)} failed"
    return True, f"{len(results)} apps refreshed"


# ── Menubar App ──────────────────────────────────────────────────────────────


class ATVLoaderApp(rumps.App):
    def __init__(self):
        super().__init__(
            "ATVLoader",
            icon=None,
            title="📺",
            quit_button=None,
        )
        self.cfg = load_config()
        self._build_menu()
        self._start_auto_refresh_check()

    def _build_menu(self):
        self.menu.clear()

        # Status
        status = self._get_status_text()
        self.menu.add(rumps.MenuItem(status, callback=None))
        self.menu.add(rumps.separator)

        # Apps
        apps_menu = rumps.MenuItem("Apps")
        for ipa in sorted(IPA_DIR.glob("*.ipa")):
            apps_menu.add(rumps.MenuItem(f"  {ipa.stem}", callback=None))
        if not list(IPA_DIR.glob("*.ipa")):
            apps_menu.add(rumps.MenuItem("  No IPAs found", callback=None))
        self.menu.add(apps_menu)
        self.menu.add(rumps.separator)

        # Actions
        self.menu.add(rumps.MenuItem("Refresh Now", callback=self.on_refresh))
        self.menu.add(rumps.MenuItem("Check Apple TV", callback=self.on_check_tv))
        self.menu.add(rumps.MenuItem("Setup Signing...", callback=self.on_setup_signing))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Open IPAs Folder", callback=self.on_open_folder))
        self.menu.add(rumps.MenuItem("View Log", callback=self.on_view_log))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Quit", callback=rumps.quit_application))

    def _get_status_text(self):
        last = self.cfg.get("last_refresh")
        if not last:
            return "Never refreshed"
        try:
            dt = datetime.fromisoformat(last)
            days = (datetime.now() - dt).days
            if days >= REFRESH_DAYS:
                return f"⚠️ Stale ({days}d ago)"
            return f"✅ Refreshed {days}d ago"
        except Exception:
            return "Unknown status"

    def _start_auto_refresh_check(self):
        """Check every hour if we need to auto-refresh."""
        def checker():
            while True:
                time.sleep(3600)  # check every hour
                try:
                    last = self.cfg.get("last_refresh")
                    if last:
                        dt = datetime.fromisoformat(last)
                        if (datetime.now() - dt).days >= REFRESH_DAYS:
                            log.info("Auto-refresh triggered")
                            self._do_refresh_async()
                except Exception as e:
                    log.error(f"Auto-refresh check error: {e}")

        t = threading.Thread(target=checker, daemon=True)
        t.start()

    def _do_refresh_async(self):
        def run():
            try:
                self.title = "📺⏳"
                loop = asyncio.new_event_loop()
                ok, msg = loop.run_until_complete(refresh_all_apps())
                loop.close()
                if ok:
                    self.title = "📺✅"
                    rumps.notification("ATVLoader", "Refresh Complete", msg)
                else:
                    self.title = "📺❌"
                    rumps.notification("ATVLoader", "Refresh Failed", msg)
                self.cfg = load_config()
                self._build_menu()
            except Exception as e:
                self.title = "📺❌"
                log.error(f"Refresh error: {e}")
                rumps.notification("ATVLoader", "Error", str(e))

        threading.Thread(target=run, daemon=True).start()

    @rumps.clicked("Refresh Now")
    def on_refresh(self, _):
        if not check_signing_materials():
            rumps.notification(
                "ATVLoader",
                "Setup Required",
                "Run 'Setup Signing...' first to configure your Apple ID certificate.",
            )
            return
        self._do_refresh_async()

    @rumps.clicked("Check Apple TV")
    def on_check_tv(self, _):
        def run():
            loop = asyncio.new_event_loop()
            try:
                addr, port = loop.run_until_complete(get_tunnel_info())
                if addr:
                    rumps.notification(
                        "ATVLoader",
                        "Apple TV Found",
                        f"Tunnel active at {addr}:{port}",
                    )
                else:
                    # Try direct discovery
                    ip, p = loop.run_until_complete(find_apple_tv())
                    if ip:
                        rumps.notification(
                            "ATVLoader",
                            "Apple TV Found (no tunnel)",
                            f"Habibi TV at {ip}:{p}\nStart tunneld to connect.",
                        )
                    else:
                        rumps.notification(
                            "ATVLoader", "Not Found", "Apple TV not found on network."
                        )
            except Exception as e:
                rumps.notification("ATVLoader", "Error", str(e))
            finally:
                loop.close()

        threading.Thread(target=run, daemon=True).start()

    @rumps.clicked("Setup Signing...")
    def on_setup_signing(self, _):
        signing_dir = APP_DIR / "signing"
        signing_dir.mkdir(exist_ok=True)

        msg = (
            "To sign IPAs, you need:\n\n"
            "1. cert.pem - Your Apple Developer certificate\n"
            "2. key.pem - The private key for the cert\n"
            "3. profile.mobileprovision - Provisioning profile\n\n"
            f"Place these files in:\n{signing_dir}\n\n"
            "To generate these with your Apple ID, run:\n"
            f"python3 {APP_DIR}/setup_signing.py"
        )
        rumps.alert("Signing Setup", msg)
        subprocess.run(["open", str(signing_dir)])

    @rumps.clicked("Open IPAs Folder")
    def on_open_folder(self, _):
        subprocess.run(["open", str(IPA_DIR)])

    @rumps.clicked("View Log")
    def on_view_log(self, _):
        subprocess.run(["open", "-a", "Console", str(LOG_FILE)])


def main():
    APP_DIR.mkdir(exist_ok=True)
    IPA_DIR.mkdir(exist_ok=True)
    SIGNED_DIR.mkdir(exist_ok=True)
    (APP_DIR / "signing").mkdir(exist_ok=True)

    log.info("ATVLoader starting")
    app = ATVLoaderApp()
    app.run()


if __name__ == "__main__":
    main()
