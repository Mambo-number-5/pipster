# ADR-0008: Rate limiting multi-livello negli adapter exchange

**Data**: 2026-10-25  
**Stato**: Proposto  
**Decisori**: [owner del progetto]

> Nota: questo ADR sarà promosso ad `Accettato` quando il secondo exchange
> adapter (Bybit o Binance) sarà implementato e il pattern sarà validato
> su più di un caso.

## Contesto

Gli exchange crypto impongono rate limit su più livelli simultaneamente.
Bitget, ad esempio, documenta:

- **Globale**: 6000 richieste/minuto/IP (= 100 req/s), condiviso tra REST e WS.
- **Per endpoint**: 20 richieste/secondo/IP per ciascun endpoint di market data (candles, time, tickers, instruments, ecc.).

I due limiti sono **indipendenti**: rispettare uno non garantisce l'altro.
Una raffica di 20 req/s su `/candles` rispetta il limite per-endpoint ma può esaurire il budget globale in 5 secondi se ci sono altre chiamate in corso.
Viceversa, 6 endpoint diversi chiamati contemporaneamente a 20 req/s ciascuno rispettano il globale (120 > 100 no, anzi lo sforano) ma non sono coordinati se il limiter traccia solo il globale.

Serve una strategia che rispetti entrambi i livelli, sia configurabile
per-exchange, e che non richieda al chiamante di sapere quali bucket esistono.

## Decisione

Ogni adapter exchange registra **due livelli di bucket** nel
`RateLimitManager`:

1. **Bucket globale** (`public_ip` per Bitget): rappresenta il limite complessivo per IP. Finestra aderente alla specifica dell'exchange (Bitget: 60_000 ms).
2. **Bucket per-endpoint** (`public_candles`, `public_time`, ...): uno per ciascun endpoint che il client chiama. Finestra aderente alla specifica per-endpoint (Bitget: 1_000 ms).

Ogni chiamata a `_request` acquisisce **atomicamente** su tutti i bucket
rilevanti: il globale, più il bucket dell'endpoint se mappato.

Il mapping endpoint → bucket vive nell'adapter (`_ENDPOINT_BUCKETS`), non
nel transport né nel `RateLimitManager`. La configurazione dei bucket vive
in `init_rate_limiters`, funzione dell'adapter.

Il `RateLimitManager` esistente (ADR-0002) già supporta l'acquisizione
atomica multi-bucket: non richiede modifiche. L'acquisizione avviene in
`before_attempt`, quindi si applica a ogni tentativo (inclusi i retry),
coerentemente con ADR-0006.

## Alternative considerate

1. **Solo bucket globale.** Pro: un solo bucket, configurazione semplice. Contro: una raffica su un singolo endpoint esaurisce il budget globale, bloccando endpoint che sarebbero passati sotto il loro limite per-endpoint. Scartata: ignora il vincolo esplicito dell'exchange.

2. **Solo bucket per-endpoint.** Pro: ogni endpoint è indipendente, nessuno blocca gli altri. Contro: N endpoint × 20 req/s può sforare il globale (100 req/s) senza che nessuno se ne accorga. Scartata: rischio di ban.

3. **Rate limiter nel worker, non nel client.** Pro: il client resta "semplice". Contro: ogni chiamante replica la logica, e il client non può garantire il rispetto dei limiti per conto suo. Con più worker, la logica è duplicata N volte. Scartata: il rate limiting è una preoccupazione del client, non del chiamante.

4. **Bucket dinamici in base all'endpoint scoperto a runtime.** Pro: nessuna mappatura hardcoded. Contro: la finestra dei bucket deve essere nota all'avvio (Bitget: 1s per endpoint, 60s per globale); scoprirla a runtime richiede parsare la doc o rischiare errori. Scartata: la mappatura statica in `_ENDPOINT_BUCKETS` è più semplice e corretta.

5. **Configurazione bucket nel transport.** Pro: un solo posto. Contro: il transport diventerebbe specifico per exchange, violando ADR-0006. Scartata.

## Conseguenze

**Positive**
- Rispetto simultaneo di entrambi i livelli dichiarati da Bitget.
- Il chiamante non deve conoscere i bucket: `_request` li gestisce.
- L'acquisizione atomica garantisce che una richiesta o consuma su tutti i bucket o su nessuno (nessun consumo parziale).
- Il pattern è riusabile: Bybit, Binance, e altri exchange usano lo stesso schema (globale + per-endpoint), con finestre diverse.
- `init_rate_limiters` per-exchange è il punto di configurazione: ogni adapter dichiara i propri bucket, il worker non deve saperlo.

**Negative**
- Due bucket per endpoint significano più chiavi Redis e più complessità di configurazione. Con Bitget sono 1 globale + N endpoint; con Bybit saranno simili.
- La mappatura `_ENDPOINT_BUCKETS` va mantenuta quando si aggiungono endpoint. Se un endpoint manca, il client usa solo `public_ip` (fallback silenzioso, ma meno preciso).
- In caso di Redis down, il fallback locale (ADR-0004) si applica a ogni bucket indipendentemente: il throttle è conservativo ma può sotto-utilizzare.

**Neutre**
- Il `RateLimitManager` non cambia: la sua interfaccia multi-bucket è già sufficiente.
- Le finestre (60s, 1s) sono hardcoded nell'adapter. Se Bitget le cambierà, si aggiornano in `init_rate_limiters`.
- L'atomicità del fallback locale (A13) è materia di ADR-0009, non di questo.

## Riferimenti

- Moduli: `exchanges/bitget/bitget_rest.py` (`_ENDPOINT_BUCKETS`, `init_rate_limiters`), `core/rate_limiter.py` (`RateLimitManager`)
- Guide: `docs/guides/rate-limiting.md`, `docs/guides/bitget-rest.md`
- ADR correlati: ADR-0002 (sliding window atomica), ADR-0004 (fallback locale), ADR-0005 (composizione fallback), ADR-0006 (transport layer)
- Documentazione Bitget: [Rate Limit](https://www.bitget.com/api-doc/common/rate-limit)