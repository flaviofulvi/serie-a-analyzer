"""Stima delle forze di attacco e difesa con il modello di Dixon-Coles.

Parametrizzazione (tutto su scala logaritmica, cosi' i gol attesi restano
positivi per costruzione e l'ottimizzazione non ha vincoli di positivita'):

    log(gol attesi casa)      = base + attacco[casa]  - difesa[ospite] + fattore_campo
    log(gol attesi trasferta) = base + attacco[ospite] - difesa[casa]

dove ``base`` e' il livello medio di gol del campionato. Attacco e difesa sono
centrati a somma zero: valgono come scostamenti dalla media della lega, quindi
attacco = +0.30 significa "segna circa il 35% in piu' della squadra media".

Due accorgimenti separano un modello utilizzabile da uno ingenuo:

* **Decadimento temporale** — ogni partita pesa ``exp(-xi * giorni_fa)``. Una
  partita di dieci mesi fa descrive una squadra che non esiste piu'. Il tempo
  pero' non e' l'unico modo in cui una squadra smette di essere se stessa: se
  ha cambiato allenatore, anche le partite di un mese fa parlano di un'altra
  squadra, e ``_pesi_allenatore`` e' la seconda meta' di questo peso.
* **Shrinkage** — una penalita' ridge tira attacco e difesa verso la media del
  campionato. Senza, dopo tre giornate il modello e' convinto che la neopromossa
  che ha vinto due volte sia da scudetto. Con poche partite vince la media della
  lega, con molte partite vincono i dati.

Un terzo accorgimento sta fuori dalla stima e dentro il pronostico, ed e'
spiegato in ``comprimi_totale``: le forze delle squadre dicono bene chi vince e
male quanti gol si fanno, quindi il TOTALE dei gol attesi si comprime verso la
media di lega mentre il rapporto fra le due squadre resta intatto.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

import numpy as np
from scipy.optimize import minimize

from .markets import MatchProbabilities, derive_markets
from .scoreline import DEFAULT_MAX_GOALS, scoreline_matrix

# Emivita del decadimento temporale: una partita di 6 mesi fa pesa la meta' di
# una di oggi. Valore di partenza ragionevole, da tarare col backtest.
DEFAULT_HALF_LIFE_DAYS = 180.0

# Intensita' dello shrinkage verso la media della lega.
DEFAULT_REGULARIZATION = 2.0

# rho e' negativo nel calcio reale; i limiti tengono la correzione tau valida.
RHO_BOUNDS = (-0.3, 0.1)

# Quanto delle differenze fra squadre resta nei gol TOTALI attesi.
#
# Sapere chi e' piu' forte dice chi vince, non quanti gol si fanno: rigiocando
# quattro stagioni col backtest l'1X2 batte le sue baseline, mentre Over 2.5 e
# Gol/NoGol fanno leggermente peggio di chi dichiara ogni volta la media di
# campionato. Il motivo, letto nella scomposizione del Brier, non e' che il
# modello sia storto: e' che sui gol la sua risoluzione e' quasi zero e la
# correlazione con cio' che accade oscilla intorno allo zero cambiando segno
# da una stagione all'altra. Il modello VARIA le previsioni sui gol, e quelle
# variazioni sono rumore — la baseline non si muove e non paga niente, il
# modello si muove a vuoto e paga ogni movimento.
#
# La cura e' togliere il movimento che non serve, non aggiungerne di nuovo:
# il totale atteso si avvicina alla media di lega, il RAPPORTO fra le due
# squadre resta identico. Con 1.0 non cambia niente; con 0.0 sui gol si
# dichiara sempre la media di campionato, tenendo intatto l'1X2.
#
# Il valore viene dal backtest, scelto lasciando fuori la stagione su cui lo
# si misura (`scripts/backtest.py --cerca-compressione`). Misurato cosi', sulle
# quattro stagioni 2022-2025, il Brier medio si muove di:
#
#     Over 2.5    -0.0031      Gol/NoGol   -0.0038      1X2   +0.0008
#
# I gol guadagnano dieci volte quello che l'1X2 perde, ed e' la condizione
# posta prima di cominciare: il mercato che funziona vale piu' di quello che
# non funziona, e se il conto fosse uscito al contrario questa costante non
# esisterebbe. Sotto lo 0.15 i gol non migliorano piu' e l'1X2 continua a
# perdere, sopra lo 0.35 il guadagno sui gol si spegne: la scelta sta dentro
# un tratto piatto, non su una punta.
#
# Non e' un numero grande e non deve esserlo: col fattore migliore i mercati
# dei gol arrivano a PAREGGIARE con la loro baseline, non a batterla. Toglie
# un danno, non aggiunge una capacita'.
DEFAULT_GOAL_COMPRESSION = 0.2

# Quanto pesa una partita giocata sotto un ALTRO allenatore.
#
# Il decadimento temporale sa una cosa sola: quanto e' vecchia una partita.
# Non sa che a novembre quella squadra e' stata esonerata, quindi le tre
# giornate col mister nuovo e le venti col vecchio entrano nella stima come se
# le avesse giocate la stessa squadra. Qui si dice che non e' cosi': le
# partite di prima del cambio pesano ``fattore`` volte le altre, con 1.0 =
# niente correzione e 0.0 = "la squadra di prima non conta affatto".
#
# **Sta a 1.00 perche' e' stato misurato, non perche' non e' stato provato.**
# Il 25 agosto 2026, con `scripts/backtest.py --cerca-allenatore`, quattro
# stagioni sono state rigiocate una volta per ogni valore della griglia (zero
# richieste, 85 cambi di panchina in archivio). Brier 1X2 medio:
#
#     fattore   0.00     0.25     0.50     0.75     0.90     0.95     1.00
#     1X2     0.6003   0.5985   0.5976   0.5971   0.5969   0.5969   0.5968
#
# La curva e' **monotona** e il minimo cade sul bordo: non c'e' nessun punto
# interno, e vale in tutte e quattro le stagioni separatamente, non solo in
# media. Dimenticare la gestione precedente peggiora i pronostici, sempre.
#
# Perche' — ed e' la parte che vale la pena ricordare. La correzione fa due
# cose in una: toglie una squadra che non esiste piu' e toglie partite vere.
# La seconda si paga sempre; la prima rende solo se il cambio di allenatore
# sposta la forza di una squadra piu' di quanto quelle partite insegnino. Nei
# nostri dati non lo fa. In parte perche' lo shrinkage gia' tira verso la
# media di lega chi ha poche partite recenti — cioe' proprio chi ha appena
# cambiato — e in parte perche' un esonero arriva dopo una serie di risultati
# anormalmente brutti, che rientrano da soli: il modello la corregge gia',
# come ritorno alla media.
#
# Il codice resta, e non e' un ripiego: e' un parametro solo, misurabile in
# venti minuti quando i dati saranno di piu'. Chi lo rialza deve rifare la
# misura, non fidarsi di questa — e deve controllare da dove passano le
# panchine: oggi le leggono il sito (`web/engine.py`) e il backtest, mentre
# `data/ingest.py` stima dalla sola cache e allenatori non ne ha. Finche'
# il fattore e' 1.00 le due strade danno lo stesso identico modello;
# rialzandolo, non piu'.
#
# Cio' che questo numero NON e' e non dev'essere: un bonus al mister nuovo.
# Premiare il rimbalzo conterebbe due volte il ritorno alla media che lo
# shrinkage gia' fa. Qui si dimentica, non si premia.
DEFAULT_FATTORE_ALLENATORE = 1.0


def comprimi_totale(
    lam: float, mu: float, media_lega: float, fattore: float
) -> tuple[float, float]:
    """Avvicina il totale dei gol attesi alla media di lega, a rapporto invariato.

    I gol attesi delle due squadre si leggono come due cose diverse::

        totale   = lam + mu     governa Over/Under e Gol/NoGol
        rapporto = lam / mu     governa chi vince

    Qui si comprime **solo il totale**, moltiplicando entrambi per la stessa
    costante: il rapporto non si muove di un millesimo, quindi l'1X2 resta
    quello che era a meno dell'effetto — piccolo ma non nullo — che il numero
    di gol ha sulla probabilita' di pareggio.

    Perche' qui e non sui mercati: schiacciare direttamente P(Over 2.5)
    sarebbe piu' semplice e romperebbe la regola su cui poggia tutto il
    progetto, cioe' che ogni mercato e' una somma di celle della stessa
    matrice. Over 2.5 non combacerebbe piu' con la somma dei risultati esatti
    che lo compongono, e due numeri sulla stessa pagina si contraddirebbero.
    Agendo su ``lam`` e ``mu`` la matrice resta una matrice, e la coerenza fra
    esiti e' garantita per costruzione come prima.

    Args:
        lam: gol attesi della squadra di casa.
        mu: gol attesi della squadra in trasferta.
        media_lega: gol totali medi del campionato su cui si e' stimato. Zero
            o negativo significa "non la so": in quel caso non si comprime,
            perche' comprimere verso un centro inventato sposterebbe ogni
            pronostico senza che nessuno l'abbia misurato.
        fattore: quanto della distanza dalla media si conserva, da 0 a 1.

    Returns:
        La coppia ``(lam, mu)`` compressa. Il totale finisce sempre fra quello
        di partenza e la media di lega — con un fattore fra 0 e 1 non puo'
        fare altro — quindi resta positivo per costruzione e non serve un
        pavimento a proteggerlo.
    """
    if not 0.0 <= fattore <= 1.0:
        raise ValueError(
            f"la compressione dei gol deve stare fra 0 e 1, ricevuto {fattore!r}"
        )
    if fattore == 1.0 or media_lega <= 0.0:
        return lam, mu
    totale = lam + mu
    if totale <= 0.0:
        return lam, mu
    compresso = media_lega + fattore * (totale - media_lega)
    k = compresso / totale
    return lam * k, mu * k


@dataclass(frozen=True)
class Match:
    """Una partita conclusa, unita' di osservazione del modello.

    Gli allenatori sono gli **id** di chi sedeva in panchina quel giorno, e
    sono facoltativi: una partita di cui non possediamo le formazioni li ha a
    ``None`` e vale come tutte le altre. Non servono a stimare l'allenatore —
    quella forza non e' identificabile con un campionato solo — ma a sapere se
    la squadra di quel giorno e' ancora la squadra di adesso (vedi
    ``_pesi_allenatore``).
    """

    home: str
    away: str
    home_goals: int
    away_goals: int
    played_on: date
    allenatore_casa: int | None = None
    allenatore_ospite: int | None = None


@dataclass(frozen=True)
class TeamEvidence:
    """Su quanta sostanza poggia la stima di una squadra.

    Il modello risponde sempre, anche su una squadra di cui non sa nulla:
    lo shrinkage la tiene incollata alla media della lega e ne esce un
    pronostico prudente, indistinguibile a occhio da uno informato. Questo
    oggetto e' cio' che rende visibile la differenza *prima* della partita,
    non dopo — l'accuratezza misurata arriva mesi piu' tardi e su decine di
    esiti, qui invece si dichiara quanto il modello sappia adesso.

    ``pull`` e' la quota di stima che viene dalla media di lega invece che
    dai dati della squadra; ``sostanza`` e' il suo complemento, cioe' quanto
    parlano davvero le partite giocate.

    Attenzione a cosa **non** dice: e' misurato alla data di riferimento
    della stima (``FittedModel.fitted_on``), che di default e' la partita
    piu' recente del campione e non il giorno di oggi. Un archivio fermo da
    un anno produce un ``pull`` basso — dal suo punto di vista i dati sono
    freschissimi — e quanto sia vecchio quel punto di vista e' un fatto a
    parte, che si legge da ``last_match``. Chi mostra questo numero deve
    mostrare anche l'altro.
    """

    team: str
    n_matches: int
    effective_sample_size: float
    last_match: date
    pull_attack: float
    pull_defense: float

    @property
    def pull(self) -> float:
        """Quanto la forza e' tirata verso la media di lega, da 0 a 1."""
        return (self.pull_attack + self.pull_defense) / 2

    @property
    def sostanza(self) -> float:
        """Quanto la stima viene dai dati della squadra, da 0 a 1."""
        return 1.0 - self.pull


@dataclass(frozen=True)
class FittedModel:
    """Forze stimate, pronte a produrre pronostici."""

    teams: tuple[str, ...]
    attack: dict[str, float]
    defense: dict[str, float]
    home_advantage: float
    base: float
    rho: float
    n_matches: int
    effective_sample_size: float
    fitted_on: date
    converged: bool
    # Iperparametri della stima. Conservarli qui e' cio' che rende una
    # versione del modello riproducibile: senza, un pronostico salvato non
    # sarebbe piu' ricostruibile a distanza di mesi.
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS
    regularization: float = DEFAULT_REGULARIZATION
    # Quanto delle differenze fra squadre resta nei gol totali attesi, e
    # verso quale media si comprime il resto. La media e' stimata dallo
    # stesso campione con cui si stima il modello, non scritta a mano: i gol
    # per partita cambiano da stagione a stagione (2.43-2.61 nelle quattro
    # che abbiamo) e una costante invecchia. Zero significa "non la so", e
    # allora non si comprime niente.
    goal_compression: float = DEFAULT_GOAL_COMPRESSION
    league_total_goals: float = 0.0
    # Quanto hanno pesato le partite giocate sotto un altro allenatore. Non
    # entra nell'impronta della versione (`data/predictions.py`) e non serve:
    # se ha spostato qualcosa, l'ha spostato dentro attacco e difesa, che
    # nell'impronta ci sono gia'.
    fattore_allenatore: float = DEFAULT_FATTORE_ALLENATORE
    # Quanto il modello sa di ogni squadra. Non entra nell'impronta della
    # versione (`data/predictions.py`): descrive la stima, non la definisce,
    # e due modelli con gli stessi parametri restano lo stesso modello.
    evidence: dict[str, TeamEvidence] = field(default_factory=dict)

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        """Gol attesi di casa e trasferta per un accoppiamento.

        E' il collo di bottiglia da cui passa ogni pronostico: la matrice dei
        risultati nasce da qui, e ogni mercato e' una somma di celle di quella
        matrice. Per questo la compressione dei gol totali
        (``comprimi_totale``) sta in questo punto e non a valle sui singoli
        mercati — cosi' Over 2.5 continua a combaciare con la somma dei
        risultati esatti che lo compongono.
        """
        self._check_known(home)
        self._check_known(away)
        lam = math.exp(self.base + self.attack[home] - self.defense[away] + self.home_advantage)
        mu = math.exp(self.base + self.attack[away] - self.defense[home])
        return comprimi_totale(lam, mu, self.league_total_goals, self.goal_compression)

    def scoreline(
        self, home: str, away: str, max_goals: int = DEFAULT_MAX_GOALS
    ) -> np.ndarray:
        """Matrice dei risultati esatti per un accoppiamento."""
        lam, mu = self.expected_goals(home, away)
        return scoreline_matrix(lam, mu, self.rho, max_goals)

    def predict(
        self, home: str, away: str, max_goals: int = DEFAULT_MAX_GOALS
    ) -> MatchProbabilities:
        """Tutti gli esiti derivati per un accoppiamento."""
        return derive_markets(self.scoreline(home, away, max_goals))

    def strength_table(self) -> list[tuple[str, float, float, float]]:
        """Squadre ordinate per forza complessiva.

        Restituisce ``(squadra, attacco, difesa, forza_netta)``. Serve alla
        spiegabilita': accanto a "Inter 62%" si mostra da dove viene quel numero.
        """
        rows = [
            (team, self.attack[team], self.defense[team], self.attack[team] + self.defense[team])
            for team in self.teams
        ]
        return sorted(rows, key=lambda row: -row[3])

    def evidence_for(self, team: str) -> TeamEvidence | None:
        """Su cosa poggia la stima di una squadra, se il modello la conosce."""
        return self.evidence.get(team)

    def evidence_table(self) -> list[TeamEvidence]:
        """Le squadre dalla piu' incerta alla piu' solida.

        L'ordine e' quello: le squadre su cui il modello sa meno sono quelle
        di cui vale la pena parlare, non le altre.
        """
        return sorted(self.evidence.values(), key=lambda e: -e.pull)

    def _check_known(self, team: str) -> None:
        if team not in self.attack:
            raise KeyError(
                f"squadra sconosciuta al modello: {team!r}. "
                f"Squadre disponibili: {', '.join(sorted(self.teams))}"
            )


def _time_weights(
    matches: Sequence[Match], as_of: date, half_life_days: float
) -> np.ndarray:
    """Peso esponenziale decrescente con l'eta' della partita."""
    if half_life_days <= 0:
        raise ValueError("l'emivita deve essere positiva")
    xi = math.log(2.0) / half_life_days
    days_ago = np.array([(as_of - m.played_on).days for m in matches], dtype=float)
    if (days_ago < 0).any():
        raise ValueError("presente una partita successiva alla data di riferimento")
    return np.exp(-xi * days_ago)


def _pesi_allenatore(matches: Sequence[Match], fattore: float) -> np.ndarray:
    """Peso ridotto per le partite giocate sotto un allenatore diverso da quello di adesso.

    "Adesso" e' l'allenatore dell'**ultima partita nota** di quella squadra
    dentro il campione, non quello vero di oggi. La differenza conta solo nel
    backtest, dove il campione si ferma al giorno del taglio: cosi' la
    correzione guarda la panchina di allora e non puo' sapere niente del
    futuro, senza che nessuno debba ricordarsi di dirglielo.

    **Il peso e' della partita, non della squadra**, ed e' il compromesso da
    dichiarare. Nel modello di Dixon-Coles una partita insegna qualcosa su
    tutte e quattro le forze in gioco — l'attacco di chi segna e la difesa di
    chi subisce, da entrambe le parti — e i termini non si separano: i gol
    fatti dalla squadra di casa parlano insieme del suo attacco e della difesa
    avversaria. Non esiste quindi un peso "solo per la squadra che ha cambiato
    allenatore". Da qui la **media** dei due fattori: se una sola delle due
    squadre ha cambiato panchina, meta' di cio' che quella partita insegna
    riguarda una squadra che non esiste piu', e l'altra meta' resta buona. Il
    prodotto dei due fattori sarebbe piu' severo e punirebbe l'avversario
    quanto chi ha cambiato, che e' proprio la cosa da non fare.

    Una partita di cui non conosciamo l'allenatore pesa come prima. E' la
    scelta giusta e non un ripiego: "non lo so" non e' "e' cambiato", e le
    stagioni senza formazioni sul disco devono continuare a comportarsi
    esattamente come si sono sempre comportate.

    **Il traghettatore**, cioe' il punto fragile, che va conosciuto e non
    nascosto. A volte in panchina siede il vice per una partita sola —
    squalifica dell'allenatore, un esonero a meta' settimana — e l'API lo
    registra come allenatore a tutti gli effetti: nell'archivio ci sono
    Martusciello per una giornata alla Lazio e Nicola per una in mezzo a due
    tratti di Giampaolo alla Cremonese. Se una di quelle partite e' l'ULTIMA
    conosciuta, tutta la storia della squadra risulta "di un altro allenatore"
    e viene alleggerita per sbaglio. Non e' rimediabile al momento del
    pronostico — che il titolare torni domenica prossima e' cosa che si sa
    dopo — e non si rimedia con una soglia inventata sulla lunghezza dei
    tratti. Il danno e' comunque limitato: quella squadra pesa meno per una
    giornata e torna com'era, ed e' esattamente il genere di rumore che il
    backtest conta quando sceglie il fattore.
    """
    if not 0.0 <= fattore <= 1.0:
        raise ValueError("il fattore dell'allenatore sta fra 0 e 1")
    if fattore == 1.0:
        return np.ones(len(matches), dtype=float)

    # L'ultimo allenatore conosciuto di ogni squadra.
    corrente: dict[str, tuple[date, int]] = {}
    for m in matches:
        for squadra, allenatore in (
            (m.home, m.allenatore_casa), (m.away, m.allenatore_ospite)
        ):
            if allenatore is None:
                continue
            visto = corrente.get(squadra)
            if visto is None or m.played_on >= visto[0]:
                corrente[squadra] = (m.played_on, allenatore)

    pesi = np.ones(len(matches), dtype=float)
    for i, m in enumerate(matches):
        quote = []
        for squadra, allenatore in (
            (m.home, m.allenatore_casa), (m.away, m.allenatore_ospite)
        ):
            visto = corrente.get(squadra)
            if allenatore is None or visto is None or allenatore == visto[1]:
                quote.append(1.0)
            else:
                quote.append(fattore)
        pesi[i] = sum(quote) / len(quote)
    return pesi


def _log_tau(
    lam: np.ndarray,
    mu: np.ndarray,
    rho: float,
    home_goals: np.ndarray,
    away_goals: np.ndarray,
) -> np.ndarray:
    """Logaritmo della correzione di Dixon-Coles, vettorizzato sulle partite."""
    tau = np.ones_like(lam)
    is_0_0 = (home_goals == 0) & (away_goals == 0)
    is_0_1 = (home_goals == 0) & (away_goals == 1)
    is_1_0 = (home_goals == 1) & (away_goals == 0)
    is_1_1 = (home_goals == 1) & (away_goals == 1)

    tau = np.where(is_0_0, 1.0 - lam * mu * rho, tau)
    tau = np.where(is_0_1, 1.0 + lam * rho, tau)
    tau = np.where(is_1_0, 1.0 + mu * rho, tau)
    tau = np.where(is_1_1, 1.0 - rho, tau)
    return np.log(np.clip(tau, 1e-10, None))


def _team_evidence(
    matches: Sequence[Match],
    weights: np.ndarray,
    lam: np.ndarray,
    mu: np.ndarray,
    regularization: float,
) -> dict[str, TeamEvidence]:
    """Quanto la forza di ogni squadra viene dai suoi dati e quanto dalla media.

    Lo shrinkage non e' una costante uguale per tutti: e' una penalita' fissa
    che compete con l'informazione portata dai dati, e quell'informazione
    cambia da squadra a squadra. Una squadra con tre stagioni recenti alle
    spalle la vince facilmente; una neopromossa, o una di cui l'ultima
    partita nota risale a un anno fa, no — e la sua forza esce quasi identica
    alla media del campionato.

    Il conto e' quello classico della stima penalizzata: attorno all'ottimo,
    verosimiglianza e penalita' si contendono il parametro in proporzione
    alla loro curvatura. Per il parametro di attacco di una squadra la
    curvatura dei dati e' ``somma(peso * gol attesi)`` sulle sue partite —
    piu' partite recenti, e piu' gol in gioco, piu' i dati pesano — mentre
    quella della penalita' e' ``2 * regularization``, sempre la stessa.
    Da qui::

        tiro verso la media = 2*reg / (curvatura dei dati + 2*reg)

    E' un'approssimazione, non un intervallo di confidenza: trascura il
    centraggio a somma zero e la correzione tau, che spostano il numero di
    poco. Serve a dire "di questa squadra il modello sa poco", e per quello
    e' abbondantemente precisa.
    """
    penalita = 2.0 * regularization
    if penalita <= 0:
        penalita = 0.0

    n_partite: dict[str, int] = {}
    peso: dict[str, float] = {}
    ultima: dict[str, date] = {}
    info_att: dict[str, float] = {}
    info_dif: dict[str, float] = {}

    for i, m in enumerate(matches):
        w = float(weights[i])
        gol_casa_attesi = float(lam[i])
        gol_ospite_attesi = float(mu[i])
        for squadra, attesi_suoi, attesi_altrui in (
            (m.home, gol_casa_attesi, gol_ospite_attesi),
            (m.away, gol_ospite_attesi, gol_casa_attesi),
        ):
            n_partite[squadra] = n_partite.get(squadra, 0) + 1
            peso[squadra] = peso.get(squadra, 0.0) + w
            if m.played_on > ultima.get(squadra, date.min):
                ultima[squadra] = m.played_on
            # L'attacco si impara dai gol che la squadra fa, la difesa da
            # quelli che subisce: due conti separati, perche' una squadra
            # puo' avere l'uno solido e l'altra no.
            info_att[squadra] = info_att.get(squadra, 0.0) + w * attesi_suoi
            info_dif[squadra] = info_dif.get(squadra, 0.0) + w * attesi_altrui

    def tiro(informazione: float) -> float:
        totale = informazione + penalita
        return penalita / totale if totale > 0 else 0.0

    return {
        squadra: TeamEvidence(
            team=squadra,
            n_matches=n_partite[squadra],
            effective_sample_size=peso[squadra],
            last_match=ultima[squadra],
            pull_attack=tiro(info_att[squadra]),
            pull_defense=tiro(info_dif[squadra]),
        )
        for squadra in n_partite
    }


def fit_dixon_coles(
    matches: Sequence[Match],
    as_of: date | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    regularization: float = DEFAULT_REGULARIZATION,
    goal_compression: float = DEFAULT_GOAL_COMPRESSION,
    fattore_allenatore: float = DEFAULT_FATTORE_ALLENATORE,
    max_iterations: int = 500,
) -> FittedModel:
    """Stima attacco, difesa, fattore campo e rho per massima verosimiglianza.

    Args:
        matches: partite concluse su cui stimare. Servono almeno un paio di
            giornate complete perche' la stima abbia senso; con meno dati lo
            shrinkage tiene le squadre vicine alla media della lega.
        as_of: data di riferimento per il decadimento temporale. Default: la
            partita piu' recente fra quelle passate.
        half_life_days: dopo quanti giorni una partita pesa la meta'.
        regularization: intensita' dello shrinkage verso la media di lega.
        goal_compression: quanto delle differenze fra squadre resta nei gol
            totali attesi (1 = niente compressione). Non entra nella stima:
            agisce sul pronostico, in ``expected_goals``.
        fattore_allenatore: quanto pesano le partite giocate sotto un altro
            allenatore (1 = niente correzione). Questo invece entra eccome
            nella stima: cambia i pesi, quindi le forze.
        max_iterations: tetto di iterazioni dell'ottimizzatore.

    Returns:
        Il modello stimato, con ``converged`` a indicare se l'ottimizzatore ha
        raggiunto la convergenza.
    """
    if not matches:
        raise ValueError("serve almeno una partita per stimare il modello")

    teams = tuple(sorted({m.home for m in matches} | {m.away for m in matches}))
    index = {team: i for i, team in enumerate(teams)}
    n_teams = len(teams)
    if n_teams < 2:
        raise ValueError("servono almeno due squadre distinte")

    reference = as_of or max(m.played_on for m in matches)
    # Due pesi che si moltiplicano e rispondono a due domande diverse: quanto
    # e' vecchia questa partita, e quanto e' ancora questa squadra.
    pesi_tempo = _time_weights(matches, reference, half_life_days)
    weights = pesi_tempo * _pesi_allenatore(matches, fattore_allenatore)

    home_idx = np.array([index[m.home] for m in matches])
    away_idx = np.array([index[m.away] for m in matches])
    home_goals = np.array([m.home_goals for m in matches], dtype=float)
    away_goals = np.array([m.away_goals for m in matches], dtype=float)

    def unpack(params: np.ndarray):
        attack = params[:n_teams]
        defense = params[n_teams : 2 * n_teams]
        # Il centraggio rende i parametri identificabili: senza, si potrebbe
        # aggiungere una costante a tutti gli attacchi e sottrarla da base
        # ottenendo la stessa verosimiglianza.
        attack = attack - attack.mean()
        defense = defense - defense.mean()
        home_adv, base, rho = params[2 * n_teams], params[2 * n_teams + 1], params[2 * n_teams + 2]
        return attack, defense, home_adv, base, rho

    def negative_log_likelihood(params: np.ndarray) -> float:
        attack, defense, home_adv, base, rho = unpack(params)

        log_lam = base + attack[home_idx] - defense[away_idx] + home_adv
        log_mu = base + attack[away_idx] - defense[home_idx]
        lam = np.exp(log_lam)
        mu = np.exp(log_mu)

        # Log-verosimiglianza Poisson senza i fattoriali: sono costanti rispetto
        # ai parametri e non spostano l'ottimo.
        log_lik = (
            home_goals * log_lam
            - lam
            + away_goals * log_mu
            - mu
            + _log_tau(lam, mu, rho, home_goals, away_goals)
        )
        weighted = float(np.sum(weights * log_lik))
        penalty = regularization * float(np.sum(attack**2) + np.sum(defense**2))
        return -weighted + penalty

    initial = np.concatenate(
        [
            np.zeros(n_teams),  # attacco
            np.zeros(n_teams),  # difesa
            [0.25],  # fattore campo: circa +28% di gol attesi
            [math.log(1.35)],  # base: gol medi per squadra a partita in Serie A
            [-0.10],  # rho
        ]
    )
    bounds = (
        [(-2.0, 2.0)] * n_teams
        + [(-2.0, 2.0)] * n_teams
        + [(-1.0, 1.0), (math.log(0.3), math.log(5.0)), RHO_BOUNDS]
    )

    result = minimize(
        negative_log_likelihood,
        initial,
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": max_iterations},
    )

    attack, defense, home_adv, base, rho = unpack(result.x)

    # I gol attesi con i parametri finali: servono a pesare quanto ogni
    # partita abbia davvero insegnato qualcosa su chi l'ha giocata.
    lam_finale = np.exp(base + attack[home_idx] - defense[away_idx] + home_adv)
    mu_finale = np.exp(base + attack[away_idx] - defense[home_idx])

    # La media di lega verso cui si comprimono i gol totali. E' la media
    # OSSERVATA nel campione, pesata come lo e' la stima: le partite vecchie
    # contano meno anche qui, altrimenti si comprimerebbe verso un campionato
    # che non si gioca piu'. Osservata e non implicata dal modello, perche'
    # con `fattore = 0` il pronostico sui gol deve coincidere con la baseline
    # vera — dire ogni volta la frequenza di campionato — e non con la media
    # che il modello crede.
    #
    # Qui pero' pesano solo gli ANNI, non le panchine: quanti gol si segnano in
    # Serie A non dipende da quante squadre hanno cambiato allenatore, e usare
    # i pesi della stima farebbe dipendere il bersaglio della compressione da
    # un fatto che col numero di gol del campionato non c'entra niente.
    gol_totali = home_goals + away_goals
    peso_totale = float(pesi_tempo.sum())
    media_gol = (
        float(np.sum(pesi_tempo * gol_totali) / peso_totale) if peso_totale > 0 else 0.0
    )

    return FittedModel(
        teams=teams,
        attack={team: float(attack[i]) for team, i in index.items()},
        defense={team: float(defense[i]) for team, i in index.items()},
        home_advantage=float(home_adv),
        base=float(base),
        rho=float(rho),
        n_matches=len(matches),
        effective_sample_size=float(weights.sum()),
        fitted_on=reference,
        converged=bool(result.success),
        half_life_days=half_life_days,
        regularization=regularization,
        goal_compression=goal_compression,
        league_total_goals=media_gol,
        fattore_allenatore=fattore_allenatore,
        evidence=_team_evidence(
            matches, weights, lam_finale, mu_finale, regularization
        ),
    )
