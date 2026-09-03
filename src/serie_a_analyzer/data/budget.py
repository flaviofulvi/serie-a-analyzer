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
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Quanto aspettare che un altro processo finisca di aggiornare il contatore, e
# dopo quanto considerare abbandonato un lucchetto rimasto li'. Sono secondi:
# aggiornare il contatore vuol dire leggere e riscrivere un file di trecento
# byte, quindi mezzo secondo e' gia' tantissimo. Il tetto basso e' voluto —
# vedi `_blocco`, dove a scadere e' il lucchetto e non la raccolta.
ATTESA_LUCCHETTO = 5.0
LUCCHETTO_ABBANDONATO = 30.0

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
            # Uno stato illeggibile non deve bloccare l'applicazione: nel
            # dubbio si riparte da zero, al massimo si spendono richieste in
            # piu' oggi. Va pero' DETTO, perche' ripartire da zero significa
            # regalarsi un intero budget giornaliero e nessuno se ne
            # accorgerebbe. Da quando `_save` scrive in modo atomico questo
            # ramo non dovrebbe scattare mai piu': se scatta e' una notizia.
            logger.warning(
                "contatore del budget illeggibile (%s): si riparte da zero, "
                "oggi si potrebbe spendere piu' del dovuto", self.state_path,
            )
            return {"day": self._today().isoformat(), "used": 0, "by_priority": {}}
        if state.get("day") != self._today().isoformat():
            return {"day": self._today().isoformat(), "used": 0, "by_priority": {}}
        return state

    def _save(self, state: dict[str, Any]) -> None:
        """Scrive il contatore senza poter lasciare un file a meta'.

        Si scrive di fianco e si rinomina, che per il filesystem e'
        un'operazione sola: chi legge nello stesso istante vede il file di
        prima o quello dopo, mai i primi cento byte del nuovo. Prima si
        scriveva sul posto, e un'interruzione a meta' — il computer che si
        spegne, una raccolta fermata con Ctrl+C — lasciava un JSON troncato,
        che `_load` traduce in "usate zero richieste": cioe' un budget intero
        regalato proprio nel giorno in cui qualcosa era andato storto.
        """
        provvisorio = self.state_path.with_suffix(".tmp")
        provvisorio.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(provvisorio, self.state_path)

    @contextmanager
    def _blocco(self):
        """Il diritto esclusivo di aggiornare il contatore, fra processi diversi.

        Serve perche' i processi che spendono sono TRE e girano insieme: il
        sito, la raccolta giornaliera e la corsa del quarto d'ora
        (`run_momento.ps1`). Contare una richiesta e' leggi-modifica-scrivi, e
        due processi che lo fanno nello stesso momento leggono lo stesso
        numero e ne scrivono uno solo: una delle due richieste risulta mai
        avvenuta, il contatore scende piu' lentamente di quanto si spende, e
        il giorno in cui il limite tornera' a cento questo e' il modo di
        sforare senza accorgersene.

        Il lucchetto e' un file creato in modo esclusivo, l'unica primitiva
        che si comporta allo stesso modo su Windows e altrove. Se non si
        riesce a prenderlo si prosegue LO STESSO, ed e' voluto: perdere un
        conteggio e' un fastidio, bloccare la lettura delle formazioni
        ufficiali mezz'ora prima del fischio d'inizio costa una giornata di
        dati. Per la stessa ragione un lucchetto piu' vecchio di
        ``LUCCHETTO_ABBANDONATO`` viene tolto: e' di un processo che non c'e'
        piu'.
        """
        lucchetto = self.state_path.with_suffix(".lock")
        scadenza = time.monotonic() + ATTESA_LUCCHETTO
        preso = False
        while True:
            try:
                os.close(os.open(lucchetto, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                preso = True
                break
            except FileExistsError:
                try:
                    vecchio = (
                        time.time() - lucchetto.stat().st_mtime
                        > LUCCHETTO_ABBANDONATO
                    )
                except OSError:
                    vecchio = False
                if vecchio:
                    try:
                        lucchetto.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= scadenza:
                    logger.warning(
                        "lucchetto del budget occupato da piu' di %.0fs: si "
                        "prosegue senza, un conteggio potrebbe perdersi",
                        ATTESA_LUCCHETTO,
                    )
                    break
                time.sleep(0.05)
            except OSError:
                # Un filesystem che non collabora non deve fermare la raccolta.
                break
        try:
            yield
        finally:
            if preso:
                try:
                    lucchetto.unlink()
                except OSError:
                    pass

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
        return self._allowance_da(self.used, priority)

    def _allowance_da(self, usate: int, priority: Priority) -> int:
        """La stessa cosa, a partire da un conteggio gia' letto.

        Esiste perche' `spend` deve decidere e scrivere sullo STESSO stato:
        rileggere il file fra il controllo e l'incremento riaprirebbe proprio
        la finestra che il lucchetto serve a chiudere.
        """
        assert self.reserve_fraction is not None
        protetto = sum(
            self.reserved_for(prio) for prio in self.reserve_fraction if prio < priority
        )
        return max(0, max(0, self.daily_limit - usate) - protetto)

    def can_spend(self, priority: Priority = Priority.NORMAL, n: int = 1) -> bool:
        return self.allowance(priority) >= n

    # ---- consumo ----------------------------------------------------------

    def spend(self, priority: Priority = Priority.NORMAL, n: int = 1) -> None:
        """Registra ``n`` richieste, o solleva ``BudgetExhausted``.

        Controllo e incremento stanno dentro lo stesso lucchetto e guardano lo
        stesso stato: erano due letture separate del file, e fra l'una e
        l'altra un altro processo poteva spendere le richieste appena
        dichiarate disponibili.
        """
        if n <= 0:
            raise ValueError("il numero di richieste deve essere positivo")
        with self._blocco():
            state = self._load()
            usate = int(state["used"])
            disponibili = self._allowance_da(usate, priority)
            if disponibili < n:
                raise BudgetExhausted(
                    f"budget esaurito per priorita' {priority.name}: "
                    f"disponibili {disponibili}, richieste {n} "
                    f"(usate oggi {usate}/{self.daily_limit})"
                )
            state["used"] = usate + n
            by_priority = state.setdefault("by_priority", {})
            by_priority[priority.name] = int(by_priority.get(priority.name, 0)) + n
            self._save(state)

    def reset(self) -> None:
        """Azzera il contatore. Utile nei test e dopo un cambio di piano."""
        with self._blocco():
            self._save({"day": self._today().isoformat(), "used": 0, "by_priority": {}})
