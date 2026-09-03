# core/rate_limiter.py
import asyncio
import time
import redis.asyncio as redis
from typing import cast
import logging

# Logger standard per modulo
logger = logging.getLogger(__name__)

class RedisTokenBucket:
    """
    Implementazione di un Token Bucket distribuito su Redis.
    Garantisce il rispetto dei Rate Limit delle API in architetture multi-container.
    """
    def __init__(self, redis_client: redis.Redis, key: str, rate: float, capacity: int):
        self.redis = redis_client
        # Prefisso di sicurezza per evitare collisioni con altre chiavi
        self.key = f"rate_limit:{key}"
        self.rate = rate          # Token generati al secondo (es. 10)
        self.capacity = capacity  # Burst massimo consentito (es. 20)

        # register_script() compila lo script Lua sul server Redis in SHA1
        # Questo evita di inviare l'intero testo dello script ad ogni chiamata,
        # azzerando l'overhead di rete e massimizzando le performance.
        self._lua_script = self.redis.register_script("""
            local key = KEYS[1]
            local capacity = tonumber(ARGV[1])
            local rate_per_ms = tonumber(ARGV[2]) / 1000
            local now = tonumber(ARGV[3])
            local requested = tonumber(ARGV[4])

            -- Recuperiamo i token attuali e l'ultimo timestamp di aggiornamento
            local info = redis.call('HMGET', key, 'tokens', 'last_refill')
            local tokens = tonumber(info[1])
            local last_refill = tonumber(info[2])

            -- Inizializzazione al primo avvio
            if not tokens then
                tokens = capacity
                last_refill = now
            end

            -- Calcolo del tempo trascorso e dei token da ricaricare
            local delta_ms = math.max(0, now - last_refill)
            local new_tokens = math.floor(delta_ms * rate_per_ms)

            if new_tokens > 0 then
                -- Non possiamo superare la capacità massima del secchio
                tokens = math.min(capacity, tokens + new_tokens)
                last_refill = now
            end

            if tokens >= requested then
                -- Token disponibile: aggiorniamo i dati
                tokens = tokens - requested
                redis.call('HMSET', key, 'tokens', tokens, 'last_refill', last_refill)
                
                -- Impostiamo un TTL per auto-cancellare la chiave se il bot viene spento
                local ttl_ms = math.ceil((capacity / rate_per_ms)) + 5000
                redis.call('PEXPIRE', key, ttl_ms)
                
                return 0 -- Successo, zero millisecondi di attesa
            else
                -- Token NON disponibile: calcoliamo i millisecondi esatti da aspettare
                local ms_to_wait = math.ceil((requested - tokens) / rate_per_ms) - delta_ms
                if ms_to_wait < 1 then ms_to_wait = 10 end -- Sicurezza per arrotondamenti
                return ms_to_wait
            end
        """)

    async def acquire(self, requested: int = 1):
        """
        Tenta di acquisire un token. Se il rate limit è raggiunto, mette in pausa
        il task corrente per il tempo strettamente necessario e riprova.
        """
        while True:
            # Calcoliamo il timestamp in Python (iniettarlo dall'esterno è una 
            # best practice per mantenere deterministici gli script Lua in Redis)
            now_ms = int(time.time() * 1000)
            
            # Eseguiamo lo script
            wait_ms = await self._lua_script(
                keys=[self.key],
                args=[self.capacity, self.rate, now_ms, requested]
            )

            # Log a livello DEBUG: utile per il debug e i test, ignorato in produzione
            # se il livello del logger è INFO o superiore (overhead trascurabile)
            logger.debug("TokenBucket %s: requested=%s wait_ms=%s", self.key, requested, wait_ms)


            # Se Redis ci dice 0 (o un nuomero negativo, impossibile), abbiamo il semaforo verde
            if cast(int, wait_ms) < 1:
                return
            
            # Altrimenti dormiamo in modo asincrono esattamente per il tempo calcolato
            await asyncio.sleep(cast(int, wait_ms) / 1000.0)

class RateLimitManager:
    def __init__(self, redis_client: redis.Redis, exchange: str):
        self.redis = redis_client
        self.exchange = exchange
        self.buckets = {}

    def add_bucket(self, name: str, rate: float, capacity: int):
        self.buckets[name] = RedisTokenBucket(
            redis_client=self.redis,
            key=f"rate_limit:{self.exchange}:{name}",
            rate=rate,
            capacity=capacity
        )

    async def acquire(self, *bucket_names: str, requested: int = 1):
        """Acquisisce token da tutti i bucket specificati."""
        for name in bucket_names:
            await self.buckets[name].acquire(requested=requested)