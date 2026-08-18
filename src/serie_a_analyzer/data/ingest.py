"""Orchestrazione dell'ingestione dati.

Il progetto vive con 100 richieste al giorno, quindi l'ingestione e' divisa in
due velocita' molto diverse:

* **Modello di squadra — praticamente gratuito.** L'endpoint ``/fixtures`` con
  lega e stagione restituisce tutte le ~380 partite con i risultati in UNA
  richiesta, nomi delle squadre inclusi. Tre stagioni di addestramento costano
  tre richieste: il motore e' alimentabile il primo giorno.

* **Strato giocatori — costoso.** Le statistiche per giocatore richiedono una
  richiesta per partita: una stagione intera vale quasi quattro giorni di
  budget. Si riempie con una coda di backfill che consuma solo cio' che avanza,
  senza mai intaccare la riserva delle richieste critiche.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..model.dixon_coles import (
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_REGULARIZATION,
    FittedModel,
    Match,
    fit_dixon_coles,
)
from .budget import BudgetExhausted, Priority
from .client import ApiFootballClient
from .parsers import FixtureRecord, parse_fixtures, to_model_matches

logger = logging.getLogger(__name__)


@dataclass
class SeasonData:
    """Le partite di una stagione, gia' interpretate."""

    season: int
    fixtures: list[FixtureRecord]

    @property
    def finished(self) -> list[FixtureRecord]:
        return [f for f in self.fixtures if f.is_finished]

    @property
    def scheduled(self) -> list[FixtureRecord]:
        return [f for f in self.fixtures if f.status == "scheduled"]


@dataclass
class TrainingSet:
    """Materiale pronto per la stima del modello."""

    matches: list[Match]
    team_names: dict[int, str]
    seasons: list[SeasonData] = field(default_factory=list)
    requests_spent: int = 0

    @property
    def n_finished(self) -> int:
        return len(self.matches)


def collect_seasons(
    client: ApiFootballClient,
    seasons: tuple[int, ...] | list[int],
    league: int | None = None,
) -> TrainingSet:
    """Scarica le stagioni indicate: una richiesta ciascuna.

    I nomi delle squadre vengono ricavati dalla stessa risposta di
    ``/fixtures``, senza chiamare ``/teams``: e' una richiesta risparmiata per
    stagione, cioe' il 3% del budget giornaliero.

    Una stagione non coperta dal piano (o che restituisce zero partite) viene
    segnalata e saltata, senza interrompere le altre.
    """
    prima = client.budget.used
    tutte: list[SeasonData] = []
    nomi: dict[int, str] = {}

    for season in seasons:
        kwargs = {"league": league} if league is not None else {}
        try:
            risposta = client.fixtures(season=season, **kwargs)
        except BudgetExhausted:
            logger.warning(
                "budget esaurito: la stagione %s e le successive non sono state scaricate",
                season,
            )
            break

        fixtures = parse_fixtures(risposta)
        if not fixtures:
            # Sul piano gratuito alcune stagioni non sono incluse: la risposta
            # arriva vuota invece di dare errore.
            logger.warning(
                "stagione %s: nessuna partita restituita. "
                "Probabilmente non e' coperta dal tuo piano.",
                season,
            )
            continue

        for f in fixtures:
            nomi.setdefault(f.home_team_id, f.home_team_name)
            nomi.setdefault(f.away_team_id, f.away_team_name)

        tutte.append(SeasonData(season=season, fixtures=fixtures))
        logger.info(
            "stagione %s: %d partite (%d concluse)",
            season, len(fixtures), len([f for f in fixtures if f.is_finished]),
        )

    concluse = [f for s in tutte for f in s.finished]
    return TrainingSet(
        matches=to_model_matches(concluse, nomi),
        team_names=nomi,
        seasons=tutte,
        requests_spent=client.budget.used - prima,
    )


def fit_from_api(
    client: ApiFootballClient,
    seasons: tuple[int, ...] | list[int],
    league: int | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    regularization: float = DEFAULT_REGULARIZATION,
) -> tuple[FittedModel, TrainingSet]:
    """Scarica le stagioni e stima il modello. Il percorso piu' corto ai dati veri."""
    training = collect_seasons(client, seasons, league)
    if not training.matches:
        raise RuntimeError(
            "nessuna partita conclusa disponibile: impossibile stimare il modello. "
            "Verifica che le stagioni configurate siano coperte dal tuo piano."
        )
    modello = fit_dixon_coles(
        training.matches,
        half_life_days=half_life_days,
        regularization=regularization,
    )
    return modello, training


def next_matchday(
    training: TrainingSet, now: datetime | None = None
) -> list[FixtureRecord]:
    """Le partite della prossima giornata da giocare.

    Non si filtra per numero di giornata: in Serie A una giornata si spalma da
    venerdi' a lunedi' e i recuperi possono slittare di mesi, quindi "prossima
    giornata" significa il primo blocco di partite in programma, non un numero.
    """
    adesso = now or datetime.now(timezone.utc)
    future = [
        f
        for s in training.seasons
        for f in s.scheduled
        if f.kickoff_utc > adesso
    ]
    if not future:
        return []

    future.sort(key=lambda f: f.kickoff_utc)
    # Tutte le partite entro una finestra di 5 giorni dalla prima in programma:
    # copre il consueto venerdi'-lunedi' senza inglobare la giornata successiva.
    prima = future[0].kickoff_utc
    return [f for f in future if (f.kickoff_utc - prima).days < 5]


def backfill_candidates(training: TrainingSet, already_have: set[int]) -> list[int]:
    """Partite da cui mancano ancora le statistiche per giocatore.

    Ordinate dalla piu' recente: se il budget consente solo poche richieste al
    giorno, conviene avere prima i dati che pesano di piu' nel modello, cioe'
    quelli recenti.
    """
    concluse = [f for s in training.seasons for f in s.finished]
    concluse.sort(key=lambda f: f.kickoff_utc, reverse=True)
    return [f.api_id for f in concluse if f.api_id not in already_have]


def run_backfill(
    client: ApiFootballClient,
    fixture_ids: list[int],
    max_requests: int | None = None,
) -> dict[int, list]:
    """Consuma la coda di backfill finche' il budget lo permette.

    Si ferma da sola quando la riserva delle priorita' alte viene raggiunta:
    e' lo scopo per cui la riserva esiste.
    """
    raccolte: dict[int, list] = {}
    for i, fixture_id in enumerate(fixture_ids):
        if max_requests is not None and i >= max_requests:
            break
        if not client.budget.can_spend(Priority.BACKFILL):
            logger.info(
                "backfill fermato dopo %d partite: budget di bassa priorita' esaurito", i
            )
            break
        try:
            raccolte[fixture_id] = client.fixture_players(fixture_id)
        except BudgetExhausted:
            break
    return raccolte
