import unittest
import time
import os
import sys

# Forza l'uso del DB 15 per i test prima ancora di importare o istanziare il client
os.environ["REDIS_DB"] = "15"

# Aggiungiamo la cartella principale al path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from redis_client import RedisClient

class TestRedisProfessional(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Inizializza il client una sola volta per la suite di test"""
        cls.db = RedisClient()
        # Verifica di sicurezza: non vogliamo piallare il DB di produzione (solitamente lo 0)
        if cls.db.client.connection_pool.connection_kwargs['db'] != 15:
            raise RuntimeError("ERRORE: Il test sta puntando a un database diverso dal 15!")

    def setUp(self):
        """Eseguito PRIMA di ogni singolo test: pulizia totale del DB 15"""
        self.db.client.flushdb()

    def test_01_json_native_logic(self):
        """Verifica il cuore di RedisJSON: salvataggio, recupero e append"""
        print("\n[TEST] Verifica Logica RedisJSON (Hot Data)...")
        
        key = "ticker:BTCUSD"
        data = {"symbol": "BTCUSD", "price": 65000, "active": True}
        
        self.db.set_json(key, data)
        res = self.db.get_json(key)
        
        # NOTA: Con l'ultima versione del mio wrapper, 'res' è già il dizionario.
        self.assertEqual(res["symbol"], "BTCUSD")
        
        history_key = "history:BTCUSD:1m"
        candela_1 = {"t": 1714400000, "o": 65000, "c": 65100}
        candela_2 = {"t": 1714400060, "o": 65100, "c": 65050}
        
        self.db.append_to_list(history_key, candela_1)
        self.db.append_to_list(history_key, candela_2)
        
        history = self.db.get_json(history_key)
        # Con wrapper clean, history è direttamente la lista:
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["c"], 65050)
        print("   -> RedisJSON gestisce correttamente gli array di candele.")

    def test_02_multithreading_pubsub(self):
        """Verifica che i segnali in background non interferiscano con la logica JSON"""
        print("\n[TEST] Verifica Multithreading e Segnali...")
        
        messaggi_ricevuti = []
        def callback(msg):
            messaggi_ricevuti.append(msg)

        stop_event = self.db.subscribe_in_background("ALERTS", callback)
        time.sleep(0.5)
        
        alert_msg = {"type": "MARGIN_CALL", "level": "HIGH"}
        self.db.publish_event("ALERTS", alert_msg)
        
        time.sleep(1)
        self.assertIn(alert_msg, messaggi_ricevuti)
        
        stop_event.set()
        print(f"   -> Thread di background isolato e funzionante.")

    def test_03_stress_async_flow(self):
        """Test di stress: scriviamo JSON mentre ascoltiamo canali"""
        print("\n[TEST] Verifica Stress Mix (JSON + PubSub)...")
        
        counter = {"count": 0}
        def fast_callback(msg):
            counter["count"] += 1

        self.db.subscribe_in_background("FEED", fast_callback)
        time.sleep(1.5)
        
        # Inondiamo Redis di piccoli aggiornamenti JSON
        for i in range(10):
            self.db.set_json(f"thread_test:{i}", {"val": i})
            self.db.publish_event("FEED", {"update": i})
        
        # Un piccolo respiro per permettere ai thread di processare i 10 messaggi
        time.sleep(1.5)
        self.assertEqual(counter["count"], 10)
        self.assertEqual(self.db.get_json("thread_test:9")["val"], 9)
        
        self.db.stop_all_subscriptions()
        print("   -> Stress test superato: nessuna collisione tra thread.")

    def test_04_stream_persistence(self):
        """Verifica che lo stream mantenga la cronologia degli eventi"""
        print("\n[TEST] Verifica Redis Streams (Audit Log)...")
        
        stream_key = "test_log_stream"
        event = {"module": "STRATEGY", "msg": "Incrocio medie rilevato"}
        
        # Scrittura
        entry_id = self.db.log_event(stream_key, event)
        self.assertIsNotNone(entry_id)
        
        # Lettura
        recent = self.db.get_recent_events(stream_key, count=1)
        if not isinstance(recent, list):
            raise TypeError("Errore nel caricamento, la risposta non è una lista")
        self.assertEqual(len(recent), 1)
        
        # La struttura di ritorno di Redis è: (id, {dizionario})
        self.assertEqual(recent[0][1]["module"], "STRATEGY")
        print(f"   -> Evento loggato con ID: {entry_id}")

    def test_05_consumer_group_resilience(self):
        """Verifica la capacità di ripresa (Recovery) dopo un finto crash"""
        print("\n[TEST] Verifica Resilienza Consumer Groups (Recovery)...")
        
        stream = "trading_signals"
        group = "bot_group"
        consumer = "worker_1"
        
        # 1. Prepariamo lo stream e il gruppo
        self.db.create_consumer_group(stream, group)
        
        # 2. Inviamo un segnale di trading
        signal = {"action": "BUY", "asset": "ETHUSD"}
        self.db.log_event(stream, signal)
        
        # 3. Il bot legge il messaggio (Simuliamo l'inizio dell'elaborazione)
        # Usiamo '>' per prendere il messaggio nuovo
        messages = self.db.read_group(stream, group, consumer, last_id='>') 
        self.assertEqual(len(messages), 1)
        msg_id = messages[0][1][0][0] # Estraiamo l'ID del messaggio ricordando che: [[b'stream_name', [(b'id', {b'chiave': b'valore'})]]]
        print(f"   -> Messaggio {msg_id} ricevuto, ma NON confermato (Simulo Crash).")

        # --- QUI IL BOT "CRASHA" (non chiamiamo acknowledge) ---

        # 4. Il bot si riavvia e cerca messaggi PENDING (non confermati)
        # Usiamo '0' invece di '>'
        recovery_messages = self.db.read_group(stream, group, consumer, last_id='0')
        
        self.assertEqual(len(recovery_messages), 1)
        recovered_id = recovery_messages[0][1][0][0]
        recovered_data = recovery_messages[0][1][0][1]
        
        self.assertEqual(msg_id, recovered_id)
        self.assertEqual(recovered_data["asset"], "ETHUSD")
        print(f"   -> Recupero riuscito: Il messaggio {recovered_id} era ancora in coda.")

        # 5. Ora confermiamo e verifichiamo che la coda sia finalmente vuota
        self.db.acknowledge(stream, group, msg_id)
        final_check = self.db.read_group(stream, group, consumer, last_id='0')
        self.assertEqual(len(final_check[0][1]), 0)
        print("   -> Messaggio confermato e rimosso dalla coda dei pendenti.")
    
    def test_06_dead_letter_queue_flow(self):
        """Verifica che un messaggio fallito troppe volte finisca nella DLQ"""
        print("\n[TEST] Verifica Flusso Dead Letter Queue (Poison Message)...")
        
        stream = "signals"
        group = "trading_group"
        consumer = "bot_1"
        dlq_stream = f"{stream}:errors"
        MAX_RETRIES = 3

        self.db.create_consumer_group(stream, group)
        
        # 1. Inseriamo un messaggio "velenoso"
        msg_payload = {"cmd": "CRASH_ME", "data": "corrupted_value"}
        self.db.log_event(stream, msg_payload)

        # 1.1 PRIMA LETTURA (ID '>'): Sposta il messaggio nella PEL
        self.db.read_group(stream, group, consumer, last_id='>')

        # 2. LETTURE SUCCESSIVE (ID '0'): Incrementa il counter times_delivered
        # Lo leggiamo altre 3 volte (totale 4 consegne)
        for _ in range(3):
            self.db.read_group(stream, group, consumer, last_id='0')

        # 3. LOGICA DI CONTROLLO (Quella del Worker)
        pending_info = self.db.get_pending_messages(stream, group)
        
        found_poison = False
        for info in pending_info:
            if info['times_delivered'] > MAX_RETRIES:
                # Recuperiamo i dati originali per non perderli
                data = self.db.get_message_by_id(stream, info['message_id'])
                
                if data:
                    # SPOSTAMENTO: Lo mettiamo nella DLQ e lo confermiamo nel principale
                    self.db.log_event(dlq_stream, data)
                    self.db.acknowledge(stream, group, info['message_id'])
                    found_poison = True

        self.assertTrue(found_poison, "Il sistema dovrebbe aver identificato il messaggio velenoso")

        # 4. Verifica Finale: Lo stream principale è pulito, la DLQ ha il messaggio
        remaining_pending = self.db.get_pending_messages(stream, group)
        self.assertEqual(len(remaining_pending), 0, "Lo stream principale dovrebbe essere vuoto dopo l'ACK")
        
        dlq_data = self.db.get_recent_events(dlq_stream, count=1)
        self.assertEqual(dlq_data[0][1]["cmd"], "CRASH_ME")
        print("   -> Successo: Messaggio velenoso isolato nella DLQ senza bloccare il bot.")

    def test_07_hash_and_json_trimming(self):
        """Test combinato: verifica le performance degli Hash e la prevenzione dei leak sui JSON"""
        print("\n[TEST] Verifica Hash Layer & Trimming Array JSON...")
        
        # 1. Verifica Operazioni Hash (Fast Cache Layer)
        hash_key = "ticker:live:BTCUSDT"
        tick_data = {"price": "67500.50", "volume": "1.245", "updated_at": str(time.time())}
        
        self.assertTrue(self.db.set_hash(hash_key, tick_data, expire_seconds=10))
        
        # Lettura completa dell'hash
        cached_hash = self.db.get_hash(hash_key)
        self.assertEqual(cached_hash["price"], "67500.50")
        
        # Lettura puntuale di un singolo campo (O(1))
        single_field = self.db.get_hash_field(hash_key, "volume")
        self.assertEqual(single_field, "1.245")
        print("   -> Strutture Hash caricate e interrogate correttamente in O(1).")
        
        # 2. Verifica Trimming automatico per evitare Memory Leak su JSON
        list_key = "history:limited:candles"
        for i in range(5):
            # Forziamo un tetto massimo di 3 elementi nell'array JSON
            self.db.append_to_list(list_key, {"candle_id": i}, max_len=3)
            
        final_list = self.db.get_json(list_key)
        # Nonostante i 5 inserimenti, la lista deve contenere solo gli ultimi 3 elementi
        self.assertEqual(len(final_list), 3)
        self.assertEqual(final_list[0]["candle_id"], 2)  # Il più vecchio superstite
        self.assertEqual(final_list[-1]["candle_id"], 4) # L'ultimo inserito
        print("   -> Trimming JSON funzionante: la memoria è protetta dalla saturazione.")

    def test_08_distributed_autoclaim_recovery(self):
        """Verifica il recupero dei messaggi pendenti orfani da parte di un worker differente"""
        print("\n[TEST] Verifica Bilanciamento e Recovery con XAUTOCLAIM...")
        
        stream = "distributed_signals"
        group = "cluster_group"
        dead_consumer = "worker_defunto_1"
        alive_consumer = "worker_attivo_2"
        
        self.db.create_consumer_group(stream, group)
        self.db.log_event(stream, {"task": "EXECUTE_ORDER", "size": "0.1"})
        
        # Il vecchio worker prende il messaggio ma scompare (non dà ACK)
        dead_messages = self.db.read_group(stream, group, dead_consumer, last_id='>')
        self.assertEqual(len(dead_messages), 1)
        original_msg_id = dead_messages[0][1][0][0]
        
        # Simuliamo un'attesa minima prima del controllo di recupero
        # (Nel codice reale metterai ad esempio 30000ms, qui usiamo 0ms per velocizzare il test)
        next_id, claimed_messages = self.db.autoclaim_messages(
            stream_name=stream,
            group_name=group,
            consumer_name=alive_consumer,
            min_idle_time_ms=0,
            count=10
        )
        
        # Il worker attivo deve essere riuscito a "rubare" il messaggio rimasto appeso
        self.assertEqual(len(claimed_messages), 1)
        self.assertEqual(claimed_messages[0][0], original_msg_id)
        self.assertEqual(claimed_messages[0][1]["task"], "EXECUTE_ORDER")
        
        # Chiudiamo la pendenza con il nuovo worker attivo
        self.assertTrue(self.db.acknowledge(stream, group, original_msg_id))
        print(f"   -> XAUTOCLAIM riuscito: {alive_consumer} ha salvato il messaggio bloccato da {dead_consumer}.")

    def tearDown(self):
        """Eseguito DOPO ogni singolo test: ferma le sottoscrizioni per evitare leak di thread"""
        self.db.stop_all_subscriptions()

if __name__ == '__main__':
    unittest.main()