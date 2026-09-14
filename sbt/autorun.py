"""Cross-platform return-to-state autostart — one entry per OS.

Linux:   systemd --user units (sbt.systemd.enable_all)
macOS:   LaunchAgent plists + launchctl (sbt.launchd.enable_all)
Windows: Startup-folder .vbs -> venv pythonw main.py --if-was-launched

Each entry runs `main.py --if-was-launched` (via run.sh on unix), which exits
silently when the profile's launched.marker is absent — so the OS always
reopens only bots that were actually running when the machine went down.
"""
import os
import sys

from . import paths

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _venv_pythonw():
    venv = os.path.join(_APP_DIR, 'venv', 'Scripts', 'pythonw.exe')
    return venv if os.path.exists(venv) else None


def windows_startup_files():
    """Create 'Simple Bot Trader.vbs' in the user Startup folder so the app
    reopens at login (return-to-state) if its launched.marker is present."""
    base = os.environ.get('APPDATA') or os.path.expanduser('~\\AppData\\Roaming')
    startup = os.path.join(base, 'Microsoft', 'Windows', 'Start Menu',
                           'Programs', 'Startup')
    main_py = os.path.join(_APP_DIR, 'main.py')
    pythonw = _venv_pythonw()
    written = []
    if not pythonw:
        return written
    try:
        os.makedirs(startup, exist_ok=True)
    except Exception:
        return written
    path = os.path.join(startup, 'Simple Bot Trader.vbs')
    body = ('Set oWS = WScript.CreateObject("WScript.Shell")\n'
            f'oWS.Run """"{pythonw}"" ""{main_py}"" --if-was-launched", 0, False\n')
    try:
        with open(path, 'w') as f:
            f.write(body)
        written.append(path)
    except Exception:
        pass
    return written


def enable_all():
    """Enable return-to-state autostart on the current OS. Returns a list of
    (platform, detail) entries for diagnostics / tests."""
    if sys.platform == 'darwin':
        from . import launchd
        written, _loaded = launchd.enable_all()
        return [('launchd', p) for p in written]
    if sys.platform.startswith('win'):
        return [('windows', p) for p in windows_startup_files()]
    from . import systemd
    written, enabled = systemd.enable_all()
    return [('systemd', p) for p in written]