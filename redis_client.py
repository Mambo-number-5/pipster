import redis
import os
import threading
import json
from typing import Any, Optional, Callable

class RedisClient:
    """
    Client ottimizzato per Redis con supporto a operazioni sincrone (Getter/Setter)
    e asincrone tramite Pub/Sub in thread separati.
    """

    def __init__(self):
        """
        Inizializza la connessione a Redis utilizzando le variabili d'ambiente.
        Configura inoltre i sistemi di controllo per il multi-threading.
        """
        self.host = os.getenv("REDIS_HOST", "localhost")
        self.port = int(os.getenv("REDIS_PORT", 6379))
        self.client = redis.Redis(
            host=self.host, 
            port=self.port, 
            decode_responses=True
        )
        
        # Lucchetto per garantire l'accesso thread-safe a risorse condivise interne
        self._lock = threading.Lock() 
        # Registro per gestire i segnali di stop dei thread in background
        self._active_subscriptions = {}

    # --- METODI STANDARD (Persistence Layer) ---

    def set_value(self, key: str, value: Any, expire_seconds: Optional[int] = None) -> bool:
        """
        Salva un valore nel database Redis.

        Args:
            key (str): La chiave identificativa del dato.
            value (Any): Il dato da salvare. Se dict o list, viene convertito in JSON.
            expire_seconds (int, opzionale): Tempo di scadenza in secondi (TTL).

        Returns:
            bool: True se il salvataggio è andato a buon fine.
        """
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        return self.client.set(key, value, ex=expire_seconds)

    def get_value(self, key: str) -> Optional[Any]:
        """
        Recupera un valore dal database e tenta di decodificarlo se JSON.

        Args:
            key (str): La chiave da cercare.

        Returns:
            Optional[Any]: Il dato decodificato, la stringa originale o None se la chiave non esiste.
        """
        data = self.client.get(key)
        if data is None:
            return None
        try:
            return json.loads(data)
        except (ValueError, TypeError):
            return data

    # --- METODI PUB/SUB (Real-time Layer) ---

    def publish_event(self, channel: str, message: Any) -> int:
        """
        Invia un messaggio in broadcast su un canale Redis.

        Args:
            channel (str): Il nome del canale su cui pubblicare.
            message (Any): Il messaggio da inviare (stringa o oggetto serializzabile in JSON).

        Returns:
            int: Il numero di client che hanno ricevuto il messaggio.
        """
        if isinstance(message, (dict, list)):
            message = json.dumps(message)
        return self.client.publish(channel, message)

    def subscribe_and_listen(self, channel: str, callback: Callable[[Any], None]):
        """
        Metodo bloccante che resta in ascolto di un canale. 
        Da usare se il processo corrente deve solo gestire messaggi in entrata.

        Args:
            channel (str): Canale da monitorare.
            callback (Callable): Funzione da eseguire ogni volta che arriva un messaggio.
        """
        pubsub = self.client.pubsub()
        pubsub.subscribe(channel)
        
        for item in pubsub.listen():
            if item['type'] == 'message':
                data = item['data']
                try:
                    data = json.loads(data)
                except (ValueError, TypeError):
                    pass
                callback(data)

    def subscribe_in_background(self, channel: str, callback: Callable) -> threading.Event:
        """
        Avvia un thread demone che ascolta un canale senza bloccare il flusso principale.

        Args:
            channel (str): Canale da monitorare.
            callback (Callable): Funzione da eseguire alla ricezione di un messaggio.

        Returns:
            threading.Event: Un oggetto evento che, se impostato (.set()), ferma il thread.
        """
        stop_event = threading.Event()
        
        def _worker():
            pubsub = self.client.pubsub()
            pubsub.subscribe(channel)
            
            while not stop_event.is_set():
                # Il timeout permette al ciclo di controllare stop_event regolarmente
                msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.5)
                if msg:
                    data = msg['data']
                    try:
                        data = json.loads(data)
                    except:
                        pass
                    callback(data)
            
            pubsub.unsubscribe(channel)
            pubsub.close()

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        
        # Salvataggio interno per tracciabilità (opzionale)
        with self._lock:
            self._active_subscriptions[channel] = stop_event
            
        return stop_event

    def stop_all_subscriptions(self):
        """Chiude gentilmente tutti i thread attivi in questa istanza."""
        with self._lock:
            for channel, stop_event in self._active_subscriptions.items():
                print(f"Fermando canale: {channel}")
                stop_event.set()
        self._active_subscriptions.clear()