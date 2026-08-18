"""Quanto valgono i pronostici gia' verificati dalla realta'.

A differenza del backtest — che rigioca il passato con il modello di oggi —
qui si guardano solo pronostici **congelati prima** del fischio d'inizio e poi
confrontati col risultato vero. E' la misura onesta, l'unica che non puo'
essere gonfiata senza accorgersene, e all'inizio della stagione sara'
inevitabilmente povera: pochi pronostici, numeri che ballano.

    python scripts/accuratezza.py
    python scripts/accuratezza.py --mercato btts --selezione yes

Prima di stampare, il rapporto risolve gli esiti rimasti in sospeso: apre il
motore esattamente come farebbe il sito. Legge dalla cache e dall'archivio
locale, quindi in condizioni normali non spende richieste.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADICE / "src"))

from serie_a_analyzer.config import ConfigError  # noqa: E402
from serie_a_analyzer.data.outcomes import (  # noqa: E402
    SOGLIA_SIGNIFICATIVITA,
    accuratezza_per_mercato,
    accuratezza_per_selezione,
    brier_1x2_multiclasse,
    calibrazione,
)
from serie_a_analyzer.web.engine import Engine  # noqa: E402

ETICHETTE = {
    "1x2": "Esito finale (1X2)",
    "doppia_chance": "Doppia chance",
    "over_under": "Over/Under",
    "btts": "Gol / NoGol",
    "clean_sheet": "Porta inviolata",
    "risultato_esatto": "Risultato esatto",
}


def titolo(testo: str) -> None:
    print(f"\n{'=' * 72}\n{testo}\n{'=' * 72}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Accuratezza dei pronostici")
    parser.add_argument("--mercato", default="1x2", help="mercato per la calibrazione")
    parser.add_argument("--selezione", default="home", help="selezione per la calibrazione")
    parser.add_argument("--bin", type=int, default=5, help="fasce della calibrazione")
    parser.add_argument("--dettaglio", action="store_true",
                        help="scompone ogni mercato nelle sue singole selezioni")
    args = parser.parse_args()

    try:
        motore = Engine()
    except ConfigError as exc:
        print(f"\n[configurazione] {exc}\n")
        return 1

    titolo("ACCURATEZZA DEI PRONOSTICI")
    print("Solo pronostici registrati prima del fischio d'inizio.")

    # Costruire lo snapshot risolve da solo gli esiti rimasti in sospeso.
    motore.snapshot()
    store = motore.pronostici
    conteggi = store.conteggi()
    print(f"\nStorico: {conteggi['pronostici']} pronostici, "
          f"{conteggi['esiti_risolti']} esiti gia' confrontati col risultato.")

    per_mercato = accuratezza_per_mercato(store)
    if not per_mercato:
        print("\nNessun pronostico e' ancora stato verificato da una partita conclusa.")
        print("Succedera' da solo: i pronostici si registrano quando il modello viene")
        print("stimato, gli esiti quando arrivano i risultati della raccolta giornaliera.\n")
        return 0

    titolo("Per mercato")
    print(f"{'Mercato':<24}{'esiti':>7}{'Brier':>10}{'baseline':>11}"
          f"{'accaduto':>11}{'previsto':>11}  verdetto")
    print("-" * 88)
    for v in per_mercato:
        if not v["significativo"]:
            verdetto = f"pochi dati (<{SOGLIA_SIGNIFICATIVITA})"
        elif v["batte_baseline"]:
            verdetto = "meglio della baseline"
        else:
            verdetto = "PEGGIO della baseline"
        print(f"{ETICHETTE.get(v['mercato'], v['mercato']):<24}{v['n']:>7}"
              f"{v['brier']:>10.4f}{v['baseline']:>11.4f}"
              f"{v['tasso_reale']:>10.1%}{v['probabilita_media']:>11.1%}  {verdetto}")

    print("\nBrier piu' basso = meglio. La baseline e' il punteggio di chi ripete")
    print("sempre la frequenza media di quell'esito senza guardare le squadre:")
    print("un mercato che non la batte non sta aggiungendo informazione.")

    if args.dettaglio:
        titolo("Per singolo esito")
        print("E' il livello a cui la baseline ha davvero senso: Over 0.5 e Over 4.5")
        print("hanno frequenze opposte, e sommarle nasconde quale delle due funziona.\n")
        print(f"{'Mercato':<20}{'Esito':<16}{'n':>7}{'Brier':>10}{'baseline':>11}{'accaduto':>11}")
        print("-" * 75)
        for v in accuratezza_per_selezione(store):
            print(f"{ETICHETTE.get(v['mercato'], v['mercato']):<20}{v['selezione']:<16}"
                  f"{v['n']:>7}{v['brier']:>10.4f}{v['baseline']:>11.4f}"
                  f"{v['tasso_reale']:>10.1%}")

    multi = brier_1x2_multiclasse(store)
    if multi:
        print(f"\n1X2 contato per partita, come nel backtest: {multi['brier']:.4f} "
              f"su {multi['n_partite']} partite "
              f"(tirare a caso vale {multi['baseline_caso']:.4f}).")

    fasce = calibrazione(store, market=args.mercato, selection=args.selezione, n_bin=args.bin)
    if fasce:
        titolo(f"Calibrazione — {args.mercato} / {args.selezione}")
        print("Quando il modello dice il 30%, succede il 30% delle volte?\n")
        print(f"{'fascia':<12}{'n':>6}{'previsto':>11}{'reale':>10}{'scarto':>10}")
        print("-" * 49)
        for f in fasce:
            scarto = f["reale"] - f["previsto"]
            print(f"{f['fascia']:<12}{f['n']:>6}{f['previsto']:>11.1%}"
                  f"{f['reale']:>10.1%}{scarto:>+10.1%}")
        print("\nScarti piccoli = fiducia ben tarata. Con poche partite per fascia")
        print("il rumore domina: e' normale all'inizio della stagione.")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
