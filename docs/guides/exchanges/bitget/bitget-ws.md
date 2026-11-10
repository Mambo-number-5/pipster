# Bitget WebSocket Client

> Stato: completo per canali pubblici. Login e canali privati implementati e coperti da test unitari con mock; verifica contro demo API key rimandata a D6.
>
> Decisioni architetturali: ADR-0003 (composizione vs ereditarietà), ADR-0005 (composizione del fallback rate limiter).
> Transport sottostante: `docs/guides/transport.md`.

## 1. Differenza V2 vs V3

Bitget ha migrato l'API REST a V3, ma la documentazione WebSocket pubblica include ancora la V2. Le due versioni differiscono nel **formato dei messaggi**, non solo nell'URL:

| Aspetto | V2 | V3 |
|---|---|---|
| Identificativo canale | `channel: "candle1m"` | `topic: "kline"` + `interval: "1m"` |
| Identificativo strumento | `instId: "BTCUSDT"` | `symbol: "BTCUSDT"` |
| Casing `instType` | `"SPOT"` | `"spot"` |
| URL pubblico | `wss://ws.bitget.com/v2/ws/public` | `wss://ws.bitget.com/v3/ws/public` |
| URL privato | `wss://ws.bitget.com/v2/ws/private` | `wss://ws.bitget.com/v3/ws/private` |

Pipster usa **solo V3**. Il codice che troverai in guide o esempi online in formato V2 non è compatibile.

## 2. URL: produzione e demo

Il costruttore di `BitgetWsClient` seleziona l'URL da due flag:

| `private` | `demo` | URL |
|---|---|---|
| `False` | `False` | `wss://ws.bitget.com/v3/ws/public` |
| `False` | `True` | `wss://wspap.bitget.com/v3/ws/public` |
| `True` | `False` | `wss://ws.bitget.com/v3/ws/private` |
| `True` | `True` | `wss://wspap.bitget.com/v3/ws/private` |

**Canali pubblici**: nessun login richiesto. `api_key=None` è sufficiente.
**Canali privati**: login obbligatorio. Demo richiede una **Demo API Key** generata dal pannello Demo di Bitget, non la chiave di produzione.

## 3. Login (canali privati)

Il login avviene automaticamente in `_on_reconnect`, subito dopo l'apertura della connessione, **se** `api_key`, `api_secret` e `passphrase` sono tutti valorizzati.

Meccanismo:

1. Costruzione della stringa `timestamp + "GET" + "/user/verify"`.
2. HMAC-SHA256 con `api_secret` come chiave, base64 dell'output.
3. Invio di `{"op": "login", "args": [{"apiKey", "passphrase", "timestamp", "sign"}]}`.
4. Attesa della risposta (`{"event":"login","code":"0"}`) con timeout di 10 secondi.

Il `timestamp` è in **secondi** (`int(time.time())`), non millisecondi. La doc V3 contiene un'ambiguità (testo "milliseconds", esempi in secondi) risolta in favore degli esempi, che sono quelli con cui il server risponde. Va verificato in D6 con demo key.

Se le credenziali sono assenti ma `private=True`, il login è comunque skippato (nessuna eccezione), con warning nel log. La connessione resta aperta ma i canali privati non funzionano.

## 4. Subscribe e unsubscribe

### Subscribe

```python
req = SubscribeReq.kline("spot", "BTCUSDT", "1m")
await client.subscribe([req], callback=on_kline)
```

`subscribe` fa tre cose:

1. **Verifica il limite di 1000 canali per connessione** (`ValueError` se superato).
2. **Consuma il throttle orario** (`LocalFixedWindow(240, 3600)`); `RuntimeError` se esaurito.
3. **Registra la callback** in `self.subscriptions[req] = callback`.
4. **Invia il frame** `{"op":"subscribe","args":[req.to_dict(), ...]}` — solo se la connessione è attiva. Se non lo è, la registrazione resta e verrà riproposta da `_on_reconnect`.

### Unsubscribe

```python
await client.unsubscribe([req])
```

Invia `{"op":"unsubscribe","args":[...]}` e rimuove le chiavi dal registry locale **dopo** l'invio riuscito. Se il socket è caduto, il registry resta coerente e `_on_reconnect` reinvierà la subscribe (che Bitget ignora se già attiva).

## 5. Topics supportati

| Topic | Costruttore | Payload (consumer) |
|---|---|---|
| `kline` | `SubscribeReq.kline(inst_type, symbol, interval)` | OHLCV |
| `ticker` | `SubscribeReq.ticker(inst_type, symbol)` | best bid/ask, last, 24h stats |
| `books` / `books1` / `books5` / `books50` | `SubscribeReq.books(inst_type, symbol, levels=1\|5\|50\|None)` | depth snapshot e update |
| `publicTrade` | `SubscribeReq.public_trade(inst_type, symbol)` | trade fills |
| `liquidation` | `SubscribeReq.liquidation(inst_type)` | liquidazioni aggregate per secondo (**no symbol**: è globale per exchange) |

Il campo `interval` è opzionale (solo kline lo usa); `symbol` è opzionale (liquidation non lo usa). Il modello è unico, la variazione vive nei costruttori nominati.

**Payload parsing**: il client **non** interpreta i payload. `on_message` riceve la stringa raw. L'interpretazione è responsabilità del consumer (worker, strategia). Questo evita al client di conoscere gli schemi di ogni topic, e consente di parsare ogni canale con una funzione pura testabile a parte.

## 6. Routing e callback

Ogni messaggio dati ricevuto passa da `_handle_raw_message`, che:

1. Ignora `"pong"` (risposta al ping).
2. Tenta `WsEnvelope.from_json`. Se il JSON è invalido, logga e ignora.
3. Smista per `event`: `login`, `error`, `subscribe`, `unsubscribe`.
4. Se è un push dati (`env.data` + `env.arg`), ricostruisce la chiave con `SubscribeReq.from_arg` e cerca la callback in `subscriptions`.

```python
async def on_kline(raw: str) -> None:
    env = WsEnvelope.from_json(raw)
    candle = env.data[0]   # {"start", "open", "high", ...}
    ...

await client.subscribe([SubscribeReq.kline("spot", "BTCUSDT", "1m")], on_kline)
```

Se l'`arg` è malformato (manca `instType` o `topic`), viene loggato un warning e il messaggio è scartato: nessuna eccezione risale al transport.

### Callback sync vs async

`on_message`, `on_connect`, `on_error` e le callback di `subscribe` possono essere sync o async. `_invoke_callback` rileva con `inspect.iscoroutinefunction` e comporta di conseguenza. Le eccezioni dentro una callback sono loggate e non interrompono il client.

## 7. Throttle

Due limiti indipendenti, entrambi in-memory (nessun Redis):

| Limite | Implementazione | Valore |
|---|---|---|
| Messaggi in uscita | `LocalSlidingWindow` | 10 msg/s |
| Subscribe | `LocalFixedWindow` | 240 / ora |

**Il ping non consuma il throttle dei messaggi in uscita**, perché è inviato direttamente da `AsyncWsClient`, non passa per `_send_payload`. Il server Bitget conta comunque il ping nel limite di 10 msg/s, ma il suo contributo è irrilevante (1 ogni 30s).

I due throttle sono **per connessione**, non globali. Ogni `BitgetWsClient` ha i suoi. Questo è coerente con i limiti Bitget, che sono espressi "per connection" (subscribe) e "per IP" (connessioni).

### Nota sulla condivisione con il rate limiter REST

Bitget dichiara che REST e WS **condividono la quota globale** (6000 req/IP/min). Tuttavia il WS contribuisce in modo trascurabile (10 msg/s contro 100 req/s) e la contabilizzazione richiederebbe di accoppiare i due client.

Il client espone un hook opzionale `before_send: Callable[[dict], Awaitable[dict]]` che riceve il payload e restituisce il payload (eventualmente modificato). Il worker lo usa per agganciare il rate limiter condiviso:

```python
async def before_send(payload: dict) -> dict:
    await rate_limiter.acquire("public_ip")
    return payload

ws = BitgetWsClient(api_key=..., before_send=before_send)
```

Il client **non** conosce il rate limiter, non conosce Redis, non conosce il nome del bucket. Sa solo che prima di inviare c'è un hook opzionale.

## 8. Riconnessione e replay

`AsyncWsClient` gestisce la riconnessione (vedi `transport.md`). `BitgetWsClient` si aggancia via `on_connect`:

1. Se credenziali presenti, esegue `login()` e attende l'esito.
2. Se `subscriptions` non è vuoto, invia un frame `subscribe` con **tutte** le richieste registrate.

Il listen loop è già attivo durante `_on_reconnect`: la risposta al login arriva perché `AsyncWsClient` legge dal socket in parallelo. Questo è il motivo per cui `on_connect` **può** attendere la risposta del server senza deadlock.

**Se il login fallisce**: log error, `_on_reconnect` prosegue comunque con il replay delle subscribe pubbliche (che non richiedono login). Le subscribe private falliranno lato server, ma il client non chiude la connessione.

## 9. Hook `before_send`

Firma: `Callable[[dict], Awaitable[dict]]`.

Riceve il payload serializzato (dict Python, non stringa), restituisce il payload — eventualmente modificato. Usi tipici:

- Rate limiting condiviso (vedi §7).
- Aggiunta di campi di tracing.
- Mascheramento di parametri sensibili nei log (con un wrapper che logga prima di restituire).

L'hook è invocato **prima** del throttle locale e dell'invio. Se solleva, il messaggio non parte e l'eccezione risale al chiamante (`subscribe`, `unsubscribe`, `login`).

## 10. Contract test

`tests/contract/exchanges/bitget/test_bitget_ws.py` verifica contro l'endpoint demo reale che:

- Il formato dei messaggi kline corrisponda allo schema documentato (campi `start`, `open`, `high`, `low`, `close`, `volume`, `turnover`).
- `WsEnvelope.from_json` gestisca il messaggio reale.
- `SubscribeReq.from_arg` ricostruisca la stessa `SubscribeReq` usata in subscribe.

Il test è **skippato di default** e si attiva con `BITGET_CONTRACT_TEST=1`. Non gira in CI: dipende dalla rete esterna. Va eseguito manualmente dopo modifiche al client, e periodicamente per rilevare cambi di formato da parte di Bitget (D6).

## 11. Riferimenti

- Moduli: `exchanges/bitget/bitget_ws.py`, `core/ws_client.py`
- Test unitari: `tests/unit/exchanges/bitget/test_bitget_ws_client.py`
- Contract test: `tests/contract/exchanges/bitget/test_bitget_ws.py`
- Documentazione Bitget: [Quick Start V3](https://www.bitget.com/api-doc/common/intro), [WebSocket](https://www.bitget.com/api-doc/common/websocket-intro), [Candlestick Channel](https://www.bitget.com/api-doc/contract/market/Candlestick-Channel)
- ADR correlati: ADR-0003, ADR-0005
