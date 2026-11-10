# tests/unit/core/test_ws_client.py
import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, patch
from xmlrpc import client

from websockets.exceptions import WebSocketException
from websockets.protocol import State

from core.ws_client import AsyncWsClient

logger = logging.getLogger(__name__)


class MockWebSocket:
    """Finta connessione WS in puro Python. Supporta:
    - coda di messaggi da emettere
    - tracking dei messaggi inviati (send)
    - simulazione di caduta (fail_first)
    - chiusura esterna via close()
    """
    def __init__(self, messages=None, fail_first=False):
        self.state = State.OPEN
        self.messages = list(messages or [])
        self.fail_first = fail_first
        self.index = 0
        self.sent: list[str] = []
        self._closed = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.state = State.CLOSED

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.fail_first:
            self.fail_first = False
            raise WebSocketException("simulated failure")

        if self.index < len(self.messages):
            msg = self.messages[self.index]
            self.index += 1
            await asyncio.sleep(0.001)
            return msg

        # Nessun altro messaggio: resto in attesa finché non vengo chiuso
        await self._closed.wait()
        raise StopAsyncIteration

    async def send(self, msg):
        self.sent.append(msg)

    async def close(self):
        self.state = State.CLOSED
        self._closed.set()


# =============================================================================
# Lifecycle: start, stop, is_connected
# =============================================================================

class TestAsyncWsClientLifecycle(unittest.IsolatedAsyncioTestCase):

    async def test_start_calls_on_connect(self):
        logger.info("[lifecycle] start() → on_connect invocato")
        on_connect = AsyncMock()
        client = AsyncWsClient("wss://fake", ping_interval=0, silence_timeout=0)
        client.on_connect = on_connect

        mock_ws = MockWebSocket()
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.05)
            await client.stop()

        on_connect.assert_awaited_once()

    async def test_start_is_idempotent(self):
        logger.info("[lifecycle] start() chiamato due volte → una sola connessione")
        connect_calls = {"n": 0}

        def side_effect(*a, **k):
            connect_calls["n"] += 1
            return MockWebSocket()

        client = AsyncWsClient("wss://fake", ping_interval=0, silence_timeout=0)
        with patch("websockets.connect", side_effect=side_effect):
            await client.start()
            await client.start()
            await asyncio.sleep(0.05)
            await client.stop()

        self.assertLessEqual(connect_calls["n"], 1)

    async def test_is_connected_reflects_socket_state(self):
        logger.info("[lifecycle] is_connected segue lo stato del socket")
        client = AsyncWsClient("wss://fake", ping_interval=0, silence_timeout=0)
        self.assertFalse(client.is_connected)

        mock_ws = MockWebSocket()
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.05)
            self.assertTrue(client.is_connected)
            await client.stop()
            await asyncio.sleep(0.05)
            self.assertFalse(client.is_connected)


# =============================================================================
# Messaggi e callback
# =============================================================================

class TestAsyncWsClientMessages(unittest.IsolatedAsyncioTestCase):

    async def test_messages_delivered_to_callback(self):
        logger.info("[messages] on_message riceve i messaggi in ordine")
        received: list[str] = []

        async def on_message(msg):
            received.append(msg)

        client = AsyncWsClient("wss://fake", ping_interval=0, silence_timeout=0)
        client.on_message = on_message

        mock_ws = MockWebSocket(messages=["m1", "m2", "m3"])
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.1)
            await client.stop()

        self.assertEqual(received, ["m1", "m2", "m3"])

    async def test_sync_callback_supported(self):
        logger.info("[messages] Callback sincrona accettata")
        received: list[str] = []
        client = AsyncWsClient("wss://fake", ping_interval=0, silence_timeout=0)
        client.on_message = lambda m: received.append(m)

        mock_ws = MockWebSocket(messages=["a", "b"])
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.1)
            await client.stop()

        self.assertEqual(received, ["a", "b"])

    async def test_exception_in_callback_does_not_crash(self):
        logger.info("[messages] Eccezione in callback → loggata, client continua")
        async def bad_callback(msg):
            raise RuntimeError("boom")

        client = AsyncWsClient("wss://fake", ping_interval=0, silence_timeout=0)
        client.on_message = bad_callback

        mock_ws = MockWebSocket(messages=["m1", "m2"])
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.1)
            await client.stop()
        # Se arriviamo qui senza eccezioni propagate, ok
    
    async def test_on_connect_can_receive_messages(self):
        """
        Verifica che durante on_connect il listen loop sia già attivo:
        i messaggi devono essere processati mentre on_connect è in esecuzione.
        """
        logger.info("[connect] on_connect può ricevere messaggi: listen loop già attivo")
        events: list[str] = []

        async def on_connect():
            events.append("connect_start")
            await asyncio.sleep(0.1)   # simula handshake
            events.append("connect_end")

        async def on_message(msg):
            events.append(f"msg:{msg}")

        client = AsyncWsClient("wss://fake", ping_interval=0, silence_timeout=0)
        client.on_connect = on_connect
        client.on_message = on_message

        mock_ws = MockWebSocket(messages=["m1", "m2"])
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.3)
            await client.stop()

        logger.info("[connect] Sequenza eventi: %s", events)
        # Tutti gli eventi presenti
        self.assertIn("connect_start", events)
        self.assertIn("msg:m1", events)
        self.assertIn("msg:m2", events)
        self.assertIn("connect_end", events)
        # I messaggi sono arrivati PRIMA che on_connect termini
        self.assertLess(events.index("msg:m1"), events.index("connect_end"))
        self.assertLess(events.index("msg:m2"), events.index("connect_end"))


# =============================================================================
# Ping loop
# =============================================================================

class TestAsyncWsClientPing(unittest.IsolatedAsyncioTestCase):

    async def test_ping_sent_periodically(self):
        logger.info("[ping] Con interval piccolo, il ping parte periodicamente")
        client = AsyncWsClient(
            "wss://fake", ping_interval=0.05, ping_payload="ping",
            silence_timeout=0, ping_origin_protocol=False,
        )
        mock_ws = MockWebSocket()
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.2)
            await client.stop()

        self.assertGreaterEqual(len(mock_ws.sent), 2)
        self.assertTrue(all(m == "ping" for m in mock_ws.sent))

    async def test_ping_disabled_if_interval_zero(self):
        logger.info("[ping] ping_interval=0 → nessun ping")
        client = AsyncWsClient("wss://fake", ping_interval=0, silence_timeout=0)
        mock_ws = MockWebSocket()
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.1)
            await client.stop()

        self.assertEqual(mock_ws.sent, [])

    async def test_ping_disabled_if_origin_protocol(self):
        logger.info("[ping] ping_origin_protocol=True → nessun ping applicativo")
        client = AsyncWsClient(
            "wss://fake", ping_interval=0.05, ping_payload="ping",
            silence_timeout=0, ping_origin_protocol=True,
        )
        mock_ws = MockWebSocket()
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.15)
            await client.stop()

        self.assertEqual(mock_ws.sent, [])


# =============================================================================
# Riconnessione
# =============================================================================

class TestAsyncWsClientReconnection(unittest.IsolatedAsyncioTestCase):

    async def test_reconnects_after_websocket_exception(self):
        logger.info("[reconnect] WebSocketException → nuovo tentativo di connessione")
        attempts = {"n": 0}

        def side_effect(*a, **k):
            attempts["n"] += 1
            return MockWebSocket(fail_first=(attempts["n"] == 1))

        client = AsyncWsClient(
            "wss://fake", ping_interval=0, silence_timeout=0,
            base_reconnect_delay=0.01, max_reconnect_delay=0.05,
        )
        with patch("websockets.connect", side_effect=side_effect):
            await client.start()
            await asyncio.sleep(0.2)
            await client.stop()

        self.assertGreaterEqual(attempts["n"], 2)

    async def test_reconnects_after_oserror(self):
        logger.info("[reconnect] OSError → nuovo tentativo")
        attempts = {"n": 0}

        def side_effect(*a, **k):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("network down")
            return MockWebSocket()

        client = AsyncWsClient(
            "wss://fake", ping_interval=0, silence_timeout=0,
            base_reconnect_delay=0.01, max_reconnect_delay=0.05,
        )
        with patch("websockets.connect", side_effect=side_effect):
            await client.start()
            await asyncio.sleep(0.2)
            await client.stop()

        self.assertGreaterEqual(attempts["n"], 2)

    async def test_on_error_called_on_network_error(self):
        logger.info("[reconnect] on_error invocato su errore di rete")
        errors: list[Exception] = []

        async def on_error(e):
            errors.append(e)

        client = AsyncWsClient(
            "wss://fake", ping_interval=0, silence_timeout=0,
            base_reconnect_delay=0.01, max_reconnect_delay=0.05,
        )
        client.on_error = on_error

        with patch("websockets.connect", side_effect=OSError("simulated")):
            await client.start()
            await asyncio.sleep(0.1)
            await client.stop()

        self.assertGreaterEqual(len(errors), 1)
        self.assertIsInstance(errors[0], OSError)

    async def test_stop_terminates_reconnect_loop(self):
        logger.info("[reconnect] stop() ferma il loop e chiude la connessione")
        client = AsyncWsClient("wss://fake", ping_interval=0, silence_timeout=0)
        mock_ws = MockWebSocket()
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.05)
            await client.stop()

        self.assertFalse(client._running)
        self.assertFalse(client.is_connected)


# =============================================================================
# Watchdog del silenzio
# =============================================================================

class TestAsyncWsClientWatchdog(unittest.IsolatedAsyncioTestCase):

    async def test_watchdog_closes_silent_connection(self):
        logger.info("[watchdog] Nessun messaggio → il socket viene chiuso")
        client = AsyncWsClient(
            "wss://fake", ping_interval=0, silence_timeout=0.05,
            base_reconnect_delay=0.05, max_reconnect_delay=0.1,
        )
        mock_ws = MockWebSocket()
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.15)
            await client.stop()

        self.assertEqual(mock_ws.state, State.CLOSED)

    async def test_watchdog_not_started_if_timeout_zero(self):
        logger.info("[watchdog] silence_timeout=0 → socket resta aperto")
        client = AsyncWsClient("wss://fake", ping_interval=0, silence_timeout=0)
        mock_ws = MockWebSocket()
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.1)
            self.assertEqual(mock_ws.state, State.OPEN)
            await client.stop()


# =============================================================================
# send()
# =============================================================================

class TestAsyncWsClientSend(unittest.IsolatedAsyncioTestCase):

    async def test_send_raises_if_not_connected(self):
        logger.info("[send] send() senza connessione → RuntimeError")
        client = AsyncWsClient("wss://fake", ping_interval=0, silence_timeout=0)
        with self.assertRaises(RuntimeError):
            await client.send("hello")

    async def test_send_forwards_to_socket(self):
        logger.info("[send] send() inoltra il messaggio al socket attivo")
        client = AsyncWsClient("wss://fake", ping_interval=0, silence_timeout=0)
        mock_ws = MockWebSocket()
        with patch("websockets.connect", return_value=mock_ws):
            await client.start()
            await asyncio.sleep(0.05)
            await client.send("hello")
            await client.stop()

        self.assertIn("hello", mock_ws.sent)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    unittest.main()