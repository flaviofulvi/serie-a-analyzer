"""Test del riempimento di una stagione passata, con la rete simulata.

Questo codice girera' una volta sola, il giorno in cui il piano viene
comprato, e spendera' piu' di mille richieste vere. Non c'e' modo di provarlo
"quasi per davvero" prima: o e' giusto adesso, o si scopre che era sbagliato
dopo aver pagato.

I comportamenti verificati sono quelli il cui fallimento costerebbe caro:

* una stagione non coperta dal piano non da' errore ma zero partite — va
  riconosciuta e detta, non scambiata per un campionato senza partite;
* le partite si salvano prima dei dettagli, perche' sono l'ossatura;
* un riempimento interrotto riprende da dove era, senza ricomprare niente;
* i dettagli non toccano mai la riserva di budget della giornata di oggi;
* un'API che risponde male non brucia mille richieste una alla volta.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from serie_a_analyzer.data.budget import RequestBudget
from serie_a_analyzer.data.cache import ResponseCache
from serie_a_analyzer.data.client import API_SPORTS_BASE_URL, ApiFootballClient
from serie_a_analyzer.data.daily import MAX_ERRORI_CONSECUTIVI
from serie_a_analyzer.data.dataset import load_dataset
from serie_a_analyzer.data.players import costruisci_archivio
from serie_a_analyzer.data.store import SeasonStore, StoreUnito
from serie_a_analyzer.data.storico import (
    dettagli_in_sospeso,
    firma_storico,
    leggi_piano,
    riempi_stagione,
    stagioni_riempite,
    stima_richieste,
    store_stagione,
)

STAGIONE = 2025
INTER, TORINO = 505, 503


def _fixture(fid, status="FT", quando="2025-09-14T16:30:00+00:00", casa=INTER, ospite=TORINO):
    return {
        "fixture": {"id": fid, "date": quando, "status": {"short": status, "long": status}},
        "league": {"id": 135, "name": "Serie A", "season": STAGIONE,
                   "round": "Regular Season - 3"},
        "teams": {"home": {"id": casa, "name": "Inter"},
                  "away": {"id": ospite, "name": "Torino"}},
        "goals": {"home": 2, "away": 0},
    }


def _wrap(response):
    return {"get": "fixtures", "errors": [], "results": len(response),
            "paging": {"current": 1, "total": 1}, "response": response}


def _players(fid, player_id=1, nome="L. Martinez", voto="7.5"):
    """Un tabellino minimo, nella forma in cui lo scrive API-Football."""
    return [{
        "team": {"id": INTER, "name": "Inter"},
        "players": [{
            "player": {"id": player_id, "name": nome},
            "statistics": [{
                "games": {"minutes": 90, "position": "F", "rating": voto, "substitute": False},
                "goals": {"total": 1, "assists": None},
                "cards": {"yellow": 0, "red": 0},
            }],
        }],
    }]


@pytest.fixture
def mondo():
    """Cosa risponde l'API finta: piano, calendario per stagione, dettagli."""
    return {
        "piano": {
            "account": {"firstname": "Flavio"},
            "subscription": {"plan": "Pro", "active": True, "end": "2027-08-21"},
            "requests": {"current": 12, "limit_day": 7500},
        },
        "per_stagione": {},
        "dettagli": {},
        "chiamate": [],
        "errore_dettagli": None,
    }


@pytest.fixture
def client(tmp_path, mondo):
    c = ApiFootballClient(
        api_key="k",
        cache=ResponseCache(root=tmp_path / "cache"),
        budget=RequestBudget(state_path=tmp_path / "budget.json", daily_limit=7500),
        # Freno spento: qui la rete e' finta e non c'e' nessun tetto al
        # minuto da rispettare. Lasciarlo acceso farebbe dormire la suite.
        min_interval_seconds=0.0,
    )

    def router(request):
        percorso = request.url.path
        mondo["chiamate"].append(percorso)
        if percorso.endswith("/status"):
            return httpx.Response(200, json={"errors": [], "response": mondo["piano"]})
        if percorso.endswith("/fixtures"):
            stagione = request.url.params.get("season")
            return httpx.Response(200, json=_wrap(mondo["per_stagione"].get(int(stagione), [])))
        if mondo["errore_dettagli"]:
            return httpx.Response(
                200, json={"errors": mondo["errore_dettagli"], "response": []}
            )
        kind = percorso.rsplit("/", 1)[-1]
        fid = int(request.url.params.get("fixture"))
        return httpx.Response(200, json=_wrap(mondo["dettagli"].get((fid, kind), [])))

    with respx.mock:
        respx.get(url__startswith=API_SPORTS_BASE_URL).mock(side_effect=router)
        yield c


@pytest.fixture
def storico(tmp_path):
    """La radice di `data/storico/`, vuota come prima di ogni acquisto."""
    return tmp_path / "storico"


# ---- il piano ---------------------------------------------------------------


def test_il_piano_a_pagamento_viene_riconosciuto(client, mondo):
    piano = leggi_piano(client)

    assert piano.piano == "Pro"
    assert piano.attivo
    assert piano.limite_giornaliero == 7500
    assert piano.a_pagamento


def test_il_piano_gratuito_non_e_a_pagamento(client, mondo):
    """Cento richieste al giorno vuol dire piano gratuito, comunque si chiami.

    Si guarda il numero e non il nome: e' il numero a decidere se le stagioni
    mancanti sono leggibili.
    """
    mondo["piano"]["requests"]["limit_day"] = 100
    mondo["piano"]["subscription"]["plan"] = "Free"

    assert not leggi_piano(client).a_pagamento


def test_un_errore_sul_piano_non_solleva(client, mondo):
    """Chi chiede "che piano ho" deve ricevere una risposta, anche negativa."""
    mondo["piano"] = None  # l'API risponde qualcosa che non si puo' interpretare
    piano = leggi_piano(client)

    assert piano.limite_giornaliero is None
    assert not piano.a_pagamento


# ---- stagione non coperta ---------------------------------------------------


def test_una_stagione_non_coperta_viene_dichiarata(client, storico, mondo):
    """Zero partite non e' un campionato vuoto: e' un piano che non arriva li'.

    E' la trappola principale di API-Football: risponde 200 con una lista
    vuota. Scambiarla per un dato buono significherebbe scrivere un archivio
    vuoto e crederlo completo.
    """
    store = store_stagione(storico, STAGIONE)
    rapporto = riempi_stagione(client, store, STAGIONE, pausa=0)

    assert not rapporto.disponibile
    assert "non e' coperta dal piano" in (rapporto.errore or "")
    assert store.load_fixtures() == {}


def test_una_stagione_senza_partite_non_conta_come_riempita(client, storico, mondo):
    """La cartella resta, ma vuota: non deve comparire fra le stagioni pronte."""
    riempi_stagione(client, store_stagione(storico, STAGIONE), STAGIONE, pausa=0)

    assert stagioni_riempite(storico) == {}


# ---- riempimento ------------------------------------------------------------


@pytest.fixture
def campionato(mondo):
    """Tre partite concluse e una ancora da giocare, come una stagione vera."""
    partite = [_fixture(1), _fixture(2), _fixture(3), _fixture(4, status="NS")]
    mondo["per_stagione"][STAGIONE] = partite
    for fid in (1, 2, 3):
        mondo["dettagli"][(fid, "players")] = _players(fid)
        mondo["dettagli"][(fid, "lineups")] = [{"team": {"id": INTER, "name": "Inter"}}]
        mondo["dettagli"][(fid, "events")] = [{"type": "Goal"}]
    return partite


def test_le_partite_arrivano_tutte_in_una_richiesta(client, storico, mondo, campionato):
    store = store_stagione(storico, STAGIONE)
    rapporto = riempi_stagione(client, store, STAGIONE, con_dettagli=False, pausa=0)

    assert rapporto.disponibile
    assert rapporto.partite == 4
    assert rapporto.concluse == 3
    assert rapporto.nuove == 4
    assert rapporto.richieste == 1
    assert len(store.load_fixtures()) == 4


def test_i_dettagli_si_prendono_solo_per_le_partite_concluse(
    client, storico, mondo, campionato
):
    """La partita mai giocata non ha tabellino: chiederlo sarebbe soldi buttati."""
    store = store_stagione(storico, STAGIONE)
    riempi_stagione(client, store, STAGIONE, pausa=0)

    assert store.has_detail(1, "players")
    assert store.detail_envelope(4, "players") is None
    assert dettagli_in_sospeso(store) == 0


def test_un_secondo_giro_non_ricompra_niente(client, storico, mondo, campionato):
    """E' la garanzia che rende il riempimento interrompibile senza paura."""
    store = store_stagione(storico, STAGIONE)
    riempi_stagione(client, store, STAGIONE, pausa=0)
    speso_prima = client.budget.used

    secondo = riempi_stagione(client, store, STAGIONE, pausa=0)

    assert secondo.richieste == 0
    assert secondo.nuove == 0
    assert client.budget.used == speso_prima


def test_il_riempimento_interrotto_riprende_da_dove_era(
    client, storico, mondo, campionato
):
    store = store_stagione(storico, STAGIONE)
    primo = riempi_stagione(client, store, STAGIONE, max_richieste=4, pausa=0)

    assert primo.in_sospeso == 5  # 3 partite x 3 dettagli, meno le 4 fatte
    assert not primo.completa

    secondo = riempi_stagione(client, store, STAGIONE, pausa=0)

    assert secondo.completa
    assert secondo.in_sospeso == 0
    # Il calendario non e' stato ricomprato: solo i dettagli mancanti.
    assert secondo.richieste == 5


def test_i_dettagli_non_toccano_la_riserva_della_giornata(
    client, storico, mondo, campionato
):
    """Anche le formazioni di un anno fa sono lavoro arretrato.

    Nella raccolta giornaliera `lineups` e' NORMAL, perche' li' serve a
    pronosticare la partita di stasera. Qui e' storia: chiederla a NORMAL
    mangerebbe la riserva che protegge le formazioni ufficiali di oggi.
    """
    riempi_stagione(client, store_stagione(storico, STAGIONE), STAGIONE, pausa=0)

    per_priorita = client.budget.usage_by_priority()
    assert per_priorita.get("NORMAL") == 1        # solo il calendario
    assert per_priorita.get("BACKFILL") == 9      # 3 partite x 3 dettagli
    assert "CRITICAL" not in per_priorita


def test_un_api_rotta_non_brucia_tutte_le_richieste(client, storico, mondo, campionato):
    """Cinque errori di fila e ci si ferma.

    Il budget viene prenotato prima di spedire: senza questo freno, un piano
    scaduto a meta' riempimento costerebbe mille richieste per salvare zero
    dati.
    """
    store = store_stagione(storico, STAGIONE)
    riempi_stagione(client, store, STAGIONE, con_dettagli=False, pausa=0)
    mondo["errore_dettagli"] = {"plan": "sottoscrizione scaduta"}
    speso_prima = client.budget.used

    rapporto = riempi_stagione(client, store, STAGIONE, pausa=0)

    assert client.budget.used - speso_prima == MAX_ERRORI_CONSECUTIVI
    assert rapporto.in_sospeso == 9  # niente e' stato salvato


def test_il_prezzo_di_una_stagione_e_dichiarato_prima():
    """Una per il calendario, tre per ogni partita conclusa."""
    assert stima_richieste(380) == 1 + 380 * 3


# ---- gli archivi riempiti ---------------------------------------------------


def test_una_stagione_riempita_si_riconosce_dal_disco(client, storico, mondo, campionato):
    """Nessun file di configurazione da tenere allineato: c'e' la cartella, o non c'e'."""
    riempi_stagione(client, store_stagione(storico, STAGIONE), STAGIONE, pausa=0)

    trovate = stagioni_riempite(storico)
    assert list(trovate) == [STAGIONE]
    assert len(trovate[STAGIONE].load_fixtures()) == 4


def test_una_cartella_che_non_e_una_stagione_viene_ignorata(storico):
    storico.mkdir(parents=True)
    (storico / "appunti").mkdir()
    (storico / "appunti" / "fixtures.json").write_text("{}", encoding="utf-8")

    assert stagioni_riempite(storico) == {}


def test_l_impronta_cambia_quando_arriva_una_stagione(client, storico, mondo, campionato):
    """E' cio' che fa ristimare il modello al sito gia' aperto, senza riavvio."""
    prima = firma_storico(storico)
    riempi_stagione(client, store_stagione(storico, STAGIONE), STAGIONE, pausa=0)

    assert firma_storico(storico) != prima


# ---- come entra nel resto del sistema ---------------------------------------


def test_la_stagione_riempita_entra_nel_dataset(client, tmp_path, storico, mondo, campionato):
    riempi_stagione(client, store_stagione(storico, STAGIONE), STAGIONE, pausa=0)
    corrente = SeasonStore(root=tmp_path / "serie_a")

    dataset = load_dataset(client, corrente, seasons=(), storici=stagioni_riempite(storico))

    assert len(dataset.fixtures) == 4
    assert dataset.n_riempite == 4
    assert dataset.stagioni_riempite == [STAGIONE]
    assert dataset.name(INTER) == "Inter"


def test_la_raccolta_giornaliera_vince_sulla_stagione_riempita(
    client, tmp_path, storico, mondo, campionato
):
    """Se la stessa partita sta in due archivi, conta quello aggiornato ogni giorno.

    Non e' un caso di scuola: il riempimento puo' sovrapporsi alla stagione in
    corso, e li' l'unica sorgente che vede una partita finire e' la raccolta.
    """
    riempi_stagione(client, store_stagione(storico, STAGIONE), STAGIONE, pausa=0)
    corrente = SeasonStore(root=tmp_path / "serie_a")
    aggiornata = _fixture(4, status="FT")
    aggiornata["goals"] = {"home": 3, "away": 1}
    corrente.upsert_fixtures([aggiornata])

    dataset = load_dataset(client, corrente, seasons=(), storici=stagioni_riempite(storico))

    partita = dataset.by_id[4]
    assert partita.is_finished
    assert (partita.home_goals, partita.away_goals) == (3, 1)


def test_le_schede_giocatore_leggono_da_tutti_gli_archivi(
    client, tmp_path, storico, mondo, campionato
):
    """Un giocatore ha una carriera sola, anche se i tabellini stanno in due posti."""
    riempi_stagione(client, store_stagione(storico, STAGIONE), STAGIONE, pausa=0)
    corrente = SeasonStore(root=tmp_path / "serie_a")
    recente = _fixture(
        99, quando=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    )
    corrente.upsert_fixtures([recente])
    corrente.save_detail(99, "players", _players(99))

    dataset = load_dataset(client, corrente, seasons=(), storici=stagioni_riempite(storico))
    archivio = costruisci_archivio(
        StoreUnito(stores=[corrente, *stagioni_riempite(storico).values()]),
        dataset.fixtures,
    )

    scheda = archivio.scheda(1)
    assert scheda is not None
    # Tre partite dalla stagione riempita piu' quella di ieri: una carriera sola.
    assert scheda.presenze == 4
    assert archivio.partite_con_statistiche == 4
    assert archivio.copertura == 1.0


def test_l_archivio_unito_preferisce_la_stagione_in_corso(tmp_path):
    """L'ordine degli archivi e' una regola, non un dettaglio: vince il primo."""
    corrente = SeasonStore(root=tmp_path / "serie_a")
    vecchio = SeasonStore(root=tmp_path / "storico" / "2025")
    corrente.save_detail(7, "players", [{"team": {"id": INTER}}])
    vecchio.save_detail(7, "players", [{"team": {"id": TORINO}}])
    vecchio.save_detail(8, "players", [{"team": {"id": TORINO}}])

    unito = StoreUnito(stores=[corrente, vecchio])

    assert unito.load_detail(7, "players")[0]["team"]["id"] == INTER
    assert unito.load_detail(8, "players")[0]["team"]["id"] == TORINO
    assert sorted(unito.fixture_ids()) == []  # i dettagli non sono partite


def test_una_busta_vuota_non_ferma_la_ricerca_negli_altri_archivi(tmp_path):
    """Vuoto qui vuol dire "qui non c'e'", non "non esiste"."""
    corrente = SeasonStore(root=tmp_path / "serie_a")
    vecchio = SeasonStore(root=tmp_path / "storico" / "2025")
    corrente.save_detail(7, "players", [])
    vecchio.save_detail(7, "players", [{"team": {"id": TORINO}}])

    assert StoreUnito(stores=[corrente, vecchio]).load_detail(7, "players")
