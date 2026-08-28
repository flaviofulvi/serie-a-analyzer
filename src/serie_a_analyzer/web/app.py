"""Applicazione web: hub delle partite e scheda del singolo incontro.

    uvicorn serie_a_analyzer.web.app:app --reload

L'hub e' il cuore dell'applicazione (Fase 6 del progetto). Le altre pagine
verranno appese qui man mano.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..data.classifica import classifica
from ..data.ingest import current_season
from ..data.players import formazioni_partita, gol_partita, prestazioni_partita
from ..data.predictions import MODE_SECCO, MODE_UFFICIALE
from ..data.store import tabellino_provvisorio
from .engine import Congelato, Engine
from .views import (
    blocchi_formazione,
    blocchi_indisponibili,
    confronto_ufficiale,
    formatta_data,
    formatta_giorno,
    griglia_risultati,
    riga_giocatore,
    riga_partita,
    righe_accuratezza,
    righe_calibrazione,
    righe_classifica,
    righe_affidabilita,
    righe_confronto_modi,
    righe_panchina,
    righe_confronto_mercato,
    righe_gol,
    righe_prestazioni,
    righe_registro,
    verdetto_calibrazione,
)

logger = logging.getLogger(__name__)

QUI = Path(__file__).parent
templates = Jinja2Templates(directory=str(QUI / "templates"))

app = FastAPI(title="Serie A Analyzer")
app.mount("/static", StaticFiles(directory=str(QUI / "static")), name="static")

# Un solo motore per processo: dataset e modello restano caldi fra le richieste.
_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine


def _riga(motore: Engine, fixture, nome, adesso: datetime) -> tuple:
    """Una partita come riga, col pronostico preso dalla fonte giusta.

    Quale sia la fonte giusta dipende da **quando** si guarda, ed e' la stessa
    regola che il registro applica in scrittura (`data/predictions.py` rifiuta
    una partita gia' iniziata):

    * prima del fischio d'inizio il modello caldo e' l'unica risposta
      possibile, ed e' anche quella giusta;
    * dopo, l'unica onesta e' quella congelata. Ricalcolare mostrerebbe un
      pronostico che ha gia' visto il risultato — il modello viene ristimato
      su ogni nuova partita, quella compresa — spacciandolo per uno fatto in
      tempo. Non e' un dettaglio di presentazione: e' la differenza fra un
      modello che ci ha preso e uno che sembra averci preso.

    Restituisce anche il ``Congelato``, che la scheda partita usa per la
    griglia dei risultati esatti: deve venire dalla stessa fonte delle
    percentuali, altrimenti la pagina mostrerebbe due pronostici diversi.
    """
    iniziata = fixture.kickoff_utc <= adesso
    congelato: Congelato | None = None

    if iniziata:
        congelato = motore.congelato(fixture.api_id)
        prob = congelato.prob if congelato else None
        gol_attesi = congelato.gol_attesi if congelato else None
    else:
        prob = motore.predict(fixture.home_team_id, fixture.away_team_id)
        gol_attesi = motore.expected_goals(fixture.home_team_id, fixture.away_team_id)

    riga = riga_partita(
        fixture, nome,
        prob=prob,
        gol_attesi=gol_attesi,
        iniziata=iniziata,
        congelato=congelato is not None,
        quando_congelato=(
            formatta_data(congelato.quando)
            if congelato and congelato.quando else None
        ),
    )
    return riga, congelato


def _righe(motore: Engine, fixtures) -> list:
    """Trasforma le partite in righe, aggiungendo il pronostico dove possibile."""
    snap = motore.snapshot()
    nome = snap.dataset.name
    adesso = datetime.now(timezone.utc)
    return [_riga(motore, f, nome, adesso)[0] for f in fixtures]


@app.get("/", response_class=HTMLResponse)
def hub(request: Request):
    """Le prossime partite con i pronostici. Se non ce ne sono, cosa c'e' invece."""
    motore = engine()
    snap = motore.snapshot()

    prossime = _righe(motore, snap.dataset.next_matchday())
    # Dieci: una giornata di Serie A intera, non un troncone di giornata.
    recenti = _righe(motore, snap.dataset.recent_results(limit=10))

    forze = []
    if snap.model:
        for posizione, (squadra, att, dif, netta) in enumerate(
            snap.model.strength_table(), start=1
        ):
            forze.append({
                "posizione": posizione,
                "squadra": squadra,
                "attacco": att,
                "difesa": dif,
                "netta": netta,
            })

    accuratezza = motore.accuratezza()
    confronto_modi = motore.confronto_modi()
    # La seconda lettura degli stessi esiti: non "quanto vale il modello" ma
    # "la fiducia che dichiara e' meritata". Sta accanto all'accuratezza e non
    # al suo posto, perche' un buon Brier convive benissimo con l'essere
    # sistematicamente troppo sicuri di se'.
    fasce = motore.calibrazione()

    # La classifica vera, quella a punti. Sempre di UNA stagione: sommando
    # quelle in archivio verrebbe fuori una squadra con trecento punti.
    tabella = righe_classifica(
        classifica(snap.dataset.fixtures, current_season(), snap.dataset.name),
        snap.model.strength_table() if snap.model else None,
    )

    return templates.TemplateResponse(request, "hub.html", {
        "prossime": prossime,
        "recenti": recenti,
        "forze": forze,
        "classifica": tabella,
        "accuratezza": righe_accuratezza(accuratezza),
        "accuratezza_1x2": accuratezza["uno_x_due"],
        "confronto_mercato": righe_confronto_mercato(accuratezza),
        "partite_confrontate": (accuratezza.get("mercato") or {}).get("partite", 0),
        # Le formazioni ufficiali servono davvero? Compare solo quando esiste
        # qualche partita pronosticata in tutti e due i modi.
        "confronto_modi": righe_confronto_modi(confronto_modi),
        "confronto_modi_totale": (confronto_modi or {}).get("totale"),
        "confronto_modi_partite": (confronto_modi or {}).get("partite", 0),
        "calibrazione": righe_calibrazione(fasce),
        "calibrazione_verdetto": verdetto_calibrazione(fasce),
        "salute": motore.health(),
        "modello": snap.model,
    })


@app.get("/partita/{fixture_id}", response_class=HTMLResponse)
def partita(request: Request, fixture_id: int):
    """Scheda di una partita: pronostici, griglia dei risultati, scontri diretti."""
    motore = engine()
    snap = motore.snapshot()

    fixture = snap.dataset.by_id.get(fixture_id)
    if fixture is None:
        raise HTTPException(status_code=404, detail="Partita non trovata")

    riga, congelato = _riga(
        motore, fixture, snap.dataset.name, datetime.now(timezone.utc)
    )

    # La griglia dei risultati esatti segue la stessa regola del trittico qui
    # sopra, e per la stessa ragione: su una partita gia' cominciata viene
    # dalla matrice congelata, non da una ricalcolata adesso. E' proprio la
    # matrice che il registro salva per questo — da li' ogni mercato si
    # riderivare mesi dopo.
    griglia = None
    if congelato is not None:
        griglia = griglia_risultati(congelato.matrice)
    elif snap.model and riga.ha_pronostico:
        griglia = griglia_risultati(snap.model.scoreline(
            snap.dataset.name(fixture.home_team_id),
            snap.dataset.name(fixture.away_team_id),
        ))

    # Il pronostico rifatto quando sono uscite le formazioni, se esiste.
    #
    # Si leggono **tutte e due** le righe dal registro, secco compreso, anche
    # quando la testata della pagina mostra gia' il modello caldo. Il confronto
    # ha senso solo fra due cose scritte nello stesso mondo: il secco
    # ricalcolato adesso avrebbe visto piu' partite dell'ufficiale congelato
    # ieri, e la loro differenza non direbbe piu' niente sulle formazioni —
    # direbbe quanto e' passato nel frattempo.
    #
    # Resta ``None`` finche' `data/ufficiali.py` non scrive niente, cioe'
    # finche' i tabellini non bastano: la sezione non compare affatto, che e'
    # la stessa regola degli indisponibili. Una sezione vuota si leggerebbe
    # come "le formazioni non cambiano niente", che e' un'informazione, e
    # sarebbe inventata.
    confronto_formazioni = confronto_ufficiale(
        motore.congelato(fixture_id, MODE_SECCO),
        motore.congelato(fixture_id, MODE_UFFICIALE),
    )

    scontri = snap.dataset.head_to_head(fixture.home_team_id, fixture.away_team_id)
    scontri = [s for s in scontri if s.api_id != fixture.api_id][:8]

    # Formazioni, gol e pagelle: gratis, sono dettagli gia' sul disco. Restano
    # vuoti finche' la partita non e' finita — prima del fischio d'inizio quei
    # tre endpoint non rispondono nulla, ed e' un altro lavoro.
    formazioni = formazioni_partita(motore.store, fixture_id)
    prestazioni = prestazioni_partita(motore.store, fixture_id, fixture.kickoff_utc)

    # Chi non gioca. A differenza dei tre dettagli qui sopra, questo dato
    # esiste **prima** della partita ed e' quindi l'unica cosa che la scheda
    # sappia dire su una gara non ancora giocata. Si legge la fotografia
    # scattata piu' vicino al fischio d'inizio, mai una successiva.
    fuori = blocchi_indisponibili(
        motore.indisponibili(fixture_id),
        fixture.home_team_id,
        snap.dataset.name(fixture.home_team_id),
        snap.dataset.name(fixture.away_team_id),
    )

    return templates.TemplateResponse(request, "partita.html", {
        "p": riga,
        "griglia": griglia,
        # Di chi esiste davvero una scheda. Questa pagina nomina anche chi
        # l'archivio non ha mai visto giocare — gli infortunati per
        # definizione — e collegarli tutti significava offrire un 404. Costa
        # la costruzione dell'archivio giocatori, che e' in cache nel motore e
        # si rifa' solo quando arrivano dettagli nuovi.
        "schede": motore.giocatori().conosciuti,
        # Quanto il modello sa delle due squadre. Va accanto al pronostico e
        # non in fondo: e' la differenza fra "prudente perche' e' una partita
        # equilibrata" e "prudente perche' non so niente di loro".
        #
        # Solo sulle partite da giocare, pero'. E' una misura del modello di
        # **adesso**, e accanto a un pronostico congelato mesi fa descriverebbe
        # una stima diversa da quella mostrata — per giunta piu' informata,
        # visto che nel frattempo ha visto anche quella partita. La domanda a
        # cui risponde e' "di queste due squadre il modello sa qualcosa?", che
        # ci si fa guardando una partita di domani: su una gia' giocata non e'
        # solo inutile, e' fuorviante.
        "affidabilita": (
            righe_affidabilita(
                motore.affidabilita(fixture.home_team_id, fixture.away_team_id), riga
            )
            if not riga.iniziata else None
        ),
        # Chi ha cambiato allenatore di recente. Vale la stessa regola
        # dell'affidabilita' e per la stessa ragione: descrive cosa c'e'
        # dentro la stima di ADESSO, e accanto a un pronostico congelato mesi
        # fa parlerebbe di un modello diverso da quello mostrato.
        #
        # `conta` dice se il modello ne tenga davvero conto, e oggi la
        # risposta e' no: il backtest ha misurato che pesare meno le partite
        # della gestione precedente peggiora i pronostici. La pagina lo
        # dichiara invece di lasciarlo credere, come fa per gli indisponibili.
        "panchine": (
            righe_panchina(
                snap.panchine, riga,
                conta=bool(snap.model and snap.model.fattore_allenatore < 1.0),
            )
            if not riga.iniziata else None
        ),
        # Le pagelle vuote hanno due cause diverse, e dirle uguali sarebbe
        # sbagliato: o il tabellino non e' ancora stato raccolto, oppure c'e'
        # ma e' la fotografia del feed live a partita in corso e l'archivio lo
        # scarta apposta. La seconda si risolve da sola e va detto.
        "tabellino_fermo": tabellino_provvisorio(
            motore.store.load_detail(fixture_id, "players")
        ),
        "formazioni": blocchi_formazione(formazioni, prestazioni),
        "confronto_formazioni": confronto_formazioni,
        "indisponibili": fuori,
        "gol": righe_gol(gol_partita(motore.store, fixture_id), fixture.home_team_id),
        "scontri": [
            {
                "quando": formatta_giorno(s.kickoff_utc),
                "casa": snap.dataset.name(s.home_team_id),
                "ospite": snap.dataset.name(s.away_team_id),
                "risultato": f"{s.home_goals}-{s.away_goals}",
                "stessa_direzione": s.home_team_id == fixture.home_team_id,
            }
            for s in scontri
        ],
        "salute": motore.health(),
    })


@app.get("/pronostici", response_class=HTMLResponse)
def pronostici(request: Request):
    """Il registro: cosa era stato detto, quando, e com'e' andata.

    E' la pagina che rende visibile la promessa del progetto — un pronostico
    registrato non si aggiorna e non si cancella — e per questo mostra anche
    le partite **ancora da giocare**: quelle sono la meta' della promessa in
    cui la risposta non c'e' ancora.

    Tutto viene dal registro, anche prima del fischio d'inizio, ed e' l'unica
    pagina del sito in cui e' cosi'. L'hub su una partita da giocare mostra il
    modello caldo, che nel frattempo puo' aver visto altre giornate; qui si
    guarda cosa fu **scritto**, che e' una domanda diversa e la pagina lo
    dichiara.
    """
    motore = engine()
    snap = motore.snapshot()

    elenco = motore.registro()
    righe = righe_registro(elenco, snap.dataset.by_id, snap.dataset.name)
    return templates.TemplateResponse(request, "pronostici.html", {
        "righe": righe,
        # I conti sono sul registro intero, non sulle righe mostrate: dire
        # "34 pronostici" guardandone 200 su 1520 sarebbe falso.
        "totale": len(elenco),
        "verificati": sum(1 for v in elenco if v["risolto"]),
        # La colonna del tipo compare solo se di tipi ce n'e' piu' d'uno.
        # Finche' le formazioni ufficiali sono spente direbbe "secco" per
        # tutte le righe, che e' una colonna di rumore.
        "modi": sorted({v["mode"] for v in elenco}),
        # Una partita puo' comparire piu' volte, e va spiegato dove si vede:
        # o perche' il modello ha cambiato idea prima del fischio d'inizio, o
        # perche' esistono i due tipi di pronostico. Se non succede, dirlo
        # sarebbe una risposta a una domanda che nessuno si e' fatto.
        "ripetute": len({r.fixture_id for r in righe}) < len(righe),
        "salute": motore.health(),
    })


@app.get("/giocatori", response_class=HTMLResponse)
def giocatori(request: Request, stagione: int | None = None):
    """Chi ha fatto cosa in un campionato: marcatori, assist e voti.

    **Una stagione per volta**, e non e' una preferenza di disegno. Da quando
    i tabellini delle stagioni passate sono sul disco (agosto 2026) l'archivio
    ne contiene quattro, e un capocannoniere calcolato su tutte sarebbe la
    somma di quattro campionati con l'aria di essere uno solo. Chi ha giocato
    piu' anni starebbe davanti a chi ha segnato di piu'.

    Senza `stagione` si mostra la piu' recente. Un'annata che l'archivio non
    ha si corregge nella piu' recente invece di dare 404: e' un indirizzo
    scritto a mano, non un guasto.
    """
    motore = engine()
    archivio = motore.giocatori()
    stagioni = archivio.stagioni()
    scelta = stagione if stagione in stagioni else (stagioni[0] if stagioni else None)

    def classifica(chiave: str, limite: int):
        return [
            riga_giocatore(s)
            for s in archivio.classifica(chiave, limite=limite, stagione=scelta)
        ]

    return templates.TemplateResponse(request, "giocatori.html", {
        "marcatori": classifica("gol", 20),
        "assistenti": classifica("assist", 10),
        "voti": classifica("voto", 10),
        "archivio": archivio,
        "stagioni": stagioni,
        "stagione": scelta,
        "quanti": archivio.giocatori_di(scelta),
        # I conti da dichiarare sopra la classifica sono quelli DI QUESTA
        # annata: quelli dell'archivio intero sarebbero un numero vero al
        # posto sbagliato.
        "concluse": archivio.concluse_per_stagione.get(scelta, 0),
        "con_tabellino": archivio.con_statistiche_per_stagione.get(scelta, 0),
        "salute": motore.health(),
    })


@app.get("/giocatore/{player_id}", response_class=HTMLResponse)
def giocatore(request: Request, player_id: int):
    """Scheda di un giocatore: i totali e la sua stagione partita per partita."""
    motore = engine()
    archivio = motore.giocatori()

    scheda = archivio.scheda(player_id)
    if scheda is None:
        raise HTTPException(status_code=404, detail="Giocatore non trovato")

    snap = motore.snapshot()
    return templates.TemplateResponse(request, "giocatore.html", {
        "g": riga_giocatore(scheda),
        "scheda": scheda,
        "partite": righe_prestazioni(scheda, snap.dataset.by_id, snap.dataset.name),
        "archivio": archivio,
        "salute": motore.health(),
    })


@app.get("/squadra/{team_id}", response_class=HTMLResponse)
def squadra(request: Request, team_id: int):
    """Scheda di una squadra: chi e' sceso in campo e come e' andata finora."""
    motore = engine()
    snap = motore.snapshot()
    archivio = motore.giocatori()

    conosciuta = team_id in snap.dataset.team_names or team_id in archivio.squadre()
    if not conosciuta:
        raise HTTPException(status_code=404, detail="Squadra non trovata")

    partite = snap.dataset.team_fixtures(team_id)
    return templates.TemplateResponse(request, "squadra.html", {
        "squadra": snap.dataset.name(team_id),
        "squadra_id": team_id,
        "rosa": [riga_giocatore(s) for s in archivio.rosa(team_id)],
        "partite": _righe(motore, [f for f in reversed(partite) if f.is_finished][:10]),
        "prossime": _righe(motore, [f for f in partite if f.status == "scheduled"][:3]),
        "archivio": archivio,
        "salute": motore.health(),
    })


@app.get("/stato")
def stato():
    """Diagnostica in JSON: cosa sa il sistema e quanto budget resta."""
    return engine().health()


@app.get("/aggiorna")
def aggiorna():
    """Ricostruisce dataset e modello anche se l'archivio non e' cambiato."""
    engine().snapshot(force=True)
    return {"esito": "ricostruito", **engine().health()}


# Le date vanno formattate anche dentro i template.
templates.env.filters["data"] = formatta_data
templates.env.filters["giorno"] = formatta_giorno
