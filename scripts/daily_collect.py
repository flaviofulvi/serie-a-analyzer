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

from serie_a_analyzer.config import (
    ConfigError,
    build_client,
    build_prediction_store,
    build_registro_invii,
    build_store,
    build_telegram,
    load_settings,
)
from serie_a_analyzer.data.daily import (
    MINUTI_DOPO_IL_VIA,
    MINUTI_PRIMA_DEL_VIA,
    ORE_DI_MARGINE_PER_I_DETTAGLI,
    backfill_details,
    collect_season_calendar,
    collect_window,
    dettagli_possono_aspettare,
    momento_utile,
)
from serie_a_analyzer.data.indisponibili import (
    ORE_DI_RACCOLTA as ORE_INDISPONIBILI,
    raccogli_indisponibili,
)
from serie_a_analyzer.data.odds import ORE_DI_RACCOLTA, raccogli_quote
from serie_a_analyzer.data.parsers import parse_fixtures
from serie_a_analyzer.data.store import tabellini_arresi, tabellini_pronti

logging.basicConfig(level=logging.INFO, format="  %(message)s")

# Da quanti giorni indietro cercare buchi nell'archivio.
FINESTRA_CONTROLLO_BUCHI = 30

# Quanto avanti guardare per quote e indisponibili quando si raccoglie a
# ridosso di una partita. Un'ora copre le partite che stanno per cominciare e
# lascia fuori quelle di domani, che verranno lette al loro momento: leggerle
# adesso significherebbe rileggerle poi, pagando due volte lo stesso dato.
ORE_VIGILIA = 1

# Quanto avanti guardano invece le due corse a orario fisso. Il loro compito
# qui non e' prendere il dato buono — quello lo prende la corsa a ridosso del
# via — ma fare da PRESIDIO: se il computer e' spento nei 40 minuti prima del
# fischio, le quote di quella partita sono perse per sempre, e una lettura
# lontana vale piu' di nessuna lettura.
#
# Ventiquattro ore, e non e' un numero tondo a caso: e' "tutto cio' che
# comincia prima che io ripassi". Guardare piu' avanti significherebbe leggere
# quote che verranno rilette domani mattina dal presidio di domani, pagando
# due volte lo stesso dato — che e' esattamente il difetto degli orari fissi
# da cui si e' partiti.
ORE_PRESIDIO = 24


def separatore(titolo: str) -> None:
    print(f"\n{'=' * 72}\n{titolo}\n{'=' * 72}")


def _ultima_raccolta(settings) -> datetime | None:
    """Quando e' finita bene l'ultima raccolta, secondo il marcatore su disco.

    Lo scrive `run_daily.ps1` in ora locale. Serve all'allarme "la raccolta e'
    ferma", che e' il modo in cui ci si accorge che il computer e' rimasto
    spento o che l'attivita' pianificata non parte piu'.
    """
    marcatore = settings.store_dir.parent / "logs" / "ultima-esecuzione.txt"
    try:
        testo = marcatore.read_text(encoding="utf-8-sig").strip()
        return datetime.strptime(testo, "%Y-%m-%d %H:%M:%S").astimezone()
    except (OSError, ValueError):
        return None


def _righe_esito(registro_pronostici, partite) -> list:
    """Le partite finite col pronostico che era stato congelato prima del via.

    Legge dal registro e non dal modello: il resoconto deve raccontare cosa il
    sistema aveva DETTO, non cosa direbbe oggi sapendo com'e' finita.
    """
    from serie_a_analyzer.notifiche import RigaEsito

    righe = []
    for f in partite:
        if not f.is_finished:
            continue
        prob = {}
        riga = registro_pronostici.ultimo_per_partita(f.api_id)
        if riga:
            for m in registro_pronostici.mercati(riga["id"]):
                if m["market"] == "1x2":
                    prob[m["selection"]] = m["probability"]
        righe.append(RigaEsito(
            casa=f.home_team_name, ospite=f.away_team_name,
            gol_casa=f.home_goals, gol_ospite=f.away_goals,
            prob_1=prob.get("home"), prob_x=prob.get("draw"), prob_2=prob.get("away"),
        ))
    return righe


def _righe_pronostico(settings, partite) -> list:
    """I pronostici delle partite di oggi, stimando il modello.

    E' l'unica parte cara di tutto il giro — un paio di secondi — ed e' per
    questo che viene chiamata solo quando c'e' davvero un messaggio da
    mandare, cioe' una volta al giorno e non a ogni esecuzione.
    """
    from serie_a_analyzer.notifiche import RigaPronostico
    from serie_a_analyzer.web.engine import Engine

    motore = Engine(settings)
    snap = motore.snapshot()
    righe = []
    for f in partite:
        prob = motore.predict(f.home_team_id, f.away_team_id)
        attesi = motore.expected_goals(f.home_team_id, f.away_team_id)
        risultato = None
        if prob is not None:
            casa, ospite = prob.top_scorelines[0][0]
            risultato = f"{casa}-{ospite}"
        righe.append(RigaPronostico(
            ora=f.kickoff_utc.astimezone().strftime("%H:%M"),
            casa=snap.dataset.name(f.home_team_id),
            ospite=snap.dataset.name(f.away_team_id),
            prob_1=prob.home_win if prob else None,
            prob_x=prob.draw if prob else None,
            prob_2=prob.away_win if prob else None,
            gol_casa=attesi[0] if attesi else None,
            gol_ospite=attesi[1] if attesi else None,
            risultato_probabile=risultato,
            motivo=None if prob else "senza storia sufficiente: nessun pronostico",
        ))
    return righe


def _notifica(settings, client, store, reports=()) -> list[str]:
    """Manda i messaggi che non sono ancora stati mandati oggi."""
    from serie_a_analyzer.notifiche import invia_quel_che_serve

    telegram = build_telegram(settings)
    if not telegram.attivo:
        return []

    registro_pronostici = build_prediction_store(settings)
    partite = parse_fixtures(list(store.load_fixtures().values()))
    falliti = [r.day.isoformat() for r in reports if r.error]

    # I tabellini su cui il sistema ha smesso di provare, e quelli arrivati
    # per davvero. Si calcolano qui, dove c'e' l'archivio: `notifiche.py`
    # riceve partite, non buste.
    fermi = set(tabellini_arresi(store, (f.api_id for f in partite)))
    pronti = tabellini_pronti(store, (f.api_id for f in partite))

    return invia_quel_che_serve(
        telegram,
        build_registro_invii(settings),
        partite,
        pronostici=lambda del_giorno: _righe_pronostico(settings, del_giorno),
        quotate=registro_pronostici.partite_quotate(),
        esiti_congelati=lambda del_giorno: _righe_esito(registro_pronostici, del_giorno),
        budget=client.budget,
        ultima_raccolta=_ultima_raccolta(settings),
        giorni_falliti=falliti,
        errore_raccolta=next((r.error for r in reports if r.error), None),
        tabellini_fermi=[f for f in partite if f.api_id in fermi],
        tabellini_pronti=pronti,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Raccolta giornaliera della Serie A")
    parser.add_argument(
        "--solo-al-momento-giusto",
        action="store_true",
        help=f"non fare niente se non c'e' una partita che comincia entro "
             f"{MINUTI_PRIMA_DEL_VIA} minuti o che e' finita da poco. Pensato "
             f"per essere lanciato spesso: quando non e' il momento esce "
             f"subito senza spendere una richiesta",
    )
    parser.add_argument(
        "--entro-ore",
        type=int,
        default=None,
        help="quanto avanti guardare per quote e indisponibili "
             f"(default: {ORE_PRESIDIO}, oppure {ORE_VIGILIA} a ridosso "
             f"di una partita; il massimo utile e' {ORE_DI_RACCOLTA}, oltre "
             "il quale i bookmaker non hanno ancora pubblicato)",
    )
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="salta formazioni, statistiche giocatori ed eventi",
    )
    parser.add_argument(
        "--dettagli-subito",
        action="store_true",
        help=f"prendi i dettagli anche nelle ultime {ORE_DI_MARGINE_PER_I_DETTAGLI} "
             "ore prima che il budget si azzeri, che di norma vengono lasciate "
             "alla raccolta del mattino",
    )
    parser.add_argument(
        "--max-detail-requests",
        type=int,
        default=None,
        help="tetto di richieste da spendere in dettagli (default: nessuno)",
    )
    parser.add_argument(
        "--no-calendario",
        action="store_true",
        help="salta il calendario di stagione (1 richiesta al giorno, il "
             "resto delle volte arriva dalla cache)",
    )
    parser.add_argument(
        "--no-quote",
        action="store_true",
        help="salta le quote dei bookmaker",
    )
    parser.add_argument(
        "--max-quote-requests",
        type=int,
        default=15,
        help="tetto di richieste da spendere in quote (una per partita)",
    )
    parser.add_argument(
        "--no-indisponibili",
        action="store_true",
        help="salta infortunati e squalificati",
    )
    parser.add_argument(
        "--max-indisponibili-requests",
        type=int,
        default=10,
        help="tetto di richieste da spendere in indisponibili (una per partita)",
    )
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n[configurazione] {exc}\n")
        return 1

    client = build_client(settings)
    store, journal = build_store(settings)

    # --- e' il momento? ------------------------------------------------------
    #
    # Chi lancia questo script ogni quarto d'ora non vuole quattro raccolte
    # complete al giorno: vuole che si svegli quando c'e' qualcosa da prendere
    # e dorma il resto del tempo. Il momento decide anche COSA prendere, ed e'
    # la parte che fa risparmiare: a ridosso del via contano le quote e gli
    # indisponibili, dopo contano i dettagli.
    momento = None
    # Due finestre e non una: quote e indisponibili hanno oggi lo stesso
    # valore di default, ma sono due decisioni diverse prese in due moduli
    # diversi, e unificarle qui le farebbe divergere in silenzio il giorno in
    # cui una delle due cambia.
    ore_quote = args.entro_ore if args.entro_ore is not None else ORE_PRESIDIO
    ore_fuori = args.entro_ore if args.entro_ore is not None else ORE_PRESIDIO
    if args.solo_al_momento_giusto:
        momento = momento_utile(parse_fixtures(list(store.load_fixtures().values())))
        if momento is None:
            print(
                "Non e' il momento: nessuna partita comincia entro "
                f"{MINUTI_PRIMA_DEL_VIA} minuti ne' e' finita da circa "
                f"{MINUTI_DOPO_IL_VIA // 60}h{MINUTI_DOPO_IL_VIA % 60:02d}. "
                "Zero richieste spese."
            )
            return 0
        # Il calendario non entra mai nelle corse a ridosso del via, in
        # nessuno dei due momenti. Quelle valgono quanto sono vicine al
        # fischio, e infilarci dentro una cosa che non ha nessuna fretta —
        # il calendario cambia con settimane di preavviso — e' il modo di far
        # saltare quella che la fretta ce l'ha davvero.
        args.no_calendario = True
        if momento == "vigilia":
            # I dettagli non hanno fretta — dall'id si leggono quando si
            # vuole — e qui ruberebbero il posto alle quote, che dopo il
            # fischio non esistono piu'.
            args.no_details = True
            if args.entro_ore is None:
                ore_quote = ore_fuori = ORE_VIGILIA
        else:
            # A partita finita non c'e' piu' niente da quotare, e la lista
            # degli indisponibili letta adesso saprebbe chi non e' sceso in
            # campo davvero: si prende al prossimo giro, prima del via.
            args.no_quote = True
            args.no_indisponibili = True

    # --- i dettagli possono aspettare? --------------------------------------
    #
    # Vale per ogni raccolta e non solo per il momento "dopo": anche la corsa
    # fissa delle 23:45 cade in questa finestra, e se prendesse lei i
    # tabellini del posticipo lo spostamento al mattino non esisterebbe.
    # Il controllo sta qui, dopo il momento, perche' la vigilia ha gia' detto
    # la sua — li' i dettagli si saltano per un motivo diverso e piu' urgente,
    # cioe' non rubare il posto alle quote.
    dettagli_rimandati = False
    if not args.no_details and not args.dettagli_subito and dettagli_possono_aspettare():
        args.no_details = True
        dettagli_rimandati = True

    separatore("RACCOLTA GIORNALIERA")
    print(f"Budget disponibile: {client.budget.remaining}/{client.budget.limite_in_vigore}")
    print(f"Archivio:           {settings.store_dir}")
    if dettagli_rimandati:
        print(
            f"Dettagli:           rimandati al mattino (meno di "
            f"{ORE_DI_MARGINE_PER_I_DETTAGLI}h al reset del budget). "
            "Si rileggono dall'id: niente va perso."
        )
    if momento == "vigilia":
        print("Momento:            vigilia — quote e indisponibili delle partite imminenti")
    elif momento == "dopo":
        cosa = "risultati" if dettagli_rimandati else "risultati e dettagli"
        print(f"Momento:            dopo — {cosa} delle partite appena finite")

    reports = collect_window(
        client, store, journal,
        league_id=settings.league_id,
        with_details=not args.no_details,
        max_detail_requests=args.max_detail_requests,
    )

    # --- il calendario di tutta la stagione ----------------------------------
    #
    # Sta qui e non piu' in basso perche' e' la stessa cosa che fa la finestra:
    # catturare gli id delle partite. Che questi non scadano — a differenza di
    # quelli letti giorno per giorno — non lo rende meno importante: e' l'unico
    # modo di sapere con settimane di anticipo chi gioca contro chi, e quindi
    # di scrivere un pronostico prima delle ultime ore.
    #
    # Sta pero' DOPO la finestra, che e' l'unica cosa davvero irrecuperabile.
    calendario = None
    if not args.no_calendario:
        calendario = collect_season_calendar(
            client, store, league_id=settings.league_id,
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

    if calendario is not None:
        separatore("Calendario della stagione")
        if calendario.error:
            print(f"Non scaricato:      {calendario.error}")
        elif calendario.n_serie_a == 0:
            print("Nessuna partita:    il filtro league+season non risponde piu'.")
            print("                    Non e' un guasto e non manca niente: la "
                  "finestra")
            print("                    di tre giorni continua da sola, come ha "
                  "sempre fatto.")
            print("                    Si puo' togliere la richiesta con "
                  "--no-calendario.")
        else:
            print(f"Partite in cartellone: {calendario.n_serie_a}")
            print(f"Nuove:                 {calendario.new_fixtures}")
            print(f"Aggiornate:            {calendario.updated_fixtures}")
            print(
                f"Richieste spese:       {calendario.requests_spent}"
                + ("  (dalla cache: gia' letto oggi)" if not calendario.requests_spent else "")
            )

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

    # --- quote del mercato ---------------------------------------------------
    #
    # Vanno prese prima della partita e basta: API-Football non le archivia, e
    # dopo il fischio d'inizio non esistono piu'. Sono il metro di paragone del
    # modello, quindi valgono qualche richiesta — ma dopo i dati delle partite,
    # mai prima.
    if not args.no_quote:
        partite = parse_fixtures(list(store.load_fixtures().values()))
        quote = raccogli_quote(
            client, build_prediction_store(settings), partite,
            entro_ore=ore_quote,
            max_richieste=args.max_quote_requests,
        )
        if quote["richieste"]:
            separatore("Quote del mercato")
            print(f"Partite quotate:    {quote['partite']}")
            print(f"Quote archiviate:   {quote['quote']}")
            print(f"Quote marcatori:    {quote['marcatori']}")
            print(f"Senza quote ancora: {quote['senza_quote']}")
            print(f"Richieste spese:    {quote['richieste']}")

    # --- infortunati e squalificati ------------------------------------------
    #
    # Per ultimi, ed e' voluto: sono l'unica cosa raccolta qui che si puo'
    # rileggere anche a partita finita (verificato su una gara del 2024). Se il
    # budget e' agli sgoccioli, e' giusto che a saltare siano loro e non le
    # quote, che dopo il fischio d'inizio non esistono piu'.
    if not args.no_indisponibili:
        partite = parse_fixtures(list(store.load_fixtures().values()))
        fuori = raccogli_indisponibili(
            client, build_prediction_store(settings), partite,
            entro_ore=ore_fuori,
            max_richieste=args.max_indisponibili_requests,
        )
        if fuori["letture"]:
            separatore("Infortunati e squalificati")
            print(f"Partite controllate: {fuori['partite']}")
            print(f"Indisponibili:       {fuori['indisponibili']}")
            print(f"Senza lista ancora:  {fuori['senza_lista']}")
            print(f"Letture:             {fuori['letture']}")
            print(f"Richieste spese:     {fuori['richieste']}")

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

    # --- notifiche ----------------------------------------------------------
    #
    # Per ultime, e senza poter far fallire niente: un messaggio perso e' un
    # fastidio, una raccolta interrotta puo' costare un giorno di partite.
    try:
        mandati = _notifica(settings, client, store, reports)
        if mandati:
            separatore("Messaggi mandati")
            for chiave in mandati:
                print(f"  {chiave}")
    except Exception as exc:  # noqa: BLE001 - vedi sopra: qui non si solleva
        logging.getLogger(__name__).warning("notifiche non mandate: %s", exc)

    # --- riepilogo ----------------------------------------------------------
    separatore("Archivio")
    s = store.stats()
    print(f"Partite di Serie A in archivio: {s['partite']}")
    print(f"Partite con dettagli:           {s['con_dettagli']}")
    print(f"Giorni raccolti finora:         {len(raccolti)}")
    print(f"Budget usato oggi:              {client.budget.used}/{client.budget.limite_in_vigore}")
    print(f"Per priorita':                  {client.budget.usage_by_priority()}")

    if errori:
        print(f"\n{errori} giorno/i non raccolti: riprovare.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
