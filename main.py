"""Simple Bot Trader entry point.

Profiles: pass `--config-dir <path>` (or set SBT_CONFIG_DIR) to run an isolated
profile with its own settings / keys / DB / logs — lets one machine run
different exchanges as separate instances. Each profile allows ONE running
instance (a lock prevents double-trading the same account).
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback

# resolve the profile BEFORE importing sbt.* (paths reads SBT_CONFIG_DIR)
_cfg = None
try:
    if '--config-dir' in sys.argv:
        _cfg = sys.argv[sys.argv.index('--config-dir') + 1]
    elif os.environ.get('SBT_CONFIG_DIR'):
        _cfg = os.environ['SBT_CONFIG_DIR']
except Exception:
    _cfg = None
if _cfg:
    os.environ['SBT_CONFIG_DIR'] = os.path.abspath(os.path.expanduser(_cfg))

from PyQt5.QtCore import QLockFile
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox

from sbt import i18n
from sbt.i18n import tr
from sbt import paths
from sbt import eula
from sbt.config import load_settings
from sbt.theme import apply_theme
from sbt.ui.main_window import MainWindow, WINDOW_ICON


def _install_crash_hook():
    crash_dir = paths.CRASH_DIR
    try:
        os.makedirs(crash_dir, exist_ok=True)
    except Exception:
        crash_dir = tempfile.gettempdir()

    # Dev machine: also save crash reports to Desktop for quick access.
    _dev_crash_dir = None
    try:
        from sbt.admin import is_admin
        if is_admin():
            _dev_crash_dir = os.path.join(os.path.expanduser('~'),
                                          'Desktop', 'crash_reports')
            os.makedirs(_dev_crash_dir, exist_ok=True)
    except Exception:
        pass

    def _dump(exc_type, exc_value, exc_tb):
        try:
            ts = time.strftime('%Y%m%d_%H%M%S')
            fn = os.path.join(crash_dir, f"crash_{ts}.log")
            with open(fn, 'w') as f:
                f.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}]\n')
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
            # Dev machine: copy to Desktop + leave a marker for the next-launch popup.
            if _dev_crash_dir:
                try:
                    import shutil
                    desktop_fn = os.path.join(_dev_crash_dir, f"crash_{ts}.log")
                    shutil.copy2(fn, desktop_fn)
                    # Marker: the popup reads this on next launch.
                    marker = os.path.join(_dev_crash_dir, '_latest.json')
                    with open(marker, 'w') as mf:
                        import json
                        json.dump({'file': desktop_fn,
                                   'error': f'{exc_type.__name__}: {exc_value}',
                                   'ts': time.strftime('%Y-%m-%d %H:%M:%S')}, mf)
                except Exception:
                    pass
            # OPT-IN: queue the crash report for the developer's reports repo
            # (sent in the background now, or at the next launch — the queue
            # survives the crash). Never blocks, never fails the app.
            try:
                from sbt.config import load_settings
                s = load_settings()
                if s.get('report_opt_in', False):
                    from sbt import report_transport
                    report_transport.send_in_background(
                        fn, 'crash',
                        f'crash: {exc_type.__name__}: {exc_value}', 1, s)
            except Exception:
                pass
            # AUTO-UPDATE rollback: if a pending update's NEW version crashed,
            # restore the previous version so the next launch runs it (and
            # file the opt-in report above).
            try:
                from sbt import auto_update
                auto_update.maybe_rollback_on_crash()
            except Exception:
                pass
        except Exception:
            pass

    def _excepthook(exc_type, exc_value, exc_tb):
        _dump(exc_type, exc_value, exc_tb)

    def _thread_hook(args):
        _dump(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _excepthook
    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass


def _show_api_picker(saved):
    """Modal picker for 'Open Another Instance': choose a SAVED API to open
    as another bot (one bot per API key), or 'Add new bot…'. Returns a profile
    path, '__add_new__', or None (cancelled)."""
    from PyQt5.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                                 QLabel, QVBoxLayout)
    dlg = QDialog()
    dlg.setWindowTitle(tr('Open Another Instance'))
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel('Pick a saved API to open as another bot\n'
                         '(frequency: one per key; auto-invest: one per pair):'))
    combo = QComboBox(dlg)
    for ap in saved:
        combo.addItem(ap['label'], ap['profile'])
    combo.addItem('Add new bot…', '__add_new__')
    lay.addWidget(combo)
    btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    btns.button(QDialogButtonBox.Ok).setText('Open')
    btns.accepted.connect(dlg.accept)
    btns.rejected.connect(dlg.reject)
    lay.addWidget(btns)
    if dlg.exec_() != QDialog.Accepted:
        return None
    return combo.currentData()


def _add_new_bot():
    """Create a brand-new bot profile (its own API). Its first-run setup runs
    the full referral / Transfer-warning / load-or-paste capture flow."""
    from PyQt5.QtWidgets import QInputDialog
    from sbt import api_registry
    try:
        total = len(api_registry.list_saved_apis(exclude_running=False))
    except Exception:
        total = 0
    if total <= 1:
        box = QMessageBox()
        box.setWindowTitle(tr('New Bot API Key'))
        box.setText('Only one API key is saved on this machine (or none).\n\n'
                    'A new bot needs its OWN API key — it cannot reuse one that\n'
                    'is already running. When the new bot\'s setup opens, use\n'
                    '"I have account" / "Don\'t have account" to create one.')
        box.addButton('OK', QMessageBox.AcceptRole)
        box.exec_()
    name, ok = QInputDialog.getText(
        None, 'Add New Bot',
        'Name this bot — it becomes its own config folder\n'
        'for its own API key:\n\n'
        'e.g.  kraken   binance   coinbase-usd   ...')
    if not ok or not name.strip():
        return
    name = ''.join(c for c in name.strip().lower() if c.isalnum() or c in '-_')
    if not name:
        return
    api_registry.launch_profile(api_registry.new_profile_dir(name))
    # multibot return-to-state (user, 2026-08-16): every profile gets its own
    # systemd unit so it relaunches after a reboot if it was open. Regenerate
    # now so the new bot is covered immediately.
    try:
        from sbt import systemd
        systemd.enable_all()
    except Exception:
        pass


def _launch_new_instance():
    """Open another bot instance: pick one of the SAVED APIs (multibot rules:
    one frequency bot per key + N auto-invest bots on different pairs), or add
    a new one. Conflicting bots are hidden; an EMPTY list asks explicitly —
    it NEVER auto-spawns a new bot (that was the launcher loop, user 2026-08-14)."""
    from sbt import api_registry
    from PyQt5.QtWidgets import QMessageBox
    saved = []
    try:
        saved = api_registry.list_saved_apis(exclude_running=True)
    except Exception:
        saved = []
    if not saved:
        box = QMessageBox()
        box.setWindowTitle(tr('Open Another Instance'))
        box.setText('No other bot is available to open right now.\n\n'
                    'Every saved API is either already running or\n'
                    'conflicts with a running bot (same pair).\n\n'
                    'Add a bot with its OWN API key, or an auto-invest\n'
                    'bot on a DIFFERENT pair?')
        btn_add = box.addButton('Add new bot', QMessageBox.AcceptRole)
        box.addButton('Cancel', QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is btn_add:
            _add_new_bot()
        return
    choice = _show_api_picker(saved)
    if choice is None:
        return
    if choice == '__add_new__':
        _add_new_bot()
    else:
        api_registry.launch_profile(choice)


def _api_identity():
    """(fingerprint, metadata) for THIS bot's API + mode. The fingerprint is
    the lock (strict per key, or per key|pair in auto-invest); the metadata
    rides along as a sidecar so the multibot picker can see WHAT is running
    (exchange, pair, mode) without touching key material."""
    try:
        from sbt import keys as keys_mod
        from sbt.config import load_settings
        from sbt import api_registry
        s = load_settings()
        exchange = (s.get('exchange') or 'coinbase').lower()
        k = keys_mod.load_keys()
        if exchange == 'coinbase':
            cred = k.get('api_key_name', '')
        else:
            cred = k.get('ccxt_api_key', '')
        product_id = (s.get('product_id') or '').strip().upper()
        mode = (s.get('holding_mode') or '').strip().lower()
        fp = api_registry.api_fingerprint(exchange, cred,
                                          product_id=product_id, mode=mode)
        key_fp = api_registry.api_fingerprint(exchange, cred)
        return fp, {'exchange': exchange, 'product_id': product_id,
                    'mode': mode, 'key_fp': key_fp}
    except Exception:
        return None, None


def _api_fingerprint():
    """Back-compat: fingerprint only."""
    return _api_identity()[0]


def _stale_lock_owner_dead(lock):
    """True when the QLockFile's owner process is gone (or unreadable).
    PyQt5's QLockFile has NO getError()/LockStaleError — the old call crashed
    EVERY second-instance launch with AttributeError (the real 'can't open
    another bot' bug). getLockInfo() returns (ok, pid, hostname, appname);
    staleness = the recorded owner pid is not alive."""
    try:
        info = lock.getLockInfo()
        if not info or len(info) < 2 or not info[0]:
            return False            # unreadable but present — don't steal it
        pid = int(info[1] or 0)
        if pid <= 0:
            return False
        if sys.platform.startswith('win'):
            return False            # no portable pid probe — trust tryLock
        try:
            os.kill(pid, 0)
            return False            # owner alive
        except PermissionError:
            return False            # alive, other user
        except OSError:
            return True             # owner gone -> stale
    except Exception:
        return False


def _lock_api(fp, meta=None):
    """Global (cross-profile) lock keyed by the API credential fingerprint.
    MULTIBOT (user, 2026-08-14): a frequency bot locks the whole key; an
    auto-invest bot locks key|pair — so one key may run ONE frequency bot +
    N auto-invest bots on DIFFERENT pairs (same pair always blocked)."""
    lock_dir = paths.API_LOCK_DIR
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except Exception:
        return None
    from sbt import api_registry
    lock = QLockFile(os.path.join(lock_dir, f'{fp}.lock'))
    if not lock.tryLock(100):
        if _stale_lock_owner_dead(lock):
            lock.removeStaleLockFile()
            api_registry.remove_lock_sidecar(fp)
            lock.tryLock(100)
    if not lock.isLocked():
        QMessageBox.warning(
            None, 'API Already In Use',
            'This API is already running in another bot on this pair.\n\n'
            'One frequency bot per API key; auto-invest bots must use\n'
            'different pairs. Pick another saved API, or add a bot with\n'
            'its own API key / a different pair.')
        return None
    api_registry.write_lock_sidecar(
        fp, (meta or {}).get('exchange', ''),
        (meta or {}).get('product_id', ''),
        (meta or {}).get('mode', ''),
        key_fp=(meta or {}).get('key_fp', ''))
    return lock


def _confirm_uninstall():
    """Disagree on the EULA -> confirm -> remove the app entirely (no leftover
    config, logs, launchers). Returns True to proceed with uninstall.
    Closing the box / Escape = cancel (returns False) — never a crash."""
    box = QMessageBox()
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(tr('Uninstall'))
    box.setText('You chose Disagree on the license agreement.\n\n'
                'The application will be REMOVED from this computer, including '
                'all settings, keys, and data. This cannot be undone.\n\n'
                'Continue uninstalling?')
    btn_uninstall = box.addButton('Uninstall', QMessageBox.AcceptRole)
    box.addButton('Cancel', QMessageBox.RejectRole)
    box.exec_()
    # BUG-017: clickedButton() is None when the box is dismissed (X/Escape) —
    # treat that as Cancel, never dereference it.
    return box.clickedButton() is btn_uninstall


def main():
    from sbt import runtime_state
    # Return-to-state BOOT GUARD: when the OS relaunches the bot (systemd user
    # service after a power-up / OS-update reboot, or after an auto-update), it
    # passes --if-was-launched. The bot only opens itself if the was-launched
    # marker says it was open when the machine went down — otherwise it exits
    # silently ("if bot wasn't open, it doesn't just open anyway").
    try:
        if '--if-was-launched' in sys.argv and not runtime_state.was_launched():
            return 0
    except Exception:
        pass
    # AUTO-UPDATE verify: if an update was applied but never took effect
    # (version mismatch), restore the backup and relaunch the previous version.
    try:
        from sbt import auto_update
        if auto_update.verify_after_launch() == 'rolled_back':
            auto_update.relaunch()
            return 0
    except Exception:
        pass
    # Harden the config dir + every sensitive file (keys, settings, DB, state)
    # to owner-only. Fixes a loose-perm config created by an older build.
    try:
        paths.secure_config()
    except Exception:
        pass
    _install_crash_hook()
    # Rolling crash log cleanup: keep at most 50 crash/error logs, delete oldest
    try:
        from sbt.log_chain import cleanup_crash_reports
        cleanup_crash_reports(paths.CRASH_DIR)
    except Exception:
        pass
    # UI language: settings override, else OS/keyboard locale (never IP).
    # Translation is local — no data collected, reports stay English.
    try:
        i18n.set_language((load_settings().get('language') or '')
                          or i18n.detect_language())
    except Exception:
        pass
    # OPT-IN report transport: retry any unsent error/crash reports from a
    # previous session in the background (never blocks startup).
    try:
        from sbt import report_transport
        from sbt.config import load_settings
        _rs = load_settings()
        threading.Thread(target=report_transport.send_pending_reports,
                         args=(_rs,), daemon=True).start()
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName('Simple Bot Trader')
    # DEV-ONLY (never ships): right-click widget inspector. SBT_ADMIN=1 on the
    # developer's build enables it; the shipped build has no inspector.
    # Import is lazy so the ship copy can exclude sbt/ui/inspector.py.
    try:
        from sbt.admin import is_admin
        if is_admin():
            from sbt.ui.inspector import install_widget_inspector
            install_widget_inspector(app)
    except Exception:
        pass
    # Graceful close clears the was-launched marker (a crash/power loss leaves
    # it behind so the boot-time relaunch still happens).
    try:
        app.aboutToQuit.connect(runtime_state.clear_launched)
        from sbt.engine import flush_log_chain
        app.aboutToQuit.connect(flush_log_chain)
    except Exception:
        pass

    # UI theme: saved preference (auto/light/dark) — applied up front so the
    # EULA gate and every window are themed consistently. 'auto' = desktop.
    try:
        _theme_pref = load_settings().get('theme', 'auto')
    except Exception:
        _theme_pref = 'auto'
    apply_theme(app, _theme_pref)

    # Dev machine: show crash report from last run (if any). The crash hook
    # saves a copy to ~/Desktop/crash_reports/ and leaves _latest.json.
    try:
        from sbt.admin import is_admin
        if is_admin():
            _marker = os.path.join(os.path.expanduser('~'),
                                   'Desktop', 'crash_reports', '_latest.json')
            if os.path.exists(_marker):
                import json as _json
                with open(_marker) as _mf:
                    _info = _json.load(_mf)
                os.remove(_marker)  # consume the marker — show once
                _err = _info.get('error', 'unknown')
                _file = _info.get('file', '')
                _ts = _info.get('ts', '')
                _box = QMessageBox()
                _box.setIcon(QMessageBox.Warning)
                _box.setWindowTitle('Crash Report (dev)')
                _box.setText(f'The bot crashed on {_ts}:\n\n{_err}\n\n'
                             f'Full log: {_file}')
                _box.setInformativeText('Check ~/Desktop/crash_reports/ for details.')
                _box.addButton('OK', QMessageBox.AcceptRole)
                _box.exec_()
    except Exception:
        pass

    # EULA gate — BEFORE first-run ("Add API"), before the bot is constructed,
    # before any locks. Disagree = full uninstall, no leftovers.
    if not eula.eula_accepted():
        dlg = eula.EulaDialog()
        if dlg.exec_() == QDialog.Accepted:
            eula.record_acceptance()
        else:
            if _confirm_uninstall():
                eula.uninstall()
            return 0

    # one running instance per profile — prevents double-trading one account
    try:
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
    except Exception:
        pass
    lock = QLockFile(os.path.join(paths.CONFIG_DIR, 'instance.lock'))
    if not lock.tryLock(100):
        # a lock left by a crashed/dead process is stale — reclaim it
        # (PyQt5 QLockFile has no getError/LockStaleError: that call CRASHED
        # every second-instance launch — check the owner pid instead)
        if _stale_lock_owner_dead(lock):
            lock.removeStaleLockFile()
            lock.tryLock(100)
        if not lock.isLocked():
            box = QMessageBox()
            box.setWindowTitle(tr('Already Running'))
            box.setText('This bot profile is already running.\n\n'
                        'Each profile runs one bot. You can open another\n'
                        'instance for a different exchange / API key.')
            btn_new = box.addButton('Open Another Instance', QMessageBox.AcceptRole)
            box.addButton('Cancel', QMessageBox.RejectRole)
            box.exec_()
            if box.clickedButton() is btn_new:
                _launch_new_instance()
            return 0

    # one bot per API key — global across every profile
    fp, _api_meta = _api_identity()
    api_lock = _lock_api(fp, _api_meta) if fp else None
    if api_lock is None and fp is not None:
        return 0

    # Initialize HMAC log chain from the API key fingerprint (tamper detection).
    # Chain stays disabled until a real API key exists (skip at first-run).
    try:
        from sbt.engine import init_log_chain
        _exc = (_api_meta or {}).get('exchange', '')
        # Derive the HMAC secret from the raw API credential (full SHA256,
        # not the truncated lock fingerprint).  Coinbase uses api_key_name;
        # CCXT uses ccxt_api_key.
        from sbt import keys as _keys_mod
        _k = _keys_mod.load_keys()
        _cred = _k.get('api_key_name', '') if _exc == 'coinbase' else _k.get('ccxt_api_key', '')
        if _exc and _cred:
            init_log_chain(_exc, _cred)
    except Exception:
        pass

    # This instance is now confirmed open — leave the was-launched marker so a
    # power-up / OS-update reboot / auto-update knows to reopen the bot.
    try:
        runtime_state.mark_launched()
    except Exception:
        pass

    try:
        app.setWindowIcon(QIcon(WINDOW_ICON))
    except Exception:
        pass
    try:
        win = MainWindow()
    except ValueError as e:
        # BUG-005: bad exchange id (or other provider config) — tell the user,
        # don't just write a crash file and die.
        QMessageBox.critical(
            None, 'Configuration Error',
            f'Could not start the bot:\n\n{e}\n\n'
            'Fix the exchange id in Settings (or delete the saved keys to '
            'redo first-time setup).')
        return 1
    win.show()
    # Dev machine: clear old crash logs on successful launch — if the app
    # reached here, previous crashes are fixed. Only clears Desktop copies.
    try:
        from sbt.admin import is_admin
        if is_admin():
            _dc = os.path.join(os.path.expanduser('~'), 'Desktop', 'crash_reports')
            if os.path.isdir(_dc):
                for f in os.listdir(_dc):
                    if f.startswith(('crash_', 'error_')) and f.endswith('.log'):
                        try:
                            os.remove(os.path.join(_dc, f))
                        except Exception:
                            pass
    except Exception:
        pass
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
