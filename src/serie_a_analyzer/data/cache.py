"""Cache su disco delle risposte grezze di API-Football.

Con 100 richieste al giorno la cache non e' un'ottimizzazione: e' cio' che
rende il progetto possibile. Ogni risposta viene salvata cosi' com'e', e una
richiesta gia' presente in cache e ancora valida non consuma budget.

Conservare il JSON grezzo (invece dei soli oggetti gia' interpretati) permette
di correggere un errore di parsing e rileggere i dati senza rispendere
richieste. Con questo vincolo di budget e' una garanzia che vale molto.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Durate di validita' consigliate per tipo di dato. La logica e': quanto in
# fretta questo dato diventa sbagliato?
TTL_FOREVER = None                  # stagioni concluse: non cambiano piu'
TTL_LINEUPS = 5 * 60               # formazioni: escono ~30 min prima, evolvono
TTL_LIVE = 2 * 60                  # partite in corso
TTL_UPCOMING_FIXTURES = 6 * 60 * 60  # calendario prossima giornata
TTL_ODDS = 30 * 60
TTL_STATIC = 7 * 24 * 60 * 60      # anagrafiche squadre, stadi


@dataclass
class ResponseCache:
    """Cache file-based con chiave derivata da endpoint e parametri."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def key(self, endpoint: str, params: dict[str, Any] | None = None) -> str:
        """Chiave stabile: stessi parametri in ordine diverso danno la stessa chiave."""
        normalized = json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"{endpoint}?{normalized}".encode("utf-8")).hexdigest()[:16]
        # Il prefisso leggibile rende la cache ispezionabile a mano.
        safe_endpoint = endpoint.strip("/").replace("/", "_") or "root"
        return f"{safe_endpoint}__{digest}"

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(
        self, endpoint: str, params: dict[str, Any] | None = None, ttl: int | None = TTL_FOREVER
    ) -> dict[str, Any] | None:
        """Risposta in cache se presente e non scaduta, altrimenti ``None``."""
        path = self._path(self.key(endpoint, params))
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if ttl is not None:
            age = time.time() - float(entry.get("cached_at", 0))
            if age > ttl:
                return None
        return entry.get("payload")

    def set(
        self, endpoint: str, params: dict[str, Any] | None, payload: dict[str, Any]
    ) -> None:
        """Salva una risposta grezza."""
        path = self._path(self.key(endpoint, params))
        entry = {
            "endpoint": endpoint,
            "params": params or {},
            "cached_at": time.time(),
            "payload": payload,
        }
        # Scrittura atomica: un'interruzione non lascia un file mezzo scritto
        # che poi verrebbe scartato come corrotto, sprecando una richiesta.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def invalidate(self, endpoint: str, params: dict[str, Any] | None = None) -> bool:
        path = self._path(self.key(endpoint, params))
        if path.exists():
            path.unlink()
            return True
        return False

    def stats(self) -> dict[str, Any]:
        """Quante risposte sono in cache e quanto spazio occupano."""
        files = list(self.root.glob("*.json"))
        return {
            "entries": len(files),
            "bytes": sum(f.stat().st_size for f in files),
        }
