#!/bin/bash

# Titolo estetico per il terminale
echo "==========================================="
echo "   AVVIO DEL BOT TRADING PIPSTER (UNIX)    "
echo "==========================================="

# 1. Controlla se Docker è in esecuzione
if ! docker info > /dev/null 2>&1; then
    echo "[ERRORE] Docker non è avviato! Apri Docker Desktop prima."
    exit 1
fi

# 2. Controlla se il file .env esiste
if [ ! -f .env ]; then
    echo "[ERRORE] Il file .env non esiste!"
    echo "Rinomina .env.example in .env e inserisci le tue API KEY."
    exit 1
fi

# 3. Avvia il container
echo "[INFO] Lancio del container Docker..."
docker compose up

echo "==========================================="
echo "        IL BOT È STATO FERMATO.            "
echo "==========================================="
