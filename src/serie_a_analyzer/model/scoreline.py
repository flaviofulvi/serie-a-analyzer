"""Costruzione della matrice dei risultati esatti.

Questo modulo produce l'oggetto centrale di tutto il progetto: la distribuzione
di probabilita' congiunta sui risultati esatti di una partita.

    matrice[x][y] = P(la squadra di casa segna x gol E la squadra ospite ne segna y)

Ogni altro pronostico (1X2, Over/Under, Gol/NoGol, handicap, clean sheet, ...)
si ottiene sommando sottoinsiemi di celle di questa matrice. Un'unica sorgente
significa che i pronostici derivati sono coerenti fra loro per costruzione:
e' impossibile che l'1X2 contraddica l'Over/Under, perche' vengono dallo stesso
oggetto.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import poisson

# Oltre gli 8 gol per squadra la probabilita' e' trascurabile anche per le
# partite piu' sbilanciate; teniamo un margine.
DEFAULT_MAX_GOALS = 10


def tau_correction(lam: float, mu: float, rho: float, size: int) -> np.ndarray:
    """Correzione di Dixon-Coles sui risultati bassi.

    Due Poisson indipendenti sottostimano sistematicamente 0-0, 1-1 e in genere
    i risultati con pochi gol: nel calcio reale i gol delle due squadre non sono
    indipendenti (il punteggio corrente cambia l'atteggiamento delle squadre).

    La correzione agisce solo sulle quattro celle con al massimo un gol per
    squadra. Con ``rho`` negativo (il caso empirico nel calcio) i pareggi bassi
    vengono alzati e 1-0 / 0-1 abbassati.
    """
    tau = np.ones((size, size))
    tau[0, 0] = 1.0 - lam * mu * rho
    tau[0, 1] = 1.0 + lam * rho
    tau[1, 0] = 1.0 + mu * rho
    tau[1, 1] = 1.0 - rho
    # Per valori estremi di rho la correzione puo' rendere negativa una cella:
    # la matrice deve restare una distribuzione di probabilita' valida.
    return np.clip(tau, 1e-10, None)


def scoreline_matrix(
    lam: float,
    mu: float,
    rho: float = 0.0,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> np.ndarray:
    """Matrice dei risultati esatti a partire dai gol attesi delle due squadre.

    Args:
        lam: gol attesi della squadra di casa.
        mu: gol attesi della squadra ospite.
        rho: parametro di correlazione di Dixon-Coles (0 = Poisson indipendenti).
        max_goals: numero massimo di gol per squadra rappresentato.

    Returns:
        Matrice ``(max_goals + 1, max_goals + 1)`` che somma esattamente a 1,
        indicizzata come ``[gol_casa][gol_trasferta]``.
    """
    if lam <= 0 or mu <= 0:
        raise ValueError(f"i gol attesi devono essere positivi (lam={lam}, mu={mu})")

    size = max_goals + 1
    goals = np.arange(size)
    joint = np.outer(poisson.pmf(goals, lam), poisson.pmf(goals, mu))
    joint = joint * tau_correction(lam, mu, rho, size)

    # La troncatura a max_goals e la correzione tau fanno perdere massa: la
    # rinormalizzazione la ridistribuisce e garantisce che somma == 1.
    total = joint.sum()
    if total <= 0:
        raise ValueError("matrice degenere: impossibile normalizzare")
    return joint / total
