"""Ricompra una stagione passata, quando il piano lo consente.

Sul piano gratuito le stagioni fuori dal triennio 2022-2024 non sono
leggibili, e fra lo storico e l'inizio della raccolta giornaliera resta un
buco. Con un piano a pagamento quel buco si chiude, e questo e' il comando che
lo chiude.

Dentro il triennio, pero', lo script funziona anche gratis: verificato il
22/08/2026, i dettagli per singola partita del 2022 e del 2024 rispondono
(formazioni con allenatore, tabellini con minuti e voti, eventi). Serve a
riempire di dettaglio stagioni di cui abbiamo solo i risultati — ~1140
richieste a stagione, quindi con `--max-richieste` e molta pazienza. Vedi il
lavoro 5 in LAVORI.txt per i tempi veri.

    python scripts/riempi_storico.py --solo-verifica   # cosa e' possibile, 2 richieste
    python scripts/riempi_storico.py                   # la scorsa stagione, tutta
    python scripts/riempi_storico.py --stagioni 2025,2021
    python scripts/riempi_storico.py --no-dettagli     # solo partite e risultati
    python scripts/riempi_storico.py --solo players    # solo i tabellini, un terzo del prezzo
    python scripts/riempi_storico.py --max-richieste 500

I tre dettagli non valgono la stessa cosa e costano ~380 richieste ciascuno
per stagione: `players` (minuti, gol, assist, tiri) e' l'unico che possa
cambiare un pronostico, `lineups` serve all'allenatore e alla pagina della
partita, `events` solo a dire *quando* si e' segnato. Con `--solo players` una
stagione costa 381 richieste invece di 1141, e il resto si compra dopo: gli
endpoint per singola partita non scadono.

Vale anche per la **stagione in corso**: `--stagioni 2026` la scarica in blocco,
cosa che il piano gratuito non permette, e recupera cosi' anche i giorni che la
raccolta giornaliera avesse mancato. L'archivio della raccolta resta comunque
quello che vince, perche' e' l'unico aggiornato ogni pomeriggio.

Si puo' interrompere e rilanciare: riprende da dove si era fermato, saltando
cio' che ha gia'. Le partite si scaricano per prime — una richiesta per
stagione — perche' sono l'ossatura da cui tutto il resto si recupera dopo.

Uscita 0 se e' andato a buon fine (anche se resta lavoro arretrato), 1 se il
piano non copre la stagione o l'API ha risposto con un errore.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from serie_a_analyzer.config import ConfigError, build_client, load_settings
from serie_a_analyzer.data.daily import DETAIL_KINDS, DETTAGLI_NOTI
from serie_a_analyzer.data.ingest import current_season
from serie_a_analyzer.data.storico import (
    PAUSA_PREDEFINITA,
    dettagli_in_sospeso,
    leggi_piano,
    riempi_stagione,
    scegli_tipi_dettaglio,
    stima_richieste,
    store_stagione,
)

logging.basicConfig(level=logging.WARNING, format="  %(message)s")


def separatore(titolo: str) -> None:
    print(f"\n{'=' * 72}\n{titolo}\n{'=' * 72}")


def _stagioni(argomento: str | None) -> list[int]:
    """Le stagioni da riempire. Senza argomento, quella appena conclusa.

    E' il caso normale: si compra il piano proprio per l'anno che manca fra lo
    storico e la raccolta giornaliera.
    """
    if not argomento:
        return [current_season() - 1]
    return [int(p.strip()) for p in argomento.split(",") if p.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Riempimento di una stagione passata "
                    "(fuori dal triennio 2022-2024 richiede un piano a pagamento)"
    )
    parser.add_argument(
        "--stagioni",
        default=None,
        help="anni separati da virgola, es. 2025,2021 (default: la scorsa stagione)",
    )
    parser.add_argument(
        "--solo-verifica",
        action="store_true",
        help="dice cosa e' possibile senza riempire niente (spende 1-2 richieste)",
    )
    parser.add_argument(
        "--no-dettagli",
        action="store_true",
        help="solo partite e risultati: niente formazioni, tabellini, eventi",
    )
    parser.add_argument(
        "--solo",
        default=None,
        metavar="TIPI",
        help="quali dettagli comprare, separati da virgola fra "
             + ", ".join(DETTAGLI_NOTI)
             + f" (default: {', '.join(DETAIL_KINDS)}). `--solo players` costa "
               "un terzo; `statistics` porta l'xG e non e' fra i predefiniti",
    )
    parser.add_argument(
        "--max-richieste",
        type=int,
        default=None,
        help="tetto di richieste da spendere in dettagli, per stagione",
    )
    parser.add_argument(
        "--pausa",
        type=float,
        default=PAUSA_PREDEFINITA,
        help=f"secondi fra una richiesta e l'altra (default {PAUSA_PREDEFINITA}: "
             "i piani a pagamento hanno anche un limite al minuto)",
    )
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n[configurazione] {exc}\n")
        return 1

    client = build_client(settings)
    stagioni = _stagioni(args.stagioni)
    try:
        tipi = scegli_tipi_dettaglio(args.solo)
    except ValueError as exc:
        print("")
        print(f"[--solo] {exc}")
        print("")
        return 1

    # --- che piano abbiamo davvero ------------------------------------------
    #
    # Prima domanda e prima richiesta. Una stagione non coperta non da' errore:
    # risponde con zero partite, che e' indistinguibile da un archivio vuoto.
    # `/status` invece lo dice in chiaro.
    separatore("PIANO SOTTOSCRITTO")
    piano = leggi_piano(client)
    if piano.errore:
        print(f"Non sono riuscito a leggere lo stato del piano: {piano.errore}")
        return 1

    print(f"Piano:              {piano.piano or 'sconosciuto'}"
          f"{'' if piano.attivo else '  (NON attivo)'}")
    if piano.scadenza:
        print(f"Scade il:           {piano.scadenza}")
    print(f"Richieste al giorno: {piano.limite_giornaliero}"
          f"  (usate oggi lato API: {piano.usate_oggi})")
    print(f"Limite configurato:  {settings.daily_request_limit}  (DAILY_REQUEST_LIMIT nel .env)")

    if not piano.a_pagamento:
        print(
            "\nIl piano concede ancora 100 richieste al giorno, cioe' e' quello\n"
            "gratuito: le stagioni fuori dal triennio 2022-2024 non sono leggibili.\n"
            "Se hai appena comprato un piano superiore, puo' volerci qualche minuto\n"
            "perche' diventi attivo: riprova fra poco."
        )
        return 1

    if piano.limite_giornaliero and piano.limite_giornaliero != settings.daily_request_limit:
        # Il contatore locale e' l'unica difesa contro lo sforare: se e' tarato
        # su un piano che non c'e' piu', il riempimento si fermerebbe dopo
        # cento richieste credendo di aver finito il budget.
        print(
            f"\nATTENZIONE: il piano concede {piano.limite_giornaliero} richieste al giorno\n"
            f"ma il contatore locale e' fermo a {settings.daily_request_limit}. Metti nel .env:\n"
            f"\n    DAILY_REQUEST_LIMIT={piano.limite_giornaliero}\n\n"
            "e rilancia. Senza, il riempimento si ferma molto prima del necessario."
        )
        return 1

    # --- cosa costa ----------------------------------------------------------
    separatore("STAGIONI DA RIEMPIRE")
    print(f"Archivio di destinazione: {settings.storico_dir}")
    print(f"Budget disponibile oggi:  {client.budget.remaining}/{settings.daily_request_limit}")
    print(f"Stagioni richieste:       {', '.join(str(s) for s in stagioni)}")
    print(f"Dettagli richiesti:       {', '.join(tipi)}"
          + ("" if len(tipi) == len(DETAIL_KINDS) else "  (gli altri si comprano dopo)"))
    print(
        f"\nCosto pieno di una stagione: ~{stima_richieste(380, tipi)} richieste "
        f"(1 per il calendario, {len(tipi)} per ogni partita conclusa).\n"
        "Si puo' interrompere in qualsiasi momento: il giro dopo riprende da qui."
    )

    if args.solo_verifica:
        separatore("SOLA VERIFICA")
        for stagione in stagioni:
            store = store_stagione(settings.storico_dir, stagione)
            rapporto = riempi_stagione(
                client, store, stagione,
                league=settings.league_id,
                con_dettagli=False,
                tipi_dettaglio=tipi,
            )
            if rapporto.errore:
                print(f"{stagione}: {rapporto.errore}")
                continue
            print(
                f"{stagione}: {rapporto.partite} partite ({rapporto.concluse} concluse), "
                f"mancano {rapporto.in_sospeso} richieste di dettaglio"
            )
        print(
            "\nIl calendario scaricato adesso resta in archivio: il riempimento\n"
            "vero non lo ricomprera'."
        )
        return 0

    # --- riempimento ---------------------------------------------------------
    esito = 0
    for stagione in stagioni:
        separatore(f"STAGIONE {stagione}")
        store = store_stagione(settings.storico_dir, stagione)
        rapporto = riempi_stagione(
            client, store, stagione,
            league=settings.league_id,
            con_dettagli=not args.no_dettagli,
            max_richieste=args.max_richieste,
            pausa=args.pausa,
            tipi_dettaglio=tipi,
        )

        if rapporto.errore:
            print(f"NON riempita: {rapporto.errore}")
            esito = 1
            continue

        print(f"Partite in archivio:  {rapporto.partite} ({rapporto.concluse} concluse)")
        print(f"Nuove / aggiornate:   {rapporto.nuove} / {rapporto.aggiornate}")
        if rapporto.dettagli:
            print("Dettagli scaricati:   "
                  + ", ".join(f"{k} {v}" for k, v in sorted(rapporto.dettagli.items())))
        print(f"Richieste spese:      {rapporto.richieste}")

        if rapporto.completa and rapporto.parziale_per_scelta:
            # "Completa" qui vuol dire "completa per cio' che si e' chiesto".
            # Dirlo e' l'unico modo di non far credere finita una stagione a
            # cui mancano ancora due endpoint su tre.
            mancanti = [
                f"{k} ({quanti})"
                for k in DETTAGLI_NOTI if k not in rapporto.tipi
                for quanti in [dettagli_in_sospeso(store, (k,))] if quanti
            ]
            print("")
            print(f"Stagione completa per {', '.join(rapporto.tipi)}: non ci sono piu' buchi.")
            if mancanti:
                print(f"Restano da comprare {', '.join(mancanti)}, se e quando")
                print("serviranno: i dettagli si leggono dall'id, e l'id non scade.")
            else:
                print("E non manca nient'altro: la stagione ha tutti i dettagli.")
        elif rapporto.completa:
            print("\nStagione completa: partite e dettagli non hanno piu' buchi.")
        else:
            print(
                f"\nRestano {rapporto.in_sospeso} richieste di dettaglio.\n"
                "Rilancia lo stesso comando (anche domani, quando il budget si azzera):\n"
                "riprende da qui senza ricomprare niente."
            )

    # --- riepilogo -----------------------------------------------------------
    separatore("RIEPILOGO")
    for stagione in stagioni:
        store = store_stagione(settings.storico_dir, stagione)
        s = store.stats()
        print(f"{stagione}: {s['partite']} partite, {s['con_dettagli']} con dettagli, "
              f"{dettagli_in_sospeso(store, tipi)} richieste ancora da fare"
              + ("" if len(tipi) == len(DETAIL_KINDS)
                 else f" di {', '.join(tipi)} ({dettagli_in_sospeso(store)} contando tutto)"))
    print(f"\nBudget usato oggi: {client.budget.used}/{settings.daily_request_limit}")
    print(
        "\nLe stagioni riempite entrano nel modello da sole: il sito se ne accorge\n"
        "alla prima pagina aperta, senza riavvii e senza toccare il .env."
    )
    return esito


if __name__ == "__main__":
    raise SystemExit(main())
