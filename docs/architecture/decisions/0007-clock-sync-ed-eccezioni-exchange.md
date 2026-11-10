# ADR-0006: Clock sync e gerarchia eccezioni negli adapter exchange

**Data**: 2026-10-24  
**Stato**: Accettato  
**Decisori**: [owner del progetto]

## Contesto

Il client REST Bitget (e i futuri adapter Bybit/Binance) deve:

1. Firmare richieste autenticate con un timestamp che il server considera valido (es.: tolleranza Bitget: 30 secondi). L'orologio host può derivare, e in produzione la deriva causa errori (es.: 40008 - timestamp expired) che bloccano l'operatività.
2. Distinguere errori business ritentabili (es. 40725: deploy lato server) da errori non ritentabili (40006: chiave invalida, 40009: firma errata).
3. Gestire il rate limiting su due livelli (globale + per-endpoint).

## Decisione

### Clock sync

Ogni adapter exchange mantiene un offset server-locale, calcolato con la
formula NTP a partire da `requestTime`/`serverTime` (dove disponibili) o dal mid-point di RTT (fallback). L'offset è applicato a tutte le firme.

Politica di refresh:
- **Lazy al primo uso autenticato**: sync on-demand.
- **Proattivo ogni 30 minuti**: previene la deriva in operatività continua.
- **Reattivo su 40008**: `_request` fa resync, rigenera gli header, ritenta. Massimo 2 resync per richiesta; oltre, solleva `BitgetTimestampExpired`.

Il resync è **trasparente al chiamante**: chi usa il client non deve sapere
che esiste un offset.

### Gerarchia eccezioni

```
core.exceptions.RetryableWithDelay
    ↑
BitgetRetryableError        ← 40725 (business-retryable)
    
Exception
    ↑
BitgetApiError(code, msg)   ← base per tutti gli errori business
    ↑
BitgetAuthError             ← 40006, 40009 (configurazione, non ritentare)
BitgetTimestampExpired      ← 40008 dopo 2 resync falliti
```

Regole:
- **40006, 40009** → `BitgetAuthError`, propagata al chiamante. Non ritentare: la correzione richiede azione umana (credenziali, orologio di sistema).
- **40008** → gestito internamente con resync. Se dopo 2 tentativi persiste, solleva `BitgetTimestampExpired`.
- **40725** → `BitgetRetryableError(delay=...)`, eredita `RetryableWithDelay`. Il chiamante può ritentare o wrappare in `retry_async` applicativo.
- Altri codici → `BitgetApiError`.

### Rate limiting multi-livello

Due bucket per exchange:
- `public_ip` (globale): `limit=6000, window_ms=60_000` per Bitget.
- `public_<endpoint>` (per endpoint): `limit=20, window_ms=1_000`.

Ogni `_request` acquisisce su entrambi in modo atomico (il `RateLimitManager` già lo supporta).

## Alternative considerate

1. **Clock sync esterno (ntpdate/NTP daemon host).** Pro: nessun codice    applicativo. Contro: in container non sempre disponibile, e comunque    l'offset rispetto al **server Bitget** può differire da quello rispetto a un pool NTP pubblico (Bitget potrebbe avere il proprio drift). Scartata.

2. **40008 esposto al chiamante.** Pro: trasparenza. Contro: accoppia ogni chiamante alla conoscenza del clock sync e della procedura di resync. Scartata: è una preoccupazione dell'adapter.

3. **Firma calcolata su body serializzato da aiohttp (`json=body`).** Pro: meno codice. Contro: se aiohttp cambia il default di serializzazione (separatori, encoding), la firma non corrisponde. Scartata: serializzazione esplicita è l'unica scelta robusta.

4. **Rate limit solo globale.** Pro: un solo bucket. Contro: ignora il limite per-endpoint di Bitget; una raffica di chiamate a `/candles` esaurirebbe il budget globale pur restando sotto il limite per-endpoint. Scartata.

## Conseguenze

**Positive**
- Il client funziona in operatività continua senza 40008.
- Gli errori sono tipizzati: il chiamante distingue auth vs transient.
- I rate limit sono rispettati su entrambi i livelli dichiarati da Bitget.

**Negative**
- Un sync all'avvio costa 1 chiamata HTTP (trascurabile).
- Due bucket per endpoint: configurazione più articolata in `init_rate_limiters`.
- Ogni adapter futuro deve replicare il pattern clock sync + eccezioni. Ma il pattern è la decisione; l'implementazione è per-exchange.

**Neutre**
- Il `RateLimitManager` non cambia: già supporta multi-bucket.
- `RetryableWithDelay` resta in `core.exceptions`: è la primitiva condivisa.

## Riferimenti

- Moduli: `exchanges/bitget/bitget_rest.py`, `core/exceptions.py`, `core/rate_limiter.py`
- Guide: `docs/guides/bitget-rest.md`, `docs/guides/transport.md`
- Documentazione Bitget: [Get Server Time](https://www.bitget.com/api-doc/spot/market/Get-Server-Time), [Rate Limit](https://www.bitget.com/api-doc/common/rate-limit)
- ADR correlati: ADR-0002, ADR-0004, ADR-0005