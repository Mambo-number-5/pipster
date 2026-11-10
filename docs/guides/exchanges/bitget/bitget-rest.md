# Bitget REST Client

> Stato: completo per gli endpoint pubblici. Gli endpoint autenticati sono implementati e testati; il supporto all'account mode (Classic vs Unified) sarà oggetto di A16.
>
> Decisioni architetturali: ADR-0007 (clock sync + eccezioni exchange), ADR-0008 (rate limiting multi-livello).
> Transport sottostante: `docs/guides/core/transport.md`.

## 1. Cosa fa

`BitgetRestClient` è il client REST asincrono per Bitget V3. Gestisce:

- Firma HMAC-SHA256 delle richieste autenticate.
- Clock sync NTP con `/api/v3/market/time` (refresh ogni 30 min).
- Rate limiting multi-livello (globale + per-endpoint) iniettato via factory.
- Traduzione degli errori business Bitget (40006, 40008, 40009, 40725) in eccezioni tipizzate, indipendentemente dal fatto che arrivino con HTTP 200 o HTTP 4xx.

## 2. Costruzione

```python
from exchanges.bitget.bitget_rest import BitgetRestClient, init_rate_limiters

limiter = init_rate_limiters(redis_client) # RateLimitManager configurato
client = BitgetRestClient(
    api_key=settings.bitget_api_key,
    secret_key=settings.bitget_api_secret,
    passphrase=settings.bitget_api_passphrase,
    demo=settings.bitget_demo,
    rate_limiter=limiter,
)
# ...
await client.close()
```

````

Il client **non** crea il rate limiter: lo riceve iniettato. Il wrapping in `ResilientRateLimiter` (fallback locale, ADR-0004/0005) è responsabilità del worker, che usa le factory `build_resilient_*`.

`init_rate_limiters` registra:

- `public_ip` — 6000 req / 60s (limite globale per IP).
- `public_candles`, `public_time`, `public_instruments`, `public_tickers`, `public_history_candles` — 20 req / 1s ciascuno.

## 3. Clock sync

`BitgetRestClient` mantiene un offset rispetto al server (`_BitgetClockSync`).
Le firme usano `_clock.now_ms()` invece dell'orologio locale.

Politica di refresh (ADR-0007):

- **Lazy**: alla prima richiesta autenticata, sync on-demand.
- **Proattivo**: refresh ogni 30 minuti.
- **Reattivo su 40008**: `_request` cattura `40008`, esegue `sync_server_time()`, rigenera gli header e ritenta. Massimo 2 retry; oltre, solleva `BitgetTimestampExpired`.

Il resync è **trasparente al chiamante**: il worker non deve gestire 40008.

L'offset è calcolato con la formula NTP quando il server espone `requestTime`:

```
offset = ((t1 - t0) + (t2 - t3)) // 2
t0 = local_before, t1 = requestTime, t2 = serverTime, t3 = local_after
```

Fallback a mid-point semplice se `requestTime` manca.

## 4. Endpoint pubblici

| Metodo                       | Endpoint Bitget                  | Bucket                   |
| ---------------------------- | -------------------------------- | ------------------------ |
| `get_candles`                | `/api/v3/market/candles`         | `public_candles`         |
| `get_historical_candles`     | `/api/v3/market/history-candles` | `public_history_candles` |
| `get_instruments`            | `/api/v3/market/instruments`     | `public_instruments`     |
| `get_tickers` / `get_ticker` | `/api/v3/market/tickers`         | `public_tickers`         |
| `fetch_server_time`          | `/api/v3/market/time`            | `public_time`            |

Tutti ritornano `list` o `dict` già estratti dal wrapper `{"code","msg","data"}` (il chiamante riceve direttamente `res["data"]` o, per il server time, `int`).

```python
candles = await client.get_candles("BTCUSDT", category="SPOT", interval="1m", limit=10)
ticker = await client.get_ticker("BTCUSDT", category="SPOT")
instruments = await client.get_instruments("SPOT", symbol="BTCUSDT")
```

## 5. Richieste autenticate

```python
res = await client._request(
    "GET", "/api/v3/account/assets", auth_required=True,
)
```

L'accesso a `_request` diretto è accettabile per endpoint autenticati non
ancora wrappati; quando aggiungerai un metodo per gli asset, seguirà lo schema degli endpoint pubblici (firma + `rate_limiter_key`).

## 6. Firma HMAC (e il bug dei 40009)

La firma è:

```
message = timestamp + method + path + "?" + query_string + body
sign = base64(hmac_sha256(secret, message))
```

**Regola critica**: `query_string` e `body` per la firma sono **NON** urlencoded.
Bitget documenta esplicitamente (40009): _"do not URL-encode the query string or body before signing"_. Il client usa due funzioni distinte:

- `_build_query_for_sign` — ordinata, non urlencoded, usata per firmare.
- `_build_query_for_url` — ordinata e urlencoded, usata nell'URL.

Il body è serializzato **esplicitamente** con `separators=(",", ":")` e inviato come `data=body_str`, non `json=body`. Motivo: la stringa firmata deve essere byte-per-byte identica a quella inviata. `json=` lascia la serializzazione ad aiohttp, che potrebbe differire (spazi dopo `:`, ordinamento chiavi, ecc.).

## 7. Errori business: HTTP 200 e HTTP 4xx

Bitget restituisce gli errori business in **due forme**:

- **HTTP 200** con body `{"code": "4000X", "msg": "..."}` — tipico per endpoint pubblici con parametri errati.
- **HTTP 4xx** con lo stesso formato nel body — tipico per endpoint autenticati con firma o timestamp errati.

`_request` gestisce entrambi i casi via `_handle_business_code`:

| Codice                    | Eccezione                                             | Retry?                    |
| ------------------------- | ----------------------------------------------------- | ------------------------- |
| 40006 (invalid key)       | `BitgetAuthError`                                     | No                        |
| 40009 (signature error)   | `BitgetAuthError`                                     | No                        |
| 40008 (timestamp expired) | `BitgetTimestampExpired`                              | Sì, 2 resync interni      |
| 40725 (service error)     | `BitgetRetryableError` (eredita `RetryableWithDelay`) | Sì, gestito dal chiamante |
| altri codici              | `BitgetApiError` o ripropaga `NonRetriableHttpError`  | No                        |

Per codici non riconosciuti:

- Se il codice è stato estratto da un **HTTP 4xx**, si ripropaga `NonRetriableHttpError` originale (più informativo, con `.body`).
- Se il codice è da un **HTTP 200**, si solleva `BitgetApiError`.

## 8. Rate limiting multi-livello

Ogni `_request` acquisisce su due bucket (ADR-0008):

```python
buckets = ("public_ip", "public_candles")   # globale + endpoint
await rate_limiter.acquire(*buckets)
```

Il `RateLimitManager` garantisce l'atomicità: o consuma su entrambi o su nessuno. Se un bucket è saturo, attende il più vincolante.

Il wrapper `ResilientRateLimiter` (ADR-0005) gestisce la degradazione su Redis down: il fallback locale è atomico (ADR-0009), quindi la semantica è uniforme tra modalità normale e degradata.

## 9. Account mode (Classic vs Unified)

**Rilevante solo per endpoint autenticati.** Bitget V3 espone gli endpoint `/api/v3/account/*` solo per **Unified Account**. Un account Classic che chiama questi endpoint riceve:

```json
{
  "code": "40084",
  "msg": "You are in Classic Account mode, and the Unified Account API is not supported at this time"
}
```

**Per l'ingestion non è un problema**: tutti gli endpoint usati sono pubblici
(`/market/*`), e funzionano per entrambe le modalità.

Per il futuro **Order Executor / Risk Manager**, servirà:

- Passare l'account a Unified Account, oppure
- Usare gli endpoint Classic (quando/se V3 li esporrà).

Il codice `40084` non è gestito come eccezione tipizzata: arriva come
`BitgetApiError("40084", ...)` e il chiamante decide. Sarà oggetto di A16.

## 10. Test

- `tests/unit/exchanges/bitget/test_bitget_rest.py` — unit + mock.
- `tests/contract/exchanges/bitget/test_bitget_rest.py` — contract test contro endpoint reale (attivabili con `BITGET_CONTRACT_TEST=1`).

Il contract test autenticato richiede `BITGET_DEMO_KEY/SECRET/PASSPHRASE`
(Demo API Key). Passa anche su `40084` perché quella risposta prova che la
firma è valida.

## 11. Riferimenti

- Modulo: `exchanges/bitget/bitget_rest.py`
- Eccezioni: `core/exceptions.py` (`HttpStatusError`, `NonRetriableHttpError`,
  `ServerError`, `RateLimitError`, `RetryableWithDelay`)
- ADR: ADR-0007 (clock sync + eccezioni), ADR-0008 (rate limiting multi-livello)
- Documentazione Bitget:
  [Market Data V3](https://www.bitget.com/api-doc/spot/market/Get-Candle-Data),
  [Quick Start](https://www.bitget.com/api-doc/common/intro)
````
