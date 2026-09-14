"""First-run dialog — credentials for any exchange.

Exchange is a searchable dropdown. Two helper buttons open the exchange in the
browser AFTER showing the "NEVER use API with Transfer" reminder:
  - [I have account]    -> that exchange's API-key page
  - [Don't have account]-> that exchange's homepage (referral link where the
                           user has one)
Then the credential fields collect the key material (coinbase: API Key Name +
Private Key PEM; any other exchange via CCXT: API Key / Secret / Password).
"""
import os
import re
import webbrowser

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QApplication, QComboBox, QCompleter, QDialog,
                             QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QTextEdit, QVBoxLayout)

from ..i18n import tr
from .. import keys
from ..config import load_settings, save_settings

_KNOWN_EXCHANGES = ['coinbase', 'binance', 'binanceus', 'kraken', 'kucoin',
                    'bybit', 'okx', 'bitget', 'gate', 'mexc']

def _all_ccxt_ids():
    """Full list of CCXT exchange IDs (105+), used to make the combo searchable.
    Falls back to curated if CCXT is unavailable (e.g. during tests)."""
    try:
        import ccxt
        return sorted(set(ccxt.exchanges))
    except Exception:
        return list(_KNOWN_EXCHANGES)

# Referral links (user-provided 2026-08-07): the referrer is paid a share of a
# new user's trading fees after they fund an account; the URL just carries the
# referrer ID — no app-side logic.
_REFERRAL = {
    'coinbase': 'https://coinbase.com/join/6P9SY9T?src=referral-link',
    'binanceus': 'https://binance.us/universal_JHHGDSKDJ/auth/registration?ref=59216177',
}

_HOMEPAGE = {
    'coinbase': 'https://www.coinbase.com',
    'binance': 'https://www.binance.com',
    'binanceus': 'https://www.binance.us',
    'kraken': 'https://www.kraken.com',
    'kucoin': 'https://www.kucoin.com',
    'bybit': 'https://www.bybit.com',
    'okx': 'https://www.okx.com',
    'bitget': 'https://www.bitget.com',
    'gate': 'https://www.gate.io',
    'mexc': 'https://www.mexc.com',
}

_APIPAGE = {
    'coinbase': 'https://www.coinbase.com/settings/api',
    'binance': 'https://www.binance.com/en/my/settings/api-management',
    'binanceus': 'https://www.binance.us/en/usercenter/settings/api-management',
    'kraken': 'https://www.kraken.com/u/security/api',
    'kucoin': 'https://www.kucoin.com/account/api',
    'bybit': 'https://www.bybit.com/app/user/api-management',
    'okx': 'https://www.okx.com/account/my-api',
    'bitget': 'https://www.bitget.com/api-doc',
    'gate': 'https://www.gate.io/myaccount/developers',
    'mexc': 'https://www.mexc.com/api',
}


_PEM_RE = re.compile(
    r'-----BEGIN ([A-Z ]*PRIVATE KEY)-----.*?-----END \1-----', re.S)
_KEY_NAME_RE = re.compile(r'organizations/[A-Za-z0-9_./\-]+')


def _extract_pem(text):
    """The PEM private-key block inside downloaded/copied text (Coinbase)."""
    m = _PEM_RE.search(text or '')
    return m.group(0) if m else ''


def _extract_pair(text):
    """A copied 'Key / Secret' blob (newline- or comma-separated) from
    exchanges that don't offer a download (e.g. Binance.US) — user copies the
    key and secret from the site, pastes them here."""
    tokens = [t.strip() for t in re.split(r'[\n,]+', text or '') if t.strip()]
    if len(tokens) >= 2 and all(len(t) > 10 for t in tokens[:2]):
        return tokens[0], tokens[1]
    return None


def warn_transfer():
    """'NEVER use API with Transfer' reminder — shown BEFORE any API-page or
    open-account link, and kept visible as a constant reminder (user, 2026-08-07)."""
    box = QMessageBox()
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(tr('API Permission Warning'))
    box.setText(
        'NEVER use API with Transfer (initiate transfer of funds) here.\n\n'
        'Use an API key with TRADE / READ permissions ONLY — never one that\n'
        'can move funds. A key with "transfer" permission could move money.\n'
        'The app only ever buys and sells; it never needs transfer access.')
    box.addButton('OK', QMessageBox.AcceptRole)
    box.exec_()


def _open_exchange_page(exchange, kind):
    """kind 'have' -> API-key page; kind 'dont' -> homepage (referral first)."""
    exchange = (exchange or '').strip().lower()
    if kind == 'have':
        url = _APIPAGE.get(exchange) or _HOMEPAGE.get(exchange)
    else:
        url = _REFERRAL.get(exchange) or _HOMEPAGE.get(exchange)
    warn_transfer()
    if url:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    else:
        box = QMessageBox()
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(tr('Open Account'))
        box.setText(f"No link known for exchange '{exchange}'.\n"
                    'Go to that exchange\'s own website to open an account / '
                    'create an API key.')
        box.addButton('OK', QMessageBox.AcceptRole)
        box.exec_()


class FirstRunDialog(QDialog):
    def __init__(self, parent=None, initial_exchange='coinbase'):
        super().__init__(parent)
        self.setWindowTitle(tr('Bot Trader - First Time Setup'))
        self.setFixedSize(520, 500)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Enter your API credentials:'))

        layout.addWidget(QLabel('Exchange:'))
        self.exchange_combo = QComboBox()
        self.exchange_combo.setEditable(True)
        self.exchange_combo.setInsertPolicy(QComboBox.NoInsert)
        self.exchange_combo.addItems(_KNOWN_EXCHANGES)
        all_ids = _all_ccxt_ids()
        completer = QCompleter(all_ids, self.exchange_combo)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.exchange_combo.setCompleter(completer)
        init = (initial_exchange or 'coinbase').strip().lower()
        if init in all_ids or init in _KNOWN_EXCHANGES:
            self.exchange_combo.setCurrentText(init)
        else:
            self.exchange_combo.setCurrentText('coinbase')
        self.exchange_combo.setToolTip(
            'Type to search any CCXT-supported exchange (105+).\n'
            'The 10 shortcuts below are quick-picks — the rest are\n'
            'all searchable by typing their name or ID.\n'
            'coinbase = native adapter; any other exchange uses CCXT.')
        layout.addWidget(self.exchange_combo)

        layout.addWidget(QLabel('Help with your exchange account:'))
        row = QVBoxLayout()
        self.have_btn = QPushButton('I have account')
        self.dont_btn = QPushButton("Don't have account")
        self.have_btn.clicked.connect(self._on_have)
        self.dont_btn.clicked.connect(self._on_dont)
        row.addWidget(self.have_btn)
        row.addWidget(self.dont_btn)
        layout.addLayout(row)

        layout.addSpacing(6)
        layout.addWidget(QLabel('API Key Name (Coinbase):'))
        self.api_key_name_edit = QLineEdit()
        self.api_key_name_edit.setPlaceholderText('Exchange API Key Name')
        layout.addWidget(self.api_key_name_edit)

        layout.addWidget(QLabel('Private Key / PEM (Coinbase):'))
        self.private_key_edit = QTextEdit()
        self.private_key_edit.setPlaceholderText('-----BEGIN EC PRIVATE KEY----- ...')
        self.private_key_edit.setFixedHeight(80)
        layout.addWidget(self.private_key_edit)

        layout.addWidget(QLabel('API Key (other exchanges via CCXT):'))
        self.ccxt_key_edit = QLineEdit()
        self.ccxt_key_edit.setPlaceholderText('optional for coinbase')
        layout.addWidget(self.ccxt_key_edit)

        layout.addWidget(QLabel('API Secret (CCXT):'))
        self.ccxt_secret_edit = QLineEdit()
        self.ccxt_secret_edit.setPlaceholderText('optional for coinbase')
        layout.addWidget(self.ccxt_secret_edit)

        layout.addWidget(QLabel('Password / Passphrase (CCXT, if required):'))
        self.ccxt_password_edit = QLineEdit()
        self.ccxt_password_edit.setPlaceholderText('optional')
        layout.addWidget(self.ccxt_password_edit)

        # Capture helper: 'download' (a key file) or 'copy' (clipboard).
        capture_row = QHBoxLayout()
        self.import_btn = QPushButton(tr("Load from file…"))
        self.clip_btn = QPushButton(tr("Paste from clipboard"))
        self.import_btn.setToolTip(
            'If the exchange lets you DOWNLOAD the API key (e.g. Coinbase),\n'
            'pick that downloaded file — the key material is filled in for you.')
        self.clip_btn.setToolTip(
            'If the exchange only lets you COPY the key (e.g. Binance.US),\n'
            'copy it on the site, then click here — it is pasted from your\n'
            'clipboard into the fields below.')
        self.import_btn.clicked.connect(self._on_import_file)
        self.clip_btn.clicked.connect(self._on_use_clipboard)
        capture_row.addWidget(self.import_btn)
        capture_row.addWidget(self.clip_btn)
        layout.addLayout(capture_row)

        self.btn = QPushButton('Save & Continue')
        self.btn.clicked.connect(self._on_save)
        layout.addWidget(self.btn)

    def _exchange(self):
        return self.exchange_combo.currentText().strip().lower() or 'coinbase'

    def _on_have(self):
        _open_exchange_page(self._exchange(), 'have')

    def _on_dont(self):
        _open_exchange_page(self._exchange(), 'dont')

    def _on_import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Load API key file',
            os.path.expanduser('~/Downloads'),
            'Key files (*.txt *.pem *.key *.json *.csv);;All files (*)')
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                self._fill_from_text(f.read())
        except Exception as e:
            QMessageBox.warning(self, 'Load Failed',
                                f'Could not read the key file:\n{e}')

    def _on_use_clipboard(self):
        try:
            text = QApplication.clipboard().text()
        except Exception:
            text = ''
        if not text or not text.strip():
            QMessageBox.information(self, 'Clipboard Empty',
                                    'Nothing to paste — copy the API key on the '
                                    'exchange site first, then click again.')
            return
        self._fill_from_text(text)

    def _fill_from_text(self, text):
        """Fill the credential fields from a downloaded key file or copied text.
        A PEM block (Coinbase) goes into the private-key box (and the key name
        is extracted); otherwise a 'Key / Secret' pair fills the CCXT fields."""
        text = text or ''
        pem = _extract_pem(text)
        if pem:
            self.exchange_combo.setCurrentText('coinbase')
            self.private_key_edit.setPlainText(pem)
            m = _KEY_NAME_RE.search(text)
            if m:
                self.api_key_name_edit.setText(m.group(0))
            return
        pair = _extract_pair(text)
        if pair:
            self.ccxt_key_edit.setText(pair[0])
            self.ccxt_secret_edit.setText(pair[1])
            return
        token = text.strip()
        if token:
            self.ccxt_key_edit.setText(token)

    def _on_save(self):
        exchange = self._exchange()
        name = self.api_key_name_edit.text().strip()
        pem = self.private_key_edit.toPlainText().strip()
        ckey = self.ccxt_key_edit.text().strip()
        csec = self.ccxt_secret_edit.text().strip()
        cpass = self.ccxt_password_edit.text().strip()
        if exchange == 'coinbase' and (not name or not pem):
            QMessageBox.warning(self, 'Missing Credentials',
                                'Coinbase needs the API Key Name and the Private Key PEM.')
            return
        keys.save_keys(name, pem, ccxt_api_key=ckey, ccxt_secret=csec, ccxt_password=cpass)
        cur = load_settings()
        cur['exchange'] = exchange
        # Auto-invest gate (user, 2026-08-09): right after first-run setup, ask
        # Full trading (default) vs Auto-invest (buy-only). Changeable in Settings.
        cur['holding_mode'] = self._ask_holding_mode()
        save_settings(cur)
        self.accept()

    def _ask_holding_mode(self):
        """First-run choice: Full trading (default) or Auto-invest (buy-only).
        Auto-invest takeover buys dips ONLY (better cost basis), never auto-
        sells, and you exit via Force Close. Full trading leaves the mode unset
        (the holding prompt + Settings combo handle it)."""
        box = QMessageBox(self)
        box.setWindowTitle(tr('Run Mode'))
        box.setText('How do you want this bot to run?\n\n'
                    'Full trading (default): dip-buy + trailing-stop + optional '
                    'DCA and limit orders — volume/cycle trading.\n\n'
                    'Auto-invest (buy-only): buys dips ONLY, never auto-sells — '
                    'for long-term holders; you exit via Force Close.\n\n'
                    '(Changeable anytime in Settings → Holding Mode.)')
        btn_full = box.addButton(tr('Full trading'), QMessageBox.AcceptRole)
        btn_ai = box.addButton(tr('Auto-invest (buy-only)'), QMessageBox.AcceptRole)
        box.setDefaultButton(btn_full)
        box.exec_()
        return 'auto_invest' if box.clickedButton() is btn_ai else ''
