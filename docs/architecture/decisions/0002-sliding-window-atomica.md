# ADR-0002: Sliding Window atomica multi-bucket per il rate limiting REST

**Data**: 2026-09-20  
**Stato**: Accettato  
**Decisori**: Team di sviluppo Pipster

---

## Contesto

Il client REST Bitget deve rispettare simultaneamente molteplici limiti imposti dall'exchange:

- **Limite globale per IP**: 6000 richieste/minuto sul dominio comune.
- **Limiti per endpoint**: variabili, tipicamente 10–20 richieste/secondo per endpoint pubblico (es. candele).
- **Limiti per UID**: per le chiamate autenticate, quote separate per utente.

In un'architettura multi-worker (container Docker concorrenti sullo stesso IP, o più istanze distribuite), questi limiti sono **condivisi tra processi**. Un singolo worker che li supera può causare il blocco dell'intero IP, con impatto su tutti gli altri worker e potenzialmente sull'attività di trading.

La prima implementazione adottata era un **token bucket distribuito su Redis** (`RedisTokenBucket`), con un `TokenBucketManager` che acquisiva token da più bucket in sequenza tramite `asyncio.gather`. Questa soluzione presentava due problemi:

1. **Modello di rate limit non allineato**: gli exchange misurano le richieste in una **finestra temporale scorrevole** ("N richieste nell'ultimo minuto"), non con un secchio che si ricarica gradualmente. Il token bucket approssima questo comportamento, ma in scenari di burst ravvicinati può concedere più richieste di quante l'exchange ne accetti nella finestra reale.

2. **Acquisizione non atomica multi-bucket**: quando una richiesta doveva consumare token da più bucket contemporaneamente (es. limite IP + limite endpoint + limite UID), il `TokenBucketManager` acquisiva in sequenza. Se il primo bucket concedeva il token ma il secondo lo negava, il token del primo era già stato consumato, sprecando quota. Sotto contesa, questo portava a starvation e throughput inferiore al potenziale.

Inoltre, test di stress hanno evidenziato che in scenari di concorrenza elevata (55+ richieste simultanee), il comportamento del token bucket era meno prevedibile del necessario per un sistema che deve garantire conformità stretta ai limiti dell'exchange.

---

## Decisione

Adottiamo una **Sliding Window Rate Limiter basata su Sorted Set (ZSET) Redis**, con **acquisizione atomica multi-bucket** implementata tramite un singolo script Lua.

### Caratteristiche della soluzione

1. **Algoritmo Sliding Window**: ogni richiesta viene registrata in una ZSET con `score = timestamp_ms` e `member = UUID univoco`. Prima di ogni acquisizione, lo script elimina con `ZREMRANGEBYSCORE` gli elementi più vecchi di `window_ms`. Il conteggio `ZCARD` rappresenta il numero esatto di richieste nella finestra.

2. **Atomicità multi-bucket**: lo script Lua valuta **tutte le chiavi coinvolte** in una fase di dry-run. Solo se **ogni bucket** può concedere lo slot richiesto, lo script esegue la fase di commit su tutte le chiavi. Se anche un solo bucket nega, **nessun bucket viene modificato** e lo script ritorna il tempo di attesa necessario.

3. **Single Source of Truth temporale**: il timestamp `now` è calcolato **dentro lo script Lua** tramite `redis.call('TIME')`, non in Python. Questo elimina il clock drift tra worker e garantisce che tutti i processi condividano la stessa nozione di "adesso".

4. **Hash tag per Redis Cluster**: le chiavi usano il prefisso `{exchange}:rate_limit:...`. Le parentesi graffe indicano a Redis Cluster di mappare tutte le chiavi dello stesso exchange sullo stesso nodo fisico, requisito necessario per gli script Lua multi-chiave.

5. **Return value informativo**: `acquire()` ritorna una mappa `{nome_limiter: chiamate_rimanenti}`, permettendo al chiamante di adattare il proprio comportamento (es. rallentare il warm-up quando il budget si avvicina allo zero).

### Esempio d'uso

```python
manager = RateLimitManager(redis_client, exchange="bitget")
manager.add_limiter("public_ip", limit=6000, window_ms=60_000)
manager.add_limiter("candles_public", limit=20, window_ms=1_000)

# Acquisizione atomica: o entrambi concedono, o nessuno consuma
remaining = await manager.acquire("public_ip", "candles_public")
# remaining == {"public_ip": 5997, "candles_public": 18}
```

---

## Alternative considerate

### 1. Token bucket sequenziale (implementazione precedente)

**Pro**: O(1) in memoria per chiave, burst naturale, semplice da implementare.  
**Contro**: non allineato al modello di rate limit dell'exchange; acquisizione multi-bucket non atomica che spreca token; comportamento imprevedibile sotto alta concorrenza.  
**Motivo dello scarto**: il problema dell'atomicità è strutturale: risolverlo richiederebbe comunque uno script Lua unificato, a quel punto il token bucket perde il suo vantaggio di semplicità.

### 2. Token bucket con dynamic throttling adattivo

**Pro**: rallenta proattivamente quando il budget si avvicina allo zero, riducendo la probabilità di hit del limite hard.  
**Contro**: rende il rate limiter non deterministico, introduce un ciclo di retroazione pericoloso in caso di degrado Redis (più lento → più throttling → più backlog → Redis ancora più lento), applica penalità anche quando c'è margine residuo.  
**Motivo dello scarto**: il throttling adattivo appartiene al livello applicativo (il worker può decidere di rallentare in base a `remaining`), non al rate limiter, che deve essere deterministico e prevedibile.

### 3. Sliding window sequenziale per-bucket

**Pro**: risolve il problema del modello di rate limit, mantiene semplicità per singolo bucket.  
**Contro**: non risolve l'atomicità multi-bucket, quindi mantiene il problema dello spreco di token.  
**Motivo dello scarto**: risolve metà del problema. Se dobbiamo comunque scrivere uno script Lua multi-chiave per l'atomicità, l'astrazione sequenziale diventa ridondante.

### 4. Contatore Redis con INCR/DECR e TTL

**Pro**: massima semplicità, O(1) in memoria.  
**Contro**: non gestisce la finestra scorrevole, approssima con finestre fisse (fixed window) che permettono burst doppi al bordo della finestra.  
**Motivo dello scarto**: inaccuratezza non accettabile per un sistema che deve garantire conformità stretta.

---

## Conseguenze

### Positive

- **Conformità esatta ai limiti**: la finestra scorrevole misura esattamente ciò che l'exchange misura, eliminando il rischio di superamento per burst ravvicinati.
- **Zero sprechi di quota**: l'atomicità multi-bucket garantisce che nessun token venga consumato quando l'acquisizione fallisce complessivamente.
- **Throughput migliore sotto contesa**: i worker non consumano quota inutilmente durante i retry, aumentando il throughput effettivo aggregato.
- **Determinismo temporale**: l'uso di `redis.call('TIME')` elimina il clock drift tra worker, rendendo il comportamento prevedibile in ambienti distribuiti.
- **Compatibilità futura con Redis Cluster**: l'uso di hash tag `{exchange}` rende le chiavi dello stesso exchange co-locate, requisito necessario per gli script multi-chiave in cluster.
- **Osservabilità**: il return value `remaining` permette al chiamante di prendere decisioni informate (rallentare warm-up, prioritizzare richieste, etc.).

### Negative

- **Memoria Redis O(N) per finestra**: ogni richiesta occupa una entry nella ZSET. Con 6000 richieste/minuto e più bucket, il consumo di memoria cresce linearmente. Con Redis Stack e volumi contenuti (ordine di MB), è accettabile; con migliaia di richieste/secondo, va monitorato.
- **Complessità dello script Lua**: lo script multi-bucket è più lungo e più difficile da testare rispetto a script single-bucket. Richiede test di stress dedicati (vedi `tests/stress/test_rate_limiter.py`).
- **Latenza leggermente superiore**: lo script multi-chiave impiega qualche microsecondo in più rispetto a un semplice `INCR`. Trascurabile in ambienti con Redis locale o nella stessa rete.

### Neutre

- **Fallback su Redis down**: se Redis non risponde, il rate limiter **non** ha fallback automatico. L'eccezione risale al chiamante, che decide se ritentare o abortire. Questa è una scelta deliberata: un rate limiter che "concede quando Redis è giù" violerebbe lo scopo stesso del rate limiting. Decisione da rivalutare in futuro se emerge la necessità di un degrado graceful esplicito.
- **Configurazione dei bucket**: i nomi e i limiti dei bucket sono definiti nel modulo exchange-specifico (`bitget_api.py`), non nel core. Modifiche ai limiti dell'exchange richiedono solo aggiornamento di quella configurazione.

---

## Implementazione

- **File principale**: `core/rate_limiter.py` (classi `RedisSlidingWindow` e `RateLimitManager`).
- **Test di stress**: `tests/stress/test_rate_limiter.py` (verifica concorrenza, recovery, atomicità multi-bucket, comportamento con endpoint non registrato).
- **Configurazione Bitget**: `exchanges/bitget/bitget_api.py` (funzione `init_rate_limiter`).

---

## Riferimenti

- Documentazione Bitget V3 — Rate Limit: `https://www.bitget.com/api-doc/common/restapi/RateLimit`
- Redis Docs — Sorted Sets: `https://redis.io/docs/data-types/sorted-sets/`
- Redis Docs — Lua scripting: `https://redis.io/docs/manual/programmability/eval-intro/`
- Redis Cluster — Hash tags: `https://redis.io/docs/reference/cluster-spec/#hash-tags`
- ADR-0003 (composizione vs ereditarietà nei client WS) — per contesto sull'architettura dei client

---

**Nota di mantenimento**: se in futuro si passerà a Redis Cluster multi-nodo, verificare che tutte le chiavi di un singolo `exchange` usino lo stesso hash tag `{exchange}`. Attualmente il prefisso è `{bitget}:rate_limit:...`, quindi la compatibilità è già garantita.

---