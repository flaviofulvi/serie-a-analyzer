"""Risultati di una stagione presi da un file, non dall'API.

Fra lo storico 2022-2024 e l'inizio della raccolta giornaliera resta un buco:
una stagione intera che il piano gratuito non lascia leggere. `storico.py` sa
chiuderlo comprandola, ~1140 richieste; questo modulo la chiude **gratis**,
quando di quella stagione esiste un file di risultati.

E' meno di quello che si compra, e la differenza va tenuta a mente: un file di
risultati da' data, squadre e gol, cioe' **esattamente e solo cio' che il
modello mangia** (vedi `to_model_matches` in `parsers.py`). Non da' formazioni,
tabellini, eventi, voti. I pronostici migliorano come se la stagione fosse
stata comprata; le schede giocatore no, e non devono nemmeno far finta.

Tre vincoli danno forma al modulo.

**Una squadra e' un id, mai un nome.** E' la regola di tutto il progetto, e qui
e' il punto piu' delicato: il file scrive "Milan", l'API dice "AC Milan". Se i
due non si riconoscono, il Milan si sdoppia in due squadre con meta' storia
ciascuna, e nessuno se ne accorge perche' il modello continua a rispondere.
Gli id non si inventano: si cercano fra le squadre gia' viste sul disco, e un
nome che non si aggancia a nessuna **ferma l'importazione** invece di entrare
in archivio come squadra nuova.

**Un risultato importato non e' un dato pagato.** Le partite ricevono id
sintetici (`92025001`), riconoscibili a occhio e lontanissimi da quelli veri.
Il giorno in cui la stessa stagione venisse ricomprata sul serio, i due
archivi non si mescolano e vince quello pagato: e' `dataset.py` a deciderlo,
saltando l'importato di una stagione che esiste anche in `data/storico/`.

**Il file si controlla prima di scriverlo.** Un calendario di Serie A ha una
forma che si verifica da sola — 380 partite, ogni squadra 19 in casa e 19
fuori, ogni accoppiamento una volta per verso. Un file che non la rispetta si
dichiara, non si importa di nascosto: l'archivio e' l'unica cosa che il
modello vedra' poi, e un errore li' dentro diventa invisibile.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .client import SERIE_A_LEAGUE_ID
from .store import SeasonStore

logger = logging.getLogger(__name__)

# Prefisso degli id sintetici. Gli id veri di API-Football stanno intorno a
# 1.550.000 e crescono di qualche centinaio di migliaia l'anno: partire da
# 9xxxxxxx tiene le due numerazioni separate per secoli, e rende evidente a
# colpo d'occhio che quella partita non e' arrivata dall'API.
_PREFISSO_SINTETICO = 9

# Formati di data accettati, provati in quest'ordine. Il primo e' quello dei
# calendari scaricabili in giro ("23/08/2025 16:30"); gli altri coprono le
# varianti piu' comuni senza costringere a riscrivere il file a mano.
_FORMATI_DATA = (
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d-%m-%Y %H:%M",
    "%m/%d/%Y %H:%M",
)

# Un risultato: "2 - 1", "2-1", "2 - 1" con trattini di varia lunghezza.
_RISULTATO = re.compile(r"^\s*(\d+)\s*[-–—:]\s*(\d+)\s*$")

# Parole che una societa' si porta nel nome e che il file di solito non scrive.
# Si tolgono solo se stanno in testa o in coda: "Inter" non deve diventare
# vuoto e "Hellas Verona" deve restare "hellas verona".
_PAROLE_SOCIETARIE = frozenset({
    "ac", "as", "fc", "cfc", "us", "usd", "ssc", "ss", "ssd", "asd", "acf",
    "calcio", "football", "club", "spa", "srl", "1909", "cf",
})

# Nomi delle colonne accettate, per ruolo. Il confronto avviene sul nome
# normalizzato (minuscolo, senza spazi), cosi' "Home Team" e "hometeam" sono
# la stessa cosa.
_COLONNE = {
    "numero": ("matchnumber", "numero", "n", "id", "match"),
    "giornata": ("roundnumber", "round", "giornata", "matchday", "week"),
    "data": ("date", "data", "datetime", "kickoff", "dateutc"),
    "luogo": ("location", "venue", "stadio", "stadium"),
    "casa": ("hometeam", "home", "casa", "squadracasa", "team1"),
    "ospite": ("awayteam", "away", "ospite", "trasferta", "squadraospite", "team2"),
    "risultato": ("result", "risultato", "score", "ft", "finalscore"),
}


class FileIllegibile(RuntimeError):
    """Il file non e' leggibile affatto. I problemi di contenuto non si sollevano."""


# ---- identita' delle squadre -----------------------------------------------


def normalizza(nome: str) -> str:
    """La forma con cui due grafie dello stesso club si riconoscono.

    Minuscolo, senza accenti, senza punteggiatura, senza le sigle societarie in
    testa o in coda. "AC Milan" e "Milan" arrivano entrambi a "milan"; "AC
    Milan W", che e' un'altra squadra, arriva a "milan w" e resta distinta.
    """
    piatto = unicodedata.normalize("NFKD", nome or "")
    piatto = "".join(c for c in piatto if not unicodedata.combining(c))
    piatto = re.sub(r"[^\w\s]", " ", piatto.lower())
    token = [t for t in piatto.split() if t]
    while len(token) > 1 and token[0] in _PAROLE_SOCIETARIE:
        token.pop(0)
    while len(token) > 1 and token[-1] in _PAROLE_SOCIETARIE:
        token.pop()
    return " ".join(token)


def indice_per_nome(nomi: dict[int, str]) -> dict[str, int]:
    """Da ``{id: nome}`` a ``{nome normalizzato: id}``.

    Un nome normalizzato che porta a due id diversi viene **tolto**: e'
    ambiguo, e tirare a indovinare fra due squadre e' peggio che dichiarare di
    non saperlo. Chi resta senza aggancio finisce fra le sconosciute, e
    l'importazione si ferma dicendolo.
    """
    per_nome: dict[str, set[int]] = {}
    for team_id, nome in nomi.items():
        chiave = normalizza(nome)
        if chiave:
            per_nome.setdefault(chiave, set()).add(int(team_id))
    return {chiave: next(iter(ids)) for chiave, ids in per_nome.items() if len(ids) == 1}


def squadre_viste(percorsi: Iterable[Path]) -> dict[int, str]:
    """Tutte le squadre nominate nei file JSON indicati, ``{id: nome}``.

    Serve per le squadre che nel nostro storico di Serie A non compaiono: una
    neopromossa poi retrocessa non ha mai giocato nelle stagioni che abbiamo,
    ma il suo id e' quasi sempre gia' sul disco — nominato da una partita di un
    altro campionato finita nella stessa busta di cache. Cercarlo li' costa
    zero richieste; chiederlo all'API ne costerebbe una.

    E' una sorgente **larga e meno affidabile** della lega che ci interessa: ci
    finisce dentro mezzo mondo, comprese squadre giovanili e femminili con nomi
    simili. Per questo va usata solo come ripiego, e solo quando il nome e'
    univoco (`indice_per_nome` scarta gli ambigui).

    Si riconosce una squadra dal fatto che ha ``id``, ``name`` e ``logo``: e'
    la forma che API-Football usa per i club e solo per loro, mentre leghe e
    giocatori nella stessa busta hanno campi diversi.
    """
    trovate: dict[int, str] = {}

    def scava(nodo: Any) -> None:
        if isinstance(nodo, dict):
            ident, nome = nodo.get("id"), nodo.get("name")
            if isinstance(ident, int) and isinstance(nome, str) and "logo" in nodo:
                trovate[ident] = nome
            for valore in nodo.values():
                scava(valore)
        elif isinstance(nodo, list):
            for valore in nodo:
                scava(valore)

    for percorso in percorsi:
        try:
            scava(json.loads(Path(percorso).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, RecursionError):
            continue
    return trovate


def sorgenti_nomi(cache_dir: Path, store_dir: Path) -> list[Path]:
    """I file su disco in cui puo' comparire l'id di una squadra."""
    cache_dir, store_dir = Path(cache_dir), Path(store_dir)
    percorsi = sorted(cache_dir.glob("fixtures*.json")) if cache_dir.exists() else []
    corrente = store_dir / "fixtures.json"
    return percorsi + ([corrente] if corrente.exists() else [])


# ---- id sintetici ----------------------------------------------------------


def id_sintetico(stagione: int, numero: int) -> int:
    """L'id di una partita importata: prefisso, stagione, numero di partita.

    Deterministico: reimportare lo stesso file aggiorna le stesse righe invece
    di affiancarne 380 nuove.
    """
    return int(f"{_PREFISSO_SINTETICO}{stagione}{numero:03d}")


def e_importata(fixture_id: int) -> bool:
    """La partita viene da un file e non dall'API."""
    testo = str(fixture_id)
    return testo.startswith(str(_PREFISSO_SINTETICO)) and len(testo) >= 8


# ---- lettura del file ------------------------------------------------------


@dataclass(frozen=True)
class RigaFile:
    """Una riga del file, gia' interpretata ma non ancora agganciata agli id."""

    numero: int
    giornata: int | None
    quando: datetime
    casa: str
    ospite: str
    gol_casa: int | None
    gol_ospite: int | None
    luogo: str | None

    @property
    def giocata(self) -> bool:
        return self.gol_casa is not None and self.gol_ospite is not None


@dataclass
class Rapporto:
    """Cosa dice il file e cosa e' finito in archivio. Sempre restituito."""

    file: str
    stagione: int
    righe: int = 0
    partite: int = 0
    giocate: int = 0
    da_giocare: int = 0
    squadre: dict[str, int] = field(default_factory=dict)
    sconosciute: list[str] = field(default_factory=list)
    problemi: list[str] = field(default_factory=list)
    avvisi: list[str] = field(default_factory=list)
    nuove: int = 0
    aggiornate: int = 0
    scritto: bool = False

    @property
    def valido(self) -> bool:
        """Nessun problema che renderebbe l'archivio *sbagliato*."""
        return not self.problemi

    @property
    def completo(self) -> bool:
        """Nessun avviso, cioe' il file ha la forma di un campionato intero."""
        return not self.avvisi


def _intesta(colonne: Iterable[str]) -> dict[str, str]:
    """Associa a ogni ruolo la colonna del file che lo contiene."""
    per_forma = {re.sub(r"[^a-z0-9]", "", (c or "").lower()): c for c in colonne}
    trovate: dict[str, str] = {}
    for ruolo, candidate in _COLONNE.items():
        for candidata in candidate:
            if candidata in per_forma:
                trovate[ruolo] = per_forma[candidata]
                break
    return trovate


def _data(grezza: str) -> datetime | None:
    """La data della partita, in UTC.

    I calendari scaricabili esistono in due versioni, ora locale e UTC, e il
    nome del file lo dice. Si assume UTC perche' e' il fuso in cui l'API espone
    le date e in cui l'archivio le conserva: un file in ora locale sposterebbe
    le partite di un paio d'ore — irrilevante per il peso temporale, ma capace
    di cambiare il giorno a quelle vicine a mezzanotte.
    """
    testo = (grezza or "").strip()
    if not testo:
        return None
    # Un fuso gia' scritto nel file (ISO 8601) e' un'informazione migliore di
    # qualunque assunzione: si usa quello.
    try:
        letta = datetime.fromisoformat(testo.replace("Z", "+00:00"))
        return letta if letta.tzinfo else letta.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for formato in _FORMATI_DATA:
        try:
            return datetime.strptime(testo, formato).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _numero_da(riga: dict[str, str], colonne: dict[str, str], ruolo: str) -> int | None:
    valore = (riga.get(colonne.get(ruolo, ""), "") or "").strip()
    return int(valore) if valore.isdigit() else None


def leggi_file(percorso: Path) -> tuple[list[RigaFile], list[str]]:
    """Legge un CSV di risultati. Restituisce ``(righe, problemi)``.

    Le righe illeggibili non fanno saltare il file: vengono elencate fra i
    problemi con il loro numero di riga, cosi' si corregge il file invece di
    indovinare cosa e' andato storto.
    """
    percorso = Path(percorso)
    try:
        # utf-8-sig: i file esportati da Excel iniziano con un BOM che
        # altrimenti resterebbe attaccato al nome della prima colonna.
        testo = percorso.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise FileIllegibile(f"non riesco a leggere {percorso}: {exc}") from exc

    lettore = csv.DictReader(testo.splitlines())
    if not lettore.fieldnames:
        raise FileIllegibile(f"{percorso.name} non ha un'intestazione leggibile")

    colonne = _intesta(lettore.fieldnames)
    mancanti = [r for r in ("data", "casa", "ospite") if r not in colonne]
    if mancanti:
        raise FileIllegibile(
            f"{percorso.name}: non trovo le colonne {', '.join(mancanti)}. "
            f"Intestazione letta: {', '.join(lettore.fieldnames)}"
        )

    righe: list[RigaFile] = []
    problemi: list[str] = []
    for indice, riga in enumerate(lettore, start=2):  # la 1 e' l'intestazione
        casa = (riga.get(colonne["casa"]) or "").strip()
        ospite = (riga.get(colonne["ospite"]) or "").strip()
        if not casa or not ospite:
            problemi.append(f"riga {indice}: manca una delle due squadre")
            continue

        quando = _data(riga.get(colonne["data"], ""))
        if quando is None:
            problemi.append(
                f"riga {indice}: data illeggibile ({riga.get(colonne['data'])!r})"
            )
            continue

        gol_casa = gol_ospite = None
        grezzo = (riga.get(colonne.get("risultato", ""), "") or "").strip()
        if grezzo:
            trovato = _RISULTATO.match(grezzo)
            if not trovato:
                problemi.append(f"riga {indice}: risultato illeggibile ({grezzo!r})")
                continue
            gol_casa, gol_ospite = int(trovato.group(1)), int(trovato.group(2))

        righe.append(RigaFile(
            numero=_numero_da(riga, colonne, "numero") or len(righe) + 1,
            giornata=_numero_da(riga, colonne, "giornata"),
            quando=quando,
            casa=casa,
            ospite=ospite,
            gol_casa=gol_casa,
            gol_ospite=gol_ospite,
            luogo=(riga.get(colonne.get("luogo", ""), "") or "").strip() or None,
        ))
    return righe, problemi


# ---- controlli -------------------------------------------------------------


def _controlla_forma(righe: list[RigaFile], rapporto: Rapporto) -> None:
    """Il file ha la forma di un campionato all'italiana?

    Sono i controlli che un calendario puo' fare su se stesso, senza sapere
    niente di Serie A: con N squadre le partite sono N*(N-1), ognuna ne gioca
    N-1 in casa e N-1 fuori, e ogni accoppiamento compare una volta per verso.
    Se tornano, il file e' completo; se non tornano, e' monco o duplicato — e
    in archivio ci finirebbe una stagione che il modello leggerebbe come vera.
    """
    squadre = {r.casa for r in righe} | {r.ospite for r in righe}
    n = len(squadre)
    attese = n * (n - 1)
    if len(righe) != attese:
        rapporto.avvisi.append(
            f"{len(righe)} partite per {n} squadre: "
            f"un campionato completo ne avrebbe {attese}"
        )

    case: dict[str, int] = {}
    fuori: dict[str, int] = {}
    for riga in righe:
        case[riga.casa] = case.get(riga.casa, 0) + 1
        fuori[riga.ospite] = fuori.get(riga.ospite, 0) + 1
    sbilanciate = sorted(
        s for s in squadre if case.get(s, 0) != n - 1 or fuori.get(s, 0) != n - 1
    )
    if sbilanciate:
        dettaglio = ", ".join(
            f"{s} ({case.get(s, 0)} in casa, {fuori.get(s, 0)} fuori)"
            for s in sbilanciate[:5]
        )
        rapporto.avvisi.append(
            f"{len(sbilanciate)} squadre non hanno {n - 1} partite per parte: {dettaglio}"
            + (" ..." if len(sbilanciate) > 5 else "")
        )

    visti: dict[tuple[str, str], int] = {}
    for riga in righe:
        coppia = (riga.casa, riga.ospite)
        visti[coppia] = visti.get(coppia, 0) + 1
    doppi = sorted(f"{c}-{o}" for (c, o), quante in visti.items() if quante > 1)
    if doppi:
        # Bloccante, non un avviso: due righe per lo stesso accoppiamento sono
        # due risultati diversi per la stessa partita, e non c'e' modo di
        # sapere quale sia quello vero.
        rapporto.problemi.append(
            f"accoppiamenti ripetuti ({len(doppi)}): {', '.join(doppi[:5])}"
            + (" ..." if len(doppi) > 5 else "")
        )

    contro_se_stesse = sorted({r.casa for r in righe if r.casa == r.ospite})
    if contro_se_stesse:
        rapporto.problemi.append(
            f"squadre che giocano contro se stesse: {', '.join(contro_se_stesse)}"
        )


# ---- conversione -----------------------------------------------------------


def _busta(
    riga: RigaFile,
    stagione: int,
    casa_id: int,
    ospite_id: int,
    nomi: dict[int, str],
    league_id: int,
) -> dict[str, Any]:
    """Una partita nella forma in cui l'API l'avrebbe consegnata.

    Il resto del progetto legge JSON grezzo di API-Football — `parse_fixture`,
    `dataset.py`, l'archivio — e non deve imparare un secondo formato per
    questa sorgente. Il costo e' scrivere qui una busta che assomigli a quelle
    vere; il ricavo e' che nessun altro modulo debba sapere che questa stagione
    e' arrivata da un file.

    Il nome della squadra e' quello **canonico dell'API**, non quello del file:
    l'archivio deve dire "AC Milan" come lo dicono tutte le altre stagioni.
    """
    stato = "FT" if riga.giocata else "NS"
    return {
        "fixture": {
            "id": id_sintetico(stagione, riga.numero),
            "date": riga.quando.isoformat(),
            "timestamp": int(riga.quando.timestamp()),
            "status": {
                "short": stato,
                "long": "Match Finished" if riga.giocata else "Not Started",
            },
            "venue": {"id": None, "name": riga.luogo, "city": None},
            # Marchio della provenienza. Non lo legge nessuno: serve a chi
            # aprira' l'archivio fra un anno chiedendosi da dove venga.
            "origine": "file importato",
        },
        "league": {
            "id": league_id,
            "name": "Serie A",
            "country": "Italy",
            "season": stagione,
            "round": f"Regular Season - {riga.giornata}" if riga.giornata else None,
        },
        "teams": {
            "home": {"id": casa_id, "name": nomi.get(casa_id, riga.casa)},
            "away": {"id": ospite_id, "name": nomi.get(ospite_id, riga.ospite)},
        },
        "goals": {"home": riga.gol_casa, "away": riga.gol_ospite},
        # Il primo tempo il file non ce l'ha, e resta ``None``: nessun mercato
        # in uso lo guarda, e inventarlo sarebbe peggio che lasciarlo vuoto.
        "score": {
            "halftime": {"home": None, "away": None},
            "fulltime": {"home": riga.gol_casa, "away": riga.gol_ospite},
        },
    }


def prepara(
    percorso: Path,
    stagione: int,
    nomi_serie_a: dict[int, str],
    nomi_larghi: dict[int, str] | None = None,
    aggiunte: dict[str, int] | None = None,
    league_id: int = SERIE_A_LEAGUE_ID,
) -> tuple[list[dict[str, Any]], Rapporto]:
    """Legge il file e lo converte, senza scrivere niente.

    Gli id delle squadre si cercano in tre posti, dal piu' affidabile al meno:
    le corrispondenze passate a mano, poi le squadre di Serie A che gia'
    conosciamo, infine — solo per chi resta scoperto e solo se il nome e'
    univoco — tutte quelle nominate nei file su disco. Un nome che non si
    aggancia da nessuna parte non viene inventato: finisce fra le sconosciute e
    ferma l'importazione.
    """
    percorso = Path(percorso)
    righe, problemi_lettura = leggi_file(percorso)
    rapporto = Rapporto(file=percorso.name, stagione=stagione, righe=len(righe))
    rapporto.problemi.extend(problemi_lettura)

    if not righe:
        rapporto.problemi.append("il file non contiene nessuna partita leggibile")
        return [], rapporto

    _controlla_forma(righe, rapporto)

    stretto = indice_per_nome(nomi_serie_a)
    largo = indice_per_nome(nomi_larghi or {})
    a_mano = {normalizza(n): i for n, i in (aggiunte or {}).items()}
    nomi_tutti = {**(nomi_larghi or {}), **nomi_serie_a}

    per_nome: dict[str, int] = {}
    sconosciute: list[str] = []
    for nome in sorted({r.casa for r in righe} | {r.ospite for r in righe}):
        chiave = normalizza(nome)
        trovato = a_mano.get(chiave, stretto.get(chiave, largo.get(chiave)))
        if trovato is None:
            sconosciute.append(nome)
        else:
            per_nome[nome] = trovato

    rapporto.squadre = per_nome
    rapporto.sconosciute = sconosciute
    if sconosciute:
        rapporto.problemi.append(
            "squadre di cui non conosco l'id: " + ", ".join(sconosciute)
        )
        return [], rapporto

    buste = [
        _busta(
            riga, stagione, per_nome[riga.casa], per_nome[riga.ospite],
            nomi_tutti, league_id,
        )
        for riga in righe
    ]
    ids = {b["fixture"]["id"] for b in buste}
    if len(ids) != len(buste):
        # Succede se il file numera due partite allo stesso modo: due buste con
        # lo stesso id, e la seconda cancellerebbe la prima in archivio.
        rapporto.problemi.append(
            f"numeri di partita ripetuti: {len(buste)} righe "
            f"ma solo {len(ids)} id distinti"
        )
        return [], rapporto

    rapporto.partite = len(buste)
    rapporto.giocate = sum(1 for r in righe if r.giocata)
    rapporto.da_giocare = rapporto.partite - rapporto.giocate
    return buste, rapporto


def _conserva_sorgente(percorso: Path, store: SeasonStore) -> None:
    """Tiene accanto all'archivio il file da cui e' stato generato.

    E' cio' che rende questa cartella l'unica davvero rigenerabile del
    progetto: senza il file di partenza, "basta reimportare" e' un consiglio
    che vale finche' quel file e' ancora sul computer di qualcuno. Copiarlo
    costa una manciata di kilobyte.

    Non e' un'operazione critica: se fallisce, l'archivio e' comunque scritto e
    valido, e non ha senso perdere un'importazione riuscita per una copia.
    """
    try:
        destinazione = Path(store.root) / f"sorgente{Path(percorso).suffix}"
        destinazione.write_bytes(Path(percorso).read_bytes())
    except OSError as exc:
        logger.warning("non sono riuscito a conservare il file sorgente: %s", exc)


def importa(
    percorso: Path,
    store: SeasonStore,
    stagione: int,
    nomi_serie_a: dict[int, str],
    nomi_larghi: dict[int, str] | None = None,
    aggiunte: dict[str, int] | None = None,
    league_id: int = SERIE_A_LEAGUE_ID,
    anche_se_incompleto: bool = False,
) -> Rapporto:
    """Converte il file e lo scrive in archivio. Non solleva: rende un rapporto.

    Non scrive in due casi: se c'e' un problema — l'archivio sarebbe
    *sbagliato* — e se il file e' incompleto, a meno che non lo si chieda
    esplicitamente. La differenza conta: un archivio sbagliato non e'
    riconoscibile a valle, uno incompleto si', ma sceglierlo deve essere una
    decisione presa e non un effetto collaterale.
    """
    buste, rapporto = prepara(
        percorso, stagione, nomi_serie_a, nomi_larghi, aggiunte, league_id
    )
    if not rapporto.valido or not buste:
        return rapporto
    if not rapporto.completo and not anche_se_incompleto:
        return rapporto

    rapporto.nuove, rapporto.aggiornate = store.upsert_fixtures(buste)
    rapporto.scritto = True
    _conserva_sorgente(percorso, store)
    logger.info(
        "stagione %s importata da %s: %d partite (%d nuove, %d aggiornate)",
        stagione, rapporto.file, rapporto.partite, rapporto.nuove, rapporto.aggiornate,
    )
    return rapporto


# ---- gli archivi importati -------------------------------------------------
#
# Stessa forma di quelli delle stagioni ricomprate (`storico.py`), perche' sono
# la stessa cosa vista dal resto del programma: una stagione su disco che il
# dataset legge. Restano pero' cartelle separate, ed e' voluto: cosi' un
# riempimento vero non si mescola mai con un'importazione, e si puo' cancellare
# l'una senza toccare l'altra.

_NOME_STAGIONE = re.compile(r"^\d{4}$")


def store_stagione(radice: Path, stagione: int) -> SeasonStore:
    """L'archivio di una stagione importata. Lo crea se non c'e'."""
    return SeasonStore(root=Path(radice) / str(stagione))


def stagioni_importate(radice: Path) -> dict[int, SeasonStore]:
    """Le stagioni presenti in `data/importato/`, dalla piu' vecchia.

    Come per le stagioni ricomprate, si guarda il disco e non un file di
    configurazione: una stagione importata e' una cartella che esiste con
    dentro delle partite. Una cartella vuota e' il residuo di un tentativo
    fallito e non conta.
    """
    radice = Path(radice)
    if not radice.exists():
        return {}
    trovate: dict[int, SeasonStore] = {}
    for voce in sorted(radice.iterdir()):
        if not voce.is_dir() or not _NOME_STAGIONE.match(voce.name):
            continue
        if not (voce / "fixtures.json").exists():
            continue
        trovate[int(voce.name)] = SeasonStore(root=voce)
    return trovate


def firma_importato(radice: Path) -> tuple:
    """Impronta delle stagioni importate: cambia quando una viene scritta.

    Serve al web esattamente come `firma_storico`: il modello e' tenuto caldo e
    va ristimato quando compare una stagione nuova, senza aspettare un riavvio.
    """
    radice = Path(radice)
    if not radice.exists():
        return ()
    impronte = []
    for stagione, store in sorted(stagioni_importate(radice).items()):
        stat = (Path(store.root) / "fixtures.json").stat()
        impronte.append((stagione, stat.st_mtime, stat.st_size))
    return tuple(impronte)
