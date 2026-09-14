"""Ed25519 report signing — sign crash/error payloads with the same key pair
used for update signatures.

Shipped builds embed only the PUBLIC key (SIGNING_PUBLIC_KEY in auto_update.py);
the PRIVATE key lives on the dev machine at ~/.config/simple-bot-trader/
dev_signing_key.json and is used by tools/sign_release.py.

A signed report carries ``SBT-REPORT-SIGNATURE: <hex>`` in its body.  The
server (or tools/sign_release.py --verify-report) recomputes SHA256 of the
payload body and verifies the Ed25519 signature against the embedded public key.

Design decisions (user 2026-08-18):
  - Reuse the same Ed25519 key pair as auto_update (SIGNING_PUBLIC_KEY).
  - Signing happens in-process (no subprocess); the private key is read from
    the dev machine's key file (0600, never shipped).
  - Dev builds (SIGNING_PUBLIC_KEY empty) produce no signature — reports are
    still sent, just unsigned.
"""
import hashlib
import json
import os

_KEY_PATH = os.path.join(os.path.expanduser('~'), '.config',
                         'simple-bot-trader', 'dev_signing_key.json')


def _load_private_key():
    """Read the dev machine's Ed25519 private key hex (or None)."""
    try:
        if os.path.exists(_KEY_PATH):
            with open(_KEY_PATH) as f:
                return json.load(f).get('private_hex')
    except Exception:
        pass
    return None


def sign_report(payload_body):
    """Sign a report body string.  Returns hex signature or '' (dev build)."""
    pk_hex = _load_private_key()
    # Read from auto_update module attribute directly (module-level import
    # captures the initial empty string; we need the current value).
    try:
        from . import auto_update as _au
        _pk = _au.SIGNING_PUBLIC_KEY
    except Exception:
        _pk = ''
    if not pk_hex or not _pk:
        return ''
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey)
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(pk_hex))
        digest = hashlib.sha256(payload_body.encode('utf-8')).hexdigest()
        sig = priv.sign(digest.encode('ascii'))
        return sig.hex()
    except Exception:
        return ''


def verify_report(payload_body, signature_hex):
    """True when `signature_hex` is a valid Ed25519 signature of the
    SHA256(payload_body) by the embedded public key."""
    try:
        from . import auto_update as _au
        pk_hex = (_au.SIGNING_PUBLIC_KEY or '').strip()
    except Exception:
        pk_hex = ''
    if not pk_hex or not signature_hex:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pk_hex))
        digest = hashlib.sha256(payload_body.encode('utf-8')).hexdigest()
        pk.verify(bytes.fromhex(signature_hex.strip()), digest.encode('ascii'))
        return True
    except Exception:
        return False
