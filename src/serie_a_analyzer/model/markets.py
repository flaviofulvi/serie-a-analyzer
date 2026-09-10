"""Derivazione dei mercati dalla matrice dei risultati.

Nessuno di questi esiti ha un modello proprio: sono tutti somme di celle della
stessa matrice. Aggiungere un nuovo tipo di pronostico significa scrivere una
nuova selezione di celle, non addestrare un nuovo modello.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Linee Over/Under calcolate di default.
DEFAULT_TOTALS_LINES = (0.5, 1.5, 2.5, 3.5, 4.5)


@dataclass(frozen=True)
class MatchProbabilities:
    """Tutti gli esiti derivati per una singola partita."""

    home_win: float
    draw: float
    away_win: float

    # Doppia chance
    home_or_draw: float
    away_or_draw: float
    home_or_away: float

    # Over/Under, indicizzato per linea: {2.5: P(Over 2.5), ...}
    over: dict[float, float]
    under: dict[float, float]

    # Entrambe le squadre segnano
    btts: float
    no_btts: float

    clean_sheet_home: float
    clean_sheet_away: float

    expected_goals_home: float
    expected_goals_away: float
    expected_total_goals: float

    # Risultati esatti piu' probabili: [((gol_casa, gol_trasferta), prob), ...]
    top_scorelines: list[tuple[tuple[int, int], float]] = field(default_factory=list)

    @property
    def most_likely_scoreline(self) -> tuple[int, int]:
        return self.top_scorelines[0][0]


def _goal_grids(size: int) -> tuple[np.ndarray, np.ndarray]:
    """Griglie dei gol di casa e trasferta, allineate alla matrice."""
    home_goals = np.arange(size).reshape(-1, 1) * np.ones((1, size))
    away_goals = np.ones((size, 1)) * np.arange(size).reshape(1, -1)
    return home_goals, away_goals


def one_x_two(matrix: np.ndarray) -> tuple[float, float, float]:
    """Probabilita' di 1, X, 2.

    La matrice e' indicizzata ``[gol_casa][gol_trasferta]``, quindi la vittoria
    interna sta sotto la diagonale (gol_casa > gol_trasferta), il pareggio sulla
    diagonale e la vittoria esterna sopra.
    """
    home = float(np.tril(matrix, -1).sum())
    draw = float(np.trace(matrix))
    away = float(np.triu(matrix, 1).sum())
    return home, draw, away


def over_under(matrix: np.ndarray, line: float) -> tuple[float, float]:
    """Probabilita' di Over e Under per una linea di gol totali.

    Le linee sono semi-intere (2.5, 3.5, ...) quindi non esiste il caso di
    rimborso: Over e Under sommano sempre a 1.
    """
    size = matrix.shape[0]
    home_goals, away_goals = _goal_grids(size)
    totals = home_goals + away_goals
    over_prob = float(matrix[totals > line].sum())
    return over_prob, 1.0 - over_prob


def both_teams_score(matrix: np.ndarray) -> float:
    """Probabilita' che entrambe le squadre segnino almeno un gol."""
    return float(matrix[1:, 1:].sum())


def winning_margin(matrix: np.ndarray, team: str, min_margin: int) -> float:
    """Probabilita' che ``team`` ('home'/'away') vinca con almeno N gol di scarto."""
    if min_margin < 1:
        raise ValueError("lo scarto minimo deve essere almeno 1")
    if team == "home":
        return float(np.tril(matrix, -min_margin).sum())
    if team == "away":
        return float(np.triu(matrix, min_margin).sum())
    raise ValueError(f"squadra sconosciuta: {team!r} (attesi 'home' o 'away')")


def expected_goals(matrix: np.ndarray) -> tuple[float, float]:
    """Gol attesi delle due squadre secondo la matrice normalizzata.

    Riletti dalla matrice invece che dai lambda del modello: dopo troncatura,
    correzione tau e rinormalizzazione i due valori differiscono leggermente, e
    quello che conta per i pronostici e' la matrice effettivamente usata.
    """
    size = matrix.shape[0]
    home_goals, away_goals = _goal_grids(size)
    return float((matrix * home_goals).sum()), float((matrix * away_goals).sum())


def team_goal_distribution(matrix: np.ndarray, team: str) -> np.ndarray:
    """P(la squadra segna k gol), per k = 0, 1, 2...: le righe o le colonne sommate."""
    if team == "home":
        return matrix.sum(axis=1)
    if team == "away":
        return matrix.sum(axis=0)
    raise ValueError(f"squadra sconosciuta: {team!r} (attesi 'home' o 'away')")


def player_scores(matrix: np.ndarray, team: str, share: float) -> float:
    """P(che un giocatore segni almeno un gol), se gli spetta ``share`` dei gol della squadra.

    Anche questo e' una somma di celle, pesata. Ogni gol della squadra e' suo
    con probabilita' ``share``, indipendentemente dagli altri, quindi in un
    risultato con k gol della sua squadra segna almeno una volta con
    probabilita' ``1 - (1 - share)^k``. Si somma quel peso su tutte le celle.

    E' la forma che tiene il giocatore agganciato alla partita: non puo'
    segnare in un risultato in cui la sua squadra non segna, e se la matrice
    da' alla squadra piu' gol, ne da' di piu' anche a lui. Una formula a parte
    — un tasso per minuto, per esempio — direbbe "segna al 40%" in una
    partita in cui la stessa pagina da' alla sua squadra il 45% di restare a
    zero, e i due numeri litigherebbero.
    """
    if not 0.0 <= share <= 1.0:
        raise ValueError(f"la quota dei gol va da 0 a 1 (ricevuto {share})")
    gol = team_goal_distribution(matrix, team)
    k = np.arange(gol.size)
    return float((gol * (1.0 - (1.0 - share) ** k)).sum())


def top_scorelines(matrix: np.ndarray, n: int = 5) -> list[tuple[tuple[int, int], float]]:
    """Gli ``n`` risultati esatti piu' probabili, in ordine decrescente."""
    flat = matrix.flatten()
    # argpartition trova gli n maggiori senza ordinare l'intera matrice.
    idx = np.argpartition(flat, -n)[-n:]
    idx = idx[np.argsort(-flat[idx])]
    size = matrix.shape[0]
    return [((int(i // size), int(i % size)), float(flat[i])) for i in idx]


def derive_markets(
    matrix: np.ndarray,
    lines: tuple[float, ...] = DEFAULT_TOTALS_LINES,
    n_scorelines: int = 5,
) -> MatchProbabilities:
    """Calcola in un colpo solo tutti gli esiti derivabili dalla matrice."""
    home, draw, away = one_x_two(matrix)
    over = {}
    under = {}
    for line in lines:
        over[line], under[line] = over_under(matrix, line)

    btts = both_teams_score(matrix)
    xg_home, xg_away = expected_goals(matrix)

    return MatchProbabilities(
        home_win=home,
        draw=draw,
        away_win=away,
        home_or_draw=home + draw,
        away_or_draw=away + draw,
        home_or_away=home + away,
        over=over,
        under=under,
        btts=btts,
        no_btts=1.0 - btts,
        # Clean sheet di casa = l'ospite non segna = prima colonna.
        clean_sheet_home=float(matrix[:, 0].sum()),
        clean_sheet_away=float(matrix[0, :].sum()),
        expected_goals_home=xg_home,
        expected_goals_away=xg_away,
        expected_total_goals=xg_home + xg_away,
        top_scorelines=top_scorelines(matrix, n_scorelines),
    )
