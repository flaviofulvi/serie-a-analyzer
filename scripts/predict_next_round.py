"""Pronostici sulla prossima giornata, dai dati reali di API-Football.

Percorso completo dalla fonte dati al pronostico:

    scarica le stagioni -> stima il modello -> matrice dei risultati -> esiti

Costa una richiesta per stagione configurata, e zero alle esecuzioni successive
finche' la cache resta valida.

    python scripts/predict_next_round.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from serie_a_analyzer.config import ConfigError, build_client, load_settings
from serie_a_analyzer.data.ingest import fit_from_api, next_matchday

logging.basicConfig(level=logging.INFO, format="  %(message)s")
PROB = "Probabilita'"


def separatore(titolo: str) -> None:
    print(f"\n{'=' * 72}\n{titolo}\n{'=' * 72}")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n[configurazione] {exc}\n")
        return 1

    client = build_client(settings)

    separatore("1. Ingestione")
    print(f"Stagioni richieste: {', '.join(str(s) for s in settings.seasons)}")
    print(f"Budget disponibile oggi: {client.budget.remaining}/{settings.daily_request_limit}\n")

    try:
        modello, training = fit_from_api(client, settings.seasons, league=settings.league_id)
    except RuntimeError as exc:
        print(f"\n[ingestione] {exc}\n")
        return 1

    print(f"\nPartite concluse raccolte: {training.n_finished}")
    print(f"Squadre riconosciute:      {len(training.team_names)}")
    print(f"Richieste spese ora:       {training.requests_spent}")
    print(f"Budget residuo:            {client.budget.remaining}")

    separatore("2. Forza stimata delle squadre")
    print(f"{'#':<4}{'Squadra':<24}{'Attacco':>10}{'Difesa':>10}{'Netta':>10}")
    print("-" * 58)
    for posizione, (team, att, dif, netta) in enumerate(modello.strength_table(), start=1):
        print(f"{posizione:<4}{team:<24}{att:>+10.3f}{dif:>+10.3f}{netta:>+10.3f}")

    print(f"\nFattore campo: {modello.home_advantage:+.3f}   "
          f"rho: {modello.rho:+.3f}   "
          f"campione efficace: {modello.effective_sample_size:.0f} partite")

    partite = next_matchday(training)
    if not partite:
        separatore("3. Prossima giornata")
        print("Nessuna partita in programma nei dati scaricati.")
        print("Se la stagione in corso non e' fra SEASONS, aggiungila nel file .env.")
        return 0

    separatore(f"3. Prossima giornata — {len(partite)} partite")
    for f in partite:
        casa = training.team_names.get(f.home_team_id, str(f.home_team_id))
        ospite = training.team_names.get(f.away_team_id, str(f.away_team_id))

        try:
            p = modello.predict(casa, ospite)
        except KeyError:
            # Neopromossa mai vista nelle stagioni di addestramento.
            print(f"\n{casa} - {ospite}: squadra non presente nello storico, saltata.")
            continue

        lam, mu = modello.expected_goals(casa, ospite)
        quando = f.kickoff_utc.strftime("%d/%m %H:%M")
        risultato, prob_risultato = p.top_scorelines[0]

        print(f"\n{quando} UTC   {casa} - {ospite}")
        print(f"   gol attesi   {lam:.2f} - {mu:.2f}")
        print(f"   1X2          1 {p.home_win:>5.1%}   X {p.draw:>5.1%}   2 {p.away_win:>5.1%}")
        print(f"   Over 2.5     {p.over[2.5]:>5.1%}      Gol/NoGol  {p.btts:>5.1%}")
        print(f"   risultato    {risultato[0]}-{risultato[1]} ({prob_risultato:.1%})")

    separatore("4. Budget")
    print(f"Usate oggi: {client.budget.used}/{settings.daily_request_limit}")
    print(f"Per priorita': {client.budget.usage_by_priority()}")
    print(f"Cache: {client.cache.stats()['entries']} risposte salvate")
    print("\nLe prossime esecuzioni leggono dalla cache e non spendono richieste.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
