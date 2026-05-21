import time
import signal
import sys
from typing import Callable
# Sostituisci con il corretto percorso di importazione del tuo wrapper
from redis_client import RedisClient  

class TradingWorker:
    """
    Worker industriale per il consumo resiliente dei segnali di trading.
    Gestisce la pulizia dei messaggi pendenti, l'isolamento dei poison messages
    e lo shutdown pulito del processo.
    """
    
    def __init__(self, db_client: RedisClient, stream: str, group: str, consumer_name: str):
        self.db = db_client
        self.stream = stream
        self.group = group
        self.consumer = consumer_name
        self.running = True
        
        # Inizializza i segnali di sistema per lo shutdown pulito
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        """Intercetta i segnali di interruzione per spegnere il bot senza corrompere i dati."""
        print(f"\n🛑 Segnale [{signum}] intercettato. Arresto del worker in corso...")
        self.running = False

    def _clear_poison_messages(self, max_retries: int):
        """Isola i messaggi che hanno fallito troppi tentativi di consegna."""
        pending_info = self.db.get_pending_messages(self.stream, self.group, count=10)
        dlq_stream = f"{self.stream}:errors"

        for info in pending_info:
            if info['times_delivered'] > max_retries:
                msg_id = info['message_id']
                print(f"☣️ Poison Message rilevato! ID: {msg_id}. Tentativi: {info['times_delivered']}")
                
                # Recupero chirurgico dei dati (Data Layer - Non esegue logica, non crasha)
                data = self.db.get_message_by_id(self.stream, msg_id)
                if data:
                    # Spostamento nella Dead Letter Queue
                    self.db.log_event(dlq_stream, data)
                    # Conferma nello stream principale per rimuoverlo dalla PEL
                    self.db.acknowledge(self.stream, self.group, msg_id)
                    print(f"📥 Messaggio {msg_id} isolato con successo in {dlq_stream}")

    def start_loop(self, business_logic_callback: Callable[[dict], bool], max_retries: int = 3):
        """
        Avvia il ciclo infinito di ascolto ed elaborazione dei messaggi.
        
        Args:
            business_logic_callback (Callable): Funzione esterna che contiene la logica 
                di trading. Deve accettare un dizionario e restituire un booleano (Successo/Fallimento).
            max_retries (int): Limite massimo di tentativi di consegna per messaggio.
        """
        print(f"🚀 Worker '{self.consumer}' avviato sullo stream '{self.stream}' [Group: {self.group}]")
        
        # Assicuriamo l'esistenza del gruppo prima di partire
        self.db.create_consumer_group(self.stream, self.group)

        while self.running:
            try:
                # 1. Fase di Sicurezza: Pulizia dei messaggi velenosi accumulati
                self._clear_poison_messages(max_retries)

                # 2. Fase di Recupero: Controlla se ci sono messaggi pendenti 'sani' (ID '0')
                # da sessioni precedenti o crash improvvisi
                messages = self.db.read_group(self.stream, self.group, self.consumer, last_id='0', block=1000)
                
                # Se la PEL è vuota, passa all'ascolto dei NUOVI messaggi in tempo reale (ID '>')
                if not messages or len(messages[0][1]) == 0:
                    messages = self.db.read_group(self.stream, self.group, self.consumer, last_id='>', block=2000)

                # 3. Elaborazione dei messaggi estratti
                for stream_name, msg_list in messages:
                    for msg_id, payload in msg_list:
                        if not self.running:
                            break
                            
                        print(f"⚡ Inizio elaborazione messaggio {msg_id}...")
                        
                        try:
                            # Esecuzione della logica di business esterna (il motore del bot)
                            success = business_logic_callback(payload)
                            
                            if success:
                                # Solo se la logica va a buon fine mandiamo l'ACK
                                self.db.acknowledge(self.stream, self.group, msg_id)
                                print(f"✅ Messaggio {msg_id} elaborato e confermato.")
                            else:
                                print(f"⚠️ La logica di business ha rifiutato il messaggio {msg_id}. Resta in PEL.")
                                
                        except Exception as business_error:
                            # Se la logica di business fallisce internamente (es. errore di parsing)
                            # NON mandiamo l'ACK. Il messaggio incrementerà il counter al prossimo ciclo.
                            print(f"❌ Errore critico durante l'esecuzione del trade {msg_id}: {business_error}")

            except Exception as e:
                print(f"⚠️ Errore generico nel ciclo del worker: {e}")
                time.sleep(2) # Evita di sovraccaricare la CPU in caso di problemi di rete con Redis
                
        print("👋 Worker arrestato in modo pulito. Nessun dato è stato corrotto.")