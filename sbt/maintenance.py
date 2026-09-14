"""Exchange maintenance notices — a background checker that polls the running
exchange's status endpoint and surfaces maintenance / incident warnings at the
TOP of the in-app log.

Checks happen:
  1. At bot startup (immediately after start)
  2. Every 30 minutes while the bot is running

Uses CCXT's built-in fetch_status() where available (most curated exchanges).
Falls back gracefully: if fetch_status returns empty / raises, nothing is shown.

The user sees a line like:
  ⚠ MAINTENANCE: Coinbase is undergoing scheduled maintenance (est. 2h remaining)
and the banner auto-clears when status returns to normal.

Standing requirement (user 2026-08-16): default ON; toggleable in Settings.
All outbound calls are to the EXCHANGE's own infrastructure — never cloud.
"""
import logging
import time
import threading

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_S = 30 * 60  # 30 minutes between background checks
_TIMEOUT_S = 5               # per-check network timeout (seconds)
_BANNER_PREFIX = '⚠ MAINTENANCE'


def _fetch_status(exchange_id):
    """Return a short human-readable maintenance string, or empty if OK.

    Uses CCXT's fetch_status() where supported.  Returns '' when:
      - the exchange has no status endpoint
      - fetch_status returns no incident / maintenance info
      - any network / timeout error occurs (silent failure — never crashes)
    """
    try:
        import ccxt
        if not hasattr(ccxt, exchange_id):
            return ''
        ex_cls = getattr(ccxt, exchange_id)
        ex = ex_cls({'enableRateLimit': True, 'timeout': _TIMEOUT_S * 1000})
        result = ex.fetch_status()
        if not isinstance(result, dict):
            return ''
        # CCXT fetch_status returns dicts like:
        #   {'status': 'ok', ...} or {'status': 'maintenance', 'eta': 120, ...}
        status = str(result.get('status', '')).lower()
        if status in ('ok', 'online', 'operational', 'up'):
            return ''
        # Build a short status string from whatever the exchange provides
        eta = result.get('eta', result.get('eta_seconds'))
        msg = result.get('info', {}).get('msg', '') or result.get('comment', '')
        parts = [f'{exchange_id.upper()} status: {status}']
        if msg:
            parts.append(msg)
        if eta and isinstance(eta, (int, float)):
            parts.append(f'est. {int(eta // 60)}min remaining')
        return ' | '.join(parts)
    except Exception:
        return ''


class MaintenanceMonitor:
    """Non-blocking daemon thread that polls exchange status periodically.

    Parameters
    ----------
    bot : TradingBot
        The live bot instance.  Has .settings, ._log(msg, user=True),
        and .settings.get('maintenance_check', True).
    """

    def __init__(self, bot):
        self._bot = bot
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Launch the background check loop (if enabled in settings)."""
        if not self._bot.settings.get('maintenance_check', True):
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Tell the background thread to exit on next iteration."""
        self._stop.set()

    # --- internal -----------------------------------------------------------

    def _loop(self):
        exchange = (self._bot.settings.get('exchange', '') or '').strip().lower()
        # Run first check immediately on startup
        self._check_and_report(exchange)
        while not self._stop.is_set():
            self._stop.wait(_CHECK_INTERVAL_S)
            if self._stop.is_set():
                break
            self._check_and_report(exchange)

    def _check_and_report(self, exchange):
        status = _fetch_status(exchange)
        # Thread-safe: maintenance_handler is wired to the dashboard's
        # set_maintenance() which pushes to the UI queue.
        handler = getattr(self._bot, '_maintenance_handler', None)
        if handler:
            try:
                handler(status)
            except Exception:
                pass
