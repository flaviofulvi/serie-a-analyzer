"""Le quote dei bookmaker: il metro di paragone che dice quanto vale il modello.

Battere "chi tira a caso" non e' un risultato. Il confronto serio e' con il
mercato: le quote incorporano infortuni, formazioni, motivazioni e il denaro
di chi ci scommette sopra, e sono notoriamente difficili da battere. Finche'
non le si guarda, si sa di battere l'ignoranza — non il mercato.

Tre fatti pratici governano questo modulo.

**Le quote non si recuperano.** API-Football le pubblica nei giorni che
precedono la partita e non le archivia: `/odds?fixture=...` su una gara del
maggio scorso risponde vuoto. Sono quindi come gli id delle partite — o si
prendono prima, o sono perse per sempre. Da qui la raccolta a ridosso del
fischio d'inizio e il fatto che il confronto col mercato si costruira' solo in
avanti, senza scorciatoie sullo storico.

**La "chiusura" non e' un campo, e' un momento.** Le quote di chiusura sono le
piu' accurate perche' hanno assorbito tutte le notizie. L'API non le marca:
qui si salva ogni lettura con l'ora, e la chiusura e' semplicemente *l'ultima
lettura prima del calcio d'inizio*. Cosi' la definizione resta vera anche se
una raccolta salta.

**Una quota non e' una probabilita'.** 1/quota somma a piu' di 1: la
differenza e' il margine del banco (overround). Qui si normalizza in
proporzione dentro ogni gruppo esaustivo — Home/Draw/Away, Over/Under della
stessa linea, Si'/No. E' il metodo standard e ha un limite noto: tende a
gonfiare gli esiti improbabili, dove il margine e' concentrato. Va bene per
un confronto onesto fra Brier; sarebbe da raffinare (Shin, power) solo se un
giorno si volesse scommetterci davvero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Quanto prima del fischio d'inizio ha senso chiedere le quote. API-Football
# le pubblica entro pochi giorni dalla partita: chiedere prima spreca
# richieste, chiedere dopo non serve piu' a niente.
ORE_DI_RACCOLTA = 48

# Sotto questa soglia di bookmaker una quota di consenso non e' un consenso.
MINIMO_BOOKMAKER = 3


@dataclass(frozen=True)
class QuotaLetta:
    """Una quota, gia' tradotta nel vocabolario del registro dei pronostici."""

    bookmaker: str
    market: str          # '1x2', 'over_under', 'btts', 'clean_sheet'
    selection: str       # le stesse selezioni salvate nei pronostici
    decimal_odds: float
    probabilita: float   # implicita, ripulita dal margine del banco


# Traduzione dei mercati di API-Football in quelli del registro.
#
# La chiave e' che le selezioni combacino **esattamente** con quelle scritte da
# `predictions.righe_mercati`: se divergessero, il confronto col mercato non
# aggancerebbe nulla e la tabella resterebbe misteriosamente vuota. Un test
# verifica la corrispondenza sui mercati che entrambi conoscono.
_1X2 = {"Home": "home", "Draw": "draw", "Away": "away"}
_SI_NO = {"Yes": "yes", "No": "no"}


def _selezione_over_under(valore: str) -> str | None:
    """'Over 2.5' -> 'over_2.5'. Ignora tutto cio' che non e' una linea di gol."""
    parti = valore.split()
    if len(parti) != 2:
        return None
    verso, linea = parti[0].lower(), parti[1]
    if verso not in ("over", "under"):
        return None
    try:
        float(linea)
    except ValueError:
        return None
    return f"{verso}_{linea}"


def _gruppi_da_bet(nome: str, valori: list[dict]) -> list[tuple[str, dict[str, float]]]:
    """Da un mercato dell'API ai gruppi esaustivi ``(mercato, {selezione: quota})``.

    Un "gruppo esaustivo" e' un insieme di esiti che copre tutti i casi
    possibili e non si sovrappone: e' l'unita' su cui il margine del banco si
    puo' togliere. Over/Under ne produce uno per linea, non uno solo — sommare
    tutte le linee darebbe un overround senza significato.

    Le selezioni non usate dal registro (il "No" del clean sheet) restano nel
    gruppo: servono a normalizzare, e vengono scartate dopo.
    """
    quote: dict[str, float] = {}
    for v in valori:
        try:
            quota = float(v["odd"])
        except (KeyError, TypeError, ValueError):
            continue
        if quota > 1.0:
            quote[str(v.get("value", ""))] = quota

    if nome == "Match Winner":
        return [("1x2", {_1X2[k]: q for k, q in quote.items() if k in _1X2})]

    if nome == "Both Teams Score":
        return [("btts", {_SI_NO[k]: q for k, q in quote.items() if k in _SI_NO})]

    if nome in ("Clean Sheet - Home", "Clean Sheet - Away"):
        lato = "home" if nome.endswith("Home") else "away"
        # Il "no" non ha una controparte nel registro, ma serve a normalizzare:
        # gli si da' un nome che non collide con nulla.
        tradotte = {}
        if "Yes" in quote:
            tradotte[lato] = quote["Yes"]
        if "No" in quote:
            tradotte[f"non_{lato}"] = quote["No"]
        return [("clean_sheet", tradotte)]

    if nome == "Goals Over/Under":
        per_linea: dict[str, dict[str, float]] = {}
        for valore, quota in quote.items():
            selezione = _selezione_over_under(valore)
            if selezione is None:
                continue
            linea = selezione.split("_", 1)[1]
            per_linea.setdefault(linea, {})[selezione] = quota
        return [("over_under", gruppo) for gruppo in per_linea.values()]

    return []


def _normalizza(quote: dict[str, float]) -> dict[str, float]:
    """Da quote decimali a probabilita' che sommano a 1.

    La somma delle inverse eccede 1: quell'eccesso e' il margine del banco, e
    si toglie in proporzione. Un gruppo incompleto viene scartato: normalizzare
    su meta' degli esiti darebbe probabilita' plausibili e sbagliate.
    """
    if len(quote) < 2:
        return {}
    inverse = {sel: 1.0 / q for sel, q in quote.items()}
    totale = sum(inverse.values())
    if totale <= 0:
        return {}
    return {sel: p / totale for sel, p in inverse.items()}


def parse_quote(elemento: dict[str, Any]) -> list[QuotaLetta]:
    """Da un elemento di ``/odds`` alle quote di tutti i bookmaker."""
    lette: list[QuotaLetta] = []
    for bookmaker in elemento.get("bookmakers") or []:
        nome_bk = str(bookmaker.get("name") or "").strip()
        if not nome_bk:
            continue
        for bet in bookmaker.get("bets") or []:
            for mercato, gruppo in _gruppi_da_bet(str(bet.get("name") or ""), bet.get("values") or []):
                probabilita = _normalizza(gruppo)
                for selezione, quota in gruppo.items():
                    # Le selezioni di servizio (il "no" del clean sheet) hanno
                    # fatto il loro lavoro nella normalizzazione e si fermano qui.
                    if selezione.startswith("non_"):
                        continue
                    if selezione not in probabilita:
                        continue
                    lette.append(QuotaLetta(
                        bookmaker=nome_bk,
                        market=mercato,
                        selection=selezione,
                        decimal_odds=quota,
                        probabilita=probabilita[selezione],
                    ))
    return lette


def consenso(quote: Iterable[QuotaLetta], minimo: int = MINIMO_BOOKMAKER) -> dict[tuple[str, str], float]:
    """La probabilita' su cui il mercato converge, per esito.

    Mediana e non media: un bookmaker con una quota fuori linea — capita, per
    errore o per posizione — sposterebbe la media e non sposta la mediana.
    Sotto ``minimo`` bookmaker non si parla di consenso e l'esito viene
    lasciato fuori.

    Le mediane prese esito per esito non sommano esattamente a 1 (sulle
    risposte vere lo scarto e' dell'ordine dello 0.1%). Rinormalizzarle
    sarebbe possibile, ma per confrontare due Brier lo scarto e' irrilevante e
    la mediana grezza resta il numero che i bookmaker hanno davvero detto.
    """
    per_esito: dict[tuple[str, str], list[float]] = {}
    for q in quote:
        per_esito.setdefault((q.market, q.selection), []).append(q.probabilita)
    return {
        chiave: median(valori)
        for chiave, valori in per_esito.items()
        if len(valori) >= minimo
    }


def consenso_per_partita(
    righe: Iterable[dict], minimo: int = MINIMO_BOOKMAKER
) -> dict[tuple[int, str, str], float]:
    """Come `consenso`, ma su piu' partite: chiave ``(partita, mercato, esito)``.

    Lavora sulle righe cosi' come escono dall'archivio, che e' la forma in cui
    servono al confronto col modello.
    """
    per_esito: dict[tuple[int, str, str], list[float]] = {}
    for r in righe:
        chiave = (int(r["fixture_id"]), r["market"], r["selection"])
        per_esito.setdefault(chiave, []).append(float(r["probability"]))
    return {
        chiave: median(valori)
        for chiave, valori in per_esito.items()
        if len(valori) >= minimo
    }


# ---- raccolta ----------------------------------------------------------------


def partite_da_quotare(
    fixtures: Iterable[Any],
    now: datetime | None = None,
    entro_ore: int = ORE_DI_RACCOLTA,
) -> list[Any]:
    """Le partite abbastanza vicine da avere quote, e non ancora iniziate.

    Dalla piu' imminente: se il budget finisce a meta', a restare senza quote
    e' la partita che ha piu' tempo davanti per essere ripresa alla prossima
    esecuzione.
    """
    adesso = now or datetime.now(timezone.utc)
    limite = adesso + timedelta(hours=entro_ore)
    imminenti = [f for f in fixtures if adesso < f.kickoff_utc <= limite]
    return sorted(imminenti, key=lambda f: f.kickoff_utc)


def raccogli_quote(
    client,
    registro,
    fixtures: Iterable[Any],
    now: datetime | None = None,
    entro_ore: int = ORE_DI_RACCOLTA,
    max_richieste: int | None = None,
) -> dict[str, int]:
    """Legge e archivia le quote delle partite imminenti.

    Non solleva: una quota mancante e' un peccato, una raccolta giornaliera
    interrotta e' un danno.
    """
    adesso = now or datetime.now(timezone.utc)
    esito = {"partite": 0, "quote": 0, "senza_quote": 0, "richieste": 0}

    for f in partite_da_quotare(fixtures, now=adesso, entro_ore=entro_ore):
        if max_richieste is not None and esito["richieste"] >= max_richieste:
            logger.info("quote: raggiunto il tetto di %d richieste", max_richieste)
            break
        try:
            risposta = client.odds(f.api_id)
        except Exception as exc:
            logger.warning("quote non lette per la partita %s: %s", f.api_id, exc)
            continue
        esito["richieste"] += 1

        lette = [q for elemento in risposta for q in parse_quote(elemento)]
        if not lette:
            # Normale piu' di quanto sembri: non tutte le partite hanno quote,
            # e i bookmaker le pubblicano quando vogliono.
            esito["senza_quote"] += 1
            continue

        scritte = registro.salva_quote(
            fixture_id=f.api_id, kickoff_utc=f.kickoff_utc, quote=lette, now=adesso
        )
        esito["partite"] += 1
        esito["quote"] += scritte

    return esito
