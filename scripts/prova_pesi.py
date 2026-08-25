"""Con che cosa si pesa un giocatore: gol+assist, o tiri+passaggi chiave?

`data/pesi.py` oggi misura quanto vale un giocatore da cio' che ha prodotto:
gol e assist per minuto giocato. E' la scelta ovvia, ed e' anche la piu'
rumorosa che si potesse fare — un titolare produce **0,14 fra gol e assist a
partita**, cioe' una quarantina di eventi in una stagione intera. Chi ne fa 5
invece di 3 puo' semplicemente essere stato fortunato.

Tiri e passaggi chiave sono lo stesso gesto misurato **un passo prima**, dove
la fortuna non e' ancora intervenuta, e capitano **1,26 volte a partita**: nove
volte tanto. L'ipotesi e' che siano un metro migliore di quanto un giocatore
conti davvero.

    python scripts/prova_pesi.py
    python scripts/prova_pesi.py --minuti 270   # quanto poco si accetta di guardare

Zero richieste: i tabellini sono tutti sul disco.

COME SI DECIDE CHI HA RAGIONE

Non si misura la **stabilita'** — un numero costante sarebbe stabilissimo e
inutile. Si misura la **capacita' di prevedere la produzione futura dello
stesso giocatore**: si spacca ogni stagione a meta', si calcola il metro sulla
prima meta' e si guarda quanto indovina i gol+assist per 90 minuti che quel
giocatore fara' nella seconda.

Il bersaglio resta gol+assist, e deve restare quello: e' cio' che `pesi.py`
vuole sapere, cioe' quanta della produzione della squadra e' in campo stasera.
La domanda non e' "quale numero e' piu' bello" ma "quale numero indovina
meglio il prodotto".

I DUE CONFRONTI, E PERCHE' NON BASTA IL PRIMO

Il tasso grezzo dice quale segnale e' migliore in se'. Ma `pesi.py` non usa il
tasso grezzo: lo tira verso la media della squadra con uno shrinkage pesato sui
minuti, e **lo shrinkage aiuta di piu' il metro piu' rumoroso**. Confrontare
solo i tassi grezzi darebbe quindi a tiri+chiave un vantaggio che nel codice
vero si ridurrebbe. Si misurano tutti e due, con la stessa formula di
`pesi_squadra`, e vale il secondo.

L'INSIDIA
Un metro puo' prevedere meglio ed essere comunque inutile qui, se il vantaggio
sparisce dopo lo shrinkage o se cambia segno da una stagione all'altra. E'
esattamente com'e' andata con i tiri a livello di squadra il 24 agosto 2026:
la misura si fa prima di cambiare il codice, non dopo.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from serie_a_analyzer.config import (
    build_client,
    build_importato_stores,
    build_store,
    build_storico_stores,
    load_settings,
)
from serie_a_analyzer.data.dataset import load_dataset
from serie_a_analyzer.data.pesi import MINUTI_PARTITA, PARTITE_DI_PRIOR
from serie_a_analyzer.data.players import prestazioni_partita
from serie_a_analyzer.data.store import StoreUnito

logging.basicConfig(level=logging.WARNING, format="  %(message)s")

# Quanti minuti servono in CIASCUNA delle due meta' perche' un giocatore entri.
# Con meno, il tasso e' una divisione per un numero piccolo: chi entra dieci
# minuti e segna avrebbe 9 gol per 90 minuti, ed e' il rumore che si sta
# cercando di togliere, non un dato.
MINUTI_MINIMI = 450  # cinque partite intere, come MINIMO_PARTITE in pesi.py

# Quanto deve staccare un metro perche' valga la pena riscrivere `pesi.py`.
# Piu' bassa della soglia della prova dei tiri perche' qui non si riscrive il
# modello, si cambia una formula dentro un modulo che non e' ancora acceso:
# costa poco e non tocca nessun pronostico gia' scritto.
GUADAGNO_CHE_VALE = 0.03


def separatore(titolo: str) -> None:
    print(f"\n{'=' * 72}\n{titolo}\n{'=' * 72}")


@dataclass
class Mezza:
    """Quanto ha fatto un giocatore in mezza stagione, coi suoi minuti."""

    minuti: int = 0
    gol: int = 0
    assist: int = 0
    tiri: int = 0
    chiave: int = 0

    @property
    def prodotto(self) -> int:
        """Gol + assist: e' il bersaglio, cioe' cio' che si vuole indovinare."""
        return self.gol + self.assist

    @property
    def creato(self) -> int:
        """Tiri + passaggi chiave: lo stesso gesto un passo prima."""
        return self.tiri + self.chiave

    @property
    def misto(self) -> int:
        """Il gesto e il suo esito insieme. Se il gol aggiungesse qualcosa che
        il tiro non ha, e' qui che si vedrebbe."""
        return self.prodotto + self.tiri


# I metri in gara. Il primo e' quello che `pesi.py` usa oggi: e' il campione da
# battere, non un concorrente come gli altri.
METRI = (
    ("gol+assist", "prodotto"),
    ("tiri+chiave", "creato"),
    ("solo tiri", "tiri"),
    ("solo chiave", "chiave"),
    ("gol+ass+tiri", "misto"),
)
RIFERIMENTO = METRI[0][0]


def raccogli(dataset, unito, meta: dict[int, object]) -> dict[tuple, tuple[Mezza, Mezza]]:
    """Per ogni (stagione, giocatore), cosa ha fatto prima e dopo il giro di boa.

    Il taglio e' la stessa data per tutti, non "le prime N partite di ognuno":
    cosi' nessuno puo' avere una prima meta' che finisce a maggio perche' ha
    giocato poco, e le due meta' sono confrontabili fra giocatori.

    I passaggi chiave e i tiri hanno una convenzione che va conosciuta: lo zero
    **non viene mai scritto**, al suo posto c'e' `null` (verificato sull'intero
    archivio). Quindi `None` significa zero e si somma come tale — mentre su
    `assists` lo zero c'e' per esteso. Due convenzioni nella stessa busta.
    """
    meta_stagione = meta
    dati: dict[tuple, tuple[Mezza, Mezza]] = defaultdict(lambda: (Mezza(), Mezza()))

    for fixture in dataset.finished:
        confine = meta_stagione.get(fixture.season)
        if confine is None:
            continue
        prima = fixture.kickoff_utc < confine
        for p in prestazioni_partita(unito, fixture.api_id, fixture.kickoff_utc):
            mezze = dati[(fixture.season, p.player_id)]
            m = mezze[0] if prima else mezze[1]
            m.minuti += p.minuti
            m.gol += p.gol
            m.assist += p.assist
            m.tiri += p.tiri or 0
            m.chiave += p.passaggi_chiave or 0
            dati[(fixture.season, p.player_id)] = mezze
    return dati


def giro_di_boa(dataset) -> dict[int, object]:
    """La data che spacca in due ogni stagione: meta' esatta fra prima e ultima."""
    per_stagione: dict[int, list] = defaultdict(list)
    for f in dataset.finished:
        per_stagione[f.season].append(f.kickoff_utc)
    confini = {}
    for stagione, date in per_stagione.items():
        if len(date) < 100:      # una stagione a meta' non si spacca
            continue
        confini[stagione] = min(date) + (max(date) - min(date)) / 2
    return confini


def coppie(
    dati: dict[tuple, tuple[Mezza, Mezza]],
    campo: str,
    minuti_minimi: int,
    con_shrinkage: bool,
    squadre: dict[tuple, int],
) -> list[tuple[float, float]]:
    """(metro sulla prima meta', gol+assist per 90 nella seconda).

    Con lo shrinkage il tasso viene tirato verso quello **della sua squadra**
    con la stessa formula di `pesi_squadra`: un prior di 5 partite-equivalenti,
    pesato sui minuti. E' la formula vera, non una sua imitazione, perche' e'
    proprio quella che potrebbe annullare il vantaggio.
    """
    validi = {
        k: v for k, v in dati.items()
        if v[0].minuti >= minuti_minimi and v[1].minuti >= minuti_minimi
    }
    if not validi:
        return []

    # Il tasso di ogni squadra sulla prima meta': il centro dello shrinkage.
    totali: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for (stagione, player_id), (prima, _) in validi.items():
        squadra = squadre.get((stagione, player_id))
        if squadra is None:
            continue
        totali[(stagione, squadra)][0] += getattr(prima, campo)
        totali[(stagione, squadra)][1] += prima.minuti

    fuori: list[tuple[float, float]] = []
    prior_minuti = PARTITE_DI_PRIOR * MINUTI_PARTITA
    for (stagione, player_id), (prima, dopo) in validi.items():
        valore = float(getattr(prima, campo))
        if con_shrinkage:
            squadra = squadre.get((stagione, player_id))
            somma, minuti = totali.get((stagione, squadra), [0.0, 0.0])
            if minuti <= 0:
                continue
            tasso_squadra = somma / minuti
            tasso = (valore + prior_minuti * tasso_squadra) / (prima.minuti + prior_minuti)
        else:
            tasso = valore / prima.minuti
        fuori.append((tasso * MINUTI_PARTITA, dopo.prodotto * MINUTI_PARTITA / dopo.minuti))
    return fuori


def correlazione(valori: list[tuple[float, float]]) -> float | None:
    """Pearson. `None` quando non c'e' abbastanza roba per dire qualcosa."""
    if len(valori) < 30:
        return None
    x = [a for a, _ in valori]
    y = [b for _, b in valori]
    if statistics.pstdev(x) == 0 or statistics.pstdev(y) == 0:
        return None
    return statistics.correlation(x, y)


def stampa_confronto(titolo: str, misure: dict[str, float | None], n: int) -> None:
    print(f"\n{titolo}  (n = {n})")
    base = misure.get(RIFERIMENTO)
    for nome, r in misure.items():
        if r is None:
            print(f"  {nome:14s}  non misurabile")
            continue
        scarto = ""
        if base is not None and nome != RIFERIMENTO:
            scarto = f"   ({r - base:+.3f})"
        print(f"  {nome:14s}  r = {r:.3f}{scarto}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Con che cosa si pesa un giocatore? Zero richieste."
    )
    parser.add_argument(
        "--minuti", type=int, default=MINUTI_MINIMI,
        help=f"minuti minimi in ciascuna meta' stagione (default {MINUTI_MINIMI})",
    )
    args = parser.parse_args()

    settings = load_settings()
    client = build_client(settings)
    store, _ = build_store(settings)
    storici = build_storico_stores(settings)
    unito = StoreUnito(stores=[store, *storici.values()])

    separatore("COSA C'E' SUL DISCO")
    dataset = load_dataset(
        client, store, settings.seasons, settings.league_id,
        storici=storici, importati=build_importato_stores(settings),
    )
    confini = giro_di_boa(dataset)
    print(f"Stagioni intere: {', '.join(str(s) for s in sorted(confini)) or 'nessuna'}")

    # La squadra di un giocatore in quella stagione: quella in cui ha giocato
    # di piu', perche' il mercato di gennaio esiste e lo shrinkage ha bisogno
    # di un centro solo.
    minuti_per_squadra: dict[tuple, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for fixture in dataset.finished:
        if fixture.season not in confini:
            continue
        for p in prestazioni_partita(unito, fixture.api_id, fixture.kickoff_utc):
            minuti_per_squadra[(fixture.season, p.player_id)][p.team_id] += p.minuti
    squadre = {k: max(v, key=v.get) for k, v in minuti_per_squadra.items()}

    dati = raccogli(dataset, unito, confini)
    print(f"Giocatori-stagione con almeno un minuto: {len(dati)}")

    n = len([
        1 for v in dati.values()
        if v[0].minuti >= args.minuti and v[1].minuti >= args.minuti
    ])
    print(f"Di cui con {args.minuti}+ minuti in tutte e due le meta': {n}")
    if n < 30:
        print("\nTroppo pochi per rispondere. Servono piu' tabellini.")
        return 1

    separatore("LA PRIMA META' PREVEDE LA SECONDA?")
    print("Si misura il metro sulla prima meta' di stagione e si guarda quanto")
    print("indovina i gol+assist per 90 minuti della seconda.")

    risultati = {}
    for con_shrinkage in (False, True):
        misure = {
            nome: correlazione(coppie(dati, campo, args.minuti, con_shrinkage, squadre))
            for nome, campo in METRI
        }
        risultati[con_shrinkage] = misure
        stampa_confronto(
            "Tasso grezzo:" if not con_shrinkage
            else "Con lo shrinkage di pesi.py (e' questo che conta):",
            misure, n,
        )

    separatore("TIENE STAGIONE PER STAGIONE?")
    print("  Un vantaggio che viene tutto da un anno solo non e' un vantaggio.")
    nomi = [n for n, _ in METRI]
    print("  stagione   n     " + "  ".join(f"{n:>12s}" for n in nomi))
    per_stagione: dict[str, list[float]] = {n: [] for n in nomi if n != RIFERIMENTO}
    for stagione in sorted(confini):
        sotto = {k: v for k, v in dati.items() if k[0] == stagione}
        quanti = len([
            1 for v in sotto.values()
            if v[0].minuti >= args.minuti and v[1].minuti >= args.minuti
        ])
        valori = {
            nome: correlazione(coppie(sotto, campo, args.minuti, True, squadre))
            for nome, campo in METRI
        }
        if valori[RIFERIMENTO] is None:
            print(f"  {stagione:^8} {quanti:<5} troppo pochi giocatori")
            continue
        for n in per_stagione:
            if valori[n] is not None:
                per_stagione[n].append(valori[n] - valori[RIFERIMENTO])
        riga = "  ".join(
            f"{valori[n]:>12.3f}" if valori[n] is not None else f"{'-':>12s}"
            for n in nomi
        )
        print(f"  {stagione:^8} {quanti:<5} {riga}")

    print(f"\n  Guadagno rispetto a {RIFERIMENTO}, il peggiore delle quattro stagioni:")
    for n, scarti in per_stagione.items():
        if scarti:
            print(f"    {n:14s} {min(scarti):+.3f}")

    separatore("VERDETTO")
    finale = risultati[True]
    base = finale.get(RIFERIMENTO)
    sfidanti = {k: v for k, v in finale.items() if k != RIFERIMENTO and v is not None}
    if base is None or not sfidanti:
        print("Non ci sono abbastanza dati per rispondere.")
        return 0

    migliore = max(sfidanti, key=lambda k: sfidanti[k])
    guadagno = sfidanti[migliore] - base

    if guadagno >= GUADAGNO_CHE_VALE:
        print(
            f"VINCE {migliore.upper()}: r {base:.3f} -> {sfidanti[migliore]:.3f} "
            f"({guadagno:+.3f}),\n"
            "e il vantaggio sopravvive allo shrinkage. Vale la pena cambiare cosa\n"
            "misura pesi.py: la formula resta quella, cambia il numeratore.\n"
            "Il bersaglio non cambia mai: restano gol+assist quelli che contano."
        )
    else:
        print(
            f"NESSUNO BATTE {RIFERIMENTO.upper()}: il migliore e' {migliore} "
            f"con {guadagno:+.3f},\n"
            f"sotto il {GUADAGNO_CHE_VALE:+.2f} che varrebbe la pena.\n"
            "pesi.py resta com'e'. Il rumore dei gol c'e', ma tiri e passaggi\n"
            "chiave non lo tolgono abbastanza da giustificare il cambio."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
