import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

"""
MONTE CARLO ANALYSIS MODULE - GUIDA ALL'USO DELLE STRATEGIE

1. SIMPLE RESAMPLING (Non-Parametrico):
   - Quando usarlo: Strategie High-Frequency o Scalping dove i trade sono chiusi velocemente.
   - Assunto: Ogni trade è indipendente (IID). L'ordine non conta.
   - Rischio: Sottostima i drawdown se la strategia soffre di "cluster" di perdite.

2. BLOCK BOOTSTRAPPING (Il nostro standard attuale):
   - Quando usarlo: Trend Following, Swing Trading, Strategie basate su Momentum.
   - Assunto: Esiste una correlazione temporale (regimi di mercato). 
   - Funzionamento: Invece di rimescolare singoli trade, rimescola "blocchi" di trade consecutivi.
   - Vantaggio: Mantiene intatta la struttura delle serie negative/positive tipiche dei trend.

3. PARAMETRIC SIMULATION (Gaussiana/Student-T):
   - Quando usarlo: Quando hai pochissimi trade ma conosci la volatilità e il rendimento atteso.
   - Assunto: I rendimenti seguono una distribuzione statistica nota.
   - Rischio: Molto alto (Mathwashing). La realtà ha spesso "code larghe" che la teoria ignora.
"""

def simple_resampling_mc(trades_pnl: np.ndarray, iterations: int = 1000, initial_capital: float = 1000.0) -> np.ndarray:
    """
    Esegue un Monte Carlo non-parametrico rimescolando i singoli trade (IID).
    
    Args:
        trades_pnl (np.ndarray): Array dei profitti/perdite dei trade reali.
        iterations (int): Numero di simulazioni da eseguire.
        initial_capital (float): Capitale iniziale.
        
    Returns:
        np.ndarray: Matrice delle curve di equity simulate.
    """
    results = []
    n_trades = len(trades_pnl)
    
    for _ in range(iterations):
        # Campionamento con reinserimento
        simulated_trades = np.random.choice(trades_pnl, size=n_trades, replace=True)
        equity_curve = initial_capital + np.cumsum(simulated_trades)
        results.append(equity_curve)
        
    return np.array(results)

def block_bootstrap_mc(trades_pnl: np.ndarray, block_size: int = 5, iterations: int = 1000, initial_capital: float = 1000.0) -> np.ndarray:
    """
    Esegue Monte Carlo rimescolando blocchi di trade per preservare la correlazione seriale.
    
    Args:
        trades_pnl (np.ndarray): Array dei profitti/perdite dei trade reali.
        block_size (int): Dimensione del blocco di trade consecutivi da mantenere uniti.
        iterations (int): Numero di simulazioni.
        initial_capital (float): Capitale iniziale.
        
    Returns:
        np.ndarray: Matrice delle curve di equity simulate.
    """
    results = []
    n_trades = len(trades_pnl)
    
    if n_trades < block_size:
        return simple_resampling_mc(trades_pnl, iterations, initial_capital)

    for _ in range(iterations):
        simulated_trades = []
        while len(simulated_trades) < n_trades:
            # Scegli un indice di partenza casuale per il blocco
            start_idx = np.random.randint(0, n_trades - block_size + 1)
            block = trades_pnl[start_idx : start_idx + block_size]
            simulated_trades.extend(block)
        
        # Tagliamo se abbiamo superato la lunghezza originale
        simulated_trades = simulated_trades[:n_trades]
        equity_curve = initial_capital + np.cumsum(simulated_trades)
        results.append(equity_curve)
        
    return np.array(results)


def parametric_simulation_mc(mu: float, sigma: float, n_trades: int, iterations: int = 1000, initial_capital: float = 1000.0) -> np.ndarray:
    """
    Esegue una simulazione Monte Carlo parametrica basata su una distribuzione normale.
    
    Args:
        mu (float): Rendimento medio atteso per trade (PnL medio).
        sigma (float): Deviazione standard dei rendimenti (volatilità dei trade).
        n_trades (int): Numero di trade da simulare per ogni iterazione.
        iterations (int): Numero di curve di equity da generare.
        initial_capital (float): Capitale di partenza.
        
    Returns:
        np.ndarray: Matrice (iterations x n_trades) delle curve di equity simulate.
    """
    # Genera una matrice di rendimenti casuali (Normal Distribution)
    simulated_returns = np.random.normal(mu, sigma, (iterations, n_trades))
    
    # Calcola le curve di equity
    equity_curves = initial_capital + np.cumsum(simulated_returns, axis=1)
    
    # Inserisce il capitale iniziale come punto di partenza in ogni curva
    starting_column = np.full((iterations, 1), initial_capital)
    return np.hstack((starting_column, equity_curves))


def calculate_mc_stats(simulations: np.ndarray, initial_capital: float) -> dict:
    """
    Calcola metriche di rischio e rendimento aggregando i risultati di tutte le simulazioni.
    
    Args:
        simulations (np.ndarray): Matrice delle curve di equity prodotte dai moduli MC.
        initial_capital (float): Capitale di partenza per il calcolo del drawdown.
        
    Returns:
        dict: Dizionario contenente medie, probabilità di rovina e Value at Risk (VaR).
    """
    final_values = simulations[:, -1]
    
    # Calcolo Drawdown per ogni simulazione
    drawdowns = []
    for sim in simulations:
        peak = np.maximum.accumulate(sim)
        dd = (peak - sim) / peak
        drawdowns.append(np.max(dd))
    
    stats = {
        "mean_final": np.mean(final_values),
        "median_final": np.median(final_values),
        "prob_loss": np.mean(final_values < initial_capital) * 100,
        "max_drawdown_avg": np.mean(drawdowns) * 100,
        "max_drawdown_95pct": np.percentile(drawdowns, 95) * 100,
        "vaR_95": initial_capital - np.percentile(final_values, 5)
    }
    return stats

def plot_mc_results(simulations, original_equity=None):
    """Genera il grafico per il PowerPoint."""
    plt.figure(figsize=(12, 6))
    
    # Plot di tutte le simulazioni con trasparenza
    for i in range(min(len(simulations), 100)): # Mostriamo solo le prime 100 per non appesantire
        plt.plot(simulations[i], color='gray', alpha=0.1)
    
    # Plot della media
    plt.plot(np.mean(simulations, axis=0), color='blue', label='Media Simulazioni', linewidth=2)
    
    if original_equity is not None:
        plt.plot(original_equity, color='red', label='Risultato Reale (Backtest)', linewidth=2)
        
    plt.title("Simulazione Monte Carlo (Block Bootstrapping)")
    plt.xlabel("Numero di Trade")
    plt.ylabel("Capitale")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.show()

# Esempio di utilizzo (mock dati)
if __name__ == "__main__":
    # Supponiamo che questi siano i PnL dei trade estratti dal tuo bot
    mock_trades = np.random.normal(loc=2, scale=20, size=50) 
    
    print("Avvio analisi Monte Carlo...")
    sims = block_bootstrap_mc(mock_trades, block_size=5, iterations=1000)
    stats = calculate_mc_stats(sims, 1000)
    
    print(f"Probabilità di chiudere in perdita: {stats['prob_loss']:.2%}")
    print(f"Drawdown Medio atteso: {stats['max_drawdown_avg']:.2%}")
    print(f"Peggior Drawdown (95° percentile): {stats['max_drawdown_95pct']:.2%}")
    
    plot_mc_results(sims)