"""Settings load/save — ported from the original config_manager."""
import json
import os

from . import paths

DEFAULT_SETTINGS = {
    'exchange': 'coinbase',      # provider name — add adapters in sbt/providers
    'product_id': '',
    'trade_size_pct': 0.5,
    'dip_bounce_pct': 0.01,
    'trailing_stop_pct': 0.01,
    'take_profit_pct': 0.05,     # mark: entry*(1+tp%) arms the trailing stop
    'maker_retries': 0,          # market orders only (limit path disabled)
    'maker_timeout_ms': 5000,    # how long a limit order waits before fallback
    # Per-side limit orders (post_only maker) + their fallback-to-market:
    'limit_order_buy': False,
    'fallback_to_taker_buy': True,
    'limit_order_sell': False,
    'fallback_to_taker_sell': True,
    # Safety nets for limit orders (user, 2026-08-09):
    'keep_open_buy_for_dca': False,   # keep an unfilled BUY limit for DCA/rearm
    'cancel_sell_below_floor': True,  # cancel a limit SELL if price drops below floor
    'auto_rearm': True,
    'log_level': 'INFO',
    # comma-separated quote currencies the user can actually trade
    # (e.g. "USD,USDC") — filters the Settings pair dropdown. Empty = all.
    'quote_currencies': '',
    # UI theme: 'auto' (desktop detection) / 'light' / 'dark' — user-toggleable
    # in Settings so the UI can be reviewed in either theme.
    'theme': 'auto',
    # OPT-IN: send crash/error reports to the developer's dedicated GitHub
    # reports repo (privacy-first — default OFF). The report is scrubbed and
    # the file stays locally for the user to review.
    'report_opt_in': False,
    # AUTO-UPDATE (runs ONLY while the app is open — never a background
    # process). Check = metadata-only GitHub releases check (approved privacy
    # exception). Install default OFF = "check + notify only"; turning it on
    # auto-installs with a heads-up, then relaunches and returns to state.
    'auto_update_check': True,
    'auto_update_install': False,
    # EXCHANGE MAINTENANCE NOTICES (user 2026-08-16): polls the exchange's
    # own status endpoint every 30 min and shows a banner at the top of the
    # in-app log when maintenance / an incident is active. Default ON.
    # Outbound = exchange's own infrastructure, never cloud services.
    'maintenance_check': True,
    # Holding mode (already-holding detection): '' = unset (prompt when a
    # holding is found); 'separate' = never touch coins the bot didn't buy
    # (SAFE default); 'combine' = adopt + manage the whole holding (today's
    # behavior); 'auto_invest' = buy-only takeover of exchange auto-invest
    # (buys dips at a better cost basis, never auto-sells, exit via Force
    # Close with a % of holdings editable).
    'holding_mode': '',
    # Seconds between REST polls of a resting limit order. 3s keeps REST low so
    # many auto-invest bots sharing one key never flag the exchange (the
    # dashboard refresh stays at 1s; only the order poll is throttled).
    'order_poll_interval_s': 3.0,
    # Capitulation — single-trade stop-loss for a bleeder: a % BELOW entry at
    # which the bot gives up and force-closes at market. DEFAULT OFF (0).
    # Only honored in SINGLE mode (DCA off) — DCA averages down and never
    # force-closes at a loss; auto-invest never auto-sells. The editable floor
    # is Trailing Stop % + 1%, so it can never be set as a deep loss.
    'capitulation_pct': 0.0,
}


def load_settings():
    data = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(paths.SETTINGS_PATH):
            with open(paths.SETTINGS_PATH, 'r') as f:
                data.update(json.load(f))
    except Exception:
        pass
    return data


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(paths.SETTINGS_PATH), exist_ok=True)
        with open(paths.SETTINGS_PATH, 'w') as f:
            json.dump(data, f, indent=4)
        paths.secure_file(paths.SETTINGS_PATH)
    except Exception:
        pass
