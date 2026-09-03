import os
import sys
import json
import asyncio
import threading
import logging
from typing import cast
from core.base_ingestion import BaseIngestionWorker
from core.models import Candle
from bitget.ws.bitget_ws_client import BitgetWsClient, SubscribeReq


logger = logging.getLogger(__name__)

class BitgetIngestionWorker(BaseIngestionWorker):
    """Worker di ingestione specifico per l'exchange Bitget.

    Estende `BaseIngestionWorker` implementando i connettori concreti per le API REST
    e il feed WebSocket di Bitget. Si occupa di tradurre il formato dati nativo di Bitget
    nel DTO centralizzato `Candle`, gestendo in thread separati le callback bloccanti
    dell'SDK ufficiale per non interferire con l'event loop asincrono principale.
    """

    def __init__(self, symbol: str, timeframe: str, inst_type: str = "SPOT"):
        """Inizializza il worker specifico per l'exchange Bitget.

        Recupera le credenziali crittografiche dall'ambiente e inizializza i client 
        REST (MarketApi) e le strutture di sincronizzazione asincrona necessarie 
        per la gestione della concorrenza multithread tra WebSocket SDK e asyncio.

        Args:
            symbol (str): Il ticker di mercato (es. "BTCUSDT").
            timeframe (str): Il time-frame operativo delle candele (es. "15m").
            inst_type (str, optional): Tipologia di strumento di Bitget. 
                Può essere "SPOT" o "USDT-FUTURES". Defaults to "SPOT".
        """
        # 1. Recupera l'host dall'ambiente. 
        # Se non c'è, usa 'localhost' per quando usi il debugger fuori da Docker
        host = os.getenv("REDIS_HOST", "localhost")
        port = int(os.getenv("REDIS_PORT", 6379))
        redis_db = int(os.getenv("REDIS_DB", 0))
        super().__init__(symbol=symbol, timeframe=timeframe, redis_host=host, redis_port=port, redis_db=redis_db)
        
        self.inst_type = inst_type
        self.api_key = os.getenv("BITGET_API_KEY", "")
        self.api_secret = os.getenv("BITGET_API_SECRET", "")
        self.passphrase = os.getenv("BITGET_PASSPHRASE", "")
        
        if self.inst_type == "SPOT":
            from bitget.v2.spot.market_api import MarketApi as SpotMarketApi
            self.market_client = SpotMarketApi(self.api_key, self.api_secret, self.passphrase)
        else:
            from bitget.v2.mix.market_api import MarketApi as MixMarketApi
            self.market_client = MixMarketApi(self.api_key, self.api_secret, self.passphrase)
        
        self.ws_queue = asyncio.Queue()
        # UTILIZZO DI THREADING EVENT per thread-safety tra il WS Daemon e l'Event Loop
        self._ws_stopping = threading.Event()
        self.loop = None
        self._live_candle: Candle | None = None  # Cache dell'ultimo tick ricevuto
        self.last_server_ts: int = 0       # ultimo timestamp del server visto nei messaggi WS

    async def sync_time(self) -> None:
        """Implementazione Bitget per calcolare il Time Offset."""
        # import time
        # try:
        #     # Esempio di pseudo-codice usando ccxt o requests verso l'API v2 di Bitget
        #     # L'endpoint pubblico di Bitget per il tempo è solitamente /api/v2/public/time
        #     response = await self.rest_client.fetch_time() # o equivalente
            
        #     # Timestamp fornito da Bitget (in millisecondi)
        #     server_time_ms = int(response['data']['serverTime']) 
            
        #     # Timestamp locale esatto nel momento in cui riceviamo la risposta
        #     local_time_ms = int(time.time() * 1000)
            
        #     # Calcola l'offset
        #     self.time_offset_ms = server_time_ms - local_time_ms
        #     logger.info("🕒 Sincronizzazione tempo completata. Offset: %s ms", self.time_offset_ms)
            
        # except Exception as e:
        #     logger.error("Impossibile sincronizzare il tempo con Bitget: %s", e)
        #     # Se fallisce, l'offset rimane 0 (fallback all'orologio di sistema)
        pass

    def _get_bitget_timeframes(self) -> tuple[str, str]:
        """Unifica e mappa i time-frame per REST API e WebSocket.
        
        Bitget usa due standard di formattazione:
        - REST SPOT: 1min, 5min, 1h, 1day...
        - REST MIX & WebSocket: 1m, 5m, 1H, 1D...

        Returns:
            tuple[str, str]: Una tupla contenente (rest_granularity, ws_channel_name).
        """
        tf_lower = self.timeframe.lower()
        
        # Mappa per WebSocket (e REST MIX)
        standard_map = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1H", "2h": "2H", "4h": "4H", "12h": "12H", "1d": "1D",
        }
        
        # Mappa specifica solo per REST SPOT
        spot_rest_map = {
            "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
            "1h": "1h", "4h": "4h", "12h": "12h", "1d": "1day",
        }

        ws_granularity = standard_map.get(tf_lower, self.timeframe)
        rest_granularity = spot_rest_map.get(tf_lower, self.timeframe) if self.inst_type == "SPOT" else ws_granularity
        
        return rest_granularity, f"candle{ws_granularity}"

    # ------------------------ REST (WARM-UP) ------------------------
    def _sync_fetch_klines(self, since_timestamp: int | None, limit: int) -> dict[str, str]:
        """Esegue la chiamata REST sincrona all'SDK ufficiale Bitget per recuperare lo storico.

        Questo metodo viene delegato a un thread separato tramite `asyncio.to_thread` 
        per evitare di bloccare l'event loop asincrono principale durante le richieste I/O.

        Args:
            since_timestamp (int | None): Timestamp (ms) da cui iniziare il recupero.
            limit (int): Numero massimo di candele da scaricare.

        Returns:
            dict: Risposta JSON grezza restituita dall'API Bitget.
        """
        rest_granularity, _ = self._get_bitget_timeframes()
        start_time = str(since_timestamp) if since_timestamp else ""
        end_time = ""
        
        parameters = {
            "symbol": self.symbol,
            "granularity": rest_granularity,
            "startTime": start_time,
            "endTime": end_time,
            "limit": str(limit)
        }
        if self.inst_type != "SPOT":
            parameters["productType"] = self.inst_type
            
        logger.debug("DEBUG - parametri REST: %s", parameters)
        return cast(dict, self.market_client.candles(params=parameters))

    async def fetch_historic_klines(self, since_timestamp: int | None = None, limit: int = 200) -> list[Candle]:
        """Scarica, convalida e normalizza le candele storiche da Bitget tramite REST.

        Gestisce la richiesta in un thread dedicato, applica un timeout di sicurezza, 
        valida la risposta ed istanzia gli oggetti DTO `Candle`.

        Args:
            since_timestamp (int | None, optional): Timestamp (ms) da cui iniziare.
            limit (int, optional): Numero di candele da scaricare. Defaults to 200.

        Returns:
            list[Candle]: Lista di candele normalizzate e ordinate cronologicamente.
        """
        # MODIFICA: timeout di 30 secondi per la chiamata sincrona in thread
        response = await asyncio.wait_for(
            asyncio.to_thread(self._sync_fetch_klines, since_timestamp, limit), timeout=30)
        
        standardized_data: list[Candle] = []
        # MODIFICA: verifica esplicita del codice di successo
        if isinstance(response, dict) and response.get("code") == "00000" and "data" in response:
            quote_index = 7 if self.inst_type == "SPOT" else 6  # Bitget REST SPOT ha un campo in più per il volume quote
            for kline in response["data"]:
                standardized_data.append(
                    Candle(timestamp=int(kline[0]),
                    open=float(kline[1]),
                    high=float(kline[2]),
                    low=float(kline[3]),
                    close=float(kline[4]),
                    volume_base=float(kline[5]),
                    volume_quote=float(kline[quote_index]),
                    is_closed=True  # Bitget REST fornisce solo candele storicizzate (chiuse)
                ))
            # Sicurezza extra: il sort è ora gestito anche dalla classe base, 
            # ma farlo qui male non fa (costo computazionale quasi nullo grazie al Timsort)
            standardized_data.sort(key=lambda x: x.timestamp)
        else:
            logger.warning("⚠️ Risposta API inattesa o errore: %s", response.get('msg', response))
        
        return standardized_data

    # ------------------------ WEBSOCKET (LIVE) ------------------------
    def _ws_callback(self, message: str) -> None:
        """Callback sincrona invocata dal thread daemon interno dell'SDK Bitget alla ricezione di un messaggio.

        Inserisce il messaggio grezzo all'interno della coda asincrona in modo thread-safe,
        permettendo all'event loop di asyncio di elaborarlo nel proprio contesto.

        Args:
            message (dict/str): Il payload JSON grezzo inviato dal WebSocket di Bitget.
        """
        # 1. Thread safety garantita dall'Event
        if self._ws_stopping.is_set():
            return

        logger.debug("🔴 [WS RAW RECEIVE]: %s", message)
        # 2. Controllo che il loop esista e NON sia chiuso
        if message and "data" in message and self.loop and not self.loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self.ws_queue.put(message), self.loop)
            except RuntimeError:
                # Può succedere se il loop si sta chiudendo esattamente in questo microsecondo
                pass

    def _ws_error_callback(self, error):
        """Intercetta gli errori interni sollevati dal thread del WebSocket di Bitget.

        Args:
            error (Exception/str): L'eccezione o il messaggio d'errore intercettato dal socket.
        """
        logger.warning("⚠️ [Bitget WS Error]: %s", error)

    async def _flush_live_candle_if_expired(self) -> None:
        """Verifica se la candela live in memoria è temporalmente scaduta.
        
        Utile per forzare la chiusura di una candela in periodi di bassa volatilità
        o quando il mercato non invia nuovi trade che spingono avanti il timestamp.
        """
        if self._live_candle is None:
            return
        # Usa il timestamp del server se disponibile, altrimenti quello locale
        now_ms = self.last_server_ts or self.get_synced_now_ms()
        if now_ms >= self._live_candle.timestamp + self.tf_ms:
            self._live_candle.is_closed = True
            await self.process_live_candle(self._live_candle)
            self._live_candle = None

    async def _process_raw_message(self, raw_message: str):
        """Elabora il JSON WebSocket, istanzia `Candle` e orchestra la riconciliazione.

        Usa un meccanismo a "Watermark" basato sull'avanzamento dei timestamp 
        per dedurre matematicamente quando la candela precedente deve considerarsi chiusa.

        Args:
            raw_message (str): Il payload JSON grezzo da Bitget.
        """
        try:
            message = json.loads(raw_message)
            if not isinstance(message, dict) or "data" not in message:
                return  # non è un messaggio che contiene candele
            elif "ts" in message:
                # Aggiorna il timestamp del server visto più recentemente
                self.last_server_ts = int(message["ts"])

            for item in message["data"]:
                # I campi posizionali seguono l'ordine documentato

                ts = int(item[0])  # timestamp in millisecondi
                # Istanzia la dataclass invece del dizionario
                current_candle = Candle(
                    timestamp=ts,
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume_base=float(item[5]),
                    volume_quote=float(item[6]),
                    is_closed=False
                )

                # Logica Watermark: se il timestamp è maggiore di quello in cache, la candela precedente è chiusa
                if self._live_candle is not None:
                    if ts > self._live_candle.timestamp:
                        # 1. Chiudi la candela precedente e inviala
                        self._live_candle.is_closed = True
                        await self.process_live_candle(self._live_candle)
                
                # 2. Aggiorna la cache con il tick corrente
                self._live_candle = current_candle
            
                # 3. Invia l'aggiornamento "live" (is_closed = False)
                await self.process_live_candle(self._live_candle)

        except (json.JSONDecodeError, IndexError, TypeError, KeyError) as e:
            logger.debug("Messaggio non elaborabile ignorato: %s", e)
        except Exception as e:
            logger.error("Errore imprevisto durante l'elaborazione del messaggio WS: %s", e, exc_info=True)

    async def listen_websocket_stream(self):
        """Gestisce il ciclo di vita del WebSocket Bitget, consumando i dati dalla coda.

        Include watchdog e controlli di inattività per forzare la riconnessione automatica 
        se il feed si blocca. Garantisce una chiusura pulita svuotando la coda e arrestando 
        i thread pendenti in caso di stop.

        Raises:
            ConnectionError: Sollevato se non vengono ricevuti dati dal WebSocket entro 60 secondi,
                per innescare la logica di riconnessione della classe base.
            asyncio.CancelledError: Intercettato e ri-sollevato durante lo shutdown del processo
                per garantire la chiusura del client socket e il rilascio delle risorse.
        """
        self.loop = asyncio.get_running_loop()
        self._ws_stopping.clear()
        
        _, channel_name = self._get_bitget_timeframes()

        ws_client = BitgetWsClient(url="wss://ws.bitget.com/v2/ws/public")
        req = SubscribeReq(inst_type=self.inst_type, channel=channel_name, instId=self.symbol)

        ws_client.error_listener = self._ws_error_callback # type: ignore
        ws_client.build()
        ws_client.subscribe([req], self._ws_callback)
        
        logger.info("🔌 [BITGET] Connessione al WebSocket (Canale: %s)...", channel_name)

        try:
            # Timeout breve (es. 5-10s) per fare check periodici senza bloccare il loop
            CHECK_INTERVAL = 10.0
            last_msg_time = self.get_synced_now_ms()
            is_debugging = sys.gettrace() is not None
            max_inactivity = 300 if is_debugging else 60
            while self.running:
                try:
                    raw_message = await asyncio.wait_for(self.ws_queue.get(), timeout=CHECK_INTERVAL)

                    # Aggiorna l'orario dell'ultimo messaggio valido ricevuto
                    last_msg_time = self.get_synced_now_ms()

                    # Elabora il messaggio
                    await self._process_raw_message(raw_message)

                    # Garantiamo SEMPRE il task_done() per evitare il blocco della coda
                    self.ws_queue.task_done()
                
                except asyncio.TimeoutError:

                    # Nessun messaggio per CHECK_INTERVAL secondi: controlla se la candela è scaduta
                    await self._flush_live_candle_if_expired()

                    
                    # Verifica inattività prolungata per riconnessione
                    if (self.get_synced_now_ms() - last_msg_time) > max_inactivity:
                        logger.error("Nessun dato WebSocket da %ss. Forzo riconnessione.", max_inactivity)
                    raise ConnectionError("WebSocket inattivo")
                
        except asyncio.CancelledError:
            logger.info("🛑 Disconnessione WebSocket Bitget in corso...")
            raise
        
        finally:
            # 1. Comunica al thread della callback di fermarsi immediatamente
            self._ws_stopping.set()
            if ws_client:
                # 2. Chiude il socket fisico
                try:
                    ws_client._BitgetWsClient__ws_client.close() # type: ignore
                except Exception as e:
                    logger.debug("Errore innocuo durante la chiusura del client WS: %s", e)

                # 2. Processa i messaggi già accodati (con timeout per non bloccare troppo)
                try:
                    while not self.ws_queue.empty():
                        raw = self.ws_queue.get_nowait()
                        # elabora raw come fai nel loop principale (estrai candele, chiama process_live_candle)
                        # per evitare duplicazione di codice, estrai una funzione helper
                        await self._process_raw_message(raw)
                        self.ws_queue.task_done()

                    # Controlla se l'ultima candela è scaduta
                    await self._flush_live_candle_if_expired()
                except asyncio.QueueEmpty:
                    pass
                except Exception:
                    logger.exception("Errore durante lo svuotamento della coda in shutdown")
            
            # 3. Piccola attesa per consentire al thread di fermarsi del tutto
            await asyncio.sleep(0.7)