"""Backtest walk-forward: il modello contro una stagione vera, giornata per giornata.

La domanda a cui risponde e' l'unica che conta prima di costruirci sopra
un'applicazione: **questi numeri valgono qualcosa?**

Il metodo e' quello che rende il risultato onesto:

    per ogni giornata M della stagione bersaglio
        stima il modello SOLO sulle partite concluse prima del primo
        calcio d'inizio di M
        pronostica le partite di M
        confronta con quello che e' successo davvero

Il modello non vede mai il futuro, nemmeno di un'ora. E' la differenza fra
"il modello descrive bene il passato" (facile, e inutile) e "il modello
prevede il futuro" (difficile, ed e' il punto).

Non consuma richieste API: legge le partite da dove le legge il sito, cioe'
dalla cache **piu'** gli archivi su disco — la raccolta giornaliera, le
stagioni ricomprate e quelle importate da un file. E' la stessa sorgente del
modello vero, ed e' importante che lo sia: un backtest che rigioca una
stagione diversa da quelle che il modello conosce misura un altro programma.

    python scripts/backtest.py                  # l'ultima stagione completa
    python scripts/backtest.py --stagione 2024  # una in particolare
    python scripts/backtest.py --compressione 0 # sui gol, sempre la media di lega
    python scripts/backtest.py --cerca-compressione   # quale compressione conviene
    python scripts/backtest.py --fattore-allenatore 0   # dimentica la gestione precedente
    python scripts/backtest.py --cerca-allenatore     # quanto pesa il cambio di panchina
    python scripts/backtest.py --emivita 90     # una partita di 3 mesi fa vale la meta'
    python scripts/backtest.py --cerca-emivita        # quanto in fretta invecchia una partita
    python scripts/backtest.py --cerca-shrinkage      # quanto tirare verso la media di lega
    python scripts/backtest.py --marcatori            # quanto vale il pronostico su chi segna
    python scripts/backtest.py --cerca-fattore        # quanto credere alle formazioni ufficiali
    python scripts/backtest.py --cerca-emivita-lega   # quanto ricordano i parametri di lega
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from math import exp, log
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from serie_a_analyzer.config import (
    ConfigError,
    build_client,
    build_importato_stores,
    build_store,
    build_storico_stores,
    load_settings,
)
from serie_a_analyzer.data.dataset import Dataset, load_dataset
from serie_a_analyzer.data.marcatori import StoriaMarcatori
from serie_a_analyzer.data.panchine import Panchine, costruisci_panchine
from serie_a_analyzer.data.parsers import FixtureRecord
from serie_a_analyzer.data.pesi import (
    FATTORE_PREDEFINITO,
    Rapporto,
    aggiusta_gol_attesi,
    archivio_alla_vigilia,
    pesi_squadra,
    rapporto_produzione,
    titolari_da_formazione,
    vale_la_pena,
)
from serie_a_analyzer.data.players import costruisci_archivio, formazioni_partita
from serie_a_analyzer.data.store import StoreUnito, formazione_provvisoria
from serie_a_analyzer.model.dixon_coles import (
    DEFAULT_EMIVITA_LEGA,
    DEFAULT_FATTORE_ALLENATORE,
    DEFAULT_GOAL_COMPRESSION,
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_REGULARIZATION,
    comprimi_totale,
    fit_dixon_coles,
)
from serie_a_analyzer.model.markets import MatchProbabilities, derive_markets, player_scores
from serie_a_analyzer.model.scoreline import scoreline_matrix

# Sotto questa soglia di partite il modello non ha abbastanza storia perche'
# il confronto sia informativo.
MIN_TRAINING_MATCHES = 200

# Quante partite deve avere una stagione per essere considerata finita e quindi
# degna di essere rigiocata per intero. Una Serie A ne ha 380: la soglia sta
# sotto per tollerare qualche partita mancante, ma abbastanza in alto da non
# scambiare per completa la stagione appena cominciata.
PARTITE_STAGIONE_COMPLETA = 300

# I valori di compressione provati da `--cerca-compressione`. Venti passi
# bastano: il fattore migliore oscilla parecchio da una stagione all'altra,
# quindi cercarlo al centesimo sarebbe misurare il rumore.
GRIGLIA_COMPRESSIONE = tuple(round(x / 20, 2) for x in range(21))

# I fattori dell'allenatore provati da `--cerca-allenatore`. Sono pochi e
# larghi apposta: al contrario della compressione, questo fattore entra nella
# STIMA, quindi ogni valore costa una rigiocata intera della stagione — una
# ventina di minuti per un giro completo. Cercarlo al centesimo costerebbe ore
# per misurare rumore: il segnale, se c'e', si vede fra "dimentica tutto" e
# "non dimenticare niente".
GRIGLIA_ALLENATORE = (0.0, 0.25, 0.5, 0.75, 1.0)

# Le emivite provate da `--cerca-emivita`, in giorni. Come il fattore
# dell'allenatore questo numero entra nella STIMA — anzi ne e' il cuore, perche'
# decide quanto conta il passato — quindi ogni valore costa una rigiocata
# intera. La griglia e' larga apposta e copre due domande diverse: fra 60 e 365
# giorni c'e' il compromesso vero (memoria corta contro campione grande), e i
# 1825 giorni in fondo sono il "non dimenticare niente", cioe' il confronto con
# e senza decadimento a un parametro solo.
GRIGLIA_EMIVITA = (60.0, 120.0, 180.0, 270.0, 365.0, 730.0, 1825.0)

# Le intensita' di shrinkage provate da `--cerca-shrinkage`. Lo zero c'e' per
# la stessa ragione dei 1825 giorni qui sopra: e' il modello senza shrinkage,
# cioe' quello che crede a ogni squadra sulla parola. Sopra il 2 di oggi si
# sale a passi larghi perche' la penalita' e' quadratica: il salto da 4 a 8 vale
# quanto quello da 0 a 4.
GRIGLIA_SHRINKAGE = (0.0, 0.5, 1.0, 2.0, 4.0, 8.0)

# Le emivite dei parametri di lega provate da `--cerca-emivita-lega` (lavoro
# 6). I 180 giorni in testa sono l'emivita delle squadre, cioe' il modello di
# oggi in un passo solo: e' la colonna di confronto. I 1825 in fondo sono il
# "non dimenticare niente" dei parametri di lega su quattro stagioni.
GRIGLIA_EMIVITA_LEGA = (180.0, 365.0, 730.0, 1095.0, 1825.0)


@dataclass
class Prediction:
    """Un pronostico e cio' che e' realmente accaduto.

    Conserva anche gli ingredienti grezzi del pronostico — i gol attesi
    **prima** della compressione, il rho e la media di lega di quel momento —
    e non solo le probabilita' che ne sono uscite. E' cio' che permette di
    provare venti compressioni diverse rigiocando la stagione **una volta
    sola**: la parte cara e' la stima, che non dipende dal fattore.
    """

    matchday: int
    home: str
    away: str
    probs: MatchProbabilities
    home_goals: int
    away_goals: int
    # Gol attesi non compressi, piu' quello che serve a ricostruire la matrice.
    lam: float = 0.0
    mu: float = 0.0
    rho: float = 0.0
    media_lega: float = 0.0
    # Serve a ritrovare la partita — e il suo tabellino — a chi misura altro
    # sulla stessa rigiocata, come i marcatori.
    fixture_id: int | None = None

    def con_compressione(self, fattore: float) -> "Prediction":
        """Lo stesso pronostico, con un'altra compressione dei gol totali."""
        lam, mu = comprimi_totale(self.lam, self.mu, self.media_lega, fattore)
        return replace(self, probs=derive_markets(scoreline_matrix(lam, mu, self.rho)))

    @property
    def outcome(self) -> str:
        """L'esito reale: '1', 'X' o '2'."""
        if self.home_goals > self.away_goals:
            return "1"
        if self.home_goals < self.away_goals:
            return "2"
        return "X"

    @property
    def predicted(self) -> str:
        """L'esito che il modello riteneva piu' probabile."""
        trio = {"1": self.probs.home_win, "X": self.probs.draw, "2": self.probs.away_win}
        return max(trio, key=trio.get)

    @property
    def prob_of_truth(self) -> float:
        """Probabilita' che il modello aveva assegnato a cio' che e' successo."""
        return {"1": self.probs.home_win, "X": self.probs.draw, "2": self.probs.away_win}[
            self.outcome
        ]

    @property
    def total_goals(self) -> int:
        return self.home_goals + self.away_goals

    @property
    def both_scored(self) -> bool:
        return self.home_goals > 0 and self.away_goals > 0


# ---- metriche --------------------------------------------------------------


def brier_1x2(preds: list[Prediction]) -> float:
    """Brier multiclasse sull'1X2: media di somma((p - reale)^2) sui tre esiti.

    Va da 0 (perfetto) a 2 (perfettamente sbagliato). Tirare a caso con 1/3
    ciascuno vale 0.667: e' la soglia sotto cui bisogna stare per aver detto
    qualcosa.
    """
    totale = 0.0
    for p in preds:
        reale = p.outcome
        for esito, prob in (("1", p.probs.home_win), ("X", p.probs.draw), ("2", p.probs.away_win)):
            totale += (prob - (1.0 if esito == reale else 0.0)) ** 2
    return totale / len(preds)


def brier_binary(probs: list[float], esiti: list[bool]) -> float:
    """Brier su un esito binario. Va da 0 a 1."""
    return sum((p - (1.0 if e else 0.0)) ** 2 for p, e in zip(probs, esiti)) / len(probs)


def log_loss_binary(probs: list[float], esiti: list[bool]) -> float:
    """Log loss su un esito binario. Le probabilita' si tengono lontane da 0 e 1."""
    totale = 0.0
    for p, e in zip(probs, esiti):
        p = min(max(p, 1e-4), 1 - 1e-4)
        totale -= log(p) if e else log(1 - p)
    return totale / len(probs)


def log_loss_1x2(preds: list[Prediction]) -> float:
    """Log loss: punisce molto piu' del Brier la sicurezza mal riposta.

    Serve proprio a questo: un modello che dice 95% e sbaglia deve pagare
    tanto, altrimenti conviene sempre esagerare la fiducia.
    """
    return -sum(log(max(p.prob_of_truth, 1e-12)) for p in preds) / len(preds)


def calibration(probs: list[float], esiti: list[bool], n_bin: int = 5):
    """Curva di calibrazione: quando dico 30%, succede il 30% delle volte?

    Restituisce ``(etichetta, n, media_predetta, frequenza_reale)`` per bin.
    E' la metrica che conta davvero per un'applicazione di pronostici: un
    modello puo' avere un buon Brier ed essere sistematicamente troppo sicuro.
    """
    bins = defaultdict(list)
    for p, e in zip(probs, esiti):
        idx = min(int(p * n_bin), n_bin - 1)
        bins[idx].append((p, e))

    righe = []
    for idx in sorted(bins):
        coppie = bins[idx]
        lo, hi = idx / n_bin, (idx + 1) / n_bin
        media = sum(p for p, _ in coppie) / len(coppie)
        reale = sum(1 for _, e in coppie if e) / len(coppie)
        righe.append((f"{lo:.0%}-{hi:.0%}", len(coppie), media, reale))
    return righe


# ---- backtest --------------------------------------------------------------


def stagioni_disponibili(dataset: Dataset) -> dict[int, int]:
    """Quante partite concluse e numerate abbiamo, per stagione."""
    return dict(sorted(Counter(
        f.season for f in dataset.finished if f.matchday is not None
    ).items()))


def stagione_predefinita(dataset: Dataset) -> int | None:
    """L'ultima stagione completa: la piu' recente che si possa rigiocare tutta.

    Non e' semplicemente la piu' recente. Quella in corso ha poche giornate
    giocate, e un backtest su tre giornate direbbe molto poco e sembrerebbe
    dire molto.
    """
    per_stagione = stagioni_disponibili(dataset)
    complete = [s for s, n in per_stagione.items() if n >= PARTITE_STAGIONE_COMPLETA]
    return max(complete) if complete else None


def run_backtest(
    dataset: Dataset,
    target: int,
    compressione: float = DEFAULT_GOAL_COMPRESSION,
    panchine: Panchine | None = None,
    fattore_allenatore: float = DEFAULT_FATTORE_ALLENATORE,
    emivita: float = DEFAULT_HALF_LIFE_DAYS,
    shrinkage: float = DEFAULT_REGULARIZATION,
    emivita_lega: float | None = DEFAULT_EMIVITA_LEGA,
) -> tuple[list[Prediction], dict]:
    """Rigioca la stagione ``target`` giornata per giornata. Zero richieste API.

    Lo storico su cui il modello si stima non e' solo quello della stagione
    bersaglio: e' **tutto** cio' che il dataset conosce e che era gia' accaduto
    al calcio d'inizio di quella giornata. Comprese le stagioni successive?
    No: il filtro e' sull'ora, non sulla stagione, quindi una partita del 2026
    non puo' entrare nella stima di una del 2025. E' il taglio che rende il
    backtest onesto, e vale anche all'indietro.

    Lo stesso taglio protegge da solo il fattore dell'allenatore: "chi allena
    adesso" e' l'allenatore dell'ultima partita **dentro il campione**, quindi
    alla decima giornata e' quello di allora e non quello di fine stagione.
    Nessuna regola in piu' da ricordarsi.
    """
    nomi = dataset.team_names

    per_giornata: dict[int, list[FixtureRecord]] = defaultdict(list)
    for f in dataset.finished:
        if f.season == target and f.matchday is not None:
            per_giornata[f.matchday].append(f)

    if not per_giornata:
        disponibili = stagioni_disponibili(dataset)
        raise RuntimeError(
            f"stagione {target} assente dai dati. Stagioni che si possono "
            f"rigiocare: {', '.join(f'{s} ({n} partite)' for s, n in disponibili.items()) or 'nessuna'}"
        )

    concluse = sorted(dataset.finished, key=lambda f: f.kickoff_utc)

    predizioni: list[Prediction] = []
    saltate = 0
    giornate_usate = 0

    for giornata in sorted(per_giornata):
        partite = sorted(per_giornata[giornata], key=lambda f: f.kickoff_utc)
        taglio = partite[0].kickoff_utc

        # Il taglio e' sul calcio d'inizio, non sulla data: una partita del
        # sabato sera non deve entrare nella stima di una del sabato pomeriggio.
        storico = [f for f in concluse if f.kickoff_utc < taglio]
        if len(storico) < MIN_TRAINING_MATCHES:
            saltate += len(partite)
            continue

        # La stima non conosce la compressione — non la usa e non ne dipende:
        # il fattore agisce sul pronostico. Chiedendo qui `goal_compression=1`
        # si ottengono i gol attesi grezzi, da cui qualunque fattore si ricava
        # dopo senza ristimare niente.
        modello = fit_dixon_coles(
            [
                _to_match(f, nomi, panchine)
                for f in storico
            ],
            as_of=taglio.date(),
            goal_compression=1.0,
            fattore_allenatore=fattore_allenatore,
            half_life_days=emivita,
            regularization=shrinkage,
            emivita_lega=emivita_lega,
        )
        giornate_usate += 1

        for f in partite:
            casa = nomi.get(f.home_team_id, str(f.home_team_id))
            ospite = nomi.get(f.away_team_id, str(f.away_team_id))
            try:
                lam, mu = modello.expected_goals(casa, ospite)
            except KeyError:
                # Neopromossa che non ha ancora giocato: nessuna storia da cui
                # stimarla. Escluderla e' piu' onesto che inventarle una forza.
                saltate += 1
                continue
            grezzo = Prediction(
                matchday=giornata,
                home=casa,
                away=ospite,
                probs=derive_markets(scoreline_matrix(lam, mu, modello.rho)),
                home_goals=f.home_goals,
                away_goals=f.away_goals,
                lam=lam,
                mu=mu,
                rho=modello.rho,
                media_lega=modello.league_total_goals,
                fixture_id=f.api_id,
            )
            predizioni.append(
                grezzo if compressione == 1.0 else grezzo.con_compressione(compressione)
            )

    return predizioni, {
        "saltate": saltate,
        "giornate": giornate_usate,
        "richieste": dataset.requests_spent,
    }


def _to_match(f: FixtureRecord, nomi: dict[int, str], panchine: Panchine | None = None):
    from serie_a_analyzer.model.dixon_coles import Match

    return Match(
        home=nomi.get(f.home_team_id, str(f.home_team_id)),
        away=nomi.get(f.away_team_id, str(f.away_team_id)),
        home_goals=f.home_goals,
        away_goals=f.away_goals,
        played_on=f.played_on,
        allenatore_casa=(
            panchine.allenatore(f.api_id, f.home_team_id) if panchine else None
        ),
        allenatore_ospite=(
            panchine.allenatore(f.api_id, f.away_team_id) if panchine else None
        ),
    )


def carica_panchine(settings, store, dataset: Dataset) -> Panchine:
    """Chi allenava chi, letto dagli stessi archivi che legge il sito.

    Zero richieste: le formazioni sono buste gia' pagate. Le stagioni
    importate da un file non entrano — non hanno formazioni e non le avranno —
    e sulle loro partite il fattore dell'allenatore semplicemente non agisce.
    """
    storici = list(build_storico_stores(settings).values())
    return costruisci_panchine(StoreUnito(stores=[store, *storici]), dataset.fixtures)


# ---- baseline --------------------------------------------------------------


def frequenze_storiche(preds: list[Prediction]) -> tuple[float, float, float]:
    """Frequenze reali di 1, X, 2 nel campione."""
    n = len(preds)
    conteggi = defaultdict(int)
    for p in preds:
        conteggi[p.outcome] += 1
    return conteggi["1"] / n, conteggi["X"] / n, conteggi["2"] / n


def brier_costante(preds: list[Prediction], trio: tuple[float, float, float]) -> float:
    """Brier di chi dice sempre le stesse tre probabilita', qualunque partita sia.

    E' il concorrente da battere: se il modello non fa meglio di questo, tutto
    il lavoro sulle forze delle squadre non sta aggiungendo informazione.
    """
    ph, pd, pa = trio
    totale = 0.0
    for p in preds:
        reale = p.outcome
        for esito, prob in (("1", ph), ("X", pd), ("2", pa)):
            totale += (prob - (1.0 if esito == reale else 0.0)) ** 2
    return totale / len(preds)


# ---- calibrazione della compressione ---------------------------------------


def brier_dei_tre(preds: list[Prediction]) -> tuple[float, float, float]:
    """Brier di 1X2, Over 2.5 e Gol/NoGol sullo stesso campione."""
    return (
        brier_1x2(preds),
        brier_binary([p.probs.over[2.5] for p in preds], [p.total_goals >= 3 for p in preds]),
        brier_binary([p.probs.btts for p in preds], [p.both_scored for p in preds]),
    )


def scansione(
    preds: list[Prediction], griglia: Sequence[float] = GRIGLIA_COMPRESSIONE
) -> dict[float, tuple[float, float, float]]:
    """Per ogni compressione della griglia, i Brier dei tre mercati.

    Non ristima niente: i pronostici arrivano gia' rigiocati, e cambiare il
    fattore vuol dire solo rifare la matrice dei risultati.
    """
    return {f: brier_dei_tre([p.con_compressione(f) for p in preds]) for f in griglia}


def _somma_gol(riga: tuple[float, float, float]) -> float:
    """Il criterio da minimizzare: i due mercati dei gol, insieme.

    L'1X2 non entra nel criterio — e' il mercato che gia' funziona, e non si
    tocca per migliorarne un altro — ma va guardato accanto al risultato: se
    la compressione lo peggiorasse piu' di quanto migliora i gol, il lavoro
    andrebbe buttato.
    """
    return riga[1] + riga[2]


def migliore(scansioni: Sequence[dict[float, tuple[float, float, float]]]) -> float:
    """La compressione che minimizza i mercati dei gol su piu' stagioni insieme."""
    griglia = sorted(scansioni[0])
    def costo(f: float) -> float:
        return sum(_somma_gol(s[f]) for s in scansioni) / len(scansioni)
    return min(griglia, key=costo)


def cerca_compressione(dataset: Dataset, stagioni: Sequence[int]) -> int:
    """Sceglie il fattore di compressione, e lo misura senza barare.

    Il fattore non si sceglie sulla stagione su cui lo si giudica: si sceglie
    sulle ALTRE, e si guarda cosa fa su quella lasciata fuori. Fatto in altro
    modo, si misura la propria capacita' di indovinare il passato — che e'
    perfetta per costruzione e non serve a niente.
    """
    titolo("COMPRESSIONE DEI GOL TOTALI - quale fattore, e quanto vale davvero")
    print("Le forze delle squadre dicono bene CHI VINCE e male QUANTI GOL.")
    print("Comprimere avvicina il totale atteso alla media di lega lasciando")
    print("intatto il rapporto fra le due squadre: 1.00 = niente compressione,")
    print("0.00 = sui gol si dichiara sempre la media di campionato.\n")

    scansioni: dict[int, dict[float, tuple[float, float, float]]] = {}
    for stagione in stagioni:
        preds, _ = run_backtest(dataset, stagione, compressione=1.0)
        if not preds:
            continue
        scansioni[stagione] = scansione(preds)
        media = preds[0].media_lega
        print(f"  stagione {stagione}: {len(preds)} partite rigiocate "
              f"(media di lega finale {media:.2f} gol)")

    if len(scansioni) < 2:
        print("\nServono almeno due stagioni complete per scegliere onestamente.\n")
        return 1

    print(f"\n{'stagione':<10}{'miglior f':>11}{'Over 2.5':>22}{'Gol/NoGol':>22}")
    print(f"{'':<10}{'(su di se)':>11}{'oggi -> con f':>22}{'oggi -> con f':>22}")
    print("-" * 66)
    for stagione, s in scansioni.items():
        f = min(sorted(s), key=lambda x: _somma_gol(s[x]))
        print(f"{stagione:<10}{f:>11.2f}"
              f"{f'{s[1.0][1]:.4f} -> {s[f][1]:.4f}':>22}"
              f"{f'{s[1.0][2]:.4f} -> {s[f][2]:.4f}':>22}")
    print("\nQuesta tabella e' solo per vedere quanto oscilla il fattore migliore:")
    print("scelto sulla stessa stagione su cui si misura, e' ottimismo puro.")

    titolo("La misura onesta: fattore scelto sulle altre stagioni")
    print(f"{'stagione':<10}{'f dalle altre':>15}{'1X2':>16}{'Over 2.5':>16}{'Gol/NoGol':>16}")
    print("-" * 74)
    deltas = [0.0, 0.0, 0.0]
    for stagione, s in scansioni.items():
        altre = [x for y, x in scansioni.items() if y != stagione]
        f = migliore(altre)
        base, con = s[1.0], s[f]
        for i in range(3):
            deltas[i] += con[i] - base[i]
        print(f"{stagione:<10}{f:>15.2f}"
              f"{f'{base[0]:.4f}->{con[0]:.4f}':>16}"
              f"{f'{base[1]:.4f}->{con[1]:.4f}':>16}"
              f"{f'{base[2]:.4f}->{con[2]:.4f}':>16}")
    n = len(scansioni)
    print("-" * 74)
    print(f"{'variazione media':<25}"
          f"{deltas[0] / n:>+16.4f}{deltas[1] / n:>+16.4f}{deltas[2] / n:>+16.4f}")
    print("\nNegativo = migliora. La colonna 1X2 e' la guardia: se peggiorasse")
    print("piu' di quanto migliorano i gol, la compressione andrebbe buttata.")

    titolo("Il quadro intero: ogni fattore, in media sulle stagioni")
    print("Serve a vedere la forma del compromesso, non a scegliere: qui il")
    print("fattore e' misurato sulle stesse stagioni su cui e' scelto.\n")
    print(f"{'fattore':<10}{'1X2':>12}{'Over 2.5':>12}{'Gol/NoGol':>12}{'gol, somma':>14}")
    print("-" * 60)
    quante = len(scansioni)
    for f in sorted(scansioni[next(iter(scansioni))]):
        medie = [
            sum(s[f][i] for s in scansioni.values()) / quante for i in range(3)
        ]
        print(f"{f:<10.2f}{medie[0]:>12.4f}{medie[1]:>12.4f}{medie[2]:>12.4f}"
              f"{medie[1] + medie[2]:>14.4f}")
    print("\nL'1X2 peggiora piano scendendo, i gol migliorano in fretta e poi")
    print("si fermano: il fattore giusto sta dove le due curve si incrociano.")

    f_finale = migliore(list(scansioni.values()))
    titolo(f"IL FATTORE DA USARE: {f_finale:.2f}")
    print("Scelto su tutte le stagioni insieme. E' il valore da mettere in")
    print("DEFAULT_GOAL_COMPRESSION (model/dixon_coles.py).")
    print("\nCosa fa, stagione per stagione, rispetto a oggi:")
    print(f"\n{'stagione':<10}{'1X2':>16}{'Over 2.5':>16}{'Gol/NoGol':>16}")
    print("-" * 58)
    for stagione, s in scansioni.items():
        base, con = s[1.0], s[f_finale]
        print(f"{stagione:<10}"
              f"{f'{base[0]:.4f}->{con[0]:.4f}':>16}"
              f"{f'{base[1]:.4f}->{con[1]:.4f}':>16}"
              f"{f'{base[2]:.4f}->{con[2]:.4f}':>16}")
    print("\nAttenzione a cosa NON dice questo numero: col fattore migliore i")
    print("mercati dei gol arrivano a PAREGGIARE con la loro baseline, non a")
    print("batterla. Toglie un danno, non aggiunge una capacita'.")
    return 0


# ---- i parametri che entrano nella stima -------------------------------------
#
# Tre parametri, una macchina sola. La compressione dei gol sta a parte perche'
# agisce **dopo** la stima e si prova a posteriori su una rigiocata sola; il
# fattore dell'allenatore, l'emivita e lo shrinkage hanno invece tutti la stessa
# forma:
#
#   * entrano nella STIMA, quindi ogni valore vuole la sua rigiocata di ogni
#     stagione — una ventina di minuti per un giro completo;
#   * si giudicano sull'**1X2**, perche' parlano tutti di *quanto vale una
#     squadra*, che e' esattamente cio' che l'1X2 misura e l'unico mercato in
#     cui il modello sa davvero qualcosa. I mercati dei gol si guardano come
#     guardia — non devono peggiorare — ma non scelgono;
#   * il valore si sceglie sulle stagioni DIVERSE da quella su cui lo si
#     misura. Scelto sulla stessa, misurerebbe la propria capacita' di
#     indovinare il passato, che e' perfetta per costruzione e non serve a
#     niente.
#
# Da qui una funzione sola, `cerca_nella_stima`, e tre descrizioni di cosa si
# sta cercando. Tre copie dello stesso rapporto divergerebbero al primo
# ritocco, e sarebbero tre metodi diversi con l'aria di essere lo stesso.


@dataclass(frozen=True)
class Ricerca:
    """Cosa si cerca, su quale griglia, e come raccontarlo."""

    parametro: str                  # il nome dell'argomento di `run_backtest`
    griglia: tuple[float, ...]
    predefinito: float
    titolo: str
    intro: tuple[str, ...]
    costante: str                   # dove va scritto il valore, se cambia
    unita: str = ""                 # per il verdetto: "giorni", oppure niente
    formato: str = "{:.2f}"
    # Cosa aggiungere al verdetto quando il valore migliore e' quello di oggi.
    # Serve al fattore dell'allenatore, dove "il valore di oggi" vuol dire
    # "nessuna correzione", che e' una notizia e non una conferma qualunque.
    nota_predefinito: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Il valore di oggi deve stare nella griglia: e' la colonna contro cui
        # tutto il rapporto si confronta. Chi cambiasse una costante senza
        # toccare la griglia otterrebbe un KeyError a meta' misura, cioe' dopo
        # venti minuti di rigiocate.
        if self.predefinito not in self.griglia:
            raise ValueError(
                f"{self.costante} vale {self.predefinito}, che non e' nella "
                f"griglia {self.griglia}: senza, non c'e' niente con cui "
                "confrontare i valori provati"
            )

    def mostra(self, valore: float) -> str:
        return self.formato.format(valore)


def migliore_su_1x2(
    scansioni: Sequence[dict[float, tuple[float, float, float]]]
) -> float:
    """Il valore che minimizza il Brier 1X2 su piu' stagioni insieme.

    Il criterio e' l'1X2 e non i gol, all'opposto della compressione: questi
    parametri parlano di **quanto vale una squadra**, che e' esattamente cio'
    che l'1X2 misura e cio' che il modello sa fare.
    """
    griglia = sorted(scansioni[0])

    def costo(valore: float) -> float:
        return sum(s[valore][0] for s in scansioni) / len(scansioni)

    return min(griglia, key=costo)


def scansione_nella_stima(
    dataset: Dataset,
    stagioni: Sequence[int],
    ricerca: Ricerca,
    panchine: Panchine | None = None,
) -> dict[int, dict[float, tuple[float, float, float]]]:
    """La parte cara: ogni stagione rigiocata una volta per ogni valore.

    Le panchine si passano sempre, anche quando il parametro cercato non e' il
    loro: se un domani `DEFAULT_FATTORE_ALLENATORE` scendesse sotto 1, una
    ricerca fatta senza di loro misurerebbe un modello diverso da quello che
    gira davvero, e nessuno se ne accorgerebbe.
    """
    scansioni: dict[int, dict[float, tuple[float, float, float]]] = {}
    for stagione in stagioni:
        riga: dict[float, tuple[float, float, float]] = {}
        for valore in ricerca.griglia:
            preds, _ = run_backtest(
                dataset, stagione, panchine=panchine, **{ricerca.parametro: valore}
            )
            if not preds:
                break
            riga[valore] = brier_dei_tre(preds)
        if riga:
            scansioni[stagione] = riga
            print(f"  stagione {stagione}: rigiocata {len(riga)} volte, "
                  f"una per valore")
    return scansioni


def cerca_nella_stima(
    dataset: Dataset,
    stagioni: Sequence[int],
    ricerca: Ricerca,
    panchine: Panchine | None = None,
) -> int:
    """Cerca un parametro della stima col metodo del progetto, e lo racconta."""
    titolo(ricerca.titolo)
    for riga in ricerca.intro:
        print(riga)
    print()

    scansioni = scansione_nella_stima(dataset, stagioni, ricerca, panchine)
    if len(scansioni) < 2:
        print("\nServono almeno due stagioni complete per scegliere onestamente.\n")
        return 1

    oggi = ricerca.mostra(ricerca.predefinito)
    titolo("La misura onesta: valore scelto sulle altre stagioni")
    print(f"{'stagione':<10}{'dalle altre':>15}{'1X2':>18}{'Over 2.5':>18}{'Gol/NoGol':>18}")
    print("-" * 79)
    deltas = [0.0, 0.0, 0.0]
    for stagione, s in scansioni.items():
        altre = [x for y, x in scansioni.items() if y != stagione]
        valore = migliore_su_1x2(altre)
        base, con = s[ricerca.predefinito], s[valore]
        for i in range(3):
            deltas[i] += con[i] - base[i]
        print(f"{stagione:<10}{ricerca.mostra(valore):>15}"
              f"{f'{base[0]:.4f}->{con[0]:.4f}':>18}"
              f"{f'{base[1]:.4f}->{con[1]:.4f}':>18}"
              f"{f'{base[2]:.4f}->{con[2]:.4f}':>18}")
    n = len(scansioni)
    print("-" * 79)
    print(f"{'variazione media':<25}"
          f"{deltas[0] / n:>+18.4f}{deltas[1] / n:>+18.4f}{deltas[2] / n:>+18.4f}")
    print(f"\nIl confronto e' sempre contro il valore di oggi "
          f"({oggi} {ricerca.unita})".rstrip(") ") + ").")
    print("Negativo = migliora. La colonna che decide e' l'1X2: e' il mercato")
    print("che parla di quanto vale una squadra. Le altre due sono la guardia:")
    print("non devono peggiorare, ma non scelgono.")

    titolo("Il quadro intero: ogni valore, in media sulle stagioni")
    print("Serve a vedere la forma della curva, non a scegliere: qui il valore")
    print("e' misurato sulle stesse stagioni su cui e' scelto.\n")
    print(f"{'valore':<12}{'1X2':>12}{'Over 2.5':>12}{'Gol/NoGol':>12}")
    print("-" * 48)
    quante = len(scansioni)
    for valore in ricerca.griglia:
        medie = [
            sum(s[valore][i] for s in scansioni.values()) / quante for i in range(3)
        ]
        print(f"{ricerca.mostra(valore):<12}"
              f"{medie[0]:>12.4f}{medie[1]:>12.4f}{medie[2]:>12.4f}")

    print(f"\n{'stagione':<10}"
          + "".join(f"{ricerca.mostra(v):>12}" for v in ricerca.griglia))
    print("-" * (10 + 12 * len(ricerca.griglia)))
    for stagione, s in scansioni.items():
        print(f"{stagione:<10}"
              + "".join(f"{s[v][0]:>12.4f}" for v in ricerca.griglia))
    print("(Brier 1X2 per stagione e per valore: se il minimo cade ogni volta")
    print(" in un punto diverso, il valore vero non e' determinato dai dati.)")

    # Il verdetto obbedisce alla MISURA ONESTA, non all'ottimo. Sono due cose
    # diverse e possono divergere: `migliore_su_1x2` guarda tutte le stagioni
    # insieme, quindi sceglie il valore avendo gia' visto le risposte, mentre
    # `guadagno` e' quel che si ottiene scegliendo ogni volta sulle **altre**
    # — cioe' l'unica delle due situazioni in cui ci si trovera' davvero.
    # Un ottimo che sparisce appena lo si sceglie senza guardare la risposta
    # non e' un ottimo: e' rumore, e proporlo lo stesso sarebbe il modo piu'
    # elegante di peggiorare il modello credendo di calibrarlo.
    scelto = migliore_su_1x2(list(scansioni.values()))
    guadagno = deltas[0] / n
    titolo(f"IL VALORE DA USARE: {ricerca.mostra(scelto)} {ricerca.unita}".rstrip()
           if scelto == ricerca.predefinito or guadagno < 0
           else f"IL VALORE DA USARE: {oggi} {ricerca.unita}".rstrip()
                + " — cioe' quello di oggi")
    if scelto == ricerca.predefinito:
        print("E' quello di oggi: la misura conferma il valore in uso, e in")
        print(f"{ricerca.costante} non c'e' niente da cambiare.")
        for riga in ricerca.nota_predefinito:
            print(riga)
    elif guadagno >= 0:
        print(f"Sulle stagioni messe INSIEME il valore migliore sarebbe "
              f"{ricerca.mostra(scelto)}, ma")
        print(f"scegliendolo sulle altre stagioni il Brier 1X2 peggiora di "
              f"{guadagno:+.4f}:")
        print("l'ottimo sparisce appena lo si sceglie senza aver visto la")
        print("risposta, e questo si chiama rumore.")
        print(f"In {ricerca.costante} non si tocca niente.")
    else:
        print(f"E' il valore da mettere in {ricerca.costante}")
        print("(model/dixon_coles.py), e la misura onesta lo conferma: scelto")
        print(f"sulle altre stagioni guadagna {guadagno:+.4f} di Brier 1X2.")
        print("Guarda pero' anche la tabella per stagione qui sopra: se il")
        print("minimo salta da un valore all'altro, il guadagno medio e' un")
        print("caso e non un segnale.")
    return 0


RICERCA_ALLENATORE = Ricerca(
    parametro="fattore_allenatore",
    griglia=GRIGLIA_ALLENATORE,
    predefinito=1.0,
    costante="DEFAULT_FATTORE_ALLENATORE",
    titolo="L'ALLENATORE - quanto pesa la squadra di prima del cambio",
    intro=(
        "Il modello pesa le partite solo per l'eta'. Una squadra che ha",
        "cambiato allenatore tre giornate fa si porta pero' dietro il girone",
        "precedente a peso pieno, come se fosse la stessa squadra.",
        "1.00 = niente correzione (come oggi), 0.00 = la gestione precedente",
        "non conta affatto.",
    ),
    nota_predefinito=(
        "1.00 vuol dire NIENTE CORREZIONE: su questi dati dimenticare la",
        "gestione precedente non migliora i pronostici. E' un risultato e non",
        "un fallimento — il modello dice che quel peso non c'era.",
    ),
)

RICERCA_EMIVITA = Ricerca(
    parametro="emivita",
    griglia=GRIGLIA_EMIVITA,
    predefinito=DEFAULT_HALF_LIFE_DAYS,
    costante="DEFAULT_HALF_LIFE_DAYS",
    unita="giorni",
    formato="{:.0f}",
    titolo="L'EMIVITA - dopo quanti giorni una partita vale la meta'",
    intro=(
        "E' il cuore della stima: decide quanto il modello ricorda. Corta, il",
        "modello segue la forma del momento e ha pochissime partite su cui",
        "poggiare; lunga, ha un campione grande fatto pero' anche di squadre",
        "che non esistono piu'.",
        "I 1825 giorni in fondo alla griglia sono il caso limite 'non",
        "dimenticare niente': con quattro stagioni in archivio equivalgono a",
        "spegnere il decadimento.",
        "Attenzione: questo parametro e lo shrinkage si parlano. Un'emivita",
        "piu' lunga vuol dire piu' campione efficace, quindi meno bisogno di",
        "essere tirati verso la media di lega. Qui se ne muove uno alla volta,",
        "e va detto.",
    ),
)

RICERCA_SHRINKAGE = Ricerca(
    parametro="shrinkage",
    griglia=GRIGLIA_SHRINKAGE,
    predefinito=DEFAULT_REGULARIZATION,
    costante="DEFAULT_REGULARIZATION",
    formato="{:.1f}",
    titolo="LO SHRINKAGE - quanto tirare le squadre verso la media di lega",
    intro=(
        "E' la prudenza del modello. Alto, ogni squadra somiglia alla media",
        "del campionato e le forze si schiacciano; basso, il modello crede a",
        "ogni squadra sulla parola, comprese quelle che hanno giocato tre",
        "partite.",
        "Lo 0.0 in cima alla griglia e' il modello senza nessuno shrinkage,",
        "cioe' il confronto 'con e senza' a un parametro solo.",
        "Vale anche qui l'avvertenza dell'emivita: i due parametri si parlano,",
        "e qui se ne muove uno alla volta.",
    ),
)


RICERCA_EMIVITA_LEGA = Ricerca(
    parametro="emivita_lega",
    griglia=GRIGLIA_EMIVITA_LEGA,
    # 180 = la stessa emivita delle squadre, cioe' il modello di oggi: con le
    # due emivite uguali `fit_dixon_coles` stima in un passo solo, come sempre.
    predefinito=DEFAULT_HALF_LIFE_DAYS,
    costante="DEFAULT_EMIVITA_LEGA",
    unita="giorni",
    formato="{:.0f}",
    titolo="L'EMIVITA DELLA LEGA - quanto ricordano vantaggio di casa, base e rho",
    intro=(
        "L'emivita delle squadre serve a dimenticarne la forma. Una stima sola",
        "la applica pero' anche ai parametri del campionato — vantaggio di",
        "casa, livello dei gol, rho — che cambiano con gli anni e non con le",
        "settimane. Qui quei tre si stimano con una memoria piu' lunga, e le",
        "squadre restano a 180 giorni.",
        "180 in testa alla griglia = la stessa memoria per tutti, cioe' oggi.",
    ),
    nota_predefinito=(
        "180 vuol dire NESSUNA memoria separata: su questi dati stimare i",
        "parametri di lega con un campione piu' lungo non migliora i",
        "pronostici, e il vantaggio di casa resta com'e'.",
    ),
)


def cerca_allenatore(
    dataset: Dataset, stagioni: Sequence[int], panchine: Panchine
) -> int:
    """Sceglie quanto pesano le partite giocate sotto un altro allenatore.

    E' l'unica delle tre ricerche che ha bisogno di un dato in piu' — le
    panchine — e l'unica che possa non avere niente da cercare: senza
    formazioni sul disco non si sa chi allenava, e un fattore che non ha su
    cosa agire darebbe quattro rigiocate identiche con l'aria di una misura.
    """
    if panchine.vuoto:
        titolo(RICERCA_ALLENATORE.titolo)
        print("Nessuna formazione sul disco: senza, non si sa chi allenava e")
        print("non c'e' niente da cercare.\n")
        return 1
    print(f"\nPanchine lette: {len(panchine.squadre())} squadre, "
          f"{panchine.partite_con_formazioni} partite su {panchine.partite_lette} "
          f"con le formazioni.")
    cambi = sum(len(panchine.cambi(s)) for s in panchine.squadre())
    print(f"Cambi di allenatore visti nell'archivio: {cambi}.")
    return cerca_nella_stima(dataset, stagioni, RICERCA_ALLENATORE, panchine)


# ---- le formazioni ufficiali ---------------------------------------------------
#
# Il lavoro 1b. `data/ufficiali.py` rifa' il pronostico quando escono le
# formazioni, spostando i gol attesi di ogni squadra in proporzione alla
# produzione che ha in campo (`data/pesi.py`); quanto spostarli lo decide
# FATTORE_PREDEFINITO, scelto a occhio. Qui lo si sceglie col metodo del
# progetto, sulle stagioni ricomprate, che hanno formazioni e tabellini di
# ogni partita.
#
# Come la compressione, questo numero agisce DOPO la stima: ogni stagione si
# rigioca una volta sola, e i fattori si provano rifacendo la matrice.
#
# Due scelte fatte PRIMA di guardare i numeri, perche' fatte dopo varrebbero
# poco:
#
#   * si misura solo sulle partite in cui la macchina vera avrebbe scritto una
#     riga (`vale_la_pena`), perche' e' su quelle che il sito confrontera' i
#     due Brier (`confronto_fra_modi`). Sulle altre il pronostico con le
#     formazioni non esiste, e contarle come pareggi annacquerebbe la misura;
#   * il fattore si sceglie sulla SOMMA dei tre Brier, non sul solo 1X2 come i
#     parametri della stima. I pesi spostano i gol attesi, cioe' tutti i
#     mercati insieme, e i mercati dei gol sono proprio la parte che si spera
#     di guadagnare (LAVORI.txt, 1b: "calibrare guardando solo l'1X2
#     butterebbe via la parte che si spera di guadagnare").

GRIGLIA_FATTORE = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
assert FATTORE_PREDEFINITO in GRIGLIA_FATTORE, "il valore di oggi deve stare nella griglia"


@dataclass(frozen=True)
class PartitaConFormazioni:
    """Un pronostico rigiocato, con la produzione che le due squadre avevano in campo."""

    pred: Prediction
    r_casa: Rapporto | None
    r_ospite: Rapporto | None

    @property
    def scritta(self) -> bool:
        """La macchina vera avrebbe scritto una riga 'formazione ufficiale'?"""
        return vale_la_pena(self.r_casa, self.r_ospite)

    def con_fattore(self, fattore: float) -> Prediction:
        """Il pronostico con le formazioni, come lo farebbe `data/ufficiali.py`.

        Si parte dai gol attesi COMPRESSI, perche' e' cio' che il modello vero
        restituisce, e l'aggiustamento si applica dopo, senza ricomprimere.
        Col fattore a zero l'aggiustamento non sposta niente: e' il pronostico
        secco, e il confronto e' "con e senza" a un parametro solo.
        """
        p = self.pred
        lam, mu = comprimi_totale(p.lam, p.mu, p.media_lega, DEFAULT_GOAL_COMPRESSION)
        lam = aggiusta_gol_attesi(lam, self.r_casa, fattore)
        mu = aggiusta_gol_attesi(mu, self.r_ospite, fattore)
        return replace(p, probs=derive_markets(scoreline_matrix(lam, mu, p.rho)))


def partite_con_formazioni(
    dataset: Dataset, unito: StoreUnito, archivio, preds: list[Prediction], stagione: int
) -> tuple[list[PartitaConFormazioni], Counter]:
    """Per ogni pronostico rigiocato, i pesi che la macchina avrebbe avuto.

    I pesi si calcolano al calcio d'inizio della prima partita della giornata,
    lo stesso istante in cui `run_backtest` stima il modello. Dal vivo la
    macchina gira 75 minuti prima di ogni partita, quindi la domenica sera
    vede anche i tabellini del sabato: qui no. La misura e' quindi un po'
    piu' povera del vero, mai piu' ricca.

    I conteggi hanno gli stessi nomi dell'esito di `pronostica_con_formazioni`,
    perche' raccontano le stesse cose.
    """
    tagli: dict[int, object] = {}
    for f in dataset.finished:
        if f.season == stagione and f.matchday is not None:
            if f.matchday not in tagli or f.kickoff_utc < tagli[f.matchday]:
                tagli[f.matchday] = f.kickoff_utc

    per_giornata: dict[int, list[Prediction]] = defaultdict(list)
    for p in preds:
        per_giornata[p.matchday].append(p)

    conti: Counter = Counter()
    righe: list[PartitaConFormazioni] = []
    for giornata in sorted(per_giornata):
        vista = archivio_alla_vigilia(archivio, dataset.fixtures, tagli[giornata], stagione)
        pesi: dict[int, object] = {}
        for p in per_giornata[giornata]:
            conti["partite"] += 1
            f = dataset.by_id[p.fixture_id]
            per_squadra = {fo.team_id: fo for fo in formazioni_partita(unito, f.api_id)}
            casa = per_squadra.get(f.home_team_id)
            ospite = per_squadra.get(f.away_team_id)
            if casa is None or ospite is None:
                conti["senza_formazioni"] += 1
                righe.append(PartitaConFormazioni(p, None, None))
                continue
            if formazione_provvisoria(unito.load_detail(f.api_id, "lineups")):
                conti["formazioni_a_meta"] += 1
                righe.append(PartitaConFormazioni(p, None, None))
                continue

            rapporti = []
            for squadra, formazione in ((f.home_team_id, casa), (f.away_team_id, ospite)):
                if squadra not in pesi:
                    pesi[squadra] = pesi_squadra(vista, squadra)
                suoi = pesi[squadra]
                rapporti.append(
                    rapporto_produzione(suoi, titolari_da_formazione(formazione))
                    if suoi else None
                )
            riga = PartitaConFormazioni(p, *rapporti)
            conti["registrate" if riga.scritta else "senza_pesi"] += 1
            righe.append(riga)
    return righe, conti


def _criterio_fattore(riga: tuple[float, float, float]) -> float:
    """Cio' che il fattore minimizza: i tre Brier insieme. Vedi in cima alla sezione."""
    return riga[0] + riga[1] + riga[2]


def _migliore_fattore(scansioni: Sequence[dict[float, tuple[float, float, float]]]) -> float:
    griglia = sorted(scansioni[0])
    return min(
        griglia,
        key=lambda f: sum(_criterio_fattore(s[f]) for s in scansioni) / len(scansioni),
    )


def _costo_partita(p: Prediction) -> float:
    """Il criterio su una partita sola, per la prova appaiata."""
    uno_x_due = sum(
        (prob - (1.0 if esito == p.outcome else 0.0)) ** 2
        for esito, prob in (("1", p.probs.home_win), ("X", p.probs.draw), ("2", p.probs.away_win))
    )
    over = (p.probs.over[2.5] - (1.0 if p.total_goals >= 3 else 0.0)) ** 2
    gol = (p.probs.btts - (1.0 if p.both_scored else 0.0)) ** 2
    return uno_x_due + over + gol


def _appaiata(coppie: list[tuple[PartitaConFormazioni, float]], contro: float = 0.0):
    """Differenza media partita per partita, col suo errore standard.

    E' la prova seria, la stessa che ha fermato la cura del difetto P1: la
    media puo' migliorare perche' poche partite migliorano molto, e quella e'
    la forma tanto di un effetto vero quanto del caso. Il rapporto fra media
    ed errore standard dice quale dei due.
    """
    diff = [
        _costo_partita(r.con_fattore(f)) - _costo_partita(r.con_fattore(contro))
        for r, f in coppie
    ]
    n = len(diff)
    media = sum(diff) / n
    varianza = sum((d - media) ** 2 for d in diff) / (n - 1) if n > 1 else 0.0
    errore = (varianza / n) ** 0.5
    migliorate = sum(1 for d in diff if d < 0)
    peggiorate = sum(1 for d in diff if d > 0)
    return n, media, errore, migliorate, peggiorate


def cerca_fattore(settings, store, dataset: Dataset, panchine: Panchine) -> int:
    """Sceglie FATTORE_PREDEFINITO, e lo misura senza barare (lavoro 1b)."""
    titolo("LE FORMAZIONI UFFICIALI - quanto credere ai pesi dei giocatori")
    print("Quando escono le formazioni, la macchina sposta i gol attesi di ogni")
    print("squadra in proporzione alla produzione che ha in campo. Il FATTORE")
    print("decide quanto: 0.00 = niente (il pronostico secco), 1.00 = tutto")
    print(f"quello che il metodo suggerisce. Oggi vale {FATTORE_PREDEFINITO:.2f}, scelto a occhio.")
    print("Si misura solo sulle partite in cui la macchina avrebbe scritto una")
    print("riga, e il fattore si sceglie sulla somma dei tre Brier.\n")

    storici = list(build_storico_stores(settings).values())
    unito = StoreUnito(stores=[store, *storici])
    archivio = costruisci_archivio(unito, dataset.fixtures)
    stagioni = sorted(
        s for s, n in archivio.con_statistiche_per_stagione.items()
        if n >= PARTITE_STAGIONE_COMPLETA
    )
    if len(stagioni) < 2:
        print("Servono almeno due stagioni complete coi tabellini.\n")
        return 1

    per_stagione: dict[int, list[PartitaConFormazioni]] = {}
    for s in stagioni:
        preds, _ = run_backtest(dataset, s, compressione=1.0, panchine=panchine)
        righe, conti = partite_con_formazioni(dataset, unito, archivio, preds, s)
        scritte = [r for r in righe if r.scritta]
        print(f"  stagione {s}: {conti['partite']} partite rigiocate, "
              f"{conti['registrate']} con la riga 'formazione ufficiale' "
              f"(senza pesi o spostamenti {conti['senza_pesi']}, senza formazioni "
              f"{conti['senza_formazioni']}, distinta a meta' {conti['formazioni_a_meta']})")
        if scritte:
            per_stagione[s] = scritte

    if len(per_stagione) < 2:
        print("\nTroppo poche partite con una riga: non c'e' niente da scegliere.\n")
        return 1

    scansioni = {
        s: {f: brier_dei_tre([r.con_fattore(f) for r in righe]) for f in GRIGLIA_FATTORE}
        for s, righe in per_stagione.items()
    }

    titolo("La misura onesta: fattore scelto sulle altre stagioni, contro il secco")
    print(f"{'stagione':<10}{'partite':>8}{'fattore':>9}{'1X2':>17}{'Over 2.5':>17}"
          f"{'Gol/NoGol':>17}")
    print("-" * 78)
    deltas = [0.0, 0.0, 0.0]
    scelti: dict[int, float] = {}
    for s, scan in scansioni.items():
        altre = [x for y, x in scansioni.items() if y != s]
        f = _migliore_fattore(altre)
        scelti[s] = f
        base, con = scan[0.0], scan[f]
        for i in range(3):
            deltas[i] += con[i] - base[i]
        print(f"{s:<10}{len(per_stagione[s]):>8}{f:>9.2f}"
              f"{f'{base[0]:.4f}->{con[0]:.4f}':>17}"
              f"{f'{base[1]:.4f}->{con[1]:.4f}':>17}"
              f"{f'{base[2]:.4f}->{con[2]:.4f}':>17}")
    n = len(scansioni)
    print("-" * 78)
    print(f"{'variazione media':<27}"
          f"{deltas[0] / n:>+17.4f}{deltas[1] / n:>+17.4f}{deltas[2] / n:>+17.4f}")
    print("\nNegativo = il pronostico con le formazioni fa meglio del secco.")

    titolo("Il quadro intero: ogni fattore, in media sulle stagioni")
    print("Serve a vedere la forma della curva, non a scegliere: qui il fattore")
    print("e' misurato sulle stesse stagioni su cui e' scelto.\n")
    print(f"{'fattore':<10}{'1X2':>12}{'Over 2.5':>12}{'Gol/NoGol':>12}{'somma':>12}")
    print("-" * 58)
    for f in GRIGLIA_FATTORE:
        medie = [sum(s[f][i] for s in scansioni.values()) / n for i in range(3)]
        segno = "  <- oggi" if f == FATTORE_PREDEFINITO else ""
        print(f"{f:<10.2f}{medie[0]:>12.4f}{medie[1]:>12.4f}{medie[2]:>12.4f}"
              f"{sum(medie):>12.4f}{segno}")
    print(f"\n{'stagione':<10}" + "".join(f"{f:>9.2f}" for f in GRIGLIA_FATTORE))
    print("-" * (10 + 9 * len(GRIGLIA_FATTORE)))
    for s, scan in scansioni.items():
        print(f"{s:<10}" + "".join(f"{_criterio_fattore(scan[f]):>9.4f}" for f in GRIGLIA_FATTORE))
    print("(la somma dei tre Brier per stagione e fattore: se il minimo salta da")
    print(" un valore all'altro, il fattore vero non e' determinato dai dati.)")

    titolo("La prova appaiata, partita per partita")
    coppie_oneste = [(r, scelti[s]) for s, righe in per_stagione.items() for r in righe]
    coppie_oggi = [(r, FATTORE_PREDEFINITO) for righe in per_stagione.values() for r in righe]
    for etichetta, coppie in (
        ("fattore scelto sulle altre, contro il secco", coppie_oneste),
        (f"il {FATTORE_PREDEFINITO:.2f} di oggi, contro il secco", coppie_oggi),
    ):
        quante, media, errore, meglio, peggio = _appaiata(coppie)
        rapporto = media / errore if errore else 0.0
        print(f"{etichetta}:")
        print(f"  {quante} partite, differenza media {media:+.5f} "
              f"({rapporto:+.2f} errori standard)")
        print(f"  migliorate {meglio}, peggiorate {peggio}\n")
    print("Sotto i due errori standard la differenza non si distingue dal caso:")
    print("e' la regola con cui e' stata fermata la cura del difetto P1.")

    scelto = _migliore_fattore(list(scansioni.values()))
    guadagno = sum(deltas) / n
    _, media, errore, _, _ = _appaiata(coppie_oneste)
    rapporto = media / errore if errore else 0.0
    titolo(f"IL VERDETTO - fattore migliore sulle stagioni insieme: {scelto:.2f}")
    if scelto == 0.0 or guadagno >= 0:
        print("Scelto senza guardare la risposta, il pronostico con le formazioni")
        print(f"non fa meglio del secco ({guadagno:+.4f} sulla somma dei Brier).")
        print("Attenzione a cosa vorrebbe dire metterlo a zero: `vale_la_pena`")
        print("guarda i rapporti, non il fattore, quindi la macchina scriverebbe")
        print("righe identiche al secco. Spegnerla e' un'altra decisione.")
    elif rapporto > -2.0:
        print(f"Scelto sulle altre stagioni migliora di {guadagno:+.4f} la somma dei")
        print(f"Brier, ma la prova appaiata si ferma a {rapporto:+.2f} errori standard:")
        print("non si distingue dal caso. Con la regola del progetto non passa.")
    else:
        print(f"Scelto sulle altre stagioni migliora di {guadagno:+.4f} la somma dei")
        print(f"Brier, e la prova appaiata dice {rapporto:+.2f} errori standard: e' un")
        print("effetto vero. E' il valore da mettere in FATTORE_PREDEFINITO")
        print("(data/pesi.py).")
    return 0


# ---- presentazione ---------------------------------------------------------


# ---- i marcatori ---------------------------------------------------------------
#
# Non una ricerca ma una misura: i parametri di data/marcatori.py sono scelti a
# priori, e qui si guarda soltanto se il pronostico che ne esce vale qualcosa.
# La domanda e' "questo giocatore segna?", una riga per giocatore e partita.


@dataclass(frozen=True)
class RigaMarcatore:
    stagione: int
    ruolo: str | None
    segna: bool
    modello: float          # dentro la matrice: il pronostico vero
    senza_partita: float    # lo stesso giocatore, come se ogni partita fosse uguale


def misura_marcatori(
    settings, store, dataset: Dataset, panchine: Panchine, stagione: int | None
) -> int:
    """Rigioca le stagioni coi tabellini e misura il pronostico su chi segna.

    Due modi, perche' sono due momenti diversi: coi TITOLARI NOTI (le
    formazioni escono quaranta minuti prima del via) e PRIMA DELLE FORMAZIONI,
    in cui i candidati sono quelli delle ultime partite e chi poi non gioca
    conta come un giocatore che non ha segnato. La prima stagione coi tabellini
    non si rigioca: fa da rodaggio, perche' senza storia ogni giocatore
    varrebbe la media del suo ruolo e la misura parlerebbe dello shrinkage.

    Le baseline vedono la stagione intera (la frequenza vera di chi segna, in
    tutto e per ruolo): e' la stessa convenzione del resto del backtest, e le
    mette in vantaggio. Batterle vale di piu', non di meno.
    """
    titolo("MARCATORI - chi segna, contro i suoi concorrenti")
    storici = list(build_storico_stores(settings).values())
    archivio = costruisci_archivio(StoreUnito(stores=[store, *storici]), dataset.fixtures)
    storia = StoriaMarcatori(archivio.prestazioni, dataset.fixtures)
    per_partita: dict[int, list] = defaultdict(list)
    for p in archivio.prestazioni:
        per_partita[p.fixture_id].append(p)

    con_tabellini = sorted(
        s for s, n in archivio.con_statistiche_per_stagione.items()
        if n >= PARTITE_STAGIONE_COMPLETA
    )
    stagioni = [stagione] if stagione is not None else con_tabellini[1:]
    if not stagioni:
        print("\nServono almeno due stagioni complete coi tabellini: la prima fa da rodaggio.\n")
        return 1
    print(f"Stagioni coi tabellini: {', '.join(map(str, con_tabellini))}")
    print(f"Rigiocate:              {', '.join(map(str, stagioni))}")

    righe: dict[str, list[RigaMarcatore]] = {"titolari": [], "rosa": []}
    gol_dei_candidati = gol_di_tutti = 0
    for s in stagioni:
        preds, _ = run_backtest(dataset, s, panchine=panchine)
        print(f"  stagione {s}: {len(preds)} partite rigiocate")
        for p in preds:
            f = dataset.by_id.get(p.fixture_id)
            prestazioni = per_partita.get(p.fixture_id)
            if f is None or not prestazioni:
                continue
            # La matrice e' quella che il sito mostrerebbe: compressa.
            lam, mu = comprimi_totale(p.lam, p.mu, p.media_lega, DEFAULT_GOAL_COMPRESSION)
            matrice = scoreline_matrix(lam, mu, p.rho)
            for lato, squadra in (("home", f.home_team_id), ("away", f.away_team_id)):
                suoi = [x for x in prestazioni if x.team_id == squadra]
                if not suoi:
                    continue
                segnato = {x.player_id: x.gol > 0 for x in suoi}
                titolari = [x for x in suoi if x.titolare]
                rosa = storia.candidati(squadra, f.kickoff_utc)
                in_rosa = {c.player_id for c in rosa}
                gol_dei_candidati += sum(x.gol for x in suoi if x.player_id in in_rosa)
                gol_di_tutti += sum(x.gol for x in suoi)
                for modo, candidati in (
                    ("titolari", storia.candidati(squadra, f.kickoff_utc, titolari=titolari)),
                    ("rosa", rosa),
                ):
                    for c in candidati:
                        righe[modo].append(RigaMarcatore(
                            stagione=s,
                            ruolo=c.ruolo,
                            segna=segnato.get(c.player_id, False),
                            modello=player_scores(matrice, lato, c.quota),
                            senza_partita=1.0 - exp(-c.tasso_90 * c.minuti_attesi / 90.0),
                        ))

    _rapporto_marcatori("COI TITOLARI NOTI (a formazioni uscite)", righe["titolari"])
    _rapporto_marcatori("PRIMA DELLE FORMAZIONI (i candidati delle ultime partite)", righe["rosa"])
    if gol_di_tutti:
        print(f"\nPrima delle formazioni i candidati hanno segnato {gol_dei_candidati} gol "
              f"su {gol_di_tutti} ({gol_dei_candidati / gol_di_tutti:.1%}):")
        print("il resto e' di chi nelle ultime partite non aveva giocato.")
    return 0


def _rapporto_marcatori(nome: str, righe: list[RigaMarcatore]) -> None:
    titolo(nome)
    if not righe:
        print("Nessuna riga.")
        return
    esiti = [r.segna for r in righe]
    frequenza = sum(esiti) / len(esiti)
    per_ruolo: dict[str | None, list[bool]] = defaultdict(list)
    for r in righe:
        per_ruolo[r.ruolo].append(r.segna)
    freq_ruolo = {k: sum(v) / len(v) for k, v in per_ruolo.items()}

    metodi = (
        ("stessa probabilita' per tutti", [frequenza] * len(righe)),
        ("in base al ruolo", [freq_ruolo[r.ruolo] for r in righe]),
        ("il giocatore, ogni partita uguale", [r.senza_partita for r in righe]),
        ("il giocatore dentro la matrice", [r.modello for r in righe]),
    )
    base = log_loss_binary(metodi[0][1], esiti)
    print(f"{len(righe)} righe giocatore-partita; segna il {frequenza:.1%}.\n")
    print(f"{'metodo':<36}{'Brier':>10}{'log loss':>11}{'guadagno':>11}")
    print("-" * 68)
    for etichetta, probs in metodi:
        ll = log_loss_binary(probs, esiti)
        print(f"{etichetta:<36}{brier_binary(probs, esiti):>10.4f}{ll:>11.4f}"
              f"{(base - ll) / base:>+11.1%}")
    print("\nLe prime due righe vedono la stagione intera: sono baseline favorite.")
    print("L'ultima e' il pronostico vero; la penultima dice quanto aggiunge la")
    print("partita — l'avversario, il campo — al solo giocatore.")

    print(f"\n{'stagione':<10}{'righe':>8}{'costante':>12}{'modello':>12}{'guadagno':>11}")
    print("-" * 53)
    for s in sorted({r.stagione for r in righe}):
        sue = [r for r in righe if r.stagione == s]
        e = [r.segna for r in sue]
        f_s = sum(e) / len(e)
        c = log_loss_binary([f_s] * len(e), e)
        m = log_loss_binary([r.modello for r in sue], e)
        print(f"{s:<10}{len(sue):>8}{c:>12.4f}{m:>12.4f}{(c - m) / c:>+11.1%}")

    probs = [r.modello for r in righe]
    print(f"\nMarcatori dichiarati {sum(probs):.0f}, accaduti {sum(esiti)} "
          "(somma delle probabilita' contro chi ha segnato davvero)")
    print(f"\n  {'fascia':<12}{'n':>7}{'previsto':>12}{'reale':>10}{'scarto':>10}")
    print("  " + "-" * 49)
    for etichetta, n, media, reale in calibration(probs, esiti, n_bin=20):
        print(f"  {etichetta:<12}{n:>7}{media:>12.1%}{reale:>10.1%}{reale - media:>+10.1%}")


def titolo(testo: str) -> None:
    print(f"\n{'=' * 74}\n{testo}\n{'=' * 74}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backtest walk-forward su una stagione conclusa"
    )
    parser.add_argument(
        "--stagione",
        type=int,
        default=None,
        help="anno di partenza della stagione da rigiocare "
             "(default: l'ultima completa fra quelle in archivio)",
    )
    parser.add_argument(
        "--compressione",
        type=float,
        default=DEFAULT_GOAL_COMPRESSION,
        help="quanto delle differenze fra squadre resta nei gol totali "
             "(0 = sui gol sempre la media di lega, 1 = niente compressione; "
             f"default: {DEFAULT_GOAL_COMPRESSION})",
    )
    parser.add_argument(
        "--cerca-compressione",
        action="store_true",
        help="prova tutti i fattori su tutte le stagioni complete e dice "
             "quale conviene, scegliendolo ogni volta sulle stagioni diverse "
             "da quella su cui lo misura",
    )
    parser.add_argument(
        "--fattore-allenatore",
        type=float,
        default=DEFAULT_FATTORE_ALLENATORE,
        help="quanto pesano le partite giocate sotto un altro allenatore "
             "(0 = la gestione precedente non conta, 1 = niente correzione; "
             f"default: {DEFAULT_FATTORE_ALLENATORE})",
    )
    parser.add_argument(
        "--cerca-allenatore",
        action="store_true",
        help="cerca quel fattore rigiocando ogni stagione una volta per "
             "valore. Zero richieste, una ventina di minuti",
    )
    parser.add_argument(
        "--emivita",
        type=float,
        default=DEFAULT_HALF_LIFE_DAYS,
        help="dopo quanti giorni una partita pesa la meta' "
             f"(default: {DEFAULT_HALF_LIFE_DAYS:.0f})",
    )
    parser.add_argument(
        "--cerca-emivita",
        action="store_true",
        help="cerca l'emivita rigiocando ogni stagione una volta per valore. "
             "Zero richieste, una mezz'ora",
    )
    parser.add_argument(
        "--shrinkage",
        type=float,
        default=DEFAULT_REGULARIZATION,
        help="quanto le forze vengono tirate verso la media di lega "
             f"(0 = per niente; default: {DEFAULT_REGULARIZATION})",
    )
    parser.add_argument(
        "--cerca-shrinkage",
        action="store_true",
        help="cerca lo shrinkage rigiocando ogni stagione una volta per "
             "valore. Zero richieste, una ventina di minuti",
    )
    parser.add_argument(
        "--emivita-lega",
        type=float,
        default=DEFAULT_EMIVITA_LEGA,
        help="emivita di vantaggio di casa, base e rho, se diversa da quella "
             "delle squadre (default: la stessa)",
    )
    parser.add_argument(
        "--cerca-emivita-lega",
        action="store_true",
        help="cerca quell'emivita rigiocando ogni stagione una volta per "
             "valore (lavoro 6). Zero richieste, una mezz'ora",
    )
    parser.add_argument(
        "--marcatori",
        action="store_true",
        help="misura il pronostico su chi segna sulle stagioni coi tabellini "
             "(con --stagione, una sola). Zero richieste",
    )
    parser.add_argument(
        "--cerca-fattore",
        action="store_true",
        help="sceglie quanto credere alle formazioni ufficiali (FATTORE_PREDEFINITO) "
             "rigiocando le stagioni coi tabellini. Zero richieste",
    )
    args = parser.parse_args()
    if not 0.0 <= args.compressione <= 1.0:
        print("\n--compressione va da 0 a 1.\n")
        return 1
    if not 0.0 <= args.fattore_allenatore <= 1.0:
        print("\n--fattore-allenatore va da 0 a 1.\n")
        return 1
    if args.emivita <= 0:
        # Zero non e' "niente decadimento" ma una divisione per zero: il
        # peso di una partita e' exp(-log(2) * giorni / emivita). Chi
        # vuole il modello senza memoria passa un'emivita lunghissima.
        print("\n--emivita dev'essere positiva. Per spegnere il "
              "decadimento si usa un valore altissimo: 1825.\n")
        return 1
    if args.shrinkage < 0:
        print("\n--shrinkage non puo' essere negativo "
              "(0 = nessuno shrinkage).\n")
        return 1
    if args.emivita_lega is not None and args.emivita_lega <= 0:
        print("\n--emivita-lega dev'essere positiva.\n")
        return 1

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n[configurazione] {exc}\n")
        return 1

    client = build_client(settings)
    prima = client.budget.used
    store, _ = build_store(settings)

    # Le stesse sorgenti che alimentano il sito: cache, raccolta giornaliera,
    # stagioni ricomprate e stagioni importate da un file.
    dataset = load_dataset(
        client, store, settings.seasons, settings.league_id,
        storici=build_storico_stores(settings),
        importati=build_importato_stores(settings),
    )

    ricerche = (
        args.cerca_compressione, args.cerca_allenatore,
        args.cerca_emivita, args.cerca_shrinkage, args.cerca_fattore,
        args.cerca_emivita_lega,
    )
    if sum(bool(r) for r in ricerche) > 1:
        # Due ricerche insieme non sono due misure: la seconda partirebbe
        # comunque dal valore di oggi della prima, e il rapporto direbbe di
        # aver scelto due parametri che non si sono mai visti.
        print("\nUna ricerca alla volta: i parametri si parlano, e cercarne due")
        print("insieme non misura nessuno dei due.\n")
        return 1

    if args.marcatori:
        if any(ricerche):
            print("\n--marcatori misura il modello di oggi: non va insieme a una ricerca.\n")
            return 1
        panchine = carica_panchine(settings, store, dataset)
        esito = misura_marcatori(settings, store, dataset, panchine, args.stagione)
        print(f"\nRichieste API spese: {client.budget.used - prima}")
        return esito

    if args.cerca_fattore:
        panchine = carica_panchine(settings, store, dataset)
        esito = cerca_fattore(settings, store, dataset, panchine)
        print(f"\nRichieste API spese: {client.budget.used - prima}")
        return esito

    if any(ricerche):
        complete = [
            s for s, quante in stagioni_disponibili(dataset).items()
            if quante >= PARTITE_STAGIONE_COMPLETA
        ]
        if not complete:
            print("\nNessuna stagione completa in archivio: non c'e' niente da rigiocare.\n")
            return 1
        if args.cerca_compressione:
            return cerca_compressione(dataset, complete)
        # Le panchine servono davvero solo alla ricerca dell'allenatore, ma si
        # leggono comunque: costano letture da disco e tengono le altre due
        # ricerche identiche al modello che gira davvero.
        panchine = carica_panchine(settings, store, dataset)
        if args.cerca_allenatore:
            return cerca_allenatore(dataset, complete, panchine)
        if args.cerca_emivita:
            ricerca = RICERCA_EMIVITA
        elif args.cerca_emivita_lega:
            ricerca = RICERCA_EMIVITA_LEGA
        else:
            ricerca = RICERCA_SHRINKAGE
        return cerca_nella_stima(dataset, complete, ricerca, panchine)

    # Le panchine si leggono sempre: costano solo letture da disco, e senza di
    # loro il fattore non avrebbe su cosa agire. Su una stagione priva di
    # formazioni restano vuote e la stima e' identica a quella di sempre.
    panchine = carica_panchine(settings, store, dataset)

    target = args.stagione or stagione_predefinita(dataset)
    if target is None:
        print("\nNessuna stagione completa in archivio: non c'e' niente da rigiocare.\n")
        return 1

    titolo(f"BACKTEST WALK-FORWARD - stagione {target}/{str(target + 1)[2:]}")
    print("Ogni giornata e' pronosticata con un modello che non ha mai visto")
    print("nemmeno una partita successiva al suo calcio d'inizio.\n")

    disponibili = stagioni_disponibili(dataset)
    print("Stagioni in archivio: "
          + ", ".join(f"{s} ({n})" for s, n in disponibili.items()))

    try:
        preds, info = run_backtest(
            dataset, target,
            compressione=args.compressione,
            panchine=panchine,
            fattore_allenatore=args.fattore_allenatore,
            emivita=args.emivita,
            shrinkage=args.shrinkage,
            emivita_lega=args.emivita_lega,
        )
    except RuntimeError as exc:
        # Chiedere una stagione che non c'e' e' un errore di chi lancia il
        # comando, non un guasto: si dice cosa c'e', senza traceback.
        print(f"\n{exc}\n")
        return 1
    if not preds:
        print("Nessun pronostico prodotto.")
        return 1

    print(f"\nPartite pronosticate: {len(preds)}")
    print(f"Giornate rigiocate:   {info['giornate']}")
    print(f"Partite escluse:      {info['saltate']} (storico insufficiente)")
    print(f"Richieste API spese:  {client.budget.used - prima}")
    if args.compressione < 1.0:
        print(f"Compressione dei gol: {args.compressione:.2f} "
              f"(media di lega {preds[0].media_lega:.2f} gol a partita)")
    if args.fattore_allenatore < 1.0:
        cambi = sum(len(panchine.cambi(sq)) for sq in panchine.squadre())
        print(f"Fattore allenatore:   {args.fattore_allenatore:.2f} "
              f"({cambi} cambi di panchina nell'archivio)")
    # I due parametri della stima si dichiarano solo se qualcuno li ha mossi:
    # scritti sempre sarebbero due righe di rumore in fondo a ogni rapporto,
    # scritti mai renderebbero impossibile capire da dove viene un numero
    # diverso dal solito.
    if args.emivita != DEFAULT_HALF_LIFE_DAYS:
        print(f"Emivita:              {args.emivita:.0f} giorni "
              f"(di serie: {DEFAULT_HALF_LIFE_DAYS:.0f})")
    if args.shrinkage != DEFAULT_REGULARIZATION:
        print(f"Shrinkage:            {args.shrinkage:.1f} "
              f"(di serie: {DEFAULT_REGULARIZATION})")
    if args.emivita_lega is not None:
        print(f"Emivita della lega:   {args.emivita_lega:.0f} giorni "
              f"(di serie: la stessa delle squadre)")

    # --- 1X2 ---------------------------------------------------------------
    titolo("1X2 - il modello contro i suoi concorrenti")

    b_modello = brier_1x2(preds)
    b_caso = brier_costante(preds, (1 / 3, 1 / 3, 1 / 3))
    freq = frequenze_storiche(preds)
    b_freq = brier_costante(preds, freq)

    print(f"{'Metodo':<42}{'Brier':>10}{'vs modello':>14}")
    print("-" * 66)
    print(f"{'MODELLO Dixon-Coles':<42}{b_modello:>10.4f}{'-':>14}")
    print(f"{'sempre 1/3 - 1/3 - 1/3 (caso puro)':<42}{b_caso:>10.4f}"
          f"{(b_caso - b_modello) / b_caso:>13.1%}")
    print(f"{'sempre le frequenze di lega':<42}{b_freq:>10.4f}"
          f"{(b_freq - b_modello) / b_freq:>13.1%}")
    print(f"\n(frequenze reali del campione: 1 {freq[0]:.1%}  X {freq[1]:.1%}  2 {freq[2]:.1%})")
    print("Brier piu' basso = meglio. La colonna a destra e' quanto il modello")
    print("guadagna sul concorrente.")

    print(f"\nLog loss modello:        {log_loss_1x2(preds):.4f}")
    azzeccati = sum(1 for p in preds if p.predicted == p.outcome)
    print(f"Esito piu' probabile azzeccato: {azzeccati}/{len(preds)} ({azzeccati / len(preds):.1%})")
    print("  (indicatore volutamente secondario: dire sempre '1' in Serie A")
    print("   ne azzecca gia' circa il 45% senza sapere nulla di calcio)")

    # --- altri mercati -----------------------------------------------------
    titolo("Altri esiti derivati dalla stessa matrice")

    over_p = [p.probs.over[2.5] for p in preds]
    over_e = [p.total_goals >= 3 for p in preds]
    btts_p = [p.probs.btts for p in preds]
    btts_e = [p.both_scored for p in preds]

    quota_over = sum(over_e) / len(over_e)
    quota_btts = sum(btts_e) / len(btts_e)

    print(f"{'Esito':<20}{'Brier':>10}{'baseline':>12}{'accaduto':>12}{'previsto':>12}")
    print("-" * 66)
    print(f"{'Over 2.5':<20}{brier_binary(over_p, over_e):>10.4f}"
          f"{brier_binary([quota_over] * len(over_e), over_e):>12.4f}"
          f"{quota_over:>12.1%}{sum(over_p) / len(over_p):>12.1%}")
    print(f"{'Gol/NoGol':<20}{brier_binary(btts_p, btts_e):>10.4f}"
          f"{brier_binary([quota_btts] * len(btts_e), btts_e):>12.4f}"
          f"{quota_btts:>12.1%}{sum(btts_p) / len(btts_p):>12.1%}")
    print("\n'baseline' = dire sempre la frequenza media della stagione.")
    print("Le ultime due colonne insieme dicono se il modello ha un bias di")
    print("fondo: se previsto e accaduto divergono, sbaglia in modo sistematico.")

    # --- calibrazione ------------------------------------------------------
    titolo("Calibrazione - quando dico 30%, succede il 30% delle volte?")

    for nome, probs, esiti in (
        ("Vittoria in casa", [p.probs.home_win for p in preds], [p.outcome == "1" for p in preds]),
        ("Over 2.5", over_p, over_e),
    ):
        print(f"\n{nome}")
        print(f"  {'fascia':<12}{'n':>6}{'previsto':>12}{'reale':>10}{'scarto':>10}")
        print("  " + "-" * 48)
        for etichetta, n, media, reale in calibration(probs, esiti):
            print(f"  {etichetta:<12}{n:>6}{media:>12.1%}{reale:>10.1%}{reale - media:>+10.1%}")

    print("\nUno scarto piccolo e senza segno costante = probabilita' oneste.")
    print("Uno scarto sempre negativo = il modello e' troppo ottimista.")

    # --- casi limite -------------------------------------------------------
    titolo("Le 5 partite in cui il modello ha sbagliato di piu'")
    peggiori = sorted(preds, key=lambda p: p.prob_of_truth)[:5]
    print(f"{'giornata':<10}{'partita':<34}{'risultato':<12}{'dava':>8}")
    print("-" * 66)
    for p in peggiori:
        print(f"{p.matchday:<10}{p.home + ' - ' + p.away:<34}"
              f"{f'{p.home_goals}-{p.away_goals} ({p.outcome})':<12}{p.prob_of_truth:>8.1%}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
