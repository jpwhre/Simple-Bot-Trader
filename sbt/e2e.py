"""E2E encryption for bug reports sent to GitHub.

RSA-4096 + AES-256-GCM hybrid encryption:
  - Report body encrypted with a random AES-256-GCM key (per report)
  - AES key encrypted with RSA-4096 public key (OAEP-SHA256)
  - GitHub receives ciphertext only — cannot read report content
  - Only the dev's private key can decrypt

The public key is embedded in the app at ship time.  The private key
never leaves the dev machine.
"""
import base64
import json
import os

from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---- ship-time constant: RSA-4096 PUBLIC KEY (PEM) ------------------------
# Generated 2026-08-24.  Private key stays on dev machine ONLY.  The PUBLIC key
# is embedded below so shipped bundles are self-contained (no path dependency).
_E2E_PUBLIC_KEY_PEM_TEXT = """
    -----BEGIN PUBLIC KEY-----
    MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAn1Clx7BdUf8K37m1xVDl
    DxJ2fON0AqeegzFdQPovYfioXT/BALo7k/Y9IfUV/zzBtic4+njWdEC/wq8U28+8
    HrVT8o0r83ZY79+0orJwTQXKAvvRYCVw1FF1BqIfKKTpgrO6cTz6L4HFlPDLf7x0
    bDqSJSnK518vPyQbuO+G+/eDlb6Gla1w9Xo0R1WXFlbieFNjsENt0WdGJM34QNDQ
    N6fD4O8HbcuKA2jEq7XhHgzKzsJwhZ4Btjt0fZ2o2h9ZEaNKpCj3/gvJvlZbaWaj
    8g9HyFrksoAiOJJO9OCJPrLkaymIfy3XLQjp9IgqZAW+aDJ1AyujcoBsVNnVoVU2
    2jZfyEI7i3/i3dXeX4ImFHEG0KMZCrEsueJkpKFlprYtBemzid5/fO+tLzOH67uq
    gMRUgCY/KkbXxnJmmI5q5XGTAntdzvdUlAP96LBlkueliqnQeEXmmgxyPUjK2QYA
    ly3xSbm+qrunp7oq/RT05IaMmn6JH6sLwvPjVX7Ptb7Yyz5dRJEg4n4Oxaye/Ybw
    CC6PXx6fXwCgeLNO6bt7evDgDhX0hjG4LqnlFxVhZw8wOfZmWSPa0vGAbvi8PgVB
    5i1zI6OhZBB1iXrx8tff/f439daP2WhbxWowKVpfbFBPrOlbYUlHrynpbp5n0WZ9
    E49RZ3oRLHOacBKepKbNwskCAwEAAQ==
    -----END PUBLIC KEY-----
"""

_E2E_PUBLIC_KEY_PEM = None  # loaded lazily (embedded text -> object)

_E2E_PUBLIC_KEY_PATHS = [
    os.path.join(os.path.dirname(__file__), '..', 'tools', 'report_public_key.pem'),
    os.path.expanduser('~/.config/simple-bot-trader/report_public_key.pem'),
]


def _load_public_key():
    """Load the RSA-4096 public key for encryption (embedded text, then paths)."""
    global _E2E_PUBLIC_KEY_PEM
    if _E2E_PUBLIC_KEY_PEM is not None:
        return _E2E_PUBLIC_KEY_PEM
    try:
        if _E2E_PUBLIC_KEY_PEM_TEXT:
            _E2E_PUBLIC_KEY_PEM = serialization.load_pem_public_key(
                _E2E_PUBLIC_KEY_PEM_TEXT.encode('ascii'))
            return _E2E_PUBLIC_KEY_PEM
    except Exception:
        pass
    for p in _E2E_PUBLIC_KEY_PATHS:
        try:
            if os.path.exists(p):
                with open(p, 'rb') as f:
                    _E2E_PUBLIC_KEY_PEM = serialization.load_pem_public_key(f.read())
                return _E2E_PUBLIC_KEY_PEM
        except Exception:
            continue
    return None


def encrypt_report(plaintext_body):
    """Encrypt a bug report body for E2E transport.

    Returns a dict with:
      - 'version': encryption format version (1)
      - 'encrypted_aes_key': RSA-OAEP encrypted AES key (base64)
      - 'nonce': AES-GCM nonce (base64)
      - 'ciphertext': AES-GCM encrypted report (base64)

    Returns None if the public key is not available (graceful degradation —
    unsigned/unencrypted reports are still sent as fallback).
    """
    pub_key = _load_public_key()
    if pub_key is None:
        return None
    try:
        # 1. Generate random AES-256-GCM key + nonce
        aes_key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)  # 96-bit nonce for AES-GCM

        # 2. Encrypt report body with AES-256-GCM (authenticated encryption)
        aesgcm = AESGCM(aes_key)
        ciphertext = aesgcm.encrypt(nonce, plaintext_body.encode('utf-8'), None)

        # 3. Encrypt AES key with RSA-4096 OAEP-SHA256
        encrypted_aes_key = pub_key.encrypt(
            aes_key,
            asym_padding.OAEP(
                mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            )
        )

        return {
            'version': 1,
            'encrypted_aes_key': base64.b64encode(encrypted_aes_key).decode(),
            'nonce': base64.b64encode(nonce).decode(),
            'ciphertext': base64.b64encode(ciphertext).decode(),
        }
    except Exception:
        return None


def encrypt_report_json(plaintext_body):
    """Encrypt and return as a JSON string for GitHub issue body."""
    encrypted = encrypt_report(plaintext_body)
    if encrypted is None:
        return None
    return json.dumps(encrypted)


def decrypt_report(encrypted_dict, private_key_pem_path):
    """Decrypt an encrypted report (dev machine only).

    Args:
        encrypted_dict: dict with version, encrypted_aes_key, nonce, ciphertext
        private_key_pem_path: path to the RSA-4096 private key PEM file

    Returns:
        Decrypted plaintext string, or None on failure.
    """
    try:
        with open(private_key_pem_path, 'rb') as f:
            private_key = serialization.load_pem_private_key(
                f.read(),
                password=b'sbt-dev-temp-passphrase',
            )

        # 1. Decrypt AES key with RSA-4096 OAEP-SHA256
        aes_key = private_key.decrypt(
            base64.b64decode(encrypted_dict['encrypted_aes_key']),
            asym_padding.OAEP(
                mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            )
        )

        # 2. Decrypt report body with AES-256-GCM
        nonce = base64.b64decode(encrypted_dict['nonce'])
        ciphertext = base64.b64decode(encrypted_dict['ciphertext'])
        aesgcm = AESGCM(aes_key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)

        return plaintext.decode('utf-8')
    except Exception:
        return None
