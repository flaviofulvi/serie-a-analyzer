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

Qui si misura invece cio' che ogni giocatore **ha fatto davanti**: gol,
assist e **tiri** per minuto giocato (vedi `produzione`). E' un dato che sta
gia' sul disco, pagato dalla raccolta giornaliera, e non costa nessuna
richiesta.

I tiri ci sono per una ragione sola, ed e' misurata: un titolare produce 0,08
gol a partita, e su una quarantina di episodi in tutta la stagione non si
distinguono quattro giocatori. Il tiro e' lo stesso gesto un passo prima,
capita otto volte piu' spesso, e indovina meglio la produzione futura
(r 0.645 -> 0.728 su quattro stagioni, `scripts/prova_pesi.py`). Il
**bersaglio** pero' non cambia mai: restano gol e assist la cosa che si vuole
prevedere, i tiri sono solo il modo meno rumoroso di arrivarci.

IL LIMITE, che va dichiarato e non nascosto

Questo metodo **vede gli attaccanti, non i difensori**, e i tiri non lo
curano: misurano chi sta davanti esattamente come gol e assist. Un centrale o
un portiere che salta la partita risultano quasi gratis, e non e' vero. Chi mostra questo numero deve dirlo. Un pezzo difensivo
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

# Quanto pesa un gol (o un assist) rispetto a un tiro, nel numeratore della
# produzione. Non e' una scelta di gusto: viene da `scripts/prova_pesi.py`,
# misurato il 24 agosto 2026 su quattro stagioni. Vedi `produzione` per il
# perche' i tiri entrano affatto.
#
# Il numero e' scelto con la regola del progetto — per ogni stagione il peso
# migliore sulle **altre tre**, poi misurato su quella tenuta fuori — e viene
# 2 tre volte su quattro. Sulla stagione mai vista guadagna sempre, da +0.050
# a +0.101 di correlazione. L'ottimo e' **piatto fra 1 e 3**: chi lo ritocca
# dentro quell'intervallo non sta sbagliando, chi lo porta a 8 si'.
#
# Attenzione a una cosa che non si vede: un gol e' ANCHE un tiro, e nel
# tabellino e' contato in tutti e due i campi. Con PESO_PRODUZIONE = 2 un gol
# vale quindi 3 in totale e un assist 2.
PESO_PRODUZIONE = 2

# Forza dello shrinkage, in partite-equivalenti. Con 5, un giocatore che ha
# giocato 5 partite piene sta a meta' strada fra il suo tasso e quello della
# squadra.
PARTITE_DI_PRIOR = 5.0

# Quanto dell'aggiustamento suggerito si applica davvero.
#
# Misurato l'11 settembre 2026 (`backtest.py --cerca-fattore`, quattro
# stagioni): la curva e' piatta fra 0 e 0.5 e peggiora sopra. Il migliore e'
# 0.25, ma sul secco vale 0.58 errori standard, e questo 0.5 vale quanto il
# secco. Non si cambia: un ottimo che non si distingue dal caso non si
# propone, e la macchina che lo userebbe e' spenta per la stessa misura
# (`MACCHINA_ACCESA` in data/ufficiali.py).
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


def produzione(gol: int, assist: int, tiri: int) -> int:
    """Il numeratore del peso di un giocatore: quanto ha inciso davanti.

    Non sono solo gol e assist, ed e' la correzione piu' importante fatta a
    questo modulo. Un titolare produce **0,08 gol a partita**: in una stagione
    intera fa una quarantina di episodi, e distinguere quattro giocatori con
    quaranta episodi non si puo'. Chi ne fa 5 invece di 3 puo' semplicemente
    essere stato fortunato, e il modulo lo prendeva per piu' forte.

    Il **tiro** e' lo stesso gesto un passo prima, dove la fortuna non e'
    ancora intervenuta, e capita 0,62 volte a partita: otto volte piu' spesso.
    Contarlo non cambia cosa si vuole sapere — resta "quanto incide davanti" —
    cambia su quante prove lo si giudica.

    Misurato prima di scriverlo (`scripts/prova_pesi.py`, quattro stagioni,
    1036 giocatori-stagione): la capacita' di indovinare i gol+assist futuri
    di un giocatore passa da r = 0.645 a r = 0.728, e il vantaggio tiene su
    tutte e quattro le stagioni — la peggiore guadagna +0.050. Sopravvive
    anche allo shrinkage qui sotto, che era il rischio vero: lo shrinkage
    aiuta di piu' il metro piu' rumoroso, quindi avrebbe potuto mangiarsi il
    vantaggio, e invece lo lascia intero.

    Due cose che sono state provate e NON entrano:

    * i **passaggi chiave** sopra i tiri non aggiungono niente (0.701 contro
      0.711 dei soli tiri): la creazione la cattura gia' il tiro. Da soli sono
      molto peggio (0.504);
    * i **tiri da soli**, senza gol e assist, perdono contro la somma (0.711
      contro 0.728). L'esito qualcosa aggiunge, non e' solo rumore.
    """
    return (gol + assist) * PESO_PRODUZIONE + tiri


@dataclass(frozen=True)
class PesoGiocatore:
    """Quanto incide un giocatore davanti, per 90 minuti.

    Gol, assist e tiri restano visibili uno per uno: `per_90` e' un indice
    composto (vedi `produzione`) e da solo non si spiega a nessuno. Chi lo
    mostra in pagina deve poter dire da cosa viene.
    """

    player_id: int
    nome: str
    minuti: int
    gol: int
    assist: int
    tiri: int
    # Gia' passato per lo shrinkage: e' il numero da usare, non la produzione
    # grezza.
    per_90: float

    @property
    def grezzo_per_90(self) -> float:
        """Il tasso senza shrinkage. Serve a mostrare la differenza, non a usarlo."""
        if self.minuti <= 0:
            return 0.0
        return produzione(self.gol, self.assist, self.tiri) * MINUTI_PARTITA / self.minuti


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
    produzione_totale = sum(produzione(s.gol, s.assist, s.tiri) for s in rosa)
    if minuti_totali <= 0:
        return None

    # Il tasso della squadra e' il centro verso cui tutti vengono tirati.
    tasso_squadra = produzione_totale / minuti_totali
    prior_minuti = PARTITE_DI_PRIOR * MINUTI_PARTITA

    pesi: dict[int, PesoGiocatore] = {}
    for s in rosa:
        prodotto = produzione(s.gol, s.assist, s.tiri)
        tasso = (
            (prodotto + prior_minuti * tasso_squadra) / (s.minuti + prior_minuti)
        )
        pesi[s.player_id] = PesoGiocatore(
            player_id=s.player_id,
            nome=s.nome,
            minuti=s.minuti,
            gol=s.gol,
            assist=s.assist,
            tiri=s.tiri,
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

    I titolari devono essere **undici**, e chi ne passa un numero diverso si
    sente rispondere "non lo so". Non e' pignoleria sui tipi: il denominatore
    e' la produzione di undici giocatori (`produzione_tipo`), quindi un
    numeratore che ne somma dieci non da' un rapporto piu' basso, da' un
    rapporto fra cose diverse. Dieci undicesimi di una squadra qualunque fanno
    ~0.91 — abbastanza da superare la soglia di `vale_la_pena` e da spostare i
    gol attesi — e nessuno se ne accorgerebbe: `affidabile` conta i giocatori
    **sconosciuti**, non quelli mancanti, quindi una formazione a dieci ha
    esattamente l'aspetto di una completa. E' successo davvero il 29 agosto
    2026, quando l'API ha servito distinte a meta' a quindici minuti dal via
    (vedi `formazione_provvisoria` in data/store.py).
    """
    elenco = list(titolari)
    if len(elenco) != GIOCATORI_IN_CAMPO or pesi.produzione_tipo <= 0:
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
    fixtures: Iterable[Any],
    adesso,
    giorni: int = GIORNI_FINESTRA,
    stagione: int | None = None,
) -> list[Any]:
    """Le partite concluse dentro la finestra, che sono quelle da cui pesare.

    Serve a costruire l'archivio delle prestazioni su cio' che racconta la rosa
    di oggi. Guardare tutto l'archivio darebbe pesi piu' stabili e riferiti a
    una squadra che non esiste piu': meta' dei giocatori hanno cambiato maglia.

    Due tagli, e servono tutti e due perche' tagliano cose diverse.

    Il primo sono i **giorni**, e vale dentro una stagione: a marzo le partite
    di ottobre raccontano una rosa che il mercato di gennaio ha gia' cambiato.

    Il secondo e' la **stagione**, e vale al confine fra due: fra maggio e
    agosto c'e' il mercato estivo, che e' il momento in cui una rosa cambia di
    piu'. Senza questo taglio, ad agosto la finestra di 120 giorni arriva ad
    aprile e pesca le ultime giornate dell'anno prima — cinque, cioe'
    esattamente quante ne servono per accendere la macchina (`MINIMO_PARTITE`).
    Ne uscirebbero pronostici costruiti su rose di PRIMA del mercato, e i
    pronostici sono immutabili: quelle righe resterebbero per sempre nel
    confronto fra i due Brier, che e' l'unica ragione per cui i pronostici con
    le formazioni ufficiali esistono.

    Il taglio per stagione toglie anche un vincolo che non c'entra niente col
    modello: senza, non si potevano comprare i tabellini di una stagione
    conclusa senza far partire la macchina per sbaglio. Con, si comprano quando
    si vuole.

    ``stagione`` a ``None`` non taglia niente ed e' quel che serve ai test e a
    chi vuole misurare a cavallo di due stagioni di proposito.
    """
    from datetime import timedelta

    inizio = adesso - timedelta(days=giorni)
    return [
        f for f in fixtures
        if getattr(f, "is_finished", False)
        and inizio <= f.kickoff_utc <= adesso
        and (stagione is None or getattr(f, "season", None) == stagione)
    ]


def archivio_alla_vigilia(
    archivio,
    fixtures: Iterable[Any],
    prima_di,
    stagione: int | None,
    giorni: int = GIORNI_FINESTRA,
):
    """Le prestazioni che la macchina delle formazioni vedrebbe poco prima di ``prima_di``.

    Serve a rigiocare il passato (`backtest.py --cerca-fattore`, lavoro 1b)
    con la stessa finestra della macchina vera: `partite_recenti`, stessi
    giorni e stessa stagione. Si parte da un archivio gia' costruito su tutto
    e lo si restringe, invece di rileggere i tabellini dal disco a ogni
    giornata: le prestazioni sono le stesse, e cambia solo il tempo.

    Una differenza c'e', ed e' tutta qui: il taglio e' STRETTO. Dal vivo la
    macchina gira prima del fischio, e la partita da pronosticare non e'
    ancora finita — `partite_recenti` puo' permettersi di includere l'istante
    stesso. Rigiocando il passato ogni partita e' finita, e con quel ``<=`` la
    partita che si sta pronosticando entrerebbe nei suoi stessi pesi: il
    backtest vedrebbe chi ha segnato quel giorno, e premierebbe il metodo per
    aver letto il risultato.
    """
    from datetime import timedelta

    from .players import ArchivioGiocatori

    recenti = partite_recenti(
        fixtures, prima_di - timedelta(microseconds=1), giorni=giorni, stagione=stagione
    )
    ids = {f.api_id for f in recenti}
    return ArchivioGiocatori(
        prestazioni=[p for p in archivio.prestazioni if p.fixture_id in ids]
    )
