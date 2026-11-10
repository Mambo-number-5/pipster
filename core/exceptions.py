"""Eccezioni condivise dal transport e dagli adapter exchange."""


class RetryableWithDelay(Exception):
    """Indica 'aspetta N secondi e ritenta' (delay indicato dal server).

    Usata da:
    - `RateLimitError` (HTTP 429 con Retry-After) nel transport
    - Errori business-specifici degli exchange che vogliono un delay esplicito

    Il `retry_async` riconosce questa classe e usa `.delay` come override
    del backoff calcolato. Se un'eccezione non estende questa classe, si usa
    il backoff standard.
    """

    def __init__(self, delay: float | None = None, message: str = ""):
        message_delay = f"retry after {delay}s" if delay is not None else "generic retrayable"
        super().__init__(message or message_delay)
        self.delay = delay


class HttpStatusError(Exception):
    """Errore HTTP con status, body e url della risposta.

    Base comune per tutti gli errori HTTP non-2xx. Il body è troncato a 500
    caratteri per evitare payload abnormi. Permette all'adapter di parsare
    il codice business dell'exchange (es. Bitget 40006/40008/40009).
    """

    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} su {url}")
        self.status = status
        self.body = body
        self.url = url


class NonRetriableHttpError(HttpStatusError):
    """HTTP 4xx o 418 (ban): non ritentabile.

    `retry_after` è opzionale: presente su 418 (ban Binance), assente sugli
    altri 4xx. Esposto per informare il chiamante della durata della penalità,
    non per il retry loop.
    """

    def __init__(self, status: int, body: str, url: str, retry_after: float | None = None):
        super().__init__(status, body, url)
        self.retry_after = retry_after


class ServerError(HttpStatusError):
    """HTTP 5xx: transient, ritentabile con backoff standard."""


class RateLimitError(RetryableWithDelay):
    """HTTP 429/503 con Retry-After opzionale, ritentabile.

    Espone sia `.delay` (per `retry_async` via `compute_delay`) sia
    `.status`/`.body`/`.url` (per diagnostica e per adapter che volessero
    parsare il body).
    """

    def __init__(self, delay: float | None, status: int, body: str, url: str):
        super().__init__(delay=delay, message=f"HTTP {status} su {url}")
        self.status = status
        self.body = body
        self.url = url