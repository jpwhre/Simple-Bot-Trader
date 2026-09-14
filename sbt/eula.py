"""EULA acceptance gate + full uninstall.

Shown BEFORE first-run ("Add API") and before the bot ever starts:
  - [Agree]    -> records acceptance (per profile, in the config dir), continue.
  - [Disagree] -> asks to confirm, then fully uninstalls the app (no leftovers).

The EULA text itself lives in EULA.txt next to the app so it can be replaced
without touching code. If that file is missing/broken, a short built-in text
is used so the gate can never be silently skipped.
"""
import glob
import os
import shutil
import tempfile

from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                             QTextEdit, QVBoxLayout)

from .i18n import tr
from . import paths

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EULA_FILE = os.path.join(_APP_DIR, 'EULA.txt')
# Acceptance is APP-global, not per-profile: stored in the default config root
# so agreeing once covers every profile (BUG-018). The default root is the
# one path all profiles share regardless of SBT_CONFIG_DIR — resolved per-OS by
# sbt.paths (Linux ~/.config, Windows %APPDATA%, macOS ~/Library/Application
# Support).
_DEFAULT_CFG_ROOT = paths.DEFAULT_CONFIG_ROOT
_ACCEPT_MARKER = os.path.join(_DEFAULT_CFG_ROOT, 'eula_accepted')

# Built-in fallback so the gate always works even if EULA.txt is missing.
_FALLBACK_EULA = (
    'END USER LICENSE AGREEMENT\n\n'
    'This tool uses your own exchange account and your exchange\'s official '
    'API. It cannot and does not bypass any exchange or regulatory '
    'restriction; your exchange enforces what your account is permitted to '
    'trade. You are responsible for using an exchange that is licensed for '
    'you.\n\n'
    '(EULA.txt was not found — this is a placeholder. A real agreement ships '
    'with the application.)\n'
)


def _load_eula_text():
    try:
        with open(_EULA_FILE, 'r', encoding='utf-8') as f:
            text = f.read().strip()
        return text or _FALLBACK_EULA
    except Exception:
        return _FALLBACK_EULA


def eula_accepted():
    return os.path.exists(_ACCEPT_MARKER)


def record_acceptance():
    try:
        os.makedirs(_DEFAULT_CFG_ROOT, exist_ok=True)
        with open(_ACCEPT_MARKER, 'w') as f:
            f.write('accepted')
        paths.secure_file(_ACCEPT_MARKER)
        return True
    except Exception:
        return False


def uninstall_targets():
    """Everything owned by the app (best-effort): install dir, config dirs
    (default + all profiles), desktop launchers, /tmp logs and API locks.
    The window icon under ~/Pictures is the user's own file — NOT removed."""
    targets = set()

    # the app itself
    if os.path.isdir(_APP_DIR):
        targets.add(_APP_DIR)

    # config: default dir + every sibling profile (simple-bot-trader-* /
    # SimpleBotTrader-* depending on the OS convention)
    cfg_root = os.path.dirname(paths.DEFAULT_CONFIG_ROOT.rstrip(os.sep))
    _base = os.path.basename(paths.DEFAULT_CONFIG_ROOT)
    targets.add(paths.DEFAULT_CONFIG_ROOT)
    targets.update(glob.glob(os.path.join(cfg_root, f'{_base}-*')))

    # desktop launchers for the app and any profile (.desktop on Linux/macOS;
    # harmless no-op glob elsewhere)
    desktop = os.path.expanduser('~/Desktop')
    targets.update(glob.glob(os.path.join(desktop, 'Simple Bot Trader*.desktop')))

    # logs, API locks, launch log — in the machine's TEMP dir
    _tmp = tempfile.gettempdir()
    targets.update(glob.glob(os.path.join(_tmp, 'sbt_events*.log')))
    targets.update(glob.glob(os.path.join(_tmp, 'sbt_order_debug*.log')))
    targets.add(os.path.join(_tmp, 'sbt_launch.log'))
    targets.add(paths.API_LOCK_DIR)

    return sorted(t for t in targets if t)


def uninstall():
    """Remove everything the app owns. Tolerates already-missing paths."""
    for target in uninstall_targets():
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    os.remove(target)
                except Exception:
                    pass
        except Exception:
            pass


class EulaDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("End User License Agreement"))
        self.resize(640, 480)

        layout = QVBoxLayout(self)

        heading = QLabel('Read the license agreement before using this app:')
        layout.addWidget(heading)

        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(_load_eula_text())
        layout.addWidget(text, 1)

        note = QLabel('By clicking Agree you accept these terms. '
                      'Disagree removes the application from this computer.')
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QHBoxLayout()
        self.agree_btn = QPushButton('Agree')
        self.disagree_btn = QPushButton('Disagree')
        buttons.addStretch(1)
        buttons.addWidget(self.disagree_btn)
        buttons.addWidget(self.agree_btn)
        layout.addLayout(buttons)

        self.agree_btn.clicked.connect(self.accept)
        self.disagree_btn.clicked.connect(self.reject)
        self.agree_btn.setDefault(True)
