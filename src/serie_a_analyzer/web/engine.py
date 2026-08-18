"""Stato condiviso dell'applicazione web: dati e modello, tenuti caldi.

Stimare il modello costa un paio di secondi su tre stagioni di partite: troppo
per farlo a ogni richiesta HTTP, e inutile, perche' fra una visita e l'altra i
dati quasi sempre non sono cambiati.

La regola di aggiornamento e' legata a **cosa** e' cambiato, non all'orologio:
il modello viene ricostruito quando l'archivio della raccolta giornaliera si
muove. E' l'unico momento in cui puo' cambiare qualcosa, perche' lo storico
delle stagioni concluse e' immutabile per definizione.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import (
    Settings, build_client, build_prediction_store, build_store, load_settings,
)
from ..data.dataset import Dataset, load_dataset
from ..data.outcomes import (
    accuratezza_per_mercato,
    brier_1x2_multiclasse,
    confronto_col_mercato,
    risolvi_pronostici,
)
from ..data.predictions import registra_partite
from ..model.dixon_coles import FittedModel, fit_dixon_coles
from ..model.markets import MatchProbabilities

logger = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """Dati e modello coerenti fra loro, con l'ora in cui sono stati prodotti."""

    dataset: Dataset
    model: FittedModel | None
    built_at: datetime
    archive_signature: tuple[float, int]
    error: str | None = None

    @property
    def ready(self) -> bool:
        return self.model is not None


class Engine:
    """Punto unico di accesso a dati e pronostici per le pagine web."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.client = build_client(self.settings)
        self.store, self.journal = build_store(self.settings)
        self.pronostici = build_prediction_store(self.settings)
        self._snapshot: Snapshot | None = None
        # Le pagine possono arrivare in parallelo: senza lucchetto due
        # richieste simultanee stimerebbero il modello due volte.
        self._lock = threading.Lock()

    # ---- ciclo di vita dello snapshot --------------------------------------

    def _archive_signature(self) -> tuple[float, int]:
        """Impronta dell'archivio: cambia se e solo se la raccolta ha scritto."""
        path = Path(self.store.root) / "fixtures.json"
        if not path.exists():
            return (0.0, 0)
        stat = path.stat()
        return (stat.st_mtime, stat.st_size)

    def snapshot(self, force: bool = False) -> Snapshot:
        with self._lock:
            firma = self._archive_signature()
            attuale = self._snapshot
            if not force and attuale is not None and attuale.archive_signature == firma:
                return attuale
            self._snapshot = self._build(firma)
            return self._snapshot

    def _build(self, firma: tuple[float, int]) -> Snapshot:
        dataset = load_dataset(
            self.client, self.store, self.settings.seasons, self.settings.league_id
        )
        adesso = datetime.now(timezone.utc)

        partite = dataset.matches()
        if not partite:
            return Snapshot(
                dataset=dataset, model=None, built_at=adesso, archive_signature=firma,
                error="Nessuna partita conclusa disponibile: impossibile stimare il modello.",
            )

        logger.info("stima del modello su %d partite", len(partite))
        modello = fit_dixon_coles(partite)
        self._registra_pronostici(dataset, modello)
        self._risolvi_esiti(dataset)
        return Snapshot(
            dataset=dataset, model=modello, built_at=adesso, archive_signature=firma
        )

    def _registra_pronostici(self, dataset: Dataset, modello: FittedModel) -> None:
        """Congela i pronostici delle partite in programma, con questo modello.

        Il momento e' questo e non un altro: appena il modello e' stato
        ristimato, cioe' l'unico istante in cui i pronostici possono essere
        cambiati. Farlo a ogni richiesta HTTP scriverebbe migliaia di righe
        identiche; farlo piu' tardi vorrebbe dire registrare un pronostico che
        ha gia' visto qualcosa in piu' di quanto dichiara.

        Un problema del registro non deve impedire di mostrare le pagine: la
        pagina senza storico e' un disservizio, la pagina che non si apre e'
        un guasto.
        """
        try:
            esito = registra_partite(
                self.pronostici, modello, dataset.upcoming(), dataset.name
            )
        except Exception as exc:  # il registro non deve mai far cadere il web
            logger.warning("storico dei pronostici non aggiornato: %s", exc)
            return
        if esito["nuovi"]:
            logger.info("pronostici registrati: %d nuovi", esito["nuovi"])

    def _risolvi_esiti(self, dataset: Dataset) -> None:
        """Confronta con la realta' i pronostici delle partite ormai concluse.

        Il momento e' lo stesso della registrazione, e non e' un caso: l'unica
        cosa che puo' aver reso risolvibile un pronostico e' l'arrivo di
        risultati nuovi, cioe' proprio cio' che ha fatto ricostruire lo
        snapshot.
        """
        try:
            esito = risolvi_pronostici(self.pronostici, dataset.finished)
        except Exception as exc:  # come sopra: mai far cadere una pagina
            logger.warning("esiti non risolti: %s", exc)
            return
        if esito["esiti"]:
            logger.info(
                "esiti risolti: %d mercati su %d partite", esito["esiti"], esito["partite"]
            )

    def accuratezza(self) -> dict:
        """Quanto valgono i pronostici gia' verificati dalla realta'.

        Vuoto finche' non c'e' nulla di risolto — ed e' giusto che l'interfaccia
        mostri il vuoto invece di un numero costruito su tre partite.
        """
        try:
            return {
                "mercati": accuratezza_per_mercato(self.pronostici),
                "uno_x_due": brier_1x2_multiclasse(self.pronostici),
                "mercato": confronto_col_mercato(self.pronostici),
            }
        except Exception as exc:
            logger.warning("accuratezza non calcolabile: %s", exc)
            return {"mercati": [], "uno_x_due": None, "mercato": None}

    # ---- pronostici --------------------------------------------------------

    def predict(self, home_id: int, away_id: int) -> MatchProbabilities | None:
        """Pronostico per un accoppiamento, o ``None`` se il modello non sa chi sono.

        Il caso ``None`` non e' un errore: una neopromossa alla prima giornata
        non ha storia da cui stimare una forza, e inventargliela sarebbe peggio
        che ammettere di non saperlo.
        """
        snap = self.snapshot()
        if snap.model is None:
            return None
        try:
            return snap.model.predict(
                snap.dataset.name(home_id), snap.dataset.name(away_id)
            )
        except KeyError:
            return None

    def expected_goals(self, home_id: int, away_id: int) -> tuple[float, float] | None:
        snap = self.snapshot()
        if snap.model is None:
            return None
        try:
            return snap.model.expected_goals(
                snap.dataset.name(home_id), snap.dataset.name(away_id)
            )
        except KeyError:
            return None

    # ---- stato per l'interfaccia ------------------------------------------

    def _conteggio(self, chiave: str) -> int:
        try:
            return self.pronostici.conteggi()[chiave]
        except Exception:
            return 0

    def _conteggio_pronostici(self) -> int:
        return self._conteggio("pronostici")

    def _conteggio_esiti(self) -> int:
        return self._conteggio("esiti_risolti")

    def health(self) -> dict:
        """Cosa sa e cosa non sa il sistema, in forma mostrabile all'utente."""
        snap = self.snapshot()
        giorni = self.journal.collected_days()
        return {
            "partite_totali": len(snap.dataset.fixtures),
            "partite_storiche": snap.dataset.n_historical,
            "partite_stagione_corrente": snap.dataset.n_current,
            "squadre": len(snap.dataset.team_names),
            "giorni_raccolti": len(giorni),
            "ultimo_giorno_raccolto": giorni[-1].isoformat() if giorni else None,
            "modello_pronto": snap.ready,
            "modello_partite": snap.model.n_matches if snap.model else 0,
            "aggiornato_alle": snap.built_at,
            "pronostici_registrati": self._conteggio_pronostici(),
            "esiti_risolti": self._conteggio_esiti(),
            "partite_quotate": self._conteggio("partite_quotate"),
            "budget_usato": self.client.budget.used,
            "budget_limite": self.settings.daily_request_limit,
            "errore": snap.error,
        }
