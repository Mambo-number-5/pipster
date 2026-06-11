from abc import ABC, abstractmethod
import asyncio
import redis.asyncio as redis

class BaseIngestionWorker(ABC):
    def __init__(self, symbol: str, timeframe: str, redis_host: str = "localhost", redis_port: int = 6379):
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.redis_stream = f"stream:market:{self.symbol}:{self.timeframe}"
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis = None  
        self.running = True

    @abstractmethod
    async def fetch_historic_klines(self, since_timestamp: int | None = None, limit: int = 200) -> list[dict]:
        """
        Scarica le candele storiche. 
        Se since_timestamp è fornito, deve scaricare i dati a partire da quel momento.
        """
        pass

    @abstractmethod
    async def listen_websocket_stream(self):
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

        self.redis = redis.Redis(host=self.redis_host, port=self.redis_port, decode_responses=True)
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