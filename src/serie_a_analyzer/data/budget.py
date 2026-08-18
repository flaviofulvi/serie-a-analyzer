"""Contabilita' delle richieste giornaliere ad API-Football.

Sul piano gratuito il budget e' di 100 richieste al giorno. Non e' un dettaglio
operativo, e' un vincolo di progetto: sforarlo significa restare senza dati nel
momento peggiore, cioe' quando escono le formazioni ufficiali mezz'ora prima
del calcio d'inizio.

Il contatore e' persistito su disco e si azzera da solo al cambio di giorno
(UTC, come il reset dell'API). Le prenotazioni sono suddivise per priorita':
le richieste di bassa priorita' (backfill storico) vengono rifiutate prima di
intaccare la riserva destinata a quelle critiche.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any

# Limite del piano gratuito di API-Football.
DEFAULT_DAILY_LIMIT = 100


class Priority(IntEnum):
    """Priorita' di una richiesta.

    L'ordine conta: la riserva protegge le priorita' alte da quelle basse.
    """

    # Formazioni ufficiali, risultati in tempo reale: se saltano, salta la
    # funzionalita' principale della giornata.
    CRITICAL = 0
    # Calendario, risultati di fine giornata, quote.
    NORMAL = 1
    # Backfill storico delle statistiche giocatore: puo' sempre aspettare domani.
    BACKFILL = 2


# Quota di budget tenuta da parte per le priorita' CRITICAL e NORMAL, espressa
# come FRAZIONE del limite giornaliero e non come numero assoluto di richieste.
# Con valori assoluti la riserva sarebbe tarata implicitamente sul piano da 100
# richieste al giorno e, cambiando piano, potrebbe superare il budget totale
# bloccando ogni chiamata.
DEFAULT_RESERVE_FRACTION = {Priority.CRITICAL: 0.25, Priority.NORMAL: 0.15}


class BudgetExhausted(RuntimeError):
    """Sollevata quando la richiesta eccede il budget della sua priorita'."""


@dataclass
class RequestBudget:
    """Contatore persistente delle richieste giornaliere."""

    state_path: Path
    daily_limit: int = DEFAULT_DAILY_LIMIT
    reserve_fraction: dict[Priority, float] | None = None

    def __post_init__(self) -> None:
        self.state_path = Path(self.state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.reserve_fraction is None:
            self.reserve_fraction = dict(DEFAULT_RESERVE_FRACTION)
        totale = sum(self.reserve_fraction.values())
        if totale >= 1.0:
            raise ValueError(
                f"le riserve sommano a {totale:.0%} del budget: non resterebbe "
                "nulla per le priorita' piu' basse"
            )

    # ---- stato su disco ---------------------------------------------------

    def _today(self) -> date:
        return datetime.now(timezone.utc).date()

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"day": self._today().isoformat(), "used": 0, "by_priority": {}}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Uno stato corrotto non deve bloccare l'applicazione: nel dubbio
            # si riparte da zero, al massimo si spendono richieste in piu' oggi.
            return {"day": self._today().isoformat(), "used": 0, "by_priority": {}}
        if state.get("day") != self._today().isoformat():
            return {"day": self._today().isoformat(), "used": 0, "by_priority": {}}
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # ---- interrogazione ---------------------------------------------------

    @property
    def used(self) -> int:
        return int(self._load()["used"])

    @property
    def remaining(self) -> int:
        return max(0, self.daily_limit - self.used)

    def usage_by_priority(self) -> dict[str, int]:
        return dict(self._load().get("by_priority", {}))

    def reserved_for(self, priority: Priority) -> int:
        """Richieste riservate a una priorita', in valore assoluto."""
        assert self.reserve_fraction is not None
        return int(self.daily_limit * self.reserve_fraction.get(priority, 0.0))

    def allowance(self, priority: Priority) -> int:
        """Quante richieste restano davvero disponibili a questa priorita'.

        Le priorita' basse "vedono" meno budget di quello nominale, perche' la
        riserva delle priorita' alte e' sottratta a monte.
        """
        assert self.reserve_fraction is not None
        protetto = sum(
            self.reserved_for(prio) for prio in self.reserve_fraction if prio < priority
        )
        return max(0, self.remaining - protetto)

    def can_spend(self, priority: Priority = Priority.NORMAL, n: int = 1) -> bool:
        return self.allowance(priority) >= n

    # ---- consumo ----------------------------------------------------------

    def spend(self, priority: Priority = Priority.NORMAL, n: int = 1) -> None:
        """Registra ``n`` richieste, o solleva ``BudgetExhausted``."""
        if n <= 0:
            raise ValueError("il numero di richieste deve essere positivo")
        if not self.can_spend(priority, n):
            raise BudgetExhausted(
                f"budget esaurito per priorita' {priority.name}: "
                f"disponibili {self.allowance(priority)}, richieste {n} "
                f"(usate oggi {self.used}/{self.daily_limit})"
            )
        state = self._load()
        state["used"] = int(state["used"]) + n
        by_priority = state.setdefault("by_priority", {})
        by_priority[priority.name] = int(by_priority.get(priority.name, 0)) + n
        self._save(state)

    def reset(self) -> None:
        """Azzera il contatore. Utile nei test e dopo un cambio di piano."""
        self._save({"day": self._today().isoformat(), "used": 0, "by_priority": {}})
