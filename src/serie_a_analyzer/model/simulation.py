"""Generazione di campionati sintetici con forze note.

Serve a validare il motore prima ancora di avere dati reali: si genera una
stagione da parametri noti, si stima il modello sui risultati e si verifica che
le forze stimate riproducano quelle vere. Se il modello non ritrova forze che
sappiamo esserci, non ha senso puntarlo sui dati veri.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from .dixon_coles import Match


@dataclass(frozen=True)
class SyntheticLeague:
    """Una stagione generata, con le forze vere che l'hanno prodotta."""

    matches: list[Match]
    true_attack: dict[str, float]
    true_defense: dict[str, float]
    true_home_advantage: float
    true_base: float


def generate_league(
    n_teams: int = 20,
    n_rounds: int = 2,
    home_advantage: float = 0.25,
    base_goals: float = 1.35,
    strength_spread: float = 0.35,
    start: date = date(2024, 8, 18),
    days_between_rounds: int = 7,
    seed: int = 42,
) -> SyntheticLeague:
    """Genera un girone all'italiana con risultati campionati da Poisson.

    Args:
        n_teams: numero di squadre.
        n_rounds: 2 per andata e ritorno.
        home_advantage: fattore campo su scala logaritmica.
        base_goals: gol medi per squadra a partita.
        strength_spread: deviazione standard delle forze fra le squadre.
        seed: seme del generatore, per test riproducibili.

    Returns:
        Le partite generate e i parametri veri che le hanno prodotte.
    """
    rng = np.random.default_rng(seed)
    teams = [f"Team {i:02d}" for i in range(n_teams)]

    attack = rng.normal(0.0, strength_spread, n_teams)
    defense = rng.normal(0.0, strength_spread, n_teams)
    attack -= attack.mean()
    defense -= defense.mean()

    base = float(np.log(base_goals))

    # Accoppiamenti: ogni coppia si incontra una volta per girone, invertendo
    # casa e trasferta a gironi alterni.
    pairings: list[tuple[int, int]] = []
    for round_index in range(n_rounds):
        for i in range(n_teams):
            for j in range(i + 1, n_teams):
                pairings.append((i, j) if round_index % 2 == 0 else (j, i))

    # Le partite vengono distribuite sull'arco della stagione invece che
    # concentrate in pochi giorni: cosi' il decadimento temporale ha un effetto
    # realistico e il relativo codice viene davvero esercitato.
    rng.shuffle(pairings)
    n_matchdays = max(1, (n_teams - 1) * n_rounds)
    per_matchday = max(1, len(pairings) // n_matchdays)

    matches: list[Match] = []
    for position, (home_i, away_i) in enumerate(pairings):
        lam = np.exp(base + attack[home_i] - defense[away_i] + home_advantage)
        mu = np.exp(base + attack[away_i] - defense[home_i])
        matchday = position // per_matchday
        matches.append(
            Match(
                home=teams[home_i],
                away=teams[away_i],
                home_goals=int(rng.poisson(lam)),
                away_goals=int(rng.poisson(mu)),
                played_on=start + timedelta(days=days_between_rounds * matchday),
            )
        )

    return SyntheticLeague(
        matches=matches,
        true_attack={team: float(attack[i]) for i, team in enumerate(teams)},
        true_defense={team: float(defense[i]) for i, team in enumerate(teams)},
        true_home_advantage=home_advantage,
        true_base=base,
    )
