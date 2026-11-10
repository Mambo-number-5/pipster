# Roadmap

> Documento vivo. Aggiornato a ogni milestone.
> Decisioni architetturali: `docs/architecture/decisions/`.
> Piani dettagliati dei task: `docs/plan/`.
> Ragionamento di supporto ed evoluzione prevista: §Note di evoluzione (sotto).

## Fase A — MVP Ingestion (in corso)
- [x] Client WS asincrono (routing, throttle, unsubscribe, login)
- [ ] Client REST asincrono (in corso)
- [x] Rate limiter distribuito (ADR-0002, ADR-0004, ADR-0005)
- [ ] Hardening rate limiter (fallback atomici, A13)
- [ ] Worker asincrono con dependency injection
- [ ] Debug end-to-end
- [ ] Graceful shutdown (SIGTERM/SIGINT)
- [ ] Logging strutturato JSON
- [ ] Healthcheck container
- [ ] Config centralizzata (pydantic-settings)
- [x] Test unitari e di integrazione (continuo — tutti i moduli)
- [x] ADR e README aggiornati
- [x] Contract test WS kline (D6 avviato)
- [ ] `HttpStatusError` nel transport (body accessibile al chiamante)

## Fase B — MVP in produzione
- [ ] Deploy su ambiente demo Bitget
- [ ] Test 7 giorni su BTCUSDT spot
- [ ] Correzione bug emersi

## Fase C — Scalabilità e osservabilità
- [ ] Backpressure (coda bounded)
- [ ] Multi-simbolo sullo stesso worker
- [ ] Consumer group Redis
- [ ] Metriche Prometheus
- [ ] Dashboard Grafana
- [ ] Alerting
- [ ] Circuit breaker sul rate limiter (ADR futuro)
- [ ] Test di carico

## Fase D — Cold layer e produzione avanzata
- [ ] Cold layer (Parquet/S3/MinIO)
- [ ] CI/CD
- [ ] Secret management
- [ ] Dead letter stream per candele corrotte
- [ ] Backup Redis (AOF/RDB)
- [ ] Contract testing con Bitget demo
- [ ] Documentazione MkDocs/Sphinx
- [ ] Contract testing con Bitget demo (in corso: WS kline ✅, REST, altri topic)



## Dopo l'ingestion
- Risk Manager
- Order Executor
- Backtesting
- Monitoring avanzato

## Opzionali (post-MVP)
- Multi-exchange (Bybit, Binance)
- SBE
- Canali liquidazione futures
- Order book WS
- Redis Cluster

---

## Note di evoluzione

**Pattern di scoperta worker** (contesto: ADR-0004)

| Fase | Pattern | Fonte di verità |
|------|---------|-----------------|
| A/B  | Config obbligatoria (`RATE_LIMIT_EXPECTED_WORKERS`) | `.env` |
| C    | Lease-based (worker pubblicano lease con TTL su Redis) | Redis + fallback config |
| D+   | Service discovery (K8s API), se adottato | K8s + fallback lease |

**Safety factor del fallback locale** (contesto: ADR-0004)

- Oggi: `0.8`
- Quando il circuit breaker sarà operativo: `0.9` (il degraded mode diventa più raro)

Entrambe le evoluzioni saranno oggetto di ADR dedicati al momento
dell'implementazione. Il "quando" è in questa roadmap; il "cosa" e il "perché" sono negli ADR.