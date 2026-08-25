"""Registro dei pronostici: si inserisce, non si aggiorna mai.

E' la regola non negoziabile del progetto. Se un pronostico potesse essere
riscritto dopo aver visto il risultato, lo storico di accuratezza varrebbe
zero — e nessuno se ne accorgerebbe, perche' un numero sbagliato ha lo stesso
aspetto di uno giusto. Rigenerare un pronostico significa quindi inserire una
riga nuova, mai toccare quella vecchia.

Qui la regola non e' affidata alla buona volonta' di chi scrive il codice:
`UPDATE` e `DELETE` su pronostici e mercati sono **vietati dal database**, con
trigger che sollevano un errore. Un domani un refactoring distratto fallisce
subito e rumorosamente, invece di corrompere lo storico in silenzio.

Due scelte meritano una spiegazione.

**Si registra solo cio' che non e' ancora accaduto.** Un pronostico su una
partita gia' giocata sarebbe barare: quella partita e' fra i dati su cui il
modello e' stato stimato. Il registro rifiuta le partite concluse.

**Si registra una riga per partita e per versione del modello.** Il web
produce pronostici a ogni visita, ma finche' il modello e' lo stesso quei
pronostici sono identici: salvarli tutti gonfierebbe l'archivio senza
aggiungere informazione. Quando arrivano nuovi dati il modello viene ristimato,
la sua impronta cambia, e il nuovo pronostico si affianca al vecchio senza
cancellarlo. Cosi' resta leggibile anche *come* la previsione e' cambiata
avvicinandosi alla partita.

Il database e' SQLite in `data/pronostici.db`: sta accanto al resto dei dati,
non chiede infrastruttura e regge comodamente una stagione. `data/schema.sql`
descrive le stesse tabelle in Postgres, per il giorno in cui si passasse a
Supabase.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from ..model.dixon_coles import FittedModel
from ..model.markets import MatchProbabilities, derive_markets

logger = logging.getLogger(__name__)

# Le due modalita' di pronostico previste dal progetto: prima che escano le
# formazioni ("secco") e dopo che sono ufficiali.
MODE_SECCO = "secco"
MODE_UFFICIALE = "formazione_ufficiale"
MODES = (MODE_SECCO, MODE_UFFICIALE)

# Quanti risultati esatti conservare fra i mercati derivati.
N_RISULTATI_ESATTI = 5

SCHEMA = """
create table if not exists model_versions (
    id           integer primary key,
    name         text    not null,
    version      text    not null,
    -- Impronta dei parametri stimati: due stime diverse dello stesso codice
    -- sono due versioni diverse, ed e' cio' che distingue i pronostici di
    -- lunedi' da quelli di venerdi'.
    fingerprint  text    not null unique,
    fitted_at    text    not null,
    n_matches    integer,
    config       text    not null,
    params       text    not null
);

create table if not exists predictions (
    id               integer primary key,
    fixture_id       integer not null,
    model_version_id integer not null references model_versions(id),
    mode             text    not null check (mode in ('secco', 'formazione_ufficiale')),
    created_at       text    not null,
    kickoff_utc      text    not null,
    home_team_id     integer not null,
    away_team_id     integer not null,
    lambda_home      real    not null,
    lambda_away      real    not null,
    rho              real    not null,
    -- La matrice dei risultati esatti serializzata. Da questa si riderivano
    -- tutti i mercati anche a distanza di mesi, esattamente come erano allora.
    scoreline        text    not null,
    unique (fixture_id, model_version_id, mode)
);

create index if not exists idx_predictions_fixture
    on predictions(fixture_id, created_at desc);

create table if not exists prediction_markets (
    id            integer primary key,
    prediction_id integer not null references predictions(id),
    market        text    not null,
    selection     text    not null,
    probability   real    not null check (probability between 0 and 1),
    unique (prediction_id, market, selection)
);

create index if not exists idx_pmarkets_lookup
    on prediction_markets(prediction_id, market);

-- Il collegamento pronostico <-> realta', popolato a partita finita. Questa
-- si', puo' essere scritta dopo: e' un fatto osservato, non una previsione.
create table if not exists prediction_outcomes (
    prediction_market_id integer primary key references prediction_markets(id),
    hit                  integer not null,
    brier                real    not null,
    resolved_at          text    not null
);

-- Le quote del mercato: il metro di paragone del modello. Non sono un
-- pronostico ma un'osservazione, e si conservano tutte le letture: la
-- "chiusura" e' l'ultima prima del calcio d'inizio, non un campo che l'API
-- dichiari.
create table if not exists odds (
    id            integer primary key,
    fixture_id    integer not null,
    bookmaker     text    not null,
    market        text    not null,
    selection     text    not null,
    decimal_odds  real    not null check (decimal_odds > 1),
    -- Probabilita' implicita gia' ripulita dal margine del banco.
    probability   real    not null check (probability between 0 and 1),
    kickoff_utc   text    not null,
    collected_at  text    not null,
    -- Minuti che mancavano al fischio d'inizio: ordina le letture e rende
    -- riconoscibile la chiusura anche se una raccolta e' saltata.
    minuti_al_via integer not null,
    unique (fixture_id, bookmaker, market, selection, collected_at)
);

create index if not exists idx_odds_fixture
    on odds(fixture_id, market, minuti_al_via);

-- Chi non gioca. Come le quote e' un'osservazione, non un pronostico, e come
-- le quote si conservano tutte le letture: la lista di una partita da giocare
-- e quella della stessa partita a fine serata sono due dati diversi, e solo il
-- primo era conoscibile prima del fischio d'inizio.
create table if not exists indisponibili (
    id            integer primary key,
    fixture_id    integer not null,
    player_id     integer not null,
    player_name   text    not null,
    team_id       integer not null,
    team_name     text    not null,
    -- 'assente' (l'API dice Missing Fixture) oppure 'in_dubbio' (Questionable).
    -- Un dubbio non e' un'assenza e non deve poter diventarlo per distrazione.
    stato         text    not null check (stato in ('assente', 'in_dubbio')),
    motivo        text    not null check (motivo in ('infortunio', 'squalifica', 'altro')),
    -- Il testo come l'ha scritto l'API: la classificazione qui sopra e' una
    -- comodita', questa e' la fonte.
    descrizione   text    not null,
    kickoff_utc   text    not null,
    collected_at  text    not null,
    -- Minuti che mancavano al via, negativi se la lettura e' successiva.
    minuti_al_via integer not null,
    unique (fixture_id, player_id, collected_at)
);

create index if not exists idx_indisponibili_fixture
    on indisponibili(fixture_id, minuti_al_via);

-- I pronostici sono immutabili, e non per convenzione: il database rifiuta.
create trigger if not exists predictions_sono_immutabili
before update on predictions
begin
    select raise(abort, 'un pronostico non si aggiorna: inseriscine uno nuovo');
end;

create trigger if not exists predictions_non_si_cancellano
before delete on predictions
begin
    select raise(abort, 'un pronostico non si cancella: lo storico vale solo se completo');
end;

create trigger if not exists mercati_sono_immutabili
before update on prediction_markets
begin
    select raise(abort, 'un mercato derivato non si aggiorna: segue il suo pronostico');
end;

create trigger if not exists mercati_non_si_cancellano
before delete on prediction_markets
begin
    select raise(abort, 'un mercato derivato non si cancella');
end;
"""


class PredizioneRifiutata(RuntimeError):
    """Il pronostico non poteva essere registrato, e il motivo conta."""


def _adesso() -> datetime:
    return datetime.now(timezone.utc)


def _impronta(model: FittedModel, name: str, version: str) -> str:
    """Identifica una stima. Stessi dati e stesso codice: stessa impronta.

    Serve a rispondere alla domanda "e' lo stesso modello di prima?" senza
    confrontare centinaia di parametri a mano — ed e' la chiave che evita di
    riscrivere lo stesso pronostico a ogni visita alla pagina.
    """
    corpo = {
        "name": name,
        "version": version,
        "base": round(model.base, 9),
        "home_advantage": round(model.home_advantage, 9),
        "rho": round(model.rho, 9),
        # La compressione dei gol e la media verso cui comprime non entrano
        # nella stima ma cambiano ogni probabilita' che ne esce: due modelli
        # con le stesse forze e compressioni diverse dicono cose diverse, e
        # devono restare due versioni distinte. Senza, un pronostico vecchio
        # sembrerebbe prodotto dal modello di oggi.
        "goal_compression": round(getattr(model, "goal_compression", 1.0), 9),
        "league_total_goals": round(getattr(model, "league_total_goals", 0.0), 9),
        "n_matches": model.n_matches,
        "attack": {t: round(model.attack[t], 9) for t in sorted(model.attack)},
        "defense": {t: round(model.defense[t], 9) for t in sorted(model.defense)},
    }
    testo = json.dumps(corpo, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(testo.encode("utf-8")).hexdigest()


def righe_mercati(prob: MatchProbabilities) -> list[tuple[str, str, float]]:
    """Gli esiti da materializzare accanto al pronostico.

    Sono tutti somme di celle della stessa matrice — nessuno ha una formula
    propria — ma averli in tabella evita di ricostruire la matrice ogni volta
    che si vuole misurare la calibrazione di un singolo mercato.
    """
    righe: list[tuple[str, str, float]] = [
        ("1x2", "home", prob.home_win),
        ("1x2", "draw", prob.draw),
        ("1x2", "away", prob.away_win),
        ("doppia_chance", "home_or_draw", prob.home_or_draw),
        ("doppia_chance", "away_or_draw", prob.away_or_draw),
        ("doppia_chance", "home_or_away", prob.home_or_away),
        ("btts", "yes", prob.btts),
        ("btts", "no", prob.no_btts),
        ("clean_sheet", "home", prob.clean_sheet_home),
        ("clean_sheet", "away", prob.clean_sheet_away),
    ]
    for linea, p in sorted(prob.over.items()):
        righe.append(("over_under", f"over_{linea}", p))
        righe.append(("over_under", f"under_{linea}", prob.under[linea]))
    for (casa, ospite), p in prob.top_scorelines[:N_RISULTATI_ESATTI]:
        righe.append(("risultato_esatto", f"{casa}-{ospite}", p))
    return righe


@dataclass
class PredictionStore:
    """Lo storico dei pronostici. Append-only per costruzione."""

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connessione() as conn:
            conn.executescript(SCHEMA)

    def _connessione(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        return conn

    # ---- versioni del modello ---------------------------------------------

    def versione_modello(
        self,
        model: FittedModel,
        name: str = "dixon_coles",
        version: str = "0.1.0",
        config: dict[str, Any] | None = None,
    ) -> int:
        """L'id della versione, registrandola se e' la prima volta che si vede."""
        impronta = _impronta(model, name, version)
        parametri = {
            "base": model.base,
            "home_advantage": model.home_advantage,
            "rho": model.rho,
            "attack": model.attack,
            "defense": model.defense,
            "teams": list(model.teams),
            "converged": model.converged,
            "effective_sample_size": model.effective_sample_size,
        }
        configurazione = config or {
            "half_life_days": getattr(model, "half_life_days", None),
            "regularization": getattr(model, "regularization", None),
            "goal_compression": getattr(model, "goal_compression", None),
            "league_total_goals": getattr(model, "league_total_goals", None),
        }

        with self._connessione() as conn:
            riga = conn.execute(
                "select id from model_versions where fingerprint = ?", (impronta,)
            ).fetchone()
            if riga:
                return int(riga["id"])
            cur = conn.execute(
                "insert into model_versions "
                "(name, version, fingerprint, fitted_at, n_matches, config, params) "
                "values (?, ?, ?, ?, ?, ?, ?)",
                (
                    name, version, impronta, model.fitted_on.isoformat(),
                    model.n_matches,
                    json.dumps(configurazione, ensure_ascii=False),
                    json.dumps(parametri, ensure_ascii=False),
                ),
            )
            logger.info("nuova versione del modello registrata (%s)", impronta[:12])
            return int(cur.lastrowid)

    # ---- pronostici --------------------------------------------------------

    def registra(
        self,
        *,
        fixture_id: int,
        kickoff_utc: datetime,
        home_team_id: int,
        away_team_id: int,
        model_version_id: int,
        matrice: np.ndarray,
        probabilita: MatchProbabilities,
        lambda_home: float,
        lambda_away: float,
        rho: float,
        mode: str = MODE_SECCO,
        now: datetime | None = None,
    ) -> int | None:
        """Registra un pronostico. ``None`` se ce n'era gia' uno identico.

        "Identico" vuol dire: stessa partita, stessa modalita', stessa versione
        del modello. In quel caso il pronostico non e' cambiato e riscriverlo
        aggiungerebbe righe, non informazione.

        Rifiuta le partite gia' iniziate: pronosticare il passato non e'
        pronosticare.
        """
        if mode not in MODES:
            raise PredizioneRifiutata(f"modalita' sconosciuta: {mode!r}")

        adesso = now or _adesso()
        if kickoff_utc <= adesso:
            raise PredizioneRifiutata(
                f"la partita {fixture_id} e' gia' iniziata ({kickoff_utc.isoformat()}): "
                "un pronostico registrato dopo il fischio d'inizio non vale nulla"
            )

        with self._connessione() as conn:
            gia_presente = conn.execute(
                "select id from predictions "
                "where fixture_id = ? and model_version_id = ? and mode = ?",
                (fixture_id, model_version_id, mode),
            ).fetchone()
            if gia_presente:
                return None

            cur = conn.execute(
                "insert into predictions (fixture_id, model_version_id, mode, created_at, "
                "kickoff_utc, home_team_id, away_team_id, lambda_home, lambda_away, rho, scoreline) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fixture_id, model_version_id, mode, adesso.isoformat(timespec="seconds"),
                    kickoff_utc.isoformat(), home_team_id, away_team_id,
                    float(lambda_home), float(lambda_away), float(rho),
                    json.dumps(np.asarray(matrice).round(9).tolist()),
                ),
            )
            prediction_id = int(cur.lastrowid)
            conn.executemany(
                "insert into prediction_markets (prediction_id, market, selection, probability) "
                "values (?, ?, ?, ?)",
                [
                    (prediction_id, mercato, selezione, float(min(max(p, 0.0), 1.0)))
                    for mercato, selezione, p in righe_mercati(probabilita)
                ],
            )
            return prediction_id

    # ---- lettura -----------------------------------------------------------

    def ultimo_per_partita(self, fixture_id: int, mode: str = MODE_SECCO) -> dict | None:
        with self._connessione() as conn:
            riga = conn.execute(
                "select * from predictions where fixture_id = ? and mode = ? "
                "order by created_at desc, id desc limit 1",
                (fixture_id, mode),
            ).fetchone()
            return dict(riga) if riga else None

    def mercati(self, prediction_id: int) -> list[dict]:
        with self._connessione() as conn:
            return [
                dict(r) for r in conn.execute(
                    "select market, selection, probability from prediction_markets "
                    "where prediction_id = ? order by market, selection",
                    (prediction_id,),
                )
            ]

    def matrice(self, prediction_id: int) -> np.ndarray | None:
        """La matrice dei risultati com'era al momento del pronostico."""
        with self._connessione() as conn:
            riga = conn.execute(
                "select scoreline from predictions where id = ?", (prediction_id,)
            ).fetchone()
        return np.array(json.loads(riga["scoreline"])) if riga else None

    def storico(self, fixture_id: int) -> list[dict]:
        """Tutti i pronostici mai fatti su una partita, dal piu' recente."""
        with self._connessione() as conn:
            return [
                dict(r) for r in conn.execute(
                    "select p.*, m.fingerprint, m.version from predictions p "
                    "join model_versions m on m.id = p.model_version_id "
                    "where p.fixture_id = ? order by p.created_at desc, p.id desc",
                    (fixture_id,),
                )
            ]

    def ultimi(self, limit: int = 20) -> list[dict]:
        with self._connessione() as conn:
            return [
                dict(r) for r in conn.execute(
                    "select p.*, m.version from predictions p "
                    "join model_versions m on m.id = p.model_version_id "
                    "order by p.created_at desc, p.id desc limit ?",
                    (limit,),
                )
            ]

    # ---- esiti -------------------------------------------------------------
    #
    # Un esito e' un fatto osservato, non una previsione: puo' essere scritto
    # dopo la partita. Si scrive pero' una volta sola, e la logica di cosa
    # significhi "verificato" sta in `outcomes.py`: qui c'e' solo il deposito.

    def partite_da_risolvere(self) -> list[int]:
        """Le partite con almeno un mercato pronosticato e non ancora risolto."""
        with self._connessione() as conn:
            return [
                int(r[0]) for r in conn.execute(
                    "select distinct p.fixture_id from predictions p "
                    "join prediction_markets pm on pm.prediction_id = p.id "
                    "left join prediction_outcomes po "
                    "  on po.prediction_market_id = pm.id "
                    "where po.prediction_market_id is null"
                )
            ]

    def mercati_da_risolvere(self, fixture_id: int) -> list[dict]:
        with self._connessione() as conn:
            return [
                dict(r) for r in conn.execute(
                    "select pm.id, pm.market, pm.selection, pm.probability "
                    "from prediction_markets pm "
                    "join predictions p on p.id = pm.prediction_id "
                    "left join prediction_outcomes po "
                    "  on po.prediction_market_id = pm.id "
                    "where p.fixture_id = ? and po.prediction_market_id is null",
                    (fixture_id,),
                )
            ]

    def salva_esiti(
        self,
        righe: Iterable[tuple[int, bool, float]],
        now: datetime | None = None,
    ) -> int:
        """Registra ``(id_mercato, verificato, brier)``. Non riscrive mai.

        ``insert or ignore``: ripassare sulle stesse partite non ricalcola
        nulla, e un esito gia' registrato resta quello che era.
        """
        istante = (now or _adesso()).isoformat(timespec="seconds")
        with self._connessione() as conn:
            prima = conn.total_changes
            conn.executemany(
                "insert or ignore into prediction_outcomes "
                "(prediction_market_id, hit, brier, resolved_at) values (?, ?, ?, ?)",
                [(pm_id, int(hit), float(b), istante) for pm_id, hit, b in righe],
            )
            return conn.total_changes - prima

    def esiti_risolti(
        self,
        market: str | None = None,
        selection: str | None = None,
        mode: str | None = None,
    ) -> list[dict]:
        """Gli esiti gia' confrontati con la realta', pronti per le metriche.

        ``mode`` non e' un filtro come gli altri due, ed e' il motivo per cui
        la colonna viaggia **sempre** insieme alla riga anche quando non si
        filtra. Una partita puo' avere due pronostici — quello secco e quello
        rifatto con le formazioni ufficiali (`data/ufficiali.py`) — e sono due
        risposte diverse alla stessa domanda, non due osservazioni della
        stessa cosa. Sommarle in un'unica media conterebbe ogni partita due
        volte e mediarebbe fra loro proprio i due numeri che il progetto vuole
        confrontare: chi legge deve sapere quale sta guardando.
        """
        condizioni, parametri = [], []
        if market is not None:
            condizioni.append("pm.market = ?")
            parametri.append(market)
        if selection is not None:
            condizioni.append("pm.selection = ?")
            parametri.append(selection)
        if mode is not None:
            condizioni.append("p.mode = ?")
            parametri.append(mode)
        dove = (" where " + " and ".join(condizioni)) if condizioni else ""

        with self._connessione() as conn:
            return [
                dict(r) for r in conn.execute(
                    "select pm.id, pm.prediction_id, pm.market, pm.selection, "
                    "       pm.probability, po.hit, po.brier, po.resolved_at, "
                    "       p.fixture_id, p.mode "
                    "from prediction_outcomes po "
                    "join prediction_markets pm on pm.id = po.prediction_market_id "
                    "join predictions p on p.id = pm.prediction_id" + dove,
                    parametri,
                )
            ]

    # ---- quote del mercato --------------------------------------------------

    def salva_quote(
        self,
        *,
        fixture_id: int,
        kickoff_utc: datetime,
        quote: Iterable[Any],
        now: datetime | None = None,
    ) -> int:
        """Archivia una lettura delle quote. Le letture si accumulano, non si sostituiscono."""
        adesso = now or _adesso()
        minuti = int((kickoff_utc - adesso).total_seconds() // 60)
        istante = adesso.isoformat(timespec="seconds")

        with self._connessione() as conn:
            prima = conn.total_changes
            conn.executemany(
                "insert or ignore into odds (fixture_id, bookmaker, market, selection, "
                "decimal_odds, probability, kickoff_utc, collected_at, minuti_al_via) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (fixture_id, q.bookmaker, q.market, q.selection,
                     float(q.decimal_odds), float(q.probabilita),
                     kickoff_utc.isoformat(), istante, minuti)
                    for q in quote
                ],
            )
            return conn.total_changes - prima

    def quote_di_chiusura(self, fixture_id: int | None = None) -> list[dict]:
        """L'ultima lettura prima del fischio d'inizio, per esito e bookmaker.

        "Ultima prima" e non "ultima": una lettura raccolta a partita iniziata
        — per un ritardo, un fuso sbagliato, un recupero anticipato — non e'
        una quota di chiusura e non deve entrare nel confronto.
        """
        condizione = "where o.minuti_al_via >= 0"
        parametri: list[Any] = []
        if fixture_id is not None:
            condizione += " and o.fixture_id = ?"
            parametri.append(fixture_id)

        with self._connessione() as conn:
            return [
                dict(r) for r in conn.execute(
                    "select o.fixture_id, o.bookmaker, o.market, o.selection, "
                    "       o.decimal_odds, o.probability, o.minuti_al_via "
                    "from odds o "
                    "join (select fixture_id, bookmaker, market, selection, "
                    "             min(minuti_al_via) as vicino "
                    "      from odds where minuti_al_via >= 0 "
                    "      group by fixture_id, bookmaker, market, selection) ultima "
                    "  on ultima.fixture_id = o.fixture_id "
                    " and ultima.bookmaker = o.bookmaker "
                    " and ultima.market = o.market "
                    " and ultima.selection = o.selection "
                    " and ultima.vicino = o.minuti_al_via "
                    + condizione,
                    parametri,
                )
            ]

    # ---- indisponibili ------------------------------------------------------

    def salva_indisponibili(
        self,
        *,
        fixture_id: int,
        kickoff_utc: datetime,
        voci: Iterable[Any],
        now: datetime | None = None,
    ) -> int:
        """Archivia una lettura degli indisponibili. Le letture si accumulano.

        Stessa forma di `salva_quote`, e per lo stesso motivo: cio' che conta
        non e' solo chi mancava, ma **quando lo si sapeva**.
        """
        adesso = now or _adesso()
        minuti = int((kickoff_utc - adesso).total_seconds() // 60)
        istante = adesso.isoformat(timespec="seconds")

        with self._connessione() as conn:
            prima = conn.total_changes
            conn.executemany(
                "insert or ignore into indisponibili (fixture_id, player_id, player_name, "
                "team_id, team_name, stato, motivo, descrizione, kickoff_utc, "
                "collected_at, minuti_al_via) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (fixture_id, int(v.player_id), v.player_name, int(v.team_id),
                     v.team_name, v.stato, v.motivo, v.descrizione,
                     kickoff_utc.isoformat(), istante, minuti)
                    for v in voci
                ],
            )
            return conn.total_changes - prima

    def indisponibili_alla_vigilia(self, fixture_id: int | None = None) -> list[dict]:
        """L'ultima lettura **precedente** al fischio d'inizio.

        "Precedente" e non "ultima": una lettura fatta a partita conclusa sa
        chi non e' sceso in campo davvero, e usarla per pronosticare sarebbe
        barare — la stessa ragione per cui il registro rifiuta un pronostico
        registrato dopo il via. Il backtest che volesse la lettura tardiva puo'
        interrogare la tabella da se', sapendo cosa sta guardando.

        Si prende **la lettura intera**, non il record piu' recente di ogni
        giocatore. La differenza non e' un dettaglio: una lista di
        indisponibili e' una fotografia, e un giocatore recuperato sparisce
        dalla fotografia successiva senza che nessuna riga lo dica. Cercando il
        piu' recente nome per nome, quel giocatore resterebbe infortunato per
        sempre.
        """
        condizione = "where i.minuti_al_via >= 0"
        parametri: list[Any] = []
        if fixture_id is not None:
            condizione += " and i.fixture_id = ?"
            parametri.append(fixture_id)

        with self._connessione() as conn:
            return [
                dict(r) for r in conn.execute(
                    "select i.fixture_id, i.player_id, i.player_name, i.team_id, "
                    "       i.team_name, i.stato, i.motivo, i.descrizione, i.minuti_al_via "
                    "from indisponibili i "
                    "join (select fixture_id, min(minuti_al_via) as vicino "
                    "      from indisponibili where minuti_al_via >= 0 "
                    "      group by fixture_id) ultima "
                    "  on ultima.fixture_id = i.fixture_id "
                    " and ultima.vicino = i.minuti_al_via "
                    + condizione,
                    parametri,
                )
            ]

    def partite_con_indisponibili(self) -> list[int]:
        with self._connessione() as conn:
            return [
                int(r[0]) for r in conn.execute(
                    "select distinct fixture_id from indisponibili"
                )
            ]

    def partite_quotate(self) -> list[int]:
        with self._connessione() as conn:
            return [
                int(r[0]) for r in conn.execute("select distinct fixture_id from odds")
            ]

    def conteggi(self) -> dict[str, int]:
        with self._connessione() as conn:
            def quanti(tabella: str) -> int:
                return int(conn.execute(f"select count(*) from {tabella}").fetchone()[0])
            return {
                "pronostici": quanti("predictions"),
                "versioni_modello": quanti("model_versions"),
                "mercati": quanti("prediction_markets"),
                "esiti_risolti": quanti("prediction_outcomes"),
                "quote": quanti("odds"),
                "partite_quotate": int(conn.execute(
                    "select count(distinct fixture_id) from odds"
                ).fetchone()[0]),
                "indisponibili": quanti("indisponibili"),
                "partite_con_indisponibili": int(conn.execute(
                    "select count(distinct fixture_id) from indisponibili"
                ).fetchone()[0]),
            }


def registra_partite(
    store: PredictionStore,
    model: FittedModel,
    fixtures: Iterable[Any],
    nome_squadra,
    mode: str = MODE_SECCO,
    now: datetime | None = None,
) -> dict[str, int]:
    """Registra il pronostico di ogni partita in programma. Non solleva mai.

    E' pensata per essere chiamata dal web, dove un problema del registro non
    deve impedire di mostrare la pagina: cio' che non si e' potuto salvare
    viene contato e messo a log, non propagato.
    """
    version_id = store.versione_modello(model)
    esito = {"nuovi": 0, "gia_presenti": 0, "senza_storia": 0, "gia_iniziate": 0}

    for f in fixtures:
        try:
            casa, ospite = nome_squadra(f.home_team_id), nome_squadra(f.away_team_id)
            lam, mu = model.expected_goals(casa, ospite)
            matrice = model.scoreline(casa, ospite)
        except KeyError:
            # Neopromossa senza storia: non si inventa un pronostico.
            esito["senza_storia"] += 1
            continue

        try:
            nuovo = store.registra(
                fixture_id=f.api_id,
                kickoff_utc=f.kickoff_utc,
                home_team_id=f.home_team_id,
                away_team_id=f.away_team_id,
                model_version_id=version_id,
                matrice=matrice,
                probabilita=derive_markets(matrice),
                lambda_home=lam,
                lambda_away=mu,
                rho=model.rho,
                mode=mode,
                now=now,
            )
        except PredizioneRifiutata:
            esito["gia_iniziate"] += 1
            continue
        except sqlite3.Error as exc:
            logger.warning("pronostico non registrato per la partita %s: %s", f.api_id, exc)
            continue

        esito["nuovi" if nuovo is not None else "gia_presenti"] += 1

    return esito
