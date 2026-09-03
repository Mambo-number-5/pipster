import time
import unittest
import redis.asyncio as redis
from typing import cast
from core.base_ingestion import BaseIngestionWorker
from core.token_bucket import RedisTokenBucket
from core.models import Candle
from core.utils import retry_async

# ==========================================
# CLASSE WORKER PER TEST (Dummy Implementation)
# ==========================================
class DummyTestWorker(BaseIngestionWorker):
    """Implementazione concreta per testare la logica base senza chiamare vere API."""
    def __init__(self):
        # Puntiamo rigorosamente al DB 15 come richiesto
        super().__init__(symbol="TESTBTC", timeframe="15m", market_type="spot", redis_db=15, redis_host="redis")
        self.gap_fill_calls = 0

    async def fetch_historic_klines(self, since_timestamp: int | None = None, limit=200):
        self.gap_fill_calls += 1
        if since_timestamp is None: 
            since_timestamp = 1000000  # Valore di default per test
        # Simuliamo il recupero di una candela mancante dal REST
        return [{
            "timestamp": since_timestamp + self.tf_ms,
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100,
            "is_closed": True
        }]

    async def listen_websocket_stream(self):
        pass

    async def sync_time(self):
        return int(time.time() * 1000)  # Simuliamo il tempo corrente in ms 

# ==========================================
# SUITE DI TEST SU FERRO (DB 15)
# ==========================================
class TestIngestionEngineRealRedis(unittest.IsolatedAsyncioTestCase):
    
    async def asyncSetUp(self):
        """Prepara l'ambiente prima di ogni singolo test."""
        print(f"\n[START] ---- Avvio test: {self._testMethodName} ----")
        self.worker = DummyTestWorker()
        self.worker.redis = redis.Redis(host=self.worker.redis_host, port=self.worker.redis_port, db=15, decode_responses=True)
        # Svuotiamo il DB 15 per avere test isolati e puliti
        await self.worker.redis.flushdb()

    async def asyncTearDown(self):
        """Chiude la connessione a Redis dopo ogni test."""
        if self.worker.redis:
            await self.worker.redis.aclose()
        # Questo print apparirà DOPO l'esecuzione di ogni test
        print(f"[END] ------ Test completato: {self._testMethodName} ------\n")

    # --- TEST 1: Token Bucket ---
    async def test_token_bucket_rate_limiting(self):
        """Verifica che il bucket blocchi le richieste sopra il burst e calcoli l'attesa."""
        if self.worker.redis is not None:
            limiter = RedisTokenBucket(self.worker.redis, key="test_limit", rate=10, capacity=5)
        else:
            raise TypeError
        start_time = time.time()
        # Svuotiamo istantaneamente la capacità massima (5 token)
        await limiter.acquire(requested=5)
        elapsed_burst = time.time() - start_time
        self.assertLess(elapsed_burst, 0.1, "Il burst iniziale doveva essere immediato")

        # Richiediamo un altro token: la capacità è vuota, il rate è 10/s (1 ogni 0.1s)
        # Dovrà farci dormire per circa 0.1 secondi.
        start_time = time.time()
        await limiter.acquire(requested=1)
        elapsed_wait = time.time() - start_time
        self.assertGreater(elapsed_wait, 0.08, "Il limiter non ha applicato il ritardo corretto")

    # --- TEST 2: Inserimento Stream e Prevenzione Duplicati ---
    async def test_push_to_redis_prevents_duplicates(self):
        """Verifica che Redis nativamente scarti candele con ID identici o vecchi."""
        kline = Candle(
            timestamp=1690000000000,
            open=1, high=2, low=1, close=2, volume_base=100, volume_quote=10, is_closed=True)

        if self.worker.redis is None:
            raise TypeError
        
        # Primo inserimento: successo
        await self.worker.push_to_redis(kline)
        stream_len = await self.worker.redis.xlen(self.worker.redis_stream)
        self.assertEqual(stream_len, 1)

        # Secondo inserimento (DUPLICATO): l'eccezione viene gestita da BaseIngestionWorker
        await self.worker.push_to_redis(kline)
        stream_len2 = await self.worker.redis.xlen(self.worker.redis_stream)
        self.assertEqual(stream_len2, 1, "Il duplicato è stato inserito nello stream!")

    # --- TEST 3: Routing delle Candele (Aperte vs Chiuse) ---
    async def test_process_live_candle_routing(self):
        """Verifica che le candele aperte vadano in HASH e le chiuse nello STREAM."""
        import copy
        if self.worker.redis is None:
                    raise TypeError
        # Candela APERTA
        open_kline = Candle(
            timestamp=1690000000000, open=1, high=2, low=1, close=1.5, volume_base=50, volume_quote=5,
            is_closed=False)
        await self.worker.process_live_candle(open_kline)
        
        hash_exists = await self.worker.redis.exists(self.worker.redis_live_key)
        stream_len = await self.worker.redis.xlen(self.worker.redis_stream)
        self.assertTrue(hash_exists, "La candela aperta non è andata nell'Hash")
        self.assertEqual(stream_len, 0, "La candela aperta è finita nello Stream")

        # Candela CHIUSA
        closed_kline = copy.copy(open_kline)
        closed_kline.is_closed = True
        closed_kline.close = 2.0  # Chiusura definitiva
        await self.worker.process_live_candle(closed_kline)

        stream_len2 = await self.worker.redis.xlen(self.worker.redis_stream)
        self.assertEqual(stream_len2, 1, "La candela chiusa non è andata nello Stream")

    # --- TEST 4: Rilevamento Gap e Recupero REST ---
    async def test_process_live_candle_gap_filling(self):
        """Verifica che un buco cronologico faccia scattare il recupero (gap fill)."""
        # Simuliamo di avere già la candela T1 in memoria
        self.worker.last_saved_timestamp = 1690000000000
        if self.worker.redis is None:
                    raise TypeError
        
        # Arriva una candela 2 timeframe più tardi (es. manca una candela da 15m in mezzo)
        t_gap = self.worker.last_saved_timestamp + (self.worker.tf_ms * 2)
        future_kline = Candle(timestamp=t_gap, open=1, high=2, low=1, close=2, volume_base=100, volume_quote=10,
            is_closed=True)
        
        # Il motore dovrebbe notare il gap e chiamare fetch_historic_klines
        await self.worker.process_live_candle(future_kline)
        
        self.assertEqual(self.worker.gap_fill_calls, 1, "Il Gap Filler non è stato invocato")
        # Nello stream dovremmo avere 2 candele: quella del gap (finta) e quella live
        stream_len = await self.worker.redis.xlen(self.worker.redis_stream)
        self.assertEqual(stream_len, 2)

    # --- TEST 5: Utils (Retry Async) ---
    async def test_retry_async_recovers_from_error(self):
        """Verifica che retry_async riprovi in caso di errore temporaneo."""
        attempts = 0

        async def faulty_coro():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionError("Finto errore di rete")
            return "Successo"

        # Passiamo la chiamata tramite lambda a retry_async
        result = await retry_async(
            lambda: faulty_coro(), 
            max_retries=3, 
            base_delay=0.1, 
            exceptions=(ConnectionError,)
        )
        
        self.assertEqual(result, "Successo")
        self.assertEqual(attempts, 3, "Non ha effettuato i 3 tentativi")
    
    # --- TEST 6: Paginazione dello Storico (Chunking) ---
    async def test_historical_gap_pagination(self):
        """Verifica che la paginazione continui fino all'esaurimento dei dati."""
        call_count = 0

        if self.worker.redis is None:
            raise TypeError

        # Sovrascriviamo temporaneamente fetch_historic_klines per simulare 5 candele paginate a blocchi di 2
        async def mock_paginated_fetch(since_timestamp=None, limit=200):
            nonlocal call_count
            call_count += 1
            
            base_ts = since_timestamp or 1000000
            
            # Chiamata 1: 2 candele (pari a limit=2) -> Continua
            if call_count == 1:
                return [
                    {"timestamp": base_ts, "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 100, "is_closed": True},
                    {"timestamp": base_ts + 900000, "open": 1.5, "high": 2.5, "low": 1.2, "close": 2, "volume": 110, "is_closed": True}
                ]
            # Chiamata 2: 2 candele (pari a limit=2) -> Continua
            elif call_count == 2:
                return [
                    {"timestamp": base_ts + 1800000, "open": 2, "high": 3, "low": 1.8, "close": 2.2, "volume": 120, "is_closed": True},
                    {"timestamp": base_ts + 2700000, "open": 2.2, "high": 3.1, "low": 2.0, "close": 3, "volume": 130, "is_closed": True}
                ]
            # Chiamata 3: 1 candela (< limit=2) -> Fine paginazione
            else:
                return [
                    {"timestamp": base_ts + 3600000, "open": 3, "high": 3.5, "low": 2.8, "close": 3.2, "volume": 140, "is_closed": True}
                ]

        self.worker.fetch_historic_klines = mock_paginated_fetch

        # Eseguiamo il riempimento del gap richiedendo pagine da 2 elementi
        total_saved = await self.worker._fill_gap(from_ts=1000000, limit=2)

        # Verifiche: 3 chiamate totali REST fatte, 5 candele salvate in Redis
        self.assertEqual(call_count, 3, "Il loop di paginazione non ha effettuato il numero corretto di chiamate")
        self.assertEqual(total_saved, 4, "Il numero totale di candele salvate a DB non corrisponde")
        
        stream_len = await self.worker.redis.xlen(self.worker.redis_stream)
        self.assertEqual(stream_len, 5, "Le candele paginate non sono state scritte correttamente nello Stream")

    # --- TEST 7: Logica di Warm-up (DB Vuoto vs DB Popolato) ---
    async def test_warmup_logic_from_scratch(self):
        """Verifica che il warm-up su DB vuoto legga l'ultimo dato e inizializzi correttamente lo stato."""
        # Simuliamo un DB che contiene già una candela
        first_kline = Candle(
            timestamp=1690000000000,
            open=100, high=105, low=99, close=102, volume_base=500, volume_quote=50, is_closed=True)
        await self.worker.push_to_redis(first_kline)

        # Inizializziamo un nuovo worker per verificare la lettura dello stato iniziale da Redis
        new_worker = DummyTestWorker()
        new_worker.redis = self.worker.redis
        
        # Recuperiamo l'ultimo timestamp salvato
        last_ts = await new_worker._get_last_redis_timestamp()
        
        self.assertEqual(last_ts, 1690000000000, "Impossibile recuperare il timestamp di warm-up dal DB")
        print(f"Warm-up completato: ultimo timestamp recuperato = {last_ts}")

if __name__ == '__main__':
    unittest.main()