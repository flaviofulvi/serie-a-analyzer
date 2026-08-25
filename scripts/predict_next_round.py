"""Pronostici sulla prossima giornata, dal terminale.

Mostra le stesse identiche cose che mostra l'hub del sito, e non e' una
coincidenza: apre lo stesso motore (`web/engine.py`). E' l'unico modo per cui
questo script e la pagina non possano divergere.

    python scripts/predict_next_round.py

Perche' conti. Fino al 25 agosto 2026 questo script stimava il modello per
conto suo, da una strada piu' vecchia che leggeva **solo le stagioni in
cache** (`SEASONS` nel .env): ignorava le stagioni ricomprate, quella
importata da file e la raccolta giornaliera, cioe' la stagione in corso.
Lavorava su ~1140 partite invece di 1531 e si fermava al 2024 — dava numeri
diversi da quelli in pagina, e niente lo diceva. Uno strumento che risponde
sempre, e a volte in modo diverso dalla fonte ufficiale, e' peggio di uno
strumento che manca.

Non spende richieste: legge dall'archivio locale e dalla cache, come il sito.
Come il sito, congela anche i pronostici delle partite in programma — e' lo
stesso motore, quindi fa le stesse cose, ed e' proprio cio' che si vuole.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADICE / "src"))

from serie_a_analyzer.config import ConfigError  # noqa: E402
from serie_a_analyzer.web.engine import Engine  # noqa: E402

logging.basicConfig(level=logging.INFO, format="  %(message)s")


def separatore(titolo: str) -> None:
    print(f"\n{'=' * 72}\n{titolo}\n{'=' * 72}")


def main() -> int:
    try:
        motore = Engine()
    except ConfigError as exc:
        print(f"\n[configurazione] {exc}\n")
        return 1

    snap = motore.snapshot()
    salute = motore.health()

    separatore("1. Su cosa si basa")
    if snap.error:
        print(f"\n[dati] {snap.error}\n")
        return 1
    print(f"Partite conosciute:   {salute['partite_totali']}")
    print(f"Squadre riconosciute: {salute['squadre']}")
    print(f"Partite nella stima:  {salute['modello_partite']}")
    print("\nDa dove arrivano. Le sorgenti si SOVRAPPONGONO - una stagione")
    print("ricomprata rimpiazza la stessa stagione letta dalla cache - quindi")
    print("questi numeri non si sommano al totale qui sopra:")
    print(f"  dalla cache          {salute['partite_storiche']:>5}")
    print(f"  stagioni ricomprate  {salute['partite_stagioni_riempite']:>5}  "
          f"{salute['stagioni_riempite']}")
    print(f"  stagioni importate   {salute['partite_stagioni_importate']:>5}  "
          f"{salute['stagioni_importate']}")
    print(f"  raccolta giornaliera {salute['partite_stagione_corrente']:>5}")

    modello = snap.model
    separatore("2. Forza stimata delle squadre")
    print(f"{'#':<4}{'Squadra':<24}{'Attacco':>10}{'Difesa':>10}{'Netta':>10}")
    print("-" * 58)
    for posizione, (team, att, dif, netta) in enumerate(modello.strength_table(), start=1):
        print(f"{posizione:<4}{team:<24}{att:>+10.3f}{dif:>+10.3f}{netta:>+10.3f}")

    print(f"\nFattore campo: {modello.home_advantage:+.3f}   "
          f"rho: {modello.rho:+.3f}   "
          f"campione efficace: {modello.effective_sample_size:.0f} partite")

    partite = snap.dataset.next_matchday()
    if not partite:
        separatore("3. Prossima giornata")
        print("Nessuna partita in programma fra quelle conosciute.")
        print("Se la raccolta giornaliera e' ferma, il calendario non arriva:")
        print("controlla con scripts/verifica_raccolta.py.")
        return 0

    separatore(f"3. Prossima giornata - {len(partite)} partite")
    print("Sono le stesse cifre dell'hub: prima del fischio d'inizio il")
    print("pronostico viene dal modello caldo, che e' quello qui sopra.")
    for f in partite:
        casa = snap.dataset.name(f.home_team_id)
        ospite = snap.dataset.name(f.away_team_id)

        p = motore.predict(f.home_team_id, f.away_team_id)
        if p is None:
            # Neopromossa senza storia: non si inventa un pronostico.
            print(f"\n{casa} - {ospite}: il modello non conosce almeno una delle due.")
            continue

        lam, mu = motore.expected_goals(f.home_team_id, f.away_team_id)
        quando = f.kickoff_utc.strftime("%d/%m %H:%M")
        risultato, prob_risultato = p.top_scorelines[0]

        print(f"\n{quando} UTC   {casa} - {ospite}")
        print(f"   gol attesi   {lam:.2f} - {mu:.2f}")
        print(f"   1X2          1 {p.home_win:>5.1%}   X {p.draw:>5.1%}   2 {p.away_win:>5.1%}")
        print(f"   Over 2.5     {p.over[2.5]:>5.1%}      Gol/NoGol  {p.btts:>5.1%}")
        print(f"   risultato    {risultato[0]}-{risultato[1]} ({prob_risultato:.1%})")

    separatore("4. Budget")
    print(f"Usate oggi: {salute['budget_usato']}/{salute['budget_limite']}")
    print("Questo script non ne spende: legge dall'archivio locale e dalla cache.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
