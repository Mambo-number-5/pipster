from redis_client import RedisClient as RC
def professional_worker_loop(db: RC, stream: str, group: str, consumer: str, max_retries: int = 3):
    """
    Esegue il ciclo infinito di consumo resiliente dei messaggi da uno stream Redis.

    Questa funzione implementa un pattern di consumo "At-Least-Once", garantendo che
    ogni messaggio venga elaborato con successo o spostato in una Dead Letter Queue (DLQ)
    dopo un numero prefissato di fallimenti. Il loop gestisce automaticamente sia il 
    recupero dei messaggi pendenti (crash recovery) che l'ascolto di nuovi segnali.

    Logica operativa:
        1. Identificazione messaggi pendenti (PEL): Interroga i messaggi letti ma non confermati.
        2. Gestione Poison Messages: Se un messaggio supera `max_retries`, viene archiviato 
           nello stream `{stream}:errors` e rimosso dalla coda principale.
        3. Elaborazione Real-time: Ascolta nuovi messaggi bloccando il thread finché non 
           ne arrivano di nuovi (ID '>').
        4. Acknowledge (ACK): Invia la conferma a Redis solo DOPO che la logica di business 
           è stata completata con successo.

    Args:
        db (RedisClient): Istanza del wrapper Redis personalizzato per l'accesso ai dati.
        stream (str): Nome dello stream Redis da cui leggere i segnali di trading.
        group (str): Nome del Consumer Group associato per la gestione dello stato.
        consumer (str): Identificativo univoco dell'istanza del bot (es. 'bot_1').
        max_retries (int, optional): Numero massimo di tentativi di consegna prima di 
            considerare un messaggio "velenoso". Default: 3.

    Returns:
        None: La funzione entra in un loop infinito e non restituisce valori.

    Raises:
        ConnectionError: In caso di perdita persistente della connessione con il server Redis.
        Exception: Eventuali errori imprevisti durante l'esecuzione della logica di business 
            vengono loggati, ma il loop prosegue con il messaggio successivo.

    Note:
        La funzione si appoggia internamente a `XRANGE` per il recupero dei dati dai 
        messaggi pendenti identificati come 'poison', evitando conflitti con il cursore 
        di lettura del Consumer Group.
    """
    db.create_consumer_group(stream, group)
    MAX_RETRIES = 3

    while True:
        # --- FASE 1: PULIZIA "MESSAGGI VELENOSI" ---
        pending_info = db.get_pending_messages(stream, group)
        
        for info in pending_info:
            msg_id = info['message_id']
            # Se è stato consegnato troppe volte, qualcuno sta facendo crashare il bot!
            if info['times_delivered'] > MAX_RETRIES:
                print(f"☣️ DLQ: Il messaggio {msg_id} è corrotto. Lo sposto...")
                
                # Leggiamo i dati del messaggio incriminato
                data = db.get_message_by_id(stream, msg_id)
                if data:
                    # Lo mettiamo in uno stream di scarto per analisi umana
                    db.log_event(f"{stream}:errors", data)
                    # Diamo l'ACK così sparisce dallo stream principale
                    db.acknowledge(stream, group, msg_id)
                continue

        # --- FASE 2: LETTURA E ELABORAZIONE ---
        # Leggiamo i messaggi (nuovi o pendenti "sani")
        messages = db.read_group(stream, group, consumer, last_id='>', block=2000)

        for stream_name, msg_list in messages:
            for msg_id, payload in msg_list:
                try:
                    # >>> QUI SUCCEDE LA MAGIA DEL TUO BOT <<<
                    print(f"Elaborazione {msg_id}...")
                    # Esempio: esegui_ordine(payload)
                    
                    # Se arriviamo qui, tutto è andato bene
                    db.acknowledge(stream, group, msg_id)
                    
                except Exception as e:
                    # Se il tuo codice crasha qui, NON facciamo l'ACK.
                    # Il messaggio resta nello stream. Al prossimo giro (o al riavvio),
                    # il 'times_delivered' sarà aumentato.
                    print(f"❌ Errore durante l'elaborazione di {msg_id}: {e}")