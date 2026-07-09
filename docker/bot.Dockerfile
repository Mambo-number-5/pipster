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

WORKDIR /app

# 3. Copia dipendenze Python
COPY requirements.txt .

# 4. Installazione con Cache Mount (Ora funzionerà con BuildKit attivo)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install -r requirements.txt

# 5. Copia del codice
COPY . .

CMD ["python", "main.py"]