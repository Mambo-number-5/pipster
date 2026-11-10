# tests/unit/exchanges/bitget/test_bitget_rest.py
import base64
import hashlib
import hmac
import json
import logging
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.exceptions import NonRetriableHttpError
from exchanges.bitget.bitget_rest import (
    BitgetApiError,
    BitgetAuthError,
    BitgetRestClient,
    BitgetRetryableError,
    BitgetTimestampExpired,
    _ENDPOINT_BUCKETS,
    _BitgetClockSync,
    init_rate_limiters,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(demo: bool = False, rate_limiter=None) -> BitgetRestClient:
    client = BitgetRestClient(api_key="test_key", secret_key="test_secret", passphrase="test_pass",
                            demo=demo, rate_limiter=rate_limiter,)
    client._ensure_time_sync = AsyncMock()   # evita sync reale nei test
    return client



def _ok_response(data=None) -> dict:
    return {"code": "00000", "msg": "success", "data": data or []}


def _error_response(code: str, msg: str = "err") -> dict:
    return {"code": code, "msg": msg}


def _fake_redis():
    fake = MagicMock()
    fake.register_script = MagicMock(return_value=AsyncMock(return_value=(1, [])))
    return fake


def _fake_time_time(first_value: float, second_value: float, increment: float = 0.0):
    """Generatore che simula time.time() usa due valori ed eventualmente dal terzo valore c'è un incremento opzionale."""
    yield first_value
    yield second_value
    while True:
        second_value += increment
        yield second_value


async def _fake_get(url, headers=None, before_attempt=None, **kw):
    if before_attempt:
        await before_attempt()
    return _ok_response()


# ---------------------------------------------------------------------------
# Area A — Firma e costruzione query
# ---------------------------------------------------------------------------

class TestBitgetFirma(unittest.TestCase):

    def test_build_query_for_sign_is_not_urlencoded(self):
        logger.info("[firma] query per firma NON urlencoded")
        params = {"symbol": "BTCUSDT", "interval": "1m"}
        q = BitgetRestClient._build_query_for_sign(params)
        logger.info("[firma] query=%s", q)
        self.assertEqual(q, "interval=1m&symbol=BTCUSDT")
        self.assertNotIn("%", q)

    def test_build_query_for_url_is_urlencoded(self):
        logger.info("[firma] query per URL urlencoded")
        params = {"symbol": "BTC USDT"}   # spazio forzato
        q = BitgetRestClient._build_query_for_url(params)
        logger.info("[firma] query=%s", q)
        self.assertEqual(q, "symbol=BTC+USDT")

    def test_sign_query_and_url_query_differ_on_special_chars(self):
        logger.info("[firma] con caratteri speciali, le due query differiscono")
        params = {"symbol": "BTC USDT", "x": "a&b"}
        sign_q = BitgetRestClient._build_query_for_sign(params)
        url_q = BitgetRestClient._build_query_for_url(params)
        logger.info("[firma] sign=%s | url=%s", sign_q, url_q)
        self.assertNotEqual(sign_q, url_q)
        self.assertIn(" ", sign_q)
        self.assertIn("&", sign_q)   # l'& è dentro il valore, non codificato

    def test_generate_signature_hmac_sha256(self):
        logger.info("[firma] HMAC-SHA256 deterministica")
        client = _make_client()
        ts = "1700000000000"
        method = "GET"
        path = "/api/v3/market/candles"
        query = "category=SPOT&symbol=BTCUSDT"
        sig = client._generate_signature(ts, method, path, query, "")

        message = ts + method + path + "?" + query
        expected = base64.b64encode(
            hmac.new(
                b"test_secret", message.encode("utf-8"), hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        logger.info("[firma] sig=%s", sig)
        self.assertEqual(sig, expected)

    def test_build_headers_contains_required_fields(self):
        logger.info("[firma] headers contengono tutti i campi Bitget")
        client = _make_client()
        with patch.object(client._clock, "now_ms", return_value=1700000000000):
            headers = client._build_headers("GET", "/api/v3/market/candles", "a=1", "")
        logger.info("[firma] headers keys: %s", sorted(headers.keys()))
        for k in ("Content-Type", "ACCESS-KEY", "ACCESS-SIGN",
                  "ACCESS-TIMESTAMP", "ACCESS-PASSPHRASE", "locale"):
            self.assertIn(k, headers)
        self.assertEqual(headers["ACCESS-TIMESTAMP"], "1700000000000")

    def test_build_headers_demo_adds_paptrading(self):
        logger.info("[firma] demo=True aggiunge paptrading: 1")
        client = _make_client(demo=True)
        with patch.object(client._clock, "now_ms", return_value=1700000000000):
            headers = client._build_headers("GET", "/api/v3/market/candles")
        self.assertEqual(headers.get("paptrading"), "1")


# ---------------------------------------------------------------------------
# Area B — Clock sync
# ---------------------------------------------------------------------------

class TestBitgetClockSync(unittest.IsolatedAsyncioTestCase):

    async def test_sync_ntp_formula_with_request_time(self):
        logger.info("[clock] formula NTP: t0=1000, t1=1200, t2=1250, t3=1300")
        clock = _BitgetClockSync()

        async def fetch():
            # Simula: locale parte a t0=1000, server riceve t1=1200,
            # server risponde con t2=1250, locale ritorna a t3=1300.
            return {
                "requestTime": 1200,
                "data": {"serverTime": "1250"},
            }

        # Patch time.time per controllare t0 e t3
        with patch("time.time", side_effect=_fake_time_time(1.000, 1.300, increment=0.1)):
            await clock.sync(fetch)
        # offset = ((t1-t0)+(t2-t3))/2 = ((200)+(-50))/2 = 75
        logger.info("[clock] offset=%dms", clock.offset_ms)
        self.assertEqual(clock.offset_ms, 75)
        self.assertTrue(clock.is_synced)

    async def test_sync_fallback_midpoint_without_request_time(self):
        logger.info("[clock] senza requestTime, fallback mid-point")
        clock = _BitgetClockSync()

        async def fetch():
            return {"data": {"serverTime": "1500"}}

        with patch("time.time", side_effect=_fake_time_time(1.000, 1.200)):
            await clock.sync(fetch)
        # mid = (1000+1200)/2 = 1100, offset = 1500 - 1100 = 400
        logger.info("[clock] offset=%dms", clock.offset_ms)
        self.assertEqual(clock.offset_ms, 400)

    async def test_now_ms_applies_offset(self):
        logger.info("[clock] now_ms = local + offset")
        clock = _BitgetClockSync()
        clock.offset_ms = 500
        with patch("time.time", return_value=1.000):
            logger.info("[clock] now_ms=%d", clock.now_ms())
            self.assertEqual(clock.now_ms(), 1500)

    async def test_needs_refresh_before_and_after_sync(self):
        logger.info("[clock] needs_refresh segue lo stato di sync")
        clock = _BitgetClockSync()
        self.assertTrue(clock.needs_refresh())

        async def fetch():
            return {"requestTime": 1000, "data": {"serverTime": "1000"}}

        with patch("time.time", side_effect=_fake_time_time(1.000, 1.000)):
            await clock.sync(fetch)
        self.assertFalse(clock.needs_refresh())

        # Simula trascorrere 31 minuti
        clock.last_sync_monotonic -= 31 * 60
        self.assertTrue(clock.needs_refresh())

    async def test_ensure_time_sync_triggers_on_first_call(self):
        logger.info("[clock] _ensure_time_sync invoca sync alla prima chiamata")
        client = BitgetRestClient(api_key="test", secret_key="test", passphrase="test")
        with patch.object(client, "sync_server_time", AsyncMock()) as mock_sync:
            await client._ensure_time_sync()
        mock_sync.assert_awaited_once()


# ---------------------------------------------------------------------------
# Area C — _request e gestione errori
# ---------------------------------------------------------------------------

class TestBitgetRequestErrors(unittest.IsolatedAsyncioTestCase):

    async def test_request_success_returns_data(self):
        logger.info("[request] code=00000 → ritorna res")
        client = _make_client()
        with patch.object(client.http, "get", AsyncMock(return_value=_ok_response([["1"]]))):
            res = await client._request("GET", "/api/v3/market/candles")
        logger.info("[request] res=%s", res)
        self.assertEqual(res["code"], "00000")
        self.assertEqual(res["data"], [["1"]])

    async def test_request_40008_resync_and_retry_succeeds(self):
        logger.info("[request] 40008 → sync → secondo tentativo 00000")
        client = _make_client()
        client._clock._synced = True   # evita sync iniziale
        with patch.object(client.http, "get", AsyncMock(side_effect=[_error_response("40008"), _ok_response({"ok": True}),])), \
              patch.object(client, "_ensure_time_sync", AsyncMock()), \
              patch.object(client, "sync_server_time", AsyncMock()) as mock_sync:
            res = await client._request(
                "GET", "/api/v3/market/candles", auth_required=True,
            )
        logger.info("[request] sync chiamato %d volta/e, res=%s",
                    mock_sync.await_count, res)
        self.assertEqual(res["code"], "00000")
        mock_sync.assert_awaited_once()

    async def test_request_40008_exhausted_raises_timestamp_expired(self):
        logger.info("[request] 40008 persistente → BitgetTimestampExpired")
        client = _make_client()
        client._clock._synced = True
        with patch.object(client.http, "get", AsyncMock(
            return_value=_error_response("40008")
        )), patch.object(client, "sync_server_time", AsyncMock()):
            with self.assertRaises(BitgetTimestampExpired) as ctx:
                await client._request("GET", "/x", auth_required=True)
        logger.info("[request] code=%s", ctx.exception.code)
        self.assertEqual(ctx.exception.code, "40008")

    async def test_request_40725_raises_retryable(self):
        logger.info("[request] 40725 → BitgetRetryableError")
        client = _make_client()
        with patch.object(client.http, "get", AsyncMock(
            return_value=_error_response("40725", "deploy")
        )):
            with self.assertRaises(BitgetRetryableError) as ctx:
                await client._request("GET", "/x")
        logger.info("[request] code=%s delay=%s", ctx.exception.code, ctx.exception.delay)
        self.assertEqual(ctx.exception.code, "40725")

    async def test_request_40006_raises_auth_error(self):
        logger.info("[request] 40006 → BitgetAuthError")
        client = _make_client()
        with patch.object(client.http, "get", AsyncMock(
            return_value=_error_response("40006", "invalid key")
        )):
            with self.assertRaises(BitgetAuthError):
                await client._request("GET", "/x")

    async def test_request_40009_raises_auth_error(self):
        logger.info("[request] 40009 → BitgetAuthError")
        client = _make_client()
        with patch.object(client.http, "get", AsyncMock(
            return_value=_error_response("40009", "bad sign")
        )):
            with self.assertRaises(BitgetAuthError):
                await client._request("GET", "/x")

    async def test_request_unknown_code_raises_api_error(self):
        logger.info("[request] codice sconosciuto → BitgetApiError")
        client = _make_client()
        with patch.object(client.http, "get", AsyncMock(
            return_value=_error_response("99999", "unknown")
        )):
            with self.assertRaises(BitgetApiError) as ctx:
                await client._request("GET", "/x")
        self.assertEqual(ctx.exception.code, "99999")

    async def test_request_post_serializes_body_with_compact_separators(self):
        logger.info("[request] POST: data=body_str con separatori compatti")
        client = _make_client()
        body = {"symbol": "BTCUSDT", "qty": 1}
        with patch.object(client.http, "post", AsyncMock(
            return_value=_ok_response(),
        )) as mock_post:
            await client._request("POST", "/api/v3/trade/place-order", body=body)
        kwargs = mock_post.call_args.kwargs
        logger.info("[request] data=%r", kwargs.get("data"))
        self.assertEqual(kwargs["data"], '{"symbol":"BTCUSDT","qty":1}')

# ---------------------------------------------------------------------------
# Area F — Business errors in HTTP 4xx
# ---------------------------------------------------------------------------

class TestBitgetBusinessErrorFromHttp4xx(unittest.IsolatedAsyncioTestCase):
    """Bitget restituisce business errors con HTTP 4xx (non solo 200).

    Il client deve estrarre il codice dal body e tradurlo in eccezioni tipizzate.
    """

    async def test_http_4xx_with_40009_body_raises_auth_error(self) -> None:
        """40009 in body HTTP 400 → BitgetAuthError."""
        client = _make_client()
        with patch.object(client.http, "get", AsyncMock(
            side_effect=NonRetriableHttpError(
                status=400,
                body='{"code":"40009","msg":"signature error"}',
                url="https://fake",
            )
        )):
            with self.assertRaises(BitgetAuthError) as ctx:
                await client._request("GET", "/api/v3/account/assets", auth_required=True)
        self.assertEqual(ctx.exception.code, "40009")
        self.assertIn("signature", ctx.exception.msg)

    async def test_http_4xx_with_40006_body_raises_auth_error(self) -> None:
        """40006 in body HTTP 400 → BitgetAuthError."""
        client = _make_client()
        with patch.object(client.http, "get", AsyncMock(
            side_effect=NonRetriableHttpError(
                status=400,
                body='{"code":"40006","msg":"invalid access key"}',
                url="https://fake",
            )
        )):
            with self.assertRaises(BitgetAuthError) as ctx:
                await client._request("GET", "/api/v3/account/assets", auth_required=True)
        self.assertEqual(ctx.exception.code, "40006")

    async def test_http_4xx_with_40008_body_triggers_resync_and_retry(self) -> None:
        """40008 via HTTP 4xx → resync + retry, poi successo."""
        client = _make_client()
        client.sync_server_time = AsyncMock()

        with patch.object(client.http, "get", AsyncMock(side_effect=[
            NonRetriableHttpError(
                status=400,
                body='{"code":"40008","msg":"timestamp expired"}',
                url="https://fake",
            ),
            {"code": "00000", "msg": "success", "data": {"ok": True}},
        ])):
            res = await client._request(
                "GET", "/api/v3/account/assets", auth_required=True,
            )
        self.assertEqual(res["code"], "00000")
        client.sync_server_time.assert_awaited_once()

    async def test_http_4xx_with_40725_body_raises_retryable(self) -> None:
        """40725 in body HTTP 400 → BitgetRetryableError."""
        client = _make_client()
        with patch.object(client.http, "get", AsyncMock(
            side_effect=NonRetriableHttpError(
                status=400,
                body='{"code":"40725","msg":"service error"}',
                url="https://fake",
            )
        )):
            with self.assertRaises(BitgetRetryableError) as ctx:
                await client._request("GET", "/x")
        self.assertEqual(ctx.exception.code, "40725")

    async def test_http_4xx_with_unknown_business_code_reraises_http_error(self) -> None:
        """Codice business sconosciuto → propaga NonRetriableHttpError originale."""
        client = _make_client()
        with patch.object(client.http, "get", AsyncMock(
            side_effect=NonRetriableHttpError(
                status=400,
                body='{"code":"99999","msg":"unknown"}',
                url="https://fake",
            )
        )):
            with self.assertRaises(NonRetriableHttpError):
                await client._request("GET", "/x")

    async def test_http_4xx_with_non_json_body_reraises_http_error(self) -> None:
        """Body non JSON → propaga NonRetriableHttpError originale."""
        client = _make_client()
        with patch.object(client.http, "get", AsyncMock(
            side_effect=NonRetriableHttpError(
                status=400,
                body="<html>Bad Request</html>",
                url="https://fake",
            )
        )):
            with self.assertRaises(NonRetriableHttpError):
                await client._request("GET", "/x")

    async def test_http_4xx_with_empty_body_reraises_http_error(self) -> None:
        """Body vuoto → propaga NonRetriableHttpError originale."""
        client = _make_client()
        with patch.object(client.http, "get", AsyncMock(
            side_effect=NonRetriableHttpError(status=400, body="", url="https://fake")
        )):
            with self.assertRaises(NonRetriableHttpError):
                await client._request("GET", "/x")


# ---------------------------------------------------------------------------
# Area D — Rate limiting multi-bucket
# ---------------------------------------------------------------------------

class TestBitgetRateLimiting(unittest.IsolatedAsyncioTestCase):

    async def test_request_acquires_global_and_endpoint_bucket(self):
        logger.info("[rl] /candles → acquire(public_ip, public_candles)")
        limiter = MagicMock()
        limiter.acquire = AsyncMock(return_value={})
        client = _make_client(rate_limiter=limiter)

        with patch.object(client.http, "get", side_effect=_fake_get):
            await client._request("GET", "/api/v3/market/candles")
        args, _ = limiter.acquire.call_args
        logger.info("[rl] bucket acquisiti: %s", args)
        self.assertIn("public_ip", args)
        self.assertIn("public_candles", args)

    async def test_request_acquires_only_global_for_unmapped_endpoint(self):
        logger.info("[rl] endpoint non mappato → solo public_ip")
        limiter = MagicMock()
        limiter.acquire = AsyncMock(return_value={})
        client = _make_client(rate_limiter=limiter)
        with patch.object(client.http, "get", side_effect=_fake_get):
            await client._request("GET", "/api/v3/market/unknown")
        args, _ = limiter.acquire.call_args
        logger.info("[rl] bucket acquisiti: %s", args)
        self.assertEqual(args, ("public_ip",))

    async def test_init_rate_limiters_registers_all_buckets(self):
        logger.info("[rl] init_rate_limiters registra globale + tutti gli endpoint")
        limiter = init_rate_limiters(_fake_redis())
        logger.info("[rl] bucket registrati: %s", sorted(limiter.buckets.keys()))

        self.assertIn("public_ip", limiter.buckets)
        # Il globale ha finestra 60_000ms; gli endpoint 1_000ms
        self.assertEqual(limiter.buckets["public_ip"], (6000, 60_000))

        for bucket_name, limit in _ENDPOINT_BUCKETS.values():
            self.assertIn(bucket_name, limiter.buckets)
            self.assertEqual(limiter.buckets[bucket_name], (limit, 1_000))

    async def test_before_attempt_called_per_retry(self):
        logger.info("[rl] before_attempt chiama acquire a ogni tentativo")
        limiter = MagicMock()
        limiter.acquire = AsyncMock(return_value={})
        client = _make_client(rate_limiter=limiter)

        captured = {}
        async def _fake_get(url, headers=None, before_attempt=None, **kw):
            if before_attempt is None:
                raise ValueError("before_attempt non passato a _fake_get")
            captured["hook"] = before_attempt
            # Simula due tentativi
            await before_attempt()
            await before_attempt()
            return _ok_response()

        with patch.object(client.http, "get", side_effect=_fake_get):
            await client._request("GET", "/api/v3/market/candles")
        logger.info("[rl] acquire chiamato %d volte", limiter.acquire.await_count)
        self.assertEqual(limiter.acquire.await_count, 2)


# ---------------------------------------------------------------------------
# Area E — Endpoint pubblici
# ---------------------------------------------------------------------------

class TestBitgetPublicEndpoints(unittest.IsolatedAsyncioTestCase):

    async def test_get_candles_builds_params(self):
        logger.info("[candles] params costruiti correttamente")
        client = _make_client()
        with patch.object(client, "_request", AsyncMock(
            return_value=_ok_response()
        )) as mock_req:
            await client.get_candles(symbol="BTCUSDT", category="SPOT", interval="1m")
        _, kwargs = mock_req.call_args
        params = kwargs["params"]
        logger.info("[candles] params=%s", params)
        self.assertEqual(params["symbol"], "BTCUSDT")
        self.assertEqual(params["category"], "SPOT")
        self.assertEqual(params["interval"], "1m")
        self.assertEqual(params["limit"], "1000")

    async def test_get_candles_includes_start_end_time(self):
        logger.info("[candles] start/end time inclusi se forniti")
        client = _make_client()
        with patch.object(client, "_request", AsyncMock(
            return_value=_ok_response()
        )) as mock_req:
            await client.get_candles(
                symbol="BTCUSDT", start_time=1000, end_time=2000,
            )
        _, kwargs = mock_req.call_args
        params = kwargs["params"]
        logger.info("[candles] startTime=%s endTime=%s",
                    params.get("startTime"), params.get("endTime"))
        self.assertEqual(params["startTime"], "1000")
        self.assertEqual(params["endTime"], "2000")

    async def test_get_historical_candles_uses_history_endpoint(self):
        logger.info("[history] endpoint /history-candles")
        client = _make_client()
        with patch.object(client, "_request", AsyncMock(
            return_value=_ok_response()
        )) as mock_req:
            await client.get_historical_candles(symbol="BTCUSDT")
        args, _ = mock_req.call_args
        logger.info("[history] path=%s", args[1])
        self.assertEqual(args[1], "/api/v3/market/history-candles")

    async def test_get_instruments_filters_by_symbol(self):
        logger.info("[instruments] symbol passato come query param")
        client = _make_client()
        with patch.object(client, "_request", AsyncMock(
            return_value=_ok_response([])
        )) as mock_req:
            await client.get_instruments(category="SPOT", symbol="BTCUSDT")
        _, kwargs = mock_req.call_args
        logger.info("[instruments] params=%s", kwargs["params"])
        self.assertEqual(kwargs["params"], {"category": "SPOT", "symbol": "BTCUSDT"})

    async def test_get_tickers_returns_data(self):
        logger.info("[tickers] ritorna data")
        client = _make_client()
        with patch.object(client, "_request", AsyncMock(
            return_value=_ok_response([{"symbol": "BTCUSDT", "lastPrice": "1"}])
        )):
            data = await client.get_tickers(category="SPOT")
        logger.info("[tickers] data=%s", data)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["symbol"], "BTCUSDT")

    async def test_get_ticker_returns_first_or_none(self):
        logger.info("[ticker] lista vuota → None; lista piena → primo elemento")
        client = _make_client()
        with patch.object(client, "_request", AsyncMock(
            return_value=_ok_response([])
        )):
            self.assertIsNone(await client.get_ticker("BTCUSDT"))

        with patch.object(client, "_request", AsyncMock(
            return_value=_ok_response([{"symbol": "BTCUSDT", "lastPrice": "1"}])
        )):
            t = await client.get_ticker("BTCUSDT")
        logger.info("[ticker] t=%s", t)
        self.assertIsNotNone(t)
        if t is None:
            self.fail("get_ticker returned None for non-empty list")
        self.assertEqual(t["symbol"], "BTCUSDT")

    async def test_get_ticker_not_found_logs_warning(self):
        logger.info("[ticker] lista vuota → warning nel log")
        client = _make_client()
        with patch.object(client, "_request", AsyncMock(
            return_value=_ok_response([])
        )):
            with self.assertLogs("exchanges.bitget.bitget_rest", level="WARNING") as cm:
                await client.get_ticker("NOPE")
        logger.info("[ticker] log=%s", cm.output)
        self.assertTrue(any("Ticker non trovato" in l for l in cm.output))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    unittest.main()