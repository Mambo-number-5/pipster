FROM python:3.11-slim

# Silenzia il warning di pip come root
ENV PIP_ROOT_USER_ACTION=ignore

# Dipendenze di sistema (necessarie per compilare alcune librerie di trading/math)
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Sfruttiamo la cache di Docker per le dipendenze
COPY requirements.txt .

# 2. PRIMA ZONA: Cache per l'aggiornamento di pip
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --upgrade pip

# 3. SECONDA ZONA: Cache per le dipendenze del progetto
# Nota: ho tolto --no-cache-dir qui, perché ora la cache la gestisce Docker col mount
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Copia il resto del progetto
COPY . .

# Il comando CMD della Versione 2 è perfetto per il debug, però qui siamo in produzione ed è meglio una semplice chiamata al main
# per il debug inserire in un file docker-compose.override.yml la vecchia versione per lanciare:
# CMD["python", "-Xfrozen_modules=off", "-m debugpy", "--listen 0.0.0.0:5678", "--wait-for-client", "main.py"]
# --wait-for-client blocca l'esecuzione finché non premi F5 su VS Codes
CMD ["python", "main.py"]