"""First-run dialog — credentials for any exchange.

Exchange is a searchable dropdown. Two helper buttons open the exchange in the
browser AFTER showing the "NEVER use API with Transfer" reminder:
  - [I have account]    -> that exchange's API-key page
  - [Don't have account]-> that exchange's homepage (referral link where the
                           user has one)
Then the credential fields collect the key material (coinbase: API Key Name +
Private Key PEM; any other exchange via CCXT: API Key / Secret / Password).
"""
import json
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

# Field names found in the JSON files exchanges let you DOWNLOAD (Coinbase
# downloads `cdp_api_key_<name>.json` with "name" + "privateKey").
_PEM_FIELDS = ('privateKey', 'private_key', 'pem', 'key_pem')
_NAME_FIELDS = ('name', 'apiKeyName', 'keyName', 'key_name')
_KEY_FIELDS = ('apiKey', 'api_key', 'key')
_SECRET_FIELDS = ('secret', 'apiSecret', 'api_secret', 'secretKey')


def _normalize_pem(text):
    """Turn escaped newlines that survive a copied JSON value (`\\n` and `\\r`)
    into the real newlines `cryptography` needs to parse the PEM block."""
    s = (text or '').strip().replace('\\r', '\r').replace('\\n', '\n')
    return s.strip()


def _extract_pem(text):
    """The PEM private-key block inside downloaded/copied text (Coinbase)."""
    m = _PEM_RE.search(text or '')
    return m.group(0) if m else ''


def _valid_pem(pem):
    """True when the text parses as a private-key PEM (Coinbase EC key)."""
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        load_pem_private_key(_normalize_pem(pem).encode('utf-8'), password=None)
        return True
    except Exception:
        return False


def _b64decode_safe(token):
    import base64
    try:
        return base64.b64decode(token)
    except Exception:
        return None


def _pem_from_key(text):
    """Turn a pasted/imported key value into a parseable PEM string.

    Coinbase's downloaded `cdp_api_key_<name>.json` carries the private key as a
    BARE single-line base64 with NO 'BEGIN/END PRIVATE KEY' armor (user field
    report 2026-09-14) — which is exactly why pasting/importing it once left the
    fields blank and stored a key that failed with MalformedFraming. Handles:
      * a complete PEM block            -> returned as-is (newlines normalized)
      * a bare base64 / hex DER key     -> decoded & re-serialized to PKCS8 PEM
      * a bare SEC1 base64              -> armed with EC PRIVATE KEY headers
    Returns '' when nothing parses.
    """
    from cryptography.hazmat.primitives.serialization import (Encoding,
                                                              NoEncryption,
                                                              PrivateFormat,
                                                              load_der_private_key)
    s = _normalize_pem(text)
    blk = _extract_pem(s)
    if blk:
        return blk
    token = ''.join(s.split())
    if not token or len(token) < 20:
        return ''
    is_hex = all(c in '0123456789abcdefABCDEF' for c in token) and len(token) % 2 == 0
    candidates = []
    if _b64decode_safe(token) is not None:
        candidates.append(_b64decode_safe(token))
    if is_hex:
        try:
            candidates.append(bytes.fromhex(token))
        except Exception:
            pass
    for der in candidates:
        if der and len(der) == 64:
            # Coinbase Ed25519 key: 64 bytes = 32-byte seed + 32-byte public
            # key (docs, 2026). Wrap the seed as a PKCS8 PEM so the regular
            # load_pem_private_key path + validator both work unchanged.
            try:
                from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                    Ed25519PrivateKey)
                key = Ed25519PrivateKey.from_private_bytes(der[:32])
                return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8,
                                         NoEncryption()).decode().strip()
            except Exception:
                pass
        try:
            key = load_der_private_key(der, password=None)
            return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8,
                                     NoEncryption()).decode().strip()
        except Exception:
            continue
    if all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=' for c in token):
        import textwrap
        body = '\n'.join(textwrap.wrap(token, 64))
        pem = (f'-----BEGIN EC PRIVATE KEY-----\n{body}\n'
               '-----END EC PRIVATE KEY-----')
        if _valid_pem(pem):
            return pem
    return ''


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

        Order of attempts:
          1. JSON (exchanges download JSON key files, e.g. Coinbase's
             `cdp_api_key_<name>.json` with "name" + "privateKey") — parsed
             structurally so escaped `\\n` inside the PEM is handled correctly.
          2. A raw PEM block anywhere in the text (+ key name extraction).
          3. A 'Key / Secret' pair (newline/comma separated) for CCXT.
          4. A bare token as a CCXT API key.
        """
        text = text or ''
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                pem = ''
                for k in _PEM_FIELDS:
                    if isinstance(obj.get(k), str):
                        pem = _pem_from_key(obj.get(k))
                        if pem:
                            break
                name = next((str(obj.get(k)).strip() for k in _NAME_FIELDS
                             if obj.get(k)), '')
                if pem:
                    self.exchange_combo.setCurrentText('coinbase')
                    self.private_key_edit.setPlainText(pem)
                    if name:
                        self.api_key_name_edit.setText(name)
                    return
                ckey = next((str(obj.get(k)).strip() for k in _KEY_FIELDS
                             if obj.get(k)), '')
                csec = next((str(obj.get(k)).strip() for k in _SECRET_FIELDS
                             if obj.get(k)), '')
                if ckey:
                    self.ccxt_key_edit.setText(ckey)
                    if csec:
                        self.ccxt_secret_edit.setText(csec)
                    return
        except Exception:
            pass  # not JSON — fall through to regex/line parsing

        pem = _extract_pem(text)
        if pem:
            self.exchange_combo.setCurrentText('coinbase')
            self.private_key_edit.setPlainText(_normalize_pem(pem))
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
        pem = _normalize_pem(self.private_key_edit.toPlainText())
        ckey = self.ccxt_key_edit.text().strip()
        csec = self.ccxt_secret_edit.text().strip()
        cpass = self.ccxt_password_edit.text().strip()
        if exchange == 'coinbase':
            if not name or not pem:
                QMessageBox.warning(self, 'Missing Credentials',
                                    'Coinbase needs the API Key Name and the Private Key PEM.')
                return
            if not _valid_pem(pem):
                converted = _pem_from_key(pem)
                if converted and _valid_pem(converted):
                    pem = converted
                    self.private_key_edit.setPlainText(pem)
                else:
                    QMessageBox.warning(
                        self, 'Invalid Private Key',
                        'That Private Key does not parse as a valid PEM key.\n\n'
                        'Coinbase keys look like:\n'
                        '-----BEGIN EC PRIVATE KEY-----\n'
                        'MIGEAgEBA...\n'
                        '-----END EC PRIVATE KEY-----\n\n'
                        'Use "Load from file…" on the downloaded cdp_api_key_<name>.json '
                        '— the name and key fill in automatically.')
                    return
            self.private_key_edit.setPlainText(pem)
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
