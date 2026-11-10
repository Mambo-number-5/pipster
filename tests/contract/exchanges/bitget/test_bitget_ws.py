# Esecuzione manuale con
# docker exec -it pipster_bot sh -c "BITGET_CONTRACT_TEST=1 python -m unittest tests.contract.exchanges.bitget.test_bitget_ws -v"
import asyncio
import os
import unittest
from typing import cast, Any

from exchanges.bitget.bitget_ws import BitgetWsClient, SubscribeReq, WsEnvelope

RUN = os.getenv("BITGET_CONTRACT_TEST") == "1"


@unittest.skipUnless(True, "Contract test: set BITGET_CONTRACT_TEST=1 (richiede rete)")
class TestBitgetWsContract(unittest.IsolatedAsyncioTestCase):
    """Verifica che i messaggi reali di Bitget siano compatibili con il parser."""

    async def test_kline_push_matches_expected_format(self):
        client = BitgetWsClient(demo=True)
        received: list[str] = []

        async def on_kline(raw: str) -> None:
            received.append(raw)

        await client.start()
        await asyncio.sleep(1.0)   # attesa connessione

        req = SubscribeReq.kline("spot", "BTCUSDT", "1m")
        await client.subscribe([req], callback=on_kline)

        # Attesa fino a 15s per un messaggio
        for _ in range(30):
            if received:
                break
            await asyncio.sleep(0.5)

        await client.unsubscribe([req])
        await client.stop()

        self.assertGreater(len(received), 0, "Nessun messaggio kline ricevuto")

        raw = received[0]
        env = WsEnvelope.from_json(raw)

        # --- Envelope ---
        self.assertIsNotNone(env.arg)
        arg = cast(dict[str, Any],env.arg)

        self.assertEqual(arg["topic"], "kline")
        self.assertEqual(arg["symbol"], "BTCUSDT")
        self.assertEqual(arg["instType"], "spot")
        self.assertIn(env.action, ("snapshot", "update"))
        
        self.assertIsNotNone(env.data)
        data = cast(list[dict[str, Any]],env.data)
        # --- Payload kline ---
        candle = data[0]
        for field in ("start", "open", "high", "low", "close", "volume", "turnover"):
            self.assertIn(field, candle, f"Campo atteso mancante: {field}")

        # --- Round-trip: il nostro from_arg ricostruisce la stessa req ---
        rebuilt = SubscribeReq.from_arg(arg)
        self.assertEqual(rebuilt, req, "from_arg non ricostruisce la SubscribeReq originale")