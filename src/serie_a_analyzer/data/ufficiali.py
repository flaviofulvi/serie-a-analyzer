"""Il pronostico rifatto quando escono le formazioni ufficiali.

E' l'unico momento in cui si sa **chi gioca davvero**, e dura poco: le
formazioni escono una mezz'ora prima del fischio d'inizio e il registro rifiuta
— giustamente — qualsiasi pronostico datato dopo il via. Fra quei due istanti
c'e' tutta la finestra utile.

DUE PRONOSTICI, NON UNO CORRETTO

Il pronostico "secco" resta dov'e'. Questo se ne aggiunge un altro accanto, in
modalita' `formazione_ufficiale`, sulla stessa partita e con la stessa versione
del modello. Non e' pignoleria: e' l'unico modo di **scoprire se questo lavoro
serve**. Dopo qualche mese si confrontano i due Brier, e se quello con le
formazioni non e' migliore la risposta non e' un'opinione contro un'altra —
e' il modello che dice che quel peso non c'era, e il lavoro si butta senza
rimpianti.

LA MACCHINA NASCE SPENTA, ED E' VOLUTO

Il peso di un giocatore si misura da cio' che ha prodotto (`data/pesi.py`), e
quella produzione sta nei tabellini. Finche' sul disco non ce ne sono
abbastanza, `pesi_squadra` restituisce ``None``, `vale_la_pena` dice di no e
qui non viene scritta **nessuna riga**. Non e' un caso degenere da tollerare:
e' il comportamento giusto. I pronostici sono immutabili e non si cancellano,
quindi una riga `formazione_ufficiale` identica a quella secca non sarebbe
inutile, sarebbe dannosa — gonfierebbe il confronto fra le due modalita' con
partite in cui non c'era niente da confrontare.

La macchina si accende da sola quando i tabellini bastano. Nessuno deve
ricordarsi di accenderla.

SPENTA PER MISURA, dall'11 settembre 2026

Poi la misura e' arrivata prima dell'accensione (`backtest.py
--cerca-fattore`, lavoro 1b): rigiocate quattro stagioni, il pronostico con le
formazioni non batte il secco — ne' in media, ne' sulle formazioni piu'
lontane dal solito. Da allora `MACCHINA_ACCESA` e' falso e qui non si scrive
niente, pesi o non pesi. La distinta pero' si legge lo stesso, UNA volta per
partita appena e' completa: serve ai probabili marcatori della scheda partita
e all'archivio. Tutto il resto di questo modulo descrive la macchina accesa, e
resta com'e' per il giorno in cui un metodo nuovo la riaccendesse — rifacendo
prima la misura, perche' dalla prima riga non si torna indietro.

DUE TRAPPOLE DA NON RIPETERE

**Una formazione vuota non si salva.** Prima che escano, l'endpoint risponde
con una lista vuota. Salvarla come dettaglio farebbe scattare il contatore dei
tentativi (`MAX_DETAIL_ATTEMPTS` in data/store.py): dopo tre letture anticipate
la partita risulterebbe "gia' tentata" e le formazioni vere, quelle di fine
serata, non verrebbero piu' chieste. Si perderebbe un dato per aver guardato
troppo presto.

**Una formazione a meta' si salva e non si usa**, ed e' l'altra faccia della
stessa trappola. L'API pubblica la distinta un pezzo per volta: il 29 agosto
2026, a quindici minuti dal via, tre partite su quattro avevano dieci titolari
o nessun panchinaro. Salvarla e' giusto — l'archivio sa che non e' l'ultima
parola (`formazione_provvisoria` in data/store.py) e la fara' ri-chiedere dopo
la partita — ma **pronosticarci sopra no**. Dieci titolari su undici fanno
scendere la produzione in campo della quota del giocatore che manca — un
undicesimo se e' un titolare qualunque, molto di piu' se e' quello bravo — per
**pura aritmetica**, cioe' abbastanza da far scattare `vale_la_pena` e da
spostare i gol attesi, e niente lo distinguerebbe da un'assenza vera: quella
riga direbbe per sempre che il modello aveva visto mancare qualcuno che invece
giocava. Si salta e si
riprova al giro dopo — l'attivita' pianificata passa ogni quarto d'ora, e
nella finestra dei 75 minuti ci sono cinque occasioni. Se la distinta non si
completa in tempo quella partita resta col solo pronostico secco, che e'
esattamente la regola qui sotto: meglio un buco.

**Meglio un buco che una riga scritta dopo.** Se questa esecuzione salta —
computer spento, budget finito, API muta — quella partita resta col solo
pronostico secco. Va bene cosi': un pronostico registrato a partita iniziata
non e' un pronostico.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..model.markets import derive_markets
from ..model.scoreline import scoreline_matrix
from .pesi import (
    FATTORE_PREDEFINITO,
    aggiusta_gol_attesi,
    pesi_squadra,
    rapporto_produzione,
    titolari_da_formazione,
    vale_la_pena,
)
from .players import formazioni_partita
from .predictions import MODE_UFFICIALE, PredizioneRifiutata
from .store import formazione_provvisoria

logger = logging.getLogger(__name__)

# Quanto prima del fischio d'inizio si comincia a chiedere le formazioni.
# Escono intorno ai 30-60 minuti: chiedere molto prima spreca richieste della
# riserva CRITICAL, chiedere troppo tardi rischia di non fare in tempo. Questa
# finestra si attraversa piu' volte, non una: l'attivita' pianificata gira ogni
# quarto d'ora e la prima lettura che trova qualcosa vince.
FINESTRA_MINUTI = 75

# Se la macchina scrive pronostici 'formazione ufficiale'. Spenta l'11
# settembre 2026 per misura, prima della prima riga: vedi in cima al modulo e
# `backtest.py --cerca-fattore`. Chi la riaccende rifaccia prima quella misura
# con un metodo nuovo — i pronostici non si cancellano.
MACCHINA_ACCESA = False


def partite_imminenti(
    fixtures: Iterable[Any],
    now: datetime | None = None,
    entro_minuti: int = FINESTRA_MINUTI,
) -> list[Any]:
    """Le partite che stanno per cominciare e non sono ancora cominciate.

    Dalla piu' imminente: se qualcosa si ferma a meta', a restare scoperta e'
    quella che ha piu' tempo davanti per essere ripresa al giro dopo.
    """
    adesso = now or datetime.now(timezone.utc)
    limite = adesso + timedelta(minutes=entro_minuti)
    imminenti = [f for f in fixtures if adesso < f.kickoff_utc <= limite]
    return sorted(imminenti, key=lambda f: f.kickoff_utc)


def leggi_formazioni(client, store, fixture_id: int) -> list[Any]:
    """Chiede le formazioni e le salva **solo se ci sono davvero**.

    Una lista vuota non e' un errore: significa che non sono ancora uscite. E
    non va salvata — vedi la trappola descritta in cima al modulo.
    """
    try:
        payload = client.lineups(fixture_id)
    except Exception as exc:
        logger.warning("formazioni non lette per la partita %s: %s", fixture_id, exc)
        return []

    if not payload:
        logger.info("formazioni della partita %s non ancora uscite", fixture_id)
        return []

    store.save_detail(fixture_id, "lineups", payload)
    return formazioni_partita(store, fixture_id)


def pronostica_con_formazioni(
    client,
    store,
    registro,
    *,
    model,
    dataset,
    archivio,
    now: datetime | None = None,
    entro_minuti: int = FINESTRA_MINUTI,
    fattore: float = FATTORE_PREDEFINITO,
    max_richieste: int | None = None,
    accesa: bool | None = None,
) -> dict[str, int]:
    """Per ogni partita imminente: formazioni, pesi, e un pronostico in piu'.

    Non solleva. Ogni motivo per cui una partita viene saltata ha il suo
    contatore, perche' "non ha fatto niente" e "non ha potuto fare niente" sono
    due diagnosi diverse e chi guarda l'uscita deve poterle distinguere.

    ``accesa`` a ``None`` vuol dire `MACCHINA_ACCESA`, letto al momento della
    chiamata e non a quello della definizione: e' cio' che permette ai test di
    provare la macchina accesa senza riaccenderla per tutti.
    """
    if accesa is None:
        accesa = MACCHINA_ACCESA
    adesso = now or datetime.now(timezone.utc)
    esito = {
        "esaminate": 0,
        "registrate": 0,
        "gia_registrate": 0,
        "senza_formazioni": 0,
        "formazioni_a_meta": 0,
        "senza_pesi": 0,
        "senza_modello": 0,
        "gia_iniziate": 0,
        "letture": 0,
        # Solo a macchina spenta: distinte lette per l'archivio, e partite in
        # cui la distinta completa c'era gia' e non si e' spesa niente.
        "spenta": 0,
        "gia_lette": 0,
    }

    for f in partite_imminenti(dataset.upcoming(), now=adesso, entro_minuti=entro_minuti):
        if max_richieste is not None and esito["letture"] >= max_richieste:
            logger.info("formazioni: raggiunto il tetto di %d richieste", max_richieste)
            break
        esito["esaminate"] += 1

        if registro.ultimo_per_partita(f.api_id, mode=MODE_UFFICIALE):
            esito["gia_registrate"] += 1
            continue

        if not accesa:
            # Spenta per misura: nessun pronostico, ma la distinta serve lo
            # stesso — ai probabili marcatori e all'archivio. Si legge finche'
            # non e' completa e poi non piu': la cache delle formazioni dura
            # cinque minuti, e da accesa la riga scritta chiudeva il caso. Da
            # spenta nessuna riga lo chiude, e rileggerla a ogni giro sarebbe
            # sul gratuito una manciata di richieste buttate per partita.
            gia = store.load_detail(f.api_id, "lineups")
            if gia and not formazione_provvisoria(gia):
                esito["gia_lette"] += 1
                continue
            formazioni = leggi_formazioni(client, store, f.api_id)
            esito["letture"] += 1
            if not formazioni:
                esito["senza_formazioni"] += 1
            elif formazione_provvisoria(store.load_detail(f.api_id, "lineups")):
                esito["formazioni_a_meta"] += 1
            else:
                esito["spenta"] += 1
            continue

        formazioni = leggi_formazioni(client, store, f.api_id)
        esito["letture"] += 1
        if not formazioni:
            esito["senza_formazioni"] += 1
            continue

        # La distinta e' uscita ma non e' finita: salvata si', usata no (vedi
        # la seconda trappola in cima al modulo). Si giudica la busta appena
        # scritta e non gli oggetti qui sopra, perche' la regola e' una sola e
        # sta in `data/store.py`: e' la stessa che dopo la partita fara'
        # ri-chiedere quel dettaglio.
        if formazione_provvisoria(store.load_detail(f.api_id, "lineups")):
            esito["formazioni_a_meta"] += 1
            continue

        per_squadra = {fo.team_id: fo for fo in formazioni}
        casa = per_squadra.get(f.home_team_id)
        ospite = per_squadra.get(f.away_team_id)
        if casa is None or ospite is None:
            # Capita con una formazione sola, o con id che non combaciano.
            esito["senza_formazioni"] += 1
            continue

        # I pesi: la meta' dell'informazione che il pronostico secco non ha.
        # Se mancano, `vale_la_pena` dira' di no e non si scrive niente.
        pesi_casa = pesi_squadra(archivio, f.home_team_id)
        pesi_ospite = pesi_squadra(archivio, f.away_team_id)
        r_casa = (
            rapporto_produzione(pesi_casa, titolari_da_formazione(casa))
            if pesi_casa else None
        )
        r_ospite = (
            rapporto_produzione(pesi_ospite, titolari_da_formazione(ospite))
            if pesi_ospite else None
        )

        if not vale_la_pena(r_casa, r_ospite):
            esito["senza_pesi"] += 1
            continue

        try:
            lam, mu = model.expected_goals(
                dataset.name(f.home_team_id), dataset.name(f.away_team_id)
            )
        except KeyError:
            # Neopromossa senza storia: nessun pronostico da correggere.
            esito["senza_modello"] += 1
            continue

        # Nota: `expected_goals` restituisce gia' i gol attesi COMPRESSI
        # verso la media di lega (`comprimi_totale`). L'aggiustamento delle
        # formazioni parte da li' e si muove liberamente: dice qualcosa sui
        # gol che il modello di squadra non sa, e ricomprimerlo dopo
        # cancellerebbe proprio cio' che si sta cercando di misurare.
        #
        # Ogni squadra si muove per conto suo. Il metodo pesa chi ATTACCA:
        # l'assenza di un difensore avversario non alza questi gol attesi,
        # perche' il metodo non la vede — ed e' un limite dichiarato, non una
        # svista da compensare con un'ipotesi.
        lam_casa = aggiusta_gol_attesi(lam, r_casa, fattore)
        lam_ospite = aggiusta_gol_attesi(mu, r_ospite, fattore)
        matrice = scoreline_matrix(lam_casa, lam_ospite, model.rho)

        try:
            registro.registra(
                fixture_id=f.api_id,
                kickoff_utc=f.kickoff_utc,
                home_team_id=f.home_team_id,
                away_team_id=f.away_team_id,
                model_version_id=registro.versione_modello(model),
                matrice=matrice,
                probabilita=derive_markets(matrice),
                lambda_home=lam_casa,
                lambda_away=lam_ospite,
                rho=model.rho,
                mode=MODE_UFFICIALE,
                now=adesso,
            )
        except PredizioneRifiutata:
            # La partita e' cominciata mentre leggevamo. Meglio un buco.
            esito["gia_iniziate"] += 1
            continue
        except Exception as exc:
            logger.warning("pronostico ufficiale non registrato per %s: %s", f.api_id, exc)
            continue

        esito["registrate"] += 1
        logger.info(
            "partita %s: pronostico con le formazioni (casa %+.1f%%, ospite %+.1f%%)",
            f.api_id,
            r_casa.variazione_percentuale if r_casa else 0.0,
            r_ospite.variazione_percentuale if r_ospite else 0.0,
        )

    return esito
