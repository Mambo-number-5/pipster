# ADR-0005: Composizione del fallback rate limiter tramite wrapper

**Data**: 2026-09-26  
**Stato**: Accettato  
**Decisori**: [owner del progetto]

## Contesto

ADR-0004 ha deciso un fallback locale bounded per il rate limiter quando Redis
è irraggiungibile, specificando anche un meccanismo: "ogni `RedisTokenBucket`,
`RedisSlidingWindow` e `RateLimitManager` istanzia un limiter locale gemello".
Quel meccanismo viola il Single Responsibility Principle: le classi Redis
finirebbero per gestire due cose distinte — il rate limiting distribuito e la
resilienza a Redis down — con quattro parametri di fallback ciascuna, codice
duplicato in ogni classe, e stato di degraded mode sparso.

Serve un meccanismo che preservi la decisione di ADR-0004 (fallback locale
bounded) separando le responsabilità. La scelta deve essere coerente con
ADR-0003 (composizione preferita all'ereditarietà).

## Decisione

Il fallback è implementato come **wrapper di composizione**:

    ResilientRateLimiter(primary, fallback, cooldown_sec=5.0)

Il wrapper espone `acquire(*args, **kwargs)` e:
- tenta `primary.acquire(...)`;
- su `redis.RedisError`, apre il circuito per `cooldown_sec` secondi e
  instrada su `fallback.acquire(...)`;
- durante il cooldown, ogni chiamata va direttamente al fallback;
- al primo successo del primary, chiude il circuito.

Le classi `RedisTokenBucket`, `RedisSlidingWindow`, `RateLimitManager` e
`TokenBucketManager` tornano alla loro responsabilità singola: rate limiting distribuito su Redis, senza alcuna nozione di fallback, cooldown o degraded mode.

Il `fallback` è un limiter locale (`LocalTokenBucket`,  `LocalSlidingWindow`) con rate ridotto. Il calcolo del rate ridotto (`rate * safety_factor / expected_workers`) avviene **al punto di costruzione  del fallback**, non nel wrapper e non nella classe Redis. Il wrapper riceve
due limiter già configurati e non conosce la matematica del dimensionamento.

La corrispondenza primary↔fallback non è discrezionale: il fallback deve
avere la stessa semantica del primary, altrimenti il contratto cambia in
degraded mode. Le factory garantiscono l'accoppiamento:

- `RedisTokenBucket` → `LocalTokenBucket` (rate + capacity)
- `RedisSlidingWindow` → `LocalSlidingWindow` (conteggio in finestra)
- `TokenBucketManager` → `_LocalTokenBucketManagerFallback` su `LocalTokenBucket`
- `RateLimitManager` → `_LocalManagerFallback` su `LocalSlidingWindow`
  (il primary è sliding-window per costruzione: ZSET per bucket)


## Alternative considerate

1. **Modificare le classi Redis per contenere il fallback.** Pro: una sola
   classe da istanziare. Contro: viola SRP, quattro parametri di fallback per
   classe, duplicazione del ciclo try/except, stato di degraded mode sparso
   in N punti. Scartata.

2. **Ereditarietà (`ResilientRedisTokenBucket(RedisTokenBucket)`).** Pro:
   riuso del codice. Contro: accoppia la resilienza al tipo specifico del
   primary, impedendo di comporre `RateLimitManager` (che ha interfaccia
   diversa) senza una gerarchia parallela. Va contro ADR-0003. Scartata.

3. **Wrapper di composizione (scelta).** Pro: una responsabilità per classe,
   testabile in isolamento, generico sull'interfaccia `acquire`, nessun
   parametro di fallback nelle classi Redis, coerente con ADR-0003. Contro:
   un oggetto in più da costruire al bootstrap; il punto di costruzione deve
   conoscere sia primary sia fallback.

4. **Fallback manager polimorfico (parametro `type`).** Pro: un solo
   costruttore per più semantiche. Contro: il primary `RateLimitManager` è
   sliding-window per costruzione; un ramo token-bucket sarebbe codice morto,
   e la configurabilità suggerirebbe una flessibilità che non esiste. Se
   servirà un manager multi-bucket a token bucket, sarà un primary diverso
   (`TokenBucketManager`), con il suo fallback e la sua factory. Scartata.

## Conseguenze

**Positive**
- `RedisTokenBucket`, `RedisSlidingWindow`, `RateLimitManager` restano pure.
- Il wrapper è testabile con due fake limiter, senza Redis.
- Un solo posto dove la politica di resilienza è definita e modificata.
- Base pronta per il circuit breaker (roadmap C8): si sostituisce il wrapper,
  non le classi Redis.

**Negative**
- Il punto di costruzione (bootstrap del worker) diventa più articolato:
  costruire primary, costruire fallback, avvolgere.
- Se in futuro servissero politiche di resilienza diverse per limiter diversi
  (es. cooldown differenti), serviranno più istanze del wrapper.

**Neutre**
- ADR-0004 resta valido per la decisione "fallback locale bounded"; viene
  integrato da questo ADR solo per la parte "Ambito / Meccanismo".
- Lo `Stato` di ADR-0004 diventa `Integrato da ADR-0005`.
- Il wrapper non gestisce retry interni né backoff: delega al primary, che
  già ritenta per conto suo dove serve.

## Riferimenti

- Moduli coinvolti: `core/rate_limiter.py` (`ResilientRateLimiter`,
  `LocalTokenBucket`, `LocalSlidingWindow`)
- ADR correlati: ADR-0003 (composizione vs ereditarietà),
  ADR-0004 (fallback locale rate limiter, integrato)
- Stato di pianificazione: `docs/roadmap.md`