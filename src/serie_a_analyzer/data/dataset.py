"""Unione delle sorgenti di partite in un unico insieme di lavoro.

I dati arrivano da strade che il piano gratuito rende profondamente diverse:

* **lo storico** (stagioni 2022-2024) scende in una richiesta per stagione e
  non cambia mai piu';
* **la stagione in corso** si accumula un giorno alla volta nell'archivio
  locale, perche' non e' scaricabile in blocco;
* **le stagioni ricomprate** (`data/storico/<anno>/`, vedi `storico.py`), che
  esistono solo se qualcuno ha pagato un piano superiore e le ha riempite;
* **le stagioni importate da un file** (`data/importato/<anno>/`, vedi
  `importato.py`): gli stessi risultati, presi da un calendario scaricato
  invece che comprati. Hanno i gol e le date, non i tabellini.

Il resto dell'applicazione non deve sapere niente di questa differenza: chiede
partite e squadre, e le ottiene. Qui dentro le strade si ricongiungono.

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
    """Tutte le partite conosciute, da qualunque sorgente arrivino."""

    fixtures: list[FixtureRecord]
    team_names: dict[int, str]
    n_historical: int = 0
    n_current: int = 0
    n_riempite: int = 0
    n_importate: int = 0
    requests_spent: int = 0
    seasons_loaded: list[int] = field(default_factory=list)
    stagioni_riempite: list[int] = field(default_factory=list)
    stagioni_importate: list[int] = field(default_factory=list)

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
    storici: dict[int, SeasonStore] | None = None,
    importati: dict[int, SeasonStore] | None = None,
) -> Dataset:
    """Mette insieme storico, stagioni ricomprate, importate e archivio corrente.

    Le stagioni gia' concluse si servono dalla cache e non costano nulla; la
    stagione in corso si legge dall'archivio su disco e non costa nulla per
    definizione, perche' e' gia' stata pagata dalla raccolta giornaliera; le
    stagioni ricomprate si leggono dal loro archivio e sono gia' state pagate
    una volta per tutte; quelle importate da un file non sono costate niente.

    L'ordine di sovrascrittura non e' arbitrario, va **dal piu' vecchio al piu'
    fresco**: cache, poi stagioni importate, poi ricomprate, poi raccolta
    giornaliera. Se una partita compare in piu' sorgenti vince l'archivio
    corrente, che e' l'unico aggiornato giorno per giorno; e i nomi delle
    squadre restano quelli visti per ultimi, perche' un club puo' essere stato
    rinominato nel frattempo.

    Una stagione presente sia importata che ricomprata viene presa **solo dal
    dato pagato**. Non basterebbe l'ordine: le partite importate hanno id
    sintetici, quindi non verrebbero sovrascritte da quelle vere ma
    affiancate, e il campionato risulterebbe giocato due volte.
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

    storici = storici or {}
    importati = importati or {}

    n_importate = 0
    caricate_da_file: list[int] = []
    for stagione in sorted(importati):
        if stagione in storici:
            # Il dato pagato ha tabellini e id veri: l'importato non aggiunge
            # niente e raddoppierebbe le partite.
            logger.info(
                "stagione %s: importata da file ma anche ricomprata, "
                "vale il dato pagato", stagione,
            )
            continue
        letta = parse_fixtures(list(importati[stagione].load_fixtures().values()))
        for f in letta:
            per_id[f.api_id] = f
            nomi.setdefault(f.home_team_id, f.home_team_name)
            nomi.setdefault(f.away_team_id, f.away_team_name)
        n_importate += len(letta)
        caricate_da_file.append(stagione)

    n_riempite = 0
    for stagione in sorted(storici):
        riempita = parse_fixtures(list(storici[stagione].load_fixtures().values()))
        for f in riempita:
            per_id[f.api_id] = f
            nomi[f.home_team_id] = f.home_team_name
            nomi[f.away_team_id] = f.away_team_name
        n_riempite += len(riempita)

    # La raccolta giornaliera per ultima: e' l'unica sorgente che vede una
    # partita cambiare stato durante il pomeriggio.
    correnti = parse_fixtures(list(store.load_fixtures().values()))
    for f in correnti:
        per_id[f.api_id] = f
        nomi[f.home_team_id] = f.home_team_name
        nomi[f.away_team_id] = f.away_team_name

    logger.info(
        "dataset: %d partite storiche, %d da stagioni importate, %d da stagioni "
        "riempite, %d dalla raccolta giornaliera, %d squadre",
        n_storiche, n_importate, n_riempite, len(correnti), len(nomi),
    )

    return Dataset(
        fixtures=sorted(per_id.values(), key=lambda f: f.kickoff_utc),
        team_names=nomi,
        n_historical=n_storiche,
        n_current=len(correnti),
        n_riempite=n_riempite,
        n_importate=n_importate,
        requests_spent=client.budget.used - prima,
        seasons_loaded=list(storiche),
        stagioni_riempite=sorted(storici),
        stagioni_importate=caricate_da_file,
    )
