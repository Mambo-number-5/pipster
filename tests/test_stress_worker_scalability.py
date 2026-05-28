import time
import os
import multiprocessing
from redis_client import RedisClient
from worker import TradingWorker
from typing import cast

os.environ["REDIS_DB"] = "15"

STREAM_NAME = "stress:stream:ordini"
GROUP_NAME = "stress_group"

def logica_pesante(payload: dict) -> bool:
    """Simula un trade che richiede tempo."""
    time.sleep(0.02)  # 20ms per messaggio
    # Rifiuta il 5% dei messaggi (ID divisibili per 20) per testare la DLQ
    if int(payload["id_ordine"]) % 20 == 0:
        return False
    return True

def run_worker(worker_name: str):
    client = RedisClient()
    worker = TradingWorker(client, STREAM_NAME, GROUP_NAME, worker_name)
    # Avvia il loop del worker
    worker.start_loop(logica_pesante, max_retries=2)

def producer_bomber(totale_messaggi: int):
    client = RedisClient()
    client.client.delete(STREAM_NAME)
    client.client.delete(f"{STREAM_NAME}:errors")
    
    # Rimuove il gruppo se esisteva già per azzerare la PEL
    try:
        client.client.xgroup_destroy(STREAM_NAME, GROUP_NAME)
    except:
        pass
        
    print(f"💣 Iniezione di {totale_messaggi} messaggi nello stream...")
    for i in range(1, totale_messaggi + 1):
        client.log_event(STREAM_NAME, {"id_ordine": i, "asset": "BTCUSDT"})
    print("✅ Iniezione completata.")

def esegui_stress_test():
    client = RedisClient()
    print("\n--- INIZIO VERI STRESS TEST & VALIDAZIONE ACCURATA ---")
    
    TOT_MSG = 200
    producer_bomber(TOT_MSG)
    
    # Avviamo i 3 Worker
    workers = []
    for i in range(3):
        worker_name = f"worker_node_{i}"
        p = multiprocessing.Process(target=run_worker, args=(worker_name,))
        p.start()
        workers.append(p)
    
    # Lasciamo lavorare tutti e 3 per 2 secondi (elaboreranno circa il 60-70% dei messaggi)
    time.sleep(2)
    
    print("\n💀 CHAOS MONKEY: Uccisione brutale di worker_node_1!")
    workers[1].terminate()  # SIGKILL istantaneo. I suoi messaggi in elaborazione restano appesi nella PEL
    workers[1].join()
    print("Messaggi in PEL aver ucciso in maniera spietata e brutale l'indifeso worker_node_1 attaccandolo alle spalle...")
    # 1. Controlliamo quanti messaggi sono ancora bloccati nelle PEL di TUTTI i consumer
    try:
        consumers = cast(list[dict], client.client.xinfo_consumers(STREAM_NAME, GROUP_NAME))
        totale_pendenti_rimasti = 0
        for c in consumers:
            print(f"Node: {c['name']} | Messaggi ancora appesi nella sua PEL: {c['pending']}")
            totale_pendenti_rimasti += c['pending']
    except Exception as e:
        print(f"Errore lettura consumers: {e}")
        totale_pendenti_rimasti = -1
    # FIX TEMPORALE: Aspettiamo 35 secondi per essere SICURI che:
    # 1. Scattino i 30 secondi interni del controllo autoclaim nei worker superstiti (0 e 2)
    # 2. I messaggi fermi nella PEL di worker_1 superino il 'min_idle_time_ms=60000' (impostiamo a 2000 per il test)
    print("⏳ Attesa di 65 secondi per far scattare la finestra di XAUTOCLAIM dei worker superstiti...")
    time.sleep(65)
    
    print("\n🛑 Spegnimento dei nodi superstiti...")
    workers[0].terminate()
    workers[2].terminate()
    workers[0].join()
    workers[2].join()
    
    # --- FASE DI VERIFICA MATEMATICA SUL DATABASE ---
    print("\n📊 --- REPORT ISPEZIONE CONSUMER GROUP ---")
    
    # 1. Controlliamo quanti messaggi sono ancora bloccati nelle PEL di TUTTI i consumer
    try:
        consumers = cast(list[dict], client.client.xinfo_consumers(STREAM_NAME, GROUP_NAME))
        totale_pendenti_rimasti = 0
        for c in consumers:
            print(f"Node: {c['name']} | Messaggi ancora appesi nella sua PEL: {c['pending']}")
            totale_pendenti_rimasti += c['pending']
    except Exception as e:
        print(f"Errore lettura consumers: {e}")
        totale_pendenti_rimasti = -1

    # 2. Controlliamo quanti messaggi sono finiti nella Dead Letter Queue (DLQ) perché difettosi (5%)
    # Ogni 20 messaggi uno fallisce. Su 200 messaggi, devono essere esattamente 10.
    totale_dlq = client.client.xlen(f"{STREAM_NAME}:errors")
    print(f"☣️ Messaggi isolati nella Dead Letter Queue ({STREAM_NAME}:errors): {totale_dlq}")
    
    # 3. Giudizio finale sulla resilienza
    print("\n⚖️ VERDETTO:")
    if totale_pendenti_rimasti == 0 and totale_dlq == (TOT_MSG // 20):
        print("🏆 SUCCESS: Resilienza totale comprovata! Tutti i messaggi orfani del worker morto sono stati salvati e processati da altri nodi.")
    else:
        print("⚠️ WARNING: Ci sono ancora messaggi bloccati o il conteggio non torna. Controlla i tempi di idle.")

if __name__ == "__main__":
    esegui_stress_test()