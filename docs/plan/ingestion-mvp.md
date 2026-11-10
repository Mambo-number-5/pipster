# Piano dettagliato — Ingestion Engine MVP

> Documento volatile: si aggiorna a ogni sessione di lavoro.
> Contesto decisionale: `docs/architecture/decisions/`.
> Vista di alto livello: `docs/roadmap.md`.
> Quando il progetto avrà un issue tracker, questo file sarà sostituito da issue individuali e verrà archiviato.
> In un progetto solo, con issue tracker non configurato, questo è il posto giusto.
> Quando avrò GitHub/GitLab Issues, il piano dettagliato migra lì e docs/plan/ si svuota. Fino ad allora, tiene il file.

# Roadmap attività — Ingestion Engine Pipster

---

## FASE A — Chiusura SDK e MVP

---

### A1. Chiudere `BitgetRestClient`

**Motivazione e scopo:**  
Il `BitgetRestClient` è la base di tutte le operazioni REST: warm-up, gap filling, sincronizzazione del tempo. Senza di esso il worker non può recuperare candele storiche né riconciliare i buchi. Chiuderlo significa definire con certezza gli endpoint V3 (o V2 dove V3 non copre), i bucket di rate limiting definitivi, la gestione della firma HMAC con query string, e la sincronizzazione del server time.

**Come implementarla:**  
Completare `fetch_server_time` con l'endpoint V3, definire in `init_rate_limiter` i bucket effettivi per gli endpoint usati (candele, time, eventuali endpoint account), e verificare che `_request` passi correttamente `before_attempt` a `AsyncHttpClient`. Testare ogni endpoint con la demo API di Bitget per confermare formato e codice di risposta.

**Quando:**  
Immediatamente, è il prerequisito per tutto il resto della Fase A. 4-6 ore.

---

### A2. Chiudere `BitgetWsClient`

**Motivazione e scopo:**  
Il `BitgetWsClient` gestisce il flusso live: login, subscribe, routing dei canali, throttle dei 10 msg/s. È quasi completo, ma mancano verifiche finali sulla gestione della riconnessione (login confermato, ripristino sottoscrizioni), sul throttle locale e sulla coerenza del routing. Chiuderlo significa avere un client WebSocket che non perde candele e non disconnette per limiti violati.

**Come implementarla:**  
Verificare che `_on_reconnect` attenda il login, che `_send_payload` sia usato ovunque (login, subscribe, reconnect), che il routing usi `SubscribeReq` come chiave. Aggiungere il TODO per `unsubscribe` (rimandato alla fase opzionale). Testare con la demo WS di Bitget su un canale candele pubblico.

**Quando:**  
Subito dopo A1, perché il worker li usa entrambi insieme. 2-4 ore.

---

### A3. Riscrivere `BitgetIngestionWorker` con i nuovi client

**Motivazione e scopo:**  
Il worker attuale usa ancora l'SDK sincrono di Bitget (`asyncio.to_thread`, `threading.Event`, `BitgetWsClient` dell'SDK). Va riscritto per usare i client asincroni proprietari, eliminando ogni traccia di blocking e threading manuale. Questo è il cuore dell'integrazione: se il worker non è asincrono puro, tutta l'architettura perde i suoi vantaggi.

**Come implementarla:**  
Sostituire `MarketApi` con `BitgetRestClient`, sostituire il client WS dell'SDK con `BitgetWsClient`, rimuovere `asyncio.Queue`, `threading.Event` e `run_coroutine_threadsafe`. La callback `on_message` diventerà un semplice `await self._process_raw_message(raw)`. Adattare `fetch_historic_klines` al formato V3 (`category`, `interval`, `type`).

**Quando:**  
Dopo A1 e A2. 8-12 ore.

---

### A4. Riscrivere `BaseIngestionWorker` con dependency injection

**Motivazione e scopo:**  
`BaseIngestionWorker` attualmente crea direttamente un `RedisTokenBucket` con parametri hardcoded di Bitget. Questo viola la separazione core/exchange: il core non deve conoscere i limiti di un exchange specifico. La soluzione è iniettare nel costruttore le dipendenze (rate limiter, client REST/WS) così che il worker agnostico riceva tutto già configurato.

**Come implementarla:**  
Modificare il costruttore di `BaseIngestionWorker` per accettare `rest_client` e `ws_client` come parametri, oppure lasciare che le sottoclassi li creino e li passino. Rimuovere ogni `os.getenv("BITGET_...")` dal core: quelli sono parametri dell'exchange e appartengono al `BitgetIngestionWorker`.

**Quando:**  
Contestualmente ad A3, perché sono strettamente accoppiati. 4-6 ore.

---

### A5. Debug end-to-end

**Motivazione e scopo:**  
Fino a questo punto ogni pezzo è stato testato in isolamento. Serve verificare che il flusso completo funzioni: avvio → sync time → warm-up → gap filling → live → salvataggio su Redis. È qui che emergono i bug di integrazione: race condition, formati dati inattesi, timeout, comportamenti di riconnessione anomali.

**Come implementarla:**  
Avviare il worker su BTCUSDT spot, timeframe 1m, con Redis reale e API demo Bitget. Monitorare con `redis-cli XRANGE` che le candele arrivino in ordine, che i duplicati siano scartati, che i gap vengano colmati. Verificare anche i casi patologici: kill del WS, Redis rallentato, simbolo inesistente.

**Quando:**  
Dopo A3 e A4, prima di procedere a Fase B. 4-8 ore.

---

### A6. Graceful shutdown (SIGTERM/SIGINT)

**Motivazione e scopo:**  
Docker invia SIGTERM e attende 10 secondi prima di uccidere il container con SIGKILL. Se il worker non chiude Redis e WebSocket entro quel tempo, si rischiano connessioni appese, stream Redis con messaggi pendenti, e log di warning. Un graceful shutdown pulito è indispensabile in produzione.

**Come implementarla:**  
Registrare handler per SIGTERM e SIGINT con `asyncio.get_event_loop().add_signal_handler`, che impostano `self.running = False` e segnalano a `asyncio.Event` di terminazione. Il ciclo principale di `run()` attende l'evento e nel `finally` chiude Redis e WebSocket. In Dockerfile, usare `CMD ["python", "-u", "main.py"]` per avere log unbuffered.

**Quando:**  
Dopo A5, prima del deploy. 3-4 ore.

---

### A7. Logging strutturato JSON

**Motivazione e scopo:**  
I log testuali sono leggibili in sviluppo ma ingestibili in produzione, dove servono aggregatori (Loki, ELK, Datadog). Il logging strutturato in JSON permette di filtrare per simbolo, timeframe, livello, e correlare eventi tra worker diversi. Va deciso ora perché ogni riga di log aggiunta dopo dovrà adeguarsi.

**Come implementarla:**  
Logging strutturato JSON: il formatter JSON è implementato dentro `core/logging_setup.py` (introdotto in A9), non come modulo separato. `configure()` accetta `json_format: bool` e `log_level: str` letti da `Settings`. Nessun `basicConfig` nei moduli. Configurare il logger root con `dictConfig`. Aggiungere campi contestuali (`symbol`, `timeframe`, `worker_id`) tramite `LoggerAdapter` o `extra`. Mantenere la formattazione lazy (`%s`) nel codice: il formatter si occupa della serializzazione.

**Quando:**  
Contestualmente ad A3, così tutte le nuove righe di log nascono già strutturate. 2-3 ore.

---

### A8. Healthcheck container

**Motivazione e scopo:**  
Docker e Kubernetes hanno bisogno di sapere se il processo è vivo. Un worker bloccato in un loop infinito o in attesa su una coda non risponde ai comandi, ma il container appare "up". Senza healthcheck, il sistema non riavvia il container e il worker resta morto per ore. L'healthcheck è la prima linea di resilienza operativa.

**Come implementarla:**  
Due opzioni: (1) file `/tmp/healthy` aggiornato ogni 30s dal worker, con `HEALTHCHECK` nel Dockerfile che verifica il timestamp; (2) endpoint HTTP minimale su una porta locale, che risponde 200 se il worker è attivo. L'opzione (2) è più standard e compatibile con Prometheus, ma richiede `aiohttp.web`. Scegliere in base ai bisogni di monitoring futuri.

**Quando:**  
Contestualmente ad A6, prima del deploy in Fase B. 2-3 ore.

---

### A9. Config centralizzata

**Motivazione e scopo:**  
Attualmente ogni modulo legge da `os.getenv()` in punti diversi del codice. Questo rende difficile sapere quali variabili sono necessarie, e impossibile validare la configurazione all'avvio. Una config centralizzata con validazione previene errori come credenziali mancanti o timeout malformati che si manifesterebbero solo a runtime. Include `core/logging_setup.py`: unico punto che configura il logging (livello, formatter, destinazione). `main.py` e `tests/run.py` chiamano `configure()` all'avvio. I moduli non chiamano mai `logging.basicConfig()`.

**Come implementarla:**  
Usare `pydantic-settings` con una classe `Settings` che dichiara tutti i campi con tipo e default. Il file `.env` viene letto all'avvio e validato: se manca `BITGET_API_KEY`, l'app non parte con un errore chiaro. Passare l'istanza di `Settings` ai componenti che ne hanno bisogno, invece di leggere `os.getenv` ovunque.

**Quando:**  
Contestualmente ad A3, così i nuovi moduli nascono con config iniettata. 4-6 ore.

---

### A10. Test unitari dei moduli nuovi

**Motivazione e scopo:**  
Il rate limiter, i client HTTP/WS e i client Bitget sono moduli critici che, se rotti, causano perdite di dati o ban dall'exchange. I test unitari garantiscono che il comportamento resti corretto durante i refactoring futuri. Il costo di scriverli ora è molto inferiore al costo di debuggare un bug in produzione.

**Come implementarla:**  
Usare `pytest` con `pytest-asyncio`. Mockare `AsyncHttpClient` e `AsyncWsClient` dove serve, usare Redis reale (db 15) per i test del rate limiter. Target: 70%+ di coverage sui moduli core e exchange. Integrare in CI (vedi D2).

**Quando:**  
Durante tutta la Fase A, man mano che i moduli vengono chiusi. 8-12 ore distribuite.

---

### A11. Test di integrazione end-to-end

**Motivazione e scopo:**  
I test unitari verificano i singoli pezzi, ma non il flusso completo. Un test di integrazione avvia il worker con un mock exchange (o la demo Bitget) e verifica che una candela arrivi correttamente in Redis, dal WS o dal REST. È l'unico modo per garantire che l'integrazione tra i moduli funzioni.

**Come implementarla:**  
Creare un `MockExchange` che simuli REST e WS con dati controllati. Avviare il worker in un test asincrono, attendere che una candela sia in Redis, verificare contenuto e ordine. Aggiungere test per gap filling e riconnessione. Alternativa: usare la demo API di Bitget con un simbolo reale (più lento ma più realistico).

**Quando:**  
Dopo A5, quando il flusso manuale funziona. 6-8 ore.

---

### A12. ADR e README

**Motivazione e scopo:**  
Le decisioni architetturali prese finora (sliding window, composizione vs ereditarietà, rate limiting nel client) vanno documentate **ora**, non tra sei mesi. Il README è la porta d'ingresso del progetto: senza, chi arriva non capisce cosa fa Pipster né come avviarlo.

**Come implementarla:**  
Scrivere `README.md` con la struttura proposta (panoramica, stato componenti, requisiti, installazione, avvio, configurazione, documentazione). Scrivere ADR-0002 (sliding window) e ADR-0003 (composizione vs ereditarietà). Creare `docs/roadmap.md` con le fasi A-D. Non aprire un sito Sphinx: solo markdown versionato.

**Quando:**  
Subito per README e ADR già maturi; il resto si aggiorna a fine Fase A. 2-3 ore.

---

### A13. Atomicità dei fallback locali e dei manager

**Motivazione e scopo:**
RateLimitManager (sliding window) e TokenBucketManager (token bucket, dopo la riscrittura atomica) sono atomici tra bucket in modalità normale: consumano su tutti o su nessuno. Ma i rispettivi fallback locali — _LocalManagerFallback e _LocalTokenBucketManagerFallback — acquisiscono in sequenza o in parallelo senza dry-run/commit. In caso di fallimento parziale (bucket A ha capacità, bucket B no), A viene consumato senza che B lo sia. Il comportamento in degraded mode è quindi semanticamente diverso da quello normale: non "una versione ridotta", ma un contratto diverso. ADR-0004 accettava questo come compromesso pragmatico, ma dopo la riscrittura atomica del primary il gap va chiuso per coerenza. Inoltre, RateLimitManager.acquire non ha il check requested > limit, che causerebbe un loop infinito silenzioso in caso di richiesta eccessiva.

**Come implementarla:**
(1) Validazione argomenti in testa a RateLimitManager.acquire e TokenBucketManager.acquire: KeyError per bucket non registrato, ValueError per requested > limit/capacity, con costruzione liste in singolo passaggio. (2) Riscrivere _LocalManagerFallback.acquire con pattern dry-run/commit: primo loop per verificare la disponibilità di tutti i bucket locali, secondo per consumare; in caso di insufficienza, calcolo del max_wait e retry. (3) Stessa riscrittura per _LocalTokenBucketManagerFallback.acquire. (4) Aggiungere metodi di introspezione a LocalSlidingWindow e LocalTokenBucket per supportare il dry-run senza consumo (es. available_slots() e wait_if_insufficient(n)). (5) ADR-0006 "Atomicità dei fallback rate limiter", con aggiornamento dello stato di ADR-0004 e ADR-0005 a Integrato da ADR-0006. (6) Test aggiornati in tests/unit/test_rate_limiter.py.

**Quando:**
Dopo A3 e A4, quando il worker userà davvero le factory e i bucket saranno noti. Non blocca A5. 4-6 ore.

---

### A14. `HttpStatusError` nel transport

**Motivazione e scopo:** il body delle risposte 4xx/5xx va perso oggi perché `ClientResponseError` di aiohttp non lo porta. Un'eccezione custom
`HttpStatusError(status, body, url)` rende il body accessibile al chiamante, semplificando la diagnostica degli errori business (40006, 40008, 40009).

**Come:** introdurre `HttpStatusError` come base, far ereditare
`RateLimitError` e `NonRetriableHttpError`. Sostituire `raise_for_status()`
con un check manuale che solleva `HttpStatusError` per 5xx (retriable) e per 4xx non gestiti. Aggiornare `_compute_delay` per leggere `.delay` da qualsiasi eccezione che lo esponga, invece di `isinstance(RetryableWithDelay)`. Test.

**Quando:** dopo A13, prima di A3. 2-3 ore.

---

## FASE B — MVP in produzione

---

### B1. Deploy su ambiente demo Bitget

**Motivazione e scopo:**  
Il codice funziona in locale, ma la produzione ha condizioni diverse: latenza di rete, DNS, firewall, limiti reali dell'exchange. Il deploy su demo Bitget verifica che il bot funzioni in un ambiente realistico, con credenziali demo e dati di mercato reali, prima di rischiare capitale reale.

**Come implementarla:**  
Configurare `.env` con credenziali demo Bitget (`paptrading=1`), docker-compose con profilo `ingest`, volumi per la persistenza. Avviare con `docker compose --profile ingest up -d`. Verificare i log e le chiavi Redis dopo 1 ora di esecuzione.

**Quando:**  
Dopo A5 e A6, quando il flusso end-to-end è stabile. 4-6 ore.

---

### B2. Test di 7 giorni su BTCUSDT spot

**Motivazione e scopo:**  
Il vero test di un ingestion engine è la stabilità nel tempo. In 7 giorni si verificano: riconnessioni multiple, gap filling reale, accumulo di memoria, gestione del cambio di giornata, weekend con volatilità ridotta. Un test breve non rivela questi problemi.

**Come implementarla:**  
Lasciare il worker in esecuzione per 7 giorni consecutivi con monitoraggio via `docker logs` e `redis-cli`. Ogni giorno verificare: numero di candele, gap residui, errori nei log, uso memoria del container. Annotare ogni anomalia in un file `docs/reports/week-01.md`.

**Quando:**  
Dopo B1, quando il deploy è stabile. 2-3 ore di setup + 168h di osservazione.

---

### B3. Correzione bug emersi

**Motivazione e scopo:**  
I bug che emergono in produzione sono quelli che nessun test unitario ha catturato: timeout inattesi, risposte API con formato diverso, comportamenti di riconnessione non previsti. Vanno corretti **prima** di procedere alla scalabilità, altrimenti si amplificano con l'aumento dei carichi.

**Come implementarla:**  
Per ogni bug: riprodurre in test, correggere, verificare che il test passi, rieseguire i test di integrazione. Aggiornare la documentazione se il bug ha rivelato un'assunzione sbagliata. Committare ogni fix con un messaggio chiaro.

**Quando:**  
Durante e dopo B2, in modo iterativo. 8-16 ore.

---

## FASE C — Scalabilità e monitoraggio

---

### C1. Backpressure con coda bounded nel worker

**Motivazione e scopo:**  
Se Redis rallenta e il read loop del WS continua a consumare dal socket, la memoria cresce senza limite → OOM kill del container. La soluzione è una coda bounded tra read loop e processing: quando è piena, il read loop si blocca, la pressione risale al TCP, e il server smette di inviare. Questo è il principio della backpressure, ed è essenziale per la stabilità a lungo termine.

**Come implementarla:**  
`asyncio.Queue(maxsize=2000)` tra callback WS e consumer. La callback fa `put_nowait` e, in caso di `QueueFull`, `await put()`. Un task consumer legge dalla coda ed esegue `_process_raw_message`. Esporre `qsize` come metrica (vedi C4). Documentare la decisione in ADR-0004.

**Quando:**  
Dopo l'MVP stabile, prima di scalare a più simboli. 4-6 ore.

---

### C2. Multi-simbolo sullo stesso worker

**Motivazione e scopo:**  
Un worker per simbolo è semplice ma inefficiente: moltiplica connessioni WS, uso di memoria, complessità operativa. Un worker che gestisce N simboli riduce il numero di container, migliora l'uso delle risorse, e semplifica la scalabilità orizzontale. È il passo più importante dopo l'MVP.

**Come implementarla:**  
Il worker accetta una lista di simboli. La connessione WS sottoscrive più canali `candle1m` (uno per simbolo) nella stessa connessione. Il routing in `_process_raw_message` usa il simbolo dal messaggio per instradare alla coda corretta. Il warm-up diventa un loop sui simboli. Attenzione al limite di 1000 canali per connessione.

**Quando:**  
Dopo C1, con backpressure già in atto per gestire il carico maggiore. 8-12 ore.

---

### C3. Consumer group Redis

**Motivazione e scopo:**  
Attualmente il downstream legge Redis Streams con `XREAD`, che non scala a più consumer. I consumer group (`XREADGROUP`) permettono a N consumer di leggere dallo stesso stream senza duplicati, con acknowledgement e retry per messaggi non processati. È la base per disaccoppiare ingestion e strategy.

**Come implementarla:**  
Creare il consumer group con `XGROUP CREATE stream:market:... $ MKSTREAM` all'avvio. Il downstream usa `XREADGROUP GROUP nome consumer` con `XAUTOCLAIM` per recuperare messaggi non confermati. Implementare un meccanismo di ack esplicito dopo il processing. Documentare la semantica in `docs/guides/ingestion.md`.

**Quando:**  
Dopo C2, quando i volumi crescono. 6-8 ore.

---

### C4. Metriche Prometheus

**Motivazione e scopo:**  
Senza metriche, il monitoraggio è cieco. Prometheus è lo standard de facto per metriche in ambiente container. Esponendo contatori, gauge e istogrammi, si ottiene visibilità su throughput, latenza, profondità coda, gap, riconnessioni. È il prerequisito per Grafana e per l'alerting.

**Come implementarla:**  
Usare `prometheus_client`: `Counter` per messaggi processati/errori/gap, `Gauge` per `qsize` e `remaining` dei bucket, `Histogram` per latenza di `push_to_redis`. Esporre su endpoint HTTP `/metrics` con `start_http_server`. Configurare Prometheus per lo scrape.

**Quando:**  
Dopo C2, quando i volumi rendono il monitoring utile. 6-8 ore.

---

### C5. Grafana dashboard

**Motivazione e scopo:**  
Le metriche Prometheus sono utili solo se visualizzate. Grafana trasforma numeri in grafici: throughput, latenza p99, profondità coda, riconnessioni, gap. Una dashboard ben fatta permette di capire in 5 secondi se il sistema è sano o sta degradando.

**Come implementarla:**  
Aggiungere Grafana e Prometheus al docker-compose. Creare una dashboard JSON versionata in `monitoring/grafana/dashboards/`. Pannelli principali: throughput per simbolo, latenza p50/p99, `qsize`, errori, riconnessioni, gap rilevati.

**Quando:**  
Dopo C4, quando le metriche sono esposte. 4-6 ore.

---

### C6. Alerting

**Motivazione e scopo:**  
Un sistema monitorato senza alerting richiede presenza umana continua. L'alerting automatico notifica quando qualcosa va storto: error rate alto, latenza anomala, nessun messaggio per N minuti, gap non colmati. È la differenza tra "speriamo che vada bene" e "sappiamo subito se non va bene".

**Come implementarla:**  
Alertmanager con Prometheus rules. Alert tipici: `error_rate_5m > 1%`, `qsize > 80% per 5 minuti`, `no_candles_received_2m`, `reconnect_count_1h > 10`. Notifiche via email, Slack, o Telegram. Configurare soglie con isteresi per evitare flap.

**Quando:**  
Dopo C5, quando la dashboard è operativa. 4-6 ore.

---

### C7. Test di carico

**Motivazione e scopo:**  
Prima di considerare l'ingestion scalabile, va verificato sotto carico. Con 20-50 simboli simultanei, il sistema regge? La coda si riempie? Il rate limiter regge? Il test di carico risponde a queste domande prima che sia la produzione a rispondere per noi.

**Come implementarla:**  
Usare `locust` o script custom che simula molti simboli con dati realistici. Misurare throughput, latenza, uso CPU/memoria, profondità coda. Confrontare i risultati con i limiti dell'exchange. Se il sistema satura, identificare il collo di bottiglia.

**Quando:**  
Dopo C2 e C4, con metriche disponibili. 6-8 ore.

---

### C8. Circuit breaker sul rate limiter (degraded mode esplicito)

**Motivazione e scopo:**
ADR-0004 introduce il fallback locale su Redis down, ma con un solo meccanismo: cooldown di 5 secondi. Se Redis resta giù per minuti o ore, ogni worker ritenta Redis ogni 5 secondi, sprecando tentativi, loggando errori, e mantenendo un throughput ridotto senza una politica esplicita di recovery. Il circuit breaker rende lo stato esplicito (CLOSED/OPEN/HALF_OPEN), evita di tempestare Redis se è a lungo irraggiungibile, e fornisce un punto di osservabilità per metriche e alert. È la differenza tra "speriamo che Redis torni" e "sappiamo quando Redis è giù, quanto tempo ci mette a tornare, e il sistema si comporta in modo prevedibile nel frattempo".

**Come implementarla:**
Wrapper attorno a Redis nel RateLimitManager con tre stati. In CLOSED, comportamento normale. Dopo N errori consecutivi (RATE_LIMIT_CB_FAILURE_THRESHOLD, es. 3) → OPEN, e per RATE_LIMIT_CB_OPEN_SEC (es. 30s) nessun tentativo a Redis: il manager usa direttamente il fallback locale. Dopo il timeout → HALF_OPEN: un singolo tentativo a Redis; se risponde, torna CLOSED; se fallisce, torna OPEN per un altro ciclo (eventualmente con backoff). Esporre lo stato come Gauge (rate_limiter_circuit_state{state="open|closed|half_open"}) e i contatori di transizione come Counter (rate_limiter_circuit_transitions_total). Configurare alert su state="open" persistente > 1 minuto. Documentare in ADR-0006.

**Quando:**
Dopo C4 (metriche) e C6 (alerting), perché senza osservabilità il circuit breaker è invisibile. Prima di C7 (test di carico), così il test di carico copre anche il comportamento in degraded mode. 6-8 ore.

---

## FASE D — Cold layer e produzione avanzata

---

### D1. Cold layer (Parquet su S3/MinIO)

**Motivazione e scopo:**  
Redis Streams è memoria volatile: se il container muore, si perde tutto. Il cold layer è lo storage a lungo termine, in formato colonnare (Parquet) su object storage (S3/MinIO). Serve per backtesting, analisi storiche, recovery da disastri, e per non dipendere da Redis per dati di mesi o anni.

**Come implementarla:**  
Job periodico (es. ogni ora) che legge da Redis Streams con `XREAD` in batch, serializza in Parquet con `pyarrow`, e carica su S3/MinIO con `boto3` o `aioboto3`. Partizionamento per `symbol/timeframe/date`. Metadata in un catalogo (file JSON o database SQLite).

**Quando:**  
Dopo che l'MVP è stabile e i dati in Redis iniziano ad accumularsi. 12-16 ore.

---

### D2. CI/CD

**Motivazione e scopo:**  
Senza CI/CD, ogni modifica è un rischio: si testa in locale, si spera che vada bene, si deploya manualmente. CI/CD automatizza linting, type checking, test, build dell'immagine, e deploy. Riduce drasticamente gli errori umani e permette iterazioni più rapide.

**Come implementarla:**  
GitHub Actions (o GitLab CI) con job: `lint` (ruff), `typecheck` (mypy), `test` (pytest), `build` (docker buildx), `push` (registry). Gate: nessun merge se i test falliscono. Deploy automatico su ambiente staging dopo merge su main.

**Quando:**  
Parallelamente alla Fase A, non dopo. 6-8 ore.

---

### D3. Secret management

**Motivazione e scopo:**  
Le credenziali API in `.env` sono ok in sviluppo ma inaccettabili in produzione: finiscono in Git, nei log, nei backup. Un secret manager centralizza le credenziali, le ruota, e le distribuisce in modo sicuro ai container. È un requisito di sicurezza, non un'opzione.

**Come implementarla:**  
Minimo: Docker secrets (`docker secret create`), montati come file in `/run/secrets/`. Intermedio: HashiCorp Vault, con iniezione a runtime via agent. Massimo: AWS Secrets Manager o GCP Secret Manager, con IAM roles. Per il livello attuale, Docker secrets è sufficiente.

**Quando:**  
Prima del deploy su ambiente reale, non demo. 4-6 ore.

---

### D4. Dead letter stream per candele corrotte

**Motivazione e scopo:**  
Se una candela arriva malformata dal WS (JSON invalido, campi mancanti, valori non numerici), oggi viene scartata silenziosamente. In produzione si vuole invece conservarla in una dead-letter stream per diagnosi: quante? da quale simbolo? che pattern? Serve per capire se è un bug del nostro parsing o un'anomalia dell'exchange.

**Come implementarla:**  
Uno stream Redis separato `stream:dlq:{exchange}`. In `_process_raw_message`, se il parsing fallisce, fare `XADD` con il raw e un campo `error_reason`. Un consumer dedicato (o un job) analizza periodicamente la DLQ e produce report.

**Quando:**  
Dopo C3, quando i consumer group sono in atto. 4-6 ore.

---

### D5. Backup Redis

**Motivazione e scopo:**  
Redis Streams in RAM: se il container muore senza persistenza, si perdono i dati non ancora copiati nel cold layer. Il backup (AOF o RDB) garantisce che tra il crash e il riavvio non ci siano perdite. È una decisione architetturale, non solo operativa: quanta perdita è accettabile?

**Come implementarla:**  
Configurare Redis con `appendonly yes` (AOF) per durabilità a ogni scrittura, o `save 60 1` (RDB) per snapshot periodici. Con AOF: latenza di scrittura leggermente superiore, ma nessuna perdita. Con RDB: prestazioni migliori, ma si perde fino all'ultimo snapshot. Con volumi Docker montati su disco persistente.

**Quando:**  
Prima del deploy in produzione reale, con dati che contano. 2-3 ore.

---

### D6. Contract testing con Bitget demo

**Motivazione e scopo:**  
Tutti i test attuali usano mock. I mock sono utili ma non garantiscono che il formato reale di Bitget sia quello atteso: un campo rinominato, un tipo cambiato, un codice di errore nuovo non vengono rilevati. Il contract testing verifica contro l'API reale (demo) che il contratto sia rispettato.

**Come implementarla:**  
Test che chiamano la demo API Bitget con un simbolo reale e verificano la struttura della risposta: presenza dei campi attesi, tipi corretti, formato coerente con il parser. Eseguire in CI periodicamente (es. una volta al giorno) perché Bitget può cambiare l'API.

**Quando:**  
Prima del deploy in Fase B, per validare l'integrazione reale. 4-6 ore.

---

### D7. Documentazione sito (Sphinx/MkDocs)

**Motivazione e scopo:**  
I file markdown in `docs/` sono ottimi per sviluppatori, ma non per chi vuole consultare il riferimento API o navigare la documentazione in modo strutturato. Un sito (Sphinx o MkDocs) genera HTML navigabile a partire da docstring e markdown, ed è il formato standard per progetti open source e professionali.

**Come implementarla:**  
MkDocs con `mkdocstrings` per Python è più semplice di Sphinx. Configurare `mkdocs.yml` con la struttura di `docs/`. Pubblicare su GitHub Pages con un workflow CI. Aggiornare automaticamente ad ogni push su main.

**Quando:**  
Dopo che il progetto è stabile, non durante lo sviluppo attivo. 8-12 ore.

---

## Attività opzionali

---

### O1. Multi-exchange (Bybit, Binance)

**Motivazione e scopo:**  
Diversificare su più exchange riduce il rischio di controparte e apre opportunità di arbitraggio. L'architettura core/exchange è già predisposta: ogni exchange è un adapter che implementa `BaseIngestionWorker` e usa i client asincroni generici. Non è una priorità finché il bot non è stabile su Bitget.

**Come implementarla:**  
Per ogni exchange: `XyzRestClient`, `XyzWsClient`, `XyzIngestionWorker`. Riuso di `AsyncHttpClient`, `AsyncWsClient`, `RateLimitManager`. Attenzione alle differenze di formato dati, timeframes, simboli. Ogni exchange richiede contract testing e test di integrazione dedicati.

**Quando:**
Dopo che il multi-simbolo su Bitget è stabile in produzione. 20-40 ore per exchange.

---

### O2. SBE (Simple Binary Encoding)

**Motivazione e scopo:**
SBE è un protocollo binario che riduce la latenza e la dimensione dei messaggi WS. Bitget lo supporta su endpoint dedicati. È utile solo in scenari HFT dove ogni microsecondo conta, e non è una priorità per un bot su candele 1m-1h.

**Come implementarla:**  
Richiede un decoder SBE (schema XML + generazione codice), integrazione con `AsyncWsClient`, e test dedicati. Il formato binario è completamente diverso dal JSON: il parser va riscritto. Da valutare solo se la latenza diventa un vincolo reale.

**Quando:**  
Solo se si passa a strategie HFT con esigenze di latenza sub-millisecondo. 16-24 ore.

---

### O3. Dynamic throttling adattivo

**Motivazione e scopo:**  
Rallentare proattivamente quando il budget di rate limit si avvicina allo zero riduce il rischio di hit del limite hard. È un concetto di load shedding applicativo, non di rate limiting. Se implementato nel rate limiter introduce cicli di retroazione pericolosi; va implementato nel chiamante (il worker) come risposta a `remaining`.

**Come implementarla:**  
Il `RateLimitManager.acquire` già ritorna `remaining`. Il worker può leggere questo valore e, se sotto una soglia configurabile, rallentare il warm-up o prioritizzare alcune richieste. Nessuna modifica al rate limiter: solo logica applicativa nel worker.

**Quando:**  
Solo con carichi elevati e contesa reale sui bucket. 8-12 ore.

---

### O4. Unsubscribe

**Motivazione e scopo:**  
`unsubscribe` permette di rimuovere sottoscrizioni attive senza chiudere la connessione. È utile per rotazione dinamica dei simboli: una strategia che cambia i mercati monitorati deve poter disiscriversi da canali non più utili. Senza, si accumulano fino al limite di 1000 canali.

**Come implementarla:**  
Aggiungere `unsubscribe(reqs)` in `BitgetWsClient` con `{"op":"unsubscribe","args":[...]}`. Rimuovere le chiavi da `self.subscriptions` dopo l'invio. Gestire il throttle (conta come messaggio in uscita). Documentare in ADR se la semantica di routing cambia.

**Quando:**  
Quando si introduce rotazione dinamica dei simboli (fase avanzata di C2). 4-6 ore.

---

### O5. Canali liquidazione futures

**Motivazione e scopo:**  
Per strategie su futures, i canali di liquidazione forniscono segnali preziosi: grandi liquidazioni precedono spesso movimenti di prezzo. Non servono per spot né per strategie su candele semplici. Va considerato solo se il bot farà trading su futures.

**Come implementarla:**  
Sottoscrizione al canale `liquidation` di Bitget V3 con `SubscribeReq` specifico. Il formato dei messaggi è diverso da candele: parser dedicato. Il worker deve instradare questi messaggi a uno stream Redis separato (`stream:liquidation:...`).

**Quando:**  
Solo quando il bot farà trading su futures e le strategie useranno il segnale. 6-8 ore.

---

### O6. Order book WS

**Motivazione e scopo:**  
L'order book fornisce profondità di mercato in tempo reale, indispensabile per strategie market-making, arbitraggio, e analisi di liquidità. È ad alta frequenza: decine o centinaia di messaggi al secondo. Richiede gestione diversa da candele.

**Come implementarla:**  
Canale `books` o `books5` di Bitget V3. Snapshot iniziale via REST, poi aggiornamenti incrementali via WS con gestione del sequence number. Storage in Redis come hash o ZSET. Alta frequenza richiede backpressure specifica e possibile storage su time-series DB.

**Quando:**  
Solo se le strategie lo richiedono. 12-16 ore.

---

### O7. Redis Cluster

**Motivazione e scopo:**  
Redis Cluster distribuisce i dati su più nodi per scalare orizzontalmente oltre i limiti di una singola istanza. Utile con molti worker e grandi volumi. Richiede hash tag `{exchange}` già presenti nel nostro design, ma anche gestione di failover e riconnessione.

**Come implementarla:**  
Configurare Redis Cluster con almeno 3 master e 3 replica. Verificare che tutte le chiavi usino hash tag coerenti. Aggiornare `redis.asyncio` per la modalità cluster. Testare failover e comportamento del rate limiter durante partizioni.

**Quando:**  
Solo quando il volume di dati e il numero di worker superano la capacità di un singolo Redis. 8-12 ore.

---

### D+. Orchestrazione multi-worker su Kubernetes (opzionale)

**Motivazione e scopo:**
L'MVP gira su Docker Compose con un singolo worker o pochi worker statici. Se Pipster crescerà a più worker concorrenti, la gestione manuale di RATE_LIMIT_EXPECTED_WORKERS diventa fragile: ogni scaling richiede un aggiornamento coordinato di config e redeploy. Kubernetes risolve il problema con service discovery nativa (Lease objects), scaling automatico (HPA), identità stabile (StatefulSet), e graceful shutdown coordinato (terminationGracePeriodSeconds). Ma introduce complessità operativa — cluster, ingress, secret, monitoring dedicato — che va giustificata dal carico reale. Non è una tappa obbligata: è una possibilità da valutare quando il collo di bottiglia diventa operativo, non tecnico.

**Come implementarla:**
Migrare i worker da Deployment a StatefulSet con podManagementPolicy: Parallel. Ogni pod pubblica un lease K8s (coordination.k8s.io/v1) con TTL breve (es. 15s), rinnovato ogni 5s. Il rate limiter conta i lease attivi per determinare expected_workers dinamicamente; RATE_LIMIT_EXPECTED_WORKERS resta come fallback per il primo avvio. terminationGracePeriodSeconds allineato con il graceful shutdown di A6, con preStop hook che aspetta il drenaggio della coda bounded (C1). HPA su metrica custom (es. lag del consumer group Redis). Config via ConfigMap/Secret; logging verso Loki; metriche verso Prometheus Operator; alert via Alertmanager.

**Quando:**
Dopo che multi-simbolo (C2) e carico reale (C7) saranno chiusi, e solo se il carico o l'operatività lo giustificheranno. Non prima. 40-60 ore.