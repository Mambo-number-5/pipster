import redis
import os

def test_connection():
    try:
        # Usa il nome del servizio definito nel docker-compose
        r = redis.Redis(host='redis', port=6379, decode_responses=True)
        r.set('test_key', 'Funziona!')
        valore = r.get('test_key')
        print(f"✅ Connessione Redis: {valore}")
    except Exception as e:
        print(f"❌ Errore: {e}")

if __name__ == "__main__":
    test_connection()