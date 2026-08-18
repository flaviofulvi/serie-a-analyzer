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

Non consuma richieste API: legge dalla cache riempita dall'ingestione.

    python scripts/backtest.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass
from math import log
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from serie_a_analyzer.config import ConfigError, build_client, load_settings
from serie_a_analyzer.data.ingest import collect_seasons
from serie_a_analyzer.data.parsers import FixtureRecord
from serie_a_analyzer.model.dixon_coles import fit_dixon_coles
from serie_a_analyzer.model.markets import MatchProbabilities

# Stagione da rigiocare. Le precedenti servono da storico iniziale.
TARGET_SEASON = 2024

# Sotto questa soglia di partite il modello non ha abbastanza storia perche'
# il confronto sia informativo.
MIN_TRAINING_MATCHES = 200


@dataclass
class Prediction:
    """Un pronostico e cio' che e' realmente accaduto."""

    matchday: int
    home: str
    away: str
    probs: MatchProbabilities
    home_goals: int
    away_goals: int

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


def run_backtest(seasons, league: int, client) -> tuple[list[Prediction], dict]:
    """Rigioca ``TARGET_SEASON`` giornata per giornata. Zero richieste API."""
    training = collect_seasons(client, seasons, league)
    nomi = training.team_names

    per_giornata: dict[int, list[FixtureRecord]] = defaultdict(list)
    tutte: list[FixtureRecord] = []
    for s in training.seasons:
        tutte.extend(s.fixtures)
        if s.season == TARGET_SEASON:
            for f in s.fixtures:
                if f.is_finished and f.matchday is not None:
                    per_giornata[f.matchday].append(f)

    if not per_giornata:
        raise RuntimeError(
            f"stagione {TARGET_SEASON} assente dai dati. "
            f"Verifica SEASONS nel file .env."
        )

    concluse = sorted(
        (f for f in tutte if f.is_finished), key=lambda f: f.kickoff_utc
    )

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

        modello = fit_dixon_coles(
            [
                _to_match(f, nomi)
                for f in storico
            ],
            as_of=taglio.date(),
        )
        giornate_usate += 1

        for f in partite:
            casa = nomi.get(f.home_team_id, str(f.home_team_id))
            ospite = nomi.get(f.away_team_id, str(f.away_team_id))
            try:
                probs = modello.predict(casa, ospite)
            except KeyError:
                # Neopromossa che non ha ancora giocato: nessuna storia da cui
                # stimarla. Escluderla e' piu' onesto che inventarle una forza.
                saltate += 1
                continue
            predizioni.append(
                Prediction(
                    matchday=giornata,
                    home=casa,
                    away=ospite,
                    probs=probs,
                    home_goals=f.home_goals,
                    away_goals=f.away_goals,
                )
            )

    return predizioni, {
        "saltate": saltate,
        "giornate": giornate_usate,
        "richieste": training.requests_spent,
    }


def _to_match(f: FixtureRecord, nomi: dict[int, str]):
    from serie_a_analyzer.model.dixon_coles import Match

    return Match(
        home=nomi.get(f.home_team_id, str(f.home_team_id)),
        away=nomi.get(f.away_team_id, str(f.away_team_id)),
        home_goals=f.home_goals,
        away_goals=f.away_goals,
        played_on=f.played_on,
    )


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


# ---- presentazione ---------------------------------------------------------


def titolo(testo: str) -> None:
    print(f"\n{'=' * 74}\n{testo}\n{'=' * 74}")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n[configurazione] {exc}\n")
        return 1

    client = build_client(settings)
    prima = client.budget.used

    titolo(f"BACKTEST WALK-FORWARD - stagione {TARGET_SEASON}/{str(TARGET_SEASON + 1)[2:]}")
    print("Ogni giornata e' pronosticata con un modello che non ha mai visto")
    print("nemmeno una partita successiva al suo calcio d'inizio.\n")

    preds, info = run_backtest(settings.seasons, settings.league_id, client)
    if not preds:
        print("Nessun pronostico prodotto.")
        return 1

    print(f"Partite pronosticate: {len(preds)}")
    print(f"Giornate rigiocate:   {info['giornate']}")
    print(f"Partite escluse:      {info['saltate']} (storico insufficiente)")
    print(f"Richieste API spese:  {client.budget.used - prima}")

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
