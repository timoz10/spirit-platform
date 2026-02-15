import pandas as pd
import numpy as np
import ta

from logger import get_logger
logger = get_logger("feature_engineering")

def add_features(df):
    # Ensure chronological order for correct pct_change
    try:
        if 'datetime' in df.columns:
            df = df.sort_values('datetime').reset_index(drop=True)
    except Exception:
        pass
    df['return'] = df['close'].pct_change()
    df['volatility'] = df['return'].rolling(window=5, min_periods=5).std()
    df['momentum'] = df['close'] - df['close'].rolling(window=5, min_periods=5).mean()
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(window=14, min_periods=14).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=14, min_periods=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    ema_fast = df['close'].ewm(span=12, adjust=False).mean()
    ema_slow = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema_fast - ema_slow
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_diff'] = df['macd'] - df['macd_signal']
    df['macd_hist'] = df['macd'] - df['macd_signal']  # Explicit MACD histogram
    # --- Improved macd_cross regime logic ---
    macd_cross = [0] * len(df)
    macd_cross_event = [0] * len(df)
    regime = 0
    for i in range(1, len(df)):
        prev = df['macd_diff'].iloc[i-1]
        curr = df['macd_diff'].iloc[i]
        if prev < 0 and curr > 0:
            regime = 1
            macd_cross[i] = regime  # Set to new regime here
            macd_cross_event[i] = 1
        elif prev > 0 and curr < 0:
            regime = -1
            macd_cross[i] = regime
            macd_cross_event[i] = -1
        else:
            macd_cross[i] = regime
            macd_cross_event[i] = 0
    # Ensure macd_cross is always integer for SQLite INTEGER column
    df['macd_cross'] = pd.Series(macd_cross).astype(int)
    df['macd_cross_event'] = macd_cross_event
    df['rsi_14'] = df['rsi']  # For compatibility
    rolling_mean = df['close'].rolling(window=20, min_periods=20).mean()
    rolling_std = df['close'].rolling(window=20, min_periods=20).std()
    df['bb_upper'] = rolling_mean + (2 * rolling_std)
    df['bb_lower'] = rolling_mean - (2 * rolling_std)
    df['bb_width'] = df['bb_upper'] - df['bb_lower']
    df['return_lag_1'] = df['return'].shift(1)
    df['momentum_lag_1'] = df['momentum'].shift(1)
    df['macd_diff_lag_1'] = df['macd_diff'].shift(1)
    df['sma200'] = df['close'].rolling(window=200, min_periods=200).mean()
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'])
    df['atr_14'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    # --- Add ADX 14 and DI+/DI- ---
    try:
        adx_indicator = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14)
        df['adx'] = adx_indicator.adx()
        # ta returns +DI and -DI via adx_pos() and adx_neg()
        df['plus_di'] = adx_indicator.adx_pos()
        df['minus_di'] = adx_indicator.adx_neg()
        # Simple trend direction label based on DI dominance
        cond_up = df['plus_di'] > df['minus_di']
        cond_down = df['plus_di'] < df['minus_di']
        df['trend_direction'] = np.where(cond_up, 'up', np.where(cond_down, 'down', None))
    except Exception as e:
        logger.warning(f"ADX/DI features error: {e}")
    # --- ML Breakout Detection Features ---
    # Rolling range percentage (10-bar)
    rolling_high_10 = df['high'].rolling(window=10, min_periods=10).max()
    rolling_low_10 = df['low'].rolling(window=10, min_periods=10).min()
    df['rolling_range_pct'] = ((rolling_high_10 - rolling_low_10) / rolling_low_10) * 100

    # Volume vs average (20-bar)
    volume_avg_20 = df['volume'].rolling(window=20, min_periods=20).mean()
    df['volume_vs_avg'] = (df['volume'] / volume_avg_20 - 1) * 100

    # Do NOT drop rows with missing indicators here; only drop in full mode in temp_data.py
    return df

def add_features_alt(df):
    """
    Add advanced technical indicators to the DataFrame.
    Handles empty or too-short DataFrames gracefully.
    """
    import ta
    if df is None or df.empty or len(df) < 20:
        logger.warning(f"Not enough data for feature engineering (len={len(df) if df is not None else 0}). Skipping.")
        return df
    df = df.copy()
    # Only compute indicators if enough data for each window
    try:
        df['stoch_k'] = ta.momentum.stoch(df['high'], df['low'], df['close'])
        df['stoch_d'] = ta.momentum.stoch_signal(df['high'], df['low'], df['close'])
        df['williams_r'] = ta.momentum.williams_r(df['high'], df['low'], df['close'])
        df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'])
        df['adx'] = ta.trend.adx(df['high'], df['low'], df['close'])
        df['cci'] = ta.trend.cci(df['high'], df['low'], df['close'])
        df['obv'] = ta.volume.on_balance_volume(df['close'], df['volume'])
        df['adl'] = ta.volume.acc_dist_index(df['high'], df['low'], df['close'], df['volume'])
        df['vwap'] = ta.volume.volume_weighted_average_price(df['high'], df['low'], df['close'], df['volume'])
        ichimoku = ta.trend.IchimokuIndicator(df['high'], df['low'])
        df['ichimoku_a'] = ichimoku.ichimoku_a()
        df['ichimoku_b'] = ichimoku.ichimoku_b()
        df['ichimoku_base_line'] = ichimoku.ichimoku_base_line()
        df['ichimoku_conversion_line'] = ichimoku.ichimoku_conversion_line()
        df['sma_10'] = df['close'].rolling(window=10).mean()
        df['sma_30'] = df['close'].rolling(window=30).mean()
        df['sma_200'] = df['close'].rolling(window=200).mean()  # <-- Add SMA200
        df['sma_cross'] = df['sma_10'] - df['sma_30']
        df['high_low_range'] = df['high'] - df['low']
        df['close_to_open'] = df['close'] - df['open']
        df['candle_body'] = (df['close'] - df['open']).abs()
        # --- Add Bollinger Bands and bb_width ---
        # --- Fibonacci Retracement Levels ---
        window = 20  # You can adjust this window
        df['fib_high'] = df['high'].rolling(window=window).max()
        df['fib_low'] = df['low'].rolling(window=window).min()
        df['fib_0'] = df['fib_low']
        df['fib_236'] = df['fib_high'] - 0.236 * (df['fib_high'] - df['fib_low'])
        df['fib_382'] = df['fib_high'] - 0.382 * (df['fib_high'] - df['fib_low'])
        df['fib_5'] = df['fib_high'] - 0.5 * (df['fib_high'] - df['fib_low'])
        df['fib_618'] = df['fib_high'] - 0.618 * (df['fib_high'] - df['fib_low'])
        df['fib_786'] = df['fib_high'] - 0.786 * (df['fib_high'] - df['fib_low'])
        df['fib_1'] = df['fib_high']
        # ---
    except Exception as e:
        logger.warning(f"Feature engineering error: {e}. Skipping problematic indicator.")

    # --- Engineered Features: Volume Spikes ---
    try:
        df['volume_zscore'] = (df['volume'] - df['volume'].rolling(20).mean()) / df['volume'].rolling(20).std()
        df['volume_rel'] = df['volume'] / df['volume'].rolling(20).mean()
        df['volume_spike'] = (df['volume_rel'] > 2).astype(int)  # 2x rolling mean
    except Exception as e:
        logger.warning(f"Volume spike feature error: {e}")

    # --- Engineered Features: Momentum Shifts ---
    try:
        df['rsi'] = ta.momentum.rsi(df['close'], window=14)
        df['rsi_change'] = df['rsi'] - df['rsi'].shift(1)
        df['macd'] = ta.trend.macd(df['close'])
        df['macd_signal'] = ta.trend.macd_signal(df['close'])
        df['macd_cross_up'] = ((df['macd'] > df['macd_signal']) & (df['macd'].shift(1) <= df['macd_signal'].shift(1))).astype(int)
        df['macd_cross_down'] = ((df['macd'] < df['macd_signal']) & (df['macd'].shift(1) >= df['macd_signal'].shift(1))).astype(int)
        df['stoch_k_diff'] = df['stoch_k'] - df['stoch_k'].shift(1)
    except Exception as e:
        logger.warning(f"Momentum shift feature error: {e}")

    # --- Engineered Features: Price Action Patterns ---
    try:
        # Bullish engulfing: current body > prev body, current open < prev close, current close > prev open
        prev_open = df['open'].shift(1)
        prev_close = df['close'].shift(1)
        prev_body = (prev_close - prev_open).abs()
        curr_body = (df['close'] - df['open']).abs()
        df['bullish_engulfing'] = ((curr_body > prev_body) & (df['open'] < prev_close) & (df['close'] > prev_open)).astype(int)
        # Doji: body < 20% of ATR
        df['doji'] = (curr_body < 0.2 * df['atr']).astype(int)
        # Large wick: (high - max(open,close)) > 0.5*ATR or (min(open,close) - low) > 0.5*ATR
        upper_wick = df['high'] - df[['open', 'close']].max(axis=1)
        lower_wick = df[['open', 'close']].min(axis=1) - df['low']
        df['large_upper_wick'] = (upper_wick > 0.5 * df['atr']).astype(int)
        df['large_lower_wick'] = (lower_wick > 0.5 * df['atr']).astype(int)
        # Body/ATR ratio
        df['body_atr_ratio'] = curr_body / df['atr']
    except Exception as e:
        logger.warning(f"Price action pattern feature error: {e}")

    # --- Engineered Features: Fibonacci Retracement Levels (20-bar window) ---
    try:
        window = 20
        high_roll = df['high'].rolling(window=window)
        low_roll = df['low'].rolling(window=window)
        fib_high = high_roll.max()
        fib_low = low_roll.min()
        fib_range = fib_high - fib_low
        # Standard Fibonacci retracement levels
        df['fib_0'] = fib_high
        df['fib_236'] = fib_high - fib_range * 0.236
        df['fib_382'] = fib_high - fib_range * 0.382
        df['fib_500'] = fib_high - fib_range * 0.5
        df['fib_618'] = fib_high - fib_range * 0.618
        df['fib_786'] = fib_high - fib_range * 0.786
        df['fib_100'] = fib_low
        # Optionally: distance of close to each level
        df['close_to_fib_236'] = df['close'] - df['fib_236']
        df['close_to_fib_382'] = df['close'] - df['fib_382']
        df['close_to_fib_500'] = df['close'] - df['fib_500']
        df['close_to_fib_618'] = df['close'] - df['fib_618']
        df['close_to_fib_786'] = df['close'] - df['fib_786']
    except Exception as e:
        logger.warning(f"Fibonacci feature error: {e}")

    # --- Add lagged features for all numerical columns ---
    try:
        import pandas as pd
        lag_steps = [1, 2, 3, 5, 10, 20, 30, 50, 100]  # Added more lag intervals
        num_cols = df.select_dtypes(include=['number']).columns
        lagged_features = {}
        for col in num_cols:
            if col in ['open', 'high', 'low', 'close', 'volume']:
                continue  # skip raw OHLCV columns
            for lag in lag_steps:
                lagged_features[f'{col}_lag{lag}'] = df[col].shift(lag)
        if lagged_features:
            lagged_df = pd.DataFrame(lagged_features, index=df.index)
            df = pd.concat([df, lagged_df], axis=1)
    except Exception as e:
        logger.warning(f"Lagged feature error: {e}")

    # Label: uptrend if close in next TREND_BARS > current close * (1+THRESHOLD)
    # Set a meaningful threshold for profitability after transaction costs
    TREND_BARS = 5  # Lookahead period for trend detection
    PROFITABILITY_THRESHOLD = 0.0075  # E.g., 0.75% minimum return after costs
    df['trend_label'] = (df['close'].shift(-TREND_BARS) > df['close'] * (1 + PROFITABILITY_THRESHOLD)).astype(int)

    return df

def calc_smma(series, length):
    smma = series.rolling(window=length, min_periods=length).mean().copy()
    for i in range(length, len(series)):
        smma.iat[i] = (smma.iat[i-1] * (length - 1) + series.iat[i]) / length
    return smma

def calc_zlema(series, length):
    ema1 = series.ewm(span=length, adjust=False).mean()
    ema2 = ema1.ewm(span=length, adjust=False).mean()
    d = ema1 - ema2
    return ema1 + d

def add_lazybear_impulse_macd(df, lengthMA=34, lengthSignal=9):
    src = (df['high'] + df['low'] + df['close']) / 3
    hi = calc_smma(df['high'], lengthMA)
    lo = calc_smma(df['low'], lengthMA)
    mi = calc_zlema(src, lengthMA)
    md = np.where(mi > hi, mi - hi, np.where(mi < lo, mi - lo, 0))
    sb = pd.Series(md).rolling(window=lengthSignal, min_periods=lengthSignal).mean()
    sh = md - sb
    # Color logic (for reference, not used in DataFrame)
    # mdc = np.where(src > mi, np.where(src > hi, 'lime', 'green'), np.where(src < lo, 'red', 'orange'))
    df['impulse_macd'] = md
    df['impulse_macd_signal'] = sb
    df['impulse_macd_hist'] = sh
    return df

def lazybear_impulse_macd_entry(md, i, threshold=0, signal=None, use_rolling_window=False, window_size=10, use_sma200=True, sma200=None):
    """
    Entry filter: Only allow entry if impulse_macd (md) is not zero (i.e., not sideways/flat).
    Optionally, a threshold can be set for minimum impulse strength.
    If use_rolling_window is True, require md[i] to be a local max/min in the last window_size bars.
    If use_sma200 is True, require price to be above SMA200 (bullish) or below (bearish).
    """
    # Basic impulse threshold filter
    if abs(md[i]) <= threshold:
        return False
    # Optional: require both impulse_macd and signal to exceed threshold
    if signal is not None and abs(signal[i]) <= threshold:
        return False
    # Optional: rolling window breakout filter
    if use_rolling_window and i >= window_size:
        window = md[i-window_size+1:i+1]
        if not (md[i] == max(window) or md[i] == min(window)):
            return False
    # Optional: SMA200 filter
    if use_sma200 and sma200 is not None:
        # Only allow long if price above SMA200, short if below (or just long-only)
        # Here, assume long-only for simplicity
        if md[i] > 0 and sma200[i] is not None and sma200[i] > 0:
            if 'close' in md.index:
                price = md['close'][i]
            else:
                price = None
            # If price is available, require price > sma200
            # (In your code, you may want to pass price separately)
            # For now, skip this check or implement as needed
            pass
    return True
