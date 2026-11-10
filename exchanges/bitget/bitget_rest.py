# exchanges/bitget/bitget_rest.py
import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlencode
from typing import Any, Callable, Awaitable, Dict, Optional

import redis.asyncio as redis

from core.exceptions import NonRetriableHttpError, RetryableWithDelay
from core.http_client import AsyncHttpClient, RateLimitError
from core.rate_limiter import RateLimitManager

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bitget.com"

# Refresh proattivo dell'orologio (secondi)
_TIME_SYNC_REFRESH_SEC = 30 * 60
# Numero massimo di resync su 40008 per singola richiesta
_TIME_SYNC_RETRY_MAX = 2

# Bucket per endpoint. Ogni endpoint Bitget ha limite 20/s/IP.
_ENDPOINT_BUCKETS: dict[str, tuple[str, int]] = {
    "/api/v3/market/candles": ("public_candles", 20),
    "/api/v3/market/history-candles": ("public_history_candles", 20),
    "/api/v3/market/time": ("public_time", 20),
    "/api/v3/market/instruments": ("public_instruments", 20),
    "/api/v3/market/tickers": ("public_tickers", 20),
}

_RETRIABLE_BUSINESS_CODES = frozenset({"40725"})
_AUTH_CODES = frozenset({"40006", "40009"})
_TIMESTAMP_EXPIRED_CODE = "40008"


# ---------------------------------------------------------------------------
# Eccezioni business Bitget
# ---------------------------------------------------------------------------

class BitgetApiError(Exception):
    """Errore business generico restituito dall'API Bitget.

    Attributes:
        code (str): Codice di errore alfanumerico restituito dall'exchange.
        msg (str): Messaggio descrittivo associato all'errore.
    """

    def __init__(self, code: str, msg: str):
        super().__init__(f"Bitget {code}: {msg}")
        self.code = code
        self.msg = msg


class BitgetAuthError(BitgetApiError):
    """Eccezione sollevata per errori di autenticazione critici (es. 40006, 40009).

    Questi errori non sono ritentabili automaticamente poiché richiedono la correzione
    delle credenziali o della configurazione delle chiavi API.
    """


class BitgetTimestampExpired(BitgetApiError):
    """Eccezione sollevata quando il timestamp della richiesta è scaduto (codice 40008).

    Viene generata se il fallimento persiste anche dopo aver tentato il resync
    automatico dell'orologio con il server.
    """


class BitgetRetryableError(RetryableWithDelay):
    """Errore business temporaneo (es. codice 40725) che supporta il retry con delay.

    Attributes:
        code (str): Codice di errore dell'exchange.
        msg (str): Descrizione dell'errore.
    """

    def __init__(self, code: str, msg: str, delay: float = 1.0):
        super().__init__(delay=delay, message=f"Bitget {code}: {msg}")
        self.code = code
        self.msg = msg


# ---------------------------------------------------------------------------
# Inizializzazione rate limiter
# ---------------------------------------------------------------------------

def init_rate_limiters(redis_client: redis.Redis) -> RateLimitManager:
    """Costruisce e configura il RateLimitManager distribuito per l'exchange Bitget.

    Registra il bucket globale per IP (limite di 6000 req/min) e un bucket
    dedicato per ciascun endpoint di mercato tracciato (20 req/s).

    Args:
        redis_client: Istanza connessa del client Redis asincrono.

    Returns:
        RateLimitManager: Gestore dei rate limit configurato per Bitget.
    """
    limiter = RateLimitManager(redis_client, exchange="bitget")
    limiter.add_limiter("public_ip", limit=6000, window_ms=60_000)
    for bucket_name, limit in _ENDPOINT_BUCKETS.values():
        limiter.add_limiter(bucket_name, limit=limit, window_ms=1_000)
    return limiter


# ---------------------------------------------------------------------------
# Clock sync
# ---------------------------------------------------------------------------

class _BitgetClockSync:
    """Mantiene l'offset temporale tra il sistema locale e i server Bitget tramite NTP."""

    def __init__(self) -> None:
        self.offset_ms: int = 0
        self.last_sync_monotonic: float = 0.0
        self._synced: bool = False

    @property
    def is_synced(self) -> bool:
        """Restituisce True se è stata eseguita almeno una sincronizzazione oraria con successo."""
        return self._synced

    def now_ms(self) -> int:
        """Restituisce il timestamp corretto in millisecondi applicando l'offset calcolato."""
        return int(time.time() * 1000) + self.offset_ms

    def needs_refresh(self, interval_sec: float = _TIME_SYNC_REFRESH_SEC) -> bool:
        """Verifica se è necessario eseguire un nuovo refresh dell'orologio.

        Args:
            interval_sec: Intervallo temporale massimo di validità della sincronizzazione.

        Returns:
            bool: True se non è mai stato sincronizzato o se il tempo trascorso supera l'intervallo.
        """
        if not self._synced:
            return True
        return (time.monotonic() - self.last_sync_monotonic) > interval_sec

    async def sync(self, fetch_payload: Callable[[], Awaitable[dict]]) -> None:
        """Esegue un round-trip verso l'endpoint temporale e calcola l'offset NTP.

        Args:
            fetch_payload: Callable asincrona che restituisce il dizionario di risposta
                da `/api/v3/market/time` (contenente `requestTime` e `data.serverTime`).
                Se `requestTime` manca, si usa il mid-point semplice.
        """
        t0 = int(time.time() * 1000)
        res = await fetch_payload()
        t3 = int(time.time() * 1000)

        data = res.get("data", {})
        server_time = int(data["serverTime"])
        request_time = res.get("requestTime")

        if request_time is not None:
            t1 = int(request_time)
            t2 = server_time
            offset = ((t1 - t0) + (t2 - t3)) // 2
            rtt = (t3 - t0) - (t2 - t1)
        else:
            local_mid = (t0 + t3) // 2
            offset = server_time - local_mid
            rtt = t3 - t0

        self.offset_ms = offset
        self._synced = True
        self.last_sync_monotonic = time.monotonic()
        logger.info(
            "Clock sync Bitget: offset=%+dms, rtt=%dms", offset, rtt,
        )


# ---------------------------------------------------------------------------
# Client REST
# ---------------------------------------------------------------------------

class BitgetRestClient:
    """Client REST asincrono per le API V3 di Bitget.

    Gestisce in modo trasparente la firma HMAC delle richieste private, la sincronizzazione
    proattiva dell'orologio, il throttling tramite rate limiter iniettato e la traduzione
    degli errori business in eccezioni tipizzate.

    Attributes:
        api_key (str): Chiave API Bitget.
        secret_key (str): Chiave segreta HMAC.
        passphrase (str): Passphrase dell'account API.
        demo (bool): Se True, utilizza l'ambiente di Testnet/Paper Trading.
        http (AsyncHttpClient): Client HTTP di trasporto sottostante.
    """

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        passphrase: str = "",
        demo: bool = False,
        rate_limiter: RateLimitManager | None = None,):
        """Inizializza il client REST Bitget.

        Args:
            api_key: Chiave API.
            secret_key: Segreto API.
            passphrase: Passphrase di sicurezza.
            demo: Attiva la modalità demo/testnet se True.
            rate_limiter: Gestore dei rate limit (opzionale).
        """
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.demo = demo
        self.http = AsyncHttpClient()
        self._rate_limiter = rate_limiter
        self._clock = _BitgetClockSync()

    async def close(self) -> None:
        """Chiude la sessione HTTP sottostante e rilascia le risorse di rete."""
        await self.http.close()

    # ------------------------------------------------------------------
    # Firma
    # ------------------------------------------------------------------

    def _generate_signature(
        self, timestamp: str, method: str, request_path: str,
        query_string: str = "", body: str = "",) -> str:
        """Genera la firma crittografica HMAC-SHA256 richiesta da Bitget V3.

        Args:
            timestamp: Timestamp Unix in millisecondi come stringa.
            method: Metodo HTTP (GET, POST, etc.).
            request_path: Percorso URI della richiesta (es. /api/v3/order/create).
            query_string: Parametri di query string ordinati (opzionale).
            body: Payload JSON serializzato (opzionale).

        Returns:
            str: Firma codificata in Base64.
        """
        message = timestamp + method.upper() + request_path
        if query_string:
            message += "?" + query_string
        message += body
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            digestmod="sha256",
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _build_headers(
        self, method: str, request_path: str,
        query_string: str = "", body_str: str = "",) -> Dict[str, str]:
        """Costruisce gli header HTTP completi di autenticazione per richieste firmate.

        Args:
            method: Metodo HTTP.
            request_path: URI path.
            query_string: Query string ordinata per la firma.
            body_str: Body della richiesta in formato stringa.

        Returns:
            Dict[str, str]: Dizionario degli header HTTP.
        """
        timestamp = str(self._clock.now_ms())
        signature = self._generate_signature(
            timestamp, method, request_path, query_string, body_str,
        )
        headers = {
            "Content-Type": "application/json",
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": self.passphrase,
            "locale": "en-US",
        }
        if self.demo:
            headers["paptrading"] = "1"
        return headers

    @staticmethod
    def _build_query_for_sign(params: Optional[Dict]) -> str:
        """Genera la stringa dei parametri ordinata e NON urlencoded per la firma crittografica.(ADR-0007)

        Args:
            params: Dizionario dei parametri di query.

        Returns:
            str: Stringa formattata e ordinata per la firma.
        """
        if not params:
            return ""
        sorted_items = sorted(params.items(), key=lambda x: x[0])
        return "&".join(f"{k}={v}" for k, v in sorted_items)

    @staticmethod
    def _build_query_for_url(params: Optional[Dict]) -> str:
        """Genera la query string ordinata e URL-encoded da aggiungere all'URL HTTP.

        Args:
            params: Dizionario dei parametri di query.

        Returns:
            str: Query string codificata.
        """
        if not params:
            return ""
        sorted_items = sorted(params.items(), key=lambda x: x[0])
        return urlencode(sorted_items)

    # ------------------------------------------------------------------
    # Clock sync
    # ------------------------------------------------------------------

    async def _fetch_time_payload(self) -> dict:
        """Recupera il body completo della risposta dall'endpoint di sincronizzazione oraria.

        Returns:
            dict: Payload JSON contenente i dati di tempo del server.
        """
        return await self._request(
            "GET", "/api/v3/market/time",
            auth_required=False, rate_limiter_key="public_time",
        )

    async def sync_server_time(self) -> None:
        """Forza un aggiornamento immediato della sincronizzazione temporale con il server Bitget."""
        await self._clock.sync(self._fetch_time_payload)

    async def _ensure_time_sync(self) -> None:
        """Verifica se il clock locale necessita di un refresh e, in tal caso, esegue il sync."""
        if self._clock.needs_refresh():
            await self.sync_server_time()

    async def fetch_server_time(self) -> int:
        """Ottiene il timestamp corrente direttamente dal server Bitget in millisecondi Unix.

        Returns:
            int: Timestamp del server in ms.
        """
        res = await self._fetch_time_payload()
        return int(res["data"]["serverTime"])

    # ------------------------------------------------------------------
    # Parsing e gestione errori business
    # ------------------------------------------------------------------    

    @staticmethod
    def _parse_business_error(body: str) -> tuple[str, str] | None:
        """Estrae (code, msg) dal body di un errore Bitget.
    
        Bitget restituisce errori business (40006, 40008, 40009, 40725) sia con
        HTTP 200 sia con HTTP 4xx. Questo helper normalizza il body JSON.
    
        Returns:
            (code, msg) se il body è JSON con campo `code`, None altrimenti.
        """
        if not body:
            return None
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        code = parsed.get("code")
        if code is None:
            return None
        return str(code), str(parsed.get("msg", ""))
    
    
    async def _handle_business_code(
        self,
        code: str,
        msg: str,
        auth_required: bool,
        attempt: int,
        original_http_error: NonRetriableHttpError | None = None,
    ) -> bool:
        """Gestisce un codice business Bitget in modo uniforme.
    
        Args:
            code: Codice business Bitget (es. "00000", "40008", "40725").
            msg: Messaggio associato.
            auth_required: Se la richiesta era autenticata.
            attempt: Numero del tentativo corrente (0-based).
            original_http_error: Se il codice è stato estratto da un HTTP 4xx,
                l'eccezione HTTP originale da ripropagare per codici non riconosciuti.
                Se None, il codice proviene da un HTTP 200 e per codici non
                riconosciuti si solleva `BitgetApiError`.
    
        Returns:
            bool: True se è stato eseguito un resync e il chiamante deve ritentare;
            False se il codice era "00000" (successo).
    
        Raises:
            BitgetAuthError: 40006, 40009.
            BitgetTimestampExpired: 40008 persistente.
            BitgetRetryableError: 40725.
            BitgetApiError: codici non riconosciuti, se `original_http_error` è None.
            NonRetriableHttpError: codici non riconosciuti, se `original_http_error`
                è fornito.
        """
        if code == "00000":
            return False
    
        if code == _TIMESTAMP_EXPIRED_CODE and auth_required and attempt < _TIME_SYNC_RETRY_MAX:
            logger.warning("Bitget 40008: resync orologio e retry (attempt=%d)", attempt + 1)
            await self.sync_server_time()
            return True
    
        if code in _RETRIABLE_BUSINESS_CODES:
            logger.warning("Bitget %s (transient): %s", code, msg)
            raise BitgetRetryableError(code, msg)
    
        if code in _AUTH_CODES:
            logger.error("Bitget %s (auth/config): %s", code, msg)
            raise BitgetAuthError(code, msg)
    
        if code == _TIMESTAMP_EXPIRED_CODE:
            logger.error("Bitget 40008 persistente dopo resync: %s", msg)
            raise BitgetTimestampExpired(code, msg)
    
        if original_http_error is not None:
            raise original_http_error
    
        logger.error("Bitget %s: %s", code, msg)
        raise BitgetApiError(code, msg)
    
    # ------------------------------------------------------------------
    # Core request
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        body: Optional[Dict] = None,
        auth_required: bool = False,
        rate_limiter_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Esegue una chiamata REST applicando firme, rate limiting, gestione errori
        e auto-resync del timestamp.

        Gestisce i codici business Bitget (40006/40008/40009/40725) sia nel caso
        HTTP 200 sia nel caso HTTP 4xx, delegando la traduzione a
        `_handle_business_code`.

        Args:
            method: Metodo HTTP ("GET", "POST", ecc.).
            path: Endpoint API (es. "/api/v3/market/candles").
            params: Parametri di query opzionali.
            body: Dizionario del corpo della richiesta per POST opzionale.
            auth_required: Se True, include le firme HMAC e verifica la sincronizzazione dell'orologio.
            rate_limiter_key: Chiave opzionale per il bucket di rate limit specifico dell'endpoint.

        Returns:
            Dict[str, Any]: Risposta JSON decodificata restituita dall'exchange.

        Raises:
            BitgetAuthError: errori di autenticazione (40006, 40009).
            BitgetTimestampExpired: 40008 persistente.
            BitgetRetryableError: errori transitori (40725).
            BitgetApiError: altri errori business in HTTP 200.
            NonRetriableHttpError: errori HTTP 4xx non riconducibili a un codice
                business Bitget (o con codice business non riconosciuto).
        """
        if auth_required:
            await self._ensure_time_sync()

        url = f"{BASE_URL}{path}"
        if params:
            url += "?" + self._build_query_for_url(params)

        # Bucket: globale + endpoint-specifico (esplicito o derivato dal path)
        if rate_limiter_key is not None:
            endpoint_bucket: str | None = rate_limiter_key
        else:
            entry = _ENDPOINT_BUCKETS.get(path)
            endpoint_bucket = entry[0] if entry is not None else None
        buckets = ("public_ip", endpoint_bucket) if endpoint_bucket else ("public_ip",)

        async def before_attempt() -> None:
            if self._rate_limiter is not None:
                await self._rate_limiter.acquire(*buckets)

        for attempt in range(_TIME_SYNC_RETRY_MAX + 1):
            # Serializza SEMPRE se il body è presente
            body_str = "" if body is None else json.dumps(body, separators=(",", ":"))

            if auth_required:
                sign_query = self._build_query_for_sign(params)
                headers = self._build_headers(method, path, sign_query, body_str)

            elif body_str != "":
                headers = {"Content-Type": "application/json"}
            
            else:
                headers: Dict[str, str] = {}

            logger.debug("Bitget REST %s %s (auth=%s, attempt=%d)",
                         method.upper(), path, auth_required, attempt + 1)

            try:
                if method.upper() == "GET":
                    res = await self.http.get(
                        url, headers=headers, before_attempt=before_attempt,
                    )
                else:
                    res = await self.http.post(
                        url, headers=headers, data=body_str,
                        before_attempt=before_attempt,
                    )
            except NonRetriableHttpError as e:
                # Bitget può restituire business errors con HTTP 4xx: prova a estrarre il codice
                parsed = self._parse_business_error(e.body)
                if parsed is None:
                    raise   # non è un errore business Bitget: propaga l'HTTP error

                code, msg = parsed
                needs_retry = await self._handle_business_code(
                    code, msg, auth_required, attempt, original_http_error=e,
                )
                if needs_retry:
                    continue
                # Solo se code == "00000" su HTTP 4xx (patologico)
                raise RuntimeError(f"Bitget: code=00000 su HTTP {e.status}, scenario inatteso"
                                 f" (body={e.body})")
            
            # Caso HTTP 200: parsing degli errori business nel body
            needs_retry = await self._handle_business_code(
                res.get("code", ""), res.get("msg", ""), auth_required, attempt,
            )
            if needs_retry:
                continue
            return res


        # Solo se _TIME_SYNC_RETRY_MAX è 0 e il primo tentativo dà 40008
        raise BitgetTimestampExpired(_TIMESTAMP_EXPIRED_CODE, "resync esaurito")

    # ------------------------------------------------------------------
    # Endpoint pubblici
    # ------------------------------------------------------------------

    async def get_candles(
        self,
        symbol: str,
        category: str = "USDT-FUTURES",
        interval: str = "1m",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        candle_type: str = "market",
        limit: int = 1000,) -> list[list[str]]:
        """Recupera le candele di mercato (OHLCV) recenti per lo strumento specificato.

        Args:
            symbol: Simbolo di mercato (es. "BTCUSDT").
            category: Categoria di mercato (es. "USDT-FUTURES", "SPOT").
            interval: Timeframe delle candele (es. "1m", "1h", "1D").
            start_time: Timestamp di inizio in millisecondi (opzionale).
            end_time: Timestamp di fine in millisecondi (opzionale).
            candle_type: Tipo di candela (default: "market").
            limit: Numero massimo di record da restituire (max 1000).

        Returns:
            list[list[str]]: Lista di candele formattate come array di stringhe grezze.
        """
        endpoint = "/api/v3/market/candles"
        params: Dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "type": candle_type,
            "limit": str(limit),
        }
        if start_time is not None:
            params["startTime"] = str(start_time)
        if end_time is not None:
            params["endTime"] = str(end_time)

        res = await self._request(
            "GET", endpoint, params=params, auth_required=False,
            rate_limiter_key="public_candles",
        )
        return res.get("data", [])

    async def get_historical_candles(
        self,
        symbol: str,
        category: str = "USDT-FUTURES",
        interval: str = "1m",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        candle_type: str = "market",
        limit: int = 100,) -> list[list[str]]:
        """Recupera lo storico delle candele di mercato (storico profondo) per lo strumento.

        Args:
            symbol: Simbolo di mercato.
            category: Categoria di mercato.
            interval: Timeframe candele.
            start_time: Timestamp inizio opzionale.
            end_time: Timestamp fine opzionale.
            candle_type: Tipo candela.
            limit: Numero massimo di record.

        Returns:
            list[list[str]]: Storico delle candele.
        """
        endpoint = "/api/v3/market/history-candles"
        params: Dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "type": candle_type,
            "limit": str(limit),
        }
        if start_time is not None:
            params["startTime"] = str(start_time)
        if end_time is not None:
            params["endTime"] = str(end_time)

        res = await self._request(
            "GET", endpoint, params=params, auth_required=False,
            rate_limiter_key="public_history_candles",
        )
        return res.get("data", [])

    async def get_instruments(
        self,
        category: str,
        symbol: Optional[str] = None,) -> list[dict]:
        """Ottiene la lista degli strumenti negoziabili per categoria, filtrabile per simbolo.

        Usata dal worker per validare i simboli prima del warm-up e per leggere
        le precisioni e le quantità minime per il Risk Manager.

        Args:
            category: Categoria di mercato (es. "SPOT", "USDT-FUTURES").
            symbol: Simbolo specifico opzionale.

        Returns:
            list[dict]: Lista dei metadati degli strumenti contrattuali.
        """
        params: Dict[str, Any] = {"category": category}
        if symbol is not None:
            params["symbol"] = symbol
        res = await self._request(
            "GET", "/api/v3/market/instruments", params=params,
            auth_required=False, rate_limiter_key="public_instruments",
        )
        return res.get("data", [])

    async def get_tickers(
        self,
        category: str,
        symbol: Optional[str] = None,) -> list[dict]:
        """Recupera i dati di ticker (prezzo corrente, volume 24h, ecc.) per categoria o simbolo.

        Args:
            category: Categoria di mercato.
            symbol: Simbolo opzionale.

        Returns:
            list[dict]: Lista dei ticker corrispondenti.
        """
        params: Dict[str, Any] = {"category": category}
        if symbol is not None:
            params["symbol"] = symbol
        res = await self._request(
            "GET", "/api/v3/market/tickers", params=params,
            auth_required=False, rate_limiter_key="public_tickers",
        )
        return res.get("data", [])

    async def get_ticker(self,
        symbol: str,
        category: str = "SPOT",) -> Optional[dict]:
        """Recupera il ticker per un singolo simbolo specifico.

        Args:
            symbol: Simbolo di mercato (es. "BTCUSDT").
            category: Categoria di mercato (default: "SPOT").

        Returns:
            Optional[dict]: Dizionario del ticker o None se non trovato.
        """
        data = await self.get_tickers(category, symbol)
        if not data:
            logger.warning("Ticker non trovato per %s/%s", category, symbol)
            return None
        return data[0]