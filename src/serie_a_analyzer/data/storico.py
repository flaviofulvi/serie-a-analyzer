"""Riempimento di una stagione passata, quando il piano lo consente.

Il piano gratuito di API-Football da' 100 richieste al giorno e **solo le
stagioni 2022-2024**. Tutto il resto del progetto e' costruito attorno a questo
vincolo, ed e' il motivo per cui fra lo storico e l'inizio della raccolta
giornaliera resta un buco: una stagione intera che nessuno ha mai visto.

Un piano a pagamento toglie quel muro. Questo modulo esiste per il giorno in
cui viene comprato: chiede all'API se la stagione e' diventata leggibile e, se
lo e', la ricompra tutta — partite prima, dettagli poi.

Tre cose lo governano.

**Si verifica prima di spendere.** Una stagione non coperta non da' errore:
l'API risponde 200 con una lista vuota, che e' indistinguibile da "quel
campionato non ha giocato". Per questo si guarda `/status`, l'unico endpoint
che dice a chiare lettere che piano e' attivo e quante richieste concede.

**Il risultato e' un archivio, non una cache.** Le partite riempite finiscono
in `data/storico/<anno>/`, con la stessa forma di `data/serie_a/`: JSON grezzo,
un `SeasonStore`. Se un domani l'abbonamento scade, quella stagione resta
leggibile — mentre la copia in cache ha un TTL e vive in una cartella che
chiunque considera buttabile, cioe' e' una garanzia piu' debole di quanto
valga il denaro speso per riempirla.

**Si riprende da dove si era fermato.** Un campionato sono ~380 partite e ogni
partita costa tre richieste: piu' di mille in tutto. Il riempimento non e'
un'operazione atomica ma una coda che si consuma finche' c'e' budget, e che al
giro dopo salta cio' che ha gia'. Le partite si scaricano tutte in una
richiesta sola e per prime: sono l'ossatura, i dettagli sono il ripieno.

Cio' che **non** si recupera, per quanto si paghi: le quote dei bookmaker di
partite gia' giocate (`odds.py` spiega perche') e i pronostici che nessuno ha
congelato prima del fischio d'inizio. Una stagione riempita rende il modello
piu' informato, non il suo storico di accuratezza piu' lungo.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .budget import BudgetExhausted, Priority
from .cache import TTL_FOREVER, TTL_UPCOMING_FIXTURES
from .client import SERIE_A_LEAGUE_ID, ApiFootballClient, ApiFootballError
from .daily import DETAIL_KINDS, collect_details_for
from .ingest import current_season
from .parsers import parse_status
from .store import SeasonStore

logger = logging.getLogger(__name__)

# Secondi di attesa fra una richiesta e l'altra durante un riempimento.
# I piani a pagamento hanno un limite **al minuto** oltre a quello giornaliero
# (il piu' piccolo concede 300/min): mille richieste sparate di fila lo
# supererebbero e l'API risponderebbe 429 mentre il contatore locale continua
# a segnare richieste spese. Un quarto di secondo tiene il ritmo a ~240 al
# minuto, sotto il piu' basso dei limiti a pagamento.
PAUSA_PREDEFINITA = 0.25

# Nome della cartella di una stagione riempita: solo cifre, cosi' un file
# qualsiasi finito li' dentro non viene scambiato per un archivio.
_NOME_STAGIONE = re.compile(r"^\d{4}$")


@dataclass
class StatoPiano:
    """Cosa dice l'API di noi: piano, scadenza, richieste concesse e spese."""

    piano: str | None = None
    attivo: bool = False
    scadenza: str | None = None
    limite_giornaliero: int | None = None
    usate_oggi: int | None = None
    errore: str | None = None

    @property
    def a_pagamento(self) -> bool:
        """Il piano concede piu' delle 100 richieste del gratuito.

        Si guarda il limite e non il nome: i nomi dei piani cambiano, il
        numero e' il fatto.
        """
        return bool(self.limite_giornaliero and self.limite_giornaliero > 100)


@dataclass
class RapportoStagione:
    """Esito del riempimento di una stagione. Sempre restituito, mai sollevato."""

    stagione: int
    disponibile: bool = False
    partite: int = 0
    concluse: int = 0
    nuove: int = 0
    aggiornate: int = 0
    dettagli: dict[str, int] = field(default_factory=dict)
    in_sospeso: int = 0
    richieste: int = 0
    errore: str | None = None

    @property
    def completa(self) -> bool:
        return self.disponibile and self.in_sospeso == 0


def leggi_piano(client: ApiFootballClient) -> StatoPiano:
    """Interroga `/status`. Una richiesta, e non solleva mai.

    E' la prima cosa da fare dopo aver cambiato piano: se qui il limite
    giornaliero e' ancora 100, la sottoscrizione non e' attiva e ogni altra
    richiesta sarebbe sprecata.
    """
    try:
        risposta = client.status()
    except (ApiFootballError, BudgetExhausted) as exc:
        return StatoPiano(errore=str(exc))

    sottoscrizione = risposta.get("subscription") or {}
    richieste = risposta.get("requests") or {}
    return StatoPiano(
        piano=sottoscrizione.get("plan"),
        attivo=bool(sottoscrizione.get("active")),
        scadenza=sottoscrizione.get("end"),
        limite_giornaliero=richieste.get("limit_day"),
        usate_oggi=richieste.get("current"),
    )


# ---- gli archivi delle stagioni riempite -----------------------------------


def store_stagione(radice: Path, stagione: int) -> SeasonStore:
    """L'archivio di una stagione riempita. Lo crea se non c'e'."""
    return SeasonStore(root=Path(radice) / str(stagione))


def stagioni_riempite(radice: Path) -> dict[int, SeasonStore]:
    """Le stagioni gia' presenti in `data/storico/`, dalla piu' vecchia.

    Si guarda il disco e non un file di configurazione: una stagione riempita
    e' una cartella che esiste, e questo la rende vera anche dopo un riavvio,
    un cambio di macchina o un'installazione da capo.

    Una cartella senza partite dentro non conta: sarebbe il residuo di un
    tentativo su una stagione che il piano non copriva.
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


def firma_storico(radice: Path) -> tuple:
    """Impronta di tutte le stagioni riempite: cambia quando una cresce.

    Serve al web, che tiene il modello caldo e deve ristimarlo quando arriva
    una stagione nuova — senza aspettare un riavvio, perche' il riempimento
    dura ore e nel frattempo il sito e' aperto.
    """
    radice = Path(radice)
    if not radice.exists():
        return ()
    impronte = []
    for stagione, store in sorted(stagioni_riempite(radice).items()):
        stat = (Path(store.root) / "fixtures.json").stat()
        impronte.append((stagione, stat.st_mtime, stat.st_size))
    return tuple(impronte)


# ---- il riempimento vero ---------------------------------------------------


def _concluse(store: SeasonStore) -> list[dict]:
    """Le partite concluse dell'archivio, dalla piu' recente.

    L'ordine non e' estetico: se il budget finisce a meta', si vuole aver preso
    prima i dettagli delle partite piu' vicine a oggi, che sono quelle che
    pesano di piu' nel modello e che l'utente guarda.
    """
    concluse = [
        raw for raw in store.load_fixtures().values()
        if parse_status((raw.get("fixture") or {}).get("status")) == "finished"
    ]
    concluse.sort(key=lambda raw: (raw.get("fixture") or {}).get("date") or "", reverse=True)
    return concluse


def dettagli_in_sospeso(store: SeasonStore) -> int:
    """Quante richieste di dettaglio mancano ancora a questa stagione.

    E' il numero da mostrare a chi ha interrotto un riempimento: dice se
    rilanciarlo costa dieci richieste o mille.
    """
    return sum(
        1
        for raw in _concluse(store)
        for kind in DETAIL_KINDS
        if store.needs_detail((raw.get("fixture") or {}).get("id"), kind)
    )


def scarica_partite(
    client: ApiFootballClient,
    stagione: int,
    league: int = SERIE_A_LEAGUE_ID,
) -> tuple[list[dict], str | None]:
    """Tutte le partite di una stagione in una richiesta sola. Non scrive nulla.

    Restituisce ``(partite, errore)``: una lista vuota senza errore significa
    che il piano non copre la stagione, ed e' una risposta legittima da
    riportare all'utente, non un guasto.

    La copia in cache di una stagione conclusa non scade mai — i suoi risultati
    non cambieranno piu' — ma quella in corso si', altrimenti il calendario
    resterebbe congelato al giorno del riempimento.
    """
    conclusa = stagione < current_season()
    try:
        risposta = client.fixtures(
            season=stagione,
            league=league,
            ttl=TTL_FOREVER if conclusa else TTL_UPCOMING_FIXTURES,
        )
    except (ApiFootballError, BudgetExhausted) as exc:
        return [], str(exc)

    # Difensivo: la richiesta filtra gia' per lega, ma l'archivio di una
    # stagione deve contenere solo Serie A anche se un giorno la query
    # cambiasse.
    return [raw for raw in risposta if (raw.get("league") or {}).get("id") == league], None


def riempi_stagione(
    client: ApiFootballClient,
    store: SeasonStore,
    stagione: int,
    league: int = SERIE_A_LEAGUE_ID,
    con_dettagli: bool = True,
    max_richieste: int | None = None,
    pausa: float = PAUSA_PREDEFINITA,
) -> RapportoStagione:
    """Ricompra una stagione intera: prima le partite, poi i dettagli.

    L'ordine e' lo stesso della raccolta giornaliera e per la stessa ragione:
    le partite sono l'ossatura da cui tutto il resto si puo' ricostruire dopo,
    i dettagli sono ripieno che si recupera da un id in qualsiasi momento. Se
    il budget basta per una cosa sola, deve bastare per le partite.

    I dettagli si chiedono tutti a priorita' BACKFILL, formazioni comprese: qui
    sono lavoro arretrato di un anno fa, e la riserva CRITICAL protegge le
    formazioni ufficiali della giornata di *oggi*.

    Non solleva: restituisce un rapporto che dice fin dove e' arrivato.
    """
    prima = client.budget.used
    rapporto = RapportoStagione(stagione=stagione)

    partite, errore = scarica_partite(client, stagione, league)
    if errore:
        rapporto.errore = errore
        rapporto.richieste = client.budget.used - prima
        return rapporto
    if not partite:
        rapporto.errore = (
            f"la stagione {stagione} non e' coperta dal piano: l'API ha risposto "
            "senza partite. Verifica con `/status` che la sottoscrizione sia attiva."
        )
        rapporto.richieste = client.budget.used - prima
        return rapporto

    rapporto.disponibile = True
    # `upsert_fixtures` distingue da solo cio' che e' nuovo da cio' che c'era
    # gia': un secondo giro sulla stessa stagione dice "zero nuove" invece di
    # far credere di aver ricomprato tutto.
    rapporto.nuove, rapporto.aggiornate = store.upsert_fixtures(partite)
    rapporto.partite = len(store.load_fixtures())
    concluse = _concluse(store)
    rapporto.concluse = len(concluse)

    if con_dettagli:
        rapporto.dettagli = collect_details_for(
            client, store, concluse,
            max_requests=max_richieste,
            priorita=Priority.BACKFILL,
            pausa=pausa,
        )

    rapporto.in_sospeso = dettagli_in_sospeso(store)
    rapporto.richieste = client.budget.used - prima
    logger.info(
        "stagione %s: %d partite (%d concluse), dettagli %s, in sospeso %d, %d richieste",
        stagione, rapporto.partite, rapporto.concluse, rapporto.dettagli,
        rapporto.in_sospeso, rapporto.richieste,
    )
    return rapporto


def stima_richieste(n_concluse: int) -> int:
    """Quante richieste costa una stagione da riempire da zero.

    Una per l'elenco delle partite, tre per ogni partita conclusa. Serve a dire
    all'utente il prezzo prima di iniziare, non dopo.
    """
    return 1 + n_concluse * len(DETAIL_KINDS)
