#!/usr/bin/env python3
"""
One-time (or whenever creds change) setup: encrypts your switch login username,
password, and enable secret into credentials.enc, protected by a vault
passphrase you choose. upgrade.py will prompt for that passphrase each run
instead of needing NET_USER/NET_PASS/NET_ENABLE exported in plaintext.

Run:
    ./venv/bin/python3 encrypt_creds.py
"""
import getpass
import json
import os
import sys

from cryptography.fernet import Fernet

import creds


def main():
    if os.path.exists(creds.CREDS_FILE):
        if input(f"{creds.CREDS_FILE} already exists, overwrite? [y/N] ").strip().lower() != "y":
            sys.exit("Aborted.")

    net_user = input("Switch login username: ").strip()
    net_pass = getpass.getpass("Switch login password: ")
    net_enable = getpass.getpass("Switch enable secret: ")

    passphrase = getpass.getpass("New vault passphrase (protects this file): ")
    confirm = getpass.getpass("Confirm vault passphrase: ")
    if passphrase != confirm:
        sys.exit("Passphrases did not match.")
    if not passphrase:
        sys.exit("Passphrase cannot be empty.")

    salt = os.urandom(16)
    key = creds.derive_key(passphrase, salt)
    token = Fernet(key).encrypt(json.dumps({
        "NET_USER": net_user,
        "NET_PASS": net_pass,
        "NET_ENABLE": net_enable,
    }).encode())

    import base64
    with open(creds.CREDS_FILE, "wb") as f:
        f.write(base64.urlsafe_b64encode(salt) + b"\n" + token)
    os.chmod(creds.CREDS_FILE, 0o600)
    print(f"Wrote encrypted credentials to {creds.CREDS_FILE} (mode 600).")
    print("Remember the vault passphrase — it is not stored anywhere and cannot be recovered.")


if __name__ == "__main__":
    main()
