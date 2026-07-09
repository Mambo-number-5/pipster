Certamente! Ecco una bozza di nota pronta da inserire nel repository (es. `docs/BITGET_API_NOTES.md`). Puoi adattarla al formato che preferisci.

---

# 📘 Guida rapida all’uso delle API di Bitget per il Bot

Questa guida raccoglie le informazioni essenziali per interagire con le API pubbliche e private di Bitget, con particolare attenzione al modulo di **ingestione dati di mercato** e alle future componenti di **risk management ed esecuzione ordini**.

## 🔎 Come orientarsi nella documentazione ufficiale

Bitget fornisce un SDK Python (`bitget-python-sdk` su PyPI) che è un wrapper delle API REST e WebSocket. Tuttavia la documentazione ufficiale è principalmente **indipendente dal linguaggio** e descrive gli endpoint HTTP/WS in dettaglio.

**Risorse fondamentali:**
- [Documentazione API Bitget v2](https://www.bitget.com/api-doc/common/intro) – endpoint, parametri, codici di errore
- [SDK Python su GitHub](https://github.com/BitgetLimited/v3-bitget-api-sdk) – esempi di utilizzo dei metodi wrapper
- [Documentazione WebSocket pubblica](https://www.bitget.com/api-doc/common/websocket-intro) – canali e formati dei messaggi

**Consiglio pratico:** anche se non esiste una guida Python dedicata, la struttura dei parametri richiesti dai metodi dell’SDK ricalca fedelmente quella degli endpoint HTTP. Quindi, quando un metodo dell’SDK non è chiaro, **leggi la documentazione REST** per capire quali campi aspettarti.

## 🧱 Comandi fondamentali per l’ingestion dati

L’ingestion worker usa **esclusivamente endpoint pubblici**, cioè quelli che **non richiedono autenticazione** (chiavi API, firma, passphrase). Servono per scaricare dati di mercato storici e in tempo reale.

### 🔄 REST – Candele storiche (warm‑up)

- **Endpoint Spot:** `GET /api/v2/spot/market/candles`
- **Endpoint Futures (MIX):** `GET /api/v2/mix/market/candles`
- **Parametri principali:**
  - `symbol`: coppia di trading (es. `BTCUSDT`) – **senza suffissi** (niente `_SPBL` o `_UMCBL` per le candele)
  - `granularity`: time‑frame **specifico per il prodotto**
    - **Spot:** `1min`, `5min`, `15min`, `30min`, `1h`, `4h`, `12h`, `1day` (notare: minuti estesi, ore minuscole)
    - **Futures:** `1m`, `3m`, `5m`, `15m`, `30m`, `1H`, `2H`, `4H`, `12H`, `1D` (minuti abbreviati, ore maiuscole)
  - `limit`: max 1000 candele (default 100)
  - `startTime`, `endTime`: timestamp in millisecondi
- **Risposta:** array di array di stringhe: `[timestamp, open, high, low, close, baseVol, quoteVol]`

**Mappatura dei timeframe nel worker:** è implementata nel metodo `_map_timeframe_to_bitget()` e distingue automaticamente tra SPOT e futures.

### 📡 WebSocket – Candele live

- **Canale Spot:** `candle.{granularity}` (es. `candle.1h`)
- **Canale Futures:** `candle{granularity}` (es. `candle1H`)
- **Dati in arrivo:** array di candele nel formato `[ts, o, h, l, c, baseVol, quoteVol]`
- **Sottoscrizione:** il worker usa l’SDK `BitgetWsClient` che gestisce la connessione e la riconnessione. Basta passare `instType`, `channel`, `symbol`.

> ⚠️ Attenzione: i messaggi WebSocket arrivano tramite thread daemon, quindi nel worker vengono inoltrati in una `asyncio.Queue` per l’elaborazione asincrona.

## 🔐 Differenza tra API pubbliche e private

### 🌍 API pubbliche (ingestion)
- **Non richiedono autenticazione**
- Utilizzate esclusivamente dall’**Ingestion Engine** per raccogliere dati di mercato
- Includono: candele storiche, ticker, order book, trade recenti, ecc.
- Possono essere chiamate anche senza chiavi (l’SDK accetta stringhe vuote per i parametri `api_key`, `api_secret`, `passphrase`)

### 🔒 API private (trading e account)
- **Richiedono autenticazione** con API Key, Secret e Passphrase
- Saranno utilizzate dal **Risk Manager** e dall’**Order Executor**
- Includono: piazzamento/cancellazione ordini, gestione posizioni, bilanci, storico ordini
- Per le candele, esistono endpoint “private” solo se si vuole accedere a dati specifici dell’account (es. storico ordini, dettagli trade), **non** per i prezzi di mercato
- **Importante:** le chiavi API vanno generate dalla piattaforma Bitget e devono avere i permessi appropriati (es. trade, read). La firma delle richieste è gestita automaticamente dall’SDK.

### 🧩 Impatto sull’architettura del bot
- **Ingestion:** usa solo endpoint pubblici → nessuna chiave necessaria (ma l’SDK le richiede comunque; possono rimanere vuote)
- **Risk Manager / Order Executor:** useranno endpoint privati → serviranno chiavi valide con permessi di trading

## 🧪 Esempi pratici

### Richiesta candele Spot con Python SDK (come nel nostro worker)

```python
from bitget.v2.spot.market_api import MarketApi as SpotMarketApi

client = SpotMarketApi("", "", "")  # chiavi vuote per endpoint pubblici
params = {
    "symbol": "BTCUSDT",
    "granularity": "1h",
    "limit": "200"
}
response = client.candles(params)
print(response)
```

### Richiesta candele Futures (USDT‑M)

```python
from bitget.v2.mix.market_api import MarketApi as MixMarketApi

client = MixMarketApi("", "", "")
params = {
    "symbol": "BTCUSDT",
    "productType": "USDT-FUTURES",
    "granularity": "1H",
    "limit": "200"
}
response = client.candles(params)
```

### Sottoscrizione WebSocket live (Spot)

```python
ws_client = BitgetWsClient()
req = SubscribeReq("SPOT", "candle.1h", "BTCUSDT")
ws_client.subscribe(req, my_callback)
ws_client.build()
```

### Sottoscrizione WebSocket live (Futures)

```python
ws_client = BitgetWsClient()
req = SubscribeReq("USDT-FUTURES", "candle1H", "BTCUSDT")
ws_client.subscribe(req, my_callback)
ws_client.build()
```

## 🚨 Errori comuni e codici di stato

| Codice | Descrizione | Causa probabile |
|--------|-------------|-----------------|
| `40034` | Simbolo non esistente | Simbolo con suffisso non valido o inesistente (es. `BTCUSDT_SPBL` per le candele) |
| `400172` | Verifica parametri fallita | Granularity errata, parametri mancanti o incoerenti con il prodotto (es. `1H` per spot) |
| `400` generico | Bad Request | Controllare `msg` nella risposta, spesso manca un parametro obbligatorio |

Per la lista completa, consultare la [sezione errori della documentazione](https://www.bitget.com/api-doc/common/error-code).

## 🧠 Note finali sullo sviluppo

- **Mappatura timeframe:** nel file `bitget_ingestion.py` è presente `_map_timeframe_to_bitget()` che gestisce in automatico la conversione in base a `inst_type`.
- **Test e debug:** usare `print` temporanei per ispezionare i parametri REST e i messaggi WebSocket, come fatto durante il debug.
- **Autenticazione futura:** quando implementerai il risk manager, assicurati di creare un client autenticato con credenziali reali e di testarlo prima sulla **testnet** (Bitget fornisce un ambiente di test).

---

Puoi salvare questa bozza come `docs/BITGET_API_NOTES.md` e integrarla nella documentazione del repository. Se vuoi un formato diverso o dettagli su altri endpoint (es. order book, ticker) fammelo sapere!