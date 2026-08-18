"""Test della matrice dei risultati."""

import numpy as np
import pytest
from scipy.stats import poisson

from serie_a_analyzer.model.scoreline import scoreline_matrix


def test_matrice_e_distribuzione_valida():
    m = scoreline_matrix(1.6, 1.1, rho=-0.1)
    assert m.shape == (11, 11)
    assert np.isclose(m.sum(), 1.0)
    assert (m >= 0).all()


def test_senza_correzione_coincide_con_poisson_indipendenti():
    """Con rho = 0 la matrice deve essere il prodotto delle marginali."""
    lam, mu = 1.7, 1.2
    m = scoreline_matrix(lam, mu, rho=0.0, max_goals=15)
    atteso = np.outer(poisson.pmf(np.arange(16), lam), poisson.pmf(np.arange(16), mu))
    atteso /= atteso.sum()
    assert np.allclose(m, atteso)


def test_rho_negativo_alza_i_pareggi_bassi():
    """La correzione di Dixon-Coles esiste per questo: piu' 0-0 e 1-1."""
    senza = scoreline_matrix(1.4, 1.2, rho=0.0)
    con = scoreline_matrix(1.4, 1.2, rho=-0.15)
    assert con[0, 0] > senza[0, 0]
    assert con[1, 1] > senza[1, 1]
    assert con[1, 0] < senza[1, 0]
    assert con[0, 1] < senza[0, 1]


def test_gol_attesi_ricostruiti_dalla_matrice():
    """Con griglia ampia e rho = 0 i momenti devono tornare ai lambda."""
    lam, mu = 1.8, 1.3
    m = scoreline_matrix(lam, mu, rho=0.0, max_goals=20)
    goals = np.arange(21)
    assert np.isclose((m.sum(axis=1) * goals).sum(), lam, atol=1e-6)
    assert np.isclose((m.sum(axis=0) * goals).sum(), mu, atol=1e-6)


def test_scambiare_i_lambda_traspone_la_matrice():
    a = scoreline_matrix(1.9, 0.9, rho=-0.1)
    b = scoreline_matrix(0.9, 1.9, rho=-0.1)
    assert np.allclose(a, b.T)


@pytest.mark.parametrize("lam,mu", [(0.0, 1.0), (1.0, -0.5)])
def test_lambda_non_positivi_rifiutati(lam, mu):
    with pytest.raises(ValueError):
        scoreline_matrix(lam, mu)
