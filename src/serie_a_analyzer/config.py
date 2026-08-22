"""Configurazione da variabili d'ambiente / file .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

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
        cache_dir=Path(os.getenv("CACHE_DIR", data_dir / "cache")),
        budget_file=Path(os.getenv("BUDGET_FILE", data_dir / "budget.json")),
        store_dir=Path(os.getenv("STORE_DIR", data_dir / "serie_a")),
        storico_dir=Path(os.getenv("STORICO_DIR", data_dir / "storico")),
        importato_dir=Path(os.getenv("IMPORTATO_DIR", data_dir / "importato")),
        journal_file=Path(os.getenv("JOURNAL_FILE", data_dir / "journal.json")),
        predictions_db=Path(os.getenv("PREDICTIONS_DB", data_dir / "pronostici.db")),
        database_url=os.getenv("DATABASE_URL") or None,
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
        return ApiFootballClient.for_rapidapi(settings.api_key, cache, budget)
    return ApiFootballClient(api_key=settings.api_key, cache=cache, budget=budget)


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
