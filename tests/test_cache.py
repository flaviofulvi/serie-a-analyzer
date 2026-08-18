"""Test della cache su disco."""

import time

import pytest

from serie_a_analyzer.data.cache import TTL_FOREVER, ResponseCache


@pytest.fixture
def cache(tmp_path):
    return ResponseCache(root=tmp_path / "cache")


def test_scrittura_e_lettura(cache):
    cache.set("fixtures", {"league": 135, "season": 2024}, {"response": [1, 2, 3]})
    letto = cache.get("fixtures", {"league": 135, "season": 2024})
    assert letto == {"response": [1, 2, 3]}


def test_assente_restituisce_none(cache):
    assert cache.get("fixtures", {"league": 135}) is None


def test_i_parametri_fanno_parte_della_chiave(cache):
    cache.set("fixtures", {"season": 2024}, {"a": 1})
    assert cache.get("fixtures", {"season": 2025}) is None
    assert cache.get("fixtures", {"season": 2024}) == {"a": 1}


def test_ordine_dei_parametri_irrilevante(cache):
    cache.set("fixtures", {"league": 135, "season": 2024}, {"a": 1})
    # Stessi parametri, ordine invertito: deve essere la stessa voce.
    assert cache.get("fixtures", {"season": 2024, "league": 135}) == {"a": 1}


def test_scadenza(cache):
    cache.set("lineups", {"fixture": 1}, {"a": 1})
    assert cache.get("lineups", {"fixture": 1}, ttl=60) == {"a": 1}
    # Un TTL gia' trascorso deve invalidare la voce.
    time.sleep(0.01)
    assert cache.get("lineups", {"fixture": 1}, ttl=0.001) is None


def test_ttl_forever_non_scade_mai(cache):
    cache.set("fixtures", {"season": 2021}, {"a": 1})
    assert cache.get("fixtures", {"season": 2021}, ttl=TTL_FOREVER) == {"a": 1}


def test_invalidazione(cache):
    cache.set("odds", {"fixture": 9}, {"a": 1})
    assert cache.invalidate("odds", {"fixture": 9}) is True
    assert cache.get("odds", {"fixture": 9}) is None
    assert cache.invalidate("odds", {"fixture": 9}) is False


def test_file_corrotto_trattato_come_assente(cache):
    cache.set("fixtures", {"season": 2024}, {"a": 1})
    percorso = cache.root / f"{cache.key('fixtures', {'season': 2024})}.json"
    percorso.write_text("{rotto")
    assert cache.get("fixtures", {"season": 2024}) is None


def test_statistiche(cache):
    assert cache.stats()["entries"] == 0
    cache.set("fixtures", {"season": 2024}, {"a": 1})
    cache.set("teams", {"season": 2024}, {"a": 2})
    assert cache.stats()["entries"] == 2
    assert cache.stats()["bytes"] > 0


def test_chiave_leggibile(cache):
    """Il prefisso rende la cache ispezionabile a mano senza decodificare hash."""
    assert cache.key("fixtures/lineups", {"fixture": 1}).startswith("fixtures_lineups__")
