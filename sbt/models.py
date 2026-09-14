"""State models — recovered from the compiled src.models.state."""
from enum import Enum


class BotStatus(Enum):
    WATCHING = 'WATCHING'
    HOLDING = 'HOLDING'
    SELLING = 'SELLING'
    BUYING = 'BUYING'
    ERROR = 'ERROR'
    STOPPED = 'STOPPED'


class BotState:
    def __init__(self):
        self.status = BotStatus.WATCHING
        self.position = None
        self.error_message = ''
        self.local_low = 0.0
        self.connection_status = ''
        self.current_price = 0.0
        self.usdc_balance = 0.0
        self.total_pnl = 0.0
        self.trade_count = 0


class Position:
    def __init__(self, product_id, entry_price, size_usdc, size_base, entry_time,
                 order_id='', highest_price=0.0, stop_price=0.0):
        self.product_id = product_id
        self.entry_price = entry_price
        self.size_usdc = size_usdc
        self.size_base = size_base
        self.entry_time = entry_time
        self.order_id = order_id
        self.highest_price = highest_price
        self.stop_price = stop_price
        self.pnl = 0.0
        self.pnl_pct = 0.0
        # Realized P&L of coins sold in an earlier partial limit-sell whose
        # remainder was kept (cancelled below floor). Carried on the position
        # so the final close books the FULL cycle P&L exactly once.
        self.partial_pnl = 0.0


class TradeRecord:
    def __init__(self, product_id, entry_order_id, exit_order_id, entry_price,
                 exit_price, entry_size, exit_size, entry_time, exit_time, status):
        self.product_id = product_id
        self.entry_order_id = entry_order_id
        self.exit_order_id = exit_order_id
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.entry_size = entry_size
        self.exit_size = exit_size
        self.entry_time = entry_time
        self.exit_time = exit_time
        self.status = status
        self.pnl = 0.0
        self.pnl_pct = 0.0
