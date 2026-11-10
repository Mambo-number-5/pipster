# tests/unit/test_rate_limiter.py
import asyncio
import logging
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

import redis

from core.rate_limiter import (
    LocalFixedWindow,
    LocalSlidingWindow,
    LocalTokenBucket,
    RateLimitManager,
    RedisSlidingWindow,
    RedisTokenBucket,
    ResilientRateLimiter,
    _LocalManagerFallback,
    _LocalTokenBucketManagerFallback,
    build_resilient_manager,
    build_resilient_sliding_window,
    build_resilient_token_bucket,
    build_resilient_token_bucket_manager,
)

logger = logging.getLogger(__name__)


def _fake_redis_sliding_window(current_count: int = 0) -> MagicMock:
    """
    Mock per RedisSlidingWindow: emula il ritorno {1, current_count} dello
    script Lua a singola chiave.
    """
    fake = MagicMock()
    fake.register_script = MagicMock(
        return_value=AsyncMock(return_value=(1, current_count))
    )
    return fake


def _fake_redis_manager(remaining: int = 9) -> MagicMock:
    """
    Mock per RateLimitManager: emula il ritorno {1, [key1, remaining1, key2, remaining2, ...]}
    dello script Lua multi-chiave.
    """
    async def _fake_lua(*args, **kwargs):
        keys = kwargs.get("keys", [])
        flat: list = []
        for k in keys:
            flat.append(k)
            flat.append(remaining)
        return (1, flat)

    fake = MagicMock()
    fake.register_script = MagicMock(return_value=AsyncMock(side_effect=_fake_lua))
    return fake

# =============================================================================
# LocalFixedWindow
# =============================================================================

class TestLocalFixedWindow(unittest.IsolatedAsyncioTestCase):

    async def test_limit_and_reset(self) -> None:
        logger.info("[FixedWindow] Verifica esaurimento token e lazy reset")
        throttle = LocalFixedWindow(capacity=5, window_sec=0.2, name="t")

        logger.info("[FixedWindow] Richiedo 3 token su 5 → atteso True")
        self.assertTrue(await throttle.check_and_acquire(3))

        logger.info("[FixedWindow] Richiedo altri 3 (sfora) → atteso False")
        self.assertFalse(await throttle.check_and_acquire(3))

        logger.info("[FixedWindow] Richiedo 2 (arrivo a 5) → atteso True")
        self.assertTrue(await throttle.check_and_acquire(2))

        logger.info("[FixedWindow] Richiedo 1 (saturo) → atteso False")
        self.assertFalse(await throttle.check_and_acquire(1))

        logger.info("[FixedWindow] Attendo 0.25s per scadenza finestra")
        await asyncio.sleep(0.25)

        logger.info("[FixedWindow] Richiedo 5 post-reset → atteso True")
        self.assertTrue(await throttle.check_and_acquire(5))

        logger.info("[FixedWindow] Richiedo 1 (saturo di nuovo) → atteso False")
        self.assertFalse(await throttle.check_and_acquire(1))

    async def test_concurrent_acquire_is_atomic(self) -> None:
        logger.info("[FixedWindow] 20 richieste concorrenti, capacity 10 → esattamente 10 granted")
        throttle = LocalFixedWindow(capacity=10, window_sec=1.0, name="t")
        results = await asyncio.gather(*[throttle.check_and_acquire(1) for _ in range(20)])
        granted = sum(results)
        logger.info("[FixedWindow] Granted: %d/20", granted)
        self.assertEqual(granted, 10)


# =============================================================================
# LocalSlidingWindow
# =============================================================================

class TestLocalSlidingWindow(unittest.IsolatedAsyncioTestCase):

    async def test_burst_within_capacity_is_immediate(self) -> None:
        logger.info("[SlidingWindow] Burst di 3 su capacity 3 → nessuna attesa")
        throttle = LocalSlidingWindow(capacity=3, window_sec=0.2, name="t")
        start = time.monotonic()
        for i in range(3):
            await throttle.acquire()
            logger.debug("[SlidingWindow] Token %d acquisito a %.4fs", i + 1, time.monotonic() - start)
        elapsed = time.monotonic() - start
        logger.info("[SlidingWindow] Burst completato in %.4fs", elapsed)
        self.assertLess(elapsed, 0.05)

    async def test_acquire_beyond_capacity_blocks(self) -> None:
        logger.info("[SlidingWindow] Capacity 2, window 0.3s → il terzo acquire attende ~0.3s")
        throttle = LocalSlidingWindow(capacity=2, window_sec=0.3, name="t")
        await throttle.acquire()
        await throttle.acquire()
        logger.debug("[SlidingWindow] Burst esaurito, il prossimo acquire deve bloccare")

        start = time.monotonic()
        await throttle.acquire()
        elapsed = time.monotonic() - start
        logger.info("[SlidingWindow] Attesa misurata: %.4fs", elapsed)
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 0.45)

    async def test_concurrent_acquire_serializes(self) -> None:
        logger.info("[SlidingWindow] 10 acquire concorrenti, capacity 5, window 0.1s")
        throttle = LocalSlidingWindow(capacity=5, window_sec=0.1, name="t")
        start = time.monotonic()
        await asyncio.gather(*[throttle.acquire() for _ in range(10)])
        elapsed = time.monotonic() - start
        logger.info("[SlidingWindow] Tutti completati in %.4fs (atteso >= 0.1s)", elapsed)
        self.assertGreaterEqual(elapsed, 0.1)

    async def test_acquire_multi_request(self) -> None:
        """acquire(requested=3) consuma 3 slot in un colpo."""
        w = LocalSlidingWindow(capacity=5, window_sec=0.2, name="t")
        await w.acquire(requested=3)
        self.assertEqual(len(w._timestamps), 3)

    async def test_acquire_requested_above_capacity_raises(self) -> None:
        """requested > capacity → ValueError, non loop infinito."""
        w = LocalSlidingWindow(capacity=5, window_sec=1.0, name="t")
        with self.assertRaises(ValueError):
            await asyncio.wait_for(w.acquire(requested=10), timeout=0.5)

    async def test_locked_returns_same_lock(self) -> None:
        """locked() ritorna sempre la stessa istanza di asyncio.Lock."""
        w = LocalSlidingWindow(capacity=5, window_sec=1.0, name="t")
        self.assertIs(w.locked(), w.locked())
        self.assertIsInstance(w.locked(), asyncio.Lock)

    async def test_check_and_commit_under_lock_split(self) -> None:
        """dry-run poi commit: check non consuma, commit sì."""
        w = LocalSlidingWindow(capacity=3, window_sec=1.0, name="t")
        async with w.locked():
            now = time.monotonic()
            ok, wait = w._check_under_lock(now, 2)
            self.assertTrue(ok)
            self.assertEqual(len(w._timestamps), 0)   # check non consuma
            w._commit_under_lock(now, 2)
            self.assertEqual(len(w._timestamps), 2)   # commit consuma



# =============================================================================
# LocalTokenBucket
# =============================================================================

class TestLocalTokenBucket(unittest.IsolatedAsyncioTestCase):

    async def test_burst_within_capacity_is_immediate(self) -> None:
        logger.info("[LocalTokenBucket] Burst di 5 su capacity 5 → nessuna attesa")
        bucket = LocalTokenBucket(rate=100.0, capacity=5, name="t")
        start = time.monotonic()
        for _ in range(5):
            await bucket.acquire()
        elapsed = time.monotonic() - start
        logger.info("[LocalTokenBucket] Burst completato in %.4fs", elapsed)
        self.assertLess(elapsed, 0.05)

    async def test_acquire_beyond_capacity_blocks(self) -> None:
        logger.info("[LocalTokenBucket] rate 50/s, capacity 1 → secondo acquire ~20ms")
        bucket = LocalTokenBucket(rate=50.0, capacity=1, name="t")
        await bucket.acquire()
        start = time.monotonic()
        await bucket.acquire()
        elapsed = time.monotonic() - start
        logger.info("[LocalTokenBucket] Attesa misurata: %.4fs", elapsed)
        self.assertGreaterEqual(elapsed, 0.015)
        self.assertLess(elapsed, 0.10)

    async def test_requested_zero_is_noop(self) -> None:
        logger.info("[LocalTokenBucket] acquire(requested=0) → no-op, nessun lock")
        bucket = LocalTokenBucket(rate=10.0, capacity=5, name="t")
        start = time.monotonic()
        await bucket.acquire(requested=0)
        elapsed = time.monotonic() - start
        logger.info("[LocalTokenBucket] No-op completato in %.5fs", elapsed)
        self.assertLess(elapsed, 0.01)

    async def test_requested_above_capacity_raises(self) -> None:
        logger.info("[LocalTokenBucket] requested=6 > capacity=5 → ValueError")
        bucket = LocalTokenBucket(rate=10.0, capacity=5, name="t")
        with self.assertRaises(ValueError):
            await bucket.acquire(requested=6)
        logger.info("[LocalTokenBucket] ValueError correttamente sollevata")

    async def test_negative_requested_raises(self) -> None:
        logger.info("[LocalTokenBucket] requested negativo → ValueError")
        bucket = LocalTokenBucket(rate=10.0, capacity=5, name="t")
        with self.assertRaises(ValueError):
            await bucket.acquire(requested=-1)

    async def test_invalid_constructor_args(self) -> None:
        logger.info("[LocalTokenBucket] rate=0 → ValueError")
        with self.assertRaises(ValueError):
            LocalTokenBucket(rate=0.0, capacity=5)
        logger.info("[LocalTokenBucket] capacity=0 → ValueError")
        with self.assertRaises(ValueError):
            LocalTokenBucket(rate=10.0, capacity=0)

    async def test_concurrent_acquire_no_lock_sleep(self) -> None:
        logger.info("[LocalTokenBucket] 10 acquire concorrenti, rate 100/s, capacity 5")
        bucket = LocalTokenBucket(rate=100.0, capacity=5, name="t")
        start = time.monotonic()
        await asyncio.gather(*[bucket.acquire() for _ in range(10)])
        elapsed = time.monotonic() - start
        # 5 attese da 10ms ≈ 50ms. Con sleep-dentro-lock sarebbe ~N * 50ms.
        logger.info(
            "[LocalTokenBucket] Completato in %.4fs (atteso ~0.05s; se fosse "
            "sleep-dentro-lock sarebbe molto di più)", elapsed,
        )
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertLess(elapsed, 0.15)

    async def test_acquire_multi_request(self) -> None:
        """acquire(requested=3) consuma 3 token."""
        b = LocalTokenBucket(rate=10.0, capacity=10, name="t")
        await b.acquire(requested=3)
        self.assertAlmostEqual(b._tokens, 7.0, places=2)

    async def test_locked_returns_same_lock(self) -> None:
        """locked() ritorna sempre la stessa istanza di asyncio.Lock."""
        b = LocalTokenBucket(rate=10.0, capacity=5, name="t")
        self.assertIs(b.locked(), b.locked())
        self.assertIsInstance(b.locked(), asyncio.Lock)

    async def test_check_and_commit_under_lock_split(self) -> None:
        """dry-run con refill idempotente, poi commit."""
        b = LocalTokenBucket(rate=100.0, capacity=5, name="t")
        async with b.locked():
            now = time.monotonic()
            ok, _ = b._check_under_lock(now, 3)
            self.assertTrue(ok)
            self.assertAlmostEqual(b._tokens, 5.0, places=1)  # check non consuma
            b._commit_under_lock(3)
            self.assertAlmostEqual(b._tokens, 2.0, places=1)  # commit consuma


# =============================================================================
# Validazione argomenti (mock Redis, nessun I/O)
# =============================================================================

class TestRedisSlidingWindowArgs(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.redis = _fake_redis_sliding_window()

    async def test_requested_above_limit_raises(self) -> None:
        logger.info("[RedisSlidingWindowArgs] requested=10 > limit=5 → ValueError")
        limiter = RedisSlidingWindow(self.redis, key="k", limit=5, window_ms=1000)
        with self.assertRaises(ValueError):
            await limiter.acquire(requested=10)

    async def test_requested_equal_to_limit_is_allowed(self) -> None:
        logger.info("[RedisSlidingWindowArgs] requested=limit=5 → consentito")
        limiter = RedisSlidingWindow(self.redis, key="k", limit=5, window_ms=1000)
        await limiter.acquire(requested=5)
        logger.info("[RedisSlidingWindowArgs] Nessuna eccezione: ok")


class TestRateLimitManagerArgs(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.redis = _fake_redis_manager()

    async def test_unregistered_bucket_raises_keyerror(self) -> None:
        logger.info("[ManagerArgs] acquire su bucket non registrato → KeyError")
        manager = RateLimitManager(self.redis, exchange="test")
        with self.assertRaises(KeyError):
            await manager.acquire("unknown")

    async def test_key_naming_uses_hash_tag(self) -> None:
        logger.info("[ManagerArgs] La chiave deve contenere {exchange} per Redis Cluster")
        manager = RateLimitManager(self.redis, exchange="bitget")
        manager.add_limiter("candles", limit=10, window_ms=1000)

        await manager.acquire("candles")
        _, kwargs = self.redis.register_script.return_value.call_args
        logger.info("[ManagerArgs] Chiave generata: %s", kwargs["keys"])
        self.assertIn("{bitget}:rate_limit:candles", kwargs["keys"])


# =============================================================================
# ResilientRateLimiter (routing + cooldown)
# =============================================================================

class _FakeLimiter:
    """Fake limiter con interfaccia acquire(*args, **kwargs)."""

    def __init__(self, name: str, fail_with: Exception | None = None):
        self.name = name
        self.calls: list[tuple] = []
        self.fail_with = fail_with

    async def acquire(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        logger.debug("[FakeLimiter:%s] acquire(args=%s, kwargs=%s)", self.name, args, kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return "ok"


class TestResilientRateLimiter(unittest.IsolatedAsyncioTestCase):

    async def test_happy_path_uses_primary(self) -> None:
        logger.info("[Resilient] Path normale: usa il primary, fallback non toccato")
        primary, fallback = _FakeLimiter("primary"), _FakeLimiter("fallback")
        wrapper = ResilientRateLimiter(primary, fallback, cooldown_sec=5.0)

        result = await wrapper.acquire(requested=1)
        logger.info("[Resilient] Result=%s, primary.calls=%d, fallback.calls=%d",
                    result, len(primary.calls), len(fallback.calls))

        self.assertEqual(result, "ok")
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(fallback.calls), 0)

    async def test_redis_error_switches_to_fallback(self) -> None:
        logger.info("[Resilient] Primary solleva RedisError → deve degradare sul fallback")
        primary = _FakeLimiter("primary", fail_with=redis.RedisError("boom"))
        fallback = _FakeLimiter("fallback")
        wrapper = ResilientRateLimiter(primary, fallback, cooldown_sec=5.0)

        result = await wrapper.acquire(requested=1)
        logger.info("[Resilient] Result=%s, primary.calls=%d, fallback.calls=%d",
                    result, len(primary.calls), len(fallback.calls))

        self.assertEqual(result, "ok")
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(fallback.calls), 1)

    async def test_during_cooldown_primary_is_not_called(self) -> None:
        logger.info("[Resilient] Dopo un fail, durante cooldown il primary non deve essere chiamato")
        primary = _FakeLimiter("primary", fail_with=redis.RedisError("boom"))
        fallback = _FakeLimiter("fallback")
        wrapper = ResilientRateLimiter(primary, fallback, cooldown_sec=0.2)

        logger.info("[Resilient] Chiamata 1: apre il circuito")
        await wrapper.acquire()
        logger.info("[Resilient] Chiamata 2: dentro il cooldown (0.2s)")
        await wrapper.acquire()
        logger.info("[Resilient] Chiamata 3: ancora dentro il cooldown")
        await wrapper.acquire()

        logger.info("[Resilient] primary.calls=%d (atteso 1), fallback.calls=%d (atteso 3)",
                    len(primary.calls), len(fallback.calls))
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(fallback.calls), 3)

    async def test_circuit_reopens_after_cooldown_and_closes_on_success(self) -> None:
        logger.info("[Resilient] Ciclo completo: fail → cooldown → retry → recovery")
        primary = _FakeLimiter("primary", fail_with=redis.RedisError("boom"))
        fallback = _FakeLimiter("fallback")
        wrapper = ResilientRateLimiter(primary, fallback, cooldown_sec=0.05)

        logger.info("[Resilient] Chiamata 1: fallisce, apre il circuito")
        await wrapper.acquire()
        logger.info("[Resilient] Attendo 0.06s (> cooldown 0.05s)")
        await asyncio.sleep(0.06)
        primary.fail_with = None
        logger.info("[Resilient] Chiamata 2: primary sano, deve chiudere il circuito")
        await wrapper.acquire()

        logger.info("[Resilient] primary.calls=%d (atteso 2), fallback.calls=%d (atteso 1)",
                    len(primary.calls), len(fallback.calls))
        self.assertEqual(len(primary.calls), 2)
        self.assertEqual(len(fallback.calls), 1)

    async def test_cooldown_negative_raises(self) -> None:
        logger.info("[Resilient] cooldown_sec negativo → ValueError in costruzione")
        with self.assertRaises(ValueError):
            ResilientRateLimiter(_FakeLimiter("p"), _FakeLimiter("f"), cooldown_sec=-1.0)

    async def test_args_and_kwargs_are_forwarded(self) -> None:
        logger.info("[Resilient] Gli argomenti devono essere inoltrati identici")
        primary, fallback = _FakeLimiter("primary"), _FakeLimiter("fallback")
        wrapper = ResilientRateLimiter(primary, fallback)

        await wrapper.acquire("a", "b", requested=3)
        args, kwargs = primary.calls[0]
        logger.info("[Resilient] Ricevuti dal primary: args=%s kwargs=%s", args, kwargs)

        self.assertEqual(args, ("a", "b"))
        self.assertEqual(kwargs, {"requested": 3})


# =============================================================================
# Fallback manager (composizione locale)
# =============================================================================

class TestLocalManagerFallback(unittest.IsolatedAsyncioTestCase):

    async def test_unknown_bucket_raises(self) -> None:
        """Bucket sconosciuto → KeyError."""
        fb = _LocalManagerFallback(
            buckets={"a": (10, 1000)}, expected_workers=1, safety_factor=0.8,
        )
        with self.assertRaises(KeyError):
            await fb.acquire("missing")

    async def test_atomic_success_returns_remaining(self) -> None:
        """Tutti i bucket hanno capacità → tutti consumati, remaining effettivo."""
        fb = _LocalManagerFallback(
            buckets={"a": (10, 1000), "b": (5, 1000)},
            expected_workers=1, safety_factor=0.8,
        )
        result = await fb.acquire("a", "b")
        # capacity locale: a=8, b=4. Dopo 1 acquire: 7 e 3.
        self.assertEqual(result, {"a": 7, "b": 3})
        self.assertEqual(len(fb._local["a"]._timestamps), 1)
        self.assertEqual(len(fb._local["b"]._timestamps), 1)

    async def test_atomic_partial_failure_no_consumption(self) -> None:
        """Un bucket saturo → NESSUN consumo sugli altri (ADR-0009)."""
        fb = _LocalManagerFallback(
            buckets={"a": (2, 1000), "b": (10, 1000)},
            expected_workers=1, safety_factor=1.0,
        )
        # Saturare 'a' (capacity=2)
        await fb.acquire("a")
        await fb.acquire("a")
        self.assertEqual(len(fb._local["a"]._timestamps), 2)
        self.assertEqual(len(fb._local["b"]._timestamps), 0)

        # Chiedere (a, b): deve bloccarsi senza consumare 'b'
        task = asyncio.create_task(fb.acquire("a", "b"))
        await asyncio.sleep(0.1)
        self.assertEqual(
            len(fb._local["b"]._timestamps), 0,
            "'b' è stato consumato nonostante 'a' fosse saturo!",
        )
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_requested_above_bucket_capacity_raises(self) -> None:
        """requested > capacity locale di un bucket → ValueError."""
        fb = _LocalManagerFallback(
            buckets={"a": (2, 1000)}, expected_workers=1, safety_factor=1.0,
        )
        with self.assertRaises(ValueError):
            await fb.acquire("a", requested=10)

    async def test_no_deadlock_with_overlapping_sets(self) -> None:
        """Due chiamate con set {a,b} e {b,c} non deadlockano (ordine alfabetico)."""
        fb = _LocalManagerFallback(
            buckets={"a": (10, 1000), "b": (10, 1000), "c": (10, 1000)},
            expected_workers=1, safety_factor=1.0,
        )
        await asyncio.wait_for(
            asyncio.gather(fb.acquire("a", "b"), fb.acquire("b", "c")),
            timeout=1.0,
        )

    async def test_local_capacity_is_scaled(self) -> None:
        """limit 10, safety 0.5, workers 2 → capacity locale 2."""
        fb = _LocalManagerFallback(
            buckets={"a": (10, 1000)}, expected_workers=2, safety_factor=0.5,
        )
        start = time.monotonic()
        await fb.acquire("a")
        await fb.acquire("a")
        # Il terzo acquire deve attendere ~1s (window 1000ms)
        await fb.acquire("a")
        self.assertGreaterEqual(time.monotonic() - start, 0.05)


class TestLocalTokenBucketManagerFallback(unittest.IsolatedAsyncioTestCase):

    async def test_unknown_bucket_raises(self) -> None:
        """Bucket sconosciuto → KeyError."""
        fb = _LocalTokenBucketManagerFallback(
            buckets={"a": (10.0, 5)}, expected_workers=1, safety_factor=0.8,
        )
        with self.assertRaises(KeyError):
            await fb.acquire("missing")

    async def test_atomic_success_returns_remaining(self) -> None:
        """Tutti i bucket consumati, remaining effettivo."""
        fb = _LocalTokenBucketManagerFallback(
            buckets={"a": (100.0, 5), "b": (100.0, 5)},
            expected_workers=1, safety_factor=1.0,
        )
        result = await fb.acquire("a", "b", requested=1)
        self.assertAlmostEqual(result["a"], 4.0, places=1)
        self.assertAlmostEqual(result["b"], 4.0, places=1)

    async def test_atomic_partial_failure_no_consumption(self) -> None:
        """Un bucket esaurito → NESSUN consumo sugli altri (ADR-0009).
    
        Rallentiamo il refill di 'a' (rate=1.0) così che il breve sleep del test
        non gli permetta di rigenerare un token: vogliamo osservare il primo
        tentativo, non un retry riuscito.
        """
        fb = _LocalTokenBucketManagerFallback(
            buckets={"a": (1.0, 2), "b": (100.0, 100)},
            expected_workers=1, safety_factor=1.0,
        )
        # Esaurire 'a' (capacity=2)
        await fb.acquire("a")
        await fb.acquire("a")
    
        task = asyncio.create_task(fb.acquire("a", "b"))
        await asyncio.sleep(0.1)
        self.assertAlmostEqual(
            fb._local["b"]._tokens, 100.0, places=1,
            msg="'b' è stato consumato nonostante 'a' fosse esaurito!",
        )
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_requested_above_bucket_capacity_raises(self) -> None:
        """requested > capacity locale di un bucket → ValueError."""
        fb = _LocalTokenBucketManagerFallback(
            buckets={"a": (10.0, 2)}, expected_workers=1, safety_factor=1.0,
        )
        with self.assertRaises(ValueError):
            await fb.acquire("a", requested=10)

    async def test_no_deadlock_with_overlapping_sets(self) -> None:
        """Due chiamate con set {a,b} e {b,c} non deadlockano."""
        fb = _LocalTokenBucketManagerFallback(
            buckets={"a": (10.0, 10), "b": (10.0, 10), "c": (10.0, 10)},
            expected_workers=1, safety_factor=1.0,
        )
        await asyncio.wait_for(
            asyncio.gather(fb.acquire("a", "b"), fb.acquire("b", "c")),
            timeout=1.0,
        )

# =============================================================================
# Factory functions
# =============================================================================

class TestFactories(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        # Le factory costruiscono sia manager sia sliding window: serve un fake
        # che sappia rispondere a entrambi i formati.
        # Il modo più semplice è un fake che decide in base al numero di chiavi.
        async def _fake_lua(*args, **kwargs):
            keys = kwargs.get("keys", [])
            if len(keys) == 1:
                return (1, 0)             # RedisSlidingWindow
            flat: list = []
            for k in keys:
                flat.append(k)
                flat.append(9)            # RateLimitManager
            return (1, flat)

        fake = MagicMock()
        fake.register_script = MagicMock(return_value=AsyncMock(side_effect=_fake_lua))
        self.redis = fake

    async def test_build_resilient_token_bucket(self) -> None:
        logger.info("[Factory:token_bucket] rate=20, capacity=10, workers=2, safety=0.5")
        wrapper = build_resilient_token_bucket(
            self.redis, key="k", rate=20.0, capacity=10,
            expected_workers=2, safety_factor=0.5, cooldown_sec=3.0,
        )
        logger.info("[Factory:token_bucket] fallback.rate=%.2f (atteso 5.0)", wrapper.fallback.rate)
        logger.info("[Factory:token_bucket] fallback.capacity=%d (atteso 2)", wrapper.fallback.capacity)

        self.assertIsInstance(wrapper, ResilientRateLimiter)
        self.assertIsInstance(wrapper.primary, RedisTokenBucket)
        self.assertIsInstance(wrapper.fallback, LocalTokenBucket)
        self.assertAlmostEqual(wrapper.fallback.rate, 5.0)
        self.assertEqual(wrapper.fallback.capacity, 2)
        self.assertEqual(wrapper.cooldown_sec, 3.0)

    async def test_build_resilient_sliding_window(self) -> None:
        logger.info("[Factory:sliding_window] limit=10, workers=1, safety=0.8")
        wrapper = build_resilient_sliding_window(
            self.redis, key="k", limit=10, window_ms=1000,
            expected_workers=1, safety_factor=0.8,
        )
        logger.info("[Factory:sliding_window] fallback.capacity=%d (atteso 8)", wrapper.fallback.capacity)
        self.assertIsInstance(wrapper.fallback, LocalSlidingWindow)
        self.assertEqual(wrapper.fallback.capacity, 8)

    async def test_build_resilient_manager(self) -> None:
        logger.info("[Factory:manager] buckets={candles: (10,1000), time: (5,1000)}")
        wrapper = build_resilient_manager(
            self.redis, exchange="bitget",
            buckets={"candles": (10, 1000), "time": (5, 1000)},
            expected_workers=1, safety_factor=0.8,
        )
        logger.info("[Factory:manager] primary buckets: %s", list(wrapper.primary.buckets))
        self.assertIsInstance(wrapper.primary, RateLimitManager)
        self.assertIsInstance(wrapper.fallback, _LocalManagerFallback)
        self.assertIn("candles", wrapper.primary.buckets)
        self.assertIn("time", wrapper.primary.buckets)

    async def test_build_resilient_token_bucket_manager(self) -> None:
        logger.info("[Factory:tb_manager] buckets={global: (50,50), candles: (10,10)}")
        wrapper = build_resilient_token_bucket_manager(
            self.redis, exchange="bitget",
            buckets={"global": (50.0, 50), "candles": (10.0, 10)},
            expected_workers=1, safety_factor=0.8,
        )
        logger.info("[Factory:tb_manager] primary buckets: %s", list(wrapper.primary.buckets))
        self.assertIsInstance(wrapper.fallback, _LocalTokenBucketManagerFallback)
        self.assertIn("global", wrapper.primary.buckets)
        self.assertIn("candles", wrapper.primary.buckets)

    async def test_factory_minimum_capacity_is_one(self) -> None:
        logger.info("[Factory:min_capacity] capacity=1, workers=100 → fallback.capacity deve essere 1")
        wrapper = build_resilient_token_bucket(
            self.redis, key="k", rate=1.0, capacity=1,
            expected_workers=100, safety_factor=0.8,
        )
        logger.info("[Factory:min_capacity] fallback.capacity=%d", wrapper.fallback.capacity)
        self.assertEqual(wrapper.fallback.capacity, 1)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    unittest.main()