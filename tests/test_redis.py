import unittest
import time
import os
import sys

# Aggiungiamo la cartella principale al path per importare RedisClient
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from redis_client import RedisClient

class TestRedisClient(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Inizializza il client prima di tutti i test"""
        cls.db = RedisClient()

    def test_01_set_get(self):
        """Testa il salvataggio e recupero di dati semplici e complessi"""
        print("\n[TEST] Verifica Getter/Setter...")
        # Stringa
        self.db.set_value("test_key", "ciao")
        self.assertEqual(self.db.get_value("test_key"), "ciao")
        
        # Dizionario (JSON)
        data = {"id": 1, "status": "active"}
        self.db.set_value("test_dict", data)
        self.assertEqual(self.db.get_value("test_dict"), data)

    def test_02_pubsub_background(self):
        """Testa il sistema di segnali in background (emergenze)"""
        print("\n[TEST] Verifica Pub/Sub in Background...")
        
        ricevuto = []
        def mia_callback(msg):
            ricevuto.append(msg)

        # Avvio ascolto
        canale = "test_channel"
        killer = self.db.subscribe_in_background(canale, mia_callback)
        
        # Aspettiamo che il thread sia attivo
        time.sleep(0.5)
        
        # Pubblichiamo un messaggio
        test_msg = {"alert": "LIQUIDAZIONE", "valore": 100}
        self.db.publish_event(canale, test_msg)
        
        # Diamo tempo al thread di processare
        time.sleep(1)
        
        self.assertIn(test_msg, ricevuto)
        print(f"   -> Messaggio ricevuto correttamente: {ricevuto[0]}")
        
        # Fermiamo il thread
        killer.set()
        time.sleep(0.5)
    
    def test_03_multi_channel_management(self):
        """Testa la gestione di più canali contemporaneamente e la chiusura selettiva"""
        print("\n[TEST] Verifica Multi-Canale e Tracciabilità...")
        
        ricevuti = {"BTC": [], "ETH": [], "SOL": []}
        
        # Definiamo una callback generica che usa il canale per smistare i dati
        def callback_multi(msg):
            # Assumiamo che il messaggio contenga il simbolo
            simbolo = msg.get("simbolo")
            if simbolo in ricevuti:
                ricevuti[simbolo].append(msg)

        # 1. Avvio dei 3 thread
        stop_btc = self.db.subscribe_in_background("BTC_CHANNEL", callback_multi)
        stop_eth = self.db.subscribe_in_background("ETH_CHANNEL", callback_multi)
        stop_sol = self.db.subscribe_in_background("SOL_CHANNEL", callback_multi)

        time.sleep(0.5) # Tempo tecnico di attivazione

        # 2. Pubblicazione messaggi mirati
        self.db.publish_event("BTC_CHANNEL", {"simbolo": "BTC", "prezzo": 60000})
        self.db.publish_event("ETH_CHANNEL", {"simbolo": "ETH", "prezzo": 3000})
        self.db.publish_event("SOL_CHANNEL", {"simbolo": "SOL", "prezzo": 150})

        time.sleep(1) # Attesa elaborazione

        # 3. Verifico se tutti e 3 i simboli hanno ricevuto un messaggio
        self.assertEqual(len(ricevuti["BTC"]), 1)
        self.assertEqual(len(ricevuti["ETH"]), 1)
        self.assertEqual(len(ricevuti["SOL"]), 1)
        print("   -> Tutti i canali hanno ricevuto il proprio messaggio.")

        # 4. Chiusura selettiva (fermiamo solo ETH)
        stop_eth.set()
        time.sleep(0.5)
        
        # Proviamo a inviare di nuovo a tutti
        self.db.publish_event("BTC_CHANNEL", {"simbolo": "BTC", "prezzo": 61000})
        self.db.publish_event("ETH_CHANNEL", {"simbolo": "ETH", "prezzo": 3100})
        
        time.sleep(1)

        # BTC deve avere 2 messaggi, ETH deve essere rimasto a 1
        self.assertEqual(len(ricevuti["BTC"]), 2)
        self.assertEqual(len(ricevuti["ETH"]), 1)
        print("   -> Chiusura selettiva riuscita: BTC continua, ETH è fermo.")

        # Pulizia finale
        stop_btc.set()
        stop_sol.set()

if __name__ == '__main__':
    unittest.main()