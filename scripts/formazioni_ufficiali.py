"""Pronostici rifatti con le formazioni ufficiali. Da eseguire vicino al via.

Le formazioni escono una mezz'ora prima del calcio d'inizio, e il registro
rifiuta qualsiasi pronostico datato dopo. Fra quei due istanti c'e' tutta la
finestra utile, ed e' per questo che questo comando NON sta nella raccolta
giornaliera: quella gira quattro volte al giorno a orari fissi, e nessuno di
quegli orari cade dove serve.

    python scripts/formazioni_ufficiali.py             # legge, calcola, registra
    python scripts/formazioni_ufficiali.py --stato     # com'e' messa la macchina, 0 richieste
    python scripts/formazioni_ufficiali.py --entro 45  # solo le partite entro 45 minuti

LA MACCHINA NASCE SPENTA
Il peso di un giocatore si misura dai tabellini, e finche' sul disco non ce ne
sono abbastanza questo comando non scrive nulla — dice perche', e non spende
quasi niente. Si accende da sola quando i dati bastano: `--stato` risponde
esattamente a "quanto manca". Vedi `data/pesi.py` per il metodo e
`data/ufficiali.py` per il perche' del "non scrive nulla".

Uscita 0 sempre che il giro sia arrivato in fondo: una partita senza formazioni
non e' un guasto, e' presto.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from serie_a_analyzer.config import (  # noqa: E402
    ConfigError,
    build_prediction_store,
    build_store,
    build_storico_stores,
    load_settings,
)
from serie_a_analyzer.data.pesi import (  # noqa: E402
    FATTORE_PREDEFINITO,
    GIORNI_FINESTRA,
    MINIMO_PARTITE,
    partite_recenti,
    pesi_squadra,
)
from serie_a_analyzer.data.ingest import current_season  # noqa: E402
from serie_a_analyzer.data.parsers import parse_fixtures  # noqa: E402
from serie_a_analyzer.data.players import costruisci_archivio  # noqa: E402
from serie_a_analyzer.data.store import StoreUnito  # noqa: E402
from serie_a_analyzer.data.ufficiali import (  # noqa: E402
    FINESTRA_MINUTI,
    partite_imminenti,
    pronostica_con_formazioni,
)
from serie_a_analyzer.web.engine import Engine  # noqa: E402

logging.basicConfig(level=logging.INFO, format="  %(message)s")

logger = logging.getLogger("formazioni")


def separatore(titolo: str) -> None:
    print(f"\n{'=' * 72}\n{titolo}\n{'=' * 72}")


def _archivio_recente(motore: Engine, dataset, adesso: datetime):
    """Le prestazioni della finestra recente, da tutti gli archivi grezzi.

    Le stagioni importate da un file restano fuori: hanno i risultati e non
    avranno mai i tabellini, e contarle direbbe "copertura parziale" su un
    campionato coperto per intero. E' la stessa regola gia' applicata alle
    schede giocatore.

    Si guarda solo la **stagione in corso**: il mercato estivo e' il momento in
    cui una rosa cambia di piu', e i 120 giorni da soli, ad agosto, arrivano
    ad aprile. Vedi `partite_recenti`.
    """
    storici = list(build_storico_stores(motore.settings).values())
    unito = StoreUnito(stores=[motore.store, *storici])
    recenti = partite_recenti(
        dataset.fixtures, adesso,
        giorni=GIORNI_FINESTRA,
        stagione=current_season(),
    )
    return costruisci_archivio(unito, recenti), len(recenti)


def _riga_registrata(registro, fixture, nome, mode) -> object | None:
    """Il pronostico congelato per una partita, come lo racconta un messaggio.

    Si legge dal registro e non dai calcoli appena fatti: il messaggio deve
    dire cio' che e' stato SCRITTO, non cio' che si potrebbe riscrivere. Se le
    due cose divergessero, il posto in cui accorgersene e' questo.
    """
    from serie_a_analyzer.data.predictions import MODE_SECCO
    from serie_a_analyzer.notifiche import RigaPronostico

    riga = registro.ultimo_per_partita(fixture.api_id, mode=mode)
    if riga is None:
        return None
    prob = {
        m["selection"]: m["probability"]
        for m in registro.mercati(riga["id"]) if m["market"] == "1x2"
    }
    if not prob:
        return None
    return RigaPronostico(
        ora=fixture.kickoff_utc.astimezone().strftime("%H:%M"),
        casa=nome(fixture.home_team_id),
        ospite=nome(fixture.away_team_id),
        prob_1=prob.get("home"), prob_x=prob.get("draw"), prob_2=prob.get("away"),
        gol_casa=riga.get("lambda_home") if mode != MODE_SECCO else None,
        gol_ospite=riga.get("lambda_away") if mode != MODE_SECCO else None,
    )


def _avvisa(settings, dataset, archivio, registro, partite) -> list[str]:
    """I due messaggi di questo comando, e nessun altro.

    Il primo — il pronostico con le formazioni — arriva solo se una riga e'
    stata davvero registrata. Il secondo arriva una volta sola nella vita del
    progetto: il giorno in cui i tabellini bastano e la macchina si accende da
    sola. Senza, quel giorno passerebbe inosservato.
    """
    from serie_a_analyzer.config import build_registro_invii, build_telegram
    from serie_a_analyzer.data.predictions import MODE_SECCO, MODE_UFFICIALE
    from serie_a_analyzer.notifiche import (
        allarme_formazioni_accese,
        messaggio_formazioni,
    )

    telegram = build_telegram(settings)
    if not telegram.attivo:
        return []

    invii = build_registro_invii(settings)
    mandati: list[str] = []

    pronte, per_partite = _squadre_pronte(dataset, archivio)
    if pronte and not invii.gia_inviato("formazioni-accese"):
        if telegram.manda(allarme_formazioni_accese(len(pronte), len(per_partite))):
            invii.segna("formazioni-accese")
            mandati.append("formazioni-accese")

    for f in partite:
        chiave = f"formazioni:{f.api_id}"
        if invii.gia_inviato(chiave):
            continue
        riga = _riga_registrata(registro, f, dataset.name, MODE_UFFICIALE)
        if riga is None:
            continue
        secca = _riga_registrata(registro, f, dataset.name, MODE_SECCO)
        if telegram.manda(messaggio_formazioni(riga, secca)):
            invii.segna(chiave)
            mandati.append(chiave)
    return mandati


def _squadre_pronte(dataset, archivio) -> tuple[list[str], list[tuple[str, int]]]:
    """Le squadre di cui i pesi si possono stimare, e quante partite ha ognuna.

    Sta qui e non dentro `_stampa_stato` perche' la stessa risposta serve a due
    cose diverse: dirlo a schermo con `--stato`, e accorgersi del momento in
    cui la macchina si accende — che e' un evento che capita una volta sola,
    mesi dopo che il codice e' stato scritto, e che senza un avviso si
    scoprirebbe per caso.
    """
    pronte: list[str] = []
    per_partite: list[tuple[str, int]] = []
    for team_id, nome in sorted(dataset.team_names.items(), key=lambda kv: kv[1]):
        n = len({p.fixture_id for s in archivio.rosa(team_id) for p in s.prestazioni})
        per_partite.append((nome, n))
        if pesi_squadra(archivio, team_id) is not None:
            pronte.append(nome)
    return pronte, per_partite


def _stampa_stato(dataset, archivio, n_recenti: int, adesso: datetime) -> None:
    separatore("Com'e' messa la macchina")
    print(f"Finestra guardata:      ultimi {GIORNI_FINESTRA} giorni, "
          f"stagione {current_season()} soltanto")
    print(f"Partite concluse:       {n_recenti}")
    # Non tutte le partite concluse possono avere un tabellino: quelle
    # importate da un file di risultati non l'avranno mai, e contarle farebbe
    # sembrare mancante cio' che non e' mai stato ottenibile.
    print(f"Di cui con archivio:    {archivio.partite_concluse}")
    print(f"Di cui col tabellino:   {archivio.partite_con_statistiche}")
    # Senza questa riga due partite sparirebbero dal conto senza motivo
    # apparente, e chi guarda penserebbe a una raccolta mancata invece che a
    # un dato che l'API non ha ancora consolidato.
    if archivio.partite_provvisorie:
        print(f"Ferme a meta' partita:  {archivio.partite_provvisorie} "
              "(l'API non le ha ancora consolidate; si richiedono da sole)")
    print(f"Giocatori conosciuti:   {archivio.giocatori_conosciuti}")

    pronte, per_partite = _squadre_pronte(dataset, archivio)

    print(f"\nSquadre con pesi:       {len(pronte)}/{len(per_partite)} "
          f"(servono {MINIMO_PARTITE} partite con tabellino a testa)")

    if not pronte:
        mancanti = MINIMO_PARTITE - max((n for _, n in per_partite), default=0)
        print("\nLa macchina e' SPENTA, ed e' giusto cosi': senza tabellini i pesi")
        print("sarebbero numeri a caso, e una riga 'formazione ufficiale' identica")
        print("a quella secca sporcherebbe il confronto fra le due modalita'.")
        if mancanti > 0:
            print(f"\nManca ancora qualche giornata: alla squadra messa meglio ne")
            print(f"servono {mancanti}. Si accende da sola, non c'e' niente da fare.")
    else:
        print("\nLa macchina e' ACCESA per queste squadre:")
        for nome in pronte:
            print(f"  {nome}")

    imminenti = partite_imminenti(dataset.upcoming(), now=adesso)
    print(f"\nPartite entro {FINESTRA_MINUTI} minuti: {len(imminenti)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pronostici con le formazioni ufficiali, a ridosso del via"
    )
    parser.add_argument(
        "--stato",
        action="store_true",
        help="dice se i tabellini bastano gia', senza spendere richieste",
    )
    parser.add_argument(
        "--entro",
        type=int,
        default=FINESTRA_MINUTI,
        help=f"minuti dal calcio d'inizio entro cui guardare (default {FINESTRA_MINUTI})",
    )
    parser.add_argument(
        "--fattore",
        type=float,
        default=FATTORE_PREDEFINITO,
        help=(
            f"quanto dell'aggiustamento applicare (default {FATTORE_PREDEFINITO}). "
            "NON e' calibrato: dovra' uscire dal backtest"
        ),
    )
    parser.add_argument(
        "--max-richieste",
        type=int,
        default=None,
        help="tetto di letture delle formazioni (una per partita)",
    )
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n[configurazione] {exc}\n")
        return 1

    adesso = datetime.now(timezone.utc)

    # Prima si guarda se c'e' qualcosa da fare, poi si accende il motore.
    # L'ordine conta: questo comando e' pensato per girare ogni quarto d'ora
    # per mesi, e stimare il modello costa un paio di secondi. Con la finestra
    # vuota — cioe' quasi sempre — deve uscire senza aver letto nulla.
    if not args.stato:
        store, _ = build_store(settings)
        calendario = parse_fixtures(list(store.load_fixtures().values()))
        if not partite_imminenti(calendario, now=adesso, entro_minuti=args.entro):
            logger.info(
                "nessuna partita entro %d minuti: non c'e' niente da fare", args.entro
            )
            return 0

    motore = Engine(settings)
    snap = motore.snapshot()
    if snap.model is None:
        print("\nIl modello non e' pronto: senza stima non c'e' niente da correggere.")
        if snap.error:
            print(f"Motivo: {snap.error}")
        return 1

    archivio, n_recenti = _archivio_recente(motore, snap.dataset, adesso)

    if args.stato:
        _stampa_stato(snap.dataset, archivio, n_recenti, adesso)
        return 0

    separatore("FORMAZIONI UFFICIALI")
    print(f"Budget disponibile: {motore.client.budget.remaining}/{settings.daily_request_limit}")
    print(f"Partite con tabellino nella finestra: {archivio.partite_con_statistiche}")

    esito = pronostica_con_formazioni(
        motore.client, motore.store, build_prediction_store(settings),
        model=snap.model,
        dataset=snap.dataset,
        archivio=archivio,
        now=adesso,
        entro_minuti=args.entro,
        fattore=args.fattore,
        max_richieste=args.max_richieste,
    )

    separatore("Esito")
    print(f"Partite esaminate:    {esito['esaminate']}")
    print(f"Pronostici nuovi:     {esito['registrate']}")
    print(f"Gia' registrate:      {esito['gia_registrate']}")
    print(f"Formazioni non uscite:{esito['senza_formazioni']:>3}")
    print(f"Senza pesi a suff.:   {esito['senza_pesi']}")
    print(f"Squadra senza storia: {esito['senza_modello']}")
    print(f"Gia' iniziate:        {esito['gia_iniziate']}")
    print(f"Letture spese:        {esito['letture']}")

    # Le notifiche per ultime e senza poter far fallire niente: un messaggio
    # perso e' un fastidio, un pronostico non registrato prima del via e' un
    # buco che non si riempie piu'.
    try:
        mandati = _avvisa(
            settings, snap.dataset, archivio, build_prediction_store(settings),
            partite_imminenti(snap.dataset.upcoming(), now=adesso, entro_minuti=args.entro),
        )
        for chiave in mandati:
            print(f"Messaggio mandato:    {chiave}")
    except Exception as exc:  # noqa: BLE001 - vedi sopra: qui non si solleva
        logger.warning("notifiche non mandate: %s", exc)

    if esito["esaminate"] and not esito["registrate"] and esito["senza_pesi"]:
        print("\nNessun pronostico scritto perche' i tabellini non bastano ancora.")
        print("E' il comportamento voluto. `--stato` dice quanto manca.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
