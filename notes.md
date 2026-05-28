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
