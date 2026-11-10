# ADR-0004: Fallback locale del rate limiter in caso di Redis irraggiungibile

**Data**: 2026-09-25
**Stato**: Integrato da ADR-0005
**Decisori**: Team di sviluppo Pipster

## Contesto

L'ingestion engine di Pipster usa un rate limiter distribuito su Redis (ADR-0002) per rispettare i limiti delle API Bitget anche con più worker concorrenti. La correttezza della decisione "posso chiamare l'exchange?" dipende interamente dalla disponibilità di Redis. Se Redis diventa irraggiungibile, il worker non sa più se ha ancora budget di richieste e non può procedere in sicurezza.

Tre strategie sono possibili in caso di Redis down: (a) bloccare tutte le chiamate all'exchange finché Redis non torna (fail-fast), (b) concedere comunque ogni richiesta (fail-open), (c) degradare verso un meccanismo locale che mantiene un throttle ma con budget ridotto. Fail-fast penalizza la disponibilità in modo sproporzionato rispetto a un blip di pochi secondi. Fail-open puro è inaccettabile: N worker che ignorano simultaneamente il limite globale espongono a ban dell'exchange in pochi secondi.

Il test `test_stress_token_bucket_fallback` attualmente verifica un fallback che **non esiste** nel codice di produzione. Il test passa per il motivo sbagliato: `redis_client.aclose()` non disabilita il pool, che si riconnette trasparentemente alla chiamata successiva. Il test va rimosso o riscritto in coerenza con questa decisione.

## Decisione

Adottiamo un **fallback locale bounded**: quando una chiamata a Redis fallisce, il limiter usa un'istanza locale equivalente con rate ridotto, e riprova Redis dopo un breve cooldown. La capacità locale è dimensionata come `rate_globale * safety_factor / expected_workers`, dove `expected_workers` è il numero di worker concorrenti previsti e `safety_factor` (< 1) fornisce margine di sicurezza.

Dettagli operativi:

- **Trigger**: eccezione `redis.RedisError` (o sottoclasse) attorno alla chiamata allo script Lua.
- **Cooldown**: dopo un fallimento, Redis non viene ritentato per `RATE_LIMIT_FALLBACK_COOLDOWN_SEC` secondi. Ogni `acquire()` in questa finestra usa direttamente il fallback locale.
- **Recovery**: alla prima chiamata dopo il cooldown si ritenta Redis; se risponde, si torna in modalità normale senza stato da riconciliare (le finestre locali si esauriscono naturalmente).
- **Multi-bucket**: in modalità degradata `RateLimitManager` perde l'atomicità e acquisisce i bucket locali in sequenza. Se un bucket locale nega, l'intero set viene ritentato dopo l'attesa: il throttle risultante è **conservativo** (può sotto-utilizzare il budget, mai superarlo).
- **Ambito**: ogni `RedisTokenBucket`, `RedisSlidingWindow` e `RateLimitManager` istanzia un limiter locale gemello con la stessa semantica (finestra/rate) e rate ridotto.
- **Configurazione** (via `Settings`, una volta centralizzata la configurazione):
  - `RATE_LIMIT_FALLBACK_ENABLED: bool = True`
  - `RATE_LIMIT_EXPECTED_WORKERS: int` (obbligatorio, senza default — l'app non parte se manca)
  - `RATE_LIMIT_FALLBACK_COOLDOWN_SEC: float = 5.0`
  - `RATE_LIMIT_FALLBACK_SAFETY_FACTOR: float = 0.8`

## Alternative considerate

### 1. Fail-fast (propaga l'eccezione al worker).
**Pro**: massima semplicità, correttezza formale, nessuna configurazione extra.
**Contro**: un blip Redis di 2 secondi interrompe warm-up e gap filling; in multi-worker ogni worker replica il blip; serve logica di retry nel worker per ogni chiamata REST. Scartata perché un blip Redis non deve tradursi in un'interruzione dell'ingestion.

### 2. Fail-open puro (concedi sempre).
**Pro**: una riga di codice, disponibilità massima.
**Contro**: N worker chiamano contemporaneamente l'exchange senza coordinazione; il limite globale viene superato in pochi secondi; ban quasi garantito. Scartata senza appello.

### 3. Fallback locale senza riduzione di rate.
**Pro**: codice più semplice, niente `expected_workers`.
**Contro**: con 4 worker il rate effettivo in degraded mode è 4× il globale → stesso rischio del fail-open, solo attenuato. Scartata.

### 4. Circuit breaker completo (Closed/Open/Half-Open).
**Pro**: evita di tempestare Redis se è giù a lungo, stato esplicito e osservabile.
**Contro**: complessità aggiuntiva, va integrato con metriche e alert. **Rimandata a un ADR futuro**: il cooldown semplice copre la maggior parte del beneficio con una frazione della complessità, e il circuit breaker si potrà aggiungere sopra questa base senza riscritture.

## Conseguenze

**Positive**
- Nessun deadlock in caso di Redis down: il worker continua a operare con throttle ridotto.
- Nessun rischio di ban: il rate effettivo in degraded mode resta entro il globale, con margine.
- Decisione esplicita e documentata: chi legge il codice sa cosa succede se Redis cade.
- Base pronta per il circuit breaker in fase di pianificazione.

**Negative**
- Introduce un parametro critico (`RATE_LIMIT_EXPECTED_WORKERS`) che, se sottostimato, può portare a superare il limite globale. Va validato all'avvio e documentato in `.env.example` e nella guida al deployment.
- In degraded mode `RateLimitManager` perde l'atomicità multi-bucket: il throttle è conservativo ma può ridurre il throughput disponibile.
- Raddoppia lo stato interno del rate limiter (bucket Redis + bucket locale per ogni limiter registrato).

**Neutre**
- Serve una metrica `rate_limiter_degraded_mode` (Gauge o Counter) per osservare il tempo speso in fallback. 
- Il comportamento del WS non cambia: il throttle dei 10 msg/s è già locale (`LocalSlidingWindow`) e non passa da Redis. Il fallback è per-processo; la somma delle quote dei worker non supera il rate globale per costruzione
- Il valore di `RATE_LIMIT_FALLBACK_SAFETY_FACTOR` (0.8) potrà essere rivisto al rialzo (fino a 0.9) quando un futuro circuit breaker ridurrà il tempo speso in degraded mode. Il valore è configurazione, non decisione: l'aggiornamento avverrà in `.env.example` e sarà motivato in un ADR dedicato.
- `RATE_LIMIT_EXPECTED_WORKERS` è obbligatorio oggi. In futuro evolverà verso un pattern lease-based (i worker pubblicano un lease con TTL su Redis; il rate limiter legge il numero di lease attivi e lo cachea localmente), e successivamente verso service discovery se il deployment lo richiederà. L'evoluzione sarà oggetto di ADR dedicati.

## Riferimenti

- Moduli coinvolti: `core/rate_limiter.py` (`RedisTokenBucket`, `RedisSlidingWindow`, `RateLimitManager`)
- Test: `tests/integration/test_rate_limiter.py` (già spostato; `test_stress_token_bucket_fallback` da riscrivere come test del degraded mode)
- Config: `core/config.py` (una volta centralizzata la configurazione), `.env.example`
- Stato di pianificazione: docs/roadmap.md
- ADR correlati: ADR-0002 (sliding window atomica)
- ADR futuri previsti: ADR-0006 (circuit breaker rate limiter)
- Documentazione esterna: [redis-py error handling](https://redis-py.readthedocs.io/en/stable/exceptions.html)