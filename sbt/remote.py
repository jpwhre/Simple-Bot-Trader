"""Remote-session detection — cross-platform (Linux / Windows / macOS).

Why (user, 2026-08-10): a bot that trades real money must not be operable
from a remote desktop session an attacker could have hijacked (or that the
user left unlocked). When a remote session is detected, the app REFUSES to
trade — it opens, shows the account, but Start is blocked until the session
is local again. Detect-and-block, never fail-secret (the app still opens so
the user can inspect).

Detection is best-effort and layered (any one signal => remote):
  - Environment: SSH X11 forwarding (SSH_CONNECTION/SSH_CLIENT + DISPLAY),
    SSH_TTY.
  - Session: Windows RDP (qwinsta shows an active RDP-Tcp session).
  - Processes: VNC (Xvnc / x11vnc / vncserver), xrdp, TeamViewer, AnyDesk
    (third-party tools are process-name heuristics).

Cross-platform, stdlib-only, no new dependencies.
"""
import os
import subprocess
import sys

# Third-party remote-desktop tools are detected by process name (heuristic —
# they are commercial products without a portable session API).
_REMOTE_PROCESSES = ('teamviewer', 'anydesk', 'xrdp', 'x11vnc', 'xvnc',
                     'vncserver', 'todesk', 'rustdesk', 'splashtop',
                     'remotedesktop', 'ultraviewer', 'mstsc')

# A force-off switch for unattended/headless boxes that legitimately run the
# bot (set SBT_ALLOW_REMOTE=1) — overrides detection so trading is allowed.
_ALLOW_ENV = 'SBT_ALLOW_REMOTE'


def _env_signals():
    """SSH-based remote indicators (cheap, no subprocess)."""
    if os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_CLIENT'):
        # SSH present; X11 forwarding over it is a remote GUI session
        if os.environ.get('DISPLAY'):
            return True
        # SSH_TTY = an interactive remote shell (no GUI, but a remote operator)
        if os.environ.get('SSH_TTY'):
            return True
    return False


def _windows_rdp():
    """Windows: an active RDP (not console) session means someone is remote."""
    if not sys.platform.startswith('win'):
        return False
    try:
        out = subprocess.run(['qwinsta'], capture_output=True, text=True,
                             timeout=10).stdout or ''
        for line in out.splitlines():
            if 'RDP-Tcp' in line and ('Active' in line or 'Conn' in line):
                return True
    except Exception:
        pass
    return False


def _process_running(name):
    """Best-effort process scan (no psutil dependency)."""
    try:
        if sys.platform.startswith('win'):
            out = subprocess.run(
                ['tasklist', '/FI', f'IMAGENAME eq {name}*'],
                capture_output=True, text=True, timeout=10).stdout or ''
            return name.lower() in out.lower()
        out = subprocess.run(['pgrep', '-f', name], capture_output=True,
                             text=True, timeout=10).stdout or ''
        return bool(out.strip())
    except Exception:
        return False


def detect_remote_session():
    """True when any remote-session signal is present (unless explicitly
    allowed via SBT_ALLOW_REMOTE=1)."""
    if os.environ.get(_ALLOW_ENV) == '1':
        return False
    if _env_signals():
        return True
    if _windows_rdp():
        return True
    for name in _REMOTE_PROCESSES:
        if _process_running(name):
            return True
    return False


def remote_reason():
    """Human-readable reason when remote, else ''."""
    if detect_remote_session():
        return ('A remote desktop / remote-access session is active. The bot '
                'does not trade while the machine is remote (set SBT_ALLOW_REMOTE=1 '
                'to override on a trusted box).')
    return ''


def is_forced_local():
    """True when the operator explicitly allowed trading under a remote
    session (SBT_ALLOW_REMOTE=1)."""
    return os.environ.get(_ALLOW_ENV) == '1'
