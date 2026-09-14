"""Main window — ported from the restored app (start/stop guardrail, price feed
wiring, startup holding-sync, deferred settings)."""
import os
import time

from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QMainWindow, QVBoxLayout, QWidget

from .. import keys
from .. import runtime_state
from ..i18n import tr
from .. import runtime_state
from ..config import load_settings
from ..models import BotStatus, Position
from .dashboard import DashboardWidget
from .first_run import FirstRunDialog
from .settings_dialog import SettingsDialog

WINDOW_ICON = os.path.join(os.path.dirname(__file__), 'styles', 'icon.png')


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._executing = False
        self._maintenance_monitor = None

        if not keys.keys_exist():
            dlg = FirstRunDialog(self)
            dlg.exec_()
        k = keys.load_keys()

        from ..engine import TradingBot
        self.bot = TradingBot(k.get('api_key_name', ''), k.get('private_key_pem', ''),
                              log_handler=self._bot_log,
                              ccxt_api_key=k.get('ccxt_api_key', ''),
                              ccxt_secret=k.get('ccxt_secret', ''),
                              ccxt_password=k.get('ccxt_password', ''))

        # Return-to-state: if the user had STOPPED the bot when it last closed,
        # come back STOPPED (it must never silently start trading after a
        # reboot). _startup_sync honours this too.
        if not runtime_state.load_started():
            self.bot.state.status = BotStatus.STOPPED
            self.bot._log('Resuming STOPPED — it was stopped before this app closed', user=True)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        self.dashboard = DashboardWidget(
            self.bot, self._on_start, self._on_stop,
            self._on_close_position, self._on_settings)
        # per-cycle in-app log: the engine clears the log when a new position
        # opens (the user sees only the current buy->hold->sell cycle)
        try:
            self.bot.clear_log_cb = self.dashboard.clear_log
        except Exception:
            pass
        # Exchange maintenance banner: wired through a dedicated callback
        # (not the _log chain) so the banner sits at the top, above the log.
        try:
            self.bot._maintenance_handler = self.dashboard.set_maintenance
        except Exception:
            pass
        layout.addWidget(self.dashboard)

        self.setWindowTitle(tr('Simple Crypto Trader'))
        try:
            self.setWindowIcon(QIcon(WINDOW_ICON))
        except Exception:
            pass
        self.resize(600, 500)

        # price feed via the exchange provider
        pid = self.bot.settings.get('product_id', '')
        if pid:
            self.bot.client.start_price_feed(pid, self._on_price_tick, self._on_conn_change)

        # dashboard refresh (also polls any pending limit order so an unfilled
        # limit never holds the bot hostage — fills/timeouts resolved here)
        self._refresh_timer = QTimer(self)

        def _refresh():
            try:
                self.bot.poll_open_orders()
            except Exception:
                pass
            try:
                # keep the persisted started/stopped flag in sync with reality
                want = self.bot.state.status != BotStatus.STOPPED
                if runtime_state.load_started() != want:
                    runtime_state.save_started(want)
            except Exception:
                pass
            self.dashboard.refresh()

        self._refresh_timer.timeout.connect(_refresh)
        self._refresh_timer.start(1000)

        # apply deferred settings once the bot leaves an active trade cycle
        self._pending_timer = QTimer(self)
        self._pending_timer.timeout.connect(self._check_apply_pending)
        self._pending_timer.start(2000)

        # startup holding-sync
        QTimer.singleShot(3000, self._startup_sync)

        # auto-update: check at launch + every 4h WHILE the app is open (never
        # a background process). Default = check + notify; install is opt-in.
        try:
            from .. import auto_update
            if self.bot.settings.get('auto_update_check', True):
                self._update_timer = QTimer(self)
                self._update_timer.timeout.connect(self._check_updates)
                self._update_timer.start(int(auto_update._CHECK_INTERVAL_S * 1000))
                QTimer.singleShot(8000, self._check_updates)
        except Exception:
            pass

    def _bot_log(self, msg):
        self.dashboard.log_message(msg)

    # ---- auto-update (runs only while this window is open) ----------------
    def _check_updates(self):
        try:
            from .. import auto_update
            if not (auto_update.UPDATES_OWNER and auto_update.UPDATES_REPO):
                return
            settings = self.bot.settings

            def work():
                try:
                    # NOTE: the check ALWAYS runs — a user who turned update
                    # checks OFF still receives SECURITY releases (they
                    # force-install; user 2026-08-16). Normal updates stay
                    # silent for that user.
                    tag, asset_url, digest, security = \
                        auto_update.check_for_update()
                    if not tag:
                        return
                    if not auto_update.is_newer(tag, auto_update.current_version()):
                        return
                    if not (security
                            or settings.get('auto_update_check', True)):
                        return
                    mandatory = auto_update.install_is_mandatory(
                        security, settings.get('auto_update_install', False))
                    QTimer.singleShot(
                        0, lambda: self._prompt_update(
                            tag, asset_url, digest, mandatory, security))
                except Exception:
                    pass

            import threading
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            pass

    def _prompt_update(self, tag, asset_url, digest, auto, security=False):
        """Heads-up / notify dialog. Never touches the trading engine — it runs
        on the GUI thread while the bot keeps trading on its own thread.
        SECURITY updates are MANDATORY: countdown always runs and 'Later' is
        not offered (an ignored patch = a vulnerable user; user 2026-08-16)."""
        from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                                     QVBoxLayout)
        from .. import auto_update
        dlg = QDialog(self)
        dlg.setWindowTitle(tr('Security Update' if security
                              else 'Update Available'))
        lay = QVBoxLayout(dlg)
        state = {'n': auto_update._HEADS_UP_S if (auto or security) else 0,
                 'go': False}
        label = QLabel()
        lay.addWidget(label)
        row = QHBoxLayout()
        install_btn = QPushButton(tr('Install now'))
        later_btn = QPushButton(tr('Required' if security
                                   else ('Later' if auto else 'Not now')))
        if security:
            later_btn.setEnabled(False)
        row.addWidget(install_btn)
        row.addWidget(later_btn)
        lay.addLayout(row)

        def _text():
            if security:
                return (f'SECURITY update v{tag} — a vulnerability is fixed '
                        f'in this release.\n'
                        f'Installing and restarting in {state["n"]}s '
                        f'(required — keeps you protected).\n'
                        f'(return-to-state resumes your bot and position.)')
            if auto:
                return (f'Update v{tag} available — installing and restarting '
                        f'in {state["n"]}s.\n'
                        f'(return-to-state resumes your bot where it was.)')
            return (f'Update v{tag} available.\n\n'
                    f'Install now (restarts the app and returns to state),\n'
                    f'or leave it — the check will re-offer it later.')

        label.setText(_text())
        countdown = QTimer(dlg)

        def _tick():
            if state['n'] > 0:
                state['n'] -= 1
                label.setText(_text())
            else:
                countdown.stop()
                dlg.accept()

        countdown.timeout.connect(_tick)

        def _install():
            state['go'] = True
            countdown.stop()
            install_btn.setEnabled(False)
            later_btn.setEnabled(False)
            label.setText(f'Installing v{tag}…')
            QTimer.singleShot(50, dlg.accept)

        install_btn.clicked.connect(_install)
        later_btn.clicked.connect(dlg.reject)
        if auto or security:
            countdown.start(1000)
        if dlg.exec_() != QDialog.Accepted or not state['go']:
            # Later / Not now -> the check re-offers next cycle. A SECURITY
            # release cannot be declined (button disabled + countdown
            # auto-accepts) — if it somehow still lands here, re-offer soon.
            if security:
                QTimer.singleShot(60_000, lambda: self._prompt_update(
                    tag, asset_url, digest, auto, security))
            return
        self._run_install(tag, asset_url, digest)

    def _run_install(self, tag, asset_url, digest=None):
        from .. import auto_update
        import threading

        def work():
            try:
                ok = auto_update.install_update(tag, asset_url,
                                                expected_sha256=digest)
            except Exception:
                ok = False
            QTimer.singleShot(0, lambda: self._finish_install(ok, tag))

        threading.Thread(target=work, daemon=True).start()

    def _finish_install(self, ok, tag):
        from PyQt5.QtWidgets import QApplication
        from .. import auto_update
        if not ok:
            self.bot._log(f'Update to v{tag} FAILED — keeping current version',
                          user=True)
            return
        self.bot._log(f'Update to v{tag} applied — restarting to return to state',
                      user=True)
        auto_update.relaunch()
        QTimer.singleShot(1500, QApplication.instance().quit)

    # ---- price feed callbacks -------------------------------------------
    def _on_price_tick(self, price):
        self.bot.on_price(price)

    def _on_conn_change(self, connected):
        self.bot.state.connection_status = 'Connected' if connected else 'Disconnected'
        self.dashboard.set_connection(connected)

    # ---- startup ----------------------------------------------------------
    def _prompt_holding_mode(self, pid, bal, own):
        """First-time holding found with no mode set — ask the user. SAFE
        default = Separate (never touch coins the bot didn't buy)."""
        from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                                     QRadioButton, QVBoxLayout)
        dlg = QDialog(self)
        dlg.setWindowTitle(tr('Holding Found'))
        lay = QVBoxLayout(dlg)
        sym = pid.split('-')[0]
        outside = max(0.0, bal - own)
        lay.addWidget(QLabel(
            f'This account holds {bal:.6f} {sym}: {own:.6f} bought by the bot, '
            f'{outside:.6f} NOT bought by the bot.\n\n'
            'How should the bot handle this pair?'))
        r_sep = QRadioButton('Keep separate (safe) — the bot only trades what IT '
                             'buys, never touches your other coins')
        r_com = QRadioButton('Combine — adopt and manage the WHOLE holding '
                             '(current behavior)')
        r_ai = QRadioButton('Auto-invest takeover — buy dips only (better cost '
                            'basis than spot-at-market), never auto-sells, exit '
                            'via Force Close')
        r_sep.setChecked(True)
        for r in (r_sep, r_com, r_ai):
            lay.addWidget(r)
        row = QHBoxLayout()
        okb = QPushButton(tr('OK'))
        skipb = QPushButton(tr('Skip (keep separate)'))
        okb.clicked.connect(dlg.accept)
        skipb.clicked.connect(dlg.reject)
        row.addWidget(okb)
        row.addWidget(skipb)
        lay.addLayout(row)
        if dlg.exec_() != QDialog.Accepted:
            return 'separate'  # SAFE default
        if r_com.isChecked():
            return 'combine'
        if r_ai.isChecked():
            return 'auto_invest'
        return 'separate'

    def _startup_sync(self, _retries=4):
        try:
            bot = self.bot
            pid = bot.settings.get('product_id', '')
            # If a position is ALREADY tracked in memory, do NOT rebuild/re-base
            # it here — pressing Start while holding must not change the entry
            # (user: "it synced to exchange with a different entry"). External
            # closes/additions are handled by the engine's periodic 60s check.
            if not pid or '-' not in pid or bot.state.position:
                return
            # a STOPPED bot stays stopped — it must never silently start
            # trading again after a reboot
            if not runtime_state.load_started():
                return
            # re-attach any resting (keep-open) limit order from before — adopt
            # its fill if it already happened
            try:
                bot.recover_pending_orders()
            except Exception:
                pass
            bal = bot.client.get_real_base_balance(pid)
            if bal is None:
                # BUG-007 fail-safe + "the show must go on": the holding CANNOT
                # be verified, so trading on an unknown position is held (never
                # buy on top of a real holding against a wrong basis). The bot
                # NEVER stops for this — it stays RUNNING and gated, keeps
                # auto-retrying the balance read in the background, and resumes
                # on its own the moment the API verifies.
                bot._position_unknown = True
                bot.state.status = BotStatus.WATCHING
                bot.state.error_message = ('Position unverified: balance read failed. '
                                           'Trades held until it verifies; auto-retrying.')
                if _retries > 0:
                    bot._log(f'SYNC: balance read failed, retrying ({_retries})')
                    QTimer.singleShot(5000, lambda: self._startup_sync(_retries - 1))
                else:
                    bot._log('SYNC: position still unverified — trades held, '
                             'auto-retrying every 30s (no user action needed)')
                    QTimer.singleShot(30000, lambda: self._startup_sync(4))
                return
            # Read succeeded: position is now known (0.0 = verifiably flat).
            bot._position_unknown = False
            self._startup_bal = bal
            if not bal or bal <= 0:
                # Verifiably flat — reconcile own_base (user closed everything
                # on the phone; own_base must never keep showing a holding).
                own = runtime_state.load_own_base()
                if own > 1e-12:
                    runtime_state.save_own_base(0.0)
                    bot._log('SYNC: exchange is flat — own_base cleared', user=True)
            elif bal > 0:
                bal_trunc = bot._inc_trunc(bal, pid)
                if bal_trunc > 0:
                    inc = bot.client.get_base_increment(pid)
                    dp = max(1, -int(__import__('math').log10(inc)))
                    own = runtime_state.load_own_base()
                    outside = max(0.0, bal_trunc - own)
                    mode = str(bot.settings.get('holding_mode') or '')
                    if not mode and (outside > 1e-12 or own <= 0):
                        mode = self._prompt_holding_mode(pid, bal_trunc, own)
                        if mode:
                            try:
                                from ..config import save_settings
                                s = load_settings()
                                s['holding_mode'] = mode
                                save_settings(s)
                                bot.settings['holding_mode'] = mode
                            except Exception:
                                pass
                    if not mode:
                        mode = 'separate'   # SAFE default if the prompt was skipped
                    bot._holding_mode = mode
                    try:
                        self.dashboard.set_close_pct_visible(mode == 'auto_invest')
                        self.dashboard.set_close_enabled(True)
                    except Exception:
                        pass
                    if mode in ('separate', 'auto_invest'):
                        # never trade outside-held coins — adopt only the bot's
                        # own base at its own (journal) cost basis.
                        # SAFETY (user 2026-08-16): the journal is NEVER trusted
                        # above the exchange's real balance. A stale/duplicated
                        # journal row (old test junk, a missed close) must not
                        # conjure coins the account does not hold — own is
                        # capped at bal_trunc (the real balance).
                        if own > 1e-12:
                            own_trunc = bot._inc_trunc(own, pid)
                            if own_trunc > bal_trunc + 1e-12:
                                bot._log(f'SYNC: journal says {own_trunc:.{dp}f} but exchange holds '
                                         f'{bal_trunc:.{dp}f} — using the REAL balance '
                                         f'(stale journal row corrected)', user=True)
                                own_trunc = bot._inc_trunc(bal_trunc, pid)
                            own_entry = None
                            try:
                                basis = bot.client.get_cost_basis(pid, own_trunc)
                                if basis:
                                    own_entry = basis[0]
                            except Exception:
                                pass
                            if own_trunc > 0:
                                bot.state.position = Position(
                                    product_id=pid, entry_price=own_entry or 0.0,
                                    size_usdc=(own_entry or 0.0) * own_trunc,
                                    size_base=own_trunc, entry_time='', order_id='',
                                    highest_price=0.0, stop_price=0.0)
                                bot.state.status = BotStatus.HOLDING
                                if own_entry and own_entry > 0:
                                    bot._log(f'SYNC: own {own_trunc:.{dp}f} {pid.split("-")[0]} resumed at bot basis ${own_entry:.2f} — {outside:.{dp}f} outside coins kept separate', user=True)
                                else:
                                    bot._log(f'SYNC: own {own_trunc:.{dp}f} {pid.split("-")[0]} resumed — awaiting first tick; {outside:.{dp}f} outside coins kept separate', user=True)
                        if outside > 1e-12:
                            bot._log(f'HOLDING MODE ({mode}): {outside:.{dp}f} coins were NOT bought by the bot — kept separate, never traded. Choose Combine or Auto-invest in Settings to manage them.', user=True)
                    else:
                        # combine — adopt the WHOLE holding at API cost basis (today's behavior)
                        pos = Position(product_id=pid, entry_price=0.0, size_usdc=0.0,
                                       size_base=bal_trunc, entry_time='', order_id='',
                                       highest_price=0.0, stop_price=0.0)
                        basis_ok = False
                        try:
                            basis = bot.client.get_cost_basis(pid, bal_trunc)
                            if basis:
                                e, b = basis
                                if e > 0 and b > 0 and abs(b - bal_trunc) / bal_trunc < 0.15:
                                    pos.entry_price = e
                                    pos.size_usdc = e * bal_trunc
                                    basis_ok = True
                        except Exception:
                            pass
                        bot.state.position = pos
                        bot.state.status = BotStatus.HOLDING
                        if basis_ok:
                            bot._log(f'SYNC: found {bal_trunc:.{dp}f} {pid.split("-")[0]} — resumed at API cost basis ${pos.entry_price:.2f} (fees included)', user=True)
                        else:
                            bot._log(f'SYNC: found {bal_trunc:.{dp}f} {pid.split("-")[0]} — awaiting first tick for entry price', user=True)
                        if outside > 1e-12:
                            bot._log(f'HOLDING MODE (combine): adopting ALL {bal_trunc:.{dp}f} — including {outside:.{dp}f} coins the bot did NOT buy.', user=True)
        except Exception:
            pass
        # --- startup health check (single consolidated status line) ---
        try:
            parts = []
            own = runtime_state.load_own_base()
            if own > 1e-12:
                parts.append(f'own_base: {own:.8f}')
            bal_val = getattr(self, '_startup_bal', None)
            if bal_val is not None:
                parts.append(f'balance: {bal_val:.8f}')
            else:
                parts.append('balance: unreadable')
            parts.append('exchange: connected')
            bot._log(f'STARTUP CHECK: {" | ".join(parts)}', user=True)
        except Exception:
            pass

    # ---- buttons ----------------------------------------------------------
    def _on_start(self):
        bot = self.bot
        # REMOTE-SESSION GATE (user, 2026-08-10): never trade from a remote
        # desktop session — the bot opens (so the account is inspectable) but
        # refuses to start trading until the session is local again. Cross-
        # platform detection in sbt.remote; SBT_ALLOW_REMOTE=1 opts out.
        try:
            from .. import remote
            if remote.detect_remote_session():
                bot._log(remote.remote_reason(), user=True)
                self.dashboard.log_message('Remote session active — trading blocked. '
                                           'Return to a local session to trade.')
                runtime_state.save_started(False)
                return
        except Exception:
            pass
        runtime_state.save_started(True)
        # Verify the held position BEFORE allowing any trading (BUG-007):
        # if the balance can't be read, the bot must not start — buying on top
        # of an unverified holding against a wrong basis is a real-money risk.
        self._startup_sync()
        if bot._position_unknown:
            bot._log('START: position unverified — running watch, trades held until balance verifies', user=True)
            bot.state.status = BotStatus.WATCHING
            bot.state.error_message = ('Position unverified: balance read failed. '
                                       'Trades held until it verifies; auto-retrying.')
            self.dashboard.log_message('Position unverified — holding trades while balance retries.')
            return
        # Start even with low funds: buying pauses until funded, but an open
        # position's exit (trailing stop) is still managed.
        try:
            bal = bot.client.get_quote_balance(bot.settings.get('product_id', ''))
        except Exception:
            bal = 0.0
        pid = bot.settings.get('product_id', '')
        min_q = bot._min_quote(pid)
        if bal < min_q:
            bot._buy_paused = True
            bot._buy_pause_check_ts = time.time()
            bot._log(f'START: balance ${bal:.2f} below ${min_q:.2f} min — buying paused until funds added', user=True)
        else:
            bot._buy_paused = False
        # resume any holding on the current product at its DB cost basis
        # (works on Start too, not just at the 3s startup timer)
        bot._log('Bot started', user=True)
        if bot.settings.get('dca', False):
            try:
                dca_bal = bot.client.get_quote_balance(bot.settings.get('product_id', ''))
                if dca_bal < min_q:
                    bot._log(f'DCA: enabled but balance ${dca_bal:.2f} is low — may pause', user=True)
            except Exception:
                pass
        bot.start()
        # Exchange maintenance monitor: polls fetch_status every 30 min,
        # shows / clears a banner at the top of the log (default ON).
        try:
            from .. import maintenance
            if self._maintenance_monitor:
                self._maintenance_monitor.stop()
            self._maintenance_monitor = maintenance.MaintenanceMonitor(bot)
            self._maintenance_monitor.start()
        except Exception:
            pass
        bs = bot.state
        if bs.status == BotStatus.HOLDING and bs.position is None:
            bs.status = BotStatus.WATCHING
            bs.error_message = 'Cleared phantom position (no actual order found)'
            self.dashboard.log_message('Cleared phantom position on startup')

    def _on_stop(self):
        if self._maintenance_monitor:
            self._maintenance_monitor.stop()
            self._maintenance_monitor = None
        self.bot.stop()
        runtime_state.save_started(False)
        self.bot._log('Bot stopped', user=True)

    def _on_close_position(self):
        bot = self.bot
        mode = str(bot.settings.get('holding_mode') or '')
        pct = 1.0
        if mode == 'auto_invest':
            try:
                pct = self.dashboard.close_pct.value() / 100.0
            except Exception:
                pct = 1.0
            bot.force_close(pct)
            return
        # Manual close always available — user decides
        bot.force_close(pct)

    def _on_settings(self):
        bot = self.bot
        # Show the user's SAVED preferences (settings.json) — NOT the engine's
        # in-memory dict, which can hold stale/transient values (e.g. a
        # temporary fixed-mode size) and misrepresent what is actually set.
        try:
            current = load_settings()
        except Exception:
            current = dict(bot.settings)
        dlg = SettingsDialog(dict(current), self._apply_settings, self, client=bot.client)
        dlg.exec_()

    def _apply_settings(self, data):
        bot = self.bot
        # API switch: user picked a different SAVED API in Settings. It is
        # handled immediately, NOT deferred — mid-trade it opens the chosen
        # API as a NEW bot alongside (this trade stays untouched); flat it
        # relaunches this bot session as that API.
        switch = data.pop('_api_switch', None)
        # defer only while an actual trade cycle is running (holding/buying/selling
        # WITH a position); otherwise apply immediately
        s = bot.state.status
        in_trade = (s in (BotStatus.HOLDING, BotStatus.BUYING, BotStatus.SELLING)
                    and bot.state.position is not None)
        if in_trade:
            bot._pending_settings_data = data
            bot._pending_settings_cb = self._apply_settings_now
            bot._log('Settings deferred until trade cycle completes', user=True)
        else:
            self._apply_settings_now(data)
        if switch:
            self._apply_api_switch(switch, in_trade)

    def _apply_api_switch(self, profile_dir, in_trade):
        from PyQt5.QtWidgets import QApplication
        from ..api_registry import launch_profile
        label = os.path.basename(os.path.normpath(profile_dir))
        launched = launch_profile(profile_dir)
        if not launched:
            self.bot._log(f'API switch: FAILED to launch {label}', user=True)
            return
        if in_trade:
            self.bot._log(f'API switch: opened {label} as a NEW bot alongside '
                          '(mid-trade — this bot keeps the open position)', user=True)
        else:
            self.bot._log(f'API switch: relaunching this bot as {label}', user=True)
            QTimer.singleShot(1500, QApplication.instance().quit)

    def _apply_settings_now(self, data):
        old_pid = self.bot.settings.get('product_id', '')
        self.bot.update_settings(data)
        # auto-invest (buy-only) mode shows the Force Close % box; others don't
        try:
            self.dashboard.set_close_pct_visible(
                str(self.bot.settings.get('holding_mode') or '') == 'auto_invest')
        except Exception:
            pass
        # Force Close always available — user decides, not DCA
        try:
            self.dashboard.set_close_enabled(True)
        except Exception:
            pass
        # if the trading pair changed, resubscribe the price feed and reset the
        # watch state so the new pair starts fresh (no stale low / spurious buy)
        new_pid = self.bot.settings.get('product_id', '')
        if new_pid and new_pid != old_pid:
            bot = self.bot
            bot.dip_detector.reset()
            bot._trigger_lowest = None
            bot._dca_bounce_low = None
            try:
                bot.client.start_price_feed(new_pid, self._on_price_tick, self._on_conn_change)
            except Exception:
                pass
        self.bot._log('Settings updated.', user=True)

    def _check_apply_pending(self):
        bot = self.bot
        if bot._pending_settings_data is None or bot._pending_settings_cb is None:
            return
        # apply only once the trade has closed successfully — never mid-trade,
        # never on a failed/fatal state (those keep _trade_closed_ok False)
        if not bot._trade_closed_ok or bot.state.position is not None:
            return
        cb = bot._pending_settings_cb
        data = bot._pending_settings_data
        bot._pending_settings_data = None
        bot._pending_settings_cb = None
        bot._trade_closed_ok = False
        try:
            cb(data)
        except Exception:
            pass
        bot._log('Deferred settings applied after trade closed successfully', user=True)

    def closeEvent(self, event):
        try:
            self.bot.client.stop_price_feed()
        except Exception:
            pass
        try:
            runtime_state.save_started(self.bot.state.status != BotStatus.STOPPED)
        except Exception:
            pass
        event.accept()
