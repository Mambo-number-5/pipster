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
        
        # NOTA: Se hai usato il mio wrapper rifattorizzato, 'res' è già il dizionario.
        # Se usi il tuo vecchio codice, devi mantenere res[0].
        # Assumendo il wrapper 'clean':
        self.assertEqual(res["symbol"], "BTCUSD")
        
        history_key = "history:BTCUSD:1m"
        candela_1 = {"t": 1714400000, "o": 65000, "c": 65100}
        candela_2 = {"t": 1714400060, "o": 65100, "c": 65050}
        
        self.db.append_to_list(history_key, candela_1)
        self.db.append_to_list(history_key, candela_2)
        
        history = self.db.get_json(history_key)
        # Con wrapper clean, history è direttamente la lista:
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["c"], 65050)
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
    
    def tearDown(self):
        """Eseguito DOPO ogni singolo test: ferma le sottoscrizioni per evitare leak di thread"""
        self.db.stop_all_subscriptions()

if __name__ == '__main__':
    unittest.main()