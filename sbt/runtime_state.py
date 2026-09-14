"""Runtime state — survives reboots so the bot returns to what it was doing.

- `started`: the user's start/stop intent. Persisted on Start/Stop/close and
  re-read on launch so a STOPPED bot stays STOPPED after a reboot (return-to-
  state; user: "Every reboot reverts to watching, and I have to stop it").
- `own_base`: the bot's accumulated base position (incremented on each buy,
  decremented on each sell, reset on Force Close). This replaces the old
  trades.db journal — the exchange is the source of truth for cost basis,
  balance, and trade history. own_base is ONLY used to distinguish bot-bought
  coins from user-held coins (separate/combine mode detection).
- pending-orders file: the engine persists resting (keep-open) limit orders so
  it can re-attach and adopt them after a reboot or in the next cycle.

Default for a fresh install: started = True (today's WATCHING behavior), so the
first run is unchanged until the user presses Stop.
"""
import json
import os
import time

from . import paths

_STATE_FILE = os.path.join(paths.CONFIG_DIR, 'runtime_state.json')
# The "was launched" marker: present = the app WAS open when the machine went
# down (power loss / OS-update reboot / crash / before an auto-update), so the
# OS relaunch must open it back up and return to state. Absent = the user chose
# not to run it — the bot must NOT open itself. Only a GRACEFUL close clears it.
_MARKER_FILE = os.path.join(paths.CONFIG_DIR, 'launched.marker')


def mark_launched():
    """Write the was-launched marker the moment the app is confirmed open."""
    try:
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        with open(_MARKER_FILE, 'w') as f:
            json.dump({'pid': os.getpid(), 'ts': int(time.time())}, f)
        return True
    except Exception:
        return False


def clear_launched():
    """Called on a GRACEFUL close only. A crash / power loss leaves the marker
    behind — which is exactly when the boot-time relaunch must happen."""
    try:
        if os.path.exists(_MARKER_FILE):
            os.remove(_MARKER_FILE)
        return True
    except Exception:
        return False


def was_launched():
    """True if the app was open when the machine last went down."""
    return os.path.exists(_MARKER_FILE)


def load_started():
    try:
        with open(_STATE_FILE) as f:
            return bool(json.load(f).get('started', True))
    except Exception:
        return True


def save_started(started):
    try:
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        cur = {}
        try:
            with open(_STATE_FILE) as f:
                cur = json.load(f)
        except Exception:
            pass
        cur['started'] = bool(started)
        with open(_STATE_FILE, 'w') as f:
            json.dump(cur, f)
        return True
    except Exception:
        return False


def load_own_base():
    """The bot's accumulated base (how much the bot bought and still holds).
    Returns 0.0 on any error or fresh install."""
    try:
        with open(_STATE_FILE) as f:
            return float(json.load(f).get('own_base', 0.0))
    except Exception:
        return 0.0


def save_own_base(own_base):
    """Atomically persist the bot's own base. Reads the existing state to
    preserve other fields (started, etc.)."""
    try:
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        cur = {}
        try:
            with open(_STATE_FILE) as f:
                cur = json.load(f)
        except Exception:
            pass
        cur['own_base'] = max(0.0, float(own_base))
        with open(_STATE_FILE, 'w') as f:
            json.dump(cur, f)
        return True
    except Exception:
        return False

