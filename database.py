import redis
import os

# Legge le variabili caricate da docker-compose
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", 6379)

# Connessione al database
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

# Esempio: Salvare lo stato di un trade
def save_trade_status(symbol, status):
    r.set(f"trade:{symbol}:status", status)


# Esempio: Leggere lo stato
status = r.get("trade:btc_usdt:status")
print(f"Stato attuale BTC: {status}")


if __name__ == "__main__":
    pass