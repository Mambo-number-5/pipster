# ADR-0009: Atomicità dei fallback locali nel rate limiter

**Data**: 2026-11-08  
**Stato**: Accettato  
**Decisori**: [owner del progetto]

## Contesto

ADR-0004 ha introdotto un fallback locale per il rate limiter, attivato
quando Redis è irraggiungibile. ADR-0005 ha specificato che il fallback è
composto tramite un wrapper (`ResilientRateLimiter`), con factory che
accoppiano primary e fallback della stessa semantica.

Il `RateLimitManager` (primary) è **atomico** su più bucket: o consuma su
tutti o su nessuno (ADR-0002). Il `TokenBucketManager` (primary, riscritto
in seguito) è anch'esso atomico.

I fallback locali (`_LocalManagerFallback`, `_LocalTokenBucketManagerFallback`)
**non sono atomici**: acquisiscono in sequenza (o in parallelo) sui bucket
locali, senza dry-run/commit. In caso di fallimento parziale (bucket A ha
capacità, bucket B no), A viene consumato anche se B non lo è. Il
comportamento in degraded mode è quindi **semanticamente diverso** da quello
normale.

ADR-0004 aveva accettato questa asimmetria come compromesso pragmatico.
Dopo la riscrittura atomica di `TokenBucketManager`, il gap va chiuso: due
modalità operative dello stesso sistema non possono avere contratti diversi.

## Decisione

I fallback locali acquisiscono in modalità **all-or-nothing** con pattern
dry-run/commit, speculare a quello dei primary:

1. **Fase 1 — dry-run**: per ogni bucket richiesto, verifica la capacità
   disponibile senza consumarla. Se uno qualsiasi è insufficiente, calcola
   il tempo di attesa necessario al bucket più vincolante, dorme, e ritenta
   dal punto 1.
2. **Fase 2 — commit**: se tutti i bucket hanno capacità sufficiente,
   consuma su tutti atomicamente.

Per supportare il dry-run, `LocalSlidingWindow` e `LocalTokenBucket`
espongono due metodi:

- `available(now: float) -> int | float`: capacità disponibile al tempo `now`,
  senza consumo.
- `commit(requested: int, now: float) -> None`: consuma `requested` assumendo
  che `available()` abbia già verificato la disponibilità.

Il lock di ciascun limiter locale protegge l'operazione `available` + `commit`
nella stessa sezione critica: tra la verifica e il consumo non ci sono
interleaving.

`_LocalManagerFallback` e `_LocalTokenBucketManagerFallback` orchestrano il
dry-run su tutti i bucket, poi il commit su tutti. Se durante il commit un
bucket fallisce (non dovrebbe, perché `available` è stato verificato sotto
lock), l'intera operazione solleva `RuntimeError`: è un bug di concorrenza,
non un caso da gestire silenziosamente.

## Alternative considerate

1. **Mantenere fallback non atomico (status quo, ADR-0004).** Pro: codice più
   semplice, meno test. Contro: semantica diversa tra normale e degraded;
   un consumatore che si aspetta "o tutto o niente" può avere stati
   inconsistenti. Scartata: dopo la riscrittura atomica del primary, il gap
   è inaccettabile.

2. **Lock globale su tutti i bucket locali durante l'acquisizione.** Pro:
   atomico per costruzione. Contro: serializza tutte le acquisizioni anche
   quando non c'è contesa; il lock globale diventa un collo di bottiglia in
   caso di molti bucket. Scartata: il pattern dry-run/commit è più scalabile
   (lock per bucket, non globale).

3. **Fallback locale con semantica "best effort" e log di warning su
   fallimento parziale.** Pro: nessun cambio di API locale. Contro: il
   chiamante non ha modo di distinguere un'acquisizione completa da una
   parziale; il warning può passare inosservato in produzione. Scartata:
   maschera il problema invece di risolverlo.

4. **Fallback atomico solo per `TokenBucketManager`, non per
   `RateLimitManager`.** Pro: metà del lavoro. Contro: lascia un'asimmetria
   interna al rate limiter (un manager atomico, uno no). Scartata: la
   coerenza richiede entrambi.

## Conseguenze

**Positive**

- Il contratto del rate limiter è uniforme: `acquire` è atomico sia in
  modalità normale sia in degraded.
- Il `ResilientRateLimiter` può wrappare qualsiasi primary senza che il
  consumatore debba sapere quale modalità è attiva.
- `build_resilient_manager` e `build_resilient_token_bucket_manager`
  producono fallback con la stessa semantica del primary.

**Negative**

- I limiter locali (`LocalSlidingWindow`, `LocalTokenBucket`) crescono di
  due metodi (`available`, `commit`) e di un test ciascuno.
- `_LocalManagerFallback` e `_LocalTokenBucketManagerFallback` diventano
  più complessi (~20 righe ciascuno invece di ~10).
- Il pattern dry-run/commit ha un caso limite: se la capacità cambia tra
  dry-run e commit (non dovrebbe, per il lock), si solleva `RuntimeError`.

**Neutre**

- L'API pubblica di `RateLimitManager` e `TokenBucketManager` non cambia:
  l'atomicità è interna al fallback.
- Le factory `build_resilient_*` non cambiano firma: cambia solo
  l'implementazione dei fallback che costruiscono.
- `init_rate_limiters` (adapter Bitget) resta invariato: ritorna un
  `RateLimitManager` puro. Il wrapping è responsabilità del worker (A3).

## Riferimenti

- Moduli: `core/rate_limiter.py` (`LocalSlidingWindow`, `LocalTokenBucket`,
  `_LocalManagerFallback`, `_LocalTokenBucketManagerFallback`,
  `build_resilient_*`)
- Test: `tests/unit/core/test_rate_limiter.py`
- ADR correlati: ADR-0002 (sliding window atomica), ADR-0004 (fallback locale),
  ADR-0005 (composizione fallback)
