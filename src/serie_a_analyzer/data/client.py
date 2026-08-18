"""Client per API-Football, con cache e budget integrati.

Ogni chiamata passa da tre porte, in quest'ordine:

    1. cache — se il dato c'e' ed e' fresco, costa zero
    2. budget — se non c'e' margine per questa priorita', si rifiuta subito
    3. rete — solo a quel punto si spende davvero una richiesta

Non esiste un modo per bypassare il budget: e' l'unica difesa contro il
restare senza richieste alle 19:00 di domenica.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .budget import Priority, RequestBudget
from .cache import (
    TTL_FOREVER,
    TTL_LINEUPS,
    TTL_LIVE,
    TTL_ODDS,
    TTL_STATIC,
    TTL_UPCOMING_FIXTURES,
    ResponseCache,
)

logger = logging.getLogger(__name__)

# Endpoint diretto di API-Sports. Chi usa il proxy RapidAPI ha host e header
# diversi: vedi `ApiFootballClient.for_rapidapi`.
API_SPORTS_BASE_URL = "https://v3.football.api-sports.io"
RAPIDAPI_BASE_URL = "https://api-football-v1.p.rapidapi.com/v3"

# Id di API-Football per la Serie A. Vive qui e non sparso nel codice, cosi'
# l'estensione ad altri campionati non richiede una caccia alle costanti.
SERIE_A_LEAGUE_ID = 135


class ApiFootballError(RuntimeError):
    """Errore restituito dall'API o risposta non interpretabile."""


@dataclass
class ApiFootballClient:
    """Client HTTP con cache e contabilita' del budget."""

    api_key: str
    cache: ResponseCache
    budget: RequestBudget
    base_url: str = API_SPORTS_BASE_URL
    key_header: str = "x-apisports-key"
    timeout: float = 20.0
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def for_rapidapi(cls, api_key: str, cache: ResponseCache, budget: RequestBudget, **kw):
        """Variante per chi accede tramite il proxy RapidAPI."""
        return cls(
            api_key=api_key,
            cache=cache,
            budget=budget,
            base_url=RAPIDAPI_BASE_URL,
            key_header="x-rapidapi-key",
            extra_headers={"x-rapidapi-host": "api-football-v1.p.rapidapi.com"},
            **kw,
        )

    # ---- chiamata generica -------------------------------------------------

    def get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        ttl: int | None = TTL_FOREVER,
        priority: Priority = Priority.NORMAL,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Esegue una chiamata, servendola dalla cache quando possibile.

        Args:
            endpoint: percorso relativo, es. ``"fixtures"``.
            params: parametri di query.
            ttl: validita' della copia in cache, in secondi. ``None`` = per sempre.
            priority: governa l'accesso alla riserva di budget.
            force_refresh: ignora la cache in lettura (la aggiorna comunque).

        Returns:
            Il contenuto del campo ``response``, gia' spacchettato.

        Raises:
            BudgetExhausted: se non c'e' margine per questa priorita'.
            ApiFootballError: se l'API segnala un errore.
        """
        params = params or {}

        if not force_refresh:
            cached = self.cache.get(endpoint, params, ttl)
            if cached is not None:
                logger.debug("cache hit: %s %s", endpoint, params)
                return self._unwrap(cached, endpoint)

        # Prenota la richiesta PRIMA di spedirla: se il processo muore a meta',
        # meglio aver contato una richiesta di troppo che una di meno.
        self.budget.spend(priority)
        logger.info(
            "richiesta API: %s %s (budget usato %d/%d)",
            endpoint, params, self.budget.used, self.budget.daily_limit,
        )

        payload = self._fetch(endpoint, params)
        self.cache.set(endpoint, params, payload)
        return self._unwrap(payload, endpoint)

    def _fetch(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {self.key_header: self.api_key, **self.extra_headers}
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            response = httpx.get(url, params=params, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ApiFootballError(f"chiamata fallita a {endpoint}: {exc}") from exc

        if response.status_code == 429:
            raise ApiFootballError(
                "limite di richieste superato lato API (429). Il contatore locale "
                "e' fuori sincrono: verifica il budget giornaliero."
            )
        if response.status_code >= 400:
            raise ApiFootballError(
                f"{endpoint} ha risposto {response.status_code}: {response.text[:200]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise ApiFootballError(f"risposta non JSON da {endpoint}") from exc

    @staticmethod
    def _unwrap(payload: dict[str, Any], endpoint: str) -> list[dict[str, Any]]:
        """Estrae ``response`` dall'involucro, facendo emergere gli errori.

        API-Football risponde 200 anche quando l'operazione e' fallita (chiave
        non valida, piano insufficiente, stagione non coperta): l'errore sta nel
        corpo, non nello status code.
        """
        errors = payload.get("errors")
        # Il campo e' una lista vuota quando va tutto bene, un dizionario di
        # messaggi quando qualcosa non va.
        if errors and not isinstance(errors, list):
            raise ApiFootballError(f"{endpoint}: {errors}")
        if isinstance(errors, list) and errors:
            raise ApiFootballError(f"{endpoint}: {errors}")

        response = payload.get("response")
        if response is None:
            raise ApiFootballError(f"{endpoint}: risposta senza campo 'response'")
        return response

    # ---- endpoint di alto livello -----------------------------------------
    #
    # I TTL e le priorita' non sono cosmetici: codificano quanto in fretta un
    # dato diventa sbagliato e quanto e' grave restare senza.

    def fixtures(self, season: int, league: int = SERIE_A_LEAGUE_ID, **extra):
        """Tutte le partite di una stagione: una sola richiesta per ~380 partite.

        E' l'endpoint che rende il modello di squadra praticamente gratuito
        anche sul piano free.
        """
        params = {"league": league, "season": season, **extra}
        return self.get("fixtures", params, ttl=TTL_UPCOMING_FIXTURES)

    def fixtures_by_date(self, day: str, league: int = SERIE_A_LEAGUE_ID, season: int | None = None):
        params: dict[str, Any] = {"league": league, "date": day}
        if season is not None:
            params["season"] = season
        return self.get("fixtures", params, ttl=TTL_LIVE, priority=Priority.CRITICAL)

    def teams(self, season: int, league: int = SERIE_A_LEAGUE_ID):
        return self.get("teams", {"league": league, "season": season}, ttl=TTL_STATIC)

    def standings(self, season: int, league: int = SERIE_A_LEAGUE_ID):
        return self.get("standings", {"league": league, "season": season}, ttl=TTL_UPCOMING_FIXTURES)

    def lineups(self, fixture_id: int):
        """Formazioni di una partita. Critica: escono ~30 min prima del via."""
        return self.get(
            "fixtures/lineups", {"fixture": fixture_id},
            ttl=TTL_LINEUPS, priority=Priority.CRITICAL,
        )

    def fixture_players(self, fixture_id: int):
        """Statistiche per giocatore di una partita.

        Il dato piu' costoso del progetto: una richiesta per partita. Va sempre
        chiesto a priorita' BACKFILL, mai a ridosso di una giornata.
        """
        return self.get(
            "fixtures/players", {"fixture": fixture_id},
            ttl=TTL_FOREVER, priority=Priority.BACKFILL,
        )

    def squad(self, team_id: int):
        return self.get("players/squads", {"team": team_id}, ttl=TTL_STATIC)

    def player_season(self, player_id: int, season: int, league: int = SERIE_A_LEAGUE_ID):
        return self.get(
            "players", {"id": player_id, "season": season, "league": league},
            ttl=TTL_FOREVER, priority=Priority.BACKFILL,
        )

    def transfers(self, player_id: int):
        return self.get(
            "transfers", {"player": player_id}, ttl=TTL_STATIC, priority=Priority.BACKFILL
        )

    def injuries(self, season: int, league: int = SERIE_A_LEAGUE_ID):
        return self.get("injuries", {"league": league, "season": season}, ttl=TTL_UPCOMING_FIXTURES)

    def odds(self, fixture_id: int):
        return self.get("odds", {"fixture": fixture_id}, ttl=TTL_ODDS)
