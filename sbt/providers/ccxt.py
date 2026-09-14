"""CCXT-backed exchange provider — one adapter for any exchange CCXT supports.

HANDS-OFF CONTRACT: this adapter ONLY reads market data / balances / fills and
executes orders. ALL trading conditions (dip-bounce, trailing stop, the
no-loss sell floor, DCA, size modes) live in the engine and are NOT touched by
CCXT. Swapping exchanges swaps only this adapter.
"""
import threading
import time

import ccxt

from .base import Provider

_DEFAULT_FEE = 0.006
_FEED_INTERVAL = 2.5


class CcxtProvider(Provider):
    name = 'CCXT'

    def __init__(self, exchange, api_key='', secret='', password=''):
        self.exchange_id = exchange
        # BUG-005: a typo'd/unknown exchange id would raise AttributeError and
        # kill the app with only a crash file. Fail early with a clear message.
        ex_cls = getattr(ccxt, exchange, None)
        if ex_cls is None:
            raise ValueError(f"Unknown exchange id '{exchange}'. "
                             'Check the spelling (e.g. kraken, binance, kucoin).')
        self._ex = ex_cls({
            'apiKey': api_key or None,
            'secret': secret or None,
            'password': password or None,
            'enableRateLimit': True,
            'timeout': 15000,
        })
        self._markets = None
        self._fee_cache = None
        self._fee_cache_time = 0.0
        self._fee_product = None
        self._last_product = None
        self._feed = None
        self._feed_stop = threading.Event()
        # Serialize ALL network I/O behind one lock per instance (the polling
        # feed thread + the GUI thread both hit ccxt/HTTPS concurrently).
        self._lock = threading.RLock()

    # ---- market info -----------------------------------------------------
    def quote_currency(self, product_id):
        return (product_id or '').split('-')[1].upper() if '-' in (product_id or '') else 'USD'

    def base_currency(self, product_id):
        return (product_id or '').split('-')[0].upper() if '-' in (product_id or '') else ''

    def _symbol(self, product_id):
        """App uses BASE-QUOTE; CCXT uses BASE/QUOTE."""
        return (product_id or '').replace('-', '/')

    def _market(self, product_id):
        with self._lock:
            sym = self._symbol(product_id)
            if self._markets is None:
                try:
                    self._markets = self._ex.load_markets()
                except Exception:
                    self._markets = {}
            m = self._markets.get(sym)
            if m is None:
                m = self._ex.market(sym)
            return m

    def get_base_increment(self, product_id):
        try:
            m = self._market(product_id)
            prec = m.get('precision') or {}
            a = prec.get('amount')
            if a is None:
                a = prec.get(self.base_currency(product_id))
            if a is None:
                return 0.000001
            if isinstance(a, float):
                return a if a > 0 else 0.000001
            return float(10 ** -int(a))
        except Exception:
            return 0.000001

    def get_price_increment(self, product_id):
        """Minimum price step for limit orders (e.g. 0.01 for2-decimal fiat
        pairs, 0.5 for some crypto pairs). Falls back to 0.01."""
        try:
            m = self._market(product_id)
            prec = m.get('precision') or {}
            p = prec.get('price')
            if p is None:
                p = prec.get(self.quote_currency(product_id))
            if p is None:
                return 0.01
            if isinstance(p, float):
                return p if p > 0 else 0.01
            return float(10 ** -int(p))
        except Exception:
            return 0.01

    def get_min_quote_size(self, product_id):
        """Minimum order size in QUOTE currency from the market's limits.
        Prefers `limits.cost.min` (already in quote); falls back to
        `limits.amount.min × current ask`. None if unknown — callers use a
        safe default."""
        try:
            m = self._market(product_id)
            limits = m.get('limits') or {}
            cost = (limits.get('cost') or {}).get('min')
            if cost is not None and float(cost) > 0:
                return float(cost)
            amt = (limits.get('amount') or {}).get('min')
            if amt is not None and float(amt) > 0:
                price = self.get_best_ask(product_id)
                if price and price > 0:
                    return float(amt) * price
        except Exception:
            pass
        return None

    # ---- balances ---------------------------------------------------------
    def _balance(self):
        try:
            with self._lock:
                return self._ex.fetch_balance()
        except Exception:
            return {}

    def get_quote_balance(self, product_id):
        b = self._balance()
        q = self.quote_currency(product_id)
        for cur in (q, 'USDC', 'USD'):
            free = b.get('free', {}).get(cur)
            if free is None:
                cur_info = b.get(cur, {})
                free = cur_info.get('free') if isinstance(cur_info, dict) else None
            if free:
                return float(free)
        return 0.0

    def get_real_base_balance(self, product_id):
        try:
            b = self._balance()
            base = self.base_currency(product_id)
            amt = b.get('free', {}).get(base)
            if amt is None:
                amt = b.get(base, {}).get('free')
            if amt is None:
                amt = b.get('total', {}).get(base)
            if amt is None:
                amt = b.get(base, {}).get('total')
            # fetch_balance() succeeded: not-found means verifiably flat (0.0),
            # NOT None — None is reserved for a failed read (BUG-007).
            return float(amt) if amt is not None else 0.0
        except Exception:
            return None

    # ---- prices -----------------------------------------------------------
    def get_best_ask(self, product_id):
        with self._lock:
            return float(self._ex.fetch_ticker(self._symbol(product_id)).get('ask') or 0)

    def get_best_bid(self, product_id):
        with self._lock:
            return float(self._ex.fetch_ticker(self._symbol(product_id)).get('bid') or 0)

    # ---- orders -----------------------------------------------------------
    def create_market_order(self, product_id, side, size, client_order_id=None):
        self._last_product = product_id
        sym = self._symbol(product_id)
        params = {}
        if client_order_id:
            params['clientOrderId'] = client_order_id
        with self._lock:
            if str(side).upper() == 'BUY':
                # engine passes a BASE amount (coin units), already truncated
                # to the base increment — never round a quote up into an
                # unaffordable order, and never leave unsellable dust.
                try:
                    order = self._ex.create_market_buy_order(sym, float(size), params)
                except Exception:
                    order = self._ex.create_market_order(sym, 'buy', float(size), None, params)
            else:
                order = self._ex.create_market_order(sym, 'sell', float(size), None, params)
        return {
            'average_filled_price': order.get('average'),
            'filled_size': order.get('filled'),
        }

    def create_limit_order(self, product_id, side, base_size, limit_price,
                           post_only=True, client_order_id=None):
        self._last_product = product_id
        params = {'postOnly': post_only}
        if client_order_id:
            params['clientOrderId'] = client_order_id
        with self._lock:
            order = self._ex.create_limit_order(self._symbol(product_id), str(side).lower(),
                                            float(base_size), float(limit_price), params)
        return {'order_id': order.get('id'), 'status': order.get('status')}

    def cancel_order(self, order_id):
        try:
            with self._lock:
                self._ex.cancel_order(order_id)
            return True
        except Exception:
            return False

    def get_order_status(self, order_id):
        with self._lock:
            o = self._ex.fetch_order(order_id)
        return {
            'status': (o.get('status') or '').upper(),
            'filled_size': float(o.get('filled') or 0),
            'average_filled_price': float(o.get('average') or 0),
        }

    # ---- fees -------------------------------------------------------------
    def get_trading_fee(self):
        now = time.time()
        # BUG-010: expire the cache like Coinbase (300s) so a fee-tier change
        # isn't locked in forever.
        if self._fee_cache is not None and now - self._fee_cache_time < 300:
            return self._fee_cache
        fee = _DEFAULT_FEE
        try:
            pid = self._last_product or self._fee_product
            if pid:
                m = self._market(pid)
                f = m.get('taker')
                if f:
                    fee = float(f)
        except Exception:
            pass
        self._fee_cache = fee
        self._fee_cache_time = now
        return fee

    # ---- cost basis (TRUE, from the exchange's own fills) -----------------
    def _fetch_all_trades(self, product_id):
        """Paginate fetch_my_trades by `since` until fewer than `limit` come
        back (BUG-004). Bounded + dedupes on timestamp so exchanges that ignore
        `since` can't loop forever."""
        trades = []
        since = None
        seen = set()
        for _ in range(50):
            try:
                with self._lock:
                    page = self._ex.fetch_my_trades(self._symbol(product_id),
                                                    since=since, limit=1000)
            except Exception:
                break
            if not page:
                break
            new = 0
            last_ts = None
            for t in page:
                key = (t.get('id'), t.get('timestamp'))
                if key in seen:
                    continue
                seen.add(key)
                trades.append(t)
                new += 1
                last_ts = t.get('timestamp')
            if new == 0 or len(page) < 1000:
                break
            if last_ts is None:
                break
            since = last_ts
        return trades

    def get_cost_basis(self, product_id, real_base):
        """Reconstruct the TRUE average entry of the current holding via CCXT
        unified trades, FEE-EXCLUDED (matches the exchange's 'average entry' —
        that number already reflects true cost): BUY adds base + quote cost
        (fee NOT added), SELL removes base FIFO (releasing cost
        proportionally). Works on any exchange."""
        try:
            trades = self._fetch_all_trades(product_id)
        except Exception:
            return None
        if not trades:
            return None
        trades.sort(key=lambda t: t.get('timestamp') or 0)
        lots = []
        for t in trades:
            side = (t.get('side') or '').upper()
            amount = float(t.get('amount') or 0)
            if amount <= 0:
                continue
            price = float(t.get('price') or 0)
            cost = float(t.get('cost') or 0) or amount * price
            if side == 'BUY':
                lots.append([amount, cost])
            elif side == 'SELL':
                rem = amount
                while rem > 1e-12 and lots:
                    lot_base, lot_cost = lots[0]
                    take = min(lot_base, rem)
                    if lot_base > 0:
                        lots[0][1] = lot_cost * (1 - take / lot_base)
                    lots[0][0] = lot_base - take
                    if lots[0][0] <= 1e-12:
                        lots.pop(0)
                    rem -= take
        base = sum(l[0] for l in lots)
        cost = sum(l[1] for l in lots)
        if base <= 0 or cost <= 0:
            return None
        return (cost / base, base)

    def product_valid(self, product_id):
        try:
            self._market(product_id)
            return True
        except Exception:
            return False

    def list_products(self):
        """All TRADEABLE pairs from the loaded markets (BASE/QUOTE ->
        BASE-QUOTE), alphabetical, for the Settings type-ahead dropdown.
        Excludes markets ccxt marks inactive (delisted/suspended)."""
        try:
            m = self._ex.load_markets()
            out = set()
            for sym, market in m.items():
                if market is not None and market.get('active') is False:
                    continue
                out.add(str(sym).replace('/', '-'))
            return sorted(out)
        except Exception:
            return []

    # ---- price feed (polling — no WS for generic ccxt) ---------------------
    def start_price_feed(self, product_id, on_price, on_connection_change):
        # Stop any previous feed FIRST (BUG-003) so a settings pair change never
        # leaves the old feed polling the old symbol and feeding wrong-pair
        # ticks into the engine. Mirrors CoinbaseProvider.start_price_feed.
        self.stop_price_feed()
        self._last_product = product_id
        self._feed_stop.clear()
        self._feed = threading.Thread(
            target=self._feed_loop, args=(product_id, on_price, on_connection_change),
            daemon=True)
        self._feed.start()

    def _feed_loop(self, product_id, on_price, on_connection_change):
        connected = False
        sym = self._symbol(product_id)
        while not self._feed_stop.is_set():
            try:
                with self._lock:
                    ticker = self._ex.fetch_ticker(sym)
                price = float(ticker.get('last') or 0)
                if price > 0:
                    if not connected:
                        connected = True
                        try:
                            on_connection_change(True)
                        except Exception:
                            pass
                    try:
                        on_price(price)
                    except Exception:
                        pass
            except Exception:
                if connected:
                    connected = False
                    try:
                        on_connection_change(False)
                    except Exception:
                        pass
            self._feed_stop.wait(_FEED_INTERVAL)

    def stop_price_feed(self):
        self._feed_stop.set()

    def describe(self):
        try:
            return self._ex.name
        except Exception:
            return self.exchange_id
