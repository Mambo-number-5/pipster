# core/rate_limiter.py
"""Modulo per la gestione avanzata dei Rate Limit distribuiti e locali.

Fornisce primitive per il controllo del flusso di invio richieste verso le API
degli exchange (es. Bitget), sia a livello distribuito (tramite Redis e script Lua)
che in-memory (per singola connessione WebSocket).
"""
from contextlib import AsyncExitStack
import asyncio
import time
import uuid
import redis.asyncio as redis
from collections import deque
from typing import cast, Any
import logging

# Logger standard per modulo
logger = logging.getLogger(__name__)

class RedisTokenBucket:
    """Implementazione di un Token Bucket distribuito basato su Redis e Lua script.

    Garantisce il rispetto dei Rate Limit in architetture multi-container (Docker)
    utilizzando un'unica sorgente di dati atomica. Implementa il calcolo dinamico del
    tempo di attesa e gestisce il refill dei token basandosi sul tempo di sistema.

    Attributes:
        redis (redis.Redis): Client asincrono di Redis.
        key (str): Chiave Redis con prefisso di namespace per la memorizzazione del bucket.
        rate (float): Numero di token rigenerati al secondo.
        capacity (int): Capacità massima del secchio (burst consentito).
    """
    
    def __init__(self, redis_client: redis.Redis, key: str, rate: float, capacity: int):
        """Inizializza una nuova istanza di RedisTokenBucket.

        Args:
            redis_client (redis.Redis): Istanza attiva del client Redis asincrono.
            key (str): Identificativo univoco dell'endpoint o risorsa da limitare.
            rate (float): Frequenza di ricarica in token/secondo (es. 10.0 per 10 req/s).
            capacity (int): Numero massimo di token accumulabili per gestire burst di traffico.
        """
        self.redis = redis_client
        # Prefisso di sicurezza per evitare collisioni con altre chiavi
        self.key = f"rate_limit:token_bucket:{key}"
        self.rate = rate          # Token generati al secondo (es. 10)
        self.capacity = capacity  # Burst massimo consentito (es. 20)

        # register_script() compila lo script Lua sul server Redis in SHA1
        # Questo evita di inviare l'intero testo dello script ad ogni chiamata,
        # azzerando l'overhead di rete e massimizzando le performance.
        self._lua_script = self.redis.register_script("""
            local key = KEYS[1]
            local capacity = tonumber(ARGV[1])
            local rate_per_ms = tonumber(ARGV[2]) / 1000
            local now = tonumber(ARGV[3])
            local requested = tonumber(ARGV[4])

            -- Recuperiamo i token attuali e l'ultimo timestamp di aggiornamento
            local info = redis.call('HMGET', key, 'tokens', 'last_refill')
            local tokens = tonumber(info[1])
            local last_refill = tonumber(info[2])

            -- Inizializzazione al primo avvio
            if not tokens then
                tokens = capacity
                last_refill = now
            end

            -- Calcolo del tempo trascorso e dei token da ricaricare
            local delta_ms = math.max(0, now - last_refill)
            local new_tokens = math.floor(delta_ms * rate_per_ms)

            if new_tokens > 0 then
                -- Non possiamo superare la capacità massima del secchio
                tokens = math.min(capacity, tokens + new_tokens)
                last_refill = now
            end

            if tokens >= requested then
                -- Token disponibile: aggiorniamo i dati
                tokens = tokens - requested
                redis.call('HSET', key, 'tokens', tokens, 'last_refill', last_refill)
                
                -- Impostiamo un TTL per auto-cancellare la chiave se il bot viene spento
                local ttl_ms = math.ceil((capacity / rate_per_ms)) + 5000
                redis.call('PEXPIRE', key, ttl_ms)
                
                return 0 -- Successo, zero millisecondi di attesa
            else
                -- Token NON disponibile: calcoliamo i millisecondi esatti da aspettare
                local ms_to_wait = math.ceil((requested - tokens) / rate_per_ms) - delta_ms
                if ms_to_wait < 1 then ms_to_wait = 10 end -- Sicurezza per arrotondamenti
                return ms_to_wait
            end
        """)

    async def acquire(self, requested: int = 1):
        """Richiede l'emissione di uno o più token, sospendendo l'esecuzione se vuoto.

        Il metodo esegue atomicamente lo script Lua. Se i token richiesti non sono
        disponibili, calcola il tempo esatto di ricarica necessario ed effettua una
        pausa asincrona (`asyncio.sleep`) prima di tentare un nuovo reinserimento.

        Args:
            requested (int, optional): Numero di token da consumare. Default a 1.
        """
        if requested < 0:
            raise ValueError(f"requested non può essere negativo, ricevuto {requested}.")
        elif requested > self.capacity:
            raise ValueError(f"requested ({requested}) supera la capacità ({self.capacity}): "
                        "il bucket non potrà mai soddisfarla.")

        while True:
            # Calcoliamo il timestamp in Python (iniettarlo dall'esterno è una 
            # best practice per mantenere deterministici gli script Lua in Redis)
            now_ms = int(time.time() * 1000)
            
            # Eseguiamo lo script
            wait_ms = await self._lua_script(
                keys=[self.key],
                args=[self.capacity, self.rate, now_ms, requested]
            )

            # Log a livello DEBUG: utile per il debug e i test, ignorato in produzione
            # se il livello del logger è INFO o superiore (overhead trascurabile)
            logger.debug("TokenBucket %s: requested=%s wait_ms=%s", self.key, requested, wait_ms)


            # Se Redis ci dice 0 (o un nuomero negativo, impossibile), abbiamo il semaforo verde
            if cast(int, wait_ms) < 1:
                return
            
            # Altrimenti dormiamo in modo asincrono esattamente per il tempo calcolato
            await asyncio.sleep(cast(int, wait_ms) / 1000.0)


class RedisSlidingWindow:
    """Rate Limiter distribuito a Finestra Scorrevole (Sliding Window) su ZSET Redis.

    Traccia le singole chiamate API all'interno di una finestra temporale mobile in millisecondi.
    Usa `redis.call('TIME')` come Single Source of Truth temporale per evitare problemi
    di Clock Drift (disallineamento dell'orologio di sistema) tra più container.

    Attributes:
        redis (redis.Redis): Client asincrono di Redis.
        key (str): Chiave ZSET usata su Redis per memorizzare i timestamp.
        limit (int): Numero massimo di richieste consentite nella finestra temporale.
        window_ms (int): Ampiezza della finestra temporale espressa in millisecondi.
    """
    
    def __init__(self, redis_client: redis.Redis, key: str, limit: int, window_ms: int):
        """Inizializza una finestra scorrevole su Redis.

        Args:
            redis_client (redis.Redis): Istanza del client Redis asincrono.
            key (str): Identificativo della risorsa da limitare.
            limit (int): Numero massimo di chiamate permesse.
            window_ms (int): Durata della finestra di controllo in millisecondi (es. 60000 per 1 min).
        """
        self.redis = redis_client
        self.key = f"{key}:rate_limit:sliding_window"
        self.limit = limit
        self.window_ms = window_ms

        self._lua_script = self.redis.register_script("""
            local key = KEYS[1]
            local window_ms = tonumber(ARGV[1])
            local limit = tonumber(ARGV[2])
            local requested = tonumber(ARGV[3])
            -- ARGV[4..3+requested] contengono gli UUID

            -- 1. Ottiene il tempo atomico dal server Redis (secondi, microsecondi)
            local time_res = redis.call('TIME')
            local now = (tonumber(time_res[1]) * 1000) + math.floor(tonumber(time_res[2]) / 1000)
            
            -- 2. Pulisce i record antecedenti alla finestra attuale
            local clear_before = now - window_ms
            redis.call('ZREMRANGEBYSCORE', key, 0, clear_before)

            -- 3. Conteggio attuale
            local current_count = redis.call('ZCARD', key)

            -- 4. Verifica capienza
            if current_count + requested <= limit then
                for i = 1, requested do
                    redis.call('ZADD', key, now, ARGV[3 + i])
                end
                redis.call('PEXPIRE', key, window_ms)
                return {1, current_count + requested}
            else
                -- Calcola quanti slot devono liberarsi
                local excess = current_count + requested - limit
                local target_index = excess - 1  -- 0-indexed
                local entry = redis.call('ZRANGE', key, target_index, target_index, 'WITHSCORES')
                local wait_ms = window_ms
                if entry and entry[2] then
                    wait_ms = (tonumber(entry[2]) + window_ms) - now
                end
                if wait_ms < 1 then wait_ms = 1 end
                return {0, wait_ms}
            end
        """)

    async def acquire(self, requested: int = 1) -> int:
        """Acquisisce i permessi per eseguire una o più chiamate API.

        Valuta se la richiesta rientra nella finestra temporale corrente. In caso
        di superamento del limite, mette in attesa il task per il tempo strettamente
        necessario alla liberazione degli slot necessari.

        Args:
            requested (int, optional): Numero di slot da richiedere nella finestra. Default a 1.

        Returns:
            int: Numero di chiamate residue ancora disponibili nella finestra corrente.

        Raises:
            ValueError: Se `requested` supera la capacità totale `limit` prevista dal limiter.
        """
        if requested > self.limit:
            raise ValueError(
                f"requested ({requested}) supera il limite totale ({self.limit})."
            )

        while True:
            # Python genera solo identificatori casuali univoci
            uuids = [uuid.uuid4().hex for _ in range(requested)]
            args = [self.window_ms, self.limit, requested] + uuids

            result = cast(tuple[int, int], await self._lua_script(keys=[self.key], args=args))

            if result[0] == 1:
                current_count = result[1]
                remaining = self.limit - current_count
                logger.debug(
                    "SlidingWindow %s: granted=%d remaining=%d",
                    self.key, requested, remaining
                )
                return remaining
            else:
                wait_ms = result[1]
                logger.debug(
                    "SlidingWindow %s: hard limit, wait %d ms",
                    self.key, wait_ms
                )
                await asyncio.sleep(wait_ms / 1000.0)


class RateLimitManager:
    """Manager Atomico Multi-Bucket per la gestione congiunta di Rate Limit multipli.

    Permette di acquisire contemporaneamente permessi su più limiti (es. Limite Globale IP
    + Limite specifico dell'Endpoint) secondo uno schema atomico **All-or-Nothing**. Se anche
    un solo bucket risulta esaurito, l'operazione esegue un rollback implicito garantendo
    zero consumo parziale dei token sugli altri bucket.

    Attributes:
        redis (redis.Redis): Client asincrono Redis.
        exchange (str): Nome dell'exchange (es. 'bitget') usato per il namespace delle chiavi.
        prefix (str): Prefisso Redis con Hashtag `{exchange}` per garantire il mapping nello
            stesso nodo in configurazioni Redis Cluster.
        buckets (dict[str, tuple[int, int]]): Mappa dei limitatori registrati `nome -> (limit, window_ms)`.
    """

    def __init__(self, redis_client: redis.Redis, exchange: str):
        """Inizializza il RateLimitManager.

        Args:
            redis_client (redis.Redis): Istanza attiva del client Redis.
            exchange (str): Nome identificativo dell'exchange di riferimento.
        """
        self.redis = redis_client
        self.exchange = exchange
        # Le chiavi avranno il formato "{exchange}:limiter_name"
        # Le parentesi graffe {} dicono a Redis Cluster di mappare tutte queste chiavi
        # sullo stesso nodo fisico, requisito fondamentale per le transazioni Lua multi-key.
        self.prefix = f"{{{self.exchange}}}:rate_limit:"
        
        # Mappa dei bucket registrati: name -> (limit, window_ms)
        self.buckets: dict[str, tuple[int, int]] = {}

        # Script Lua Unificato: Valuta N chiavi atomicamente
        self._lua_script = self.redis.register_script("""
            local n = #KEYS
            local redis_time = redis.call('TIME')
            local now = tonumber(redis_time[1]) * 1000 + math.floor(tonumber(redis_time[2]) / 1000)
            local requested = tonumber(ARGV[1])

            -- ARGV struttura:
            -- 1: requested
            -- 2..n+1: window_ms per ogni chiave
            -- n+2..2n+1: limit per ogni chiave
            -- 2n+2... : UUIDs

            local max_wait_ms = 0
            local can_proceed = true
            local current_counts = {}

            -- FASE 1: Dry-Run (Controllo senza modificare)
            for i = 1, n do
                local key = KEYS[i]
                local window = tonumber(ARGV[1 + i])
                local limit = tonumber(ARGV[1 + n + i])
                
                -- Pulizia record scaduti
                local clear_before = now - window
                redis.call('ZREMRANGEBYSCORE', key, 0, clear_before)
                
                -- Lettura stato
                local count = redis.call('ZCARD', key)
                current_counts[i] = count
                
                if count + requested > limit then
                    can_proceed = false
                    
                    -- Calcolo dell'attesa necessaria per QUESTO bucket
                    local excess = count + requested - limit
                    local target_index = excess - 1
                    local entry = redis.call('ZRANGE', key, target_index, target_index, 'WITHSCORES')
                    
                    local wait_ms = window
                    if entry and entry[2] then
                        wait_ms = (tonumber(entry[2]) + window) - now
                    end
                    if wait_ms < 1 then wait_ms = 1 end
                    
                    -- Conserviamo il tempo di attesa maggiore tra tutti i bucket falliti
                    if wait_ms > max_wait_ms then 
                        max_wait_ms = wait_ms 
                    end
                end
            end

            -- Se anche un solo bucket fallisce, abortiamo e restituiamo l'attesa massima
            if not can_proceed then
                return {0, max_wait_ms}
            end

            -- FASE 2: Commit (Aggiorniamo tutti i bucket)
            local remaining_map = {}
            for i = 1, n do
                local key = KEYS[i]
                local window = tonumber(ARGV[1 + i])
                local limit = tonumber(ARGV[1 + n + i])
                
                -- Aggiungiamo i nuovi token
                for j = 1, requested do
                    redis.call('ZADD', key, now, ARGV[1 + 2*n + j])
                end
                redis.call('PEXPIRE', key, window)
                
                -- Calcoliamo il rimanente per il chiamante
                table.insert(remaining_map, key)
                table.insert(remaining_map, limit - (current_counts[i] + requested))
            end

            return {1, remaining_map}
        """)

    def add_limiter(self, name: str, limit: int, window_ms: int) -> None:
        """Registra una regola di rate limit all'interno del Manager.

        Args:
            name (str): Nome identificativo della regola (es. 'order_place' o 'ip_global').
            limit (int): Numero di chiamate permesse nella finestra temporale.
            window_ms (int): Ampiezza della finestra di controllo in millisecondi.
        """
        self.buckets[name] = (limit, window_ms)

    async def acquire(self, *limiter_names: str, requested: int = 1) -> dict[str, int]:
        """Tenta l'acquisizione atomica di token da molteplici limitatori contemporaneamente.

        Se uno qualsiasi dei limitatori richiesti non dispone di spazio sufficiente,
        nessun token viene consumato da alcun bucket e la chiamata viene messa in
        attesa asincrona fino al ripristino del bucket più vincolante.

        Args:
            *limiter_names (str): Nomi dei limitatori precedentemente registrati tramite `add_limiter`.
            requested (int, optional): Quantità di token da consumare su ciascun bucket. Default a 1.

        Returns:
            dict[str, int]: Dizionario contenente i token rimanenti per ciascun limiter specificato
                nella forma `{"nome_limiter": chiamate_rimanenti}`.

        Raises:
            KeyError: Se uno dei nomi specificati in `limiter_names` non è stato registrato.
        """
        # Prepariamo le liste per lo script Lua
        keys: list[str] = list()
        windows: list[int] = list()
        limits: list[int] = list()
        for name in limiter_names:
            if name not in self.buckets:
                raise KeyError(f"Bucket '{name}' non registrato.")

            limit, window = self.buckets[name]
            if requested > limit:
                raise ValueError(
                    f"requested ({requested}) supera il limite del bucket '{name}'({limit}).")
            keys.append(f"{self.prefix}{name}")
            windows.append(window)
            limits.append(limit)

        while True:
            # Generiamo gli UUID in Python per evitare randomness nello script Lua
            uuids: list[str] = [uuid.uuid4().hex for _ in range(requested)]
            
            args: list = [requested] + windows + limits + uuids
            
            result = cast(tuple[int, Any], await self._lua_script(keys=keys, args=args))
            
            if result[0] == 1:
                # Successo! Ricostruiamo il dizionario dai risultati (chiave, valore, chiave, valore...)
                raw_map = cast(list, result[1])
                remaining_map = {}
                for i in range(0, len(raw_map), 2):
                    # Puliamo il prefisso per restituire solo il nome originale al chiamante
                    clean_name = cast(str, raw_map[i].replace(self.prefix, ""))
                    remaining_map[clean_name] = cast(int, raw_map[i+1])
                
                logger.debug("Acquisizione atomica success: %s", remaining_map)
                return remaining_map
            else:
                # Fallimento su uno o più bucket. Nessun token consumato.
                wait_ms = cast(int, result[1])
                logger.debug("Rate limit hit su bucket multipli. Attesa: %d ms", wait_ms)
                await asyncio.sleep(wait_ms / 1000.0)


class TokenBucketManager:
    """Manager Atomico Multi-Bucket basato su algoritmo Token Bucket.

    Permette di acquisire contemporaneamente permessi su molteplici Token Bucket
    (es. limite IP + limite endpoint) in modo **strettamente atomico**. Se anche
    un solo bucket non possiede token sufficienti, nessuno dei bucket viene consumato,
    e la chiamata attende il ripristino del bucket più vincolante (all-or-nothing).

    Attributes:
        redis (redis.Redis): Client asincrono Redis.
        exchange (str): Nome dell'exchange usato per il namespace delle chiavi.
        prefix (str): Prefisso Redis con Hashtag `{exchange}` per garantire l'allocazione
            sullo stesso nodo in configurazioni Redis Cluster.
        buckets (dict[str, tuple[float, int]]): Mappa dei bucket registrati
            `nome -> (rate, capacity)`.
    """
    
    def __init__(self, redis_client: redis.Redis, exchange: str):
        """Inizializza il TokenBucketManager Atomico.

        Args:
            redis_client (redis.Redis): Client asincrono Redis.
            exchange (str): Nome identificativo dell'exchange.
        """
        self.redis = redis_client
        self.exchange = exchange

        # Hash Tag {exchange} obbligatorio per l'atomicità multi-key su Redis Cluster.
        self.prefix = f"{{{self.exchange}}}:token_bucket:"

        # Mappa locale della configurazione: name -> (rate, capacity)
        self.buckets: dict[str, tuple[float, int]] = {}

        # Script Lua Unificato: Valuta N Token Bucket atomicamente
        self._lua_script = self.redis.register_script("""
            local n = #KEYS
            local redis_time = redis.call('TIME')
            -- redis_time[1] = secondi, redis_time[2] = microsecondi
            local now = tonumber(redis_time[1]) + (tonumber(redis_time[2]) / 1000000.0)
            local requested = tonumber(ARGV[1])

            -- Struttura ARGV:
            -- 1: requested
            -- 2 .. n+1: rate per ogni chiave
            -- n+2 .. 2n+1: capacity per ogni chiave

            local max_wait_sec = 0
            local can_proceed = true
            local current_available = {}

            -- FASE 1: Dry-Run (Controllo disponibilità senza modificare)
            for i = 1, n do
                local key = KEYS[i]
                local rate = tonumber(ARGV[1 + i])
                local capacity = tonumber(ARGV[1 + n + i])
                
                -- HGETALL per leggere token residui e ultimo riempimento
                local bucket_data = redis.call('HMGET', key, 'tokens', 'last_refill')
                local tokens = tonumber(bucket_data[1])
                local last_refill = tonumber(bucket_data[2])
                
                -- Inizializzazione lazy se la chiave non esiste
                if not tokens or not last_refill then
                    tokens = capacity
                    last_refill = now
                end
                
                local elapsed = math.max(0, now - last_refill)
                local available = math.min(capacity, tokens + (elapsed * rate))
                current_available[i] = available
                
                if available < requested then
                    can_proceed = false
                    local wait_sec = (requested - available) / rate
                    if wait_sec > max_wait_sec then
                        max_wait_sec = wait_sec
                    end
                end
            end

            -- Se anche un solo bucket fallisce, abortiamo e restituiamo l'attesa.
            -- Usiamo tostring() per evitare perdite di precisione in virgola mobile verso Python.
            if not can_proceed then
                return {0, tostring(max_wait_sec)}
            end

            -- FASE 2: Commit (Sottraiamo token da tutti i bucket)
            local remaining_map = {}
            for i = 1, n do
                local key = KEYS[i]
                local rate = tonumber(ARGV[1 + i])
                local capacity = tonumber(ARGV[1 + n + i])
                
                local new_tokens = current_available[i] - requested
                
                -- Aggiornamento stato
                redis.call('HMSET', key, 'tokens', tostring(new_tokens), 'last_refill', tostring(now))
                
                -- TTL di pulizia: il tempo (in secondi) per rigenerare tutti i token + 2s di margine
                local ttl_sec = math.ceil(capacity / rate) + 2
                redis.call('EXPIRE', key, ttl_sec)
                
                table.insert(remaining_map, key)
                table.insert(remaining_map, tostring(new_tokens))
            end

            return {1, remaining_map}
        """)

    def add_bucket(self, name: str, rate: float, capacity: int) -> None:
        """Registra una regola Token Bucket all'interno del Manager.

        Args:
            name (str): Nome identificativo della regola.
            rate (float): Tasso di ricarica dei token al secondo.
            capacity (int): Capacità massima del secchio (burst consentito).
        """
        self.buckets[name] = (rate, capacity)

    async def acquire(self, *bucket_names: str, requested: int = 1) -> dict[str, float]:
        """Tenta l'acquisizione atomica di token da molteplici secchi contemporaneamente.

        Se uno qualsiasi dei bucket richiesti non possiede token sufficienti,
        nessun token viene scalato. La chiamata attende asincronamente il tempo
        necessario al ripristino del secchio più scarico.

        Args:
            *bucket_names (str): Nomi dei bucket precedentemente registrati.
            requested (int, optional): Quantità di token da consumare su ciascun bucket. Default a 1.

        Returns:
            dict[str, float]: Mappa contenente il numero di token rimanenti per ciascun
                bucket (es. `{"endpoint_candles": 4.5, "global": 18.2}`).

        Raises:
            KeyError: Se viene richiesto un bucket non preventivamente registrato.
        """
        keys = [f"{self.prefix}{name}" for name in bucket_names]
        rates = [self.buckets[name][0] for name in bucket_names]
        capacities = [self.buckets[name][1] for name in bucket_names]

        while True:
            args = [requested] + rates + capacities
            
            result = cast(tuple[int, Any], await self._lua_script(keys=keys, args=args))
            
            if result[0] == 1:
                # Acquisizione riuscita
                raw_map = cast(list, result[1])
                remaining_map = {}
                for i in range(0, len(raw_map), 2):
                    clean_name = cast(str, raw_map[i].decode('utf-8') if isinstance(raw_map[i], bytes) else raw_map[i]).replace(self.prefix, "")
                    # Convertiamo in float poiché il bucket genera frazioni di token
                    remaining_map[clean_name] = float(raw_map[i+1])
                
                logger.debug("TokenBucketManager acquisizione atomica: %s", remaining_map)
                return remaining_map
            else:
                # Fallimento (Rate limit). Estraggo e converto il wait_sec restituito dallo script.
                wait_sec = float(result[1])
                logger.debug("TokenBucket limit hit. Attesa %f sec (all-or-nothing rollback).", wait_sec)
                await asyncio.sleep(wait_sec)


class LocalSlidingWindow:
    """Throttle in-memory ad alta frequenza per la singola connessione WebSocket.

    Sfrutta una `collections.deque` a dimensione fissa (`maxlen`) per tracciare i timestamp
    degli invii in tempo $O(1)$ senza necessità di cicli iterativi di pulizia.

    Attributes:
        capacity (int): Numero massimo di messaggi pervenuti consentiti nella finestra.
        window_sec (float): Ampiezza della finestra di controllo espressa in secondi.
        name (str): Identificativo dell'istanza utile nei log di debug.
    """

    def __init__(self, capacity: int = 10, window_sec: float = 1.0, name: str = "SlidingWindow"):
        """Inizializza la finestra scorrevole locale in-memory.

        Args:
            capacity (int, optional): Messaggi massimi inviabili nella finestra. Default a 10.
            window_sec (float, optional): Durata della finestra in secondi. Default a 1.0.
            name (str, optional): Nome dell'istanza per il logging. Default a "SlidingWindow".
        """
        self.capacity = capacity
        self.window_sec = window_sec
        self.name = name

        # maxlen è il trucco: se la coda è piena, un nuovo append butta fuori il primo in automatico
        self._timestamps: deque[float] = deque(maxlen=capacity)
        #: Coda circolare a dimensione fissa contenente i timestamp (monotonic) delle richieste eseguite.
        
        self._lock: asyncio.Lock = asyncio.Lock()
        #: Lock asincrono per prevenire race condition durante la verifica e l'aggiunta di nuovi timestamp.

    async def acquire(self, requested: int = 1) -> None:
        """Regola il flusso d'uscita dei messaggi sospendendo l'esecuzione se saturo.

        Garantisce l'accesso sequenziale e thread-safe tramite un `asyncio.Lock`.
        Se la coda interna è piena ed il messaggio più vecchio è stato registrato
        meno di `window_sec` secondi fa, il task attende la differenza temporale.
        """
        while True:
            async with self._lock:
                now = time.monotonic()
                ok, wait = self._check_under_lock(now, requested)

                # Se la coda non è piena, acquisisco il token ed esco
                if ok:
                    self._commit_under_lock(now, requested)
                    return

            # Lock rilasciato: gli altri task possono valutare la propria attesa
            await asyncio.sleep(wait)

    def _check_under_lock(self, now: float, requested: int) -> tuple[bool, float]:
        """Dry-run. Ritorna (ok, wait_sec). Assume lock acquisito.

        Effetto collaterale: rimuove i timestamp scaduti (pulizia idempotente).
        """
        if requested < 0:
            raise ValueError(f"requested non può essere negativo, ricevuto {requested}.")
        elif requested > self.capacity:
            raise ValueError(
                f"requested ({requested}) supera la capacità ({self.capacity}).")

        cutoff = now - self.window_sec
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
            logger.debug("[%s] Finestra scorsa. Coda: %d/%d", self.name, len(self._timestamps), self.capacity)

        if len(self._timestamps) + requested <= self.capacity:
            return True, 0.0

        excess = len(self._timestamps) + requested - self.capacity
        idx = excess - 1
        if idx < len(self._timestamps):
            wait = (self._timestamps[idx] + self.window_sec) - now
        else:
            wait = self.window_sec
        logger.debug("[%s] Saturo (%d/%d, %.2fs). Attesa %.3fs.", self.name, len(self._timestamps), self.capacity, cutoff, wait)
        return False, max(wait, 0.001)

    def _commit_under_lock(self, now: float, requested: int) -> None:
        """Consuma `requested` slot. Assume lock acquisito e check già passato."""
        for _ in range(requested):
            self._timestamps.append(now)
            logger.debug("[%s] Token acquisito. Coda: %d/%d", self.name, len(self._timestamps), self.capacity)

    def locked(self) -> asyncio.Lock:
        """Ritorna il lock interno per coordinamento multi-bucket (ADR-0009)."""
        return self._lock

    def remaining(self) -> int:
        """Slot residui dopo l'ultimo commit. Solo per introspezione."""
        return self.capacity - len(self._timestamps)


class LocalFixedWindow:
    """Throttle in-memory a Finestra Fissa per limiti a bassa frequenza e lunga durata.

    Studiato per controlli a bassa frequenza (es. massimo 240 iscrizioni ai canali per ora).
    Implementa un meccanismo di reset pigro (Lazy Reset) per azzerare i contatori senza
    necessità di task background continui.

    Attributes:
        capacity (int): Numero massimo di azioni permesse nella finestra temporale.
        window_sec (float): Durata della finestra temporale espressa in secondi.
        name (str): Nome identificativo dell'istanza nei log.
    """

    def __init__(self, capacity: int, window_sec: float, name: str = "FixedWindow"):
        """Inizializza il contatore a finestra fissa in-memory.

        Args:
            capacity (int): Capacità massima del contatore prima del blocco.
            window_sec (float): Ampiezza della finestra temporale in secondi (es. 3600 per 1h).
            name (str, optional): Nome dell'istanza per il logging. Default a "FixedWindow".
        """
        self.capacity: int = capacity
        self.window_sec: float = window_sec
        self.name: str = name

        self._count: int = 0
        #: Contatore delle azioni/richieste accumulate all'interno della finestra corrente.

        self._window_start: float = time.monotonic()
        #: Timestamp (monotonic) dell'inizio della finestra corrente per la gestione del lazy reset.

        self._lock: asyncio.Lock = asyncio.Lock()
        #: Lock asincrono per prevenire race condition sul controllo, l'incremento e il reset del contatore.

    async def check_and_acquire(self, amount: int = 1) -> bool:
        """Verifica la disponibilità di token ed eventuale incremento del contatore.

        Esegue un reset pigro se il tempo trascorso dall'inizio della finestra supera `window_sec`.
        A differenza dei limiter bloccanti, questo metodo **non** effettua `sleep`, ma
        restituisce immediatamente un booleano esplicito.

        Args:
            amount (int, optional): Quantità di token da registrare. Default a 1.

        Returns:
            bool: `True` se l'operazione è stata autorizzata ed allocata, `False` se il
                limite massimo della finestra verrebbe superato.
        """
        if amount < 0:
            raise ValueError(f"amount non può essere negativo, ricevuto {amount}.")
        elif amount > self.capacity:
            raise ValueError(
                f"amount ({amount}) supera la capacità ({self.capacity}).")
        async with self._lock:
            now = time.monotonic()
            
            # Reset della finestra se è passato il tempo previsto
            if now - self._window_start >= self.window_sec:
                logger.debug(
                    "[%s] Finestra di %.1fs scaduta. Reset contatore (era: %d/%d).",
                    self.name,
                    self.window_sec,
                    self._count,
                    self.capacity,
                )
                self._count = 0
                self._window_start = now
            
            # Controlla se la richiesta sfora il limite
            if self._count + amount > self.capacity:
                logger.debug( "[%s] Richiesta di %d token RIFIUTATA. Supererebbe il limite (%d + %d > %d).",
                    self.name, amount, self._count, amount, self.capacity,)
                return False
            
            self._count += amount
            logger.debug("[%s] Acquisiti %d token. Utilizzo attuale finestra: %d/%d",
                self.name, amount, self._count, self.capacity,)
            return True


class LocalTokenBucket:
    """Implementazione in-memory dell'algoritmo Token Bucket per rate limiting locale.

    Utilizzato come fallback isolato e privo di dipendenze quando l'infrastruttura
    centralizzata (Redis) risulta irraggiungibile. Mantiene lo stato dei token in-memory,
    regolato da un lock asincrono per garantire la thread-safety all'interno dello stesso
    event loop.

    Attributes:
        rate (float): Tasso di ricarica continuo, espresso in token al secondo.
        capacity (int): Capienza massima del bucket (rappresenta il burst consentito).
        name (str): Identificativo logico del bucket per scopi di logging e tracciabilità.
    """

    def __init__(self, rate: float, capacity: int, name: str = "TokenBucket") -> None:
        """Inizializza il bucket locale validando i parametri di rate e capacità.

        Args:
            rate (float): Numero di token rigenerati al secondo (> 0).
            capacity (int): Numero massimo di token accumulabili (>= 1).
            name (str, optional): Nome del bucket per il logging. Defaults to "TokenBucket".

        Raises:
            ValueError: Se `rate` è <= 0 o se `capacity` è < 1.
        """
        if rate <= 0:
            raise ValueError(f"rate deve essere > 0, ricevuto {rate}.")
        if capacity < 1:
            raise ValueError(f"capacity deve essere >= 1, ricevuto {capacity}.")
        self.rate = rate
        self.capacity = capacity
        self.name = name
        
        self._tokens = float(capacity)
        #: Conteggio istantaneo dei token attualmente disponibili nel bucket.
        
        self._last_refill: float = time.monotonic()
        #: Timestamp (monotonic) dell'ultimo refill dei token.
        
        self._lock: asyncio.Lock = asyncio.Lock()
        #: Lock asincrono per prevenire race condition su _tokens e _last_refill.

    async def acquire(self, requested: int = 1) -> None:
        """Acquisisce in modo asincrono la quantità di token richiesta.

        Se i token disponibili nel bucket non sono sufficienti, il metodo sospende l'esecuzione
        (`asyncio.sleep`) per il tempo esatto necessario alla rigenerazione della quota mancante,
        rilasciando il lock durante l'attesa.

        Args:
            requested (int, optional): Numero di token da acquisire. Defaults to 1.

        Raises:
            ValueError: Se `requested` è negativo o se supera la capacità totale (`capacity`)
                del bucket, rendendo l'acquisizione strutturalmente impossibile.
        """
        if requested < 0:
            raise ValueError(f"requested non può essere negativo, ricevuto {requested}.")
        elif requested > self.capacity:
            raise ValueError(
                f"requested ({requested}) supera la capacità ({self.capacity}): "
                "il bucket non potrà mai soddisfarla.")

        while True:
            async with self._lock:
                now = time.monotonic()
                ok, wait = self._check_under_lock(now, requested)
                if ok:
                    self._commit_under_lock(requested)
                    return
                
                logger.debug(
                    "[%s] Rate locale esaurito. Attesa %.3fs per %d token "
                    "(disponibili: %.2f/%d).",
                    self.name, wait, requested, self._tokens, self.capacity,
                )

            await asyncio.sleep(wait)

    def _check_under_lock(self, now: float, requested: int) -> tuple[bool, float]:
        """Dry-run + refill. Ritorna (ok, wait_sec). Assume lock acquisito.

        Effetto collaterale: aggiorna `_tokens` e `_last_refill` (refill idempotente).
        """
        elapsed = now - self._last_refill
        self._tokens = min(float(self.capacity), self._tokens + elapsed * self.rate)
        self._last_refill = now

        if self._tokens >= requested:
            return True, 0.0

        wait = (requested - self._tokens) / self.rate
        return False, wait

    def _commit_under_lock(self, requested: int) -> None:
        """Consuma `requested` token. Assume lock acquisito e check già passato."""
        self._tokens -= requested
        logger.debug("[%s] Token locali acquisiti: %d. Disponibili: %.2f/%d",
            self.name, requested, self._tokens, self.capacity,)

    def locked(self) -> asyncio.Lock:
        """Ritorna il lock interno per coordinamento multi-bucket (ADR-0009)."""
        return self._lock

    def remaining(self) -> float:
        """Token residui dopo l'ultimo commit. Solo per introspezione."""
        return self._tokens

class ResilientRateLimiter:
    """Wrapper di composizione per il rate limiting con pattern Circuit Breaker integrato.

    Instrada le richieste primariamente verso il rate limiter distribuito (`primary`).
    Se il primary solleva un errore di infrastruttura (`redis.RedisError`), il circuito passa
    in stato *degraded* (aperto) per un intervallo temporale `cooldown_sec`, dirottando
    trasparentemente tutte le chiamate successive sul componente di `fallback` in-memory.

    Attributes:
        primary (Any): Rate limiter principale centralizzato (es. `RedisTokenBucket`).
        fallback (Any): Rate limiter secondario locale (es. `LocalTokenBucket`).
        cooldown_sec (float): Finestra temporale di degradazione (in secondi) prima di ritentare il primary.
    """

    def __init__(self, primary: Any, fallback: Any, cooldown_sec: float = 5.0):
        """Inizializza il wrapper di resilienza.

        Args:
            primary (Any): Istanza del rate limiter distribuito primario.
            fallback (Any): Istanza del rate limiter locale da usare come fallback.
            cooldown_sec (float, optional): Durata in secondi dello stato di degradazione. Defaults to 5.0.

        Raises:
            ValueError: Se `cooldown_sec` è un valore negativo.
        """
        if cooldown_sec < 0:
            raise ValueError("cooldown_sec non può essere negativo.")
        self.primary = primary
        self.fallback = fallback
        self.cooldown_sec = cooldown_sec

        self._degraded_until: float = 0.0
        #: Timestamp (float) monotonic che indica il limite temporale
        # dello stato di circuito aperto (degraded mode).

    async def acquire(self, *args, **kwargs):
        """Esegue l'acquisizione delle quote dal limiter attivo.

        Sottopone l'operazione al `fallback` se il circuito è aperto (in stato degraded).
        In caso contrario tenta l'acquisizione dal `primary`; qualora questo sollevi
        un `redis.RedisError`, apre il circuito per la durata di `cooldown_sec` e
        ripiega sul `fallback`.

        Args:
            *args (Any): Argomenti posizionali inoltrati al metodo `acquire` sottostante.
            **kwargs (Any): Argomenti chiave/valore inoltrati al metodo `acquire` sottostante.

        Returns:
            Any: Il risultato restituito dall'operazione `acquire` (es. `None` o dizionario di stato).
        """
        if time.monotonic() < self._degraded_until:
            return await self.fallback.acquire(*args, **kwargs)

        try:
            result = await self.primary.acquire(*args, **kwargs)
            if self._degraded_until > 0.0:
                logger.info("Rate limiter: primary recuperato, circuito chiuso.")
                self._degraded_until = 0.0
            return result
        except redis.RedisError as e:
            logger.warning(
                "Rate limiter: primary irraggiungibile, degraded mode per %.1fs. Causa: %s",
                self.cooldown_sec, e,
            )
            self._degraded_until = time.monotonic() + self.cooldown_sec
            return await self.fallback.acquire(*args, **kwargs)

# core/rate_limiter.py — factory functions

def _local_rate(rate: float, safety_factor: float, expected_workers: int) -> float:
    """Calcola il rate per worker applicando un fattore di sicurezza conservativo.

    Args:
        rate (float): Rate nominale complessivo consentito dall'exchange.
        safety_factor (float): Quota percentuale di sicurezza (es. 0.8 per 80%).
        expected_workers (int): Numero totale di processi/worker concorrenti stimati.

    Returns:
        float: Tasso massimo calcolato per la singola istanza locale.
    """
    return rate * safety_factor / expected_workers

def build_resilient_token_bucket(redis_client: redis.Redis, key: str, rate: float, capacity: int,
    *, expected_workers: int, safety_factor: float = 0.8, cooldown_sec: float = 5.0,) -> ResilientRateLimiter:
    """Costruisce un `ResilientRateLimiter` basato su algoritmo Token Bucket.

    Configura un `RedisTokenBucket` come primario e scala automaticamente la quota per il
    `LocalTokenBucket` di fallback in base al numero di worker e al fattore di sicurezza.

    Args:
        redis_client (Any): Istanza del client Redis (asincrona secondo l'SDK).
        key (str): Chiave/identificatore unico della risorsa sottoposta a rate limit.
        rate (float): Tasso teorico globale di rigenerazione token (token/sec).
        capacity (int): Capacità globale del burst (token massimi).
        expected_workers (int): Numero stimato di worker attivi per la ripartizione del fallback.
        safety_factor (float, optional): Margine conservativo per prevenire sforamenti globali
            durante il partizionamento locale. Defaults to 0.8 (80%).
        cooldown_sec (float, optional): Intervallo di isolamento Redis in caso di errori di rete. Defaults to 5.0.

    Returns:
        ResilientRateLimiter: Istanza del rate limiter resiliente pronta all'uso.
    """
    primary = RedisTokenBucket(redis_client, key=key, rate=rate, capacity=capacity)
    fallback = LocalTokenBucket(
        rate=_local_rate(rate, safety_factor, expected_workers),
        capacity=max(1, int(capacity * safety_factor / expected_workers)),
        name=key,
    )
    return ResilientRateLimiter(primary, fallback, cooldown_sec=cooldown_sec)

def build_resilient_sliding_window(redis_client: redis.Redis, key: str, limit: int, window_ms: int,
    *, expected_workers: int, safety_factor: float = 0.8, cooldown_sec: float = 5.0,) -> ResilientRateLimiter:
    """Costruisce un `ResilientRateLimiter` basato su algoritmo Sliding Window Log/Counter.

    Args:
        redis_client (Any): Istanza del client Redis.
        key (str): Chiave/identificatore unico della risorsa.
        limit (int): Numero massimo di richieste consentite nella finestra temporale.
        window_ms (int): Ampiezza della finestra temporale espressa in millisecondi.
        expected_workers (int): Numero stimato di worker attivi per la ripartizione del fallback.
        safety_factor (float, optional): Margine conservativo sul limite locale. Defaults to 0.8.
        cooldown_sec (float, optional): Durata della degraded mode su errore Redis. Defaults to 5.0.

    Returns:
        ResilientRateLimiter: Istanza del rate limiter resiliente configurata con Sliding Window.
    """
    primary = RedisSlidingWindow(redis_client, key=key, limit=limit, window_ms=window_ms)
    fallback = LocalSlidingWindow(capacity=max(1, int(limit * safety_factor / expected_workers)),
        window_sec=window_ms / 1000.0, name=key,)
    return ResilientRateLimiter(primary, fallback, cooldown_sec=cooldown_sec)

def build_resilient_manager(redis_client: redis.Redis, exchange: str, buckets: dict[str, tuple[int, int]],
    *, expected_workers: int, safety_factor: float = 0.8, cooldown_sec: float = 5.0,) -> ResilientRateLimiter:
    """Costruisce un gestore di rate limit multi-bucket (es. peso/endpoint multipli) resiliente.

    Mappa un insieme di regole distribuite su un `RateLimitManager` e genera un componente
    `_LocalManagerFallback` per coordinare i singoli bucket in-memory durante le interruzioni di Redis.

    Args:
        redis_client (Any): Istanza del client Redis.
        exchange (str): Nome o identificatore dell'exchange di destinazione.
        buckets (Dict[str, Tuple[int, int]]): Mappa nel formato `{nome_bucket: (limite, finestra_ms)}`.
        expected_workers (int): Numero totale di worker concorrenti nell'architettura.
        safety_factor (float, optional): Fattore di riduzione quota per i bucket di fallback. Defaults to 0.8.
        cooldown_sec (float, optional): Tempo di ripristino per tentare la riconnessione a Redis. Defaults to 5.0.

    Returns:
        ResilientRateLimiter: Manager composito pronto a gestire acquisizioni arbitrarie multi-bucket.
    """
    primary = RateLimitManager(redis_client, exchange=exchange)
    for name, (limit, window_ms) in buckets.items():
        primary.add_limiter(name, limit=limit, window_ms=window_ms)

    fallback = _LocalManagerFallback(
        buckets=buckets,
        expected_workers=expected_workers,
        safety_factor=safety_factor,
    )
    return ResilientRateLimiter(primary, fallback, cooldown_sec=cooldown_sec)

class _LocalManagerFallback:
    """Coordinatore locale di fallback per RateLimitManager, atomico su multi-bucket.

    Implementa il pattern dry-run/commit (ADR-0009) speculare allo script Lua del
    primary: acquisisce i lock di tutti i bucket in ordine alfabetico, esegue il
    dry-run su ciascuno, e solo se TUTTI concedono la quota esegue il commit.
    In caso contrario rilascia i lock e attende il tempo necessario al bucket più
    vincolante, poi ritenta.

    L'ordine alfabetico dei lock previene deadlock quando chiamate concorrenti
    coinvolgono set di bucket parzialmente sovrapposti (es. {a,b} e {b,c}).
    """

    def __init__(self, buckets: dict[str, tuple[int, int]], expected_workers: int, safety_factor: float) -> None:
        """Inizializza un insieme di `LocalSlidingWindow` derivati dalla mappa dei bucket.
        

        Args:
            buckets (Dict[str, Tuple[int, int]]): Configurazione dei bucket `{nome: (limite, finestra_ms)}`.
            expected_workers (int): Numero stimato di worker per il calcolo della quota locale.
            safety_factor (float): Percentuale di tolleranza e sicurezza applicata ai limiti.
        """
        self._local: dict[str, LocalSlidingWindow] = {}
        #: Mappa (Dict[str, LocalSlidingWindow]) dei limiter locali in-memory
        # associati ai rispettivi nomi.
        
        for name, (limit, window_ms) in buckets.items():
            self._local[name] = LocalSlidingWindow(
                capacity=max(1, int(limit * safety_factor / expected_workers)),
                window_sec=window_ms / 1000.0,
                name=name,
            )

    async def acquire(self, *limiter_names: str, requested: int = 1) -> dict[str, int]:
        """Acquisisce atomicamente la quota da tutti i bucket richiesti.
        Implementa 2-Phase Locking e Dry-Run/Commit a specchio dello script Lua Redis.

        Args:
            *limiter_names (str): Nomi dei limiter su cui verificare e consumare la quota.
            requested (int, optional): Unità di peso da consumare per ciascun limiter. Defaults to 1.

        Returns:
            dict[str, int]: Mappa `{nome: remaining_after_commit}` per ogni limiter.

        Raises:
            KeyError: Se viene richiesto un limiter non registrato nella mappa iniziale dei bucket.
            ValueError: Se `requested` supera la capacità locale di un bucket.
        """
        for name in limiter_names:
            if name not in self._local:
                raise KeyError(f"Bucket '{name}' non registrato nel fallback locale.")
            if requested > self._local[name].capacity:
                raise ValueError(f"requested ({requested}) supera la capacità del bucket '{name}' "
                    f"({self._local[name].capacity}).")

        # Ordine deterministico per evitare deadlock tra set di bucket diversi
        names_sorted = sorted(set(limiter_names))

        while True:

            now = time.monotonic()
            remaining_map: dict[str, int] = {}
            max_wait = 0.001
            can_proceed = True            

            # Acquisizione di TUTTI i lock in ordine, mantenuti fino al commit
            async with AsyncExitStack() as stack:

                for name in names_sorted:
                    await stack.enter_async_context(self._local[name].locked())
            
                # FASE 1: dry-run su tutti i bucket (i lock sono tenuti)
                for name in names_sorted:
                    ok, wait = self._local[name]._check_under_lock(now, requested)
                    if not ok:
                        can_proceed = False
                        if wait > max_wait:
                            max_wait = wait

                # FASE 2: commit solo se TUTTI hanno concesso (ancora sotto lock)
                if can_proceed:
                    for name in names_sorted:
                        self._local[name]._commit_under_lock(now, requested)
                        remaining_map[name] = self._local[name].remaining()
                    logger.debug("Fallback SlidingWindowManager atomico success: %s", remaining_map,)
                    return remaining_map

            # Lock rilasciati dall'AsyncExitStack prima dello sleep
            logger.debug("Fallback multi-bucket limit hit. Attesa %.3fs prima del retry.", max_wait)
            await asyncio.sleep(max_wait)



def build_resilient_token_bucket_manager(redis_client: redis.Redis, exchange: str, buckets: dict[str, tuple[float, int]],   # {name: (rate, capacity)}
    *, expected_workers: int, safety_factor: float = 0.8, cooldown_sec: float = 5.0,) -> ResilientRateLimiter:
    """Costruisce un `ResilientRateLimiter` basato su un gestore multi-bucket Token Bucket.

    Mappa un insieme di regole di rate limiting (espresse come tasso e capacità) su un
    `TokenBucketManager` primario e genera un coordinatore di fallback locale
    `_LocalTokenBucketManagerFallback` con quote ricalcolate per l'esecuzione in degrado.

    Args:
        redis_client (redis.Redis): Istanza del client Redis per la gestione distribuita.
        exchange (str): Nome o identificatore logico dell'exchange target.
        buckets (dict[str, tuple[float, int]]): Mappa della configurazione dei bucket
            nel formato `{nome_bucket: (rate, capacity)}`.
        expected_workers (int): Numero totale di worker concorrenti previsti nell'architettura.
        safety_factor (float, optional): Fattore conservativo per la ripartizione locale delle
            quote (es. 0.8 per l'80%). Defaults to 0.8.
        cooldown_sec (float, optional): Finestra temporale di isolamento Redis (in secondi)
            in caso di errore di rete. Defaults to 5.0.

    Returns:
        ResilientRateLimiter: Wrapper di resilienza pronto a gestire acquisizioni
        concorrenti multi-bucket Token Bucket.
    """
    primary = TokenBucketManager(redis_client, exchange=exchange)
    for name, (rate, capacity) in buckets.items():
        primary.add_bucket(name, rate=rate, capacity=capacity)

    fallback = _LocalTokenBucketManagerFallback(
        buckets=buckets,
        expected_workers=expected_workers,
        safety_factor=safety_factor,
    )
    return ResilientRateLimiter(primary, fallback, cooldown_sec=cooldown_sec)


class _LocalTokenBucketManagerFallback:
    """Coordinatore locale di fallback per TokenBucketManager, atomico su multi-bucket.

    Pattern dry-run/commit (ADR-0009) speculare a `TokenBucketManager`: acquisisce
    i lock di tutti i bucket in ordine alfabetico, esegue il dry-run su ciascuno,
    e solo se TUTTI concedono la quota esegue il commit. In caso contrario rilascia
    i lock, attende il tempo necessario al bucket più vincolante, e ritenta.

    """

    def __init__(self,buckets: dict[str, tuple[float, int]], expected_workers: int,
        safety_factor: float,):
        """Inizializza le istanze di `LocalTokenBucket` ridimensionando tassi e capacità.

        Args:
            buckets (dict[str, tuple[float, int]]): Configurazione dei bucket `{nome: (rate, capacity)}`.
            expected_workers (int): Numero stimato di worker attivi per la ripartizione del carico.
            safety_factor (float): Margine conservativo applicato a tassi e capacità locali.
        """
        self._local: dict[str, LocalTokenBucket] = {}
        #: Mappa (dict[str, LocalTokenBucket]) dei bucket locali
        # associati ai rispettivi nomi.
        
        for name, (rate, capacity) in buckets.items():
            self._local[name] = LocalTokenBucket(
                rate=_local_rate(rate, safety_factor, expected_workers),
                capacity=max(1, int(capacity * safety_factor / expected_workers)),
                name=name,
            )

    async def acquire(self, *bucket_names: str, requested: int = 1) -> dict[str, float]:
        """Acquisisce atomicamente la quota da tutti i bucket richiesti.
        Implementa 2-Phase Locking e Dry-Run/Commit a specchio dello script Lua Redis.

        Args:
            *bucket_names (str): Nomi dei bucket da cui consumare i token.
            requested (int, optional): Numero di token da acquisire per ciascun bucket. Defaults to 1.

        Returns:
            dict[str, float]: Mappa `{nome: remaining_after_commit}` per ogni bucket.

        Raises:
            KeyError: Se un bucket non è registrato.
            ValueError: Se `requested` supera la capacità locale di un bucket.

        """
        for name in bucket_names:
            if name not in self._local:
                raise KeyError(f"Bucket '{name}' non registrato nel fallback locale.")
            if requested > self._local[name].capacity:
                raise ValueError(
                    f"requested ({requested}) supera la capacità locale del bucket "
                    f"'{name}' ({self._local[name].capacity})."
                )

        names_sorted = sorted(set(bucket_names))

        while True:
            now = time.monotonic()
            remaining_map: dict[str, float] = {}
            max_wait = 0.001
            can_proceed = True

            async with AsyncExitStack() as stack:
                for name in names_sorted:
                    await stack.enter_async_context(self._local[name].locked())

                # FASE 1: dry-run (con refill idempotente)
                for name in names_sorted:
                    ok, wait = self._local[name]._check_under_lock(now, requested)
                    if not ok:
                        can_proceed = False
                        if wait > max_wait:
                            max_wait = wait

                # FASE 2: commit
                if can_proceed:
                    for name in names_sorted:
                        self._local[name]._commit_under_lock(requested)
                        remaining_map[name] = self._local[name].remaining()
                    logger.debug("Fallback TokenBucketManager atomico success: %s", remaining_map,)
                    return remaining_map
            
            logger.debug("Fallback TokenBucket limit hit. Attesa %.3fs.", max_wait)
            await asyncio.sleep(max_wait)