"""Test della contabilita' del budget giornaliero."""

import json

import pytest

from serie_a_analyzer.data.budget import (
    BudgetExhausted,
    Priority,
    RequestBudget,
)


@pytest.fixture
def budget(tmp_path):
    return RequestBudget(state_path=tmp_path / "budget.json", daily_limit=100)


def test_parte_da_zero(budget):
    assert budget.used == 0
    assert budget.remaining == 100


def test_il_consumo_si_accumula(budget):
    budget.spend(Priority.NORMAL, 3)
    budget.spend(Priority.NORMAL)
    assert budget.used == 4
    assert budget.remaining == 96


def test_lo_stato_sopravvive_al_riavvio(tmp_path):
    percorso = tmp_path / "budget.json"
    RequestBudget(state_path=percorso).spend(Priority.NORMAL, 7)
    # Nuova istanza: simula un processo riavviato.
    assert RequestBudget(state_path=percorso).used == 7


def test_la_riserva_protegge_le_priorita_alte(budget):
    """Il backfill deve fermarsi lasciando margine alle formazioni ufficiali."""
    budget.spend(Priority.BACKFILL, 55)
    # Riserva di default su 100: 25% CRITICAL + 15% NORMAL = 40 protette.
    assert budget.allowance(Priority.BACKFILL) == 100 - 55 - 40
    assert budget.allowance(Priority.CRITICAL) == 45

    with pytest.raises(BudgetExhausted):
        budget.spend(Priority.BACKFILL, 10)

    # Le richieste critiche passano ancora: e' esattamente lo scopo della riserva.
    budget.spend(Priority.CRITICAL, 10)
    assert budget.used == 65


def test_il_limite_complessivo_e_invalicabile(budget):
    budget.spend(Priority.CRITICAL, 100)
    assert budget.remaining == 0
    with pytest.raises(BudgetExhausted):
        budget.spend(Priority.CRITICAL)


def test_uso_per_priorita(budget):
    budget.spend(Priority.CRITICAL, 2)
    budget.spend(Priority.BACKFILL, 5)
    assert budget.usage_by_priority() == {"CRITICAL": 2, "BACKFILL": 5}


def test_il_contatore_si_azzera_al_cambio_di_giorno(tmp_path):
    percorso = tmp_path / "budget.json"
    b = RequestBudget(state_path=percorso)
    b.spend(Priority.NORMAL, 40)
    # Riscriviamo lo stato come se fosse di ieri.
    stato = json.loads(percorso.read_text())
    stato["day"] = "2020-01-01"
    percorso.write_text(json.dumps(stato))
    assert RequestBudget(state_path=percorso).used == 0


def test_stato_corrotto_non_blocca(tmp_path):
    percorso = tmp_path / "budget.json"
    percorso.write_text("{non e' json")
    assert RequestBudget(state_path=percorso).used == 0


def test_rifiuta_quantita_non_positive(budget):
    with pytest.raises(ValueError):
        budget.spend(Priority.NORMAL, 0)


def test_reset(budget):
    budget.spend(Priority.NORMAL, 10)
    budget.reset()
    assert budget.used == 0


def test_la_riserva_scala_col_limite_giornaliero(tmp_path):
    """Con un piano diverso la riserva deve restare proporzionata, non fissa."""
    piccolo = RequestBudget(state_path=tmp_path / "p.json", daily_limit=10)
    assert piccolo.reserved_for(Priority.CRITICAL) == 2
    # Con riserve assolute tarate su 100 richieste, qui non passerebbe nulla.
    assert piccolo.allowance(Priority.NORMAL) == 8
    piccolo.spend(Priority.NORMAL, 8)
    assert piccolo.used == 8

    grande = RequestBudget(state_path=tmp_path / "g.json", daily_limit=7500)
    assert grande.reserved_for(Priority.CRITICAL) == 1875


def test_riserve_incoerenti_rifiutate(tmp_path):
    with pytest.raises(ValueError, match="riserve"):
        RequestBudget(
            state_path=tmp_path / "x.json",
            reserve_fraction={Priority.CRITICAL: 0.7, Priority.NORMAL: 0.5},
        )
