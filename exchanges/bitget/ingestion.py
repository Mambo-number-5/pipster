import os
import sys
import json
import asyncio
from typing import cast
from core.base_ingestion import BaseIngestionWorker

from bitget.ws.bitget_ws_client import BitgetWsClient, SubscribeReq

class BitgetWsSubscribeRequest:
    """Richiesta di sottoscrizione compatibile con BitgetWsClient.

    Utilizza i nomi dei campi ufficiali (instType, instId, channel) e
    implementa __hash__ e __eq__ per essere usata come chiave nella mappa
    interna dei listener.
    """
    def __init__(self, instType: str, channel: str, instId: str):
        self.instType = instType
        self.channel = channel
        self.instId = instId

    def __hash__(self):
        return hash(self.instType + self.channel + self.instId)

    def __eq__(self, other):
        if isinstance(other, BitgetWsSubscribeRequest):
            return (self.instType == other.instType and
                    self.channel == other.channel and
                    self.instId == other.instId)
        return False


class BitgetIngestionWorker(BaseIngestionWorker):

    def __init__(self, symbol: str, timeframe: str, inst_type: str = "SPOT"):
        """Inizializza il worker specifico per l'exchange Bitget.

        Recupera le credenziali crittografiche dal file d'ambiente e inizializza
        i client REST (MarketApi) e le strutture di sincronizzazione asincrona
        necessarie per la gestione della concorrenza multithread.

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
        self._ws_stopping = False
        self.loop = None

    def _map_timeframe_to_bitget(self) -> str:
        """Mappa i time-frame standard del sistema nella codifica stringa richiesta dalle API REST Bitget v2.

        Returns:
            str: Il codice granularity accettato da Bitget (es. "15m", "1H", "4H").
        """
        if self.inst_type == "SPOT":
            # Mapping per SPOT (minuti estesi, ore minuscole)
            spot_map = {
                "1m": "1min",
                "5m": "5min",
                "15m": "15min",
                "30m": "30min",
                "1h": "1h",
                "4h": "4h",
                "12h": "12h",
                "1d": "1day",
            }
            return spot_map.get(self.timeframe.lower(), self.timeframe)
        else:
            # Mapping per MIX / USDT-FUTURES (minuti abbreviati, ore maiuscole)
            mix_map = {
                "1m": "1m",
                "5m": "5m",
                "15m": "15m",
                "30m": "30m",
                "1h": "1H",
                "2h": "2H",
                "4h": "4H",
                "12h": "12H",
                "1d": "1D",
            }
            return mix_map.get(self.timeframe.lower(), self.timeframe)
        
    def _get_market_symbol(self) -> str:
            if self.inst_type == "SPOT":
                return f"{self.symbol}_SPBL"
            return self.symbol

    # ------------------------ REST (WARM-UP) ------------------------
    def _sync_fetch_klines(self, since_timestamp: int | None, limit: int) -> dict:
        """Esegue la chiamata REST sincrona e bloccante all'SDK ufficiale Bitget per recuperare lo storico.

        Questo metodo non deve essere invocato direttamente nell'event loop principale per evitare
        il blocco del thread asincrono. Deve essere incapsulato in un thread separato.

        Args:
            since_timestamp (int | None): Timestamp Unix in millisecondi da cui iniziare il recupero.
                Se None, l'exchange restituisce i dati più recenti a ritroso.
            limit (int): Numero massimo di candele da scaricare.

        Returns:
            dict: La risposta JSON grezza restituita dai server di Bitget.
        """
        granularity = self._map_timeframe_to_bitget()
        start_time = str(since_timestamp) if since_timestamp else ""
        end_time = ""
        if self.inst_type == "SPOT":
            parameters = {
                "symbol": self.symbol,
                "granularity": granularity,
                "startTime": start_time,
                "endTime": end_time,
                "limit": str(limit)
                }
        else:
            parameters = {
                "symbol": self.symbol,
                "productType": self.inst_type,
                "granularity": granularity,
                "startTime": start_time,
                "endTime": end_time,
                "limit": str(limit)
            }
        print("DEBUG - parametri REST:", parameters)
        return cast(dict, self.market_client.candles(params=parameters))

    async def fetch_historic_klines(self, since_timestamp: int | None = None, limit: int = 200) -> list[dict]:
        """Scarica, convalida e normalizza le candele storiche da Bitget tramite chiamate REST.

        Implementazione dell'interfaccia astratta della classe base. Gestisce la richiesta
        in un thread dedicato, applica un timeout di sicurezza di 30 secondi e valida
        il codice di stato della risposta dell'exchange.

        Args:
            since_timestamp (int | None, optional): Timestamp iniziale in millisecondi. Defaults to None.
            limit (int, optional): Numero di candele storiche da richiedere. Defaults to 200.

        Returns:
            list[dict]: Lista di dizionari normalizzati e ordinati cronologicamente pronti per Redis::

                {
                    "timestamp": int,
                    "open": float,
                    "high": float,
                    "low": float,
                    "close": float,
                    "volume": float
                }
        """
        try:
            # MODIFICA: timeout di 30 secondi per la chiamata sincrona in thread
            response = await asyncio.wait_for(
                asyncio.to_thread(self._sync_fetch_klines, since_timestamp, limit),
                timeout=30
            )
        except asyncio.TimeoutError:
            print("⏱️ Timeout durante il recupero storico. Nessun dato importato.")
            return []
        
        standardized_data = []
        # MODIFICA: verifica esplicita del codice di successo
        if response and response.get("code") == "00000" and "data" in response:
            for kline in response["data"]:
                standardized_data.append({
                    "timestamp": int(kline[0]),
                    "open": float(kline[1]),
                    "high": float(kline[2]),
                    "low": float(kline[3]),
                    "close": float(kline[4]),
                    "volume": float(kline[5])
                })
            standardized_data.sort(key=lambda x: x["timestamp"])
        else:
            print(f"⚠️ Risposta API inattesa o errore: {response.get('msg', response)}")
        
        return standardized_data

    # ------------------------ WEBSOCKET (LIVE) ------------------------
    def _ws_callback(self, message):
        """Callback sincrona invocata dal thread daemon interno dell'SDK Bitget alla ricezione di un messaggio.

        Inserisce il messaggio grezzo all'interno della coda asincrona in modo thread-safe,
        permettendo all'event loop di asyncio di elaborarlo nel proprio contesto.

        Args:
            message (dict/str): Il payload JSON grezzo inviato dal WebSocket di Bitget.
        """
        if self._ws_stopping:
            return
        # AGGIUNGI QUESTO PRINT DI DIAGNOSTICA:
        print("🔴 [WS RAW RECEIVE]:", message)
        if message and "data" in message and self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.ws_queue.put(message), self.loop)

    def _ws_error_callback(self, error):
        """Callback di gestione degli errori interni sollevati dal thread del WebSocket di Bitget.

        Args:
            error (Exception/str): L'eccezione o il messaggio d'errore intercettato dal socket.
        """
        print(f"⚠️ [Bitget WS Error]: {error}")

    def _get_ws_channel_and_granularity(self) -> tuple[str, str]:
        """Calcola la granularità e il nome del canale WebSocket secondo la documentazione ufficiale.

        I canali candela di Bitget sono identici per SPOT e FUTURES:
        ``candle1m``, ``candle5m``, ``candle15m``, ``candle30m``,
        ``candle1H``, ``candle4H``, ``candle12H``, ``candle1D``, ecc.

        La mappatura è unica e non dipende da ``inst_type``.

        Returns:
            tuple[str, str]: (granularity, channel_name)
            - *granularity*: stringa da usare nelle chiamate REST (es. "1min", "1H").
            - *channel_name*: nome del canale WebSocket (es. "candle1H").
        """
        # Mappatura unificata per WebSocket (minuti abbreviati, ore MAIUSCOLE)
        ws_map = {
            "1m": "1m",
            "5m": "5m",
            "15m": "15m",
            "30m": "30m",
            "1h": "1H",
            "2h": "2H",
            "4h": "4H",
            "12h": "12H",
            "1d": "1D",
        }
        granularity = ws_map.get(self.timeframe.lower(), self.timeframe)
        channel_name = f"candle{granularity}"
        return granularity, channel_name

    async def listen_websocket_stream(self):
        """Gestisce la connessione persistente al WebSocket live di Bitget e consuma la coda dati.

        Inizializza il client WS nativo, si iscrive al canale corretto (es. candle15m) e processa
        i messaggi in arrivo filtrando i pacchetti e delegando l'inserimento su Redis.
        Include un controllo di timeout a 60 secondi per rilevare connessioni silenti o morte,
        forzando una disconnessione pulita in caso di inattività.

        Raises:
            ConnectionError: Sollevato se non vengono ricevuti dati dal WebSocket entro 60 secondi,
                per innescare la logica di riconnessione della classe base.
            asyncio.CancelledError: Intercettato e ri-sollevato durante lo shutdown del processo
                per garantire la chiusura del client socket e il rilascio delle risorse.
        """
        self.loop = asyncio.get_running_loop()
        _, channel_name = self._get_ws_channel_and_granularity()

        ws_client = BitgetWsClient(url="wss://ws.bitget.com/v2/ws/public")
        req = SubscribeReq(inst_type=self.inst_type, channel=channel_name, instId=self.symbol)

        ws_client.error_listener = self._ws_error_callback # type: ignore
        ws_client.build()
        ws_client.subscribe([req], self._ws_callback)
        
        print(f"🔌 [BITGET] Connessione al WebSocket (Canale: {channel_name})...")

        try:
            while self.running:
                # MODIFICA: timeout sulla coda per rilevare connessioni morte
                try:
                    is_debugging = sys.gettrace() is not None
                    timeout_limit = 300 if is_debugging else 60
                    raw_message = await asyncio.wait_for(self.ws_queue.get(), timeout=timeout_limit)
                except asyncio.TimeoutError:
                    print("⚠️ Nessun dato WebSocket da 60s. Forzo riconnessione.")
                    raise ConnectionError("WebSocket inattivo")
                
                try:
                    # 2. Deserializzazione efficiente nel consumer
                    message = json.loads(raw_message)
                    # Se è un messaggio valido e contiene dati
                    if isinstance(message, dict) and "data" in message:
                        for item in message["data"]:
                            # Bitget V2 risponde con un array posizionale:
                            # [timestamp, open, high, low, close, volume, ...]
                            standardized = {
                                "timestamp": int(item[0]),
                                "open": float(item[1]),
                                "high": float(item[2]),
                                "low": float(item[3]),
                                "close": float(item[4]),
                                "volume": float(item[7]) # Volume in USDT
                            }
            
                            # 3. Il push a Redis è DENTRO il for. 
                            # Se Redis è giù, l'eccezione sale all'esterno, interrompendo il loop 
                            # e venendo gestita dal metodo run() principale per il riavvio del bot.
                            await self.push_to_redis(standardized)

                except (json.JSONDecodeError, IndexError, TypeError):
                    # Ignoriamo i messaggi di sistema, i pong o payload formattati male in modo silenzioso
                    pass
                
                finally:
                    # Garantiamo SEMPRE il task_done() per evitare il blocco della coda
                    self.ws_queue.task_done()
                
        except asyncio.CancelledError:
            print("🛑 Disconnessione WebSocket Bitget in corso...")
            raise
        
        finally:
            if ws_client:
                # Segnala alla callback di non inserire più messaggi
                self._ws_stopping = True
                ws_client._BitgetWsClient__ws_client.close() # type: ignore
                # Attendi un istante per dare al thread la possibilità di uscire
                await asyncio.sleep(0.5)