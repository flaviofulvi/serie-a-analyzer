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
