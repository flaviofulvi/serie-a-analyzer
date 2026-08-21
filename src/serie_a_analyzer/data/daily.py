"""Raccolta giornaliera della stagione in corso.

Il piano gratuito di API-Football espone la stagione in corso solo attraverso
una finestra scorrevole di tre giorni — ieri, oggi, domani — e solo se la
richiesta **non** filtra per lega (aggiungendo ``league`` l'API pretende anche
``season``, che per la stagione corrente e' bloccata).

Da qui il funzionamento di questo modulo:

    per ognuno dei tre giorni della finestra
        se il giorno e' passato ed era gia' completo -> non si spende nulla
        altrimenti scarica tutte le partite di quel giorno (1 richiesta)
        filtra la Serie A in locale
        salva nell'archivio
        per le partite concluse senza dettagli: formazioni, giocatori, eventi

Tre giorni invece di uno solo perche' i tre fanno lavori diversi: **domani** e'
il calendario da cui nascono i pronostici, **oggi** sono i risultati che
arrivano, **ieri** e' la rete di sicurezza per le partite che erano ancora in
corso all'ultima esecuzione.

Solo **ieri** puo' essere chiuso. Oggi e domani vengono richiesti a ogni
esecuzione, anche quando risultano vuoti: il calendario della Serie A cambia,
e un giorno dichiarato "senza partite" con un giorno di anticipo non verrebbe
mai piu' guardato. Sono circa nove richieste al giorno su cento: il prezzo di
non poter perdere un turno.

Un giorno saltato non e' recuperabile. E' il motivo per cui questo modulo
registra tutto su `DayJournal`: un buco deve essere visibile subito.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .budget import BudgetExhausted, Priority
from .cache import TTL_FOREVER, TTL_LIVE
from .client import SERIE_A_LEAGUE_ID, ApiFootballClient, ApiFootballError
from .parsers import parse_status
from .store import FINAL_STATUSES, DayJournal, SeasonStore

logger = logging.getLogger(__name__)

# Ampiezza della finestra concessa dal piano gratuito, in giorni prima e dopo
# oggi. Verificato il 18/08/2026: l'API risponde "try from 2026-08-17 to
# 2026-08-19", cioe' ieri/oggi/domani.
FREE_PLAN_WINDOW_DAYS = 1

# Dettagli raccolti per ogni partita conclusa, in ordine di importanza. Le
# formazioni servono ai pronostici, gli altri due alla scheda giocatore.
DETAIL_KINDS = ("lineups", "players", "events")

_DETAIL_PRIORITY = {
    "lineups": Priority.NORMAL,
    "players": Priority.BACKFILL,
    "events": Priority.BACKFILL,
}

# Quanti errori di fila si accettano prima di smettere. Un errore isolato e'
# normale (una partita senza tabellino, un timeout); cinque di fila vogliono
# dire che l'API e' rotta o il piano non copre l'endpoint, e insistere
# brucerebbe budget una richiesta alla volta senza salvare niente. Conta solo
# perche' il budget viene prenotato PRIMA di spedire: un errore costa comunque.
MAX_ERRORI_CONSECUTIVI = 5


@dataclass
class DayReport:
    """Esito della raccolta di un singolo giorno."""

    day: date
    skipped: bool = False
    n_serie_a: int = 0
    n_final: int = 0
    new_fixtures: int = 0
    updated_fixtures: int = 0
    details: dict[str, int] = field(default_factory=dict)
    requests_spent: int = 0
    error: str | None = None

    @property
    def closed(self) -> bool:
        return self.n_serie_a == self.n_final


def window_days(today: date | None = None, span: int = FREE_PLAN_WINDOW_DAYS) -> list[date]:
    """I giorni leggibili adesso: da ieri a domani.

    Ordinati dal piu' vecchio al piu' recente. Ieri per primo perche' e' quello
    che rischia di chiudersi definitivamente: domani sara' fuori finestra.
    """
    oggi = today or datetime.now(timezone.utc).date()
    return [oggi + timedelta(days=offset) for offset in range(-span, span + 1)]


def _is_serie_a(raw: dict, league_id: int) -> bool:
    return (raw.get("league") or {}).get("id") == league_id


def _status_of(raw: dict) -> str:
    return parse_status((raw.get("fixture") or {}).get("status"))


def collect_day(
    client: ApiFootballClient,
    store: SeasonStore,
    journal: DayJournal,
    day: date,
    league_id: int = SERIE_A_LEAGUE_ID,
    with_details: bool = True,
    max_detail_requests: int | None = None,
    ttl: int | None = TTL_LIVE,
    today: date | None = None,
) -> DayReport:
    """Raccoglie un giorno. Un giorno gia' chiuso non costa nulla.

    ``ttl`` governa quanto puo' essere vecchia la copia in cache accettata. Il
    valore di default fa da freno contro due esecuzioni ravvicinate; ``0``
    obbliga a rileggere davvero dall'API, che e' cio' che serve quando si sta
    inseguendo una partita in corso.

    ``today`` e' la data rispetto a cui si decide se un giorno puo' essere
    chiuso: oggi e domani non lo sono mai, perche' il calendario di quei giorni
    puo' ancora cambiare.
    """
    oggi = today or datetime.now(timezone.utc).date()
    report = DayReport(day=day)
    prima = client.budget.used

    if journal.is_closed(day, today=oggi):
        report.skipped = True
        logger.info("%s: gia' chiuso, nessuna richiesta", day)
        return report

    try:
        tutte = client.fixtures_on_date(day.isoformat(), ttl=ttl)
    except (ApiFootballError, BudgetExhausted) as exc:
        report.error = str(exc)
        report.requests_spent = client.budget.used - prima
        logger.error("%s: raccolta fallita — %s", day, exc)
        return report

    serie_a = [raw for raw in tutte if _is_serie_a(raw, league_id)]
    report.n_serie_a = len(serie_a)
    report.n_final = sum(1 for raw in serie_a if _status_of(raw) in FINAL_STATUSES)
    report.new_fixtures, report.updated_fixtures = store.upsert_fixtures(serie_a)

    if with_details:
        report.details = collect_details_for(
            client, store, serie_a, max_requests=max_detail_requests
        )

    report.requests_spent = client.budget.used - prima
    journal.record(
        day,
        collected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        n_fixtures=report.n_serie_a,
        n_final=report.n_final,
        requests_spent=report.requests_spent,
        today=oggi,
    )
    logger.info(
        "%s: %d partite di Serie A (%d definitive), %d richieste",
        day, report.n_serie_a, report.n_final, report.requests_spent,
    )
    return report


def collect_details_for(
    client: ApiFootballClient,
    store: SeasonStore,
    fixtures: list[dict],
    max_requests: int | None = None,
    priorita: Priority | None = None,
    pausa: float = 0.0,
) -> dict[str, int]:
    """Formazioni, statistiche giocatori ed eventi delle partite concluse.

    Solo per le partite gia' concluse: prima del fischio d'inizio i tre
    endpoint rispondono vuoto e la richiesta sarebbe sprecata. Le formazioni
    ufficiali *prima* della partita sono un lavoro diverso, che va fatto a
    ridosso del calcio d'inizio e non da una raccolta giornaliera.

    Si ferma da sola quando il budget della priorita' corrispondente finisce:
    un archivio incompleto e' recuperabile domani — gli endpoint per singola
    partita non hanno la finestra di tre giorni — mentre restare senza
    richieste durante una giornata di campionato no.

    ``priorita`` sostituisce quella naturale di ogni dettaglio. Serve al
    riempimento di una stagione passata (`data/storico.py`): li' anche le
    formazioni sono lavoro arretrato, e chiederle a priorita' NORMAL
    mangerebbe la riserva che protegge la giornata di campionato di oggi.

    ``pausa`` sono i secondi di attesa dopo ogni richiesta davvero spedita. A
    zero per la raccolta giornaliera, che di richieste ne fa nove; diversa da
    zero quando se ne fanno mille di fila, perche' i piani a pagamento hanno
    anche un limite **al minuto** e superarlo fa rispondere 429 all'API mentre
    il contatore locale continua a segnare richieste spese.
    """
    raccolti: dict[str, int] = {}
    spese = 0
    errori_di_fila = 0

    for raw in fixtures:
        if _status_of(raw) != "finished":
            continue
        fid = (raw.get("fixture") or {}).get("id")
        if fid is None:
            continue

        for kind in DETAIL_KINDS:
            if not store.needs_detail(fid, kind):
                continue
            if max_requests is not None and spese >= max_requests:
                logger.info("dettagli: raggiunto il tetto di %d richieste", max_requests)
                return raccolti

            prio = priorita if priorita is not None else _DETAIL_PRIORITY[kind]
            if not client.budget.can_spend(prio):
                logger.info("dettagli: budget %s esaurito, si riprende domani", prio.name)
                continue

            try:
                payload = client.get(
                    f"fixtures/{kind}", {"fixture": fid},
                    ttl=TTL_FOREVER, priority=prio,
                )
            except (ApiFootballError, BudgetExhausted) as exc:
                logger.warning("dettagli %s per la partita %s: %s", kind, fid, exc)
                errori_di_fila += 1
                if errori_di_fila >= MAX_ERRORI_CONSECUTIVI:
                    logger.error(
                        "dettagli: %d errori di fila, mi fermo. "
                        "Le partite gia' salvate restano, il resto si riprende dopo.",
                        errori_di_fila,
                    )
                    return raccolti
                continue

            errori_di_fila = 0
            # Si salva anche una risposta vuota: la busta conta i tentativi e
            # impedisce sia di rinunciare al primo colpo sia di riprovare per
            # sempre su un campionato che quel dato non lo pubblica.
            store.save_detail(fid, kind, payload)
            if payload:
                raccolti[kind] = raccolti.get(kind, 0) + 1
            spese += 1
            if pausa:
                time.sleep(pausa)

    return raccolti


def backfill_details(
    client: ApiFootballClient,
    store: SeasonStore,
    max_requests: int | None = None,
) -> dict[str, int]:
    """Recupera i dettagli mancanti di tutte le partite gia' in archivio.

    Esiste perche' i due limiti del piano gratuito sono di natura diversa:

    * l'elenco delle partite di un giorno e' leggibile **solo** entro la
      finestra di tre giorni — se si perde, e' perso;
    * i dettagli di una partita si leggono dal suo id, e quello non scade.

    Quindi la raccolta giornaliera non deve morire se il budget finisce a meta'
    di una giornata di campionato: basta aver catturato gli id. Il resto lo
    recupera questa funzione, il giorno dopo, con calma.

    Le partite piu' recenti per prime: sono quelle che pesano di piu' nel
    modello e quelle che l'utente sta guardando.
    """
    archivio = store.load_fixtures()
    concluse = [raw for raw in archivio.values() if _status_of(raw) == "finished"]
    concluse.sort(key=lambda raw: (raw.get("fixture") or {}).get("date") or "", reverse=True)

    in_sospeso = [
        raw for raw in concluse
        if any(
            store.needs_detail((raw.get("fixture") or {}).get("id"), kind)
            for kind in DETAIL_KINDS
        )
    ]
    if not in_sospeso:
        return {}

    logger.info("recupero dettagli: %d partite in sospeso", len(in_sospeso))
    return collect_details_for(client, store, in_sospeso, max_requests=max_requests)


def collect_window(
    client: ApiFootballClient,
    store: SeasonStore,
    journal: DayJournal,
    today: date | None = None,
    league_id: int = SERIE_A_LEAGUE_ID,
    with_details: bool = True,
    max_detail_requests: int | None = None,
    ttl: int | None = TTL_LIVE,
) -> list[DayReport]:
    """Raccoglie tutti i giorni della finestra. E' l'operazione da schedulare."""
    oggi = today or datetime.now(timezone.utc).date()
    reports = []
    for giorno in window_days(oggi):
        reports.append(
            collect_day(
                client, store, journal, giorno,
                league_id=league_id,
                with_details=with_details,
                max_detail_requests=max_detail_requests,
                ttl=ttl,
                today=oggi,
            )
        )
    return reports
