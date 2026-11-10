# Rate Limiting

> Stato: completo per il layer rate-limiter. La sezione 5 (integrazione con il
> worker) sarà completata dopo A3.
>
> Decisioni architetturali: ADR-0002, ADR-0004, ADR-0005.

## 1. Perché serve

Ogni exchange impone un numero massimo di richieste per unità di tempo. Superarlo
significa, nella migliore delle ipotesi, ricevere errori `429 Too Many Requests`;
nella peggiore, un ban temporaneo dell'IP o della chiave API, che mette fuori uso
l'ingestion per minuti o ore.

Il rate limiting di Pipster ha tre requisiti che lo distinguono da un semplice
`sleep`:

1. **Distribuito.** Più worker possono girare in parallelo. Il limite è globale
   per l'exchange, non per processo. Serve uno stato condiviso.
2. **Atomico.** Quando una chiamata consuma budget su più limiti
   (es. limite globale + limite per endpoint), o li consuma tutti o nessuno.
   Non deve esistere il consumo parziale.
3. **Resiliente.** Se Redis è irraggiungibile, il worker non deve né bloccarsi
   né ignorare il limite. Deve degradare con un budget ridotto.

## 2. Concetti: token bucket vs sliding window

Due modelli di rate limiting, con semantiche diverse.

**Token bucket.** Il secchio contiene N token, ricaricati a velocità costante.
Ogni chiamata consuma un token. Se il secchio è vuoto, si attende il tempo di
ricarica. Consente *burst*: se il secchio è pieno, N richieste passano
istantaneamente. È il modello giusto quando l'exchange documenta un rate medio
e tollera picchi.

**Sliding window.** Si contano le richieste effettuate negli ultimi `window_ms`
millisecondi. Se il conteggio è sotto il limite, la richiesta passa; altrimenti
si attende che la più vecchia esca dalla finestra. Non consente burst oltre il
limite: in ogni istante, il numero di richieste nella finestra non supera il
tetto. È il modello giusto quando l'exchange documenta "max N richieste in
qualsiasi intervallo di durata W".

I due modelli non sono intercambiabili: con gli stessi parametri numerici
producono throughput diversi. La scelta dipende da cosa l'exchange documenta,
non da preferenze.

## 3. Architettura

Il modulo `core/rate_limiter.py` contiene tre famiglie di classi:

- **Limiter Redis** (distribuiti, usati in condizioni normali):
  `RedisTokenBucket`, `RedisSlidingWindow`, `RateLimitManager`,
  `TokenBucketManager`.
- **Limiter locali** (in-memory, usati come fallback):
  `LocalTokenBucket`, `LocalSlidingWindow`, `LocalFixedWindow`.
- **Wrapper e factory** (composizione e resilienza):
  `ResilientRateLimiter`, `build_resilient_*`.

### 3.1 RedisTokenBucket

Token bucket distribuito, implementato in uno script Lua registrato su Redis.
Stato in un hash Redis (`tokens`, `last_refill`); TTL automatico per non lasciare
chiavi orfane.

Uso:

```python
limiter = RedisTokenBucket(redis, key="candles", rate=10.0, capacity=20)
await limiter.acquire()               # 1 token
await limiter.acquire(requested=5)    # 5 token, attende se necessario
```

`rate` è token/secondo; `capacity` è il burst massimo. `requested > capacity`
solleva `ValueError`.

### 3.2 RedisSlidingWindow

Sliding window distribuita su ZSET. Ogni chiamata è un elemento con score
timestamp. Lo script Lua pulisce gli elementi fuori finestra, conta quelli
attivi, e o aggiunge la richiesta o calcola l'attesa.

Uso:

```python
limiter = RedisSlidingWindow(redis, key="orders", limit=10, window_ms=1000)
remaining = await limiter.acquire()   # ritorna i token residui nella finestra
```

`requested > limit` solleva `ValueError`.

### 3.3 RateLimitManager

Coordina più limiti **atomicamente**. Dato un set di bucket registrati,
`acquire("global", "candles")` o consuma un token su entrambi o non ne consuma
su nessuno. Se uno è saturo, attende il tempo necessario al più vincolante.

Uso:

```python
mgr = RateLimitManager(redis, exchange="bitget")
mgr.add_limiter("global", limit=20, window_ms=1000)
mgr.add_limiter("candles", limit=10, window_ms=1000)
remaining = await mgr.acquire("global", "candles")
# → {"global": 19, "candles": 9}
```

Le chiavi usano hash tag `{bitget}` per garantire che in Redis Cluster tutte le
chiavi di un manager finiscano sullo stesso nodo.
Questo previene l'errore `CROSSSLOT Keys in request don't hash to the same slot`
durante l'esecuzione degli script Lua multi-key.

`acquire` su un bucket non registrato solleva `KeyError`; `requested > limit` di
un bucket solleva `ValueError`.

### 3.4 TokenBucketManager

Come `RateLimitManager`, ma compone più `RedisTokenBucket` invece di un singolo
script multi-key. L'acquisizione è **parallela** (`asyncio.gather`), non atomica:
ogni bucket consuma indipendentemente. Va usato quando l'atomicità tra bucket
non è richiesta.

### 3.5 ResilientRateLimiter

Wrapper di composizione introdotto da ADR-0005. Non conosce Redis, non calcola
rate: riceve due limiter già configurati e instrada.

```python
wrapper = ResilientRateLimiter(primary, fallback, cooldown_sec=5.0)
```

Comportamento:

- Condizioni normali: `primary.acquire(...)`.
- Su `redis.RedisError`: apre il circuito per `cooldown_sec` secondi, tutte le
  chiamate vanno al `fallback`.
- Dopo il cooldown: ritenta il primary; al primo successo, chiude il circuito.

Le factory costruiscono wrapper già cablati:

```python
wrapper = build_resilient_token_bucket(
    redis, key="candles", rate=10.0, capacity=20,
    expected_workers=4, safety_factor=0.8, cooldown_sec=5.0,
)
```

Tre varianti: `build_resilient_token_bucket`, `build_resilient_sliding_window`,
`build_resilient_manager`, `build_resilient_token_bucket_manager`.

Il fallback ha **stessa semantica** del primary: token bucket per token bucket,
sliding window per sliding window. La corrispondenza è garantita dalle factory.

#### 3.5.1 Log operativi

Il wrapper emette log ai cambi di stato:

- **Ingresso in degraded mode** (`WARNING`): al primo `redis.RedisError`.
  Picchi di WARNING indicano Redis instabile o caduto.
- **Chiamata in degraded mode** (`DEBUG`): ogni `acquire` instradato al fallback.
  Utile per misurare la durata del degrado.
- **Ripristino** (`INFO`): al primo successo del primary dopo il cooldown.

Le metriche Prometheus agganceranno questi stessi punti di emissione.

## 4. Configurazione

### 4.1 Parametri

| Parametro | Tipo | Default | Descrizione |
|---|---|---|---|
| `expected_workers` | int | **obbligatorio** | Numero di worker concorrenti previsti |
| `safety_factor` | float | `0.8` | Frazione del rate globale da usare in fallback |
| `cooldown_sec` | float | `5.0` | Secondi di degraded mode dopo un errore Redis |

`expected_workers` è **obbligatorio senza default**: se manca, l'app non parte.
È un parametro critico — se sottostimato, il fallback può superare il limite
globale.

### 4.2 Variabili d'ambiente

Lette da `Settings` (pydantic, A9):

| Variabile | Mappa su |
|---|---|
| `RATE_LIMIT_FALLBACK_ENABLED` | abilita il fallback locale |
| `RATE_LIMIT_EXPECTED_WORKERS` | `expected_workers` |
| `RATE_LIMIT_FALLBACK_COOLDOWN_SEC` | `cooldown_sec` |
| `RATE_LIMIT_FALLBACK_SAFETY_FACTOR` | `safety_factor` |

In `.env.example` sono presenti con commento esplicativo. In Docker Compose di
sviluppo `RATE_LIMIT_EXPECTED_WORKERS=1`.

### 4.3 Formula del rate ridotto in degraded mode

Quando il fallback è attivo, il limiter locale usa:

```
rate_locale   = rate_globale * safety_factor / expected_workers
capacity_locale = max(1, int(capacity_globale * safety_factor / expected_workers))
```

Con `rate_globale=20`, `safety_factor=0.8`, `expected_workers=4`:

```
rate_locale = 20 * 0.8 / 4 = 4 token/s
capacity_locale = max(1, int(20 * 0.8 / 4)) = 4
```

Il `max(1, ...)` garantisce che il fallback abbia almeno capacità 1 anche con
valori molto piccoli.

## 5. Uso nel worker

<!-- TODO: completare dopo A3 (riscrittura del BitgetIngestionWorker) -->

Il worker costruirà i limiter tramite le factory e li userà come contesto
attorno alle chiamate REST. Quindi il worker costruisce i limiter via factory
e li inietta nei client; i client li usano internamente. Il worker non chiama
mai acquire direttamente. Le decisioni su quali bucket registrare e con quali
limiti dipendono da cosa Bitget documenta (vedi `BITGET_API_Notes.md`).

## 6. Test e debug

I test sono organizzati in due cartelle:

- `tests/unit/test_rate_limiter.py` — limiter locali, validazione argomenti,
  routing del `ResilientRateLimiter`, factory. Nessun Redis: mock dove serve.
- `tests/integration/test_rate_limiter.py` — limiter Redis-based,
  `RateLimitManager`, `TokenBucketManager`, `ResilientRateLimiter` con Redis
  reale (db 15).

Per eseguire con log visibile:

```bash
python -m unittest tests/unit/test_rate_limiter.py
```

Il livello di log è controllato da `PIPSTER_LOG_LEVEL` (default `INFO`). In
`launch.json`, il profilo `Debug Unit Tests` imposta la variabile a `DEBUG`
quando serve.

Il logging è configurato a livello di modulo con `logging.basicConfig`, con
livello letto da `PIPSTER_LOG_LEVEL`. In A9 questa configurazione sarà
centralizzata in `core/logging_setup.py` e condivisa con la produzione.

## 7. Riferimenti ADR

- **ADR-0002** — Sliding window atomica: design dello script Lua multi-key.
- **ADR-0004** — Fallback locale bounded: perché il fallback esiste, parametri,
  formula.
- **ADR-0005** — Composizione del fallback rate limiter: perché `ResilientRateLimiter`
  è un wrapper e non un'ereditarietà.

