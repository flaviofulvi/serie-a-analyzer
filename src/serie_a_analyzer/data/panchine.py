"""Chi allenava chi, e da quando. Ricavato dal disco, senza spendere richieste.

Ogni risposta di ``/fixtures/lineups`` porta l'allenatore delle due squadre.
Quelle buste sono gia' pagate — dalla raccolta giornaliera per la stagione in
corso, dal riempimento storico per le stagioni ricomprate — e finora nessuno
leggeva quel campo. Qui diventa la cronologia delle panchine: per ogni squadra,
chi sedeva in panchina e in quale tratto di calendario.

**A cosa serve, e a cosa no.** Non serve a stimare "quanto vale un allenatore":
una forza che viaggia con lui da una squadra all'altra non e' identificabile
con un campionato solo, e chi ci prova misura la squadra credendo di misurare
l'uomo. Serve a una cosa sola e piu' piccola: sapere **quando una squadra ha
smesso di essere se stessa**. Il modello pesa le partite solo per l'eta'
(``exp(-xi * giorni)``), quindi una squadra che ha cambiato allenatore tre
giornate fa si porta dietro il girone precedente a peso pieno, come se fosse la
stessa squadra. Non lo e', e questo modulo e' cio' che permette di dirlo.

**L'identita' e' l'id, mai il nome.** Vale la regola di giocatori e squadre, e
qui morde piu' che altrove: API-Football scrive "Allegri" in una busta e
"M. Allegri" in un'altra, e un cambio di panchina inventato spezzerebbe la
storia di una squadra in due tronconi nel punto sbagliato.

**Una lettura mancante non spezza una panchina.** Se di una partita le
formazioni non ci sono — non ancora raccolte, o una stagione che non le ha —
quella partita non dice niente su chi allenava: si salta, e il tratto continua.
Trattarla come un cambio produrrebbe cambi di allenatore dal nulla, ed e' il
modo piu' rapido di far dimenticare al modello meta' campionato.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from .players import formazioni_partita

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Panchina:
    """Un tratto di panchina: un allenatore, una squadra, un intervallo.

    Le date sono quelle della **prima e dell'ultima partita conosciuta** con
    quell'allenatore, non quelle del contratto: l'archivio sa cosa e' successo
    in campo e nient'altro. Un esonero deciso in settimana si vede qui alla
    domenica successiva, ed e' esattamente il momento in cui comincia a
    contare per il modello.
    """

    team_id: int
    allenatore_id: int
    nome: str
    dal: date
    al: date
    partite: int


@dataclass
class Panchine:
    """La cronologia delle panchine, pronta da interrogare.

    Due letture, che servono a due cose diverse: ``allenatore`` risponde
    partita per partita ed e' cio' che il modello usa per pesare; la
    ``cronologia`` racconta una squadra ed e' cio' che una pagina mostra.
    """

    per_partita: dict[int, dict[int, int]] = field(default_factory=dict)
    _nomi: dict[int, str] = field(default_factory=dict, repr=False)
    _cronologia: dict[int, list[Panchina]] = field(default_factory=dict, repr=False)
    # Su quante partite le formazioni c'erano davvero. Una cronologia costruita
    # su tre partite non e' sbagliata, e' muta: dichiararlo permette a chi la
    # usa di non scambiare il silenzio per stabilita'.
    partite_lette: int = 0
    partite_con_formazioni: int = 0

    @property
    def vuoto(self) -> bool:
        return not self.per_partita

    def allenatore(self, fixture_id: int, team_id: int) -> int | None:
        """Chi allenava questa squadra in questa partita, se lo sappiamo."""
        return self.per_partita.get(fixture_id, {}).get(team_id)

    def nome(self, allenatore_id: int | None) -> str | None:
        if allenatore_id is None:
            return None
        return self._nomi.get(allenatore_id)

    def cronologia(self, team_id: int) -> list[Panchina]:
        """I tratti di panchina di una squadra, dal piu' vecchio."""
        return list(self._cronologia.get(team_id, ()))

    def attuale(self, team_id: int) -> Panchina | None:
        """L'ultimo tratto conosciuto, cioe' chi allena adesso.

        "Adesso" e' relativo ai dati, non al calendario: e' l'allenatore
        dell'ultima partita di cui possediamo le formazioni. In un backtest,
        dove le partite finiscono al giorno del taglio, e' l'allenatore di
        allora — ed e' cio' che rende la misura onesta senza doverlo dire
        due volte.
        """
        tratti = self._cronologia.get(team_id)
        return tratti[-1] if tratti else None

    def cambi(self, team_id: int) -> list[Panchina]:
        """I tratti successivi al primo: ogni elemento e' un cambio."""
        return self.cronologia(team_id)[1:]

    def squadre(self) -> list[int]:
        return sorted(self._cronologia)


def _tratti(
    letture: list[tuple[date, int, str]],
) -> list[Panchina]:
    """Da un elenco di (data, id allenatore, nome) ai tratti di panchina.

    Un tratto si chiude quando l'id cambia. Un allenatore che torna dopo un
    altro apre un tratto nuovo, e cosi' dev'essere: la squadra allenata da
    lui a novembre e quella di marzo sono separate da un periodo in cui non
    la allenava.
    """
    tratti: list[Panchina] = []
    for quando, allenatore_id, nome in letture:
        ultimo = tratti[-1] if tratti else None
        if ultimo is not None and ultimo.allenatore_id == allenatore_id:
            tratti[-1] = Panchina(
                team_id=ultimo.team_id,
                allenatore_id=allenatore_id,
                nome=nome or ultimo.nome,
                dal=ultimo.dal,
                al=max(ultimo.al, quando),
                partite=ultimo.partite + 1,
            )
        else:
            tratti.append(Panchina(
                team_id=0,  # riempito da chi chiama: qui la squadra non serve
                allenatore_id=allenatore_id,
                nome=nome,
                dal=quando,
                al=quando,
                partite=1,
            ))
    return tratti


def costruisci_panchine(store, fixtures: Iterable[Any]) -> Panchine:
    """Legge le formazioni gia' sul disco e ne ricava chi allenava chi.

    Costa solo letture da disco, mai richieste all'API: e' lo stesso patto di
    `data/players.py`, e per la stessa ragione — quelle buste sono gia' state
    pagate una volta.

    Si guardano le partite **concluse di cui possediamo l'archivio grezzo**.
    Una stagione che abbiamo solo come elenco di risultati — lo storico
    2022-2024 dalla cache, o una stagione importata da un file — non ha
    formazioni e non ne avra' mai: non compare, e va bene cosi'. Sulle sue
    partite il modello continuera' a pesare solo per l'eta', che e' quello che
    ha sempre fatto.

    ``store`` puo' essere un `SeasonStore` o piu' archivi in uno `StoreUnito`:
    servono solo `fixture_ids()` e `load_detail()`.
    """
    raccolte = set(store.fixture_ids())
    concluse = sorted(
        (f for f in fixtures if f.is_finished and f.api_id in raccolte),
        key=lambda f: f.kickoff_utc,
    )

    per_partita: dict[int, dict[int, int]] = {}
    nomi: dict[int, str] = {}
    letture: dict[int, list[tuple[date, int, str]]] = {}
    con_formazioni = 0

    for f in concluse:
        formazioni = formazioni_partita(store, f.api_id)
        if not formazioni:
            continue
        visti = False
        for schieramento in formazioni:
            if schieramento.allenatore_id is None:
                continue
            visti = True
            per_partita.setdefault(f.api_id, {})[schieramento.team_id] = (
                schieramento.allenatore_id
            )
            # Vince il nome piu' recente: e' l'ultima grafia che l'API ha
            # usato, e l'unica cosa che il nome debba fare e' comparire.
            nomi[schieramento.allenatore_id] = (
                schieramento.allenatore or nomi.get(schieramento.allenatore_id) or
                str(schieramento.allenatore_id)
            )
            letture.setdefault(schieramento.team_id, []).append((
                f.kickoff_utc.date(),
                schieramento.allenatore_id,
                schieramento.allenatore or str(schieramento.allenatore_id),
            ))
        if visti:
            con_formazioni += 1

    cronologia: dict[int, list[Panchina]] = {}
    for team_id, righe in letture.items():
        tratti = _tratti(sorted(righe, key=lambda r: r[0]))
        cronologia[team_id] = [
            Panchina(
                team_id=team_id,
                allenatore_id=t.allenatore_id,
                nome=nomi.get(t.allenatore_id, t.nome),
                dal=t.dal,
                al=t.al,
                partite=t.partite,
            )
            for t in tratti
        ]

    logger.info(
        "panchine: %d squadre, %d partite con le formazioni su %d concluse",
        len(cronologia), con_formazioni, len(concluse),
    )
    return Panchine(
        per_partita=per_partita,
        _nomi=nomi,
        _cronologia=cronologia,
        partite_lette=len(concluse),
        partite_con_formazioni=con_formazioni,
    )
