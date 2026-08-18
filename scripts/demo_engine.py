"""Dimostrazione del motore statistico su un campionato sintetico.

Non servono credenziali ne' dati reali: genera una stagione da forze note,
stima il modello e mostra un pronostico completo con la sua scomposizione.

    python scripts/demo_engine.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from serie_a_analyzer.model.dixon_coles import fit_dixon_coles
from serie_a_analyzer.model.markets import derive_markets, winning_margin
from serie_a_analyzer.model.simulation import generate_league


# Definite qui perche' l'apostrofo non puo' comparire dentro una f-string
# delimitata da apici singoli.
PROB = "Probabilita'"
TOP_SCORES = "Risultati esatti piu' probabili"


def separatore(titolo: str) -> None:
    print(f"\n{'=' * 66}\n{titolo}\n{'=' * 66}")


def main() -> None:
    separatore("1. Generazione di un campionato sintetico")
    lega = generate_league(n_teams=20, n_rounds=6, seed=7)
    print(f"Partite generate: {len(lega.matches)}  (20 squadre, 6 gironi)")
    print(f"Dalla data {min(m.played_on for m in lega.matches)} "
          f"al {max(m.played_on for m in lega.matches)}")

    separatore("2. Stima del modello")
    modello = fit_dixon_coles(lega.matches, half_life_days=2000.0, regularization=0.5)
    print(f"Convergenza:            {'si' if modello.converged else 'NO'}")
    print(f"Partite usate:          {modello.n_matches}")
    print(f"Campione efficace:      {modello.effective_sample_size:.1f} (dopo decadimento)")
    print(f"Fattore campo stimato:  {modello.home_advantage:+.3f}  (vero {lega.true_home_advantage:+.3f})")
    print(f"Livello gol stimato:    {modello.base:+.3f}  (vero {lega.true_base:+.3f})")
    print(f"rho stimato:            {modello.rho:+.3f}")

    separatore("3. Classifica per forza stimata")
    print(f"{'Squadra':<12}{'Attacco':>10}{'Difesa':>10}{'Netta':>10}   {'vs. forza vera':>16}")
    print("-" * 66)
    tabella = modello.strength_table()
    for team, att, dif, netta in tabella[:5] + [("...", 0, 0, 0)] + tabella[-3:]:
        if team == "...":
            print(f"{'...':<12}")
            continue
        vera = lega.true_attack[team] + lega.true_defense[team]
        print(f"{team:<12}{att:>+10.3f}{dif:>+10.3f}{netta:>+10.3f}   {vera:>+16.3f}")

    # Prendiamo la squadra piu' forte in casa contro una di meta' classifica.
    casa = tabella[0][0]
    ospite = tabella[len(tabella) // 2][0]

    separatore(f"4. Pronostico: {casa} - {ospite}")
    lam, mu = modello.expected_goals(casa, ospite)
    matrice = modello.scoreline(casa, ospite)
    p = derive_markets(matrice)

    print(f"\nGol attesi:  {casa} {lam:.2f}  -  {ospite} {mu:.2f}\n")

    print("Da dove viene il numero (scomposizione su scala logaritmica):")
    print(f"  livello medio del campionato   {modello.base:+.3f}")
    print(f"  attacco {casa:<22} {modello.attack[casa]:+.3f}")
    print(f"  difesa  {ospite:<22} {-modello.defense[ospite]:+.3f}")
    print(f"  fattore campo                  {modello.home_advantage:+.3f}")
    print(f"  {'-' * 46}")
    print(f"  log(gol attesi casa)           {np.log(lam):+.3f}  ->  {lam:.2f} gol")

    print(f"\n{'Esito 1X2':<24}{PROB:>14}")
    print("-" * 38)
    print(f"{'1 (' + casa + ')':<24}{p.home_win:>13.1%}")
    print(f"{'X (pareggio)':<24}{p.draw:>13.1%}")
    print(f"{'2 (' + ospite + ')':<24}{p.away_win:>13.1%}")

    print(f"\n{'Gol totali':<24}{'Over':>10}{'Under':>10}")
    print("-" * 44)
    for line in sorted(p.over):
        print(f"{'linea ' + format(line, '.1f'):<24}{p.over[line]:>10.1%}{p.under[line]:>10.1%}")

    print(f"\n{'Altri esiti':<30}{PROB:>14}")
    print("-" * 44)
    print(f"{'Entrambe segnano':<30}{p.btts:>13.1%}")
    print(f"{'Clean sheet ' + casa:<30}{p.clean_sheet_home:>13.1%}")
    print(f"{'Clean sheet ' + ospite:<30}{p.clean_sheet_away:>13.1%}")
    print(f"{casa + ' vince di 2+':<30}{winning_margin(matrice, 'home', 2):>13.1%}")
    print(f"{casa + ' vince di 3+':<30}{winning_margin(matrice, 'home', 3):>13.1%}")

    print(f"\n{TOP_SCORES:<30}{PROB:>14}")
    print("-" * 44)
    for (h, a), prob in p.top_scorelines:
        print(f"{'  ' + str(h) + ' - ' + str(a):<30}{prob:>13.1%}")

    separatore("5. Matrice dei risultati (%) — la sorgente di tutto quanto sopra")
    print(f"\n{'':>8}" + "".join(f"{g:>7}" for g in range(6)) + "   <- gol " + ospite)
    for h in range(6):
        riga = "".join(f"{matrice[h, a] * 100:>7.2f}" for a in range(6))
        print(f"{h:>8}{riga}")
    print(f"\n(righe = gol {casa}, colonne = gol {ospite}; troncata per leggibilita)")

    print(f"\nSomma dell'intera matrice: {matrice.sum():.6f}")
    print("Ogni esito qui sopra e' una somma di celle di questa matrice.")


if __name__ == "__main__":
    main()
