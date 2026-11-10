# tests/unit/exchanges/bitget/test_bitget_ws.py
import logging
import json
import unittest
from unittest.mock import AsyncMock, MagicMock
from websockets.protocol import State

from exchanges.bitget.bitget_ws import SubscribeReq, BitgetWsClient, WsEnvelope

logger = logging.getLogger(__name__)


def _make_client(**overrides) -> tuple[BitgetWsClient, AsyncMock]:
    """Costruisce un BitgetWsClient con transport mockato e socket 'connesso'."""
    defaults = {"silence_timeout": 0.0, "private": False}
    defaults.update(overrides)
    client = BitgetWsClient(**defaults)
    send_mock: AsyncMock = AsyncMock()
    client._client.send = send_mock
    client._client._ws = MagicMock(state=State.OPEN)
    return client, send_mock

# =============================================================
# Creazione dei SubscribeReq
# =============================================================

class TestSubscribeReqConstruction(unittest.TestCase):

    def test_kline(self) -> None:
        logger.info("[kline] Costruzione richiesta kline spot/BTCUSDT/1m")
        req = SubscribeReq.kline("spot", "BTCUSDT", "1m")
        logger.info("[kline] Risultato: topic=%s symbol=%s interval=%s",
                    req.topic, req.symbol, req.interval)
        self.assertEqual(req.inst_type, "spot")
        self.assertEqual(req.topic, "kline")
        self.assertEqual(req.symbol, "BTCUSDT")
        self.assertEqual(req.interval, "1m")

    def test_ticker(self) -> None:
        logger.info("[ticker] Costruzione richiesta ticker senza interval")
        req = SubscribeReq.ticker("spot", "BTCUSDT")
        logger.info("[ticker] Risultato: topic=%s interval=%s", req.topic, req.interval)
        self.assertEqual(req.topic, "ticker")
        self.assertIsNone(req.interval)

    def test_books_default(self) -> None:
        logger.info("[books] Costruzione books senza specificare il livello")
        req = SubscribeReq.books("usdt-futures", "BTCUSDT")
        logger.info("[books] Risultato: topic=%s", req.topic)
        self.assertEqual(req.topic, "books")

    def test_books_with_levels(self) -> None:
        logger.info("[books] Costruzione books con livelli 1, 5, 50")
        cases = [(1, "books1"), (5, "books5"), (50, "books50")]
        for levels, expected in cases:
            req = SubscribeReq.books("usdt-futures", "BTCUSDT", levels=levels)
            logger.info("[books] levels=%d → topic=%s (atteso %s)",
                        levels, req.topic, expected)
            self.assertEqual(req.topic, expected)

    def test_public_trade(self) -> None:
        logger.info("[publicTrade] Costruzione richiesta publicTrade")
        req = SubscribeReq.public_trade("usdt-futures", "BTCUSDT")
        logger.info("[publicTrade] Risultato: topic=%s symbol=%s",
                    req.topic, req.symbol)
        self.assertEqual(req.topic, "publicTrade")
        self.assertEqual(req.symbol, "BTCUSDT")

    def test_liquidation_has_no_symbol(self) -> None:
        logger.info("[liquidation] Costruzione richiesta senza symbol")
        req = SubscribeReq.liquidation("usdt-futures")
        logger.info("[liquidation] Risultato: topic=%s symbol=%s interval=%s",
                    req.topic, req.symbol, req.interval)
        self.assertEqual(req.topic, "liquidation")
        self.assertIsNone(req.symbol)
        self.assertIsNone(req.interval)

# =============================================================
# Da SubscribeReq a dict
# =============================================================

class TestSubscribeReqToDict(unittest.TestCase):

    def test_kline_payload(self) -> None:
        logger.info("[to_dict:kline] Verifica payload completo con interval")
        req = SubscribeReq.kline("spot", "BTCUSDT", "1D")
        payload = req.to_dict()
        logger.info("[to_dict:kline] Payload: %s", payload)
        self.assertEqual(payload, {
            "instType": "spot",
            "topic": "kline",
            "symbol": "BTCUSDT",
            "interval": "1D",
        })

    def test_ticker_payload_omits_interval(self) -> None:
        logger.info("[to_dict:ticker] Verifica che interval sia omesso")
        req = SubscribeReq.ticker("spot", "BTCUSDT")
        payload = req.to_dict()
        logger.info("[to_dict:ticker] Payload: %s", payload)
        self.assertNotIn("interval", payload)

    def test_liquidation_payload_omits_symbol(self) -> None:
        logger.info("[to_dict:liquidation] Verifica che symbol sia omesso")
        req = SubscribeReq.liquidation("usdt-futures")
        payload = req.to_dict()
        logger.info("[to_dict:liquidation] Payload: %s", payload)
        self.assertEqual(payload, {"instType": "usdt-futures", "topic": "liquidation"})
        self.assertNotIn("symbol", payload)


# =============================================================
# Da dict a SubscribeReq
# =============================================================

class TestSubscribeReqFromArg(unittest.TestCase):

    def test_from_kline_arg(self) -> None:
        logger.info("[from_arg:kline] Ricostruzione da arg di push V3")
        arg = {"instType": "spot", "topic": "kline", "symbol": "BTCUSDT", "interval": "1D"}
        req = SubscribeReq.from_arg(arg)
        logger.info("[from_arg:kline] Arg=%s → Req=%s", arg, req)
        self.assertEqual(req, SubscribeReq.kline("spot", "BTCUSDT", "1D"))

    def test_from_ticker_arg(self) -> None:
        logger.info("[from_arg:ticker] Ricostruzione da arg senza interval")
        arg = {"instType": "spot", "topic": "ticker", "symbol": "BTCUSDT"}
        req = SubscribeReq.from_arg(arg)
        logger.info("[from_arg:ticker] Arg=%s → Req=%s", arg, req)
        self.assertEqual(req, SubscribeReq.ticker("spot", "BTCUSDT"))

    def test_from_liquidation_arg_without_symbol(self) -> None:
        logger.info("[from_arg:liquidation] Arg senza symbol (caso liquidation)")
        arg = {"instType": "usdt-futures", "topic": "liquidation"}
        req = SubscribeReq.from_arg(arg)
        logger.info("[from_arg:liquidation] Arg=%s → Req=%s", arg, req)
        self.assertEqual(req, SubscribeReq.liquidation("usdt-futures"))
        self.assertIsNone(req.symbol)

    def test_roundtrip_to_dict_from_arg(self) -> None:
        logger.info("[roundtrip] to_dict → from_arg deve restituire la stessa istanza")
        original = SubscribeReq.kline("spot", "BTCUSDT", "1m")
        payload = original.to_dict()
        rebuilt = SubscribeReq.from_arg(payload)
        logger.info("[roundtrip] Originale=%s, Ricostruita=%s", original, rebuilt)
        self.assertEqual(original, rebuilt)

# =============================================================
# Hashability di SubscribeReq
# =============================================================

class TestSubscribeReqHashability(unittest.TestCase):

    def test_usable_as_dict_key(self) -> None:
        logger.info("[hash] Verifica utilizzo come chiave di dict")
        registry: dict[SubscribeReq, str] = {}
        req = SubscribeReq.kline("spot", "BTCUSDT", "1m")
        registry[req] = "callback"
        logger.info("[hash] Registry: %s", registry)
        self.assertEqual(registry[req], "callback")

    def test_equal_instances_hash_equal(self) -> None:
        logger.info("[hash] Istanze uguali → hash uguali")
        a = SubscribeReq.kline("spot", "BTCUSDT", "1m")
        b = SubscribeReq.kline("spot", "BTCUSDT", "1m")
        logger.info("[hash] hash(a)=%d, hash(b)=%d", hash(a), hash(b))
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))

    def test_different_instances_are_different_keys(self) -> None:
        logger.info("[hash] Istanze diverse (interval) → chiavi diverse")
        registry: dict[SubscribeReq, str] = {}
        registry[SubscribeReq.kline("spot", "BTCUSDT", "1m")] = "a"
        registry[SubscribeReq.kline("spot", "BTCUSDT", "5m")] = "b"
        logger.info("[hash] Registry ha %d chiavi distinte", len(registry))
        self.assertEqual(len(registry), 2)

    def test_frozen_rejects_mutation(self) -> None:
        logger.info("[frozen] Il tentativo di mutazione deve sollevare")
        req = SubscribeReq.kline("spot", "BTCUSDT", "1m")
        with self.assertRaises(Exception):
            req.topic = "ticker"  # type: ignore[misc]
        logger.info("[frozen] Mutazione correttamente rifiutata")


# =============================================================================
# Login
# =============================================================================

class TestBitgetWsClientLogin(unittest.IsolatedAsyncioTestCase):

    async def test_login_success(self):
        logger.info("[login] Frame inviato, server risponde code=0")
        client, send_mock = _make_client(api_key="k", api_secret="s", passphrase="p")

        async def fake_send(payload):
            # Simula la risposta del server al frame di login
            client._handle_login_event(WsEnvelope(event="login", code="0", msg=""))
        client._send_payload = fake_send

        result = await client.login(timeout=1.0)
        logger.info("[login] Risultato=%s, _login_ok=%s", result, client._login_ok)
        self.assertTrue(result)
        self.assertTrue(client._login_ok)

    async def test_login_failure(self):
        logger.info("[login] Server risponde con code!=0")
        client, send_mock = _make_client(api_key="k", api_secret="s", passphrase="p")

        async def fake_send(payload):
            client._handle_login_event(WsEnvelope(event="login", code="30005", msg="err"))
        client._send_payload = fake_send

        result = await client.login(timeout=1.0)
        logger.info("[login] Risultato=%s, _login_ok=%s", result, client._login_ok)
        self.assertFalse(result)
        self.assertFalse(client._login_ok)

    async def test_login_timeout(self):
        logger.info("[login] Nessuna risposta entro timeout")
        client, send_mock = _make_client(api_key="k", api_secret="s", passphrase="p")

        async def fake_send(payload):
            pass   # nessuna risposta
        client._send_payload = fake_send

        result = await client.login(timeout=0.05)
        logger.info("[login] Risultato=%s (atteso False)", result)
        self.assertFalse(result)

    async def test_login_without_credentials_raises(self):
        logger.info("[login] Credenziali assenti → ValueError")
        client, send_mock = _make_client()
        with self.assertRaises(ValueError):
            await client.login()

    async def test_login_payload_shape(self):
        logger.info("[login] Verifica forma del payload inviato")
        client, send_mock = _make_client(api_key="mykey", api_secret="mysecret", passphrase="mypass")

        captured = {}
        async def capture(payload):
            captured.update(payload)
            client._handle_login_event(WsEnvelope(event="login", code="0"))
        client._send_payload = capture

        await client.login(timeout=1.0)
        logger.info("[login] Payload catturato: %s", captured)
        self.assertEqual(captured["op"], "login")
        self.assertEqual(captured["args"][0]["apiKey"], "mykey")
        self.assertEqual(captured["args"][0]["passphrase"], "mypass")
        self.assertIn("sign", captured["args"][0])
        self.assertIn("timestamp", captured["args"][0])

# =============================================================================
# Subscribe
# =============================================================================

class TestBitgetWsClientSubscribe(unittest.IsolatedAsyncioTestCase):

    async def test_subscribe_sends_payload_when_connected(self):
        logger.info("[subscribe] Canale connesso → payload inviato")
        client, send_mock = _make_client()
        reqs = [SubscribeReq.kline("spot", "BTCUSDT", "1m")]

        await client.subscribe(reqs, callback=lambda m: None)

        logger.info("[subscribe] send chiamato %d volta/e", send_mock.call_count)
        self.assertEqual(send_mock.call_count, 1)
        sent = json.loads(send_mock.call_args[0][0])
        self.assertEqual(sent["op"], "subscribe")
        self.assertEqual(sent["args"][0]["topic"], "kline")

    async def test_subscribe_registers_callback(self):
        logger.info("[subscribe] Callback registrata nel registry")
        client, send_mock = _make_client()
        cb = lambda m: None
        req = SubscribeReq.kline("spot", "BTCUSDT", "1m")
        await client.subscribe([req], callback=cb)
        self.assertIn(req, client.subscriptions)
        self.assertIs(client.subscriptions[req], cb)

    async def test_subscribe_skips_send_when_not_connected(self):
        logger.info("[subscribe] Canale disconnesso → nessun send")
        client, send_mock = _make_client()
        client._client._ws = None   # is_connected → False

        req = SubscribeReq.ticker("spot", "BTCUSDT")
        await client.subscribe([req], callback=lambda m: None)

        self.assertEqual(send_mock.call_count, 0)
        # Ma il registry è comunque aggiornato (per il replay su riconnessione)
        self.assertIn(req, client.subscriptions)

    async def test_subscribe_raises_over_1000_channels(self):
        logger.info("[subscribe] Richiesta supera il limite 1000 canali")
        client, send_mock = _make_client()
        # Riempio manualmente il registry
        for i in range(1000):
            client.subscriptions[SubscribeReq.ticker("spot", f"SYM{i}")] = lambda m: None

        with self.assertRaises(ValueError):
            await client.subscribe(
                [SubscribeReq.ticker("spot", "BTCUSDT")],
                callback=lambda m: None,
            )

# =============================================================================
# Unsubscribe
# =============================================================================

class TestBitgetWsClientUnsubscribe(unittest.IsolatedAsyncioTestCase):

    async def test_unsubscribe_sends_payload_and_cleans_registry(self):
        logger.info("[unsubscribe] Payload inviato e registry pulito")
        client, send_mock = _make_client()
        req = SubscribeReq.kline("spot", "BTCUSDT", "1m")
        await client.subscribe([req], callback=lambda m: None)

        await client.unsubscribe([req])

        self.assertNotIn(req, client.subscriptions)
        last_payload = json.loads(send_mock.call_args[0][0])
        self.assertEqual(last_payload["op"], "unsubscribe")

    async def test_unsubscribe_empty_list_is_noop(self):
        logger.info("[unsubscribe] Lista vuota → nessun send")
        client, send_mock  = _make_client()
        await client.unsubscribe([])
        self.assertEqual(send_mock.call_count, 0)

# =============================================================================
# _handle_raw_message
# =============================================================================

class TestBitgetWsClientHandleRawMessage(unittest.IsolatedAsyncioTestCase):

    async def test_pong_ignored(self):
        logger.info("[handle] 'pong' ignorato")
        client, send_mock  = _make_client()
        await client._handle_raw_message("pong")   # nessuna eccezione

    async def test_login_event_sets_flag(self):
        logger.info("[handle] event=login code=0 → _login_ok=True")
        client, send_mock  = _make_client()
        await client._handle_raw_message(json.dumps({"event": "login", "code": "0"}))
        self.assertTrue(client._login_ok)
        self.assertTrue(client._login_event.is_set())

    async def test_login_event_failure(self):
        logger.info("[handle] event=login code!=0 → _login_ok=False")
        client, send_mock  = _make_client()
        await client._handle_raw_message(json.dumps({"event": "login", "code": "30005"}))
        self.assertFalse(client._login_ok)

    async def test_error_event_invokes_listener(self):
        logger.info("[handle] event=error → error_listener invocato")
        client, send_mock  = _make_client()
        listener = AsyncMock()
        client.error_listener = listener

        await client._handle_raw_message(
            json.dumps({"event": "error", "code": "400", "msg": "bad"})
        )
        listener.assert_awaited_once()

    async def test_error_event_unblocks_pending_login(self):
        logger.info("[handle] error durante login pending → sblocca con KO")
        client, send_mock  = _make_client()
        client._pending_login = True
        client._login_event.clear()

        await client._handle_raw_message(
            json.dumps({"event": "error", "code": "400", "msg": "bad"})
        )
        self.assertTrue(client._login_event.is_set())
        self.assertFalse(client._login_ok)

    async def test_subscribe_confirmation_logged(self):
        logger.info("[handle] event=subscribe → log, nessuna callback")
        client, send_mock  = _make_client()
        cb = AsyncMock()
        req = SubscribeReq.kline("spot", "BTCUSDT", "1m")
        client.subscriptions[req] = cb

        await client._handle_raw_message(json.dumps({
            "event": "subscribe",
            "arg": req.to_dict(),
        }))
        cb.assert_not_awaited()

    async def test_data_routes_to_registered_callback(self):
        logger.info("[handle] Dati con arg registrato → callback invocata")
        client, send_mock  = _make_client()
        cb = AsyncMock()
        req = SubscribeReq.kline("spot", "BTCUSDT", "1m")
        client.subscriptions[req] = cb

        raw = json.dumps({
            "data": [{"start": "1", "open": "1", "high": "1", "low": "1", "close": "1"}],
            "arg": req.to_dict(),
            "action": "snapshot",
            "ts": 1234567890,
        })
        await client._handle_raw_message(raw)
        cb.assert_awaited_once_with(raw)

    async def test_data_to_unknown_channel_is_ignored(self):
        logger.info("[handle] Dati per canale non sottoscritto → nessuna callback")
        client, send_mock  = _make_client()
        cb = AsyncMock()
        other_req = SubscribeReq.kline("spot", "ETHUSDT", "1m")
        client.subscriptions[other_req] = cb

        raw = json.dumps({
            "data": [{}],
            "arg": SubscribeReq.kline("spot", "BTCUSDT", "1m").to_dict(),
            "action": "snapshot",
        })
        await client._handle_raw_message(raw)
        cb.assert_not_awaited()

    async def test_malformed_arg_is_logged_no_exception(self):
        logger.info("[handle] arg senza instType → warning, nessuna eccezione")
        client, send_mock  = _make_client()
        raw = json.dumps({"data": [{}], "arg": {"topic": "kline"}})
        await client._handle_raw_message(raw)   # nessuna eccezione propagata

    async def test_invalid_json_is_logged_no_exception(self):
        logger.info("[handle] JSON invalido → warning, nessuna eccezione")
        client, send_mock  = _make_client()
        await client._handle_raw_message("not json at all")   # nessuna eccezione

# =============================================================================
# _on_reconnect
# =============================================================================

class TestBitgetWsClientReconnect(unittest.IsolatedAsyncioTestCase):

    async def test_reconnect_logs_in_if_credentials(self):
        logger.info("[reconnect] Con credenziali → login() invocato")
        client, send_mock = _make_client(api_key="k", api_secret="s", passphrase="p")
        client.login = AsyncMock(return_value=True)

        await client._on_reconnect()
        client.login.assert_awaited_once()

    async def test_reconnect_skips_login_without_credentials(self):
        logger.info("[reconnect] Senza credenziali → login() non invocato")
        client, send_mock  = _make_client()
        client.login = AsyncMock()

        await client._on_reconnect()
        client.login.assert_not_awaited()

    async def test_reconnect_replays_subscriptions(self):
        logger.info("[reconnect] Replay delle sottoscrizioni attive")
        client, send_mock  = _make_client()
        req1 = SubscribeReq.kline("spot", "BTCUSDT", "1m")
        req2 = SubscribeReq.ticker("spot", "ETHUSDT")
        client.subscriptions[req1] = lambda m: None
        client.subscriptions[req2] = lambda m: None

        await client._on_reconnect()

        self.assertEqual(send_mock.call_count, 1)
        payload = json.loads(send_mock.call_args[0][0])
        self.assertEqual(payload["op"], "subscribe")
        self.assertEqual(len(payload["args"]), 2)

# =============================================================================
# _invoke_callback
# =============================================================================

class TestBitgetWsClientInvokeCallback(unittest.IsolatedAsyncioTestCase):

    async def test_async_callback_awaited(self):
        logger.info("[invoke] Callback async")
        client, send_mock  = _make_client()
        cb = AsyncMock()
        await client._invoke_callback(cb, "x", "y")
        cb.assert_awaited_once_with("x", "y")

    async def test_sync_callback_called(self):
        logger.info("[invoke] Callback sync")
        client, send_mock  = _make_client()
        captured = []
        cb = lambda *args: captured.append(args)
        await client._invoke_callback(cb, "x", "y")
        self.assertEqual(captured, [("x", "y")])

    async def test_exception_in_callback_does_not_propagate(self):
        logger.info("[invoke] Eccezione in callback → loggata, non propagata")
        client, send_mock  = _make_client()
        async def bad():
            raise RuntimeError("boom")
        await client._invoke_callback(bad)   # nessuna eccezione


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    unittest.main()