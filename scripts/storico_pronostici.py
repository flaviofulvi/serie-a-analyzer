"""Cosa c'e' nello storico dei pronostici. Non spende richieste.

Un archivio che nessuno guarda e' un archivio di cui non ci si accorge quando
smette di riempirsi. Questo script lo apre e lo mostra:

    python scripts/storico_pronostici.py              # gli ultimi pronostici
    python scripts/storico_pronostici.py --partita 1234567   # come e' cambiato

La seconda forma e' la piu' interessante: mostra tutti i pronostici mai fatti
su una partita, dal piu' recente al piu' vecchio. Se il modello ha imparato
qualcosa avvicinandosi al fischio d'inizio, si vede li'.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADICE / "src"))

from serie_a_analyzer.config import ConfigError, build_prediction_store, load_settings  # noqa: E402


def riga_1x2(store, prediction_id: int) -> str:
    quote = {
        m["selection"]: m["probability"]
        for m in store.mercati(prediction_id) if m["market"] == "1x2"
    }
    if not quote:
        return ""
    return (f"1 {quote['home']:5.1%}   X {quote['draw']:5.1%}   2 {quote['away']:5.1%}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Storico dei pronostici")
    parser.add_argument("--partita", type=int, help="id della partita da seguire nel tempo")
    parser.add_argument("--quanti", type=int, default=15, help="quanti pronostici elencare")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n[configurazione] {exc}\n")
        return 1

    store = build_prediction_store(settings)
    conteggi = store.conteggi()

    print(f"\nArchivio: {settings.predictions_db}")
    print(f"  {conteggi['pronostici']} pronostici, "
          f"{conteggi['versioni_modello']} versioni del modello, "
          f"{conteggi['mercati']} esiti derivati, "
          f"{conteggi['esiti_risolti']} risolti")

    if conteggi["pronostici"] == 0:
        print("\nNessun pronostico registrato. Se ne producono aprendo l'applicazione")
        print("web con almeno una partita in programma in archivio.\n")
        return 0

    if args.partita:
        storico = store.storico(args.partita)
        if not storico:
            print(f"\nNessun pronostico per la partita {args.partita}.\n")
            return 1
        print(f"\nPartita {args.partita} — {len(storico)} pronostici, dal piu' recente:\n")
        for p in storico:
            print(f"  {p['created_at']}  [{p['mode']}]  "
                  f"gol attesi {p['lambda_home']:.2f}-{p['lambda_away']:.2f}   "
                  f"{riga_1x2(store, p['id'])}")
        print()
        return 0

    print(f"\nUltimi {args.quanti} pronostici:\n")
    for p in store.ultimi(args.quanti):
        print(f"  {p['created_at']}  partita {p['fixture_id']:>8}  [{p['mode']}]  "
              f"{riga_1x2(store, p['id'])}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
