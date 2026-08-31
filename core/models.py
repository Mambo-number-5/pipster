from dataclasses import dataclass

@dataclass(slots=True)
class Candle:
    """Modello dati centralizzato per la rappresentazione unificata delle candele (OHLCV).

    Questa classe agisce da Data Transfer Object (DTO) agnostico rispetto all'exchange
    di origine, garantendo type safety e prestazioni elevate nell'hot layer del bot.
    L'utilizzo di `slots=True` previene la creazione del dizionario interno `__dict__`
    per ciascuna istanza, riducendo drasticamente l'impronta di memoria RAM ed eliminando
    l'overhead di CPU tipico delle validazioni su flussi di dati ad alta frequenza.

    Attributes:
        timestamp (int): Unix timestamp espresso in millisecondi (UTC) riferito all'apertura della candela.
        open (float): Prezzo di apertura del timeframe.
        high (float): Prezzo massimo toccato all'interno del timeframe.
        low (float): Prezzo minimo toccato all'interno del timeframe.
        close (float): Prezzo di chiusura (o prezzo corrente se la candela è in corso).
        volume_base (float): Volume complessivo scambiato espresso nella valuta base (es. BTC).
        volume_quote (float): Volume complessivo scambiato espresso nella valuta quota (es. USDT).
        is_closed (bool): Flag di stato: `True` se la candela è definitivamente chiusa, 
            `False` se la candela è in corso di formazione nel flusso WebSocket live.
    """
    timestamp: int          # Unix ms
    open: float
    high: float
    low: float
    close: float
    volume_base: float
    volume_quote: float
    is_closed: bool

    def to_redis_stream_fields(self, symbol: str) -> dict[str, str]:
        """Serializza gli attributi della candela nel formato stringa piatto richiesto da Redis Streams.

        Esegue la conversione esplicita di tutti i tipi numerici in stringhe per garantire 
        la compatibilità nativa con l'operazione `XADD`, evitando overhead di serializzazione 
        JSON o problemi di encoding di tipo tra client differenti.

        Args:
            symbol (str): Il ticker di mercato da associare al payload dello stream (es. "BTCUSDT").

        Returns:
            dict[str, str]: Mappa chiave-valore pronta per l'inserimento in uno stream Redis.
        """
        return {
            "symbol": symbol,
            "timestamp": str(self.timestamp),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume_base": str(self.volume_base),
            "volume_quote": str(self.volume_quote),
        }

    def to_redis_live_hash(self, symbol: str) -> dict[str, str]:
        """Serializza la candela per il tracciamento della candela live non ancora chiusa (Redis Hash).

        Integrazione del dizionario generato per gli stream con il campo flag `is_closed` 
        (mappato come "1" o "0"), permettendo ai componenti downstream di distinguere 
        gli aggiornamenti temporanei dai dati storicizzati.

        Args:
            symbol (str): Il ticker di mercato da associare alla chiave Hash (es. "BTCUSDT").

        Returns:
            dict[str, str]: Mappa chiave-valore pronta per l'aggiornamento tramite operazione `HSET`.
        """
        fields = self.to_redis_stream_fields(symbol)
        fields["is_closed"] = "1" if self.is_closed else "0"
        return fields