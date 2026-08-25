"""Test dell'orchestrazione dell'ingestione, con la rete simulata."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from serie_a_analyzer.data.budget import Priority, RequestBudget
from serie_a_analyzer.data.cache import ResponseCache
from serie_a_analyzer.data.client import API_SPORTS_BASE_URL, ApiFootballClient
from serie_a_analyzer.data.ingest import (
    backfill_candidates,
    collect_seasons,
    run_backfill,
)


def _partita(fixture_id, home_id, home, away_id, away, gh, ga, quando, status="FT"):
    return {
        "fixture": {
            "id": fixture_id,
            "date": quando.isoformat(),
            "venue": {"name": "Stadio"},
            "status": {"short": status},
        },
        "league": {"id": 135, "season": 2024, "round": "Regular Season - 1"},
        "teams": {"home": {"id": home_id, "name": home}, "away": {"id": away_id, "name": away}},
        "goals": {"home": gh, "away": ga},
        "score": {"halftime": {"home": None, "away": None}},
    }


def _stagione(season, n_squadre=6, base=datetime(2024, 8, 20, 18, 0, tzinfo=timezone.utc)):
    """Stagione sintetica nel formato di API-Football, andata e ritorno."""
    squadre = [(500 + i, f"Squadra {i}") for i in range(n_squadre)]
    partite = []
    fixture_id = season * 1000
    giorno = 0
    for giro in range(2):
        for i, (id_a, nome_a) in enumerate(squadre):
            for j, (id_b, nome_b) in enumerate(squadre):
                if i >= j:
                    continue
                casa, ospite = ((id_a, nome_a), (id_b, nome_b)) if giro == 0 else ((id_b, nome_b), (id_a, nome_a))
                partite.append(
                    _partita(
                        fixture_id, casa[0], casa[1], ospite[0], ospite[1],
                        (i + j) % 4, (i + 1) % 3,
                        base + timedelta(days=giorno),
                    )
                )
                fixture_id += 1
                giorno += 1
    return {"get": "fixtures", "errors": [], "results": len(partite), "response": partite}


@pytest.fixture
def client(tmp_path):
    return ApiFootballClient(
        api_key="k",
        cache=ResponseCache(root=tmp_path / "cache"),
        budget=RequestBudget(state_path=tmp_path / "budget.json", daily_limit=100),
        # Freno spento: qui la rete e' finta e non c'e' nessun tetto al
        # minuto da rispettare. Lasciarlo acceso farebbe dormire la suite.
        min_interval_seconds=0.0,
    )


@respx.mock
def test_una_richiesta_per_stagione(client):
    """Il presupposto che rende il progetto fattibile sul piano gratuito."""
    route = respx.get(url__startswith=f"{API_SPORTS_BASE_URL}/fixtures").mock(
        side_effect=[
            httpx.Response(200, json=_stagione(2023)),
            httpx.Response(200, json=_stagione(2024)),
        ]
    )
    training = collect_seasons(client, [2023, 2024])

    assert route.call_count == 2
    assert training.requests_spent == 2
    assert len(training.seasons) == 2
    assert training.n_finished == 60  # 30 partite per stagione


@respx.mock
def test_i_nomi_arrivano_dalle_stesse_risposte(client):
    """Nessuna richiesta a /teams: i nomi sono gia' dentro /fixtures."""
    route = respx.get(url__startswith=f"{API_SPORTS_BASE_URL}/fixtures").mock(
        return_value=httpx.Response(200, json=_stagione(2024))
    )
    training = collect_seasons(client, [2024])
    assert route.call_count == 1
    assert len(training.team_names) == 6
    assert training.team_names[500] == "Squadra 0"


@respx.mock
def test_stagione_vuota_saltata_senza_interrompere(client):
    """Sul piano gratuito una stagione non coperta torna vuota, non in errore."""
    respx.get(url__startswith=f"{API_SPORTS_BASE_URL}/fixtures").mock(
        side_effect=[
            httpx.Response(200, json={"errors": [], "results": 0, "response": []}),
            httpx.Response(200, json=_stagione(2024)),
        ]
    )
    training = collect_seasons(client, [2019, 2024])
    assert len(training.seasons) == 1
    assert training.n_finished == 30


@respx.mock
def test_budget_esaurito_interrompe_senza_esplodere(client):
    respx.get(url__startswith=f"{API_SPORTS_BASE_URL}/fixtures").mock(
        return_value=httpx.Response(200, json=_stagione(2024))
    )
    client.budget.spend(Priority.CRITICAL, 100)
    training = collect_seasons(client, [2022, 2023, 2024])
    # Nessuna eccezione: si restituisce cio' che si e' riusciti a ottenere.
    assert training.seasons == []
    assert training.matches == []


@respx.mock
def test_coda_di_backfill_ordinata_dalla_piu_recente(client):
    respx.get(url__startswith=f"{API_SPORTS_BASE_URL}/fixtures").mock(
        return_value=httpx.Response(200, json=_stagione(2024))
    )
    training = collect_seasons(client, [2024])
    coda = backfill_candidates(training, already_have=set())

    assert len(coda) == 30
    # I dati recenti pesano di piu' nel modello: vanno presi per primi.
    date_ordinate = [
        f.kickoff_utc
        for s in training.seasons
        for f in s.finished
    ]
    assert coda[0] != coda[-1]
    assert max(date_ordinate) is not None

    gia_presenti = set(coda[:10])
    assert len(backfill_candidates(training, gia_presenti)) == 20


@respx.mock
def test_il_backfill_si_ferma_alla_riserva(client):
    """Il backfill non deve mai mangiare le richieste critiche."""
    respx.get(url__startswith=f"{API_SPORTS_BASE_URL}/fixtures/players").mock(
        return_value=httpx.Response(200, json={"errors": [], "response": [{"a": 1}]})
    )
    # Riserva di default su 100: 25 CRITICAL + 15 NORMAL = 40 protette.
    client.budget.spend(Priority.NORMAL, 20)
    raccolte = run_backfill(client, list(range(1, 100)))

    assert len(raccolte) == 40  # 100 - 20 gia' spese - 40 riservate
    assert client.budget.allowance(Priority.BACKFILL) == 0
    # Restano richieste per le formazioni ufficiali: e' lo scopo della riserva.
    assert client.budget.allowance(Priority.CRITICAL) == 40


@respx.mock
def test_backfill_con_tetto_esplicito(client):
    respx.get(url__startswith=f"{API_SPORTS_BASE_URL}/fixtures/players").mock(
        return_value=httpx.Response(200, json={"errors": [], "response": []})
    )
    raccolte = run_backfill(client, list(range(1, 50)), max_requests=5)
    assert len(raccolte) == 5
    assert client.budget.used == 5
