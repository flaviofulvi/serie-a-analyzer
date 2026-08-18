"""Test della stima delle forze.

Il test centrale e' quello di recupero: si genera una stagione da forze note e
si verifica che il modello le ritrovi. E' l'unico modo per sapere se il motore
funziona prima di avere dati reali.
"""

from datetime import date, timedelta

import numpy as np
import pytest

from serie_a_analyzer.model.dixon_coles import Match, fit_dixon_coles
from serie_a_analyzer.model.simulation import generate_league


@pytest.fixture(scope="module")
def lega():
    # Tre stagioni: con una sola il rumore su 380 partite e' ancora alto.
    return generate_league(n_teams=20, n_rounds=6, seed=7)


@pytest.fixture(scope="module")
def modello(lega):
    # Emivita lunga: qui le forze vere sono costanti nel tempo, non vogliamo
    # scartare meta' del campione.
    return fit_dixon_coles(lega.matches, half_life_days=2000.0, regularization=0.5)


def test_il_modello_converge(modello, lega):
    assert modello.converged
    assert modello.n_matches == len(lega.matches)
    assert set(modello.teams) == set(lega.true_attack)


def test_recupera_le_forze_di_attacco(modello, lega):
    stimato = np.array([modello.attack[t] for t in modello.teams])
    vero = np.array([lega.true_attack[t] for t in modello.teams])
    correlazione = np.corrcoef(stimato, vero)[0, 1]
    assert correlazione > 0.9, f"correlazione attacco troppo bassa: {correlazione:.3f}"


def test_recupera_le_forze_di_difesa(modello, lega):
    stimato = np.array([modello.defense[t] for t in modello.teams])
    vero = np.array([lega.true_defense[t] for t in modello.teams])
    correlazione = np.corrcoef(stimato, vero)[0, 1]
    assert correlazione > 0.9, f"correlazione difesa troppo bassa: {correlazione:.3f}"


def test_recupera_il_fattore_campo(modello, lega):
    assert modello.home_advantage == pytest.approx(lega.true_home_advantage, abs=0.06)


def test_recupera_il_livello_di_gol(modello, lega):
    assert modello.base == pytest.approx(lega.true_base, abs=0.08)


def test_parametri_centrati_a_somma_zero(modello):
    assert sum(modello.attack.values()) == pytest.approx(0.0, abs=1e-8)
    assert sum(modello.defense.values()) == pytest.approx(0.0, abs=1e-8)


def test_il_fattore_campo_alza_i_gol_attesi(modello):
    a, b = modello.teams[0], modello.teams[1]
    lam_casa, _ = modello.expected_goals(a, b)
    _, mu_trasferta = modello.expected_goals(b, a)
    # Stesse squadre, ruoli invertiti: 'a' segna di piu' quando gioca in casa.
    assert lam_casa > mu_trasferta


def test_shrinkage_con_pochissimi_dati():
    """Con due partite le forze devono restare vicine alla media della lega."""
    partite = [
        Match("Alfa", "Beta", 5, 0, date(2025, 8, 24)),
        Match("Gamma", "Delta", 0, 4, date(2025, 8, 24)),
    ]
    m = fit_dixon_coles(partite, regularization=5.0)
    # Senza shrinkage il modello direbbe che l'Alfa e' infinitamente forte.
    assert all(abs(v) < 0.6 for v in m.attack.values())
    assert all(abs(v) < 0.6 for v in m.defense.values())


def test_le_partite_recenti_pesano_di_piu():
    """Stessa squadra, prima scarsa e poi fortissima: deve risultare forte."""
    oggi = date(2025, 8, 24)
    partite = []
    # Un anno fa: Alfa perde sempre.
    for k in range(12):
        partite.append(Match("Alfa", "Beta", 0, 3, oggi - timedelta(days=400 + k)))
        partite.append(Match("Gamma", "Delta", 1, 1, oggi - timedelta(days=400 + k)))
    # Ultimo mese: Alfa vince sempre e goleada.
    for k in range(12):
        partite.append(Match("Alfa", "Beta", 4, 0, oggi - timedelta(days=k)))
        partite.append(Match("Gamma", "Delta", 1, 1, oggi - timedelta(days=k)))

    recente = fit_dixon_coles(partite, as_of=oggi, half_life_days=60.0, regularization=0.5)
    lungo = fit_dixon_coles(partite, as_of=oggi, half_life_days=5000.0, regularization=0.5)

    # Con memoria corta conta la forma attuale; con memoria lunga i due periodi
    # si compensano e l'Alfa risulta molto piu' vicina alla media.
    assert recente.attack["Alfa"] > lungo.attack["Alfa"]


def test_campione_efficace_minore_del_numero_di_partite(lega):
    m = fit_dixon_coles(lega.matches, half_life_days=180.0)
    assert 0 < m.effective_sample_size < m.n_matches


def test_squadra_sconosciuta(modello):
    with pytest.raises(KeyError):
        modello.expected_goals("Inter", modello.teams[0])


def test_serve_almeno_una_partita():
    with pytest.raises(ValueError):
        fit_dixon_coles([])


def test_rifiuta_partite_nel_futuro():
    partite = [Match("Alfa", "Beta", 1, 0, date(2025, 9, 1))]
    with pytest.raises(ValueError):
        fit_dixon_coles(partite, as_of=date(2025, 8, 1))
