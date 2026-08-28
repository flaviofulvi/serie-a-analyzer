"""Test della raccolta giornaliera, con la rete simulata.

Questo e' il pezzo che deve girare da solo tutti i giorni per mesi. I
comportamenti verificati qui sono quelli il cui fallimento non si vedrebbe
subito:

* la Serie A viene filtrata in locale, perche' la richiesta non puo' filtrarla;
* un giorno gia' chiuso non spende nulla;
* un giorno con partite ancora in corso viene ripreso;
* se il budget finisce, gli id delle partite restano salvati — sono l'unica
  cosa davvero irrecuperabile.
"""

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx

from serie_a_analyzer.data.budget import Priority, RequestBudget
from serie_a_analyzer.data.cache import ResponseCache
from serie_a_analyzer.data.client import API_SPORTS_BASE_URL, ApiFootballClient
from serie_a_analyzer.data.daily import (
    backfill_details,
    collect_day,
    collect_season_calendar,
    collect_window,
    dettagli_possono_aspettare,
    DURATA_MOMENTO_DOPO,
    MINUTI_PRIMA_DEL_VIA,
    momento_utile,
    window_days,
)
from serie_a_analyzer.data.store import DayJournal, SeasonStore

GIORNO = date(2026, 8, 22)

# Il giorno successivo alla partita. Serve a quasi tutti i test perche' un
# giorno viene chiuso solo quando e' passato: senza un "oggi" esplicito, le
# asserzioni sulla chiusura cambierebbero esito a seconda della data in cui la
# suite viene eseguita.
IL_GIORNO_DOPO = date(2026, 8, 23)


def _fixture(fid, league=135, status="FT", ora="16:30:00", giorno="2026-08-22"):
    return {
        "fixture": {"id": fid, "date": f"{giorno}T{ora}+00:00",
                    "status": {"short": status, "long": status}},
        "league": {"id": league, "name": "Serie A" if league == 135 else "Altro",
                   "season": 2026, "round": "Regular Season - 1"},
        "teams": {"home": {"id": 505, "name": "Inter"},
                  "away": {"id": 503, "name": "Torino"}},
        "goals": {"home": 2, "away": 0},
    }


def _wrap(response):
    return {"get": "fixtures", "errors": [], "results": len(response),
            "paging": {"current": 1, "total": 1}, "response": response}


@pytest.fixture
def mondo():
    """Cosa risponde l'API finta, per giorno e per endpoint di dettaglio."""
    return {
        "per_giorno": {},
        # Il calendario di stagione: stessa rotta, ma chiesta per `season`
        # invece che per `date`.
        "stagione": [],
        "dettagli": {},
        "chiamate": [],
        # Se valorizzato, ogni chiamata risponde con questo errore.
        "errore": None,
    }


@pytest.fixture
def client(tmp_path, mondo):
    """Client con la rete simulata.

    La rotta si registra *dentro* il contesto di respx e vive quanto la
    fixture: registrarla fuori la lascerebbe attiva anche per gli altri file
    di test, che si troverebbero risposte vuote senza capire perche'.
    """
    c = ApiFootballClient(
        api_key="k",
        cache=ResponseCache(root=tmp_path / "cache"),
        budget=RequestBudget(state_path=tmp_path / "budget.json", daily_limit=100),
        # Freno spento: qui la rete e' finta e non c'e' nessun tetto al
        # minuto da rispettare. Lasciarlo acceso farebbe dormire la suite.
        min_interval_seconds=0.0,
    )

    def router(request):
        percorso = request.url.path
        mondo["chiamate"].append(percorso)
        if mondo["errore"]:
            return httpx.Response(200, json={"errors": mondo["errore"], "response": []})
        if percorso.endswith("/fixtures"):
            giorno = request.url.params.get("date")
            if giorno is None:
                return httpx.Response(200, json=_wrap(mondo["stagione"]))
            return httpx.Response(200, json=_wrap(mondo["per_giorno"].get(giorno, [])))
        # /fixtures/lineups, /fixtures/players, /fixtures/events
        kind = percorso.rsplit("/", 1)[-1]
        fid = int(request.url.params.get("fixture"))
        return httpx.Response(200, json=_wrap(mondo["dettagli"].get((fid, kind), [])))

    with respx.mock:
        respx.get(url__startswith=API_SPORTS_BASE_URL).mock(side_effect=router)
        yield c


@pytest.fixture
def store(tmp_path):
    return SeasonStore(root=tmp_path / "serie_a")


@pytest.fixture
def journal(tmp_path):
    return DayJournal(path=tmp_path / "journal.json")


# ---- finestra --------------------------------------------------------------


def test_la_finestra_e_ieri_oggi_domani():
    assert window_days(date(2026, 8, 22)) == [
        date(2026, 8, 21), date(2026, 8, 22), date(2026, 8, 23)
    ]


def test_la_finestra_parte_dal_giorno_piu_vecchio():
    """Ieri per primo: domani sara' fuori finestra, oggi no."""
    giorni = window_days(date(2026, 8, 22))
    assert giorni == sorted(giorni)


# ---- il calendario di tutta la stagione ------------------------------------


def test_il_calendario_porta_la_stagione_in_una_richiesta(client, store, mondo):
    """Cio' che la finestra di tre giorni non puo' vedere: maggio."""
    mondo["stagione"] = [
        _fixture(1, giorno="2026-08-22"),
        _fixture(2, giorno="2027-05-30", status="NS"),
        _fixture(3, league=39, giorno="2027-05-30"),   # Premier League
    ]

    report = collect_season_calendar(client, store, season=2026)

    assert report.n_serie_a == 2
    assert report.requests_spent == 1
    assert sorted(store.fixture_ids()) == [1, 2]


def test_il_calendario_non_cancella_un_risultato_gia_raccolto(client, store, mondo):
    """Il punto pericoloso, provato dall'esterno.

    La risposta passa da una cache di sei ore: quella letta la sera puo'
    essere la fotografia del pomeriggio, in cui la partita delle 18:30 non era
    ancora cominciata. L'archivio della stagione in corso e' l'unica copia che
    esiste.
    """
    store.upsert_fixtures([_fixture(1, status="FT")])
    mondo["stagione"] = [_fixture(1, status="NS")]

    report = collect_season_calendar(client, store, season=2026)

    assert (report.new_fixtures, report.updated_fixtures) == (0, 0)
    salvata = store.load_fixtures()["1"]
    assert salvata["fixture"]["status"]["short"] == "FT"
    assert salvata["goals"] == {"home": 2, "away": 0}


def test_il_calendario_una_volta_al_giorno_lo_decide_la_cache(client, store, mondo):
    """Le quattro raccolte quotidiane lo chiedono tutte, una sola lo paga."""
    mondo["stagione"] = [_fixture(1)]

    collect_season_calendar(client, store, season=2026)
    secondo = collect_season_calendar(client, store, season=2026)

    assert secondo.requests_spent == 0
    assert secondo.n_serie_a == 1


def test_una_risposta_vuota_non_e_un_guasto(client, store, mondo):
    """Il giorno del ritorno al piano gratuito: zero partite, nessun errore.

    Non deve cancellare niente ne' sembrare un guasto: la finestra di tre
    giorni continua da sola, ed e' cosi' che si e' sempre lavorato.
    """
    store.upsert_fixtures([_fixture(1, status="FT")])
    mondo["stagione"] = []

    report = collect_season_calendar(client, store, season=2026)

    assert report.error is None
    assert report.n_serie_a == 0
    assert store.fixture_ids() == [1]


def test_un_errore_dell_api_non_solleva(client, store, mondo):
    """Il calendario e' la cosa meno urgente: se non risponde si riprova domani."""
    mondo["errore"] = {"plan": "Free plans do not have access to this season"}

    report = collect_season_calendar(client, store, season=2026)

    assert report.error is not None
    assert report.n_serie_a == 0


# ---- filtro della lega -----------------------------------------------------


def test_la_serie_a_viene_filtrata_in_locale(client, store, journal, mondo):
    """La richiesta non puo' filtrare per lega: lo facciamo noi sui dati."""
    mondo["per_giorno"]["2026-08-22"] = [
        _fixture(1, league=135),
        _fixture(2, league=39),    # Premier League
        _fixture(3, league=135),
        _fixture(4, league=140),   # LaLiga
    ]

    report = collect_day(client, store, journal, GIORNO, with_details=False, today=IL_GIORNO_DOPO)

    assert report.n_serie_a == 2
    assert sorted(store.fixture_ids()) == [1, 3]


def test_un_giorno_senza_serie_a_e_comunque_registrato(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, league=39)]

    report = collect_day(client, store, journal, GIORNO, with_details=False, today=IL_GIORNO_DOPO)

    assert report.n_serie_a == 0
    assert report.requests_spent == 1
    # Registrato e chiuso: senza, sembrerebbe un buco per sempre.
    assert journal.is_closed(GIORNO, today=IL_GIORNO_DOPO)


# ---- chiusura del giorno ---------------------------------------------------


def test_un_giorno_chiuso_non_costa_nulla(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1)]
    collect_day(client, store, journal, GIORNO, with_details=False, today=IL_GIORNO_DOPO)
    speso_prima = client.budget.used

    report = collect_day(client, store, journal, GIORNO, with_details=False, today=IL_GIORNO_DOPO)

    assert report.skipped
    assert report.requests_spent == 0
    assert client.budget.used == speso_prima


def test_un_giorno_con_partite_in_corso_viene_ripreso(client, store, journal, mondo):
    """E' cosi' che si recuperano i risultati di una partita ancora aperta."""
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="2H")]
    primo = collect_day(client, store, journal, GIORNO, with_details=False, today=IL_GIORNO_DOPO)
    assert not primo.closed
    assert not journal.is_closed(GIORNO, today=IL_GIORNO_DOPO)

    # ttl=0: si sta inseguendo una partita in corso, la copia in cache di un
    # minuto fa e' esattamente quella che non serve.
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="FT")]
    secondo = collect_day(client, store, journal, GIORNO, with_details=False, ttl=0, today=IL_GIORNO_DOPO)

    assert not secondo.skipped
    assert secondo.closed
    assert secondo.updated_fixtures == 1
    assert journal.is_closed(GIORNO, today=IL_GIORNO_DOPO)


def test_la_cache_frena_due_esecuzioni_ravvicinate(client, store, journal, mondo):
    """Col TTL di default una seconda passata immediata non spende budget."""
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="2H")]
    collect_day(client, store, journal, GIORNO, with_details=False, today=IL_GIORNO_DOPO)
    speso = client.budget.used

    collect_day(client, store, journal, GIORNO, with_details=False, today=IL_GIORNO_DOPO)

    assert client.budget.used == speso


def test_una_partita_rinviata_tiene_il_giorno_aperto(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="PST")]
    report = collect_day(client, store, journal, GIORNO, with_details=False, today=IL_GIORNO_DOPO)
    assert not report.closed


# ---- dettagli --------------------------------------------------------------


def test_i_dettagli_si_prendono_solo_per_le_partite_concluse(client, store, journal, mondo):
    """Prima del fischio d'inizio i tre endpoint rispondono vuoto."""
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="FT"), _fixture(2, status="NS")]
    for kind in ("lineups", "players", "events"):
        mondo["dettagli"][(1, kind)] = [{"dato": kind}]

    collect_day(client, store, journal, GIORNO, today=IL_GIORNO_DOPO)

    assert store.has_detail(1, "lineups")
    assert store.load_detail(2, "lineups") is None
    # Tre dettagli per la sola partita conclusa: sulla programmata nemmeno
    # una richiesta, sarebbe budget buttato su una risposta vuota.
    assert sum(1 for c in mondo["chiamate"] if c != "/fixtures") == 3


def test_i_dettagli_gia_presenti_non_si_riscaricano(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="2H")]
    mondo["per_giorno"]["2026-08-23"] = []
    for kind in ("lineups", "players", "events"):
        mondo["dettagli"][(1, kind)] = [{"dato": kind}]

    collect_day(client, store, journal, GIORNO, with_details=False, today=IL_GIORNO_DOPO)
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="FT")]
    collect_day(client, store, journal, GIORNO, today=IL_GIORNO_DOPO)
    speso = client.budget.used

    # Il giorno e' chiuso: seconda passata a costo zero.
    collect_day(client, store, journal, GIORNO, today=IL_GIORNO_DOPO)
    assert client.budget.used == speso


def test_il_tetto_di_richieste_per_i_dettagli_viene_rispettato(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(i) for i in range(1, 6)]
    for i in range(1, 6):
        for kind in ("lineups", "players", "events"):
            mondo["dettagli"][(i, kind)] = [{"dato": kind}]

    collect_day(client, store, journal, GIORNO, max_detail_requests=4, today=IL_GIORNO_DOPO)

    # 1 per il giorno + 4 di dettagli, non 1 + 15.
    assert client.budget.used == 5


# ---- budget ----------------------------------------------------------------


def test_se_il_budget_finisce_gli_id_restano_salvati(client, store, journal, mondo):
    """E' la garanzia che conta: gli id sono l'unica cosa irrecuperabile.

    I dettagli si leggono dall'id della partita, e quell'endpoint non ha la
    finestra di tre giorni: quello che manca oggi si prende domani.
    """
    mondo["per_giorno"]["2026-08-22"] = [_fixture(i) for i in range(1, 4)]
    for i in range(1, 4):
        for kind in ("lineups", "players", "events"):
            mondo["dettagli"][(i, kind)] = [{"dato": kind}]

    # Si lascia margine solo per la richiesta del giorno.
    client.budget.spend(Priority.CRITICAL, client.budget.daily_limit - 26)

    report = collect_day(client, store, journal, GIORNO, today=IL_GIORNO_DOPO)

    assert sorted(store.fixture_ids()) == [1, 2, 3]
    assert report.n_serie_a == 3
    assert journal.is_closed(GIORNO, today=IL_GIORNO_DOPO)


def test_il_recupero_completa_i_dettagli_rimasti_indietro(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(i) for i in range(1, 4)]
    for i in range(1, 4):
        for kind in ("lineups", "players", "events"):
            mondo["dettagli"][(i, kind)] = [{"dato": kind}]

    collect_day(client, store, journal, GIORNO, max_detail_requests=2, today=IL_GIORNO_DOPO)
    assert not store.has_detail(3, "events")

    backfill_details(client, store)

    for i in range(1, 4):
        for kind in ("lineups", "players", "events"):
            assert store.has_detail(i, kind), f"partita {i}, {kind}"


def test_il_recupero_non_spende_nulla_se_non_manca_niente(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1)]
    for kind in ("lineups", "players", "events"):
        mondo["dettagli"][(1, kind)] = [{"dato": kind}]

    collect_day(client, store, journal, GIORNO, today=IL_GIORNO_DOPO)
    speso = client.budget.used

    assert backfill_details(client, store) == {}
    assert client.budget.used == speso


def _tabellino(minuti):
    """Un `/fixtures/players` con gli undici titolari fermi a ``minuti``."""
    return [{
        "team": {"id": 505, "name": "Inter"},
        "players": [
            {"player": {"id": pid, "name": f"Giocatore {pid}"},
             "statistics": [{"games": {"minutes": minuti, "substitute": False},
                             "goals": {"total": None, "assists": None}}]}
            for pid in range(1, 12)
        ],
    }]


def test_un_tabellino_fermo_a_meta_partita_viene_richiesto_di_nuovo(
    client, store, journal, mondo
):
    """Il guasto del 22 agosto 2026, in piccolo.

    L'API risponde prima con la fotografia del 64' e poi, ore dopo, col
    tabellino vero. Se l'archivio considerasse chiusa la prima risposta,
    la seconda non verrebbe mai chiesta e le pagelle resterebbero quelle di
    meta' partita per sempre.
    """
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1)]
    mondo["per_giorno"]["2026-08-23"] = []
    mondo["dettagli"][(1, "lineups")] = [{"dato": "lineups"}]
    mondo["dettagli"][(1, "events")] = [{"dato": "events"}]
    mondo["dettagli"][(1, "players")] = _tabellino(64)

    collect_day(client, store, journal, GIORNO, today=IL_GIORNO_DOPO)
    assert store.has_detail(1, "players")

    # Ore dopo, l'API si consolida.
    mondo["dettagli"][(1, "players")] = _tabellino(90)
    backfill_details(client, store)

    minuti = store.load_detail(1, "players")[0]["players"][0]["statistics"][0]
    assert minuti["games"]["minutes"] == 90


def test_la_ri_richiesta_di_un_tabellino_scavalca_la_cache(client, store, journal, mondo):
    """La cache dei dettagli non scade mai: senza scavalcarla non si guarisce.

    E' la meta' meno visibile del guasto. `needs_detail` rimetterebbe la
    partita in coda, la richiesta verrebbe servita dalla copia sbagliata in
    cache, e il contatore dei tentativi salirebbe fino al tetto senza che una
    sola chiamata sia mai partita davvero.
    """
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1)]
    mondo["per_giorno"]["2026-08-23"] = []
    mondo["dettagli"][(1, "lineups")] = [{"dato": "lineups"}]
    mondo["dettagli"][(1, "events")] = [{"dato": "events"}]
    mondo["dettagli"][(1, "players")] = _tabellino(64)

    collect_day(client, store, journal, GIORNO, today=IL_GIORNO_DOPO)
    mondo["dettagli"][(1, "players")] = _tabellino(90)
    mondo["chiamate"].clear()

    backfill_details(client, store)

    assert "/fixtures/players" in mondo["chiamate"], (
        "la richiesta non e' arrivata all'API: servita dalla cache"
    )


def test_un_tabellino_completo_non_si_richiede_due_volte(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1)]
    mondo["per_giorno"]["2026-08-23"] = []
    mondo["dettagli"][(1, "lineups")] = [{"dato": "lineups"}]
    mondo["dettagli"][(1, "events")] = [{"dato": "events"}]
    mondo["dettagli"][(1, "players")] = _tabellino(90)

    collect_day(client, store, journal, GIORNO, today=IL_GIORNO_DOPO)
    speso = client.budget.used

    assert backfill_details(client, store) == {}
    assert client.budget.used == speso


# ---- i dettagli del posticipo aspettano il mattino ---------------------------
#
# Deciso dopo la sera del 22 agosto 2026: i tabellini del posticipo delle 20:45
# si pagavano con le richieste avanzate di una giornata quasi finita, e per di
# piu' erano ancora la fotografia del feed live. La soglia e' ancorata al reset
# del budget (mezzanotte UTC), non a un'ora italiana, cosi' non si sposta col
# cambio dell'ora legale.


def _utc(quando: str) -> datetime:
    return datetime.fromisoformat(quando).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("quando", [
    "2026-08-22T21:00",   # estate: dettagli del posticipo delle 20:45
    "2026-08-22T21:45",   # estate: la corsa fissa delle 23:45
    "2026-12-06T22:00",   # inverno: stesso posticipo, un'ora piu' in la' in UTC
    "2026-12-06T22:45",   # inverno: stessa corsa fissa
])
def test_a_ridosso_del_reset_i_dettagli_aspettano(quando):
    assert dettagli_possono_aspettare(_utc(quando))


@pytest.mark.parametrize("quando", [
    "2026-08-22T18:45",   # estate: dettagli della partita delle 18:30
    "2026-08-22T15:15",   # estate: dettagli del pomeriggio
    "2026-08-22T06:00",   # estate: la raccolta delle 08:00
    "2026-12-06T19:15",   # inverno: dettagli della partita delle 18:00
    "2026-12-06T07:00",   # inverno: la raccolta delle 08:00
])
def test_col_giorno_davanti_i_dettagli_si_prendono_subito(quando):
    assert not dettagli_possono_aspettare(_utc(quando))


def test_la_soglia_e_in_utc_e_non_si_sposta_con_l_ora_legale():
    """Il posticipo va in campo alla stessa ora italiana tutto l'anno.

    In UTC no: d'estate il fischio d'inizio delle 20:45 e' alle 18:45, d'inverno
    alle 19:45. Una soglia scritta in ora italiana cadrebbe nel posto sbagliato
    per meta' stagione; questa, ancorata al reset del budget, no.
    """
    for giorno in ("2026-08-22", "2026-12-06"):
        assert dettagli_possono_aspettare(_utc(f"{giorno}T22:30"))
        assert not dettagli_possono_aspettare(_utc(f"{giorno}T17:00"))


def test_un_giorno_chiuso_senza_dettagli_li_recupera_il_mattino_dopo(
    client, store, journal, mondo
):
    """La rete di sicurezza dello spostamento, ed e' quella che lo rende sicuro.

    La corsa delle 23:45 chiude la giornata perche' i risultati sono
    definitivi, ma i dettagli non li ha presi. Un giorno chiuso non viene piu'
    richiesto, quindi `collect_window` non ci tornera' sopra: se non ci fosse
    `backfill_details` a pescarli dall'id, quei tabellini non arriverebbero
    mai piu'.
    """
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1), _fixture(2)]
    mondo["per_giorno"]["2026-08-23"] = []
    for i in (1, 2):
        for kind in ("lineups", "players", "events"):
            mondo["dettagli"][(i, kind)] = [{"dato": kind}]

    # La sera: risultati si', dettagli no.
    collect_day(client, store, journal, GIORNO, with_details=False, today=IL_GIORNO_DOPO)
    assert journal.is_closed(GIORNO, today=IL_GIORNO_DOPO), "il giorno doveva chiudersi"
    assert not store.has_detail(1, "players")

    # Il mattino dopo: il giorno e' chiuso e non costa nulla...
    speso = client.budget.used
    collect_day(client, store, journal, GIORNO, today=IL_GIORNO_DOPO)
    assert client.budget.used == speso, "un giorno chiuso non va richiesto"

    # ...ma i dettagli arrivano lo stesso, perche' l'id non scade.
    backfill_details(client, store)
    for i in (1, 2):
        for kind in ("lineups", "players", "events"):
            assert store.has_detail(i, kind), f"partita {i}, {kind}"


def test_il_recupero_parte_dalle_partite_piu_recenti(client, store, journal, mondo):
    """Col budget contato, prima i dati che pesano di piu' nel modello."""
    mondo["per_giorno"]["2026-08-22"] = [
        _fixture(1, giorno="2026-08-22", ora="12:00:00"),
        _fixture(2, giorno="2026-08-22", ora="20:45:00"),
    ]
    for i in (1, 2):
        for kind in ("lineups", "players", "events"):
            mondo["dettagli"][(i, kind)] = [{"dato": kind}]

    collect_day(client, store, journal, GIORNO, with_details=False, today=IL_GIORNO_DOPO)
    backfill_details(client, store, max_requests=3)

    assert store.has_detail(2, "lineups")
    assert not store.has_detail(1, "lineups")


# ---- errori ----------------------------------------------------------------


def test_un_errore_dell_api_non_chiude_il_giorno(client, store, journal, mondo):
    """Se il giorno risultasse chiuso, non verrebbe piu' richiesto: perso."""
    mondo["errore"] = {"plan": "Free plans do not have access to this season."}

    report = collect_day(client, store, journal, GIORNO, with_details=False, today=IL_GIORNO_DOPO)

    assert report.error is not None
    assert journal.entry(GIORNO) is None
    assert not journal.is_closed(GIORNO, today=IL_GIORNO_DOPO)


# ---- finestra completa -----------------------------------------------------


def test_la_finestra_raccoglie_tutti_e_tre_i_giorni(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-21"] = [_fixture(1, giorno="2026-08-21")]
    mondo["per_giorno"]["2026-08-22"] = [_fixture(2)]
    mondo["per_giorno"]["2026-08-23"] = [_fixture(3, giorno="2026-08-23", status="NS")]

    reports = collect_window(
        client, store, journal, today=GIORNO, with_details=False
    )

    assert len(reports) == 3
    assert sorted(store.fixture_ids()) == [1, 2, 3]
    assert journal.collected_days() == [
        date(2026, 8, 21), date(2026, 8, 22), date(2026, 8, 23)
    ]


# ---- giorni non ancora passati ---------------------------------------------
#
# La finestra guarda anche avanti, e il calendario della Serie A cambia:
# recuperi e anticipi compaiono a stagione in corso. Un giorno ancora da
# giocare non va mai chiuso, perche' chiuderlo significa non guardarlo piu' —
# e quando lo si guarderebbe di nuovo sarebbe gia' fuori finestra.


def test_domani_non_viene_chiuso_neanche_se_e_vuoto(client, store, journal, mondo):
    """E' il caso della pausa estiva: senza partite, tutto sembra definitivo."""
    mondo["per_giorno"]["2026-08-22"] = []

    report = collect_day(
        client, store, journal, GIORNO, with_details=False, today=date(2026, 8, 21)
    )

    assert report.n_serie_a == 0
    assert journal.entry(GIORNO) is not None, "il giorno va comunque registrato"
    assert not journal.is_closed(GIORNO, today=date(2026, 8, 21))


def test_una_partita_aggiunta_dopo_viene_presa(client, store, journal, mondo):
    """Il recupero calendarizzato dopo il nostro primo sguardo: il caso che si perde."""
    mondo["per_giorno"]["2026-08-22"] = []
    collect_day(client, store, journal, GIORNO, with_details=False, today=date(2026, 8, 21))

    # Il giorno dopo la partita compare in calendario.
    mondo["per_giorno"]["2026-08-22"] = [_fixture(77, status="NS")]
    report = collect_day(
        client, store, journal, GIORNO, with_details=False, ttl=0, today=GIORNO
    )

    assert not report.skipped, "il giorno era stato chiuso in anticipo"
    assert store.fixture_ids() == [77]


def test_oggi_non_viene_chiuso_neanche_a_partite_finite(client, store, journal, mondo):
    """Una serale finisce alle 22:45: fino a mezzanotte il giorno resta aperto."""
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="FT")]

    collect_day(client, store, journal, GIORNO, with_details=False, today=GIORNO)

    assert not journal.is_closed(GIORNO, today=GIORNO)
    # E il giorno dopo si chiude da solo, senza altre richieste.
    assert journal.is_closed(GIORNO, today=IL_GIORNO_DOPO)


def test_la_finestra_chiude_solo_ieri(client, store, journal, mondo):
    """Il conto delle richieste di una giornata tipo: 1 per ieri, poi oggi e domani."""
    for giorno in ("2026-08-21", "2026-08-22", "2026-08-23"):
        mondo["per_giorno"][giorno] = [_fixture(9, giorno=giorno, status="FT")]

    collect_window(client, store, journal, today=GIORNO, with_details=False)

    assert journal.is_closed(date(2026, 8, 21), today=GIORNO)
    assert not journal.is_closed(GIORNO, today=GIORNO)
    assert not journal.is_closed(date(2026, 8, 23), today=GIORNO)


# ---- quando conviene raccogliere -------------------------------------------
#
# La raccolta a orari fissi passa vicino al fischio d'inizio per caso. Quote e
# indisponibili invece valgono quanto sono VICINI al via: le prime dopo non
# esistono piu', i secondi letti a partita finita sanno chi non e' sceso in
# campo davvero. Da qui `momento_utile`, che decide guardando il calendario.


class _Partita:
    """Il minimo che `momento_utile` guarda di una partita."""

    def __init__(self, kickoff):
        self.kickoff_utc = kickoff


VIA = datetime(2026, 8, 22, 18, 45, tzinfo=timezone.utc)


def _quando(minuti_al_via):
    """L'istante in cui mancano ``minuti_al_via`` minuti al fischio."""
    return VIA - timedelta(minutes=minuti_al_via)


@pytest.mark.parametrize("minuti_al_via", [40, 30, 10, 1])
def test_e_vigilia_nei_minuti_prima_del_via(minuti_al_via):
    assert momento_utile([_Partita(VIA)], _quando(minuti_al_via)) == "vigilia"


@pytest.mark.parametrize("minuti_al_via", [41, 90, 600])
def test_troppo_presto_non_e_il_momento(minuti_al_via):
    """Leggere le quote di una partita di domani significa rileggerle domani."""
    assert momento_utile([_Partita(VIA)], _quando(minuti_al_via)) is None


def test_al_fischio_d_inizio_la_vigilia_e_finita():
    """Un pronostico dopo il via e' barare, e le quote non esistono piu'."""
    assert momento_utile([_Partita(VIA)], VIA) is None


@pytest.mark.parametrize("minuti_dopo", [135, 160, 194])
def test_e_il_momento_dei_dettagli_dopo_la_partita(minuti_dopo):
    assert momento_utile([_Partita(VIA)], _quando(-minuti_dopo)) == "dopo"


@pytest.mark.parametrize("minuti_dopo", [100, 134, 195, 400])
def test_fuori_dalla_finestra_dopo_non_si_fa_niente(minuti_dopo):
    """Prima si troverebbe una partita ancora in corso, dopo ci pensa la
    raccolta delle 23:45."""
    assert momento_utile([_Partita(VIA)], _quando(-minuti_dopo)) is None


def test_la_vigilia_batte_il_dopo():
    """Se una e' appena finita e un'altra sta per cominciare, contano le quote.

    I dettagli della prima si leggono dall'id quando si vuole; le quote della
    seconda dopo il fischio non esistono piu'.
    """
    finita = _Partita(VIA - timedelta(minutes=150))
    imminente = _Partita(VIA + timedelta(minutes=20))
    assert momento_utile([finita, imminente], VIA) == "vigilia"


def test_una_finestra_larga_quanto_il_passo_di_chi_chiama():
    """Chi chiama ogni quarto d'ora deve trovare il momento, non scavalcarlo.

    Sia la vigilia (40 minuti) sia il dopo (60) sono piu' larghi del quarto
    d'ora fra un controllo e l'altro: se fossero istanti, il momento giusto
    cadrebbe fra due esecuzioni e non arriverebbe mai.
    """
    assert MINUTI_PRIMA_DEL_VIA >= 15
    assert DURATA_MOMENTO_DOPO >= 15


def test_senza_partite_non_e_mai_il_momento():
    assert momento_utile([], VIA) is None


def test_una_partita_senza_orario_non_rompe_niente():
    """Il calendario cambia, e una riga monca non deve fermare la raccolta."""
    assert momento_utile([_Partita(None), _Partita(VIA)], _quando(20)) == "vigilia"
