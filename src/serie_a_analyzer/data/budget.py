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
import threading
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

# Su Windows aprire un file che qualcun altro sta leggendo o sostituendo puo'
# fallire per un istante: non e' un guasto, e' una finestra che si chiude da
# sola in pochi millisecondi. Quanto insistere prima di crederci davvero.
# Misurato il 4 settembre 2026, senza tentativi e con quattro lettori del
# contatore addosso: 2932 scritture su 3000 fallivano. Con i tentativi e un
# lettore ogni 50 millisecondi — molto piu' di quanto il sito legga davvero —
# non ne fallisce nessuna.
TENTATIVI_CONDIVISIONE = 20
ATTESA_FRA_TENTATIVI = 0.02


def _riprovando(azione, *args, **kwargs):
    """Esegue ``azione``, insistendo sui permessi negati passeggeri.

    Windows nega l'accesso a un file mentre un altro processo lo tiene aperto
    o lo sta sostituendo, e lo fa con lo stesso errore di un permesso mancante
    per davvero. La differenza non si legge dall'errore: si legge dal fatto che
    il primo sparisce da solo e il secondo no. Quindi si riprova, e solo chi
    resiste a tutti i tentativi viene creduto.
    """
    for rimasti in range(TENTATIVI_CONDIVISIONE - 1, -1, -1):
        try:
            return azione(*args, **kwargs)
        except PermissionError:
            if not rimasti:
                raise
            time.sleep(ATTESA_FRA_TENTATIVI)


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

    def _stato_vuoto(self) -> dict[str, Any]:
        return {"day": self._today().isoformat(), "used": 0, "by_priority": {}}

    def _load(self) -> dict[str, Any]:
        """Legge il contatore, distinguendo "non c'e'" da "non si riesce".

        Le due cose finiscono allo stesso posto — si riparte da zero — ma solo
        la prima e' un fatto. La seconda e' una rinuncia, e va detta: ripartire
        da zero significa regalarsi un intero budget giornaliero, e nessuno se
        ne accorgerebbe.

        Il file non si controlla con `exists()` prima di aprirlo, e non e' un
        risparmio ma una correzione: mentre un altro processo sostituisce il
        contatore, su Windows anche `exists()` puo' rispondere di no — e
        rispondeva di no in silenzio, senza nemmeno il warning, perche' quel
        ramo credeva di aver trovato un contatore mai scritto. Si apre e basta:
        un file che non c'e' lo dice da solo con FileNotFoundError.
        """
        try:
            testo = _riprovando(self.state_path.read_text, encoding="utf-8")
        except FileNotFoundError:
            # Non e' mai stato scritto: zero e' la verita', non una rinuncia.
            return self._stato_vuoto()
        except OSError:
            logger.warning(
                "contatore del budget non leggibile (%s): si riparte da zero, "
                "oggi si potrebbe spendere piu' del dovuto", self.state_path,
            )
            return self._stato_vuoto()
        try:
            state = json.loads(testo)
        except json.JSONDecodeError:
            # Da quando `_save` scrive in modo atomico questo ramo non dovrebbe
            # scattare mai piu': se scatta e' una notizia.
            logger.warning(
                "contatore del budget illeggibile (%s): si riparte da zero, "
                "oggi si potrebbe spendere piu' del dovuto", self.state_path,
            )
            return self._stato_vuoto()
        if state.get("day") != self._today().isoformat():
            return self._stato_vuoto()
        return state

    def _provvisorio(self) -> Path:
        """Il file di fianco su cui si scrive prima di rinominare.

        Un nome per SCRITTORE — processo piu' thread — e non uno per contatore:
        il lucchetto e' volutamente a perdere, quindi due scrittori possono
        coesistere per disegno, e con un nome solo si troncavano a vicenda.
        """
        return self.state_path.with_name(
            f"{self.state_path.stem}.{os.getpid()}-{threading.get_ident()}.tmp"
        )

    def _save(self, state: dict[str, Any]) -> None:
        """Scrive il contatore senza poter lasciare un file a meta'.

        Si scrive di fianco e si rinomina, che per il filesystem e'
        un'operazione sola: chi legge nello stesso istante vede il file di
        prima o quello dopo, mai i primi cento byte del nuovo. Prima si
        scriveva sul posto, e un'interruzione a meta' — il computer che si
        spegne, una raccolta fermata con Ctrl+C — lasciava un JSON troncato,
        che `_load` traduce in "usate zero richieste": cioe' un budget intero
        regalato proprio nel giorno in cui qualcosa era andato storto.

        Il file di fianco ha un nome DIVERSO per ogni scrittore (processo piu'
        thread), e non e' un dettaglio: era uno solo per tutti, e il lucchetto
        e' volutamente a perdere, quindi due scrittori possono coesistere per
        disegno. Coesistendo si pestavano lo stesso `.tmp` — uno lo troncava
        mentre l'altro lo rinominava — e ne usciva un `PermissionError` che
        nessun chiamante cattura: la raccolta moriva li'. Un nome per ciascuno
        rende il ripiego innocuo, che e' la cosa che il ripiego prometteva.

        La rinomina insiste (`_riprovando`) perche' i LETTORI stanno fuori dal
        lucchetto — `client.py` scrive `budget.used` nel log dopo ogni
        richiesta, `/stato` lo mostra a ogni pagina — e su Windows un lettore
        che tiene aperto il file fa fallire chi lo sostituisce. Misurato il 4
        settembre 2026, prima che si insistesse: con quattro lettori addosso
        fallivano 2932 scritture su 3000, e l'errore usciva da `spend`.

        Se dopo tutti i tentativi non si riesce comunque, si rinuncia e lo si
        dice: questa richiesta risultera' non spesa. E' la stessa scelta del
        lucchetto e per la stessa ragione — perdere un conteggio e' un
        fastidio, fermare la raccolta costa una giornata di partite che l'API
        non restituira' mai piu'.

        Il limite, misurato e non dedotto: con QUATTRO lettori in ciclo stretto
        addosso — che nessun processo di questo progetto fa — i tentativi non
        bastano piu' e si perde un conteggio su cinque, sempre dicendolo. Con
        un lettore ogni 50 millisecondi, che e' molto piu' di quanto il sito
        legga davvero, non se ne perde nessuno. Chi un giorno mettesse il
        contatore sotto interrogazione continua deve saperlo: la difesa e'
        dimensionata sui lettori che ci sono.
        """
        provvisorio = self._provvisorio()
        try:
            provvisorio.write_text(json.dumps(state, indent=2), encoding="utf-8")
            _riprovando(os.replace, provvisorio, self.state_path)
        except OSError as exc:
            logger.warning(
                "contatore del budget non scrivibile (%s): questa richiesta non "
                "risultera' spesa, oggi si potrebbe spendere piu' del dovuto", exc,
            )
            try:
                provvisorio.unlink()
            except OSError:
                pass

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

        "Occupato" su Windows si presenta in DUE modi e non in uno: un
        lucchetto appena tolto resta in cancellazione per un istante, e chi
        prova ad aprirlo in quella finestra non riceve FileExistsError ma un
        permesso negato. Trattare il secondo come "filesystem che non
        collabora" — cioe' proseguire subito e senza dirlo — era il modo di
        uscire dalla sezione critica in silenzio, ed e' il difetto che faceva
        sparire conteggi: 8 volte su 3192 prese, misurate il 4 settembre 2026.
        Non lo si distingue da un permesso mancante per davvero guardando
        l'errore, e nemmeno chiedendo se il file c'e' (`exists()` risponde di
        no anche li'): lo si distingue aspettando, perche' il primo passa da
        solo e il secondo no. Chi resiste fino alla scadenza finisce quindi
        nello stesso ripiego di un lucchetto occupato sul serio, che e'
        limitato e dichiarato.
        """
        lucchetto = self.state_path.with_suffix(".lock")
        scadenza = time.monotonic() + ATTESA_LUCCHETTO
        preso = False
        while True:
            try:
                os.close(os.open(lucchetto, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                preso = True
                break
            except (FileExistsError, PermissionError):
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
            except OSError as exc:
                # Un filesystem che non collabora non deve fermare la raccolta.
                # Ma va DETTO: e' l'unico ramo che rinuncia al lucchetto senza
                # nemmeno aspettare, e in silenzio nessuno lo scoprirebbe.
                logger.warning(
                    "lucchetto del budget non creabile (%s): si prosegue senza, "
                    "un conteggio potrebbe perdersi", exc,
                )
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
