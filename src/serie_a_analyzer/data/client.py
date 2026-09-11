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

Dall'11 settembre 2026 i due tetti non sono piu' soltanto numeri scritti nel
.env. API-Football li dichiara negli header di OGNI risposta — al giorno, al
minuto, e quante richieste restano — e il client li legge
(`_impara_i_limiti`): il contatore si allinea a quello del server, il limite
giornaliero scende da solo se il server ne concede meno, e il freno si alza da
solo se il tetto al minuto lo chiede. Serve per un giorno preciso, il ritorno
al piano gratuito del 24 settembre 2026: prima la sicurezza stava in due righe
del .env ricordate a mano, e la seconda — il freno — era quella che si
dimentica. Vale in una direzione sola: da soli si diventa piu' prudenti, mai
meno.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import httpx

from .budget import LimitiDichiarati, Priority, RequestBudget, stessi_limiti
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
# riempimento storico vorrebbero quasi nove ore di esecuzione. Nella direzione
# opposta non serve toccarlo: tornando al gratuito il freno si rialza da solo,
# perche' il server dichiara il suo tetto (vedi `intervallo_in_vigore`).
DEFAULT_MIN_INTERVAL_SECONDS = 7.0

# Il tetto al minuto del piano gratuito: e' quello su cui i 7 secondi qui sopra
# sono tarati, e da cui si ricava il freno per qualunque altro tetto.
TETTO_MINUTO_GRATUITO = 10

# Quanto aspettare quando il server dice che le richieste di questo minuto sono
# finite. Non dice quando la sua finestra si riapre, quindi se ne aspetta una
# intera: e' l'unica attesa sicuramente abbastanza lunga.
ATTESA_MINUTO_ESAURITO = 60.0

# Gli header con cui API-Football dichiara i suoi limiti in ogni risposta.
# Letti da una risposta vera l'11 settembre 2026, piano Pro: 7500 al giorno e
# 300 al minuto (non 450, come si credeva). httpx li confronta senza badare
# alle maiuscole, e il server le mescola.
HEADER_LIMITE_GIORNO = "x-ratelimit-requests-limit"
HEADER_RIMASTE_GIORNO = "x-ratelimit-requests-remaining"
HEADER_LIMITE_MINUTO = "x-ratelimit-limit"
HEADER_RIMASTE_MINUTO = "x-ratelimit-remaining"


def _intero(valore: Any, minimo: int = 0) -> int | None:
    """Un numero da un header, o ``None`` se manca o non e' un numero."""
    try:
        numero = int(str(valore).strip())
    except (TypeError, ValueError):
        return None
    return numero if numero >= minimo else None


def limiti_dagli_header(headers: Any) -> LimitiDichiarati | None:
    """I limiti che una risposta dichiara, o ``None`` se non ne dichiara.

    Un TETTO a zero si scarta come un tetto assente: nessun piano concede zero
    richieste, e crederci fermerebbe tutto per un header malformato. Una
    RIMANENZA a zero invece e' un'informazione vera, ed e' la piu' preziosa.
    """
    if headers is None:
        return None
    limiti = LimitiDichiarati(
        giorno=_intero(headers.get(HEADER_LIMITE_GIORNO), minimo=1),
        rimaste_giorno=_intero(headers.get(HEADER_RIMASTE_GIORNO)),
        minuto=_intero(headers.get(HEADER_LIMITE_MINUTO), minimo=1),
        rimaste_minuto=_intero(headers.get(HEADER_RIMASTE_MINUTO)),
    )
    if limiti == LimitiDichiarati():
        return None
    return limiti


def intervallo_per_tetto(tetto: int) -> float:
    """I secondi fra due richieste che un tetto al minuto richiede.

    La stessa proporzione dei 7 secondi scelti per il gratuito — otto
    richieste e mezza al minuto su dieci — applicata a qualunque tetto: 7
    secondi su 10 al minuto, 0,23 su 300.
    """
    return DEFAULT_MIN_INTERVAL_SECONDS * TETTO_MINUTO_GRATUITO / tetto


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
    # Il tetto al minuto dichiarato dal server, e quante richieste gli restano
    # in questo minuto. Il primo si ricorda anche fra un processo e l'altro
    # (sta nel contatore, vedi `_tetto_al_minuto`); il secondo vale solo per
    # la prossima richiesta, ed e' giusto che muoia col processo.
    _limite_minuto: int | None = field(default=None, init=False, repr=False)
    _rimaste_minuto: int | None = field(default=None, init=False, repr=False)
    _piano_letto: bool = field(default=False, init=False, repr=False)

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
        usate, limite = self.budget.usate_e_limite()
        logger.info(
            "richiesta API: %s %s (budget usato %d/%d)", endpoint, params, usate, limite,
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

        Quanto aspettare lo decide `intervallo_in_vigore`, con un caso in piu':
        se l'ultima risposta ha detto che le richieste di questo minuto sono
        finite, si aspetta un minuto intero. Il freno di un processo non vede
        gli altri due che spendono insieme a lui — il sito e le due raccolte —
        e il server e' l'unico che li conta tutti.
        """
        intervallo = self.intervallo_in_vigore
        if intervallo <= 0:
            return
        if self._rimaste_minuto == 0:
            intervallo = max(intervallo, ATTESA_MINUTO_ESAURITO)
        if self._ultima_richiesta is not None:
            attesa = intervallo - (time.monotonic() - self._ultima_richiesta)
            if attesa > 0:
                logger.debug("freno: attendo %.1fs prima della prossima richiesta", attesa)
                time.sleep(attesa)
        # Si segna PRIMA di spedire, come per il budget: se la chiamata
        # fallisce e il chiamante riprova subito, il freno deve valere lo
        # stesso — un tentativo andato male ha consumato la sua quota al
        # minuto esattamente come uno riuscito.
        self._ultima_richiesta = time.monotonic()

    @property
    def intervallo_in_vigore(self) -> float:
        """I secondi fra due richieste: il piu' prudente fra .env e server.

        Il piu' prudente, come per il budget e per la stessa ragione: il giorno
        del ritorno al gratuito il .env dira' ancora 1 secondo, il server 10
        richieste al minuto, e un 429 si paga senza ricevere niente. Da allora
        il freno si alza da solo a 7 secondi. Sul Pro invece vince il .env (1
        secondo contro 0,23): il server lascerebbe correre di piu', ma nessuno
        ha fretta.

        Zero nel .env spegne il freno del tutto, dichiarazioni comprese: ha
        senso solo nei test, dove la rete e' finta.
        """
        if self.min_interval_seconds <= 0:
            return 0.0
        tetto = self._tetto_al_minuto()
        if not tetto:
            return self.min_interval_seconds
        return max(self.min_interval_seconds, intervallo_per_tetto(tetto))

    def _tetto_al_minuto(self) -> int | None:
        """Il tetto al minuto noto: dall'ultima risposta, o dal contatore.

        Il contatore si legge una volta sola per processo, e serve a chi non
        ha ancora spedito niente: `/stato` mostra il freno del sito, che puo'
        non fare una richiesta per giorni, e un numero preso dal solo .env
        direbbe il falso proprio il giorno del downgrade.
        """
        if self._limite_minuto is None and not self._piano_letto:
            self._piano_letto = True
            piano = self.budget.piano_dichiarato() or {}
            self._limite_minuto = _intero(piano.get("minuto"), minimo=1)
        return self._limite_minuto

    def _impara_i_limiti(self, headers: Any, spedita_il: date) -> None:
        """Legge dagli header quanto concede il piano, e lo porta nel contatore.

        Non solleva mai, ed e' la regola di tutto cio' che sta attorno a una
        richiesta gia' pagata: la risposta e' arrivata, e un guasto nel
        leggerne gli header non deve costare il dato. Si torna al .env, cioe'
        esattamente al mondo di prima.
        """
        try:
            limiti = limiti_dagli_header(headers)
            if limiti is None:
                return
            if limiti.minuto is not None:
                self._limite_minuto = limiti.minuto
                self._piano_letto = True
            self._rimaste_minuto = limiti.rimaste_minuto
            prima, dopo = self.budget.registra_dichiarazione(limiti, giorno=spedita_il)
        except Exception:  # vedi la docstring: qui niente puo' costare il dato
            logger.warning("limiti dichiarati dall'API non registrati", exc_info=True)
            return
        self._racconta_il_piano(prima, dopo)

    def _racconta_il_piano(
        self, prima: dict[str, Any] | None, dopo: dict[str, Any] | None,
    ) -> None:
        """Dice nel log che il piano e' cambiato: una volta, non a ogni risposta."""
        if not dopo or stessi_limiti(prima, dopo):
            return
        giorno, minuto = dopo.get("giorno"), dopo.get("minuto")
        if prima is None:
            logger.info("l'API dichiara %s richieste al giorno e %s al minuto", giorno, minuto)
        else:
            logger.warning(
                "il piano dell'API e' cambiato: da %s al giorno e %s al minuto a %s e %s",
                prima.get("giorno"), prima.get("minuto"), giorno, minuto,
            )
        if giorno and giorno != self.budget.daily_limit:
            logger.warning(
                "DAILY_REQUEST_LIMIT nel .env dice %d, l'API %d: il contatore usa %d",
                self.budget.daily_limit, giorno, min(giorno, self.budget.daily_limit),
            )

    def _fetch(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {self.key_header: self.api_key, **self.extra_headers}
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        spedita_il = datetime.now(timezone.utc).date()
        try:
            response = httpx.get(url, params=params, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ApiFootballError(f"chiamata fallita a {endpoint}: {exc}") from exc

        # Prima di guardare lo status: anche un 429 dichiara i limiti, ed e'
        # proprio la risposta da cui conviene impararli.
        self._impara_i_limiti(response.headers, spedita_il)

        if response.status_code == 429:
            usate, limite = self.budget.usate_e_limite()
            tetto = (
                f", che il server dichiara di {self._limite_minuto}"
                if self._limite_minuto else ""
            )
            raise ApiFootballError(
                "limite di richieste superato lato API (429). Se il budget "
                f"giornaliero non e' esaurito ({usate}/{limite}), il tetto "
                f"superato e' quello al MINUTO{tetto}: il freno era a "
                f"{self.intervallo_in_vigore:g}s. Di solito vuol dire piu' "
                "processi che spendono insieme, che il freno di ognuno non "
                "vede: alza API_MIN_INTERVAL_SECONDS, oggi a "
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
