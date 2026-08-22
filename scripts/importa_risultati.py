"""Importa i risultati di una stagione da un file, senza spendere richieste.

Fra lo storico 2022-2024 e l'inizio della raccolta giornaliera manca una
stagione intera, che il piano gratuito non lascia leggere. `riempi_storico.py`
la ricompra con ~1140 richieste; questo comando la prende da un file di
risultati scaricato altrove e costa **zero**.

    python scripts/importa_risultati.py serie-a-2025-UTC.csv --solo-verifica
    python scripts/importa_risultati.py serie-a-2025-UTC.csv

Il primo comando non scrive niente: legge il file, aggancia le squadre ai loro
id e dice cosa entrerebbe in archivio. Il secondo lo scrive davvero, in
`data/importato/<stagione>/`.

Cosa entra e cosa no: un file di risultati ha date, squadre e gol, che sono
esattamente cio' che il modello usa per stimare le forze. Non ha formazioni,
tabellini ne' voti — le schede giocatore di quella stagione resteranno vuote,
e l'unico modo di riempirle e' comprarla davvero.

La stagione si ricava dal nome del file quando possibile ("serie-a-2025" ->
2025), altrimenti va detta con --stagione. E' l'anno di **partenza**: la
2025/26 e' la stagione 2025.

Uscita 0 se l'importazione e' andata a buon fine, 1 se il file ha problemi che
la rendono insicura.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from serie_a_analyzer.config import ConfigError, build_client, build_store, load_settings
from serie_a_analyzer.data.dataset import load_dataset
from serie_a_analyzer.data.importato import (
    FileIllegibile,
    importa,
    prepara,
    sorgenti_nomi,
    squadre_viste,
    store_stagione,
)

logging.basicConfig(level=logging.WARNING, format="  %(message)s")

# Un anno a quattro cifre dentro il nome del file: "serie-a-2025-UTC.csv".
_ANNO_NEL_NOME = re.compile(r"(19|20)\d{2}")


def separatore(titolo: str) -> None:
    print(f"\n{'=' * 72}\n{titolo}\n{'=' * 72}")


def _stagione(percorso: Path, esplicita: int | None) -> int | None:
    """L'anno di partenza della stagione: quello detto, o quello nel nome."""
    if esplicita:
        return esplicita
    trovato = _ANNO_NEL_NOME.search(percorso.stem)
    return int(trovato.group(0)) if trovato else None


def _aggiunte(valori: list[str] | None) -> dict[str, int]:
    """Le corrispondenze passate a mano, ``--squadra "Pisa=801"``."""
    mappa: dict[str, int] = {}
    for valore in valori or []:
        if "=" not in valore:
            raise SystemExit(f"--squadra vuole la forma Nome=id, ricevuto: {valore!r}")
        nome, _, ident = valore.partition("=")
        if not ident.strip().isdigit():
            raise SystemExit(f"--squadra vuole un id numerico, ricevuto: {valore!r}")
        mappa[nome.strip()] = int(ident)
    return mappa


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Importa i risultati di una stagione da un file (zero richieste)"
    )
    parser.add_argument("file", help="il file dei risultati (CSV)")
    parser.add_argument(
        "--stagione",
        type=int,
        default=None,
        help="anno di partenza, es. 2025 per la 2025/26 (default: letto dal nome del file)",
    )
    parser.add_argument(
        "--solo-verifica",
        action="store_true",
        help="legge e controlla senza scrivere niente in archivio",
    )
    parser.add_argument(
        "--squadra",
        action="append",
        default=None,
        metavar="NOME=ID",
        help="corrispondenza da usare per una squadra che non riesco a riconoscere "
             "(ripetibile)",
    )
    parser.add_argument(
        "--anche-se-incompleto",
        action="store_true",
        help="importa anche un file che non ha la forma di un campionato intero",
    )
    args = parser.parse_args()

    percorso = Path(args.file)
    if not percorso.exists():
        print(f"\n[file] {percorso} non esiste.\n")
        return 1

    stagione = _stagione(percorso, args.stagione)
    if stagione is None:
        print(
            f"\n[stagione] non riesco a capire di che stagione sia {percorso.name}.\n"
            "Dimmelo con --stagione 2025 (l'anno di partenza: la 2025/26 e' la 2025).\n"
        )
        return 1

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n[configurazione] {exc}\n")
        return 1

    # --- chi sono le squadre -------------------------------------------------
    #
    # Gli id non si inventano: si prendono da cio' che gia' abbiamo. Il dataset
    # da' le squadre passate dalla Serie A, che e' la sorgente giusta; i file di
    # cache danno tutte le altre, ed e' li' che si trova la neopromossa poi
    # retrocessa che nel nostro storico non ha mai giocato.
    separatore("CHI SONO LE SQUADRE")
    client = build_client(settings)
    store_corrente, _ = build_store(settings)
    prima = client.budget.used
    dataset = load_dataset(
        client, store_corrente, settings.seasons, settings.league_id,
    )
    larghi = squadre_viste(sorgenti_nomi(settings.cache_dir, settings.store_dir))
    print(f"Squadre note dalla Serie A:  {len(dataset.team_names)}")
    print(f"Squadre nominate sul disco:  {len(larghi)}  (ripiego, solo se il nome e' univoco)")
    speso = client.budget.used - prima
    print(f"Richieste spese finora:      {speso}"
          + ("  (tutto dalla cache)" if speso == 0 else ""))

    # --- cosa dice il file ---------------------------------------------------
    separatore(f"IL FILE — stagione {stagione}/{str(stagione + 1)[-2:]}")
    print(f"File:     {percorso}")
    print(f"Archivio: {store_stagione(settings.importato_dir, stagione).root}")

    try:
        _, anteprima = prepara(
            percorso, stagione, dataset.team_names, larghi,
            _aggiunte(args.squadra), settings.league_id,
        )
    except FileIllegibile as exc:
        print(f"\nNon riesco a leggere il file: {exc}\n")
        return 1

    print(f"\nRighe lette:        {anteprima.righe}")
    if anteprima.squadre:
        print(f"Squadre agganciate: {len(anteprima.squadre)}")
        for nome, ident in sorted(anteprima.squadre.items()):
            canonico = dataset.team_names.get(ident) or larghi.get(ident, "?")
            uguale = "" if canonico == nome else f"  ({canonico})"
            print(f"   {nome:<16} -> {ident}{uguale}")

    if anteprima.partite:
        print(f"\nPartite convertite: {anteprima.partite} "
              f"({anteprima.giocate} giocate, {anteprima.da_giocare} da giocare)")

    if anteprima.avvisi:
        print("\nIl file non ha la forma di un campionato completo:")
        for avviso in anteprima.avvisi:
            print(f"   - {avviso}")

    if anteprima.problemi:
        separatore("NON IMPORTO")
        for problema in anteprima.problemi:
            print(f"   - {problema}")
        if anteprima.sconosciute:
            print(
                "\nUna squadra che non riconosco non entra in archivio con un id\n"
                "inventato: si sdoppierebbe dalla stessa squadra vista altrove e il\n"
                "modello ne stimerebbe due meta'. Passa l'id giusto a mano, per esempio:\n"
                f"\n    --squadra \"{anteprima.sconosciute[0]}=801\"\n"
            )
        return 1

    if args.solo_verifica:
        separatore("SOLA VERIFICA")
        print("Niente e' stato scritto. Rilancia senza --solo-verifica per importare.")
        return 0

    # --- importazione --------------------------------------------------------
    separatore("IMPORTAZIONE")
    store = store_stagione(settings.importato_dir, stagione)
    rapporto = importa(
        percorso, store, stagione, dataset.team_names, larghi,
        _aggiunte(args.squadra), settings.league_id,
        anche_se_incompleto=args.anche_se_incompleto,
    )

    if not rapporto.scritto:
        print("Non ho scritto niente.")
        if not rapporto.completo:
            print(
                "\nIl file e' incompleto (vedi sopra). Se sai che va bene cosi',\n"
                "rilancia con --anche-se-incompleto."
            )
        return 1

    print(f"Partite in archivio: {rapporto.partite}")
    print(f"Nuove / aggiornate:  {rapporto.nuove} / {rapporto.aggiornate}")
    print(f"Richieste spese:     {client.budget.used - prima}")
    print(
        f"\nLa stagione {stagione}/{str(stagione + 1)[-2:]} entra nel modello da sola: il sito\n"
        "se ne accorge alla prima pagina aperta, senza riavvii.\n"
        "\nCosa NON e' entrato, e non entrera' mai da un file di risultati:\n"
        "formazioni, tabellini, voti. Le schede giocatore di questa stagione\n"
        "restano vuote; per averle serve ricomprarla (scripts/riempi_storico.py).\n"
        "\nSe un giorno la ricomprerai davvero, il dato pagato prendera' il posto\n"
        "di questo da solo: non serve cancellare niente."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
