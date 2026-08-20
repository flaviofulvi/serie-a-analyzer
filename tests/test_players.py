"""Test delle schede giocatore.

I payload riproducono la forma vera di ``fixtures/players``,
``fixtures/lineups`` e ``fixtures/events``: nessun test tocca la rete, e
l'archivio e' un `SeasonStore` in una cartella temporanea.

Cio' che si verifica non e' "il parser gira" ma le cose che, sbagliate, fanno
mostrare un numero falso senza che nessuno se ne accorga: ``null`` letto come
dato mancante invece che come zero, un giocatore sdoppiato dal nome, i gol
contati due volte perche' arrivano da due sorgenti, e una copertura parziale
spacciata per stagione intera.
"""

from datetime import datetime, timedelta, timezone

import pytest

from serie_a_analyzer.data.parsers import FixtureRecord
from serie_a_analyzer.data.players import (
    costruisci_archivio,
    formazioni_partita,
    gol_partita,
    prestazioni_partita,
)
from serie_a_analyzer.data.store import SeasonStore

INTER, TORINO, MILAN = 505, 503, 489
ADESSO = datetime(2026, 9, 20, 18, 45, tzinfo=timezone.utc)


# ---- materiale di prova -----------------------------------------------------


def _partita(api_id, quando, casa=INTER, ospite=TORINO, gol=(2, 0)):
    return FixtureRecord(
        api_id=api_id, season=2026, league_id=135,
        round_label="Regular Season - 1", matchday=1,
        kickoff_utc=quando, status="finished",
        home_team_id=casa, home_team_name=str(casa),
        away_team_id=ospite, away_team_name=str(ospite),
        home_goals=gol[0], away_goals=gol[1],
        home_goals_ht=None, away_goals_ht=None, venue_name="Stadio",
    )


def _giocatore(
    pid, nome, minuti=90, titolare=True, gol=None, assist=0,
    voto="6.5", gialli=0, rossi=0, numero=10, posizione="M", tiri=None,
):
    """Una voce di ``fixtures/players``, con i ``null`` dove li mette l'API."""
    return {
        "player": {"id": pid, "name": nome},
        "statistics": [{
            "games": {
                "minutes": minuti, "number": numero, "position": posizione,
                "rating": voto, "captain": False, "substitute": not titolare,
            },
            "shots": {"total": tiri, "on": None},
            "goals": {"total": gol, "conceded": None, "assists": assist, "saves": None},
            "cards": {"yellow": gialli, "red": rossi},
        }],
    }


def _tabellino(*blocchi):
    return [{"team": {"id": tid, "name": str(tid)}, "players": list(giocatori)}
            for tid, giocatori in blocchi]


@pytest.fixture
def store(tmp_path):
    return SeasonStore(root=tmp_path / "serie_a")


# ---- lettura del tabellino --------------------------------------------------


def test_le_prestazioni_arrivano_dal_tabellino(store):
    store.save_detail(1, "players", _tabellino(
        (INTER, [_giocatore(30556, "L. Martinez", gol=2, assist=1, voto="8.2")]),
        (TORINO, [_giocatore(9999, "Portiere", posizione="G", voto="5.1")]),
    ))

    righe = prestazioni_partita(store, 1, ADESSO)

    assert len(righe) == 2
    lautaro = next(p for p in righe if p.player_id == 30556)
    assert (lautaro.gol, lautaro.assist, lautaro.voto) == (2, 1, 8.2)
    assert lautaro.team_id == INTER
    assert lautaro.titolare is True
    assert lautaro.ruolo == "centrocampista"


def test_chi_non_e_entrato_non_fa_presenza(store):
    """Un panchinaro inutilizzato ha i minuti a ``null``: non e' una presenza."""
    store.save_detail(1, "players", _tabellino((INTER, [
        _giocatore(1, "Titolare"),
        _giocatore(2, "Mai entrato", minuti=None, titolare=False, voto=None),
    ])))

    righe = prestazioni_partita(store, 1, ADESSO)

    assert [p.player_id for p in righe] == [1]


def test_null_vale_zero_per_i_gol_ma_non_per_i_tiri(store):
    """Due ``null`` che significano cose diverse, e vanno letti diversamente."""
    store.save_detail(1, "players", _tabellino(
        (INTER, [_giocatore(1, "Senza gol", gol=None, tiri=None)]),
    ))

    riga = prestazioni_partita(store, 1, ADESSO)[0]

    assert riga.gol == 0            # non ha segnato: e' uno zero, non un buco
    assert riga.tiri is None        # l'API non copre il dato: resta ignoto


def test_una_busta_vuota_non_e_un_errore(store):
    """Payload vuoto = "l'API non lo copre". Si restituisce niente, non si esplode."""
    store.save_detail(1, "players", [])

    assert prestazioni_partita(store, 1, ADESSO) == []
    assert formazioni_partita(store, 1) == []
    assert gol_partita(store, 1) == []


def test_un_dettaglio_mai_raccolto_non_rompe_nulla(store):
    assert prestazioni_partita(store, 404, ADESSO) == []


# ---- aggregazione -----------------------------------------------------------


def _archivio_di_due_partite(store):
    """Due giornate: Lautaro segna, cambia nome nell'anagrafica, non cambia id."""
    prima = _partita(1, ADESSO - timedelta(days=7))
    seconda = _partita(2, ADESSO)

    store.upsert_fixtures([
        {"fixture": {"id": 1, "date": prima.kickoff_utc.isoformat()}},
        {"fixture": {"id": 2, "date": seconda.kickoff_utc.isoformat()}},
    ])
    store.save_detail(1, "players", _tabellino((INTER, [
        _giocatore(30556, "L. Martinez", gol=1, assist=0, voto="7.5", minuti=90),
        _giocatore(200, "Riserva", minuti=20, titolare=False, voto=None),
    ])))
    store.save_detail(2, "players", _tabellino((INTER, [
        _giocatore(30556, "Lautaro Martinez", gol=2, assist=1, voto="8.5", minuti=80),
        _giocatore(200, "Riserva", minuti=45, titolare=True, voto="6.0"),
    ])))
    return costruisci_archivio(store, [prima, seconda])


def test_i_totali_sono_la_somma_delle_prestazioni(store):
    scheda = _archivio_di_due_partite(store).scheda(30556)

    assert scheda.presenze == 2
    assert scheda.da_titolare == 2
    assert scheda.minuti == 170
    assert scheda.gol == 3
    assert scheda.assist == 1
    assert scheda.voto_medio == pytest.approx(8.0)


def test_l_identita_e_l_id_non_il_nome(store):
    """Due grafie dello stesso giocatore restano un giocatore solo."""
    archivio = _archivio_di_due_partite(store)

    assert archivio.giocatori_conosciuti == 2
    # Vince il nome dell'ultima partita: e' quello piu' aggiornato.
    assert archivio.scheda(30556).nome == "Lautaro Martinez"


def test_le_prestazioni_partono_dalla_piu_recente(store):
    scheda = _archivio_di_due_partite(store).scheda(30556)

    assert [p.fixture_id for p in scheda.prestazioni] == [2, 1]


def test_il_voto_medio_ignora_le_partite_senza_voto(store):
    """Contare come zero le partite senza voto punirebbe chi gioca di piu'."""
    scheda = _archivio_di_due_partite(store).scheda(200)

    assert scheda.presenze == 2
    assert scheda.partite_con_voto == 1
    assert scheda.voto_medio == pytest.approx(6.0)


def test_senza_nessun_voto_la_media_non_esiste(store):
    store.upsert_fixtures([{"fixture": {"id": 1}}])
    store.save_detail(1, "players", _tabellino((INTER, [_giocatore(1, "Uno", voto=None)])))

    scheda = costruisci_archivio(store, [_partita(1, ADESSO)]).scheda(1)

    assert scheda.voto_medio is None
    assert scheda.presenze == 1


def test_la_squadra_attuale_e_quella_dell_ultima_partita(store):
    """A gennaio si cambia maglia: l'archivio sa dire solo l'ultima vista."""
    prima = _partita(1, ADESSO - timedelta(days=30))
    seconda = _partita(2, ADESSO, casa=MILAN)
    store.upsert_fixtures([{"fixture": {"id": 1}}, {"fixture": {"id": 2}}])
    store.save_detail(1, "players", _tabellino((INTER, [_giocatore(7, "Girovago")])))
    store.save_detail(2, "players", _tabellino((MILAN, [_giocatore(7, "Girovago")])))

    scheda = costruisci_archivio(store, [prima, seconda]).scheda(7)

    assert scheda.squadra_attuale[0] == MILAN
    assert [tid for tid, _ in scheda.squadre] == [MILAN, INTER]


def test_la_rosa_conta_solo_le_partite_giocate_con_quella_squadra(store):
    prima = _partita(1, ADESSO - timedelta(days=30))
    seconda = _partita(2, ADESSO, casa=MILAN)
    store.upsert_fixtures([{"fixture": {"id": 1}}, {"fixture": {"id": 2}}])
    store.save_detail(1, "players", _tabellino((INTER, [_giocatore(7, "Girovago", gol=3)])))
    store.save_detail(2, "players", _tabellino((MILAN, [_giocatore(7, "Girovago", gol=1)])))

    archivio = costruisci_archivio(store, [prima, seconda])

    assert archivio.scheda(7).gol == 3 + 1
    assert archivio.rosa(MILAN)[0].gol == 1
    assert archivio.rosa(INTER)[0].gol == 3
    assert archivio.rosa(TORINO) == []


def test_la_rosa_mette_davanti_chi_gioca_di_piu(store):
    store.upsert_fixtures([{"fixture": {"id": 1}}])
    store.save_detail(1, "players", _tabellino((INTER, [
        _giocatore(1, "Subentrato", minuti=10, titolare=False),
        _giocatore(2, "Titolare", minuti=90),
    ])))

    rosa = costruisci_archivio(store, [_partita(1, ADESSO)]).rosa(INTER)

    assert [g.nome for g in rosa] == ["Titolare", "Subentrato"]


# ---- copertura: cio' che manca va dichiarato --------------------------------


def test_la_copertura_dice_su_quante_partite_sono_i_conti(store):
    con_tabellino = _partita(1, ADESSO - timedelta(days=7))
    senza = _partita(2, ADESSO)
    store.upsert_fixtures([{"fixture": {"id": 1}}, {"fixture": {"id": 2}}])
    store.save_detail(1, "players", _tabellino((INTER, [_giocatore(1, "Uno")])))
    store.save_detail(2, "players", [])   # busta vuota: l'API non lo copre

    archivio = costruisci_archivio(store, [con_tabellino, senza])

    assert archivio.partite_concluse == 2
    assert archivio.partite_con_statistiche == 1
    assert archivio.copertura == pytest.approx(0.5)


def test_lo_storico_senza_dettagli_non_falsa_la_copertura(store):
    """Le stagioni 2022-2024 non hanno tabellini: contarle direbbe una bugia.

    Arrivano dalla cache come elenco di risultati e non passano mai
    dall'archivio della stagione in corso. Se finissero nel denominatore, una
    copertura piena sembrerebbe del 2%.
    """
    corrente = _partita(1, ADESSO)
    store.upsert_fixtures([{"fixture": {"id": 1}}])
    store.save_detail(1, "players", _tabellino((INTER, [_giocatore(1, "Uno")])))
    storiche = [_partita(1000 + i, ADESSO - timedelta(days=900 + i)) for i in range(50)]

    archivio = costruisci_archivio(store, [corrente, *storiche])

    assert archivio.partite_concluse == 1
    assert archivio.copertura == pytest.approx(1.0)


def test_le_partite_da_giocare_restano_fuori(store):
    programmata = FixtureRecord(
        api_id=9, season=2026, league_id=135, round_label=None, matchday=None,
        kickoff_utc=ADESSO + timedelta(days=3), status="scheduled",
        home_team_id=INTER, home_team_name="Inter",
        away_team_id=TORINO, away_team_name="Torino",
        home_goals=None, away_goals=None,
        home_goals_ht=None, away_goals_ht=None, venue_name=None,
    )
    store.upsert_fixtures([{"fixture": {"id": 9}}])

    archivio = costruisci_archivio(store, [programmata])

    assert archivio.vuoto
    assert archivio.partite_concluse == 0
    assert archivio.copertura == 0.0


# ---- formazioni ed eventi ---------------------------------------------------


def test_le_formazioni_distinguono_titolari_e_panchina(store):
    store.save_detail(1, "lineups", [{
        "team": {"id": INTER, "name": "Inter"},
        "formation": "3-5-2",
        "coach": {"id": 1, "name": "Un Allenatore"},
        "startXI": [{"player": {"id": 1, "name": "Portiere", "number": 1, "pos": "G", "grid": "1:1"}}],
        "substitutes": [{"player": {"id": 2, "name": "Riserva", "number": 28, "pos": "M", "grid": None}}],
    }])

    formazione = formazioni_partita(store, 1)[0]

    assert formazione.modulo == "3-5-2"
    assert formazione.allenatore == "Un Allenatore"
    assert [g.nome for g in formazione.titolari] == ["Portiere"]
    assert formazione.titolari[0].ruolo == "portiere"
    assert [g.nome for g in formazione.panchina] == ["Riserva"]


def _evento_gol(minuto, pid, nome, dettaglio="Normal Goal", extra=None, assist=None):
    return {
        "time": {"elapsed": minuto, "extra": extra},
        "team": {"id": INTER, "name": "Inter"},
        "player": {"id": pid, "name": nome},
        "assist": assist or {"id": None, "name": None},
        "type": "Goal",
        "detail": dettaglio,
    }


def test_i_gol_arrivano_in_ordine_e_con_i_recuperi(store):
    store.save_detail(1, "events", [
        _evento_gol(90, 2, "Secondo", extra=3),
        {"time": {"elapsed": 60}, "team": {"id": INTER, "name": "Inter"},
         "player": {"id": 9, "name": "Chi esce"}, "type": "subst", "detail": "Substitution 1"},
        _evento_gol(12, 1, "Primo", dettaglio="Penalty"),
    ])

    gol = gol_partita(store, 1)

    assert [g.minuto for g in gol] == [12, 93]
    assert gol[0].su_rigore is True
    assert gol[1].su_rigore is False


def test_gli_eventi_non_si_sommano_al_tabellino(store):
    """Due sorgenti che si sovrappongono: i conti vengono da una sola.

    Il gol e' scritto sia nel tabellino sia negli eventi. Se il modulo li
    sommasse, ogni marcatore raddoppierebbe i suoi gol senza che nessun test
    banale se ne accorga.
    """
    store.upsert_fixtures([{"fixture": {"id": 1}}])
    store.save_detail(1, "players", _tabellino((INTER, [_giocatore(1, "Uno", gol=1)])))
    store.save_detail(1, "events", [_evento_gol(30, 1, "Uno")])

    archivio = costruisci_archivio(store, [_partita(1, ADESSO)])

    assert archivio.scheda(1).gol == 1
    assert len(gol_partita(store, 1)) == 1


# ---- classifiche ------------------------------------------------------------


def test_la_classifica_per_voto_esclude_chi_ha_giocato_poco(store):
    """Con una partita sola in cima ci finisce chi ha avuto una bella serata."""
    partite = []
    for i in range(6):
        partite.append(_partita(100 + i, ADESSO - timedelta(days=i)))
        store.upsert_fixtures([{"fixture": {"id": 100 + i}}])
        store.save_detail(100 + i, "players", _tabellino((INTER, [
            _giocatore(1, "Costante", voto="7.0"),
        ])))
    partite.append(_partita(200, ADESSO))
    store.upsert_fixtures([{"fixture": {"id": 200}}])
    store.save_detail(200, "players", _tabellino((INTER, [
        _giocatore(2, "Meteora", voto="9.5"),
    ])))

    classifica = costruisci_archivio(store, partite).classifica("voto")

    assert [s.nome for s in classifica] == ["Costante"]


def test_la_classifica_marcatori_ordina_per_gol(store):
    store.upsert_fixtures([{"fixture": {"id": 1}}])
    store.save_detail(1, "players", _tabellino((INTER, [
        _giocatore(1, "Uno", gol=1),
        _giocatore(2, "Due", gol=3),
        _giocatore(3, "Tre", gol=None),
    ])))

    classifica = costruisci_archivio(store, [_partita(1, ADESSO)]).classifica("gol", limite=2)

    assert [s.nome for s in classifica] == ["Due", "Uno"]
