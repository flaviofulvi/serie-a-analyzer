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

from .engine import Engine
from .views import (
    formatta_data,
    formatta_giorno,
    griglia_risultati,
    riga_partita,
    righe_accuratezza,
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

    return templates.TemplateResponse(request, "partita.html", {
        "p": riga,
        "griglia": griglia,
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
