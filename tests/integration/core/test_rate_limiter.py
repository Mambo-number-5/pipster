# tests/integration/test_rate_limiter.py
import asyncio
import logging
import os
import time
from tracemalloc import start
import unittest

import redis.asyncio as redis

from core.rate_limiter import (
    LocalTokenBucket,
    RateLimitManager,
    RedisSlidingWindow,
    RedisTokenBucket,
    ResilientRateLimiter,
    TokenBucketManager,
    build_resilient_token_bucket,
)

logger = logging.getLogger(__name__)


class TestRateLimiterIntegration(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.host = os.getenv("REDIS_HOST", "localhost")
        self.port = int(os.getenv("REDIS_PORT", 6379))
        self.redis_client = redis.Redis(
            host=self.host, port=self.port, db=15, decode_responses=True
        )
        logger.info("[Setup] Connessione a Redis %s:%d db=15", self.host, self.port)
        await self.redis_client.flushdb()
        logger.debug("[Setup] DB 15 pulito")

    async def asyncTearDown(self) -> None:
        await self.redis_client.flushdb()
        await self.redis_client.aclose()
        logger.debug("[Teardown] DB 15 pulito, connessione chiusa")

    # ---------- RedisTokenBucket ----------

    async def test_token_bucket_burst_and_recovery(self) -> None:
        logger.info("[TB] Burst di 5 immediato (capacity 5), poi attesa ~1s per 5 token a rate 5/s")
        limiter = RedisTokenBucket(self.redis_client, key="it_tb_1", rate=5.0, capacity=5)
        await limiter.acquire(requested=0)  # Warm-up script
        await self.redis_client.delete(limiter.key)
        logger.debug("[TB] Script Lua compilato e bucket azzerato")

        start = time.time()
        await asyncio.gather(*[limiter.acquire() for _ in range(5)])
        burst_elapsed = time.time() - start
        logger.info("[TB] Burst di 5 completato in %.4fs", burst_elapsed)
        self.assertLess(burst_elapsed, 0.1)

        start = time.time()
        await limiter.acquire(requested=5)   # 5 token a 5/s → ~1s
        elapsed = time.time() - start
        logger.info("[TB] Attesa per 5 token: %.4fs", elapsed)
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 1.3)

    async def test_token_bucket_manager_parallel(self) -> None:
        logger.info("[TBManager] Due bucket acquisiti in parallelo: a(50/s) + b(10/s)")
        mgr = TokenBucketManager(self.redis_client, exchange="it_tb_mgr")
        mgr.add_bucket("a", rate=50.0, capacity=50)
        mgr.add_bucket("b", rate=10.0, capacity=10)

        start = time.monotonic()
        await mgr.acquire("a", "b", requested=1)
        elapsed = time.monotonic() - start
        logger.info("[TBManager] Tempo totale: %.4fs", elapsed)
        self.assertLess(elapsed, 0.1)

    # ---------- RedisSlidingWindow ----------

    async def test_sliding_window_enforcement(self) -> None:
        logger.info("[SW] limit=5, window=2s: la sesta richiesta deve attendere ~2s")
        limiter = RedisSlidingWindow(self.redis_client, key="it_sw_1", limit=5, window_ms=2000)
        await limiter.acquire(requested=0)
        await self.redis_client.delete(limiter.key)

        await asyncio.gather(*[limiter.acquire() for _ in range(5)])
        logger.debug("[SW] 5 richieste consumate, il bucket è saturo")

        start = time.time()
        await limiter.acquire()
        elapsed = time.time() - start
        logger.info("[SW] Attesa sesta richiesta: %.4fs", elapsed)
        self.assertGreaterEqual(elapsed, 1.8)
        self.assertLess(elapsed, 2.5)

    async def test_sliding_window_recovery_full(self) -> None:
        logger.info("[SW] limit=3, window=1s: dopo 1.1s di sleep, 3 richieste di nuovo immediate")
        limiter = RedisSlidingWindow(self.redis_client, key="it_sw_2", limit=3, window_ms=1000)
        for _ in range(3):
            await limiter.acquire()
        logger.debug("[SW] Capacità esaurita, dormo 1.1s")

        await asyncio.sleep(1.1)

        start = time.time()
        await asyncio.gather(*[limiter.acquire() for _ in range(3)])
        elapsed = time.time() - start
        logger.info("[SW] Post-recovery 3 richieste in %.4fs", elapsed)
        self.assertLess(elapsed, 0.1)

    # ---------- RateLimitManager ----------

    async def test_manager_atomic_multi_bucket(self) -> None:
        logger.info("[Manager:atomic] fast(10), slow(2): se 'slow' saturo, nessun consumo su 'fast'")
        mgr = RateLimitManager(self.redis_client, exchange="it_mgr_2")
        mgr.add_limiter("fast", limit=10, window_ms=60000)
        mgr.add_limiter("slow", limit=2, window_ms=60000)

        r1 = await mgr.acquire("fast", "slow")
        logger.info("[Manager:atomic] Giro 1: fast=%d slow=%d", r1["fast"], r1["slow"])
        self.assertEqual(r1["fast"], 9)
        self.assertEqual(r1["slow"], 1)

        r2 = await mgr.acquire("fast", "slow")
        logger.info("[Manager:atomic] Giro 2: fast=%d slow=%d", r2["fast"], r2["slow"])
        self.assertEqual(r2["fast"], 8)
        self.assertEqual(r2["slow"], 0)

        fast_key = "{it_mgr_2}:rate_limit:fast"
        before = await self.redis_client.zcard(fast_key)
        logger.info("[Manager:atomic] ZCARD(fast) prima del giro 3: %d", before)

        task = asyncio.create_task(mgr.acquire("fast", "slow"))
        await asyncio.sleep(0.3)

        during = await self.redis_client.zcard(fast_key)
        logger.info("[Manager:atomic] ZCARD(fast) durante l'attesa del giro 3: %d", during)
        self.assertEqual(during, before, "Il bucket 'fast' è stato alterato durante un'acquisizione fallita!")

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        logger.info("[Manager:atomic] Task annullato correttamente")

    async def test_manager_routing_mixed_limits(self) -> None:
        logger.info("[Manager:mixed] fast(10/s) + slow(2/s): 3 slow + 10 fast in parallelo")
        mgr = RateLimitManager(self.redis_client, exchange="it_mgr_1")
        mgr.add_limiter("fast", limit=10, window_ms=1000)
        mgr.add_limiter("slow", limit=2, window_ms=1000)

        start = time.time()
        await asyncio.gather(
            *([mgr.acquire("slow") for _ in range(3)] +
              [mgr.acquire("fast") for _ in range(10)])
        )
        elapsed = time.time() - start
        logger.info("[Manager:mixed] Tempo totale: %.4fs (atteso ~1s, dominato da 'slow')", elapsed)
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 1.5)

    # ---------- ResilientRateLimiter (ADR-0005) ----------

    async def test_resilient_token_bucket_switches_to_local_on_redis_error(self) -> None:
        logger.info("[Resilient:int] Verifica degradazione su RedisError e recovery")
        wrapper = build_resilient_token_bucket(
            self.redis_client, key="it_res_1",
            rate=50.0, capacity=10, expected_workers=1, safety_factor=0.8, cooldown_sec=0.1,
        )
        await wrapper.acquire()
        logger.info("[Resilient:int] Warm-up del primary completato")

        original = wrapper.primary._lua_script
        async def _boom(*a, **k):
            raise redis.RedisError("simulated outage")
        wrapper.primary._lua_script = _boom
        logger.info("[Resilient:int] Redis simulato down: lo script Lua ora solleva RedisError")

        start = time.monotonic()
        await wrapper.acquire()
        await wrapper.acquire()
        elapsed = time.monotonic() - start
        logger.info("[Resilient:int] Due acquire in degraded mode: %.4fs (fallback locale)", elapsed)
        self.assertLess(elapsed, 0.5)

        wrapper.primary._lua_script = original
        await asyncio.sleep(0.15)
        logger.info("[Resilient:int] Ripristino primary dopo cooldown: acquire finale")
        await wrapper.acquire()
        logger.info("[Resilient:int] Recovery completata senza eccezioni")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",)
    unittest.main()