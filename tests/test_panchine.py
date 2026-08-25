"""Test della cronologia delle panchine e del peso che ne deriva.

I payload riproducono la forma vera di ``fixtures/lineups``: nessun test tocca
la rete. Cio' che si verifica non e' "il parser gira" ma le cose che, sbagliate,
spezzerebbero la storia di una squadra nel punto sbagliato senza che nessuno se
ne accorga — un allenatore sdoppiato dal nome, una formazione mancante letta
come un esonero, e un traghettatore di una giornata.

Sui pesi la domanda e' un'altra e sta nel modello: una partita giocata sotto un
altro allenatore deve pesare meno, ma **non deve** cancellare l'avversario, che
allenatore non l'ha cambiato.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from serie_a_analyzer.data.panchine import costruisci_panchine
from serie_a_analyzer.data.parsers import FixtureRecord
from serie_a_analyzer.data.store import SeasonStore
from serie_a_analyzer.model.dixon_coles import Match, _pesi_allenatore

MILAN, INTER, ROMA = 489, 505, 497
PIOLI, FONSECA, CONCEICAO = 2000, 2001, 2002
INZAGHI, MOURINHO = 3000, 4000


# ---- materiale di prova -----------------------------------------------------


def _partita(api_id, giorno, casa=MILAN, ospite=INTER, season=2024):
    return FixtureRecord(
        api_id=api_id, season=season, league_id=135,
        round_label="Regular Season - 1", matchday=1,
        kickoff_utc=datetime(2024, 9, giorno, 18, 45, tzinfo=timezone.utc),
        status="finished",
        home_team_id=casa, home_team_name=str(casa),
        away_team_id=ospite, away_team_name=str(ospite),
        home_goals=1, away_goals=0,
        home_goals_ht=None, away_goals_ht=None, venue_name="Stadio",
    )


def _formazioni(*blocchi):
    """Un payload di ``fixtures/lineups``: (squadra, id allenatore, nome)."""
    return [
        {
            "team": {"id": tid, "name": str(tid)},
            "formation": "4-3-3",
            "coach": {"id": cid, "name": nome},
            "startXI": [],
            "substitutes": [],
        }
        for tid, cid, nome in blocchi
    ]


@pytest.fixture
def store(tmp_path):
    return SeasonStore(root=tmp_path / "serie_a")


def _archivio(store, partite):
    """Registra le partite nell'archivio grezzo e costruisce le panchine.

    Il passaggio non e' cerimoniale: si guardano solo le partite di cui
    possediamo l'archivio, non tutte quelle che il modello conosce.
    """
    store.upsert_fixtures([{"fixture": {"id": f.api_id}} for f in partite])
    return costruisci_panchine(store, partite)


# ---- la cronologia ----------------------------------------------------------


def test_un_allenatore_solo_e_un_tratto_solo(store):
    partite = [_partita(i, i) for i in (1, 2, 3)]
    for f in partite:
        store.save_detail(f.api_id, "lineups",
                          _formazioni((MILAN, PIOLI, "S. Pioli"), (INTER, INZAGHI, "S. Inzaghi")))

    panchine = _archivio(store, partite)

    tratti = panchine.cronologia(MILAN)
    assert len(tratti) == 1
    assert (tratti[0].allenatore_id, tratti[0].partite) == (PIOLI, 3)
    assert panchine.cambi(MILAN) == []


def test_l_identita_e_l_id_non_il_nome(store):
    """"Pioli" e "S. Pioli" sono lo stesso uomo: un cambio inventato spezzerebbe
    la storia della squadra nel punto sbagliato."""
    partite = [_partita(i, i) for i in (1, 2)]
    store.save_detail(1, "lineups", _formazioni((MILAN, PIOLI, "Stefano Pioli")))
    store.save_detail(2, "lineups", _formazioni((MILAN, PIOLI, "S. Pioli")))

    panchine = _archivio(store, partite)

    assert len(panchine.cronologia(MILAN)) == 1
    # Vince il nome piu' recente: e' solo un'etichetta da mostrare.
    assert panchine.cronologia(MILAN)[0].nome == "S. Pioli"


def test_un_cambio_apre_un_tratto_nuovo(store):
    partite = [_partita(i, i) for i in (1, 2, 3)]
    store.save_detail(1, "lineups", _formazioni((MILAN, PIOLI, "S. Pioli")))
    store.save_detail(2, "lineups", _formazioni((MILAN, FONSECA, "P. Fonseca")))
    store.save_detail(3, "lineups", _formazioni((MILAN, FONSECA, "P. Fonseca")))

    panchine = _archivio(store, partite)

    tratti = panchine.cronologia(MILAN)
    assert [t.allenatore_id for t in tratti] == [PIOLI, FONSECA]
    assert tratti[1].dal == date(2024, 9, 2)
    assert tratti[1].partite == 2
    assert panchine.attuale(MILAN).allenatore_id == FONSECA


def test_una_formazione_mancante_non_spezza_un_tratto(store):
    """"Non lo so" non e' "e' cambiato".

    Se una partita non ha le formazioni — non ancora raccolte, o una stagione
    che non le avra' mai — quella partita non dice niente su chi allenava.
    Trattarla come un cambio produrrebbe esoneri dal nulla.
    """
    partite = [_partita(i, i) for i in (1, 2, 3)]
    store.save_detail(1, "lineups", _formazioni((MILAN, PIOLI, "S. Pioli")))
    # la 2 non ce l'ha
    store.save_detail(3, "lineups", _formazioni((MILAN, PIOLI, "S. Pioli")))

    panchine = _archivio(store, partite)

    tratti = panchine.cronologia(MILAN)
    assert len(tratti) == 1
    assert tratti[0].partite == 2          # due partite lette, non tre
    assert panchine.allenatore(2, MILAN) is None


def test_il_traghettatore_di_una_giornata_e_un_tratto(store):
    """Il vice per una partita sola apre e chiude un tratto, e il titolare che
    torna ne apre un terzo.

    E' successo davvero (Cremonese, marzo 2026) ed e' il comportamento voluto:
    inventare una soglia sulla lunghezza dei tratti per "ricucire" nasconderebbe
    un fatto vero sotto una regola arbitraria.
    """
    partite = [_partita(i, i) for i in (1, 2, 3)]
    store.save_detail(1, "lineups", _formazioni((MILAN, PIOLI, "S. Pioli")))
    store.save_detail(2, "lineups", _formazioni((MILAN, CONCEICAO, "Il vice")))
    store.save_detail(3, "lineups", _formazioni((MILAN, PIOLI, "S. Pioli")))

    tratti = _archivio(store, partite).cronologia(MILAN)

    assert [t.allenatore_id for t in tratti] == [PIOLI, CONCEICAO, PIOLI]


def test_una_stagione_senza_formazioni_non_ha_panchine(store):
    """Lo storico dalla cache e le stagioni importate da un file: solo risultati."""
    partite = [_partita(i, i) for i in (1, 2)]

    panchine = _archivio(store, partite)

    assert panchine.vuoto
    assert panchine.squadre() == []
    assert panchine.partite_con_formazioni == 0


def test_una_partita_non_conclusa_non_entra(store):
    """Le formazioni di una partita in programma non ci sono, ma il filtro sta
    a monte: la cronologia si costruisce sul giocato."""
    programmata = _partita(1, 1)
    programmata = FixtureRecord(**{**programmata.__dict__, "status": "scheduled",
                                   "home_goals": None, "away_goals": None})
    store.save_detail(1, "lineups", _formazioni((MILAN, PIOLI, "S. Pioli")))

    panchine = _archivio(store, [programmata])

    assert panchine.vuoto


# ---- il peso che ne deriva --------------------------------------------------


def _m(casa, ospite, giorno, all_casa=None, all_ospite=None):
    return Match(
        home=casa, away=ospite, home_goals=1, away_goals=0,
        played_on=date(2024, 9, giorno),
        allenatore_casa=all_casa, allenatore_ospite=all_ospite,
    )


def test_senza_allenatori_i_pesi_non_si_muovono():
    """Il caso di sempre: nessuna formazione sul disco, stima identica a prima."""
    partite = [_m("Milan", "Inter", 1), _m("Milan", "Roma", 8)]

    assert list(_pesi_allenatore(partite, 0.0)) == [1.0, 1.0]


def test_col_fattore_a_uno_non_succede_niente():
    partite = [_m("Milan", "Inter", 1, PIOLI, INZAGHI),
               _m("Milan", "Roma", 8, FONSECA, MOURINHO)]

    assert list(_pesi_allenatore(partite, 1.0)) == [1.0, 1.0]


def test_la_partita_sotto_un_altro_allenatore_pesa_meno():
    """Il Milan cambia: la sua partita di prima vale meno, ma non e' azzerata.

    Meta' di cio' che quella partita insegna riguarda l'Inter, che allenatore
    non l'ha cambiato: da qui la media dei due fattori e non il prodotto.
    """
    partite = [
        _m("Milan", "Inter", 1, PIOLI, INZAGHI),      # gestione vecchia del Milan
        _m("Milan", "Roma", 8, FONSECA, MOURINHO),    # gestione attuale
    ]

    pesi = _pesi_allenatore(partite, 0.5)

    assert pesi[0] == pytest.approx(0.75)   # (0.5 del Milan + 1.0 dell'Inter) / 2
    assert pesi[1] == pytest.approx(1.0)


def test_se_cambiano_tutte_e_due_il_fattore_si_applica_pieno():
    partite = [
        _m("Milan", "Inter", 1, PIOLI, INZAGHI),
        _m("Milan", "Inter", 8, FONSECA, MOURINHO),
    ]

    pesi = _pesi_allenatore(partite, 0.4)

    assert pesi[0] == pytest.approx(0.4)
    assert pesi[1] == pytest.approx(1.0)


def test_chi_allena_adesso_e_l_ultimo_del_campione():
    """Non l'allenatore di oggi: quello dell'ultima partita che si sta guardando.

    E' cio' che rende onesto il backtest senza regole in piu': alla decima
    giornata il campione si ferma li', e "adesso" e' l'allenatore di allora.
    """
    fino_a_meta = [
        _m("Milan", "Inter", 1, PIOLI, INZAGHI),
        _m("Milan", "Roma", 8, PIOLI, MOURINHO),
    ]

    # Con Pioli ancora in panchina nessuna partita e' "di un altro".
    assert list(_pesi_allenatore(fino_a_meta, 0.0)) == [1.0, 1.0]


def test_un_allenatore_ignoto_non_e_un_allenatore_diverso():
    """Una partita di cui non sappiamo chi allenava pesa come sempre."""
    partite = [
        _m("Milan", "Inter", 1, None, INZAGHI),
        _m("Milan", "Inter", 8, FONSECA, INZAGHI),
    ]

    assert list(_pesi_allenatore(partite, 0.0)) == [1.0, 1.0]


def test_il_fattore_sta_fra_zero_e_uno():
    with pytest.raises(ValueError):
        _pesi_allenatore([_m("Milan", "Inter", 1)], 1.5)


# ---- la stima intera --------------------------------------------------------


def test_col_fattore_a_uno_la_stima_e_identica_a_prima():
    """La rete di sicurezza: acceso il modulo, chi non lo usa non se ne accorge."""
    from serie_a_analyzer.model.dixon_coles import fit_dixon_coles

    partite = []
    for i, giorno in enumerate(range(1, 25)):
        casa, ospite = ("Milan", "Inter") if i % 2 else ("Inter", "Milan")
        partite.append(Match(
            home=casa, away=ospite, home_goals=2, away_goals=1,
            played_on=date(2024, 9, 1) + timedelta(days=giorno * 3),
            allenatore_casa=PIOLI if casa == "Milan" else INZAGHI,
            allenatore_ospite=PIOLI if ospite == "Milan" else INZAGHI,
        ))
    partite.append(Match(
        home="Milan", away="Roma", home_goals=1, away_goals=1,
        played_on=date(2025, 1, 5), allenatore_casa=FONSECA, allenatore_ospite=MOURINHO,
    ))

    con = fit_dixon_coles(partite, fattore_allenatore=1.0)
    senza = fit_dixon_coles([
        Match(m.home, m.away, m.home_goals, m.away_goals, m.played_on) for m in partite
    ], fattore_allenatore=1.0)

    assert con.attack["Milan"] == pytest.approx(senza.attack["Milan"], abs=1e-9)
    assert con.fattore_allenatore == 1.0


def test_il_fattore_toglie_sostanza_a_chi_ha_cambiato():
    """Dimenticare e' anche perdere: chi ha cambiato panchina finisce con meno
    dati alle spalle, e l'indicatore di affidabilita' lo dice da solo."""
    from serie_a_analyzer.model.dixon_coles import fit_dixon_coles

    partite = []
    for i, giorno in enumerate(range(1, 25)):
        casa, ospite = ("Milan", "Inter") if i % 2 else ("Inter", "Milan")
        partite.append(Match(
            home=casa, away=ospite, home_goals=2, away_goals=1,
            played_on=date(2024, 9, 1) + timedelta(days=giorno * 3),
            allenatore_casa=PIOLI if casa == "Milan" else INZAGHI,
            allenatore_ospite=PIOLI if ospite == "Milan" else INZAGHI,
        ))
    partite.append(Match(
        home="Milan", away="Inter", home_goals=1, away_goals=1,
        played_on=date(2025, 1, 5), allenatore_casa=FONSECA, allenatore_ospite=INZAGHI,
    ))

    pieno = fit_dixon_coles(partite, fattore_allenatore=1.0)
    ridotto = fit_dixon_coles(partite, fattore_allenatore=0.2)

    assert (ridotto.evidence["Milan"].effective_sample_size
            < pieno.evidence["Milan"].effective_sample_size)
    assert ridotto.evidence["Milan"].pull > pieno.evidence["Milan"].pull


# ---- cosa ne dice la pagina -------------------------------------------------


def test_si_dichiara_solo_un_cambio_recente(store):
    """Oltre un'emivita le partite di prima pesano gia' quasi zero da sole:
    allarmare allora sarebbe allarmare per un problema gia' risolto."""
    from serie_a_analyzer.web.views import cambio_panchina

    partite = [_partita(i, i) for i in (1, 2)]
    store.save_detail(1, "lineups", _formazioni((MILAN, PIOLI, "S. Pioli")))
    store.save_detail(2, "lineups", _formazioni((MILAN, FONSECA, "P. Fonseca")))
    panchine = _archivio(store, partite)

    subito = cambio_panchina(panchine, "Milan", MILAN, date(2024, 10, 1), conta=False)
    tardi = cambio_panchina(panchine, "Milan", MILAN, date(2026, 1, 1), conta=False)

    assert subito is not None
    assert (subito.allenatore, subito.precedente) == ("P. Fonseca", "S. Pioli")
    assert subito.conta is False, "il modello non ne tiene conto, e va detto"
    assert tardi is None


def test_una_squadra_senza_cambi_non_ha_niente_da_dichiarare(store):
    from serie_a_analyzer.web.views import cambio_panchina

    partite = [_partita(i, i) for i in (1, 2)]
    for f in partite:
        store.save_detail(f.api_id, "lineups", _formazioni((MILAN, PIOLI, "S. Pioli")))

    panchine = _archivio(store, partite)

    assert cambio_panchina(panchine, "Milan", MILAN, date(2024, 10, 1), conta=False) is None
