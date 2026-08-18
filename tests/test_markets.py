"""Test dei mercati derivati.

Il valore dell'architettura a matrice unica sta nella coerenza: questi test
verificano soprattutto che gli esiti derivati non possano contraddirsi.
"""

import numpy as np
import pytest

from serie_a_analyzer.model.markets import (
    both_teams_score,
    derive_markets,
    expected_goals,
    one_x_two,
    over_under,
    top_scorelines,
    winning_margin,
)
from serie_a_analyzer.model.scoreline import scoreline_matrix


@pytest.fixture
def matrice():
    return scoreline_matrix(1.7, 1.1, rho=-0.12)


def test_1x2_e_una_partizione(matrice):
    home, draw, away = one_x_two(matrice)
    assert np.isclose(home + draw + away, 1.0)
    assert home > away  # 1.7 gol attesi contro 1.1


def test_over_e_under_sono_complementari(matrice):
    for line in (0.5, 1.5, 2.5, 3.5):
        over, under = over_under(matrice, line)
        assert np.isclose(over + under, 1.0)


def test_over_decresce_al_crescere_della_linea(matrice):
    probs = [over_under(matrice, line)[0] for line in (0.5, 1.5, 2.5, 3.5, 4.5)]
    assert probs == sorted(probs, reverse=True)


def test_over_05_e_il_complemento_dello_zero_a_zero(matrice):
    over_05, _ = over_under(matrice, 0.5)
    assert np.isclose(over_05, 1.0 - matrice[0, 0])


def test_btts_coerente_con_i_clean_sheet(matrice):
    """Entrambe segnano == nessuna delle due tiene la porta inviolata."""
    m = derive_markets(matrice)
    nessun_gol_ospite = matrice[:, 0].sum()
    nessun_gol_casa = matrice[0, :].sum()
    zero_a_zero = matrice[0, 0]
    # Inclusione-esclusione sull'unione dei due clean sheet.
    atteso = 1.0 - (nessun_gol_ospite + nessun_gol_casa - zero_a_zero)
    assert np.isclose(m.btts, atteso)
    assert np.isclose(m.btts + m.no_btts, 1.0)


def test_doppia_chance_coerente_con_1x2(matrice):
    m = derive_markets(matrice)
    assert np.isclose(m.home_or_draw, m.home_win + m.draw)
    assert np.isclose(m.away_or_draw, m.away_win + m.draw)
    assert np.isclose(m.home_or_away, m.home_win + m.away_win)


def test_scarto_minimo_e_annidato(matrice):
    """Vincere con 2+ gol implica vincere: le probabilita' devono decrescere."""
    uno = winning_margin(matrice, "home", 1)
    due = winning_margin(matrice, "home", 2)
    tre = winning_margin(matrice, "home", 3)
    assert uno > due > tre
    # Vincere con almeno 1 gol di scarto e' esattamente vincere.
    assert np.isclose(uno, one_x_two(matrice)[0])


def test_scarto_squadra_sconosciuta(matrice):
    with pytest.raises(ValueError):
        winning_margin(matrice, "ospiti", 1)


def test_gol_attesi_positivi_e_sensati(matrice):
    xg_home, xg_away = expected_goals(matrice)
    assert 1.0 < xg_home < 2.5
    assert 0.5 < xg_away < 2.0
    assert xg_home > xg_away


def test_risultati_piu_probabili_ordinati(matrice):
    top = top_scorelines(matrice, n=6)
    assert len(top) == 6
    probs = [p for _, p in top]
    assert probs == sorted(probs, reverse=True)
    # Nessun altro risultato deve superare il primo classificato.
    assert np.isclose(probs[0], matrice.max())


def test_pareggio_favorito_quando_le_squadre_sono_pari():
    m = derive_markets(scoreline_matrix(1.3, 1.3, rho=-0.12))
    assert np.isclose(m.home_win, m.away_win)
    assert np.isclose(m.expected_goals_home, m.expected_goals_away)
