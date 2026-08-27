import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

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
) -> T:
    """Esegue una funzione asincrona applicando una strategia di Exponential Backoff con Jitter.

    Args:
        awaitable_factory: Funzione/callable priva di argomenti che restituisce un Awaitable 
            (es. `lambda: client.get_data()`).
        max_retries: Numero massimo di tentativi prima di rilanciare l'eccezione.
        base_delay: Tempo di attesa base in secondi per il calcolo dell'exponential backoff.
        exceptions: Singola eccezione o tupla di eccezioni da intercettare per eseguire il retry.

    Returns:
        Il valore restituito dall'awaitable eseguito con successo.

    Raises:
        BaseException: Rilancia l'ultima eccezione intercettata se viene superato `max_retries`, 
            oppure immediatamente se si verifica un errore non incluso in `exceptions`.
    """
    attempt = 0
    while True:
        try:
            return await awaitable_factory()
        except exceptions as e:
            attempt += 1
            if attempt > max_retries:
                logger.error(
                    "Raggiunto il limite massimo di tentativi (%d). Errore: %s",
                    max_retries,
                    e,
                )
                raise

            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                "Errore temporaneo (%s). Tentativo %d/%d tra %.2fs...",
                e,
                attempt,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)

def timeframe_to_ms(timeframe: str) -> int:
    """Utility per convertire la stringa timeframe in millisecondi (es. '15m' -> 900000)."""
    unit = timeframe[-1]
    value = int(timeframe[:-1])
    multipliers = {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    return value * multipliers.get(unit, -1) * 1000