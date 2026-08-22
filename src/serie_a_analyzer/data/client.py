"""Client per API-Football, con cache, budget e freno integrati.

Ogni chiamata passa da quattro porte, in quest'ordine:

    1. cache — se il dato c'e' ed e' fresco, costa zero
    2. budget — se non c'e' margine per questa priorita', si rifiuta subito
    3. freno — si aspetta che sia passato abbastanza dalla richiesta precedente
    4. rete — solo a quel punto si spende davvero una richiesta

Non esiste un modo per bypassare il budget: e' l'unica difesa contro il
restare senza richieste alle 19:00 di domenica.

Il **freno** difende da un limite diverso e piu' insidioso: oltre al tetto
giornaliero, API-Football impone un tetto **al minuto** (10 sul piano
gratuito). Superarlo fa rispondere 429 mentre il contatore locale continua a
segnare richieste spese — cioe' si paga la richiesta e non si riceve il dato.
E' successo davvero il 22 agosto 2026: la raccolta ha sparato una ventina di
chiamate di fila e ha perso gli indisponibili di quattro partite, con il
budget locale fermo a 47/100.

Il freno sta qui e non nei chiamanti per la stessa ragione per cui ci sta il
budget: e' l'unico punto da cui passano tutte le richieste, e una regola che
vive in un posto solo non si puo' dimenticare in un ramo nuovo del codice.
Vale sulle richieste **davvero spedite**: una risposta servita dalla cache non
tocca la rete e non aspetta niente.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .budget import Priority, RequestBudget
from .cache import (
    TTL_FOREVER,
    TTL_INJURIES,
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

# Secondi minimi fra due richieste davvero spedite.
#
# Il piano gratuito concede 10 richieste al minuto: 7 secondi ne fanno passare
# 8 al minuto, cioe' si sta sotto il tetto con un margine che regge anche se
# l'orologio dell'API conta le finestre in modo leggermente diverso dal nostro.
#
# E' volutamente prudente. Il costo e' tempo di attesa, che qui non serve a
# nessuno: la raccolta gira quattro volte al giorno da sola e nessuno la
# guarda. Il costo di sbagliare dall'altra parte e' perdere un dato non
# recuperabile — le quote di una partita, che dopo il fischio non esistono
# piu'.
#
# Chi passa a un piano a pagamento lo abbassa da .env con
# API_MIN_INTERVAL_SECONDS: con 7 secondi le 4500 richieste al giorno di un
# riempimento storico vorrebbero quasi nove ore di esecuzione.
DEFAULT_MIN_INTERVAL_SECONDS = 7.0


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
    # Secondi minimi fra due richieste spedite davvero. Zero disattiva il
    # freno: ha senso solo nei test, dove non c'e' nessuna rete da proteggere.
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    # Quando e' partita l'ultima richiesta, su orologio monotono. `None`
    # finche' non se n'e' spedita nessuna: la prima non aspetta mai.
    _ultima_richiesta: float | None = field(default=None, init=False, repr=False)

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

        self._attendi_il_turno()
        payload = self._fetch(endpoint, params)
        # Si spacchetta PRIMA di salvare: `_unwrap` solleva se la risposta e'
        # un errore, e un errore in cache con TTL_FOREVER congelerebbe per
        # sempre un guasto temporaneo — o un limite di piano poi rimosso —
        # senza che nessuna richiesta futura possa piu' correggerlo.
        result = self._unwrap(payload, endpoint)
        self.cache.set(endpoint, params, payload)
        return result

    def _attendi_il_turno(self) -> None:
        """Aspetta, se dall'ultima richiesta e' passato troppo poco.

        Il tempo si misura su ``time.monotonic``, non sull'ora del giorno: un
        aggiustamento dell'orologio di sistema — o l'ora legale — non deve
        poter far credere al freno che sia passato un secolo, o meno di zero.
        """
        if self.min_interval_seconds <= 0:
            return
        if self._ultima_richiesta is not None:
            attesa = self.min_interval_seconds - (time.monotonic() - self._ultima_richiesta)
            if attesa > 0:
                logger.debug("freno: attendo %.1fs prima della prossima richiesta", attesa)
                time.sleep(attesa)
        # Si segna PRIMA di spedire, come per il budget: se la chiamata
        # fallisce e il chiamante riprova subito, il freno deve valere lo
        # stesso — un tentativo andato male ha consumato la sua quota al
        # minuto esattamente come uno riuscito.
        self._ultima_richiesta = time.monotonic()

    def _fetch(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {self.key_header: self.api_key, **self.extra_headers}
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            response = httpx.get(url, params=params, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ApiFootballError(f"chiamata fallita a {endpoint}: {exc}") from exc

        if response.status_code == 429:
            raise ApiFootballError(
                "limite di richieste superato lato API (429). Se il budget "
                f"giornaliero non e' esaurito ({self.budget.used}/"
                f"{self.budget.daily_limit}), il tetto superato e' quello al "
                f"MINUTO: alza API_MIN_INTERVAL_SECONDS, oggi a "
                f"{self.min_interval_seconds:g}s."
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

    def status(self) -> dict[str, Any]:
        """Piano sottoscritto, limite giornaliero e richieste gia' spese.

        E' l'unico endpoint che risponde su noi stessi invece che sul calcio, e
        l'unico modo non ambiguo di sapere se un piano nuovo e' davvero attivo:
        sulle stagioni non coperte l'API risponde 200 con una lista vuota, che
        e' indistinguibile da "quel campionato quel giorno non ha giocato".

        Restituisce il dizionario cosi' com'e' — ``account``, ``subscription``,
        ``requests`` — perche' e' l'unica risposta dell'API che non ha una
        forma di dominio in cui tradurla.

        Non si tiene in cache: la domanda "che piano ho adesso" ha senso solo
        se la risposta e' di adesso.
        """
        risposta = self.get("status", {}, ttl=0, priority=Priority.CRITICAL)
        # `/status` e' l'unico endpoint che mette un oggetto in `response`,
        # non una lista: `_unwrap` lo restituisce tale e quale.
        if isinstance(risposta, list):
            return risposta[0] if risposta else {}
        return risposta

    def fixtures(
        self,
        season: int,
        league: int = SERIE_A_LEAGUE_ID,
        ttl: int | None = TTL_UPCOMING_FIXTURES,
        **extra,
    ):
        """Tutte le partite di una stagione: una sola richiesta per ~380 partite.

        E' l'endpoint che rende il modello di squadra praticamente gratuito
        anche sul piano free.

        Il ``ttl`` va passato ``TTL_FOREVER`` per le stagioni concluse: i loro
        risultati non cambieranno mai piu', e lasciarli scadere ogni sei ore
        significherebbe rispendere le richieste di addestramento a ogni
        riavvio dell'applicazione.
        """
        params = {"league": league, "season": season, **extra}
        return self.get("fixtures", params, ttl=ttl)

    def fixtures_on_date(
        self,
        day: str,
        ttl: int | None = TTL_LIVE,
        priority: Priority = Priority.NORMAL,
        force_refresh: bool = False,
    ):
        """Tutte le partite del mondo in una data, senza filtro di lega.

        L'assenza del parametro ``league`` non e' una dimenticanza: e' l'unico
        modo di leggere la stagione in corso sul piano gratuito. Aggiungendo
        ``league`` l'API pretende anche ``season``, e la stagione corrente e'
        bloccata ("Free plans do not have access to this season"). Chiedendo la
        sola data la risposta arriva completa — risultati inclusi — e la Serie A
        si filtra in locale, gratis.

        Il piano gratuito copre una finestra scorrevole di tre giorni: ieri,
        oggi e domani. Fuori da quella non si torna indietro, ed e' il motivo
        per cui la raccolta giornaliera non puo' saltare un giorno.
        """
        return self.get(
            "fixtures", {"date": day}, ttl=ttl, priority=priority, force_refresh=force_refresh
        )

    def fixture_events(self, fixture_id: int):
        """Gol, cartellini e sostituzioni di una partita."""
        return self.get(
            "fixtures/events", {"fixture": fixture_id},
            ttl=TTL_FOREVER, priority=Priority.BACKFILL,
        )

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
        """Indisponibili di un'intera stagione. **Bloccato dal piano gratuito.**

        Verificato il 22/08/2026: risponde "Free plans do not have access to
        this season, try from 2022 to 2024" — lo stesso filtro league+season
        che tiene fuori la stagione in corso dalle fixtures. Resta qui per il
        giorno di un piano superiore; per lavorare oggi si usa
        `injuries_fixture`.
        """
        return self.get("injuries", {"league": league, "season": season}, ttl=TTL_UPCOMING_FIXTURES)

    def injuries_fixture(self, fixture_id: int, ttl: int | None = TTL_INJURIES):
        """Chi salta *questa* partita: infortunati, squalificati e in dubbio.

        La strada che funziona sul piano gratuito, perche' come tutti gli
        endpoint per singola partita non passa dal filtro league+season.

        Priorita' `BACKFILL` e non `CRITICAL`, e non e' un dettaglio: gli
        indisponibili di una partita **si recuperano anche dopo** (verificato
        su una gara del 17/08/2024, che risponde ancora). Non sono come le
        quote o come gli id delle partite: se una raccolta salta, si riprende
        domani. Chiederli a priorita' alta significherebbe intaccare la
        riserva che protegge le formazioni ufficiali di oggi per un dato che
        aspetta.
        """
        return self.get(
            "injuries", {"fixture": fixture_id}, ttl=ttl, priority=Priority.BACKFILL
        )

    def odds(self, fixture_id: int):
        return self.get("odds", {"fixture": fixture_id}, ttl=TTL_ODDS)
