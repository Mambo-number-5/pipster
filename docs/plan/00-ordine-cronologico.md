# Ordine cronologico delle attività

> Ultimo aggiornamento: 2026-XX-XX
> Vista di alto livello: `docs/roadmap.md`
> Dettagli di ogni attività: `docs/plan/ingestion-mvp.md`
>
> Stato:
>
> - ✅ Completato
> - 🔄 In corso
> - ⏭️ Prossimo (pronto a partire)
> - ⏸️ Bloccato da dipendenza
> - 🔁 Continuo (in parallelo ad altre attività, non ha una fine netta)
> - ⬜ Da fare

## Pre-fase A — Setup e fondamenta

- ✅ Rate limiter con fallback locale (ADR-0002, 0004, 0005)
- ✅ Test unit e integrazione rate limiter
- ✅ Guida `docs/guides/rate-limiting.md`
- ✅ Devcontainer con `sleep infinity` e bind mount `.vscode-server`

## Fase A — MVP Ingestion

| #   | Attività                                                 | Stato | Dipendenze            |
| --- | -------------------------------------------------------- | ----- | --------------------- |
| A2  | Chiudere `BitgetWsClient`                                | ✅    | —                     |
| A1  | Chiudere `BitgetRestClient`                              | 🔄    | —                     |
| A3  | Riscrivere `BitgetIngestionWorker`                       | ⏸️    | A1, A2                |
| A4  | Riscrivere `BaseIngestionWorker` con DI                  | ⏸️    | A3                    |
| A5  | Debug end-to-end                                         | ⏸️    | A3, A4                |
| A6  | Graceful shutdown (SIGTERM/SIGINT)                       | ⏸️    | A5                    |
| A8  | Healthcheck container                                    | ⏸️    | A6                    |
| A7  | Logging strutturato JSON                                 | ⏸️    | A3                    |
| A9  | Config centralizzata (pydantic-settings + logging setup) | ⏸️    | A3                    |
| A10 | Test unitari dei moduli nuovi                            | 🔁    | in parallelo ad A1-A9 |
| A11 | Test di integrazione end-to-end                          | ⏸️    | A5                    |
| A12 | ADR e README aggiornati                                  | 🔁    | fine Fase A           |
| A13 | Atomicità fallback locali e manager                      | ✅    | A1, A2                |

**Prossima azione:** A1.

## Fase B — MVP in produzione

| #   | Attività                       | Stato | Dipendenze  |
| --- | ------------------------------ | ----- | ----------- |
| B1  | Deploy su ambiente demo Bitget | ⏸️    | fine Fase A |
| B2  | Test 7 giorni su BTCUSDT spot  | ⏸️    | B1          |
| B3  | Correzione bug emersi          | ⏸️    | B2          |

## Fase C — Scalabilità e osservabilità

| #   | Attività                          | Stato | Dipendenze |
| --- | --------------------------------- | ----- | ---------- |
| C1  | Backpressure (coda bounded)       | ⏸️    | B3         |
| C2  | Multi-simbolo sullo stesso worker | ⏸️    | C1         |
| C3  | Consumer group Redis              | ⏸️    | C2         |
| C4  | Metriche Prometheus               | ⏸️    | C2         |
| C5  | Dashboard Grafana                 | ⏸️    | C4         |
| C6  | Alerting                          | ⏸️    | C5         |
| C8  | Circuit breaker sul rate limiter  | ⏸️    | C4, C6     |
| C7  | Test di carico                    | ⏸️    | C2, C4     |

## Fase D — Cold layer e produzione avanzata

| #   | Attività                                  | Stato | Dipendenze                                   |
| --- | ----------------------------------------- | ----- | -------------------------------------------- |
| D2  | CI/CD                                     | 🔁    | in parallelo a Fase A                        |
| D1  | Cold layer (Parquet/S3/MinIO)             | ⏸️    | C3                                           |
| D3  | Secret management                         | ⏸️    | B1                                           |
| D4  | Dead letter stream per candele corrotte   | ⏸️    | C3                                           |
| D5  | Backup Redis (AOF/RDB)                    | ⏸️    | B1                                           |
| D6  | Contract testing Bitget                   | 🔁    | A2 (WS kline ✅), A1, cresce con ogni client |
| D7  | Documentazione sito (MkDocs/Sphinx)       | 🔁    | fine Fase D                                  |
| D+  | Orchestrazione multi-worker su Kubernetes | ⬜    | opzionale, dopo C7                           |

## Attività opzionali

| #   | Attività                        | Stato | Quando                          |
| --- | ------------------------------- | ----- | ------------------------------- |
| O1  | Multi-exchange (Bybit, Binance) | ⬜    | dopo multi-simbolo stabile      |
| O2  | SBE                             | ⬜    | solo se HFT                     |
| O3  | Dynamic throttling adattivo     | ⬜    | solo con contesa reale          |
| O4  | `unsubscribe`                   | ✅    | insieme ad A2                   |
| O5  | Canali liquidazione futures     | ⬜    | solo se futures                 |
| O6  | Order book WS                   | ⬜    | solo se strategie lo richiedono |
| O7  | Redis Cluster                   | ⬜    | solo a volumi molto alti        |
| D+  | Kubernetes                      | ⬜    | solo se carico lo giustifica    |

---

## Note sull'ordine

**A2 prima di A1:** il WS è il pezzo mancante e il più delicato; chiuderlo sblocca A3.

**A3 e A4 insieme:** sono strettamente accoppiati (worker + DI); affrontarli in sessioni consecutive evita di toccare due volte lo stesso codice.

**D2 (CI/CD) in parallelo ad A:** non aspettare la fine della Fase A. Un workflow GitHub Actions che esegue `ruff`, `mypy`, `pytest` a ogni push previene regressioni durante lo sviluppo.

**C8 prima di C7:** il test di carico (C7) deve coprire anche il comportamento in degraded mode; quindi il circuit breaker (C8) va introdotto prima.

**D+ è opzionale:** non è una tappa obbligata. Va intrapresa solo se il carico o l'operatività la giustificano.

**A13 non blocca A5-A11:** è una rifinitura che può essere fatta anche dopo il debug end-to-end, prima di B1.
