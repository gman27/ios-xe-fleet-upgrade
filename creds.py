"""
Shared helpers for encrypt_creds.py and upgrade.py.

credentials.enc holds NET_USER/NET_PASS/NET_ENABLE encrypted with a key derived
(PBKDF2-SHA256) from a vault passphrase you type at runtime. The passphrase is
never written anywhere; without it credentials.enc is useless.
"""
import base64
import getpass
import json
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.enc")
KDF_ITERATIONS = 480_000


def derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=KDF_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def load_credentials() -> dict:
    """Prompts for the vault passphrase and returns {'NET_USER', 'NET_PASS', 'NET_ENABLE'}."""
    with open(CREDS_FILE, "rb") as f:
        salt_b64, token = f.read().split(b"\n", 1)
    salt = base64.urlsafe_b64decode(salt_b64)

    passphrase = getpass.getpass("Vault passphrase: ")
    key = derive_key(passphrase, salt)
    try:
        data = Fernet(key).decrypt(token)
    except InvalidToken:
        raise SystemExit("Wrong vault passphrase.")
    return json.loads(data)
