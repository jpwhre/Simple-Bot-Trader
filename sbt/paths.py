"""Simple Bot Trader — paths and directories.

CROSS-PLATFORM: the app targets Linux, Windows and macOS, so nothing here may
assume one OS. The default config root follows each OS's convention:
  Linux   ~/.config/simple-bot-trader
  Windows %APPDATA%/SimpleBotTrader
  macOS   ~/Library/Application Support/SimpleBotTrader
Logs and API locks live in the machine's TEMP directory (Linux /tmp, Windows
%TEMP%, macOS /tmp). Set SBT_CONFIG_DIR (or pass --config-dir to main.py) to run
an isolated profile: each profile gets its own settings / keys / DB / crash
reports and its own log file, so one machine can run different exchanges
independently.
"""
import os
import sys
import tempfile


def _default_config_root():
    """The OS-appropriate default config root (never overridden by env)."""
    if sys.platform.startswith('win'):
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'SimpleBotTrader')
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/SimpleBotTrader')
    return os.path.expanduser('~/.config/simple-bot-trader')


DEFAULT_CONFIG_ROOT = _default_config_root()

_cfg = os.environ.get('SBT_CONFIG_DIR') or ''
if _cfg:
    CONFIG_ROOT = os.path.abspath(os.path.expanduser(_cfg))
else:
    CONFIG_ROOT = DEFAULT_CONFIG_ROOT

CONFIG_DIR = os.path.join(CONFIG_ROOT, 'config')

KEYS_FILE = os.path.join(CONFIG_DIR, 'keys.enc')
SALT_FILE = os.path.join(CONFIG_DIR, '.salt')
SETTINGS_PATH = os.path.join(CONFIG_DIR, 'settings.json')
DB_PATH = os.path.join(CONFIG_DIR, 'trades.db')
CRASH_DIR = os.path.join(CONFIG_DIR, 'crash_reports')

# Machine-independent temp locations (Linux /tmp, Windows %TEMP%, macOS /tmp).
_TMP = tempfile.gettempdir()
# The API lock directory is shared across every profile (one bot per API key).
API_LOCK_DIR = os.path.join(_TMP, 'sbt_api_locks')

# logs are profile-keyed so multiple instances never interleave
if os.path.abspath(CONFIG_ROOT) == os.path.abspath(DEFAULT_CONFIG_ROOT):
    LOG_FILE = os.path.join(_TMP, 'sbt_events.log')
    ORDER_DEBUG = os.path.join(_TMP, 'sbt_order_debug.log')
else:
    _profile = os.path.basename(CONFIG_ROOT.rstrip(os.sep)) or 'profile'
    LOG_FILE = os.path.join(_TMP, f'sbt_events_{_profile}.log')
    ORDER_DEBUG = os.path.join(_TMP, f'sbt_order_debug_{_profile}.log')


def secure_dir(path):
    """Create (if needed) and lock a directory to owner-only (0700).

    The config dir holds encrypted exchange keys; on multi-user systems a
    world-readable copy lets another local user copy keys.enc + .salt and
    decrypt on this machine."""
    try:
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o700)
        return True
    except Exception:
        return False


def secure_file(path):
    """Lock a single file to owner-only (0600). No-op on platforms without
    chmod (never raises)."""
    try:
        os.chmod(path, 0o600)
        return True
    except Exception:
        return False


def secure_config():
    """Harden the config dir and every sensitive file inside it. Called at app
    startup so a previously loose-perm config is fixed immediately, and again
    after each sensitive write (keys, settings, DB, state)."""
    if not secure_dir(CONFIG_DIR):
        return
    for name in ('keys.enc', '.salt', 'settings.json', 'trades.db',
                 'update_state.json', 'pending_reports.json',
                 'eula_accepted', 'legacy.json'):
        p = os.path.join(CONFIG_DIR, name)
        if os.path.exists(p):
            secure_file(p)
    if os.path.isdir(CRASH_DIR):
        secure_dir(CRASH_DIR)
