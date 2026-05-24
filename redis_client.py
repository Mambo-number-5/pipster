import redis
import os
import threading
import json
from typing import Any, Optional, Callable, cast

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
        self.client = redis.Redis(host=self.host,
                                port=self.port,
                                db=self.db,
                                decode_responses=True,
                                health_check_interval=30,  # Controlla la connessione in background ogni 30s
                                retry_on_timeout=True,     # Ci riprova automaticamente se la rete fluttua
                                socket_keepalive=True      # Mantiene il canale aperto con il container
                                )
        
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
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_write")
            except Exception:
                print("Errore nell'incremento del contatore redis_write")
                pass # Evita che un problema di rete nel log generi un crash a catena

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
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_read")
            except Exception:
                print("Errore nell'incremento del contatore redis_read")
                pass # Evita che un problema di rete nel log generi un crash a catena

            print(f"Errore RedisJSON get: {e}")
            return default

    def append_to_list(self, key: str, item: Any, path: str = "$", max_len: int = 0) -> bool:
        """
        Aggiunge atomicamente un elemento a un array JSON esistente. 
        Se max_len è specificato, taglia l'array mantenendo solo gli ultimi N elementi,
        prevenendo saturazioni della memoria (Memory Leak).

        Args:
            key (str): La chiave dell'array JSON.
            item (Any): L'elemento (es. dizionario candela) da aggiungere in coda.
            path (str, opzionale): Il percorso dell'array all'interno del JSON. Default "$".
            max_len (int, opzionale): Numero massimo di elementi da mantenere. Se l'array 
                                      supera questo limite, i più vecchi vengono scartati.
        Returns:
            bool: True se l'elemento è stato aggiunto (e l'array eventualmente tagliato), 
                  False se la chiave non esiste o non è un array.
        """
        try:
            # Se la chiave non esiste, inizializzala come lista vuota
            if not self.client.exists(key):
                self.set_json(key, [])
            
            # Aggiunge il nuovo elemento
            result = self.client.json().arrappend(key, path, item)
            
            # Operazione di Trimming: mantiene solo gli ultimi 'max_len' elementi
            if max_len > 0:
                # Utilizzando indici negativi (-max_len a -1), Redis taglia l'inizio della lista 
                # mantenendo intatta la "coda" più recente.
                self.client.json().arrtrim(key, path, -max_len, -1)
                
            return bool(result and None not in result)
        
        except redis.exceptions.ResponseError as e: # type: ignore
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_write")
            except Exception:
                print("Errore nell'incremento del contatore redis_write")
                pass # Evita che un problema di rete nel log generi un crash a catena

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
        try:
            if isinstance(message, (dict, list)):
                message = json.dumps(message)
            return cast(int, self.client.publish(channel, message))
        except Exception as e:
            # Monitoraggio Errori
            try:
                self.client.incr("errors:counter:redis_pubsub")
            except:
                print("Errore nell'incremento del contatore redis_pubsub")
                pass
            print(f"❌ Errore Pub/Sub (Publish) su {channel}: {e}")
            return 0

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
            try:
                pubsub = self.client.pubsub()
                pubsub.subscribe(channel)
                
                while not stop_event.is_set():
                    try:
                        # Il timeout permette al ciclo di controllare stop_event regolarmente
                        msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
                        if msg:
                            data = msg['data']
                            try:
                                data = json.loads(data)
                            except:
                                pass
                            callback(data)
                    except Exception as e:
                        # Se cade la connessione nel thread, lo tracciamo
                        try:
                            self.client.incr("errors:counter:redis_pubsub")
                        except:
                            print("Errore nell'incremento del contatore redis_pubsub")
                            pass
                        print(f"❌ Errore nel thread in ascolto su {channel}: {e}")
                        time.sleep(2.5) # Evita il surriscaldamento della CPU in caso di riavvio
            
                pubsub.unsubscribe(channel)
                pubsub.close()
            except Exception as outer_e:
                try:
                    self.client.incr("errors:counter:redis_pubsub")
                except:
                    print("Errore nell'incremento del contatore redis_pubsub")
                    pass
                print(f"❌ Errore critico inibizione Pub/Sub {channel}: {outer_e}")
            finally:
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

    # --- METODI STREAM (Event Logging & History) ---

    def log_event(self, stream_name: str, event_data: dict, max_len: int = 2000) -> Optional[str]:
        """
        Aggiunge un evento allo stream. Usa max_len per mantenere il log snello (FIFO).

        Args:
            stream_name (str): Il nome della chiave dello stream su Redis.
            event_data (dict): Dizionario contenente i dati dell'evento (coppie chiave-valore).
            max_len (int, optional): Numero massimo di elementi da mantenere nello stream. 
                Default a 2000.

        Returns:
            Optional[str]: L'ID generato per la nuova entry (formato 'timestamp-sequence') 
                o None in caso di errore.
        """
        try:
            # Sanificazione dati: se un valore è una lista o un dizionario, lo trasformiamo in stringa JSON
            sanitized_data = {}
                
            for k, valore in event_data.items():
                # Chiamata singola all'inizio: estraiamo il tipo esatto
                tipo_valore = type(valore)

                # Confronto diretto della classe (molto più veloce di chiamare isinstance più volte)
                if tipo_valore is str or tipo_valore is int or tipo_valore is float:
                    sanitized_data[k] = valore

                elif tipo_valore is bool:
                    sanitized_data[k] = 1 if valore else 0

                elif tipo_valore is dict or tipo_valore is list:
                    sanitized_data[k] = json.dumps(valore)
                
                elif valore is not None:
                    # Fallback sicuro per oggetti particolari, conversioni custom, ecc.
                    sanitized_data[k] = str(valore)

            # XADD nome_stream MAXLEN ~ 2000 * campi...
            # Il simbolo '*' genera automaticamente l'ID basato sul timestamp
            entry_id = self.client.xadd(name=stream_name, fields=sanitized_data, maxlen=max_len, 
                approximate=True  # Più efficiente: non taglia esattamente a 2000 ma quando comodo a Redis
            )
            return cast(str, entry_id)
        except Exception as e:
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_write")
            except Exception:
                print("Errore nell'incremento del contatore redis_write")
                pass # Evita che un problema di rete nel log generi un crash a catena
                
            print(f"❌ Errore Stream XADD su {stream_name}: {e}")
            return None

    def get_recent_events(self, stream_name: str, count: int = 10) -> list:
        """
        Recupera gli ultimi N eventi dallo stream.

        Args:
            stream_name (str): Il nome dello stream da interrogare.
            count (int, optional): Numero di eventi recenti da restituire. Default a 10.

        Returns:
            List[Any]: Una lista di tuple o dizionari (a seconda della config del client) 
                contenenti ID e dati degli eventi. Ritorna una lista vuota in caso di errore.
        """
        try:
            # XREVRANGE legge dallo stream al contrario (dal più recente)
            events = cast(list, self.client.xrevrange(stream_name, max='+', min='-', count=count))
            return events
        except Exception as e:
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_read")
            except Exception:
                print("Errore nell'incremento del contatore redis_read")
                pass # Evita che un problema di rete nel log generi un crash a catena

            print(f"⚠️ Errore lettura stream {stream_name}: {e}")
            return []

    # --- CONSUMER GROUPS (Resilienza e Scalabilità) ---
    def create_consumer_group(self, stream_name: str, group_name: str) -> None:
        """
        Crea un gruppo di consumatori per uno stream specifico. 
        Se lo stream non esiste, viene creato automaticamente (MKSTREAM).

        Args:
            stream_name (str): Nome dello stream Redis.
            group_name (str): Nome del gruppo di consumatori da creare.
        
        Note:
            Viene usato l'ID '0' per indicare al gruppo di iniziare a leggere dall'inizio della storia dello stream.
        """
        try:
            # id='0' = Leggi tutto dall'inizio. Usa id='$' per leggere solo i nuovi arrivi da ora in poi.
            self.client.xgroup_create(stream_name, group_name, id='0', mkstream=True)
        except redis.exceptions.ResponseError as e: # type: ignore
            # Ignoriamo l'errore se il gruppo esiste già, altrimenti lo segnaliamo
            if "already exists" not in str(e):
                print(f"❌ Errore creazione gruppo: {e}")
            
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_admin")
            except Exception:
                print("Errore nell'incremento del contatore redis_admin")
                pass # Evita che un problema di rete nel log generi un crash a catena


    def read_group(self, stream_name: str, group_name: str, consumer_name: str, last_id: str = '>', count: int = 1, block: int = 0) -> list[Any]:
        """
        Legge messaggi inediti destinati a un consumatore specifico all'interno di un gruppo.

        Args:
            stream_name (str): Nome dello stream.
            group_name (str): Nome del gruppo di consumatori.
            consumer_name (str): Nome univoco del consumatore (es. 'worker-1').
            last_id (str): L'ID da cui partire. 
                - ">" per nuovi messaggi mai consegnati.
                - "0" per messaggi in sospeso (PENDING) non ancora confermati.
            count (int, optional): Numero massimo di messaggi da prelevare. Default 1.
            block (int, optional): Tempo di attesa in millisecondi (0 = infinito). 
                Se > 0, il metodo diventa bloccante finché non arriva un messaggio.

        Returns:
            List[Any]: Lista di messaggi ricevuti. Formato tipico: 
                [[b'stream_name', [(b'id', {b'chiave': b'valore'})]]]
        """
        try:
            # L'ID '>' indica a Redis di consegnare solo messaggi "nuovi" (mai letti dal gruppo)
            result = self.client.xreadgroup(
                groupname=group_name, 
                consumername=consumer_name, 
                streams={stream_name: last_id}, 
                count=count, 
                block=block
            )
            return cast(list, result)
        except Exception as e:
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_read")
            except Exception:
                print("Errore nell'incremento del contatore redis_read")
                pass # Evita che un problema di rete nel log generi un crash a catena

            print(f"⚠️ Errore lettura gruppo {group_name}: {e}")
            return []

    def acknowledge(self, stream_name: str, group_name: str, message_id: str) -> bool:
        """
        Conferma l'avvenuta elaborazione di un messaggio (XACK). 
        Rimuove il messaggio dalla PEL (Pending Entries List) del gruppo.

        Args:
            stream_name (str): Nome dello stream.
            group_name (str): Nome del gruppo di consumatori.
            message_id (str): L'ID del messaggio da confermare.

        Returns:
            bool: True se il messaggio è stato confermato correttamente, False altrimenti.
        """
        try:
            # Ritorna il numero di messaggi confermati (dovrebbe essere 1)
            return bool(self.client.xack(stream_name, group_name, message_id))
        except Exception as e:
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_admin")
            except Exception:
                print("Errore nell'incremento del contatore redis_admin")
                pass # Evita che un problema di rete nel log generi un crash a catena

            print(f"❌ Errore XACK per ID {message_id}: {e}")
            return False
    
    def get_pending_messages(self, stream: str, group: str, count: int = 10) -> list[dict]:
        """
        Interroga la Pending Entries List (PEL) di un Consumer Group.

        Recupera informazioni dettagliate sui messaggi che sono stati consegnati ai 
        consumer ma non sono ancora stati confermati con un ACK. Questo metodo è 
        fondamentale per implementare logiche di recovery (riassegnazione messaggi) 
        e per identificare 'Poison Messages' (messaggi velenosi).

        Args:
            stream (str): Il nome dello stream da interrogare.
            group (str): Il nome del gruppo di consumatori associato allo stream.
            count (int, optional): Numero massimo di messaggi pendenti da restituire. 
                Default è 10.

        Returns:
            list[dict]: Una lista di dizionari, dove ogni dizionario contiene:
                - 'message_id' (str): L'ID univoco del messaggio in Redis (es. '1714400000-0').
                - 'consumer' (str): Il nome del consumer che ha attualmente in carico il messaggio.
                - 'time_since_delivered' (int): Millisecondi passati dall'ultima consegna.
                - 'times_delivered' (int): Numero totale di tentativi di consegna effettuati.

        Raises:
            redis.exceptions.RedisError: In caso di problemi di connessione o se 
                lo stream/gruppo non esistono.

        Note:
            Viene utilizzato il comando `XPENDING` con il range completo (da '-' a '+').
            Se il counter 'times_delivered' è significativamente più alto di 1, 
            potrebbe indicare che il messaggio causa un crash del consumer.
        """
        try:
            # XPENDING <stream> <group> <min> <max> <count>
            # '-' e '+' indicano rispettivamente l'ID minimo e massimo possibile.
            return cast(list, self.client.xpending_range(stream, group, min='-', max='+', count=count))
        except Exception as e:
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_admin")
            except Exception:
                print("Errore nell'incremento del contatore redis_admin")
                pass # Evita che un problema di rete nel log generi un crash a catena

            print(f"⚠️ Errore durante l'interrogazione XPENDING su {stream}: {e}")
            return []
    
    def get_message_by_id(self, stream: str, msg_id: str) -> Optional[dict]:
        """
        Recupera il contenuto di un singolo messaggio specifico tramite il suo ID.

        A differenza di `read_group`, questo metodo non interagisce con i cursori 
        del Consumer Group e non sposta il puntatore di lettura. Viene utilizzato 
        principalmente durante le fasi di recovery o per isolare i dati di un 
        'Poison Message' prima di spostarlo nella Dead Letter Queue.

        Args:
            stream (str): Il nome dello stream in cui cercare il messaggio.
            msg_id (str): L'ID univoco del messaggio Redis (es. '1714400000-0').

        Returns:
            Optional[dict]: Il dizionario contenente i dati del messaggio se trovato, 
                altrimenti None.

        Note:
            Internamente utilizza il comando `XRANGE` impostando sia l'ID minimo 
            che l'ID massimo al valore di `msg_id`. Questo garantisce il recupero 
            esatto del record cercato.
        """
        try:
            # XRANGE stream_name start end [COUNT n]
            res = cast(list, self.client.xrange(stream, min=msg_id, max=msg_id, count=1))
            
            # La struttura restituita da xrange è: [[ID, {campi}]]
            if res and len(res) > 0:
                return cast(dict, res[0][1])
            return None
            
        except Exception as e:
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_read")
            except Exception:
                print("Errore nell'incremento del contatore redis_read")
                pass # Evita che un problema di rete nel log generi un crash a catena

            print(f"⚠️ Errore nel recupero puntuale del messaggio {msg_id}: {e}")
            return None
    
    def autoclaim_messages(self, stream_name: str, group_name: str, consumer_name: str, min_idle_time_ms: int, start_id: str = '0', count: int = 10) -> tuple[str, list]:
        """
        Rivendica (claim) i messaggi bloccati nella PEL (Pending Entries List) da troppo tempo,
        riassegnandoli a questo consumer. Fondamentale per la recovery post-crash.

        Args:
            stream_name (str): Nome dello stream Redis.
            group_name (str): Nome del gruppo di consumatori.
            consumer_name (str): Il nome del worker ATTUALE che sta rivendicando i messaggi.
            min_idle_time_ms (int): Tempo minimo in millisecondi da cui il messaggio deve 
                                    essere "pending" prima di poter essere rubato.
            start_id (str, optional): L'ID da cui iniziare la scansione della PEL. Default '0'.
            count (int, optional): Numero massimo di messaggi da rivendicare in una chiamata.

        Returns:
            tuple[str, list]: Una tupla contenente:
                - Il prossimo ID da cui continuare la scansione per l'autoclaim successivo.
                - Una lista dei messaggi rivendicati con successo (formato identico a read_group).
        """
        try:
            # XAUTOCLAIM <stream> <group> <consumer> <min-idle-time> <start> [COUNT count]
            # Restituisce il nuovo puntatore e i messaggi recuperati
            result = cast(list, self.client.xautoclaim(
                name=stream_name,
                groupname=group_name,
                consumername=consumer_name,
                min_idle_time=min_idle_time_ms,
                start_id=start_id,
                count=count
            ))
            
            # Redis-py restituisce una lista/tupla dove l'elemento 0 è il next_id 
            # e l'elemento 1 è la lista dei messaggi.
            next_start_id = result[0]
            claimed_messages = result[1]
            
            return cast(str, next_start_id), cast(list, claimed_messages)
            
        except Exception as e:
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_admin")
            except Exception:
                print("Errore nell'incremento del contatore redis_admin")
                pass # Evita che un problema di rete nel log generi un crash a catena

            print(f"⚠️ Errore durante l'XAUTOCLAIM sullo stream {stream_name}: {e}")
            return start_id, []
    
    # --- METODI HASH (Fast Cache Layer - Latenza Minima) ---
    def set_hash(self, key: str, mapping: dict[str, Any], expire_seconds: int = 0) -> bool:
        """
        Scrive o aggiorna in modo atomico più campi all'interno di un Hash Redis (HSET).

        Rappresenta il layer a latenza minima (complessità temporale O(1)) ideale per 
        memorizzare snapshot in tempo reale (es. ticker di mercato, metriche ad alta 
        frequenza o indicatori volatili) che non richiedono strutture annidate.

        Args:
            key (str): Chiave identificativa dell'Hash (es. 'ticker:live:BTCUSDT').
            mapping (dict[str, Any]): Dizionario contenente le coppie campo-valore da salvare.
            expire_seconds (int, opzionale): Tempo di scadenza della chiave in secondi (TTL).

        Returns:
            bool: True se la scrittura ha avuto successo, False in caso di eccezioni.
        """
        try:
            self.client.hset(key, mapping=mapping)
            if expire_seconds:
                self.client.expire(key, expire_seconds)
            return True
        except Exception as e:
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_write")
            except Exception:
                print("Errore nell'incremento del contatore redis_write")
                pass # Evita che un problema di rete nel log generi un crash a catena

            print(f"❌ Errore Redis Hash HSET su {key}: {e}")
            return False

    def get_hash(self, key: str) -> dict[str, str]:
        """
        Recupera l'intero contenuto di un Hash Redis (HGETALL).

        Restituisce tutti i campi e i valori associati alla chiave specificata. Avendo 
        configurato il client con `decode_responses=True`, i tipi restituiti vengono 
        automaticamente convertiti in stringhe Python.

        Args:
            key (str): La chiave dell'Hash da interrogare.

        Returns:
            dict[str, str]: Dizionario con le coppie campo-valore estratte, 
                            o un dizionario vuoto se la chiave non esiste.
        """
        try:
            return cast(dict[str, str], self.client.hgetall(key))
        except Exception as e:
            # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_read")
            except Exception:
                print("Errore nell'incremento del contatore redis_read")
                pass # Evita che un problema di rete nel log generi un crash a catena

            print(f"⚠️ Errore lettura Hash HGETALL su {key}: {e}")
            return {}

    def get_hash_field(self, key: str, field: str) -> Optional[str]:
        """
        Recupera il valore di un singolo campo specifico all'interno di un Hash (HGET).

        Sfrutta la complessità O(1) di Redis per estrarre selettivamente un dato 
        senza dover caricare l'intera struttura in memoria, ottimizzando l'uso della banda.

        Args:
            key (str): La chiave dell'Hash.
            field (str): Il nome del campo specifico da estrarre.

        Returns:
            Optional[str]: Il valore del campo come stringa, oppure None se la chiave 
                           o il campo non esistono.
        """
        try:
            return cast(Optional[str], self.client.hget(key, field))
        except Exception as e:
                        # PREDISPOSIZIONE MONITORAGGIO: Incrementiamo un contatore centralizzato su Redis
            try:
                self.client.incr("errors:counter:redis_read")
            except Exception:
                print("Errore nell'incremento del contatore redis_read")
                pass # Evita che un problema di rete nel log generi un crash a catena

            print(f"⚠️ Errore lettura campo Hash HGET su {key}:{field}: {e}")
            return None