# `docs/getting-started.md`

Ecco il contenuto. È pensato per chi arriva al progetto la prima volta (anche te stesso tra sei mesi): porta da zero a worker in esecuzione in meno di 15 minuti.

```markdown
# Getting Started

Guida rapida per installare, configurare e avviare Pipster in ambiente di sviluppo.

> **Stato attuale del progetto**: l'ingestion engine è in sviluppo attivo
> (Fase A della [Roadmap](roadmap.md)). Le altre componenti (risk manager,
> order executor, backtesting) sono pianificate.

## Prerequisiti

Assicurati di avere installato:

- **Python 3.11+** (per esecuzione locale senza Docker)
- **Docker** e **Docker Compose v2** (raccomandato)
- **Git**
- **Redis Stack Server** (fornito via Docker, nessuna installazione locale richiesta)

Verifica le versioni:

```bash
python --version    # >= 3.11
docker --version    # >= 24
docker compose version  # >= 2.20
```

## 1. Clona il repository

```bash
git clone <url-repo> pipster
cd pipster
```

## 2. Configura le credenziali

Copia il file di esempio e compila le variabili:

```bash
cp .env.example .env
```

Apri `.env` con il tuo editor e imposta **almeno**:

```env
# Credenziali Bitget (usa API demo per i primi test)
BITGET_API_KEY=your_api_key_here
BITGET_API_SECRET=your_api_secret_here
BITGET_PASSPHRASE=your_passphrase_here

# Redis (default: container interno)
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0

# Simbolo e timeframe da tracciare
SYMBOL=BTCUSDT
TIMEFRAME=1m
MARKET_TYPE=spot
```

Per creare le API key:
- **Produzione**: [Bitget API Management](https://www.bitget.com/api-doc/common/intro)
- **Demo trading**: attiva la modalità demo nell'account, crea una Demo API Key,
  e imposta `BITGET_DEMO=true` in `.env` (aggiunge l'header `paptrading: 1`).

> ⚠️ **Mai committare `.env`**. È già nel `.gitignore`. Se lo esponi per errore,
> revoca immediatamente le chiavi da Bitget.

## 3. Avvia Redis

Redis è il cuore dell'hot layer: streams, rate limiter, live candles.

```bash
docker compose up -d redis
```

Verifica che sia pronto:

```bash
docker compose exec redis redis-cli ping
# Atteso: PONG
```

## 4. Avvia il bot

### Modalità sviluppo (con debugger remoto)

```bash
docker compose --profile dev up
```

Il debugger ascolta su `localhost:5678`. Puoi collegarti da VS Code con la
configurazione in `.vscode/launch.json`.

### Modalità produzione (senza debugger)

```bash
docker compose up -d bot
```

### Script di avvio rapido

- **Linux/macOS**: `./scripts/avvia_bot.sh`
- **Windows**: `scripts\avvia_bot.bat`

## 5. Verifica che funzioni

### Controlla i log

```bash
docker compose logs -f bot
```

Cosa cercare:
- `🔥 [WARM-UP]` → inizio dello scarico storico
- `✅ [WARM-UP] Sincronizzazione completata: N nuove candele` → storico caricato
- `⚡ [LIVE] Avvio ascolto WebSocket in tempo reale...` → inizio del flusso live

### Ispeziona Redis

```bash
# Numero di candele nello stream
docker compose exec redis redis-cli XLEN stream:market:spot:BTCUSDT:1m

# Ultime 3 candele
docker compose exec redis redis-cli XREVRANGE stream:market:spot:BTCUSDT:1m + - COUNT 3

# Candela live corrente (Hash)
docker compose exec redis redis-cli HGETALL live:market:spot:BTCUSDT:1m
```

Se vedi candele nello stream e la hash live aggiornarsi, il worker funziona.

## 6. Ferma il bot

```bash
# Ferma i container (mantiene i dati Redis nel volume)
docker compose down

# Ferma e cancella anche i dati Redis
docker compose down -v
```

Il worker gestisce `SIGTERM`/`SIGINT` per uno shutdown pulito: chiude WebSocket,
Redis e rilascia le risorse entro 10 secondi.

## Troubleshooting

### `Redis connection refused`

Il container Redis non è ancora pronto. Aspetta 5-10 secondi e riprova.
Verifica con `docker compose ps`.

### `Bitget API error 40001` o simile

Credenziali errate o mancanti. Verifica `.env` e che le API key siano attive.
Se stai usando la demo, controlla che `BITGET_DEMO=true` sia impostato.

### `WebSocket disconnected` ripetuto

Se il WS si riconnette continuamente, controlla:
- Connessione internet funzionante
- IP non bloccato da Bitget (limite 300 connessioni/5min)
- Nessun firewall che blocca `wss://`

Il client applica backoff esponenziale con jitter: riconnessioni ravvicinate
sono normali all'inizio, poi si stabilizzano.

### Il worker parte ma non salva candele

Verifica che il simbolo esista su Bitget e che il timeframe sia supportato.
Controlla i log a livello DEBUG impostando `LOG_LEVEL=DEBUG` in `.env`.

### Gap nel flusso live

Se vedi `Gap temporale rilevato!` nei log, il worker sta colmando automaticamente
il buco tramite REST. È normale dopo una disconnessione prolungata.

## Prossimi passi

- Leggi [Architecture Overview](../architecture/overview.md) per capire come
  i componenti si parlano.
- Consulta [Ingestion Flow](../guides/ingestion.md) per i dettagli su warm-up,
  gap filling e riconciliazione.
- Se vuoi contribuire, leggi la [Roadmap](../roadmap.md) per le attività aperte.
- Per decisioni architetturali, consulta gli
  [ADR](../architecture/decisions/).

## Dove chiedere aiuto

- **Bug e feature**: apri una issue sul repository.
- **Domande architetturali**: controlla prima gli ADR e `notes.md`.
- **Documentazione Bitget**: [docs ufficiali](https://www.bitget.com/api-doc/common/intro).
```

---

## Note su questo getting-started

**Cosa ho messo dentro**:
- **Prerequisiti verificabili** con comandi copia-incolla.
- **Percorso lineare** in 6 passi: clone → config → avvia Redis → avvia bot → verifica → stop.
- **Verifica concreta**: comandi `redis-cli` che mostrano se il worker sta funzionando davvero.
- **Troubleshooting dei casi più comuni** che emergono nei primi test (Redis, credenziali, WS, gap).
- **Link a documenti correlati** per chi vuole approfondire.

**Cosa NON ho messo dentro** (e perché):
- **Configurazione avanzata**: quella va in `docs/reference/configuration.md`.
- **Spiegazione dell'architettura**: quella va in `docs/architecture/overview.md`.
- **Dettagli su rate limiting e backpressure**: vanno in `docs/guides/ingestion.md`.
- **Multi-simbolo, cold layer, monitoring**: non ancora implementati, non hanno senso qui.

**Quando aggiornarlo**:
- Quando cambia un comando (es. `docker compose` → `docker compose up`).
- Quando si aggiungono nuove variabili `.env` obbligatorie.
- Quando il flusso di avvio cambia (es. aggiunta di un servizio Prometheus).
- **Non** aggiungere sezioni "in previsione": se una feature non esiste, non va nel getting-started.

**Lunghezza**: ~120 righe, il giusto per essere consultabile in 5 minuti senza scroll infinito. Se supera le 200 righe, va spezzato in `getting-started.md` (basics) + `advanced-setup.md` (configurazioni particolari).

Quando vuoi procedere con `docs/guides/ingestion.md` (che è il naturale secondo passo dopo il getting-started), dimmelo e lo scriviamo insieme.