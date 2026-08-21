"""Stima delle forze di attacco e difesa con il modello di Dixon-Coles.

Parametrizzazione (tutto su scala logaritmica, cosi' i gol attesi restano
positivi per costruzione e l'ottimizzazione non ha vincoli di positivita'):

    log(gol attesi casa)      = base + attacco[casa]  - difesa[ospite] + fattore_campo
    log(gol attesi trasferta) = base + attacco[ospite] - difesa[casa]

dove ``base`` e' il livello medio di gol del campionato. Attacco e difesa sono
centrati a somma zero: valgono come scostamenti dalla media della lega, quindi
attacco = +0.30 significa "segna circa il 35% in piu' della squadra media".

Due accorgimenti separano un modello utilizzabile da uno ingenuo:

* **Decadimento temporale** — ogni partita pesa ``exp(-xi * giorni_fa)``. Una
  partita di dieci mesi fa descrive una squadra che non esiste piu'.
* **Shrinkage** — una penalita' ridge tira attacco e difesa verso la media del
  campionato. Senza, dopo tre giornate il modello e' convinto che la neopromossa
  che ha vinto due volte sia da scudetto. Con poche partite vince la media della
  lega, con molte partite vincono i dati.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

import numpy as np
from scipy.optimize import minimize

from .markets import MatchProbabilities, derive_markets
from .scoreline import DEFAULT_MAX_GOALS, scoreline_matrix

# Emivita del decadimento temporale: una partita di 6 mesi fa pesa la meta' di
# una di oggi. Valore di partenza ragionevole, da tarare col backtest.
DEFAULT_HALF_LIFE_DAYS = 180.0

# Intensita' dello shrinkage verso la media della lega.
DEFAULT_REGULARIZATION = 2.0

# rho e' negativo nel calcio reale; i limiti tengono la correzione tau valida.
RHO_BOUNDS = (-0.3, 0.1)


@dataclass(frozen=True)
class Match:
    """Una partita conclusa, unita' di osservazione del modello."""

    home: str
    away: str
    home_goals: int
    away_goals: int
    played_on: date


@dataclass(frozen=True)
class TeamEvidence:
    """Su quanta sostanza poggia la stima di una squadra.

    Il modello risponde sempre, anche su una squadra di cui non sa nulla:
    lo shrinkage la tiene incollata alla media della lega e ne esce un
    pronostico prudente, indistinguibile a occhio da uno informato. Questo
    oggetto e' cio' che rende visibile la differenza *prima* della partita,
    non dopo — l'accuratezza misurata arriva mesi piu' tardi e su decine di
    esiti, qui invece si dichiara quanto il modello sappia adesso.

    ``pull`` e' la quota di stima che viene dalla media di lega invece che
    dai dati della squadra; ``sostanza`` e' il suo complemento, cioe' quanto
    parlano davvero le partite giocate.

    Attenzione a cosa **non** dice: e' misurato alla data di riferimento
    della stima (``FittedModel.fitted_on``), che di default e' la partita
    piu' recente del campione e non il giorno di oggi. Un archivio fermo da
    un anno produce un ``pull`` basso — dal suo punto di vista i dati sono
    freschissimi — e quanto sia vecchio quel punto di vista e' un fatto a
    parte, che si legge da ``last_match``. Chi mostra questo numero deve
    mostrare anche l'altro.
    """

    team: str
    n_matches: int
    effective_sample_size: float
    last_match: date
    pull_attack: float
    pull_defense: float

    @property
    def pull(self) -> float:
        """Quanto la forza e' tirata verso la media di lega, da 0 a 1."""
        return (self.pull_attack + self.pull_defense) / 2

    @property
    def sostanza(self) -> float:
        """Quanto la stima viene dai dati della squadra, da 0 a 1."""
        return 1.0 - self.pull


@dataclass(frozen=True)
class FittedModel:
    """Forze stimate, pronte a produrre pronostici."""

    teams: tuple[str, ...]
    attack: dict[str, float]
    defense: dict[str, float]
    home_advantage: float
    base: float
    rho: float
    n_matches: int
    effective_sample_size: float
    fitted_on: date
    converged: bool
    # Iperparametri della stima. Conservarli qui e' cio' che rende una
    # versione del modello riproducibile: senza, un pronostico salvato non
    # sarebbe piu' ricostruibile a distanza di mesi.
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS
    regularization: float = DEFAULT_REGULARIZATION
    # Quanto il modello sa di ogni squadra. Non entra nell'impronta della
    # versione (`data/predictions.py`): descrive la stima, non la definisce,
    # e due modelli con gli stessi parametri restano lo stesso modello.
    evidence: dict[str, TeamEvidence] = field(default_factory=dict)

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        """Gol attesi di casa e trasferta per un accoppiamento."""
        self._check_known(home)
        self._check_known(away)
        lam = math.exp(self.base + self.attack[home] - self.defense[away] + self.home_advantage)
        mu = math.exp(self.base + self.attack[away] - self.defense[home])
        return lam, mu

    def scoreline(
        self, home: str, away: str, max_goals: int = DEFAULT_MAX_GOALS
    ) -> np.ndarray:
        """Matrice dei risultati esatti per un accoppiamento."""
        lam, mu = self.expected_goals(home, away)
        return scoreline_matrix(lam, mu, self.rho, max_goals)

    def predict(
        self, home: str, away: str, max_goals: int = DEFAULT_MAX_GOALS
    ) -> MatchProbabilities:
        """Tutti gli esiti derivati per un accoppiamento."""
        return derive_markets(self.scoreline(home, away, max_goals))

    def strength_table(self) -> list[tuple[str, float, float, float]]:
        """Squadre ordinate per forza complessiva.

        Restituisce ``(squadra, attacco, difesa, forza_netta)``. Serve alla
        spiegabilita': accanto a "Inter 62%" si mostra da dove viene quel numero.
        """
        rows = [
            (team, self.attack[team], self.defense[team], self.attack[team] + self.defense[team])
            for team in self.teams
        ]
        return sorted(rows, key=lambda row: -row[3])

    def evidence_for(self, team: str) -> TeamEvidence | None:
        """Su cosa poggia la stima di una squadra, se il modello la conosce."""
        return self.evidence.get(team)

    def evidence_table(self) -> list[TeamEvidence]:
        """Le squadre dalla piu' incerta alla piu' solida.

        L'ordine e' quello: le squadre su cui il modello sa meno sono quelle
        di cui vale la pena parlare, non le altre.
        """
        return sorted(self.evidence.values(), key=lambda e: -e.pull)

    def _check_known(self, team: str) -> None:
        if team not in self.attack:
            raise KeyError(
                f"squadra sconosciuta al modello: {team!r}. "
                f"Squadre disponibili: {', '.join(sorted(self.teams))}"
            )


def _time_weights(
    matches: Sequence[Match], as_of: date, half_life_days: float
) -> np.ndarray:
    """Peso esponenziale decrescente con l'eta' della partita."""
    if half_life_days <= 0:
        raise ValueError("l'emivita deve essere positiva")
    xi = math.log(2.0) / half_life_days
    days_ago = np.array([(as_of - m.played_on).days for m in matches], dtype=float)
    if (days_ago < 0).any():
        raise ValueError("presente una partita successiva alla data di riferimento")
    return np.exp(-xi * days_ago)


def _log_tau(
    lam: np.ndarray,
    mu: np.ndarray,
    rho: float,
    home_goals: np.ndarray,
    away_goals: np.ndarray,
) -> np.ndarray:
    """Logaritmo della correzione di Dixon-Coles, vettorizzato sulle partite."""
    tau = np.ones_like(lam)
    is_0_0 = (home_goals == 0) & (away_goals == 0)
    is_0_1 = (home_goals == 0) & (away_goals == 1)
    is_1_0 = (home_goals == 1) & (away_goals == 0)
    is_1_1 = (home_goals == 1) & (away_goals == 1)

    tau = np.where(is_0_0, 1.0 - lam * mu * rho, tau)
    tau = np.where(is_0_1, 1.0 + lam * rho, tau)
    tau = np.where(is_1_0, 1.0 + mu * rho, tau)
    tau = np.where(is_1_1, 1.0 - rho, tau)
    return np.log(np.clip(tau, 1e-10, None))


def _team_evidence(
    matches: Sequence[Match],
    weights: np.ndarray,
    lam: np.ndarray,
    mu: np.ndarray,
    regularization: float,
) -> dict[str, TeamEvidence]:
    """Quanto la forza di ogni squadra viene dai suoi dati e quanto dalla media.

    Lo shrinkage non e' una costante uguale per tutti: e' una penalita' fissa
    che compete con l'informazione portata dai dati, e quell'informazione
    cambia da squadra a squadra. Una squadra con tre stagioni recenti alle
    spalle la vince facilmente; una neopromossa, o una di cui l'ultima
    partita nota risale a un anno fa, no — e la sua forza esce quasi identica
    alla media del campionato.

    Il conto e' quello classico della stima penalizzata: attorno all'ottimo,
    verosimiglianza e penalita' si contendono il parametro in proporzione
    alla loro curvatura. Per il parametro di attacco di una squadra la
    curvatura dei dati e' ``somma(peso * gol attesi)`` sulle sue partite —
    piu' partite recenti, e piu' gol in gioco, piu' i dati pesano — mentre
    quella della penalita' e' ``2 * regularization``, sempre la stessa.
    Da qui::

        tiro verso la media = 2*reg / (curvatura dei dati + 2*reg)

    E' un'approssimazione, non un intervallo di confidenza: trascura il
    centraggio a somma zero e la correzione tau, che spostano il numero di
    poco. Serve a dire "di questa squadra il modello sa poco", e per quello
    e' abbondantemente precisa.
    """
    penalita = 2.0 * regularization
    if penalita <= 0:
        penalita = 0.0

    n_partite: dict[str, int] = {}
    peso: dict[str, float] = {}
    ultima: dict[str, date] = {}
    info_att: dict[str, float] = {}
    info_dif: dict[str, float] = {}

    for i, m in enumerate(matches):
        w = float(weights[i])
        gol_casa_attesi = float(lam[i])
        gol_ospite_attesi = float(mu[i])
        for squadra, attesi_suoi, attesi_altrui in (
            (m.home, gol_casa_attesi, gol_ospite_attesi),
            (m.away, gol_ospite_attesi, gol_casa_attesi),
        ):
            n_partite[squadra] = n_partite.get(squadra, 0) + 1
            peso[squadra] = peso.get(squadra, 0.0) + w
            if m.played_on > ultima.get(squadra, date.min):
                ultima[squadra] = m.played_on
            # L'attacco si impara dai gol che la squadra fa, la difesa da
            # quelli che subisce: due conti separati, perche' una squadra
            # puo' avere l'uno solido e l'altra no.
            info_att[squadra] = info_att.get(squadra, 0.0) + w * attesi_suoi
            info_dif[squadra] = info_dif.get(squadra, 0.0) + w * attesi_altrui

    def tiro(informazione: float) -> float:
        totale = informazione + penalita
        return penalita / totale if totale > 0 else 0.0

    return {
        squadra: TeamEvidence(
            team=squadra,
            n_matches=n_partite[squadra],
            effective_sample_size=peso[squadra],
            last_match=ultima[squadra],
            pull_attack=tiro(info_att[squadra]),
            pull_defense=tiro(info_dif[squadra]),
        )
        for squadra in n_partite
    }


def fit_dixon_coles(
    matches: Sequence[Match],
    as_of: date | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    regularization: float = DEFAULT_REGULARIZATION,
    max_iterations: int = 500,
) -> FittedModel:
    """Stima attacco, difesa, fattore campo e rho per massima verosimiglianza.

    Args:
        matches: partite concluse su cui stimare. Servono almeno un paio di
            giornate complete perche' la stima abbia senso; con meno dati lo
            shrinkage tiene le squadre vicine alla media della lega.
        as_of: data di riferimento per il decadimento temporale. Default: la
            partita piu' recente fra quelle passate.
        half_life_days: dopo quanti giorni una partita pesa la meta'.
        regularization: intensita' dello shrinkage verso la media di lega.
        max_iterations: tetto di iterazioni dell'ottimizzatore.

    Returns:
        Il modello stimato, con ``converged`` a indicare se l'ottimizzatore ha
        raggiunto la convergenza.
    """
    if not matches:
        raise ValueError("serve almeno una partita per stimare il modello")

    teams = tuple(sorted({m.home for m in matches} | {m.away for m in matches}))
    index = {team: i for i, team in enumerate(teams)}
    n_teams = len(teams)
    if n_teams < 2:
        raise ValueError("servono almeno due squadre distinte")

    reference = as_of or max(m.played_on for m in matches)
    weights = _time_weights(matches, reference, half_life_days)

    home_idx = np.array([index[m.home] for m in matches])
    away_idx = np.array([index[m.away] for m in matches])
    home_goals = np.array([m.home_goals for m in matches], dtype=float)
    away_goals = np.array([m.away_goals for m in matches], dtype=float)

    def unpack(params: np.ndarray):
        attack = params[:n_teams]
        defense = params[n_teams : 2 * n_teams]
        # Il centraggio rende i parametri identificabili: senza, si potrebbe
        # aggiungere una costante a tutti gli attacchi e sottrarla da base
        # ottenendo la stessa verosimiglianza.
        attack = attack - attack.mean()
        defense = defense - defense.mean()
        home_adv, base, rho = params[2 * n_teams], params[2 * n_teams + 1], params[2 * n_teams + 2]
        return attack, defense, home_adv, base, rho

    def negative_log_likelihood(params: np.ndarray) -> float:
        attack, defense, home_adv, base, rho = unpack(params)

        log_lam = base + attack[home_idx] - defense[away_idx] + home_adv
        log_mu = base + attack[away_idx] - defense[home_idx]
        lam = np.exp(log_lam)
        mu = np.exp(log_mu)

        # Log-verosimiglianza Poisson senza i fattoriali: sono costanti rispetto
        # ai parametri e non spostano l'ottimo.
        log_lik = (
            home_goals * log_lam
            - lam
            + away_goals * log_mu
            - mu
            + _log_tau(lam, mu, rho, home_goals, away_goals)
        )
        weighted = float(np.sum(weights * log_lik))
        penalty = regularization * float(np.sum(attack**2) + np.sum(defense**2))
        return -weighted + penalty

    initial = np.concatenate(
        [
            np.zeros(n_teams),  # attacco
            np.zeros(n_teams),  # difesa
            [0.25],  # fattore campo: circa +28% di gol attesi
            [math.log(1.35)],  # base: gol medi per squadra a partita in Serie A
            [-0.10],  # rho
        ]
    )
    bounds = (
        [(-2.0, 2.0)] * n_teams
        + [(-2.0, 2.0)] * n_teams
        + [(-1.0, 1.0), (math.log(0.3), math.log(5.0)), RHO_BOUNDS]
    )

    result = minimize(
        negative_log_likelihood,
        initial,
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": max_iterations},
    )

    attack, defense, home_adv, base, rho = unpack(result.x)

    # I gol attesi con i parametri finali: servono a pesare quanto ogni
    # partita abbia davvero insegnato qualcosa su chi l'ha giocata.
    lam_finale = np.exp(base + attack[home_idx] - defense[away_idx] + home_adv)
    mu_finale = np.exp(base + attack[away_idx] - defense[home_idx])

    return FittedModel(
        teams=teams,
        attack={team: float(attack[i]) for team, i in index.items()},
        defense={team: float(defense[i]) for team, i in index.items()},
        home_advantage=float(home_adv),
        base=float(base),
        rho=float(rho),
        n_matches=len(matches),
        effective_sample_size=float(weights.sum()),
        fitted_on=reference,
        converged=bool(result.success),
        half_life_days=half_life_days,
        regularization=regularization,
        evidence=_team_evidence(
            matches, weights, lam_finale, mu_finale, regularization
        ),
    )
