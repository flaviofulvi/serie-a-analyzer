"""Quanto pesa un giocatore, misurato da cio' che ha prodotto.

Serve a rispondere a una domanda che il modello di squadra non sa nemmeno
porsi: *se nel Como gioca o non gioca Nico Paz, cambia qualcosa?* Dixon-Coles
stima la forza di una SQUADRA, e la squadra e' la stessa che ci giochi Paz o
Perrone.

LA SCELTA DI FONDO, che e' anche la piu' importante

Non si stima la forza dei giocatori dai RISULTATI. La tentazione sarebbe
guardare come va la squadra quando uno c'e' e quando manca, ma quel numero e'
rumore travestito da misura, per tre ragioni che si sommano:

* **poche osservazioni** — un titolare salta cinque o sei partite in una
  stagione, e cinque partite non dicono niente;
* **causa ed effetto confusi** — si sta fuori proprio nei periodi storti, o si
  riposa contro le piccole: "il Como segna meno senza Paz" misura anche
  l'avversario e il calendario, e li attribuisce a lui;
* **l'attribuzione e' un problema inverso** — dai gol di squadra risalire al
  contributo dei singoli e' difficilissimo quando cambiano poche pedine alla
  volta e si segna poco. Nel basket funziona; nel calcio servono molte
  stagioni.

Qui si misura invece cio' che ogni giocatore **ha prodotto**: gol e assist per
minuto giocato. E' un dato che sta gia' sul disco, pagato dalla raccolta
giornaliera, e non costa nessuna richiesta.

IL LIMITE, che va dichiarato e non nascosto

Questo metodo **vede gli attaccanti, non i difensori**. Gol e assist misurano
chi sta davanti: un centrale o un portiere che salta la partita risultano quasi
gratis, e non e' vero. Chi mostra questo numero deve dirlo. Un pezzo difensivo
si potra' aggiungere — dai gol subiti quando quel giocatore era in campo — ma
e' piu' rumoroso e non appartiene alla prima versione.

Due cose che il metodo non sa fare, e che valgono la stessa dichiarazione: un
acquisto nuovo non ha produzione passata (qui viene trattato come un giocatore
medio, non come uno che vale zero — vedi `rapporto_produzione`), e i pesi
dell'anno scorso non si trasferiscono a una squadra che ha cambiato meta' rosa.

LO SHRINKAGE, che e' cio' che tiene in piedi tutto

Un giocatore entrato dieci minuti e andato a segno ha un tasso di un gol ogni
dieci minuti. Preso alla lettera, varrebbe piu' di tutta la squadra. Il tasso
di ognuno viene quindi tirato verso la media della sua squadra con un peso che
dipende da quanto ha giocato: chi ha molti minuti resta dov'e', chi ne ha
pochi finisce vicino alla media. E' la stessa idea del ridge in
`model/dixon_coles.py`, applicata ai giocatori invece che alle squadre.

Sotto un numero minimo di partite con il tabellino non si stima niente e si
restituisce ``None``. ``None`` significa "non lo so" e non deve mai diventare
1.0, che significa "so che non cambia nulla": sono due risposte diverse e
solo la prima e' onesta quando i dati mancano.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Quanto indietro guardare, in giorni. Piu' larga la finestra, piu' stabile la
# stima e piu' vecchia l'informazione: quattro mesi sono una decina di giornate
# per ogni squadra, abbastanza da vedere le rotazioni senza risalire a una rosa
# che non esiste piu'. In giorni e non in giornate perche' l'archivio e' uno
# solo per tutte le squadre, e i recuperi sfasano il calendario.
GIORNI_FINESTRA = 120

# Sotto questa soglia di partite con il tabellino non si stima niente. Non e'
# prudenza generica: con tre partite il giocatore piu' "pesante" della squadra
# e' semplicemente quello che ha segnato per primo.
MINIMO_PARTITE = 5

MINUTI_PARTITA = 90
GIOCATORI_IN_CAMPO = 11

# Forza dello shrinkage, in partite-equivalenti. Con 5, un giocatore che ha
# giocato 5 partite piene sta a meta' strada fra il suo tasso e quello della
# squadra.
PARTITE_DI_PRIOR = 5.0

# Quanto dell'aggiustamento suggerito si applica davvero.
#
# ATTENZIONE: questo numero **non e' calibrato**. Dovra' uscire dal backtest —
# si rigioca il passato con e senza aggiustamento e si guarda quale Brier e'
# migliore — e finche' quel confronto non esiste resta una scelta prudente,
# scritta qui perche' si veda. Meta' significa: credo alla direzione che il
# metodo indica, non alla sua ampiezza.
FATTORE_PREDEFINITO = 0.5

# Tetto al movimento dei gol attesi, in entrambi i versi. Un giocatore
# importante vale 0,2-0,3 gol attesi su una squadra che se ne aspetta poco piu'
# di uno: il 30% e' gia' il caso estremo di un fuoriclasse assente, e oltre
# quella soglia il numero direbbe piu' del metodo che della partita.
LIMITE_SPOSTAMENTO = 0.30

# Quanti titolari sconosciuti all'archivio si accettano prima di rinunciare.
# Qualche acquisto nuovo si assorbe trattandolo da giocatore medio; meta'
# squadra sconosciuta significa che l'archivio parla di un'altra squadra.
MASSIMO_SCONOSCIUTI = 4


@dataclass(frozen=True)
class PesoGiocatore:
    """Quanto produce un giocatore, in gol+assist per 90 minuti."""

    player_id: int
    nome: str
    minuti: int
    gol: int
    assist: int
    # Gia' passato per lo shrinkage: e' il numero da usare, non (gol+assist).
    per_90: float

    @property
    def grezzo_per_90(self) -> float:
        """Il tasso senza shrinkage. Serve a mostrare la differenza, non a usarlo."""
        if self.minuti <= 0:
            return 0.0
        return (self.gol + self.assist) * MINUTI_PARTITA / self.minuti


@dataclass(frozen=True)
class PesiSquadra:
    """I pesi di una squadra e la sua formazione tipo."""

    team_id: int
    partite: int
    pesi: dict[int, PesoGiocatore]
    # Gli undici piu' impiegati nella finestra: e' il metro di paragone.
    formazione_tipo: tuple[int, ...]
    produzione_tipo: float

    @property
    def produzione_media(self) -> float:
        """Quanto produce un titolare qualsiasi. E' il valore dato a chi non conosciamo."""
        if not self.formazione_tipo:
            return 0.0
        return self.produzione_tipo / len(self.formazione_tipo)


@dataclass(frozen=True)
class Rapporto:
    """Quanta della produzione abituale e' in campo oggi.

    Non e' solo un numero: si porta dietro **quanto ci si puo' credere**. Un
    rapporto calcolato su cinque titolari sconosciuti all'archivio ha lo stesso
    aspetto di uno calcolato su undici noti, e non vale la stessa cosa.
    """

    valore: float
    noti: int
    sconosciuti: int

    @property
    def affidabile(self) -> bool:
        return self.sconosciuti <= MASSIMO_SCONOSCIUTI

    @property
    def variazione_percentuale(self) -> float:
        return (self.valore - 1.0) * 100.0


def pesi_squadra(
    archivio,
    team_id: int,
    minimo_partite: int = MINIMO_PARTITE,
) -> PesiSquadra | None:
    """I pesi dei giocatori di una squadra, dall'archivio delle prestazioni.

    ``archivio`` e' un `ArchivioGiocatori` (data/players.py) costruito **sulla
    sola finestra recente**: le prestazioni di due anni fa non raccontano la
    rosa di oggi, e il modo per escluderle e' passare meno partite, non
    scontarle qui.

    Restituisce ``None`` quando l'archivio non ne sa abbastanza. Chi chiama
    deve distinguere quel ``None`` da un rapporto pari a 1: il primo e' "non lo
    so", il secondo e' "so che non cambia".
    """
    rosa = archivio.rosa(team_id)
    if not rosa:
        return None

    partite = len({p.fixture_id for s in rosa for p in s.prestazioni})
    if partite < minimo_partite:
        logger.debug(
            "pesi: squadra %s ha solo %d partite con tabellino, non basta",
            team_id, partite,
        )
        return None

    minuti_totali = sum(s.minuti for s in rosa)
    produzione_totale = sum(s.gol + s.assist for s in rosa)
    if minuti_totali <= 0:
        return None

    # Il tasso della squadra e' il centro verso cui tutti vengono tirati.
    tasso_squadra = produzione_totale / minuti_totali
    prior_minuti = PARTITE_DI_PRIOR * MINUTI_PARTITA

    pesi: dict[int, PesoGiocatore] = {}
    for s in rosa:
        prodotto = s.gol + s.assist
        tasso = (
            (prodotto + prior_minuti * tasso_squadra) / (s.minuti + prior_minuti)
        )
        pesi[s.player_id] = PesoGiocatore(
            player_id=s.player_id,
            nome=s.nome,
            minuti=s.minuti,
            gol=s.gol,
            assist=s.assist,
            per_90=tasso * MINUTI_PARTITA,
        )

    # `rosa` arriva gia' ordinata per minuti: i primi undici sono la
    # formazione tipo, cioe' la squadra "normale" contro cui si misura quella
    # di oggi.
    tipo = tuple(s.player_id for s in rosa[:GIOCATORI_IN_CAMPO])
    return PesiSquadra(
        team_id=team_id,
        partite=partite,
        pesi=pesi,
        formazione_tipo=tipo,
        produzione_tipo=sum(pesi[pid].per_90 for pid in tipo),
    )


def rapporto_produzione(
    pesi: PesiSquadra, titolari: Iterable[int]
) -> Rapporto | None:
    """Quanta della produzione abituale scende in campo oggi.

    1.0 significa "la formazione tipo". 0.8 significa che manca un quinto di
    cio' che quella squadra produce di solito.

    Un titolare che l'archivio non conosce — un acquisto nuovo, un ragazzo
    all'esordio — vale la **produzione media di un titolare**, non zero.
    Dargli zero direbbe che la squadra e' crollata, e non e' quello che si sa
    di lui: si sa che non si sa. Quanti sono viene contato e riportato, perche'
    un rapporto costruito per meta' su ipotesi va dichiarato.
    """
    elenco = list(titolari)
    if not elenco or pesi.produzione_tipo <= 0:
        return None

    neutro = pesi.produzione_media
    noti = sconosciuti = 0
    oggi = 0.0
    for player_id in elenco:
        peso = pesi.pesi.get(player_id)
        if peso is None:
            sconosciuti += 1
            oggi += neutro
        else:
            noti += 1
            oggi += peso.per_90

    return Rapporto(
        valore=oggi / pesi.produzione_tipo,
        noti=noti,
        sconosciuti=sconosciuti,
    )


def aggiusta_gol_attesi(
    lam: float,
    rapporto: Rapporto | None,
    fattore: float = FATTORE_PREDEFINITO,
    limite: float = LIMITE_SPOSTAMENTO,
) -> float:
    """Sposta i gol attesi in proporzione alla produzione in campo.

    Senza rapporto, o con un rapporto poco affidabile, restituisce ``lam``
    intatto: non sapere e' un motivo per non toccare niente, non per indovinare.
    """
    if rapporto is None or not rapporto.affidabile:
        return lam
    spostamento = fattore * (rapporto.valore - 1.0)
    spostamento = max(-limite, min(limite, spostamento))
    return lam * (1.0 + spostamento)


def vale_la_pena(
    casa: Rapporto | None, ospite: Rapporto | None, soglia: float = 0.02
) -> bool:
    """Questo aggiustamento cambia abbastanza da meritare una riga in archivio?

    I pronostici sono immutabili e non si cancellano: una riga
    'formazione_ufficiale' identica a quella 'secca' non e' inutile, e' dannosa,
    perche' gonfia il confronto fra le due modalita' con partite in cui non
    c'era niente da confrontare. Meglio nessuna riga.

    E' anche cio' che tiene spenta tutta la macchina finche' i tabellini sul
    disco non bastano: senza pesi non ci sono rapporti, senza rapporti non si
    scrive niente.
    """
    return any(
        r is not None and r.affidabile and abs(r.valore - 1.0) >= soglia
        for r in (casa, ospite)
    )


def titolari_da_formazione(formazione: Any) -> list[int]:
    """Gli id degli undici titolari, che e' l'unica cosa che serve qui."""
    return [riga.player_id for riga in getattr(formazione, "titolari", [])]


def partite_recenti(
    fixtures: Iterable[Any], adesso, giorni: int = GIORNI_FINESTRA
) -> list[Any]:
    """Le partite concluse dentro la finestra, che sono quelle da cui pesare.

    Serve a costruire l'archivio delle prestazioni su cio' che racconta la rosa
    di oggi. Guardare tutto l'archivio darebbe pesi piu' stabili e riferiti a
    una squadra che non esiste piu': meta' dei giocatori hanno cambiato maglia.
    """
    from datetime import timedelta

    inizio = adesso - timedelta(days=giorni)
    return [
        f for f in fixtures
        if getattr(f, "is_finished", False) and inizio <= f.kickoff_utc <= adesso
    ]
