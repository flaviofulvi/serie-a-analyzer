"""Unione delle due sorgenti di partite in un unico insieme di lavoro.

I dati arrivano da due strade che il piano gratuito rende profondamente
diverse:

* **lo storico** (stagioni 2022-2024) scende in una richiesta per stagione e
  non cambia mai piu';
* **la stagione in corso** si accumula un giorno alla volta nell'archivio
  locale, perche' non e' scaricabile in blocco.

Il resto dell'applicazione non deve sapere niente di questa differenza: chiede
partite e squadre, e le ottiene. Qui dentro le due strade si ricongiungono.

L'identita' di una squadra e' il suo **id numerico**, mai il nome: fra endpoint
e stagioni lo stesso club compare come "Inter" o "Internazionale", e usare il
nome come chiave lo sdoppierebbe in due entita' con meta' storia ciascuna.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..model.dixon_coles import Match
from .client import SERIE_A_LEAGUE_ID, ApiFootballClient
from .ingest import collect_seasons, current_season
from .parsers import FixtureRecord, parse_fixtures, to_model_matches
from .store import SeasonStore

logger = logging.getLogger(__name__)

# Finestra entro cui le partite in programma contano come "prossima giornata".
# In Serie A una giornata si spalma da venerdi' a lunedi'; cinque giorni la
# coprono senza inglobare quella successiva.
MATCHDAY_WINDOW_DAYS = 5


@dataclass
class Dataset:
    """Tutte le partite conosciute, da entrambe le sorgenti."""

    fixtures: list[FixtureRecord]
    team_names: dict[int, str]
    n_historical: int = 0
    n_current: int = 0
    requests_spent: int = 0
    seasons_loaded: list[int] = field(default_factory=list)

    def name(self, team_id: int) -> str:
        return self.team_names.get(team_id, str(team_id))

    @property
    def by_id(self) -> dict[int, FixtureRecord]:
        return {f.api_id: f for f in self.fixtures}

    @property
    def finished(self) -> list[FixtureRecord]:
        return [f for f in self.fixtures if f.is_finished]

    def matches(self) -> list[Match]:
        """Le partite concluse, nel formato che il modello si aspetta."""
        return to_model_matches(self.finished, self.team_names)

    def upcoming(self, now: datetime | None = None) -> list[FixtureRecord]:
        """Le partite ancora da giocare, dalla piu' vicina."""
        adesso = now or datetime.now(timezone.utc)
        future = [f for f in self.fixtures if f.status == "scheduled" and f.kickoff_utc > adesso]
        return sorted(future, key=lambda f: f.kickoff_utc)

    def next_matchday(self, now: datetime | None = None) -> list[FixtureRecord]:
        """Il primo blocco di partite in programma.

        Non si filtra per numero di giornata: i recuperi slittano di mesi e
        una "giornata" per numero conterrebbe partite lontanissime fra loro.
        """
        future = self.upcoming(now)
        if not future:
            return []
        prima = future[0].kickoff_utc
        return [f for f in future if (f.kickoff_utc - prima).days < MATCHDAY_WINDOW_DAYS]

    def recent_results(self, limit: int = 10, now: datetime | None = None) -> list[FixtureRecord]:
        """Le ultime partite concluse, dalla piu' recente."""
        return sorted(self.finished, key=lambda f: f.kickoff_utc, reverse=True)[:limit]

    def team_fixtures(self, team_id: int) -> list[FixtureRecord]:
        return sorted(
            (f for f in self.fixtures if team_id in (f.home_team_id, f.away_team_id)),
            key=lambda f: f.kickoff_utc,
        )

    def head_to_head(self, home_id: int, away_id: int) -> list[FixtureRecord]:
        """Gli scontri diretti fra due squadre, dal piu' recente."""
        coppia = {home_id, away_id}
        return sorted(
            (f for f in self.finished if {f.home_team_id, f.away_team_id} == coppia),
            key=lambda f: f.kickoff_utc,
            reverse=True,
        )


def load_dataset(
    client: ApiFootballClient,
    store: SeasonStore,
    seasons: tuple[int, ...] | list[int],
    league_id: int = SERIE_A_LEAGUE_ID,
) -> Dataset:
    """Mette insieme storico e archivio corrente.

    Le stagioni gia' concluse si servono dalla cache e non costano nulla; la
    stagione in corso si legge dall'archivio su disco e non costa nulla per
    definizione, perche' e' gia' stata pagata dalla raccolta giornaliera.

    Se una partita compare in entrambe le sorgenti vince l'archivio corrente:
    e' quello aggiornato piu' di recente.
    """
    prima = client.budget.used

    corrente = current_season()
    storiche = [s for s in seasons if s < corrente]
    training = collect_seasons(client, storiche, league_id) if storiche else None

    per_id: dict[int, FixtureRecord] = {}
    nomi: dict[int, str] = {}
    n_storiche = 0

    if training is not None:
        for stagione in training.seasons:
            for f in stagione.fixtures:
                per_id[f.api_id] = f
                n_storiche += 1
        nomi.update(training.team_names)

    # L'archivio corrente per ultimo: sovrascrive lo storico dove si sovrappone
    # e da' i nomi piu' recenti (una squadra puo' essere stata rinominata).
    correnti = parse_fixtures(list(store.load_fixtures().values()))
    for f in correnti:
        per_id[f.api_id] = f
        nomi[f.home_team_id] = f.home_team_name
        nomi[f.away_team_id] = f.away_team_name

    logger.info(
        "dataset: %d partite storiche, %d dalla raccolta giornaliera, %d squadre",
        n_storiche, len(correnti), len(nomi),
    )

    return Dataset(
        fixtures=sorted(per_id.values(), key=lambda f: f.kickoff_utc),
        team_names=nomi,
        n_historical=n_storiche,
        n_current=len(correnti),
        requests_spent=client.budget.used - prima,
        seasons_loaded=list(storiche),
    )
