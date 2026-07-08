import os
import sys
import asyncio

# 1. FORZATURA DB DI TEST (Prima di qualsiasi importazione del codice del bot)
os.environ["REDIS_DB"] = "15"

# 2. SISTEMAZIONE PATH PER GLI IMPORT LOCALI
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitget_ingestion import BitgetIngestionWorker

async def main():
    # Estrazione parametri direttamente dall'ambiente (già popolato da Docker Compose)
    exchange = os.getenv("EXCHANGE_ID", "bitget")
    raw_symbol = os.getenv("SYMBOL", "BTC/USDT")
    symbol = raw_symbol.replace("/", "").upper()  # Bitget vuole BTCUSDT
    timeframe = os.getenv("TIMEFRAME", "1h")
    
    print("=" * 60)
    print(f"🚀 AVVIO TEST DI INGESTIONE IN DB 15 ({exchange.upper()})")
    print(f"📈 Ticker: {symbol} (Origine: {raw_symbol}) | Timeframe: {timeframe}")
    print("=" * 60)

    # Inizializzazione del Worker
    worker = BitgetIngestionWorker(
        symbol=symbol, 
        timeframe=timeframe, 
        inst_type="SPOT"
    )
    
    try:
        print("\n[INFO] Avvio del loop del worker (Warm-up storico + WebSocket Live)...")
        print("[INFO] Premi CTRL+C per arrestare il test.\n")
        await worker.run()
    except asyncio.CancelledError:
        print("\n[OK] Task asincrono interrotto pulitamente.")
        raise KeyboardInterrupt
    except Exception as e:
        print(f"\n❌ Errore critico durante l'esecuzione: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Test interrotto manualmente dall'utente. Sgancio in corso.")