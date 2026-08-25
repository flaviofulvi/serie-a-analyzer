"""La domanda che decide se il modello a tiri vale un mese di lavoro.

    "I tiri di una squadra prevedono i suoi gol meglio dei suoi gol?"

E' il passo (b) del giorno del piano, e si fa PRIMA di scrivere qualunque
modello: se i tiri non dicono niente in piu' dei gol, il lavoro 8 finisce qui e
si e' risparmiato un mese. Non costa richieste — i tabellini sono gia' sul
disco, pagati dal riempimento del passo (a).

    python scripts/prova_tiri.py
    python scripts/prova_tiri.py --finestra 8   # quante partite guarda indietro

COME E' FATTA LA MISURA

Per ogni squadra e ogni partita si guarda **indietro** di N partite (solo
quelle gia' giocate: mai la partita stessa, mai le successive) e si calcola la
media di tre cose — gol, tiri, tiri in porta. Poi si chiede quale delle tre
indovina meglio quello che succede nella partita **dopo**. La correlazione fra
previsione e realta' e' il numero: piu' alta, meglio quel dato prevede.

Il confronto e' fra dati che vedono esattamente le stesse partite, quindi e'
onesto per costruzione: se i tiri vincono, vincono perche' sono meno rumorosi
dei gol, non perche' hanno guardato di piu'.

Non si attraversa il confine fra due stagioni: fra maggio e agosto c'e' il
mercato, e le ultime sei partite di una squadra di aprile non dicono niente
sulla squadra di settembre.

TRE DOMANDE, NON UNA

I gol fatti e i gol subiti si prevedono con difficolta' diverse, e nessuna
delle due e' la domanda che conta. Quella e' la terza: i **gol totali** di una
partita, cioe' l'Over/Under, l'unico mercato dove il modello oggi pareggia e
basta (vedi il lavoro 7). Un dato che migliorasse solo l'attacco non
servirebbe a niente li'.

Il verdetto guarda quindi soprattutto la terza, e pretende che il vantaggio
tenga su piu' finestre: una misura che vince a 6 partite e perde a 8 e'
rumore, non un segnale.

COSA SI STA MISURANDO DAVVERO, E VA DETTO

Il campo `shots.total` dei tabellini **non conta i tiri murati**. Verificato il
24 agosto 2026 su due partite confrontando `/fixtures/players` con
`/fixtures/statistics`: 27 tiri di cui 9 murati contro 18 nei tabellini, 25 di
cui 5 murati contro 20. Il conto torna esatto tutte e due le volte. Quindi qui
si misurano i **tiri non murati**, che come predittore e' una definizione
legittima — anzi di solito migliore, perche' un tiro murato e' quasi sempre un
tiro cattivo — ma non e' "i tiri" e non va raccontata cosi'.

L'**xG** non e' in questi dati: sta in `/fixtures/statistics`, che costa una
richiesta per partita e che non abbiamo mai chiamato. Il 24 agosto 2026 si e'
verificato che il piano Pro lo espone (0.88 contro 1.96 in Venezia-Juventus).
Se un giorno si volesse riaprire il lavoro 8, quella e' la strada da provare —
non i tiri.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from serie_a_analyzer.config import (
    build_client,
    build_importato_stores,
    build_store,
    build_storico_stores,
    load_settings,
)
from serie_a_analyzer.data.daily import DETTAGLIO_STATISTICHE
from serie_a_analyzer.data.dataset import load_dataset
from serie_a_analyzer.data.players import prestazioni_partita
from serie_a_analyzer.data.store import StoreUnito

logging.basicConfig(level=logging.WARNING, format="  %(message)s")

FINESTRA_PREDEFINITA = 6

# Le finestre su cui si chiede che il risultato tenga. Un vantaggio che cambia
# di segno passando da 6 a 8 partite non e' un segnale.
FINESTRE_DI_CONTROLLO = (4, 6, 8, 10)

# Quanto deve staccare un dato per giustificare di riscrivere cosa mangia
# `fit_dixon_coles` e ristimare tutto. Non e' una soglia statistica ma pratica:
# il Brier e' molto piu' grossolano di una correlazione, e sotto questo
# guadagno la differenza si mangerebbe nel suo rumore.
GUADAGNO_CHE_VALE = 0.05

# Sotto questa correlazione un predittore non prevede niente di utile,
# staccasse pure tutti gli altri: r = 0.10 vuol dire l'1% di quel che succede.
CORRELAZIONE_MINIMA_UTILE = 0.15

# Quanto di una stagione un dato deve coprire perche' quella stagione entri nel
# confronto. Vedi `solo_complete`: sotto questa soglia i buchi non sono
# incidenti ma un pezzo di stagione che quel dato non ha mai avuto, e le
# finestre non sono piu' quelle che dicono di essere.
COPERTURA_MINIMA = 0.95


# I dati che si confrontano, in un posto solo. `gol` e' il riferimento: e' cio'
# che il modello guarda oggi, e ogni altro dato deve batterlo per esistere.
MISURE = (
    ("gol", "gol_fatti", "gol_subiti"),
    ("tiri", "tiri_fatti", "tiri_subiti"),
    ("tiri in porta", "in_porta_fatti", "in_porta_subiti"),
    ("xG", "xg_fatti", "xg_subiti"),
)
RIFERIMENTO = MISURE[0][0]


def separatore(titolo: str) -> None:
    print(f"\n{'=' * 72}\n{titolo}\n{'=' * 72}")


@dataclass
class Rendimento:
    """Cosa ha fatto una squadra in una partita. Niente altro serve."""

    stagione: int
    quando: datetime
    team_id: int
    avversario_id: int
    gol_fatti: int
    gol_subiti: int
    tiri_fatti: int
    tiri_subiti: int
    in_porta_fatti: int
    in_porta_subiti: int
    # `None` quando `/fixtures/statistics` non e' stato comprato per quella
    # stagione, o quando l'API non ce l'ha (Fiorentina-Inter del 6 febbraio
    # 2025, la partita rigiocata, non ha statistiche).
    xg_fatti: float | None = None
    xg_subiti: float | None = None


def raccogli(dataset, unito) -> list[Rendimento]:
    """Una riga per squadra e per partita, solo dove il tabellino c'e'.

    I **gol** si prendono dal risultato della partita e non dal tabellino: e'
    il risultato a essere il fatto, e un autogol non viene contato al
    marcatore. I **tiri** si prendono dal tabellino, che e' l'unico posto in
    cui esistono, sommando i giocatori scesi in campo.

    Si passa da `prestazioni_partita` invece di leggere le buste a mano perche'
    e' li' che vive il filtro sui tabellini provvisori: la fotografia del feed
    live darebbe una squadra con quattro tiri e zero gol, che e' falsa e
    sposterebbe la misura proprio nella direzione che stiamo cercando di
    misurare.
    """
    righe: list[Rendimento] = []
    senza_tabellino = 0

    for fixture in dataset.finished:
        prestazioni = prestazioni_partita(unito, fixture.api_id, fixture.kickoff_utc)
        if not prestazioni:
            senza_tabellino += 1
            continue

        tiri: dict[int, int] = defaultdict(int)
        in_porta: dict[int, int] = defaultdict(int)
        for p in prestazioni:
            tiri[p.team_id] += p.tiri or 0
            in_porta[p.team_id] += p.tiri_in_porta or 0

        xg = xg_partita(unito, fixture.api_id)
        casa, ospite = fixture.home_team_id, fixture.away_team_id
        if casa not in tiri or ospite not in tiri:
            # Un tabellino con una squadra sola non e' confrontabile: i tiri
            # subiti risulterebbero zero per finta.
            senza_tabellino += 1
            continue

        for squadra, avversario, gf, gs in (
            (casa, ospite, fixture.home_goals, fixture.away_goals),
            (ospite, casa, fixture.away_goals, fixture.home_goals),
        ):
            righe.append(Rendimento(
                stagione=fixture.season,
                quando=fixture.kickoff_utc,
                team_id=squadra,
                avversario_id=avversario,
                gol_fatti=gf,
                gol_subiti=gs,
                tiri_fatti=tiri[squadra],
                tiri_subiti=tiri[avversario],
                in_porta_fatti=in_porta[squadra],
                in_porta_subiti=in_porta[avversario],
                xg_fatti=xg.get(squadra),
                xg_subiti=xg.get(avversario),
            ))

    if senza_tabellino:
        print(f"  ({senza_tabellino} partite concluse senza tabellino utilizzabile, escluse)")
    return righe


def xg_partita(unito, fixture_id: int) -> dict[int, float]:
    """L'xG di ogni squadra, dalla busta di `/fixtures/statistics`.

    Vuota quando quella stagione non e' stata comprata: il chiamante deve
    distinguere "non lo so" da "zero", che qui sarebbe una squadra che non ha
    mai tirato in porta.
    """
    blocchi = unito.load_detail(fixture_id, DETTAGLIO_STATISTICHE)
    if not blocchi:
        return {}

    valori: dict[int, float] = {}
    for blocco in blocchi:
        team_id = (blocco.get("team") or {}).get("id")
        if team_id is None:
            continue
        for voce in blocco.get("statistics") or []:
            if voce.get("type") != "expected_goals":
                continue
            grezzo = voce.get("value")
            if grezzo in (None, ""):
                continue
            try:
                valori[int(team_id)] = float(grezzo)
            except (TypeError, ValueError):
                continue
    return valori


def correlazione(coppie: list[tuple[float, float]]) -> float | None:
    """Pearson fra previsione e realta'. `None` se non c'e' abbastanza roba.

    Si restituisce `None` e non zero: "non lo so" e "so che non c'e' legame"
    sono due risposte diverse, ed e' la stessa regola dei pesi.
    """
    if len(coppie) < 30:
        return None
    x = [a for a, _ in coppie]
    y = [b for _, b in coppie]
    if statistics.pstdev(x) == 0 or statistics.pstdev(y) == 0:
        return None
    return statistics.correlation(x, y)


def _per_squadra(righe: list[Rendimento]) -> dict[tuple[int, int], list[Rendimento]]:
    """Le partite di ogni squadra, per stagione, in ordine di calendario."""
    indice: dict[tuple[int, int], list[Rendimento]] = defaultdict(list)
    for r in righe:
        indice[(r.stagione, r.team_id)].append(r)
    for partite in indice.values():
        partite.sort(key=lambda r: r.quando)
    return indice


def _media(partite: list[Rendimento], campo: str) -> float:
    return sum(getattr(p, campo) for p in partite) / len(partite)


def _misurabile(righe: list[Rendimento], campo: str) -> bool:
    """Il dato c'e' su TUTTE le righe, o non si confronta affatto.

    Meta' delle righe con l'xG e meta' senza darebbe due misure calcolate su
    partite diverse, che e' il modo piu' facile di far vincere quella
    sbagliata. Chi chiama filtra prima le righe (vedi `solo_complete`); qui si
    controlla e basta.
    """
    return all(getattr(r, campo, None) is not None for r in righe)


def solo_complete(righe: list[Rendimento]) -> tuple[list[Rendimento], list[str]]:
    """Le stagioni che un dato copre quasi per intero, piu' quelle scartate.

    Si giudica per **stagione** e non per riga, ed e' il punto delicato di
    tutta la prova. L'xG del 2022/23 c'e' solo su 201 partite su 381:
    API-Football ha cominciato a pubblicarlo a campionato in corso. Togliendo
    le righe scoperte, le partite rimaste di una squadra non sono piu'
    consecutive, e "le sei precedenti" diventa "le sei precedenti di cui
    abbiamo notizia", che possono coprire mezza stagione. La finestra
    misurerebbe una cosa diversa da quella che dichiara.

    Ma "per intero" alla lettera e' troppo: il 2024/25 ha l'xG su 379 partite
    su 380 — manca Fiorentina-Inter del 6 febbraio, la partita rigiocata, che
    l'API non copre — e buttare una stagione per un buco solo costa molto piu'
    di quanto protegga. Da qui la soglia: sopra `COPERTURA_MINIMA` la stagione
    entra e le poche righe scoperte si scartano, perche' un buco isolato
    sposta un paio di finestre per due squadre; sotto, i buchi sono
    sistematici e la stagione esce tutta.
    """
    campi = [campo for _, campo, _ in MISURE] + [campo for _, _, campo in MISURE]
    completa = lambda r: all(getattr(r, c, None) is not None for c in campi)

    tenute: list[Rendimento] = []
    scartate: list[str] = []
    for stagione in sorted({r.stagione for r in righe}):
        sotto = [r for r in righe if r.stagione == stagione]
        buone = [r for r in sotto if completa(r)]
        copertura = len(buone) / len(sotto)
        if copertura >= COPERTURA_MINIMA:
            tenute.extend(buone)
        else:
            scartate.append(f"{stagione} (coperta al {copertura * 100:.0f}%)")
    return tenute, scartate


def prova_squadra(righe: list[Rendimento], finestra: int) -> dict[str, dict[str, list]]:
    """Attacco e difesa: le N partite precedenti prevedono la prossima?

    La partita corrente non entra mai nella sua stessa previsione — sarebbe
    barare, ed e' lo stesso errore che il registro dei pronostici evita per
    costruzione.
    """
    misure = [m for m in MISURE if _misurabile(righe, m[1])]
    attacco: dict[str, list] = {nome: [] for nome, _, _ in misure}
    difesa: dict[str, list] = {nome: [] for nome, _, _ in misure}

    for partite in _per_squadra(righe).values():
        for i in range(finestra, len(partite)):
            passato, futuro = partite[i - finestra:i], partite[i]
            for nome, fatti, subiti in misure:
                attacco[nome].append((_media(passato, fatti), futuro.gol_fatti))
                difesa[nome].append((_media(passato, subiti), futuro.gol_subiti))

    return {"attacco": attacco, "difesa": difesa}


def prova_totali(righe: list[Rendimento], finestra: int) -> dict[str, list]:
    """I gol totali di una partita, che e' la domanda dell'Over/Under.

    La previsione si costruisce come la costruirebbe il modello: l'attacco di
    una squadra piu' la difesa dell'altra, nei due sensi, mediati. Cambia solo
    con che cosa si misurano attacco e difesa — gol, tiri o tiri in porta —
    cosi' il confronto isola il dato e non la formula.
    """
    indice = _per_squadra(righe)
    posizione = {
        (r.stagione, r.team_id, r.quando): i
        for partite in indice.values()
        for i, r in enumerate(partite)
    }

    quali = [m for m in MISURE if _misurabile(righe, m[1])]
    misure: dict[str, list] = {nome: [] for nome, _, _ in quali}
    viste: set = set()

    for r in righe:
        # Ogni partita ha due righe, una per squadra: se ne guarda una sola.
        chiave = (r.stagione, tuple(sorted((r.team_id, r.avversario_id))), r.quando)
        if chiave in viste:
            continue
        viste.add(chiave)

        i = posizione[(r.stagione, r.team_id, r.quando)]
        j = posizione.get((r.stagione, r.avversario_id, r.quando))
        if j is None or i < finestra or j < finestra:
            continue

        noi = indice[(r.stagione, r.team_id)][i - finestra:i]
        loro = indice[(r.stagione, r.avversario_id)][j - finestra:j]
        totale_vero = r.gol_fatti + r.gol_subiti

        for nome, fatti, subiti in quali:
            atteso = (
                _media(noi, fatti) + _media(loro, subiti)
                + _media(loro, fatti) + _media(noi, subiti)
            ) / 2
            misure[nome].append((atteso, totale_vero))

    return misure


def stampa_tabella(titolo: str, misure: dict[str, list], riferimento: str) -> None:
    print(f"\n{titolo}")
    base = correlazione(misure[riferimento])
    for nome, coppie in misure.items():
        r = correlazione(coppie)
        if r is None:
            print(f"  {nome:24s}  non misurabile ({len(coppie)} osservazioni)")
            continue
        scarto = ""
        if base is not None and nome != riferimento:
            scarto = f"   ({r - base:+.3f} rispetto ai gol)"
        print(f"  {nome:24s}  r = {r:.3f}   n = {len(coppie)}{scarto}")


def stampa_tenuta(righe: list[Rendimento]) -> dict[str, dict[int, float]]:
    """Lo stesso confronto su piu' finestre. Restituisce i guadagni sui totali.

    E' la parte che distingue un segnale da una coincidenza: un vantaggio vero
    non dipende da quante partite si guardano indietro.
    """
    nomi = [nome for nome, campo, _ in MISURE if _misurabile(righe, campo)]
    intestazione = "  ".join(f"{n:>7s}" for n in nomi)
    print(f"\n  finestra   n      {intestazione}")
    guadagni: dict[str, dict[int, float]] = {n: {} for n in nomi if n != RIFERIMENTO}

    for finestra in FINESTRE_DI_CONTROLLO:
        totali = prova_totali(righe, finestra)
        valori = {n: correlazione(totali[n]) for n in nomi}
        if any(v is None for v in valori.values()):
            continue
        for n in guadagni:
            guadagni[n][finestra] = valori[n] - valori[RIFERIMENTO]
        riga = "  ".join(f"{valori[n]:>7.3f}" for n in nomi)
        print(f"  {finestra:^8} {len(totali[RIFERIMENTO]):<5}  {riga}")

    print(f"\n  (sono i GOL TOTALI della partita, cioe' l'Over/Under)")
    return guadagni


def stampa_per_stagione(righe: list[Rendimento], finestra: int, sfidante: str) -> None:
    """Lo stesso confronto una stagione per volta, sul dato che ha vinto.

    Un vantaggio che viene tutto da un anno solo non e' un vantaggio: e' quel
    che quell'anno aveva di particolare.
    """
    print(f"\n  stagione   n      {RIFERIMENTO:>7s}  {sfidante:>7s}   differenza")
    for stagione in sorted({r.stagione for r in righe}):
        sotto = [r for r in righe if r.stagione == stagione]
        misure = prova_totali(sotto, finestra)
        g = correlazione(misure.get(RIFERIMENTO, []))
        t = correlazione(misure.get(sfidante, []))
        if g is None or t is None:
            n = len(misure.get(RIFERIMENTO, []))
            print(f"  {stagione:^8} {n:<5}  troppo poche partite")
            continue
        print(f"  {stagione:^8} {len(misure[RIFERIMENTO]):<5}  {g:>7.3f}  {t:>7.3f}"
              f"     {t - g:+.3f}")


def verdetto(
    righe: list[Rendimento],
    finestra: int,
    guadagni: dict[str, dict[int, float]],
) -> tuple[str, str]:
    """Si va avanti o si smette? La domanda decisiva e' l'Over/Under.

    Restituisce (sfidante migliore, testo). Tre condizioni, e servono tutte: lo
    sfidante deve staccare i gol, il vantaggio deve tenere su tutte le
    finestre, e lo sfidante deve prevedere davvero qualcosa. La terza e' quella
    che si dimentica: si puo' vincere un confronto fra due numeri che non
    dicono niente nessuno dei due.
    """
    totali = prova_totali(righe, finestra)
    base = correlazione(totali.get(RIFERIMENTO, []))
    if base is None:
        return RIFERIMENTO, "Non ci sono abbastanza dati per rispondere."

    punteggi = {
        nome: correlazione(coppie)
        for nome, coppie in totali.items()
        if nome != RIFERIMENTO and correlazione(coppie) is not None
    }
    if not punteggi:
        return RIFERIMENTO, "Non c'e' nessuno sfidante misurabile."

    sfidante = max(punteggi, key=lambda n: punteggi[n])
    r = punteggi[sfidante]

    stacca = r - base >= GUADAGNO_CHE_VALE
    tiene = bool(guadagni.get(sfidante)) and all(g > 0 for g in guadagni[sfidante].values())
    utile = r >= CORRELAZIONE_MINIMA_UTILE

    if stacca and tiene and utile:
        return sfidante, (
            f"VINCE {sfidante.upper()} (r {base:.3f} -> {r:.3f} sui gol totali).\n"
            f"Vale la pena costruirci sopra: si cambia cosa mangia fit_dixon_coles,\n"
            f"non il modello, e si confrontano i due Brier col backtest. Poi va\n"
            f"RICALIBRATO il fattore di compressione del lavoro 7, scelto quando\n"
            f"sui gol non c'era segnale."
        )

    motivi = []
    if not stacca:
        motivi.append(f"il migliore e' {sfidante} e sui gol totali guadagna "
                      f"{r - base:+.3f}, sotto il {GUADAGNO_CHE_VALE:+.2f} "
                      "che varrebbe la pena")
    if not tiene:
        motivi.append(f"{sfidante} non tiene su tutte le finestre")
    if not utile:
        motivi.append(f"e comunque r = {r:.3f}, cioe' da solo non spiega quasi "
                      "niente di quel che succede in una partita")

    return sfidante, (
        "NESSUNO BATTE I GOL: " + "; ".join(motivi) + f".\n"
        f"\n"
        f"E' la risposta piatta prevista nell'insidia del lavoro 8, e adesso lo e'\n"
        f"anche per l'xG, che era l'ultima strada. Sui gol di questo campionato non\n"
        f"c'e' segnale da trovare: il lavoro 8 si chiude, e la compressione del\n"
        f"lavoro 7 resta la risposta giusta: toglie un danno invece di\n"
        f"aggiungere una capacita' che i dati non hanno."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="I tiri prevedono i gol meglio dei gol? Zero richieste."
    )
    parser.add_argument(
        "--finestra",
        type=int,
        default=FINESTRA_PREDEFINITA,
        help=f"quante partite precedenti guardare (default {FINESTRA_PREDEFINITA})",
    )
    args = parser.parse_args()

    settings = load_settings()
    client = build_client(settings)
    store, _ = build_store(settings)
    storici = build_storico_stores(settings)
    # Le stagioni importate da un file restano fuori dagli archivi grezzi: hanno
    # i risultati e non avranno mai i tabellini. Nel dataset invece ci sono,
    # perche' li' servono a completare il calendario.
    unito = StoreUnito(stores=[store, *storici.values()])

    separatore("COSA C'E' SUL DISCO")
    dataset = load_dataset(
        client, store, settings.seasons, settings.league_id,
        storici=storici,
        importati=build_importato_stores(settings),
    )
    print(f"Partite concluse conosciute: {len(dataset.finished)}")
    righe = raccogli(dataset, unito)
    print(f"Righe squadra-partita col tabellino: {len(righe)}")

    # Il confronto dev'essere fra dati che vedono le STESSE partite: una riga
    # senza xG non e' una riga con xG zero.
    complete, scartate = solo_complete(righe)
    if complete:
        righe = complete
        if scartate:
            quali = ", ".join(scartate)
            print(f"  (stagioni escluse perche' un dato non le copre per intero: {quali})")
    else:
        print("  (nessuna stagione ha l'xG per intero: si confrontano solo i tiri.")
        print("   Per averlo: riempi_storico.py --solo statistics)")

    stagioni = sorted({r.stagione for r in righe})
    print(f"Stagioni: {', '.join(str(s) for s in stagioni) or 'nessuna'}")

    if len(righe) < 200:
        print(
            "\nTroppo poco per rispondere. Servono i tabellini di almeno una\n"
            "stagione: vedi il passo (a) del giorno del piano."
        )
        return 1

    separatore(f"LE ULTIME {args.finestra} PARTITE PREVEDONO LA PROSSIMA?")
    squadra = prova_squadra(righe, args.finestra)
    stampa_tabella("Quanti gol FARA' la squadra:", squadra["attacco"], RIFERIMENTO)
    stampa_tabella("Quanti gol SUBIRA' la squadra:", squadra["difesa"], RIFERIMENTO)
    stampa_tabella(
        "Quanti gol TOTALI avra' la partita (e' questa che conta):",
        prova_totali(righe, args.finestra), RIFERIMENTO,
    )
    print(
        "\nr e' la correlazione fra quel che il dato prevedeva e quel che e'\n"
        "successo davvero. Zero vuol dire nessun legame."
    )

    separatore("TIENE, O E' UNA COINCIDENZA?")
    guadagni = stampa_tenuta(righe)
    sfidante, testo = verdetto(righe, args.finestra, guadagni)
    stampa_per_stagione(righe, args.finestra, sfidante)

    separatore("VERDETTO")
    print(testo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
