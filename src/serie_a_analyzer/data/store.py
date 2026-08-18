"""Archivio locale della stagione in corso e diario della raccolta.

Sul piano gratuito la stagione in corso e' leggibile solo un giorno alla volta,
dentro una finestra di tre giorni: ieri, oggi, domani. Fuori da quella finestra
non si torna indietro.

La conseguenza e' il fatto centrale attorno a cui gira questo modulo:

    **l'applicazione e' l'unica copia della stagione in corso.**

Non e' una cache che si puo' buttare e riempire di nuovo. Un giorno non
raccolto e' un giorno perso per sempre, e con esso le partite giocate quel
giorno. Da qui due scelte:

* `SeasonStore` conserva il JSON **grezzo**, non gli oggetti gia' interpretati.
  Un errore nel parsing si corregge e si rilegge; un dato mai salvato no.
* `DayJournal` tiene il registro di cosa e' stato raccolto e quando, cosi' un
  buco si vede subito invece di scoprirlo a fine stagione.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# Stati in cui una partita non cambiera' piu': raccolti questi, il giorno e'
# chiuso e non va piu' richiesto.
FINAL_STATUSES = frozenset({"finished", "cancelled"})

# Quante volte riprovare un dettaglio che torna vuoto prima di rinunciare.
# Serve perche' una risposta vuota e' ambigua: puo' voler dire "non ancora
# pubblicato" (riprovare fra qualche ora ha senso) oppure "questo campionato
# non lo copre" (riprovare all'infinito brucerebbe budget per niente).
MAX_DETAIL_ATTEMPTS = 3


def _write_json(path: Path, payload: Any) -> None:
    """Scrittura atomica: un'interruzione non lascia un archivio corrotto."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _oggi() -> date:
    """Oggi in UTC, cioe' il fuso in cui l'API espone le date delle partite."""
    return datetime.now(timezone.utc).date()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


@dataclass
class SeasonStore:
    """Le partite di Serie A raccolte giorno per giorno, in JSON grezzo.

    Chiave: l'id di API-Football della partita. Ripassare lo stesso giorno
    aggiorna la riga invece di duplicarla, cosi' una partita vista prima
    "programmata" e poi "conclusa" resta una sola.
    """

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def _fixtures_path(self) -> Path:
        return self.root / "fixtures.json"

    def _detail_path(self, fixture_id: int, kind: str) -> Path:
        return self.root / "details" / str(fixture_id) / f"{kind}.json"

    # ---- partite ----------------------------------------------------------

    def load_fixtures(self) -> dict[str, dict[str, Any]]:
        return _read_json(self._fixtures_path, {})

    def upsert_fixtures(self, raw_fixtures: Iterable[dict[str, Any]]) -> tuple[int, int]:
        """Inserisce o aggiorna partite. Restituisce ``(nuove, aggiornate)``."""
        archivio = self.load_fixtures()
        nuove = aggiornate = 0
        for raw in raw_fixtures:
            fid = (raw.get("fixture") or {}).get("id")
            if fid is None:
                continue
            chiave = str(fid)
            if chiave in archivio:
                # Si sovrascrive solo se qualcosa e' cambiato: evita di
                # riscrivere l'intero archivio a ogni passaggio a vuoto.
                if archivio[chiave] != raw:
                    archivio[chiave] = raw
                    aggiornate += 1
            else:
                archivio[chiave] = raw
                nuove += 1
        if nuove or aggiornate:
            _write_json(self._fixtures_path, archivio)
        return nuove, aggiornate

    def fixture_ids(self) -> list[int]:
        return [int(k) for k in self.load_fixtures()]

    # ---- dettagli per partita ---------------------------------------------
    #
    # Formazioni, statistiche giocatori ed eventi vivono in una busta che
    # conserva anche il numero di tentativi. Senza, una risposta vuota salvata
    # una volta sembrerebbe per sempre un dettaglio "gia' raccolto" e la
    # partita resterebbe senza formazione senza che nessuno se ne accorga.

    def detail_envelope(self, fixture_id: int, kind: str) -> dict[str, Any] | None:
        return _read_json(self._detail_path(fixture_id, kind), None)

    def save_detail(self, fixture_id: int, kind: str, payload: Any) -> None:
        """Salva un dettaglio, contando il tentativo anche se torna vuoto."""
        path = self._detail_path(fixture_id, kind)
        precedente = _read_json(path, None) or {}
        _write_json(path, {
            "attempts": int(precedente.get("attempts", 0)) + 1,
            "payload": payload,
        })

    def load_detail(self, fixture_id: int, kind: str) -> Any:
        """Il contenuto utile di un dettaglio, o ``None`` se non c'e'."""
        busta = self.detail_envelope(fixture_id, kind)
        return busta.get("payload") if busta else None

    def has_detail(self, fixture_id: int, kind: str) -> bool:
        """Il dettaglio e' presente e non vuoto."""
        return bool(self.load_detail(fixture_id, kind))

    def needs_detail(self, fixture_id: int, kind: str) -> bool:
        """Vale la pena spendere una richiesta per questo dettaglio?

        No se lo abbiamo gia', e no se abbiamo gia' provato abbastanza volte a
        prenderlo trovandolo vuoto: a quel punto il campionato semplicemente
        non lo copre.
        """
        busta = self.detail_envelope(fixture_id, kind)
        if busta is None:
            return True
        if busta.get("payload"):
            return False
        return int(busta.get("attempts", 0)) < MAX_DETAIL_ATTEMPTS

    def stats(self) -> dict[str, int]:
        fixtures = self.load_fixtures()
        dettagli = self.root / "details"
        return {
            "partite": len(fixtures),
            "con_dettagli": sum(1 for _ in dettagli.iterdir()) if dettagli.exists() else 0,
        }


@dataclass
class DayJournal:
    """Registro dei giorni raccolti: cosa c'era, cosa manca, cosa e' perso.

    Un giorno viene marcato ``chiuso`` solo quando sono vere **due** cose:
    tutte le sue partite di Serie A sono in uno stato definitivo, e il giorno
    e' ormai passato. Finche' non lo e', il giorno viene richiesto di nuovo
    alla prossima esecuzione — e' come si recuperano i risultati di una partita
    che era ancora in corso al momento della raccolta.

    La seconda condizione e' meno ovvia della prima ma pesa di piu'. Un giorno
    ancora da giocare risulta banalmente "tutto definitivo" quando e' vuoto:
    zero partite, zero concluse. Chiuderlo per questo significherebbe non
    richiederlo mai piu' — e il calendario della Serie A **cambia**, con
    recuperi e anticipi che compaiono anche a pochi giorni di distanza. Un
    turno aggiunto dopo il nostro sguardo cadrebbe fuori dalla finestra di tre
    giorni senza che nessuno se ne accorga, cioe' andrebbe perso per sempre.
    Meglio spendere una richiesta di troppo che scoprire a stagione finita che
    manca una giornata.
    """

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Any]:
        return _read_json(self.path, {"days": {}})

    def record(
        self,
        day: date,
        *,
        collected_at: str,
        n_fixtures: int,
        n_final: int,
        requests_spent: int,
        today: date | None = None,   # accettato per simmetria con is_closed
    ) -> None:
        """Registra cosa si e' visto quel giorno. Non decide se richiederlo ancora.

        Il diario tiene **osservazioni**, non decisioni: ``closed`` qui vuol
        dire soltanto "tutte le partite viste erano in uno stato definitivo".
        Se il giorno vada davvero richiesto di nuovo lo stabilisce
        `is_closed`, che ci aggiunge il passare del tempo. Separare le due cose
        fa si' che un giorno osservato completo stasera si chiuda da solo
        domani, senza spendere un'altra richiesta per riscoprirlo.
        """
        stato = self._load()
        stato.setdefault("days", {})[day.isoformat()] = {
            "collected_at": collected_at,
            "fixtures": n_fixtures,
            "final": n_final,
            "closed": n_fixtures == n_final,
            "requests_spent": requests_spent,
        }
        _write_json(self.path, stato)

    def entry(self, day: date) -> dict[str, Any] | None:
        return self._load().get("days", {}).get(day.isoformat())

    def is_closed(self, day: date, today: date | None = None) -> bool:
        """Il giorno e' definitivo: non va piu' richiesto.

        Due condizioni, e la seconda e' quella che salva l'archivio: il giorno
        deve essere **passato**. Oggi e domani non si chiudono mai, per quanto
        completi sembrino.

        Che la regola stia qui, in lettura, e non nel dato scritto, e' una
        scelta: rende impossibile che un diario vecchio — o corretto a mano —
        faccia saltare la raccolta di un giorno che deve ancora arrivare.
        """
        voce = self.entry(day)
        if not voce or not voce.get("closed"):
            return False
        return day < (today or _oggi())

    def collected_days(self) -> list[date]:
        return sorted(date.fromisoformat(d) for d in self._load().get("days", {}))

    def missing_days(self, since: date, until: date) -> list[date]:
        """Giorni mai raccolti nell'intervallo. Sono buchi irrecuperabili.

        Non e' una lista di cose da rifare: la finestra di tre giorni e' gia'
        passata. Serve a sapere cosa manca all'archivio, e quindi quanto
        fidarsi delle statistiche costruite sopra.
        """
        visti = set(self._load().get("days", {}))
        mancanti = []
        giorno = since
        while giorno <= until:
            if giorno.isoformat() not in visti:
                mancanti.append(giorno)
            giorno += timedelta(days=1)
        return mancanti
