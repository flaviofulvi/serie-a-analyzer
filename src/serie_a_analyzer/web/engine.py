"""Stato condiviso dell'applicazione web: dati e modello, tenuti caldi.

Stimare il modello costa un paio di secondi su tre stagioni di partite: troppo
per farlo a ogni richiesta HTTP, e inutile, perche' fra una visita e l'altra i
dati quasi sempre non sono cambiati.

La regola di aggiornamento e' legata a **cosa** e' cambiato, non all'orologio:
il modello viene ricostruito quando si muove uno degli archivi su disco —
quello della raccolta giornaliera o quello di una stagione ricomprata. Sono i
soli momenti in cui i dati possono cambiare: una stagione conclusa e gia'
scaricata e' immutabile per definizione.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..config import (
    Settings,
    build_client,
    build_importato_stores,
    build_prediction_store,
    build_store,
    build_storico_stores,
    load_settings,
)
from ..data.dataset import Dataset, load_dataset
from ..data.outcomes import (
    MODO_PREDEFINITO,
    accuratezza_per_mercato,
    brier_1x2_multiclasse,
    confronto_col_mercato,
    confronto_fra_modi,
    risolvi_pronostici,
)
from ..data.panchine import Panchine, costruisci_panchine
from ..data.players import ArchivioGiocatori, costruisci_archivio
from ..data.predictions import MODE_SECCO, registra_partite
from ..data.importato import firma_importato
from ..data.store import SeasonStore, StoreUnito
from ..data.storico import firma_storico
from ..model.dixon_coles import FittedModel, TeamEvidence, fit_dixon_coles
from ..model.markets import MatchProbabilities, derive_markets

logger = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """Dati e modello coerenti fra loro, con l'ora in cui sono stati prodotti."""

    dataset: Dataset
    model: FittedModel | None
    built_at: datetime
    archive_signature: tuple
    # Chi allenava chi. Sta nello snapshot e non da parte perche' e' un
    # ingrediente della stima, non un contorno delle pagine: il modello qui
    # accanto e' stato stimato con queste panchine e con nessun'altra.
    panchine: Panchine | None = None
    error: str | None = None

    @property
    def ready(self) -> bool:
        return self.model is not None


@dataclass(frozen=True)
class Congelato:
    """Un pronostico letto dal registro, com'era prima del fischio d'inizio.

    Esiste perche' su una partita gia' cominciata il modello caldo non e' piu'
    un testimone attendibile: e' stato ristimato **includendo quel risultato**,
    quindi ricalcolare darebbe numeri che nessuno aveva prima e li mostrerebbe
    come se fossero stati detti in tempo. E' la stessa regola che il registro
    applica in scrittura (`data/predictions.py` rifiuta una partita gia'
    iniziata), applicata finalmente anche in lettura.

    La matrice viaggia insieme alle probabilita' perche' la griglia dei
    risultati esatti deve venire dalla stessa fonte: percentuali congelate
    accanto a una griglia ricalcolata sarebbero due pronostici diversi sulla
    stessa pagina. I gol attesi vengono dalla riga e non dalla matrice: sono i
    lambda esatti che il modello dichiaro' allora, non la loro media troncata.
    """

    prob: MatchProbabilities
    matrice: np.ndarray
    gol_attesi: tuple[float, float]
    quando: datetime | None


def _istante(testo: str | None) -> datetime | None:
    """La data di un pronostico, o ``None`` se il registro non la sa dire."""
    if not testo:
        return None
    try:
        return datetime.fromisoformat(testo)
    except ValueError:
        return None


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
        # L'archivio dei giocatori ha una vita sua: si costruisce leggendo i
        # dettagli, che arrivano quando vogliono, e serve solo alle sue pagine.
        self._giocatori: ArchivioGiocatori | None = None
        self._firma_giocatori: tuple | None = None
        self._lock_giocatori = threading.Lock()

    # ---- ciclo di vita dello snapshot --------------------------------------

    def _archive_signature(self) -> tuple:
        """Impronta degli archivi: cambia se e solo se qualcosa ha scritto.

        Quattro pezzi, che si muovono per ragioni diverse: la raccolta
        giornaliera scrive `fixtures.json` quattro volte al giorno, un
        riempimento storico aggiunge una cartella dentro `data/storico/` una
        volta ogni tanto, l'importazione di un file ne aggiunge una dentro
        `data/importato/`, e i **dettagli** arrivano per conto loro.
        Guardare solo il primo lascerebbe il modello fermo alle stagioni di
        prima anche dopo aver ricomprato o importato un campionato intero — con
        il sito aperto e nessun motivo apparente per riavviarlo.

        L'ultimo pezzo e' entrato quando il modello ha cominciato a guardare
        chi sedeva in panchina (`data/panchine.py`). Prima i dettagli erano
        roba delle sole pagine dei giocatori, che hanno un'impronta loro;
        adesso una formazione che arriva puo' cambiare la stima, e una stima
        che non se ne accorge sarebbe ferma all'allenatore di prima.
        """
        path = Path(self.store.root) / "fixtures.json"
        corrente = (path.stat().st_mtime, path.stat().st_size) if path.exists() else (0.0, 0)
        return (
            corrente,
            firma_storico(self.settings.storico_dir),
            firma_importato(self.settings.importato_dir),
            self._archivi_dettagli().details_signature(),
        )

    def snapshot(self, force: bool = False) -> Snapshot:
        with self._lock:
            firma = self._archive_signature()
            attuale = self._snapshot
            if not force and attuale is not None and attuale.archive_signature == firma:
                return attuale
            self._snapshot = self._build(firma)
            return self._snapshot

    def _build(self, firma: tuple) -> Snapshot:
        dataset = load_dataset(
            self.client, self.store, self.settings.seasons, self.settings.league_id,
            storici=build_storico_stores(self.settings),
            importati=build_importato_stores(self.settings),
        )
        adesso = datetime.now(timezone.utc)

        # Chi allenava chi, dalle formazioni gia' sul disco: zero richieste.
        # Un guasto qui non deve impedire di stimare — senza panchine il
        # modello e' quello di sempre, che e' un pronostico in meno di
        # informazione, non una pagina rotta.
        try:
            panchine = costruisci_panchine(self._archivi_dettagli(), dataset.fixtures)
        except Exception as exc:
            logger.warning("panchine non lette: %s", exc)
            panchine = None

        partite = dataset.matches(panchine)
        if not partite:
            return Snapshot(
                dataset=dataset, model=None, built_at=adesso, archive_signature=firma,
                panchine=panchine,
                error="Nessuna partita conclusa disponibile: impossibile stimare il modello.",
            )

        logger.info("stima del modello su %d partite", len(partite))
        modello = fit_dixon_coles(partite)
        self._registra_pronostici(dataset, modello)
        self._risolvi_esiti(dataset)
        return Snapshot(
            dataset=dataset, model=modello, built_at=adesso, archive_signature=firma,
            panchine=panchine,
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

    def accuratezza(self, mode: str = MODO_PREDEFINITO) -> dict:
        """Quanto valgono i pronostici gia' verificati dalla realta'.

        Vuoto finche' non c'e' nulla di risolto — ed e' giusto che l'interfaccia
        mostri il vuoto invece di un numero costruito su tre partite.

        Il modo si passa **esplicitamente** anche se e' il default: dal giorno
        in cui `data/ufficiali.py` comincia a scrivere, una partita ha due
        pronostici, e "quale dei due sta guardando l'hub" e' una domanda con
        una risposta, non un dettaglio. E' il secco perche' e' l'unico che
        esiste per ogni partita: la serie continua, quella il cui andamento nel
        tempo si puo' leggere senza che cambi cosa si sta misurando.
        """
        try:
            return {
                "mercati": accuratezza_per_mercato(self.pronostici, mode=mode),
                "uno_x_due": brier_1x2_multiclasse(self.pronostici, mode=mode),
                "mercato": confronto_col_mercato(self.pronostici, mode=mode),
                "modo": mode,
            }
        except Exception as exc:
            logger.warning("accuratezza non calcolabile: %s", exc)
            return {"mercati": [], "uno_x_due": None, "mercato": None, "modo": mode}

    def confronto_modi(self) -> dict | None:
        """I due tipi di pronostico a confronto, sulle partite che hanno entrambi.

        Sta a parte da `accuratezza` perche' risponde a un'altra domanda. Li'
        si chiede "quanto vale il modello"; qui "rifare il pronostico quando
        escono le formazioni **serve**?" — che e' la ragione per cui
        `data/ufficiali.py` scrive una seconda riga invece di sostituire la
        prima.

        Torna ``None`` quando non c'e' niente da confrontare, che e' la
        situazione finche' le formazioni ufficiali restano spente. Un vuoto e
        un pareggio si mostrano in modo diverso: il primo dice "non ancora
        provato", il secondo "provato e non ha reso".
        """
        try:
            confronto = confronto_fra_modi(self.pronostici)
        except Exception as exc:  # come l'accuratezza: mai far cadere una pagina
            logger.warning("confronto fra i modi non calcolabile: %s", exc)
            return None
        return confronto if confronto["totale"] else None

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

    def congelato(self, fixture_id: int, mode: str = MODE_SECCO) -> Congelato | None:
        """Il pronostico registrato su questa partita, o ``None`` se non c'e'.

        Il compagno di ``predict`` per le partite gia' cominciate: quello
        risponde "cosa penso adesso", questo "cosa avevo detto prima". Sulle
        partite gia' giocate solo il secondo e' onesto.

        Non passa dallo snapshot, ed e' voluto: una riga di registro e'
        immutabile per costruzione e non ha niente da spartire col modello
        caldo, che nel frattempo e' stato ristimato. ``ultimo_per_partita``
        basta a prendere quella giusta senza filtrare sull'orario, perche' dopo
        il fischio d'inizio il registro non accetta scritture: l'ultima riga
        **e'** per costruzione l'ultima precedente al via.

        Come il resto delle letture di registro, non solleva mai: una pagina
        senza pronostico e' un disservizio, una pagina che non si apre e' un
        guasto.
        """
        try:
            riga = self.pronostici.ultimo_per_partita(fixture_id, mode)
            if riga is None:
                return None
            matrice = self.pronostici.matrice(riga["id"])
        except Exception as exc:  # il registro non deve mai far cadere il web
            logger.warning(
                "pronostico congelato non leggibile (partita %s): %s", fixture_id, exc
            )
            return None
        if matrice is None:
            return None
        return Congelato(
            prob=derive_markets(matrice),
            matrice=matrice,
            gol_attesi=(float(riga["lambda_home"]), float(riga["lambda_away"])),
            quando=_istante(riga["created_at"]),
        )

    def affidabilita(
        self, home_id: int, away_id: int
    ) -> tuple[TeamEvidence | None, TeamEvidence | None]:
        """Su quanti dati poggiano le due forze che generano il pronostico.

        Sta accanto a ``predict`` perche' risponde alla stessa richiesta: il
        pronostico e' il numero, questo e' quanto vale la pena fidarsene. Un
        ``None`` per lato non e' un errore ma la stessa cosa che dice
        ``predict``, cioe' che il modello non conosce quella squadra.
        """
        snap = self.snapshot()
        if snap.model is None:
            return (None, None)
        return tuple(  # type: ignore[return-value]
            snap.model.evidence_for(snap.dataset.name(squadra))
            for squadra in (home_id, away_id)
        )

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

    # ---- indisponibili -----------------------------------------------------

    def indisponibili(self, fixture_id: int) -> list[dict]:
        """Chi non gioca questa partita, secondo l'ultima lettura utile.

        Non passa dallo snapshot: sono righe del registro, non un pezzo del
        modello, e cambiano fra una ristima e l'altra — una raccolta a ridosso
        della partita non deve aspettare che qualcosa invalidi l'archivio per
        comparire nella pagina.

        Come tutto cio' che questa classe espone alle pagine, un guasto qui
        vale una sezione vuota, non un errore: la scheda partita deve aprirsi
        anche se il registro non risponde.
        """
        try:
            return self.pronostici.indisponibili_alla_vigilia(fixture_id)
        except Exception as exc:  # sqlite, file mancante, schema vecchio
            logger.warning("indisponibili non letti per la partita %s: %s", fixture_id, exc)
            return []

    # ---- giocatori ---------------------------------------------------------

    def giocatori(self) -> ArchivioGiocatori:
        """Le prestazioni dei giocatori, lette dai dettagli gia' su disco.

        Costruito **su richiesta** e non insieme allo snapshot: l'hub non ne ha
        bisogno, e leggere un file per ogni partita conclusa non deve pesare su
        chi guarda solo i pronostici.

        Si rifa' quando i dettagli cambiano, che non e' quando cambia
        l'archivio delle partite: la raccolta scarica prima gli id e poi, con
        calma e anche giorni dopo, formazioni e statistiche. Da qui un'impronta
        e un lucchetto propri.
        """
        # Fuori dal lucchetto: `snapshot()` prende il suo, e prenderli
        # annidati sarebbe il modo piu' rapido per bloccare il processo.
        dataset = self.snapshot().dataset
        with self._lock_giocatori:
            archivi = self._archivi_dettagli()
            firma = archivi.details_signature()
            if self._giocatori is not None and self._firma_giocatori == firma:
                return self._giocatori
            self._giocatori = costruisci_archivio(archivi, dataset.fixtures)
            self._firma_giocatori = firma
            return self._giocatori

    def _archivi_dettagli(self) -> StoreUnito:
        """Tutti gli archivi che possono contenere un tabellino, in un oggetto solo.

        Prima la stagione in corso, poi le stagioni ricomprate: e' l'ordine in
        cui si vuole trovare il dato quando per caso sta in entrambi.

        Le stagioni **importate da un file non entrano**, ed e' voluto: hanno i
        risultati ma non i tabellini, e non li avranno mai. Contarle qui
        direbbe alle pagine dei giocatori che la copertura e' parziale su un
        campionato che invece copriamo per intero — la stessa trappola gia'
        evitata per lo storico 2022-2024, che pure arriva senza tabellini.
        """
        storici: list[SeasonStore] = list(build_storico_stores(self.settings).values())
        return StoreUnito(stores=[self.store, *storici])

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
            "partite_stagioni_riempite": snap.dataset.n_riempite,
            "stagioni_riempite": snap.dataset.stagioni_riempite,
            "partite_stagioni_importate": snap.dataset.n_importate,
            "stagioni_importate": snap.dataset.stagioni_importate,
            "squadre": len(snap.dataset.team_names),
            "giorni_raccolti": len(giorni),
            "ultimo_giorno_raccolto": giorni[-1].isoformat() if giorni else None,
            "modello_pronto": snap.ready,
            "modello_partite": snap.model.n_matches if snap.model else 0,
            # Quanto delle differenze fra squadre resta nei gol totali, e la
            # media verso cui si comprime il resto. Sta qui perche' e' la
            # sola cosa del pronostico che non si legge dalle forze: due
            # numeri diversi, a parita' di squadre, danno due Over diversi.
            "compressione_gol": snap.model.goal_compression if snap.model else None,
            # Quanto pesano le partite giocate sotto un altro allenatore, e su
            # quante squadre il sistema sappia dire chi le allena. Con il
            # fattore a 1 la seconda riga resta comunque vera e utile: dice
            # che il dato c'e', pronto per il giorno in cui servisse.
            "fattore_allenatore": snap.model.fattore_allenatore if snap.model else None,
            "squadre_con_panchina": (
                len(snap.panchine.squadre()) if snap.panchine else 0
            ),
            "cambi_di_panchina": (
                sum(len(snap.panchine.cambi(sq)) for sq in snap.panchine.squadre())
                if snap.panchine else 0
            ),
            "media_gol_lega": (
                round(snap.model.league_total_goals, 3) if snap.model else None
            ),
            "aggiornato_alle": snap.built_at,
            "pronostici_registrati": self._conteggio_pronostici(),
            "esiti_risolti": self._conteggio_esiti(),
            "partite_quotate": self._conteggio("partite_quotate"),
            "partite_con_indisponibili": self._conteggio("partite_con_indisponibili"),
            "budget_usato": self.client.budget.used,
            "budget_limite": self.settings.daily_request_limit,
            "errore": snap.error,
        }
