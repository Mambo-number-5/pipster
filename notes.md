# 📓 Note di Sviluppo - Pipster Bot

Questo file serve a conservare la logica dietro le scelte tecniche e i comandi "salvavita" da ricordare per il futuro.

## 🔗 Deep Linking al Codice
Per collegare una nota a una riga specifica del codice in VS Code, usa questa sintassi:
*   `[Test Connessione Redis](../tests/test_redis.py#L10)` -> Premendo `Ctrl + Click` verrai portato alla riga 10 del file.
*   *Nota:* Se aggiungi righe di codice, il numero del link potrebbe sballare, ma punta comunque al file corretto.

---

## 🐋 Gestione Docker & Redis

### Mandare comandi ai container
*   **Eseguire un comando in un container attivo:** `docker exec -it <nome_container> <comando>`
*   **Vedere i log in tempo reale:** `docker compose logs -f bot`

### Entrare in Redis & Comandi Base
Per ispezionare il database manualmente senza dashboard grafica:
1. **Entra nel CLI:** `docker exec -it pipster_redis redis-cli`
2. **Comandi utili:**
    * `PING` -> Risposta `PONG` (test connessione).
    * `KEYS *` -> Elenca tutte le chiavi (usa con cautela).
    * `JSON.GET <chiave>` -> Legge il contenuto di un oggetto JSON.
    * `MONITOR` -> Debug in tempo reale: mostra ogni comando ricevuto dal server.
    * `FLUSHALL` -> **ATTENZIONE:** Cancella l'intero database.

### </u> Ottimizzazione Memoria (Molto importante per Linux) </u>
Se Redis gira su un host Linux, per evitare problemi di latenza nel salvataggio dei dati (snapshot), aggiungi questa riga al sistema ospite (Debian):
```bash
echo 'vm.overcommit_memory = 1' | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

---

## 📂 Archivio Logiche Infrastruttura

### 🛠 Docker Compose (Logica Volumi e Log)
*   **Historical Data (.parquet):** Anche se montiamo la cartella root `.:/app`, è bene tenere traccia esplicita dei dati pesanti. Se in futuro vorrai isolare i dati storici su un disco diverso, ti basterà cambiare il mapping nel volume: `- ./historical_data:/app/historical_data`.
*   **PYTHONUNBUFFERED=1:** Fondamentale per vedere i `print()` o i log del bot immediatamente nel terminale Docker senza ritardi di buffering.
*   **Healthcheck:** Redis deve rispondere al `ping` prima che il bot provi a connettersi, altrimenti il bot crasha subito (Connection Refused).

### 🏗 Dockerfile (Ottimizzazione Layer)
*   **Layer Caching:** L'installazione di `apt-get` è in cima perché è la parte più pesante (450MB) e stabile. Non deve essere rifatta se cambi solo il codice Python.
*   **ENV PIP_ROOT_USER_ACTION=ignore:** Silenzia i warning di sicurezza poiché nel container giriamo volutamente come root.
*   **--mount=type=cache:** Accelera drasticamente le build successive. Se aggiungi una sola libreria al `requirements.txt`, scarica solo quella invece di riscaricare tutti i 200MB di pacchetti.

### 🐛 Debugging (Override Configuration)
Il file `docker-compose.override.yml` è il tuo "laboratorio".
*   **-Xfrozen_modules=off:** Necessario nelle versioni recenti di Python (3.11+) per permettere al debugger di VS Code di "entrare" correttamente nei moduli interni durante il debug.
*   **--wait-for-client:** Blocca l'esecuzione del bot alla prima riga. Il bot non parte finché tu non premi **F5** in VS Code.
*   **esempio di uso**: python -Xfrozen_modules=off -m debugpy --listen 0.0.0.0:5678 --wait-for-client file_generico.py

### 🕹️ Cheat Sheet: Comandi di Debug (Override)
Da copiare nel file `docker-compose.override.yml` in base a cosa vuoi testare. Ricorda che `--wait-for-client` blocca tutto finché non premi **F5**.

*   **Avvio standard del Bot:**
    `command: python -Xfrozen_modules=off -m debugpy --listen 0.0.0.0:5678 --wait-for-client main.py`

*   **Eseguire un intero file di test (Modulo unittest):**
    `command: python -Xfrozen_modules=off -m debugpy --listen 0.0.0.0:5678 --wait-for-client -m unittest tests/test_redis.py`

*   **Eseguire un singolo metodo specifico (Precisione chirurgica):**
    `command: python -Xfrozen_modules=off -m debugpy --listen 0.0.0.0:5678 --wait-for-client -m unittest tests.test_redis.NomeClasse.nome_metodo`

*   **Eseguire un modulo test come script (usa `if __name__ == "__main__":`):**
    `command: python -Xfrozen_modules=off -m debugpy --listen 0.0.0.0:5678 --wait-for-client tests/test_redis.py`

**Perché queste varianti?**
1.  **`-m unittest`**: È utile perché non ti obbliga a inserire il blocco `main` in ogni file di test; Python cercherà automaticamente tutte le funzioni che iniziano con `test_`.
2.  **Percorso con i punti (`tests.test_redis...`)**: Necessario quando usi il flag `-m`, perché Python tratta il file come un modulo del pacchetto.
3.  **Percorso con lo slash (`tests/test_redis.py`)**: Si usa quando lanci il file direttamente come script.

---

# 09/05/2026 🛠 Architettura e Logica del Bot

## 🔄 Gestione Flusso Consumer Groups (Streams)
Il bot utilizza i Redis Streams con Consumer Groups per garantire che nessun segnale di trading venga perso (**At-Least-Once Delivery**).

### 1. Il Ciclo di Vita del Segnale
La logica di consumo è implementata nel `professional_worker_loop` (Logica di Business) e si appoggia al [RedisClient](redis_client.py) (Data Layer).

*   **Recupero (Recovery):** All'avvio, il bot interroga la **PEL (Pending Entries List)** usando l'ID `0`. Questo permette di riprendere messaggi letti ma mai confermati (es. a causa di un crash).
*   **Identificazione Poison Messages:** Tramite `get_pending_messages`, monitoriamo il contatore `times_delivered`. 
*   **Dead Letter Queue (DLQ):** Se un messaggio supera la soglia di `MAX_RETRIES` (default 3), viene considerato "velenoso" (causa crash). Viene spostato in `stream:errors` e rimosso dalla coda principale tramite `XACK`.

### 2. Funzioni Chiave

#### `professional_worker_loop(db, stream, group, consumer)`
*   **Scopo:** Gestire il loop infinito di ascolto segnali in modo resiliente.
*   **Logica Interna:**
    1. Verifica messaggi bloccati nella PEL.
    2. Sposta eventuali messaggi falliti nella DLQ.
    3. Ascolta nuovi messaggi con `last_id='>'`.
    4. Esegue la logica di trading e invia `XACK` solo a operazione conclusa.

#### `get_pending_messages(stream, group, count)`
Interroga `XPENDING` per ottenere i metadati dei messaggi non confermati. Restituisce una lista di dict con `message_id` e `times_delivered`.

## ⚠️ Note Tecniche e "Gotchas"
*   **Recupero dati per ID:** Non usare `XREADGROUP` per ottenere un singolo messaggio di cui si conosce l'ID. Usare invece `XRANGE stream_name id id`.
*   **PEL Activation:** Un messaggio entra nella PEL solo dopo la **prima** lettura con ID `>`.
*   **FlushDB:** Nei test, usare sempre `flushdb()` per isolare le suite, ma assicurarsi di essere sul DB corretto (es. 15) per non piallare la produzione.

ID Usato,Cosa legge?,Stato del Messaggio
>,Solo nuovi messaggi,Mai consegnati a nessuno nel gruppo
0,Solo messaggi pendenti,Consegnati a questo consumer ma non confermati
XRANGE,Messaggio specifico,"Usato per ""estrarre"" i dati senza muovere i cursori"

---

# 18/05/2026 Note di Sviluppo - Pipster Bot

## 📋 Architettura e Modifiche al Repository

### 1. Gestione Permessi Docker (Linux Compatibility)
* **Problema:** L'uso della direttiva `user: "${UID}:${GID}"` direttamente nel file `docker-compose.yml` bloccava l'avvio del container su macchine Windows (dove le variabili non esistono) e introduceva ridondanze su macOS. Al contempo, omettere la direttiva su Linux Ubuntu causava il blocco dei permessi di scrittura dei file generati dal container (proprietà di `root`).
* **Soluzione:** Architettura a file di configurazione separati.
  * `docker-compose.yml`: Mantenuto pulito e universale (agnostico rispetto al sistema operativo).
  * `docker-compose.override.yml` (in `.gitignore`): Inietta la direttiva `user: "${UID}:${GID}"` solo localmente sulla macchina Linux di sviluppo.
  * `.env` (in `.gitignore`): File locale contenente l'esplicitazione dei parametri `UID=1000` e `GID=1000` per mappare l'utente nativo del sistema operativo dentro il container.
  * `.devcontainer/devcontainer.json`: Configurato con `"remoteUser": "root"` per consentire al debugger di VS Code di superare i vincoli di permessi e installare correttamente i componenti interni di debug.

### 2. Gestione dei Processi in Multiprocessing: `.terminate()` vs `.join()`
Nello script di stress test e chaos engineering, la gestione del ciclo di vita dei nodi worker paralleli richiede l'uso combinato di due metodi fondamentali della libreria `multiprocessing`:
* **`process.terminate()` (Asincrono / Il Mandato di Sfratto):** Invia un segnale `SIGTERM` al processo figlio a livello di Sistema Operativo. Dice al kernel di interrompere immediatamente l'esecuzione del processo. Essendo un'operazione asincrona, il codice principale lancia il comando e passa subito alla riga successiva, senza attendere l'effettiva transizione di stato del figlio.
* **`process.join()` (Sincrono / La Pulizia del Kernel):** Blocca l'esecuzione dello script principale finché il processo figlio non ha terminato tutte le sue procedure di chiusura, rilasciato la memoria RAM e chiuso i descrittori di file. 
* **Perché usarli insieme:** Se si usasse solo `.terminate()`, il processo genitore potrebbe chiudersi troppo in fretta o ignorare l'uscita del figlio, lasciando quest'ultimo nello stato di **processo Zombie** (un processo morto che occupa ancora uno slot nella tabella dei processi del kernel Linux). Il `.join()` garantisce il "reaping" (la raccolta del codice di uscita), ripulendo il sistema operativo.

### 3. Debugging Avanzato Multiprocesso in VS Code
Di default, il debugger di Python si aggancia esclusivamente al processo "Padre" che avvia l'ambiente. I processi figli generati da `multiprocessing` nascono in un angolo cieco e le loro eccezioni interne (come i `TypeError` sui dati dello stream) vengono inghiottite dal silenzio del background.

* **Soluzione:** È necessario istruire esplicitamente l'estensione di debug modificando o creando il file `.vscode/launch.json` nella cartella principale del progetto, attivando il tracciamento dei sottoprocessi:
```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Python: Debug Stress Test",
            "type": "python",
            "request": "launch",
            "program": "${file}",
            "console": "integratedTerminal",
            "justMyCode": true,
            "subProcess": true // 🚀 Consente al debugger di agganciarsi in automatico ai worker figli
        }
    ]
}
```
    Tecnica di debug: Una volta attivato "subProcess": true, è possibile monitorare i nodi dal pannello Call Stack di VS Code (selezionando il processo figlio attivo) o impostando dei Breakpoint Condizionali (es. self.worker_name == "worker_node_0") per non far saltare continuamente il cursore del debugger tra i vari worker concorrenti.

4. Flusso di Elaborazione del TradingWorker

Il TradingWorker implementa un motore di consumo resiliente basato su Redis Consumer Groups, strutturato in un loop infinito a tre fasi:

    Recupero Orfani (XAUTOCLAIM): All'avvio e a intervalli regolari impostati dall'applicazione (es. ogni 30 secondi), interroga lo stream. Redis convalida la richiesta solo se i messaggi nella PEL di altri nodi hanno superato il min_idle_time (parametro di sicurezza del DB per evitare furti di trade tra worker attivi).

    Svuotamento PEL Locale (Ripristino Crash): Controlla la propria Pending Entries List per rielaborare i messaggi rimasti in sospeso a causa di un eventuale crash precedente del worker stesso.

    Ascolto in Tempo Reale: Esegue una lettura bloccante sullo stream per intercettare i nuovi segnali operativi inviati dall'Engine.

Isolamento Errori (DLQ): Se la logica di business fallisce (es. errori di tipo o validazione), il messaggio viene riprociato fino a un massimo di max_retries. Al superamento della soglia, il messaggio viene rimosso dallo stream principale e isolato in uno stream dedicato di errore ({stream}:errors), che funge da Dead Letter Queue.
5. Validazione tramite Stress Test e Chaos Engineering (test_stress_scalability.py)

L'architettura di streaming è stata validata con successo superando uno stress test multiprocesso:

    Carico: Iniezione simultanea di centinaia di messaggi nello stream Redis (sanificando i tipi dato da stringa a intero in fase di lettura).

    Scalabilità Orizzontale: Tre istanze parallele del worker hanno consumato i messaggi concorrentemente senza duplicazioni o conflitti di gara (race conditions).

    Chaos Test: Interruzione brutale tramite SIGKILL di un worker a metà elaborazione, lasciando messaggi orfani nella sua PEL.

    Esito: I restanti due worker superstiti, superata la finestra temporale combinata (Attesa loop + Lock di Idle di Redis), hanno recuperato i messaggi pendenti tramite la logica di autoclaim. Il report di ispezione finale ha confermato l'azzeramento delle PEL e il corretto isolamento dei soli messaggi volutamente difettosi nella Dead Letter Queue.

---
# 06/06/2026 📌 Punti fondamentali della programmazione asincrona (invariati)

1. **L’event loop è l’orchestratore centrale** – un singolo thread esegue le coroutine, passando da una all’altra quando incontrano `await`.  
2. **`async def`** definisce una coroutine che può essere sospesa e ripresa.  
3. **`await`** sospende la coroutine corrente finché l’oggetto attendibile (coroutine, Task, Future) non è completato. **Non blocca il thread** – cede il controllo all’event loop.
4. **`asyncio.gather()`** esegue più coroutine in concorrenza e attende che tutte finiscano.
5. **`async with`** gestisce contesti asincroni: crea l’oggetto in modo sincrono, poi chiama `__aenter__` (coroutine) e `__aexit__` (coroutine).
6. **`asyncio.sleep(1)`** cede il controllo per un tempo determinato, evitando tight loop (CPU al 100%).
7. **I Task** (`create_task()`) sono coroutine avvolte che girano in background gestite dall’event loop.  
8. **Il kernel (epoll, kqueue, IOCP)** sblocca le attese di I/O – l’event loop si addormenta e viene svegliato dal sistema operativo quando i dati sono pronti o un timer scade.
9. **Graceful shutdown**: catturare `CancelledError` e usare `try/finally` o `async with` per rilasciare le risorse.

---

## 🔁 Flusso dettagliato del `BaseIngestionWorker` aggiornato

### 1. Inizializzazione (`__init__`)
- Stessa versione: salva `symbol`, `timeframe`, calcola `redis_stream`, imposta `redis = None` e `running = True`.

### 2. Avvio (`asyncio.run(worker.run())`)
- L’event loop esegue la coroutine `run()`.

### 3. Dentro `run()`

#### 3a. Connessione a Redis
- `self.redis = redis.Redis(...)` – crea il client asincrono (la connessione fisica avviene alla prima operazione).

#### 3b. Warm‑up storico (`await self._warmup_storico()`)
**Novità**: ora interroga Redis per evitare duplicati e riempire il gap.
- Chiama `await self._get_last_redis_timestamp()`:
  - Usa `xrevrange` con `count=1` per ottenere l’ultima candela nello stream.
  - Restituisce il timestamp (intero) o `None` se lo stream è vuoto.
- Se `last_ts` esiste, chiama `await self.fetch_historic_klines(since_timestamp=last_ts, limit=200)` – scarica **solo le candele più recenti** a partire da quel timestamp (gap filling).
- Se `last_ts` è `None`, chiama `await self.fetch_historic_klines(limit=200)` – scarica le ultime 200 candele.
- Cicla sulle candele recuperate e **scarta quelle con timestamp ≤ last_ts** (evita duplicati).
- Per ogni candela nuova: `await self.push_to_redis(kline)`.
  - **Nota**: `push_to_redis` ora **non ha try/except** – se Redis fallisce, l’eccezione si propaga verso l’alto (terminando il warm‑up e poi il loop live).

#### 3c. Loop live del WebSocket
```python
while self.running:
    try:
        await self.listen_websocket_stream()
        await asyncio.sleep(1)
    except asyncio.CancelledError:
        self.running = False
        break
    except Exception as e:
        print(f"Connessione interrotta (WS o Redis): {e}. Riconnessione tra 5s...")
        await asyncio.sleep(5)
```
- **`await self.listen_websocket_stream()`** – metodo concreto che si connette al WebSocket e, per ogni candela chiusa, chiama `push_to_redis()`.
- Se il WebSocket cade o se `push_to_redis()` solleva un’eccezione (es. Redis offline), l’eccezione viene catturata, si attende 5 secondi e poi si riprova a chiamare `listen_websocket_stream()` (che ristabilirà anche la connessione a Redis? No – Redis è già connesso, ma se Redis è andato giù, la prossima `push_to_redis` fallirà di nuovo. In ogni caso il loop continua a tentare).
- `CancelledError` esce dal loop.

#### 3d. Cleanup (`finally`)
- `await self.redis.close()` – chiude la connessione Redis in modo pulito.

### 4. Shutdown
- Ctrl+C → `CancelledError` → worker esce dal loop, chiude Redis, termina l’event loop.

---

## 🔄 Differenze principali rispetto alla versione precedente (da ricordare)

| Aspetto | Versione originale | Versione aggiornata |
|---------|-------------------|---------------------|
| Warm‑up | Scarica sempre 200 candele, senza controlli | Legge l’ultimo timestamp da Redis e fa gap filling (evita buchi e duplicati) |
| Gestione errori Redis in `push_to_redis` | `try/except` interno – silenzia l’errore | **Nessun** `try/except` – l’errore si propaga e fa riavviare il loop WebSocket |
| `fetch_historic_klines` | Solo parametro `limit` | Ora ha anche `since_timestamp` (opzionale) per scaricare da un momento specifico |
| Resilienza | Errore Redis non interrompeva il worker | Qualsiasi errore Redis (o WebSocket) causa una riconnessione completa dopo 5s |

---

## 🧠 Schema riassuntivo aggiornato

```
asyncio.run(worker.run())
    │
    ├─> self.redis = redis.Redis(...)
    │
    ├─> await self._warmup_storico()
    │       ├─> last_ts = await self._get_last_redis_timestamp()
    │       ├─> if last_ts: historical = await self.fetch_historic_klines(since_timestamp=last_ts)
    │       │   else: historical = await self.fetch_historic_klines(limit=200)
    │       └─> per kline in historical: if kline["timestamp"] > last_ts: await self.push_to_redis(kline)
    │
    └─> while self.running:
            try:
                await self.listen_websocket_stream()   # contiene push_to_redis (senza try)
                await asyncio.sleep(1)
            except CancelledError:
                self.running = False
            except Exception as e:
                await asyncio.sleep(5)   # riconnessione
    │
    └─> await self.redis.close()
```

---

# 09/06/2026
Ecco la spiegazione dettagliata di come `BitgetIngestionWorker` eredita da `BaseIngestionWorker` e come funziona il flusso, con attenzione alle scelte asincrone.

---

## 1. Ereditarietà e implementazione dei metodi astratti

La classe `BitgetIngestionWorker` **eredita** da `BaseIngestionWorker` e quindi **deve obbligatoriamente implementare** i due metodi astratti:

- `fetch_historic_klines(self, since_timestamp, limit)` → per lo storico via REST.
- `listen_websocket_stream(self)` → per il flusso live via WebSocket.

Inoltre aggiunge:
- Attributi specifici Bitget: `inst_type` (SPOT o FUTURES), chiavi API, `market_client` (SDK REST), `ws_queue` (coda asincrona per il ponte tra thread), `loop` (riferimento all’event loop).
- Metodi di supporto: `_map_timeframe_to_bitget`, `_sync_fetch_klines`, `_ws_callback`, `_ws_error_callback`.

L’ereditarietà permette di **riutilizzare** tutto il ciclo di vita (connessione Redis, warm-up, loop principale con riconnessioni, graceful shutdown) senza riscriverlo. Il worker concreto si occupa solo delle interazioni con il broker specifico.

---

## 2. Flusso completo del `BitgetIngestionWorker` quando si chiama `asyncio.run(worker.run())`

### Fase 0 – Inizializzazione
Viene creata l’istanza:
```python
worker = BitgetIngestionWorker(symbol="BTCUSDT", timeframe="15m", inst_type="SPOT")
```
- Viene chiamato `__init__` della classe base, che imposta `symbol`, `timeframe`, `redis_stream`, ecc.
- Poi `__init__` della classe figlia imposta `inst_type`, carica le chiavi, crea `market_client` (client REST sincrono dell’SDK), crea `ws_queue` e `loop = None`.

### Fase 1 – Avvio di `run()` (ereditato dalla base)
`asyncio.run(worker.run())` lancia l’event loop ed esegue la coroutine `run()` della classe base.

**1.1 Connessione a Redis**  
`self.redis = redis.Redis(...)` – prepara il client asincrono.

**1.2 Warm-up storico (`_warmup_storico`)**  
La base chiama `await self._get_last_redis_timestamp()` per sapere l’ultima candela già salvata (se esiste).  
Poi chiama `await self.fetch_historic_klines(since_timestamp=last_ts)`.

Qui **la classe figlia** esegue la sua implementazione di `fetch_historic_klines`.

---

### Fase 2 – Implementazione di `fetch_historic_klines` (asincrona con `to_thread`)

```python
async def fetch_historic_klines(self, since_timestamp, limit):
    response = await asyncio.to_thread(self._sync_fetch_klines, since_timestamp, limit)
    # ... normalizza e restituisce
```

**Perché `asyncio.to_thread`?**  
Il metodo `_sync_fetch_klines` chiama `self.market_client.candles(...)`, che è una funzione **sincrona e bloccante** (tipica degli SDK REST). Se la chiamassi direttamente all’interno di una coroutine, bloccherebbe l’intero event loop per tutta la durata della richiesta, vanificando l’asincronia.

`asyncio.to_thread` esegue la funzione in un **thread separato** del pool di thread di asyncio, e `await` attende che il thread termini. Durante l’attesa, l’event loop può eseguire altre coroutine (qui non ce ne sono, ma è una buona pratica).

Una volta ottenuta la risposta, il metodo normalizza i dati (converte stringhe in float/int) e li ordina per timestamp.

**Attenzione:** La normalizzazione e l’ordinamento sono operazioni sincrone veloci – non bloccano significativamente l’event loop, quindi vanno bene direttamente nella coroutine.

Il risultato viene restituito alla classe base, che lo filtra (solo timestamp > last_ts) e chiama `push_to_redis` per ogni candela (operazione asincrona, con `await`).

---

### Fase 3 – Warm-up completato, si passa al loop live

La base entra nel `while self.running` e chiama `await self.listen_websocket_stream()`.

#### 3.1 Preparazione del WebSocket asincrono “ibrido”

Il metodo `listen_websocket_stream` della classe figlia fa queste operazioni:

- Salva il loop corrente: `self.loop = asyncio.get_running_loop()`.
- Crea un client WebSocket **sincrono** di Bitget (`BitgetWsClient`). Questo client, quando chiamato `build()`, avvia un **thread interno** che gestisce la connessione WebSocket e chiama callback quando arrivano messaggi.
- Imposta le callback: `ws_client.subscribe(req, self._ws_callback)` e `ws_client.error_listener`.
- Avvia il client con `ws_client.build()` – **operazione sincrona non bloccante** (il thread parte e ritorna subito).

#### 3.2 La callback `_ws_callback` – ponte tra thread e asyncio

```python
def _ws_callback(self, message):
    asyncio.run_coroutine_threadsafe(self.ws_queue.put(message), self.loop)
```

- Questa callback viene eseguita **nel thread interno** dell’SDK, **non** nell’event loop di asyncio.
- Non possiamo usare `await` qui (sarebbe SyntaxError). Dobbiamo comunicare con l’event loop in modo thread‑safe.
- `asyncio.run_coroutine_threadsafe` prende una coroutine (`self.ws_queue.put(message)`) e un loop target, e la programma per essere eseguita su quel loop. Restituisce un `concurrent.futures.Future` (che ignoriamo).
- `self.ws_queue` è una `asyncio.Queue` – un contenitore thread‑safe specifico per asyncio. Il suo metodo `put` è una coroutine; viene eseguita nell’event loop principale, e il messaggio viene accodato.

#### 3.3 Loop principale di ascolto

```python
while self.running:
    message = await self.ws_queue.get()
    for item in message["data"]:
        standardized = {...}
        await self.push_to_redis(standardized)
    self.ws_queue.task_done()
```

- `await self.ws_queue.get()` sospende la coroutine finché non arriva un messaggio nella coda. Mentre aspetta, l’event loop è libero (ma qui non ci sono altre coroutine, quindi rimane in idle).
- Quando il thread WebSocket riceve un messaggio, lo mette nella coda, e il task si risveglia.
- Per ogni candela nel messaggio, normalizza e chiama `await self.push_to_redis` – operazione asincrona che scrive su Redis.
- L’event loop, durante l’attesa della scrittura su Redis, **potrebbe** eseguire altre coroutine (se ce ne fossero), ma in questo caso siamo nel thread principale e non ci sono altre coroutine attive. Resta il fatto che `push_to_redis` cede il controllo e la coroutine `listen_websocket_stream` viene sospesa fino al completamento della scrittura.

#### 3.4 Gestione della chiusura

- Alla ricezione di `CancelledError` (durante lo shutdown), viene rilanciata dopo aver stampato un messaggio. Questo permette alla base di catturarla, uscire dal loop e chiudere Redis.

- Il `finally` chiude il client WebSocket sincrono (`ws_client.close()`), terminando il thread interno.

---

### Fase 4 – Riconnessione automatica (gestita dalla classe base)

Se `listen_websocket_stream` termina con un’eccezione (es. fallimento della connessione, errore di Redis propagato da `push_to_redis`), la base la cattura, attende 5 secondi e riprova a chiamare `listen_websocket_stream` (che ricreerà tutto da capo: nuovo client WS, nuova sottoscrizione). Questo garantisce resilienza.

---

## 3. Scelte asincrone notevoli e perché sono state fatte

| Scelta | Motivo |
|--------|--------|
| `asyncio.to_thread` per le chiamate REST | L’SDK Bitget fornisce solo funzioni **sincrone** (bloccanti). Per non bloccare l’event loop, le si esegue in un thread separato. |
| `asyncio.Queue` come ponte | Il WebSocket SDK di Bitget è **sincrono** e usa callback in thread separati. Per trasferire i messaggi all’event loop asyncio in modo thread‑safe, si usa una coda asincrona. |
| `asyncio.run_coroutine_threadsafe` nella callback | Permette di chiamare una coroutine (`queue.put`) dall’esterno dell’event loop (dal thread dell’SDK) in modo sicuro. |
| `self.loop = asyncio.get_running_loop()` | Necessario per sapere su quale loop eseguire `run_coroutine_threadsafe`. |
| Nessun `try/except` su `push_to_redis` (come nella base modificata) | Qualsiasi errore Redis fa terminare `listen_websocket_stream`, innescando la riconnessione gestita dalla base. |

---

## 4. Schema riassuntivo del flusso asincrono nel worker Bitget

```
asyncio.run(worker.run())
    │
    ├─ redis = connessione asincrona
    │
    ├─ _warmup_storico()
    │    ├─ _get_last_redis_timestamp()   (async, leggero)
    │    └─ fetch_historic_klines()
    │         └─ await asyncio.to_thread(_sync_fetch_klines)   (blocco in thread separato)
    │         └─ normalizzazione e ordinamento (sincrono veloce)
    │    └─ per ogni candela: await push_to_redis()   (asincrono, scrive su Redis)
    │
    └─ while self.running:
           try:
               await listen_websocket_stream()
           except Exception:
               await asyncio.sleep(5)   # riconnessione

Dentro listen_websocket_stream():
    │
    ├─ ottiene loop corrente
    ├─ crea ws_client sincrono, imposta callback
    ├─ ws_client.build() → parte thread interno
    │
    └─ while self.running:
           message = await ws_queue.get()       # aspetta messaggi dal thread
           for candela in message["data"]:
               await push_to_redis(candela)    # scrive su Redis
```

**Nel thread interno dell’SDK Bitget:**
- Riceve un messaggio WebSocket → chiama `_ws_callback`
- `_ws_callback` esegue `asyncio.run_coroutine_threadsafe(ws_queue.put(...), loop)`
- La coroutine `ws_queue.put` viene programmata nell’event loop principale e accoda il messaggio.

---

## Conclusione

La classe `BitgetIngestionWorker` sfrutta l’ereditarietà per riutilizzare tutta la logica robusta di `BaseIngestionWorker` (connessione Redis, warm-up intelligente, riconnessioni, shutdown). Le **scelte asincrone** (thread per REST, coda+run_coroutine_threadsafe per WebSocket) sono necessarie per integrare librerie sincrone (SDK Bitget) in un’architettura asincrona, senza bloccare l’event loop. Il flusso complessivo mantiene la concorrenza e la resilienza.

# Note di Sviluppo - Ingestion Engine (Bitget)

## ⚡ Gestione dei Timeout e Resilienza Live
Nell'implementazione del connettore Bitget, il WebSocket viene gestito tramite un'architettura a coda asincrona (`asyncio.Queue`) per fare da ponte con l'SDK multithread nativo. Per prevenire il problema delle connessioni "metà aperte" (Half-Open Connections, in cui il socket TCP muore silenziosamente senza inviare un close frame), è stato implementato un timeout di 60 secondi sulla lettura della coda.

### ⚠️ Edge Case: Asset a Bassa Liquidità
Il timeout a 60 secondi agisce come un "heartbeat logico basato sui dati". 
- **Asset ad alta liquidità (es. BTCUSDT):** Funziona perfettamente poiché viene generata e aggiornata almeno una candela (o tick) al secondo.
- **Asset a bassa liquidità (es. Ticker esotici Spot):** Se sul mercato non avvengono trade per più di 60 secondi, il WebSocket non riceverà dati aggiornati. Questo causerà un falso positivo: il bot crederà che la connessione sia morta e forzerà un ciclo continuo e inutile di disconnessione/riconnessione.

**Soluzioni/Mitigazioni Future:**
1. **Innalzamento del Timeout:** Portare il timeout a 300 secondi (5 minuti) o più se si tracciano coppie illiquide.
2. **Heartbeat Nativo (Ping/Pong):** Invece di monitorare la presenza di dati di mercato, intercettare i frame di `ping/pong` nativi scambiati tra l'SDK di Bitget e i server dell'exchange per validare lo stato del link TCP indipendentemente dai trade reali.

---

# 18/06/2026
Esatto, è proprio così: questo comportamento è universale per qualsiasi codice Python (sincrono o asincrono) che esegua un loop o un processo continuo e che venga debuggato in modalità **`attach`** dentro Docker. Con il `TradingWorker` succedeva la stessa cosa, ma i cicli erano speculari e non essendoci una fase di warm-up iniziale lineare (che scarica dati una volta sola al boot), l'effetto "salto temporale" era invisibile all'occhio.

Ecco il riassunto pronto da copiare e incollare nel tuo `notes.md` per fissare la procedura.

---

```markdown
## 🛑 Debugging Remoto in Docker (Modalità Attach): Regole e Flusso

Quando ci si aggancia a un container con il debugger (`request: attach`), lo "Stop" di VS Code disconnette solo l'interfaccia grafica, ma **NON** uccide il processo Python dentro Docker. Se il codice contiene cicli infiniti o task asincroni (`asyncio`), questi continueranno a girare in background.

### Il Problema del "Secondo Avvio"
1. **Primo F5:** Il container è vergine, `--wait-for-client` blocca Python alla riga zero. Il debugger si aggancia, esegue il warm-up e va nel loop.
2. **Disconnessione (Shift+F5):** Il debugger si stacca, ma Python continua a girare nel loop infinito dentro Docker.
3. **Secondo F5:** Il debugger si riaggancia, ma Python è già nel loop. Salta i breakpoint iniziali, il warm-up non si rivede (perché è già passato) e i log del terminale si sfasano.

---

### 🔄 Flusso di Lavoro Corretto (La Routine di Debug)

Per testare modifiche al codice o rieseguire il flusso dall'inizio (incluso il Warm-up):

1. **Uccidi il processo fantasma (Riavvia il container):**
   Prima di premere F5, lancia da terminale sul tuo PC:
   ```bash
   docker restart pipster_bot

```

*Nota: Il container si riavvierà e si bloccherà alla riga zero in attesa del client.*

2. **Aggancia il debugger:**
Premi **F5** su VS Code per avviare la configurazione `Python: Docker Debug`. I breakpoint iniziali verranno ora rispettati regolarmente.
3. **Usa il terminale nativo di Docker per i Log:**
La console di debug di VS Code può perdersi i `print()` asincroni a causa dei buffer TCP. Per la certezza matematica di vedere tutto l'output in tempo reale, tieni un terminale separato aperto con:
```bash
docker logs -f pipster_bot

```



### ⚡ Gestione dei Breakpoint nell'Asincrono (`asyncio`)

* **Evita lo Step Over (F10) sugli `await`:** Nelle librerie di rete (come il WebSocket di Bitget), fare il passo singolo su un `await` rilascia il controllo all'event loop. Se nel frattempo scattano timeout o ping di sottofondo, il debugger perde il filo e l'esecuzione "scappa".
* **Strategia:** Metti un breakpoint sulla riga *successiva* all'operazione asincrona e premi **Continua (F5)** invece di avanzare a passi.

```

```

---
# 19/06/2026

Massima concentrazione. Se hai visto che `params` è un dizionario, hai decifrato la "filosofia segreta" di questa libreria.

Gli sviluppatori di Bitget hanno fatto una scelta strutturale drastica: **non hanno mappato i singoli parametri delle funzioni in Python**. Hanno preferito creare dei metodi "passacarte" che prendono un dizionario generico e lo girano direttamente alle loro API HTTP.

Ecco la mappa definitiva per capire come è organizzata la libreria e come usarla senza impazzire.

---

## L'Architettura dell'SDK Bitget

Quando vedi un import del tipo `from bitget.v2.mix.market_api import MarketApi`, la struttura a matrioska si scompone così:

* **`v2` (o `v1`)**: Indica la versione dell'endpoint dell'exchange. Usa sempre la **`v2`** dove disponibile, poiché la v1 è deprecata per molti mercati.
* **`mix` (o `spot`)**: Identifica il tipo di mercato.
* `spot` = Mercato classico (compri e vendi l'asset reale).
* `mix` = Il termine interno di Bitget per i **Derivati / Futures / Perpetual Contracts**. Visto che stai costruendo un bot di trading quantitativo, `mix` è quasi certamente il tuo target principale.


* **`market_api` (o `account_api` / `order_api`)**: Identifica il dominio funzionale:
* `market_api`: Dati pubblici (candele, orderbook, ticker). **Non** richiedono API key.
* `account_api`: Dati privati del conto (saldi, margini). Richiedono autenticazione.
* `order_api`: Esecuzione ordini (compra, vendi, stop loss). Richiedono autenticazione.



---

## Come mappare i parametri (La Regola dell'Involucro)

Dato che l'SDK accetta solo un dizionario `params`, devi guardare la documentazione web ufficiale di Bitget e copiare le chiavi dei parametri *esattamente* come sono scritte lì (rispettando il camelCase).

Per l'endpoint `candles` del mercato `v2/mix` (Futures), l'API si aspetta queste tre chiavi fondamentali:

1. **`symbol`**: Il ticker dello strumento (es. `BTCUSDT`).
2. **`granularity`**: Il timeframe (es. `1m`, `5m`, `1h`, `1D`).
3. **`productType`**: Questo è il parametro cruciale che spesso ci si dimentica nei contratti. Indica il tipo di margine. Per i futures standard regolati in USDT devi passare **`usdt-futures`** (nella V1 si chiamava `umcbl`).

### Il codice d'esempio pulito per il tuo Warm-up:

```python
from bitget.v2.mix.market_api import MarketApi

# 1. Istanzie l'API (senza chiavi, sono dati pubblici)
market_client = MarketApi()

# 2. Costruisci il dizionario esattamente come richiesto dalle specifiche V2 Mix
params = {
    "symbol": "BTCUSDT",
    "granularity": "1h",
    "productType": "usdt-futures"  # <-- Cruciale per non ricevere errori dall'exchange
}

# 3. Esegui la chiamata passandogli il dizionario
response = market_client.candles(params)

# 4. Bitget risponde SEMPRE con una struttura sSStandard standardizzata
if response.get("code") == "00000":  # "00000" nella codifica Bitget significa "Successo"
    raw_candles = response.get("data", [])
    # Ora hai una lista di liste: [timestamp, open, high, low, close, volume]
else:
    raise Exception(f"Errore API Bitget: {response.get('msg')}")

```

---

## E per il WebSocket live?

Il modulo `BitgetWsClient` ragiona con la stessa logica "raw". Non ha metodi del tipo `subscribe_to_btc()`. Devi mandargli un dizionario di configurazione (chiamato *payload*) per dirgli cosa ascoltare.

La struttura V2 per connettersi e ascoltare le candele live in tempo reale è questa:

```python
# Struttura del messaggio di sottoscrizione da inviare al socket
subscribe_message = {
    "op": "subscribe",
    "args": [
        {
            "instType": "USDT-FUTURES",
            "channel": "candle1h",  # Il canale fonde la parola 'candle' col timeframe
            "instId": "BTCUSDT"
        }
    ]
}

# Invio sul client WebSocket già inizializzato
ws_client.send(subscribe_message)

```
---
Ecco le istruzioni pronte da incollare direttamente su `notes.md` con i diversi metodi per ispezionare Redis in ambiente Docker.

---

# Guida all'Ispezione di Redis in Docker

### Metodo 1: `docker exec` Interattivo (Consigliato)

Accedi direttamente alla CLI di Redis dentro il container Docker in esecuzione.

**1. Shell interattiva su DB 15:**

```bash
docker exec -it <nome_container_redis> redis-cli -n 15

```

**2. Esecuzione di comandi diretti in riga singola (senza entrare nella shell):**

```bash
# Leggi l'Hash della candela live
docker exec -it <nome_container_redis> redis-cli -n 15 HGETALL candle:SPOT:BTCUSDT:1m:live

# Leggi gli ultimi 3 elementi dello Stream delle candele chiuse
docker exec -it <nome_container_redis> redis-cli -n 15 XREVRANGE candle:SPOT:BTCUSDT:1m:stream + - COUNT 3

```

---

### Metodo 2: Container Temporaneo (Se Redis non espone le porte all'host)

Utile se Redis è in una rete Docker privata e non hai `redis-cli` installato sull'host.

```bash
docker run --rm -it --network <nome_rete_docker> redis:alpine redis-cli -h <nome_service_redis> -p 6379 -n 15

```

---

### Metodo 3: Da Host Locale (Se la porta 6379 è mappata `6379:6379`)

Se nel `docker-compose.yml` hai configurato `ports: - "6379:6379"` e hai `redis-cli` installato sulla tua macchina.

```bash
redis-cli -h localhost -p 6379 -n 15

```

---

### Metodo 4: Script Monoriga via Python (Dal Container App)

Se vuoi ispezionare i dati esattamente dal punto di vista dell'applicazione Python.

```bash
docker exec -it <nome_container_app> python -c "import redis, os; r = redis.Redis(host=os.getenv('REDIS_HOST', 'localhost'), port=6379, db=15); print(r.hgetall('candle:SPOT:BTCUSDT:1m:live'))"

```

---

### Cheatsheet Comandi Utili da Eseguire in `redis-cli`

**1. Verifica Chiavi e Tipo Dati:**

```redis
# Elenca tutte le chiavi presenti nel DB 15
KEYS candle:*

# Controlla il tipo di struttura (hash o stream)
TYPE candle:SPOT:BTCUSDT:1m:live
TYPE candle:SPOT:BTCUSDT:1m:stream

```

**2. Ispezione Candela Live (Hash):**

```redis
# Mostra tutti i campi della candela in costruzione
HGETALL candle:SPOT:BTCUSDT:1m:live

# Estrai solo specifici campi
HMGET candle:SPOT:BTCUSDT:1m:live close volume_quote timestamp

```

**3. Ispezione Candele Chiuse (Stream):**

```redis
# Conta quante candele chiuse sono attualmente salvate
XLEN candle:SPOT:BTCUSDT:1m:stream

# Leggi le ultime N candele salvate (dalla più recente alla più vecchia)
XREVRANGE candle:SPOT:BTCUSDT:1m:stream + - COUNT 5

# Leggi le prime N candele salvate (dalla più vecchia alla più recente)
XRANGE candle:SPOT:BTCUSDT:1m:stream - + COUNT 5

```

**4. Debugging in Tempo Reale (Monitor):**

```redis
# Traccia OGNI operazione in arrivo su Redis in tempo reale (attenzione in prod: impatta le performance)
MONITOR

```

L'idea di documentare ogni modulo è ottima per la manutenibilità, ma scrivere e aggiornare manualmente ogni singola funzione in file Markdown separati è un'anti-pattern che porta rapidamente al **documentation drift** (la documentazione diventa obsoleta non appena rifattorizzi il codice).

In ambito DevOps e modern software engineering la pratica standard è il paradigma **Docs-as-Code**:

* **Docstrings nel codice (API Reference):** Le singole funzioni, classi e parametri vanno documentati *esclusivamente* all'interno del codice Python usando un formato standard (come *Google Style* o *NumPy*). Il codice resta la singola fonte di verità.
* **Architettura e Guide in `docs/`:** La cartella `docs/` deve contenere la visione d'insieme, l'architettura di sistema, le guide operative e i flussi di dati.
* **Generazione Automatica via CI/CD:** Si usa un generatore di documentazione che legge automaticamente le docstring Python e compila un sito statico o file Markdown sempre aggiornati.

---

**Struttura raccomandata per `docs/**`

```text
docs/
├── index.md                 # Panoramica del progetto, obiettivi e prerequisiti
├── architecture/
│   ├── overview.md          # Architettura high-level (Docker, Redis, Moduli)
│   └── data_flow.md         # Flussi dati (es. gestione websocket, retry logic)
├── guides/
│   ├── setup.md             # Guida per ambiente di sviluppo e devcontainer
│   └── configuration.md     # Spiegazione variabili .env e file di configurazione
├── adr/                     # Architectural Decision Records (scelte chiave di design)
│   └── 0001-async-client.md # Es. perché aiohttp invece di requests/httpx
└── api/                     # Pagine segnaposto per la generazione automatica API

```

---

**Lo stack DevOps ideale per Python**

* **MkDocs + `mkdocstrings**`: È lo standard moderno per Python. Con pochissima configurazione legge il codice della tua app e genera automaticamente la documentazione delle funzioni e delle classi formattata in Markdown/HTML.
* **Linting delle docstrings**: Integrazione nel flusso di test o pre-commit (`interrogate` o `flake8-docstrings`) per fallire la build se aggiungi una funzione pubblica senza docstring.
* **Deploy automatico**: Tramite GitHub Actions / GitLab CI compili la documentazione a ogni merge su `main` e la pubblichi (es. su GitHub Pages).

Mantenendo l'API Reference dentro le docstring Python (come hai già fatto impeccabilmente per `AsyncHttpClient`), in `docs/` ti basterà spiegare il *perché* e il *come si collegano i pezzi*, lasciando l'automazione a gestire il *cosa fa ogni funzione*.