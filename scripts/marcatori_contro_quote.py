"""Il pronostico su chi segna contro le quote di Bet365. Non spende richieste.

E' la seconda meta' del lavoro 10: il backtest (`backtest.py --marcatori`) dice
che il pronostico batte chi guarda solo il ruolo, e qui si guarda il metro vero,
il bookmaker. Si misura solo cio' che si sarebbe potuto dire prima del fischio:

* la matrice e' quella CONGELATA nel registro, l'ultima scritta prima del via,
  e mai un ricalcolo — il modello di oggi ha visto il risultato;
* la storia dei giocatori e' quella di prima del via (`StoriaMarcatori`
  risponde sempre "prima di un istante");
* i titolari sono quelli veri: e' il modo "a formazioni uscite", lo stesso
  della prima riga del backtest;
* la quota e' la CHIUSURA, l'ultima lettura con minuti al via >= 0.

    python scripts/marcatori_contro_quote.py

IL NOME. Il bookmaker scrive "Lautaro Martinez", l'archivio "Lautaro Martínez"
con un id. L'abbinamento si fa dentro la partita (le due distinte, panchine
comprese: un nome ha molti meno omonimi fra quaranta giocatori che fra mille),
e solo quando e' univoco. Chi non si abbina non si inventa: si CONTA, e resta
fuori da entrambe le colonne, cosi' modello e bookmaker sono misurati sulle
stesse righe — la regola del confronto con le quote.

IL MARGINE. Su "chi segna" non si toglie normalizzando: non e' un gruppo
esaustivo, possono segnare in tre o nessuno (vedi data/odds.py). Si mostrano
quindi due Bet365: la quota grezza (1/quota, col margine dentro) e la quota
riscalata perche' dichiari esattamente i marcatori accaduti. La seconda sceglie
il suo fattore guardando la risposta, e va letta come una baseline FAVORITA —
la stessa convenzione del backtest: batterla vale di piu', non di meno.
"""

from __future__ import annotations

import sqlite3
import sys
import unicodedata
from collections import defaultdict
from math import log
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADICE / "src"))

from serie_a_analyzer.config import (  # noqa: E402
    ConfigError,
    build_client,
    build_importato_stores,
    build_prediction_store,
    build_store,
    build_storico_stores,
    load_settings,
)
from serie_a_analyzer.data.dataset import load_dataset  # noqa: E402
from serie_a_analyzer.data.marcatori import StoriaMarcatori, probabili_marcatori  # noqa: E402
from serie_a_analyzer.data.players import costruisci_archivio, formazioni_partita  # noqa: E402
from serie_a_analyzer.data.store import StoreUnito, formazione_provvisoria  # noqa: E402

BOOKMAKER = "Bet365"

# Sotto questo numero di gol fra le righe abbinate il confronto e' dichiarato
# come aneddoto: su una cinquantina di marcatori una differenza nel terzo
# decimale del log loss e' il caso.
GOL_PER_UN_VERDETTO = 200


def normalizza(nome: str) -> str:
    """Minuscolo, senza accenti ne' punteggiatura: 'Çalhanoğlu' -> 'calhanoglu'."""
    decomposto = unicodedata.normalize("NFKD", nome.lower())
    senza = "".join(c for c in decomposto if not unicodedata.combining(c))
    # Le lettere che NFKD non scompone: senza, "Østigård" non diventerebbe mai
    # "ostigard" e resterebbe fuori senza che niente lo dica.
    senza = senza.replace("ı", "i").replace("ø", "o").replace("ł", "l").replace("đ", "d")
    return " ".join("".join(c if c.isalnum() else " " for c in senza).split())


def abbina(nome_bk: str, rosa: dict[int, str]) -> int | None:
    """L'id del giocatore che il bookmaker chiama ``nome_bk``, se univoco.

    Quattro gradini, dal piu' severo: il nome identico; tutte le parole
    dell'uno dentro l'altro ("Pio Esposito" e "Francesco Pio Esposito");
    cognome e iniziale del nome; iniziale e cognome dell'archivio fra le
    parole del bookmaker ("P. Kalulu" e "Pierre Kalulu Kyatengwa"). Al primo
    gradino che trova UN solo giocatore ci si ferma; due candidati sono
    un'ambiguita', e un'ambiguita' non si risolve tirando a indovinare.
    """
    cercato = normalizza(nome_bk)
    parole = set(cercato.split())
    if not parole:
        return None
    rosa_n = {pid: normalizza(n) for pid, n in rosa.items()}

    gradini = (
        lambda n: n == cercato,
        lambda n: bool(parole) and (parole <= set(n.split()) or set(n.split()) <= parole),
        lambda n: (
            n.split()[-1:] == cercato.split()[-1:]
            and n[:1] == cercato[:1]
        ),
        lambda n: (
            len(n.split()) >= 2
            and len(n.split()[-1]) >= 3
            and n.split()[-1] in parole
            and n[:1] == cercato[:1]
        ),
    )
    for gradino in gradini:
        trovati = [pid for pid, n in rosa_n.items() if n and gradino(n)]
        if len(trovati) == 1:
            return trovati[0]
        if len(trovati) > 1:
            return None
    return None


def quote_di_chiusura(db: Path) -> dict[int, dict[str, float]]:
    """Per ogni partita, nome -> quota decimale dell'ultima lettura prima del via."""
    conn = sqlite3.connect(db)
    try:
        righe = conn.execute(
            """
            select q.fixture_id, q.giocatore, q.decimal_odds
            from quote_marcatori q
            where q.bookmaker = ? and q.tipo = 'segna' and q.lato = 'partita'
              and q.collected_at = (
                  select max(r.collected_at) from quote_marcatori r
                  where r.fixture_id = q.fixture_id and r.bookmaker = q.bookmaker
                    and r.tipo = 'segna' and r.lato = 'partita' and r.minuti_al_via >= 0
              )
            """,
            (BOOKMAKER,),
        ).fetchall()
    finally:
        conn.close()
    out: dict[int, dict[str, float]] = defaultdict(dict)
    for fid, nome, quota in righe:
        out[int(fid)][nome] = float(quota)
    return out


def log_loss(p: float, y: bool) -> float:
    p = min(max(p, 1e-12), 1 - 1e-12)
    return -log(p) if y else -log(1 - p)


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n[configurazione] {exc}\n")
        return 1

    client = build_client(settings)
    prima = client.budget.used
    store, _ = build_store(settings)
    registro = build_prediction_store(settings)
    dataset = load_dataset(
        client, store, settings.seasons, settings.league_id,
        storici=build_storico_stores(settings),
        importati=build_importato_stores(settings),
    )
    storici = list(build_storico_stores(settings).values())
    archivio = costruisci_archivio(StoreUnito(stores=[store, *storici]), dataset.fixtures)
    storia = StoriaMarcatori(archivio.prestazioni, dataset.fixtures)
    per_partita: dict[int, list] = defaultdict(list)
    for p in archivio.prestazioni:
        per_partita[p.fixture_id].append(p)

    quote = quote_di_chiusura(registro.path)
    conti = defaultdict(int)
    # (fixture_id, segna, modello, bet365 grezza)
    righe: list[tuple[int, bool, float, float]] = []
    for fid, listino in sorted(quote.items()):
        f = dataset.by_id.get(fid)
        prestazioni = per_partita.get(fid)
        if f is None or not prestazioni:
            conti["senza_tabellino"] += 1
            continue
        pronostico = registro.ultimo_per_partita(fid)
        if pronostico is None:
            conti["senza_pronostico"] += 1
            continue
        distinta = store.load_detail(fid, "lineups")
        formazioni = {x.team_id: x for x in formazioni_partita(store, fid)}
        squadre = (f.home_team_id, f.away_team_id)
        # La stessa condizione della scheda partita (`Engine.probabili_marcatori`).
        if (not distinta or formazione_provvisoria(distinta)
                or not all(t in formazioni and len(formazioni[t].titolari) == 11
                           for t in squadre)):
            conti["distinta_non_intera"] += 1
            continue
        conti["partite"] += 1

        matrice = registro.matrice(pronostico["id"])
        rosa = {
            g.player_id: g.nome
            for x in formazioni.values() for g in (*x.titolari, *x.panchina)
        }
        per_id: dict[int, float] = {}
        ambigui = set()
        for nome_bk, q in listino.items():
            pid = abbina(nome_bk, rosa)
            if pid is None:
                continue
            if pid in per_id:
                ambigui.add(pid)
            per_id[pid] = q
        segnato = {x.player_id: x.gol > 0 for x in prestazioni}

        for lato, team_id in zip(("home", "away"), squadre):
            titolari = formazioni[team_id].titolari
            candidati = storia.candidati(team_id, f.kickoff_utc, titolari=titolari)
            for m in probabili_marcatori(matrice, lato, candidati):
                pid = m.candidato.player_id
                conti["titolari"] += 1
                if pid not in per_id or pid in ambigui:
                    conti["titolari_senza_quota"] += 1
                    if segnato.get(pid, False):
                        conti["gol_senza_quota"] += 1
                    continue
                righe.append((fid, segnato.get(pid, False), m.probabilita, 1.0 / per_id[pid]))

    print("=" * 74)
    print(f"MARCATORI - il pronostico contro {BOOKMAKER}, sulle partite 2026")
    print("=" * 74)
    print(f"Partite con quote sui marcatori:        {len(quote)}")
    print(f"  misurate:                             {conti['partite']}")
    print(f"  senza tabellino:                      {conti['senza_tabellino']}")
    print(f"  senza pronostico nel registro:        {conti['senza_pronostico']}")
    print(f"  distinta non intera:                  {conti['distinta_non_intera']}")
    print(f"Titolari:                               {conti['titolari']}")
    print(f"  abbinati a una quota:                 {len(righe)} "
          f"({len(righe) / max(conti['titolari'], 1):.1%})")
    print(f"  senza quota o con nome ambiguo:       {conti['titolari_senza_quota']} "
          f"(di cui hanno segnato {conti['gol_senza_quota']})")
    if not righe:
        print("\nNessuna riga da confrontare.\n")
        return 1

    esiti = [r[1] for r in righe]
    gol = sum(esiti)
    grezza = [r[3] for r in righe]
    fattore = gol / sum(grezza)
    riscalata = [min(p * fattore, 1 - 1e-9) for p in grezza]
    modello = [r[2] for r in righe]
    frequenza = gol / len(righe)

    print(f"\n{len(righe)} righe, {gol} hanno segnato ({frequenza:.1%}).\n")
    print(f"{'chi':<40}{'dichiarati':>11}{'Brier':>9}{'log loss':>10}")
    print("-" * 70)
    for etichetta, probs in (
        ("stessa probabilita' per tutti", [frequenza] * len(righe)),
        (f"{BOOKMAKER}, quota grezza", grezza),
        (f"{BOOKMAKER}, riscalata (favorita)", riscalata),
        ("il pronostico, dentro la matrice", modello),
    ):
        brier = sum((p - y) ** 2 for p, y in zip(probs, esiti)) / len(righe)
        ll = sum(log_loss(p, y) for p, y in zip(probs, esiti)) / len(righe)
        print(f"{etichetta:<40}{sum(probs):>11.1f}{brier:>9.4f}{ll:>10.4f}")
    print(f"\nAccaduti: {gol}. La riscalata divide le quote grezze per "
          f"{1 / fattore:.3f}, scelto guardando la risposta.")

    # La prova appaiata per partita, contro la baseline favorita: le righe di
    # una stessa partita condividono la matrice e non sono indipendenti.
    diff: dict[int, float] = defaultdict(float)
    for (fid, y, pm, _), pb in zip(righe, riscalata):
        diff[fid] += log_loss(pm, y) - log_loss(pb, y)
    valori = list(diff.values())
    n = len(valori)
    media = sum(valori) / n
    varianza = sum((v - media) ** 2 for v in valori) / (n - 1) if n > 1 else 0.0
    errore = (varianza / n) ** 0.5
    rapporto = media / errore if errore else 0.0
    print(f"\nProva appaiata, pronostico contro {BOOKMAKER} riscalata, per partita:")
    print(f"  {n} partite, differenza media {media:+.4f} ({rapporto:+.2f} errori standard)")
    print(f"  il pronostico fa meglio in {sum(v < 0 for v in valori)}, "
          f"peggio in {sum(v > 0 for v in valori)}")

    print()
    if gol < GOL_PER_UN_VERDETTO:
        print(f"Con {gol} gol e' un aneddoto, non un verdetto: ne servono almeno "
              f"{GOL_PER_UN_VERDETTO}.")
    elif abs(rapporto) < 2.0:
        print("Sotto i due errori standard: pari col bookmaker.")
    elif rapporto < 0:
        print(f"Il pronostico batte {BOOKMAKER}, oltre i due errori standard.")
    else:
        print(f"{BOOKMAKER} batte il pronostico, oltre i due errori standard.")
    print(f"\nRichieste API spese: {client.budget.used - prima}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
