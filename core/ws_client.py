import asyncio
import inspect
import logging
import time
import random


from typing import Any, Awaitable, Callable, Optional, Union

import websockets
from websockets.asyncio.client import ClientConnection as WsConnectionType
from websockets.exceptions import WebSocketException, ConnectionClosedError
from websockets.protocol import State

logger = logging.getLogger(__name__)


class AsyncWsClient:
    """Client WebSocket generico, asincrono e agnostico basato sulla libreria websockets.

    Gestisce automaticamente il ciclo di vita della connessione, le riconnessioni
    con backoff esponenziale e jitter, l'invio di ping periodici e la rilevazione
    di connessioni silenti (zombie) tramite watchdog.

    Attributes:
        url (str): Endpoint WebSocket a cui connettersi.
        ping_interval (Optional[float]): Intervallo in secondi tra i ping di heartbeat.
        ping_payload (Optional[str]): Payload da inviare durante il ping.
        base_reconnect_delay (float): Ritardo iniziale in secondi per il backoff esponenziale.
        max_reconnect_delay (float): Ritardo massimo in secondi per il backoff esponenziale.
        silence_timeout (Optional[float]): Tempo massimo in secondi di inattività prima di forzare il reset.
        on_message (Optional[Callable[..., Union[Awaitable[None], None]]]): Callback eseguita alla ricezione di un messaggio.
        on_connect (Optional[Callable[..., Union[Awaitable[None], None]]]): Callback eseguita a connessione avvenuta.
        on_error (Optional[Callable[..., Union[Awaitable[None], None]]]): Callback eseguita in caso di errore di rete.
    """

    def __init__(
        self,
        url: str,
        ping_interval: float = 30.0,
        ping_payload: str = "ping",
        base_reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 150.0,
        silence_timeout: float = 70.0,
        ping_origin_protocol: bool = False,
    ) -> None:
        """Inizializza il client WebSocket agnostico.

        Args:
            url: URL dell'endpoint WebSocket.
            ping_interval: Frequenza di invio heartbeat in secondi.
            ping_payload: Messaggio di ping testuale personalizzato.
            base_reconnect_delay: Delay iniziale per le riconnessioni in secondi.
            max_reconnect_delay: Delay massimo consentito per le riconnessioni in secondi.
            silence_timeout: Timeout di inattività in secondi per disconnessioni forzate.
            ping_origin_protocol: Indica se utilizzare come heartbeat il protocollo
              originale di websockets o usare un messaggio stringa tramite il metodo 'send'.
        """
        self.url = url
        self.ping_interval = ping_interval
        self.ping_payload = ping_payload
        self.ping_origin_protocol = ping_origin_protocol
        self.base_reconnect_delay = base_reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self._reconnect_attempts = 0

        self.silence_timeout = silence_timeout
        self._last_message_time: float = 0.0

        self._ws: Optional[WsConnectionType] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._running = False

        self.on_message: Optional[Callable[..., Union[Awaitable[None], None]]] = None
        self.on_connect: Optional[Callable[..., Union[Awaitable[None], None]]] = None
        self.on_error: Optional[Callable[..., Union[Awaitable[None], None]]] = None

    async def start(self) -> None:
        """Avvia il task di connessione e il ciclo perpetuo di riconnessione asincrona.

        Se il client è già in esecuzione, il metodo termina immediatamente senza
        effettuare alcuna operazione.
        """
        if self._running:
            return
        self._running = True
        self._reconnect_attempts = 0
        self._listen_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Loop infinito di gestione riconnessioni con Exponential Backoff e Jitter.

        Calcola dinamicamente il tempo di attesa tra un tentativo e l'altro utilizzando
        una progressione esponenziale limitata da `max_reconnect_delay` e aggiungendo
        un fattore casuale (jitter) per evitare colpi sincronizzati (thundering herd).
        """
        attempt = 0
        while self._running:
            start_time = time.monotonic()
            try:
                await self._connect_and_listen()
                # Se _connect_and_listen termina senza eccezioni, connessione chiusa pulitamente
                # Calcoliamo uptime per eventuale reset del backoff
                uptime = time.monotonic() - start_time
                if uptime > 10.0:
                    logger.debug("Connessione durata %.1fs, reset dei tentativi di backoff", uptime)
                    attempt = 0
                else:
                    attempt += 1
            except asyncio.CancelledError:
                break
            except (WebSocketException, OSError, TimeoutError) as e:
                uptime = time.monotonic() - start_time
                if uptime > 10.0:
                    logger.debug("Connessione caduta dopo %.1fs, reset dei tentativi di backoff", uptime)
                    attempt = 0
                else:
                    attempt += 1

                if self.on_error:
                    await self._invoke_callback(self.on_error, e)

                delay = min(self.max_reconnect_delay, self.base_reconnect_delay * (2 ** attempt))
                jitter = random.uniform(0, 0.3 * delay)
                sleep_time = delay + jitter
                logger.warning(
                    "Errore WS (%s): %s. Riconnessione tra %.2fs (Tentativo %d)...",
                    self.url, e, sleep_time, attempt
                )
                await asyncio.sleep(sleep_time)
            else:
                # Nessuna eccezione: connessione chiusa, attendi un attimo e riprova
                if not self._running:
                    break
                logger.debug("Connessione chiusa. Riconnessione immediata...")
                await asyncio.sleep(0.5)

    async def stop(self) -> None:
        """Arresta formalmente il client WebSocket.

        Cancella il task di ascolto/riconnessione principale e chiude in modo pulito
        la connessione di rete attiva.
        """
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
        if self._ws:
            await self._ws.close()
        logger.info("WebSocket client fermato per %s", self.url)

    async def send(self, message: str) -> None:
        """Invia una stringa di dati grezzi sul socket attivo.

        Args:
            message: Stringa (tipicamente un payload JSON serializzato) da trasmettere.

        Raises:
            RuntimeError: Se il WebSocket non risulta nello stato connesso (`is_connected` è False).
        """
        if self.is_connected:
            await self._ws.send(message) #type: ignore
        else:
            raise RuntimeError(f"WebSocket non connesso ({self.url}).")

    @property
    def is_connected(self) -> bool:
        """Indica se il socket è istanziato e attualmente nello stato OPEN."""
        return self._ws is not None and self._ws.state == State.OPEN

    async def _listen_loop(self, ws) -> None:
        """Legge continuamente i messaggi in arrivo dal socket attivo e invoca la callback on_message.

        Args:
            ws: Istanza della connessione WebSocket attiva da cui leggere i frame.
        """
        try:
            logger.info("LISTEN LOOP avviato...")
            async for message in ws:
                self._last_message_time = time.monotonic()
                if self.on_message:
                    await self._invoke_callback(self.on_message, message)
        except asyncio.CancelledError:
            raise

    async def _connect_and_listen(self) -> None:
        """Gestisce l'apertura della connessione di rete e coordina i task di contorno.

        Inizializza i task concorrenti di ascolto messaggi (`_listen_loop`), ping (`_ping_loop`)
        e controllo silenzio (`_silence_watchdog`), invocando contestualmente la callback `on_connect`.
        """

        if self.ping_origin_protocol:
            ping_origin = self.ping_interval
        else:
            ping_origin = None
            
        async with websockets.connect(self.url, ping_interval=ping_origin) as ws:

            self._ws = ws
            self._last_message_time = time.monotonic()
            logger.info("WebSocket connesso: %s", self.url)


            # 1. PRIMA crea i task contestuali
            # LA BEST PRACTICE: Creiamo i task contestuali ALLA CONNESSIONE. Il listen loop è il primo,
            # così on_connect può ricevere messaggi mentre è in esecuzione.
            tasks: list[asyncio.Task] = [asyncio.create_task(self._listen_loop(ws)),]

            if self.ping_interval and self.ping_payload and not self.ping_origin_protocol:
                tasks.append(asyncio.create_task(self._ping_loop()))

            if self.silence_timeout:
                tasks.append(asyncio.create_task(self._silence_watchdog()))

            # 2. POI chiama on_connect (i messaggi sono già in lettura)
            try:
                if self.on_connect:
                    await self._invoke_callback(self.on_connect)
                await tasks[0]
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self._ws = None
                logger.info("WebSocket disconnesso: %s", self.url)

    async def _silence_watchdog(self) -> None:
        """Watchdog di controllo per rilevare connessioni silenti (zombie).

        Monitora il tempo trascorso dall'ultimo messaggio ricevuto (`_last_message_time`).
        Se l'inattività supera `silence_timeout`, forza la chiusura del socket per innescare
        automaticamente la procedura di riconnessione.
        """
        try:
            logger.info("SILENCE WATCHDOG avviato...")
            while True:
                now = time.monotonic()
                time_since_last = now - self._last_message_time
                time_left = self.silence_timeout - time_since_last

                if time_left > 0:
                    await asyncio.sleep(time_left)

                else:
                    logger.warning(
                            "Silenzio WebSocket per %.1fs (> %.1fs). Forzo chiusura per riconnessione.",
                            time_since_last,
                            self.silence_timeout)
                    if self._ws:
                        await self._ws.close()
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Errore nel watchdog del silenzio WS")

    async def _ping_loop(self) -> None:
        """Invia periodicamente il frame di ping testuale configurato."""
        try:
            logger.info("PING LOOP avviato...")
            while True:
                    # Niente if! Se il task è vivo, siamo connessi e il payload esiste.
                    await asyncio.sleep(self.ping_interval)
                    await self.send(self.ping_payload)
        except asyncio.CancelledError:
            pass

    async def _invoke_callback(self, callback: Callable, *args: Any) -> None:
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
        except Exception as e:
            logger.exception("Errore nella callback WS: %s", e)

