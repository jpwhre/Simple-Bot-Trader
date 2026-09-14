"""Return-to-state LaunchAgent management (macOS) — mirrors sbt/systemd.py.

macOS has no system-level autostart for user apps; a LaunchAgent with RunAtLoad
fires when the user logs in. ONE agent per bot profile, each running
run.sh --if-was-launched [--config-dir <profile>]: main.py exits silently when
that profile's launched.marker is absent, so agents never open a bot the user
chose not to run. This module is macOS-only by construction (Windows uses the
Startup folder, Linux uses systemd).
"""
import os
import plistlib
import sys

from . import api_registry, paths

_AGENTS_DIR = os.path.expanduser('~/Library/LaunchAgents')
_LABEL = 'com.simplebottrader'
_IS_MAC = sys.platform == 'darwin'

_RUN_SH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'run.sh')


def _label(profile_dir):
    label = _LABEL
    if os.path.abspath(profile_dir) != os.path.abspath(paths.DEFAULT_CONFIG_ROOT):
        base = os.path.basename(os.path.abspath(profile_dir).rstrip(os.sep))
        prefix = api_registry._PROFILE_BASE + '-'
        name = base[len(prefix):] if base.startswith(prefix) else base
        safe = ''.join(c for c in name if c.isalnum() or c in '._-') or 'profile'
        label = f'{_LABEL}-{safe}'
    return label


def _plist_content(profile_dir):
    args = ['--if-was-launched']
    if os.path.abspath(profile_dir) != os.path.abspath(paths.DEFAULT_CONFIG_ROOT):
        args += ['--config-dir', os.path.abspath(profile_dir)]
    return {
        'Label': _label(profile_dir),
        'ProgramArguments': ['/bin/bash', _RUN_SH] + args,
        'RunAtLoad': True,
        'KeepAlive': False,
        'WorkingDirectory': os.path.dirname(_RUN_SH),
        'StandardErrorPath': '/tmp/sbt_launchd.log',
        'LimitLoadToSessionType': 'Aqua',
    }


def write_agents():
    """Write (overwrite) a LaunchAgent plist for the default profile AND every
    named profile. Returns the list of plist paths written. macOS only."""
    if not _IS_MAC:
        return []
    written = []
    try:
        os.makedirs(_AGENTS_DIR, exist_ok=True)
    except Exception:
        return written
    seen = set()
    for d in [paths.DEFAULT_CONFIG_ROOT] + list(api_registry.profile_dirs()):
        if os.path.abspath(d) in seen:
            continue
        seen.add(os.path.abspath(d))
        path = os.path.join(_AGENTS_DIR, _label(d) + '.plist')
        try:
            with open(path, 'wb') as f:
                plistlib.dump(_plist_content(d), f)
            written.append(path)
        except Exception:
            continue
    return written


def enable_all():
    """Write every agent + best-effort `launchctl load -w`. Returns
    (written_paths, loaded_ok). macOS only — no-op elsewhere."""
    import subprocess
    if not _IS_MAC:
        return [], []
    written = write_agents()
    loaded = []
    for p in written:
        try:
            r = subprocess.run(['launchctl', 'load', '-w', p],
                               capture_output=True, timeout=30)
            loaded.append((p, r.returncode == 0))
        except Exception:
            loaded.append((p, False))
    return written, loaded