# ADR-0003: Composizione vs Ereditarietà per i client WebSocket

**Data**: 2026-09-24  
**Stato**: Accettato  
**Decisori**: Team di sviluppo Pipster

---

## Contesto

Il progetto ha due gerarchie di classi con scopi simili ma esigenze opposte:

1. **Client WebSocket**: `AsyncWsClient` (core, agnostico) e `BitgetWsClient` (exchange-specifico). Entrambi gestiscono una connessione WebSocket, ma con livelli di astrazione diversi: il primo si occupa del trasporto, il secondo del protocollo Bitget.

2. **Worker di ingestion**: `BaseIngestionWorker` (core, astratto) e `BitgetIngestionWorker` (exchange-specifico). Entrambi orchestrano il ciclo di vita dell'ingestion, con il secondo che implementa i metodi specifici di Bitget.

Per il worker la scelta è stata **ereditarietà**: `BitgetIngestionWorker(BaseIngestionWorker)`. La relazione è "is-a": un worker Bitget **è** un worker di ingestion con comportamenti specifici di Bitget.

Per il client WebSocket la scelta è stata **composizione**: `BitgetWsClient` **possiede** un `AsyncWsClient`. La relazione è "has-a": un client Bitget **ha** un client WebSocket generico e ci costruisce sopra.

Questa scelta non è ovvia e merita di essere documentata, perché in futuro la tentazione di usare ereditarietà anche qui sarà forte.

---

## Decisione

Adottiamo **composizione** per `BitgetWsClient` e **ereditarietà** per `BitgetIngestionWorker`.

La scelta dipende dalla **natura della relazione**:

- Se il figlio **è una specializzazione** del padre (stessa semantica, comportamento esteso o raffinato): **ereditarietà**.
- Se il figlio **usa** il padre come componente interno, cambiandone il contesto d'uso: **composizione**.

### Applicazione concreta

**`BitgetIngestionWorker(BaseIngestionWorker)` — ereditarietà**  
Il worker Bitget è un worker di ingestion: implementa gli stessi metodi astratti (`sync_time`, `fetch_historic_klines`, `listen_websocket_stream`) con logica specifica. Il contratto pubblico della classe base è rispettato: chi ha un riferimento a `BaseIngestionWorker` può chiamare `.run()` senza sapere se è Bitget o un altro exchange. La relazione è genuinamente "is-a".

**`BitgetWsClient` con `self._client = AsyncWsClient(...)` — composizione**  
Il client Bitget **usa** un client WebSocket generico per il trasporto, ma aggiunge un livello di protocollo (login, subscribe, routing). Il `send` del client Bitget non è una specializzazione del `send` di `AsyncWsClient`: è un'operazione diversa che internamente serializza JSON, applica throttle, e poi delega al trasporto. Non c'è un "is-a".

---

## Alternative considerate

### 1. Ereditarietà per `BitgetWsClient(AsyncWsClient)`

**Pro**: meno codice, accesso diretto a `_ws`, `is_connected`, `_invoke_callback`.  
**Contro**:
- **Viola il principio di sostituzione di Liskov**: un `BitgetWsClient` non può essere usato ovunque serva un `AsyncWsClient`, perché `send` accetta payload diversi (dict JSON con semantica Bitget vs stringa grezza) e `subscribe` è un metodo che il padre non conosce.
- **Rende difficile testare in isolamento**: per testare `BitgetWsClient` servirebbe mockare `AsyncWsClient` tramite ereditarietà, oppure rifattorizzare. Con la composizione, inietti un mock nel costruttore.
- **Accoppia il ciclo di vita**: se `AsyncWsClient` cambia la firma di un metodo interno, `BitgetWsClient` si rompe in modi non ovvi.
- **Impedisce di avere più client WS**: un domani potresti volere due connessioni (una per canali pubblici, una per privati). Con l'ereditarietà è complesso; con la composizione è naturale.

**Motivo dello scarto**: la composizione è più flessibile, più testabile, e riflette correttamente la relazione "has-a".

### 2. Nessuna astrazione, solo `BitgetWsClient` monolitico

**Pro**: meno file, meno indirezioni.  
**Contro**: ogni exchange duplicherebbe la logica di riconnessione, ping, watchdog, backoff. Il codice comune sarebbe copiato o estratto in funzioni libere, perdendo coerenza.  
**Motivo dello scarto**: il core `AsyncWsClient` è già scritto, testato e robusto. Buttarlo via per evitare un'astrazione è un peggioramento netto.

### 3. Ereditarietà per entrambi o composizione per entrambi

**Pro**: coerenza stilistica.  
**Contro**: la scelta corretta dipende dalla semantica, non dallo stile. Forzare la stessa soluzione ovunque è un errore.  
**Motivo dello scarto**: la natura delle due relazioni è diversa ("is-a" per il worker, "has-a" per il client), e la soluzione deve rifletterla.

---

## Conseguenze

### Positive

- **Separazione netta tra trasporto e protocollo**: `AsyncWsClient` gestisce connessione, ping, riconnessione, watchdog. `BitgetWsClient` gestisce login, subscribe, routing dei canali. Ogni livello è testabile in isolamento.
- **Testabilità**: `BitgetWsClient` può essere testato iniettando un mock di `AsyncWsClient`. I test attuali (`test_bitget_ws_client.py`) sfruttano questa proprietà senza dover toccare la rete.
- **Riusabilità**: `AsyncWsClient` è già pronto per essere usato da un eventuale `BinanceWsClient`, `BybitWsClient`, ecc. senza modifiche.
- **Sostituibilità**: se in futuro volessimo sostituire `AsyncWsClient` con un'implementazione basata su `aiohttp` invece di `websockets`, basterebbe che esponga la stessa interfaccia pubblica. `BitgetWsClient` non cambierebbe una riga.
- **Lifecycle indipendente**: possiamo avere due `BitgetWsClient` (uno pubblico, uno privato) che condividono lo stesso `AsyncWsClient` o ne usano due distinti, senza toccare la gerarchia.

### Negative

- **Un livello di indirezione in più**: `BitgetWsClient.send()` chiama `AsyncWsClient.send()`, che chiama `WebSocket.send()`. In caso di debug, bisogna seguire la catena.
- **Accesso controllato allo stato interno**: `BitgetWsClient` non accede direttamente a `_ws` o `is_connected`; deve usare la property pubblica `is_connected` e i metodi esposti. Questo è un bene per l'incapsulamento, ma richiede disciplina per non "aprire" il client interno.
- **Leggermente più codice**: `BitgetWsClient` ha un attributo `self._client` che eredita funzionalità invece di averle direttamente.

### Neutre

- **Il pattern di callback rimane**: `AsyncWsClient` espone `on_message`, `on_connect`, `on_error`. `BitgetWsClient` le collega a metodi propri (`_handle_raw_message`, `_on_reconnect`, `_on_ws_error`). Questo pattern è coerente con entrambe le scelte.

---

## Regola generale

| Relazione | Esempio | Scelta |
|---|---|---|
| "is-a": il figlio è una specializzazione del padre | `BitgetIngestionWorker` è un `BaseIngestionWorker` | **Ereditarietà** |
| "has-a": il figlio usa il padre come componente | `BitgetWsClient` usa un `AsyncWsClient` | **Composizione** |
| "is-implemented-in-terms-of": il figlio riusa il padre internamente ma con semantica diversa | `BitgetWsClient` che estende `AsyncWsClient` solo per riusarne il codice | **Composizione** (violerebbe Liskov) |

**Regola operativa**: prima di scrivere `class Figlio(Padre)`, chiediti: *"posso passare un'istanza di Figlio dove il codice si aspetta Padre senza sorprese?"*. Se la risposta è no, usa la composizione.

---

## Implementazione

- **File principale**: `core/http_client.py` (`AsyncWsClient`), `exchanges/bitget/bitget_api.py` (`BitgetWsClient`).
- **Test**: `tests/unit/test_async_ws_client.py` (trasporto), `tests/unit/test_bitget_ws_client.py` (protocollo, con mock iniettato).

---

## Riferimenti

- Principio di sostituzione di Liskov: `https://en.wikipedia.org/wiki/Liskov_substitution_principle`
- Design Patterns (Gamma et al.), sezione "Favor Composition Over Inheritance"
- ADR-0002 (sliding window atomica) — per contesto sull'architettura dei client
- ADR-0004 (backpressure worker) — per contesto sull'architettura del worker

---

**Nota di mantenimento**: se in futuro si aggiunge un secondo exchange, il pattern di composizione va replicato: un `XyzWsClient` che **possiede** un `AsyncWsClient` e implementa il protocollo Xyz. Non ereditare da `BitgetWsClient`.
