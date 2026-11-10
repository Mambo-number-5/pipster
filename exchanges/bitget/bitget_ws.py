import asyncio
import base64
import hashlib
import hmac
import inspect
import json

from typing import Dict, Optional, Callable, Awaitable, Any, Optional, Union

import time
import logging

from dataclasses import dataclass

from core.rate_limiter import LocalFixedWindow, LocalSlidingWindow
from core.ws_client import AsyncWsClient


logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class SubscribeReq:
    """Rappresenta una richiesta di sottoscrizione a un canale WebSocket Bitget V3.

    Incapsula i parametri di routing del canale (tipo di strumento, topic,
    simbolo e intervallo temporale opzionali) garantendo immutabilità e
    ottimizzazione della memoria grazie a `slots=True`.

    Attributes:
        inst_type (str): Tipo di mercato o strumento (es. "SPOT", "USDT-FUTURES", "COIN-FUTURES").
        topic (str): Nome del canale o topic (es. "ticker", "kline", "books").
        symbol (Optional[str]): Identificativo dello strumento/coppia (es. "BTCUSDT").
        interval (Optional[str]): Intervallo temporale per le candele kline (es. "1m", "1h").
    """
    inst_type: str
    topic: str
    symbol: Optional[str] = None
    interval: Optional[str] = None

    def to_dict(self) -> dict[str, str]:
        """Converte l'oggetto di sottoscrizione nel dizionario raw richiesto da Bitget.

        Returns:
            dict[str, str]: Dizionario contenente le chiavi di protocollo ('instType', 'topic', ecc.).
        """
        d: dict[str, str] = {"instType": self.inst_type, "topic": self.topic}
        if self.symbol is not None:
            d["symbol"] = self.symbol
        if self.interval is not None:
            d["interval"] = self.interval
        return d

    @classmethod
    def kline(cls, inst_type: str, symbol: str, interval: str) -> "SubscribeReq":
        """Factory method per creare una richiesta di sottoscrizione al canale kline (candele).

        Args:
            inst_type: Tipo di strumento (es. "SPOT", "USDT-FUTURES").
            symbol: Simbolo di mercato (es. "BTCUSDT").
            interval: Timeframe delle candele (es. "1m", "15m", "1h").

        Returns:
            SubscribeReq: Istanza configurata per le candele.
        """
        return cls(inst_type, "kline", symbol, interval=interval)

    @classmethod
    def ticker(cls, inst_type: str, symbol: str) -> "SubscribeReq":
        """Factory method per creare una richiesta di sottoscrizione al canale ticker.

        Args:
            inst_type: Tipo di strumento.
            symbol: Simbolo di mercato.

        Returns:
            SubscribeReq: Istanza configurata per i ticker.
        """
        return cls(inst_type, "ticker", symbol)

    @classmethod
    def books(cls, inst_type: str, symbol: str, levels: Optional[int] = None) -> "SubscribeReq":
        """Factory method per creare una richiesta di sottoscrizione alOrder Book (depth).

        Args:
            inst_type: Tipo di strumento.
            symbol: Simbolo di mercato.
            levels: Numero di livelli di profondità opzionali (se None, usa il topic standard "books").

        Returns:
            SubscribeReq: Istanza configurata per il libro degli ordini.
        """
        topic = "books" if levels is None else f"books{levels}"
        return cls(inst_type, topic, symbol)

    @classmethod
    def public_trade(cls, inst_type: str, symbol: str) -> "SubscribeReq":
        """Factory method per creare una richiesta di sottoscrizione al flusso trade pubblici.

        Args:
            inst_type: Tipo di strumento.
            symbol: Simbolo di mercato.

        Returns:
            SubscribeReq: Istanza configurata per i trade pubblici.
        """
        return cls(inst_type, "publicTrade", symbol)

    @classmethod
    def liquidation(cls, inst_type: str) -> "SubscribeReq":
        """Factory method per creare una richiesta di sottoscrizione alle liquidazioni di mercato.

        Args:
            inst_type: Tipo di strumento.

        Returns:
            SubscribeReq: Istanza configurata per le liquidazioni.
        """
        return cls(inst_type, "liquidation")   # niente symbol

    @classmethod
    def from_arg(cls, arg: dict) -> "SubscribeReq":
        """Ricostruisce un oggetto SubscribeReq a partire dal dizionario 'arg' restituito dal server.

        Args:
            arg: Dizionario di metadata del canale ricevuto nel messaggio WebSocket.

        Returns:
            SubscribeReq: Istanza popolata con i dati estratti.
        """
        return cls(
            inst_type=arg["instType"],
            topic=arg["topic"],
            symbol=arg.get("symbol"),
            interval=arg.get("interval"),
        )

@dataclass(frozen=True, slots=True)
class WsEnvelope:
    """Busta di normalizzazione comune a tutti i messaggi WebSocket Bitget V3.

    Esegue il parsing una-tantum della struttura di controllo di alto livello.
    Il campo `data` viene mantenuto come lista di dizionari grezzi per demandare
    l'interpretazione specifica del payload al consumer finale.

    Attributes:
        event (Optional[str]): Tipo di evento di sistema (es. "login", "subscribe", "error").
        arg (Optional[dict[str, Any]]): Metadati descrittivi del canale associato al messaggio.
        action (Optional[str]): Tipo di operazione sui dati ("snapshot", "update").
        data (Optional[list[dict[str, Any]]]): Lista dei payload di dati di mercato o account.
        ts (Optional[int]): Timestamp Unix dell'evento trasmesso dal server.
        code (Optional[Union[str, int]]): Codice di stato o di errore restituito da Bitget.
        msg (Optional[str]): Messaggio testuale descrittivo associato all'evento o all'errore.
        conn_id (Optional[str]): Identificativo univoco della connessione lato server.
    """
    event: Optional[str] = None       # "login", "subscribe", "unsubscribe", "error"
    arg: Optional[dict[str, Any]] = None
    action: Optional[str] = None      # "snapshot", "update"
    data: Optional[list[dict[str, Any]]] = None
    ts: Optional[int] = None
    code: Optional[Union[str, int]] = None
    msg: Optional[str] = None
    conn_id: Optional[str] = None

    @classmethod
    def from_json(cls, raw: str) -> "WsEnvelope":
        """Analizza una stringa JSON grezza ricevuta via WebSocket e restituisce un'istanza WsEnvelope.

        Args:
            raw: Stringa JSON grezza proveniente dal socket.

        Returns:
            WsEnvelope: Istanza normalizzata della busta di messaggio.

        Raises:
            ValueError: Se la stringa non è un JSON valido o la radice non è un oggetto.
        """
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError(f"Messaggio non JSON: {raw[:200]}")
        if not isinstance(d, dict):
            raise ValueError("Payload radice non è un oggetto JSON")
        return cls(
            event=d.get("event"),
            arg=d.get("arg"),
            action=d.get("action"),
            data=d.get("data"),
            ts=d.get("ts"),
            code=d.get("code"),
            msg=d.get("msg"),
            conn_id=d.get("connId"),
        )

_PROD_PUBLIC = "wss://ws.bitget.com/v3/ws/public"
_PROD_PRIVATE = "wss://ws.bitget.com/v3/ws/private"
_DEMO_PUBLIC = "wss://wspap.bitget.com/v3/ws/public"
_DEMO_PRIVATE = "wss://wspap.bitget.com/v3/ws/private"

class BitgetWsClient:
    """Client WebSocket asincrono specializzato per l'exchange Bitget (API V3).

    Gestisce l'autenticazione HMAC SHA256 per endpoint privati, il ciclo di vita
    delle sottoscrizioni ai canali pubblici e privati, il ripristino automatico
    delle sessioni e il throttling integrato dei messaggi in uscita (limiti
    di invio e di sottoscrizione oraria).

    Attributes:
        api_key (Optional[str]): Chiave API Bitget per i canali privati.
        api_secret (Optional[str]): Segreto API per la firma crittografica HMAC.
        passphrase (Optional[str]): Passphrase associata alle API key.
        error_listener (Optional[Callable[[str], Awaitable[None]]]): Callback asincrona per gli errori.
        subscriptions (Dict[SubscribeReq, Callable]): Registro attivo delle sottoscrizioni e relative callback.
    """
    def __init__(self,
                 *,
                 private: bool = False,
                 demo: bool = False,
                 api_key: Optional[str] = None,
                 api_secret: Optional[str] = None,
                 passphrase: Optional[str] = None,
                 silence_timeout: float = 60.0,
                 before_send: Optional[Callable[[dict], Awaitable[dict]]] = None
                 ):
        """Inizializza il client WebSocket Bitget V3.

        Args:
            private: Se True, connette all'endpoint privato; altrimenti a quello pubblico.
            demo: Se True, connette all'ambiente di testnet/paper trading.
            api_key: Chiave API per autenticazione privata.
            api_secret: Segreto API per firma HMAC.
            passphrase: Passphrase delle API.
            silence_timeout: Tempo massimo in secondi di inattività prima di forzare il reset.
            before_send: Hook asincrono opzionale eseguito prima di serializzare e inviare un payload.
        """
        if private:
            url = _DEMO_PRIVATE if demo else _PROD_PRIVATE
        else:
            url = _DEMO_PUBLIC if demo else _PROD_PUBLIC
        self._client = AsyncWsClient(
            url=url,
            ping_interval=30.0,
            ping_payload="ping",
            silence_timeout=silence_timeout
        )
        self._before_send = before_send
        # resto invariato
        self._client.on_message = self._handle_raw_message
        self._client.on_connect = self._on_reconnect
        self._client.on_error = self._on_ws_error
        self.error_listener: Optional[Callable[[str], Awaitable[None]]] = None

        #Credenziali
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase

        # Stato del login asincrono (sincronizzazione via Event + flag booleani)
        self._login_event: asyncio.Event = asyncio.Event()
        self._login_ok: bool = False
        self._pending_login: bool = False
        self._requires_login = private

        self.subscriptions: Dict[SubscribeReq, Callable] = {}
    
        # --- Throttling Esternalizzato ---
        # Limite invio WebSocket: 10 messaggi al secondo
        self._msg_throttle = LocalSlidingWindow(capacity=10, window_sec=1.0)
        
        # Limite sottoscrizioni Bitget: 240 canali all'ora
        self._sub_throttle = LocalFixedWindow(capacity=240, window_sec=3600.0)

    async def start(self) -> None:
        """Avvia la connessione del client WebSocket sottostante e il loop di gestione."""
        logger.info("Avvio BitgetWsClient su %s", self._client.url)
        await self._client.start()

    async def stop(self) -> None:
        """Arresta formalmente la connessione WebSocket e rilascia le risorse."""
        logger.info("Arresto BitgetWsClient su %s", self._client.url)
        await self._client.stop()

    async def _send_payload(self, payload: dict) -> None:
        """Serializza e invia un payload JSON applicando l'hook before_send e il throttle a 10 msg/s.

        Args:
            payload: Dizionario da trasmettere sul socket.
        """
        if self._before_send:
            payload = await self._before_send(payload)
        await self._msg_throttle.acquire()
        await self._client.send(json.dumps(payload))

    async def login(self, timeout: float = 10.0) -> bool:
        """Esegue l'autenticazione HMAC SHA256 per l'endpoint privato Bitget V3.

        Args:
            timeout: Tempo massimo in secondi di attesa per la risposta di login dal server.

        Returns:
            bool: True se l'autenticazione ha avuto successo, False altrimenti.

        Raises:
            ValueError: Se le credenziali API (api_key, api_secret, passphrase) sono incomplete.
        """
        if not self.api_key or not self.api_secret or not self.passphrase:
            raise ValueError("Credenziali API incomplete per il login al WebSocket privato.")

        # Reset dello stato
        self._login_event.clear()
        self._login_ok = False
        self._pending_login = True

        # Bitget V3 WS richiede il timestamp Unix in secondi
        timestamp = str(int(time.time()))
        sign_string = timestamp + "GET" + "/user/verify"

        mac = hmac.new(
            self.api_secret.encode("utf-8"),
            sign_string.encode("utf-8"),
            hashlib.sha256,
        )
        sign = base64.b64encode(mac.digest()).decode("utf-8")

        payload = {
            "op": "login",
            "args": [
                {
                    "apiKey": self.api_key,
                    "passphrase": self.passphrase,
                    "timestamp": timestamp,
                    "sign": sign,
                }
            ],
        }
        logger.info("Invio frame di autenticazione a Bitget WS...")
        await self._send_payload(payload)

        try:
            await asyncio.wait_for(self._login_event.wait(), timeout=timeout)
            logger.info("Autenticazione WS Bitget completata con successo.")
        except asyncio.TimeoutError:
            logger.error("Timeout durante l'attesa della risposta di login da Bitget WS.")
            self._pending_login = False
            return False

        self._pending_login = False
        return self._login_ok

    async def subscribe(self, reqs: list[SubscribeReq], callback: Callable) -> None:
        """Registra e invia la richiesta di sottoscrizione per uno o più canali di mercato.

        Args:
            reqs: Lista di oggetti SubscribeReq da sottoscrivere.
            callback: Funzione o coroutine da invocare alla ricezione di dati sui canali.

        Raises:
            ValueError: Se si supera il limite massimo di 1000 canali per connessione.
            RuntimeError: Se viene esaurito il rate limit orario delle sottoscrizioni (240/ora).
        """
        if self._requires_login and not self._login_ok:
            raise RuntimeError(
                "Login non completato: impossibile sottoscrivere canali privati (errore Bitget 30004)."
            )


        # Controllo limite fisso di connessione
        if len(self.subscriptions) + len(reqs) > 1000:
            raise ValueError(
                "Superato il limite di 1000 canali per connessione Bitget "
                f"(attuali: {len(self.subscriptions)}, richiesti: {len(reqs)})."
            )

        # Controllo Rate Limit orario in modo sicuro tramite il throttle dedicato
        if not await self._sub_throttle.check_and_acquire(amount=len(reqs)):
            logger.warning("Limite di 240 subscribe/ora superato su Bitget! Richiesta saltata/rallentata.")
            # Puoi decidere se lanciare un'eccezione o gestire la cosa diversamente
            raise RuntimeError("Rate limit orario delle sottoscrizioni esaurito.")

        for req in reqs:
            self.subscriptions[req] = callback

        if self._client.is_connected:
            logger.info("Invio richiesta di sottoscrizione per %d canali", len(reqs))
            payload = {"op": "subscribe", "args": [req.to_dict() for req in reqs]}
            await self._send_payload(payload)
    
    async def _on_ws_error(self, error: Exception) -> None:
        """Inoltra gli errori di trasporto del transport layer al listener registrato.

        Args:
            error: Eccezione di rete o WebSocket riscontrata.
        """
        logger.warning("Errore di trasporto WS Bitget: %s", str(error))
        if self.error_listener:
            await self._invoke_callback(self.error_listener, str(error))

    async def _on_reconnect(self) -> None:
        """Callback di sistema invocata da AsyncWsClient quando la connessione viene stabilita o ripristinata."""
        logger.info("Connessione stabilita/ripristinata su %s", self._client.url)

        if self.api_key:
            ok = await self.login()
            if not ok:
                logger.error("Login WS fallito dopo la (ri)connessione. Le sottoscrizioni private potrebbero non funzionare.")

        if self.subscriptions:
            reqs = list(self.subscriptions.keys())
            logger.info("Ripristino sottoscrizione per %d canali attivi...", len(reqs))
            payload = {"op": "subscribe", "args": [req.to_dict() for req in reqs]}
            await self._send_payload(payload)

    async def _handle_raw_message(self, message: str) -> None:
        """Analizza il messaggio raw in arrivo, normalizzandolo tramite WsEnvelope e smistandolo.

        Args:
            message: Stringa JSON grezza o frame di controllo ricevuto dal WebSocket.
        """
        if message == "pong":
            return

        try:
            env = WsEnvelope.from_json(message)
        except ValueError as e:
            logger.error("Messaggio WS non decodificabile: %s", e)
            return

        if env.event == "login":
            self._handle_login_event(env)
        elif env.event == "error":
            await self._handle_error_event(env)
        elif env.event in ("subscribe", "unsubscribe"):
            logger.debug("Conferma %s: arg=%s", env.event, env.arg)
        elif env.data is not None and env.arg is not None:
            await self._route_data(env, message)

    def _handle_login_event(self, env: WsEnvelope) -> None:
        """Elabora l'esito del frame di autenticazione in arrivo dal server.

        Args:
            env: Busta normalizzata WsEnvelope contenente il codice di risposta del login.
        """
        if env.code in ("0", 0):
            logger.info("Autenticazione WS Bitget completata.")
            self._login_ok = True
        else:
            logger.error("Errore autenticazione WS: code=%s msg=%s", env.code, env.msg)
            self._login_ok = False
        self._login_event.set()


    async def _handle_error_event(self, env: WsEnvelope) -> None:
        """Gestisce i messaggi di errore inviati dal server Bitget tramite WebSocket.

        Args:
            env: Busta normalizzata WsEnvelope contenente i dettagli dell'errore.
        """
        err_msg = f"Bitget WS Error [{env.code}]: {env.msg}"
        logger.warning("Errore WS: %s", err_msg)
        if self.error_listener:
            await self._invoke_callback(self.error_listener, err_msg)
        if self._pending_login and not self._login_event.is_set():
            self._login_ok = False
            self._login_event.set()

    async def _route_data(self, env: WsEnvelope, raw: str) -> None:
        """Instrada i messaggi di dati di mercato verso la callback registrata per lo specifico canale.

        Args:
            env: Busta WsEnvelope contenente i metadati di routing (arg).
            raw: Messaggio JSON grezzo originale da passare alla callback utente.
        """
        try:
            req_key = SubscribeReq.from_arg(env.arg)  # type: ignore[arg-type]
        except (KeyError, TypeError) as e:
            logger.warning("arg malformato: %s | %s", env.arg, e)
            return
        callback = self.subscriptions.get(req_key) # callback: dizionario di callback che usa le richieste come chiave
        if callback is None:
            logger.debug("Canale non sottoscritto: %s", env.arg)
            return
        await self._invoke_callback(callback, raw)

    async def _invoke_callback(self, callback: Callable, *args) -> None:
        """Esegue in sicurezza una callback utente gestendo sia coroutine asincrone che funzioni sincrone.

        Args:
            callback: Funzione o coroutine da eseguire.
            *args: Argomenti posizionali da passare alla callback.
        """
        try:
            if inspect.iscoroutinefunction(callback):
                await callback(*args)
            else:
                callback(*args)
        except Exception:
            logger.error("Eccezione durante l'esecuzione della callback utente Bitget WS", exc_info=True)

    async def unsubscribe(self, reqs: list[SubscribeReq]) -> None:
        """Rimuove una o più sottoscrizioni attive e invia il frame di disiscrizione.

        Args:
            reqs: Lista di oggetti SubscribeReq da rimuovere.
        """
        if not reqs:
            return

        payload = {
            "op": "unsubscribe",
            "args": [req.to_dict() for req in reqs],
        }
        logger.info("Invio unsubscribe per %d canali", len(reqs))
        await self._send_payload(payload)

        for req in reqs:
            self.subscriptions.pop(req, None)

# TO DO: in ingestion con v3 quote_index è sempre 6, meglio usare v3 per questo motivo. In v2 invece è 6 per futures e 7 per spot.