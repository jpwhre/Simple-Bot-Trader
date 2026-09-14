"""Desktop theme detection + stylesheet application.

Tries the desktop's dark preference per-OS: Linux (GNOME color-scheme, XFCE
theme name, GTK theme name, GTK_THEME env), macOS (AppleInterfaceStyle),
Windows (light default — the in-app theme toggle covers the choice). Falls
back to dark — matching the original app.
"""
import os
import re
import subprocess
import sys


def _cmd(args):
    try:
        out = subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=5)
        return out.decode('utf-8', 'ignore').strip()
    except Exception:
        return None


def detect_dark_theme():
    # BUG-011: check the free env override FIRST — no subprocess spawn at all
    # when it's set (common on this machine / launch scripts).
    v = os.environ.get('GTK_THEME', '')
    if v:
        return 'dark' in v.lower()
    # Non-Linux: the GTK/gsettings commands below don't exist on Windows/macOS.
    if not sys.platform.startswith('linux'):
        if sys.platform == 'darwin':
            # macOS native dark mode: `defaults read -g AppleInterfaceStyle`
            v = _cmd(['defaults', 'read', '-g', 'AppleInterfaceStyle'])
            if v:
                return 'dark' in v.lower()
        # Windows / unknown: no reliable CLI dark signal — the in-app
        # Auto/Light/Dark toggle is the mechanism.
        return False
    # 1. GNOME / modern desktops: color-scheme preference
    v = _cmd(['gsettings', 'get', 'org.gnome.desktop.interface', 'color-scheme'])
    if v:
        v = v.strip("'").strip('"')
        if 'dark' in v.lower():
            return True
        if 'prefer-light' in v.lower():
            return False
    # 2. XFCE: actual theme name (Mint-Y-Dark, etc.)
    v = _cmd(['xfconf-query', '-c', 'xsettings', '-p', '/Net/ThemeName'])
    if v:
        return 'dark' in v.lower()
    # 3. GTK theme name via gsettings
    v = _cmd(['gsettings', 'get', 'org.gnome.desktop.interface', 'gtk-theme'])
    if v and v != "''":
        return 'dark' in v.lower()
    # default: dark (as the original app was)
    return True


def _abs_urls(qss, base_dir):
    """Rewrite relative url(...) image paths to absolute — Qt resolves
    relative stylesheet URLs against the process CWD, not the QSS file, so
    theme images (checkbox ticks, etc.) would silently fail otherwise."""
    try:
        def _repl(m):
            p = m.group(1).strip()
            if p[:1] in ('"', "'") and p[-1:] == p[:1]:
                p = p[1:-1]
            return f'url({os.path.join(base_dir, p)})'
        return re.sub(r'url\(\s*([^)]+?)\s*\)', _repl, qss)
    except Exception:
        return qss


def apply_theme(app, preference=None):
    """Apply the theme stylesheet. `preference` is 'auto' (default), 'light',
    or 'dark' — 'auto' uses desktop detection. Returns True if the applied
    theme is dark."""
    pref = (preference or 'auto').strip().lower()
    if pref == 'dark':
        dark = True
    elif pref == 'light':
        dark = False
    else:
        dark = detect_dark_theme()
    styles_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui', 'styles')
    qss = os.path.join(styles_dir, 'dark.qss' if dark else 'light.qss')
    try:
        if os.path.exists(qss):
            with open(qss, 'r') as f:
                app.setStyleSheet(_abs_urls(f.read(), styles_dir))
    except Exception:
        pass
    return dark
