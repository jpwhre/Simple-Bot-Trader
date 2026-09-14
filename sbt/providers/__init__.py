from .base import Provider
from .ccxt import CcxtProvider
from .coinbase import CoinbaseProvider


def create_provider(exchange, api_key_name='', private_key_pem='',
                    ccxt_api_key='', ccxt_secret='', ccxt_password=''):
    """Factory. 'coinbase' uses the native adapter; any other exchange id
    (kraken, binance, kucoin, ...) routes through the CCXT-backed adapter.
    Trading conditions live in the engine — the adapter only executes/reads."""
    exchange = (exchange or 'coinbase').lower()
    if exchange == 'coinbase':
        return CoinbaseProvider(api_key_name, private_key_pem)
    return CcxtProvider(exchange, ccxt_api_key, ccxt_secret, ccxt_password)
