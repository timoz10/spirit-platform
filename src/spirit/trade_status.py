
"""
trade_status.py

Responsible for checking the Kraken API for any open trades and logging account balance when the program starts.
"""

from logger import get_logger
from utils.kraken_api_client import get_kraken_balances, get_open_orders

def check_open_trades_and_balance():
    """
    Checks Kraken API for open trades and account balance. Logs both.
    Returns tuple: (open_orders, balances)
    """
    logger = get_logger("trade_status")
    open_orders = None
    balances = None
    try:
        open_orders = get_open_orders()
        open_orders_list = open_orders.get('open', {}) if open_orders else {}
        if open_orders_list:
            logger.info(f"Found {len(open_orders_list)} open trades: {list(open_orders_list.keys())}")
        else:
            logger.info("No open trades found.")
    except Exception as e:
        logger.error(f"Error checking open trades: {e}")
    try:
        balances = get_kraken_balances()
        logger.info(f"Account balances: {balances}")
    except Exception as e:
        logger.error(f"Error fetching account balances: {e}")
    return open_orders, balances

if __name__ == "__main__":
    check_open_trades_and_balance()
