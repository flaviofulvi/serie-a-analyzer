"""Test dei parser delle risposte API-Football."""

from datetime import date

import pytest

from serie_a_analyzer.data.parsers import (
    parse_fixture,
    parse_fixtures,
    parse_height_cm,
    parse_matchday,
    parse_player,
    parse_team,
    to_model_matches,
)


def test_partita_conclusa(fixture_conclusa):
    f = parse_fixture(fixture_conclusa)
    assert f.api_id == 1035037
    assert f.season == 2024
    assert f.status == "finished"
    assert f.is_finished
    assert (f.home_goals, f.away_goals) == (2, 0)
    assert (f.home_goals_ht, f.away_goals_ht) == (1, 0)
    assert f.matchday == 1
    assert f.played_on == date(2024, 8, 17)
    assert f.home_team_id == 505


def test_partita_programmata(fixture_programmata):
    f = parse_fixture(fixture_programmata)
    assert f.status == "scheduled"
    assert not f.is_finished
    assert f.home_goals is None
    assert f.matchday == 3


def test_partita_rinviata(fixture_rinviata):
    """I rinvii sono strutturali in Serie A e non devono passare per concluse."""
    f = parse_fixture(fixture_rinviata)
    assert f.status == "postponed"
    assert not f.is_finished


@pytest.mark.parametrize(
    "etichetta,atteso",
    [
        ("Regular Season - 1", 1),
        ("Regular Season - 38", 38),
        ("Quarter-finals", None),
        (None, None),
        ("", None),
    ],
)
def test_estrazione_giornata(etichetta, atteso):
    assert parse_matchday(etichetta) == atteso


@pytest.mark.parametrize(
    "grezzo,atteso",
    [("174 cm", 174), ("180cm", 180), (None, None), ("", None), ("sconosciuta", None)],
)
def test_estrazione_altezza(grezzo, atteso):
    assert parse_height_cm(grezzo) == atteso


def test_squadra(team_raw):
    t = parse_team(team_raw)
    assert t.api_id == 505
    assert t.name == "Inter"
    assert t.short_name == "INT"
    assert t.founded == 1908
    assert t.venue_city == "Milano"


def test_giocatore(player_raw):
    p = parse_player(player_raw)
    assert p.api_id == 30556
    assert p.birth_date == date(1997, 8, 22)
    assert p.height_cm == 174
    assert p.weight_kg == 72
    assert p.nationality == "Argentina"


def test_giocatore_senza_data_di_nascita(player_raw):
    player_raw["player"]["birth"]["date"] = None
    assert parse_player(player_raw).birth_date is None


def test_righe_malformate_saltate(fixture_conclusa):
    """Una riga rotta non deve far perdere l'intera stagione gia' scaricata."""
    rotte = [fixture_conclusa, {"fixture": {}}, {}, {"fixture": {"id": 1}}]
    risultato = parse_fixtures(rotte)
    assert len(risultato) == 1
    assert risultato[0].api_id == 1035037


def test_solo_le_partite_concluse_alimentano_il_modello(
    fixture_conclusa, fixture_programmata, fixture_rinviata
):
    fixtures = parse_fixtures([fixture_conclusa, fixture_programmata, fixture_rinviata])
    matches = to_model_matches(fixtures)
    assert len(matches) == 1
    assert matches[0].home_goals == 2


def test_le_squadre_sono_identificate_dall_id(fixture_conclusa):
    """Senza mappa dei nomi si usa l'id: mai il nome, che cambia fra endpoint."""
    fixtures = parse_fixtures([fixture_conclusa])
    senza_nomi = to_model_matches(fixtures)
    assert senza_nomi[0].home == "505"

    con_nomi = to_model_matches(fixtures, {505: "Inter", 503: "Torino"})
    assert con_nomi[0].home == "Inter"
    assert con_nomi[0].away == "Torino"
