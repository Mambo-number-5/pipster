import redis
import os
import threading
import json
from typing import Any, Optional, Callable

class RedisClient:
    """
    Client ottimizzato per Redis con supporto nativo a RedisJSON
    e operazioni Pub/Sub per il trading in tempo reale.
    """

    def __init__(self):
        """
        Inizializza la connessione a Redis utilizzando le variabili d'ambiente.
        Configura inoltre i sistemi di controllo per il multi-threading.
        """
        self.host = os.getenv("REDIS_HOST", "localhost")
        self.port = int(os.getenv("REDIS_PORT", 6379))
        self.db = int(os.getenv("REDIS_DB", 0)) # Default 0 per produzione
        self.client = redis.Redis(host=self.host, port=self.port, db=self.db ,decode_responses=True)
        
        # Lucchetto per garantire l'accesso thread-safe a risorse condivise interne
        self._lock = threading.Lock() 
        # Registro per gestire i segnali di stop dei thread in background
        self._active_subscriptions = {}

    # --- METODI REDIS-JSON (Hot Data Layer - Tempo reale) ---

    def set_json(self, key: str, data: Any, path: str = "$", expire_seconds: Optional[int] = None) -> bool:
        """
        Salva un oggetto JSON e, se specificato, imposta un tempo di scadenza (TTL).

        Args:
            key (str): La chiave identificativa.
            data (Any): L'oggetto da salvare.
            path (str): Il percorso JSON. Default "$".
            expire_seconds (int, opzionale): Tempo di scadenza in secondi.

        Returns:
            bool: True se il salvataggio ha avuto successo.
        """
        try:
            # Passaggio 1: Salviamo il JSON
            success = self.client.json().set(key, path, data)
        
            # Passaggio 2: Se abbiamo un TTL e il salvataggio è riuscito, impostiamo la scadenza
            if success and expire_seconds:
                success = self.client.expire(key, expire_seconds)
            
            return bool(success)
        
        except Exception as e:
            print(f"Errore RedisJSON set: {e}")
            return False

    def get_json(self, key: str, path: str = "$", default: Any = None) -> Any:
        """
        Recupera un oggetto JSON nativo o una sua specifica parte.

        Args:
            key (str): La chiave da interrogare.
            path (str, opzionale): Il selettore JSONPath per estrarre solo dati specifici. Default "$" (intero oggetto).
            default: Il valore di default restituito nel caso in cui non redis non trova chiavi oppure incontra errori.
        Returns:
            Optional[Any]: I dati estratti (già convertiti in tipi Python) o default se la chiave non esiste.
        """
        try:
            data = self.client.json().get(key, path)

            if data is None:
                return default
            
            # Se usiamo JSONPath ($), Redis restituisce sempre una lista.
            # Se la lista è vuota, torniamo il default.
            elif isinstance(data, list) and path.startswith("$"):
                return data[0] if data else default
            
            else:
                return data
        
        except Exception as e:
            print(f"Errore RedisJSON get: {e}")
            return default


    def append_to_list(self, key: str, item: Any, path: str = "$") -> bool:
        """
        Aggiunge atomicamente un elemento a un array JSON esistente. 
        Ideale per lo streaming di candele senza dover ri-caricare l'intero storico.

        Args:
            key (str): La chiave dell'array JSON.
            item (Any): L'elemento (es. dizionario candela) da aggiungere in coda.
            path (str, opzionale): Il percorso dell'array all'interno del JSON. Default "$".
        Returns:
            bool: True se l'elemento è stato aggiunto, False se la chiave non esiste o non è un array.
        """
        try:
            # Se la chiave non esiste, inizializzala come lista vuota
            if not self.client.exists(key):
                self.set_json(key, [])
            result = self.client.json().arrappend(key, path, item)
            return bool(result and result[0] is not None)
        
        except redis.exceptions.ResponseError as e: # type: ignore
            print(f"❌ Errore: Stai cercando di aggiungere dati a un campo che non è una lista! {e}")
            return False


    # --- METODI PUB/SUB (Mantenuti per il flusso dal Broker) ---

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
        return bool(self.client.publish(channel, message))


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
            # Cancellazione dell'elemento dal dizionario delle sottoscrizioni attive
            with self._lock:
                self._active_subscriptions.pop(channel, None) # Non esplode se la chiave è già sparita

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