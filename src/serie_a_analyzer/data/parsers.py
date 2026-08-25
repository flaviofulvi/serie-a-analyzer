"""Traduzione delle risposte di API-Football negli oggetti del dominio.

I parser sono deliberatamente tolleranti: campi mancanti diventano ``None``
invece di far esplodere l'ingestione. Con 100 richieste al giorno, una riga
malformata non deve costringere a ripetere il download di un'intera stagione.
Cio' che non e' recuperabile viene segnalato, non nascosto.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ..model.dixon_coles import Match

# Codici di stato di API-Football raggruppati nei quattro stati che ci servono.
_STATUS_MAP = {
    "TBD": "scheduled", "NS": "scheduled",
    "1H": "live", "HT": "live", "2H": "live", "ET": "live",
    "BT": "live", "P": "live", "SUSP": "live", "INT": "live", "LIVE": "live",
    "FT": "finished", "AET": "finished", "PEN": "finished",
    "PST": "postponed",
    "CANC": "cancelled", "ABD": "cancelled", "AWD": "cancelled", "WO": "cancelled",
}

_ROUND_RE = re.compile(r"(\d+)\s*$")
_HEIGHT_RE = re.compile(r"(\d+)")


@dataclass(frozen=True)
class FixtureRecord:
    """Una partita come arriva dalla fonte dati, gia' normalizzata."""

    api_id: int
    season: int
    league_id: int
    round_label: str | None
    matchday: int | None
    kickoff_utc: datetime
    status: str
    home_team_id: int
    home_team_name: str
    away_team_id: int
    away_team_name: str
    home_goals: int | None
    away_goals: int | None
    home_goals_ht: int | None
    away_goals_ht: int | None
    venue_name: str | None

    @property
    def is_finished(self) -> bool:
        return self.status == "finished" and self.home_goals is not None and self.away_goals is not None

    @property
    def played_on(self) -> date:
        return self.kickoff_utc.date()


@dataclass(frozen=True)
class TeamRecord:
    api_id: int
    name: str
    short_name: str | None
    founded: int | None
    venue_name: str | None
    venue_city: str | None


@dataclass(frozen=True)
class PlayerRecord:
    api_id: int
    name: str
    firstname: str | None
    lastname: str | None
    birth_date: date | None
    nationality: str | None
    height_cm: int | None
    weight_kg: int | None
    position: str | None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_matchday(round_label: str | None) -> int | None:
    """Estrae il numero di giornata da "Regular Season - 12".

    Torna ``None`` per i turni non numerati (playoff, fasi finali di coppa):
    e' un caso legittimo, non un errore.
    """
    if not round_label:
        return None
    match = _ROUND_RE.search(round_label)
    return int(match.group(1)) if match else None


def parse_height_cm(raw: str | None) -> int | None:
    """Da "180 cm" a 180."""
    if not raw:
        return None
    match = _HEIGHT_RE.search(str(raw))
    return int(match.group(1)) if match else None


def parse_status(raw: dict[str, Any] | None) -> str:
    """Riduce i ~18 codici di API-Football ai 5 stati dello schema."""
    short = (raw or {}).get("short")
    return _STATUS_MAP.get(short, "scheduled")


def parse_fixture(raw: dict[str, Any]) -> FixtureRecord:
    """Da un elemento di ``/fixtures`` a un ``FixtureRecord``."""
    fixture = raw.get("fixture") or {}
    league = raw.get("league") or {}
    teams = raw.get("teams") or {}
    goals = raw.get("goals") or {}
    score = raw.get("score") or {}
    halftime = score.get("halftime") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}

    kickoff_raw = fixture.get("date")
    if not kickoff_raw:
        raise ValueError(f"partita senza data: id={fixture.get('id')}")
    # API-Football usa ISO 8601 con offset esplicito ("+00:00").
    kickoff = datetime.fromisoformat(kickoff_raw)

    round_label = league.get("round")
    venue = fixture.get("venue") or {}

    return FixtureRecord(
        api_id=int(fixture["id"]),
        season=int(league.get("season")),
        league_id=int(league.get("id")),
        round_label=round_label,
        matchday=parse_matchday(round_label),
        kickoff_utc=kickoff,
        status=parse_status(fixture.get("status")),
        home_team_id=int(home["id"]),
        home_team_name=home.get("name", ""),
        away_team_id=int(away["id"]),
        away_team_name=away.get("name", ""),
        home_goals=_int_or_none(goals.get("home")),
        away_goals=_int_or_none(goals.get("away")),
        home_goals_ht=_int_or_none(halftime.get("home")),
        away_goals_ht=_int_or_none(halftime.get("away")),
        venue_name=venue.get("name"),
    )


def parse_fixtures(response: list[dict[str, Any]]) -> list[FixtureRecord]:
    """Interpreta una risposta ``/fixtures``, saltando le righe irrecuperabili."""
    out: list[FixtureRecord] = []
    for raw in response:
        try:
            out.append(parse_fixture(raw))
        except (KeyError, ValueError, TypeError):
            # Meglio perdere una partita che l'intera stagione gia' scaricata.
            continue
    return out


def parse_team(raw: dict[str, Any]) -> TeamRecord:
    """Da un elemento di ``/teams`` a un ``TeamRecord``."""
    team = raw.get("team") or raw
    venue = raw.get("venue") or {}
    return TeamRecord(
        api_id=int(team["id"]),
        name=team.get("name", ""),
        short_name=team.get("code"),
        founded=_int_or_none(team.get("founded")),
        venue_name=venue.get("name"),
        venue_city=venue.get("city"),
    )


def parse_player(raw: dict[str, Any]) -> PlayerRecord:
    """Da un elemento di ``/players`` o ``/players/squads`` a un ``PlayerRecord``."""
    player = raw.get("player") or raw
    birth = player.get("birth") or {}

    birth_date = None
    if birth.get("date"):
        try:
            birth_date = date.fromisoformat(birth["date"])
        except ValueError:
            birth_date = None

    return PlayerRecord(
        api_id=int(player["id"]),
        name=player.get("name", ""),
        firstname=player.get("firstname"),
        lastname=player.get("lastname"),
        birth_date=birth_date,
        nationality=player.get("nationality"),
        height_cm=parse_height_cm(player.get("height")),
        weight_kg=parse_height_cm(player.get("weight")),
        position=player.get("position"),
    )


def to_model_matches(
    fixtures: list[FixtureRecord],
    name_by_team_id: dict[int, str] | None = None,
    panchine: Any | None = None,
) -> list[Match]:
    """Filtra e converte le partite concluse in input per il modello.

    Le partite non concluse (programmate, rinviate, annullate) vengono escluse:
    il modello si stima solo su risultati reali. Le squadre sono identificate
    dall'id di API-Football, non dal nome, perche' i nomi cambiano fra endpoint
    e stagioni ("Inter" / "Internazionale") e un disallineamento sdoppierebbe
    silenziosamente una squadra in due entita' distinte.

    ``panchine`` e' facoltativo (`data/panchine.py`): quando c'e', ogni partita
    si porta dietro gli id degli allenatori di quel giorno, e il modello puo'
    pesare meno le partite di una squadra che nel frattempo ha cambiato
    panchina. Senza, gli allenatori restano ``None`` e la stima e' quella di
    sempre — che e' anche cio' che deve succedere sulle stagioni di cui
    possediamo solo i risultati.
    """
    names = name_by_team_id or {}
    matches: list[Match] = []
    for f in fixtures:
        if not f.is_finished:
            continue
        matches.append(
            Match(
                home=names.get(f.home_team_id, str(f.home_team_id)),
                away=names.get(f.away_team_id, str(f.away_team_id)),
                home_goals=f.home_goals,
                away_goals=f.away_goals,
                played_on=f.played_on,
                allenatore_casa=(
                    panchine.allenatore(f.api_id, f.home_team_id) if panchine else None
                ),
                allenatore_ospite=(
                    panchine.allenatore(f.api_id, f.away_team_id) if panchine else None
                ),
            )
        )
    return matches
