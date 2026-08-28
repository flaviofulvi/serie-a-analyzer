"""Raccolta giornaliera della stagione in corso.

Si raccolgono due cose diverse, e la differenza e' tutta qui:

* il **calendario** — chi gioca contro chi, e quando — che arriva intero in una
  richiesta (`collect_season_calendar`) e non e' deperibile: cambia con
  settimane di preavviso, per recuperi e anticipi;
* gli **stati** — una partita che comincia, un risultato che arriva, un
  tabellino che si consolida — che si leggono un giorno alla volta dentro una
  finestra di tre giorni, e fuori da quella non si recuperano piu'.

Il calendario intero pero' si paga: e' l'unica richiesta di questo modulo che
passa dal filtro ``league+season``, cioe' proprio cio' che il piano gratuito
blocca. La finestra di tre giorni no — chiede la sola data, senza ``league``,
ed e' il modo in cui la stagione in corso si e' sempre letta anche senza
pagare. Se un giorno il calendario smettesse di rispondere, tutto il resto
continuerebbe identico.

Da qui il funzionamento della raccolta giorno per giorno:

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
from typing import Any, Iterable

from .budget import BudgetExhausted, Priority
from .cache import TTL_CALENDARIO_STAGIONE, TTL_FOREVER, TTL_LIVE
from .client import SERIE_A_LEAGUE_ID, ApiFootballClient, ApiFootballError
from .ingest import current_season
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

# Un quarto dettaglio che l'archivio sa chiedere ma la raccolta giornaliera
# NON prende: `/fixtures/statistics`, cioe' i totali di squadra e soprattutto
# l'**xG**. Sta fuori da DETAIL_KINDS di proposito e la distinzione e' il punto:
# quello che la raccolta prende ogni giorno e' cio' di cui il sistema vive, e
# l'xG per ora e' un dato in prova (il lavoro 8, riaperto il 24 agosto 2026
# dopo che i tiri sono usciti piatti). Si compra per le stagioni passate con
# `riempi_storico.py --solo statistics` e si misura; se paga, allora entra
# qui — non prima, o si sarebbe raccolto ogni giorno un dato che nessuno usa.
# Nessun'altra parte del codice va toccata per chiederlo: `needs_detail` e
# `save_detail` lavorano su una stringa qualunque.
DETTAGLIO_STATISTICHE = "statistics"

# Tutti i dettagli che si sa chiedere. E' contro questo che si valida una
# scelta da riga di comando, non contro DETAIL_KINDS.
DETTAGLI_NOTI = DETAIL_KINDS + (DETTAGLIO_STATISTICHE,)

_DETAIL_PRIORITY = {
    "lineups": Priority.NORMAL,
    "players": Priority.BACKFILL,
    "events": Priority.BACKFILL,
    DETTAGLIO_STATISTICHE: Priority.BACKFILL,
}

# --- quando conviene raccogliere ------------------------------------------
#
# La raccolta a orari fissi ha un difetto che si vede solo guardando cosa
# raccoglie: le quote e gli indisponibili valgono quanto sono VICINI al
# fischio d'inizio, e un orario fisso ci passa vicino per caso. Il 22 agosto
# 2026 la corsa delle 20:00 ha letto le quote di una partita delle 20:45 (bene)
# e insieme quelle di una partita del giorno dopo (inutile, verranno rilette).
#
# Da qui la regola: non un orario, ma un MOMENTO, calcolato da ogni partita.
#
# Prima del via, per le due cose che dopo il fischio non si prendono piu' o
# non valgono piu': le quote spariscono, e la lista degli indisponibili letta
# a partita finita sa chi non e' sceso in campo davvero — usarla per
# pronosticare sarebbe barare.
MINUTI_PRIMA_DEL_VIA = 40

# Dopo, per i dettagli e il risultato definitivo. 90 minuti di gioco, 15 di
# intervallo e mezz'ora di margine per recuperi, VAR e interviste: 2h15. Non
# c'e' fretta — i dettagli si leggono dall'id in qualsiasi momento — ma prima
# di cosi' si troverebbe una partita ancora in corso e si spenderebbe una
# richiesta per niente.
MINUTI_DOPO_IL_VIA = 135

# Quanto resta "utile" il momento dopo. Serve perche' chi chiama passa ogni
# 15 minuti: senza una finestra larga almeno quanto quel passo, il momento
# giusto cadrebbe fra un controllo e l'altro e non arriverebbe mai.
DURATA_MOMENTO_DOPO = 60


def momento_utile(
    fixtures: Iterable[Any], adesso: datetime | None = None
) -> str | None:
    """Questo istante serve a qualcosa, o e' meglio non spendere niente?

    Risponde ``"vigilia"`` se c'e' una partita che sta per cominciare,
    ``"dopo"`` se ce n'e' una finita da poco, ``None`` se non c'e' motivo di
    chiamare l'API — ed e' la risposta piu' frequente, perche' in una giornata
    di Serie A i momenti utili sono cinque o sei su novantasei controlli.

    La vigilia vince sul dopo: se una partita e' appena finita e un'altra sta
    per cominciare, cio' che si perde per sempre sono le quote della seconda.

    Args:
        fixtures: partite con ``kickoff_utc``, come le legge ``parse_fixtures``.
        adesso: istante di riferimento, per i test. Default: ora, UTC.

    Returns:
        ``"vigilia"``, ``"dopo"`` oppure ``None``.
    """
    ora = adesso or datetime.now(timezone.utc)
    dopo = False

    for f in fixtures:
        via = getattr(f, "kickoff_utc", None)
        if via is None:
            continue
        minuti = (via - ora).total_seconds() / 60.0
        if 0 < minuti <= MINUTI_PRIMA_DEL_VIA:
            return "vigilia"
        passati = -minuti
        if MINUTI_DOPO_IL_VIA <= passati < MINUTI_DOPO_IL_VIA + DURATA_MOMENTO_DOPO:
            dopo = True

    return "dopo" if dopo else None


# Quanto margine deve restare prima che il budget si azzeri, perche' valga la
# pena prendere i dettagli adesso invece che alla raccolta del mattino.
#
# L'ancora e' la mezzanotte UTC — il momento in cui API-Football rimette il
# contatore a cento — e non un'ora italiana, per due motivi. Il primo e' che
# quella e' la cosa vera: i dettagli presi in coda di giornata si pagano con
# le richieste avanzate, quelli presi la mattina con un budget intero, ed e'
# esattamente il guasto del 22 agosto 2026 (la corsa delle 23:45 trovo' 75
# richieste su 100 gia' spese e non prese' niente). Il secondo e' che un'ora
# italiana si sposterebbe da sola due volte l'anno: col cambio dell'ora legale
# il posticipo delle 20:45 passa dalle 18:45 UTC alle 19:45 UTC, e una soglia
# scritta in ora locale cadrebbe nel posto sbagliato per sei mesi.
#
# Quattro ore separano il posticipo da tutto il resto, in entrambi i fusi:
#
#   via 20:45 (18:45 o 19:45 UTC) -> dettagli pronti 3h o 2h prima del reset
#   via 18:30 (16:30 o 17:30 UTC) -> 5h15 o 4h15 prima
#   via 15:00 (13:00 o 14:00 UTC) -> 8h45 o 7h45 prima
#
# Solo il posticipo aspetta il mattino: e' quello che finisce a ridosso del
# reset ed e' quello i cui tabellini l'API consegna piu' tardi.
ORE_DI_MARGINE_PER_I_DETTAGLI = 4


def dettagli_possono_aspettare(adesso: datetime | None = None) -> bool:
    """Conviene lasciare i dettagli alla raccolta del mattino?

    Vale per **qualunque** raccolta, non solo per il momento "dopo": anche la
    corsa fissa delle 23:45 cade dentro questa finestra, e il suo compito e'
    chiudere la giornata con i risultati definitivi — non raccogliere
    tabellini, che sono la cosa meno urgente che il sistema conosca.

    Aspettare non costa niente e guadagna due cose. Il budget: dopo il reset
    le stesse richieste si pagano con un contatore intero. E il tempo: il
    tabellino di una partita finita da poco puo' essere ancora la fotografia
    del feed live (vedi `tabellino_provvisorio` in `store.py`), e ogni ora di
    attesa e' un'ora in cui l'API puo' consolidarlo.

    Non e' pero' una garanzia, e non va raccontata come tale: la sera del 22
    agosto 2026 due tabellini erano ancora fermi **quattro ore** dopo il
    fischio finale. Questa regola riduce quante volte si prende una fotografia
    invece di un tabellino; a ripescarla quando succede resta `needs_detail`.
    """
    ora = adesso or datetime.now(timezone.utc)
    reset = (ora + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    ore_al_reset = (reset - ora).total_seconds() / 3600.0
    return ore_al_reset < ORE_DI_MARGINE_PER_I_DETTAGLI


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
    kinds: Iterable[str] | None = None,
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

    ``kinds`` restringe quali dei tre dettagli chiedere. La raccolta di oggi
    li vuole tutti — costano nove richieste e la partita e' una sola — ma il
    riempimento di una stagione passata no: li' ogni endpoint sono ~380
    richieste, e i tre non servono alle stesse cose. `players` e' l'unico che
    puo' cambiare un pronostico (minuti, gol, assist e tiri: `data/pesi.py` e
    il modello a tiri); `lineups` serve all'allenatore e alla pagina della
    partita, `events` solo a dire *quando* si e' segnato. Un terzo della spesa
    porta quindi quasi tutto il valore, e questo parametro e' il modo di
    comprare quel terzo.
    """
    kinds = tuple(kinds) if kinds is not None else DETAIL_KINDS
    raccolti: dict[str, int] = {}
    spese = 0
    errori_di_fila = 0

    for raw in fixtures:
        if _status_of(raw) != "finished":
            continue
        fid = (raw.get("fixture") or {}).get("id")
        if fid is None:
            continue

        for kind in kinds:
            if not store.needs_detail(fid, kind):
                continue
            if max_requests is not None and spese >= max_requests:
                logger.info("dettagli: raggiunto il tetto di %d richieste", max_requests)
                return raccolti

            prio = priorita if priorita is not None else _DETAIL_PRIORITY[kind]
            if not client.budget.can_spend(prio):
                logger.info("dettagli: budget %s esaurito, si riprende domani", prio.name)
                continue

            # Se un dettaglio ce l'abbiamo gia' e lo stiamo richiedendo, e'
            # perche' quello che abbiamo non va (`needs_detail`): un tabellino
            # fermo a partita in corso. La cache di quei dettagli non scade
            # mai, quindi senza `force_refresh` la richiesta verrebbe servita
            # dalla copia sbagliata — nessuna chiamata all'API, nessuna
            # correzione, e il contatore dei tentativi che sale fino al tetto
            # senza aver mai chiesto niente a nessuno.
            riprova = store.has_detail(fid, kind)
            try:
                payload = client.get(
                    f"fixtures/{kind}", {"fixture": fid},
                    ttl=TTL_FOREVER, priority=prio,
                    force_refresh=riprova,
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


@dataclass
class CalendarReport:
    """Esito dello scaricamento del calendario di stagione."""

    n_serie_a: int = 0
    new_fixtures: int = 0
    updated_fixtures: int = 0
    requests_spent: int = 0
    error: str | None = None


def collect_season_calendar(
    client: ApiFootballClient,
    store: SeasonStore,
    league_id: int = SERIE_A_LEAGUE_ID,
    season: int | None = None,
    today: date | None = None,
    ttl: int | None = TTL_CALENDARIO_STAGIONE,
) -> CalendarReport:
    """Tutto il campionato in una richiesta: chi gioca contro chi, e quando.

    E' l'altra meta' della raccolta, e fa un lavoro che la finestra di tre
    giorni non puo' fare. La finestra vede gli **stati**: una partita che
    comincia, un risultato che arriva, un tabellino che si consolida. Questa
    vede il **calendario**, cioe' le 380 partite di maggio comprese, e serve
    perche' un pronostico scritto con ore di anticipo e uno scritto con giorni
    di anticipo non sono la stessa cosa.

    Non sostituisce `collect_window` e non ne fa le veci: gli stati continuano
    ad arrivare da li'.

    Due cose che sembrano dettagli:

    * la stagione si chiede a `current_season()` e non si scrive a mano. Il
      rollover e' a luglio: un anno scritto a mano diventerebbe muto il luglio
      successivo, e in silenzio;
    * si scrive con `mai_regredire=True`. Questa risposta passa da una cache di
      sei ore e puo' quindi essere piu' vecchia dell'archivio: senza quella
      guardia, una lettura del pomeriggio riscriverebbe come "da giocare" le
      partite della sera. E' l'unico punto della raccolta in cui si potrebbe
      perdere un risultato.

    Il filtro sulla lega resta anche avendo chiesto la sola Serie A: costa
    niente ed e' la stessa difesa di `collect_day`.

    Quante volte chiamarla non lo decide un orologio ma il `ttl`: un giorno,
    quindi le quattro raccolte quotidiane la chiedono tutte e solo la prima
    paga una richiesta. Lo stesso vale il giorno in cui il piano tornasse
    gratuito e il filtro `league+season` si richiudesse: la risposta sarebbe
    **vuota e non un errore**, quindi finirebbe in cache come le altre e
    costerebbe una richiesta al giorno, non quattro. Zero partite qui non
    vuol dire che il campionato sia finito: vuol dire che questa strada non
    e' piu' aperta, e la finestra di tre giorni continua da sola.
    """
    anno = season if season is not None else current_season(today)
    report = CalendarReport()
    prima = client.budget.used

    try:
        tutte = client.fixtures(season=anno, league=league_id, ttl=ttl)
    except (ApiFootballError, BudgetExhausted) as exc:
        report.error = str(exc)
        report.requests_spent = client.budget.used - prima
        logger.error("calendario %s non scaricato — %s", anno, exc)
        return report

    serie_a = [raw for raw in tutte if _is_serie_a(raw, league_id)]
    report.n_serie_a = len(serie_a)
    report.new_fixtures, report.updated_fixtures = store.upsert_fixtures(
        serie_a, mai_regredire=True
    )
    report.requests_spent = client.budget.used - prima
    logger.info(
        "calendario %s: %d partite, %d nuove, %d aggiornate (%d richieste)",
        anno, report.n_serie_a, report.new_fixtures, report.updated_fixtures,
        report.requests_spent,
    )
    return report


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
