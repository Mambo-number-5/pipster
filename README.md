# Pipster

Bot di trading algoritmico modulare per mercati crypto, con ingestion dati,
gestione del rischio, esecuzione ordini, backtesting e monitoraggio.

> ⚠️ **Stato**: in sviluppo attivo. L'ingestion engine è completo al 70%.
> Le altre componenti sono pianificate (vedi [Roadmap](docs/roadmap.md)).

## Panoramica

Pipster è pensato come una piattaforma modulare a microservizi.
Ogni componente può essere sviluppato, testato e scalato indipendentemente.

| Componente | Stato | Descrizione |
|---|---|---|
| **Ingestion Engine** | 🟡 In sviluppo | Ricezione candele REST + WS, normalizzazione, deduplica, salvataggio su Redis Streams |
| **Risk Manager** | ⚪ Pianificato | Position sizing, stop-loss, limiti di esposizione |
| **Order Executor** | ⚪ Pianificato | Invio, monitoraggio e riconciliazione ordini |
| **Backtesting** | ⚪ Pianificato | Simulazione su dati storici |
| **Monitoring** | ⚪ Pianificato | Metriche Prometheus + dashboard Grafana |

## Caratteristiche tecniche

- **Asincrono**: interamente basato su `asyncio`, senza blocchi sincroni.
- **Resiliente**: riconnessione con backoff esponenziale e jitter, watchdog del silenzio, rate limiting distribuito.
- **Multi-worker ready**: rate limiter atomico su Redis, consumer group, hash tag per Redis Cluster.
- **Testato**: unit test, integration test, stress test sul rate limiter.

## Requisiti

- Python 3.11+
- Docker e Docker Compose
- Redis (Stack Server per moduli avanzati)

## Installazione

```bash
git clone <repo>
cd pipster
cp .env.example .env
# Modifica .env con le tue credenziali Bitget
docker compose up -d redis
```

## Avvio

Modalità sviluppo (con debugger):
```bash
docker compose --profile dev up
```

Modalità produzione:
```bash
docker compose up -d
```

Script di avvio rapido:
- **Windows**: `scripts/avvia_bot.bat`
- **Linux/macOS**: `scripts/avvia_bot.sh`

## Configurazione

Tutte le variabili sono definite in `.env`. Vedi [docs/reference/configuration.md](docs/reference/configuration.md).

Esempio minimo:
```env
BITGET_API_KEY=xxx
BITGET_API_SECRET=xxx
BITGET_PASSPHRASE=xxx
REDIS_HOST=redis
REDIS_PORT=6379
SYMBOL=BTCUSDT
TIMEFRAME=1m
```

## Documentazione

- [Architettura](docs/architecture/overview.md)
- [Decisioni architetturali (ADR)](docs/architecture/decisions/)
- [Guida all'ingestion](docs/guides/ingestion.md)
- [Rate limiting](docs/guides/rate-limiting.md)
- [Deployment](docs/guides/deployment.md)
- [Roadmap](docs/roadmap.md)

## Sviluppo

Test:
```bash
docker compose exec bot pytest
```

Lint e type checking:
```bash
ruff check .
mypy core/ exchanges/
```

## Disclaimer

Uso educativo e di ricerca. Il trading comporta rischio di perdita del capitale.
Nessuna garanzia di profitto. Usa solo capitale che puoi permetterti di perdere.

## Licenza

[TBD]
```