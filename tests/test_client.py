"""Test del client API-Football, con la rete simulata.

Il comportamento piu' importante da verificare non e' che le chiamate
funzionino, ma che il budget non possa essere sforato e che la cache eviti
davvero di spenderlo.
"""

import httpx
import pytest
import respx

from serie_a_analyzer.data.budget import BudgetExhausted, Priority, RequestBudget
from serie_a_analyzer.data.cache import ResponseCache
from serie_a_analyzer.data.client import (
    API_SPORTS_BASE_URL,
    ApiFootballClient,
    ApiFootballError,
)


@pytest.fixture
def client(tmp_path):
    return ApiFootballClient(
        api_key="chiave-di-prova",
        cache=ResponseCache(root=tmp_path / "cache"),
        budget=RequestBudget(state_path=tmp_path / "budget.json", daily_limit=10),
        # Freno spento: qui la rete e' finta e non c'e' nessun tetto al
        # minuto da rispettare. Lasciarlo acceso farebbe dormire la suite.
        min_interval_seconds=0.0,
    )


def _mock(payload, status=200):
    return respx.get(url__startswith=f"{API_SPORTS_BASE_URL}/fixtures").mock(
        return_value=httpx.Response(status, json=payload)
    )


@respx.mock
def test_chiamata_riuscita(client, risposta_api):
    route = _mock(risposta_api)
    risultato = client.get("fixtures", {"league": 135, "season": 2024})
    assert len(risultato) == 2
    assert route.call_count == 1
    assert client.budget.used == 1


@respx.mock
def test_la_cache_evita_la_seconda_richiesta(client, risposta_api):
    route = _mock(risposta_api)
    client.get("fixtures", {"league": 135, "season": 2024})
    client.get("fixtures", {"league": 135, "season": 2024})
    # E' il comportamento su cui si regge tutto il piano free.
    assert route.call_count == 1
    assert client.budget.used == 1


@respx.mock
def test_force_refresh_spende_una_richiesta(client, risposta_api):
    route = _mock(risposta_api)
    client.get("fixtures", {"league": 135, "season": 2024})
    client.get("fixtures", {"league": 135, "season": 2024}, force_refresh=True)
    assert route.call_count == 2
    assert client.budget.used == 2


@respx.mock
def test_la_chiave_viene_inviata(client, risposta_api):
    route = _mock(risposta_api)
    client.get("fixtures", {"season": 2024})
    assert route.calls[0].request.headers["x-apisports-key"] == "chiave-di-prova"


@respx.mock
def test_budget_esaurito_blocca_prima_della_rete(client, risposta_api):
    route = _mock(risposta_api)
    client.budget.spend(Priority.CRITICAL, 10)
    with pytest.raises(BudgetExhausted):
        client.get("fixtures", {"season": 2024})
    # Nessuna chiamata deve partire: il budget e' una barriera, non un avviso.
    assert route.call_count == 0


@respx.mock
def test_errore_nel_corpo_con_status_200(client):
    """API-Football risponde 200 anche quando l'operazione e' fallita."""
    _mock({
        "get": "fixtures",
        "errors": {"token": "Chiave non valida."},
        "results": 0,
        "response": [],
    })
    with pytest.raises(ApiFootballError, match="token"):
        client.get("fixtures", {"season": 2024})


@respx.mock
def test_errore_http(client):
    _mock({"message": "errore interno"}, status=500)
    with pytest.raises(ApiFootballError, match="500"):
        client.get("fixtures", {"season": 2024})


@respx.mock
def test_limite_lato_api(client):
    _mock({}, status=429)
    with pytest.raises(ApiFootballError, match="429"):
        client.get("fixtures", {"season": 2024})


@respx.mock
def test_una_stagione_costa_una_richiesta(client, risposta_api):
    """Il presupposto che rende il progetto fattibile sul piano gratuito."""
    route = _mock(risposta_api)
    client.fixtures(season=2024)
    assert route.call_count == 1
    assert client.budget.used == 1
    assert route.calls[0].request.url.params["league"] == "135"


@respx.mock
def test_variante_rapidapi(tmp_path, risposta_api):
    c = ApiFootballClient.for_rapidapi(
        api_key="k",
        cache=ResponseCache(root=tmp_path / "c"),
        budget=RequestBudget(state_path=tmp_path / "b.json"),
    )
    route = respx.get(url__startswith="https://api-football-v1.p.rapidapi.com").mock(
        return_value=httpx.Response(200, json=risposta_api)
    )
    c.get("fixtures", {"season": 2024})
    assert route.calls[0].request.headers["x-rapidapi-key"] == "k"
    assert route.calls[0].request.headers["x-rapidapi-host"]


# ---- il freno fra una richiesta e l'altra -----------------------------------
#
# API-Football ha DUE tetti: quello giornaliero, che il budget difende, e uno
# al minuto — 10 richieste sul piano gratuito. Il secondo e' piu' insidioso,
# perche' superarlo fa rispondere 429 mentre la richiesta viene contata lo
# stesso: si paga e non si riceve il dato. E' successo il 22/08/2026, e la
# raccolta ha perso gli indisponibili di quattro partite.


@pytest.fixture
def orologio(monkeypatch):
    """Un orologio finto, cosi' i test del freno non dormono davvero."""

    class Orologio:
        def __init__(self):
            self.adesso = 1000.0
            self.dormite = []

        def monotonic(self):
            return self.adesso

        def sleep(self, secondi):
            self.dormite.append(secondi)
            self.adesso += secondi

    falso = Orologio()
    monkeypatch.setattr("serie_a_analyzer.data.client.time.monotonic", falso.monotonic)
    monkeypatch.setattr("serie_a_analyzer.data.client.time.sleep", falso.sleep)
    return falso


def _client_frenato(tmp_path, secondi=7.0):
    return ApiFootballClient(
        api_key="k",
        cache=ResponseCache(root=tmp_path / "cache"),
        budget=RequestBudget(state_path=tmp_path / "budget.json", daily_limit=100),
        min_interval_seconds=secondi,
    )


@respx.mock
def test_la_prima_richiesta_non_aspetta(tmp_path, orologio, risposta_api):
    _mock(risposta_api)
    _client_frenato(tmp_path).fixtures(season=2024)
    assert orologio.dormite == []


@respx.mock
def test_il_freno_distanzia_due_richieste(tmp_path, orologio, risposta_api):
    _mock(risposta_api)
    c = _client_frenato(tmp_path)
    c.fixtures(season=2024)
    c.fixtures(season=2023)
    assert orologio.dormite == [pytest.approx(7.0)]


@respx.mock
def test_il_freno_scala_il_tempo_gia_passato(tmp_path, orologio, risposta_api):
    """Chi ha fatto altro nel frattempo non deve aspettare due volte."""
    _mock(risposta_api)
    c = _client_frenato(tmp_path)
    c.fixtures(season=2024)
    orologio.adesso += 5.0          # cinque secondi di lavoro fra le due
    c.fixtures(season=2023)
    assert orologio.dormite == [pytest.approx(2.0)]


@respx.mock
def test_una_risposta_dalla_cache_non_aspetta(tmp_path, orologio, risposta_api):
    """Il freno protegge la rete, e la cache la rete non la tocca."""
    _mock(risposta_api)
    c = _client_frenato(tmp_path)
    c.fixtures(season=2024)
    c.fixtures(season=2024)         # stessa chiamata: cache hit
    assert orologio.dormite == []
    assert c.budget.used == 1


@respx.mock
def test_il_freno_vale_anche_dopo_un_errore(tmp_path, orologio, risposta_api):
    """Un tentativo fallito ha consumato la sua quota al minuto lo stesso.

    E' il motivo per cui il momento della richiesta si segna PRIMA di
    spedirla: se il freno partisse solo dalle chiamate riuscite, un 429
    seguito da un tentativo immediato ne genererebbe un altro.
    """
    rotta = respx.get(url__startswith=f"{API_SPORTS_BASE_URL}/fixtures")
    rotta.mock(return_value=httpx.Response(429, json={}))
    c = _client_frenato(tmp_path)
    with pytest.raises(ApiFootballError):
        c.fixtures(season=2024)

    rotta.mock(return_value=httpx.Response(200, json=risposta_api))
    c.fixtures(season=2023)
    assert orologio.dormite == [pytest.approx(7.0)]


@respx.mock
def test_il_messaggio_del_429_indica_il_tetto_al_minuto(tmp_path, orologio, risposta_api):
    """Il messaggio vecchio mandava a guardare il budget giornaliero, che era a posto."""
    respx.get(url__startswith=f"{API_SPORTS_BASE_URL}/fixtures").mock(
        return_value=httpx.Response(429, json={})
    )
    with pytest.raises(ApiFootballError, match="MINUTO"):
        _client_frenato(tmp_path).fixtures(season=2024)


@respx.mock
def test_il_freno_si_puo_spegnere(tmp_path, orologio, risposta_api):
    _mock(risposta_api)
    c = _client_frenato(tmp_path, secondi=0.0)
    c.fixtures(season=2024)
    c.fixtures(season=2023)
    assert orologio.dormite == []
