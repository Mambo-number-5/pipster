import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_async(
    awaitable_factory: Callable[[], Awaitable[T]],
    max_retries: int = 6,
    base_delay: float = 1.3,
    exceptions: type[BaseException] | tuple[type[BaseException], ...] = (
        asyncio.TimeoutError,
        ConnectionError,
        OSError,
    ),
    compute_delay: Callable[[BaseException, int], float | None] | None = None,
) -> T:
    """Esegue una funzione asincrona applicando Exponential Backoff con Jitter.

    Args:
        awaitable_factory: Callable senza argomenti che restituisce un Awaitable.
        max_retries: Numero di retry dopo il tentativo iniziale.
        base_delay: Tempo di attesa base in secondi.
        exceptions: Eccezioni che innescano il retry.
        compute_delay: Callback opzionale `(exception, attempt) -> float | None`.
            Se ritorna un float, sostituisce il backoff calcolato per quel tentativo.
            Se ritorna None, si usa il backoff standard.

    Returns:
        Il valore dell'awaitable eseguito con successo.

    Raises:
        BaseException: L'ultima eccezione se `max_retries` è superato, o subito se
            l'eccezione non è in `exceptions`.
    """
    attempt = 0
    while True:
        try:
            return await awaitable_factory()
        except exceptions as e:
            attempt += 1
            if attempt > max_retries:
                logger.error(
                    "Raggiunto il limite massimo di retry (%d). Errore: %s",
                    max_retries, e,
                )
                raise

            delay: float | None = compute_delay(e, attempt) if compute_delay else None

            if delay is None:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)

            logger.warning(
                "Errore temporaneo (%s). Tentativo %d/%d tra %.2fs...",
                e, attempt, max_retries, delay,
            )
            await asyncio.sleep(cast(float, delay)) # Per il type checker, delay non è None qui

def timeframe_to_ms(timeframe: str) -> int:
    """Utility per convertire la stringa timeframe in millisecondi (es. '15m' -> 900000)."""
    unit = timeframe[-1]
    value = int(timeframe[:-1])
    multipliers = {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    return value * multipliers.get(unit, -1) * 1000

def get_current_timestamp_ms() -> int:
    """Restituisce il tempo di sistema in millisecondi (UTC)."""
    return int(time.time() * 1000)
