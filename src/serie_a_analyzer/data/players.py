"""Schede giocatore ricavate da cio' che e' gia' sul disco.

Questo modulo **non chiede niente all'API**. Ogni partita conclusa lascia
nell'archivio tre dettagli gia' pagati dalla raccolta giornaliera — formazioni,
statistiche per giocatore ed eventi — che finora nessuno leggeva. Qui quei JSON
grezzi diventano le prestazioni di un giocatore, partita per partita.

Tre cose governano il modulo.

**Un dettaglio vive dentro una busta.** `SeasonStore` salva sotto la chiave
``payload`` e ci affianca il numero di tentativi: una busta con payload vuoto
significa "l'API non lo copre", non "non ancora raccolto". `load_detail` la
apre gia', e qui non si legge mai un file a mano.

**L'identita' di un giocatore e' l'id numerico, mai il nome.** Vale la stessa
regola delle squadre: fra endpoint e stagioni lo stesso giocatore compare come
"L. Martinez" o "Lautaro Martinez", e usare il nome come chiave lo sdoppierebbe
in due carriere con meta' storia ciascuna. Il nome e' solo un'etichetta da
mostrare, e si tiene quella vista piu' di recente.

**I numeri vengono da una sorgente sola.** Minuti, gol, assist, cartellini e
voto arrivano tutti da ``players``. Gli eventi non si sommano ai gol: servono
solo a raccontare *quando* si e' segnato, nella scheda della partita. Mescolare
due sorgenti che si sovrappongono e' il modo piu' semplice per contare due
volte lo stesso gol senza accorgersene.

Cio' che manca viene dichiarato, non riempito: `ArchivioGiocatori` sa su quante
partite concluse esistono davvero le statistiche, e le pagine lo dicono.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .store import tabellino_provvisorio

logger = logging.getLogger(__name__)

# Ruoli come li scrive API-Football, nelle formazioni e nelle statistiche.
RUOLI = {
    "G": "portiere",
    "D": "difensore",
    "M": "centrocampista",
    "F": "attaccante",
}


def ruolo_esteso(codice: str | None) -> str | None:
    return RUOLI.get(codice or "")


def _intero(valore: Any, default: int | None = 0) -> int | None:
    """Un contatore dell'API. ``null`` vale zero: non e' un dato mancante.

    API-Football scrive ``null`` dove noi scriveremmo 0 — un portiere ha
    ``goals.total: null``, non ``0``. Per i contatori la traduzione giusta e'
    quindi lo zero; per cio' che l'API a volte non copre affatto, come i tiri,
    si passa ``default=None`` e il valore resta dichiaratamente ignoto.
    """
    if valore is None or valore == "":
        return default
    try:
        return int(valore)
    except (TypeError, ValueError):
        return default


def _decimale(valore: Any) -> float | None:
    if valore is None or valore == "":
        return None
    try:
        return float(valore)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Prestazione:
    """Un giocatore in una partita: quanto ha giocato e cosa ha fatto."""

    fixture_id: int
    quando: datetime
    player_id: int
    nome: str
    team_id: int
    squadra: str
    minuti: int
    titolare: bool
    posizione: str | None
    numero: int | None
    gol: int
    assist: int
    gialli: int
    rossi: int
    tiri: int | None
    tiri_in_porta: int | None
    voto: float | None

    @property
    def ruolo(self) -> str | None:
        return ruolo_esteso(self.posizione)


@dataclass(frozen=True)
class InCampo:
    """Una riga di formazione: chi era in campo, chi in panchina."""

    player_id: int
    nome: str
    numero: int | None
    posizione: str | None
    griglia: str | None        # "4:2", la posizione sul terreno

    @property
    def ruolo(self) -> str | None:
        return ruolo_esteso(self.posizione)


@dataclass(frozen=True)
class Formazione:
    """La formazione di una squadra in una partita."""

    team_id: int
    squadra: str
    modulo: str | None
    allenatore: str | None
    titolari: list[InCampo]
    panchina: list[InCampo]


@dataclass(frozen=True)
class Gol:
    """Un gol come lo racconta il flusso degli eventi."""

    minuto: int | None
    team_id: int
    squadra: str
    marcatore_id: int | None
    marcatore: str | None
    assist_id: int | None
    assist: str | None
    tipo: str | None           # "Normal Goal", "Penalty", "Own Goal"...

    @property
    def su_rigore(self) -> bool:
        return (self.tipo or "").lower() == "penalty"

    @property
    def autogol(self) -> bool:
        return (self.tipo or "").lower() == "own goal"


# ---- lettura dei dettagli ----------------------------------------------------


def prestazioni_partita(store, fixture_id: int, quando: datetime) -> list[Prestazione]:
    """Le prestazioni dei giocatori scesi in campo in una partita.

    Chi non e' entrato resta fuori: un panchinaro inutilizzato compare nel
    payload con i minuti a ``null``, e tenerlo gonfierebbe le presenze con
    partite mai giocate. La panchina, dove serve vederla, sta in `Formazione`.
    """
    blocchi = store.load_detail(fixture_id, "players")
    if not blocchi:
        return []
    if tabellino_provvisorio(blocchi):
        # Un tabellino fermo a partita in corso non e' un tabellino parziale:
        # e' un tabellino SBAGLIATO. Genoa-Napoli del 22 agosto 2026 diceva
        # zero gol e ventidue titolari al 64', e chi lo legge non ha modo di
        # accorgersene — nel conto di una squadra entra come una partita in
        # cui nessuno ha prodotto niente, e i pesi dei giocatori (data/pesi.py)
        # ne escono schiacciati verso il basso senza che nulla lo segnali.
        # Vale come "non ancora raccolto", che e' la verita': arrivera'.
        return []

    out: list[Prestazione] = []
    for blocco in blocchi:
        squadra = blocco.get("team") or {}
        team_id = _intero(squadra.get("id"), None)
        if team_id is None:
            continue
        for voce in blocco.get("players") or []:
            giocatore = voce.get("player") or {}
            player_id = _intero(giocatore.get("id"), None)
            if player_id is None:
                continue
            # `statistics` e' una lista con una sola voce per le partite; le
            # altre forme dell'endpoint (statistiche di stagione) qui non
            # arrivano mai, ma la lista c'e' lo stesso e va spacchettata.
            statistiche = (voce.get("statistics") or [{}])[0]
            partita = statistiche.get("games") or {}
            minuti = _intero(partita.get("minutes"), 0) or 0
            if minuti <= 0:
                continue

            gol = statistiche.get("goals") or {}
            cartellini = statistiche.get("cards") or {}
            tiri = statistiche.get("shots") or {}
            out.append(Prestazione(
                fixture_id=fixture_id,
                quando=quando,
                player_id=player_id,
                nome=giocatore.get("name") or str(player_id),
                team_id=team_id,
                squadra=squadra.get("name") or str(team_id),
                minuti=minuti,
                titolare=not bool(partita.get("substitute")),
                posizione=partita.get("position"),
                numero=_intero(partita.get("number"), None),
                gol=_intero(gol.get("total")) or 0,
                assist=_intero(gol.get("assists")) or 0,
                gialli=_intero(cartellini.get("yellow")) or 0,
                rossi=_intero(cartellini.get("red")) or 0,
                tiri=_intero(tiri.get("total"), None),
                tiri_in_porta=_intero(tiri.get("on"), None),
                voto=_decimale(partita.get("rating")),
            ))
    return out


def _in_campo(voci: Iterable[dict[str, Any]] | None) -> list[InCampo]:
    righe: list[InCampo] = []
    for voce in voci or []:
        giocatore = (voce or {}).get("player") or {}
        player_id = _intero(giocatore.get("id"), None)
        if player_id is None:
            continue
        righe.append(InCampo(
            player_id=player_id,
            nome=giocatore.get("name") or str(player_id),
            numero=_intero(giocatore.get("number"), None),
            posizione=giocatore.get("pos"),
            griglia=giocatore.get("grid"),
        ))
    return righe


def formazioni_partita(store, fixture_id: int) -> list[Formazione]:
    """Le due formazioni di una partita, nell'ordine in cui le da' l'API."""
    blocchi = store.load_detail(fixture_id, "lineups")
    if not blocchi:
        return []

    out: list[Formazione] = []
    for blocco in blocchi:
        squadra = blocco.get("team") or {}
        team_id = _intero(squadra.get("id"), None)
        if team_id is None:
            continue
        out.append(Formazione(
            team_id=team_id,
            squadra=squadra.get("name") or str(team_id),
            modulo=blocco.get("formation"),
            allenatore=(blocco.get("coach") or {}).get("name"),
            titolari=_in_campo(blocco.get("startXI")),
            panchina=_in_campo(blocco.get("substitutes")),
        ))
    return out


def gol_partita(store, fixture_id: int) -> list[Gol]:
    """I gol in ordine di minuto, dal flusso degli eventi.

    Serve a raccontare la partita, non a contare: le somme per giocatore
    vengono da ``players`` e da nessun'altra parte.
    """
    eventi = store.load_detail(fixture_id, "events")
    if not eventi:
        return []

    gol: list[Gol] = []
    for evento in eventi:
        if (evento.get("type") or "") != "Goal":
            continue
        squadra = evento.get("team") or {}
        team_id = _intero(squadra.get("id"), None)
        if team_id is None:
            continue
        marcatore = evento.get("player") or {}
        assist = evento.get("assist") or {}
        tempo = evento.get("time") or {}
        minuto = _intero(tempo.get("elapsed"), None)
        recupero = _intero(tempo.get("extra"), None)
        gol.append(Gol(
            minuto=minuto + recupero if minuto is not None and recupero else minuto,
            team_id=team_id,
            squadra=squadra.get("name") or str(team_id),
            marcatore_id=_intero(marcatore.get("id"), None),
            marcatore=marcatore.get("name"),
            assist_id=_intero(assist.get("id"), None),
            assist=assist.get("name"),
            tipo=evento.get("detail"),
        ))
    # I gol senza minuto in fondo: sono rari e non hanno un posto nel racconto.
    gol.sort(key=lambda g: (g.minuto is None, g.minuto or 0))
    return gol


# ---- aggregazione ------------------------------------------------------------


@dataclass(frozen=True)
class SchedaGiocatore:
    """Quello che sappiamo di un giocatore, tutto derivato dalle prestazioni.

    Non c'e' nessun totale conservato: sono somme calcolate al volo, cosi' una
    scheda non puo' andare fuori sincrono con le partite che la compongono.
    """

    player_id: int
    nome: str
    prestazioni: list[Prestazione]     # dalla piu' recente

    @property
    def presenze(self) -> int:
        return len(self.prestazioni)

    @property
    def da_titolare(self) -> int:
        return sum(1 for p in self.prestazioni if p.titolare)

    @property
    def minuti(self) -> int:
        return sum(p.minuti for p in self.prestazioni)

    @property
    def gol(self) -> int:
        return sum(p.gol for p in self.prestazioni)

    @property
    def assist(self) -> int:
        return sum(p.assist for p in self.prestazioni)

    @property
    def gialli(self) -> int:
        return sum(p.gialli for p in self.prestazioni)

    @property
    def rossi(self) -> int:
        return sum(p.rossi for p in self.prestazioni)

    @property
    def squadra_attuale(self) -> tuple[int, str] | None:
        """La squadra dell'ultima partita giocata, non "la squadra" in assoluto.

        A gennaio si cambia maglia: l'unica cosa che l'archivio puo' dire con
        certezza e' con chi il giocatore e' sceso in campo l'ultima volta.
        """
        if not self.prestazioni:
            return None
        ultima = self.prestazioni[0]
        return (ultima.team_id, ultima.squadra)

    @property
    def squadre(self) -> list[tuple[int, str]]:
        """Le squadre con cui ha giocato, dalla piu' recente."""
        viste: dict[int, str] = {}
        for p in self.prestazioni:
            viste.setdefault(p.team_id, p.squadra)
        return list(viste.items())

    @property
    def ruolo(self) -> str | None:
        """Il ruolo in cui e' sceso in campo piu' spesso."""
        conteggio = Counter(p.posizione for p in self.prestazioni if p.posizione)
        if not conteggio:
            return None
        return ruolo_esteso(conteggio.most_common(1)[0][0])

    @property
    def voti(self) -> list[float]:
        return [p.voto for p in self.prestazioni if p.voto is not None]

    @property
    def voto_medio(self) -> float | None:
        """La media dei voti disponibili, ``None`` se non ce n'e' nessuno.

        API-Football non assegna un voto in tutte le partite. Contare le
        mancanti come zero abbasserebbe la media di chi ha giocato di piu':
        meglio dichiarare su quante partite la media e' calcolata.
        """
        voti = self.voti
        return sum(voti) / len(voti) if voti else None

    @property
    def partite_con_voto(self) -> int:
        return len(self.voti)

    @property
    def minuti_per_gol(self) -> float | None:
        return self.minuti / self.gol if self.gol else None

    def con_squadra(self, team_id: int) -> "SchedaGiocatore":
        """La stessa scheda ristretta a una sola squadra."""
        return SchedaGiocatore(
            player_id=self.player_id,
            nome=self.nome,
            prestazioni=[p for p in self.prestazioni if p.team_id == team_id],
        )


# Come si ordina una classifica: la chiave, e i criteri che rompono la parita'.
_CLASSIFICHE = {
    "gol": lambda s: (s.gol, s.assist, s.minuti),
    "assist": lambda s: (s.assist, s.gol, s.minuti),
    "minuti": lambda s: (s.minuti, s.gol, s.assist),
    "voto": lambda s: (s.voto_medio or 0.0, s.partite_con_voto),
}

# Sotto questa soglia una media voto non e' una media: chi ha giocato una
# partita sola finirebbe in cima alla classifica per caso.
MINIMO_PARTITE_PER_VOTO = 5


@dataclass
class ArchivioGiocatori:
    """Le prestazioni di tutti, indicizzate per giocatore e per squadra.

    Si porta dietro anche quanto e' completo: su quante partite concluse
    esistono davvero le statistiche per giocatore. E' il numero che permette
    alle pagine di dire "questo conto e' parziale" invece di mostrare tre
    presenze come se fossero la stagione intera.
    """

    prestazioni: list[Prestazione]
    partite_concluse: int = 0
    partite_con_statistiche: int = 0
    # Quante sono ferme alla fotografia del feed live. Si contano invece di
    # sparire, perche' "manca" e "l'API non l'ha ancora consolidato" sono due
    # risposte diverse e la seconda si risolve da sola.
    partite_provvisorie: int = 0
    _per_giocatore: dict[int, list[Prestazione]] = field(default_factory=dict, repr=False)
    _per_squadra: dict[int, set[int]] = field(default_factory=dict, repr=False)
    _nomi: dict[int, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # Dalla piu' recente: e' l'ordine in cui si legge una carriera, ed e'
        # cio' che rende `squadra_attuale` la squadra dell'ultima partita.
        self.prestazioni = sorted(self.prestazioni, key=lambda p: p.quando, reverse=True)
        for p in self.prestazioni:
            self._per_giocatore.setdefault(p.player_id, []).append(p)
            self._per_squadra.setdefault(p.team_id, set()).add(p.player_id)
            # Vince il nome piu' recente, cioe' il primo incontrato.
            self._nomi.setdefault(p.player_id, p.nome)

    @property
    def vuoto(self) -> bool:
        return not self.prestazioni

    @property
    def giocatori_conosciuti(self) -> int:
        return len(self._per_giocatore)

    @property
    def copertura(self) -> float:
        """Quota di partite concluse per cui esistono le statistiche, da 0 a 1."""
        if not self.partite_concluse:
            return 0.0
        return self.partite_con_statistiche / self.partite_concluse

    def scheda(self, player_id: int) -> SchedaGiocatore | None:
        righe = self._per_giocatore.get(player_id)
        if not righe:
            return None
        return SchedaGiocatore(
            player_id=player_id, nome=self._nomi[player_id], prestazioni=righe
        )

    def rosa(self, team_id: int) -> list[SchedaGiocatore]:
        """Chi e' sceso in campo per questa squadra, dai piu' impiegati.

        Non e' la rosa ufficiale ma quella *vista giocare*: chi non e' mai
        entrato non compare, perche' l'archivio non ne sa nulla.
        """
        schede = []
        for player_id in self._per_squadra.get(team_id, ()):
            scheda = self.scheda(player_id)
            if scheda is not None:
                schede.append(scheda.con_squadra(team_id))
        return sorted(schede, key=lambda s: (s.minuti, s.gol), reverse=True)

    def squadre(self) -> dict[int, str]:
        """Le squadre di cui si conosce almeno un giocatore."""
        nomi: dict[int, str] = {}
        for p in self.prestazioni:
            nomi.setdefault(p.team_id, p.squadra)
        return nomi

    def classifica(
        self,
        chiave: str = "gol",
        limite: int = 20,
        minimo_voti: int = MINIMO_PARTITE_PER_VOTO,
    ) -> list[SchedaGiocatore]:
        """I primi della lista per gol, assist, minuti o voto medio."""
        ordina = _CLASSIFICHE.get(chiave, _CLASSIFICHE["gol"])
        schede = [s for s in (self.scheda(pid) for pid in self._per_giocatore) if s]
        if chiave == "voto":
            schede = [s for s in schede if s.partite_con_voto >= minimo_voti]
        return sorted(schede, key=ordina, reverse=True)[:limite]


def costruisci_archivio(store, fixtures: Iterable[Any]) -> ArchivioGiocatori:
    """Legge i dettagli delle partite raccolte e ne fa un archivio.

    Costa solo letture da disco, mai richieste all'API: quei dettagli li ha
    gia' pagati la raccolta giornaliera. Resta pero' un lavoro da fare una
    volta e tenere caldo — un file per partita — non a ogni pagina aperta.

    Si guardano solo le partite **di cui possediamo l'archivio grezzo** — la
    raccolta giornaliera e le stagioni eventualmente ricomprate — non tutte
    quelle che il modello conosce. Lo storico 2022-2024 arriva dalla cache come
    elenco di risultati e non ha dettagli: contarlo qui non aggiungerebbe un
    solo giocatore e farebbe sembrare la copertura un decimo di quella vera,
    cioe' direbbe una cosa falsa proprio nel numero che serve a dichiarare
    quanto i conti sono parziali.

    ``store`` puo' essere un `SeasonStore` o piu' archivi messi insieme in uno
    `StoreUnito`: qui servono solo `fixture_ids()` e `load_detail()`, e da dove
    arrivi il tabellino di una partita non cambia niente.
    """
    raccolte = set(store.fixture_ids())
    concluse = [f for f in fixtures if f.is_finished and f.api_id in raccolte]
    prestazioni: list[Prestazione] = []
    con_statistiche = 0
    provvisorie = 0

    for f in concluse:
        righe = prestazioni_partita(store, f.api_id, f.kickoff_utc)
        if righe:
            con_statistiche += 1
            prestazioni.extend(righe)
        elif tabellino_provvisorio(store.load_detail(f.api_id, "players")):
            provvisorie += 1

    logger.info(
        "archivio giocatori: %d prestazioni da %d partite su %d concluse"
        "%s",
        len(prestazioni), con_statistiche, len(concluse),
        f" ({provvisorie} col tabellino fermo a meta' partita)" if provvisorie else "",
    )
    return ArchivioGiocatori(
        prestazioni=prestazioni,
        partite_concluse=len(concluse),
        partite_con_statistiche=con_statistiche,
        partite_provvisorie=provvisorie,
    )
