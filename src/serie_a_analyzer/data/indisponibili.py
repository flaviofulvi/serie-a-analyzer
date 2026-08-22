"""Chi non gioca: infortunati, squalificati e giocatori in dubbio.

E' l'informazione che manca al pronostico "secco". Dixon-Coles stima la forza
di una SQUADRA, e la squadra e' la stessa che ci giochi il suo giocatore
migliore o la sua riserva. Sapere chi manca non basta a correggere il
pronostico — per quello serve anche quanto pesa chi manca — ma e' il primo dei
due ingredienti, ed e' quello che si puo' avere subito.

Quattro fatti governano il modulo, e tre sono stati verificati sull'API vera il
22 agosto 2026.

**Si chiedono per partita, non per stagione.** ``/injuries?league=135&season=``
risponde "Free plans do not have access to this season": e' lo stesso filtro
league+season che tiene fuori la stagione in corso dalle fixtures.
``/injuries?fixture=`` invece funziona, come tutti gli endpoint per singola
partita.

**Si recuperano anche dopo.** Una partita del 17 agosto 2024 risponde ancora
oggi con i suoi otto indisponibili. Non sono come le quote, che dopo il
fischio d'inizio non esistono piu': se una raccolta salta, si riprende domani.
Da qui la priorita' `BACKFILL` e il fatto che questo modulo non abbia nessuna
fretta.

**Ma il momento della lettura conta lo stesso**, ed e' il punto delicato. La
lista di una partita ancora da giocare e quella della stessa partita a fine
serata non sono lo stesso dato: la seconda sa chi non e' sceso in campo
davvero. Usare la seconda per pronosticare sarebbe barare, allo stesso modo in
cui e' barare registrare un pronostico a partita iniziata. Per questo di ogni
lettura si salva **quanto mancava al via** (`minuti_al_via`, negativo se la
lettura e' successiva): la stessa regola delle quote, e per lo stesso motivo.
Chi pronostica guarda solo le letture con `minuti_al_via >= 0`; il backtest,
che rigioca il passato, puo' guardare anche le altre — sapendo cosa sta
guardando.

**L'API duplica le righe.** Verificato su Inter-Monza del 22/08/2026: la
risposta contiene 16 record per 8 giocatori, a coppie identiche. Chi sommasse
senza deduplicare conterebbe ogni assenza due volte, e nessun errore lo
segnalerebbe. L'identita' e' l'id del giocatore, mai il nome — la stessa regola
delle squadre e dei giocatori altrove nel progetto.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Quanto prima della partita ha senso leggere gli indisponibili. Piu' larga
# della finestra delle quote: qui non si rischia di perdere il dato, si rischia
# solo di leggerlo quando e' ancora incompleto. Le squalifiche si sanno da
# giorni, gli infortuni si aggiornano fino alla rifinitura.
ORE_DI_RACCOLTA = 48

# I due stati che l'API distingue. Uno e' un fatto, l'altro un dubbio: non
# vanno mai mescolati, perche' solo il primo puo' muovere un pronostico.
ASSENTE = "assente"          # 'Missing Fixture': non ci sara'
IN_DUBBIO = "in_dubbio"      # 'Questionable': forse

_TIPI = {
    "missing fixture": ASSENTE,
    "questionable": IN_DUBBIO,
}

# I tre motivi del progetto, gli stessi di `player_availability` in
# data/schema.sql. Il testo esatto dell'API si conserva comunque: questa e' una
# classificazione per leggere a colpo d'occhio, non una traduzione.
INFORTUNIO = "infortunio"
SQUALIFICA = "squalifica"
ALTRO = "altro"

# Una squalifica si riconosce da poche formule fisse. Tutto cio' che contiene
# "injur" e' un infortunio, dal generico "Injury" a "Thigh Injury". Il resto —
# malattia, motivi personali, nazionale — resta 'altro' invece di essere
# forzato in una delle due caselle piene.
_MOTIVI_SQUALIFICA = ("suspended", "yellow cards", "red card", "suspension")

# Traduzioni dei motivi visti davvero nelle risposte. Un motivo sconosciuto si
# mostra come l'ha scritto l'API: meglio una parola in inglese che una
# categoria inventata.
_TRADUZIONI = {
    "suspended": "squalificato",
    "yellow cards": "squalificato (diffida)",
    "red card": "squalificato (rosso)",
    "injury": "infortunio",
    "muscle injury": "problema muscolare",
    "knee injury": "problema al ginocchio",
    "thigh injury": "problema alla coscia",
    "shin injury": "problema alla tibia",
    "shoulder injury": "problema alla spalla",
    "ankle injury": "problema alla caviglia",
    "back injury": "problema alla schiena",
    "foot injury": "problema al piede",
    "calf injury": "problema al polpaccio",
    "groin injury": "problema all'inguine",
    "hamstring injury": "problema al flessore",
    "illness": "malattia",
    "inactive": "non convocato",
    "personal reasons": "motivi personali",
    "national duty": "impegni con la nazionale",
}


def classifica_motivo(reason: str | None) -> str:
    """Da 'Thigh Injury' a 'infortunio'. Un motivo ignoto resta 'altro'."""
    testo = (reason or "").strip().lower()
    if any(chiave in testo for chiave in _MOTIVI_SQUALIFICA):
        return SQUALIFICA
    if "injur" in testo:
        return INFORTUNIO
    return ALTRO


def traduci_motivo(reason: str | None) -> str:
    """Il motivo in italiano, o com'e' scritto se non lo conosciamo."""
    testo = (reason or "").strip()
    return _TRADUZIONI.get(testo.lower(), testo or "motivo non dichiarato")


@dataclass(frozen=True)
class Indisponibile:
    """Un giocatore che salta una partita, o che rischia di saltarla."""

    fixture_id: int
    player_id: int
    player_name: str
    team_id: int
    team_name: str
    stato: str            # ASSENTE oppure IN_DUBBIO
    motivo: str           # INFORTUNIO / SQUALIFICA / ALTRO
    descrizione: str      # il testo dell'API, non tradotto

    def __post_init__(self) -> None:
        """Stato e motivo si controllano qui, non solo nel database.

        La tabella ha i suoi `check`, ma l'inserimento usa `insert or ignore`
        per non riscrivere una lettura gia' presente — e `or ignore` scarta in
        **silenzio** anche le righe che violano un vincolo. Un valore
        inventato non finirebbe in archivio, il che va bene, ma nessuno se ne
        accorgerebbe, il che non va bene.
        """
        if self.stato not in (ASSENTE, IN_DUBBIO):
            raise ValueError(f"stato non riconosciuto: {self.stato!r}")
        if self.motivo not in (INFORTUNIO, SQUALIFICA, ALTRO):
            raise ValueError(f"motivo non riconosciuto: {self.motivo!r}")

    @property
    def certo(self) -> bool:
        """Solo un'assenza certa puo' muovere un pronostico."""
        return self.stato == ASSENTE

    @property
    def motivo_leggibile(self) -> str:
        return traduci_motivo(self.descrizione)


def parse_indisponibili(risposta: Iterable[dict[str, Any]]) -> list[Indisponibile]:
    """Da ``/injuries?fixture=`` agli indisponibili, senza doppioni.

    La deduplica non e' una precauzione teorica: l'API restituisce davvero le
    stesse righe due volte (verificato il 22/08/2026). La chiave e'
    ``(partita, giocatore)`` — un giocatore ha un solo motivo per non giocare
    una partita, e se ne arrivassero due la prima lettura vince invece di
    sommarsi.
    """
    visti: set[tuple[int, int]] = set()
    letti: list[Indisponibile] = []

    for voce in risposta or []:
        giocatore = voce.get("player") or {}
        squadra = voce.get("team") or {}
        partita = voce.get("fixture") or {}
        try:
            player_id = int(giocatore["id"])
            team_id = int(squadra["id"])
            fixture_id = int(partita["id"])
        except (KeyError, TypeError, ValueError):
            # Senza id non c'e' identita', e un nome non la sostituisce.
            continue

        chiave = (fixture_id, player_id)
        if chiave in visti:
            continue
        visti.add(chiave)

        descrizione = str(giocatore.get("reason") or "").strip()
        letti.append(Indisponibile(
            fixture_id=fixture_id,
            player_id=player_id,
            player_name=str(giocatore.get("name") or "").strip() or f"#{player_id}",
            team_id=team_id,
            team_name=str(squadra.get("name") or "").strip(),
            stato=_TIPI.get(str(giocatore.get("type") or "").strip().lower(), ASSENTE),
            motivo=classifica_motivo(descrizione),
            descrizione=descrizione,
        ))

    return letti


def per_squadra(
    indisponibili: Iterable[Indisponibile], solo_certi: bool = False
) -> dict[int, list[Indisponibile]]:
    """Raggruppa per squadra, che e' come li guarda chi legge una scheda partita."""
    gruppi: dict[int, list[Indisponibile]] = {}
    for i in indisponibili:
        if solo_certi and not i.certo:
            continue
        gruppi.setdefault(i.team_id, []).append(i)
    for voci in gruppi.values():
        # Prima le assenze certe, poi per nome: un ordine stabile fra letture.
        voci.sort(key=lambda i: (not i.certo, i.player_name))
    return gruppi


# ---- raccolta ----------------------------------------------------------------


def _budget_speso(client) -> int | None:
    """Quante richieste il client ha gia' consumato oggi, se sa dirlo.

    I client finti dei test non hanno un budget: in quel caso si torna a
    contare le letture, che per loro sono la stessa cosa.
    """
    budget = getattr(client, "budget", None)
    try:
        return int(budget.used)
    except (AttributeError, TypeError, ValueError):
        return None


def partite_da_controllare(
    fixtures: Iterable[Any],
    now: datetime | None = None,
    entro_ore: int = ORE_DI_RACCOLTA,
) -> list[Any]:
    """Le partite abbastanza vicine da avere una lista di indisponibili sensata.

    Dalla piu' imminente, come per le quote: se il budget finisce a meta', a
    restare scoperta e' la partita che ha piu' tempo davanti per essere ripresa
    alla prossima esecuzione.
    """
    adesso = now or datetime.now(timezone.utc)
    limite = adesso + timedelta(hours=entro_ore)
    imminenti = [f for f in fixtures if adesso < f.kickoff_utc <= limite]
    return sorted(imminenti, key=lambda f: f.kickoff_utc)


def raccogli_indisponibili(
    client,
    registro,
    fixtures: Iterable[Any],
    now: datetime | None = None,
    entro_ore: int = ORE_DI_RACCOLTA,
    max_richieste: int | None = None,
) -> dict[str, int]:
    """Legge e archivia gli indisponibili delle partite imminenti.

    Non solleva mai: la raccolta giornaliera ha da salvare cose che si perdono
    per sempre, e questa non e' una di quelle. Se fallisce, si riprende domani.
    """
    adesso = now or datetime.now(timezone.utc)
    esito = {"partite": 0, "indisponibili": 0, "senza_lista": 0,
             "letture": 0, "richieste": 0}
    speso_prima = _budget_speso(client)

    for f in partite_da_controllare(fixtures, now=adesso, entro_ore=entro_ore):
        if max_richieste is not None and esito["letture"] >= max_richieste:
            logger.info("indisponibili: raggiunto il tetto di %d letture", max_richieste)
            break
        try:
            risposta = client.injuries_fixture(f.api_id)
        except Exception as exc:
            logger.warning("indisponibili non letti per la partita %s: %s", f.api_id, exc)
            continue
        esito["letture"] += 1

        letti = parse_indisponibili(risposta)
        if not letti:
            # Succede, ed e' una buona notizia: nessuno indisponibile. Ma
            # succede anche quando l'API non ha ancora la lista, e le due cose
            # da fuori non si distinguono: per questo non si scrive niente.
            esito["senza_lista"] += 1
            continue

        scritti = registro.salva_indisponibili(
            fixture_id=f.api_id, kickoff_utc=f.kickoff_utc, voci=letti, now=adesso
        )
        esito["partite"] += 1
        esito["indisponibili"] += scritti

    # Letture e richieste non coincidono: una risposta ancora in cache non
    # tocca il budget. Dire "richieste spese" contando le letture gonfierebbe
    # il costo di questo passo proprio nel riepilogo che serve a giudicarlo.
    speso_dopo = _budget_speso(client)
    if speso_prima is not None and speso_dopo is not None:
        esito["richieste"] = max(0, speso_dopo - speso_prima)
    else:
        esito["richieste"] = esito["letture"]

    return esito
