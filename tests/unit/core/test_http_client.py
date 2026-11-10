# tests/unit/core/test_http_client.py
import asyncio
import logging
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from core.exceptions import (RetryableWithDelay, NonRetriableHttpError,
                                RateLimitError, ServerError)
from core.http_client import AsyncHttpClient

logger = logging.getLogger(__name__)


def _make_response(status=200, json_body=None, text_body=None, headers=None):
    """Costruisce un mock di risposta aiohttp con status e body controllati.

    Per status 5xx, `raise_for_status` solleva `ClientResponseError` reale,
    così il test esercita il comportamento di aiohttp invece del mock.
    """
    resp = AsyncMock()
    resp.status = status
    resp.headers = headers or {}

    if status >= 500:
        resp.raise_for_status = MagicMock(
            side_effect=aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=status,
            )
        )
    else:
        resp.raise_for_status = MagicMock()

    async def _json():
        if json_body is None:
            raise aiohttp.ContentTypeError(MagicMock(), MagicMock())
        return json_body
    resp.json = _json

    async def _text():
        return text_body or ""
    resp.text = _text
    return resp




def _make_request_cm(response):
    """Context manager che restituisce `response` come valore di __aenter__."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestAsyncHttpClientBasics(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncHttpClient()

    async def asyncTearDown(self):
        await self.client.close()

    @patch("aiohttp.ClientSession.request")
    async def test_get_success_json(self, mock_request):
        logger.info("[GET] 200 con JSON → dict decodificato")
        mock_request.return_value = _make_request_cm(_make_response(200, json_body={"price": 50000}))
        result = await self.client.get("https://fake/ticker")
        logger.info("[GET] Risultato: %s", result)
        self.assertEqual(result["price"], 50000)
        mock_request.assert_called_once()

    @patch("aiohttp.ClientSession.request")
    async def test_post_sends_json_body(self, mock_request):
        logger.info("[POST] il json_data viene inoltrato a aiohttp come json=")
        mock_request.return_value = _make_request_cm(_make_response(200, json_body={"ok": True}))
        result = await self.client.post("https://fake/order", json_data={"symbol": "BTCUSDT"})
        self.assertTrue(result["ok"])
        _, kwargs = mock_request.call_args
        logger.info("[POST] kwargs['json']=%s", kwargs.get("json"))
        self.assertEqual(kwargs["json"], {"symbol": "BTCUSDT"})

    @patch("aiohttp.ClientSession.request")
    async def test_delete_uses_params(self, mock_request):
        logger.info("[DELETE] query params passati come params=")
        mock_request.return_value = _make_request_cm(_make_response(200, json_body={"deleted": 1}))
        await self.client.delete("https://fake/order/1", params={"id": "1"})
        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["params"], {"id": "1"})

    @patch("aiohttp.ClientSession.request")
    async def test_204_returns_none(self, mock_request):
        logger.info("[204] No Content → None")
        mock_request.return_value = _make_request_cm(_make_response(204))
        result = await self.client.get("https://fake/nothing")
        self.assertIsNone(result)

    @patch("aiohttp.ClientSession.request")
    async def test_response_text_when_not_json(self, mock_request):
        logger.info("[non-JSON] Content-Type non JSON → ritorna testo")
        mock_request.return_value = _make_request_cm(
            _make_response(200, text_body="<html>ok</html>")
        )
        result = await self.client.get("https://fake/page")
        self.assertEqual(result, "<html>ok</html>")


class TestAsyncHttpClientRetry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncHttpClient()

    async def asyncTearDown(self):
        await self.client.close()

    @patch("aiohttp.ClientSession.request")
    async def test_retry_on_timeout(self, mock_request):
        logger.info("[retry] Timeout → ClientError → successo (3 tentativi)")
        mock_request.side_effect = [
            asyncio.TimeoutError("t1"),
            aiohttp.ClientError("c2"),
            _make_request_cm(_make_response(200, json_body={"ok": True})),
        ]
        result = await self.client.get("https://fake", max_retries=3, base_delay=0.01)
        self.assertTrue(result["ok"])
        self.assertEqual(mock_request.call_count, 3)

    @patch("aiohttp.ClientSession.request")
    async def test_retry_exhausted_raises(self, mock_request):
        logger.info("[retry] tutte le chiamate falliscono → eccezione propagata")
        mock_request.side_effect = aiohttp.ClientError("always fails")
        with self.assertRaises(aiohttp.ClientError):
            await self.client.get("https://fake", max_retries=2, base_delay=0.01)
        logger.info("[retry] tentativi totali: %d", mock_request.call_count)
        self.assertEqual(mock_request.call_count, 3)  # 1 + 2 retry

    @patch("aiohttp.ClientSession.request")
    async def test_hook_called_before_each_attempt(self, mock_request):
        logger.info("[hook] before_attempt invocato per ogni tentativo")
        mock_request.side_effect = [
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
            _make_request_cm(_make_response(200, json_body={})),
        ]
        hook = AsyncMock()
        await self.client.get("https://fake", max_retries=3, base_delay=0.01, before_attempt=hook)
        logger.info("[hook] chiamate: %d", hook.call_count)
        self.assertEqual(hook.call_count, 3)

    @patch("aiohttp.ClientSession.request")
    async def test_4xx_not_retried(self, mock_request):
        """400/401/403/404 → NonRetriableHttpError al primo tentativo, nessun retry."""
        logger.info("[4xx] 404 al primo tentativo → NonRetriableHttpError, no retry")
        mock_request.return_value = _make_request_cm(_make_response(404, text_body='{"code":"404"}'))

        with self.assertRaises(NonRetriableHttpError) as ctx:
            await self.client.get("https://fake", max_retries=3, base_delay=0.01)

        logger.info("[4xx] status=%d, tentativi=%d", ctx.exception.status, mock_request.call_count)
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(mock_request.call_count, 1)

    @patch("aiohttp.ClientSession.request")
    async def test_5xx_is_retried(self, mock_request):
        """500/502/503 → retry con backoff, poi successo."""
        logger.info("[5xx] 503 al primo tentativo → retry, secondo tentativo 200")
        mock_request.side_effect = [
            _make_request_cm(_make_response(503)),
            _make_request_cm(_make_response(200, json_body={"ok": True})),
        ]

        result = await self.client.get("https://fake", max_retries=3, base_delay=0.01)
        logger.info("[5xx] risultato=%s, tentativi=%d", result, mock_request.call_count)
        self.assertTrue(result["ok"])
        self.assertEqual(mock_request.call_count, 2)


class TestAsyncHttpClientRateLimit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncHttpClient()

    async def asyncTearDown(self):
        await self.client.close()

    @patch("aiohttp.ClientSession.request")
    async def test_429_respects_retry_after(self, mock_request):
        logger.info("[429] Retry-After=0.1s → il secondo tentativo attende 0.1s")
        mock_request.side_effect = [
            _make_request_cm(_make_response(429, headers={"Retry-After": "0.1"})),
            _make_request_cm(_make_response(200, json_body={"ok": True})),
        ]
        with patch("core.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await self.client.get("https://fake", max_retries=3)
        self.assertTrue(result["ok"])
        # Il primo retry deve usare 0.1, non il backoff standard (1.0 * 2^0 + jitter)
        logger.info("[429] sleep chiamato con: %s", mock_sleep.call_args_list)
        args, _ = mock_sleep.call_args_list[0]
        self.assertAlmostEqual(args[0], 0.1, places=2)

    @patch("aiohttp.ClientSession.request")
    async def test_429_exhausted_raises_rate_limit_error(self, mock_request):
        logger.info("[429] tutti 429 → RateLimitError con retry_after corretto")
        mock_request.side_effect = lambda *a, **k: _make_request_cm(
            _make_response(429, headers={"Retry-After": "0.05"})
        )
        with patch("core.utils.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(RateLimitError) as ctx:
                await self.client.get("https://fake", max_retries=2)
        logger.info("[429] retry_after=%s", ctx.exception.delay)
        
        self.assertIsNotNone(ctx.exception.delay)
        assert ctx.exception.delay is not None
        self.assertAlmostEqual(ctx.exception.delay, 0.05, places=2)
        self.assertIsInstance(ctx.exception, RetryableWithDelay)

    @patch("aiohttp.ClientSession.request")
    async def test_503_with_retry_after_is_retried(self, mock_request):
        """503 con Retry-After → RateLimitError → delay dal server, poi successo."""
        logger.info("[503] Retry-After=0.1s → retry con delay 0.1")
        mock_request.side_effect = [
            _make_request_cm(_make_response(503, headers={"Retry-After": "0.1"})),
            _make_request_cm(_make_response(200, json_body={"ok": True})),
        ]
        with patch("core.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await self.client.get("https://fake", max_retries=3)
        self.assertTrue(result["ok"])
        logger.info("[503] sleep chiamato con: %s", mock_sleep.call_args_list)
        args, _ = mock_sleep.call_args_list[0]
        self.assertAlmostEqual(args[0], 0.1, places=2)

    @patch("aiohttp.ClientSession.request")
    async def test_418_is_not_retried(self, mock_request):
        """418 (ban Binance) → NonRetriableHttpError con retry_after, nessun retry."""
        logger.info("[418] ban → NonRetriableHttpError al primo tentativo")
        mock_request.return_value = _make_request_cm(
            _make_response(418, headers={"Retry-After": "120"})
        )
        with self.assertRaises(NonRetriableHttpError) as ctx:
            await self.client.get("https://fake", max_retries=3, base_delay=0.01)
    
        logger.info("[418] status=%d, retry_after=%s, tentativi=%d",
                    ctx.exception.status, ctx.exception.retry_after, mock_request.call_count)
        
        self.assertEqual(ctx.exception.status, 418)
        self.assertIsNotNone(ctx.exception.retry_after)
        assert ctx.exception.retry_after is not None   # solo per Pylance
        self.assertAlmostEqual(ctx.exception.retry_after, 120.0, places=1)
        self.assertEqual(mock_request.call_count, 1)
    
    @patch("aiohttp.ClientSession.request")
    async def test_429_without_retry_after_uses_backoff(self, mock_request):
        """429 senza Retry-After → backoff standard (non 1.0 fisso)."""
        logger.info("[429] senza header → retry_async calcola il backoff")
        mock_request.side_effect = [
            _make_request_cm(_make_response(429)),   # nessun header Retry-After
            _make_request_cm(_make_response(200, json_body={"ok": True})),
        ]
        with patch("core.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await self.client.get("https://fake", max_retries=3, base_delay=0.5)
        self.assertTrue(result["ok"])
        args, _ = mock_sleep.call_args_list[0]
        # backoff = base_delay * 2^0 + jitter (uniform 0..0.5)
        logger.info("[429] delay usato (atteso >= 0.5): %s", args[0])
        self.assertGreaterEqual(args[0], 0.5)
        self.assertLess(args[0], 1.1)   # 0.5 + max jitter 0.5 + margine


class TestAsyncHttpClientLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_close_idempotent(self):
        logger.info("[close] due chiamate consecutive non sollevano")
        client = AsyncHttpClient()
        await client.close()
        await client.close()
        logger.info("[close] ok")

    @patch("aiohttp.ClientSession.request")
    async def test_session_reused_across_calls(self, mock_request):
        logger.info("[session] la sessione è creata una sola volta")
        mock_request.return_value = _make_request_cm(_make_response(200, json_body={}))
        client = AsyncHttpClient()
        try:
            await client.get("https://fake/a")
            session_1 = client._session
            await client.get("https://fake/b")
            session_2 = client._session
            self.assertIs(session_1, session_2)
            logger.info("[session] id identico: %s", id(session_1) == id(session_2))
        finally:
            await client.close()


class TestAsyncHttpClientBodyInErrors(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.client = AsyncHttpClient()

    async def asyncTearDown(self):
        await self.client.close()

    @patch("aiohttp.ClientSession.request")
    async def test_4xx_body_is_accessible_and_logged(self, mock_request):
        """4xx: body in .body dell'eccezione, loggato a WARNING."""
        mock_request.return_value = _make_request_cm(
            _make_response(400, text_body='{"code":"40009","msg":"signature error"}')
        )
        with self.assertLogs("core.http_client", level="WARNING") as cm:
            with self.assertRaises(NonRetriableHttpError) as ctx:
                await self.client.get("https://fake")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("40009", ctx.exception.body)
        self.assertTrue(any("40009" in line for line in cm.output))

    @patch("aiohttp.ClientSession.request")
    async def test_5xx_body_is_accessible_and_retried(self, mock_request):
        """5xx: ServerError con .body, retriable."""
        mock_request.side_effect = [
            _make_request_cm(_make_response(503, text_body='{"error":"overloaded"}')),
            _make_request_cm(_make_response(200, json_body={"ok": True})),
        ]
        result = await self.client.get("https://fake", max_retries=3, base_delay=0.01)
        self.assertTrue(result["ok"])
        self.assertEqual(mock_request.call_count, 2)

    @patch("aiohttp.ClientSession.request")
    async def test_5xx_body_exposed_when_retry_exhausted(self, mock_request):
        """5xx persistente: ServerError con .body dopo retry."""
        mock_request.return_value = _make_request_cm(
            _make_response(502, text_body='{"error":"bad gateway"}')
        )
        with self.assertRaises(ServerError) as ctx:
            await self.client.get("https://fake", max_retries=1, base_delay=0.01)
        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("bad gateway", ctx.exception.body)

    @patch("aiohttp.ClientSession.request")
    async def test_429_body_and_delay_preserved(self, mock_request):
        """429: RateLimitError ha sia .delay che .body."""
        mock_request.return_value = _make_request_cm(
            _make_response(429, headers={"Retry-After": "2.0"},
                           text_body='{"rate":"limited"}')
        )
        with patch("core.utils.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(RateLimitError) as ctx:
                await self.client.get("https://fake", max_retries=1)
        self.assertIsNotNone(ctx.exception.delay)
        assert ctx.exception.delay is not None
        self.assertAlmostEqual(ctx.exception.delay, 2.0, places=1)
        self.assertIn("limited", ctx.exception.body)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    unittest.main()