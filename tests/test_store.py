"""Test dell'archivio della stagione in corso e del diario della raccolta.

Il comportamento che conta qui non e' "salva e rilegge": e' che un buco si
veda e che un dettaglio vuoto non venga scambiato per un dettaglio raccolto.
Sulla stagione in corso l'archivio e' l'unica copia esistente, e un errore
silenzioso qui si scopre a fine campionato, quando non c'e' piu' rimedio.
"""

from datetime import date

import pytest

from serie_a_analyzer.data.store import (
    MAX_DETAIL_ATTEMPTS,
    DayJournal,
    SeasonStore,
)


@pytest.fixture
def store(tmp_path):
    return SeasonStore(root=tmp_path / "serie_a")


@pytest.fixture
def journal(tmp_path):
    return DayJournal(path=tmp_path / "journal.json")


def _fixture(fid: int, status: str = "FT", home: int = 1, away: int = 0):
    return {
        "fixture": {"id": fid, "date": "2026-08-22T16:30:00+00:00",
                    "status": {"short": status, "long": status}},
        "league": {"id": 135, "name": "Serie A", "season": 2026},
        "teams": {"home": {"id": 505, "name": "Inter"},
                  "away": {"id": 503, "name": "Torino"}},
        "goals": {"home": home, "away": away},
    }


# ---- archivio delle partite ------------------------------------------------


def test_inserimento_e_rilettura(store):
    nuove, aggiornate = store.upsert_fixtures([_fixture(1), _fixture(2)])
    assert (nuove, aggiornate) == (2, 0)
    assert sorted(store.fixture_ids()) == [1, 2]


def test_ripassare_lo_stesso_giorno_non_duplica(store):
    store.upsert_fixtures([_fixture(1)])
    nuove, aggiornate = store.upsert_fixtures([_fixture(1)])
    assert (nuove, aggiornate) == (0, 0)
    assert store.fixture_ids() == [1]


def test_una_partita_che_finisce_viene_aggiornata(store):
    """Vista prima in corso e poi conclusa deve restare una sola riga."""
    store.upsert_fixtures([_fixture(1, status="1H", home=0, away=0)])
    nuove, aggiornate = store.upsert_fixtures([_fixture(1, status="FT", home=2, away=1)])

    assert (nuove, aggiornate) == (0, 1)
    assert len(store.fixture_ids()) == 1
    assert store.load_fixtures()["1"]["goals"] == {"home": 2, "away": 1}


def test_partita_senza_id_viene_ignorata(store):
    store.upsert_fixtures([{"fixture": {}, "league": {"id": 135}}])
    assert store.fixture_ids() == []


def test_archivio_vuoto_non_esplode(store):
    assert store.load_fixtures() == {}
    assert store.stats()["partite"] == 0


# ---- dettagli --------------------------------------------------------------


def test_dettaglio_pieno_non_va_richiesto_di_nuovo(store):
    store.save_detail(1, "lineups", [{"team": {"name": "Inter"}}])
    assert store.has_detail(1, "lineups")
    assert not store.needs_detail(1, "lineups")


def test_dettaglio_mai_chiesto_serve(store):
    assert store.needs_detail(1, "lineups")


def test_dettaglio_vuoto_si_riprova_ma_non_per_sempre(store):
    """Una risposta vuota e' ambigua: puo' essere "non ancora pubblicato".

    Va riprovata qualche volta, ma non all'infinito: su un campionato che quel
    dato non lo copre si brucerebbe budget a ogni esecuzione, ogni giorno.
    """
    for tentativo in range(1, MAX_DETAIL_ATTEMPTS):
        store.save_detail(1, "lineups", [])
        assert store.needs_detail(1, "lineups"), f"tentativo {tentativo}"

    store.save_detail(1, "lineups", [])
    assert not store.needs_detail(1, "lineups")
    assert not store.has_detail(1, "lineups")


def test_un_dettaglio_che_arriva_dopo_un_vuoto_chiude_la_partita(store):
    store.save_detail(1, "lineups", [])
    store.save_detail(1, "lineups", [{"team": {"name": "Inter"}}])
    assert store.has_detail(1, "lineups")
    assert not store.needs_detail(1, "lineups")


def test_i_dettagli_non_si_mescolano_fra_partite_e_tipi(store):
    store.save_detail(1, "lineups", ["a"])
    store.save_detail(2, "lineups", ["b"])
    store.save_detail(1, "events", ["c"])

    assert store.load_detail(1, "lineups") == ["a"]
    assert store.load_detail(2, "lineups") == ["b"]
    assert store.load_detail(1, "events") == ["c"]
    assert store.load_detail(2, "events") is None


# ---- diario ----------------------------------------------------------------


# Tutte le date dei test sono giudicate rispetto a un "oggi" esplicito: la
# chiusura di un giorno dipende da quanto e' passato, e un test non puo'
# cambiare esito a seconda del giorno in cui viene eseguito.
OGGI = date(2026, 8, 23)


def _registra(journal, giorno, fixtures, final):
    journal.record(
        giorno, collected_at="2026-08-22T23:00:00+00:00",
        n_fixtures=fixtures, n_final=final, requests_spent=1,
    )


def test_un_giorno_passato_con_tutte_le_partite_finite_e_chiuso(journal):
    _registra(journal, date(2026, 8, 22), fixtures=3, final=3)
    assert journal.is_closed(date(2026, 8, 22), today=OGGI)


def test_un_giorno_con_partite_in_corso_resta_aperto(journal):
    _registra(journal, date(2026, 8, 22), fixtures=3, final=1)
    assert not journal.is_closed(date(2026, 8, 22), today=OGGI)


def test_un_giorno_passato_senza_partite_e_chiuso(journal):
    """Ormai non c'e' nulla che possa ancora arrivare: inutile richiederlo."""
    _registra(journal, date(2026, 8, 20), fixtures=0, final=0)
    assert journal.is_closed(date(2026, 8, 20), today=OGGI)


def test_un_giorno_ancora_da_giocare_non_e_mai_chiuso(journal):
    """Vuoto oggi non vuol dire vuoto domani: i recuperi si aggiungono dopo."""
    _registra(journal, date(2026, 8, 24), fixtures=0, final=0)
    assert not journal.is_closed(date(2026, 8, 24), today=OGGI)

    _registra(journal, date(2026, 8, 25), fixtures=2, final=2)
    assert not journal.is_closed(date(2026, 8, 25), today=OGGI)


def test_oggi_non_e_chiuso_ma_domani_lo_sara(journal):
    _registra(journal, OGGI, fixtures=2, final=2)
    assert not journal.is_closed(OGGI, today=OGGI)
    assert journal.is_closed(OGGI, today=date(2026, 8, 24))


def test_una_chiusura_anticipata_gia_scritta_viene_ignorata(journal, tmp_path):
    """Difesa in lettura: un diario di una versione precedente non fa danni."""
    import json
    journal.path.write_text(json.dumps({"days": {"2026-08-24": {
        "collected_at": "2026-08-23T08:00:00+00:00",
        "fixtures": 0, "final": 0, "closed": True, "requests_spent": 1,
    }}}), encoding="utf-8")

    assert not journal.is_closed(date(2026, 8, 24), today=OGGI)


def test_un_giorno_mai_visto_non_e_chiuso(journal):
    assert not journal.is_closed(date(2026, 8, 22), today=OGGI)


def test_i_buchi_vengono_elencati(journal):
    _registra(journal, date(2026, 8, 22), fixtures=1, final=1)
    _registra(journal, date(2026, 8, 25), fixtures=1, final=1)

    buchi = journal.missing_days(date(2026, 8, 22), date(2026, 8, 25))
    assert buchi == [date(2026, 8, 23), date(2026, 8, 24)]


def test_nessun_buco_se_la_raccolta_e_continua(journal):
    for giorno in range(22, 26):
        _registra(journal, date(2026, 8, giorno), fixtures=0, final=0)
    assert journal.missing_days(date(2026, 8, 22), date(2026, 8, 25)) == []


def test_i_giorni_raccolti_sono_in_ordine(journal):
    _registra(journal, date(2026, 8, 25), fixtures=0, final=0)
    _registra(journal, date(2026, 8, 22), fixtures=0, final=0)
    assert journal.collected_days() == [date(2026, 8, 22), date(2026, 8, 25)]


def test_il_diario_sopravvive_a_un_file_corrotto(tmp_path):
    """Meglio ripartire da zero che bloccare la raccolta di oggi."""
    path = tmp_path / "journal.json"
    path.write_text("{ non e' json", encoding="utf-8")
    j = DayJournal(path=path)
    assert j.collected_days() == []
    _registra(j, date(2026, 8, 22), fixtures=1, final=1)
    assert j.is_closed(date(2026, 8, 22), today=OGGI)
