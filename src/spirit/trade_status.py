
"""
trade_status.py

Checks for open trades and account balance at startup via ExchangeProvider.
"""

from spirit.logger import get_logger


def check_open_trades_and_balance():
    """
    Checks exchange for open trades and account balance. Logs both.
    Returns tuple: (open_orders, balances)
    """
    logger = get_logger("trade_status")
    open_orders = None
    balances = None
    try:
        from spirit.exchange import get_exchange_provider
        ep = get_exchange_provider()
        open_orders_list = ep.get_open_orders()
        if open_orders_list:
            logger.info(f"Found {len(open_orders_list)} open trades: {[o.txid for o in open_orders_list]}")
        else:
            logger.info("No open trades found.")
        # Return in legacy format for callers that expect dict
        open_orders = {'open': {o.txid: o.raw or {} for o in open_orders_list}}
    except Exception as e:
        logger.error(f"Error checking open trades: {e}")
    try:
        from spirit.exchange import get_exchange_provider
        ep = get_exchange_provider()
        balances = ep.get_balance()
        logger.info(f"Account balances: {balances}")
    except Exception as e:
        logger.error(f"Error fetching account balances: {e}")
    return open_orders, balances

if __name__ == "__main__":
    check_open_trades_and_balance()
