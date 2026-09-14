"""Dashboard widget — ported from the restored app's redesign (labels over
buttons, Entry/Stop/Peak/Size info row, WS status colors, fee-aware Stop)."""
import queue
from datetime import datetime

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QDoubleSpinBox, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QSizePolicy, QTextEdit, QVBoxLayout,
                             QWidget)

from ..i18n import tr
from ..models import BotStatus

_FIAT_SYMBOLS = {'USD': '$', 'USDC': '$', 'USDT': '$',
                 'EUR': '€', 'GBP': '£', 'JPY': '¥', 'CNY': '¥',
                 'KRW': '₩', 'AUD': 'A$', 'CAD': 'C$', 'CHF': 'CHF',
                 'BRL': 'R$', 'MXN': 'MX$'}

def _quote_label(quote):
    """Return the display label for a quote currency: '$' for USD, '€' for
    EUR, etc. For crypto quotes (XRP, BTC, ...) return the code itself."""
    return _FIAT_SYMBOLS.get(quote.upper(), quote.upper())


class DashboardWidget(QFrame):
    """Qt widgets are NOT thread-safe: the engine + price feed run on a
    background thread, so every widget mutation (log append, connection status)
    must be marshaled to the GUI thread. Callers post to `_ui_q` from any
    thread; a GUI-thread QTimer drains it and does the actual widget work."""

    def __init__(self, bot, on_start, on_stop, on_close, on_settings, parent=None):
        super().__init__(parent)
        self.bot = bot
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_close = on_close
        self._on_settings = on_settings
        self._cached_balance = None
        self._cached_balance_time = 0.0
        self._label_font = QFont()
        self._ui_q = queue.Queue()
        self._drain_timer = QTimer(self)
        self._drain_timer.timeout.connect(self._drain_ui_q)
        self._drain_timer.start(50)
        self._setup_ui()

    def _make_info_pair(self, label_text):
        outer = QVBoxLayout()
        outer.setSpacing(0)
        outer.setAlignment(Qt.AlignCenter)
        lbl = QLabel(label_text)
        lbl.setFont(self._label_font)
        lbl.setStyleSheet("color: #888; background: transparent;")
        lbl.setAlignment(Qt.AlignCenter)
        val = QLabel("--")
        val.setFont(self._label_font)
        val.setStyleSheet("color: #4488ff; background: transparent;")
        val.setAlignment(Qt.AlignCenter)
        outer.addWidget(lbl)
        outer.addWidget(val)
        return outer, val

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # header: Status | Price | P&L ......... Websocket
        header = QHBoxLayout()
        self.status_label = QLabel(tr("STOPPED"))
        self.status_label.setObjectName('statusLabel')
        self.priceLabel = QLabel(tr("Price: --"))
        self.priceLabel.setObjectName('priceLabel')
        self.pnlLabel = QLabel(tr("P&L: --"))
        self.pnlLabel.setObjectName('pnlLabel')
        self.conn_label = QLabel(tr("Disconnected"))
        self.conn_label.setObjectName('connLabel')
        header.addWidget(self.status_label)
        header.addSpacing(12)
        header.addWidget(self.priceLabel)
        header.addSpacing(12)
        header.addWidget(self.pnlLabel)
        header.addStretch(1)
        header.addWidget(self.conn_label)
        layout.addLayout(header)

        info_row = QHBoxLayout()
        info_row.setSpacing(8)
        self.entry_label, self.entry_val = self._make_info_pair("Entry")
        self.stop_label, self.stop_val = self._make_info_pair("Stop")
        self.peak_label, self.peak_val = self._make_info_pair("Peak")
        self.size_label, self.size_val = self._make_info_pair("Size")
        info_row.addLayout(self.entry_label)
        info_row.addLayout(self.stop_label)
        info_row.addLayout(self.peak_label)
        info_row.addLayout(self.size_label)
        layout.addLayout(info_row)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton(tr("Start"))
        self.stop_btn = QPushButton(tr("Stop"))
        self.close_btn = QPushButton(tr("Force Close"))
        # % of holdings to close — ONLY visible in auto-invest mode (buy-only).
        self.close_pct = QDoubleSpinBox()
        self.close_pct.setRange(1.0, 100.0)
        self.close_pct.setValue(100.0)
        self.close_pct.setDecimals(0)
        self.close_pct.setSuffix('%')
        self.close_pct.setToolTip(tr('Auto-invest mode: what % of your accumulated '
                                     'holdings to sell now (rest keeps accumulating).'))
        self.close_pct.hide()
        self.settings_btn = QPushButton(tr("Settings"))
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)
        self.close_btn.clicked.connect(self._on_close)
        self.settings_btn.clicked.connect(self._on_settings)
        for b in (self.start_btn, self.stop_btn, self.close_btn, self.settings_btn):
            btn_row.addWidget(b)
        btn_row.addWidget(self.close_pct)
        layout.addLayout(btn_row)

        # Account Balance bar — fixed theme-button look (styled via #balanceBar
        # in the QSS files so it matches the theme's buttons exactly), balance
        # always green. Never changes color with the trade (user, 2026-08-07).
        self._balance_lbl = QLabel(tr("Account Balance: --"))
        self._balance_lbl.setObjectName('balanceBar')
        self._balance_lbl.setFixedHeight(26)
        self._balance_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._balance_lbl)

        # Exchange maintenance banner — sits above the log, auto-shows/clears.
        # Orange background when active; hidden when exchange is operational.
        self._maintenance_banner = QLabel('')
        self._maintenance_banner.setObjectName('maintenanceBanner')
        self._maintenance_banner.setAlignment(Qt.AlignCenter)
        self._maintenance_banner.setWordWrap(True)
        self._maintenance_banner.hide()
        layout.addWidget(self._maintenance_banner)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName('logArea')
        self.log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.log, 1)

        self._pending_lbl = QLabel("")
        self._pending_lbl.setStyleSheet("color: #ffc107; padding: 2px;")
        self._pending_lbl.setAlignment(Qt.AlignCenter)
        self._pending_lbl.setFixedHeight(22)
        layout.addWidget(self._pending_lbl)

        self.reset_btn = QPushButton(tr("Reset"))
        self.reset_btn.setStyleSheet("background: #ff1744; color: #fff;")
        self.reset_btn.clicked.connect(self._do_reset)
        self.reset_btn.hide()
        layout.addWidget(self.reset_btn)

    def _do_reset(self):
        bot = self.bot
        bot.state.status = BotStatus.STOPPED
        bot.state.error_message = ''
        bot.state.position = None
        bot.state.local_low = 0.0
        bot.dip_detector.reset()
        bot.trailing_stop.reset()
        bot._trigger_lowest = None
        bot._dca_bounce_low = None
        bot._dca_paused = False
        bot._buy_paused = False
        bot._buy_pause_check_ts = 0.0
        bot._trade_closed_ok = False
        bot._in_on_price = False
        bot._buy_fail_count = 0
        self.log_message('Bot reset: state cleared, ready to start')

    def set_close_pct_visible(self, visible):
        """Auto-invest (buy-only) mode shows the 'Trim Position' spinbox
        with % edit; other modes show 'Close Position' (emergency eject)."""
        try:
            self.close_pct.setVisible(bool(visible))
            self.close_btn.setText(tr('Trim Position') if visible else tr('Close Position'))
            if visible:
                self.close_pct.setEnabled(True)
                self.close_btn.setEnabled(True)
        except Exception:
            pass

    def set_close_enabled(self, enabled):
        """Force Close / Trim % is for single trades and auto-invest —
        disabled in DCA single-trade mode (DCA handles itself and never
        force-closes at a loss). Auto-invest mode is ALWAYS enabled."""
        try:
            # auto-invest: always enabled (Trim % is its only exit)
            if hasattr(self, 'close_pct') and self.close_pct.isVisible():
                self.close_btn.setEnabled(True)
            else:
                self.close_btn.setEnabled(bool(enabled))
        except Exception:
            pass

    def log_message(self, msg):
        # Thread-safe: called from the engine thread; the actual QTextEdit
        # append happens on the GUI thread in _drain_ui_q.
        self._ui_q.put(('log', msg))

    def set_connection(self, connected):
        # Thread-safe: called from the feed thread; the widget update happens
        # on the GUI thread in _drain_ui_q.
        self._ui_q.put(('conn', tr('Connected') if connected else tr('Disconnected')))

    def clear_log(self):
        # Thread-safe: per-cycle log — a new buy cycle clears the in-app log so
        # the user sees only the current buy->hold->sell cycle. The full history
        # stays in the on-disk diagnostic log.
        self._ui_q.put(('clear', None))

    def set_maintenance(self, status_text):
        """Thread-safe: show / clear the exchange maintenance banner.
        Called by MaintenanceMonitor via bot._maintenance_handler."""
        self._ui_q.put(('maintenance', status_text))

    def _drain_ui_q(self):
        try:
            while True:
                kind, payload = self._ui_q.get_nowait()
                if kind == 'log':
                    self._append_log(payload)
                elif kind == 'conn':
                    self.conn_label.setText(payload)
                elif kind == 'clear':
                    self.log.clear()
                elif kind == 'maintenance':
                    self._update_maintenance_banner(payload)
        except queue.Empty:
            pass

    def _update_maintenance_banner(self, status_text):
        """Show the maintenance banner (orange) or hide it (status clear)."""
        if status_text:
            self._maintenance_banner.setText(f'⚠ EXCHANGE MAINTENANCE: {status_text}')
            self._maintenance_banner.setStyleSheet(
                'background-color: #e65100; color: white; font-weight: bold; '
                'padding: 6px; border-radius: 3px;')
            self._maintenance_banner.show()
        else:
            self._maintenance_banner.hide()
            self._maintenance_banner.setText('')

    def _append_log(self, msg):
        try:
            self.log.append(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
            sb = self.log.verticalScrollBar()
            sb.setValue(sb.maximum())
        except Exception:
            pass

    def refresh(self):
        bot = self.bot
        try:
            s = bot.state
        except Exception:
            s = None
        try:
            pos = s.position if s else None
        except Exception:
            pos = None
        status = s.status if s else None
        self.status_label.setText(tr(status.value) if status else '')
        if s and s.error_message:
            self.status_label.setToolTip(s.error_message)

        try:
            qc = 'USD'
            pid = bot.settings.get('product_id', '')
            if '-' in pid:
                qc = pid.split('-')[1].upper()
        except Exception:
            qc = 'USD'

        # live price
        try:
            cur = s.current_price if s else 0.0
            self.priceLabel.setText(tr("Price: {p}").format(p=f"{cur:.6f} {qc}") if cur else tr("Price: --"))
        except Exception:
            self.priceLabel.setText(tr("Price: --"))

        # P&L — green when positive, red when negative, yellow when not holding
        try:
            if pos and pos.entry_price:
                pnl = pos.pnl or 0.0
                pct = (pos.pnl_pct or 0.0) * 100.0
                self.pnlLabel.setText(tr("P&L: {p}").format(p=f"${pnl:+.2f} ({pct:+.2f}%)"))
                if pnl > 0:
                    self.pnlLabel.setStyleSheet("color: #00c853;")
                elif pnl < 0:
                    self.pnlLabel.setStyleSheet("color: #ff1744;")
                else:
                    self.pnlLabel.setStyleSheet("color: #ffc107;")
            else:
                total = (s.total_pnl or 0.0) if s else 0.0
                self.pnlLabel.setText(tr("Total P&L: {p}").format(p=f"${total:+.2f}"))
                self.pnlLabel.setStyleSheet("color: #ffc107;")
        except Exception:
            self.pnlLabel.setText(tr("P&L: --"))

        if pos:
            self.entry_label.itemAt(0).widget().setText(tr("Entry"))
            self.entry_val.setStyleSheet("color: #4488ff;")
            self.entry_val.setText(f"${pos.entry_price}" if pos.entry_price else "--")
            asset = pos.product_id.split('-')[0] if pos.product_id else '?'
            try:
                sb = bot._inc_trunc(pos.size_base, pos.product_id) if pos.size_base else pos.size_base
                inc = bot.client.get_base_increment(pos.product_id)
                dp = max(1, -int(__import__('math').log10(inc)))
                self.size_val.setText(f"{sb:.{dp}f} {asset}" if pos.size_base else "--")
            except Exception:
                self.size_val.setText("--")
            # DUAL Stop/Peak (user, 2026-08-10):
            #   RED  (price below the no-loss floor): the bot is finding a bottom
            #        to buy — Stop = the dip-low the % bounce triggers off; Peak =
            #        the spot price that WILL trigger the next buy (low x 1+dip).
            #   GREEN (price at/above the floor): Stop = the price that actually
            #        closes the trade; Peak = the trailing-stop peak.
            # Single trades (no dip-buy low tracked) keep their current values but
            # still get the red/green color on both fields.
            # TAKE-PROFIT MARK (user, 2026-09-11): red = below the mark (the bot
            # HOLDS here, no sell — DCA re-buys below entry); green = at/above
            # the mark (the trailing stop is armed and can close in profit).
            # The display uses the SAME mark the engine enforces, so "green"
            # only ever appears when a close is actually possible.
            try:
                floor = bot._take_profit_mark(pos.entry_price) if pos.entry_price else 0.0
            except Exception:
                tp = float(bot.settings.get('take_profit_pct', 0.05) or 0.0)
                floor = (pos.entry_price * (1 + tp)) if pos.entry_price else 0.0
            cur = (s.current_price or 0.0) if s else 0.0
            in_red = bool(pos.entry_price and cur and cur < floor)
            dip_low = getattr(bot, '_dca_bounce_low', None)
            if in_red and dip_low:
                try:
                    dip = bot.effective_dip()
                except Exception:
                    dip = 0.01
                self.stop_val.setText(f"${dip_low:.6f}")
                self.peak_val.setText(f"${dip_low * (1 + dip):.6f}")
                self.stop_val.setStyleSheet("color: #ff1744;")
                self.peak_val.setStyleSheet("color: #ff1744;")
            else:
                self.stop_val.setText(f"${pos.stop_price}" if pos.stop_price else "--")
                self.peak_val.setText(f"${pos.highest_price}" if pos.highest_price else "--")
                style = "#ff1744" if in_red else "#00c853"
                self.stop_val.setStyleSheet(f"color: {style};")
                self.peak_val.setStyleSheet(f"color: {style};")
        elif s and s.status == BotStatus.WATCHING and bot._trigger_lowest is not None:
            dip = bot.effective_dip()
            trig = bot._trigger_lowest * (1 + dip)
            self.entry_val.setText(f"${trig:.6f}")
            self.entry_val.setStyleSheet("color: #ffc107;")
            self.entry_label.itemAt(0).widget().setText(tr("Trigger"))
            for v in (self.stop_val, self.peak_val, self.size_val):
                v.setText("--")
                v.setStyleSheet("color: #4488ff;")
        else:
            for v in (self.entry_val, self.stop_val, self.peak_val, self.size_val):
                v.setText("--")
                v.setStyleSheet("color: #4488ff;")
            self.entry_label.itemAt(0).widget().setText("Entry")

        if bot._pending_settings_data is not None:
            self._pending_lbl.setText("Settings pending (applies after trade)")
        else:
            self._pending_lbl.setText("")

        try:
            import time
            now = time.time()
            if self._cached_balance is None or now - self._cached_balance_time > 30:
                new_bal = bot.client.get_quote_balance(bot.settings.get('product_id', ''))
                if new_bal is not None and new_bal >= 0:
                    self._cached_balance = new_bal
                    self._cached_balance_time = now
            if self._cached_balance is not None:
                sym = _quote_label(qc)
                # Fiat: $28.22 USD (symbol prefix). Crypto: 123.45 XRP (code only).
                bal_str = f"{sym}{self._cached_balance:.2f}" if len(sym) <= 2 else f"{self._cached_balance:.2f}"
                self._balance_lbl.setText(f"Account Balance: {bal_str} {qc}")
        except Exception:
            pass

        # balance bar never changes color with the trade — fixed themed style
        # (set once in _setup_ui); nothing overrides it here.

        if s is not None:
            self.reset_btn.setVisible(s.status == BotStatus.ERROR)

        try:
            ws = (s.connection_status or '').lower()
            if 'disconnect' in ws:
                self.conn_label.setStyleSheet("color: #ff1744;")
            elif 'connect' in ws:
                self.conn_label.setStyleSheet("color: #00c853;")
            else:
                self.conn_label.setStyleSheet("color: #ffc107;")
        except Exception:
            pass
