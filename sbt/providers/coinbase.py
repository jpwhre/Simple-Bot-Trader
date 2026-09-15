"""Coinbase provider — the ONLY place Coinbase specifics live.

All engine/GUI/config code is exchange-agnostic; swap this for another adapter
to support a different exchange. Ported from the original app's client logic.
"""
import base64
import json
import os
import queue
import threading
import time

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .. import paths
from .base import Provider

_REST_BASE = 'https://api.coinbase.com/api/v3/brokerage'
_PUBLIC_EXCHANGE = 'https://api.exchange.coinbase.com'
_WS_URL = 'wss://advanced-trade-ws.coinbase.com'
_DEFAULT_FEE = 0.006


def _log_event(msg):
    try:
        with open(paths.LOG_FILE, 'a') as f:
            f.write(f'[{time.strftime("%H:%M:%S")}] {msg}\n')
    except Exception:
        pass


# OpenSSL (cryptography/EC-key + SSL) is not safe under concurrent use across
# threads. Both the REST client and the price feed build JWTs; serialize the
# shared crypto operation process-wide.
_jwt_lock = threading.RLock()


def _monkey_jwt_encode(payload, key, algorithm='ES256', headers=None):
    with _jwt_lock:
        import jwt as jwt_lib
        if headers is None:
            headers = {}
        if 'kid' not in headers:
            headers['kid'] = payload.get('sub', '')
        headers['typ'] = None
        if isinstance(key, str):
            key = load_pem_private_key(key.encode('utf-8'), password=None)
        return jwt_lib.encode(payload, key, algorithm=algorithm, headers=headers)


def _jwt_alg_for(private_key_pem):
    """'EdDSA' for an Ed25519 key, 'ES256' for a (P-256) EC key.

    Coinbase CDP keys come in BOTH flavours (docs, 2026): ECDSA exports a PEM
    (ES256 JWT); Ed25519 exports a bare 64-byte base64 (32-byte seed + 32-byte
    public key) which first_run._pem_from_key converts to a PKCS8 PEM that
    loads as an Ed25519PrivateKey. The JWT algorithm must match the key type.
    """
    try:
        key = load_pem_private_key(private_key_pem.encode('utf-8'), password=None)
        return 'EdDSA' if isinstance(key, Ed25519PrivateKey) else 'ES256'
    except Exception:
        return 'ES256'


class CoinbasePriceFeed:
    def __init__(self, api_key_name, private_key_pem, product_id, jwt_alg='ES256'):
        self.api_key_name = api_key_name
        self.private_key_pem = private_key_pem
        self.product_id = product_id
        self.jwt_alg = jwt_alg
        self.ws = None
        self.running = False
        self._stopped = False
        self.on_price = None
        self.on_connection_change = None
        self._thread = None

    def _build_jwt(self):
        payload = {
            'iss': 'cdp',
            'sub': self.api_key_name,
            'nbf': int(time.time()),
            'exp': int(time.time()) + 120,
        }
        return _monkey_jwt_encode(payload, self.private_key_pem,
                                  algorithm=self.jwt_alg)

    def _run_forever_with_keepalive(self):
        while self.running:
            try:
                self.ws.run_forever(ping_interval=30, ping_timeout=10,
                                    ping_payload='', reconnect=5)
            except Exception:
                pass
            time.sleep(5)

    def start(self):
        import websocket
        self._stopped = False
        self.running = True
        self.ws = websocket.WebSocketApp(
            _WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_close=self._on_close,
            on_error=self._on_error,
        )
        self._thread = threading.Thread(target=self._run_forever_with_keepalive, daemon=True)
        self._thread.start()

    def _on_open(self, ws):
        try:
            sub = {
                'type': 'subscribe',
                'channel': 'ticker',
                'product_ids': [self.product_id],
                'jwt': self._build_jwt(),
            }
            ws.send(json.dumps(sub))
        except Exception:
            pass
        if self.on_connection_change:
            try:
                self.on_connection_change(True)
            except Exception:
                pass

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get('channel') == 'ticker':
                for event in data.get('events', []):
                    for t in event.get('tickers', []):
                        price_str = t.get('price')
                        if price_str and self.on_price:
                            self.on_price(float(price_str))
        except Exception:
            pass

    def _on_close(self, ws, *args):
        # Do NOT restart the feed here (BUG-001). _run_forever_with_keepalive
        # already loops run_forever(reconnect=5) on this same thread, so a
        # restart from _on_close would spawn a SECOND thread driving the same
        # WebSocketApp -> duplicate ticks, concurrent engine calls, double
        # orders. Just mark disconnected; the keepalive loop reconnects.
        if self.on_connection_change:
            try:
                self.on_connection_change(False)
            except Exception:
                pass
        if self._stopped:
            return
        _log_event('Disconnected — reconnecting')

    def _on_error(self, ws, error):
        if self.on_connection_change:
            try:
                self.on_connection_change(False)
            except Exception:
                pass

    def stop(self):
        self._stopped = True
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass


class CoinbaseProvider(Provider):
    name = 'Coinbase'

    def __init__(self, api_key_name, private_key_pem):
        self.api_key_name = api_key_name
        self.private_key_pem = private_key_pem
        self._jwt_alg = _jwt_alg_for(private_key_pem)
        self._fee_cache = None
        self._fee_cache_time = 0.0
        self._inc_cache = {}
        self._min_quote_cache = {}
        self._product_info_cache = {}
        self._products = None
        self._products_time = 0.0
        self._feed = None
        # Serialize ALL network I/O behind one lock per instance. The price
        # feed thread and the GUI thread both make HTTPS calls concurrently;
        # OpenSSL's _ssl is not safe under that (heap corruption -> crash),
        # so every signed/public request goes through this lock.
        self._net_lock = threading.RLock()

    # ---- market info -----------------------------------------------------
    def quote_currency(self, product_id):
        return (product_id or '').split('-')[1].upper() if '-' in (product_id or '') else 'USD'

    def base_currency(self, product_id):
        return (product_id or '').split('-')[0].upper() if '-' in (product_id or '') else ''

    def _product_info(self, product_id):
        """Cached product record (base_increment, min_market_funds, ...) from
        the public exchange endpoint. None on failure — a failed result is NOT
        cached, so a transient API blip retries on the next call instead of
        locking in the fallback for the whole session (BUG-020)."""
        cached = self._product_info_cache.get(product_id)
        if cached is not None:
            return cached
        info = None
        try:
            with self._net_lock:
                r = requests.get(f'{_PUBLIC_EXCHANGE}/products/{product_id}', timeout=10)
            if r.status_code == 200:
                info = r.json()
        except Exception:
            pass
        if info is not None:
            self._product_info_cache[product_id] = info
        return info

    def get_base_increment(self, product_id):
        if product_id in self._inc_cache:
            return self._inc_cache[product_id]
        inc = 0.000001
        info = self._product_info(product_id)
        try:
            if info is not None:
                inc = float(info.get('base_increment', 0.000001))
        except Exception:
            pass
        self._inc_cache[product_id] = inc
        return inc

    def get_price_increment(self, product_id):
        """Minimum price step for limit orders from the exchange's own product
        record (quote_increment). Falls back to 0.01."""
        info = self._product_info(product_id)
        try:
            if info is not None:
                inc = float(info.get('quote_increment', 0.01))
                if inc > 0:
                    return inc
        except Exception:
            pass
        return 0.01

    def get_min_quote_size(self, product_id):
        """Minimum market-order size in the QUOTE currency from the exchange's
        own product record (`min_market_funds`). None if unknown — callers
        fall back to a safe default so the guardrail never silently weakens.
        A failed lookup is not cached (BUG-020): a transient API blip retries
        next call instead of locking in the fallback for the session."""
        cached = self._min_quote_cache.get(product_id)
        if cached is not None:
            return cached
        m = None
        info = self._product_info(product_id)
        try:
            if info is not None:
                raw = info.get('min_market_funds')
                if raw is not None:
                    m = float(raw)
        except Exception:
            m = None
        if m is not None:
            self._min_quote_cache[product_id] = m
        return m

    # ---- signed requests -------------------------------------------------
    def _build_jwt(self, method, path):
        base = _REST_BASE
        if base.startswith('https://'):
            base = base[len('https://'):]
        payload = {
            'iss': 'cdp',
            'sub': self.api_key_name,
            'nbf': int(time.time()),
            'exp': int(time.time()) + 120,
            'uri': f'{method} {base}{path}',
        }
        return _monkey_jwt_encode(payload, self.private_key_pem,
                                  algorithm=self._jwt_alg)

    def _request(self, method, path, data=None):
        with self._net_lock:
            url = _REST_BASE + path
            headers = {
                'Authorization': 'Bearer ' + self._build_jwt(method, path),
                'Content-Type': 'application/json',
            }
            try:
                if data is not None:
                    r = requests.request(method, url, json=data, headers=headers, timeout=30)
                else:
                    r = requests.request(method, url, headers=headers, timeout=30)
                if r.status_code >= 400:
                    raise Exception(f'Coinbase API error {r.status_code}: {r.text} [URL:{method} {url}]')
                return r.json()
            except Exception as e:
                raise Exception(f'{e} [URL: {method} {url}]')

    # ---- balances ---------------------------------------------------------
    def get_quote_balance(self, product_id):
        """Get quote currency balance with retry on transient failures.
        Returns 0.0 only after all retries exhausted (not on a single glitch)."""
        quote_cur = self.quote_currency(product_id)
        last_err = None
        for attempt in range(3):
            try:
                portfolios = self._request('GET', '/portfolios')
                p_uuid = portfolios.get('portfolios', [{}])[0].get('uuid', '')
                if p_uuid:
                    breakdown = self._request('GET', f'/portfolios/{p_uuid}')
                    for pos in breakdown.get('breakdown', {}).get('spot_positions', []):
                        if (pos.get('asset') or '').upper() == quote_cur:
                            val = float(pos.get('available_to_trade_fiat', 0))
                            if val > 0:
                                return val
                    for pos in breakdown.get('breakdown', {}).get('spot_positions', []):
                        if (pos.get('asset') or '').upper() in ('USDC', 'USD'):
                            val = float(pos.get('available_to_trade_fiat', 0))
                            if val > 0:
                                return val
                # Portfolios returned but no match — try /accounts fallback
                accounts = self._request('GET', '/accounts')
                balances = {}
                for acct in accounts.get('accounts', []):
                    cur = (acct.get('currency') or '').upper()
                    bal = float(acct.get('available_balance', {}).get('value', 0))
                    balances[cur] = balances.get(cur, 0.0) + bal
                return balances.get(quote_cur, balances.get('USDC', balances.get('USD', 0.0)))
            except Exception as e:
                last_err = e
                # Retry on transient errors (429, 5xx, timeout)
                if attempt < 2:
                    time.sleep(1 * (2 ** attempt))  # 1s, 2s backoff
        # All retries failed — log and return 0.0
        try:
            import logging
            logging.getLogger('sbt').warning(
                f'get_quote_balance failed after 3 retries: {last_err}')
        except Exception:
            pass
        return 0.0

    def get_real_base_balance(self, product_id):
        """Real base balance held for this pair, or 0.0 if the account
        verifiably has none, or None if the read FAILED (caller must not trade
        on an unknown position). Read-succeeded-but-not-found must be 0.0, NOT
        None — None is reserved for actual read failure (see BUG-007)."""
        base = self.base_currency(product_id)
        try:
            p = self._request('GET', '/portfolios')
            uuid = p.get('portfolios', [{}])[0].get('uuid', '')
            if uuid:
                bd = self._request('GET', f'/portfolios/{uuid}')
                for sp in bd.get('breakdown', {}).get('spot_positions', []):
                    if (sp.get('asset') or '').upper() == base:
                        bal = float(sp.get('available_to_trade_crypto', 0))
                        if bal <= 0:
                            bal = float(sp.get('total_balance_crypto', 0))
                        return bal
                # read succeeded, no such asset -> verified flat
                return 0.0
        except Exception:
            pass
        return None

    # ---- order history / cost basis --------------------------------------
    def _list_fills(self):
        """Transaction-level fills straight from the exchange. The signed JWT
        401s on query-string request targets (dev-verified), so cursor
        pagination is attempted FIRST; if the signature rejects the query
        string we gracefully fall back to the single page (BUG-004 limiter)
        rather than breaking auth. Fee-inclusive fill data; the fee vs avg
        question is handled by get_cost_basis, not here."""
        try:
            resp = self._request('GET', '/orders/historical/fills?cursor=0')
            page = resp.get('fills') or []
            if resp.get('cursor') or page:
                return self._paginate_fills(resp)
        except Exception:
            pass
        # Signed-query-string rejected (legacy auth): single page, no cursor.
        try:
            resp = self._request('GET', '/orders/historical/fills')
            return resp.get('fills', [])
        except Exception:
            return []

    def _paginate_fills(self, first):
        hits = list(first.get('fills', []) or [])
        cursor = first.get('cursor', '')
        seen = set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            try:
                resp = self._request('GET', f'/orders/historical/fills?cursor={cursor}')
            except Exception:
                break
            hits.extend(resp.get('fills', []) or [])
            cursor = resp.get('cursor', '')
        return hits

    def get_cost_basis(self, product_id, real_base):
        """Reconstruct the TRUE average entry of the current holding from the
        exchange's own fills, FEE-EXCLUDED (matches the 'average entry' the
        exchange's site/API reports — that number already reflects true cost):
        BUY fills add base + quote cost (commission NOT added), SELL fills
        remove base FIFO (releasing cost proportionally). Returns
        (entry_price, base) or None if it can't be determined."""
        try:
            fills = self._list_fills()
        except Exception:
            return None
        fills = [f for f in fills if (f.get('product_id') or '').upper() == product_id.upper()]
        fills.sort(key=lambda f: f.get('trade_time') or '')
        lots = []
        for f in fills:
            side = (f.get('side') or '').upper()
            price = float(f.get('price') or 0)
            raw = float(f.get('size') or 0)
            if raw <= 0:
                continue
            in_quote = bool(f.get('size_in_quote'))
            base = raw / price if (in_quote and price > 0) else raw
            if base <= 0:
                continue
            cost = (raw if in_quote else base * price)
            if side == 'BUY':
                lots.append([base, cost])
            elif side == 'SELL':
                rem = base
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
            with self._net_lock:
                r = requests.get(f'{_PUBLIC_EXCHANGE}/products/{product_id}', timeout=10)
            return r.status_code == 200
        except Exception:
            return None

    # ---- all pairs (Settings type-ahead dropdown) -------------------------
    def list_products(self):
        """All TRADEABLE BASE-QUOTE pairs from the public exchange products
        list (no auth needed), cached 10 min so the Settings dialog opens fast
        without hammering the API. Excludes delisted / trading_disabled pairs
        (BUG-015: the dropdown previously offered 310 dead markets)."""
        now = time.time()
        try:
            if self._products is not None and now - self._products_time < 600:
                return self._products
            with self._net_lock:
                r = requests.get(f'{_PUBLIC_EXCHANGE}/products', timeout=15)
            if r.status_code != 200:
                return self._products or []
            prods = sorted({
                p.get('id') for p in r.json()
                if p.get('id')
                and p.get('status') == 'online'
                and not p.get('trading_disabled')
            })
            self._products = prods
            self._products_time = now
            return prods
        except Exception:
            return self._products or []

    # ---- prices -----------------------------------------------------------
    def _public_book(self, product_id):
        with self._net_lock:
            r = requests.get(f'{_REST_BASE}/market/product_book',
                             params={'product_id': product_id}, timeout=10)
            if r.status_code >= 400:
                raise Exception(f'Coinbase API error {r.status_code}: {r.text}')
            return r.json()

    def get_best_ask(self, product_id):
        resp = self._public_book(product_id)
        pb = resp.get('pricebook', resp)
        asks = pb.get('asks', [])
        if asks:
            if isinstance(asks[0], list):
                return float(asks[0][0])
            return float(asks[0]['price'])
        return 0.0

    def get_best_bid(self, product_id):
        resp = self._public_book(product_id)
        pb = resp.get('pricebook', resp)
        bids = pb.get('bids', [])
        if bids:
            if isinstance(bids[0], list):
                return float(bids[0][0])
            return float(bids[0]['price'])
        return 0.0

    # ---- orders -----------------------------------------------------------
    def _min_quote_size(self, product_id):
        """Order floor in quote currency: the exchange's real minimum
        (min_market_funds), or the user's min_quote_size setting if higher.
        Falls back to 2.0 only when the API value is unknown — the guardrail
        never goes below an enforced exchange minimum."""
        try:
            api_min = self.get_min_quote_size(product_id)
        except Exception:
            api_min = None
        user_min = 2.0
        try:
            with open(paths.SETTINGS_PATH) as f:
                user_min = float(json.load(f).get('min_quote_size', 0.0) or 0.0)
        except Exception:
            user_min = 0.0
        if api_min is not None and api_min > 0:
            return max(api_min, user_min) if user_min > 0 else api_min
        return user_min if user_min > 0 else 2.0

    def _normalize_order_response(self, resp, kind='Order'):
        """Coinbase nests the exchange order id at success_response.order_id
        (live-probed 2026-08-18; the old code looked for a top-level order_id
        / 'order' key that does not exist, so the engine ALWAYS fell back to
        tracking its own client_order_id — status/cancel by client id then
        400/404'd, every limit read 'unreachable', and the bot stacked
        duplicate live orders). This normalizer:
          1. raises the exchange's rejection when success=False
          2. hoists success_response.order_id to resp['order_id']
          3. RAISES when success=True but no exchange order id is present —
             the caller must never track an order it cannot query or cancel.
        """
        if not isinstance(resp, dict):
            raise Exception(f'{kind} failed: unexpected response type')
        if resp.get('success') is False:
            err = resp.get('error_response', {}) or {}
            details = err.get('error_details', '')
            reason = err.get('new_order_failure_reason', '')
            msg = err.get('message', err.get('error', 'unknown error'))
            raise Exception(
                f'{kind} rejected: {msg}'
                + (f' ({details})' if details and details != msg else '')
                + (f' [reason={reason}]' if reason else ''))
        oid = (resp.get('order_id')
               or (resp.get('success_response') or {}).get('order_id')
               or (resp.get('order') or {}).get('order_id'))
        if not oid:
            raise Exception(f'{kind} failed: no exchange order_id in response')
        resp['order_id'] = oid
        # keep the legacy top-level convenience fields when present
        order = resp.get('order') or {}
        for k in ('average_filled_price', 'filled_size', 'status'):
            if k not in resp and k in order:
                resp[k] = order[k]
        return resp

    def create_market_order(self, product_id, side, size, client_order_id=None):
        if client_order_id is None:
            client_order_id = str(time.time())
        qs = float(size)
        # NOTE: the ENGINE owns all size/minimum logic — it truncates BASE units
        # down to the increment and floors at a CLEAN base amount whose quote
        # value >= the exchange minimum (_inc_ceil(min_quote/price)). This
        # provider must NOT re-apply a minimum here: a previous version compared
        # the BASE size against the QUOTE minimum (e.g. 0.00198855 ZEC < $1.00),
        # which inflated a $1 buy into 1.0 ZEC (~$500) — an order rejection or a
        # massive over-buy. Sizing stays solely in the engine.
        side_u = side.upper()
        inc = self.get_base_increment(product_id)
        dp = max(1, -int(__import__('math').log10(inc)))
        payload = {
            'client_order_id': client_order_id,
            'product_id': product_id,
            'side': side_u,
            'order_configuration': {
                'market_market_ioc': {'base_size': f"{qs:.{dp}f}", 'rfq_disabled': True}
            },
        }
        try:
            with open(paths.ORDER_DEBUG, 'a') as f:
                f.write(f'MARKET ORDER PAYLOAD: {json.dumps(payload)}\n')
        except Exception:
            pass
        resp = self._request('POST', '/orders', data=payload)
        try:
            with open(paths.ORDER_DEBUG, 'a') as f:
                f.write(f'MARKET ORDER RESPONSE: {json.dumps(resp)}\n')
        except Exception:
            pass
        return self._normalize_order_response(resp, kind='Market order')

    def create_limit_order(self, product_id, side, base_size, limit_price,
                           post_only=True, client_order_id=None):
        if client_order_id is None:
            client_order_id = str(time.time())
        payload = {
            'client_order_id': client_order_id,
            'product_id': product_id,
            'side': side.upper(),
            'order_configuration': {
                'limit_limit_gtc': {
                    'base_size': str(base_size),
                    'limit_price': str(limit_price),
                    'post_only': post_only,
                }
            },
        }
        resp = self._request('POST', '/orders', data=payload)
        return self._normalize_order_response(resp, kind='Limit order')

    def find_order_by_client_id(self, client_order_id):
        """Recover the EXCHANGE order record for one of our client_order_ids
        (recovery path for pending entries written before order-id tracking
        was fixed 2026-08-18). Uses /orders/historical/batch WITHOUT a query
        string — the signed JWT 401s on query params (BUG-004) and the plain
        endpoint returns the recent 1000 orders. Returns the order dict or
        None."""
        try:
            resp = self._request('GET', '/orders/historical/batch')
            for o in (resp.get('orders') or []):
                if o.get('client_order_id') == client_order_id:
                    return o
        except Exception:
            pass
        return None

    def cancel_order(self, order_id):
        """Cancel an open GTC order (limit path). True if the exchange reports
        it cancelled (or already gone)."""
        try:
            resp = self._request('POST', '/orders/batch_cancel',
                                 data={'order_ids': [order_id]})
            results = resp.get('results', []) or []
            return any(r.get('order_id') == order_id
                       and r.get('success') is True for r in results)
        except Exception:
            return False

    def get_order_status(self, order_id):
        """Status of an order. Path-based GET (no query string — keeps the
        signed JWT valid). Returns status/filled_size/average_filled_price.
        The order record may omit `average_filled_price` — when it does, the
        average is computed from `filled_value / filled_size` so a limit fill
        uses the TRUE fill price (the engine falls back to the limit price only
        if neither is available)."""
        resp = self._request('GET', f'/orders/historical/{order_id}')
        order = resp.get('order', {}) or {}
        filled = float(order.get('filled_size') or 0)
        avg = float(order.get('average_filled_price') or 0)
        if avg <= 0 and filled > 0:
            fv = float(order.get('filled_value') or 0)
            if fv > 0:
                avg = fv / filled
        return {
            'status': (order.get('status') or '').upper(),
            'filled_size': filled,
            'average_filled_price': avg,
        }

    # ---- fees -------------------------------------------------------------
    def get_trading_fee(self):
        now = time.time()
        if self._fee_cache is not None and now - self._fee_cache_time < 300:
            return self._fee_cache
        try:
            resp = self._request('GET', '/transaction_summary')
            fee = float(resp.get('fee_tier', {}).get('taker_fee_rate', _DEFAULT_FEE))
            self._fee_cache = fee
            self._fee_cache_time = now
            return fee
        except Exception:
            return _DEFAULT_FEE

    # ---- price feed -------------------------------------------------------
    def start_price_feed(self, product_id, on_price, on_connection_change):
        self.stop_price_feed()
        self._feed = CoinbasePriceFeed(self.api_key_name, self.private_key_pem,
                                       product_id, jwt_alg=self._jwt_alg)
        self._feed.on_price = on_price
        self._feed.on_connection_change = on_connection_change
        self._feed.start()

    def stop_price_feed(self):
        if self._feed:
            try:
                self._feed.stop()
            except Exception:
                pass
            self._feed = None

    def describe(self):
        return 'Coinbase'
