"""Dati di esempio nel formato di API-Football.

Riprodotti dalla forma reale delle risposte v3, cosi' i parser possono essere
verificati senza spendere richieste del budget giornaliero.
"""

import pytest


@pytest.fixture
def fixture_conclusa():
    return {
        "fixture": {
            "id": 1035037,
            "referee": "D. Massa",
            "timezone": "UTC",
            "date": "2024-08-17T16:30:00+00:00",
            "timestamp": 1723912200,
            "venue": {"id": 907, "name": "Stadio Giuseppe Meazza", "city": "Milano"},
            "status": {"long": "Match Finished", "short": "FT", "elapsed": 90},
        },
        "league": {
            "id": 135,
            "name": "Serie A",
            "country": "Italy",
            "season": 2024,
            "round": "Regular Season - 1",
        },
        "teams": {
            "home": {"id": 505, "name": "Inter", "winner": True},
            "away": {"id": 503, "name": "Torino", "winner": False},
        },
        "goals": {"home": 2, "away": 0},
        "score": {
            "halftime": {"home": 1, "away": 0},
            "fulltime": {"home": 2, "away": 0},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }


@pytest.fixture
def fixture_programmata():
    return {
        "fixture": {
            "id": 1035200,
            "timezone": "UTC",
            "date": "2025-09-14T18:45:00+00:00",
            "venue": {"id": 910, "name": "Stadio Olimpico", "city": "Roma"},
            "status": {"long": "Not Started", "short": "NS", "elapsed": None},
        },
        "league": {"id": 135, "name": "Serie A", "season": 2025, "round": "Regular Season - 3"},
        "teams": {
            "home": {"id": 497, "name": "AS Roma", "winner": None},
            "away": {"id": 492, "name": "Napoli", "winner": None},
        },
        "goals": {"home": None, "away": None},
        "score": {"halftime": {"home": None, "away": None}},
    }


@pytest.fixture
def fixture_rinviata():
    return {
        "fixture": {
            "id": 1035300,
            "timezone": "UTC",
            "date": "2025-02-02T14:00:00+00:00",
            "venue": {"id": 912, "name": "Stadio Luigi Ferraris", "city": "Genova"},
            "status": {"long": "Match Postponed", "short": "PST", "elapsed": None},
        },
        "league": {"id": 135, "name": "Serie A", "season": 2024, "round": "Regular Season - 23"},
        "teams": {
            "home": {"id": 495, "name": "Genoa", "winner": None},
            "away": {"id": 487, "name": "Lazio", "winner": None},
        },
        "goals": {"home": None, "away": None},
        "score": {"halftime": {"home": None, "away": None}},
    }


@pytest.fixture
def team_raw():
    return {
        "team": {
            "id": 505,
            "name": "Inter",
            "code": "INT",
            "country": "Italy",
            "founded": 1908,
            "national": False,
        },
        "venue": {
            "id": 907,
            "name": "Stadio Giuseppe Meazza",
            "city": "Milano",
            "capacity": 80018,
        },
    }


@pytest.fixture
def player_raw():
    return {
        "player": {
            "id": 30556,
            "name": "L. Martinez",
            "firstname": "Lautaro Javier",
            "lastname": "Martinez",
            "age": 27,
            "birth": {"date": "1997-08-22", "place": "Bahia Blanca", "country": "Argentina"},
            "nationality": "Argentina",
            "height": "174 cm",
            "weight": "72 kg",
            "injured": False,
            "position": "Attacker",
        }
    }


@pytest.fixture
def risposta_api(fixture_conclusa, fixture_programmata):
    """Involucro completo di una risposta v3."""
    return {
        "get": "fixtures",
        "parameters": {"league": "135", "season": "2024"},
        "errors": [],
        "results": 2,
        "paging": {"current": 1, "total": 1},
        "response": [fixture_conclusa, fixture_programmata],
    }
