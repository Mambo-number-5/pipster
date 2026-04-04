import os
import time
import ccxt
import pandas as pd
import matplotlib.pyplot as plt
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from engine import generate_signals, manage_positions

# Caricamento configurazione
load_dotenv()

# Parametri presi dal .env o con valori di default sicuri
SYMBOL = os.getenv("SYMBOL", "BTC/USDT")
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", 1000.0))
DAYS_BACK = int(os.getenv("DAYS_BACK", 93))
TIMEFRAMES = os.getenv("TIMEFRAMES", "15m").split(",")

# Dizionario parametri (potresti anche metterlo in un file JSON esterno)
from config_params import TIMEFRAME_PARAMS 

# =========================================================
# DOWNLOAD DATI
# =========================================================
def fetch_data(exchange, symbol, tf, days_back=93, limit=1000):
    since = exchange.parse8601(
        (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    )
    all_candles = []

    while True:
        try:
            candles = exchange.fetch_ohlcv(symbol, timeframe=tf, since=since, limit=limit)
        except Exception as e:
            print(f"Errore su {tf}: {e}")
            break

        if not candles:
            break

        all_candles += candles
        since = candles[-1][0] + 1

        print(f"{tf} candles scaricate: {len(all_candles)}")
        time.sleep(exchange.rateLimit / 1000)

        if len(candles) < limit:
            break

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(
        all_candles,
        columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"]
    )
    df["Date"] = pd.to_datetime(df["Timestamp"], unit="ms", utc=True)
    df = df.set_index("Date").drop(columns=["Timestamp"]).sort_index()

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def main():
    print(f"🚀 Avvio Pipster Bot - Symbol: {SYMBOL}")
    
    # Inizializzazione Exchange
    exchange = ccxt.binance({"enableRateLimit": True})
    
    all_trades = []
    
    for tf in TIMEFRAMES:
        df = fetch_data(exchange, SYMBOL, tf.strip(), DAYS_BACK)
        if df.empty: continue
        
        # Calcolo segnali e Backtest
        df_signals = generate_signals(df, tf, TIMEFRAME_PARAMS)
        trades = manage_positions(df_signals, INITIAL_CAPITAL)
        
        if not trades.empty:
            all_trades.append(trades)

    # Output finale
    if all_trades:
        final_report = pd.concat(all_trades).sort_index()
        print(f"💰 Capitale Finale: {final_report['Capital'].iloc[-1]:.2f}")
        # Salvataggio grafico (ottimo per Docker così lo vedi fuori dal container)
        final_report["Capital"].plot()
        plt.savefig("report_backtest.png")
        print("📈 Grafico salvato in report_backtest.png")
    else:
        print("❌ Nessun trade eseguito.")

if __name__ == "__main__":
    main()
