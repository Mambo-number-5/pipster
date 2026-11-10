# Transport Layer: HTTP e WebSocket

> Stato: completo per `AsyncHttpClient` e `AsyncWsClient`.
> I client Bitget-specifici (`BitgetRestClient`, `BitgetWsClient`) sono in
> sviluppo (A1, A2). Le sezioni di integrazione saranno aggiunte al termine.

## 1. Perché due client separati

Pipster comunica con gli exchange su due canali distinti, con requisiti diversi:

- **REST (HTTP)**: richieste one-shot, con retry, timeout, e rate limit. Usato per warm-up, gap filling, sincronizzazione del tempo, e in futuro per ordini.
- **WebSocket (WS)**: connessione persistente, bidirezionale, con riconnessione automatica e heartbeat. Usato per dati live.

Le due classi `AsyncHttpClient` e `AsyncWsClient` forniscono le primitive generiche (trasporto, retry, backoff, ping), **senza sapere nulla di Bitget**.
I client specifici dell'exchange (`BitgetRestClient`, `BitgetWsClient`) si costruiscono sopra queste primitive.
Questa separazione permette di testare il transport in isolamento, e di aggiungere exchange senza toccare il codice generico.

## 2. AsyncHttpClient

### 2.1 Responsabilità

`AsyncHttpClient` incapsula `aiohttp.ClientSession` e fornisce richieste HTTP asincrone con retry automatico, timeout configurabili e un hook `before_attempt` per iniettare il rate limiting.

### 2.2 Configurazione

| Parametro         | Default                           | Descrizione                                |
| ----------------- | --------------------------------- | ------------------------------------------ |
| `default_timeout` | `aiohttp.ClientTimeout(total=10)` | Timeout predefinito per tutte le richieste |

La sessione è creata **lazy** alla prima richiesta. Se l'event loop cambia (es. test isolati), la sessione viene ricreata. Il lock garantisce che la creazione sia atomica anche con più task concorrenti.

### 2.3 Uso

```python
http = AsyncHttpClient()

# GET
data = await http.get("https://api.bitget.com/api/v3/market/candles", params={...})

# POST
result = await http.post("https://api.bitget.com/api/v3/...", json_data={...})

# DELETE
await http.delete("https://api.bitget.com/api/v3/...")

# Chiusura (alla fine del ciclo di vita)
await http.close()
```

Tutti i metodi restituiscono il **JSON decodificato** se il `Content-Type` lo consente, altrimenti il **testo** grezzo. Una risposta `204 No Content` restituisce `None`.

### 2.4 Retry

Il retry è delegato a `core.utils.retry_async`. Per default:

- **`max_retries=3`** — numero di **retry** dopo il tentativo iniziale (quindi fino a 4 tentativi totali).
- **`base_delay=1.3`** — delay iniziale in secondi. Backoff esponenziale:
  `base_delay * 2**(attempt-1)`, più jitter uniforme `random.uniform(0, 0.5)`.
- **Eccezioni ritentate**: `aiohttp.ClientError`, `asyncio.TimeoutError`, `ConnectionError`, `OSError`, `RateLimitError`.

Una richiesta che esaurisce i retry propaga l'ultima eccezione al chiamante.

#### Rate limiting: per tentativo, non per operazione

Ogni tentativo consuma il budget di rate limiting tramite `before_attempt`, inclusi i retry. Un retry per errore di rete consuma comunque un token, anche se il server non ha ricevuto la richiesta originale.

È una scelta conservativa: il server Bitget conta le richieste che riceve, e un retry che arriva è indistinguibile da una chiamata nuova. Preferiamo sotto-utilizzare il budget piuttosto che rischiare un ban.

#### 4xx e 5xx: retry solo sui transient

Il transport distingue tre categorie di errore HTTP:

| Status                            | Eccezione sollevata                                   | Retry?                                                              |
| --------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------- |
| 429, 503                          | `RateLimitError` (con `.body`)                        | Sì, delay da `Retry-After` se presente, altrimenti backoff standard |
| 418                               | `NonRetriableHttpError` (con `retry_after` e `.body`) | **No**, propagata subito                                            |
| altri 4xx (400, 401, 403, 404, …) | `NonRetriableHttpError` (con `.body`)                 | **No**                                                              |
| 5xx                               | ServerError                                           | Sì, backoff standard                                                |

I **4xx** indicano un problema nella richiesta (payload malformato, credenziali errate, risorsa inesistente): ritentarli non cambia l'esito, consuma budget di rate limit e ritarda l'emersione dell'errore. Vengono quindi propagati al chiamante al primo tentativo.

I **5xx** indicano un problema transitorio lato server (overload, deploy,
downstream): Bitget raccomanda esplicitamente di ritentarli. Vengono gestiti da `retry_async` con backoff esponenziale standard.

#### 429/503 e `Retry-After`

Un HTTP **429** o **503** è tradotto in `RateLimitError`, che eredita da `core.exceptions.RetryableWithDelay`. Se la risposta include `Retry-After`, il valore è passato al campo `.delay` e usato come delay del prossimo tentativo, ignorando il backoff calcolato. Se l'header è assente, `.delay` è `None` e `retry_async` calcola il backoff esponenziale standard.

Il 503 è incluso perché la specifica HTTP (RFC 7231) prevede `Retry-After`
su di esso, e i server lo usano quando sono temporaneamente sovraccarichi.

#### 418: ban, non rate limit

Un HTTP **418** (usato da Binance per indicare un IP auto-bannato dopo aver
insistito su un 429) è tradotto in `NonRetriableHttpError` con `retry_after` valorizzato. Non viene ritentato: ritentare durante un ban prolunga la penalità. Il `retry_after` resta disponibile al chiamante per logging, alerting, o pausa manuale.

#### Redirect 3xx

`aiohttp` segue automaticamente i redirect (fino a 10). Il transport non
intercetta i 3xx e non gestisce esplicitamente `Retry-After` su di essi.
La specifica HTTP permette `Retry-After` su qualsiasi 3xx, ma gli exchange
target non usano redirect per rate limiting. Se in futuro un exchange lo
facesse, si valuterebbe `allow_redirects=False` con loop manuale.

#### Body degli errori HTTP

Tutte le eccezioni HTTP (`HttpStatusError` e sottoclassi) portano il body della risposta in `.body` (troncato a 500 caratteri). Il body è letto **prima** di sollevare l'eccezione, perché `ClientResponseError` di aiohttp non lo trasporta.
Su status >= 400 il body è anche loggato a WARNING.

Questo permette agli adapter di exchange di parsare il codice business (es. Bitget `40006`/`40008`/`40009`) direttamente da `e.body`, senza dover rifare la chiamata o leggere un log.

### 2.5 Hook `before_attempt`

Prima di ogni tentativo (incluso il primo), se `before_attempt` è specificato, viene eseguito. È il punto di iniezione del rate limiter:

```python
async def rate_limit_hook():
    await limiter.acquire("global", "candles")

data = await http.get(url, params=params, before_attempt=rate_limit_hook)
```

Comportamento:

- **Chiamato a ogni tentativo**, non solo al primo. Coerente con il principio "rate limit per tentativo" (§2.4).
- **Chiamato anche per tentativi che falliranno con 4xx**: il token è consumato comunque.
- **Se solleva, il retry non lo intercetta** (a meno che l'eccezione sia in `retry_exceptions`): risale al chiamante. Questo è desiderabile: se il rate limiter è mal configurato, meglio fallire subito che riprovare in loop.

### 2.6 Gestione del ciclo di vita

- La sessione è creata alla prima richiesta.
- `close()` chiude la sessione e rilascia le risorse.
- Chiamare `close()` più volte è sicuro (no-op se già chiusa).

## 3. AsyncWsClient

### 3.1 Responsabilità

`AsyncWsClient` gestisce il ciclo di vita di una connessione WebSocket:
apertura, ascolto dei messaggi, ping heartbeat, watchdog del silenzio,
riconnessione con backoff. Non conosce il formato dei messaggi né il
protocollo dell'exchange.

### 3.2 Configurazione

| Parametro              | Default  | Descrizione                                           |
| ---------------------- | -------- | ----------------------------------------------------- |
| `url`                  | —        | Endpoint WebSocket (`wss://...`)                      |
| `ping_interval`        | `30.0`   | Secondi tra ping heartbeat                            |
| `ping_payload`         | `"ping"` | Payload del ping (se `ping_origin_protocol=False`)    |
| `base_reconnect_delay` | `1.0`    | Delay iniziale per il backoff                         |
| `max_reconnect_delay`  | `150.0`  | Delay massimo per il backoff                          |
| `silence_timeout`      | `70.0`   | Secondi senza messaggi prima di forzare riconnessione |
| `ping_origin_protocol` | `False`  | Se `True`, usa il ping nativo del protocollo WS       |

### 3.3 Uso

```python
client = AsyncWsClient(url="wss://ws.bitget.com/v2/ws/public")

async def on_message(raw: str):
    # parsing specifico dell'exchange
    ...

async def on_connect():
    # login, subscribe iniziale
    ...

client.on_message = on_message
client.on_connect = on_connect

await client.start()      # avvia il loop di connessione + riconnessione
await asyncio.sleep(...)  # il client gira in background
await client.stop()       # chiude la connessione
```

Il metodo `start()` **ritorna immediatamente**: la connessione vive in un task di background (`_listen_task`). Il client è operativo fino a `stop()`.

### 3.4 Ciclo di vita della connessione

Per ogni connessione attiva, `AsyncWsClient` esegue:

1. Apre il socket con `websockets.connect`.
2. Invoca `on_connect` (utile per login e subscribe).
3. Avvia i task contestuali: `_ping_loop` (se `ping_interval > 0` e `ping_origin_protocol=False`) e `_silence_watchdog` (se `silence_timeout > 0`).
4. Entra nel loop `async for message in ws` e invoca `on_message` per ogni messaggio.
5. Al termine (chiusura o eccezione), cancella i task contestuali e azzera `self._ws`.

I task contestuali sono **creati e distrutti per ogni connessione**. Questo
evita ping orfani dopo una riconnessione.

### 3.5 Riconnessione con backoff

Il loop `_reconnect_loop` gestisce le riconnessioni con backoff esponenziale e jitter:

- **Backoff**: `delay = min(max_reconnect_delay, base_reconnect_delay * 2**attempt)`.
- **Jitter**: aggiunge `random.uniform(0, 0.3 * delay)` per evitare riconnessioni sincronizzate tra più worker.
- **Reset**: se una connessione è durata più di **10 secondi**, `attempt` viene azzerato al prossimo giro. Questo evita che un singolo blip iniziale gonfi il backoff per ore.

Il loop è **infinito**: girano finché `stop()` non imposta `_running = False`.

### 3.6 Ping: due strategie

Il parametro `ping_origin_protocol` decide la strategia:

- **`False` (default)**: il client invia `ping_payload` come stringa ogni
  `ping_interval` secondi, tramite `_ping_loop`. Utile quando l'exchange
  richiede un ping applicativo (es. `{"op": "ping"}`).
- **`True`**: il client lascia che la libreria `websockets` gestisca il ping nativo del protocollo (frame di controllo). `_ping_loop` non parte.

Per Bitget useremo probabilmente `ping_payload='{"op":"ping"}'` con
`ping_origin_protocol=False` — da verificare in A2.

### 3.7 Watchdog del silenzio

`_silence_watchdog` monitora il tempo trascorso dall'ultimo messaggio ricevuto.
Se supera `silence_timeout`:

1. Logga un warning.
2. Chiude il socket forzatamente (`self._ws.close()`).
3. La chiusura fa terminare `async for`, il che fa uscire `_connect_and_listen`, e `_reconnect_loop` tenta una nuova connessione.

Il watchdog è essenziale per rilevare **connessioni zombie**: socket
apparentemente aperti (TCP keepalive OK) ma che non ricevono più messaggi.

### 3.8 Callbacks

Tre callback opzionali, tutte async o sync (il client gestisce entrambe):

- **`on_message(raw: str)`**: invocata per ogni messaggio ricevuto.
- **`on_connect()`**: invocata subito dopo la connessione, prima del loop di ascolto. Qui si fa login e subscribe.
- **`on_error(exc: Exception)`**: invocata su eccezioni di rete, prima del calcolo del backoff.

Le eccezioni sollevate dentro una callback **non** interrompono il client:
sono loggate e ignorate. Questo garantisce che un bug nel parsing non
distrugga la connessione.

### 3.9 Invio di messaggi

```python
await client.send('{"op":"subscribe","args":[...]}')
```

`send` solleva `RuntimeError` se il socket non è connesso.

> Non c'è coda: l'invio è diretto sul socket attivo.
> Se serve throttling (es. Bitget limita 10 msg/s), va implementato nel client specifico (`BitgetWsClient`), sopra `send`.

## 4. Test

- `tests/unit/test_http_client.py`: copre entrambi i client con mock di `aiohttp.ClientSession.request` e `websockets.connect`. Nessuna rete reale.
- `MockWebSocket`: implementazione pura-Python dell'interfaccia di `websockets`, evita i problemi di compatibilità degli `AsyncMock` con gli async iterator.

## 5. Riferimenti

- Moduli: `core/http_client.py`, `core/utils.py` (`retry_async`)
- Test: `tests/unit/test_http_client.py`
- Librerie: [aiohttp](https://docs.aiohttp.org/), [websockets](https://websockets.readthedocs.io/)
- Client specifici (in sviluppo): `exchanges/bitget/bitget_api.py`, `exchanges/bitget/bitget_ws.py`
- ADR correlati: ADR-0003 (composizione vs ereditarietà)
- Eccezioni HTTP: `core.exceptions` (`HttpStatusError`, `NonRetriableHttpError`, `ServerError`, `RateLimitError`)
- Retry: `core/utils.py` (`retry_async`, `compute_delay`)

### 5.1 retry_async

`core/utils.py` implementa `retry_async`: applica Exponential Backoff con jitter a un `awaitable_factory` (callable senza argomenti che ritorna un awaitable). Usato sia da `AsyncHttpClient` sia, in futuro, da altri componenti che richiedono retry di rete.

Firma:

    retry_async(awaitable_factory, max_retries=6, base_delay=1.3, exceptions=(...))

Semantica di `max_retries`: numero di **retry** dopo il tentativo iniziale, non numero totale di tentativi.

```

---

### Note su cosa ho lasciato fuori

- **Sezione su `BitgetWsClient`**: `<!-- TODO -->` implicito, il titolo dice "in sviluppo". Quando A2 sarà chiusa, aggiungi una sezione 6 "Client Bitget" con login, subscribe, routing, throttling.
- **Esempi di throttling**: non ci sono, perché `AsyncWsClient` non ne ha. Sono responsabilità di `BitgetWsClient` (vedi 3.9).
- **`retry_async`**: menzionata ma non documentata in dettaglio. Se vuoi, in futuro puoi scrivere una guida `docs/guides/retry.md` dedicata — ma per ora il riferimento al modulo basta.

### Sul test file

Ho dato un'occhiata: le due classi sono coperte in modo sensato, il `MockWebSocket` è ben fatto (aggira il problema reale degli `AsyncMock` con gli async iterator), e i test coprono happy path e reconnessione.

Se la guida va bene, il prossimo passo è A2: mandami lo stato attuale di `BitgetWsClient` (se esiste un file `bitget_ws.py`, o se è ancora dentro `bitget_api.py`), e partiamo con la scrittura.
```
