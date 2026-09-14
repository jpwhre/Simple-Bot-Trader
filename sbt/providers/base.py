"""Exchange provider contract.

The engine talks ONLY to this interface — no exchange-specific code anywhere
in the engine, config, or UI. Each exchange is one adapter module.
"""


class Provider:
    name = 'Provider'

    # ---- market info -----------------------------------------------------
    def quote_currency(self, product_id):
        raise NotImplementedError

    def base_currency(self, product_id):
        raise NotImplementedError

    def get_base_increment(self, product_id):
        raise NotImplementedError

    def get_price_increment(self, product_id):
        """Minimum price step for limit orders (e.g. 0.01 for2-decimal fiat
        pairs). Falls back to 0.01."""
        return 0.01

    def get_min_quote_size(self, product_id):
        """Minimum market-order size in the QUOTE currency for this pair
        (what the exchange actually enforces — Coinbase `min_market_funds`,
        ccxt `limits.cost.min`). None if it can't be determined; callers fall
        back to a safe default (never trade below an unknown minimum)."""
        return None

    # ---- balances ---------------------------------------------------------
    def get_quote_balance(self, product_id):
        raise NotImplementedError

    def get_real_base_balance(self, product_id):
        raise NotImplementedError

    # ---- prices -----------------------------------------------------------
    def get_best_ask(self, product_id):
        raise NotImplementedError

    def get_best_bid(self, product_id):
        raise NotImplementedError

    # ---- orders -----------------------------------------------------------
    def create_market_order(self, product_id, side, size, client_order_id=None):
        raise NotImplementedError

    def create_limit_order(self, product_id, side, base_size, limit_price,
                           post_only=True, client_order_id=None):
        raise NotImplementedError

    def cancel_order(self, order_id):
        """Cancel an open GTC limit order by id. Returns True on success."""
        raise NotImplementedError

    def get_order_status(self, order_id):
        """Status of an order. Returns a dict:
        {status, filled_size, average_filled_price}
        with status in (FILLED, PARTIALLY_FILLED, OPEN, CANCELED, EXPIRED,
        REJECTED). Raises on an unreachable exchange (caller retries)."""
        raise NotImplementedError

    # ---- fees -------------------------------------------------------------
    def get_trading_fee(self):
        raise NotImplementedError

    # ---- cost basis / market validity -------------------------------------
    def get_cost_basis(self, product_id, real_base):
        """TRUE fee-inclusive cost basis of the current holding from the
        exchange's own records, or None if unavailable."""
        return None

    def product_valid(self, product_id):
        """True if the pair is supported by this exchange, False if not,
        None if it can't be determined."""
        return None

    def list_products(self):
        """All supported BASE-QUOTE pairs (alphabetical) for the Settings
        type-ahead dropdown, or [] if unavailable."""
        return []

    # ---- price feed -------------------------------------------------------
    def start_price_feed(self, product_id, on_price, on_connection_change):
        raise NotImplementedError

    def stop_price_feed(self):
        raise NotImplementedError

    def describe(self):
        return self.name
