"""Settings dialog — ported from the restored app (Product ID ticker + editable
quote box, trade-size mode row, DCA, deferred save while running)."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QCompleter, QDialog,
                             QDoubleSpinBox, QFormLayout, QHBoxLayout, QInputDialog,
                             QLabel, QLineEdit, QMessageBox, QPushButton, QSizePolicy,
                             QVBoxLayout,
                             QWidget)

from .. import api_registry
from .. import paths
from ..config import save_settings
from ..i18n import tr
from ..theme import apply_theme

# Coinbase USDC deposit address on Base network (ship-time constant).
# Empty = donate button hidden (dev builds). Set at ship time.
DONATE_ADDRESS = '0x17889acEba7F4592f7B956288946feE6Cd719723'

_FIAT_SYMBOLS = {'USD': '$', 'USDC': '$', 'USDT': '$',
                 'EUR': '€', 'GBP': '£', 'JPY': '¥', 'CNY': '¥',
                 'KRW': '₩', 'AUD': 'A$', 'CAD': 'C$', 'CHF': 'CHF',
                 'BRL': 'R$', 'MXN': 'MX$'}

def _quote_label(quote):
    """Return the display label for a quote currency: '$' for USD, '€' for
    EUR, etc. For crypto quotes (XRP, BTC, ...) return the code itself."""
    return _FIAT_SYMBOLS.get(quote.upper(), quote.upper())


class SettingsDialog(QDialog):
    def __init__(self, settings, on_save, parent=None, client=None):
        super().__init__(parent)
        self.settings = settings
        self.on_save = on_save
        self.client = client
        self.fields = {}
        self.setWindowTitle(tr('Bot Trader - Settings'))
        self.setFixedSize(400, 640)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        # --- Exchange: dropdown of the API keys SAVED on this machine.
        # The running bot's own API is shown first; other saved APIs (not
        # running) can be switched to — flat = relaunch this session as that
        # API, mid-trade = open it as a NEW bot alongside (this trade stays
        # untouched). "Add new bot…" opens another bot whose setup collects a
        # brand-new API (I have account / Don't have account + the
        # NEVER-use-Transfer reminder + file/clipboard capture).
        self._current_exchange = str(settings.get('exchange', 'coinbase') or 'coinbase').strip().lower()
        self._api_switch_target = None
        self._api_switch_label = ''
        self._api_combo = QComboBox()
        cur_api = None
        try:
            cur_api = api_registry.read_api(paths.CONFIG_ROOT)
        except Exception:
            cur_api = None
        if cur_api and cur_api.get('cred'):
            self._api_combo.addItem(f"{cur_api['exchange']} (this bot)")
        else:
            self._api_combo.addItem(f'{self._current_exchange} (this bot)')
        self._saved_apis = []
        try:
            self._saved_apis = api_registry.list_saved_apis(exclude_running=True)
        except Exception:
            self._saved_apis = []
        for ap in self._saved_apis:
            self._api_combo.addItem(ap['label'])
        self._api_combo.addItem('Add new bot…')
        self._api_combo.setToolTip(
            'This bot\'s API is shown first. Pick another saved API to switch:\n'
            '- flat (no open trade): this bot relaunches as that API\n'
            '- mid-trade: it opens as a NEW bot alongside, this trade is untouched\n'
            '"Add new bot…" opens another bot\'s setup to collect a new API.')
        form.addRow(tr('Exchange:'), self._api_combo)

        def _on_api_index(idx):
            if idx == 0:
                self._api_switch_target = None
                self._api_switch_label = ''
            elif idx == 1 + len(self._saved_apis):
                # 'Add new bot…' — act IMMEDIATELY on selection (user
                # 2026-08-14: clicking it and nothing happening felt broken;
                # the old flow only fired at Save). Revert the combo so a
                # cancelled add doesn't leave the dialog in a broken state.
                self._api_combo.blockSignals(True)
                self._api_combo.setCurrentIndex(0)
                self._api_combo.blockSignals(False)
                self._api_switch_target = None
                self._api_switch_label = ''
                self._on_add_new()
                return
            else:
                self._api_switch_target = self._saved_apis[idx - 1]['profile']
                self._api_switch_label = self._saved_apis[idx - 1]['label']
        self._api_combo.currentIndexChanged.connect(_on_api_index)

        # --- Trading pair: editable type-ahead dropdown of the API's real
        # supported pairs (alphabetical). Typing narrows the list. No -USD
        # hardcode: the quote is whatever the exchange actually trades
        # (USD, USDC, JPY, ...) — from the live API, not from a guess.
        cur = str(settings.get('product_id', '') or '').strip().upper()
        # quote-currencies preference filters the list to pairs the user's
        # account can actually fund/trade (e.g. "USD,USDC"); empty = all.
        qc_filter = [q.strip().upper() for q in
                     str(settings.get('quote_currencies', '') or '').split(',')
                     if q.strip()]
        products = []
        if self.client is not None:
            try:
                products = self.client.list_products() or []
            except Exception:
                products = []
        if qc_filter:
            products = [p for p in products
                        if '-' in p and p.rsplit('-', 1)[1].upper() in qc_filter]
        if not products:
            products = [cur] if cur else []
        self._product_combo = QComboBox()
        self._product_combo.setEditable(True)
        self._product_combo.setInsertPolicy(QComboBox.NoInsert)
        self._product_combo.addItems(sorted(products))
        self._product_combo.setCurrentText(cur)
        completer = QCompleter(sorted(products), self._product_combo)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self._product_combo.setCompleter(completer)
        self._product_combo.setMinimumWidth(150)
        self._product_combo.setToolTip(
            'Type to filter the exchange\'s real trading pairs (alphabetical).\n'
            'Quote is whatever the API supports — USD, USDC, JPY, ...')
        form.addRow(tr('Trading Pair:'), self._product_combo)

        # API minimum order size for the current pair (getvalue minimum — the
        # floor the exchange actually enforces). Refreshed when the pair is
        # committed (Enter / focus-out / popup pick), NOT per keystroke, so
        # typing a pair never fires an API call per letter. Falls back to 2.0
        # only if the API can't be reached.
        self._pair_min = self._lookup_pair_min(cur)

        def _on_pair_changed():
            pid = self._product_combo.currentText().strip().upper()
            self._pair_min = self._lookup_pair_min(pid)
            if 'trade_size_fixed' in self.fields:
                self.fields['trade_size_fixed'].setMinimum(self._pair_min)
                if self.fields['trade_size_fixed'].value() < self._pair_min:
                    self.fields['trade_size_fixed'].setValue(self._pair_min)
            # refresh "Fixed X Amount" label + prefix for the new quote currency
            qc = pid.rsplit('-', 1)[1] if '-' in pid else 'USD'
            ql = _quote_label(qc)
            mode.blockSignals(True)
            mode.setItemText(1, tr(f'Fixed {_quote_label(qc)} Amount'))
            mode.blockSignals(False)
            fixed_sb.setPrefix(f'{ql} ')

        self._product_combo.lineEdit().editingFinished.connect(_on_pair_changed)
        self._product_combo.activated.connect(_on_pair_changed)
        # --- Quote currencies the account can trade (comma-separated, empty=all)
        self._quote_cur = QLineEdit(str(settings.get('quote_currencies', '') or ''))
        self._quote_cur.setPlaceholderText('e.g. USD,USDC  (empty = all)')
        self._quote_cur.setToolTip(
            'Quote currencies your account can actually trade.\n'
            'Filters the Trading Pair dropdown to these — e.g. USD,USDC.\n'
            'The app cannot detect your account\'s region; set this to the\n'
            'currencies you can fund (you see all 13 quote currencies otherwise).')
        form.addRow(tr('Quote Currencies:'), self._quote_cur)

        # --- Trade size: mode + value row
        # "Fixed X Amount" follows the pair's quote currency (e.g. "Fixed $ Amount"
        # for ZEC-USD, "Fixed XRP Amount" for SOL-XRP, "Fixed € Amount" for BTC-EUR).
        _qc = cur.rsplit('-', 1)[1] if '-' in cur else 'USD'
        _ql = _quote_label(_qc)
        mode = QComboBox()
        mode.addItems([tr('% of Balance'), tr(f'Fixed {_ql} Amount'),
                       tr('Number of Coin/Token')])
        cur_mode = settings.get('trade_size_mode', 'pct')
        mode.setCurrentIndex(0 if cur_mode == 'pct' else (1 if cur_mode == 'fixed' else 2))
        self.fields['trade_size_mode'] = mode

        pct_sb = QDoubleSpinBox()
        pct_sb.setRange(1.0, 100.0)
        pct_sb.setSingleStep(1.0)
        pct_sb.setDecimals(0)
        pct_sb.setSuffix('%')
        pct_sb.setValue(float(settings.get('trade_size_pct', 0.5)) * 100.0)
        self.fields['trade_size_pct'] = pct_sb

        fixed_sb = QDoubleSpinBox()
        fixed_sb.setMinimum(self._pair_min)
        fixed_sb.setMaximum(100000.0)
        fixed_sb.setSingleStep(5.0)
        fixed_sb.setDecimals(2)
        fixed_sb.setPrefix(f'{_ql} ')
        fixed_sb.setValue(max(float(settings.get('trade_size_fixed', 10.0)), self._pair_min))
        self.fields['trade_size_fixed'] = fixed_sb

        coin_sb = QDoubleSpinBox()
        coin_sb.setRange(0.001, 100000.0)
        coin_sb.setSingleStep(0.1)
        coin_sb.setDecimals(6)
        coin_sb.setValue(float(settings.get('trade_size_coin', 1.0)))
        self.fields['trade_size_coin'] = coin_sb

        container = QWidget()
        hl = QHBoxLayout(container)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(mode)
        hl.addWidget(pct_sb)
        hl.addWidget(fixed_sb)
        hl.addWidget(coin_sb)
        form.addRow(tr('Trade Size:'), container)

        def toggle(idx):
            pct_sb.setVisible(idx == 0)
            fixed_sb.setVisible(idx == 1)
            coin_sb.setVisible(idx == 2)
        mode.currentIndexChanged.connect(toggle)
        toggle(mode.currentIndex())

        # --- Dip bounce / trailing stop — shown/entered as PERCENT (0.0%–100%
        # structure, user 2026-08-07), stored as fractions (÷100 on save, ×100
        # on load). Engine reads the fraction — unchanged.
        dip_sb = QDoubleSpinBox()
        dip_sb.setRange(0.1, 50.0)
        dip_sb.setSingleStep(0.1)
        dip_sb.setDecimals(1)
        dip_sb.setSuffix('%')
        dip_sb.setValue(float(settings.get('dip_bounce_pct', 0.01)) * 100.0)
        dip_sb.setToolTip(tr('When the price dips below its recent low, the bot '
                             'waits for a bounce back UP by this much before '
                             'buying — so it buys the pullback, not a falling '
                             'knife. Higher = it needs a bigger bounce before it '
                             'buys.'))
        self.fields['dip_bounce_pct'] = dip_sb
        form.addRow(tr('Dip Bounce %:'), dip_sb)

        trail_sb = QDoubleSpinBox()
        trail_sb.setRange(0.1, 50.0)
        trail_sb.setSingleStep(0.1)
        trail_sb.setDecimals(1)
        trail_sb.setSuffix('%')
        trail_sb.setValue(float(settings.get('trailing_stop_pct', 0.01)) * 100.0)
        trail_sb.setToolTip(tr('While holding, the bot tracks the highest price '
                               'it reached. If the price drops this % from that '
                               'peak, it sells to lock in profit. Higher = it '
                               'lets profit give back more before selling (a '
                               'wider safety band).'))
        self.fields['trailing_stop_pct'] = trail_sb
        form.addRow(tr('Trailing Stop %:'), trail_sb)

        # --- Take Profit: the mark (% above entry) that ARMS the trailing stop.
        # Below the mark the bot HOLDS (never sells, no trail); at/above it the
        # trailing stop arms at that peak and follows upward (take_profit then
        # trailing does the closing). Auto-invest stays buy-only (no auto-sell).
        tp_sb = QDoubleSpinBox()
        tp_sb.setRange(0.1, 200.0)
        tp_sb.setSingleStep(0.5)
        tp_sb.setDecimals(1)
        tp_sb.setSuffix('%')
        tp_sb.setValue(float(settings.get('take_profit_pct', 0.05)) * 100.0)
        tp_sb.setToolTip(tr('Target profit mark: price must reach this % ABOVE '
                            'entry before the bot will sell. Below the mark it '
                            'just holds (and DCA re-buys below entry). At/above '
                            'the mark the trailing stop arms at that peak and '
                            'follows it up — a pullback of the Trailing Stop % '
                            'closes the trade in profit.'))
        self.fields['take_profit_pct'] = tp_sb
        form.addRow(tr('Take Profit %:'), tp_sb)

        # --- Capitulation: single-trade stop-loss for a bleeder (default OFF).
        # Editable floor = Trailing Stop % + 1 — never a deep loss. 0 = off.
        cap_sb = QDoubleSpinBox()
        cap_sb.setRange(0.0, 100.0)
        cap_sb.setSingleStep(0.5)
        cap_sb.setDecimals(1)
        cap_sb.setSuffix('%')
        cap_sb.setValue(float(settings.get('capitulation_pct', 0.0)) * 100.0)
        cap_sb.setToolTip(tr('Single-trade stop-loss for a coin that keeps '
                             'falling (a "bleeder"): if price drops this % below '
                             'what you paid, the bot gives up and sells at '
                             'market — a small, bounded loss instead of holding '
                             'forever. 0 = OFF.\n'
                             'Editable floor = Trailing Stop % + 1, so it can '
                             'never be a deep loss.\n'
                             'Only for SINGLE trades — DCA averages down and '
                             'never force-closes at a loss; auto-invest never '
                             'auto-sells.'))
        self.fields['capitulation_pct'] = cap_sb

        def _sync_cap(_=None):
            try:
                floor = trail_sb.value() + 1.0
            except Exception:
                floor = 2.0
            v = cap_sb.value()
            if v > 0 and v < floor:
                cap_sb.setValue(floor)  # snap an 'on' value to the floor
            cap_sb.setMinimum(0.0)

        trail_sb.valueChanged.connect(_sync_cap)
        cap_sb.valueChanged.connect(_sync_cap)
        _sync_cap()
        form.addRow(tr('Capitulation % (single trade):'), cap_sb)

        # --- Auto rearm / DCA
        ar_cb = QCheckBox()
        ar_cb.setChecked(bool(settings.get('auto_rearm', True)))
        self.fields['auto_rearm'] = ar_cb
        form.addRow(tr('Auto Rearm:'), ar_cb)

        dca_cb = QCheckBox()
        dca_cb.setChecked(bool(settings.get('dca', False)))
        self.fields['dca'] = dca_cb
        form.addRow(tr('DCA:'), dca_cb)

        # --- Limit orders (post_only maker) + fallback-to-taker, PER SIDE.
        # A side's "Fallback to taker" is only meaningful when its limit order
        # is on — it is greyed out otherwise (user, 2026-08-09).
        lim_buy = QCheckBox()
        lim_buy.setChecked(bool(settings.get('limit_order_buy', False)))
        lim_buy.setToolTip('Buy with a post_only LIMIT (maker) at the dip-bounce\n'
                           'trigger price — cheaper fee, fills only if price pulls\n'
                           'back to it. The bot keeps watching while it rests.')
        self.fields['limit_order_buy'] = lim_buy
        fb_buy = QCheckBox()
        fb_buy.setChecked(bool(settings.get('fallback_to_taker_buy', True)))
        fb_buy.setToolTip('If the limit buy can\'t fill in time (or post_only is\n'
                          'rejected), buy at MARKET (taker) so you never miss the\n'
                          'entry. Greyed out unless Limit Order Buy is on.')
        self.fields['fallback_to_taker_buy'] = fb_buy

        def _sync_buy(on):
            fb_buy.setEnabled(on)
        lim_buy.toggled.connect(_sync_buy)
        _sync_buy(lim_buy.isChecked())
        form.addRow(tr('Limit Order Buy:'), lim_buy)
        form.addRow(tr('Fallback to taker Buy:'), fb_buy)

        lim_sell = QCheckBox()
        lim_sell.setChecked(bool(settings.get('limit_order_sell', False)))
        lim_sell.setToolTip('Sell with a post_only LIMIT (maker) at the best bid —\n'
                            'cheaper fee, only placed above the no-loss entry\n'
                            'floor (never sells at a loss).')
        self.fields['limit_order_sell'] = lim_sell
        fb_sell = QCheckBox()
        fb_sell.setChecked(bool(settings.get('fallback_to_taker_sell', True)))
        fb_sell.setToolTip('If the limit sell can\'t fill in time, sell at MARKET\n'
                           '(taker). Greyed out unless Limit Order Sell is on.')
        self.fields['fallback_to_taker_sell'] = fb_sell

        def _sync_sell(on):
            fb_sell.setEnabled(on)
        lim_sell.toggled.connect(_sync_sell)
        _sync_sell(lim_sell.isChecked())
        form.addRow(tr('Limit Order Sell:'), lim_sell)
        form.addRow(tr('Fallback to taker Sell:'), fb_sell)

        # Safety nets for limit orders (user, 2026-08-09)
        keep_cb = QCheckBox()
        keep_cb.setChecked(bool(settings.get('keep_open_buy_for_dca', False)))
        keep_cb.setToolTip('Keep unfilled buy limits resting for the NEXT cycle /\n'
                           'after a reboot — the bot adopts them when they fill\n'
                           '(the position is journaled as that order).')
        self.fields['keep_open_buy_for_dca'] = keep_cb
        form.addRow(tr('Keep unfilled buy for DCA/Auto Rearm:'), keep_cb)

        cancel_cb = QCheckBox()
        cancel_cb.setChecked(bool(settings.get('cancel_sell_below_floor', True)))
        cancel_cb.setToolTip('Cancel a resting limit sell if price drops below\n'
                             'the no-loss floor (the fee-adjusted entry).')
        self.fields['cancel_sell_below_floor'] = cancel_cb
        form.addRow(tr('Cancel limit sell below floor:'), cancel_cb)

        # --- Limit order wait (maker_timeout_ms in seconds)
        wait_sb = QDoubleSpinBox()
        wait_sb.setRange(1.0, 30.0)
        wait_sb.setSingleStep(1.0)
        wait_sb.setDecimals(0)
        wait_sb.setSuffix(' s')
        try:
            wait_sb.setValue(float(settings.get('maker_timeout_ms', 5000)) / 1000.0)
        except Exception:
            wait_sb.setValue(5.0)
        wait_sb.setToolTip(tr('How long a resting limit order waits for a '
                              'passive (maker) fill before the bot gives up and '
                              'fills at market (taker), so it never misses the '
                              'trade. Shorter = it switches to market sooner.'))
        self.fields['maker_timeout_ms'] = wait_sb
        form.addRow(tr('Limit Order Maker Wait:'), wait_sb)

        # --- UI theme: Auto (desktop detection) / Light / Dark — live preview
        self._orig_theme = str(settings.get('theme', 'auto') or 'auto').strip().lower()
        if self._orig_theme not in ('auto', 'light', 'dark'):
            self._orig_theme = 'auto'
        theme_combo = QComboBox()
        theme_combo.addItems([tr('Auto'), tr('Light'), tr('Dark')])
        theme_combo.setCurrentIndex(
            0 if self._orig_theme == 'auto' else (1 if self._orig_theme == 'light' else 2))
        theme_combo.setToolTip(
            'Auto = follow the desktop. Light/Dark override — handy for\n'
            'reviewing the UI (e.g. checkbox visibility) in either theme.')
        self._theme_combo = theme_combo
        form.addRow(tr('Theme:'), theme_combo)

        def _theme_value():
            return ['auto', 'light', 'dark'][theme_combo.currentIndex()]

        def _preview_theme():
            try:
                apply_theme(QApplication.instance(), _theme_value())
            except Exception:
                pass

        theme_combo.currentIndexChanged.connect(_preview_theme)

        # --- UI language: Auto (OS/keyboard locale) or a specific language.
        # Local translation only — no data collected; applies on next launch.
        lang_combo = QComboBox()
        lang_combo.addItems(['Auto', 'English', 'Español', 'Português',
                             'Français', 'Deutsch', '中文'])
        _lang = str(settings.get('language') or '').lower()
        _lang_idx = {'': 0, 'en': 1, 'es': 2, 'pt': 3, 'fr': 4, 'de': 5, 'zh': 6}
        lang_combo.setCurrentIndex(_lang_idx.get(_lang, 0))
        lang_combo.setToolTip(tr('UI language — detected from the OS/keyboard '
                                 'locale (never IP), or pick one. Local only, '
                                 'no data collected. Applies on next launch; '
                                 'reports stay English.'))
        self._lang_combo = lang_combo
        form.addRow(tr('Language:'), lang_combo)

        # --- Crash/error report upload — OPT-IN (privacy-first, default off)
        report_cb = QCheckBox()
        report_cb.setChecked(bool(settings.get('report_opt_in', False)))
        report_cb.setToolTip('When the bot hits an internal error or crash it '
                             'collects a report\n'
                             '(crash_reports/error_*.log, which stays on THIS '
                             'machine for you to\n'
                             'review). With this ON, it auto-sends the scrubbed '
                             'report to the\n'
                             'developer\'s GitHub reports repo (no API keys — '
                             'settings redacted).\n'
                             'OFF by default. Nothing is ever sent unless you '
                             'turn this on.')
        self.fields['report_opt_in'] = report_cb
        form.addRow(tr('Send crash/error reports (opt-in):'), report_cb)

        # --- Auto-update: check (metadata-only) + install (opt-in) ----------
        upd_cb = QCheckBox()
        upd_cb.setChecked(bool(settings.get('auto_update_check', True)))
        upd_cb.setToolTip('While the app is open, check GitHub (metadata only — '
                          'no user\n'
                          'data) for a newer release. Never runs as a background '
                          'process.\n'
                          'ON by default.')
        self.fields['auto_update_check'] = upd_cb
        upd_install_cb = QCheckBox()
        upd_install_cb.setChecked(bool(settings.get('auto_update_install', False)))
        upd_install_cb.setToolTip('When a new release is found: INSTALL it '
                                  'automatically with a heads-up\n'
                                  '(10s countdown), restart the app, and return '
                                  'to state — mid-trade is fine\n'
                                  '(position resumes from the exchange). OFF by '
                                  'default = only check + notify\n'
                                  '(you click Install now). A failed update '
                                  'auto-rolls back to the previous version.')
        self.fields['auto_update_install'] = upd_install_cb

        def _sync_upd(on):
            upd_install_cb.setEnabled(on)
        upd_cb.toggled.connect(_sync_upd)
        _sync_upd(upd_cb.isChecked())
        form.addRow(tr('Check for updates:'), upd_cb)
        form.addRow(tr('Install updates automatically:'), upd_install_cb)

        # EXCHANGE MAINTENANCE NOTICES (user 2026-08-16): polls the exchange's
        # own status endpoint every 30 min and shows a banner at the top of the
        # in-app log when maintenance / an incident is active. Default ON.
        maint_cb = QCheckBox()
        maint_cb.setChecked(bool(settings.get('maintenance_check', True)))
        maint_cb.setToolTip(
            'Poll the exchange\'s own status page every 30 minutes.\n'
            'When maintenance or an incident is active, a banner is\n'
            'shown at the top of the log. Auto-clears when resolved.')
        self.fields['maintenance_check'] = maint_cb
        form.addRow(tr('Exchange maintenance notices:'), maint_cb)

        # --- Holding mode (already-holding detection, 2026-08-09) ----------
        hm_combo = QComboBox()
        hm_combo.addItems([tr('Keep separate (safe)'), tr('Combine (manage everything)'),
                           tr('Auto-invest takeover (buy-only + Force Close)')])
        hm_idx = {'separate': 0, 'combine': 1, 'auto_invest': 2}
        _hm = str(settings.get('holding_mode') or 'separate')
        hm_combo.setCurrentIndex(hm_idx.get(_hm, 0))
        hm_combo.setToolTip(
            'How the bot handles a pair where the account holds coins it did '
            'NOT buy.\n'
            'Keep separate (safe): the bot only trades what IT buys — your other '
            'coins are never touched.\n'
            'Combine: adopt and manage the WHOLE holding at API cost basis '
            '(volume/cycle trading).\n'
            'Auto-invest takeover: buy dips only (better cost basis than '
            'spot-at-market), NEVER auto-sells — for long-term holders; you exit '
            'via Force Close (a % of holdings, at market).')
        self._holding_combo = hm_combo
        form.addRow(tr('Holding Mode:'), hm_combo)

        # --- buttons
        btn_row = QHBoxLayout()
        self.save_btn = QPushButton(tr('Save'))
        self.cancel_btn = QPushButton(tr('Cancel'))
        self.save_btn.clicked.connect(self._save)

        def _cancel():
            try:
                apply_theme(QApplication.instance(), self._orig_theme)
            except Exception:
                pass
            self.reject()

        self._cancel = _cancel
        self.cancel_btn.clicked.connect(_cancel)
        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch(1)
        # Donate — opens a dialog with the USDC deposit address on Base.
        # Hidden when DONATE_ADDRESS is empty (dev builds).
        if DONATE_ADDRESS:
            self.donate_btn = QPushButton(tr('Donate'))
            self.donate_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.donate_btn.setToolTip('Support the developer with a USDC donation\n'
                                       '(Base network, ~$0.001 gas fee).')
            self.donate_btn.clicked.connect(self._on_donate)
            btn_row.addWidget(self.donate_btn)
        # Un-install — bottom right, sized only as wide as the text. Runs the
        # same full uninstall as EULA-Disagree (app + config + keys + logs).
        self.uninstall_btn = QPushButton(tr('Un-install'))
        self.uninstall_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.uninstall_btn.setToolTip('Permanently remove the app, all settings, '
                                      'keys, and data from this computer. '
                                      'Cannot be undone.')
        self.uninstall_btn.clicked.connect(self._on_uninstall)
        btn_row.addWidget(self.uninstall_btn)
        layout.addLayout(btn_row)

    def _on_uninstall(self):
        from PyQt5.QtWidgets import QApplication, QMessageBox
        from .. import eula
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle('Un-install')
        box.setText('This will REMOVE the application from this computer, '
                    'including all settings, API keys, and data. '
                    'This cannot be undone.\n\n'
                    'Continue?')
        btn_go = box.addButton('Un-install', QMessageBox.AcceptRole)
        box.addButton('Cancel', QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is not btn_go:
            return
        eula.uninstall()
        try:
            QApplication.instance().quit()
        except Exception:
            pass

    def _on_donate(self):
        """Show the donate dialog with the USDC deposit address on Base."""
        from PyQt5.QtWidgets import QApplication, QMessageBox
        dlg = QMessageBox(self)
        dlg.setWindowTitle(tr('Donate'))
        dlg.setIcon(QMessageBox.Information)
        dlg.setText(
            f'<b>USDC on Base Network</b><br><br>'
            f'Address:<br>'
            f'<code style="font-size:11px">{DONATE_ADDRESS}</code><br><br>'
            f'<b style="color:#d32f2f">Send USDC on the BASE network only.</b><br>'
            f'Sending on Ethereum mainnet or other networks<br>'
            f'will result in permanent loss of funds.<br><br>'
            f'Gas fee: ~$0.001 &nbsp; Minimum: ~$1 USDC'
        )
        dlg.setTextFormat(2)  # Qt.RichText
        copy_btn = dlg.addButton('Copy Address', QMessageBox.ActionRole)
        dlg.addButton(QMessageBox.Close)
        dlg.exec_()
        if dlg.clickedButton() is copy_btn:
            try:
                QApplication.clipboard().setText(DONATE_ADDRESS)
            except Exception:
                pass

    def _lookup_pair_min(self, product_id):
        """Minimum market-order size (quote currency) for a pair from the live
        API. Falls back to 2.0 when the API value is unknown — never below the
        exchange's enforced minimum."""
        if not product_id or self.client is None:
            return 2.0
        try:
            m = self.client.get_min_quote_size(product_id)
            if m is not None and m > 0:
                return m
        except Exception:
            pass
        return 2.0

    def _save(self):
        data = {}
        for k, v in self.fields.items():
            if isinstance(v, QLineEdit):
                data[k] = v.text()
            elif hasattr(v, 'value'):
                data[k] = v.value()
            elif isinstance(v, QComboBox):
                idx = v.currentIndex()
                data[k] = 'pct' if idx == 0 else ('fixed' if idx == 1 else 'coin')
            else:
                data[k] = v.isChecked()
        # PERCENT spinboxes (0.0%–100% UI) store FRACTIONS (0.01 = 1%) in
        # settings/engine — convert back on save (engine untouched).
        for k in ('dip_bounce_pct', 'trailing_stop_pct', 'trade_size_pct',
                  'capitulation_pct', 'take_profit_pct'):
            if k in data:
                data[k] = data[k] / 100.0
        # Limit-order wait is entered in SECONDS but stored in ms.
        if 'maker_timeout_ms' in data:
            data['maker_timeout_ms'] = int(round(float(data['maker_timeout_ms']) * 1000.0))
        # holding mode from its combo (separate / combine / auto_invest)
        data['holding_mode'] = ['separate', 'combine', 'auto_invest'][
            self._holding_combo.currentIndex()]
        # UI language from its combo ('' = auto / OS-keyboard locale)
        data['language'] = ['', 'en', 'es', 'pt', 'fr', 'de', 'zh'][
            self._lang_combo.currentIndex()]
        try:
            from ..i18n import set_language
            set_language(data['language'] or None)
        except Exception:
            pass
        # trading pair from the type-ahead dropdown (typed value survives even
        # if it isn't in the list — the API check below validates it)
        data['product_id'] = self._product_combo.currentText().strip().upper()
        data['quote_currencies'] = ','.join(
            q.strip().upper() for q in self._quote_cur.text().split(',') if q.strip())
        # this profile's exchange never changes here — switching APIs is handled
        # via _api_switch (launch the chosen API's profile)
        data['exchange'] = self._current_exchange
        data['theme'] = ['auto', 'light', 'dark'][self._theme_combo.currentIndex()]
        # preserve settings the dialog does not show (before any early return)
        cur = self.settings
        for k in ('auto_rearm', 'dca', 'maker_retries', 'exchange'):
            if k not in data and k in cur:
                data[k] = cur[k]
        # "Add new bot…" now acts IMMEDIATELY on selection (see _on_api_index);
        # nothing to do here at Save time.
        # a DIFFERENT saved API selected -> switch to it
        target = getattr(self, '_api_switch_target', None)
        if target:
            QMessageBox.information(
                self, 'Switch API',
                f'Switching to "{self._api_switch_label}".\n\n'
                '- If this bot is mid-trade, it opens as a NEW bot alongside\n'
                '  and this trade is left untouched.\n'
                '- If it is flat, this bot relaunches as that API.')
            save_settings(data)
            data['_api_switch'] = target
            self.on_save(data)
            self.accept()
            return
        # API check: warn when the exchange doesn't support the trading pair
        if self.client is not None:
            try:
                ok = self.client.product_valid(data['product_id'])
                if ok is False:
                    QMessageBox.warning(self, 'Unsupported Pair',
                                        f"API (exchange) doesn't support trading pair {data['product_id']}.")
                    return
            except Exception:
                pass
        save_settings(data)
        self.on_save(data)
        self.accept()

    def _on_add_new(self):
        name, ok = QInputDialog.getText(
            self, 'Add New Bot',
            'Name this bot (it gets its OWN API key):\n\n'
            'e.g.  kraken   binance   ...')
        if not ok or not name.strip():
            return
        name = ''.join(c for c in name.strip().lower() if c.isalnum() or c in '-_')
        if not name:
            return
        if api_registry.launch_profile(api_registry.new_profile_dir(name)):
            # multibot return-to-state (user, 2026-08-16): regenerate systemd
            # units so the new profile also relaunches after a reboot.
            try:
                from .. import systemd
                systemd.enable_all()
            except Exception:
                pass
            QMessageBox.information(
                self, 'New Bot',
                f'Opening "{name}" — its setup screen will collect that\n'
                "exchange's API key (I have account / Don't have account, the\n"
                'NEVER-use-Transfer reminder, and Load-from-file / Paste-from-\n'
                'clipboard capture).')
