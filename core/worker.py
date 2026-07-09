import time
import signal
import json
from typing import Callable
from core.redis_client import RedisClient

class TradingWorker:
    """
    Worker industriale per il consumo resiliente dei segnali di trading.
    Gestisce XAUTOCLAIM, pulizia PEL, isolamento Dead Letter Queue e shutdown pulito.
    """
    
    def __init__(self, db_client: RedisClient, stream: str, group: str, consumer_name: str):
        self.db = db_client
        self.stream = stream
        self.group = group
        self.consumer = consumer_name
        self.running = True
        
        # Intercettazione segnali OS per shutdown pulito
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum: int, frame):
        """
        Intercetta i segnali del sistema operativo per un arresto pulito (graceful shutdown).
        
        Imposta il flag di esecuzione a False per interrompere il loop principale
        e si assicura che tutti i thread demone del Pub/Sub vengano chiusi senza
        lasciare connessioni di rete appese su Redis.

        Args:
            signum (int): Il numero del segnale intercettato (es. 2 per SIGINT, 15 per SIGTERM).
            frame: L'oggetto frame di esecuzione corrente al momento del segnale.
        """
        print(f"\n🛑 Segnale [{signum}] intercettato. Arresto del worker '{self.consumer}' in corso...")
        
        # 1. Ferma il loop principale del worker (evita di estrarre nuovi messaggi)
        self.running = False
        
        # 2. Chiude gentilmente tutti i thread demone del Pub/Sub per evitare socket appesi
        if hasattr(self, 'db') and self.db:
            print("🛑 Chiusura dei canali Pub/Sub attivi...")
            self.db.stop_all_subscriptions()

    def _deserialize_payload(self, raw_payload: dict) -> dict:
        """
        Ricostruisce le strutture dati complesse lette dallo stream Redis.

        Poiché il RedisClient converte liste e dizionari annidati in stringhe JSON 
        per compatibilità con XADD, questo metodo tenta di fare il parsing inverso 
        delle stringhe che assomigliano a JSON, restituendo un dizionario nativo Python.

        Args:
            raw_payload (dict): Il dizionario grezzo estratto dallo stream Redis.

        Returns:
            dict: Il dizionario con le stringhe JSON riconvertite in liste o dizionari.
        """
        parsed = {}
        for k, v in raw_payload.items():
            if isinstance(v, str):
                try:
                    # Tenta di parsare stringhe che sembrano JSON (iniziano con { o [)
                    if (v.startswith('{') and v.endswith('}')) or (v.startswith('[') and v.endswith(']')):
                        parsed[k] = json.loads(v)
                    else:
                        parsed[k] = v
                except json.JSONDecodeError:
                    parsed[k] = v
            else:
                parsed[k] = v
        return parsed

    def _process_messages(self, messages_batch, business_callback: Callable, max_retries: int):
        """
        Motore centrale di elaborazione per i messaggi estratti dallo stream.

        Itera sui messaggi ricevuti, li deserializza e li passa alla logica di business.
        Se la logica ha successo, invia l'ACK a Redis. Se fallisce, verifica se il
        messaggio ha superato il numero massimo di tentativi e, in tal caso, lo
        sposta nella Dead Letter Queue (DLQ).

        Args:
            messages_batch (list): Lista di messaggi nel formato restituito da XREADGROUP.
            business_callback (Callable): La funzione che contiene la logica di trading.
            max_retries (int): Numero massimo di fallimenti consentiti prima di isolare il messaggio.
        """
        if not messages_batch:
            return

        dlq_stream = f"{self.stream}:errors"

        for stream_name, msg_list in messages_batch:
            for msg_id, raw_payload in msg_list:
                if not self.running:
                    break
                    
                print(f"⚡ Elaborazione messaggio {msg_id}...")
                payload = self._deserialize_payload(raw_payload)
                
                processing_failed = False  # 1. Inizializziamo il flag di stato

                try:
                    success = business_callback(payload)
                    if success:
                        self.db.acknowledge(self.stream, self.group, msg_id)
                        print(f"✅ Messaggio {msg_id} confermato.")
                    else:
                        print(f"⚠️ Rifiutato dalla logica di business: {msg_id}. Resta in PEL.")
                        processing_failed = True  # Fallimento di business (Niente eccezione)
                        
                except Exception as e:
                    print(f"❌ Errore critico esecuzione {msg_id}: {e}")
                    processing_failed = True  # Fallimento per crash del codice
                
                # 2. Controlliamo se il messaggio è diventato Velenoso SOLO se l'elaborazione è fallita
                if processing_failed:
                    # DOPO aver fallito, controlliamo se il messaggio è diventato Velenoso
                    # Questo evita di chiamare XPENDING inutilmente a ogni ciclo
                    pending_info = self.db.get_pending_messages(self.stream, self.group, count=100)
                    for info in pending_info:
                        if info['message_id'] == msg_id and info['times_delivered'] >= max_retries:
                            print(f"☣️ Poison Message {msg_id} isolato nella DLQ (Tentativi: {info['times_delivered']})")
                            self.db.log_event(dlq_stream, raw_payload)
                            self.db.acknowledge(self.stream, self.group, msg_id)

    def start_loop(self, business_logic_callback: Callable[[dict], bool], max_retries: int = 3):
        """
        Avvia il ciclo infinito di consumo dei messaggi dallo stream Redis.

        Gestisce l'intero ciclo di vita della Consumer Group:
        1. XAUTOCLAIM periodico per recuperare messaggi da worker "morti".
        2. Lettura della PEL locale per riprocessare messaggi pendenti propri.
        3. Ascolto bloccante per nuovi messaggi in tempo reale.

        Args:
            business_logic_callback (Callable): Funzione che accetta un dict (payload) e ritorna un bool.
            max_retries (int, optional): Tentativi massimi prima della DLQ. Default a 3.
        """
        print(f"🚀 Worker '{self.consumer}' avviato su '{self.stream}' [Group: {self.group}]")
        self.db.create_consumer_group(self.stream, self.group)

        last_autoclaim_time = time.time()

        while self.running:
            try:
                # 1. FASE AUTOCLAIM (Ogni 30 secondi): Recuperiamo orfani di altri worker morti
                current_time = time.time()
                if current_time - last_autoclaim_time > 30:
                    _, claimed = self.db.autoclaim_messages(self.stream, self.group,
                                                            self.consumer, min_idle_time_ms=60000)

                    if claimed:
                        print(f"🔄 Recuperati {len(claimed)} messaggi orfani da altri worker.")
                        # Riformattiamo l'output per passarlo al motore centrale
                        self._process_messages([(self.stream, claimed)], business_logic_callback, max_retries)
                    last_autoclaim_time = current_time

                # 2. FASE PEL LOCALE: Processiamo i MIEI messaggi falliti precedentemente
                # Niente block=... con '0', la risposta è istantanea
                pel_messages = self.db.read_group(self.stream, self.group, self.consumer, last_id='0')
                if pel_messages and len(pel_messages[0][1]) > 0:
                    print(f"♻️ Trovati {len(pel_messages[0][1])} messaggi nella mia PEL locale. Riprovo...")
                    self._process_messages(pel_messages, business_logic_callback, max_retries)

                # 3. FASE NUOVI MESSAGGI: Aspettiamo messaggi freschi in tempo reale
                # Qui usiamo block=2000 (2 secondi) per non stressare la CPU
                new_messages = self.db.read_group(self.stream, self.group, self.consumer, last_id='>', block=2000)
                if new_messages and len(new_messages[0][1]) > 0:
                    self._process_messages(new_messages, business_logic_callback, max_retries)

            except Exception as e:
                # Cattura errori di rete, micro-crash del database
                print(f"⚠️ Errore generico ciclo worker: {e}")
                try:
                    self.db.client.incr("errors:counter:worker_loop")
                except:
                    pass
                time.sleep(2)

        print("👋 Worker arrestato. Shutdown completato.")