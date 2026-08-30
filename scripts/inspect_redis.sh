#!/usr/bin/env bash

# Configurazione di default
CONTAINER="${REDIS_CONTAINER:-pipster_redis}"
ACTION="${1:-live}"
SYMBOL="${2:-BTCUSDT}"
TF="${3:-1m}"
DB="${4:-15}"
TYPE="${5:-spot}"

KEY_LIVE="live:market:${TYPE}:${SYMBOL}:${TF}"
KEY_STREAM="stream:market:${TYPE}:${SYMBOL}:${TF}"

case "$ACTION" in
  live)
    echo "🔍 [DB $DB] Hash Live: $KEY_LIVE"
    docker exec -it "$CONTAINER" redis-cli -n "$DB" HGETALL "$KEY_LIVE"
    ;;
  stream)
    echo "📊 [DB $DB] Ultime 5 candele Stream: $KEY_STREAM"
    docker exec -it "$CONTAINER" redis-cli -n "$DB" XREVRANGE "$KEY_STREAM" + - COUNT 5
    ;;
  watch)
    echo "👀 Monitoring live di $KEY_LIVE (Ctrl+C per uscire)..."
    watch -n 1 "docker exec -t $CONTAINER redis-cli -n $DB HGETALL $KEY_LIVE"
    ;;
  keys)
    echo "🔑 [DB $DB] Chiavi presenti:"
    docker exec -it "$CONTAINER" redis-cli -n "$DB" KEYS "candle:*"
    ;;
  cli)
    docker exec -it "$CONTAINER" redis-cli -n "$DB"
    ;;
  info)
    echo "ℹ️ [DB $DB] Informazioni Stream: $KEY_STREAM"
    docker exec -it "$CONTAINER" redis-cli -n "$DB" XINFO STREAM "$KEY_STREAM"
    echo ""
    echo "💾 [DB $DB] Memoria occupata:"
    docker exec -it "$CONTAINER" redis-cli -n "$DB" MEMORY USAGE "$KEY_STREAM"
    ;;
  *)
    echo "Uso: $0 [live|stream|watch|keys|cli|info] [SYMBOL] [TIMEFRAME] [DB] [TYPE]"
    echo "Esempio: $0 live ETHUSDT 5m 15 SPOT"
    exit 1
    ;;
esac