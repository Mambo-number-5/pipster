# ADR-0006: Architettura del transport layer HTTP/WebSocket

**Data**: 2026-10-24
**Stato**: Accettato
**Decisori**: Mambo n.5

## Contesto

Pipster deve comunicare con exchange crypto su due canali distinti: REST (richieste one-shot con retry, rate limiting, error handling) e WebSocket (connessione persistente con riconnessione, heartbeat, watchdog). Le due modalità hanno requisiti diversi ma condividono concetti comuni (retry, backoff, distinzione errori transient/permanenti).

Serve un layer di trasporto che sia:
- **Agnostico rispetto all'exchange**: le primitive devono funzionare per Bitget, Bybit, Binance senza modifiche.
- **Testabile senza rete**: la CI non deve dipendere da servizi esterni.
- **Componibile**: chi lo usa inietta hook e callback, non eredita da esso.
- **Corretto sul protocollo HTTP**: rispettare `Retry-After`, distinguere 4xx da 5xx, non ritentare errori non-transient.

## Decisione

### Separazione in due classi

`AsyncHttpClient` (REST) e `AsyncWsClient` (WS) sono classi separate, in moduli separati (`core/http_client.py`, `core/ws_client.py`). Non condividono codice: usano librerie diverse (`aiohttp` vs `websockets`), hanno cicli di vita diversi (request/response vs connessione persistente), e la loro unica comunanza — la nozione di "retry" — è già estratta in `retry_async`.

### Retry per-tentativo

Ogni tentativo (incluso il primo) consuma il budget di rate limiting tramite l'hook `before_attempt`. I retry per errori di rete consumano un token anche se il server non ha ricevuto la richiesta.

È una scelta conservativa: il server exchange conta le richieste che riceve, e un retry che arriva è indistinguibile da una chiamata nuova. Il rischio di ban è asimmetrico (un singolo sforamento può costare ore di blocco), quindi si preferisce sotto-utilizzare il budget piuttosto che rischiare.

### Distinzione errori HTTP

Quattro categorie, con comportamenti diversi:

| Categoria | Status | Eccezione | Retry? |
|---|---|---|---|
| Rate limit (transient) | 429, 503 | `RateLimitError` (eredita `RetryableWithDelay`) | Sì, delay da `Retry-After` se presente |
| Ban (non-transient) | 418 | `NonRetriableHttpError` con `retry_after` | No |
| Client error (non-transient) | altri 4xx | `NonRetriableHttpError` | No |
| Server error (transient) | 5xx senza `Retry-After` | `ClientResponseError` | Sì, backoff standard |

Il 418 è trattato separatamente dal 429 perché è un **ban**, non un rate limit: ritentare durante un ban prolunga la penalità. Il `retry_after` resta disponibile al chiamante per logging/alerting/pausa manuale, ma il retry loop non lo usa.

### Primitiva `RetryableWithDelay`

`core.exceptions.RetryableWithDelay` è un'eccezione che porta un `.delay: float | None`. `retry_async` la riconosce via `compute_delay` e usa `.delay` se presente, altrimenti il backoff standard. La primitiva è condivisa tra transport (`RateLimitError`) e adapter exchange (come `BitgetRetryableError`), così il meccanismo di "aspetta N secondi" è uniforme senza che il transport conosca alcun codice business.

Se `.delay` è `None` (header `Retry-After` assente), si usa il backoff esponenziale standard. Il server non ha indicato un tempo, quindi non se ne inventa uno.

### Hook `before_attempt` per tentativo

L'hook è chiamato **prima di ogni tentativo**, non solo del primo. Se l'hook solleva, l'eccezione non è intercettata da `retry_async` (a meno che sia in `retry_exceptions`): risale al chiamante. È desiderabile: se il rate limiter è mal configurato, meglio fallire subito che retryare in loop.

### WebSocket: listen loop concorrente con `on_connect`

Il listen loop (`async for msg in ws`) è avviato come task **prima** di invocare `on_connect`. Questo permette a `on_connect` di eseguire handshake applicativi (login, subscribe) che richiedono risposte dal server, senza deadlock. Senza questa scelta, il client dell'exchange non potrebbe fare login: `on_connect` aspetterebbe la risposta, ma il listen loop non sarebbe ancora attivo, e la risposta non arriverebbe mai.

### WebSocket: watchdog del silenzio

Un task dedicato monitora il tempo dall'ultimo messaggio ricevuto. Se supera `silence_timeout`, chiude il socket, forzando la riconnessione. Serve a rilevare **connessioni zombie**: socket TCP apparentemente aperti (keepalive OK) ma che non ricevono più messaggi. Senza watchdog, una connessione zombie resta "aperta" per ore senza fornire dati.

### WebSocket: ping applicativo vs protocollo

Due modalità, selezionabili con `ping_origin_protocol`:
- **Applicativo** (`False`): invia `ping_payload` come frame testuale ogni `ping_interval` secondi. Utile quando l'exchange richiede un ping applicativo specifico (Bitget: stringa `"ping"`).
- **Protocollo** (`True`): lascia che `websockets` gestisca il ping nativo del protocollo (frame di controllo).

La scelta è per-exchange, non globale, perché ogni exchange ha la sua convenzione.

## Alternative considerate

1. **Una sola classe `AsyncClient` che gestisce sia HTTP sia WS.** Pro: un solo modulo. Contro: le due modalità non condividono quasi nulla; unificarle crea una classe con 40 metodi e due cicli di vita. Scartata.

2. **Retry per-operazione logica (solo il primo tentativo consuma budget).** Pro: "giusto" se i retry sono per errori transient lato client. Contro: il server conta comunque i retry che riceve; rischi di sforare. Scartata per conservatività.

3. **Tutti i 4xx ritentati (via `ClientError` in `retry_exceptions`).** Pro: codice più semplice. Contro: ritenta 400/401/403/404 che non cambiano esito, consuma budget, ritarda l'emersione dell'errore. Scartata.

4. **`Retry-After` ignorato (backoff uniforme per tutti gli errori transient).** Pro: un solo comportamento. Contro: il server ha detto esplicitamente quando ritentare; ignorarlo è scortese e può prolungare il blocco. Scartata.

5. **`on_connect` eseguito prima del listen loop (approccio A al deadlock login).** Pro: più semplice, nessun task concorrente. Contro: impossibile fare login, perché la risposta del server non viene letta finché `on_connect` non ritorna. Scartata.

6. **Watchdog assente, affidamento al TCP keepalive.** Pro: meno codice. Contro: il keepalive TCP non rileva applicazioni che non rispondono; le connessioni zombie restano "aperte" per ore. Scartata.

## Conseguenze

**Positive**
- Il transport è agnostico: funziona per qualsiasi exchange, con hook e callback iniettati.
- Il comportamento su errori HTTP è corretto per il protocollo, non per un exchange specifico.
- Il WS gestisce i casi patologici (login, riconnessione, silenzio) in modo trasparente.
- La primitiva `RetryableWithDelay` è condivisa tra transport e adapter senza accoppiamento.

**Negative**
- Il retry per-tentativo può sotto-utilizzare il budget in caso di errori di rete locali.
- Il listen loop concorrente introduce una race: se `on_connect` è lento, i messaggi si accumulano in buffer. Accettabile con 1-2 login.
- Due classi, due file, due guide: più superficie da mantenere.

**Neutre**
- La gestione del 418 è predisposta per Binance ma non attiva per Bitget (che non lo usa).
- `retry_async` ha un parametro `compute_delay` che oggi è usato solo dal transport.

## Riferimenti

- Moduli: `core/http_client.py`, `core/ws_client.py`, `core/exceptions.py`, `core/utils.py`
- Guide: `docs/guides/transport.md`
- ADR correlati: ADR-0003 (composizione vs ereditarietà), ADR-0005 (composizione fallback rate limiter)
- Documentazione esterna: RFC 7231 (`Retry-After`), [aiohttp](https://docs.aiohttp.org/), [websockets](https://websockets.readthedocs.io/)
