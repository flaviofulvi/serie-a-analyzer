"""Il confronto fra cio' che era stato pronosticato e cio' che e' successo.

E' la meta' che mancava. Uno storico di pronostici senza esiti dice solo che
il sistema ha parlato, non se aveva ragione: qui ogni esito registrato prima
della partita viene messo accanto al risultato vero e trasformato in due
numeri, `hit` e il contributo al Brier score.

**Perche' il Brier e non la percentuale di azzeccati.** In Serie A predire
sempre "1" azzecca gia' il 45% delle volte, e un pronostico dato al 51% che si
avvera non e' piu' bravo di uno dato al 90% che si avvera: lo e' molto meno. Il
Brier — `(probabilita' - esito)^2` — misura quanto la fiducia dichiarata era
meritata, che e' l'unica domanda sensata da fare a un pronostico
probabilistico. Accanto sta la **baseline**: il punteggio che avrebbe ottenuto
chi dicesse sempre la frequenza media di quell'esito, senza guardare le
squadre. Un mercato che non batte la propria baseline non sta aggiungendo
niente, e va detto invece che nascosto.

Un esito e' un fatto osservato, non una previsione: a differenza dei
pronostici puo' essere scritto dopo. Si scrive pero' **una volta sola**
(`insert or ignore`): ripassare sulle stesse partite non ricalcola nulla e non
costa niente. Il caso limite noto e' la partita decisa a tavolino giorni dopo:
il risultato cambia e l'esito registrato resta quello del campo. E' un evento
raro e visibile; correggerlo automaticamente aprirebbe la porta a riscritture
dello storico, che e' esattamente cio' che questo progetto non vuole.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from math import log
from typing import Any, Callable, Iterable

from .predictions import PredictionStore

logger = logging.getLogger(__name__)

# Sotto questa soglia di pronostici risolti, ogni metrica e' rumore: due
# partite fortunate bastano a far sembrare eccellente un mercato inutile.
SOGLIA_SIGNIFICATIVITA = 30


class EsitoNonRisolvibile(ValueError):
    """Non si sa dire se questa selezione si e' verificata.

    Un mercato che si sa salvare ma non si sa risolvere e' peggio di un mercato
    assente: resta nello storico e non entra mai nel conto dell'accuratezza.
    """


# ---- si e' verificato? ------------------------------------------------------
#
# Un risolutore per ogni mercato prodotto da `predictions.righe_mercati`. Il
# test `test_ogni_mercato_salvato_sa_risolversi` verifica che la copertura sia
# completa: aggiungere un mercato senza il suo risolutore fa fallire la suite.


def _risolvi_1x2(selection: str, casa: int, ospite: int) -> bool:
    if selection == "home":
        return casa > ospite
    if selection == "draw":
        return casa == ospite
    if selection == "away":
        return casa < ospite
    raise EsitoNonRisolvibile(f"1x2: selezione sconosciuta {selection!r}")


def _risolvi_doppia_chance(selection: str, casa: int, ospite: int) -> bool:
    if selection == "home_or_draw":
        return casa >= ospite
    if selection == "away_or_draw":
        return casa <= ospite
    if selection == "home_or_away":
        return casa != ospite
    raise EsitoNonRisolvibile(f"doppia_chance: selezione sconosciuta {selection!r}")


def _risolvi_over_under(selection: str, casa: int, ospite: int) -> bool:
    verso, _, linea = selection.partition("_")
    try:
        soglia = float(linea)
    except ValueError as exc:
        raise EsitoNonRisolvibile(f"over_under: linea illeggibile in {selection!r}") from exc
    totale = casa + ospite
    if verso == "over":
        return totale > soglia
    if verso == "under":
        return totale < soglia
    raise EsitoNonRisolvibile(f"over_under: verso sconosciuto in {selection!r}")


def _risolvi_btts(selection: str, casa: int, ospite: int) -> bool:
    entrambe = casa > 0 and ospite > 0
    if selection == "yes":
        return entrambe
    if selection == "no":
        return not entrambe
    raise EsitoNonRisolvibile(f"btts: selezione sconosciuta {selection!r}")


def _risolvi_clean_sheet(selection: str, casa: int, ospite: int) -> bool:
    # Clean sheet della squadra di casa = l'ospite non ha segnato.
    if selection == "home":
        return ospite == 0
    if selection == "away":
        return casa == 0
    raise EsitoNonRisolvibile(f"clean_sheet: selezione sconosciuta {selection!r}")


def _risolvi_risultato_esatto(selection: str, casa: int, ospite: int) -> bool:
    try:
        gc, go = (int(parte) for parte in selection.split("-"))
    except ValueError as exc:
        raise EsitoNonRisolvibile(f"risultato_esatto: {selection!r} non e' un punteggio") from exc
    return (casa, ospite) == (gc, go)


RISOLUTORI: dict[str, Callable[[str, int, int], bool]] = {
    "1x2": _risolvi_1x2,
    "doppia_chance": _risolvi_doppia_chance,
    "over_under": _risolvi_over_under,
    "btts": _risolvi_btts,
    "clean_sheet": _risolvi_clean_sheet,
    "risultato_esatto": _risolvi_risultato_esatto,
}


def esito_verificato(market: str, selection: str, gol_casa: int, gol_ospite: int) -> bool:
    """L'esito pronosticato si e' verificato, dato il risultato finale?"""
    risolutore = RISOLUTORI.get(market)
    if risolutore is None:
        raise EsitoNonRisolvibile(f"mercato senza risolutore: {market!r}")
    return risolutore(selection, gol_casa, gol_ospite)


def brier(probabilita: float, verificato: bool) -> float:
    """Il contributo al Brier score di un singolo esito: (p - reale)^2."""
    return (probabilita - (1.0 if verificato else 0.0)) ** 2


# ---- risoluzione -------------------------------------------------------------


def risolvi_partita(
    store: PredictionStore,
    fixture_id: int,
    gol_casa: int,
    gol_ospite: int,
    now: datetime | None = None,
) -> int:
    """Risolve tutti i mercati ancora aperti di una partita. Restituisce quanti."""
    da_risolvere = store.mercati_da_risolvere(fixture_id)
    if not da_risolvere:
        return 0

    righe = []
    for m in da_risolvere:
        try:
            verificato = esito_verificato(m["market"], m["selection"], gol_casa, gol_ospite)
        except EsitoNonRisolvibile as exc:
            # Non e' un dettaglio da ignorare: quel mercato non entrera' mai
            # nel conto dell'accuratezza, e nessuno se ne accorgerebbe.
            logger.warning("esito non risolvibile (partita %s): %s", fixture_id, exc)
            continue
        righe.append((m["id"], verificato, brier(m["probability"], verificato)))

    return store.salva_esiti(righe, now=now)


def risolvi_pronostici(
    store: PredictionStore,
    fixtures: Iterable[Any],
    now: datetime | None = None,
) -> dict[str, int]:
    """Risolve tutto cio' che si puo' risolvere, date le partite conosciute.

    Parte dalle partite che *hanno* un pronostico in sospeso, non da tutte
    quelle esistenti: lo storico e' piccolo, il dataset no.
    """
    in_attesa = set(store.partite_da_risolvere())
    if not in_attesa:
        return {"partite": 0, "esiti": 0, "ancora_in_attesa": 0}

    per_id = {
        f.api_id: f for f in fixtures
        if f.api_id in in_attesa and f.is_finished
    }

    partite = esiti = 0
    for fixture_id, f in per_id.items():
        scritti = risolvi_partita(store, fixture_id, f.home_goals, f.away_goals, now=now)
        if scritti:
            partite += 1
            esiti += scritti

    return {
        "partite": partite,
        "esiti": esiti,
        # Partite pronosticate che non sono ancora finite (o di cui non
        # conosciamo ancora il risultato): il conto normale, non un problema.
        "ancora_in_attesa": len(in_attesa) - len(per_id),
    }


# ---- quanto valgono i pronostici ---------------------------------------------


def _baseline(tasso: float) -> float:
    """Il Brier di chi ripete sempre la frequenza media di quell'esito.

    E' il muro da superare: se conoscere le squadre non fa meglio di questo,
    il mercato non sta aggiungendo informazione sulla singola partita.
    """
    return tasso * (1.0 - tasso)


def _metriche(righe: list[dict]) -> dict[str, float]:
    """Brier, log loss, frequenza reale e fiducia media di un gruppo di esiti."""
    n = len(righe)
    tasso = sum(1 for r in righe if r["hit"]) / n
    return {
        "n": n,
        "brier": sum(r["brier"] for r in righe) / n,
        "baseline": _baseline(tasso),
        "log_loss": -sum(
            log(max(r["probability"] if r["hit"] else 1.0 - r["probability"], 1e-12))
            for r in righe
        ) / n,
        "tasso_reale": tasso,
        "probabilita_media": sum(r["probability"] for r in righe) / n,
    }


def _raggruppa(righe: list[dict], chiave) -> dict[Any, list[dict]]:
    gruppi: dict[Any, list[dict]] = {}
    for riga in righe:
        gruppi.setdefault(chiave(riga), []).append(riga)
    return gruppi


def accuratezza_per_selezione(store: PredictionStore) -> list[dict]:
    """Come sta andando ogni singolo esito: Over 2.5 separato da Over 4.5.

    E' il livello a cui la baseline ha davvero senso, ed e' dove si vede quale
    parte di un mercato funziona e quale no.
    """
    risultato = []
    for (mercato, selezione), righe in _raggruppa(
        store.esiti_risolti(), lambda r: (r["market"], r["selection"])
    ).items():
        voce = {"mercato": mercato, "selezione": selezione, **_metriche(righe)}
        voce["significativo"] = voce["n"] >= SOGLIA_SIGNIFICATIVITA
        voce["batte_baseline"] = voce["brier"] < voce["baseline"]
        risultato.append(voce)
    return sorted(risultato, key=lambda v: (v["mercato"], v["selezione"]))


def accuratezza_per_mercato(store: PredictionStore) -> list[dict]:
    """Come sta andando ogni mercato, dal piu' misurato al meno.

    Il Brier si media direttamente sugli esiti, la baseline **no**: va
    calcolata per singola selezione e poi mediata. Su Over/Under la differenza
    e' tutta: la linea 0.5 si avvera quasi sempre e la 4.5 quasi mai, ma
    messe nello stesso mucchio si compensano fino a una frequenza del 50%, che
    darebbe al modello un muro molto piu' basso di quello vero. Una baseline
    troppo generosa e' peggio di nessuna baseline: fa sembrare informativo un
    mercato che non lo e'.

    ``significativo`` dice se ci sono abbastanza esiti perche' il numero
    significhi qualcosa: mostrarlo senza questa avvertenza sarebbe il modo piu'
    semplice di mentire con dati veri.
    """
    risultato = []
    for mercato, righe in _raggruppa(store.esiti_risolti(), lambda r: r["market"]).items():
        voce = {"mercato": mercato, **_metriche(righe)}
        voce["baseline"] = sum(
            _baseline(sum(1 for r in gruppo if r["hit"]) / len(gruppo)) * len(gruppo)
            for gruppo in _raggruppa(righe, lambda r: r["selection"]).values()
        ) / len(righe)
        voce["significativo"] = voce["n"] >= SOGLIA_SIGNIFICATIVITA
        voce["batte_baseline"] = voce["brier"] < voce["baseline"]
        risultato.append(voce)

    return sorted(risultato, key=lambda v: -v["n"])


def brier_1x2_multiclasse(store: PredictionStore) -> dict | None:
    """Il Brier dell'1X2 nella forma confrontabile col backtest.

    Il backtest somma i tre esiti di ogni partita (0 perfetto, 0.667 tirando a
    caso); l'aggregato per selezione fa la media sui tre. Sono lo stesso numero
    diviso tre, ma metterli accanto senza dirlo confonderebbe: qui si calcola
    per partita, come nel backtest, cosi' i due valori si possono guardare
    insieme.
    """
    per_pronostico: dict[int, float] = {}
    for riga in store.esiti_risolti(market="1x2"):
        per_pronostico[riga["prediction_id"]] = (
            per_pronostico.get(riga["prediction_id"], 0.0) + riga["brier"]
        )
    if not per_pronostico:
        return None
    return {
        "n_partite": len(per_pronostico),
        "brier": sum(per_pronostico.values()) / len(per_pronostico),
        # Riferimento fisso: 1/3 a testa, cioe' non sapere nulla.
        "baseline_caso": 2 / 3,
    }


def calibrazione(
    store: PredictionStore,
    market: str = "1x2",
    selection: str | None = None,
    n_bin: int = 5,
) -> list[dict]:
    """Quando il modello dice 30%, succede il 30% delle volte?

    E' la metrica che conta di piu' per un'applicazione di pronostici: si puo'
    avere un buon Brier ed essere sistematicamente troppo sicuri di se'.
    """
    bins: dict[int, list[dict]] = {}
    for riga in store.esiti_risolti(market=market, selection=selection):
        idx = min(int(riga["probability"] * n_bin), n_bin - 1)
        bins.setdefault(idx, []).append(riga)

    righe = []
    for idx in sorted(bins):
        gruppo = bins[idx]
        righe.append({
            "fascia": f"{idx / n_bin:.0%}-{(idx + 1) / n_bin:.0%}",
            "n": len(gruppo),
            "previsto": sum(r["probability"] for r in gruppo) / len(gruppo),
            "reale": sum(1 for r in gruppo if r["hit"]) / len(gruppo),
        })
    return righe


def adesso() -> datetime:
    return datetime.now(timezone.utc)
