"""Encrypted API-key storage — AES-256-GCM authenticated encryption.

Machine-bound: PBKDF2-HMAC-SHA256(100k iters, salt from .salt file) over the
hostname, AES-256-GCM encrypted JSON. Backward-compatible with Fernet (AES-128)
— old keys.enc files are transparently migrated on first load.
"""
import base64
import json
import os
import socket

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import paths

_DEFAULT_SALT = b'simple-bot-trader-salt'

# Version marker: first byte of encrypted file
_VERSION_GCM = b'\x02'  # AES-256-GCM


def _machine_key():
    """Derive a 256-bit key from hostname + salt (machine-bound)."""
    salt = _DEFAULT_SALT
    if os.path.exists(paths.SALT_FILE):
        with open(paths.SALT_FILE, 'rb') as f:
            salt = f.read()
    else:
        try:
            os.makedirs(paths.CONFIG_DIR, exist_ok=True)
            with open(paths.SALT_FILE, 'wb') as f:
                f.write(salt)
            paths.secure_file(paths.SALT_FILE)
        except Exception:
            pass
    machine_id = socket.gethostname().encode()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000)
    return kdf.derive(machine_id)


def _fernet():
    """Legacy Fernet key (for backward compatibility with old keys.enc)."""
    return Fernet(base64.urlsafe_b64encode(_machine_key()))


def _aesgcm():
    """AES-256-GCM cipher using the same machine-bound key."""
    return AESGCM(_machine_key())


def load_keys():
    """Load and decrypt API keys. Transparently migrates Fernet → AES-256-GCM."""
    if not os.path.exists(paths.KEYS_FILE):
        return {'api_key_name': '', 'private_key_pem': ''}
    try:
        with open(paths.KEYS_FILE, 'rb') as f:
            raw = f.read()
        # Check version marker
        if raw[:1] == _VERSION_GCM:
            # AES-256-GCM: version(1) + nonce(12) + ciphertext
            nonce = raw[1:13]
            ciphertext = raw[13:]
            plaintext = _aesgcm().decrypt(nonce, ciphertext, None)
            data = json.loads(plaintext.decode())
        else:
            # Legacy Fernet (no version marker) — decrypt and re-encrypt
            data = json.loads(_fernet().decrypt(raw).decode())
            # Migrate to AES-256-GCM on next save
            _save_raw(data)
        return data
    except Exception:
        return {'api_key_name': '', 'private_key_pem': ''}


def _save_raw(data):
    """Encrypt and write AES-256-GCM directly (internal)."""
    plaintext = json.dumps(data).encode()
    nonce = os.urandom(12)  # 96-bit nonce for AES-GCM
    ciphertext = _aesgcm().encrypt(nonce, plaintext, None)
    # Version marker(1) + nonce(12) + ciphertext
    blob = _VERSION_GCM + nonce + ciphertext
    try:
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        with open(paths.KEYS_FILE, 'wb') as f:
            f.write(blob)
        paths.secure_file(paths.KEYS_FILE)
    except Exception:
        pass


def save_keys(api_key_name, private_key_pem, ccxt_api_key='', ccxt_secret='', ccxt_password=''):
    """Save API keys with AES-256-GCM authenticated encryption."""
    existing = load_keys()
    existing['api_key_name'] = api_key_name
    existing['private_key_pem'] = private_key_pem
    if ccxt_api_key:
        existing['ccxt_api_key'] = ccxt_api_key
    if ccxt_secret:
        existing['ccxt_secret'] = ccxt_secret
    if ccxt_password:
        existing['ccxt_password'] = ccxt_password
    _save_raw(existing)


def keys_exist():
    return os.path.exists(paths.KEYS_FILE)
