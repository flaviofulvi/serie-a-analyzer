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

from datetime import date

import httpx
import pytest
import respx

from serie_a_analyzer.data.budget import Priority, RequestBudget
from serie_a_analyzer.data.cache import ResponseCache
from serie_a_analyzer.data.client import API_SPORTS_BASE_URL, ApiFootballClient
from serie_a_analyzer.data.daily import (
    backfill_details,
    collect_day,
    collect_window,
    window_days,
)
from serie_a_analyzer.data.store import DayJournal, SeasonStore

GIORNO = date(2026, 8, 22)


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
    )

    def router(request):
        percorso = request.url.path
        mondo["chiamate"].append(percorso)
        if mondo["errore"]:
            return httpx.Response(200, json={"errors": mondo["errore"], "response": []})
        if percorso.endswith("/fixtures"):
            giorno = request.url.params.get("date")
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


# ---- filtro della lega -----------------------------------------------------


def test_la_serie_a_viene_filtrata_in_locale(client, store, journal, mondo):
    """La richiesta non puo' filtrare per lega: lo facciamo noi sui dati."""
    mondo["per_giorno"]["2026-08-22"] = [
        _fixture(1, league=135),
        _fixture(2, league=39),    # Premier League
        _fixture(3, league=135),
        _fixture(4, league=140),   # LaLiga
    ]

    report = collect_day(client, store, journal, GIORNO, with_details=False)

    assert report.n_serie_a == 2
    assert sorted(store.fixture_ids()) == [1, 3]


def test_un_giorno_senza_serie_a_e_comunque_registrato(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, league=39)]

    report = collect_day(client, store, journal, GIORNO, with_details=False)

    assert report.n_serie_a == 0
    assert report.requests_spent == 1
    # Registrato e chiuso: senza, sembrerebbe un buco per sempre.
    assert journal.is_closed(GIORNO)


# ---- chiusura del giorno ---------------------------------------------------


def test_un_giorno_chiuso_non_costa_nulla(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1)]
    collect_day(client, store, journal, GIORNO, with_details=False)
    speso_prima = client.budget.used

    report = collect_day(client, store, journal, GIORNO, with_details=False)

    assert report.skipped
    assert report.requests_spent == 0
    assert client.budget.used == speso_prima


def test_un_giorno_con_partite_in_corso_viene_ripreso(client, store, journal, mondo):
    """E' cosi' che si recuperano i risultati di una partita ancora aperta."""
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="2H")]
    primo = collect_day(client, store, journal, GIORNO, with_details=False)
    assert not primo.closed
    assert not journal.is_closed(GIORNO)

    # ttl=0: si sta inseguendo una partita in corso, la copia in cache di un
    # minuto fa e' esattamente quella che non serve.
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="FT")]
    secondo = collect_day(client, store, journal, GIORNO, with_details=False, ttl=0)

    assert not secondo.skipped
    assert secondo.closed
    assert secondo.updated_fixtures == 1
    assert journal.is_closed(GIORNO)


def test_la_cache_frena_due_esecuzioni_ravvicinate(client, store, journal, mondo):
    """Col TTL di default una seconda passata immediata non spende budget."""
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="2H")]
    collect_day(client, store, journal, GIORNO, with_details=False)
    speso = client.budget.used

    collect_day(client, store, journal, GIORNO, with_details=False)

    assert client.budget.used == speso


def test_una_partita_rinviata_tiene_il_giorno_aperto(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="PST")]
    report = collect_day(client, store, journal, GIORNO, with_details=False)
    assert not report.closed


# ---- dettagli --------------------------------------------------------------


def test_i_dettagli_si_prendono_solo_per_le_partite_concluse(client, store, journal, mondo):
    """Prima del fischio d'inizio i tre endpoint rispondono vuoto."""
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="FT"), _fixture(2, status="NS")]
    for kind in ("lineups", "players", "events"):
        mondo["dettagli"][(1, kind)] = [{"dato": kind}]

    collect_day(client, store, journal, GIORNO)

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

    collect_day(client, store, journal, GIORNO, with_details=False)
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1, status="FT")]
    collect_day(client, store, journal, GIORNO)
    speso = client.budget.used

    # Il giorno e' chiuso: seconda passata a costo zero.
    collect_day(client, store, journal, GIORNO)
    assert client.budget.used == speso


def test_il_tetto_di_richieste_per_i_dettagli_viene_rispettato(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(i) for i in range(1, 6)]
    for i in range(1, 6):
        for kind in ("lineups", "players", "events"):
            mondo["dettagli"][(i, kind)] = [{"dato": kind}]

    collect_day(client, store, journal, GIORNO, max_detail_requests=4)

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

    report = collect_day(client, store, journal, GIORNO)

    assert sorted(store.fixture_ids()) == [1, 2, 3]
    assert report.n_serie_a == 3
    assert journal.is_closed(GIORNO)


def test_il_recupero_completa_i_dettagli_rimasti_indietro(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(i) for i in range(1, 4)]
    for i in range(1, 4):
        for kind in ("lineups", "players", "events"):
            mondo["dettagli"][(i, kind)] = [{"dato": kind}]

    collect_day(client, store, journal, GIORNO, max_detail_requests=2)
    assert not store.has_detail(3, "events")

    backfill_details(client, store)

    for i in range(1, 4):
        for kind in ("lineups", "players", "events"):
            assert store.has_detail(i, kind), f"partita {i}, {kind}"


def test_il_recupero_non_spende_nulla_se_non_manca_niente(client, store, journal, mondo):
    mondo["per_giorno"]["2026-08-22"] = [_fixture(1)]
    for kind in ("lineups", "players", "events"):
        mondo["dettagli"][(1, kind)] = [{"dato": kind}]

    collect_day(client, store, journal, GIORNO)
    speso = client.budget.used

    assert backfill_details(client, store) == {}
    assert client.budget.used == speso


def test_il_recupero_parte_dalle_partite_piu_recenti(client, store, journal, mondo):
    """Col budget contato, prima i dati che pesano di piu' nel modello."""
    mondo["per_giorno"]["2026-08-22"] = [
        _fixture(1, giorno="2026-08-22", ora="12:00:00"),
        _fixture(2, giorno="2026-08-22", ora="20:45:00"),
    ]
    for i in (1, 2):
        for kind in ("lineups", "players", "events"):
            mondo["dettagli"][(i, kind)] = [{"dato": kind}]

    collect_day(client, store, journal, GIORNO, with_details=False)
    backfill_details(client, store, max_requests=3)

    assert store.has_detail(2, "lineups")
    assert not store.has_detail(1, "lineups")


# ---- errori ----------------------------------------------------------------


def test_un_errore_dell_api_non_chiude_il_giorno(client, store, journal, mondo):
    """Se il giorno risultasse chiuso, non verrebbe piu' richiesto: perso."""
    mondo["errore"] = {"plan": "Free plans do not have access to this season."}

    report = collect_day(client, store, journal, GIORNO, with_details=False)

    assert report.error is not None
    assert journal.entry(GIORNO) is None
    assert not journal.is_closed(GIORNO)


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
