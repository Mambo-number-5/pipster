FROM python:3.11-slim

# Dipendenze di sistema (necessarie per compilare alcune librerie di trading/math)
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Sfruttiamo la cache di Docker per le dipendenze
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia il resto del progetto
COPY . .

# Espone la porta del debugger
EXPOSE 5678

# Il comando CMD della Versione 2 è perfetto per il debug:
# --wait-for-client blocca l'esecuzione finché non premi F5 su VS Code
CMD ["python", "main.py"]
