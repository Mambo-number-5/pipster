import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib import response

import aiohttp

from core.utils import retry_async
from core.exceptions import RetryableWithDelay, NonRetriableHttpError, RateLimitError, ServerError

logger = logging.getLogger(__name__)



_DEFAULT_RETRY_EXCEPTIONS: Tuple[type, ...] = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
    RateLimitError,
    ServerError,
)

# Status che indicano "aspetta Retry-After e ritenta"
_RETRY_AFTER_STATUSES: frozenset[int] = frozenset({429, 503})

# Status che indicano un ban: non ritentare, ma porta il delay al chiamante
_BAN_STATUSES: frozenset[int] = frozenset({418})

def _compute_delay(exc: BaseException, attempt: int) -> float | None:
    """Ritorna un delay esplicito per eccezioni RetryableWithDelay, altrimenti None."""
    if isinstance(exc, RetryableWithDelay):
        return exc.delay
    return None


class AsyncHttpClient:
    """Client HTTP asincrono basato su aiohttp con gestione thread-safe della sessione e retry automatici."""

    def __init__(self, default_timeout: Optional[aiohttp.ClientTimeout] = None) -> None:
        """Inizializza il client HTTP.

        Args:
            default_timeout: Timeout predefinito per le chiamate HTTP (default: 10s).
        """
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._default_timeout = default_timeout or aiohttp.ClientTimeout(total=10)

    async def _get_session(self) -> aiohttp.ClientSession:
        """Garantisce la creazione di una singola ClientSession legata all'event loop corrente.

        Returns:
            aiohttp.ClientSession: La sessione HTTP attiva.
        """
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(timeout=self._default_timeout)
        return self._session

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        data: Any = None,
        timeout: Optional[aiohttp.ClientTimeout] = None,
        max_retries: int = 3,
        base_delay: float = 1.0,
        retry_exceptions: Tuple[type, ...] = _DEFAULT_RETRY_EXCEPTIONS,
        before_attempt: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Any:
        """Esegue una richiesta HTTP generica incapsulata nella logica di retry.

        Args:
            method: Metodo HTTP (GET, POST, PUT, DELETE, etc.).
            url: Endpoint della richiesta.
            headers: Header HTTP opzionali.
            params: Query parameters opzionali.
            json_data: Payload JSON per POST/PUT.
            data: Form data o body grezzo opzionale.
            timeout: Custom timeout per la singola richiesta.
            max_retries: Numero massimo di tentativi in caso di errore transient.
            base_delay: Ritardo base tra i tentativi in secondi.
            retry_exceptions: Tupla delle eccezioni che innescano il retry.
            before_attempt: Hook opzionale da eseguire prima di ogni tentativo.

        Returns:
            Any: Contenuto JSON decodificato o stringa di testo della risposta.
        """
        async def _do_request() -> Any:
            if before_attempt is not None:
                await before_attempt()
            session = await self._get_session()
            req_timeout = timeout or self._default_timeout

            async with session.request(
                method=method.upper(),
                url=url,
                headers=headers,
                params=params,
                json=json_data,
                data=data,
                timeout=req_timeout,
            ) as response:

                # Legge il body per errori: ClientResponseError
                # non lo porta. Va letto prima di qualsiasi raise,
                # altrimenti il context manager chiude la connessione
                # senza che il body sia mai stato consumato.

                body_preview = ""

                if response.status >= 400:
                    try:
                        body_preview = await response.text()
                    except Exception:
                        body_preview = "<unreadable body>"
                    logger.warning("HTTP %d su %s %s — body: %s",
                        response.status, method.upper(), url, body_preview[:500],)
                
                if response.status in _RETRY_AFTER_STATUSES:
                    retry_after = response.headers.get("Retry-After")
                    
                    if retry_after is not None:
                        delay = float(retry_after)
                        wait_str = f"{delay:.2f}s"
                    else:
                        delay= None
                        wait_str = "assente (backoff standard)"

                    logger.warning("HTTP %d su %s %s — Retry-After=%s",
                        response.status, method.upper(), url, wait_str)
                    raise RateLimitError(delay=delay, status=response.status,
                                            body=body_preview, url=url,)
                elif response.status in _BAN_STATUSES:
                    header = response.headers.get("Retry-After")

                    if header is not None:
                        retry_after = float(header)
                        wait_str = f"{retry_after:.1f}s"
                    else:
                        retry_after = None
                        wait_str = "assente"
                    
                    logger.error(
                        "HTTP %d (ban) su %s %s — Retry-After=%s. Non ritento.",
                        response.status, method.upper(), url, wait_str,
                    )
                    raise NonRetriableHttpError(response.status, body_preview, url, retry_after=retry_after,)

                elif 400 <= response.status < 500:
                    logger.warning("HTTP %d su %s %s (non ritentato)", response.status, method.upper(), url)
                    raise NonRetriableHttpError(response.status, body_preview, url)

                elif 500 <= response.status < 600:
                    logger.warning("HTTP %d su %s %s (ritento)", response.status, method.upper(), url)
                    raise ServerError(response.status, body_preview, url)

                if response.status == 204:
                    return None

                try:
                    return await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    return await response.text()

        return await retry_async(
            _do_request,
            max_retries=max_retries,
            base_delay=base_delay,
            exceptions=retry_exceptions,
            compute_delay=_compute_delay,
        )

    async def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[aiohttp.ClientTimeout] = None,
        max_retries: int = 3,
        base_delay: float = 1.0,
        retry_exceptions: Tuple[type, ...] = _DEFAULT_RETRY_EXCEPTIONS,

        before_attempt: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Any:
        """Esegue una richiesta HTTP GET asincrona con supporto retry.

        Args:
            url: Target URL.
            headers: Header HTTP opzionali.
            params: Query parameters.
            timeout: Custom timeout.
            max_retries: Numero massimo di tentativi.
            base_delay: Delay di base tra i retry.
            retry_exceptions: Eccezioni che innescano il retry.
            before_attempt: Hook pre-tentativo.

        Returns:
            Any: Risposta JSON o testo.
        """
        return await self._request_with_retry(
            "GET",
            url,
            headers=headers,
            params=params,
            timeout=timeout,
            max_retries=max_retries,
            base_delay=base_delay,
            retry_exceptions=retry_exceptions,
            before_attempt=before_attempt,
        )

    async def post(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        data: Any = None,
        timeout: Optional[aiohttp.ClientTimeout] = None,
        max_retries: int = 3,
        base_delay: float = 1.0,
        retry_exceptions: Tuple[type, ...] = _DEFAULT_RETRY_EXCEPTIONS,
        before_attempt: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Any:
        """Esegue una richiesta HTTP POST asincrona con supporto retry.

        Args:
            url: Target URL.
            headers: Header HTTP opzionali.
            json_data: Payload serializzabile in JSON.
            data: Form data o payload binario/testuale.
            timeout: Custom timeout.
            max_retries: Numero massimo di tentativi.
            base_delay: Delay di base tra i retry.
            retry_exceptions: Eccezioni che innescano il retry.
            before_attempt: Hook pre-tentativo.

        Returns:
            Any: Risposta JSON o testo.
        """
        return await self._request_with_retry(
            "POST",
            url,
            headers=headers,
            json_data=json_data,
            data=data,
            timeout=timeout,
            max_retries=max_retries,
            base_delay=base_delay,
            retry_exceptions=retry_exceptions,
            before_attempt=before_attempt,
        )

    async def delete(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: Optional[aiohttp.ClientTimeout] = None,
        max_retries: int = 3,
        base_delay: float = 1.0,
        retry_exceptions: Tuple[type, ...] = _DEFAULT_RETRY_EXCEPTIONS,
        before_attempt: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Any:
        """Esegue una richiesta HTTP DELETE asincrona con supporto retry.

        Args:
            url: Target URL.
            headers: Header HTTP opzionali.
            params: Query parameters.
            json_data: Payload JSON facoltativo.
            timeout: Custom timeout.
            max_retries: Numero massimo di tentativi.
            base_delay: Delay di base tra i retry.
            retry_exceptions: Eccezioni che innescano il retry.
            before_attempt: Hook pre-tentativo.

        Returns:
            Any: Risposta JSON o testo.
        """
        return await self._request_with_retry(
            "DELETE",
            url,
            headers=headers,
            params=params,
            json_data=json_data,
            timeout=timeout,
            max_retries=max_retries,
            base_delay=base_delay,
            retry_exceptions=retry_exceptions,
            before_attempt=before_attempt,
        )

    async def close(self) -> None:
        """Chiude la sessione aiohttp attiva rilasciando le risorse di rete correlate."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("AsyncHttpClient: sessione chiusa")