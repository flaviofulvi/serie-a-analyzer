"""Chi segna: la probabilita' che un giocatore vada in gol, dalla matrice dei risultati.

LA FORMA, che viene dalla regola del progetto

Il pronostico non e' un modello nuovo. La matrice dei risultati esatti dice
gia' quanti gol fara' la squadra; qui si stima soltanto **quale parte di quei
gol spetta a ciascun giocatore** (la sua `quota`), e la probabilita' che segni
e' una somma di celle della stessa matrice (`player_scores` in
model/markets.py). Cosi' "Lautaro segna al 40%" e "l'Inter resta a zero al 25%"
escono dallo stesso oggetto e non possono contraddirsi.

LA QUOTA di un giocatore e' il rapporto fra due numeri:

* quanto produce lui in una partita: il suo tasso per 90 minuti per i minuti
  che ci si aspetta giochi;
* quanto segna di solito la sua squadra, dal punteggio vero delle ultime
  partite — autogol a favore compresi, perche' sono compresi anche nei gol
  della matrice.

Il **tasso** mescola i gol veri e quelli "da tiri" (i tiri moltiplicati per
quanto spesso un tiro diventa gol in campionato). E' la stessa ragione di
data/pesi.py: un titolare segna una volta ogni dodici partite, e su quaranta
episodi a stagione non si distinguono quattro giocatori; il tiro capita otto
volte piu' spesso. Poi passa per uno **shrinkage** verso la media del suo
ruolo, pesato sui minuti: chi e' entrato dieci minuti e ha segnato non vale
piu' di un centravanti. Un giocatore che l'archivio non ha mai visto vale la
media del suo ruolo, non zero.

DUE MODI, perche' le formazioni escono quaranta minuti prima del fischio

* **titolari noti**: si passano gli undici, e i minuti attesi di ognuno sono
  quelli che gioca di solito quando parte titolare;
* **prima delle formazioni**: i candidati sono quelli scesi in campo nelle
  ultime partite della squadra, e i minuti attesi sono la loro media su quelle
  partite — zero dove non hanno giocato. Chi e' dato per assente certo non
  entra. E' il modo meno preciso e il piu' onesto su chi giochera'.

I NUMERI QUI SOTTO SONO SCELTI A PRIORI e non cercati, dichiarati come tali:
la misura e' `scripts/backtest.py --marcatori`, e chi li ritocca la rifa'.

I LIMITI, da dire accanto al numero

* rigori: chi li tira non e' riconosciuto, il suo tasso li contiene e basta;
* un giocatore che cambia ruolo porta con se' il tasso di quello vecchio;
* un titolare segna nell'8.5% delle partite: il piu' probabile di una partita
  raramente supera il 40-50%, e scriverlo come "segnera'" sarebbe falso.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import numpy as np

from ..model.markets import player_scores

# Quanto conta un gol vero rispetto a un gol "da tiri", nel tasso. Meta' e
# meta': il gol e' cio' che si vuole prevedere, il tiro il modo meno rumoroso
# di arrivarci. Scelto a priori.
PESO_GOL = 0.5

# Forza dello shrinkage verso la media del ruolo, in minuti: dieci partite
# piene. Chi ne ha giocate dieci sta a meta' fra il suo tasso e quello del
# ruolo. Scelto a priori.
MINUTI_DI_PRIOR = 900.0

# I minuti di un titolare qualunque, e quante partite-equivalenti pesano nel
# conto dei minuti attesi da titolare di ognuno.
MINUTI_TITOLARE = 78.0
PARTITE_DI_PRIOR_MINUTI = 3.0

# Quante partite della squadra dicono quanto segna "di solito": una stagione.
# Piu' corta sarebbe rumore, piu' lunga parlerebbe di un'altra squadra.
PARTITE_SQUADRA = 38
PARTITE_DI_PRIOR_SQUADRA = 5.0

# Quante partite della squadra dicono chi gioca, prima delle formazioni.
PARTITE_RECENTI = 5

# Nessuno fa tutti i gol della sua squadra: il tetto impedisce a un tasso
# gonfiato (un giocatore con pochissimi minuti in una squadra che segna poco)
# di diventare una certezza.
QUOTA_MASSIMA = 0.95

# Valori di partenza, usati solo quando l'archivio non sa ancora niente.
CONVERSIONE_PREDEFINITA = 0.11      # gol per tiro
TASSO_PREDEFINITO = 0.10            # gol-equivalenti per 90 minuti
GOL_PREDEFINITI = 1.3               # gol di una squadra a partita

RUOLI = ("G", "D", "M", "F")

# Quanto il pronostico dichiara di troppo: misurato l'11 settembre 2026 con
# `backtest.py --marcatori` sul 2023-2025, coi titolari noti, 2273 marcatori
# dichiarati contro 2110 accaduti. Non corregge niente — una correzione e' un
# parametro e va scelta col metodo del progetto. Sta qui per essere DETTO
# accanto al numero, e chi rifa' la misura aggiorna anche questo.
SOVRASTIMA_MISURATA = 2273 / 2110 - 1

# Le colonne delle somme cumulate.
_MINUTI, _GOL, _TIRI, _TITOLARE, _MINUTI_DA_TITOLARE = range(5)
_COLONNE = 5


@dataclass(frozen=True)
class Candidato:
    """Un giocatore che puo' segnare, con cio' che se ne sa prima del fischio."""

    player_id: int
    nome: str
    team_id: int
    ruolo: str | None
    minuti_attesi: float
    # Gol-equivalenti per 90 minuti, gia' passati per lo shrinkage.
    tasso_90: float
    # La parte dei gol della squadra che gli spetta, da 0 a QUOTA_MASSIMA.
    quota: float


@dataclass(frozen=True)
class Marcatore:
    candidato: Candidato
    probabilita: float


class _Serie:
    """Somme cumulate nel tempo: quanto si era accumulato prima di un istante."""

    def __init__(self, voci: list[tuple[datetime, tuple[float, ...]]], colonne: int):
        self.istanti = [q for q, _ in voci]
        self.cumulate = np.zeros((len(voci) + 1, colonne))
        if voci:
            self.cumulate[1:] = np.cumsum(np.array([v for _, v in voci], dtype=float), axis=0)

    def indice(self, prima: datetime) -> int:
        # bisect_left: una voce con lo stesso istante del fischio d'inizio e'
        # la partita stessa, e non si conta.
        return bisect.bisect_left(self.istanti, prima)

    def prima_di(self, prima: datetime) -> np.ndarray:
        return self.cumulate[self.indice(prima)]


class StoriaMarcatori:
    """Le prestazioni di tutti, pronte per chiedere "cosa si sapeva prima di quel giorno".

    Ogni domanda prende un istante e guarda solo cio' che e' successo prima:
    e' cio' che permette di rigiocare quattro stagioni senza che una partita
    veda il proprio tabellino, e al sito di usare lo stesso oggetto per la
    partita di stasera.
    """

    def __init__(self, prestazioni: Iterable[Any], fixtures: Iterable[Any]):
        partite = {
            f.api_id: f for f in fixtures
            if f.home_goals is not None and f.away_goals is not None
        }
        per_giocatore: dict[int, list] = defaultdict(list)
        per_ruolo: dict[str, list] = defaultdict(list)
        tutte: list = []
        self._minuti_in_partita: dict[tuple[int, int], dict[int, int]] = defaultdict(dict)
        self._nomi: dict[int, str] = {}

        for p in sorted(prestazioni, key=lambda p: p.quando):
            titolare = 1.0 if p.titolare else 0.0
            voce = (float(p.minuti), float(p.gol), float(p.tiri or 0),
                    titolare, p.minuti * titolare)
            per_giocatore[p.player_id].append((p.quando, voce, p.posizione, p.team_id))
            if p.posizione in RUOLI:
                per_ruolo[p.posizione].append((p.quando, voce))
            tutte.append((p.quando, voce))
            self._minuti_in_partita[(p.fixture_id, p.team_id)][p.player_id] = p.minuti
            self._nomi[p.player_id] = p.nome

        # Per giocatore: le somme nel tempo, e accanto — voce per voce — il
        # ruolo e la squadra di quella partita.
        self._giocatori = {
            pid: (
                _Serie([(r[0], r[1]) for r in righe], _COLONNE),
                [r[2] for r in righe],
                [r[3] for r in righe],
            )
            for pid, righe in per_giocatore.items()
        }
        self._ruoli = {r: _Serie(v, _COLONNE) for r, v in per_ruolo.items()}
        self._tutti = _Serie(tutte, _COLONNE)

        # Le partite di ogni squadra di cui si ha il tabellino, col punteggio
        # VERO: la somma dei gol dei giocatori perderebbe gli autogol a
        # favore, che la matrice invece conta.
        per_squadra: dict[int, list[tuple[datetime, int, int]]] = defaultdict(list)
        lega: list = []
        for fixture_id, team_id in self._minuti_in_partita:
            f = partite.get(fixture_id)
            if f is None:
                continue
            if team_id == f.home_team_id:
                gol = f.home_goals
            elif team_id == f.away_team_id:
                gol = f.away_goals
            else:
                continue
            per_squadra[team_id].append((f.kickoff_utc, fixture_id, int(gol)))
            lega.append((f.kickoff_utc, (1.0, float(gol))))
        for righe in per_squadra.values():
            righe.sort()
        self._squadre = dict(per_squadra)
        lega.sort(key=lambda v: v[0])
        self._lega = _Serie(lega, 2)

    # ---- i pezzi della quota ------------------------------------------------

    def conversione(self, prima: datetime) -> float:
        """Gol per tiro in campionato, prima di ``prima``."""
        somme = self._tutti.prima_di(prima)
        return somme[_GOL] / somme[_TIRI] if somme[_TIRI] > 0 else CONVERSIONE_PREDEFINITA

    @staticmethod
    def _produzione(somme: np.ndarray, conversione: float) -> float:
        return PESO_GOL * somme[_GOL] + (1.0 - PESO_GOL) * conversione * somme[_TIRI]

    def tasso_ruolo(self, ruolo: str | None, prima: datetime) -> float:
        """Gol-equivalenti per 90 del ruolo: il centro verso cui si tira ognuno."""
        conversione = self.conversione(prima)
        serie = self._ruoli.get(ruolo) if ruolo else None
        somme = serie.prima_di(prima) if serie is not None else None
        if somme is None or somme[_MINUTI] <= 0:
            somme = self._tutti.prima_di(prima)
        if somme[_MINUTI] <= 0:
            return TASSO_PREDEFINITO
        return self._produzione(somme, conversione) / somme[_MINUTI] * 90.0

    def ruolo(self, player_id: int, prima: datetime) -> str | None:
        """L'ultimo ruolo in cui l'archivio l'ha visto giocare, prima di ``prima``."""
        dati = self._giocatori.get(player_id)
        if dati is None:
            return None
        serie, ruoli, _ = dati
        for ruolo in reversed(ruoli[:serie.indice(prima)]):
            if ruolo in RUOLI:
                return ruolo
        return None

    def squadra_attuale(self, player_id: int, prima: datetime) -> int | None:
        """La squadra della sua ultima partita prima di ``prima``.

        Serve a una cosa sola, e nasce da un caso vero: l'11 settembre 2026
        Gaetano compariva fra i candidati dell'Atalanta E del Cagliari, ceduto
        a fine mercato ma ancora nelle ultime cinque partite di chi l'aveva
        venduto. Chi ha giocato l'ultima volta con un'altra maglia non e' piu'
        un candidato di questa.
        """
        dati = self._giocatori.get(player_id)
        if dati is None:
            return None
        serie, _, squadre = dati
        i = serie.indice(prima)
        return squadre[i - 1] if i else None

    def tasso(self, player_id: int, ruolo: str | None, prima: datetime) -> float:
        """I gol-equivalenti per 90 di un giocatore, tirati verso la media del ruolo."""
        conversione = self.conversione(prima)
        centro = self.tasso_ruolo(ruolo, prima)
        dati = self._giocatori.get(player_id)
        somme = dati[0].prima_di(prima) if dati is not None else np.zeros(_COLONNE)
        return (
            (self._produzione(somme, conversione) + MINUTI_DI_PRIOR / 90.0 * centro)
            / (somme[_MINUTI] + MINUTI_DI_PRIOR) * 90.0
        )

    def minuti_da_titolare(self, player_id: int, prima: datetime) -> float:
        """Quanto gioca di solito quando parte titolare."""
        dati = self._giocatori.get(player_id)
        somme = dati[0].prima_di(prima) if dati is not None else np.zeros(_COLONNE)
        return (
            (somme[_MINUTI_DA_TITOLARE] + PARTITE_DI_PRIOR_MINUTI * MINUTI_TITOLARE)
            / (somme[_TITOLARE] + PARTITE_DI_PRIOR_MINUTI)
        )

    def gol_abituali(self, team_id: int, prima: datetime) -> float:
        """Quanti gol fa di solito la squadra, tirati verso la media di lega."""
        lega = self._lega.prima_di(prima)
        media = lega[1] / lega[0] if lega[0] > 0 else GOL_PREDEFINITI
        recenti = [g for q, _, g in self._squadre.get(team_id, ()) if q < prima][-PARTITE_SQUADRA:]
        return (
            (sum(recenti) + PARTITE_DI_PRIOR_SQUADRA * media)
            / (len(recenti) + PARTITE_DI_PRIOR_SQUADRA)
        )

    def partite_recenti(self, team_id: int, prima: datetime) -> list[int]:
        """Le ultime partite della squadra col tabellino, prima di ``prima``."""
        return [
            fid for q, fid, _ in self._squadre.get(team_id, ()) if q < prima
        ][-PARTITE_RECENTI:]

    # ---- i candidati --------------------------------------------------------

    def candidati(
        self,
        team_id: int,
        prima: datetime,
        titolari: Iterable[Any] | None = None,
        assenti: Iterable[int] = (),
    ) -> list[Candidato]:
        """Chi puo' segnare per ``team_id`` in una partita che comincia a ``prima``.

        ``titolari`` sono oggetti con ``player_id``, ``nome`` e ``posizione``
        (una riga di formazione va bene cosi' com'e'). Senza, si guarda chi ha
        giocato nelle ultime partite, e ``assenti`` toglie chi e' fuori per
        certo — solo le assenze certe: un dubbio non e' un'assenza.
        """
        abituali = self.gol_abituali(team_id, prima)
        if titolari is not None:
            voci = [
                (t.player_id, t.nome, getattr(t, "posizione", None),
                 self.minuti_da_titolare(t.player_id, prima))
                for t in titolari
            ]
        else:
            recenti = self.partite_recenti(team_id, prima)
            fuori = set(assenti)
            minuti: dict[int, float] = defaultdict(float)
            for fid in recenti:
                for pid, m in self._minuti_in_partita[(fid, team_id)].items():
                    minuti[pid] += m
            voci = [
                (pid, self._nomi.get(pid, str(pid)), None, totale / len(recenti))
                for pid, totale in minuti.items()
                if pid not in fuori and self.squadra_attuale(pid, prima) == team_id
            ]

        out: list[Candidato] = []
        for pid, nome, ruolo, minuti_attesi in voci:
            if ruolo not in RUOLI:
                ruolo = self.ruolo(pid, prima)
            tasso = self.tasso(pid, ruolo, prima)
            quota = tasso * minuti_attesi / 90.0 / abituali if abituali > 0 else 0.0
            out.append(Candidato(
                player_id=pid,
                nome=nome,
                team_id=team_id,
                ruolo=ruolo,
                minuti_attesi=minuti_attesi,
                tasso_90=tasso,
                quota=min(max(quota, 0.0), QUOTA_MASSIMA),
            ))
        return out


def probabili_marcatori(
    matrice: np.ndarray, lato: str, candidati: Iterable[Candidato]
) -> list[Marcatore]:
    """La probabilita' di segnare di ogni candidato, dal piu' probabile.

    ``lato`` e' 'home' o 'away': da' la riga o la colonna della matrice da cui
    si leggono i gol della sua squadra.
    """
    righe = [Marcatore(c, player_scores(matrice, lato, c.quota)) for c in candidati]
    return sorted(righe, key=lambda m: m.probabilita, reverse=True)
