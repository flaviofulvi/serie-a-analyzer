"""Raccolta giornaliera della Serie A. Da eseguire ogni giorno, senza saltarne.

Sul piano gratuito la stagione in corso e' leggibile solo nella finestra
ieri/oggi/domani. Un giorno non raccolto e' perso: non esiste modo di tornare
indietro a prenderlo. Questo script e' quindi l'unico punto in cui l'archivio
della stagione cresce.

    python scripts/daily_collect.py                # la finestra completa
    python scripts/daily_collect.py --no-details   # solo partite e risultati
    python scripts/daily_collect.py --max-detail-requests 20

Uscita 0 se tutto e' andato a buon fine, 1 se almeno un giorno e' fallito: cosi'
uno scheduler puo' accorgersene e riprovare.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from serie_a_analyzer.config import ConfigError, build_client, build_store, load_settings
from serie_a_analyzer.data.daily import backfill_details, collect_window

logging.basicConfig(level=logging.INFO, format="  %(message)s")

# Da quanti giorni indietro cercare buchi nell'archivio.
FINESTRA_CONTROLLO_BUCHI = 30


def separatore(titolo: str) -> None:
    print(f"\n{'=' * 72}\n{titolo}\n{'=' * 72}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Raccolta giornaliera della Serie A")
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="salta formazioni, statistiche giocatori ed eventi",
    )
    parser.add_argument(
        "--max-detail-requests",
        type=int,
        default=None,
        help="tetto di richieste da spendere in dettagli (default: nessuno)",
    )
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n[configurazione] {exc}\n")
        return 1

    client = build_client(settings)
    store, journal = build_store(settings)

    separatore("RACCOLTA GIORNALIERA")
    print(f"Budget disponibile: {client.budget.remaining}/{settings.daily_request_limit}")
    print(f"Archivio:           {settings.store_dir}")

    reports = collect_window(
        client, store, journal,
        league_id=settings.league_id,
        with_details=not args.no_details,
        max_detail_requests=args.max_detail_requests,
    )

    separatore("Esito per giorno")
    print(f"{'giorno':<14}{'Serie A':>9}{'definitive':>12}{'nuove':>8}{'agg.':>7}{'rich.':>7}  stato")
    print("-" * 72)
    errori = 0
    for r in reports:
        if r.error:
            errori += 1
            stato = f"ERRORE: {r.error[:28]}"
        elif r.skipped:
            stato = "gia' chiuso"
        elif r.n_serie_a == 0:
            stato = "nessuna partita"
        elif r.closed:
            stato = "chiuso"
        else:
            stato = "aperto, si ricontrolla"
        print(f"{r.day.isoformat():<14}{r.n_serie_a:>9}{r.n_final:>12}"
              f"{r.new_fixtures:>8}{r.updated_fixtures:>7}{r.requests_spent:>7}  {stato}")

    dettagli: dict[str, int] = {}
    for r in reports:
        for kind, n in r.details.items():
            dettagli[kind] = dettagli.get(kind, 0) + n

    # Recupero dei dettagli rimasti indietro nei giorni precedenti. Non ha la
    # fretta della finestra di tre giorni: gli id delle partite sono gia' al
    # sicuro in archivio, e da quelli i dettagli si leggono quando si vuole.
    if not args.no_details:
        residuo = None
        if args.max_detail_requests is not None:
            residuo = max(0, args.max_detail_requests - sum(dettagli.values()))
        if residuo is None or residuo > 0:
            for kind, n in backfill_details(client, store, max_requests=residuo).items():
                dettagli[kind] = dettagli.get(kind, 0) + n

    if dettagli:
        print("\nDettagli scaricati: " + ", ".join(f"{k} {v}" for k, v in sorted(dettagli.items())))

    # --- buchi nell'archivio ------------------------------------------------
    oggi = datetime.now(timezone.utc).date()
    raccolti = journal.collected_days()
    if raccolti:
        # Si cerca solo da quando la raccolta e' iniziata: i giorni precedenti
        # non sono buchi, semplicemente non ci riguardavano.
        inizio = max(raccolti[0], oggi - timedelta(days=FINESTRA_CONTROLLO_BUCHI))
        buchi = [g for g in journal.missing_days(inizio, oggi) if g <= oggi]
        if buchi:
            separatore("ATTENZIONE: giorni mancanti nell'archivio")
            print("Questi giorni non sono mai stati raccolti e sono fuori dalla")
            print("finestra di tre giorni: non sono piu' recuperabili.\n")
            for g in buchi:
                print(f"  {g.isoformat()}")
            print("\nSe fra questi c'e' una giornata di campionato, le statistiche")
            print("della stagione in corso saranno incomplete.")

    # --- riepilogo ----------------------------------------------------------
    separatore("Archivio")
    s = store.stats()
    print(f"Partite di Serie A in archivio: {s['partite']}")
    print(f"Partite con dettagli:           {s['con_dettagli']}")
    print(f"Giorni raccolti finora:         {len(raccolti)}")
    print(f"Budget usato oggi:              {client.budget.used}/{settings.daily_request_limit}")
    print(f"Per priorita':                  {client.budget.usage_by_priority()}")

    if errori:
        print(f"\n{errori} giorno/i non raccolti: riprovare.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
