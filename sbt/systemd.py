"""Return-to-state systemd unit management — ONE unit per bot profile.

Why (user, 2026-08-16): multibot means several profiles (the default config
root + every `simple-bot-trader-<name>` / `SimpleBotTrader-<name>` dir), each
with its OWN runtime_state.json + launched.marker. Each profile's bot must
reopen itself after a reboot IF it was open when the machine went down. One
systemd unit can only relaunch ONE profile, so every profile needs its own
unit, and each passes `--config-dir <profile>`.

Unit name:  simple-bot-trader.service                  (default profile)
            simple-bot-trader-<name>.service           (named profile)

Each unit runs run.sh --if-was-launched [--config-dir <profile>]. The app
itself exits silently when that profile's marker is absent — so units NEVER
open a bot the user chose not to run, and there is never a background process.
"""
import os
import sys

from . import api_registry, paths

_UNIT_DIR = os.path.expanduser('~/.config/systemd/user')
_DEFAULT_UNIT = 'simple-bot-trader.service'

# systemd is Linux-only. Windows uses the Startup folder and macOS uses a
# LaunchAgent for return-to-state (ship checklist) — this module must never
# be auto-invoked on those OSes (cross-platform / on-device requirement,
# user 2026-08-16).
_IS_LINUX = (not sys.platform.startswith('win')) and (sys.platform != 'darwin')

# run.sh lives next to main.py (one app, many profiles).
_RUN_SH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'run.sh')

_HEADER = (
    '# Simple Bot Trader return-to-state relaunch (auto-generated).\n'
    '# Reopen this PROFILE\'s bot at login ONLY IF it was open when the\n'
    '# machine went down (main.py --if-was-launched checks the profile\'s own\n'
    '# launched.marker and exits silently when it is absent).\n'
    '# Regenerate with:  python3 -c "from sbt import systemd; systemd.enable_all()"\n'
)


def _unit_content(profile_dir=None, name=None):
    args = '--if-was-launched'
    if profile_dir:
        args += f' --config-dir {os.path.abspath(profile_dir)}'
    if name:
        args += f' # profile {name}'
    return (_HEADER +
            '[Unit]\n'
            'Description=Simple Bot Trader return-to-state relaunch\n'
            'After=graphical-session.target\n'
            '\n'
            '[Service]\n'
            'Type=simple\n'
            f'ExecStart={_RUN_SH} {args}\n'
            'Environment=DISPLAY=:0.0\n'
            'Restart=on-failure\n'
            'RestartSec=5\n'
            '\n'
            '[Install]\n'
            'WantedBy=default.target\n')


def _unit_name(profile_dir):
    if os.path.abspath(profile_dir) == os.path.abspath(paths.DEFAULT_CONFIG_ROOT):
        return _DEFAULT_UNIT
    base = os.path.basename(os.path.abspath(profile_dir).rstrip(os.sep))
    # strip the profile base prefix, e.g. simple-bot-trader-kraken -> kraken
    prefix = api_registry._PROFILE_BASE + '-'
    if base.startswith(prefix):
        name = base[len(prefix):]
    else:
        name = base
    safe = ''.join(c for c in name if c.isalnum() or c in '._-') or 'profile'
    return f'simple-bot-trader-{safe}.service'


def write_units():
    """Write (overwrite) a systemd user unit for the default profile AND every
    named profile on disk. Returns the list of unit paths written.
    LINUX ONLY — no-op on Windows/macOS (they use Startup/LaunchAgent)."""
    if not _IS_LINUX:
        return []
    written = []
    try:
        os.makedirs(_UNIT_DIR, exist_ok=True)
    except Exception:
        return written
    targets = [paths.DEFAULT_CONFIG_ROOT] + list(api_registry.profile_dirs())
    seen = set()
    for d in targets:
        if os.path.abspath(d) in seen:
            continue
        seen.add(os.path.abspath(d))
        unit = os.path.join(_UNIT_DIR, _unit_name(d))
        try:
            with open(unit, 'w') as f:
                f.write(_unit_content(profile_dir=None if os.path.abspath(d) == os.path.abspath(paths.DEFAULT_CONFIG_ROOT) else d))
            written.append(unit)
        except Exception:
            continue
    return written


def enable_all():
    """Write every profile unit, daemon-reload, and enable them all (idempotent).
    Returns (written_units, enabled_units). LINUX ONLY — no-op elsewhere."""
    import subprocess
    if not _IS_LINUX:
        return [], []
    written = write_units()
    enabled = []
    for unit in written:
        name = os.path.basename(unit)
        try:
            subprocess.run(['systemctl', '--user', 'daemon-reload'],
                           capture_output=True, timeout=30)
            r = subprocess.run(['systemctl', '--user', 'enable', name],
                               capture_output=True, timeout=30)
            enabled.append((name, r.returncode == 0))
        except Exception:
            enabled.append((name, False))
    return written, enabled
