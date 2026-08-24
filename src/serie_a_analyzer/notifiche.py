"""Messaggi su Telegram: cosa sta facendo il sistema, quando vale la pena dirlo.

Quattro regole danno forma a questo modulo, e sono tutte difensive.

* **Il silenzio e' il default.** Senza token in `.env` il sistema funziona
  identico e non manda niente. Una notifica e' un di piu', mai un requisito.
* **Non solleva mai.** Telegram irraggiungibile, token sbagliato, rete giu':
  la raccolta deve continuare. Vale la stessa regola di `registra_partite` —
  cio' che non si e' potuto mandare si conta e si mette a log, non si propaga.
  Perdere un messaggio e' un fastidio, perdere le quote di una partita e'
  definitivo.
* **Si decide dal CONTENUTO, non dall'orologio.** Nessun messaggio e' legato a
  un orario: si guarda cosa c'e' sul disco e cosa non e' ancora stato detto.
  E' cio' che fa arrivare il resoconto delle 23:45 la mattina dopo, se il
  computer era spento — senza che nessuno debba prevedere quel caso.
* **Niente si manda due volte.** Un registro degli invii tiene il conto. E'
  l'altra faccia della regola gia' scritta in `run_formazioni.ps1` e in
  `web/views.py`: un avviso che c'e' sempre non lo legge piu' nessuno, e un
  bot che ripete lo stesso messaggio e' un log con le notifiche accese.

Da qui la scelta di cosa mandare: i messaggi che si vogliono leggere — i
pronostici della giornata, com'e' andata, e i tabellini di una giornata
arrivati tutti — e gli allarmi che si devono leggere, ognuno dei quali segnala
qualcosa di rotto. Le novanta esecuzioni al giorno che non fanno niente
restano mute: dirle sarebbe insegnare a ignorare le altre.

Sui tabellini i messaggi sono due e dicono il contrario l'uno dell'altro:
`tabellini_pronti` quando sono arrivati tutti e quelli veri, `tabellini_fermi`
quando il sistema ha smesso di poter rimediare. Una giornata prende uno dei
due, mai entrambi.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

API_TELEGRAM = "https://api.telegram.org"

# Telegram rifiuta i messaggi oltre i 4096 caratteri. Si tronca prima, con un
# avviso: meglio un messaggio dichiaratamente incompleto di uno mai arrivato.
MAX_CARATTERI = 4000

DEFAULT_TIMEOUT = 10.0

# Sotto questa soglia di richieste rimaste si avvisa. Non e' "budget finito":
# e' "sta per finire", cioe' il momento in cui si puo' ancora decidere di
# rimandare un riempimento storico invece di scoprire il buco a cose fatte.
SOGLIA_BUDGET = 15

# Da quante ore senza una raccolta riuscita si considera saltato un giro. Le
# corse fisse sono due, alle 08:00 e alle 23:45: sedici ore coprono la piu'
# lunga delle due distanze senza gridare al lupo per un ritardo di un'ora.
ORE_SENZA_RACCOLTA = 16

# Quanto indietro guardare per l'allarme sulle quote perse. Tre giorni e' la
# finestra del piano gratuito: piu' indietro non ha senso perche' quelle
# partite non sono comunque piu' recuperabili, e ricordarle ogni giorno
# trasformerebbe un allarme in un rimprovero quotidiano.
_GIORNI_ALLARME_QUOTE = timedelta(days=3)

# Quanto indietro guardare per annunciare i tabellini arrivati. Piu' lunga
# della finestra delle quote perche' un tabellino puo' consolidarsi giorni
# dopo la partita, e piu' corta di "sempre" per due ragioni: oltre una
# settimana non e' piu' una novita', e senza un limite la prima esecuzione
# dopo aver acceso questa notifica manderebbe un messaggio per ogni giornata
# gia' archiviata.
_GIORNI_TABELLINI_PRONTI = timedelta(days=7)


# ---- il canale --------------------------------------------------------------


@dataclass
class Telegram:
    """Il canale verso una chat. Non solleva mai."""

    token: str | None = None
    chat_id: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    # Acceso da `.env`, serve nei primi giorni per vedere che la macchina
    # funziona davvero. Chi lo lascia acceso per sempre si condanna a ignorare
    # anche gli allarmi.
    verboso: bool = False
    inviati: list[str] = field(default_factory=list, repr=False)

    @property
    def attivo(self) -> bool:
        return bool(self.token and self.chat_id)

    def manda(self, testo: str) -> bool:
        """Manda un messaggio. Dice se ci e' riuscito, e non solleva.

        Il valore di ritorno serve a chi tiene il registro degli invii: un
        messaggio non partito non deve risultare "gia' mandato", altrimenti il
        resoconto perso in un momento di rete assente non arriverebbe mai piu'.
        """
        if not self.attivo:
            logger.debug("telegram non configurato: messaggio non mandato")
            return False

        corpo = testo
        if len(corpo) > MAX_CARATTERI:
            corpo = corpo[:MAX_CARATTERI] + "\n\n[...messaggio troncato]"

        try:
            risposta = httpx.post(
                f"{API_TELEGRAM}/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": corpo,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("telegram non raggiungibile: %s", exc)
            return False

        if risposta.status_code >= 400:
            logger.warning(
                "telegram ha risposto %s: %s", risposta.status_code, risposta.text[:200]
            )
            return False

        self.inviati.append(corpo)
        return True

    def manda_se_verboso(self, testo: str) -> bool:
        """Per il rumore di fondo: parte solo con la modalita' verbosa accesa."""
        return self.manda(testo) if self.verboso else False


# ---- il registro degli invii ------------------------------------------------


@dataclass
class RegistroInvii:
    """Cosa e' gia' stato detto, cosi' non lo si ripete.

    Un file JSON e non una tabella del registro dei pronostici: e' un dettaglio
    operativo del canale, non un dato del dominio. Si puo' cancellare senza
    perdere niente di serio — al massimo si riceve un messaggio due volte, che
    e' il danno piu' piccolo che questo modulo sappia fare.
    """

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _carica(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            dati = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Un registro illeggibile non deve bloccare niente: al massimo si
            # ripete un messaggio.
            logger.warning("registro degli invii illeggibile: riparto da zero")
            return {}
        return dati if isinstance(dati, dict) else {}

    def gia_inviato(self, chiave: str) -> bool:
        return chiave in self._carica()

    def segna(self, chiave: str, quando: datetime | None = None) -> None:
        dati = self._carica()
        dati[chiave] = (quando or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        try:
            self.path.write_text(
                json.dumps(dati, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("registro degli invii non scritto: %s", exc)


# ---- cosa c'e' da dire ------------------------------------------------------


def _giorno(partita: Any) -> date:
    return partita.kickoff_utc.date()


def giorno_da_pronosticare(
    partite: Iterable[Any], registro: RegistroInvii, adesso: datetime | None = None
) -> date | None:
    """Il giorno di cui mandare i pronostici, se non sono gia' stati mandati.

    Serve almeno una partita ancora da giocare: a giornata cominciata un
    pronostico non e' piu' un pronostico, ed e' la stessa regola per cui il
    registro rifiuta una riga scritta dopo il fischio d'inizio.
    """
    ora = adesso or datetime.now(timezone.utc)
    oggi = ora.date()
    da_giocare = [f for f in partite if _giorno(f) == oggi and f.kickoff_utc > ora]
    if not da_giocare:
        return None
    return None if registro.gia_inviato(f"pronostici:{oggi}") else oggi


def giorno_da_riepilogare(
    partite: Iterable[Any], registro: RegistroInvii, adesso: datetime | None = None
) -> date | None:
    """L'ultimo giorno finito di cui non si e' ancora raccontato com'e' andata.

    Non "oggi": il giorno piu' recente che abbia tutte le partite concluse e
    nessun resoconto mandato. E' cosi' che il riepilogo delle 23:45 arriva la
    mattina dopo se il computer era spento, senza che quel caso sia previsto
    da nessuna parte.

    "Tutte le partite" vuol dire **tutte quelle in calendario quel giorno**, e
    non solo quelle gia' cominciate. La differenza sembra teorica e non lo e':
    il 24 agosto 2026 alle 20:32 il resoconto della domenica parti' con la sola
    Bologna-Lazio, perche' Roma-Fiorentina — via alle 20:45 — non entrava
    nemmeno nel conto, e un giorno fatto di sole partite finite risulta finito.
    Il danno non e' il messaggio in anticipo ma quello che non arriva piu': la
    chiave `resoconto:2026-08-24` era segnata, e il posticipo non sarebbe stato
    raccontato mai. Una partita che deve ancora cominciare e' esattamente come
    una in corso — il giorno non e' finito.
    """
    ora = adesso or datetime.now(timezone.utc)
    per_giorno: dict[date, list[Any]] = {}
    for f in partite:
        if _giorno(f) <= ora.date():
            per_giorno.setdefault(_giorno(f), []).append(f)

    for giorno in sorted(per_giorno, reverse=True):
        if not all(getattr(f, "is_finished", False) for f in per_giorno[giorno]):
            continue
        if registro.gia_inviato(f"resoconto:{giorno}"):
            # Il piu' recente e' gia' stato raccontato: i precedenti pure, e
            # tornare indietro manderebbe resoconti archeologici.
            return None
        return giorno
    return None


def giorni_coi_tabellini_pronti(
    partite: Iterable[Any],
    pronti: Iterable[int],
    registro: RegistroInvii,
    adesso: datetime | None = None,
) -> list[date]:
    """I giorni conclusi i cui tabellini sono arrivati **tutti**, e non detti.

    Dal piu' vecchio al piu' recente, cosi' i messaggi arrivano in ordine.

    Qui NON si usa la regola di `giorno_da_riepilogare` — "il piu' recente, e
    se e' gia' stato detto fermati" — e la differenza e' tutta nel dato. Un
    risultato arriva quando la partita finisce, quindi i giorni si completano
    in ordine e guardare solo l'ultimo basta. Un tabellino no: il 22 agosto
    2026 due partite del sabato avevano ancora la fotografia del feed live
    mentre quelle della domenica erano gia' a posto. Fermandosi al piu'
    recente, la domenica verrebbe annunciata e il sabato — che e' proprio
    quello che si stava aspettando — non lo sarebbe **mai piu'**.

    Il prezzo di guardarli tutti sarebbe un messaggio per ogni giornata della
    stagione la prima volta che si accende questa notifica. Da qui la
    finestra: oltre una settimana un tabellino non e' piu' una novita', e chi
    aspettava ha gia' smesso di aspettare.

    Un giorno con un tabellino ancora provvisorio non e' pronto e non compare.
    Un giorno i cui tabellini si sono arresi per sempre (`tabellini_arresi`)
    non comparira' mai, ed e' giusto: quel caso ha gia' il suo allarme, che
    dice l'esatto contrario di questo messaggio.

    Il giorno si guarda **intero**: tutte le partite in calendario, non solo
    quelle gia' finite. Guardare le altre significava non guardarle affatto —
    il 24 agosto 2026 alle 20:48, con Roma-Fiorentina al primo minuto, il
    giorno risultava fatto della sola Bologna-Lazio e quindi completo. Il
    messaggio pero' elenca tutte le partite della data, e annunciava come
    definitivo un tabellino che non esisteva: chi decide e chi scrive devono
    guardare lo stesso insieme, altrimenti la bugia e' garantita. Una partita
    non ancora conclusa vale come tabellino che manca, che e' la verita'.
    """
    ora = adesso or datetime.now(timezone.utc)
    sono_pronti = set(pronti)
    limite = ora.date() - _GIORNI_TABELLINI_PRONTI

    per_giorno: dict[date, list[Any]] = {}
    for f in partite:
        if limite <= _giorno(f) <= ora.date():
            per_giorno.setdefault(_giorno(f), []).append(f)

    return [
        giorno for giorno in sorted(per_giorno)
        if all(
            getattr(f, "is_finished", False) and f.api_id in sono_pronti
            for f in per_giorno[giorno]
        )
        and not registro.gia_inviato(f"tabellini-pronti:{giorno}")
    ]


def partite_senza_quote(partite: Iterable[Any], quotate: Iterable[int]) -> list[Any]:
    """Partite gia' cominciate di cui non abbiamo nemmeno una lettura di quote.

    E' l'unico allarme del sistema su un danno DEFINITIVO: le quote non si
    recuperano, e senza di esse quella partita non entrera' mai nel confronto
    col mercato — il paragone che conta piu' di tutti.
    """
    con_quote = set(quotate)
    return [f for f in partite if f.api_id not in con_quote]


# ---- i messaggi -------------------------------------------------------------
#
# Sono funzioni pure che restituiscono testo: si provano senza rete, e chi le
# legge vede subito cosa arrivera' sul telefono. Il formato e' HTML perche'
# Telegram lo accetta e non ha le trappole di escape del Markdown.


@dataclass(frozen=True)
class RigaPronostico:
    """Una partita in programma, come la racconta il messaggio del mattino."""

    ora: str
    casa: str
    ospite: str
    prob_1: float | None = None
    prob_x: float | None = None
    prob_2: float | None = None
    gol_casa: float | None = None
    gol_ospite: float | None = None
    risultato_probabile: str | None = None
    # Perche' il modello non pronostica questa partita, quando non lo fa.
    motivo: str | None = None

    @property
    def ha_pronostico(self) -> bool:
        return self.prob_1 is not None


@dataclass(frozen=True)
class RigaEsito:
    """Una partita finita, col pronostico che era stato congelato prima."""

    casa: str
    ospite: str
    gol_casa: int
    gol_ospite: int
    prob_1: float | None = None
    prob_x: float | None = None
    prob_2: float | None = None

    @property
    def esito_reale(self) -> str:
        if self.gol_casa > self.gol_ospite:
            return "1"
        return "2" if self.gol_casa < self.gol_ospite else "X"

    @property
    def probabilita(self) -> dict[str, float] | None:
        if self.prob_1 is None:
            return None
        return {"1": self.prob_1, "X": self.prob_x or 0.0, "2": self.prob_2 or 0.0}

    @property
    def prob_del_vero(self) -> float | None:
        """Quanta probabilita' il modello dava a cio' che e' successo."""
        p = self.probabilita
        return None if p is None else p[self.esito_reale]

    @property
    def brier(self) -> float | None:
        """Brier multiclasse di questa partita: 0 perfetto, 2 il contrario."""
        p = self.probabilita
        if p is None:
            return None
        vero = self.esito_reale
        return sum((v - (1.0 if k == vero else 0.0)) ** 2 for k, v in p.items())


def _pct(valore: float | None) -> str:
    return "--" if valore is None else f"{valore:.0%}"


def messaggio_pronostici(giorno: date, righe: list[RigaPronostico]) -> str:
    """I pronostici della giornata, quelli secchi del mattino.

    Dichiara cosa NON sa: a quest'ora non ci sono ne' formazioni ne' quote
    fresche, e chiamarli "i pronostici" senza aggiungere altro darebbe a un
    numero prudente l'aria di un numero informato.
    """
    testa = [f"<b>Serie A — {giorno.strftime('%d/%m/%Y')}</b>", ""]
    if not righe:
        return "\n".join(testa + ["Oggi non si gioca."])

    corpo: list[str] = []
    for r in righe:
        corpo.append(f"<b>{r.ora}  {r.casa} - {r.ospite}</b>")
        if not r.ha_pronostico:
            corpo.append(f"   {r.motivo or 'nessun pronostico: storia insufficiente'}")
            corpo.append("")
            continue
        corpo.append(f"   1 {_pct(r.prob_1)}   X {_pct(r.prob_x)}   2 {_pct(r.prob_2)}")
        dettagli = []
        if r.gol_casa is not None and r.gol_ospite is not None:
            dettagli.append(f"gol attesi {r.gol_casa:.2f}-{r.gol_ospite:.2f}")
        if r.risultato_probabile:
            dettagli.append(f"piu' probabile {r.risultato_probabile}")
        if dettagli:
            corpo.append("   " + "   ".join(dettagli))
        corpo.append("")

    coda = [
        "<i>Pronostico secco: a quest'ora non esistono ne' le formazioni ne' le",
        "quote di chiusura. Su Over/Under e Gol/NoGol il modello dichiara quasi",
        "sempre la media di campionato, perche' e' quanto sa davvero.</i>",
    ]
    return "\n".join(testa + corpo + coda)


def messaggio_resoconto(giorno: date, righe: list[RigaEsito]) -> str:
    """Com'e' andata. Il Brier davanti, gli azzeccati dietro, ed e' voluto.

    "Percentuale di pronostici azzeccati" e' la misura che tutti guardano e che
    non dice niente: predire sempre "1" in Serie A ne azzecca gia' il 45% senza
    sapere nulla di calcio. Metterla per prima, anche solo in un messaggio,
    insegnerebbe a leggere la cosa sbagliata.
    """
    testa = [f"<b>Com'e' andata — {giorno.strftime('%d/%m/%Y')}</b>", ""]
    if not righe:
        return "\n".join(testa + ["Nessuna partita conclusa."])

    corpo = []
    for r in righe:
        dava = "" if r.prob_del_vero is None else f"  (dava {_pct(r.prob_del_vero)})"
        corpo.append(f"{r.casa} - {r.ospite}  <b>{r.gol_casa}-{r.gol_ospite}</b>{dava}")

    misurati = [r for r in righe if r.brier is not None]
    coda = [""]
    if misurati:
        medio = sum(r.brier for r in misurati) / len(misurati)
        azzeccati = sum(
            1 for r in misurati
            if max(r.probabilita, key=r.probabilita.get) == r.esito_reale
        )
        coda.append(f"<b>Brier 1X2: {medio:.3f}</b> su {len(misurati)} partite")
        coda.append("<i>Piu' basso e' meglio: chi tira a caso sta a 0.667, il")
        coda.append("modello nel backtest a 0.59. Un giorno solo non e' una")
        coda.append("misura, e' un aneddoto.</i>")
        coda.append("")
        coda.append(f"Esito piu' probabile azzeccato: {azzeccati}/{len(misurati)}")
    else:
        coda.append("<i>Nessun pronostico era stato congelato prima del via.</i>")
    return "\n".join(testa + corpo + coda)


def messaggio_formazioni(
    riga: RigaPronostico, secco: RigaPronostico | None = None
) -> str:
    """Il pronostico rifatto quando escono le formazioni, mezz'ora prima.

    Mostra il PRIMA e il DOPO, non il solo numero nuovo. Da solo, un "1 41%"
    non si distingue da un pronostico qualsiasi; accanto al 45% di due ore
    prima dice l'unica cosa che questo lavoro ha da dire, cioe' quanto pesa
    chi scende in campo davvero. E i due numeri esistono entrambi nel
    registro, che tiene le due modalita' separate apposta.

    Parte solo quando `data/ufficiali.py` ha davvero scritto una riga, cioe'
    quando i pesi dei giocatori esistono e spostano qualcosa. Finche' i
    tabellini sul disco non bastano questo messaggio non arriva mai — ed e' il
    comportamento giusto, non un guasto da sistemare.
    """
    righe = [
        f"<b>Formazioni ufficiali — {riga.casa} - {riga.ospite}</b>",
        f"Si comincia alle {riga.ora}.",
        "",
        f"1 {_pct(riga.prob_1)}   X {_pct(riga.prob_x)}   2 {_pct(riga.prob_2)}",
    ]
    if secco is not None and secco.ha_pronostico:
        righe.append(
            f"<i>prima delle formazioni: "
            f"1 {_pct(secco.prob_1)}   X {_pct(secco.prob_x)}   2 {_pct(secco.prob_2)}</i>"
        )
    if riga.gol_casa is not None and riga.gol_ospite is not None:
        righe.append(f"gol attesi {riga.gol_casa:.2f}-{riga.gol_ospite:.2f}")
    righe += [
        "",
        "<i>Questo aggiustamento pesa chi ATTACCA: se manca un difensore il",
        "modello non se ne accorge. I due pronostici restano separati apposta:",
        "fra qualche mese il confronto fra i Brier dira' se serviva.</i>",
    ]
    return "\n".join(righe)


# ---- i cinque allarmi -------------------------------------------------------
#
# Ognuno segnala qualcosa di rotto, e il primo segnala un danno che non si
# ripara. Nessuno deve poter diventare routine: se uno arrivasse ogni giorno
# non sarebbe piu' un allarme, e la risposta giusta sarebbe aggiustare la
# causa, non silenziare il messaggio.


def allarme_quote_perse(partite: list[Any]) -> str:
    """Il piu' grave: sono cominciate senza che avessimo una sola quota."""
    elenco = "\n".join(f"  {p.casa} - {p.ospite}" for p in partite)
    return "\n".join([
        "<b>ALLARME — quote perse per sempre</b>",
        "",
        f"{len(partite)} partite sono cominciate senza che ne avessimo nemmeno",
        "una lettura di quote:",
        "",
        elenco,
        "",
        "<i>API-Football non le archivia: non si recuperano a nessun prezzo.",
        "Quelle partite non entreranno mai nel confronto col mercato, che e' il",
        "paragone piu' importante che il sistema sappia fare. Il computer era",
        "spento nei 40 minuti prima del via?</i>",
    ])


def allarme_raccolta_fallita(giorni: list[str], errore: str) -> str:
    return "\n".join([
        "<b>ALLARME — raccolta fallita</b>",
        "",
        f"Giorni non raccolti: {', '.join(giorni)}",
        f"Errore: {errore}",
        "",
        "<i>La finestra del piano gratuito e' di tre giorni: se un giorno non",
        "viene raccolto entro domani, le sue partite sono perse per sempre.</i>",
    ])


def allarme_raccolta_ferma(ultima: datetime | None, ore: float) -> str:
    quando = ultima.strftime("%d/%m alle %H:%M") if ultima else "mai"
    return "\n".join([
        "<b>ALLARME — la raccolta e' ferma</b>",
        "",
        f"Ultima raccolta riuscita: {quando} ({ore:.0f} ore fa).",
        "",
        "<i>Il computer e' rimasto spento, o l'attivita' pianificata non parte",
        "piu'. Finche' si resta dentro i tre giorni non si e' perso niente.</i>",
    ])


def allarme_budget(rimaste: int, limite: int) -> str:
    return "\n".join([
        "<b>ALLARME — budget quasi esaurito</b>",
        "",
        f"Restano {rimaste} richieste su {limite}.",
        "",
        "<i>Le riserve proteggono le formazioni ufficiali, ma quote e",
        "indisponibili delle prossime partite rischiano di saltare. Se e' in",
        "corso un riempimento storico, conviene fermarlo.</i>",
    ])


def allarme_formazioni_accese(pronte: int, totale: int) -> str:
    """L'unico allarme che porta una buona notizia, ed e' per questo che c'e'.

    La macchina delle formazioni ufficiali si accende da sola quando i
    tabellini bastano, mesi dopo essere stata scritta. Senza un messaggio, lo
    si scoprirebbe per caso guardando una pagina.
    """
    return "\n".join([
        "<b>Si e' accesa la macchina delle formazioni ufficiali</b>",
        "",
        f"{pronte} squadre su {totale} hanno abbastanza tabellini per pesare",
        "chi scende in campo.",
        "",
        "<i>Da adesso ogni partita avra' DUE pronostici: quello secco e quello",
        "con le formazioni. Fra qualche mese il confronto fra i due Brier dira'",
        "se il lavoro serviva. Da fare ora: calibrare FATTORE_PREDEFINITO col",
        "backtest, che finora e' scelto a occhio.</i>",
    ])


def messaggio_tabellini_pronti(giorno: date, partite: list[Any]) -> str:
    """I tabellini di una giornata sono arrivati tutti, e quelli veri.

    Non e' un allarme: e' il secondo messaggio del sistema che porta una buona
    notizia, insieme a quello delle formazioni accese. Ha senso mandarlo
    perche' e' il momento in cui quelle partite **cominciano a servire** —
    prima il risultato c'era gia', ma minuti, gol, assist e voti no, e sono
    quelli che alimentano le schede giocatore e il peso di chi scende in campo.

    Dice anche quante volte l'attesa e' stata piu' lunga del solito, perche' e'
    proprio la domanda che ci si fa dopo aver ricevuto un allarme sui tabellini
    fermi: quelli di ieri, alla fine, sono arrivati?
    """
    righe = [
        f"<b>Tabellini completi — {giorno.strftime('%d/%m/%Y')}</b>",
        "",
        f"Tutte e {len(partite)} le partite hanno il tabellino definitivo:",
        "minuti, gol, assist, cartellini e voti.",
        "",
    ]
    for f in sorted(partite, key=lambda f: f.kickoff_utc):
        righe.append(f"  {f.home_team_name} - {f.away_team_name}")
    righe += [
        "",
        "<i>Da adesso quelle partite entrano nelle schede giocatore e nel peso",
        "di chi scende in campo, e contano fra quelle che accendono i",
        "pronostici con le formazioni ufficiali.</i>",
    ]
    return "\n".join(righe)


def allarme_tabellini_fermi(partite: list[Any]) -> str:
    """Un tabellino sbagliato che nessuno chiedera' piu'.

    Arriva tardi apposta. Nelle ore dopo una partita l'API puo' servire la
    fotografia del feed live invece del tabellino, ed e' normale: la raccolta
    lo richiede da sola finche' non si consolida. Questo messaggio parte solo
    quando i tentativi sono finiti — cioe' quando il sistema ha smesso di poter
    rimediare e quel dato resta sbagliato per sempre.

    Non e' un guasto della raccolta e va detto, altrimenti si va a cercare un
    errore dove non c'e'.
    """
    righe = [
        "<b>Tabellini rimasti a meta' partita</b>",
        "",
        "Per queste partite l'API ha continuato a servire la lettura del feed",
        "live invece del tabellino finale, e i tentativi sono finiti:",
        "",
    ]
    for f in partite:
        righe.append(
            f"  {_giorno(f)}  {f.home_team_name}-{f.away_team_name} (id {f.api_id})"
        )
    righe += [
        "",
        "<i>Non e' un guasto della raccolta: le partite ci sono e i risultati",
        "sono giusti. Mancano minuti, gol e voti dei singoli, quindi quelle",
        "partite restano fuori dalle schede giocatore e dal peso delle",
        "formazioni. Per farle richiedere ancora si cancella la busta",
        "players.json di quella partita da data/serie_a/details/.</i>",
    ]
    return "\n".join(righe)


# ---- il giro completo -------------------------------------------------------


def invia_quel_che_serve(
    telegram: Telegram,
    registro: RegistroInvii,
    partite: list[Any],
    pronostici,
    quotate: Iterable[int] | None = None,
    esiti_congelati=None,
    budget: Any | None = None,
    ultima_raccolta: datetime | None = None,
    giorni_falliti: list[str] | None = None,
    errore_raccolta: str | None = None,
    tabellini_fermi: list[Any] | None = None,
    tabellini_pronti: Iterable[int] | None = None,
    adesso: datetime | None = None,
) -> list[str]:
    """Manda tutto cio' che oggi non e' ancora stato detto, e dice cosa ha mandato.

    Il modello non entra qui dentro: ``pronostici`` ed ``esiti_congelati`` sono
    funzioni che il chiamante passa. E' quello che tiene questo modulo puro e
    provabile senza rete ne' stima — ma soprattutto e' quello che permette di
    decidere PRIMA se c'e' qualcosa da dire, e di non stimare un modello per
    scoprire che no. La raccolta al momento giusto passa di qui ogni quarto
    d'ora: pagare due secondi di stima ogni volta, per novanta volte al giorno,
    sarebbe un cattivo affare.

    Args:
        telegram: il canale. Se non e' attivo qui non succede niente.
        registro: cosa e' gia' stato detto.
        partite: tutte le partite in archivio (``FixtureRecord``).
        pronostici: ``(partite_del_giorno) -> list[RigaPronostico]``, chiamata
            solo se c'e' davvero un giorno da pronosticare.
        quotate: id delle partite di cui esiste almeno una lettura di quote.
        esiti_congelati: ``(partite_del_giorno) -> list[RigaEsito]``, chiamata
            solo se c'e' un giorno da riepilogare.
        budget: il contatore, per l'allarme sulle richieste rimaste.
        ultima_raccolta: quando e' andata a buon fine l'ultima volta.
        giorni_falliti: i giorni che la raccolta appena finita non ha preso.
        errore_raccolta: il primo errore, da mettere nel messaggio.
        tabellini_fermi: le partite il cui tabellino e' sbagliato e non verra'
            piu' richiesto (`tabellini_arresi` in data/store.py). Gia' pronte:
            qui non si guarda nessun archivio.
        tabellini_pronti: gli id delle partite il cui tabellino c'e' ed e'
            quello finale (`tabellini_pronti` in data/store.py). Stessa regola:
            la domanda sull'archivio l'ha gia' fatta il chiamante.
        adesso: istante di riferimento, per i test.

    Returns:
        Le chiavi dei messaggi effettivamente partiti.
    """
    if not telegram.attivo:
        return []

    ora = adesso or datetime.now(timezone.utc)
    oggi = ora.date()
    mandati: list[str] = []

    def prova(chiave: str, testo: str) -> None:
        # Si segna solo cio' che e' partito davvero: un resoconto perso perche'
        # la rete era giu' deve poter arrivare al giro dopo.
        if registro.gia_inviato(chiave):
            return
        if telegram.manda(testo):
            registro.segna(chiave, ora)
            mandati.append(chiave)

    # --- gli allarmi per primi, che sono la ragione per cui il bot esiste ---
    if giorni_falliti:
        prova(
            f"raccolta-fallita:{oggi}",
            allarme_raccolta_fallita(giorni_falliti, errore_raccolta or "sconosciuto"),
        )

    if quotate is not None:
        cominciate = [
            f for f in partite
            if f.kickoff_utc <= ora and _giorno(f) >= oggi - _GIORNI_ALLARME_QUOTE
        ]
        perse = partite_senza_quote(cominciate, quotate)
        if perse:
            prova(f"quote-perse:{oggi}", allarme_quote_perse(perse))

    if ultima_raccolta is not None:
        ore = (ora - ultima_raccolta).total_seconds() / 3600
        if ore >= ORE_SENZA_RACCOLTA:
            prova(f"raccolta-ferma:{oggi}", allarme_raccolta_ferma(ultima_raccolta, ore))

    if budget is not None and budget.remaining <= SOGLIA_BUDGET:
        prova(f"budget:{oggi}", allarme_budget(budget.remaining, budget.daily_limit))

    if tabellini_fermi:
        # La chiave porta gli id e non la data: e' un fatto che riguarda quelle
        # partite, non quel giorno. Cosi' non si ripete ogni giorno per le
        # stesse, e una partita nuova che si arrende domani viene detta.
        ids = "-".join(str(f.api_id) for f in sorted(tabellini_fermi, key=lambda f: f.api_id))
        prova(f"tabellini-fermi:{ids}", allarme_tabellini_fermi(tabellini_fermi))

    # --- i due messaggi che si vogliono leggere -----------------------------
    giorno = giorno_da_pronosticare(partite, registro, ora)
    if giorno is not None:
        del_giorno = sorted(
            (f for f in partite if _giorno(f) == giorno), key=lambda f: f.kickoff_utc
        )
        prova(f"pronostici:{giorno}", messaggio_pronostici(giorno, pronostici(del_giorno)))

    if tabellini_pronti is not None:
        for giorno in giorni_coi_tabellini_pronti(
            partite, tabellini_pronti, registro, ora
        ):
            prova(
                f"tabellini-pronti:{giorno}",
                messaggio_tabellini_pronti(
                    giorno, [f for f in partite if _giorno(f) == giorno]
                ),
            )

    if esiti_congelati is not None:
        giorno = giorno_da_riepilogare(partite, registro, ora)
        if giorno is not None:
            del_giorno = sorted(
                (f for f in partite if _giorno(f) == giorno), key=lambda f: f.kickoff_utc
            )
            prova(
                f"resoconto:{giorno}",
                messaggio_resoconto(giorno, esiti_congelati(del_giorno)),
            )

    return mandati
