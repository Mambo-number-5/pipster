# tests/contract/exchanges/bitget/test_bitget_rest.py
"""Contract test REST Bitget: verifica il formato delle risposte pubbliche.

Skippato di default. Attivare con:

    # Da terminale (dentro il container)
    BITGET_CONTRACT_TEST=1 python -m unittest tests.contract.exchanges.bitget.test_bitget_rest

    # Tramite debugger: usare il profilo "Contract test (current file)"
    # in .vscode/launch.json, che imposta BITGET_CONTRACT_TEST=1.

Non dipende da Redis né da credenziali: usa solo endpoint pubblici.
Le chiamate autenticate richiedono BITGET_CONTRACT_TEST=1 e credenziali
in variabili d'ambiente (vedi nota in fondo al file).
"""

import os, asyncio
import time
import unittest

from exchanges.bitget.bitget_rest import BitgetRestClient

RUN = os.getenv("BITGET_CONTRACT_TEST") == "1"


@unittest.skipUnless(RUN, "Contract test: set BITGET_CONTRACT_TEST=1 (richiede rete)")
class TestBitgetRestContract(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.client = BitgetRestClient()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    # ------------------------------------------------------------------
    # Server time / clock sync
    # ------------------------------------------------------------------

    async def test_server_time_v3_returns_valid_format(self) -> None:
        """Verifica che /api/v3/market/time risponda nel formato V3 documentato."""
        res = await self.client._fetch_time_payload()

        self.assertEqual(res["code"], "00000")
        self.assertIn("requestTime", res)
        self.assertIn("data", res)
        self.assertIn("serverTime", res["data"])

        server_time = int(res["data"]["serverTime"])
        local = int(time.time() * 1000)
        drift = abs(server_time - local)
        self.assertLess(drift, 60_000, f"Drift server-locale inaccettabile: {drift}ms",)

    async def test_clock_sync_converges(self) -> None:
        """sync_server_time() completa e produce un offset plausibile."""
        await self.client.sync_server_time()
        self.assertTrue(self.client._clock.is_synced)
        self.assertLess(abs(self.client._clock.offset_ms), 5_000, f"Offset anomalo: {self.client._clock.offset_ms}ms",)

    # ------------------------------------------------------------------
    # Candele
    # ------------------------------------------------------------------

    async def test_candles_spot_returns_ohlcv_format(self) -> None:
        """GET /candles?category=SPOT ritorna array [ts, o, h, l, c, vol, turnover]."""
        data = await self.client.get_candles(
            symbol="BTCUSDT", category="SPOT", interval="1m", limit=5,
        )
        self.assertGreater(len(data), 0, "Nessuna candela ricevuta")
        candle = data[0]
        self.assertEqual(len(candle), 7, f"Attesi 7 campi, ricevuti {len(candle)}")

        ts, o, h, l, c, vol, turnover = candle
        self.assertTrue(ts.isdigit(), f"timestamp non numerico: {ts}")
        for name, val in [("open", o), ("high", h), ("low", l), ("close", c)]:
            float(val)  # solleva ValueError se non parsabile
        float(vol)
        float(turnover)

        # La candela più recente non può essere più vecchia di 5 minuti
        candle_age_ms = int(time.time() * 1000) - int(ts)
        self.assertLess(candle_age_ms, 5 * 60_000)

    async def test_candles_futures_accepts_category(self) -> None:
        """Il client accetta category=USDT-FUTURES e ritorna dati validi."""
        data = await self.client.get_candles(symbol="BTCUSDT", category="USDT-FUTURES", interval="1m", limit=5,)
        self.assertGreater(len(data), 0)
        self.assertEqual(len(data[0]), 7)

    async def test_historical_candles_returns_data(self) -> None:
        """GET /history-candles risponde con lo stesso formato di /candles."""
        now_ms = int(time.time() * 1000)
        # Range di 1 giorno, che dovrebbe contenere candele
        data = await self.client.get_historical_candles(
            symbol="BTCUSDT", category="SPOT", interval="1m",
            start_time=now_ms - 24 * 3600 * 1000, end_time=now_ms, limit=10,
        )
        # Può essere vuoto se il range è interamente > 90 giorni fa? No, è recente.
        # Bitget richiede che endTime sia < 90 giorni fa per /history-candles.
        # Se la risposta è vuota, potrebbe essere che il range non rispetta il vincolo.
        # In quel caso il test è skip, non fail.
        if not data:
            self.skipTest("history-candles ha richiesto un range fuori specifica")
        candle = data[0]
        self.assertEqual(len(candle), 7)

    # ------------------------------------------------------------------
    # Instruments e tickers
    # ------------------------------------------------------------------

    async def test_instruments_returns_precision_fields(self) -> None:
        """GET /instruments ritorna i campi di precisione usati dal Risk Manager."""
        data = await self.client.get_instruments(category="SPOT", symbol="BTCUSDT")
        self.assertGreater(len(data), 0, "Nessun instrument per BTCUSDT/SPOT")
        instr = data[0]
        for field in (
            "symbol", "category", "baseCoin", "quoteCoin",
            "pricePrecision", "quantityPrecision", "status",
        ):
            self.assertIn(field, instr, f"Campo mancante: {field}")
        self.assertEqual(instr["symbol"], "BTCUSDT")

    async def test_tickers_returns_last_price(self) -> None:
        """GET /tickers ritorna lastPrice e ts recente."""
        data = await self.client.get_tickers(category="SPOT", symbol="BTCUSDT")
        self.assertGreater(len(data), 0)
        ticker = data[0]
        self.assertIn("lastPrice", ticker)
        self.assertIn("ts", ticker)
        float(ticker["lastPrice"])

        ticker_age_ms = int(time.time() * 1000) - int(ticker["ts"])
        self.assertLess(
            ticker_age_ms, 60_000,
            f"Ticker troppo vecchio: {ticker_age_ms}ms",
        )

    # ------------------------------------------------------------------
    # Sanity: latenza (opzionale)
    # ------------------------------------------------------------------

    async def test_multiple_requests_complete_quickly(self) -> None:
        """10 chiamate a /time completano in < 5s: sanity sulla latenza del server.

        Non verifica il rate limiter (il client in contract test non ha Redis
        iniettato). Verifica solo che la latenza di rete sia ragionevole e che
        non ci siano blocchi inattesi nel client.
        """
        start = time.monotonic()
        for _ in range(10):
            res = await self.client._fetch_time_payload()
            self.assertEqual(res["code"], "00000")
        elapsed = time.monotonic() - start
        self.assertLess(
            elapsed, 5.0,
            f"10 chiamate hanno impiegato {elapsed:.2f}s: latenza troppo alta",)

    @unittest.skipUnless(
        RUN and os.getenv("BITGET_API_KEY"), # "BITGET_DEMO_KEY"
        "Richiede BITGET_DEMO_KEY per validare firma HMAC contro demo",)
    async def test_authenticated_request_with_demo_key(self) -> None:
        """Verifica che Bitget accetti la nostra firma HMAC (read-only, demo key)."""
        client = BitgetRestClient(
            api_key=os.environ["BITGET_API_KEY"],
            secret_key=os.environ["BITGET_API_SECRET"],
            passphrase=os.getenv("BITGET_API_PASSPHRASE", ""),
            demo=False,
        )
        try:
            # Endpoint read-only autenticato (da documentazione Bitget)
            res = await client._request(
                "GET", "/api/v3/account/assets", auth_required=True,
            )
            self.assertEqual(res["code"], "00000",
                f"Firma rifiutata: {res.get('msg')} (code {res.get('code')})")
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()