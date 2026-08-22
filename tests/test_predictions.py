"""Test dello storico dei pronostici.

Il comportamento da difendere qui e' uno solo, ma e' il piu' importante del
progetto: un pronostico registrato non cambia piu'. Tutto il resto — non
duplicare, non pronosticare il passato, riderivare i mercati mesi dopo — serve
a rendere quello storico degno di essere usato per misurare il modello.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from serie_a_analyzer.data.predictions import (
    MODE_SECCO,
    MODE_UFFICIALE,
    PredictionStore,
    PredizioneRifiutata,
    registra_partite,
)
from serie_a_analyzer.model.dixon_coles import FittedModel
from serie_a_analyzer.model.markets import derive_markets

ADESSO = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
FISCHIO = ADESSO + timedelta(days=2)


@dataclass
class PartitaFinta:
    """Il minimo che il registro chiede a una partita."""

    api_id: int
    kickoff_utc: datetime
    home_team_id: int
    away_team_id: int


def _modello(base: float = 0.2) -> FittedModel:
    return FittedModel(
        teams=("Inter", "Torino"),
        attack={"Inter": 0.35, "Torino": -0.10},
        defense={"Inter": 0.20, "Torino": -0.05},
        home_advantage=0.25,
        base=base,
        rho=-0.05,
        n_matches=380,
        effective_sample_size=210.0,
        fitted_on=date(2026, 8, 20),
        converged=True,
    )


@pytest.fixture
def store(tmp_path):
    return PredictionStore(path=tmp_path / "pronostici.db")


def _registra(store, modello=None, fixture_id=1, mode=MODE_SECCO, kickoff=FISCHIO):
    modello = modello or _modello()
    matrice = modello.scoreline("Inter", "Torino")
    lam, mu = modello.expected_goals("Inter", "Torino")
    return store.registra(
        fixture_id=fixture_id,
        kickoff_utc=kickoff,
        home_team_id=505,
        away_team_id=503,
        model_version_id=store.versione_modello(modello),
        matrice=matrice,
        probabilita=derive_markets(matrice),
        lambda_home=lam,
        lambda_away=mu,
        rho=modello.rho,
        mode=mode,
        now=ADESSO,
    )


# ---- immutabilita' ----------------------------------------------------------


def test_un_pronostico_non_si_puo_aggiornare(store):
    """La regola non negoziabile, fatta rispettare dal database."""
    pid = _registra(store)

    with store._connessione() as conn:
        with pytest.raises(Exception, match="non si aggiorna"):
            conn.execute("update predictions set lambda_home = 99 where id = ?", (pid,))


def test_un_pronostico_non_si_puo_cancellare(store):
    pid = _registra(store)

    with store._connessione() as conn:
        with pytest.raises(Exception, match="non si cancella"):
            conn.execute("delete from predictions where id = ?", (pid,))


def test_i_mercati_derivati_sono_altrettanto_immutabili(store):
    pid = _registra(store)

    with store._connessione() as conn:
        with pytest.raises(Exception, match="non si aggiorna"):
            conn.execute(
                "update prediction_markets set probability = 0.99 where prediction_id = ?",
                (pid,),
            )


# ---- una riga per versione del modello --------------------------------------


def test_lo_stesso_modello_non_riscrive_lo_stesso_pronostico(store):
    """Il web rigenera i pronostici a ogni visita: non devono moltiplicarsi."""
    assert _registra(store) is not None
    assert _registra(store) is None
    assert store.conteggi()["pronostici"] == 1


def test_un_modello_ristimato_affianca_un_pronostico_nuovo(store):
    """Cosi' si vede come la previsione e' cambiata avvicinandosi alla partita."""
    primo = _registra(store, _modello(base=0.20))
    secondo = _registra(store, _modello(base=0.30))

    assert primo != secondo
    storico = store.storico(fixture_id=1)
    assert len(storico) == 2
    assert storico[0]["lambda_home"] != storico[1]["lambda_home"]
    assert store.conteggi()["versioni_modello"] == 2


def test_le_due_modalita_convivono(store):
    """Secco e formazione ufficiale sono due pronostici diversi, non uno corretto."""
    assert _registra(store, mode=MODE_SECCO) is not None
    assert _registra(store, mode=MODE_UFFICIALE) is not None
    assert store.conteggi()["pronostici"] == 2


def test_una_modalita_inventata_viene_rifiutata(store):
    with pytest.raises(PredizioneRifiutata, match="modalita'"):
        _registra(store, mode="a naso")


# ---- non si pronostica il passato -------------------------------------------


def test_una_partita_gia_iniziata_viene_rifiutata(store):
    """Il modello e' stato stimato su quella partita: sarebbe barare."""
    with pytest.raises(PredizioneRifiutata, match="gia' iniziata"):
        _registra(store, kickoff=ADESSO - timedelta(hours=1))


def test_il_fischio_d_inizio_esatto_e_gia_tardi(store):
    with pytest.raises(PredizioneRifiutata):
        _registra(store, kickoff=ADESSO)


# ---- cosa resta salvato ------------------------------------------------------


def test_la_matrice_si_rilegge_identica(store):
    """E' cio' che permette di riderivare qualsiasi mercato mesi dopo."""
    modello = _modello()
    pid = _registra(store, modello)

    riletta = store.matrice(pid)
    attesa = modello.scoreline("Inter", "Torino")

    assert riletta.shape == attesa.shape
    assert np.allclose(riletta, attesa, atol=1e-9)


def test_i_mercati_riderivati_coincidono_con_quelli_salvati(store):
    pid = _registra(store)
    salvati = {(m["market"], m["selection"]): m["probability"] for m in store.mercati(pid)}
    riderivati = derive_markets(store.matrice(pid))

    assert salvati[("1x2", "home")] == pytest.approx(riderivati.home_win, abs=1e-6)
    assert salvati[("btts", "yes")] == pytest.approx(riderivati.btts, abs=1e-6)
    assert salvati[("over_under", "over_2.5")] == pytest.approx(riderivati.over[2.5], abs=1e-6)


def test_l_1x2_salvato_somma_a_uno(store):
    pid = _registra(store)
    uno_x_due = [m for m in store.mercati(pid) if m["market"] == "1x2"]
    assert sum(m["probability"] for m in uno_x_due) == pytest.approx(1.0, abs=1e-6)


def test_il_pronostico_conserva_il_contesto_della_partita(store):
    _registra(store, fixture_id=42)
    riga = store.ultimo_per_partita(42)

    assert riga["home_team_id"] == 505
    assert riga["away_team_id"] == 503
    assert riga["mode"] == MODE_SECCO
    assert riga["kickoff_utc"] == FISCHIO.isoformat()


# ---- registrazione in blocco -------------------------------------------------


def test_si_registrano_solo_le_partite_pronosticabili(store):
    """Chi non ha storia e chi ha gia' giocato viene contato, non inventato."""
    modello = _modello()
    nomi = {505: "Inter", 503: "Torino", 999: "Neopromossa"}

    esito = registra_partite(
        store, modello,
        [
            PartitaFinta(1, FISCHIO, 505, 503),
            PartitaFinta(2, FISCHIO, 505, 999),                    # squadra ignota
            PartitaFinta(3, ADESSO - timedelta(days=1), 503, 505),  # gia' giocata
        ],
        nomi.get,
        now=ADESSO,
    )

    assert esito == {"nuovi": 1, "gia_presenti": 0, "senza_storia": 1, "gia_iniziate": 1}
    assert store.conteggi()["pronostici"] == 1


def test_registrare_due_volte_di_fila_non_aggiunge_nulla(store):
    modello = _modello()
    partite = [PartitaFinta(1, FISCHIO, 505, 503)]
    nomi = {505: "Inter", 503: "Torino"}

    registra_partite(store, modello, partite, nomi.get, now=ADESSO)
    esito = registra_partite(store, modello, partite, nomi.get, now=ADESSO)

    assert esito["nuovi"] == 0
    assert esito["gia_presenti"] == 1
    assert store.conteggi()["pronostici"] == 1


def test_la_compressione_dei_gol_cambia_la_versione_del_modello(store):
    """Cambia le probabilita', quindi e' un altro modello.

    Il rovescio del test qui sotto: l'affidabilita' descrive la stima e non
    la definisce, la compressione invece riscrive ogni numero che esce. Due
    pronostici prodotti con compressioni diverse non devono finire sulla
    stessa riga di ``model_versions``, altrimenti confrontare i Brier di
    prima e di dopo misurerebbe due cose mescolate.
    """
    prima = replace(_modello(), goal_compression=1.0, league_total_goals=2.5)
    dopo = replace(prima, goal_compression=0.2)
    assert store.versione_modello(prima) != store.versione_modello(dopo)


def test_la_media_di_lega_cambia_la_versione_del_modello(store):
    """Stesso fattore, media diversa: i gol attesi finiscono altrove."""
    prima = replace(_modello(), goal_compression=0.2, league_total_goals=2.5)
    assert store.versione_modello(prima) != store.versione_modello(
        replace(prima, league_total_goals=2.7)
    )


def test_l_affidabilita_non_cambia_la_versione_del_modello(store):
    """Sapere *su cosa* poggia una stima non la rende una stima diversa.

    L'indicatore di affidabilita' descrive la stima, non la definisce: due
    modelli con gli stessi parametri sono lo stesso modello, e devono cadere
    sulla stessa riga di ``model_versions``. Se entrasse nell'impronta, ogni
    partita giocata da chiunque farebbe nascere una versione nuova e lo
    storico si riempirebbe di pronostici identici.
    """
    from serie_a_analyzer.model.dixon_coles import TeamEvidence

    nudo = _modello()
    vestito = replace(
        nudo,
        evidence={
            "Inter": TeamEvidence(
                team="Inter", n_matches=114, effective_sample_size=5.4,
                last_match=date(2026, 5, 24),
                pull_attack=0.36, pull_defense=0.35,
            )
        },
    )
    assert store.versione_modello(nudo) == store.versione_modello(vestito)
