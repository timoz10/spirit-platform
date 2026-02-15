import os
import requests
import time
import hashlib
import hmac
import base64

def _get_env_or_file(name: str) -> str | None:
    """
    Return secret from environment NAME or file pointed by NAME_FILE.
    Files are read once at import; whitespace is stripped. Returns None if unset.
    """
    val = os.environ.get(name)
    if val:
        return val.strip()
    file_path = os.environ.get(f"{name}_FILE")
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            # Don't raise at import time; defer to runtime checks
            return None
    return None

KRAKEN_API_KEY = _get_env_or_file('KRAKEN_API_KEY')
KRAKEN_API_SECRET = _get_env_or_file('KRAKEN_API_SECRET')
# Base URL for all Kraken API calls (public and private)
KRAKEN_API_URL = os.environ.get('KRAKEN_API_BASE_URL', 'https://api.kraken.com')


# Import config values
from system_config import KRAKEN_PAIR, KRAKEN_OHLC_COUNT, KRAKEN_OHLC_INTERVAL


# Helper to sign requests
def _sign_kraken(path, data, secret, nonce):
    postdata = data.copy()
    postdata['nonce'] = nonce
    postdata = '&'.join([f"{k}={v}" for k, v in postdata.items()])
    encoded = (str(nonce) + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest())

# Public OHLC fetch for KRAKEN_PAIR with configurable interval
def get_ohlc_data(count=KRAKEN_OHLC_COUNT, interval=KRAKEN_OHLC_INTERVAL, pair=KRAKEN_PAIR, only_closed=True):
    """
    Fetch the most recent `count` candles for the specified pair from Kraken public API.
    Interval is set by system_config.py (e.g., 1 for 1min, 15 for 15min).
    Returns a list of dicts with keys: datetime, open, high, low, close, volume, count
    """
    url = f"{KRAKEN_API_URL}/0/public/OHLC"
    params = {
        'pair': pair,
        'interval': interval
    }
    from logger import logger
    import os
    logger.debug(f"[get_ohlc_data] Requesting: url={url} params={params} (interval type={type(interval)})")
    headers = {
        'User-Agent': os.environ.get('KRAKEN_BOT_USER_AGENT', 'kraken-bot/1.0 (+https://example.local)')
    }
    verbose = os.environ.get('DEBUG_VERBOSE', '').lower() in ['1', 'true', 'yes']
    max_retries = int(os.environ.get('KRAKEN_API_MAX_RETRIES', 3))
    backoff_base = float(os.environ.get('KRAKEN_API_BACKOFF_BASE', 1.5))
    attempt = 0
    last_exc = None
    while attempt < max_retries:
        try:
            response = requests.get(url, params=params, headers=headers, timeout=20)
            logger.debug(f"[get_ohlc_data] HTTP {response.status_code} for pair={pair} interval={interval}")
            if response.status_code == 429:
                # Rate limited by edge/proxy
                ra = response.headers.get('Retry-After')
                logger.warning(f"[API] HTTP 429 Rate Limited. Retry-After={ra}. attempt={attempt+1}/{max_retries}")
                import time as _t
                sleep_s = (backoff_base ** attempt)
                if ra:
                    try:
                        sleep_s = max(sleep_s, float(ra))
                    except Exception:
                        pass
                _t.sleep(sleep_s)
                attempt += 1
                continue
            response.raise_for_status()
            if verbose:
                logger.debug(f"[get_ohlc_data] Raw response: {response.text[:1000]}...")
            result = response.json()
            if result.get('error'):
                errs = result['error']
                msg = '; '.join(errs)
                # Kraken sometimes reports rate limit in error array on private endpoints; guard anyway
                if any('rate' in e.lower() for e in errs):
                    logger.warning(f"[API] Kraken error indicates rate limiting: {msg}. attempt={attempt+1}/{max_retries}")
                    import time as _t
                    _t.sleep(backoff_base ** attempt)
                    attempt += 1
                    continue
                raise RuntimeError(f"Kraken API error: {errs}")
            break
        except requests.RequestException as e:
            last_exc = e
            # Backoff on transient network/5xx
            status = getattr(e.response, 'status_code', None) if hasattr(e, 'response') else None
            logger.warning(f"[API] RequestException status={status} attempt={attempt+1}/{max_retries}: {e}")
            import time as _t
            _t.sleep(backoff_base ** attempt)
            attempt += 1
        except Exception as e:
            last_exc = e
            logger.error(f"[API] Unexpected error on get_ohlc_data: {e}")
            raise
    else:
        # Exhausted retries
        raise RuntimeError(f"Failed to fetch OHLC after {max_retries} attempts: {last_exc}")
    # Dynamically get the pair key (ignore 'last')
    pair_key = next(k for k in result['result'].keys() if k != 'last')
    ohlc_data = result['result'][pair_key]
    # Each row: [time, open, high, low, close, vwap, volume, count]
    candles = []
    for row in ohlc_data[-count:]:
        candles.append({
            'datetime': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(int(row[0]))),
            'timestamp': int(row[0]),
            'open': float(row[1]),
            'high': float(row[2]),
            'low': float(row[3]),
            'close': float(row[4]),
            'vwap': float(row[5]),
            'volume': float(row[6]),
            'count': int(row[7])
        })
    # Optionally drop the last candle if it appears to be open
    if only_closed and candles:
        import pandas as pd
        interval_minutes = int(interval)
        now = pd.Timestamp.utcnow().floor(f'{interval_minutes}min')
        if now.tzinfo is None:
            now = now.tz_localize('UTC')
        open_candle_threshold = now - pd.Timedelta(minutes=interval_minutes)
        last_ts = pd.to_datetime(candles[-1]['datetime'])
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize('UTC')
        from logger import logger
        logger.debug(f"[API][DEBUG] last_ts: {last_ts} (tz-aware={last_ts.tzinfo is not None}), open_candle_threshold: {open_candle_threshold}")
        if last_ts >= open_candle_threshold:
            logger.warning(f"[API] Dropping last row {last_ts} — appears to be open/incomplete candle.")
            candles = candles[:-1]
    return candles

def get_ticker(pair=KRAKEN_PAIR):
    """Fetch bid/ask/last from Kraken public Ticker API (no auth needed)."""
    url = f"{KRAKEN_API_URL}/0/public/Ticker"
    headers = {
        'User-Agent': os.environ.get('KRAKEN_BOT_USER_AGENT', 'kraken-bot/1.0 (+https://example.local)')
    }
    response = requests.get(url, params={'pair': pair}, headers=headers, timeout=10)
    response.raise_for_status()
    result = response.json()
    if result.get('error'):
        raise RuntimeError(f"Kraken Ticker API error: {result['error']}")
    data = list(result['result'].values())[0]
    return {
        'ask': float(data['a'][0]),
        'bid': float(data['b'][0]),
        'last': float(data['c'][0]),
    }


# Fetch account balances
def get_kraken_balances():
    if not KRAKEN_API_KEY or not KRAKEN_API_SECRET:
        raise RuntimeError("Kraken API key/secret not set in environment variables.")
    path = '/0/private/Balance'
    url = KRAKEN_API_URL + path
    nonce = str(int(time.time() * 1000))
    data = {'nonce': nonce}
    headers = {
        'API-Key': KRAKEN_API_KEY,
        'API-Sign': _sign_kraken(path, data, KRAKEN_API_SECRET, nonce)
    }
    response = requests.post(url, headers=headers, data=data)
    response.raise_for_status()
    result = response.json()
    if result.get('error'):
        raise RuntimeError(f"Kraken API error: {result['error']}")
    return result['result']

def place_order(pair, side, volume, price=None, ordertype='market', stop_loss=None, take_profit=None, validate=False):
    """
    Place a buy/sell order on Kraken.
    side: 'buy' or 'sell'
    ordertype: 'market' or 'limit'
    stop_loss: price for stop loss (optional)
    take_profit: price for take profit (optional)
    validate: if True, Kraken will validate only (no real order)
    """
    if not KRAKEN_API_KEY or not KRAKEN_API_SECRET:
        raise RuntimeError("Kraken API key/secret not set in environment variables.")
    path = '/0/private/AddOrder'
    url = KRAKEN_API_URL + path
    nonce = str(int(time.time() * 1000))
    data = {
        'nonce': nonce,
        'pair': pair,
        'type': side,
        'ordertype': ordertype,
        'volume': str(volume),
        'validate': 'true' if validate else 'false'
    }
    if price and ordertype == 'limit':
        data['price'] = str(price)
    if stop_loss:
        data['stopprice'] = str(stop_loss)
        data['close[ordertype]'] = 'stop-loss'
        data['close[price]'] = str(stop_loss)
    if take_profit:
        data['close[ordertype]'] = 'take-profit'
        data['close[price]'] = str(take_profit)
    headers = {
        'API-Key': KRAKEN_API_KEY,
        'API-Sign': _sign_kraken(path, data, KRAKEN_API_SECRET, nonce)
    }
    response = requests.post(url, headers=headers, data=data)
    response.raise_for_status()
    result = response.json()
    if result.get('error'):
        raise RuntimeError(f"Kraken API error: {result['error']}")
    return result['result']

def close_order(txid):
    """
    Cancel/close an order by transaction ID.
    """
    if not KRAKEN_API_KEY or not KRAKEN_API_SECRET:
        raise RuntimeError("Kraken API key/secret not set in environment variables.")
    path = '/0/private/CancelOrder'
    url = KRAKEN_API_URL + path
    nonce = str(int(time.time() * 1000))
    data = {'nonce': nonce, 'txid': txid}
    headers = {
        'API-Key': KRAKEN_API_KEY,
        'API-Sign': _sign_kraken(path, data, KRAKEN_API_SECRET, nonce)
    }
    response = requests.post(url, headers=headers, data=data)
    response.raise_for_status()
    result = response.json()
    if result.get('error'):
        raise RuntimeError(f"Kraken API error: {result['error']}")
    return result['result']

def get_open_orders():
    """
    Fetch all open orders.
    """
    if not KRAKEN_API_KEY or not KRAKEN_API_SECRET:
        raise RuntimeError("Kraken API key/secret not set in environment variables.")
    path = '/0/private/OpenOrders'
    url = KRAKEN_API_URL + path
    nonce = str(int(time.time() * 1000))
    data = {'nonce': nonce}
    headers = {
        'API-Key': KRAKEN_API_KEY,
        'API-Sign': _sign_kraken(path, data, KRAKEN_API_SECRET, nonce)
    }
    response = requests.post(url, headers=headers, data=data)
    response.raise_for_status()
    result = response.json()
    if result.get('error'):
        raise RuntimeError(f"Kraken API error: {result['error']}")
    return result['result']

# Example usage
if __name__ == "__main__":
    print(get_kraken_balances())
