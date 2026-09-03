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

**Una partita, un pronostico.** Ogni misura di questo modulo parte da
`esiti_risolti`, che tiene una riga sola per partita — l'ultima scritta prima
del fischio d'inizio, cioe' quella che le pagine mostrano. Il registro invece
le conserva tutte, una per versione del modello, e fa bene: avvicinandosi alla
partita il modello cambia idea e quel cambiamento e' leggibile. Ma sono
tentativi sulla stessa domanda, non osservazioni diverse, e mediarli insieme
conta quella partita tante volte quante sono le ristime che l'hanno preceduta.
Il danno non e' la media un po' storta: e' che gonfiava `n`, e su `n` poggiava
`SOGLIA_SIGNIFICATIVITA`, cioe' la decisione se un numero meriti un verdetto o
un "troppi pochi dati". Il perche' per esteso sta in `esiti_risolti`.

La soglia oggi non poggia piu' su `n` ma su **`partite`**, ed e' l'altra meta'
dello stesso ragionamento: una partita si conta una volta anche quando dieci
mercati la guardano. Le due correzioni sono gemelle — la prima toglie il
doppio conteggio nel tempo (piu' ristime della stessa gara), la seconda quello
nello spazio (piu' selezioni della stessa gara) — e servono tutte e due,
perche' ognuna da sola lascia in piedi l'altra.

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

from .odds import consenso_per_partita
from .predictions import MODE_SECCO, MODE_UFFICIALE, PredictionStore, istante

logger = logging.getLogger(__name__)

# Sotto questa soglia ogni metrica e' rumore: due partite fortunate bastano a
# far sembrare eccellente un mercato inutile.
#
# **Si conta in PARTITE, non in esiti**, ed e' la seconda meta' della
# correzione cominciata in `esiti_risolti`. La' si e' tolto il conteggio
# multiplo della stessa partita ri-pronosticata; qui si toglie quello della
# stessa partita letta da piu' mercati. Le dieci linee Over/Under di una
# partita — Over 0.5, 1.5, 2.5... — sono decise tutte dallo STESSO numero di
# gol: se finisce 0-2, "Over 0.5 vero", "Over 1.5 vero" e "Under 2.5 vero"
# sono un'osservazione sola vestita da tre. Contarle per esiti diceva
# "Over/Under, 200 esiti" su venti partite, cioe' dieci volte la sostanza
# vera, e faceva scattare il verdetto dove sarebbe dovuto restare "troppi
# pochi dati".
#
# Sull'1X2 le tre selezioni di una partita sono esaustive e la soglia sulle
# partite e' ovviamente giusta; su Over/Under il numero di linee per partita
# puo' cambiare nel tempo, e agganciarsi alle partite rende la soglia stabile
# invece che dipendente da quante linee si e' deciso di salvare. Le tabelle
# continuano a mostrare gli esiti, che sono un'informazione vera: e' il
# VERDETTO a dipendere dalle partite.
SOGLIA_SIGNIFICATIVITA = 30

# Su quale dei due pronostici si misura, quando nessuno lo dice.
#
# Una partita puo' averne due: quello **secco**, scritto appena il modello la
# vede, e quello rifatto quando escono le **formazioni ufficiali**
# (`data/ufficiali.py`). Nessuna misura puo' sommarli: sono due risposte alla
# stessa domanda, quindi una media che li mescola conta ogni partita due volte
# e appiattisce l'uno sull'altro proprio i due numeri che si vogliono
# confrontare.
#
# Il default e' il secco, e non e' una scelta di comodo. E' l'unico dei due che
# esiste per **ogni** partita pronosticata: e' la serie continua, quella il cui
# andamento nel tempo si puo' guardare senza che cambi cosa si sta misurando.
# L'ufficiale compare solo dove i pesi dei giocatori esistevano e spostavano
# qualcosa (`vale_la_pena`), quindi su un sottoinsieme che si allarga da solo
# col passare delle giornate: usarlo come numero di riferimento significherebbe
# cambiare popolazione ogni settimana.
#
# Il confronto vero fra i due sta in `confronto_fra_modi`, che li mette uno
# accanto all'altro **sugli stessi esiti** — l'unico modo in cui la domanda ha
# una risposta.
MODO_PREDEFINITO = MODE_SECCO


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
    """Brier, log loss, frequenza reale e fiducia media di un gruppo di esiti.

    ``n`` sono gli esiti e ``partite`` le gare distinte da cui vengono. I due
    numeri divergono appena un mercato ha piu' selezioni per partita, ed e'
    ``partite`` quello su cui si decide se un numero valga un verdetto: vedi
    `SOGLIA_SIGNIFICATIVITA`.
    """
    n = len(righe)
    tasso = sum(1 for r in righe if r["hit"]) / n
    return {
        "n": n,
        "partite": len({r["fixture_id"] for r in righe}),
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


def accuratezza_per_selezione(
    store: PredictionStore, mode: str = MODO_PREDEFINITO
) -> list[dict]:
    """Come sta andando ogni singolo esito: Over 2.5 separato da Over 4.5.

    E' il livello a cui la baseline ha davvero senso, ed e' dove si vede quale
    parte di un mercato funziona e quale no.

    Su ``mode`` vale quel che dice `MODO_PREDEFINITO`: si misura un tipo di
    pronostico alla volta, mai i due insieme.
    """
    risultato = []
    for (mercato, selezione), righe in _raggruppa(
        store.esiti_risolti(mode=mode), lambda r: (r["market"], r["selection"])
    ).items():
        voce = {"mercato": mercato, "selezione": selezione, **_metriche(righe)}
        voce["significativo"] = voce["partite"] >= SOGLIA_SIGNIFICATIVITA
        voce["batte_baseline"] = voce["brier"] < voce["baseline"]
        risultato.append(voce)
    return sorted(risultato, key=lambda v: (v["mercato"], v["selezione"]))


def accuratezza_per_mercato(
    store: PredictionStore, mode: str = MODO_PREDEFINITO
) -> list[dict]:
    """Come sta andando ogni mercato, dal piu' misurato al meno.

    Il Brier si media direttamente sugli esiti, la baseline **no**: va
    calcolata per singola selezione e poi mediata. Su Over/Under la differenza
    e' tutta: la linea 0.5 si avvera quasi sempre e la 4.5 quasi mai, ma
    messe nello stesso mucchio si compensano fino a una frequenza del 50%, che
    darebbe al modello un muro molto piu' basso di quello vero. Una baseline
    troppo generosa e' peggio di nessuna baseline: fa sembrare informativo un
    mercato che non lo e'.

    ``significativo`` dice se il numero merita un verdetto, e si conta in
    **partite** e non in esiti: le dieci linee Over/Under di una gara sono
    decise dallo stesso punteggio, quindi valgono un'osservazione sola. Vedi
    `SOGLIA_SIGNIFICATIVITA`. Mostrare un Brier senza questa avvertenza
    sarebbe il modo piu' semplice di mentire con dati veri.

    Su ``mode`` vale quel che dice `MODO_PREDEFINITO`: si misura un tipo di
    pronostico alla volta, mai i due insieme.
    """
    risultato = []
    for mercato, righe in _raggruppa(
        store.esiti_risolti(mode=mode), lambda r: r["market"]
    ).items():
        voce = {"mercato": mercato, **_metriche(righe)}
        voce["baseline"] = sum(
            _baseline(sum(1 for r in gruppo if r["hit"]) / len(gruppo)) * len(gruppo)
            for gruppo in _raggruppa(righe, lambda r: r["selection"]).values()
        ) / len(righe)
        voce["significativo"] = voce["partite"] >= SOGLIA_SIGNIFICATIVITA
        voce["batte_baseline"] = voce["brier"] < voce["baseline"]
        risultato.append(voce)

    return sorted(risultato, key=lambda v: -v["n"])


def brier_1x2_multiclasse(
    store: PredictionStore, mode: str = MODO_PREDEFINITO
) -> dict | None:
    """Il Brier dell'1X2 nella forma confrontabile col backtest.

    Il backtest somma i tre esiti di ogni partita (0 perfetto, 0.667 tirando a
    caso); l'aggregato per selezione fa la media sui tre. Sono lo stesso numero
    diviso tre, ma metterli accanto senza dirlo confonderebbe: qui si calcola
    per partita, come nel backtest, cosi' i due valori si possono guardare
    insieme.

    Il conteggio si raggruppa per **partita** e non per pronostico, ed e' una
    differenza che si vede solo quando le due cose divergono — cioe' sempre,
    finche' `esiti_risolti` non ha imparato a tenere una riga sola per partita.
    Sommare i tre esiti di ogni *pronostico* dava a Genoa-Lecce cinque voci in
    una media che si chiama "per partita", e la pagina leggeva «su 34 partite»
    con dieci partite in archivio. La chiave qui e' quindi il ``fixture_id``:
    cosi' il numero e' giusto **per costruzione** e non perche' qualcun altro
    ha filtrato bene prima di arrivare qui.

    Sul ``mode`` vale la stessa aritmetica per un'altra ragione: una partita
    puo' avere il pronostico secco e quello con le formazioni ufficiali, e
    sono due risposte alla stessa domanda. Senza il filtro finirebbero
    sommate nella stessa chiave, che e' peggio del doppio conteggio.
    """
    per_partita: dict[int, float] = {}
    for riga in store.esiti_risolti(market="1x2", mode=mode):
        per_partita[riga["fixture_id"]] = (
            per_partita.get(riga["fixture_id"], 0.0) + riga["brier"]
        )
    if not per_partita:
        return None
    return {
        "n_partite": len(per_partita),
        "brier": sum(per_partita.values()) / len(per_partita),
        # Riferimento fisso: 1/3 a testa, cioe' non sapere nulla.
        "baseline_caso": 2 / 3,
    }


def confronto_col_mercato(
    store: PredictionStore, mode: str = MODO_PREDEFINITO
) -> dict:
    """Modello contro quote di chiusura, sugli stessi identici esiti.

    E' la domanda che conta: battere chi tira a caso non e' un risultato,
    battere il mercato lo e'. Le quote incorporano infortuni, formazioni e il
    denaro di chi scommette, e sono difficili da battere — un pareggio con
    loro sarebbe gia' un ottimo esito.

    Il confronto e' ristretto agli esiti per cui esistono **entrambe** le
    cose, pronostico risolto e quota di consenso. Confrontare il Brier del
    modello su tutte le partite con quello del mercato su una meta' diversa
    darebbe un numero che non significa niente.

    Per la stessa ragione si guarda un tipo di pronostico alla volta: la quota
    di chiusura di una partita e' **una**, e mettergli accanto due pronostici
    la conterebbe due volte, con lo stesso Brier del banco ripetuto — cioe' un
    confronto falsato proprio dalla parte che non e' cambiata.
    """
    quote = consenso_per_partita(store.quote_di_chiusura())
    if not quote:
        return {"mercati": [], "totale": None, "partite": 0}

    accoppiati: list[dict] = []
    for riga in store.esiti_risolti(mode=mode):
        chiave = (riga["fixture_id"], riga["market"], riga["selection"])
        if chiave not in quote:
            continue
        p_mercato = quote[chiave]
        accoppiati.append({
            **riga,
            "brier_modello": riga["brier"],
            "brier_mercato": brier(p_mercato, bool(riga["hit"])),
            "probabilita_mercato": p_mercato,
        })

    if not accoppiati:
        return {"mercati": [], "totale": None, "partite": 0}

    def riepiloga(righe: list[dict]) -> dict:
        n = len(righe)
        modello = sum(r["brier_modello"] for r in righe) / n
        mercato = sum(r["brier_mercato"] for r in righe) / n
        return {
            "n": n,
            "partite": len({r["fixture_id"] for r in righe}),
            "brier_modello": modello,
            "brier_mercato": mercato,
            "scarto": modello - mercato,
            "batte_il_mercato": modello < mercato,
        }

    mercati = [
        {"mercato": mercato, **riepiloga(righe)}
        for mercato, righe in _raggruppa(accoppiati, lambda r: r["market"]).items()
    ]
    for voce in mercati:
        voce["significativo"] = voce["partite"] >= SOGLIA_SIGNIFICATIVITA

    return {
        "mercati": sorted(mercati, key=lambda v: -v["n"]),
        "totale": riepiloga(accoppiati),
        "partite": len({r["fixture_id"] for r in accoppiati}),
    }


def confronto_fra_modi(
    store: PredictionStore,
    primo: str = MODE_SECCO,
    secondo: str = MODE_UFFICIALE,
) -> dict:
    """Il pronostico secco contro quello rifatto con le formazioni ufficiali.

    E' la domanda per cui `data/ufficiali.py` esiste: rifare il pronostico
    quando escono le formazioni **serve**? Non e' un'opinione, e' un numero —
    ed e' anche il modo in cui quel lavoro puo' morire, come sono morti il
    modello a tiri e il fattore dell'allenatore.

    La regola che rende il confronto onesto e' la stessa gia' usata contro le
    quote: si guardano **solo** gli esiti per cui esistono tutti e due. Il
    pronostico ufficiale nasce su un sottoinsieme — solo dove i pesi dei
    giocatori esistevano e spostavano qualcosa — e misurarlo su quelle partite
    contro un secco misurato su tutte confronterebbe due popolazioni diverse:
    verrebbe fuori quale gruppo di partite era piu' facile da indovinare, non
    quale dei due pronostici era migliore.

    Torna vuoto finche' i due modi non si sovrappongono da nessuna parte, che
    e' la situazione di oggi (nessun pronostico ufficiale scritto) e va
    mostrata come un vuoto, non come un pareggio.
    """
    def per_esito(mode: str) -> dict[tuple, dict]:
        return {
            (r["fixture_id"], r["market"], r["selection"]): r
            for r in store.esiti_risolti(mode=mode)
        }

    righe_primo = per_esito(primo)
    righe_secondo = per_esito(secondo)
    comuni = sorted(set(righe_primo) & set(righe_secondo))
    if not comuni:
        return {"mercati": [], "totale": None, "partite": 0, "primo": primo, "secondo": secondo}

    accoppiati = [
        {
            "fixture_id": chiave[0],
            "market": chiave[1],
            "selection": chiave[2],
            "brier_primo": righe_primo[chiave]["brier"],
            "brier_secondo": righe_secondo[chiave]["brier"],
        }
        for chiave in comuni
    ]

    def riepiloga(righe: list[dict]) -> dict:
        n = len(righe)
        uno = sum(r["brier_primo"] for r in righe) / n
        due = sum(r["brier_secondo"] for r in righe) / n
        return {
            "n": n,
            "partite": len({r["fixture_id"] for r in righe}),
            "brier_primo": uno,
            "brier_secondo": due,
            # Negativo = il secondo (le formazioni) e' migliore. Il segno e'
            # questo e non l'opposto perche' nel Brier piu' basso e' meglio.
            "scarto": due - uno,
            "il_secondo_e_migliore": due < uno,
        }

    mercati = [
        {"mercato": mercato, **riepiloga(righe)}
        for mercato, righe in _raggruppa(accoppiati, lambda r: r["market"]).items()
    ]
    for voce in mercati:
        voce["significativo"] = voce["partite"] >= SOGLIA_SIGNIFICATIVITA

    return {
        "mercati": sorted(mercati, key=lambda v: -v["n"]),
        "totale": riepiloga(accoppiati),
        "partite": len({r["fixture_id"] for r in accoppiati}),
        "primo": primo,
        "secondo": secondo,
    }


def calibrazione(
    store: PredictionStore,
    market: str = "1x2",
    selection: str | None = None,
    n_bin: int = 5,
    mode: str = MODO_PREDEFINITO,
) -> list[dict]:
    """Quando il modello dice 30%, succede il 30% delle volte?

    E' la metrica che conta di piu' per un'applicazione di pronostici: si puo'
    avere un buon Brier ed essere sistematicamente troppo sicuri di se'.

    Un tipo di pronostico alla volta, come per le altre misure: due modi di
    dichiarare la fiducia mescolati nella stessa fascia direbbero che e'
    calibrato un sistema che non esiste.
    """
    bins: dict[int, list[dict]] = {}
    for riga in store.esiti_risolti(market=market, selection=selection, mode=mode):
        idx = min(int(riga["probability"] * n_bin), n_bin - 1)
        bins.setdefault(idx, []).append(riga)

    righe = []
    for idx in sorted(bins):
        gruppo = bins[idx]
        righe.append({
            "fascia": f"{idx / n_bin:.0%}-{(idx + 1) / n_bin:.0%}",
            "n": len(gruppo),
            # Le gare distinte che hanno prodotto quegli esiti. Sull'1X2 una
            # partita ne mette tre, in fasce anche diverse: dire solo `n`
            # faceva sembrare trenta osservazioni cio' che erano dieci gare.
            "partite": len({r["fixture_id"] for r in gruppo}),
            "previsto": sum(r["probability"] for r in gruppo) / len(gruppo),
            "reale": sum(1 for r in gruppo if r["hit"]) / len(gruppo),
        })
    return righe


def partite_misurate(
    store: PredictionStore,
    market: str = "1x2",
    selection: str | None = None,
    mode: str = MODO_PREDEFINITO,
) -> int:
    """Su quante **gare distinte** poggia una misura.

    Esiste perche' la soglia del progetto si conta in partite e non in esiti
    (`SOGLIA_SIGNIFICATIVITA`), e la calibrazione era rimasta indietro: le
    sue fasce contano esiti, e sull'1X2 ogni gara ne mette tre. Sommare le
    partite delle fasce non risponderebbe — la stessa gara compare in piu'
    fasce e verrebbe contata due o tre volte — quindi il conto delle gare
    distinte si fa qui, dove le righe sono ancora tutte insieme.
    """
    return len({
        r["fixture_id"]
        for r in store.esiti_risolti(
            market=market, selection=selection, mode=mode
        )
    })


# ---- il registro, riga per riga ----------------------------------------------


def elenco_pronostici(
    store: PredictionStore, market: str = "1x2", mode: str | None = None
) -> list[dict]:
    """Ogni pronostico registrato, con accanto com'e' andata.

    E' l'unica cosa di questo modulo che **non e' una misura**, ed e' il
    motivo per cui ci sta lo stesso: mette il pronostico e la realta' uno
    accanto all'altra, che e' il mestiere del modulo. Cambia cosa succede
    quando la realta' non c'e' ancora — una misura salta quella riga, un
    registro la mostra con l'esito vuoto. Un pronostico in attesa e'
    esattamente cio' che il progetto promette di non poter piu' cambiare, e
    nasconderlo fino al fischio finale toglierebbe di mezzo la meta' della
    promessa.

    Il ``brier`` e' la **somma sui tre esiti**, cioe' la stessa forma del
    backtest: 0 se perfetto, 0.667 tirando a caso. Ha senso finche' il
    mercato e' esaustivo — l'1X2 lo e' — e resta ``None`` finche' non sono
    risolte tutte le selezioni di quel pronostico: sommarne due su tre darebbe
    un punteggio ottimo per il motivo sbagliato.

    ``mode`` e' ``None`` di default, al contrario di ogni misura del modulo, e
    non e' una dimenticanza. Elencare i due tipi di pronostico non li mescola:
    nessuna media li fonde in un numero solo, restano due righe, e vederle una
    sotto l'altra sulla stessa partita e' precisamente il punto.
    """
    per_pronostico: dict[int, dict] = {}
    for riga in store.mercati_con_esito(market, mode=mode):
        voce = per_pronostico.setdefault(riga["prediction_id"], {
            "id": riga["prediction_id"],
            "fixture_id": riga["fixture_id"],
            "mode": riga["mode"],
            "scritto_il": istante(riga["created_at"]),
            "versione": riga["version"],
            "gol_attesi": (riga["lambda_home"], riga["lambda_away"]),
            "selezioni": {},
        })
        voce["selezioni"][riga["selection"]] = {
            "probabilita": riga["probability"],
            "hit": None if riga["hit"] is None else bool(riga["hit"]),
            "brier": riga["brier"],
        }

    elenco = []
    for voce in per_pronostico.values():
        selezioni = list(voce["selezioni"].values())
        risolto = bool(selezioni) and all(s["hit"] is not None for s in selezioni)
        elenco.append({
            **voce,
            "risolto": risolto,
            "brier": sum(s["brier"] for s in selezioni) if risolto else None,
            # Quali selezioni si sono verificate, secondo il registro stesso e
            # non secondo il calendario. Su un mercato esaustivo e' una sola;
            # se un domani questa funzione venisse chiamata su Over/Under, di
            # righe vere ce ne sarebbero tante quante le linee — e chi legge
            # deve vederle tutte invece di ricevere la prima capitata.
            "riuscite": [n for n, s in voce["selezioni"].items() if s["hit"]],
        })
    return elenco


def adesso() -> datetime:
    return datetime.now(timezone.utc)
