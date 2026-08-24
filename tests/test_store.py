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
    MAX_TABELLINO_ATTEMPTS,
    DayJournal,
    SeasonStore,
    tabellini_arresi,
    tabellini_pronti,
    tabellino_provvisorio,
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


# ---- tabellini fermi a partita in corso --------------------------------------
#
# Genoa-Napoli e Parma-Cagliari, 22 agosto 2026: l'API ha servito per ore
# l'ultima lettura del feed live come se fosse il tabellino finale. Ventidue
# titolari al 64', nessun gol, nessuna sostituzione. Il rischio non e' la
# giornata storta dell'API — succede — ma che l'archivio la consideri chiusa.


def _tabellino(minuti: int, gol: int = 0, con_riserve: bool = True):
    """Un `/fixtures/players` con tutti i titolari fermi a ``minuti``."""
    def giocatore(pid, min_giocati, gol_suoi=0):
        return {
            "player": {"id": pid, "name": f"Giocatore {pid}"},
            "statistics": [{
                "games": {"minutes": min_giocati, "rating": "6.5",
                          "substitute": min_giocati is None},
                "goals": {"total": gol_suoi or None, "assists": None},
            }],
        }

    squadra = [giocatore(i, minuti) for i in range(1, 12)]
    if gol:
        squadra[10]["statistics"][0]["goals"]["total"] = gol
    if con_riserve:
        squadra += [giocatore(100 + i, None) for i in range(5)]
    return [{"team": {"id": 1, "name": "Genoa"}, "players": squadra}]


def test_tabellino_fermo_a_meta_partita_e_provvisorio():
    assert tabellino_provvisorio(_tabellino(64))
    assert tabellino_provvisorio(_tabellino(61))


def test_tabellino_arrivato_in_fondo_e_definitivo():
    assert not tabellino_provvisorio(_tabellino(90))
    # I recuperi portano oltre il 90': resta definitivo.
    assert not tabellino_provvisorio(_tabellino(96))


def test_un_tabellino_con_un_autogol_non_e_provvisorio():
    """Il controllo guarda i minuti, non i gol, e questo e' il motivo.

    Con un autogol la somma dei gol del tabellino non torna col risultato:
    confrontarli marcherebbe quella partita come provvisoria per sempre.
    """
    assert not tabellino_provvisorio(_tabellino(90, gol=0))


def test_un_tabellino_vuoto_non_e_provvisorio_ma_mancante():
    """Sono due casi diversi e hanno due tetti diversi di tentativi."""
    assert not tabellino_provvisorio([])
    assert not tabellino_provvisorio(None)


def test_tabellino_provvisorio_va_richiesto_di_nuovo(store):
    store.save_detail(1, "players", _tabellino(64))
    assert store.has_detail(1, "players")
    assert store.needs_detail(1, "players")


def test_tabellino_definitivo_chiude_la_partita(store):
    store.save_detail(1, "players", _tabellino(64))
    store.save_detail(1, "players", _tabellino(90))
    assert not store.needs_detail(1, "players")


def test_tabellino_provvisorio_non_si_riprova_per_sempre(store):
    """Se l'API non lo consolida mai, prima o poi si smette di pagarlo."""
    for tentativo in range(1, MAX_TABELLINO_ATTEMPTS):
        store.save_detail(1, "players", _tabellino(64))
        assert store.needs_detail(1, "players"), f"tentativo {tentativo}"

    store.save_detail(1, "players", _tabellino(64))
    assert not store.needs_detail(1, "players")


def test_un_tabellino_provvisorio_non_e_subito_un_caso_perso(store):
    """Nelle ore dopo la partita e' normale: nessuno va disturbato."""
    store.save_detail(1, "players", _tabellino(64))

    assert tabellini_arresi(store, [1]) == []


def test_quando_i_tentativi_finiscono_e_un_caso_perso(store):
    """E' l'unico momento che vale un messaggio: il sistema ha smesso di provare."""
    for _ in range(MAX_TABELLINO_ATTEMPTS):
        store.save_detail(1, "players", _tabellino(64))

    assert not store.needs_detail(1, "players")
    assert tabellini_arresi(store, [1]) == [1]


def test_un_tabellino_buono_non_e_mai_un_caso_perso(store):
    store.save_detail(1, "players", _tabellino(90))
    store.save_detail(2, "players", [])

    assert tabellini_arresi(store, [1, 2]) == []


def test_un_tabellino_finito_e_pronto(store):
    store.save_detail(1, "players", _tabellino(90))

    assert tabellini_pronti(store, [1]) == [1]


def test_un_tabellino_provvisorio_non_e_pronto(store):
    """Non e' un tabellino parziale, e' un tabellino sbagliato: conta come non
    ancora arrivato, che e' la verita' — arrivera'."""
    store.save_detail(1, "players", _tabellino(64))

    assert tabellini_pronti(store, [1]) == []


def test_un_tabellino_che_manca_non_e_pronto(store):
    store.save_detail(1, "players", [])

    assert tabellini_pronti(store, [1, 2]) == []


def test_pronti_e_arresi_non_si_sovrappongono_mai(store):
    """Una partita non puo' essere insieme "arrivata" e "persa": i due
    messaggi che ne nascono dicono il contrario l'uno dell'altro."""
    store.save_detail(1, "players", _tabellino(90))
    for _ in range(MAX_TABELLINO_ATTEMPTS):
        store.save_detail(2, "players", _tabellino(64))

    assert tabellini_pronti(store, [1, 2]) == [1]
    assert tabellini_arresi(store, [1, 2]) == [2]


def test_la_regola_del_tabellino_vale_solo_per_i_tabellini(store):
    """Formazioni ed eventi non hanno minuti giocati: non vanno toccati.

    Gli eventi delle due partite del 22 agosto erano completi mentre il
    tabellino era fermo: applicare la regola a tutto rimetterebbe in coda
    dettagli che vanno benissimo.
    """
    for kind in ("lineups", "events"):
        store.save_detail(2, kind, _tabellino(64))
        assert not store.needs_detail(2, kind), kind


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
