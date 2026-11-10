import time
import threading
import os
import signal
from core.redis_client import RedisClient
from core.worker import TradingWorker

# Forza il client a usare il DB 15 per i test
os.environ["REDIS_DB"] = "15"

def finta_logica_di_trading(payload: dict) -> bool:
    """Una callback fittizia per testare il Worker."""
    print(f"   [Engine Mock] Elaborazione trade: {payload}")
    if "prezzo" in payload:
        print("   [Engine Mock] 🟢 Condizione verificata, trade eseguito!")
        return True
    print("   [Engine Mock] 🔴 Dati invalidi, trade rifiutato!")
    return False

def ricevitore_pubsub(messaggio: dict):
    """Callback per il test del Pub/Sub."""
    print(f"   [Pub/Sub Mock] Ricevuto segnale istantaneo: {messaggio}")

def esegui_test():
    print("\n--- INIZIO TEST INTEGRAZIONE INFRASTRUTTURA ---")
    
    # 1. Inizializza il client
    client = RedisClient()
    
    # Pulisce i dati del DB 15 per avere un test pulito
    client.client.flushdb()
    
    stream_name = "test:stream:ordini"
    group_name = "trading_group"
    worker_name = "worker_alpha"
    channel_name = "test:canale:prezzi"

    print("\n[TEST 1] Pub/Sub e Background Threads...")
    client.subscribe_in_background(channel_name, ricevitore_pubsub)
    time.sleep(0.5) # Dà il tempo al thread di mettersi in ascolto
    client.publish_event(channel_name, {"asset": "BTCUSDT", "prezzo_attuale": 65000})
    time.sleep(0.5)

    print("\n[TEST 2] Iniezione Dati Complessi nello Stream (Sanificazione)...")
    payload_complesso = {
        "asset": "ETHUSDT",
        "prezzo": 3500.50,
        "indicatori": {"RSI": 45, "MACD": "bullish"}, # Dizionario annidato
        "flag_test": True
    }
    client.log_event(stream_name, payload_complesso)
    print("Dato inserito nello stream con successo.")

    print("\n[TEST 3] Avvio del TradingWorker...")
    worker = TradingWorker(client, stream_name, group_name, worker_name)
    
    # Facciamo partire il worker in un thread separato per non bloccare lo script di test
    worker_thread = threading.Thread(
        target=worker.start_loop, 
        args=(finta_logica_di_trading, 3), 
        daemon=True
    )
    worker_thread.start()

    # Diamo al worker 3 secondi per processare i messaggi e loggare
    time.sleep(3)

    print("\n[TEST 4] Test dello Shutdown Pulito (Graceful Shutdown)...")
    # Simuliamo l'invio del segnale SIGINT (Ctrl+C) al nostro stesso processo
    os.kill(os.getpid(), signal.SIGINT)
    
    # Attendiamo che il thread del worker si chiuda effettivamente
    worker_thread.join(timeout=3)
    
    print("\n--- TEST COMPLETATO ---")
    print("Se vedi questo messaggio senza errori, l'infrastruttura è funzionante al 100%.")

if __name__ == "__main__":
    esegui_test()