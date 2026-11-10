FROM python:3.11-slim

# 1. Installazione pacchetti di sistema (Il blocco più pesante: 450MB)
# Lo mettiamo in cima perché una volta fatto, non lo toccherai quasi mai.
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

# 2. Solo ora dichiariamo le variabili di ambiente utili per l'installazione delle librerie di 'pip'.
ENV PIP_ROOT_USER_ACTION=ignore

# CREAZIONE UTENTE (Risolve il riavvio lento di devcontainer)
# Creiamo il gruppo e l'utente 'pipster' associandogli l'UID 1000. 
# Il parametro -m crea la sua cartella home in /home/pipster
RUN groupadd -g 1000 pipster && \
    useradd -u 1000 -g pipster -m -s /bin/bash pipster

WORKDIR /app

# 3. Copia dipendenze Python
COPY requirements.txt .

# 4. Installazione con Cache Mount (Ora funzionerà con BuildKit attivo)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install -r requirements.txt

# 4.5 INIEZIONE AUTOMATICA SDK BITGET (Risolve l'assenza di setup.py a monte)
# Cloniamo il repo ufficiale, copiamo la cartella core nei site-packages globali e puliamo la cache temporanea.
RUN git clone https://github.com/BitgetLimited/v3-bitget-api-sdk.git /tmp/bitget-sdk && \
    cp -r /tmp/bitget-sdk/bitget-python-sdk-api/bitget /usr/local/lib/python3.11/site-packages/ && \
    rm -rf /tmp/bitget-sdk

# 5. Copia del codice
COPY . .

CMD ["python", "main.py"]