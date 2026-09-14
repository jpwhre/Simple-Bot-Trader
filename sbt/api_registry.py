"""API registry — enumerate the API credentials saved on this machine.

Every profile (default config root plus each `simple-bot-trader-<name>` /
`SimpleBotTrader-<name>`) stores its own encrypted keys.enc (machine-bound:
hostname + PBKDF2 salt) and its exchange id in settings.json. This module lists
those saved APIs so the Settings exchange field and the "Open Another Instance"
flow can offer them as a dropdown — with the ALREADY-RUNNING ones excluded (one
API key runs in exactly ONE bot; the API lock in the temp dir is the authority
for what is running).

Read-only: never writes, never sends anything off-machine. Cross-platform: the
config root and lock dir come from sbt.paths, which follows each OS's
convention (Linux/Windows/macOS).
"""
import base64
import glob
import hashlib
import json
import os
import socket

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import paths

_CFG_ROOT = os.path.dirname(paths.DEFAULT_CONFIG_ROOT.rstrip(os.sep))
_DEFAULT_ROOT = paths.DEFAULT_CONFIG_ROOT
_LOCK_DIR = paths.API_LOCK_DIR
_PROFILE_BASE = os.path.basename(paths.DEFAULT_CONFIG_ROOT)


def profile_dirs():
    """Every config-root directory that could hold a profile (default first)."""
    dirs = []
    if os.path.isdir(_DEFAULT_ROOT):
        dirs.append(_DEFAULT_ROOT)
    dirs += sorted(glob.glob(os.path.join(_CFG_ROOT, f'{_PROFILE_BASE}-*')))
    return dirs


def _machine_key(salt_path):
    salt = b'simple-bot-trader-salt'
    try:
        with open(salt_path, 'rb') as f:
            salt = f.read()
    except Exception:
        pass
    machine_id = socket.gethostname().encode()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000)
    return base64.urlsafe_b64encode(kdf.derive(machine_id))


def read_api(profile_dir):
    """Decrypt ONE profile's keys.enc and return its identity, or None."""
    cfg = os.path.join(profile_dir, 'config')
    keys_file = os.path.join(cfg, 'keys.enc')
    salt_file = os.path.join(cfg, '.salt')
    settings_file = os.path.join(cfg, 'settings.json')
    if not os.path.exists(keys_file):
        return None
    try:
        fernet = Fernet(_machine_key(salt_file))
        with open(keys_file, 'rb') as f:
            data = json.loads(fernet.decrypt(f.read()).decode())
    except Exception:
        return None
    exchange = 'coinbase'
    product_id = ''
    mode = ''
    try:
        with open(settings_file) as f:
            _s = json.load(f)
            exchange = (_s.get('exchange') or 'coinbase').lower()
            product_id = (_s.get('product_id') or '').strip().upper()
            mode = (_s.get('holding_mode') or '').strip().lower()
    except Exception:
        pass
    cred = data.get('api_key_name', '') if exchange == 'coinbase' else data.get('ccxt_api_key', '')
    cred = (cred or '').strip()
    if not cred:
        return None
    # Only the EXCHANGE name is shown in the UI dropdown (user, 2026-08-09) —
    # the credential/key-name stays internal (never displayed). The trading
    # pair + mode ARE shown so the user can tell same-exchange bots apart
    # (multibot: one frequency bot per key + N auto-invest bots per pair).
    label = exchange
    if product_id:
        label += f' · {product_id}'
    if mode == 'auto_invest':
        label += ' · auto-invest'
    return {'profile': profile_dir, 'exchange': exchange, 'cred': cred,
            'product_id': product_id, 'mode': mode, 'label': label}


def api_fingerprint(exchange, cred, product_id=None, mode=''):
    """Fingerprint for the one-API-per-bot lock (must match main.py).
    AUTO-INVEST (user 2026-08-09, Option B): one key may run MANY bots — the
    lock is PER PAIR (blocks two bots on the same asset, allows many assets
    per key). Every other mode keeps the strict one-key-one-bot lock."""
    if not cred:
        return None
    if mode == 'auto_invest' and product_id:
        return hashlib.sha256(
            f'{exchange}|{cred}|{str(product_id).upper()}'.encode()).hexdigest()[:32]
    return hashlib.sha256(f'{exchange}|{cred}'.encode()).hexdigest()[:32]


def running_fingerprints():
    """The API fingerprints currently locked by a running bot (lock files)."""
    fps = set()
    for path in glob.glob(os.path.join(_LOCK_DIR, '*.lock')):
        fps.add(os.path.basename(path)[:-len('.lock')])
    return fps


def write_lock_sidecar(fp, exchange, product_id='', mode='', key_fp=''):
    """Metadata sidecar next to a lock file so other sessions can see WHAT is
    running (pair + mode + key fingerprint), not just an opaque hash. Contains
    NO key material — key_fp is the same non-reversible hash used as the
    strict lock's filename."""
    try:
        os.makedirs(_LOCK_DIR, exist_ok=True)
        with open(os.path.join(_LOCK_DIR, f'{fp}.json'), 'w') as f:
            json.dump({'key_fp': key_fp or fp,
                       'exchange': (exchange or '').lower(),
                       'product_id': str(product_id or '').upper(),
                       'mode': (mode or '').lower(),
                       'ts': int(__import__('time').time())}, f)
    except Exception:
        pass


def remove_lock_sidecar(fp):
    try:
        os.remove(os.path.join(_LOCK_DIR, f'{fp}.json'))
    except Exception:
        pass


def running_bots():
    """Running bots with their lock metadata:
    [{fp, key_fp, exchange, product_id, mode, known}].
    Lock FILES are the authority; sidecars add pair/mode. A lock with no
    sidecar (an older build) is 'known': False — callers treat it
    conservatively (assume it owns the whole key)."""
    out = []
    for path in glob.glob(os.path.join(_LOCK_DIR, '*.lock')):
        fp = os.path.basename(path)[:-len('.lock')]
        meta = {}
        sp = os.path.join(_LOCK_DIR, f'{fp}.json')
        if os.path.exists(sp):
            try:
                with open(sp) as f:
                    meta = json.load(f) or {}
            except Exception:
                meta = {}
        out.append({'fp': fp,
                    'key_fp': meta.get('key_fp') or fp,
                    'exchange': (meta.get('exchange') or '').lower(),
                    'product_id': str(meta.get('product_id') or '').upper(),
                    'mode': (meta.get('mode') or '').lower(),
                    'known': bool(meta)})
    return out


def conflicts_with_running(info, running=None):
    """MULTIBOT conflict rules (user model, 2026-08-14):
      - frequency trading (floor + buy dips): ONE bot per API key
      - auto-invest (buy-only, no floor): one bot per API key PER PAIR
      - same key + same trading pair is ALWAYS a conflict (double-trade risk)
      - a lock with no sidecar (older build) = unknown -> treat as owning the
        whole key (conservative)
    `info` = a read_api() dict (exchange, cred, product_id, mode)."""
    running = running_bots() if running is None else running
    if not running or not info.get('cred'):
        return False
    ex = (info.get('exchange') or '').lower()
    pair = str(info.get('product_id') or '').upper()
    mode = (info.get('mode') or '').lower()
    key_fp = api_fingerprint(ex, info['cred'])
    for r in running:
        if not r.get('known'):
            # older build: only its lock fp is known — assume it owns the key
            if r['fp'] == key_fp:
                return True
            continue
        if r.get('key_fp') != key_fp:
            continue  # a different API key entirely — never a conflict
        r_pair = r['product_id']
        if r['mode'] == 'auto_invest':
            # running auto-invest on this key: blocks the SAME pair only
            if not pair or r_pair == pair:
                return True
        else:
            # a frequency bot holds this key
            if mode != 'auto_invest':
                return True  # one frequency bot per key
            if not pair or r_pair == pair:
                return True  # auto-invest on the frequency bot's own pair
    return False


def list_saved_apis(exclude_running=True):
    """All saved APIs across profiles. With exclude_running (default), omits
    only APIs that CONFLICT with a running bot (multibot rules — a same-key
    auto-invest bot on a different pair stays available)."""
    running = running_bots() if exclude_running else None
    apis = []
    for d in profile_dirs():
        info = read_api(d)
        if info is None:
            continue
        if exclude_running and conflicts_with_running(info, running):
            continue
        apis.append(info)
    return apis


def is_running(info):
    fp = api_fingerprint(info['exchange'], info['cred'])
    return bool(fp and fp in running_fingerprints())


def new_profile_dir(name):
    """The config-root directory for a NEW bot profile (its own API)."""
    return os.path.join(_CFG_ROOT, f'{_PROFILE_BASE}-{name}')


def main_py_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'main.py')


def launch_profile(profile_dir):
    """Start a detached bot instance for the given profile (own config/keys/
    API). Used by the Settings API-switch and the 'Add new bot' flow."""
    import subprocess
    import sys
    env = dict(os.environ)
    env['SBT_CONFIG_DIR'] = os.path.abspath(profile_dir)
    try:
        subprocess.Popen([sys.executable, os.path.abspath(main_py_path()),
                          '--config-dir', os.path.abspath(profile_dir)],
                         env=env, start_new_session=True)
        return True
    except Exception:
        return False
