from abc import ABC, abstractmethod
import logging
import asyncio
import random, os
import redis.asyncio as redis
from redis.exceptions import ConnectionError, ResponseError
from core.utils import timeframe_to_ms, retry_async
from core.rate_limiter import RedisTokenBucket

logger = logging.getLogger(__name__)

class BaseIngestionWorker(ABC):
    """Classe base astratta per l'ingestione dei dati di mercato (Ingestion Engine).

        Questa classe implementa il ciclo di vita generico di un worker di ingestione
        seguendo lo Strategy Pattern. Gestisce in autonomia la connessione a Redis,
        la fase di riscaldamento storico (Warm-up) e la resilienza del flusso live.
        I broker specifici devono ereditare da questa classe e implementare i metodi
        astratti per l'interazione con le proprie API.

        Attributes:
            symbol (str): Il simbolo di mercato da tracciare (es. "BTCUSDT").
            timeframe (str): Il time-frame delle candele (es. "15m", "1h").
            market_type (str): Il tipo di mercato ("spot", "futures").
            tf_ms (int): Durata del timeframe precomputata in millisecondi.
            redis_stream (str): Il nome dello stream Redis per le candele storicizzate (`stream:market:type:symbol:tf`).
            redis_live_key (str): La chiave Hash Redis per il tracciamento della candela in corso (`live:market:type:symbol:tf`).
            redis_host (str): Indirizzo host del server Redis.
            redis_port (int): Porta di ascolto del server Redis.
            redis_db (int): Numero del database Redis usato.
            redis (redis.Redis | None): Istanza del client Redis asincrono, inizializzata in `run()`.
            running (bool): Flag di controllo per il ciclo di vita del WebSocket live.
            limiter (RedisTokenBucket | None): Istanza del rate limiter per le chiamate REST.
            last_saved_timestamp (int): Timestamp in ms dell'ultima candela chiusa salvata in memoria per la riconciliazione rapida.
    """

    def __init__(self, symbol: str, timeframe: str, market_type: str = "spot", redis_host: str = "localhost", redis_port: int = 6379, redis_db = 0):
        """Inizializza i parametri di configurazione del worker di ingestione e lo stato di riconciliazione.
        Args:
            symbol (str): Il simbolo di mercato da tracciare (es. "BTCUSDT").
            timeframe (str): Il time-frame delle candele (es. "15m", "1h").
            market_type (str, optional): Il tipo di mercato ("spot", "futures"). Defaults to "spot".
            redis_host (str, optional): Indirizzo host del server Redis. Defaults to "localhost".
            redis_port (int, optional): Porta di ascolto del server Redis. Defaults to 6379.
            redis_db (int, optional): Numero del database Redis usato. Defaults to 0.
        """
        self.symbol: str = symbol.upper()
        self.timeframe: str = timeframe

        self.tf_ms: int = timeframe_to_ms(timeframe) # Calcolo precomputato per verifiche
        self.market_type: str = market_type.lower()  # "spot", "futures", "linear", ecc.

        self.redis_stream: str = f"stream:market:{self.market_type}:{self.symbol}:{self.timeframe}"
        # Nuova chiave per candele live
        self.redis_live_key: str = f"live:market:{self.market_type}:{self.symbol}:{self.timeframe}"

        self.redis_host: str = redis_host
        self.redis_port: int = redis_port
        self.redis_db: int = redis_db
        self.redis: redis.Redis | None = None  
        self.running: bool = True
        self.limiter: RedisTokenBucket | None = None  # verrà creato in run()

        # Stato in memoria vitale per la riconciliazione rapida
        self.last_saved_timestamp: int = 0

    @abstractmethod
    async def fetch_historic_klines(self, since_timestamp: int | None = None, limit: int = 200) -> list[dict]:
        """Scarica le candele storiche (Kline/Candlestick) tramite chiamate REST.

        Questo metodo deve essere implementato dal connettore specifico del broker.
        Viene invocato durante la fase di warm-up e per il riempimento dei gap (gap filling).

        Args:
            since_timestamp (int | None, optional): Timestamp Unix in ms da cui iniziare lo scarico.
            limit (int): Il numero massimo di candele storiche da recuperare.

        Returns:
            list[dict]: Una lista di dizionari, dove ogni dizionario rappresenta una candela
                normalizzata con la seguente struttura tassativa::

                    {
                        "timestamp": int,  # Millisecondi Unix UTC
                        "open": float | str,
                        "high": float | str,
                        "low": float | str,
                        "close": float | str,
                        "volume": float | str,
                        "is_closed": bool  # True per candele storiche, False se la candela sta ancora formandosi
                    }
        """
        pass

    @abstractmethod
    async def listen_websocket_stream(self):
        """Gestisce la connessione persistente al WebSocket live del broker.

        Questo metodo deve implementare il loop di ascolto del feed WebSocket specifico.
        Deve occuparsi della sottoscrizione ai canali corretti, della ricezione dei messaggi,
        della loro normalizzazione e dell'invio finale a Redis tramite `push_to_redis()`.

        Raises:
            Exception: Qualsiasi errore di rete o disconnessione del socket deve essere
                sollevato per permettere alla classe base di gestire la riconnessione.
        """
        pass

    async def _get_last_redis_timestamp(self) -> int | None:
        """Legge l'ultima candela presente nello stream per evitare duplicati o buchi."""
        try:
            # XREVRANGE con count=1 prende l'ultimo elemento inserito
            last_entry = await self.redis.xrevrange(self.redis_stream, max='+', min='-', count=1) # type: ignore
            if last_entry:
                # Struttura di ritorno: [(message_id, {fields})]
                fields = last_entry[0][1]
                return int(fields.get("timestamp", 0))
            return None
        except Exception as e:
            logger.warning("⚠️ Impossibile leggere lo stato precedente da Redis: %s", e, exc_info=True)
            return None

    async def push_to_redis(self, standardized_kline: dict) -> int:
        """Inietta una candela normalizzata e CHIUSA all'interno dello stream Redis dedicato.

        Forza l'ID del messaggio su Redis usando il timestamp della candela (formato `{timestamp}-0`).
        Questo garantisce a livello di database l'ordinamento cronologico rigoroso ed evita
        la scrittura di candele duplicate o fuori ordine.
        
        Esegue il cast preventivo a stringa di tutti i valori numerici per garantire
        la compatibilità nativa con i Redis Streams ed evita sovrascritture incontrollate
        grazie alla separazione degli stream per simbolo.

        Args:
            standardized_kline (dict): Dizionario della candela normalizzata da salvare nello stream.
                (timestamp, open, high, low, close, volume, is_closed).
        
        Raises:
            ConnectionError: Se la connessione Redis non è stata inizializzata.
            redis.exceptions.ResponseError: Se si verifica un errore Redis diverso dal rifiuto
                di ID non progressivi/duplicati.
        """
        if not self.redis:
            raise ConnectionError("Connessione Redis non inizializzata.")
        
        ts_str = str(standardized_kline["timestamp"])
        try:
            # FORZATURA ID: usiamo il timestamp come ID del messaggio (es. "1690000000000-0")
            # Questo previene NATIVAMENTE inserimenti doppi o fuori ordine.
            await self.redis.xadd(
                    name=self.redis_stream,
                    fields={
                    "symbol": self.symbol,
                    "timestamp": ts_str,
                    "open": str(standardized_kline["open"]),
                    "high": str(standardized_kline["high"]),
                    "low": str(standardized_kline["low"]),
                    "close": str(standardized_kline["close"]),
                    "volume": str(standardized_kline["volume"]),
                    # is_closed NON viene passato a Redis
                },
                id=f"{ts_str}-0",
                maxlen=1000,
                approximate=True
                )
        except ResponseError as e:
            # Se un altro processo o un disallineamento tenta di inserire un ID vecchio/uguale, Redis lo rigetta
            if "ID specified is equal or smaller" in str(e) or "ID specified in XADD is equal or smaller" in str(e):
                logger.debug("Duplicato/Out of order bloccato nativamente da Redis per TS: %s", ts_str)
                return 0
            else:
                raise
        else:
            logger.debug("Candela salvata nello stream Redis: %s", ts_str)
            self.last_saved_timestamp = int(ts_str)  # Aggiorniamo lo stato interno per la riconciliazione rapida
            return 1

    async def process_live_candle(self, kline: dict[str, str]):
        """Orchestra la riconciliazione in tempo reale dei dati provenienti dal WebSocket.

        Filtra le candele non ancora chiuse indirizzandole su una chiave Hash volatile (`live:market:...`),
        scarta duplicati e messaggi arrivati fuori ordine basandosi sullo stato interno `last_saved_timestamp`,
        e attiva automaticamente il riempimento dei buchi temporali (`_fill_gap`) se rileva un salto cronologico.

        Args:
            kline (dict[str, str]): La candela normalizzata ricevuta dal flusso live WebSocket.
        """
        ts = int(kline["timestamp"])
        is_closed = kline.get("is_closed", False)

        # --- PROBLEMA 4: Candele parziali/in corso ---
        if not is_closed:
            # Upsert su un Hash Redis: i dati live vengono sovrascritti continuamente
            # Utile per monitorare il prezzo in tempo reale, ma non sporca lo storico!
            if self.redis:
                await self.redis.hset(self.redis_live_key, mapping={k: str(v) for k, v in kline.items()})
            return

        # Da qui in poi elaboriamo solo candele storicizzate (CHIUSE)
        
        if self.last_saved_timestamp == 0:
            self.last_saved_timestamp = await self._get_last_redis_timestamp() or 0

        # --- PROBLEMI 1, 2 e 5: Duplicati (Handoff & Riconnessioni) e Arrivi fuori ordine ---
        if ts <= self.last_saved_timestamp:
            logger.debug("Candela scartata: %s (<= %s)", ts, self.last_saved_timestamp)
            return

        # --- PROBLEMA 3: Buchi temporali (Gap) ---
        # Se la distanza tra l'ultima candela e questa supera un timeframe, c'è un buco.
        if self.last_saved_timestamp > 0 and (ts - self.last_saved_timestamp) > self.tf_ms:
            logger.warning("Gap temporale rilevato! Da %s a %s. Avvio mini-warmup...", self.last_saved_timestamp, ts)
            # Sospendiamo un attimo il live processing e andiamo a scaricare i dati mancanti
            await self._fill_gap(from_ts=self.last_saved_timestamp, to_ts=ts)

        # Inserimento sicuro nello stream
        await self.push_to_redis(kline)

    async def _fill_gap(self, from_ts: int, to_ts: int | None = None, limit: int = 200) -> int:
        """Scarica e salva tutte le candele mancanti tra from_ts e to_ts (se dato, altrimenti fino ad ora).

        Paginazione, rate limiting e retry automatici inclusi.
        `push_to_redis` aggiorna `self.last_saved_timestamp` in caso di successo.

        Args:
            from_ts (int): Timestamp (ms) dell'ultima candela salvata con successo.
            to_ts (int | None): Timestamp (ms) fino al quale riempire il gap. Se None, va fino ad ora.
            limit (int): Numero massimo di candele per singola chiamata REST.

        Returns:
            int: Numero di candele effettivamente aggiunte allo stream Redis.
        """
        current_start = from_ts
        total_added = 0
        max_iterations = 100  # salvaguardia contro loop infiniti

        for _ in range(max_iterations):
            # Rispetta il rate limit prima di ogni chiamata REST
            if self.limiter:
                await self.limiter.acquire(requested=1)

            # Esegue la chiamata REST con retry automatico su errori temporanei
            try:
                data = await retry_async(
                    lambda: self.fetch_historic_klines(since_timestamp=current_start, limit=limit),
                    max_retries=5,
                    base_delay=1.33,
                    exceptions=(asyncio.TimeoutError, ConnectionError, OSError)
                )
            except Exception as e:
                logger.error("Errore definitivo durante il fetch storico: %s", e, exc_info=True)
                break

            if not data:
                break  # exchange non ha più dati

            # Garantiamo ordine cronologico crescente
            data.sort(key=lambda x: int(x["timestamp"]))

            for kline in data:
                k_ts = int(kline["timestamp"])

                # Se è specificato un limite superiore, fermati quando lo raggiungi
                if to_ts is not None and k_ts >= to_ts:
                    return total_added  # gap colmato fino alla candela live

                # Evita candele non chiuse o con timestamp già coperto
                if k_ts <= from_ts or not kline.get("is_closed", True):
                    continue

                # Salva la candela; push_to_redis ritorna 1 se inserita, 0 se scartata
                total_added += await self.push_to_redis(kline)

            # Prepara la prossima pagina
            last_ts = int(data[-1]["timestamp"])
            if to_ts is not None and last_ts >= to_ts:
                break
            if len(data) < limit:
                break  # ultima pagina
            current_start = last_ts + 1
        else:
            logger.warning("Raggiunto il numero massimo di iterazioni (%d) durante il fill gap.", max_iterations)

        return total_added
    
    async def _warmup_storico(self):
        """Esegue la fase di riscaldamento iniziale scaricando i dati passati.

        Metodo interno privato. Recupera lo storico recente tramite le API REST del broker
        e lo spinge su Redis prima dell'attivazione del flusso in tempo reale. In caso di fallimento,
        permette l'avvio degradato basato solo sul flusso live.
        """
        logger.info("🔥 [WARM-UP] Verifica stato per %s (%s)...", self.symbol, self.timeframe)
        try:
            last_ts = await self._get_last_redis_timestamp()

            if last_ts:
                logger.info("🔄 Ultimo dato in DB: %s. Scarico il gap mancante...", last_ts)
                added = await self._fill_gap(from_ts=last_ts, to_ts=None)
            else:
                logger.info("🆕 Nessun dato pregresso. Scarico storico base...")

                # Se non c'è storico, partiamo da zero: scarichiamo solo le ultime N candele
                if self.limiter: await self.limiter.acquire(requested=1)
                data = await retry_async(
                    lambda: self.fetch_historic_klines(limit=200),
                    max_retries=5,
                    base_delay=1.5,
                    exceptions=(asyncio.TimeoutError, ConnectionError, OSError)
                )

                # Garantiamo ordine cronologico crescente
                data.sort(key=lambda x: int(x["timestamp"]))

                added = 0
                for kline in data:
                    if await self.push_to_redis(kline):
                        added += 1
            logger.info("✅ [WARM-UP] Sincronizzazione completata: %d nuove candele.", added)
        except Exception as e:
            logger.warning("⚠️ Errore durante il warm-up storico: %s", e, exc_info=True)

    async def run(self):
        """Orchestra l'intero ciclo di vita del processo di ingestione.

        Inizializza la connessione asincrona a Redis all'interno dell'event loop corrente,
        avvia la routine di warm-up e mantiene attivo il loop del WebSocket live.
        Include la gestione della backpressure latente, la logica di riconnessione automatica
        in caso di disconnessione del broker e garantisce un Graceful Shutdown strutturato.
        """

        self.redis = redis.Redis(host=self.redis_host, port=self.redis_port, db=self.redis_db, decode_responses=True)
        
        # Configura il rate limiter per le API REST di Bitget
        rate = float(os.getenv("BITGET_RATE_LIMIT", "19"))          # token al secondo
        capacity = int(os.getenv("BITGET_RATE_LIMIT_BURST", "20"))   # burst massimo
        limiter_key = os.getenv("BITGET_RATE_LIMIT_KEY", "bitget_rest")  # chiave condivisa da tutti i worker
        self.limiter = RedisTokenBucket(
            redis_client=self.redis,
            key=limiter_key,
            rate=rate,
            capacity=capacity
        )
        
        backoff = 1  # secondi iniziali
        max_backoff = 150  # massimo 150s
        try:
            await self._warmup_storico()
            
            logger.info(f"⚡ [LIVE] Avvio ascolto WebSocket in tempo reale...")
            while self.running:
                try:
                    await self.listen_websocket_stream()
                    # Se il listener termina senza errori (caso raro), resetta il backoff
                    backoff = 1
                    await asyncio.sleep(1)

                except asyncio.CancelledError:
                    logger.info("🛑 [INTERRUPT] Segnale di stop ricevuto. Uscita...")
                    self.running = False
                    break
                except Exception as e:
                    # Calcola attesa con jitter (es. backoff + random 0-30% del backoff)
                    jitter = random.uniform(0, 0.3 * backoff)
                    wait_time = backoff + jitter
                    logger.error("💥 Connessione interrotta: %s. Riconnessione tra %.2fs...", e, wait_time, exc_info=True)
                    await asyncio.sleep(wait_time)
                    # Raddoppia il backoff fino al massimo
                    backoff = min(backoff * 2, max_backoff)
        finally:
            logger.info("🛑 [SHUTDOWN] Chiusura connessione Redis...")
            if self.redis:
                await self.redis.close()