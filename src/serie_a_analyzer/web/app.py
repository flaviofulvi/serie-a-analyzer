"""Applicazione web: hub delle partite e scheda del singolo incontro.

    uvicorn serie_a_analyzer.web.app:app --reload

L'hub e' il cuore dell'applicazione (Fase 6 del progetto). Le altre pagine
verranno appese qui man mano.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..data.players import formazioni_partita, gol_partita, prestazioni_partita
from .engine import Engine
from .views import (
    blocchi_formazione,
    formatta_data,
    formatta_giorno,
    griglia_risultati,
    riga_giocatore,
    riga_partita,
    righe_accuratezza,
    righe_affidabilita,
    righe_confronto_mercato,
    righe_gol,
    righe_prestazioni,
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


def _righe(motore: Engine, fixtures) -> list:
    """Trasforma le partite in righe, aggiungendo il pronostico dove possibile."""
    snap = motore.snapshot()
    nome = snap.dataset.name
    return [
        riga_partita(
            f, nome,
            prob=motore.predict(f.home_team_id, f.away_team_id),
            gol_attesi=motore.expected_goals(f.home_team_id, f.away_team_id),
        )
        for f in fixtures
    ]


@app.get("/", response_class=HTMLResponse)
def hub(request: Request):
    """Le prossime partite con i pronostici. Se non ce ne sono, cosa c'e' invece."""
    motore = engine()
    snap = motore.snapshot()

    prossime = _righe(motore, snap.dataset.next_matchday())
    recenti = _righe(motore, snap.dataset.recent_results(limit=8))

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

    return templates.TemplateResponse(request, "hub.html", {
        "prossime": prossime,
        "recenti": recenti,
        "forze": forze,
        "accuratezza": righe_accuratezza(accuratezza),
        "accuratezza_1x2": accuratezza["uno_x_due"],
        "confronto_mercato": righe_confronto_mercato(accuratezza),
        "partite_confrontate": (accuratezza.get("mercato") or {}).get("partite", 0),
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

    riga = _righe(motore, [fixture])[0]

    griglia = None
    if snap.model and riga.ha_pronostico:
        matrice = snap.model.scoreline(
            snap.dataset.name(fixture.home_team_id),
            snap.dataset.name(fixture.away_team_id),
        )
        griglia = griglia_risultati(matrice)

    scontri = snap.dataset.head_to_head(fixture.home_team_id, fixture.away_team_id)
    scontri = [s for s in scontri if s.api_id != fixture.api_id][:8]

    # Formazioni, gol e pagelle: gratis, sono dettagli gia' sul disco. Restano
    # vuoti finche' la partita non e' finita — prima del fischio d'inizio quei
    # tre endpoint non rispondono nulla, ed e' un altro lavoro.
    formazioni = formazioni_partita(motore.store, fixture_id)
    prestazioni = prestazioni_partita(motore.store, fixture_id, fixture.kickoff_utc)

    return templates.TemplateResponse(request, "partita.html", {
        "p": riga,
        "griglia": griglia,
        # Quanto il modello sa delle due squadre. Va accanto al pronostico e
        # non in fondo: e' la differenza fra "prudente perche' e' una partita
        # equilibrata" e "prudente perche' non so niente di loro".
        "affidabilita": righe_affidabilita(
            motore.affidabilita(fixture.home_team_id, fixture.away_team_id), riga
        ),
        "formazioni": blocchi_formazione(formazioni, prestazioni),
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


@app.get("/giocatori", response_class=HTMLResponse)
def giocatori(request: Request):
    """Chi ha fatto cosa: marcatori, assist e voti dalle partite raccolte."""
    motore = engine()
    archivio = motore.giocatori()

    return templates.TemplateResponse(request, "giocatori.html", {
        "marcatori": [riga_giocatore(s) for s in archivio.classifica("gol", limite=20)],
        "assistenti": [riga_giocatore(s) for s in archivio.classifica("assist", limite=10)],
        "voti": [riga_giocatore(s) for s in archivio.classifica("voto", limite=10)],
        "archivio": archivio,
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
