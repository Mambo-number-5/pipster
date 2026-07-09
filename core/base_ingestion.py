from abc import ABC, abstractmethod
import asyncio
import redis.asyncio as redis

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
            redis_stream (str): Il nome univoco dello stream Redis calcolato per simbolo e timeframe.
            redis_host (str): Indirizzo host del server Redis.
            redis_port (int): Porta di ascolto del server Redis.
            readus_db (int): Numero du database redus usato
            redis (redis.Redis | None): Istanza del client Redis asincrono, inizializzata in `run()`.
            running (bool): Flag di controllo per il ciclo di vita del WebSocket live.
    """

    def __init__(self, symbol: str, timeframe: str, redis_host: str = "localhost", redis_port: int = 6379, redis_db = 0):
        """Inizializza i parametri di configurazione del worker di ingestione."""
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.redis_stream = f"stream:market:{self.symbol}:{self.timeframe}"
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis_db = redis_db
        self.redis = None  
        self.running = True

    @abstractmethod
    async def fetch_historic_klines(self, since_timestamp: int | None = None, limit: int = 200) -> list[dict]:
        """Scarica le candele storiche (Kline/Candlestick) tramite chiamate REST.

        Questo metodo deve essere implementato dal connettore specifico del broker.
        Viene invocato durante la fase di warm-up per garantire la continuità degli indicatori.

        Args:
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
                        "volume": float | str
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
            print(f"⚠️ Impossibile leggere lo stato precedente da Redis: {e}")
            return None

    async def push_to_redis(self, standardized_kline: dict):
        """Inietta una candela normalizzata all'interno dello stream Redis dedicato.

        Esegue il cast preventivo a stringa di tutti i valori numerici per garantire
        la compatibilità nativa con i Redis Streams ed evita sovrascritture incontrollate
        grazie alla separazione degli stream per simbolo.

        Args:
            standardized_kline (dict): Dizionario contenente i dati strutturati della candela
                (timestamp, open, high, low, close, volume).
        """
        if not self.redis:
            raise ConnectionError("Connessione Redis non inizializzata.")
        
        # Niente try/except qui. Se Redis crasha, vogliamo che l'errore propaghi 
        # verso l'alto per chiudere il WebSocket e tentare un riavvio pulito.
        await self.redis.xadd(
            name=self.redis_stream,
            fields={
                "symbol": self.symbol,
                "timestamp": str(standardized_kline["timestamp"]),
                "open": str(standardized_kline["open"]),
                "high": str(standardized_kline["high"]),
                "low": str(standardized_kline["low"]),
                "close": str(standardized_kline["close"]),
                "volume": str(standardized_kline["volume"])
            },
            maxlen=1000,
            approximate=True
        )

    async def _warmup_storico(self):
        """Esegue la fase di riscaldamento iniziale scaricando i dati passati.

        Metodo interno privato. Recupera lo storico recente tramite le API REST del broker
        e lo spinge su Redis prima dell'attivazione del flusso in tempo reale. In caso di fallimento,
        permette l'avvio degradato basato solo sul flusso live.
        """
        print(f"🔥 [WARM-UP] Verifica stato per {self.symbol} ({self.timeframe})...")
        try:
            last_ts = await self._get_last_redis_timestamp()
            
            if last_ts:
                print(f"🔄 Ultimo dato in DB: {last_ts}. Scarico il gap mancante...")
                historical_data = await self.fetch_historic_klines(since_timestamp=last_ts)
            else:
                print(f"🆕 Nessun dato pregresso. Scarico storico base...")
                historical_data = await self.fetch_historic_klines(limit=200)

            # Filtriamo eventuali candele già presenti (stesso o vecchio timestamp)
            new_candles = 0
            for kline in historical_data:
                if not last_ts or int(kline["timestamp"]) > last_ts:
                    await self.push_to_redis(kline)
                    new_candles += 1
                    
            print(f"✅ [WARM-UP] Sincronizzazione completata: {new_candles} nuove candele aggiunte.")
            
        except Exception as e:
            print(f"⚠️ Errore durante il warm-up storico: {e}")

    async def run(self):
        """Orchestra l'intero ciclo di vita del processo di ingestione.

        Inizializza la connessione asincrona a Redis all'interno dell'event loop corrente,
        avvia la routine di warm-up e mantiene attivo il loop del WebSocket live.
        Include la gestione della backpressure latente, la logica di riconnessione automatica
        in caso di disconnessione del broker e garantisce un Graceful Shutdown strutturato.
        """

        self.redis = redis.Redis(host=self.redis_host, port=self.redis_port, db=self.redis_db, decode_responses=True)
        try:
            await self._warmup_storico()
            
            print(f"⚡ [LIVE] Avvio ascolto WebSocket in tempo reale...")
            while self.running:
                try:
                    await self.listen_websocket_stream()
                    await asyncio.sleep(1) 
                except asyncio.CancelledError:
                    print("🛑 [INTERRUPT] Segnale di stop ricevuto. Uscita...")
                    self.running = False
                    break
                except Exception as e:
                    # ORA anche gli errori di Redis finiranno qui!
                    print(f"💥 Connessione interrotta (WS o Redis): {e}. Riconnessione tra 5s...")
                    await asyncio.sleep(5)
        finally:
            print("🛑 [SHUTDOWN] Chiusura connessione Redis...")
            if self.redis:
                await self.redis.close()