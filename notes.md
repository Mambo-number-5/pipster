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

---

**Perché queste varianti?**
1.  **`-m unittest`**: È utile perché non ti obbliga a inserire il blocco `main` in ogni file di test; Python cercherà automaticamente tutte le funzioni che iniziano con `test_`.
2.  **Percorso con i punti (`tests.test_redis...`)**: Necessario quando usi il flag `-m`, perché Python tratta il file come un modulo del pacchetto.
3.  **Percorso con lo slash (`tests/test_redis.py`)**: Si usa quando lanci il file direttamente come script.
