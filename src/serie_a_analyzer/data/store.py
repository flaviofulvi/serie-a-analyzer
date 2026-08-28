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
import os
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

# Quante volte riprovare un tabellino che c'e' ma e' una fotografia scattata a
# partita in corso. Piu' generoso di MAX_DETAIL_ATTEMPTS perche' qui non si sta
# aspettando un dato che forse non esiste: quel tabellino esistera' di sicuro,
# e l'attesa si misura in ore. Un tetto serve lo stesso, per il caso in cui
# l'API non lo consolidi mai: senza, si spenderebbe una richiesta a ogni
# raccolta, per sempre. Dodici copre un paio di giorni di raccolte: il
# tabellino di una partita giocata esiste, e vale la pena aspettarlo.
MAX_TABELLINO_ATTEMPTS = 12

# Sotto quanti minuti giocati un tabellino non e' finito. Un tabellino
# definitivo ha sempre qualcuno arrivato in fondo; una fotografia scattata al
# 64' ha tutti e ventidue i titolari fermi sullo stesso numero.
MINUTI_DI_UNA_PARTITA_INTERA = 90


def _write_json(path: Path, payload: Any) -> None:
    """Scrittura atomica: un'interruzione non lascia un archivio corrotto."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def tabellino_provvisorio(payload: Any) -> bool:
    """Il tabellino di `/fixtures/players` e' una fotografia a partita in corso?

    Nasce da un guasto vero, il 22 agosto 2026: Genoa-Napoli e Parma-Cagliari
    sono state chiuse in archivio con tutti e ventidue i titolari fermi al 64'
    e al 61', zero gol e zero sostituzioni, mentre gli eventi — chiesti nello
    stesso minuto, allo stesso server — raccontavano la partita intera. Non e'
    un errore nostro: e' l'API che per quelle due partite continuava a servire
    l'ultima lettura del suo feed live, due ore dopo il fischio finale.

    Diventa un errore nostro un attimo dopo, ed e' il punto: `needs_detail`
    considerava concluso qualunque dettaglio non vuoto, quindi quel tabellino
    sbagliato non sarebbe mai piu' stato richiesto. Un dato che non si puo'
    correggere e' peggio di un dato che manca — un buco si vede, una pagella
    del 64' spacciata per finale no.

    Il segnale sono i **minuti**, non i gol. Verrebbe naturale confrontare i
    gol del tabellino col risultato della partita, e su queste due partite
    funzionerebbe (0 contro 2, 0 contro 1); ma gli autogol non entrano nel
    conto del marcatore, e ogni partita con un autogol risulterebbe
    provvisoria per sempre. I minuti non hanno questa ambiguita': in una
    partita finita qualcuno arriva sempre in fondo.

    Resta fuori un caso, e vale dichiararlo: una fotografia scattata al 90'
    esatto, prima di un gol al 90'+3, passerebbe questo controllo. E' molto
    meno probabile di quella al 64' e non c'e' un modo di scoprirla che non
    apra la porta agli autogol.
    """
    if not payload:
        # Vuoto e' un'altra cosa e ha gia' la sua regola: MAX_DETAIL_ATTEMPTS.
        return False
    minuti = [
        statistiche[0].get("games", {}).get("minutes")
        for squadra in payload
        for giocatore in squadra.get("players", [])
        if (statistiche := giocatore.get("statistics"))
    ]
    giocati = [m for m in minuti if m is not None]
    if not giocati:
        # Un tabellino senza nemmeno un minuto non dice niente: non e' una
        # fotografia a meta' partita, e' un payload di forma inattesa. Non
        # tocca a questa funzione decidere che farne.
        return False
    return max(giocati) < MINUTI_DI_UNA_PARTITA_INTERA


def tabellini_arresi(store, fixture_ids: Iterable[int]) -> list[int]:
    """Le partite il cui tabellino e' sbagliato e non verra' piu' richiesto.

    E' il momento — l'unico — in cui vale la pena disturbare qualcuno. Un
    tabellino provvisorio nelle ore dopo la partita e' normale e si risolve da
    solo: dirlo ogni sera insegnerebbe a ignorare il messaggio. Quando invece i
    tentativi sono finiti, il sistema ha smesso di poter rimediare e quel dato
    resta sbagliato per sempre a meno che qualcuno non intervenga.

    Sta qui e non in `notifiche.py` perche' la domanda e' sull'archivio, non
    sul canale: chi manda i messaggi riceve la risposta gia' pronta e non sa
    nulla di buste e tentativi.
    """
    return [
        fid for fid in fixture_ids
        if tabellino_provvisorio(store.load_detail(fid, "players"))
        and not store.needs_detail(fid, "players")
    ]


def tabellini_pronti(store, fixture_ids: Iterable[int]) -> list[int]:
    """Le partite il cui tabellino c'e' **ed e' quello finale**.

    E' l'altra faccia di `tabellini_arresi`, e serve a dire la buona notizia
    invece del guasto: quando i tabellini di una giornata sono arrivati tutti,
    quelle partite entrano nelle schede giocatore e nel peso di chi scende in
    campo — cioe' e' successo qualcosa, non e' solo finita un'attesa.

    "Pronto" vuol dire due cose insieme, e togliendone una si mentirebbe: la
    busta c'e' (non e' vuota) e non e' la fotografia del feed live scattata a
    partita in corso. Un tabellino provvisorio non e' un tabellino parziale, e'
    un tabellino sbagliato, e conta come non ancora arrivato — che e' la
    verita', perche' arrivera'.

    Sta qui e non in `notifiche.py` per la stessa ragione di `tabellini_arresi`:
    la domanda e' sull'archivio, non sul canale. Chi manda i messaggi riceve la
    risposta gia' pronta e non sa nulla di buste.
    """
    return [
        fid for fid in fixture_ids
        if (payload := store.load_detail(fid, "players"))
        and not tabellino_provvisorio(payload)
    ]


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

        C'e' un terzo caso, e sta qui in lettura per la stessa ragione di
        `is_closed()`: un tabellino puo' esserci ed essere sbagliato, perche'
        l'API a volte serve l'ultima lettura del feed live anche a partita
        finita da un pezzo (vedi `tabellino_provvisorio`). Quello va richiesto
        ancora. Metterlo nel dato scritto non basterebbe: le buste salvate
        prima di questa regola resterebbero sbagliate per sempre.
        """
        busta = self.detail_envelope(fixture_id, kind)
        if busta is None:
            return True
        payload = busta.get("payload")
        tentativi = int(busta.get("attempts", 0))
        if payload:
            if kind == "players" and tabellino_provvisorio(payload):
                return tentativi < MAX_TABELLINO_ATTEMPTS
            return False
        return tentativi < MAX_DETAIL_ATTEMPTS

    def details_signature(self) -> tuple[int, float]:
        """Impronta dei dettagli: cambia quando ne viene salvato uno nuovo.

        Serve a chi tiene in memoria qualcosa costruito leggendoli — le schede
        giocatore — per sapere quando rifarlo. Non basta l'impronta di
        `fixtures.json`: i dettagli arrivano dopo, spesso senza che l'elenco
        delle partite si muova di un byte.

        Si guardano le cartelle, non i file dentro: sono un ordine di grandezza
        meno, e la data di una cartella cambia comunque a ogni file scritto al
        suo interno. Non ci si accontenta pero' della data di `details`: chi
        volesse cavarsela con quella si perderebbe proprio il caso che conta —
        un tabellino **riscritto**, perche' un file nuovo dentro una cartella
        che c'era gia' non cambia la data della cartella che la contiene.

        Va letta con `os.scandir` e non con `iterdir()` piu' `stat()`, e la
        differenza non e' cosmetica: `stat()` e' una chiamata di sistema per
        cartella, mentre `scandir` quei dati se li porta gia' dietro dalla
        lettura della directory. Con quattro stagioni sul disco sono ~1530
        cartelle, e misurando si passa da **30 ms a 1 ms**. Conta perche'
        l'impronta viene chiesta piu' volte per pagina e la pagina si guarda
        dal telefono. La strada alternativa — tenersi l'impronta in caldo
        qualche secondo — costava un modello che si ristima in ritardo su un
        risultato appena arrivato, ed e' un prezzo molto piu' alto di questo.
        """
        radice = self.root / "details"
        if not radice.exists():
            return (0, 0.0)
        quante = 0
        ultima = 0.0
        with os.scandir(radice) as voci:
            for voce in voci:
                quante += 1
                ultima = max(ultima, voce.stat().st_mtime)
        return (quante, ultima)

    def stats(self) -> dict[str, int]:
        fixtures = self.load_fixtures()
        dettagli = self.root / "details"
        return {
            "partite": len(fixtures),
            "con_dettagli": sum(1 for _ in dettagli.iterdir()) if dettagli.exists() else 0,
        }


@dataclass
class StoreUnito:
    """Piu' archivi letti come se fossero uno solo. Sola lettura.

    Nasce quando una stagione passata viene riempita (`data/storico.py`): da
    quel momento i dettagli di una partita stanno in due posti diversi —
    `data/serie_a/` per la stagione in corso, `data/storico/<anno>/` per quelle
    ricomprate — e chi li legge non deve saperlo. Le schede giocatore, in
    particolare, chiedono "il tabellino della partita 12345" e devono averlo da
    qualunque archivio provenga.

    L'ordine conta: **vince il primo archivio che ha il dato**. Si mette per
    primo quello della stagione in corso, che e' l'unica copia esistente dei
    suoi giorni; gli archivi storici sono sempre riscaricabili finche' il piano
    li copre.
    """

    stores: list[SeasonStore]

    def fixture_ids(self) -> list[int]:
        visti: dict[int, None] = {}
        for store in self.stores:
            for fid in store.fixture_ids():
                visti.setdefault(fid, None)
        return list(visti)

    def load_detail(self, fixture_id: int, kind: str) -> Any:
        """Il primo dettaglio non vuoto fra gli archivi, o ``None``.

        Una busta vuota in un archivio non ferma la ricerca: vuol dire che li'
        quel dato non c'e', non che non esista altrove.
        """
        for store in self.stores:
            payload = store.load_detail(fixture_id, kind)
            if payload:
                return payload
        return None

    def has_detail(self, fixture_id: int, kind: str) -> bool:
        return bool(self.load_detail(fixture_id, kind))

    def details_signature(self) -> tuple:
        """Impronta di tutti gli archivi messi in fila.

        Cambia se cambia uno qualsiasi di loro: un riempimento storico appena
        finito deve far ricostruire le schede giocatore esattamente come le fa
        ricostruire la raccolta di ieri sera.
        """
        return tuple(store.details_signature() for store in self.stores)


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
