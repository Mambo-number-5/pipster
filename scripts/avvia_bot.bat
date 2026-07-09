@echo off
title Avvio Bot Trading Pipster
echo ===========================================
echo    AVVIO DEL BOT TRADING IN CORSO...
echo ===========================================
echo.

:: Controlla se il file .env esiste, altrimenti avvisa l'utente
if not exist .env (
    echo [ERRORE] Il file .env non esiste! 
    echo Rinomina .env.example in .env e inserisci le tue API KEY.
    pause
    exit
)

:: Avvia il container tramite Docker Compose
echo [INFO] Lancio del container Docker...
docker compose up

echo.
echo ===========================================
echo    IL BOT E' STATO FERMATO.
echo ===========================================
pause