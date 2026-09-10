"""Test del pronostico su chi segna.

Due cose da presidiare. La prima e' la regola del progetto: la probabilita' di
un giocatore e' una somma di celle della matrice, quindi non puo' dire cose
che la matrice smentisce. La seconda e' la stessa del backtest: una domanda
fatta "prima di quel giorno" non deve vedere niente di quel giorno, altrimenti
il pronostico si ricorda del gol invece di prevederlo.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from serie_a_analyzer.data.marcatori import (
    MINUTI_TITOLARE,
    PARTITE_RECENTI,
    QUOTA_MASSIMA,
    StoriaMarcatori,
    probabili_marcatori,
)
from serie_a_analyzer.data.players import Prestazione
from serie_a_analyzer.model.markets import player_scores, team_goal_distribution
from serie_a_analyzer.model.scoreline import scoreline_matrix

INIZIO = datetime(2025, 8, 23, 18, 45, tzinfo=timezone.utc)
CASA, OSPITE = 505, 503


@pytest.fixture
def matrice():
    return scoreline_matrix(1.7, 1.1, rho=-0.05)


# ---- la matematica, dentro la matrice -----------------------------------------


def test_chi_non_ha_quota_non_segna_e_chi_ha_tutto_segna_se_segna_la_squadra(matrice):
    assert player_scores(matrice, "home", 0.0) == 0.0
    # Con tutta la quota segna esattamente quando segna la sua squadra.
    assert np.isclose(player_scores(matrice, "home", 1.0), 1.0 - matrice[0, :].sum())
    assert np.isclose(player_scores(matrice, "away", 1.0), 1.0 - matrice[:, 0].sum())


def test_un_giocatore_non_segna_piu_spesso_della_sua_squadra(matrice):
    segna_la_squadra = 1.0 - team_goal_distribution(matrice, "home")[0]
    for quota in (0.05, 0.2, 0.5, 0.95):
        assert player_scores(matrice, "home", quota) <= segna_la_squadra + 1e-12


def test_piu_quota_vuol_dire_piu_probabilita(matrice):
    probs = [player_scores(matrice, "home", q) for q in (0.05, 0.1, 0.2, 0.4)]
    assert probs == sorted(probs)


def test_senza_correlazione_e_la_formula_di_poisson():
    """Con rho = 0 i gol sono Poisson: la somma di celle deve dare 1 - exp(-quota * lambda)."""
    lam, quota = 1.6, 0.25
    m = scoreline_matrix(lam, 1.2, rho=0.0)
    assert np.isclose(player_scores(m, "home", quota), 1.0 - np.exp(-quota * lam), atol=1e-6)


def test_una_squadra_piu_forte_da_piu_probabilita_allo_stesso_giocatore():
    debole = scoreline_matrix(0.9, 1.5)
    forte = scoreline_matrix(2.2, 0.8)
    assert player_scores(forte, "home", 0.2) > player_scores(debole, "home", 0.2)


def test_i_valori_impossibili_si_rifiutano(matrice):
    with pytest.raises(ValueError):
        player_scores(matrice, "home", 1.2)
    with pytest.raises(ValueError):
        player_scores(matrice, "casa", 0.2)


# ---- la storia dei giocatori ---------------------------------------------------


@dataclass
class PartitaFinta:
    api_id: int
    kickoff_utc: datetime
    home_team_id: int = CASA
    away_team_id: int = OSPITE
    home_goals: int | None = 1
    away_goals: int | None = 0


def _prestazione(fid, quando, pid, *, gol=0, tiri=0, minuti=90, titolare=True,
                 posizione="F", squadra=CASA):
    return Prestazione(
        fixture_id=fid, quando=quando, stagione=2025, player_id=pid,
        nome=f"giocatore {pid}", team_id=squadra, squadra=str(squadra),
        minuti=minuti, titolare=titolare, posizione=posizione, numero=None,
        gol=gol, assist=0, gialli=0, rossi=0, tiri=tiri, tiri_in_porta=None,
        passaggi_chiave=None, voto=None,
    )


@dataclass
class InCampoFinto:
    player_id: int
    nome: str
    posizione: str | None


def _campionato(giornate=10):
    """Un centravanti (9) che segna e tira, un difensore (4) che tira ogni tanto."""
    partite, prestazioni = [], []
    for g in range(giornate):
        quando = INIZIO + timedelta(days=7 * g)
        partite.append(PartitaFinta(api_id=g + 1, kickoff_utc=quando, home_goals=1))
        prestazioni.append(_prestazione(g + 1, quando, 9, gol=1 if g % 2 else 0, tiri=3))
        # E' l'unico difensore: se non tirasse mai, la media del suo ruolo
        # varrebbe zero e lui con lei.
        prestazioni.append(_prestazione(g + 1, quando, 4, posizione="D", tiri=1 if g % 3 == 0 else 0))
    return partite, prestazioni


def test_chi_segna_e_tira_ha_una_quota_piu_alta_di_chi_non_fa_niente():
    partite, prestazioni = _campionato()
    storia = StoriaMarcatori(prestazioni, partite)
    dopo = INIZIO + timedelta(days=100)

    candidati = {c.player_id: c for c in storia.candidati(CASA, dopo, titolari=[
        InCampoFinto(9, "Centravanti", "F"), InCampoFinto(4, "Difensore", "D"),
    ])}

    assert candidati[9].quota > candidati[4].quota > 0.0


def test_la_partita_stessa_e_il_futuro_non_entrano_nella_stima():
    """Un poker il giorno della partita non deve alzare il pronostico di quella partita."""
    partite, prestazioni = _campionato()
    quel_giorno = INIZIO + timedelta(days=70)
    partite.append(PartitaFinta(api_id=99, kickoff_utc=quel_giorno, home_goals=4))
    storia_senza = StoriaMarcatori(prestazioni, partite)
    storia_con = StoriaMarcatori(
        prestazioni + [_prestazione(99, quel_giorno, 9, gol=4, tiri=8)], partite
    )

    assert storia_con.tasso(9, "F", quel_giorno) == storia_senza.tasso(9, "F", quel_giorno)
    assert storia_con.gol_abituali(CASA, quel_giorno) == storia_senza.gol_abituali(CASA, quel_giorno)
    assert storia_con.tasso(9, "F", quel_giorno + timedelta(seconds=1)) > \
        storia_senza.tasso(9, "F", quel_giorno)


def test_un_titolare_sconosciuto_vale_la_media_del_ruolo_non_zero():
    partite, prestazioni = _campionato()
    storia = StoriaMarcatori(prestazioni, partite)
    dopo = INIZIO + timedelta(days=100)

    [nuovo] = storia.candidati(CASA, dopo, titolari=[InCampoFinto(77, "Acquisto", "F")])

    assert nuovo.tasso_90 == pytest.approx(storia.tasso_ruolo("F", dopo))
    assert nuovo.minuti_attesi == pytest.approx(MINUTI_TITOLARE)
    assert nuovo.quota > 0.0


def test_i_gol_della_squadra_sono_quelli_del_punteggio_non_dei_marcatori():
    """Gli autogol a favore stanno nella matrice: devono stare anche nel denominatore."""
    quando = INIZIO
    partite = [PartitaFinta(api_id=1, kickoff_utc=quando, home_goals=3, away_goals=0)]
    # Nessun giocatore di casa ha segnato: tre autogol. Contando i marcatori
    # le due squadre risulterebbero uguali, a zero gol tutte e due.
    storia = StoriaMarcatori(
        [_prestazione(1, quando, 9), _prestazione(1, quando, 30, squadra=OSPITE)], partite
    )
    dopo = quando + timedelta(days=1)

    assert storia.gol_abituali(CASA, dopo) > storia.gol_abituali(OSPITE, dopo)


def test_prima_delle_formazioni_i_minuti_sono_la_media_delle_ultime_partite():
    partite, prestazioni = _campionato()
    ultima = INIZIO + timedelta(days=7 * 9)
    # Un panchinaro entrato solo nell'ultima partita, per 30 minuti.
    prestazioni.append(_prestazione(10, ultima, 21, minuti=30, titolare=False, posizione="M"))
    storia = StoriaMarcatori(prestazioni, partite)

    candidati = {c.player_id: c for c in storia.candidati(CASA, ultima + timedelta(days=1))}

    assert set(candidati) == {9, 4, 21}
    assert candidati[21].minuti_attesi == pytest.approx(30 / PARTITE_RECENTI)
    assert candidati[9].minuti_attesi == pytest.approx(90)


def test_chi_e_stato_ceduto_non_e_piu_un_candidato_di_chi_l_ha_venduto():
    """Il caso di Gaetano, l'11/09/2026: candidato di due squadre alla volta."""
    partite, prestazioni = [], []
    for g in range(10):
        quando = INIZIO + timedelta(days=7 * g)
        partite.append(PartitaFinta(api_id=g + 1, kickoff_utc=quando))
        prestazioni.append(_prestazione(g + 1, quando, 9))
        prestazioni.append(_prestazione(g + 1, quando, 60, squadra=OSPITE))
        # Il 50 gioca con l'ospite fino all'ottava, poi passa alla squadra di
        # casa e gioca con lei l'ultima.
        if g < 8:
            prestazioni.append(_prestazione(g + 1, quando, 50, squadra=OSPITE))
        elif g == 9:
            prestazioni.append(_prestazione(g + 1, quando, 50))
    storia = StoriaMarcatori(prestazioni, partite)
    dopo = INIZIO + timedelta(days=100)

    ospite = {c.player_id for c in storia.candidati(OSPITE, dopo)}
    casa = {c.player_id for c in storia.candidati(CASA, dopo)}

    assert 50 not in ospite
    assert 50 in casa
    assert storia.squadra_attuale(50, dopo) == CASA
    # Prima del trasferimento era dell'ospite, e la domanda "prima di" lo sa.
    assert storia.squadra_attuale(50, INIZIO + timedelta(days=7 * 5)) == OSPITE


def test_chi_e_assente_per_certo_non_e_un_candidato():
    partite, prestazioni = _campionato()
    storia = StoriaMarcatori(prestazioni, partite)

    candidati = storia.candidati(CASA, INIZIO + timedelta(days=100), assenti={9})

    assert [c.player_id for c in candidati] == [4]


def test_nessuno_fa_tutti_i_gol_della_sua_squadra():
    """Un tasso gonfiato su pochi minuti non deve diventare una certezza."""
    quando = INIZIO
    partite = [PartitaFinta(api_id=1, kickoff_utc=quando, home_goals=1)]
    # Cinque gol in dieci minuti: senza tetto la quota verrebbe decine di volte
    # i gol di tutta la squadra.
    prestazioni = [_prestazione(1, quando, 9, gol=5, tiri=20, minuti=10, titolare=False)]
    storia = StoriaMarcatori(prestazioni, partite)

    [c] = storia.candidati(CASA, quando + timedelta(days=1), titolari=[
        InCampoFinto(9, "Fenomeno", "F"),
    ])

    assert c.quota <= QUOTA_MASSIMA


def test_i_marcatori_escono_dal_piu_probabile(matrice):
    partite, prestazioni = _campionato()
    storia = StoriaMarcatori(prestazioni, partite)
    candidati = storia.candidati(CASA, INIZIO + timedelta(days=100), titolari=[
        InCampoFinto(4, "Difensore", "D"), InCampoFinto(9, "Centravanti", "F"),
    ])

    marcatori = probabili_marcatori(matrice, "home", candidati)

    assert [m.candidato.player_id for m in marcatori] == [9, 4]
    assert all(0.0 < m.probabilita < 1.0 for m in marcatori)
