import pandas as pd
from indicators import ema, atr, rsi

def generate_signals(df, timeframe, params):
    p = params[timeframe]
    data = df.copy()
    
    # Indicatori
    data["EMA_TREND"] = ema(data["Close"], 200)
    data["EMA_FAST"] = ema(data["Close"], 20)
    data["EMA_SLOW"] = ema(data["Close"], 50)
    data["ATR"] = atr(data, 14)
    data["RSI"] = rsi(data["Close"], 14)
    
    # Filtri
    data["VOL_MA20"] = data["Volume"].rolling(20).mean()
    data["ATR_PCT"] = data["ATR"] / data["Close"]

    # Logica Segnale Long
    long_condition = (
        (data["Close"] > data["EMA_TREND"]) &
        (data["EMA_FAST"] > data["EMA_SLOW"]) &
        (data["RSI"].between(45, 65)) &
        (data["Volume"] >= data["VOL_MA20"]) &
        (data["ATR_PCT"] < 0.025)
    )

    data["Signal"] = 0
    data.loc[long_condition, "Signal"] = 1
    
    # Inietta parametri di rischio nel dataframe
    for key, value in p.items():
        data[key] = value
        
    return data

# [Qui inseriresti la funzione manage_positions che hai già, 
#  ma la puliremo per gestire meglio i log dei trade]
# =========================================================
# BACKTEST
# =========================================================
def manage_positions(
    df,
    capitale_iniziale,
    fee_pct=0.0004,
    max_consecutive_losses=2,
    cooldown_bars=10,
    max_drawdown_pct=0.12
):
    trades = []
    capital = float(capitale_iniziale)
    peak_equity = capital

    in_position = False
    entry_price = 0.0
    size_btc = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    trailing_stop = 0.0
    consecutive_losses = 0
    cooldown = 0

    for idx, row in df.iterrows():
        price = float(row["Close"])
        atr_value = row["ATR"]
        signal = int(row["Signal"]) if pd.notna(row["Signal"]) else 0

        if cooldown > 0:
            cooldown -= 1
            signal = 0

        if in_position:
            open_pnl = size_btc * (price - entry_price)
            current_equity = capital + open_pnl
        else:
            current_equity = capital

        peak_equity = max(peak_equity, current_equity)
        drawdown_pct = (peak_equity - current_equity) / peak_equity if peak_equity > 0 else 0.0
        trading_enabled = drawdown_pct < max_drawdown_pct

        if in_position and pd.notna(atr_value) and atr_value > 0:
            new_trailing_stop = price - float(row["Trail_ATR_Mult"]) * float(atr_value)
            trailing_stop = max(trailing_stop, new_trailing_stop)

            exit_reason = None

            if price <= stop_loss:
                exit_reason = "Stop Loss"
            elif price <= trailing_stop:
                exit_reason = "Trailing Stop"
            elif price >= take_profit:
                exit_reason = "Take Profit"

            if exit_reason is not None:
                gross_pnl = size_btc * (price - entry_price)
                exit_fee = size_btc * price * fee_pct
                net_pnl = gross_pnl - exit_fee
                capital += net_pnl

                trades.append({
                    "Date": idx,
                    "Action": "Exit Long",
                    "Price": price,
                    "Entry_Price": entry_price,
                    "Size_BTC": size_btc,
                    "PnL": net_pnl,
                    "Capital": capital,
                    "Reason": exit_reason
                })

                if net_pnl < 0:
                    consecutive_losses += 1
                else:
                    consecutive_losses = 0

                if consecutive_losses >= max_consecutive_losses:
                    cooldown = cooldown_bars
                    consecutive_losses = 0

                in_position = False
                entry_price = 0.0
                size_btc = 0.0
                stop_loss = 0.0
                take_profit = 0.0
                trailing_stop = 0.0

        if (
            not in_position and
            trading_enabled and
            signal == 1 and
            pd.notna(atr_value) and
            atr_value > 0
        ):
            risk_amount = capital * float(row["Risk_Pct"])
            stop_distance = float(atr_value) * float(row["ATR_Stop_Mult"])

            if stop_distance > 0:
                raw_size_btc = risk_amount / stop_distance
                max_size_btc = capital / price
                final_size_btc = min(raw_size_btc, max_size_btc)

                if final_size_btc > 0:
                    entry_price = price
                    size_btc = final_size_btc
                    stop_loss = entry_price - stop_distance
                    take_profit = entry_price + (float(atr_value) * float(row["ATR_TP_Mult"]))
                    trailing_stop = entry_price - (float(atr_value) * float(row["Trail_ATR_Mult"]))
                    in_position = True

                    entry_fee = size_btc * entry_price * fee_pct
                    capital -= entry_fee

                    trades.append({
                        "Date": idx,
                        "Action": "Enter Long",
                        "Price": price,
                        "Entry_Price": entry_price,
                        "Size_BTC": size_btc,
                        "PnL": -entry_fee,
                        "Capital": capital,
                        "Reason": "Signal"
                    })

    df_trades = pd.DataFrame(trades)

    if not df_trades.empty:
        df_trades = df_trades.set_index("Date")
        df_trades["Cumulative"] = df_trades["Capital"]

    return df_trades

