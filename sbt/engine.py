"""Trading engine — exact port of the restored app's behavior as clean source.

Contains the state machine (WATCHING -> dip -> BUY -> HOLDING -> trailing stop
-> SELL -> auto-rearm), DCA, fee-accurate thresholds, and the never-take-a-loss
exit — all talking to the exchange-agnostic Provider interface.
"""
import json
import math
import os
import secrets
import tempfile
import time
import uuid
from datetime import datetime, timezone

from . import paths
from .config import load_settings
from .models import BotStatus, BotState, Position
from .providers import create_provider

# Admin build (SBT_ADMIN=1): the in-app log shows EVERY engine line so the
# developer can catch everything; the shipped build shows only user=True lines.
try:
    from .admin import is_admin as _is_admin
    _ADMIN_LOG = bool(_is_admin())
except Exception:
    _ADMIN_LOG = False


def _clean_error(exc):
    """Strip HTML, extract the meaningful part of an exchange error for
    user-facing logs. A shipped user should never see raw HTML or 502
    stack traces — just 'exchange temporarily unavailable' or a short
    human-readable message."""
    s = str(exc)
    # Coinbase/Cloudflare HTML error pages
    if '<html' in s.lower() or '<title' in s.lower():
        import re as _re
        title = _re.search(r'<title>(.*?)</title>', s, _re.I)
        msg = title.group(1).strip() if title else 'exchange error'
        return f'exchange: {msg}'
    # HTTP status codes from provider wrappers
    for code in ('502', '503', '504', '429'):
        if code in s:
            return f'exchange temporarily unavailable ({code})'
    # Truncate very long messages (provider internals, JSON dumps)
    if len(s) > 120:
        s = s[:117] + '...'
    return s


# The dip-bounce used for buys is never allowed below the exchange's CURRENT
# taker fee + this margin, so a bounce always clears the fee (prevents the
# compounding re-buy trap when the fee exceeds the user's bounce %).
DIP_FEE_MARGIN = 0.001

# ---- "the show must go on": no internal error ever stops the bot ----------
# A bot meant for UNATTENDED use must never land in a state that waits for a
# human to press Start. On any internal error it logs, collects a reviewable
# error report (throttled per phase so a recurring fault never spams issues),
# and returns to WATCHING — the report is for the user's review + the
# developer's fix, never a reason to stop trading.
_ERROR_REPORT_WINDOW_S = 6 * 3600   # at most one error report per phase per 6h

# ---- HMAC log chain (tamper detection, user 2026-08-18) --------------------
_log_chain = None  # LogChain instance, initialized after API key is loaded


def init_log_chain(exchange, cred):
    """Initialize the HMAC log chain from the API key fingerprint.  Called once
    at startup after the API key is available.  Chain stays disabled (None)
    until a real API key exists."""
    global _log_chain
    try:
        from .log_chain import LogChain, _derive_secret
        secret_hex = _derive_secret(exchange, cred)
        if secret_hex:
            _log_chain = LogChain(paths.LOG_FILE, secret_hex,
                                  state_dir=paths.CONFIG_DIR)
    except Exception:
        _log_chain = None


def flush_log_chain():
    """Persist chain state at shutdown."""
    if _log_chain:
        try:
            _log_chain.flush()
        except Exception:
            pass


def _log_chain_integrity():
    """Return the chain integrity report string (for crash reports)."""
    if _log_chain:
        return _log_chain.integrity_report()
    return 'LOG CHAIN: DISABLED (not initialized)'


def _write_error_report(phase, detail, count=1):
    """Write a structured, reviewable error report next to the crash reports.
    Contains the phase, the detail, the occurrence count, the HMAC chain
    integrity status, and the tail of the diagnostic log so the user (and the
    developer) can see what happened.  The transport (opt-in GitHub issue
    upload) is a separate item."""
    try:
        from .log_chain import cleanup_crash_reports
        d = paths.CRASH_DIR
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            d = tempfile.gettempdir()
        fn = os.path.join(d, f"error_{time.strftime('%Y%m%d_%H%M%S')}_{phase}.log")
        with open(fn, 'w') as f:
            f.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] error report ({phase}) — occurrence #{count}\n')
            f.write(f'phase: {phase}\n')
            f.write(f'detail: {detail}\n')
            f.write(f'occurrence: {count}\n')
            f.write(f'chain: {_log_chain_integrity()}\n')
            f.write('recovery: the bot recovered and is watching again — no user action needed\n')
            f.write('---- diagnostic log tail ----\n')
            try:
                tail = open(paths.LOG_FILE).read().splitlines()[-25:]
                f.write('\n'.join(tail) + '\n')
            except Exception:
                pass
        # rolling cleanup: keep at most 50 crash/error logs.  ONLY ever run
        # this against the app-owned CRASH_DIR — if the write fell back to
        # the shared temp dir, deleting "oldest crash_*.log" there could
        # remove OTHER processes'/users' files.
        try:
            if os.path.abspath(d) == os.path.abspath(paths.CRASH_DIR):
                cleanup_crash_reports(d)
        except Exception:
            pass
        return fn
    except Exception:
        return None


def _log_file(msg):
    try:
        line = f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
        if _log_chain:
            _log_chain.append(line)
        else:
            with open(paths.LOG_FILE, 'a') as f:
                f.write(f'{line}\n')
    except Exception:
        pass


class DipDetector:
    def __init__(self, dip_bounce_pct):
        self.dip_bounce_pct = dip_bounce_pct
        self.local_low = None
        self.armed = True

    def reset(self):
        self.local_low = None
        self.armed = True

    def update(self, price):
        if not self.armed:
            return False
        if self.local_low is None or price < self.local_low:
            self.local_low = price
        if price >= self.local_low * (1 + self.dip_bounce_pct):
            self.armed = False
            return True
        return False


class TrailingStop:
    def __init__(self, trail_pct):
        self.trail_pct = trail_pct
        self.reset()

    def reset(self):
        self.highest_price = None
        self.entry_price = None
        self.active = False

    def activate(self, entry_price):
        self.highest_price = entry_price
        self.entry_price = entry_price
        self.active = True

    def update(self, price):
        if self.active:
            if price > self.highest_price:
                self.highest_price = price
            return price <= self.highest_price * (1 - self.trail_pct)
        return False

    def get_stop_price(self):
        if self.active:
            return self.highest_price * (1 - self.trail_pct)
        return self.highest_price


def generate_order_id():
    return str(uuid.uuid4())


class TradingBot:
    def __init__(self, api_key_name, private_key_pem, log_handler=None,
                 ccxt_api_key='', ccxt_secret='', ccxt_password=''):
        self._settings = load_settings()
        self.settings = self._settings
        exchange = self.settings.get('exchange', 'coinbase') or 'coinbase'
        self.client = create_provider(exchange, api_key_name, private_key_pem,
                                      ccxt_api_key, ccxt_secret, ccxt_password)
        self.state = BotState()
        self.dip_detector = DipDetector(self.effective_dip())
        self.trailing_stop = TrailingStop(float(self.settings.get('trailing_stop_pct', 0.01)))
        self.order_id = None
        self.log_handler = log_handler
        # engine state
        self._trigger_lowest = None
        self._dca_bounce_low = None
        self._dca_paused = False
        self._dca_pause_check_ts = 0.0
        self._dca_bal_fail_count = 0  # consecutive balance-check failures
        self._sell_cooldown_until = 0.0
        # BUY COOLDOWN (2026-08-18 incident): after any buy attempt, no new
        # buy (market or limit) for this many seconds — choppy price must not
        # machine-gun orders every bounce (the morning stack drained $28 in
        # two minutes). Default 60s; settings override: buy_cooldown_s.
        self._last_buy_ts = 0.0
        self._buy_paused = False
        self._buy_pause_check_ts = 0.0
        self._in_on_price = False
        self._buy_fail_count = 0
        # per-phase error-report bookkeeping (throttle window + running count)
        self._error_counts = {}
        self._last_error_report = {}
        # per-cycle in-app log: set by the UI; called when a new position opens
        # so the user sees only the current buy->hold->sell cycle.
        self.clear_log_cb = None
        self._pending_settings_data = None
        self._pending_settings_cb = None
        self._trade_closed_ok = False
        # True while the held position cannot be verified from the exchange
        # (balance read failing) — the bot must NOT trade on an unknown
        # position (never buy on top of a real holding against a wrong basis).
        self._position_unknown = False
        # Pending limit orders (post_only maker) being polled. There can be
        # SEVERAL resting buys (dip triggers keep firing — an unfilled limit
        # must never stop the bot from making another buy trigger; "the show
        # must go on"). Each dict: {kind:'buy'|'dca'|'sell', order_id,
        # limit_price, placed, size, fallback:bool, keep_open:bool (buy/dca)}.
        self._pending = self._load_parked()

    # ---- helpers ---------------------------------------------------------
    def _log(self, msg, user=False):
        """Diagnostic file gets EVERY message. The in-app (user) log only gets
        lines marked user=True — 'what happened' for the user, never engine/
        provider internals (no API keys, no per-tick diagnostics). EXCEPTION:
        the admin build (SBT_ADMIN=1, dev machine) shows the FULL log in-app so
        the developer can catch everything the bot does."""
        _log_file(msg)
        if (user or _ADMIN_LOG) and self.log_handler:
            try:
                self.log_handler(msg)
            except Exception:
                pass

    def _cycle_clear(self):
        """New buy cycle -> clear the in-app log so the user sees only the
        current buy->hold->sell cycle (full history stays on disk)."""
        cb = getattr(self, 'clear_log_cb', None)
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _update_own_base(self, delta):
        """Atomically update the bot's own base (how much it bought and still
        holds). Persisted to runtime_state.json so it survives restarts.
        Called on each buy/sell fill. The exchange is the source of truth for
        cost basis; own_base only distinguishes bot-bought vs user-held coins."""
        from . import runtime_state
        cur = runtime_state.load_own_base()
        new_val = max(0.0, cur + delta)
        runtime_state.save_own_base(new_val)
        return new_val

    def _maybe_report_error(self, phase, detail):
        """Collect a reviewable error report (throttled per phase). The bot
        ALWAYS recovers to watching afterwards — the report is for the user's
        review and the developer's fix, never a reason to stop trading. If the
        user opted in, the scrubbed report is ALSO auto-sent to the developer's
        GitHub reports repo (never blocks; failures retry at the next launch)."""
        self._error_counts[phase] = self._error_counts.get(phase, 0) + 1
        try:
            now = time.time()
            if now - self._last_error_report.get(phase, 0.0) >= _ERROR_REPORT_WINDOW_S:
                self._last_error_report[phase] = now
                fn = _write_error_report(phase, detail, count=self._error_counts[phase])
                if fn:
                    try:
                        from . import report_transport
                        if self.settings.get('report_opt_in', False):
                            report_transport.send_in_background(
                                fn, phase, detail, self._error_counts[phase],
                                self.settings)
                    except Exception:
                        pass
        except Exception:
            pass

    def _recover_to_watching(self, phase, detail):
        """Standard recovery: log (user-facing), report (throttled), and return
        to WATCHING. The show must go on — nothing internal ever stops the bot.
        Does NOT reset _buy_fail_count — recurrence accumulates until a
        successful buy clears it."""
        self._maybe_report_error(phase, detail)
        self.state.status = BotStatus.WATCHING
        self.state.position = None
        self.state.error_message = f'{detail} — recovered, watching again'
        self.state.local_low = 0.0
        self.dip_detector.reset()
        self._log(f'ERROR ({phase}): {detail} — recovered, watching again', user=True)

    def _inc_trunc(self, val, pid):
        inc = self.client.get_base_increment(pid)
        return math.floor(val / inc + 1e-12) * inc

    def _inc_ceil(self, val, pid):
        """Smallest multiple of the base increment that is >= val — used ONLY
        for the order MINIMUM floor (a clean sellable base amount whose quote
        value clears the exchange minimum). Never used for the trade size
        itself (trade size is always truncated DOWN)."""
        inc = self.client.get_base_increment(pid)
        return math.ceil(val / inc - 1e-12) * inc

    def _no_pair_warning(self):
        return False

    def _min_quote(self, product_id):
        """Minimum market-order size in QUOTE currency: the exchange's REAL
        minimum (getvalue) for the pair, or the user's min_quote_size setting
        if higher. Falls back to 2.0 ONLY when the API value is unknown — the
        guardrail never trades below an enforced exchange minimum."""
        try:
            api_min = self.client.get_min_quote_size(product_id)
        except Exception:
            api_min = None
        try:
            user_min = float(self.settings.get('min_quote_size', 0.0) or 0.0)
        except Exception:
            user_min = 0.0
        if api_min is not None and api_min > 0:
            return max(api_min, user_min) if user_min > 0 else api_min
        return user_min if user_min > 0 else 2.0

    def _take_profit_mark(self, entry):
        """Price (per unit) that must be reached BEFORE the trailing stop arms:
        entry × (1 + take_profit_pct). Below the mark the bot HOLDS (never
        sells, no trail, DCA re-buys below entry); at/above it the trailing
        stop arms at that peak and follows upward. Replaces the old
        fee + trailing% + $0.05 floor — that 'floor selling at the nickel'
        (user-flagged) trapped trades in a loss-less-but-pointless close."""
        tp = float(self.settings.get('take_profit_pct', 0.05) or 0.0)
        return entry * (1 + tp)

    def effective_dip(self):
        """Dip-bounce used for buys: the user's setting, but never below the
        exchange's CURRENT taker fee + a 0.1% margin. If the user's bounce is
        too small to clear the fee (e.g. 1% vs a 1.2% tier), it is raised so a
        bounce can always reach the fee-adjusted entry (no compounding buys).
        Tracks the live fee tier automatically — when the user crosses a fee
        threshold and the fee drops, the effective value reverts to the user's
        setting. Silent, exchange-agnostic, no dialogs."""
        try:
            fee = self.client.get_trading_fee()
        except Exception:
            fee = 0.0
        return max(float(self.settings.get('dip_bounce_pct', 0.01)), fee + DIP_FEE_MARGIN)

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        if self.state.status == BotStatus.STOPPED:
            self.state.status = BotStatus.WATCHING
            self.dip_detector.reset()
            self.state.local_low = 0.0

    def stop(self):
        self.state.status = BotStatus.STOPPED

    def update_settings(self, new_settings):
        self.settings.update(new_settings)
        self._reapply_settings()

    def _reapply_settings(self):
        s = self.settings
        self.dip_detector = DipDetector(self.effective_dip())
        self.trailing_stop = TrailingStop(float(s.get('trailing_stop_pct', 0.01)))

    # ---- state machine ---------------------------------------------------
    def on_price(self, price):
        self._in_on_price = True
        try:
            self.state.current_price = price
            # resume paused buying once funds are available again (throttled)
            if self._buy_paused and time.time() - self._buy_pause_check_ts > 30:
                self._buy_pause_check_ts = time.time()
                try:
                    bal = self.client.get_quote_balance(self.settings.get('product_id', ''))
                except Exception:
                    bal = 0.0
                if bal >= self._min_quote(self.settings.get('product_id', '')):
                    self._buy_paused = False
                    self.dip_detector.reset()
                    self._trigger_lowest = None
                    self._log(f'BUY RESUMED — {bal:.2f} {self.client.quote_currency(self.settings.get("product_id", ""))} available, watching for bounce', user=True)
            if self.state.status == BotStatus.WATCHING:
                if not self._buy_paused:
                    # PERIODIC SYNC (60s) — adopt an external position that
                    # appeared while watching (user bought / a resting 3rd-party
                    # limit filled outside the bot). When flat, do nothing and
                    # keep watching for a dip.
                    now = time.time()
                    if now - getattr(self, '_last_watch_sync', 0.0) >= 60.0:
                        self._last_watch_sync = now
                        try:
                            pid = self.settings.get('product_id', '')
                            real_bal = self.client.get_real_base_balance(pid)
                            if real_bal is not None and real_bal > 1e-12:
                                real_trunc = self._inc_trunc(real_bal, pid)
                                inc = self.client.get_base_increment(pid)
                                dp = max(1, -int(__import__('math').log10(inc)))
                                if real_trunc > 1e-12:
                                    pos = Position(
                                        product_id=pid, entry_price=0.0,
                                        size_usdc=0.0, size_base=real_trunc,
                                        entry_time='', order_id='',
                                        highest_price=0.0, stop_price=0.0)
                                    basis = self.client.get_cost_basis(pid, real_trunc)
                                    if basis:
                                        e, b = basis
                                        if e > 0 and b > 0 and abs(b - real_trunc) / real_trunc < 0.15:
                                            pos.entry_price = e
                                            pos.size_usdc = e * real_trunc
                                    self.state.position = pos
                                    self.state.status = BotStatus.HOLDING
                                    if pos.entry_price and pos.entry_price > 0:
                                        # trail arms itself at/above the
                                        # take-profit mark in on_price — do not
                                        # pre-arm on the exchange avg basis.
                                        self.trailing_stop.reset()
                                        pos.stop_price = self._take_profit_mark(pos.entry_price)
                                        self._log(f'SYNC: adopted external position {real_trunc:.{dp}f} {pid.split("-")[0]} at ${pos.entry_price:.2f} (exchange avg)', user=True)
                                    else:
                                        self._log(f'SYNC: adopted external position {real_trunc:.{dp}f} {pid.split("-")[0]} — awaiting first tick for entry price', user=True)
                                    return
                        except Exception:
                            pass
                    if self._trigger_lowest is None or price < self._trigger_lowest:
                        self._trigger_lowest = price
                    if self.dip_detector.update(price):
                        # RE-ARM ON FIRE (2026-08-18): the detector used to
                        # keep its old low after firing, so one bounce
                        # re-triggered every tick (the 'TRIGGER: dip detected'
                        # machine-gun). A new buy now needs a NEW dip+bounce.
                        self.dip_detector.reset()
                        self._trigger_lowest = price
                        # BUY COOLDOWN: a choppy market must not machine-gun
                        # orders — skip triggers inside the cooldown window.
                        if time.time() - self._last_buy_ts < self._buy_cooldown_s():
                            self._log('BUY cooldown — trigger skipped (recent buy)')
                        else:
                            self.state.status = BotStatus.BUYING
                            self.execute_buy()
            elif self.state.status == BotStatus.HOLDING and self.state.position:
                self._trigger_lowest = None
                pos = self.state.position
                # PERIODIC BALANCE CHECK — detect external closes (user sold via
                # Coinbase app etc) AND external additions (user bought more
                # outside the bot). Every 60s, verify the exchange balance.
                now = time.time()
                if now - getattr(self, '_last_balance_check', 0.0) >= 60.0:
                    self._last_balance_check = now
                    try:
                        real_bal = self.client.get_real_base_balance(
                            self.state.position.product_id)
                        if real_bal is not None and real_bal <= 1e-12:
                            self._log('SYNC: exchange balance is 0 — position '
                                      'closed externally, resuming WATCHING',
                                      user=True)
                            self.state.position = None
                            self.state.status = BotStatus.WATCHING
                            self.state.error_message = ''
                            self.trailing_stop.reset()
                            # MUST re-arm the dip detector: it was left armed
                            # from the HOLDING period, so the next tick would
                            # instantly "detect a dip" and re-buy 1s after the
                            # user closed on the phone. Fresh watch = fresh dip
                            # required before any new buy.
                            self.dip_detector.reset()
                            self._trigger_lowest = None
                            runtime_state.save_own_base(0.0)
                            return
                        # COMBINE MODE: adopt an external addition (user bought more outside
                        # the bot). Re-fetch balance + cost basis, update
                        # position size and FEE-EXCLUDED entry. Only re-base
                        # on a GENUINE increase in real balance (not every
                        # fill — the fixed 60s call must not flail the entry).
                        if (real_bal is not None and real_bal > 0
                                and str(self.settings.get('holding_mode') or '')
                                in ('combine', 'auto_invest')):
                            try:
                                pid2 = self.settings.get('product_id', '')
                                real_trunc = self._inc_trunc(real_bal, pid2)
                                if real_trunc - (self.state.position.size_base or 0.0) > 1e-12:
                                    inc = self.client.get_base_increment(pid2)
                                    dp = max(1, -int(__import__('math').log10(inc)))
                                    basis = self.client.get_cost_basis(pid2, real_trunc)
                                    if basis:
                                        e, b = basis
                                        if e > 0 and b > 0 and abs(b - real_trunc) / real_trunc < 0.15:
                                            self.state.position.size_base = real_trunc
                                            self.state.position.entry_price = e
                                            self.state.position.size_usdc = e * real_trunc
                                            self._log(f'SYNC: adopted {real_trunc:.{dp}f} {pid2.split("-")[0]} externally — entry {e:.2f} (exchange avg)', user=True)
                                            # trail re-arms itself at/above the
                                            # take-profit mark in on_price — do
                                            # NOT force-arm on the new basis.
                                            self.trailing_stop.reset()
                            except Exception:
                                pass
                    except Exception:
                        pass
                # CAPITULATION — single-trade stop-loss for a bleeder (default
                # OFF). Only in single mode (DCA averages down and never
                # force-closes at a loss; auto-invest never auto-sells). The
                # editable floor is Trailing Stop % + 1, so it is never a deep
                # loss. At the level, give up and close at market once.
                cap = float(self.settings.get('capitulation_pct', 0.0) or 0.0)
                if (cap > 0 and not self.settings.get('dca', False)
                        and not self._buy_only() and pos.entry_price and pos.entry_price > 0
                        and price <= pos.entry_price * (1 - cap)):
                    if not getattr(self, '_cap_tried', False):
                        self._cap_tried = True
                        self._log(f'CAPITULATION: price ${price:.2f} is {cap*100:.1f}% below entry ${pos.entry_price:.2f} — closing single trade at market', user=True)
                        self.force_close(1.0)
                    return
                if not pos.entry_price or pos.entry_price == 0.0:
                    # fee-excluded entry (match the exchange site) — the true
                    # avg fill/order price, not a fee-inflated number
                    pos.entry_price = price
                    pos.highest_price = price
                    pos.stop_price = price * (1 - float(self.settings.get('trailing_stop_pct', 0.01)))
                    pos.size_usdc = pos.size_base * pos.entry_price
                    self.trailing_stop.reset()
                    self._log(f'SYNC: entry price set from tick ${pos.entry_price:.2f}')
                # TAKE-PROFIT TRAILING GATE (user, 2026-09-11: replace the old
                # fee + trailing% + $0.05 floor — it "sold at the nickel").
                # The bot HOLDS until price reaches the take-profit mark
                # (entry × (1+take_profit_pct)). Below the mark: no sell, no
                # trailing, no re-arm churn — DCA keeps re-buying below entry.
                # At/above the mark the trailing stop ARMS at that price and
                # follows the peak; a trailing_stop_pct pullback closes the
                # trade in profit. Single trades get the same take-profit/trail
                # behavior (auto-invest remains buy-only — execute_sell blocks).
                mark = (self._take_profit_mark(pos.entry_price)
                        if pos.entry_price and pos.entry_price > 0 else 0.0)
                below_mark = bool(pos.entry_price and pos.entry_price > 0
                                  and price < mark)
                if not self.trailing_stop.active:
                    if below_mark:
                        # HOLD below the mark — the mark is the target; never
                        # evaluate or re-base a trail down here.
                        should_sell = False
                        pos.stop_price = mark
                        self.trailing_stop.reset()
                    else:
                        # Mark reached — ARM the trailing stop AT this price.
                        # From here it follows any climb (peak) and fires on a
                        # trailing% pullback, even if price dips back under the
                        # mark (the stop is still above entry -> a profit close).
                        pos.highest_price = price
                        self.trailing_stop.activate(price)
                        pos.stop_price = self.trailing_stop.get_stop_price()
                        should_sell = self.trailing_stop.update(price)
                        self._log(f'SYNC: take-profit mark targeted, trailing armed @ ${price:.2f}', user=True)
                else:
                    # Trail already armed — keep following the peak. Still sells
                    # on a trailing% pullback even below the mark (profit close).
                    should_sell = self.trailing_stop.update(price)
                    pos.highest_price = self.trailing_stop.highest_price
                    pos.stop_price = self.trailing_stop.get_stop_price()
                cpct = ((price - pos.entry_price) / pos.entry_price) if pos.entry_price else 0.0
                pos.pnl_pct = cpct
                pos.pnl = cpct * pos.size_usdc
                if should_sell:
                    self.state.status = BotStatus.SELLING
                    self.execute_sell()
            # DCA bounce re-buy while HOLDING at a loss; stops when price
            # recovers to the (fee-adjusted) entry, and re-arms whether or not
            # the last order filled. HOLDING gate (BUG-002): a STOPPED bot must
            # never trade, and _exec_dca_buy flips status to HOLDING, so without
            # this gate Stop would be ignored and the bot would un-stop itself.
            # Re-buy while HOLDING: DCA (entry-capped, averages back to entry)
            # OR auto-invest (buy-only) — which has NO entry/floor caps: pure
            # dip accumulation, buys every bounce below the floor, exit only via
            # Force Close (user, 2026-08-10). HOLDING gate (BUG-002): a STOPPED
            # bot must never trade, and _exec_dca_buy flips status to HOLDING, so
            # without this gate Stop would be ignored and the bot would un-stop.
            if ((self.settings.get('dca', False) or self._buy_only())
                    and self.state and self.state.status == BotStatus.HOLDING
                    and self.state.position):
                entry = self.state.position.entry_price or 0.0
                capped = self.settings.get('dca', False) and not self._buy_only()
                cap_hit = False
                if capped and entry > 0:
                    # Entry is the fee-excluded average (matches the exchange
                    # site). DCA stops buying when price is ABOVE entry — buying
                    # above the raw average only raises the average ("stop
                    # buying at entry + fee" intent, now expressed against the
                    # fee-excluded entry directly).
                    if price > entry:
                        cap_hit = True
                        self._dca_bounce_low = None
                        # Cancel any resting DCA limit orders that would fill
                        # above entry — a resting limit can sit on the exchange
                        # and fill when price rises, bypassing the entry cap.
                        for p in list(self._pending):
                            if p.get('kind') in ('buy', 'dca'):
                                try:
                                    self.client.cancel_order(p['order_id'])
                                    self._log(f'DCA entry cap: cancelled resting {p["kind"]} limit '
                                              f'(order {p["order_id"]}) — price at/above entry', user=True)
                                except Exception:
                                    pass
                                self._pending_remove(p['order_id'])
                if not cap_hit and not self._dca_paused and not self._buy_paused:
                    if self._dca_bounce_low is None or price < self._dca_bounce_low:
                        self._dca_bounce_low = price
                    dip = self.effective_dip()
                    if price >= self._dca_bounce_low * (1 + dip):
                        # BUY COOLDOWN: shared throttle with the main buy
                        if time.time() - self._last_buy_ts < self._buy_cooldown_s():
                            pass  # keep the bounce low; re-arms after cooldown
                        else:
                            self._exec_dca_buy()
                elif time.time() - self._dca_pause_check_ts > 30:
                    self._dca_pause_check_ts = time.time()
                    if self._dca_paused:
                        try:
                            bal = self.client.get_quote_balance(self.state.position.product_id)
                        except Exception:
                            bal = 0.0
                        if bal >= self._min_quote(self.state.position.product_id):
                            self._dca_paused = False
                            self._dca_bal_fail_count = 0
                            self._dca_bounce_low = None
                            self._log(f'DCA: RESUMED — {bal:.2f} {self.client.quote_currency(self.state.position.product_id)} available, watching for bounce', user=True)
            # safety net: SELLING at/below a loss -> hold (never take a loss)
            if self.state.status == BotStatus.SELLING and self.state and self.state.position:
                entry = self.state.position.entry_price or 0.0
                if entry > 0:
                    mark = self._take_profit_mark(entry)
                    # No-loss gate = ENTRY (the trail only armed at/above the
                    # take-profit mark, so a fill below entry is the only real
                    # loss to block). A resting limit above entry may still fill
                    # at a profit and is kept unless the user cancels below floor.
                    if price < entry:
                        # A resting LIMIT SELL: cancel it if it would fill at a
                        # loss (cancel_sell_below_floor). If disabled, it is KEPT
                        # (its price is >= its own limit, still no-loss) unless
                        # it now sits below entry.
                        pending_sell = self._pending_find('sell') is not None
                        keep_limit = bool(pending_sell
                                          and not self.settings.get('cancel_sell_below_floor', True))
                        if pending_sell and not keep_limit:
                            _psell = self._pending_find('sell')
                            if _psell:
                                try:
                                    self.client.cancel_order(_psell['order_id'])
                                except Exception:
                                    pass
                                self._pending_remove(_psell['order_id'])
                            self._log(f'LIMIT SELL cancelled below entry (${price:.2f} < ${entry:.2f})', user=True)
                        if not keep_limit:
                            self._log(f'SELL: at loss (${price:.2f} < ${entry:.2f}), holding for better exit')
                            if self.settings.get('dca', False) and not self._dca_paused:
                                self._dca_bounce_low = price
                            self.state.status = BotStatus.HOLDING
                            self.trailing_stop.reset()
                            # re-arm the trail ONLY back at the take-profit mark
                            self.state.position.highest_price = mark
                            self.state.position.stop_price = mark
        finally:
            self._in_on_price = False

    # ---- buy -------------------------------------------------------------
    def execute_buy(self, force_market=False):
        self._log('TRIGGER: dip detected, executing buy')
        # record the attempt — the on_price trigger paths enforce the cooldown
        # (resolution fallbacks may call this directly and are never gated)
        self._last_buy_ts = time.time()
        # BUG-007 + "show must go on": while the held position can't be
        # verified (balance read failing), NEVER buy on top of an unknown
        # holding. The bot stays RUNNING but gated — a background sync
        # auto-verifies and clears this; it never stops waiting for a user.
        if getattr(self, '_position_unknown', False):
            # NEVER wipe a held position (2026-08-18 incident: the pause paths
            # set position=None while the exchange still held coins, so the
            # dashboard showed WATCHING/flat with a live holding).
            if self.state.position is None:
                self.state.status = BotStatus.WATCHING
                self.state.local_low = 0.0
                self.dip_detector.reset()
            self._log('BUY held — position unverified (balance read failing), auto-retrying', user=True)
            return
        product_id = self.settings.get('product_id', '')
        # low-funds guard: pause instead of failing an order. The bot stays
        # active so an open position's exit (trailing stop) is still managed.
        try:
            bal = self.client.get_quote_balance(product_id)
        except Exception:
            bal = 0.0
        min_q = self._min_quote(product_id)
        if bal < min_q:
            self._buy_paused = True
            self._buy_pause_check_ts = time.time()
            # NEVER wipe the position here either — the exit keeps managing.
            if self.state.position is None:
                self.state.status = BotStatus.WATCHING
                self.state.local_low = 0.0
                self.dip_detector.reset()
            self._log(f'BUY PAUSED — {bal:.2f} {self.client.quote_currency(product_id)} below {min_q:.2f} minimum, waiting for funds to resume', user=True)
            return
        # Effective trade-size fraction for THIS buy. mode 'pct' uses the user's
        # setting directly; 'fixed'/'coin' compute a one-off size_pct from the
        # current balance so we NEVER mutate the user's trade_size_pct setting.
        # (Previously a transient fixed/bal value was written into it and leaked
        # into the UI as a false "% of balance" preference — e.g. 46% after a
        # fixed buy while the user's setting stayed 1%.)
        size_pct = self.settings.get('trade_size_pct', 0.5)
        try:
            mode = self.settings.get('trade_size_mode', 'pct')
            bal = self.client.get_quote_balance(product_id)
            if mode == 'fixed':
                fixed = float(self.settings.get('trade_size_fixed', 10.0))
                if fixed < min_q:
                    fixed = min_q
                if bal > 0:
                    # never order more than the account actually holds —
                    # "Fixed $100 with a $10 balance buys $10 worth" (no reject)
                    if fixed > bal:
                        fixed = bal
                    size_pct = fixed / bal
            elif mode == 'coin':
                coins = float(self.settings.get('trade_size_coin', 1.0))
                price = self.client.get_best_ask(product_id)
                if price > 0 and bal > 0:
                    max_coins = self._inc_trunc(bal / price, product_id)
                    if coins > max_coins:
                        coins = max_coins
                    quote = coins * price
                    if quote < min_q:
                        coins = self._inc_trunc(min_q / price, product_id)
                        quote = coins * price
                    size_pct = quote / bal
        except Exception:
            pass
        self.settings['maker_retries'] = 0
        try:
            self.state.status = BotStatus.BUYING
            usdc = self.client.get_quote_balance(product_id)
            self.state.usdc_balance = usdc
            desired_quote = usdc * size_pct
            price = self.client.get_best_ask(product_id)
            # BUY in BASE units, truncated DOWN to the base increment — never
            # rounded up. Rounding up a buy can exceed the account (lack-of-
            # funds flag) and can produce base dust that can never be sold
            # (stranded position, auto-rearm waits forever). A truncated base
            # is exactly sellable (same increment the sell uses).
            base = 0.0
            if price and price > 0:
                base = self._inc_trunc(desired_quote / price, product_id)
                # minimum: a CLEAN base amount whose quote value >= the exchange
                # minimum (so it is both allowed AND fully sellable)
                if base * price < min_q:
                    base = self._inc_ceil(min_q / price, product_id)
            if base <= 0:
                self._buy_paused = True
                self._buy_pause_check_ts = time.time()
                if self.state.position is None:
                    self.state.status = BotStatus.WATCHING
                    self.state.local_low = 0.0
                    self.dip_detector.reset()
                self._log(f'BUY PAUSED — cannot size a clean order (price unavailable)', user=True)
                return
            # LIMIT BUY path: post_only maker at the dip-bounce trigger price
            # (low * (1 + effective dip)). It fills only if price pulls back
            # there; the order is polled by poll_open_orders. Never holds the
            # watching hostage: timeout resolves to fallback / keep-open.
            if self.settings.get('limit_order_buy', False) and not force_market:
                if self._place_limit_buy(product_id, usdc, size_pct, min_q):
                    return
            inc = self.client.get_base_increment(product_id)
            dp = max(1, -int(math.log10(inc)))
            base_size = f"{base:.{dp}f}"
            quote_size = f"{base * price:.2f}"
            self.order_id = generate_order_id()
            resp = self.client.create_market_order(product_id, 'BUY', base_size, self.order_id)
            bought_price = float(resp.get('average_filled_price') or 0) or self.client.get_best_bid(product_id)
            filled_size = float(resp.get('filled_size') or 0)
            if filled_size <= 0 and bought_price:
                filled_size = float(quote_size) / bought_price
            self.state.position = Position(
                product_id=product_id, entry_price=bought_price, size_usdc=float(quote_size),
                size_base=filled_size, entry_time=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                order_id=self.order_id, highest_price=bought_price, stop_price=0.0)
            # Under take-profit, DO NOT arm the trailing stop at the buy price —
            # it must stay disarmed (HOLD, DCA re-buys) until price reaches the
            # take-profit mark. The on_price gate arms it there.
            self.trailing_stop.reset()
            self.state.status = BotStatus.HOLDING
            self._trade_closed_ok = False
            self._cap_tried = False
            # entry is the FEE-EXCLUDED average (match the exchange's site
            # display; the site's number already reflects the true cost).
            self.state.position.entry_price = bought_price
            # Journal-only write (BUG-006): a failure here must NOT mark a real
            # buy as failed — the position state above is already set and the
            # exchange filled the order. own_base tracks how much the bot
            # bought; the exchange is the source of truth for cost basis.
            try:
                self._update_own_base(filled_size)
            except Exception:
                self._log('WARN: own_base write failed — position tracked in memory/API only')
        except Exception as e:
            # THE SHOW MUST GO ON: a failed buy never stops the bot. Count it
            # (for the report), recover to WATCHING, and let the next dip try
            # again — unattended use has no one to press Start.
            self._buy_fail_count += 1
            self._recover_to_watching('buy', f'buy failed ({self._buy_fail_count}x): {e}')
            return
        # phantom-holding fix: bought but no position -> treat as not filled,
        # recover to watching (never stop the bot on a fill ambiguity)
        if self.state.status == BotStatus.HOLDING and self.state.position is None:
            self._buy_fail_count += 1
            self._recover_to_watching('buy', f'buy did not fill ({self._buy_fail_count}x)')
        # sync position with real exchange balance (fee-adjusted entry)
        if self.state.status == BotStatus.HOLDING and self.state.position is not None:
            self._buy_fail_count = 0
            p = self.state.position
            orig_usdc = p.size_usdc
            real = self.client.get_real_base_balance(product_id)
            if real and real > 0:
                p.size_base = real
            elif p.entry_price and p.entry_price > 0:
                min_qs = self._min_quote(product_id)
                boosted = max(orig_usdc, min_qs)
                if boosted > orig_usdc:
                    p.size_base = boosted / p.entry_price
            if p.size_base and p.size_base > 0:
                p.size_base = self._inc_trunc(p.size_base, product_id)
            if p.size_base and p.entry_price:
                p.size_usdc = p.size_base * p.entry_price
            self.state.error_message = ''
            inc = self.client.get_base_increment(product_id)
            dp = max(1, -int(math.log10(inc)))
            self._log(f'BUY: {p.size_base:.{dp}f} @ ${p.entry_price:.2f} (${p.size_usdc:.2f})', user=True)

    # ---- DCA -------------------------------------------------------------
    def _buy_cooldown_s(self):
        return max(0.0, float(self.settings.get('buy_cooldown_s', 60.0)))

    def _exec_dca_buy(self, force_market=False):
        if not self.state or not self.state.position:
            return
        # record the attempt — the on_price bounce path enforces the cooldown
        self._last_buy_ts = time.time()
        # Main buy paused (balance < minimum) — DCA must also pause
        if self._buy_paused:
            return
        try:
            entry = self.state.position.entry_price or 0.0
            # AUTO-INVEST (buy-only): NO entry cap — pure dip accumulation
            # (keeps averaging down on every bounce; user, 2026-08-10). Only
            # DCA mode stops buying once price recovers to the entry.
            if entry > 0 and not self._buy_only():
                curr = self.client.get_best_ask(self.state.position.product_id)
                if curr > entry:
                    self._dca_bounce_low = None
                    self._log(f'DCA: price ${curr} >= entry ${entry:.2f}, skipping buy — waiting for sell')
                    return
            pid = self.state.position.product_id
            bal = self.client.get_quote_balance(pid)
            if bal <= 0:
                self._dca_bal_fail_count += 1
                if self._dca_bal_fail_count >= 2:
                    self._dca_paused = True
                    self._dca_pause_check_ts = time.time()
                    self._dca_bounce_low = None
                    self._log(f'DCA: PAUSED — {self.client.quote_currency(pid)} balance depleted ({self._dca_bal_fail_count} consecutive failures)', user=True)
                    price = self.client.get_best_ask(pid)
                    entry = self.state.position.entry_price or 0.0
                    tf = self.client.get_trading_fee()
                    if entry > 0 and price >= entry:
                        self._log('DCA: price at/above entry while paused, letting sell proceed')
                else:
                    self._log(f'DCA: balance read returned 0 (attempt {self._dca_bal_fail_count}/2 — not pausing yet)')
                return
            # Balance OK — reset failure counter
            self._dca_bal_fail_count = 0
            if self._dca_paused:
                self._dca_pause_check_ts = time.time()
                self._dca_bounce_low = None
                return
            min_q = self._min_quote(pid)
            mode = self.settings.get('trade_size_mode', 'pct')
            price = self.client.get_best_ask(pid)
            if mode == 'pct':
                trade_usdc = bal * float(self.settings.get('trade_size_pct', 0.5))
            elif mode == 'fixed':
                trade_usdc = float(self.settings.get('trade_size_fixed', 10.0))
            else:
                trade_usdc = float(self.settings.get('trade_size_coin', 1.0)) * price
            trade_usdc = max(trade_usdc, min_q)
            trade_usdc = min(trade_usdc, bal)
            if trade_usdc < min_q:
                self._dca_paused = True
                self._dca_pause_check_ts = time.time()
                self._dca_bounce_low = None
                self._log(f'DCA: PAUSED — {bal:.2f} {self.client.quote_currency(pid)} below {min_q:.2f} minimum', user=True)
                return
            oid = secrets.token_hex(8)
            tf = self.client.get_trading_fee()
            price = self.client.get_best_ask(pid)
            if not price or price <= 0:
                self._dca_bounce_low = None
                self._log('DCA: ERROR — no price, re-arming watch', user=True)
                return
            dca_base = self._inc_trunc(trade_usdc / price, pid)
            if dca_base * price < min_q:
                dca_base = self._inc_ceil(min_q / price, pid)
            if dca_base <= 0:
                self._dca_bounce_low = None
                self._log('DCA: ERROR — cannot size clean order, re-arming watch', user=True)
                return
            inc2 = self.client.get_base_increment(pid)
            dp2 = max(1, -int(math.log10(inc2)))
            # DCA limit path: post_only maker at the DCA trigger price, polled
            # like the main buy. Works whether DCA is on or single-shot (DCA
            # off still uses the main buy path above). Never holds the watch.
            if self.settings.get('limit_order_buy', False) and not force_market:
                if self._place_limit_dca(pid, trade_usdc, min_q):
                    return
            self._log('DCA: position still at loss, buying more', user=True)
            self.client.create_market_order(pid, 'BUY', f"{dca_base:.{dp2}f}", oid)
            trade_usdc = dca_base * price
            self._log(f'DCA: bought ${trade_usdc:.2f} worth (fee ~${trade_usdc * tf:.2f})', user=True)
            old_base = self.state.position.size_base or 0.0
            old_usdc = self.state.position.size_usdc or (old_base * self.state.position.entry_price if self.state.position.entry_price else 0.0)
            new_base = trade_usdc * (1 - tf) / price
            total_base = old_base + new_base
            total_usdc = old_usdc + trade_usdc
            avg = total_usdc / total_base if total_base else price
            self.state.position.entry_price = avg
            self.state.position.size_base = self._inc_trunc(total_base, pid)
            self.state.position.size_usdc = total_usdc
            self.state.position.highest_price = price
            self.state.position.stop_price = price * (1 - float(self.settings.get('trailing_stop_pct', 0.01)))
            self.state.position.entry_time = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            # DCA buys are BELOW entry — trail stays disarmed (take-profit) and
            # arms itself at the mark in on_price's HOLDING gate.
            self.trailing_stop.reset()
            self.state.status = BotStatus.HOLDING
            self._trade_closed_ok = False
            self._dca_bounce_low = None
            try:
                self._update_own_base(new_base)
            except Exception:
                pass
            self._log(f'DCA: avg entry ${avg:.2f}, total {total_base:.8f} {pid.split("-")[0]}', user=True)
        except Exception as e:
            self._log(f'DCA: ERROR — {_clean_error(e)} (re-arming watch)', user=True)
            self._dca_bounce_low = None

    # ---- sell ------------------------------------------------------------
    def _buy_only(self):
        """Auto-invest takeover mode: never auto-sells — buys dips and exits
        only via Force Close. The user sets a % of holdings to close."""
        return self.settings.get('holding_mode', '') == 'auto_invest'

    def execute_sell(self, force_market=False):
        self._log('TRIGGER: trailing stop hit, executing sell')
        # SELL FAILURE COOLDOWN: after a failed sell, wait 60 seconds before
        # retrying. Prevents the tight retry loop (limit rejected → market
        # rejected → next tick retries → spam every 1.5s).
        if time.time() < self._sell_cooldown_until:
            return
        # AUTO-INVEST (buy-only): the bot never auto-sells — exit only via
        # Force Close. Keep the position, re-arm, keep accumulating.
        if self._buy_only():
            self._log('AUTO-INVEST: auto-sell held — exit only via Force Close', user=True)
            if self.state and self.state.position:
                self.state.status = BotStatus.HOLDING
                self.trailing_stop.reset()
                self.trailing_stop.activate(self.state.current_price
                                            or self.state.position.entry_price or 0.0)
            return
        # NOTE: deferred settings are NOT applied here — applying them mid-sell
        # could switch the price feed to a different pair and misprice this
        # position's stop/sell. They are applied by the pending-check timer
        # after the trade fully completes (position is None).
        # Defensive guard (BUG-009): never deref a None position if sell is
        # somehow reached without one (currently unreachable via on_price).
        if not self.state or not self.state.position:
            self._log('SELL: no position to sell — ignoring')
            return
        # a pending buy/DCA limit must not orphan on the exchange — cancel it,
        # or keep it resting for the next cycle (keep-open)
        self._manage_parked_before_sell()
        product_id = self.settings.get('product_id', '')
        real_bal = self.client.get_real_base_balance(product_id)
        # GHOST POSITION FIX: if the exchange reports 0 balance, the position
        # was already sold/withdrawn — clear it and stop retrying.
        if not real_bal or real_bal <= 0:
            self._log('SELL: exchange reports 0 balance — clearing ghost position', user=True)
            self.state.position = None
            self.state.status = BotStatus.WATCHING
            self._trade_closed_ok = True
            self._cancel_pending_buys(keep_keep_open=bool(self.settings.get('auto_rearm', True)))
            return
        if self.state and self.state.position:
            self.state.position.size_base = self._inc_trunc(real_bal, self.state.position.product_id)
            inc = self.client.get_base_increment(self.state.position.product_id)
            dp = max(1, -int(math.log10(inc)))
            self._log(f'SELL: fetched real balance={real_bal:.{dp}f}')
        if self.state.position and (not self.state.position.entry_price or self.state.position.entry_price == 0.0):
            self._log('SELL: skipping — entry_price not set yet (waiting for first tick)')
            return
        # never-take-a-loss: at/near a loss -> hold and re-arm.
        # The sell gate is ENTRY, not the take-profit mark: a trailing stop may
        # fire just below the mark (peak × (1-trail)) yet still be a profit —
        # that close must NOT be blocked. Only a fill below ENTRY is a loss.
        if self.state and self.state.position and self._in_on_price:
            entry = self.state.position.entry_price or 0.0
            if entry > 0:
                price = self.state.current_price or 0.0
                if price < entry:
                    self._log(f'SELL: at loss (${price:.2f} < ${entry:.2f}), holding for better exit')
                    self.state.status = BotStatus.HOLDING
                    if self.settings.get('dca', False) and not self._dca_paused:
                        if self._dca_bounce_low is None or price < self._dca_bounce_low:
                            self._dca_bounce_low = price
                    self.trailing_stop.reset()
                    return
        # LIMIT SELL path: post_only maker at the best bid — only attempted when
        # the bid is at/above the never-take-a-loss floor (a maker sell below
        # the floor would be a loss). Polled; timeout/partial resolved by
        # poll_open_orders.
        if self.settings.get('limit_order_sell', False) and not force_market:
            if self._place_limit_sell(self.state.position):
                return
        self.settings['maker_retries'] = 0
        sold = False
        try:
            self.state.status = BotStatus.SELLING
            pos = self.state.position
            inc = self.client.get_base_increment(pos.product_id)
            dp = max(1, -int(math.log10(inc)))
            self.order_id = generate_order_id()
            resp = self.client.create_market_order(pos.product_id, 'SELL',
                                                   f"{pos.size_base:.{dp}f}", self.order_id)
            exit_price = float(resp.get('average_filled_price') or 0) or self.client.get_best_ask(pos.product_id)
            # net P&L including the sell fee, vs the fee-adjusted cost basis
            # (entry already includes the buy fee) — matches the exchange
            cost = (pos.size_usdc if pos.size_usdc and pos.size_usdc > 0
                    else pos.entry_price * pos.size_base)
            tf = self.client.get_trading_fee()
            gross = exit_price * pos.size_base
            # full-cycle realized P&L = remainder close + any earlier partial
            # limit-sell realized (cancelled below floor) — booked once here
            pnl = gross * (1 - tf) - cost + (getattr(pos, 'partial_pnl', 0.0) or 0.0)
            pnl_pct = (pnl / cost) if cost and cost > 0 else 0.0
            # Position closed — reset own_base (exchange is source of truth).
            try:
                self._update_own_base(-pos.size_base)
            except Exception:
                pass
            self.state.total_pnl += pnl
            self.state.trade_count += 1
            self.state.position = None
            if self.settings.get('auto_rearm', True):
                self.state.status = BotStatus.WATCHING
                self.dip_detector.reset()
                self.state.local_low = 0.0
            else:
                self.state.status = BotStatus.STOPPED
            sold = True
        except Exception as e:
            prev = BotStatus.HOLDING if self.state.position else BotStatus.WATCHING
            self.state.status = prev
            self.state.error_message = f'Sell failed: {e}'
            self._sell_cooldown_until = time.time() + 60
            self._maybe_report_error('sell', f'sell failed: {e}')
            self._log(f'ERROR: sell failed - {_clean_error(e)} (retry in 60s)', user=True)
        if sold:
            self._log('SELL: position closed successfully', user=True)
            self._trade_closed_ok = True
            self._trigger_lowest = None
            self._dca_bounce_low = None
            self._dca_paused = False
            # successful close: cancel unfilled buys (keep keep-open ones if
            # rearming — they belong to the next cycle)
            self._cancel_pending_buys(keep_keep_open=bool(self.settings.get('auto_rearm', True)))

    def force_close(self, fraction=1.0):
        """SELL a `fraction` (0,1] of the position at MARKET — the ONLY sell in
        auto-invest mode (Trim), and the emergency eject in all other modes.
        fraction 1.0 = full close; <1.0 = sell that portion (auto-invest Trim).
        Returns True on a sale."""
        pos = self.state.position
        if pos is None:
            self._log('FORCE CLOSE: no position to close', user=True)
            return False
        try:
            pid = pos.product_id
            inc = self.client.get_base_increment(pid)
            dp = max(1, -int(math.log10(inc)))
            sell_size = self._inc_trunc(pos.size_base * max(0.0, min(1.0, fraction)), pid)
            if sell_size <= 0:
                self._log('FORCE CLOSE: nothing to sell', user=True)
                return False
            oid = generate_order_id()
            resp = self.client.create_market_order(pid, 'SELL', f"{sell_size:.{dp}f}", oid)
            exit_price = float(resp.get('average_filled_price') or 0) or self.client.get_best_bid(pid)
            tf = self.client.get_trading_fee()
            if sell_size >= pos.size_base - 1e-12:
                # full close
                cost = (pos.size_usdc if pos.size_usdc and pos.size_usdc > 0
                        else pos.entry_price * pos.size_base)
                gross = exit_price * pos.size_base
                pnl = gross * (1 - tf) - cost + (getattr(pos, 'partial_pnl', 0.0) or 0.0)
                pnl_pct = (pnl / cost) if cost and cost > 0 else 0.0
                try:
                    self._update_own_base(-pos.size_base)
                except Exception:
                    pass
                self.state.total_pnl += pnl
                self.state.trade_count += 1
                self.state.position = None
                # A FORCED close at a LOSS must NOT auto-rearm into another
                # possible loss — stop and let the user decide (user 2026-08-09).
                if pnl < 0:
                    self.state.status = BotStatus.STOPPED
                    self._log('FORCE CLOSE: closed at a loss — bot stopped, will NOT auto-rearm into another loss. Press Start to trade again.', user=True)
                elif self.settings.get('auto_rearm', True):
                    self.state.status = BotStatus.WATCHING
                    self.dip_detector.reset()
                    self.state.local_low = 0.0
                else:
                    self.state.status = BotStatus.STOPPED
                self._trade_closed_ok = True
                self._trigger_lowest = None
                self._dca_bounce_low = None
                self._dca_paused = False
                self._cancel_pending_buys(keep_keep_open=bool(self.settings.get('auto_rearm', True)))
                self._log(f'FORCE CLOSE: sold {sell_size:.{dp}f} @ ${exit_price:.2f} — position closed', user=True)
                return True
            # partial close: book this portion's realized P&L on the position
            # (folded into the final close — never double-counted), keep the rest
            partial_pnl = sell_size * exit_price * (1 - tf) - sell_size * (pos.entry_price or 0)
            pos.partial_pnl = (getattr(pos, 'partial_pnl', 0.0) or 0.0) + partial_pnl
            pos.size_base = self._inc_trunc(pos.size_base - sell_size, pid)
            pos.size_usdc = pos.size_base * (pos.entry_price or 0)
            try:
                self._update_own_base(-sell_size)
            except Exception:
                pass
            self.state.status = BotStatus.HOLDING
            self.trailing_stop.reset()
            self.trailing_stop.activate(exit_price)
            self._log(f'FORCE CLOSE: sold {sell_size:.{dp}f} @ ${exit_price:.2f} — kept {pos.size_base:.{dp}f} accumulating', user=True)
            return True
        except Exception as e:
            self._log(f'FORCE CLOSE failed ({_clean_error(e)}) — position kept', user=True)
            return False

    # ---- limit orders (post_only maker) + fallback to taker --------------
    def _limit_timeout_s(self):
        try:
            return max(1.0, float(self.settings.get('maker_timeout_ms', 5000)) / 1000.0)
        except Exception:
            return 5.0

    # ---- pending-order persistence ----------------------------------------
    def _pending_path(self):
        return os.path.join(paths.CONFIG_DIR, 'pending_orders.json')

    def _load_parked(self):
        try:
            with open(self._pending_path()) as f:
                data = json.load(f)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict) and d.get('order_id')]
        except Exception:
            pass
        return []

    def _persist_parked(self):
        try:
            os.makedirs(paths.CONFIG_DIR, exist_ok=True)
            with open(self._pending_path(), 'w') as f:
                json.dump(self._pending, f)
        except Exception:
            pass

    def _pending_add(self, ol):
        self._pending = [p for p in self._pending if p.get('order_id') != ol.get('order_id')]
        self._pending.append(ol)
        self._persist_parked()

    def _pending_remove(self, order_id):
        before = len(self._pending)
        self._pending = [p for p in self._pending if p.get('order_id') != order_id]
        if len(self._pending) != before:
            self._persist_parked()

    def _pending_find(self, kind):
        for p in self._pending:
            if p.get('kind') == kind:
                return p
        return None

    def _replace_resting_buys(self):
        """ONE resting (non-keep-open) buy/dca limit at a time: cancel + drop
        any existing ones. Returns True when it is safe to place a new order
        (none existed, or every cancel was CONFIRMED). False = an old order
        may still be live — do not stack a new one (2026-08-18 incident)."""
        safe = True
        for p in list(self._pending):
            if p.get('kind') not in ('buy', 'dca') or p.get('keep_open'):
                continue
            try:
                dead = bool(self.client.cancel_order(p['order_id']))
            except Exception:
                dead = False
            if dead:
                self._pending_remove(p['order_id'])
            else:
                safe = False
        return safe

    def _cancel_pending_buys(self, keep_keep_open=True):
        """Cancel unfilled buy/DCA limits. keep_keep_open=False cancels ALL of
        them (successful close while NOT auto-rearming); True keeps keep-open
        orders resting for the NEXT cycle."""
        for p in list(self._pending):
            if p.get('kind') in ('buy', 'dca'):
                if keep_keep_open and p.get('keep_open'):
                    continue
                try:
                    self.client.cancel_order(p['order_id'])
                except Exception:
                    pass
                self._pending_remove(p['order_id'])
                self._log(f'cancelled unfilled {p["kind"]} limit {p["order_id"]}', user=True)

    def _place_limit_buy(self, product_id, quote_bal, size_pct, min_q):
        """Place a post_only limit BUY at the dip-bounce trigger price
        (low * (1 + effective dip)). The bot KEEPS WATCHING — an unfilled limit
        never stops it from making another buy trigger ("the show must go on").
        Returns True if placed (execute_buy should return); False to proceed to
        the taker order (fallback after 5 maker rejections)."""
        max_maker = 5
        try:
            low = getattr(self.dip_detector, 'local_low', None)
            dip = self.effective_dip()
            limit_price = (low * (1 + dip)) if (low and low > 0) else 0.0
            if not limit_price or limit_price <= 0:
                limit_price = self.client.get_best_ask(product_id)
            if not limit_price or limit_price <= 0:
                self.state.status = BotStatus.WATCHING
                self._log('LIMIT BUY: no price — waiting for the next dip', user=True)
                return True
            base = self._inc_trunc(quote_bal * size_pct / limit_price, product_id)
            if base * limit_price < min_q:
                base = self._inc_ceil(min_q / limit_price, product_id)
            if base <= 0:
                self.state.status = BotStatus.WATCHING
                self._log('LIMIT BUY: cannot size a clean order — waiting', user=True)
                return True
            inc = self.client.get_base_increment(product_id)
            dp = max(1, -int(math.log10(inc)))
            price_inc = self.client.get_price_increment(product_id)
            pdp = max(1, -int(math.log10(price_inc)))
            # ONE resting buy at a time (2026-08-18): cancel any existing
            # non-keep-open buy/dca limit before placing a new one — replace,
            # never stack. If the old order's cancel is unconfirmed, do NOT
            # place (it may still be live).
            if not self._replace_resting_buys():
                self._log('LIMIT BUY: existing order not confirmed dead — not stacking a new one')
                self.state.status = BotStatus.WATCHING
                return True
            # MAKER RETRIES: try post_only up to 5 times. Each rejection
            # waits the exchange's 5s cooldown before retrying.
            retries = self.settings.get('maker_retries', 0)
            for attempt in range(retries, max_maker):
                oid = generate_order_id()
                try:
                    resp = self.client.create_limit_order(product_id, 'BUY',
                                                          f"{base:.{dp}f}", f"{limit_price:.{pdp}f}",
                                                          post_only=True, client_order_id=oid)
                    self._pending_add({
                        'kind': 'buy', 'order_id': resp['order_id'],
                        'client_order_id': oid,
                        'limit_price': limit_price, 'placed': time.time(), 'size': base,
                        'fallback': bool(self.settings.get('fallback_to_taker_buy', True)),
                        'keep_open': bool(self.settings.get('keep_open_buy_for_dca', False)),
                    })
                    self.state.status = BotStatus.WATCHING
                    self._log(f'LIMIT BUY placed @ ${limit_price:.2f} — keep watching, fallback in {self._limit_timeout_s():.0f}s', user=True)
                    self.settings['maker_retries'] = 0
                    return True
                except Exception as e:
                    self.settings['maker_retries'] = attempt + 1
                    if attempt + 1 < max_maker:
                        self._log(f'LIMIT BUY maker rejected ({attempt + 1}/{max_maker}) — retrying in 5s ({_clean_error(e)})')
                        time.sleep(5)
                    else:
                        self._log(f'LIMIT BUY: maker rejected {max_maker}x — switching to taker ({_clean_error(e)})')
            # TAKER FALLBACK: post_only=False after 5 maker rejections
            oid = generate_order_id()
            resp = self.client.create_limit_order(product_id, 'BUY',
                                                  f"{base:.{dp}f}", f"{limit_price:.{pdp}f}",
                                                  post_only=False, client_order_id=oid)
            self._pending_add({
                'kind': 'buy', 'order_id': resp['order_id'],
                'client_order_id': oid,
                'limit_price': limit_price, 'placed': time.time(), 'size': base,
                'fallback': False,
                'keep_open': bool(self.settings.get('keep_open_buy_for_dca', False)),
            })
            self.state.status = BotStatus.WATCHING
            self._log(f'LIMIT BUY taker placed @ ${limit_price:.2f} — fill or timeout in {self._limit_timeout_s():.0f}s', user=True)
            self.settings['maker_retries'] = 0
            return True
        except Exception as e:
            self.settings['maker_retries'] = 0
            if self.settings.get('fallback_to_taker_buy', True):
                self._log(f'LIMIT BUY rejected — no fallback, waiting for the next dip ({_clean_error(e)})', user=True)
            else:
                self._log(f'LIMIT BUY rejected — no fallback, waiting for the next dip ({_clean_error(e)})', user=True)
            self.state.status = BotStatus.WATCHING
            return True

    def _place_limit_dca(self, pid, trade_usdc, min_q):
        """Place a post_only limit BUY for the DCA at its trigger price
        (_dca_bounce_low * (1 + effective dip)). The DCA watch continues — more
        DCA triggers can place more. Returns True if placed; False to proceed to
        the taker DCA (fallback after 5 maker rejections)."""
        max_maker = 5
        try:
            low = self._dca_bounce_low
            dip = self.effective_dip()
            limit_price = (low * (1 + dip)) if (low and low > 0) else self.client.get_best_ask(pid)
            if not limit_price or limit_price <= 0:
                self._dca_bounce_low = None
                self._log('DCA: ERROR — no price, re-arming watch', user=True)
                return True
            dca_base = self._inc_trunc(trade_usdc / limit_price, pid)
            if dca_base * limit_price < min_q:
                dca_base = self._inc_ceil(min_q / limit_price, pid)
            if dca_base <= 0:
                self._dca_bounce_low = None
                self._log('DCA: ERROR — cannot size clean order, re-arming watch', user=True)
                return True
            inc2 = self.client.get_base_increment(pid)
            dp2 = max(1, -int(math.log10(inc2)))
            price_inc2 = self.client.get_price_increment(pid)
            pdp2 = max(1, -int(math.log10(price_inc2)))
            # ONE resting buy at a time — replace, never stack (2026-08-18)
            if not self._replace_resting_buys():
                self._log('DCA: LIMIT — existing order not confirmed dead, not stacking')
                return True
            # MAKER RETRIES: try post_only up to 5 times
            retries = self.settings.get('maker_retries', 0)
            for attempt in range(retries, max_maker):
                oid = secrets.token_hex(8)
                try:
                    resp = self.client.create_limit_order(pid, 'BUY', f"{dca_base:.{dp2}f}",
                                                          f"{limit_price:.{pdp2}f}", post_only=True,
                                                          client_order_id=oid)
                    self._pending_add({
                        'kind': 'dca', 'order_id': resp['order_id'],
                        'client_order_id': oid,
                        'limit_price': limit_price, 'placed': time.time(), 'size': dca_base,
                        'fallback': bool(self.settings.get('fallback_to_taker_buy', True)),
                        'keep_open': bool(self.settings.get('keep_open_buy_for_dca', False)),
                    })
                    self._log(f'DCA: LIMIT buy placed @ ${limit_price:.2f} — keep watching, fallback in {self._limit_timeout_s():.0f}s', user=True)
                    self.settings['maker_retries'] = 0
                    return True
                except Exception as e:
                    self.settings['maker_retries'] = attempt + 1
                    if attempt + 1 < max_maker:
                        self._log(f'DCA: LIMIT maker rejected ({attempt + 1}/{max_maker}) — retrying in 5s ({_clean_error(e)})')
                        time.sleep(5)
                    else:
                        self._log(f'DCA: LIMIT maker rejected {max_maker}x — switching to taker ({_clean_error(e)})')
            # TAKER FALLBACK: post_only=False after 5 maker rejections
            oid = secrets.token_hex(8)
            resp = self.client.create_limit_order(pid, 'BUY', f"{dca_base:.{dp2}f}",
                                                  f"{limit_price:.{pdp2}f}", post_only=False,
                                                  client_order_id=oid)
            self._pending_add({
                'kind': 'dca', 'order_id': resp['order_id'],
                'client_order_id': oid,
                'limit_price': limit_price, 'placed': time.time(), 'size': dca_base,
                'fallback': False,
                'keep_open': bool(self.settings.get('keep_open_buy_for_dca', False)),
            })
            self._log(f'DCA: LIMIT taker placed @ ${limit_price:.2f} — fill or timeout in {self._limit_timeout_s():.0f}s', user=True)
            self.settings['maker_retries'] = 0
            return True
        except Exception as e:
            self.settings['maker_retries'] = 0
            if self.settings.get('fallback_to_taker_buy', True):
                self._log(f'DCA: LIMIT rejected — no fallback, re-arming watch ({_clean_error(e)})', user=True)
            else:
                self._log(f'DCA: LIMIT rejected — no fallback, re-arming watch ({_clean_error(e)})', user=True)
            self._dca_bounce_low = None
            return True

    def _place_limit_sell(self, pos):
        """Place a post_only limit SELL at the best bid, only if the bid is
        at/above the position ENTRY (no-loss). Returns True if placed
        (execute_sell should return); False to proceed to the taker order."""
        max_maker = 5
        try:
            entry = pos.entry_price or 0.0
            bid = self.client.get_best_bid(pos.product_id)
            if not bid or bid <= 0 or bid < entry:
                self.state.status = BotStatus.HOLDING
                self.trailing_stop.reset()
                self._log('LIMIT SELL: bid below entry — holding (never a loss)')
                return True
            inc = self.client.get_base_increment(pos.product_id)
            dp = max(1, -int(math.log10(inc)))
            price_inc = self.client.get_price_increment(pos.product_id)
            pdp = max(1, -int(math.log10(price_inc)))
            # MAKER RETRIES: try post_only up to 5 times with no-loss re-check
            retries = self.settings.get('maker_retries', 0)
            for attempt in range(retries, max_maker):
                # NO-LOSS RE-CHECK before each attempt (5s exchange cooldown)
                bid = self.client.get_best_bid(pos.product_id)
                if not bid or bid <= 0 or bid < entry:
                    self.state.status = BotStatus.HOLDING
                    self.trailing_stop.reset()
                    self._log(f'LIMIT SELL: bid ${bid or 0:.2f} below entry ${entry:.2f} — holding (never a loss)')
                    self.settings['maker_retries'] = 0
                    return True
                oid = generate_order_id()
                try:
                    resp = self.client.create_limit_order(pos.product_id, 'SELL',
                                                          f"{pos.size_base:.{dp}f}", f"{bid:.{pdp}f}",
                                                          post_only=True, client_order_id=oid)
                    self._pending_add({
                        'kind': 'sell', 'order_id': resp['order_id'],
                        'client_order_id': oid,
                        'limit_price': bid, 'placed': time.time(), 'size': pos.size_base,
                        'fallback': bool(self.settings.get('fallback_to_taker_sell', True)),
                    })
                    self.state.status = BotStatus.SELLING
                    self._log(f'LIMIT SELL placed @ ${bid:.2f} — fill or fallback in {self._limit_timeout_s():.0f}s', user=True)
                    self.settings['maker_retries'] = 0
                    return True
                except Exception as e:
                    self.settings['maker_retries'] = attempt + 1
                    if attempt + 1 < max_maker:
                        self._log(f'LIMIT SELL maker rejected ({attempt + 1}/{max_maker}) — retrying in 5s ({_clean_error(e)})')
                        time.sleep(5)
                    else:
                        self._log(f'LIMIT SELL: maker rejected {max_maker}x — switching to taker ({_clean_error(e)})')
            # TAKER FALLBACK: post_only=False after 5 maker rejections
            # NO-LOSS RE-CHECK before taker placement
            bid = self.client.get_best_bid(pos.product_id)
            if not bid or bid <= 0 or bid < entry:
                self.state.status = BotStatus.HOLDING
                self.trailing_stop.reset()
                self._log(f'LIMIT SELL taker: bid ${bid or 0:.2f} below floor ${floor:.2f} — holding (never a loss)')
                self.settings['maker_retries'] = 0
                return True
            oid = generate_order_id()
            resp = self.client.create_limit_order(pos.product_id, 'SELL',
                                                  f"{pos.size_base:.{dp}f}", f"{bid:.{pdp}f}",
                                                  post_only=False, client_order_id=oid)
            self._pending_add({
                'kind': 'sell', 'order_id': resp['order_id'],
                'client_order_id': oid,
                'limit_price': bid, 'placed': time.time(), 'size': pos.size_base,
                'fallback': False,
            })
            self.state.status = BotStatus.SELLING
            self._log(f'LIMIT SELL taker placed @ ${bid:.2f} — fill or timeout in {self._limit_timeout_s():.0f}s', user=True)
            self.settings['maker_retries'] = 0
            return True
        except Exception as e:
            self.settings['maker_retries'] = 0
            if self.settings.get('fallback_to_taker_sell', True):
                self._log(f'LIMIT SELL rejected — no fallback, holding ({_clean_error(e)})', user=True)
            else:
                self._log(f'LIMIT SELL rejected — no fallback, holding ({_clean_error(e)})', user=True)
            self.state.status = BotStatus.HOLDING
            self._sell_cooldown_until = time.time() + 60
            self.trailing_stop.reset()
            # trail re-arms automatically at/above the take-profit mark in on_price
            return True

    def _manage_parked_before_sell(self):
        """When a sell begins, pending buy/DCA limits are either CANCELLED
        (they belong to the cycle ending) or KEPT resting for the NEXT cycle
        (keep-open — the bot re-attaches and adopts their fills later)."""
        for p in list(self._pending):
            if p.get('kind') not in ('buy', 'dca'):
                continue
            if p.get('keep_open'):
                self._log(f'{p["kind"].upper()} buy limit kept for next cycle (order {p["order_id"]})', user=True)
            else:
                try:
                    self.client.cancel_order(p['order_id'])
                    self._log(f'cancelled pending {p["kind"]} limit {p["order_id"]} before sell', user=True)
                except Exception:
                    pass
                self._pending_remove(p['order_id'])

    def poll_open_orders(self):
        """Poll every pending limit order (GUI timer). Resolves fills, timeouts,
        partials, and cancels so an unfilled limit never holds the bot hostage
        and never blocks new buy triggers. THROTTLED (`order_poll_interval_s`,
        default 3.0 from config): with many auto-invest bots sharing one key,
        each resting order is queried at most once per interval — keeps REST
        calls low so many bots never flag the exchange."""
        if not self._pending:
            self._last_poll_ts = getattr(self, '_last_poll_ts', 0.0)
            self._last_poll_ts = 0.0
            return
        interval = float(self.settings.get('order_poll_interval_s', 0.0) or 0.0)
        now = time.time()
        if interval > 0 and now - getattr(self, '_last_poll_ts', 0.0) < interval:
            return
        self._last_poll_ts = now
        for ol in list(self._pending):
            try:
                st = self.client.get_order_status(ol['order_id'])
            except Exception:
                # Status read failed: transient network, OR (legacy entries
                # from before 2026-08-18) the tracked id is our client id,
                # which the exchange rejects. NEVER resolve destructively on
                # an unknown order — the 2026-08-18 incident stacked ~28 live
                # limit orders this way. First recover the REAL exchange id,
                # then process it; only if the order cannot be found anywhere
                # is it treated as never-placed (no fallback buy on top of a
                # possibly-live order).
                ol['fails'] = ol.get('fails', 0) + 1
                if ol.get('fails', 0) >= 5:
                    self._log(f'order {ol["order_id"]} unreachable {ol["fails"]}x — '
                              'recovering by client id')
                    rec = None
                    try:
                        rec = self.client.find_order_by_client_id(
                            ol.get('client_order_id') or ol['order_id'])
                    except Exception:
                        rec = None
                    if rec is not None:
                        ol['order_id'] = rec.get('order_id') or ol['order_id']
                        ol['fails'] = 0
                        ol['lost_count'] = 0
                        status = (rec.get('status') or '').upper()
                        filled = float(rec.get('filled_size') or 0)
                        avg = float(rec.get('average_filled_price') or 0)
                        if avg <= 0 and filled > 0:
                            fv = float(rec.get('filled_value') or 0)
                            if fv > 0:
                                avg = fv / filled
                        self._log(f'order recovered: {ol["order_id"]} '
                                  f'(status {status})', user=True)
                        self._process_polled_order(ol, status, filled, avg)
                    else:
                        # Not found in the recent history either. Try a cancel:
                        # success = it was live and is now dead (safe to move
                        # on); failure = ambiguous (network down?) — keep
                        # tracking, back off, never double-spend.
                        cancelled = False
                        try:
                            cancelled = self.client.cancel_order(ol['order_id'])
                        except Exception:
                            cancelled = False
                        if cancelled:
                            self._log(f'order {ol["order_id"]} not found — '
                                      'cancelled; resolved as unfilled', user=True)
                            self._pending_remove(ol['order_id'])
                        else:
                            ol['lost_count'] = ol.get('lost_count', 0) + 1
                            ol['fails'] = 3  # retry recovery in a couple polls
                            if ol.get('lost_count', 0) >= 3:
                                self._log(f'order {ol["order_id"]} unresolvable — '
                                          'dropped from tracking. Verify on the '
                                          'exchange app; NO fallback was placed.',
                                          user=True)
                                self._pending_remove(ol['order_id'])
                            else:
                                self._log(f'order {ol["order_id"]} status unknown '
                                          '(network?) — holding, NOT falling back',
                                          user=True)
                continue
            ol['fails'] = 0
            ol['lost_count'] = 0
            status = (st.get('status') or '').upper()
            filled = float(st.get('filled_size') or 0)
            avg = float(st.get('average_filled_price') or 0)
            self._process_polled_order(ol, status, filled, avg)

    def _process_polled_order(self, ol, status, filled, avg):
        """Shared poll processing for a pending order whose exchange state is
        known (polled directly or recovered by client id)."""
        timed_out = time.time() - float(ol.get('placed') or 0) > self._limit_timeout_s()
        kind = ol.get('kind')
        already_dead = status in ('CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED')
        if kind in ('buy', 'dca'):
            if filled > 0:
                if self.state.position is not None:
                    self._apply_dca_fill(ol, filled, avg)     # merge into holding
                else:
                    self._adopt_buy_fill(ol, filled, avg)     # new position
            elif already_dead:
                self._resolve_unfilled_buy(ol, already_dead=True)
            elif timed_out:
                self._resolve_unfilled_buy(ol, already_dead=False)
        else:  # sell
            size = float(ol.get('size') or 0)
            if filled >= size - 1e-12:
                self._complete_sell(ol, filled, avg)
            elif filled > 0:
                self._handle_partial_sell(ol, filled, avg)
            elif already_dead:
                self._resolve_unfilled_sell(ol, already_dead=True)
            elif timed_out:
                self._resolve_unfilled_sell(ol, already_dead=False)

    def _adopt_buy_fill(self, ol, filled, avg):
        """A pending limit BUY filled with no position yet — adopt the REAL fill
        as a NEW position (the resting order id is journaled as the entry, so the
        bot knows where it came from)."""
        pid = self.settings.get('product_id', '')
        price = avg or ol.get('limit_price') or 0.0
        if filled <= 0 or price <= 0:
            self._resolve_unfilled_buy(ol, already_dead=False)
            return
        try:
            self.client.cancel_order(ol['order_id'])
        except Exception:
            pass
        self._pending_remove(ol['order_id'])
        self.order_id = ol['order_id']
        self.state.position = Position(product_id=pid, entry_price=price,
                                       size_usdc=filled * price, size_base=filled,
                                        entry_time=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                                       order_id=ol['order_id'], highest_price=price,
                                       stop_price=0.0)
        try:
            self.state.position.entry_price = price  # fee-excluded (site avg)
        except Exception:
            pass
        # Take-profit: keep the trail DISARMED until the mark is reached.
        self.trailing_stop.reset()
        self.state.status = BotStatus.HOLDING
        self._trade_closed_ok = False
        self._cap_tried = False
        try:
            self._update_own_base(filled)
        except Exception:
            pass
        try:
            real = self.client.get_real_base_balance(pid)
            if real and real > 0:
                self.state.position.size_base = self._inc_trunc(real, pid)
                self.state.position.size_usdc = (self.state.position.size_base
                                                 * self.state.position.entry_price)
        except Exception:
            pass
        self._log(f'LIMIT BUY FILLED: {filled:.8f} @ ${self.state.position.entry_price:.2f} '
                  f'(fees included, from resting order {ol["order_id"]})', user=True)

    def _apply_dca_fill(self, ol, filled, avg):
        """A pending buy/DCA limit filled while a position exists — merge the REAL
        fill into the holding (update the fee-adjusted average, re-arm the
        trailing stop)."""
        if not self.state or not self.state.position:
            self._pending_remove(ol['order_id'])
            return
        pid = self.state.position.product_id
        price = avg or ol.get('limit_price') or 0.0
        if filled <= 0 or price <= 0:
            self._resolve_unfilled_buy(ol, already_dead=False)
            return
        tf = self.client.get_trading_fee()
        old_base = self.state.position.size_base or 0.0
        old_usdc = (self.state.position.size_usdc
                    or (old_base * self.state.position.entry_price
                        if self.state.position.entry_price else 0.0))
        new_base = filled
        total_base = old_base + new_base
        total_usdc = old_usdc + (filled * price)
        avg_price = total_usdc / total_base if total_base else price
        self.state.position.entry_price = avg_price
        self.state.position.size_base = self._inc_trunc(total_base, pid)
        self.state.position.size_usdc = total_usdc
        self.state.position.highest_price = price
        self.state.position.stop_price = price * (1 - float(self.settings.get('trailing_stop_pct', 0.01)))
        self.state.position.entry_time = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        # DCA fills are BELOW entry — trail stays disarmed (take-profit),
        # arms itself at the mark in on_price's HOLDING gate.
        self.trailing_stop.reset()
        self.state.status = BotStatus.HOLDING
        self._trade_closed_ok = False
        self._dca_bounce_low = None
        self._pending_remove(ol['order_id'])
        try:
            self._update_own_base(new_base)
        except Exception:
            pass
        self._log(f'DCA (limit) FILLED: avg entry ${avg_price:.2f}, total {total_base:.8f} {pid.split("-")[0]}', user=True)

    def _cancel_confirmed(self, ol, already_dead):
        """True when the resting order is CONFIRMED not-live. A market
        fallback may only run after this — falling back while the limit may
        still fill is how the 2026-08-18 incident stacked ~28 duplicate live
        orders and drained the account. `already_dead` = the exchange itself
        reported CANCELED/EXPIRED/REJECTED, so no cancel is needed."""
        if already_dead:
            return True
        try:
            return bool(self.client.cancel_order(ol['order_id']))
        except Exception:
            return False

    def _resolve_unfilled_buy(self, ol, already_dead=True):
        if ol.get('keep_open'):
            if not ol.get('kept'):
                ol['kept'] = True
                self._log('LIMIT BUY kept open for DCA/Auto Rearm (resting until it fills)', user=True)
            return
        if ol.get('fallback'):
            if not self._cancel_confirmed(ol, already_dead):
                self._log('LIMIT BUY unfilled but cancel unconfirmed — order kept, '
                          'NO fallback while it may still be live', user=True)
                ol['fails'] = 0   # keep polling; cancel retries next timeout
                return
            self._pending_remove(ol['order_id'])
            self._log('LIMIT BUY unfilled — cancelled, falling back to taker', user=True)
            # TAKER FALLBACK: place a post_only=False limit at best ask
            try:
                pid = self.settings.get('product_id', '')
                ask = self.client.get_best_ask(pid)
                if not ask or ask <= 0:
                    self._log('LIMIT BUY taker: no price — waiting for next dip', user=True)
                    self.state.status = BotStatus.WATCHING
                    return
                inc = self.client.get_base_increment(pid)
                dp = max(1, -int(math.log10(inc)))
                size = ol.get('size', 0)
                if not size or size <= 0:
                    self.state.status = BotStatus.WATCHING
                    return
                oid = generate_order_id()
                resp = self.client.create_limit_order(pid, 'BUY',
                                                      f"{size:.{dp}f}", f"{ask:.2f}",
                                                      post_only=False, client_order_id=oid)
                self._pending_add({
                    'kind': 'buy', 'order_id': resp['order_id'],
                    'client_order_id': oid,
                    'limit_price': ask, 'placed': time.time(), 'size': size,
                    'fallback': False,
                    'keep_open': bool(self.settings.get('keep_open_buy_for_dca', False)),
                })
                self.state.status = BotStatus.WATCHING
                self._log(f'LIMIT BUY taker placed @ ${ask:.2f} — fill or timeout in {self._limit_timeout_s():.0f}s', user=True)
            except Exception as e:
                self._log(f'LIMIT BUY taker failed ({_clean_error(e)}) — watching again', user=True)
                self.state.status = BotStatus.WATCHING
            return
        try:
            self.client.cancel_order(ol['order_id'])
        except Exception:
            pass
        self._pending_remove(ol['order_id'])
        if self.state.position is None:
            self.state.status = BotStatus.WATCHING
        self._log('LIMIT BUY unfilled — cancelled, watching again', user=True)

    def _resolve_unfilled_dca(self, ol, already_dead=True):
        if ol.get('keep_open'):
            if not ol.get('kept'):
                ol['kept'] = True
                self._log('DCA: LIMIT kept open for DCA/Auto Rearm (resting until it fills)', user=True)
            return
        if ol.get('fallback'):
            if not self._cancel_confirmed(ol, already_dead):
                self._log('DCA: LIMIT unfilled but cancel unconfirmed — order kept, '
                          'NO fallback while it may still be live', user=True)
                ol['fails'] = 0
                return
            self._pending_remove(ol['order_id'])
            self._log('DCA: LIMIT unfilled — cancelled, falling back to taker', user=True)
            # TAKER FALLBACK: place a post_only=False limit at best ask
            try:
                pid = self.settings.get('product_id', '')
                ask = self.client.get_best_ask(pid)
                if not ask or ask <= 0:
                    self._log('DCA: taker — no price, re-arming watch', user=True)
                    self._dca_bounce_low = None
                    return
                inc = self.client.get_base_increment(pid)
                dp = max(1, -int(math.log10(inc)))
                size = ol.get('size', 0)
                if not size or size <= 0:
                    self._dca_bounce_low = None
                    return
                oid = secrets.token_hex(8)
                resp = self.client.create_limit_order(pid, 'BUY',
                                                      f"{size:.{dp}f}", f"{ask:.2f}",
                                                      post_only=False, client_order_id=oid)
                self._pending_add({
                    'kind': 'dca', 'order_id': resp['order_id'],
                    'client_order_id': oid,
                    'limit_price': ask, 'placed': time.time(), 'size': size,
                    'fallback': False,
                    'keep_open': bool(self.settings.get('keep_open_buy_for_dca', False)),
                })
                self._log(f'DCA: taker placed @ ${ask:.2f} — fill or timeout in {self._limit_timeout_s():.0f}s', user=True)
            except Exception as e:
                self._log(f'DCA: taker failed ({_clean_error(e)}) — re-arming watch', user=True)
                self._dca_bounce_low = None
            return
        try:
            self.client.cancel_order(ol['order_id'])
        except Exception:
            pass
        self._pending_remove(ol['order_id'])
        self._dca_bounce_low = None
        self._log('DCA: LIMIT unfilled — cancelled, re-arming watch', user=True)

    def _complete_sell(self, ol, filled, avg):
        """A pending limit SELL fully filled — close the cycle successfully and
        clean up pending buys per auto_rearm."""
        pos = self.state.position
        self._pending_remove(ol['order_id'])
        if pos is None:
            return
        exit_price = avg or ol.get('limit_price') or 0.0
        self.order_id = ol['order_id']
        try:
            cost = (pos.size_usdc if pos.size_usdc and pos.size_usdc > 0
                    else pos.entry_price * pos.size_base)
            tf = self.client.get_trading_fee()
            gross = exit_price * pos.size_base
            # + any earlier partial limit-sell realized P&L (booked once here)
            pnl = gross * (1 - tf) - cost + (getattr(pos, 'partial_pnl', 0.0) or 0.0)
            pnl_pct = (pnl / cost) if cost and cost > 0 else 0.0
            try:
                self._update_own_base(-pos.size_base)
            except Exception:
                pass
            self.state.total_pnl += pnl
            self.state.trade_count += 1
        except Exception:
            pass
        self.state.position = None
        if self.settings.get('auto_rearm', True):
            self.state.status = BotStatus.WATCHING
            self.dip_detector.reset()
            self.state.local_low = 0.0
        else:
            self.state.status = BotStatus.STOPPED
        self._trade_closed_ok = True
        self._trigger_lowest = None
        self._dca_bounce_low = None
        self._dca_paused = False
        # successful close: cancel unfilled buys (keep keep-open ones if rearming)
        self._cancel_pending_buys(keep_keep_open=bool(self.settings.get('auto_rearm', True)))
        self._log('LIMIT SELL FILLED — position closed successfully', user=True)

    def _handle_partial_sell(self, ol, filled, avg):
        """A pending limit SELL partially filled. If the REST can close above the
        no-loss floor, sell it at market and close. Otherwise cancel the
        remainder, keep the reduced position, and continue the cycle until it
        closes successfully (never a loss)."""
        pos = self.state.position
        self._pending_remove(ol['order_id'])
        if pos is None:
            return
        size = float(ol.get('size') or 0)
        remaining = max(0.0, size - filled)
        try:
            bid = self.client.get_best_bid(pos.product_id)
        except Exception:
            bid = avg or ol.get('limit_price') or 0.0
        floor = pos.entry_price or 0.0   # no-loss gate — sell rest only above entry
        if remaining > 0 and bid and bid >= floor:
            try:
                self.client.cancel_order(ol['order_id'])
            except Exception:
                pass
            try:
                inc = self.client.get_base_increment(pos.product_id)
                dp = max(1, -int(math.log10(inc)))
                oid = generate_order_id()
                resp = self.client.create_market_order(pos.product_id, 'SELL',
                                                       f"{remaining:.{dp}f}", oid)
                exit_price = float(resp.get('average_filled_price') or 0) or bid
                total_base = filled + remaining
                cost = (pos.size_usdc if pos.size_usdc and pos.size_usdc > 0
                        else pos.entry_price * total_base)
                tf = self.client.get_trading_fee()
                gross = exit_price * total_base
                # + any earlier partial limit-sell realized P&L (this rest
                # close books the FULL cycle P&L exactly once)
                pnl = gross * (1 - tf) - cost + (getattr(pos, 'partial_pnl', 0.0) or 0.0)
                pnl_pct = (pnl / cost) if cost and cost > 0 else 0.0
                try:
                    self._update_own_base(-total_base)
                except Exception:
                    pass
                self.state.total_pnl += pnl
                self.state.trade_count += 1
                self.state.position = None
                if self.settings.get('auto_rearm', True):
                    self.state.status = BotStatus.WATCHING
                    self.dip_detector.reset()
                    self.state.local_low = 0.0
                else:
                    self.state.status = BotStatus.STOPPED
                self._trade_closed_ok = True
                self._trigger_lowest = None
                self._dca_bounce_low = None
                self._dca_paused = False
                self._cancel_pending_buys(keep_keep_open=bool(self.settings.get('auto_rearm', True)))
                self._log('LIMIT SELL partial — rest sold at market, position closed successfully', user=True)
            except Exception as e:
                self._log(f'LIMIT SELL partial: market-close failed ({_clean_error(e)}) — re-arming', user=True)
                self.state.status = BotStatus.HOLDING
                self.trailing_stop.reset()
                self.trailing_stop.activate(avg or ol.get('limit_price') or bid or 0.0)
        else:
            try:
                self.client.cancel_order(ol['order_id'])
            except Exception:
                pass
            # Book the realized P&L of the coins that DID sell in this partial
            # fill (at the partial fill price, fee-adjusted like the close
            # math). It rides on the position and is added to total_pnl +
            # journal exactly once when the remainder finally closes — never
            # lost, never double-counted.
            try:
                exit_price = avg or ol.get('limit_price') or 0.0
                tf = self.client.get_trading_fee()
                if filled > 0 and exit_price > 0:
                    cost_portion = filled * (pos.entry_price or 0)
                    partial_pnl = filled * exit_price * (1 - tf) - cost_portion
                    pos.partial_pnl = (getattr(pos, 'partial_pnl', 0.0) or 0.0) + partial_pnl
            except Exception:
                pass
            self.state.position.size_base = self._inc_trunc(remaining, pos.product_id)
            self.state.position.size_usdc = (self.state.position.size_base
                                             * (pos.entry_price or 0))
            try:
                self._update_own_base(-filled)
            except Exception:
                pass
            self.state.status = BotStatus.HOLDING
            self.trailing_stop.reset()
            self.trailing_stop.activate(avg or ol.get('limit_price') or bid or 0.0)
            self._log(f'LIMIT SELL partial ({filled:.8f}) — rest cancelled below floor, continuing cycle '
                      f'(size reduced to {self.state.position.size_base:.8f})', user=True)

    def _resolve_unfilled_sell(self, ol, already_dead=True):
        if ol.get('fallback'):
            if not self._cancel_confirmed(ol, already_dead):
                self._log('LIMIT SELL unfilled but cancel unconfirmed — order kept, '
                          'NO fallback while it may still be live', user=True)
                ol['fails'] = 0
                return
            self._pending_remove(ol['order_id'])
            self._log('LIMIT SELL unfilled — cancelled, falling back to taker', user=True)
            # TAKER FALLBACK: post_only=False with floor re-check
            pos = self.state.position
            if not pos:
                self.state.status = BotStatus.WATCHING
                return
            bid = self.client.get_best_bid(pos.product_id)
            floor = pos.entry_price or 0.0   # no-loss gate for the taker close
            if not bid or bid <= 0 or bid < floor:
                self.state.status = BotStatus.HOLDING
                self.trailing_stop.reset()
                self._log(f'LIMIT SELL taker: bid ${bid or 0:.2f} below entry ${floor:.2f} — holding (never a loss)')
                return
            try:
                inc = self.client.get_base_increment(pos.product_id)
                dp = max(1, -int(math.log10(inc)))
                price_inc = self.client.get_price_increment(pos.product_id)
                pdp = max(1, -int(math.log10(price_inc)))
                oid = generate_order_id()
                resp = self.client.create_limit_order(pos.product_id, 'SELL',
                                                      f"{pos.size_base:.{dp}f}", f"{bid:.{pdp}f}",
                                                      post_only=False, client_order_id=oid)
                self._pending_add({
                    'kind': 'sell', 'order_id': resp['order_id'],
                    'client_order_id': oid,
                    'limit_price': bid, 'placed': time.time(), 'size': pos.size_base,
                    'fallback': False,
                })
                self.state.status = BotStatus.SELLING
                self._log(f'LIMIT SELL taker placed @ ${bid:.2f} — fill or timeout in {self._limit_timeout_s():.0f}s', user=True)
            except Exception as e:
                self._log(f'LIMIT SELL taker failed ({_clean_error(e)}) — holding for better exit', user=True)
                self.state.status = BotStatus.HOLDING
                self.trailing_stop.reset()
            return
        try:
            self.client.cancel_order(ol['order_id'])
        except Exception:
            pass
        self._pending_remove(ol['order_id'])
        self.state.status = BotStatus.HOLDING
        self.trailing_stop.reset()
        self._log('LIMIT SELL unfilled — cancelled, holding for a better exit', user=True)

    def recover_pending_orders(self):
        """Re-attach pending limit orders left before a reboot. If one already
        filled, ADOPT it (the resting order id is journaled as the entry, so the
        bot knows where the position came from)."""
        if not self._pending:
            return
        for ol in list(self._pending):
            oid = ol.get('order_id')
            if not oid:
                self._pending_remove(oid)
                continue
            try:
                st = self.client.get_order_status(oid)
            except Exception:
                continue  # transient — leave parked, try next startup
            status = (st.get('status') or '').upper()
            filled = float(st.get('filled_size') or 0)
            avg = float(st.get('average_filled_price') or 0)
            if filled > 0:
                kind = ol.get('kind')
                self._log(f'Adopted from resting {kind} limit order {oid} (already filled)', user=True)
                if kind in ('buy', 'dca'):
                    if self.state.position is not None:
                        self._apply_dca_fill(ol, filled, avg)
                    else:
                        self._adopt_buy_fill(ol, filled, avg)
                else:
                    self._complete_sell(ol, filled, avg)
            elif status in ('CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED'):
                self._pending_remove(oid)
                self._log(f'Resting order {oid} was {status} — dropped', user=True)
            else:
                self._log(f'Recovered resting {ol.get("kind")} limit {oid} '
                          f'@ ${ol.get("limit_price", 0):.2f} — watching it', user=True)
        self._persist_parked()
