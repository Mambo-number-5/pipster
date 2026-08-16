# tests/stress/test_rate_limiter.py
import os
import asyncio
import time
import unittest
import redis.asyncio as redis
import logging

from core.rate_limiter import RedisTokenBucket

logging.basicConfig(level=logging.DEBUG)

class TestRateLimiterStress(unittest.IsolatedAsyncioTestCase):
    
    async def asyncSetUp(self):
        """
        Inizializza una connessione reale al server Redis.
        Usa il DB 15 per evitare di sovrascrivere i dati del bot nel DB 0.
        """
        self.host = os.getenv("REDIS_HOST", "localhost")
        self.port = int(os.getenv("REDIS_PORT", 6379))
        self.redis_client = redis.Redis(host=self.host, port=self.port, db=15, decode_responses=True)
        await self.redis_client.flushdb()

    async def asyncTearDown(self):
        """
        Pulizia finale e chiusura della connessione.
        """
        await self.redis_client.flushdb()
        await self.redis_client.aclose()

    async def test_stress_token_bucket_concurrency(self):
        """
        Obiettivo: 55 richieste simultanee. 
        Capacità (Burst): 5 token. 
        Rate (Ricarica): 10 token al secondo.
        Le prime 5 passano subito. Le restanti 50 impiegano ~5 secondi.
        """
        rate = 10.0
        capacity = 5
        limiter = RedisTokenBucket(self.redis_client, key="stress_test_1", rate=rate, capacity=capacity)
        
        num_requests = 55

        # --- FASE DI WARMUP (Risolve il Cold Start in Docker) ---
        # Forza Redis a compilare lo script e sveglia il pool di connessioni
        await limiter.acquire(requested=0)
        await self.redis_client.delete(limiter.key) # Resettiamo il secchio a pieno
        # --------------------------------------------------------

        async def worker(worker_id: int):
            await limiter.acquire(requested=1)
            return time.time() - start_time

        start_time = time.time()

        # Lanciamo 55 worker ESATTAMENTE nello stesso istante
        tasks = [worker(i) for i in range(num_requests)]
        completion_times = await asyncio.gather(*tasks)
        
        total_elapsed = time.time() - start_time
        
        immediate_requests = sum(1 for t in completion_times if t < 0.1)
        
        print(f"\n[Test 1] Tempo totale per {num_requests} richieste: {total_elapsed:.2f}s")
        print(f"[Test 1] Richieste passate istantaneamente (Burst): {immediate_requests}")
        
        self.assertGreaterEqual(immediate_requests, capacity, "Il burst iniziale ha fallito.")
        self.assertGreaterEqual(total_elapsed, 5.0, "Il Rate Limiter è troppo veloce.")
        self.assertLess(total_elapsed, 6.5, "Il Rate Limiter è troppo lento.")

    async def test_stress_token_bucket_custom_requested(self):
        """
        Test specifico per verificare richieste multiple simultanee.
        """
        rate = 20.0
        capacity = 20
        limiter = RedisTokenBucket(self.redis_client, key="stress_test_2", rate=rate, capacity=capacity)
        
        start_time = time.time()
        await limiter.acquire(requested=20)
        burst_elapsed = time.time() - start_time
        
        self.assertLess(burst_elapsed, 0.1, "Il consumo del secchio pieno non è stato istantaneo!")
        
        # Secchio vuoto. Richiediamo altri 10 token. Tempo previsto: 0.5s.
        start_time_2 = time.time()
        await limiter.acquire(requested=10)
        recovery_elapsed = time.time() - start_time_2
        
        print(f"\n[Test 2] Tempo di ricarica per 10 token: {recovery_elapsed:.2f}s")
        
        self.assertGreaterEqual(recovery_elapsed, 0.5, "La ricarica è avvenuta troppo velocemente.")
        self.assertLess(recovery_elapsed, 0.7, "La ricarica è troppo lenta.")

if __name__ == '__main__':
    unittest.main()