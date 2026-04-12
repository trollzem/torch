#!/usr/bin/env python3
"""
ATVLoader Signing Setup
Generates signing certificate and provisioning profile for sideloading.

Uses Apple's developer services through pymobiledevice3's tunnel to:
1. Get Apple TV's UDID
2. Guide user through certificate creation
3. Create/download provisioning profile
"""

import asyncio
import json
import os
import plistlib
import subprocess
import sys
import urllib.request
from pathlib import Path

APP_DIR = Path.home() / "atvloader"
SIGNING_DIR = APP_DIR / "signing"


def get_tunnel_info():
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:49151/", timeout=3)
        tunnels = json.loads(resp.read())
        for identifier, entries in tunnels.items():
            if entries:
                return entries[0]["tunnel-address"], entries[0]["tunnel-port"]
    except Exception:
        pass
    return None, None


def get_device_info():
    addr, port = get_tunnel_info()
    if not addr:
        print("ERROR: No tunnel found. Make sure pymobiledevice3 tunneld is running.")
        print("  Run: sudo pymobiledevice3 remote tunneld --wifi")
        return None

    result = subprocess.run(
        [sys.executable, "-m", "pymobiledevice3", "lockdown", "info",
         "--rsd", addr, str(port)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        return json.loads(result.stdout)
    print(f"ERROR: {result.stderr}")
    return None


def generate_self_signed_cert():
    """Generate a self-signed certificate for ad-hoc signing."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "ATVLoader Signing"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ATVLoader"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )

    SIGNING_DIR.mkdir(exist_ok=True)
    (SIGNING_DIR / "cert.pem").write_bytes(cert_pem)
    (SIGNING_DIR / "key.pem").write_bytes(key_pem)

    print(f"Certificate saved to {SIGNING_DIR / 'cert.pem'}")
    print(f"Private key saved to {SIGNING_DIR / 'key.pem'}")
    return cert_pem, key_pem


def create_wildcard_profile(udid):
    """Create a minimal provisioning profile for ad-hoc signing."""
    import datetime
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs7
    from cryptography import x509

    cert_pem = (SIGNING_DIR / "cert.pem").read_bytes()
    key_pem = (SIGNING_DIR / "key.pem").read_bytes()

    cert = x509.load_pem_x509_certificate(cert_pem)
    key = serialization.load_pem_private_key(key_pem, password=None)

    # Create the provisioning profile plist
    profile_plist = {
        "AppIDName": "ATVLoader Wildcard",
        "ApplicationIdentifierPrefix": ["ATVLOADER"],
        "CreationDate": datetime.datetime.now(datetime.UTC),
        "DeveloperCertificates": [cert.public_bytes(serialization.Encoding.DER)],
        "Entitlements": {
            "application-identifier": "ATVLOADER.*",
            "get-task-allow": True,
            "keychain-access-groups": ["ATVLOADER.*"],
        },
        "ExpirationDate": datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7),
        "Name": "ATVLoader Profile",
        "Platform": ["tvOS"],
        "ProvisionedDevices": [udid],
        "TeamIdentifier": ["ATVLOADER"],
        "TeamName": "ATVLoader",
        "TimeToLive": 7,
        "UUID": "ATVLOADER-PROFILE-UUID",
        "Version": 1,
    }

    payload = plistlib.dumps(profile_plist)

    # Sign the profile with PKCS#7 (CMS)
    signed = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(payload)
        .add_signer(cert, key, hashes.SHA256())
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
    )

    profile_path = SIGNING_DIR / "profile.mobileprovision"
    profile_path.write_bytes(signed)
    print(f"Provisioning profile saved to {profile_path}")
    return profile_path


def main():
    print("=" * 60)
    print("ATVLoader Signing Setup")
    print("=" * 60)
    print()

    # Step 1: Check tunnel
    print("[1/4] Checking Apple TV tunnel...")
    addr, port = get_tunnel_info()
    if addr:
        print(f"  Tunnel active: {addr}:{port}")
    else:
        print("  No tunnel found!")
        print("  Start the tunnel with: sudo pymobiledevice3 remote tunneld --wifi")
        sys.exit(1)

    # Step 2: Get device info
    print("[2/4] Getting Apple TV info...")
    info = get_device_info()
    if info:
        udid = info.get("UniqueDeviceID", "unknown")
        name = info.get("DeviceName", "unknown")
        print(f"  Device: {name}")
        print(f"  UDID: {udid}")
        print(f"  tvOS: {info.get('ProductVersion', 'unknown')}")
    else:
        print("  Could not get device info")
        sys.exit(1)

    # Step 3: Generate certificate
    print("[3/4] Generating signing certificate...")
    if (SIGNING_DIR / "cert.pem").exists() and (SIGNING_DIR / "key.pem").exists():
        print("  Certificate already exists. Overwrite? (y/N): ", end="", flush=True)
        if input().strip().lower() != "y":
            print("  Keeping existing certificate.")
        else:
            generate_self_signed_cert()
    else:
        generate_self_signed_cert()

    # Step 4: Create provisioning profile
    print("[4/4] Creating provisioning profile...")
    create_wildcard_profile(udid)

    print()
    print("=" * 60)
    print("Setup complete! You can now use ATVLoader to sign and install IPAs.")
    print()
    print("Files created:")
    for f in SIGNING_DIR.iterdir():
        print(f"  {f}")
    print()
    print("To test signing, run:")
    print(f"  zsign -k {SIGNING_DIR}/key.pem -c {SIGNING_DIR}/cert.pem \\")
    print(f"    -m {SIGNING_DIR}/profile.mobileprovision \\")
    print(f"    -o /tmp/test_signed.ipa ~/atvloader/ipas/YouTube.ipa")
    print()
    print("To install:")
    print(f"  sudo pymobiledevice3 apps install --rsd {addr} {port} /tmp/test_signed.ipa")


if __name__ == "__main__":
    main()
