"""Configurazione da variabili d'ambiente / file .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Import diretto e non differito come gli altri di `data`: qui serve solo una
# costante, e leggerla altrove significherebbe scrivere due volte lo stesso
# numero — che e' il modo classico di farne divergere uno.
from .data.client import DEFAULT_MIN_INTERVAL_SECONDS

# Radice del progetto: da qui pendono cache e stato del budget.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """Configurazione mancante o incoerente."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_provider: str            # 'apisports' oppure 'rapidapi'
    daily_request_limit: int
    api_min_interval_seconds: float  # secondi minimi fra due richieste spedite
    cache_dir: Path
    budget_file: Path
    store_dir: Path              # archivio della stagione in corso
    storico_dir: Path            # stagioni passate ricomprate con un piano a pagamento
    importato_dir: Path          # stagioni passate importate da un file di risultati
    journal_file: Path           # registro dei giorni raccolti
    predictions_db: Path         # storico immutabile dei pronostici
    database_url: str | None
    league_id: int
    seasons: tuple[int, ...]     # stagioni su cui stimare il modello

    # Le notifiche hanno un default e le altre no, ed e' una distinzione
    # voluta: senza chiave API il sistema non puo' funzionare, senza Telegram
    # funziona identico e in silenzio. Un campo obbligatorio direbbe il
    # contrario a chi legge, e costringerebbe a nominarlo ovunque si costruisca
    # una configurazione — test compresi.
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_verboso: bool = False
    notifiche_file: Path = PROJECT_ROOT / "data" / "notifiche.json"

    @property
    def has_database(self) -> bool:
        return bool(self.database_url)


def _parse_seasons(raw: str) -> tuple[int, ...]:
    return tuple(int(part.strip()) for part in raw.split(",") if part.strip())


def load_settings() -> Settings:
    """Legge la configurazione, spiegando cosa manca invece di fallire in modo opaco."""
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            "API_FOOTBALL_KEY non impostata.\n"
            "Copia .env.example in .env e inserisci la tua chiave "
            "(la trovi su dashboard.api-football.com)."
        )

    data_dir = PROJECT_ROOT / "data"
    return Settings(
        api_key=api_key,
        api_provider=os.getenv("API_FOOTBALL_PROVIDER", "apisports").strip().lower(),
        daily_request_limit=int(os.getenv("DAILY_REQUEST_LIMIT", "100")),
        # Il tetto al minuto di API-Football, che e' cosa diversa da quello
        # giornaliero: sul piano gratuito sono 10 richieste al minuto, e
        # superarlo fa perdere il dato pagando lo stesso la richiesta. Sui
        # piani superiori il tetto e' molto piu' alto e questo valore si
        # abbassa da .env, altrimenti un riempimento storico impiegherebbe
        # ore di attesa invece che minuti.
        api_min_interval_seconds=float(
            os.getenv("API_MIN_INTERVAL_SECONDS", str(DEFAULT_MIN_INTERVAL_SECONDS))
        ),
        cache_dir=Path(os.getenv("CACHE_DIR", data_dir / "cache")),
        budget_file=Path(os.getenv("BUDGET_FILE", data_dir / "budget.json")),
        store_dir=Path(os.getenv("STORE_DIR", data_dir / "serie_a")),
        storico_dir=Path(os.getenv("STORICO_DIR", data_dir / "storico")),
        importato_dir=Path(os.getenv("IMPORTATO_DIR", data_dir / "importato")),
        journal_file=Path(os.getenv("JOURNAL_FILE", data_dir / "journal.json")),
        predictions_db=Path(os.getenv("PREDICTIONS_DB", data_dir / "pronostici.db")),
        database_url=os.getenv("DATABASE_URL") or None,
        # Senza token o senza chat il canale resta spento e nessuna riga di
        # codice se ne accorge: una notifica e' un di piu', mai un requisito.
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip() or None,
        telegram_verboso=os.getenv("TELEGRAM_VERBOSO", "").strip().lower()
        in ("1", "true", "si", "s"),
        notifiche_file=Path(os.getenv("NOTIFICHE_FILE", data_dir / "notifiche.json")),
        league_id=int(os.getenv("LEAGUE_ID", "135")),
        seasons=_parse_seasons(os.getenv("SEASONS", "2022,2023,2024")),
    )


def build_client(settings: Settings | None = None):
    """Costruisce il client con cache e budget gia' collegati."""
    from .data.budget import RequestBudget
    from .data.cache import ResponseCache
    from .data.client import ApiFootballClient

    settings = settings or load_settings()
    cache = ResponseCache(root=settings.cache_dir)
    budget = RequestBudget(
        state_path=settings.budget_file, daily_limit=settings.daily_request_limit
    )

    if settings.api_provider == "rapidapi":
        return ApiFootballClient.for_rapidapi(
            settings.api_key, cache, budget,
            min_interval_seconds=settings.api_min_interval_seconds,
        )
    return ApiFootballClient(
        api_key=settings.api_key, cache=cache, budget=budget,
        min_interval_seconds=settings.api_min_interval_seconds,
    )


def build_telegram(settings: Settings | None = None):
    """Il canale delle notifiche. Spento se manca il token: non e' un errore."""
    from .notifiche import Telegram

    settings = settings or load_settings()
    return Telegram(
        token=settings.telegram_token,
        chat_id=settings.telegram_chat_id,
        verboso=settings.telegram_verboso,
    )


def build_registro_invii(settings: Settings | None = None):
    """Cosa e' gia' stato detto: serve a non dirlo due volte."""
    from .notifiche import RegistroInvii

    settings = settings or load_settings()
    return RegistroInvii(path=settings.notifiche_file)


def build_store(settings: Settings | None = None):
    """Archivio della stagione in corso e diario della raccolta."""
    from .data.store import DayJournal, SeasonStore

    settings = settings or load_settings()
    return SeasonStore(root=settings.store_dir), DayJournal(path=settings.journal_file)


def build_storico_stores(settings: Settings | None = None):
    """Gli archivi delle stagioni passate gia' riempite, ``{anno: SeasonStore}``.

    Vuoto finche' nessuno ha comprato un piano a pagamento e lanciato
    `scripts/riempi_storico.py`: sul piano gratuito quelle stagioni non sono
    leggibili e la cartella non esiste nemmeno.

    Si rilegge dal disco a ogni chiamata, e non si tiene in memoria: un
    riempimento dura ore e chi lo lancia non deve dover riavviare il sito per
    vedere comparire la stagione nuova.
    """
    from .data.storico import stagioni_riempite

    settings = settings or load_settings()
    return stagioni_riempite(settings.storico_dir)


def build_importato_stores(settings: Settings | None = None):
    """Gli archivi delle stagioni importate da un file, ``{anno: SeasonStore}``.

    Vuoto finche' nessuno ha lanciato `scripts/importa_risultati.py`. Come per
    le stagioni ricomprate si rilegge dal disco a ogni chiamata: importare una
    stagione non deve richiedere di riavviare il sito.
    """
    from .data.importato import stagioni_importate

    settings = settings or load_settings()
    return stagioni_importate(settings.importato_dir)


def build_prediction_store(settings: Settings | None = None):
    """Storico dei pronostici. Si crea da solo alla prima apertura."""
    from .data.predictions import PredictionStore

    settings = settings or load_settings()
    return PredictionStore(path=settings.predictions_db)
