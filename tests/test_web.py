"""Test delle pagine web.

Il motore viene costruito su un archivio finto in una cartella temporanea e con
``seasons=()``, cosi' le pagine non toccano mai la rete: l'intero percorso
archivio -> dataset -> modello -> HTML gira in memoria.

Cio' che si verifica non e' l'aspetto grafico ma le tre cose che rompono
davvero una pagina di pronostici: che i numeri arrivino, che l'assenza di dati
venga detta invece che nascosta, e che il modello non venga ristimato a ogni
richiesta.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from serie_a_analyzer.config import Settings
from serie_a_analyzer.data.predictions import MODE_SECCO, MODE_UFFICIALE, registra_partite
from serie_a_analyzer.web import app as app_module
from serie_a_analyzer.web.engine import Engine
from serie_a_analyzer.data.outcomes import SOGLIA_SIGNIFICATIVITA
from serie_a_analyzer.web.views import (
    MINIMO_PER_FASCIA,
    confronto_ufficiale,
    distanza_breve,
    righe_calibrazione,
    righe_registro,
    verdetto_calibrazione,
)

SQUADRE = {
    1: "Inter", 2: "Milan", 3: "Juventus",
    4: "Napoli", 5: "Roma", 6: "Lecce",
}

ADESSO = datetime.now(timezone.utc)


def _nome(team_id):
    return SQUADRE.get(team_id, f"Squadra {team_id}")


def _raw(fid, casa, ospite, quando, gol=None, giornata=1):
    concluso = gol is not None
    return {
        "fixture": {
            "id": fid,
            "date": quando.isoformat(),
            "status": {"short": "FT" if concluso else "NS",
                       "long": "Match Finished" if concluso else "Not Started"},
            "venue": {"name": "Stadio"},
        },
        "league": {"id": 135, "name": "Serie A", "season": 2026,
                   "round": f"Regular Season - {giornata}"},
        "teams": {"home": {"id": casa, "name": _nome(casa)},
                  "away": {"id": ospite, "name": _nome(ospite)}},
        "goals": {"home": gol[0] if concluso else None,
                  "away": gol[1] if concluso else None},
        "score": {"halftime": {"home": None, "away": None}},
    }


def _campionato_finto():
    """Girone di andata concluso piu' due partite in programma.

    I risultati non sono casuali: l'Inter vince sempre e il Lecce perde sempre,
    cosi' il modello ha un segnale netto da trovare e il test puo' affermare
    qualcosa sull'ordine delle forze invece che solo sui codici HTTP.
    """
    partite = []
    fid = 1000
    giorno = ADESSO - timedelta(days=120)

    squadre = list(SQUADRE)
    for i, casa in enumerate(squadre):
        for ospite in squadre[i + 1:]:
            for a, b in ((casa, ospite), (ospite, casa)):
                fid += 1
                giorno += timedelta(days=2)
                forza = {1: 3, 2: 2, 3: 2, 4: 1, 5: 1, 6: 0}
                gol = (forza[a] + 1, forza[b])
                partite.append(_raw(fid, a, b, giorno, gol=gol))

    # Due partite future: sono quelle che l'hub deve mostrare.
    partite.append(_raw(9001, 1, 6, ADESSO + timedelta(days=2), giornata=20))
    partite.append(_raw(9002, 2, 3, ADESSO + timedelta(days=2, hours=3), giornata=20))
    return partite


@pytest.fixture
def motore(tmp_path):
    settings = Settings(
        api_key="chiave-di-prova",
        api_provider="apisports",
        daily_request_limit=100,
        # Il motore non tocca la rete qui, ma il freno del client dorme
        # sul serio: spento.
        api_min_interval_seconds=0.0,
        cache_dir=tmp_path / "cache",
        budget_file=tmp_path / "budget.json",
        store_dir=tmp_path / "serie_a",
        storico_dir=tmp_path / "storico",
        importato_dir=tmp_path / "importato",
        journal_file=tmp_path / "journal.json",
        predictions_db=tmp_path / "pronostici.db",
        database_url=None,
        league_id=135,
        # Nessuna stagione storica: niente rete, l'archivio basta.
        seasons=(),
    )
    eng = Engine(settings)
    eng.store.upsert_fixtures(_campionato_finto())
    return eng


def _congela_il_passato(eng: Engine) -> None:
    """Simula un sistema che era acceso anche allora.

    Senza questo le pagine sarebbero un mondo impossibile: partite giocate di
    cui nessuno aveva mai detto niente prima. E dal momento che le pagine
    mostrano il pronostico **congelato** e non un ricalcolo, un mondo cosi'
    non farebbe apparire nessun numero sulle partite passate — nascondendo
    proprio cio' che questi test vogliono guardare.

    Ogni riga viene scritta con ``now`` un'ora prima del suo calcio d'inizio,
    che e' l'unico modo di passare dal controllo del registro: una partita
    gia' cominciata non si registra, ed e' la regola che rende sensato tutto
    il resto.
    """
    snap = eng.snapshot()
    if snap.model is None:
        return
    for partita in snap.dataset.finished:
        registra_partite(
            eng.pronostici, snap.model, [partita], snap.dataset.name,
            now=partita.kickoff_utc - timedelta(hours=1),
        )


@pytest.fixture
def client(motore, monkeypatch):
    monkeypatch.setattr(app_module, "_engine", motore)
    return TestClient(app_module.app)


@pytest.fixture
def client_col_passato(motore, monkeypatch):
    """Come ``client``, ma in un mondo dove il sistema girava gia' allora.

    Sta a parte e non nel fixture di tutti perche' i pronostici delle partite
    concluse sono esattamente cio' che altri test verificano non venga scritto
    da solo: metterli nel mondo comune ne distruggerebbe la premessa.
    """
    _congela_il_passato(motore)
    monkeypatch.setattr(app_module, "_engine", motore)
    return TestClient(app_module.app)


# ---- hub -------------------------------------------------------------------


def test_l_hub_mostra_le_partite_in_programma(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Prossime partite" in r.text
    assert "Inter" in r.text and "Lecce" in r.text


def test_l_hub_mostra_le_probabilita(client):
    """Senza numeri la pagina non serve a niente."""
    r = client.get("/")
    assert "Over 2.5" in r.text
    assert "Gol/NoGol" in r.text
    assert "%" in r.text


def test_l_hub_mostra_la_tabella_delle_forze(client):
    r = client.get("/")
    assert "Forza stimata delle squadre" in r.text
    assert "Fattore campo" in r.text


def test_l_ordine_delle_forze_rispecchia_i_risultati(motore):
    """L'Inter ha sempre vinto, il Lecce ha sempre perso: si deve vedere."""
    snap = motore.snapshot()
    ordine = [squadra for squadra, _, _, _ in snap.model.strength_table()]
    assert ordine[0] == "Inter"
    assert ordine[-1] == "Lecce"


def test_l_hub_dice_quando_non_c_e_niente(tmp_path, monkeypatch):
    """Un archivio vuoto deve produrre una spiegazione, non una pagina muta."""
    settings = Settings(
        api_key="k", api_provider="apisports", daily_request_limit=100, api_min_interval_seconds=0.0,
        cache_dir=tmp_path / "c", budget_file=tmp_path / "b.json",
        store_dir=tmp_path / "s", storico_dir=tmp_path / "st",
        importato_dir=tmp_path / "imp",
        journal_file=tmp_path / "j.json",
        predictions_db=tmp_path / "pronostici.db",
        database_url=None, league_id=135, seasons=(),
    )
    monkeypatch.setattr(app_module, "_engine", Engine(settings))
    r = TestClient(app_module.app).get("/")

    assert r.status_code == 200
    assert "Nessuna partita in programma" in r.text


# ---- scheda partita --------------------------------------------------------


def test_la_scheda_partita_ha_tutti_i_mercati(client):
    r = client.get("/partita/9001")
    assert r.status_code == 200
    for atteso in ("Esito finale", "Doppia chance", "Over / Under",
                   "Tutti i risultati esatti", "Scontri diretti"):
        assert atteso in r.text, atteso


def test_la_scheda_avvisa_del_limite_sull_over_under(client):
    """Il backtest dice che su questi esiti il modello non aggiunge nulla.

    Mostrarli senza dirlo sarebbe la cosa piu' disonesta dell'applicazione.

    Dal 9 settembre 2026 l'avviso e' una riga invece di un paragrafo — le
    spiegazioni del sito sono finite in ISTRUZIONI.txt — ma le sette parole
    che il progetto pretende sono le stesse: e' su quelle che il test insiste,
    non sulla lunghezza.
    """
    r = client.get("/partita/9001")
    assert "media di campionato" in r.text
    assert "quanto sa davvero" in r.text


def test_la_scheda_dichiara_la_compressione_dei_gol(client):
    """Il totale atteso e' schiacciato sulla media di lega: va detto, non subito.

    E' la stessa regola dell'avviso qui sopra. Un totale sempre uguale a
    quello di ogni altra partita sembra un difetto se non si spiega che e'
    una scelta, e che nasce da quattro stagioni rigiocate.
    """
    r = client.get("/partita/9001")
    assert "vicino alla media del" in r.text
    # Il numero arriva dal modello, non dal template: e' la media di lega
    # stimata sullo stesso campione, e cambia con l'archivio.
    media = client.get("/stato").json()["media_gol_lega"]
    assert f"{media:.2f} gol a partita" in r.text


def test_lo_stato_dice_quanto_si_comprime(client):
    stato = client.get("/stato").json()
    assert 0.0 <= stato["compressione_gol"] <= 1.0
    assert stato["media_gol_lega"] > 0


def test_gli_scontri_diretti_escludono_la_partita_stessa(client):
    r = client.get("/partita/9001")
    assert r.text.count("campo invertito") >= 1


def test_partita_inesistente(client):
    assert client.get("/partita/424242").status_code == 404


def test_squadra_sconosciuta_al_modello(client, motore):
    """Una neopromossa senza storia: si dice, non si inventa una forza."""
    motore.store.upsert_fixtures([
        _raw(9500, 1, 99, ADESSO + timedelta(days=3), giornata=21)
    ])
    r = client.get("/partita/9500")

    assert r.status_code == 200
    assert "Nessun pronostico disponibile" in r.text


# ---- affidabilita' del pronostico ------------------------------------------
#
# Il pronostico prudente di una squadra sconosciuta e quello di una partita
# davvero equilibrata sono lo stesso numero. La scheda deve saperli
# distinguere prima del fischio d'inizio, non mesi dopo con l'accuratezza.


def test_la_scheda_dice_su_cosa_si_basa_il_pronostico(client):
    r = client.get("/partita/9001")
    assert "Su cosa si basa" in r.text
    assert "verso la media" in r.text


def test_la_scheda_mostra_una_riga_per_squadra(client, motore):
    r = client.get("/partita/9001")
    snap = motore.snapshot()
    for squadra in ("Inter", "Lecce"):
        evidenza = snap.model.evidence_for(squadra)
        assert f"&minus;{round(evidenza.pull * 100)}%" in r.text


def test_una_squadra_con_poche_partite_viene_dichiarata_debole(motore, monkeypatch):
    """Chi ha giocato poco esce tirato verso la media, e la pagina lo dice.

    E' il caso per cui l'indicatore esiste: senza, quel pronostico
    prudentissimo sembrerebbe una lettura della partita.
    """
    # Una settima squadra che ha giocato due sole volte, contro l'Inter.
    motore.store.upsert_fixtures([
        _raw(9600, 1, 7, ADESSO - timedelta(days=10), gol=(2, 1), giornata=19),
        _raw(9601, 7, 1, ADESSO - timedelta(days=5), gol=(1, 1), giornata=19),
        _raw(9602, 1, 7, ADESSO + timedelta(days=2), giornata=21),
    ])
    monkeypatch.setattr(app_module, "_engine", motore)
    client = TestClient(app_module.app)

    snap = motore.snapshot()
    novellina = snap.model.evidence_for(_nome(7))
    veterana = snap.model.evidence_for("Inter")
    assert novellina.n_matches == 2
    assert novellina.pull > veterana.pull

    r = client.get("/partita/9602")
    assert r.status_code == 200
    assert "il modello sa poco" in r.text or "stima debole" in r.text


def test_senza_pronostico_non_si_parla_di_affidabilita(client, motore):
    """Non sapere chi e' una squadra e saperne poco sono cose diverse.

    Mostrare una riga sola farebbe sembrare solidissima l'altra, mentre il
    motivo per cui manca e' esattamente l'opposto.
    """
    motore.store.upsert_fixtures([
        _raw(9700, 1, 98, ADESSO + timedelta(days=3), giornata=21)
    ])
    r = client.get("/partita/9700")
    assert r.status_code == 200
    assert "Nessun pronostico disponibile" in r.text
    assert "Su cosa si basa" not in r.text


def test_un_archivio_fermo_da_un_anno_viene_dichiarato_tale(tmp_path, monkeypatch):
    """Il caso che il solo tiro verso la media non vedrebbe.

    Il decadimento temporale e' relativo alla partita piu' recente del
    campione: un archivio fermo da un anno ha pesi identici a uno di ieri, e
    la barra da sola direbbe "stima solida". La data dell'ultima partita e'
    l'unica cosa che smaschera la fotografia vecchia, e deve comparire.
    """
    settings = Settings(
        api_key="chiave-di-prova", api_provider="apisports", daily_request_limit=100, api_min_interval_seconds=0.0,
        cache_dir=tmp_path / "cache", budget_file=tmp_path / "budget.json",
        store_dir=tmp_path / "serie_a", storico_dir=tmp_path / "storico",
        importato_dir=tmp_path / "importato",
        journal_file=tmp_path / "journal.json",
        predictions_db=tmp_path / "pronostici.db", database_url=None,
        league_id=135, seasons=(),
    )
    eng = Engine(settings)

    # Lo stesso campionato finto, ma giocato piu' di un anno fa.
    vecchie = []
    for grezza in _campionato_finto():
        quando = datetime.fromisoformat(grezza["fixture"]["date"])
        if grezza["goals"]["home"] is None:
            continue  # le partite in programma restano nel futuro
        grezza = {**grezza, "fixture": {
            **grezza["fixture"], "date": (quando - timedelta(days=500)).isoformat()
        }}
        vecchie.append(grezza)
    vecchie.append(_raw(9800, 1, 6, ADESSO + timedelta(days=2), giornata=20))
    eng.store.upsert_fixtures(vecchie)

    monkeypatch.setattr(app_module, "_engine", eng)
    r = TestClient(app_module.app).get("/partita/9800")

    assert r.status_code == 200
    assert "giorni fa" in r.text
    assert "dati vecchi" in r.text


def test_un_archivio_fresco_non_grida_al_lupo(client):
    """L'avviso sulla vecchiaia compare solo quando c'e' vecchiaia.

    Un avviso che c'e' sempre non lo legge piu' nessuno, ed e' esattamente
    cosi' che un limite dichiarato torna a essere un limite nascosto.
    """
    r = client.get("/partita/9001")
    assert "Su cosa si basa" in r.text
    assert "dati vecchi" not in r.text
    assert "giorni fa" not in r.text


# ---- stato e cache del modello ---------------------------------------------


def test_lo_stato_riporta_numeri_coerenti(client, motore):
    stato = client.get("/stato").json()
    assert stato["modello_pronto"] is True
    assert stato["partite_stagione_corrente"] == len(motore.store.fixture_ids())
    assert stato["squadre"] == len(SQUADRE)


def test_il_modello_non_viene_ristimato_a_ogni_richiesta(motore):
    """Stimare costa secondi: due visite di fila devono usare lo stesso oggetto."""
    primo = motore.snapshot()
    secondo = motore.snapshot()
    assert primo is secondo


def test_il_modello_si_ricostruisce_quando_l_archivio_cambia(motore):
    primo = motore.snapshot()
    motore.store.upsert_fixtures([
        _raw(9600, 3, 4, ADESSO - timedelta(days=1), gol=(5, 0))
    ])
    secondo = motore.snapshot()

    assert secondo is not primo
    assert secondo.model.n_matches == primo.model.n_matches + 1


def test_aggiorna_forza_la_ricostruzione(client, motore):
    primo = motore.snapshot()
    r = client.post("/aggiorna", follow_redirects=False)
    assert r.status_code == 303
    assert motore.snapshot() is not primo


def test_aggiorna_non_si_esegue_con_un_collegamento(client):
    """Ricostruire scrive: pronostici registrati, esiti risolti.

    Un GET lo segue da solo il precaricamento del browser, e le righe che
    scrive non si cancellano. Deve essere un POST, cioe' qualcosa che
    succede solo se qualcuno preme.
    """
    assert client.get("/aggiorna").status_code == 405
    assert 'action="/aggiorna"' in client.get("/").text


# ---- storico dei pronostici -------------------------------------------------


def test_i_pronostici_delle_partite_in_programma_vengono_registrati(motore):
    """Se non restassero scritti da qualche parte, non sarebbero misurabili."""
    motore.snapshot()

    assert motore.pronostici.conteggi()["pronostici"] == 2
    for fixture_id in (9001, 9002):
        riga = motore.pronostici.ultimo_per_partita(fixture_id)
        assert riga is not None, f"partita {fixture_id} senza pronostico"
        assert riga["lambda_home"] > 0


def test_ricaricare_le_pagine_non_moltiplica_i_pronostici(motore):
    """L'hub rigenera i pronostici a ogni visita: lo storico non deve gonfiarsi."""
    motore.snapshot()
    for _ in range(5):
        motore.snapshot(force=True)

    assert motore.pronostici.conteggi()["pronostici"] == 2


def test_le_partite_gia_giocate_non_finiscono_nello_storico(motore):
    """Il modello e' stato stimato su quelle: pronosticarle sarebbe barare."""
    motore.snapshot()

    with motore.pronostici._connessione() as conn:
        righe = conn.execute("select fixture_id from predictions").fetchall()
    assert {r["fixture_id"] for r in righe} == {9001, 9002}


def test_il_ciclo_si_chiude_quando_la_partita_finisce(motore):
    """Pronostico prima, risultato dopo, esito misurato: il giro completo."""
    motore.snapshot()
    assert motore.pronostici.conteggi()["esiti_risolti"] == 0
    attesi = len(motore.pronostici.mercati_da_risolvere(9001))
    assert attesi > 0

    # La partita 9001 si gioca e finisce 2-0.
    motore.store.upsert_fixtures([_raw(9001, 1, 6, ADESSO + timedelta(days=2), gol=(2, 0))])
    motore.snapshot(force=True)

    esiti = motore.pronostici.esiti_risolti()
    assert len(esiti) == attesi
    assert {e["fixture_id"] for e in esiti} == {9001}
    # L'Inter ha vinto in casa: fra i tre esiti dell'1X2 ne risulta uno solo.
    uno_x_due = [e for e in esiti if e["market"] == "1x2"]
    assert sum(1 for e in uno_x_due if e["hit"]) == 1
    assert next(e["selection"] for e in uno_x_due if e["hit"]) == "home"


def test_l_hub_mostra_l_accuratezza_solo_quando_c_e_qualcosa_da_dire(motore, monkeypatch):
    motore.snapshot()
    monkeypatch.setattr(app_module, "_engine", motore)
    client = TestClient(app_module.app)

    assert "Come stanno andando i pronostici" not in client.get("/").text

    motore.store.upsert_fixtures([_raw(9001, 1, 6, ADESSO + timedelta(days=2), gol=(2, 0))])
    motore.snapshot(force=True)
    pagina = client.get("/").text

    assert "Come stanno andando i pronostici" in pagina
    # Con una sola partita misurata il verdetto deve dirlo, non dare numeri secchi.
    assert "troppi pochi dati" in pagina


# ---- giocatori --------------------------------------------------------------
#
# Le pagine dei giocatori leggono i dettagli salvati accanto alle partite. Nel
# campionato finto non ce ne sono: si aggiungono a mano, nella forma in cui la
# raccolta li scrive.

PARTITA_CON_DETTAGLI = 1001   # Inter - Milan, la prima del girone finto


def _tabellino_finto():
    return [
        {"team": {"id": 1, "name": "Inter"}, "players": [
            {"player": {"id": 10, "name": "Bomber"},
             "statistics": [{"games": {"minutes": 90, "number": 9, "position": "F",
                                       "rating": "8.0", "substitute": False},
                             "shots": {"total": 4, "on": 2},
                             "goals": {"total": 2, "assists": 1},
                             "cards": {"yellow": 0, "red": 0}}]},
            {"player": {"id": 11, "name": "Mai Entrato"},
             "statistics": [{"games": {"minutes": None, "number": 23, "position": "M",
                                       "rating": None, "substitute": True},
                             "shots": {"total": None, "on": None},
                             "goals": {"total": None, "assists": None},
                             "cards": {"yellow": None, "red": None}}]},
        ]},
        {"team": {"id": 2, "name": "Milan"}, "players": [
            {"player": {"id": 20, "name": "Difensore"},
             "statistics": [{"games": {"minutes": 90, "number": 4, "position": "D",
                                       "rating": "5.5", "substitute": False},
                             "shots": {"total": 0, "on": 0},
                             "goals": {"total": None, "assists": None},
                             "cards": {"yellow": 1, "red": 0}}]},
        ]},
    ]


def _formazioni_finte():
    return [{
        "team": {"id": 1, "name": "Inter"},
        "formation": "3-5-2",
        "coach": {"id": 1, "name": "Un Allenatore"},
        "startXI": [{"player": {"id": 10, "name": "Bomber", "number": 9, "pos": "F", "grid": "4:1"}}],
        "substitutes": [{"player": {"id": 11, "name": "Mai Entrato", "number": 23, "pos": "M", "grid": None}}],
    }]


@pytest.fixture
def con_dettagli(motore):
    motore.store.save_detail(PARTITA_CON_DETTAGLI, "players", _tabellino_finto())
    motore.store.save_detail(PARTITA_CON_DETTAGLI, "lineups", _formazioni_finte())
    motore.store.save_detail(PARTITA_CON_DETTAGLI, "events", [
        {"time": {"elapsed": 22, "extra": None},
         "team": {"id": 1, "name": "Inter"},
         "player": {"id": 10, "name": "Bomber"},
         "assist": {"id": None, "name": None},
         "type": "Goal", "detail": "Normal Goal"},
    ])
    return motore


def test_la_pagina_giocatori_dice_quando_non_sa_niente(client):
    """Senza tabellini si spiega perche', non si mostra una tabella vuota."""
    r = client.get("/giocatori")

    assert r.status_code == 200
    assert "Nessuna statistica per giocatore" in r.text


def test_la_pagina_giocatori_mostra_i_marcatori(con_dettagli, client):
    r = client.get("/giocatori")

    assert r.status_code == 200
    assert "Bomber" in r.text
    assert "Marcatori" in r.text


def test_la_pagina_giocatori_dichiara_l_annata(con_dettagli, client):
    """Una classifica senza annata sopra sarebbe la somma di quattro campionati.

    Fino al 9 settembre 2026 lo diceva un paragrafo; ora lo dice l'annata
    scritta in cima, che e' il dato e non il discorso. Il test chiede quello:
    che la pagina nomini la stagione di cui sta parlando.
    """
    import re

    r = client.get("/giocatori")

    assert r.status_code == 200
    assert re.search(r"stagione \d{4}/\d{2}", r.text)


def test_un_annata_inventata_non_e_un_guasto(con_dettagli, client):
    """E' un indirizzo scritto a mano: si mostra la piu' recente, non un 404."""
    r = client.get("/giocatori?stagione=1999")

    assert r.status_code == 200
    assert "Marcatori" in r.text


def test_la_scheda_giocatore_ha_totali_e_partite(con_dettagli, client):
    r = client.get("/giocatore/10")

    assert r.status_code == 200
    assert "Bomber" in r.text
    assert "Partita per partita" in r.text
    assert "attaccante" in r.text


def test_chi_non_e_mai_sceso_in_campo_non_ha_scheda(con_dettagli, client):
    """Un panchinaro inutilizzato non e' un giocatore che l'archivio conosce."""
    assert client.get("/giocatore/11").status_code == 404


def test_giocatore_inesistente(client):
    assert client.get("/giocatore/424242").status_code == 404


def test_la_scheda_squadra_mostra_chi_e_sceso_in_campo(con_dettagli, client):
    r = client.get("/squadra/1")

    assert r.status_code == 200
    assert "Bomber" in r.text
    assert "Ultimi risultati" in r.text


def test_la_scheda_squadra_regge_senza_statistiche(client):
    """Una squadra c'e' anche prima che esistano i tabellini: si dice cosi'."""
    r = client.get("/squadra/1")

    assert r.status_code == 200
    assert "Nessuna statistica per giocatore" in r.text


def test_squadra_inesistente(client):
    assert client.get("/squadra/424242").status_code == 404


def test_la_scheda_partita_mostra_le_formazioni(con_dettagli, client):
    r = client.get(f"/partita/{PARTITA_CON_DETTAGLI}")

    assert r.status_code == 200
    assert "Formazioni" in r.text
    assert "3-5-2" in r.text
    assert "Un Allenatore" in r.text
    assert 'href="/giocatore/10"' in r.text


def test_la_scheda_partita_dichiara_il_tabellino_non_definitivo(motore, client):
    """Pagelle vuote hanno due cause diverse, e vanno dette diverse.

    Se il tabellino manca del tutto la pagina lo dice gia'. Se invece c'e' ma
    e' la fotografia del feed live — come per Genoa-Napoli del 22 agosto 2026 —
    le colonne restano vuote lo stesso, e senza una riga sembrerebbe una
    raccolta mancata invece di un dato che si sistema da solo.
    """
    motore.store.save_detail(PARTITA_CON_DETTAGLI, "lineups", _formazioni_finte())
    motore.store.save_detail(PARTITA_CON_DETTAGLI, "players", [
        {"team": {"id": 1, "name": "Inter"}, "players": [
            {"player": {"id": 10, "name": "Bomber"},
             "statistics": [{"games": {"minutes": 64, "number": 9, "position": "F",
                                       "rating": "6.5", "substitute": False},
                             "shots": {"total": None, "on": None},
                             "goals": {"total": None, "assists": None},
                             "cards": {"yellow": None, "red": None}}]},
        ]},
    ])

    r = client.get(f"/partita/{PARTITA_CON_DETTAGLI}")

    assert r.status_code == 200
    assert "non ancora definitivo" in r.text


def test_un_tabellino_buono_non_fa_comparire_l_avviso(con_dettagli, client):
    r = client.get(f"/partita/{PARTITA_CON_DETTAGLI}")

    assert "ancora definitivo" not in r.text


def _tabellino_fermo():
    """Un tabellino provvisorio: nessuno arriva in fondo, e i gol non ci sono."""
    return [
        {"team": {"id": 1, "name": "Inter"}, "players": [
            {"player": {"id": 10, "name": "Bomber"},
             "statistics": [{"games": {"minutes": 64, "number": 9, "position": "F",
                                       "rating": "6.5", "substitute": False},
                             "shots": {"total": None, "on": None},
                             "goals": {"total": None, "assists": None},
                             "cards": {"yellow": None, "red": None}}]},
        ]},
    ]


def test_col_tabellino_fermo_la_panchina_non_dichiara_chi_non_e_entrato(motore, client):
    """"Non lo so" non si scrive "non entrato": e' una bugia verificabile.

    In Genoa-Napoli del 22 agosto 2026 il tabellino era fermo al 64' e la
    panchina diceva "non entrato" accanto a De Bruyne e Vergara, cioe' ai due
    che erano entrati al 67' e avevano segnato entrambi i gol elencati dalla
    stessa pagina poche righe sopra.
    """
    motore.store.save_detail(PARTITA_CON_DETTAGLI, "lineups", _formazioni_finte())
    motore.store.save_detail(PARTITA_CON_DETTAGLI, "players", _tabellino_fermo())

    r = client.get(f"/partita/{PARTITA_CON_DETTAGLI}")

    assert r.status_code == 200
    assert "Mai Entrato" in r.text        # il nome resta: quello lo sappiamo
    assert "non entrato" not in r.text    # se sia entrato, no


def test_senza_tabellino_la_panchina_non_dichiara_chi_non_e_entrato(motore, client):
    """Stessa ignoranza, altra causa: il tabellino non e' mai stato raccolto."""
    motore.store.save_detail(PARTITA_CON_DETTAGLI, "lineups", _formazioni_finte())

    r = client.get(f"/partita/{PARTITA_CON_DETTAGLI}")

    assert r.status_code == 200
    assert "Mai Entrato" in r.text
    assert "non entrato" not in r.text


def test_col_tabellino_buono_la_panchina_dichiara_chi_non_e_entrato(con_dettagli, client):
    """Quando il tabellino c'e', un panchinaro che non compare non e' entrato.

    L'endpoint elenca anche i subentrati mai usati, coi minuti a ``null``:
    la loro assenza dalle prestazioni e' un fatto, non un buco.
    """
    r = client.get(f"/partita/{PARTITA_CON_DETTAGLI}")

    assert r.status_code == 200
    assert "non entrato" in r.text


def test_la_scheda_partita_dichiara_le_formazioni_mancanti(client):
    r = client.get(f"/partita/{PARTITA_CON_DETTAGLI}")

    assert "Nessuna formazione nell'archivio" in r.text


def test_l_archivio_dei_giocatori_si_rifa_quando_arrivano_i_dettagli(motore):
    """I dettagli arrivano dopo le partite: l'impronta dell'archivio non basta."""
    prima = motore.giocatori()
    assert prima.vuoto

    motore.store.save_detail(PARTITA_CON_DETTAGLI, "players", _tabellino_finto())
    dopo = motore.giocatori()

    assert dopo is not prima
    assert dopo.giocatori_conosciuti == 2


def test_l_archivio_dei_giocatori_non_si_ricostruisce_a_ogni_pagina(con_dettagli):
    """Leggere un file per partita conclusa non deve pesare su ogni richiesta."""
    assert con_dettagli.giocatori() is con_dettagli.giocatori()


def test_lo_stato_conta_pronostici_ed_esiti(motore, monkeypatch):
    motore.snapshot()
    motore.store.upsert_fixtures([_raw(9001, 1, 6, ADESSO + timedelta(days=2), gol=(2, 0))])
    motore.snapshot(force=True)

    monkeypatch.setattr(app_module, "_engine", motore)
    stato = TestClient(app_module.app).get("/stato").json()

    # Tre e non due: il risultato nuovo ha fatto ristimare il modello, e la
    # partita ancora da giocare ha ricevuto un pronostico aggiornato accanto
    # al precedente. E' il comportamento voluto — lo storico tiene entrambi.
    assert stato["pronostici_registrati"] == 3
    assert stato["esiti_risolti"] > 0


# ---- stagioni ricomprate ----------------------------------------------------


def _stagione_ricomprata(motore, fid=7001, quando=None):
    """Scrive una partita in `data/storico/2025/`, come farebbe il riempimento."""
    from serie_a_analyzer.data.storico import store_stagione

    store = store_stagione(motore.settings.storico_dir, 2025)
    store.upsert_fixtures([_raw(fid, 1, 2, quando or (ADESSO - timedelta(days=300)), gol=(3, 0))])
    return store


def test_una_stagione_ricomprata_entra_nel_modello_senza_riavvio(motore):
    """Il riempimento dura ore e il sito resta aperto: deve accorgersene da solo.

    L'impronta guardata dal motore comprende `data/storico/`, non solo
    l'archivio della raccolta giornaliera: senza, il modello resterebbe fermo
    alle stagioni di prima anche dopo aver ricomprato un campionato intero.
    """
    prima = motore.snapshot()
    _stagione_ricomprata(motore)

    dopo = motore.snapshot()

    assert dopo is not prima
    assert dopo.dataset.n_riempite == 1
    assert dopo.dataset.stagioni_riempite == [2025]
    assert dopo.model.n_matches == prima.model.n_matches + 1


def test_il_pie_di_pagina_dichiara_le_stagioni_ricomprate(motore, monkeypatch):
    """Compaiono solo quando ci sono: sul piano gratuito non esistono."""
    monkeypatch.setattr(app_module, "_engine", motore)
    client = TestClient(app_module.app)
    assert "stagioni ricomprate" not in client.get("/").text

    _stagione_ricomprata(motore)

    assert "stagioni ricomprate (2025)" in client.get("/").text


def test_i_tabellini_di_una_stagione_ricomprata_contano_nelle_schede(motore):
    """Un giocatore ha una carriera sola, comunque sia stata pagata."""
    store = _stagione_ricomprata(motore, fid=7002)
    store.save_detail(7002, "players", _tabellino_finto())

    archivio = motore.giocatori()

    assert archivio.giocatori_conosciuti == 2
    assert archivio.partite_con_statistiche == 1


# ---- indisponibili ---------------------------------------------------------


def test_la_scheda_non_parla_di_assenze_se_non_ne_conosce(client):
    """Nessuna lettura in archivio: la sezione non compare per niente.

    Una sezione "Indisponibili" vuota si leggerebbe come "non manca nessuno",
    che e' un'informazione — e sarebbe inventata.
    """
    r = client.get("/partita/9001")
    assert r.status_code == 200
    assert "Indisponibili" not in r.text


def test_la_scheda_mostra_assenti_e_in_dubbio_separati(client, motore):
    from datetime import timedelta

    from serie_a_analyzer.data.indisponibili import ASSENTE, IN_DUBBIO, Indisponibile

    fixture = motore.snapshot().dataset.by_id[9001]
    voci = [
        Indisponibile(9001, 111, "Tizio Squalificato", fixture.home_team_id,
                      "Casa", ASSENTE, "squalifica", "Suspended"),
        Indisponibile(9001, 222, "Caio Acciaccato", fixture.away_team_id,
                      "Ospite", IN_DUBBIO, "infortunio", "Thigh Injury"),
    ]
    motore.pronostici.salva_indisponibili(
        fixture_id=9001, kickoff_utc=fixture.kickoff_utc, voci=voci,
        now=fixture.kickoff_utc - timedelta(hours=3),
    )

    r = client.get("/partita/9001")
    assert "Indisponibili" in r.text
    assert "Tizio Squalificato" in r.text
    assert "squalificato" in r.text
    assert "In dubbio" in r.text
    assert "Caio Acciaccato" in r.text
    # E la pagina deve dire che il pronostico non ne tiene conto.
    assert "non tiene conto" in r.text


def test_un_assente_senza_scheda_e_un_nome_non_un_collegamento(client, motore):
    """Un infortunato che l'archivio non ha mai visto giocare non ha pagina.

    Collegarlo lo stesso significa offrire un 404, ed e' il caso normale e non
    l'eccezione: gli infortunati sono per definizione gente che non e' scesa
    in campo. Il nome resta — quella e' l'informazione — e sparisce solo la
    promessa di poterci cliccare sopra.
    """
    from datetime import timedelta

    from serie_a_analyzer.data.indisponibili import ASSENTE, Indisponibile

    fixture = motore.snapshot().dataset.by_id[9001]
    motore.pronostici.salva_indisponibili(
        fixture_id=9001, kickoff_utc=fixture.kickoff_utc,
        voci=[Indisponibile(9001, 4242, "Sempronio Mai Visto", fixture.home_team_id,
                            "Casa", ASSENTE, "infortunio", "Injury")],
        now=fixture.kickoff_utc - timedelta(hours=3),
    )

    r = client.get("/partita/9001")
    assert "Sempronio Mai Visto" in r.text
    assert 'href="/giocatore/4242"' not in r.text
    assert client.get("/giocatore/4242").status_code == 404


def test_una_lettura_dopo_il_fischio_non_finisce_nella_scheda(client, motore):
    """Sapere chi non e' sceso in campo e' un'altra cosa dal saperlo prima."""
    from datetime import timedelta

    from serie_a_analyzer.data.indisponibili import ASSENTE, Indisponibile

    fixture = motore.snapshot().dataset.by_id[9001]
    motore.pronostici.salva_indisponibili(
        fixture_id=9001, kickoff_utc=fixture.kickoff_utc,
        voci=[Indisponibile(9001, 111, "Tizio Tardivo", fixture.home_team_id,
                            "Casa", ASSENTE, "infortunio", "Injury")],
        now=fixture.kickoff_utc + timedelta(hours=2),
    )

    r = client.get("/partita/9001")
    assert "Tizio Tardivo" not in r.text


# ---- i due giudizi -----------------------------------------------------------
#
# Nell'elenco degli ultimi risultati le due palline in fondo alla riga
# rispondono a due domande diverse: il segno accaduto era quello che il
# modello preferiva, e il punteggio era fra quelli nominati in pagina. Fino
# al 6 settembre 2026 era una pastiglia sola a tre gradini, e li' i due
# giudizi si pestavano — vedi
# ``test_il_punteggio_giusto_col_segno_sbagliato_e_mezzo_verdetto``.
# I test lavorano su matrici costruite a mano perche' e' l'unico modo di
# decidere quali punteggi siano i probabili invece di sperarci.


def _probabilita(celle):
    """Un ``MatchProbabilities`` da una manciata di celle {(casa, ospite): p}."""
    import numpy as np

    from serie_a_analyzer.model.markets import derive_markets

    matrice = np.zeros((5, 5))
    for (casa, ospite), p in celle.items():
        matrice[casa][ospite] = p
    return derive_markets(matrice)


def _riga(prob, gol_casa, gol_ospite):
    from serie_a_analyzer.web.views import RigaPartita

    return RigaPartita(
        id=1, casa="Inter", ospite="Lecce", casa_id=1, ospite_id=6,
        quando="sabato 22 ago, 18:30", giornata=1, conclusa=True,
        gol_casa=gol_casa, gol_ospite=gol_ospite, prob=prob,
    )


# Il modello dava 2-1 come punteggio piu' probabile e la vittoria interna come
# esito: 0.55 di 1, 0.20 di X, 0.25 di 2.
_DAVA_2_1 = {
    (2, 1): 0.25, (1, 0): 0.18, (2, 0): 0.12,
    (1, 1): 0.12, (0, 0): 0.08,
    (0, 1): 0.14, (1, 2): 0.11,
}


def test_il_punteggio_nominato_accende_tutte_e_due_le_palline():
    riga = _riga(_probabilita(_DAVA_2_1), 2, 1)
    assert riga.giudicabile is True
    assert riga.pronostico_azzeccato is True
    assert riga.risultato_azzeccato is True


def test_azzeccare_solo_chi_vince_accende_una_pallina_sola():
    """Vittoria interna si', ma 3-0 non e' il 2-1 che il modello aveva nominato."""
    riga = _riga(_probabilita(_DAVA_2_1), 3, 0)
    assert riga.pronostico_azzeccato is True
    assert riga.risultato_azzeccato is False


def test_l_esito_sbagliato_le_spegne_tutte_e_due():
    riga = _riga(_probabilita(_DAVA_2_1), 0, 2)
    assert riga.pronostico_azzeccato is False
    assert riga.risultato_azzeccato is False


def test_senza_pronostico_non_si_giudica_niente():
    """Il modello non conosceva le squadre: non c'e' niente da giudicare."""
    riga = _riga(None, 2, 1)
    assert riga.giudicabile is False
    assert riga.pronostico_azzeccato is None
    assert riga.risultato_azzeccato is None


def test_una_partita_da_giocare_non_si_giudica():
    from serie_a_analyzer.web.views import RigaPartita

    riga = RigaPartita(
        id=1, casa="Inter", ospite="Lecce", casa_id=1, ospite_id=6,
        quando="sabato 22 ago, 18:30", giornata=1, conclusa=False,
        gol_casa=None, gol_ospite=None, prob=_probabilita(_DAVA_2_1),
    )
    assert riga.giudicabile is False


def test_il_punteggio_giusto_col_segno_sbagliato_e_mezzo_verdetto():
    """Il caso che ha fatto nascere le due colonne, il 6 settembre 2026.

    E' Juventus-Milan: il modello dava l'1 al 45% e nominava 1-0 e 1-1 fra i
    punteggi probabili, la partita e' finita 1-1. Il punteggio era fra quelli
    detti, il segno no. La pastiglia unica di allora guardava il punteggio per
    primo e scriveva "azzeccato" in verde su una riga in cui meta' del
    giudizio era sbagliata; adesso sono due palline e dicono cose diverse.

    Qui sotto la stessa forma su una matrice costruita a mano: il punteggio
    piu' probabile e' 1-1, ma sommando *tutte* le vittorie interne si supera
    la somma di *tutti* i pareggi, quindi il favorito e' l'1 mentre il
    risultato nominato e' un pareggio.
    """
    prob = _probabilita({
        (1, 1): 0.20, (0, 0): 0.05, (2, 2): 0.05,      # pareggi: 0.30
        (1, 0): 0.12, (2, 0): 0.12, (2, 1): 0.12, (3, 0): 0.06,  # casa: 0.42
        (0, 1): 0.16, (0, 2): 0.12,                    # trasferta: 0.28
    })
    riga = _riga(prob, 1, 1)

    assert riga.esito_previsto == "1"
    assert riga.pronostico_azzeccato is False   # il segno: sbagliato
    assert riga.risultato_azzeccato is True     # il punteggio: azzeccato
    assert riga.giudicabile is True


# ---- i punteggi nominati in pagina -----------------------------------------

# Il modello da' 1-0 al 12% e 2-0 al 10.3%: due celle vicine, non due
# pronostici. Sotto, il terzo si ferma al 9% e resta fuori.
_DUE_SOPRA_LA_SOGLIA = {
    (1, 0): 0.12, (2, 0): 0.103, (2, 1): 0.09, (3, 0): 0.07, (3, 1): 0.06,
    (0, 0): 0.08, (1, 1): 0.09, (2, 2): 0.06,
    (0, 1): 0.09, (1, 2): 0.08, (0, 2): 0.07, (0, 3): 0.047, (1, 3): 0.04,
}


def test_si_nominano_tutti_i_punteggi_sopra_il_dieci_per_cento():
    riga = _riga(_probabilita(_DUE_SOPRA_LA_SOGLIA), None, None)
    assert riga.risultati_probabili == "1-0/2-0"
    assert riga.piu_di_un_risultato_probabile is True


def test_l_ordine_e_quello_della_probabilita():
    """Il piu' probabile per primo, sempre: e' l'unica lettura che l'occhio fa."""
    prob = _probabilita({
        (2, 0): 0.16, (1, 0): 0.12, (0, 0): 0.09, (1, 1): 0.09, (2, 1): 0.09,
        (3, 0): 0.09, (0, 1): 0.09, (1, 2): 0.09, (0, 2): 0.09, (2, 2): 0.09,
    })
    assert prob.most_likely_scoreline == (2, 0)
    riga = _riga(prob, None, None)
    assert riga.risultati_probabili == "2-0/1-0"


def test_un_solo_punteggio_sopra_la_soglia_resta_al_singolare():
    prob = _probabilita({
        (1, 0): 0.16, (1, 1): 0.09, (0, 0): 0.09, (2, 0): 0.09, (2, 1): 0.09,
        (0, 1): 0.09, (1, 2): 0.09, (0, 2): 0.09, (2, 2): 0.09, (3, 0): 0.06,
        (3, 1): 0.06,
    })
    riga = _riga(prob, None, None)
    assert riga.risultati_probabili == "1-0"
    assert riga.piu_di_un_risultato_probabile is False


def test_se_nessuno_arriva_alla_soglia_si_torna_al_piu_probabile():
    """Mai uno spazio bianco: si leggerebbe come "il modello non sa dirlo".

    E' falso — la matrice c'e', e' solo piu' larga del solito — ed e' la
    differenza che il progetto tiene ovunque fra "non lo so" e "non ce n'e'
    abbastanza". Qui non lo sappiamo *meno* del solito: lo sappiamo diviso su
    piu' celle.
    """
    prob = _probabilita({
        (1, 1): 0.095, (1, 0): 0.085, (0, 1): 0.08, (2, 1): 0.08, (1, 2): 0.08,
        (0, 0): 0.07, (2, 0): 0.07, (0, 2): 0.07, (2, 2): 0.07,
        (3, 1): 0.06, (1, 3): 0.06, (3, 0): 0.06, (0, 3): 0.06, (3, 2): 0.06,
    })
    riga = _riga(prob, None, None)
    assert max(p for _, p in prob.top_scorelines) < 0.10
    assert riga.risultati_probabili == "1-1"
    assert riga.piu_di_un_risultato_probabile is False


def test_senza_pronostico_non_si_nomina_niente():
    riga = _riga(None, None, None)
    assert riga.risultati_probabili == ""


def test_il_verde_del_punteggio_vale_per_ogni_punteggio_nominato():
    """La regola, cambiata il 3 settembre 2026 su richiesta dell'utente.

    In pagina l'hub scrive "1-0/2-0": due punteggi, tutti e due sopra il 10%,
    messi li' apposta per dire che fra quelli il modello non sta scegliendo.
    Una partita finita 2-0 e' quindi finita su un punteggio che la pagina
    aveva scritto, e la pallina del punteggio lo chiama azzeccato — anche se
    il primo della lista era un altro. Prima contava solo il primo.
    """
    prob = _probabilita(_DUE_SOPRA_LA_SOGLIA)
    riga = _riga(prob, 2, 0)

    assert riga.risultati_probabili == "1-0/2-0"   # cio' che l'hub mostra
    assert riga.risultato_probabile == "1-0"       # il primo della lista
    assert riga.risultato_azzeccato is True


def test_un_punteggio_sotto_la_soglia_non_e_azzeccato():
    """L'altra meta' della regola, senza la quale il verdetto direbbe sempre si'.

    Il 2-1 vale il 9% e in pagina non compare: la partita finita 2-1 ha visto
    il modello indovinare chi vinceva e nient'altro. Se bastasse una cella
    qualsiasi della matrice la pallina verde non vorrebbe piu' dire niente —
    la matrice contiene ogni punteggio possibile.
    """
    riga = _riga(_probabilita(_DUE_SOPRA_LA_SOGLIA), 2, 1)

    assert "2-1" not in riga.risultati_probabili
    assert riga.risultato_azzeccato is False
    assert riga.pronostico_azzeccato is True


def test_il_verdetto_guarda_gli_stessi_punteggi_che_la_pagina_mostra():
    """La guardia che conta adesso: una lista sola, letta da due parti.

    Anteprima e pallina devono leggere la STESSA cosa, altrimenti la pagina
    nomina un punteggio e poi lo chiama sbagliato. Vale anche nel caso di
    ripiego, quando nessuna cella arriva alla soglia e in pagina resta il solo
    piu' probabile: li' il verdetto deve stringersi insieme all'anteprima.
    """
    sotto_soglia = {
        (1, 1): 0.095, (1, 0): 0.085, (0, 1): 0.08, (2, 1): 0.08, (1, 2): 0.08,
        (0, 0): 0.07, (2, 0): 0.07, (0, 2): 0.07, (2, 2): 0.07,
        (3, 1): 0.06, (1, 3): 0.06, (3, 0): 0.06, (0, 3): 0.06,
    }
    prob = _probabilita(sotto_soglia)

    assert _riga(prob, 1, 1).risultati_probabili == "1-1"
    assert _riga(prob, 1, 1).risultato_azzeccato is True
    # 1-0 e' il secondo della matrice ma la pagina non lo nomina: non conta.
    assert _riga(prob, 1, 0).risultato_azzeccato is False


def test_l_hub_scrive_i_punteggi_probabili(client):
    r = client.get("/")
    assert "Piu' probabil" in r.text


# ---- l'esito scelto fra 1, X e 2 ---------------------------------------------
#
# Non e' un mercato nuovo: e' la stessa somma di celle che tinge il favorito
# nella scheda squadra e che decide la pallina dell'esito, scritta a parole
# accanto ai punteggi probabili. Le righe qui sotto esistono perche'
# resti UNA somma sola: due argmax scritti in due posti sarebbero uguali oggi
# e divergerebbero al primo ritocco.


def test_l_esito_scelto_e_il_piu_probabile_dei_tre():
    """1 vale 0.55, X 0.20, 2 0.25: la pagina scrive 1."""
    riga = _riga(_probabilita(_DAVA_2_1), 2, 1)
    assert riga.esito_previsto == "1"
    assert riga.probabilita_esito_previsto == pytest.approx(riga.prob.home_win)


def test_la_probabilita_scritta_e_quella_dell_esito_scritto():
    """Le due cose viaggiano insieme, e devono parlare della stessa lettera."""
    prob = _probabilita({
        (0, 1): 0.22, (0, 2): 0.18, (1, 2): 0.14,      # trasferta: 0.54
        (1, 1): 0.14, (0, 0): 0.10,                    # pareggio:  0.24
        (1, 0): 0.12, (2, 1): 0.10,                    # casa:      0.22
    })
    riga = _riga(prob, 0, 1)
    scelta = {"1": prob.home_win, "X": prob.draw, "2": prob.away_win}
    assert riga.esito_previsto == "2"
    assert riga.probabilita_esito_previsto == pytest.approx(scelta["2"])


def test_senza_pronostico_non_si_scrive_nessun_esito():
    """"Non lo so" non deve diventare una lettera, come sempre in questo progetto."""
    from serie_a_analyzer.web.views import RigaPartita

    riga = RigaPartita(
        id=1, casa="Inter", ospite="Lecce", casa_id=1, ospite_id=6,
        quando="sabato 22 ago, 18:30", giornata=1, conclusa=False,
        gol_casa=None, gol_ospite=None, prob=None,
    )
    assert riga.esito_previsto is None
    assert riga.probabilita_esito_previsto is None


def test_l_esito_scelto_e_lo_stesso_che_giudica_il_verdetto():
    """La lettera nominata in pagina e quella su cui si viene giudicati.

    Se le due potessero divergere, la pagina prometterebbe un esito e la
    pastiglia ne premierebbe un altro: e' la trappola gia' chiusa per i
    cinque punteggi, e vale identica qui.
    """
    prob = _probabilita(_DAVA_2_1)
    for gol_casa, gol_ospite in ((2, 1), (1, 1), (0, 2)):
        riga = _riga(prob, gol_casa, gol_ospite)
        assert riga.pronostico_azzeccato == (riga.esito_reale == riga.esito_previsto)


def test_l_hub_scrive_l_esito_scelto(client):
    r = client.get("/")
    assert "esito-scelto" in r.text
    assert ">Esito<" in r.text


def test_la_scheda_di_una_partita_da_giocare_scrive_l_esito_scelto(client, motore):
    futura = motore.snapshot().dataset.upcoming()[0]
    r = client.get(f"/partita/{futura.api_id}")
    assert "esito-scelto" in r.text
    assert "L'esito che ne esce" in r.text


# ---- la scelta sul Gol/NoGol -------------------------------------------------
#
# Accanto all'esito, e nella stessa forma, dal 7 settembre 2026. Non e' un
# mercato nuovo — e' la ``prob.btts`` che la scheda partita gia' mostrava —
# ma detta come scelta invece che come probabilita' secca, e una scelta ha
# bisogno di due guardie che una probabilita' non aveva: che la sigla e il
# numero parlino della stessa cosa, e che la pagina dica quanto poco il
# modello stia davvero scegliendo.


def test_il_gol_nogol_scelto_e_il_piu_probabile_dei_due():
    prob = _probabilita({
        (1, 1): 0.22, (2, 1): 0.14, (1, 2): 0.12,      # GG: 0.48
        (1, 0): 0.16, (0, 1): 0.14, (0, 0): 0.14,      # NG: 0.52 (con lo 0-0)
        (2, 0): 0.08,
    })
    riga = _riga(prob, 1, 0)

    assert riga.gol_nogol_previsto == "NG"
    assert riga.probabilita_gol_nogol_previsto == pytest.approx(prob.no_btts)


def test_il_numero_scritto_e_quello_della_sigla_scritta():
    """Le due cose viaggiano insieme e devono parlare della stessa risposta."""
    prob = _probabilita({
        (1, 1): 0.30, (2, 1): 0.18, (1, 2): 0.14,      # GG: 0.62
        (1, 0): 0.14, (0, 1): 0.12, (0, 0): 0.12,      # NG: 0.38
    })
    riga = _riga(prob, 1, 1)

    assert riga.gol_nogol_previsto == "GG"
    assert riga.probabilita_gol_nogol_previsto == pytest.approx(prob.btts)


def test_la_percentuale_del_gol_nogol_non_scende_mai_sotto_meta():
    """Il pavimento e' aritmetico, ed e' la ragione per cui va dichiarato.

    GG e NG sommano a uno: il prescelto e' il maggiore di due numeri che
    fanno cento, quindi parte dal 50%. E' lo stesso pavimento che ha fatto
    scartare la doppia chance, un piano piu' in basso — un numero che comincia
    da meta' sembra una decisione e non lo e'.
    """
    matrici = (_DAVA_2_1, _DUE_SOPRA_LA_SOGLIA)
    for celle in matrici:
        riga = _riga(_probabilita(celle), 1, 1)
        assert riga.probabilita_gol_nogol_previsto >= 0.5


def test_senza_pronostico_non_si_scrive_nessun_gol_nogol():
    """"Non lo so" non diventa una sigla, come sempre in questo progetto."""
    riga = _riga(None, 1, 1)
    assert riga.gol_nogol_previsto is None
    assert riga.probabilita_gol_nogol_previsto is None


def test_l_hub_scrive_il_gol_nogol_scelto(client):
    r = client.get("/")
    assert ">Gol/NoGol<" in r.text
    assert "GG &middot;" in r.text or "NG &middot;" in r.text


def test_il_limite_del_gol_nogol_e_dichiarato_dove_stanno_i_numeri(client):
    """Sui mercati dei gol il modello dichiara quasi sempre la media di lega.

    Fino al 9 settembre 2026 il limite stava scritto in due posti: sull'hub,
    accanto alla sigla, e nella scheda partita accanto all'Over/Under. Il
    primo se n'e' andato con le spiegazioni laterali, per decisione
    dell'utente — l'hub mostra la sigla e la sua percentuale, e il perche' sta
    in ISTRUZIONI.txt. Il secondo **resta**, ed e' quello che questo test
    protegge: nella pagina dove i mercati dei gol si leggono per esteso, il
    limite deve stare accanto ai numeri.

    Le due percentuali sull'hub non sono mute nemmeno da sole: il 52% scritto
    accanto a "NG" e' il modo in cui quella riga dice di non aver deciso
    niente, ed e' il motivo per cui la cifra non si puo' togliere.
    """
    partita = client.get("/partita/9001").text
    assert "media di campionato" in partita

    hub = client.get("/").text
    assert "Gol/NoGol" in hub, "premessa: la sigla c'e' ancora"


def test_il_gol_nogol_non_prende_l_alfabeto_degli_esiti(client):
    """Grafite, cenere e magenta vogliono dire casa, pareggio e trasferta.

    Tingere "NG" con una di quelle direbbe una terza cosa in un alfabeto che
    ne dice due: la sigla resta senza velo, ed e' l'unico modo di tenere in
    piedi la regola del colore che dice una cosa sola.
    """
    import re

    r = client.get("/")
    for riga in re.findall(r"<b[^>]*>[^<]*(?:GG|NG) &middot;", r.text):
        assert "esito-scelto" not in riga
        assert "tinta-" not in riga


def test_l_esito_scelto_di_una_partita_giocata_viene_dal_congelato(motore):
    """Dopo il fischio d'inizio la lettera esce dalla matrice del registro.

    Viene gratis perche' `esito_previsto` legge `self.prob`, che su una
    partita iniziata e' gia' quella congelata — ma "viene gratis" e' proprio
    il motivo per cui qualcuno potrebbe scriverne una seconda copia che
    interroga il modello caldo, ed e' quella che questo test impedisce.
    """
    _congela_il_passato(motore)
    snap = motore.snapshot()
    partita = snap.dataset.recent_results(limit=1)[0]

    riga, _ = app_module._riga(
        motore, partita, snap.dataset.name, datetime.now(timezone.utc)
    )

    registrato = motore.pronostici.ultimo_per_partita(partita.api_id)
    mercati = {
        m["selection"]: m["probability"]
        for m in motore.pronostici.mercati(registrato["id"])
        if m["market"] == "1x2"
    }
    atteso = {"home": "1", "draw": "X", "away": "2"}[max(mercati, key=mercati.get)]

    assert riga.congelato
    assert riga.esito_previsto == atteso
    assert riga.probabilita_esito_previsto == pytest.approx(max(mercati.values()))


def test_l_hub_disegna_le_due_palline(client_col_passato):
    """I cerchi ci sono, e la pastiglia a tre gradini non torna."""
    r = client_col_passato.get("/")
    assert VERDE in r.text or ROSSO in r.text
    # Il giudizio non prende l'alfabeto degli esiti: grafite, cenere e magenta
    # restano ai fatti (punteggio e casella accaduta), qui si giudica.
    assert "verdetto ok tinta-" not in r.text
    for morto in ("verdetto esatto", "verdetto esito", "verdetto niente"):
        assert morto not in r.text


# ---- il pronostico di una partita gia' giocata --------------------------------
#
# E' il bug che queste righe esistono per non far tornare. La scheda partita e
# l'hub ricalcolavano il pronostico col modello caldo anche sulle partite
# concluse; ma il modello viene ristimato su ogni risultato nuovo, quello della
# partita guardata compreso, e i numeri che ne uscivano erano un ricordo
# travestito da previsione. Su Venezia-Lecce del 23/08/2026 la pagina diceva
# 40-31-29 prima e 34-28-38 dopo, con la vittoria del Lecce cresciuta di nove
# punti a partita finita.


def test_una_partita_giocata_mostra_il_pronostico_congelato(motore):
    """I numeri in pagina sono quelli del registro, non un ricalcolo di oggi."""
    _congela_il_passato(motore)
    snap = motore.snapshot()
    partita = snap.dataset.recent_results(limit=1)[0]

    riga, congelato = app_module._riga(
        motore, partita, snap.dataset.name, datetime.now(timezone.utc)
    )

    assert riga.iniziata and riga.congelato
    assert congelato is not None

    registrato = motore.pronostici.ultimo_per_partita(partita.api_id)
    mercati = {
        m["selection"]: m["probability"]
        for m in motore.pronostici.mercati(registrato["id"])
        if m["market"] == "1x2"
    }
    assert riga.prob.home_win == pytest.approx(mercati["home"])
    assert riga.prob.draw == pytest.approx(mercati["draw"])
    assert riga.prob.away_win == pytest.approx(mercati["away"])


def test_una_partita_giocata_non_interroga_il_modello_caldo(motore, monkeypatch):
    """Il modello di oggi non viene proprio consultato, non solo ignorato.

    Confrontare il ricalcolo col congelato non basterebbe: finche' il modello
    non cambia i due numeri coincidono, e un test cosi' passerebbe anche col
    bug dentro. Qui la strada del ricalcolo viene minata — se la pagina ci
    passa, salta.
    """
    _congela_il_passato(motore)
    snap = motore.snapshot()
    partita = snap.dataset.recent_results(limit=1)[0]

    def vietato(*args, **kwargs):
        raise AssertionError("una partita gia' giocata non si ripronostica")

    monkeypatch.setattr(Engine, "predict", vietato)
    monkeypatch.setattr(Engine, "expected_goals", vietato)

    riga, congelato = app_module._riga(
        motore, partita, snap.dataset.name, datetime.now(timezone.utc)
    )
    assert riga.ha_pronostico and congelato is not None


def test_una_partita_giocata_senza_registro_non_inventa_un_pronostico(motore):
    """Meglio dire che non c'era, che mostrarne uno che ha visto il risultato."""
    snap = motore.snapshot()          # nessun `_congela_il_passato` qui
    partita = snap.dataset.recent_results(limit=1)[0]

    riga, congelato = app_module._riga(
        motore, partita, snap.dataset.name, datetime.now(timezone.utc)
    )

    assert congelato is None
    assert riga.prob is None
    assert riga.pronostico_perduto is True
    assert riga.ha_pronostico is False


def test_la_scheda_di_una_partita_giocata_dice_che_il_pronostico_e_congelato(
    client_col_passato, motore
):
    partita = motore.snapshot().dataset.recent_results(limit=1)[0]
    r = client_col_passato.get(f"/partita/{partita.api_id}")

    assert r.status_code == 200
    assert "congelate prima del fischio d'inizio" in r.text
    assert "Non vengono ricalcolate" in r.text


def test_la_scheda_distingue_le_due_assenze_di_pronostico(client, motore):
    """"Non lo so" e "nessuno lo chiese in tempo" non sono la stessa frase."""
    partita = motore.snapshot().dataset.recent_results(limit=1)[0]
    r = client.get(f"/partita/{partita.api_id}")

    assert "Nessun pronostico fu registrato" in r.text
    assert "non conosce almeno una delle due squadre" not in r.text


def test_la_griglia_dei_risultati_viene_dalla_matrice_congelata(client_col_passato, motore):
    """Percentuali congelate e griglia ricalcolata sarebbero due pronostici."""
    snap = motore.snapshot()
    partita = snap.dataset.recent_results(limit=1)[0]
    riga, congelato = app_module._riga(
        motore, partita, snap.dataset.name, datetime.now(timezone.utc)
    )

    assert congelato is not None
    # La casella piu' probabile della griglia e' quella del pronostico mostrato.
    (casa, ospite), _ = riga.prob.top_scorelines[0]
    assert congelato.matrice.argmax() == casa * congelato.matrice.shape[1] + ospite
    assert client_col_passato.get(f"/partita/{partita.api_id}").status_code == 200


def test_l_affidabilita_non_compare_su_una_partita_gia_giocata(client_col_passato, motore):
    """Misura il modello di adesso: accanto a un pronostico di allora inganna."""
    snap = motore.snapshot()
    futura = snap.dataset.upcoming()[0]
    passata = snap.dataset.recent_results(limit=1)[0]

    assert "Su cosa si basa" in client_col_passato.get(f"/partita/{futura.api_id}").text
    assert "Su cosa si basa" not in client_col_passato.get(f"/partita/{passata.api_id}").text


# ---- il cambio di allenatore ------------------------------------------------


def _lineups(casa, ospite, allenatore_casa, allenatore_ospite):
    return [
        {"team": {"id": casa, "name": _nome(casa)}, "formation": "4-3-3",
         "coach": {"id": allenatore_casa, "name": f"Mister {allenatore_casa}"},
         "startXI": [], "substitutes": []},
        {"team": {"id": ospite, "name": _nome(ospite)}, "formation": "4-3-3",
         "coach": {"id": allenatore_ospite, "name": f"Mister {allenatore_ospite}"},
         "startXI": [], "substitutes": []},
    ]


def _milan_cambia_allenatore(eng: Engine, quante: int = 4) -> None:
    """Le formazioni sul disco, col Milan che cambia panchina nelle ultime."""
    partite = sorted(eng.snapshot().dataset.finished, key=lambda f: f.kickoff_utc)
    del_milan = [f for f in partite if 2 in (f.home_team_id, f.away_team_id)]
    nuove = {f.api_id for f in del_milan[-quante:]}
    for f in partite:
        def mister(team_id):
            if team_id == 2:
                return 999 if f.api_id in nuove else 111
            return 500 + team_id
        eng.store.save_detail(f.api_id, "lineups", _lineups(
            f.home_team_id, f.away_team_id,
            mister(f.home_team_id), mister(f.away_team_id),
        ))


def test_la_scheda_dichiara_il_cambio_di_panchina(motore, client):
    """Il modello pesa solo l'eta' di una partita: se la squadra ha cambiato
    guida, le partite di prima entrano lo stesso e la pagina deve dirlo."""
    _milan_cambia_allenatore(motore)

    pagina = client.get("/partita/9002").text   # Milan - Juventus, da giocare

    assert "Ha cambiato allenatore" in pagina
    assert "Mister 999" in pagina, "l'allenatore di adesso"
    assert "Mister 111" in pagina, "e quello di prima"


def test_la_scheda_dice_che_il_pronostico_non_ne_tiene_conto(motore, client):
    """La stessa regola degli indisponibili: se il modello non usa un dato,
    la pagina lo dichiara invece di lasciarlo credere."""
    _milan_cambia_allenatore(motore)

    pagina = client.get("/partita/9002").text

    assert "non ne tiene conto" in pagina


def test_senza_cambi_la_sezione_non_compare(motore, client):
    """Una sezione vuota si leggerebbe come "e' cambiato qualcosa", che sarebbe
    inventato: senza cambi non compare affatto."""
    _milan_cambia_allenatore(motore, quante=0)

    pagina = client.get("/partita/9002").text

    assert "Ha cambiato allenatore" not in pagina


def test_una_formazione_nuova_fa_ristimare_il_modello(motore):
    """Il modello ora dipende dalle formazioni, quindi l'impronta degli archivi
    deve muoversi anche quando arriva soltanto un dettaglio.

    Senza, la stima resterebbe all'allenatore di prima con il sito aperto e
    nessun motivo apparente per riavviarlo.
    """
    prima = motore.snapshot()
    _milan_cambia_allenatore(motore)

    dopo = motore.snapshot()

    assert dopo is not prima
    assert dopo.panchine is not None
    assert len(dopo.panchine.cronologia(2)) == 2


# ---- il pronostico rifatto con le formazioni ufficiali ----------------------
#
# Oggi non ne esiste nessuno: la macchina c'e' ed e' spenta finche' i tabellini
# non bastano. Queste pagine vanno pero' scritte e provate PRIMA, altrimenti il
# giorno dell'accensione il lavoro partirebbe, scriverebbe nel registro, e sul
# sito non cambierebbe niente.


def _pronostico_con_le_formazioni(eng: Engine, fixture_id: int, spinta: float = 0.45):
    """Aggiunge a una partita il secondo pronostico, come fara' `ufficiali.py`.

    Il modello e' deliberatamente diverso da quello secco — piu' sbilanciato
    verso la casa — perche' un pronostico identico non avrebbe niente da
    mostrare e, nel sistema vero, non verrebbe nemmeno scritto (`vale_la_pena`).
    """
    snap = eng.snapshot()
    partita = snap.dataset.by_id[fixture_id]

    registra_partite(
        eng.pronostici, snap.model, [partita], snap.dataset.name,
        now=partita.kickoff_utc - timedelta(hours=6),
    )

    gonfiato = replace(snap.model, home_advantage=snap.model.home_advantage + spinta)
    registra_partite(
        eng.pronostici, gonfiato, [partita], snap.dataset.name,
        mode=MODE_UFFICIALE,
        now=partita.kickoff_utc - timedelta(hours=1),
    )


def test_senza_formazioni_ufficiali_la_sezione_non_compare(client):
    """La situazione di oggi, e deve restare muta.

    Una sezione vuota direbbe "le formazioni non cambiano niente", che e'
    un'informazione, e sarebbe inventata.
    """
    pagina = client.get("/partita/9001").text

    assert "Il pronostico rifatto" not in pagina
    assert "Con le formazioni" not in pagina


def test_la_scheda_partita_mostra_i_due_pronostici_a_confronto(motore, monkeypatch):
    _pronostico_con_le_formazioni(motore, 9001)
    monkeypatch.setattr(app_module, "_engine", motore)

    pagina = TestClient(app_module.app).get("/partita/9001").text

    assert "Il pronostico rifatto" in pagina
    # Il confronto e' fra due righe di registro, e la pagina lo dice.
    assert "secco scritto" in pagina
    assert "rifatto" in pagina
    # Il limite del metodo va dichiarato, non compensato.
    assert "quasi gratis" in pagina
    assert "difensori" in pagina


def test_a_partita_finita_il_pronostico_grande_e_il_secco(motore, monkeypatch):
    """Se non lo dicesse, i due numeri diversi in pagina si contraddirebbero."""
    passata = motore.snapshot().dataset.finished[0]
    _pronostico_con_le_formazioni(motore, passata.api_id)
    monkeypatch.setattr(app_module, "_engine", motore)

    pagina = TestClient(app_module.app).get(f"/partita/{passata.api_id}").text

    assert "in cima alla pagina e' quello <b>secco</b>" in pagina


def test_prima_del_via_il_pronostico_grande_non_e_nessuno_dei_due(motore, monkeypatch):
    """Su una partita da giocare la testata e' il modello di ADESSO.

    La regola di `_riga` e' modello caldo prima del fischio d'inizio, registro
    dopo — quindi finche' la partita non comincia il trittico in cima non e'
    la riga secca del registro, e puo' mostrare numeri diversi dalla colonna
    "Secco" della tabella qui sotto. La pagina lo diceva al contrario, e si
    contraddiceva da sola proprio dove i due numeri sono affiancati.
    """
    _pronostico_con_le_formazioni(motore, 9001)
    monkeypatch.setattr(app_module, "_engine", motore)

    pagina = TestClient(app_module.app).get("/partita/9001").text

    assert "Il pronostico rifatto" in pagina, "premessa: il confronto c'e'"
    assert "in cima alla pagina e' quello <b>secco</b>" not in pagina
    assert "il <b>modello di adesso</b>" in pagina


def test_il_confronto_viene_da_due_righe_congelate_e_non_dal_modello_caldo(motore):
    """La differenza fra i due deve raccontare le formazioni, non il tempo.

    Il secco ricalcolato adesso avrebbe visto piu' partite dell'ufficiale
    congelato ieri: la loro differenza direbbe quanto e' passato nel
    frattempo, non quanto pesa sapere chi gioca.
    """
    _pronostico_con_le_formazioni(motore, 9001)

    confronto = confronto_ufficiale(
        motore.congelato(9001, MODE_SECCO),
        motore.congelato(9001, MODE_UFFICIALE),
    )

    assert confronto is not None
    # La spinta sul fattore campo deve vedersi come casa piu' probabile.
    casa = next(e for e in confronto.esiti if e["esito"] == "1")
    assert casa["scarto"] > 0
    assert confronto.spostamento_massimo > 0.01


def test_senza_una_delle_due_righe_non_si_confronta_niente(motore):
    """Mostrare l'ufficiale da solo lo farebbe sembrare "il pronostico" e
    basta, e sparirebbe l'informazione per cui e' stato scritto."""
    assert confronto_ufficiale(None, None) is None
    assert confronto_ufficiale(motore.congelato(9001, MODE_SECCO), None) is None


def test_l_hub_mostra_il_verdetto_sulle_formazioni_solo_quando_ce_n_e_uno(
    motore, monkeypatch
):
    monkeypatch.setattr(app_module, "_engine", motore)
    client = TestClient(app_module.app)

    assert "Le formazioni ufficiali" not in client.get("/").text

    # Una partita conclusa con tutti e due i pronostici, poi risolta.
    conclusa = motore.snapshot().dataset.finished[-1]
    _pronostico_con_le_formazioni(motore, conclusa.api_id)
    motore.snapshot(force=True)

    pagina = client.get("/").text
    assert "Le formazioni ufficiali" in pagina
    assert "Serve rifarlo?" in pagina


def test_il_verdetto_dell_hub_usa_solo_le_partite_con_entrambi(motore, monkeypatch):
    """Il numero di partite dichiarato in pagina e' quello del confronto, non
    quello di tutti i pronostici: sono cose diverse e mostrare il secondo
    gonfierebbe la base di una misura che ne usa un'altra."""
    _congela_il_passato(motore)
    conclusa = motore.snapshot().dataset.finished[-1]
    _pronostico_con_le_formazioni(motore, conclusa.api_id)
    motore.snapshot(force=True)

    confronto = motore.confronto_modi()

    assert confronto["partite"] == 1
    assert motore.pronostici.conteggi()["pronostici"] > 1


def test_quando_le_formazioni_confermano_la_pagina_lo_dice(motore, monkeypatch):
    """"Non e' cambiato niente" e' un esito, non un vuoto da nascondere.

    Un pronostico ufficiale nasce solo se sposta qualcosa, ma "qualcosa" puo'
    essere mezzo punto percentuale: dirlo evita che il lettore cerchi una
    differenza che non c'e'.
    """
    _pronostico_con_le_formazioni(motore, 9001, spinta=0.001)
    monkeypatch.setattr(app_module, "_engine", motore)

    pagina = TestClient(app_module.app).get("/partita/9001").text

    assert "Il pronostico rifatto" in pagina
    assert "hanno <b>confermato</b>" in pagina


# ---- la calibrazione in pagina ----------------------------------------------
#
# La seconda lettura degli stessi esiti che l'accuratezza riassume in un
# Brier. Quello che va difeso qui non e' il calcolo — sta in data/outcomes.py e
# ha i suoi test — ma le due cose che in pagina si possono raccontare male:
# quando una fascia ha troppo pochi esiti per dire qualcosa, e cosa significa
# "in media sbaglia di tanto".


def _fasce(*coppie):
    """Fasce finte: (n, previsto, reale)."""
    return [
        {"fascia": f"fascia {i}", "n": n, "previsto": previsto, "reale": reale}
        for i, (n, previsto, reale) in enumerate(coppie)
    ]


def test_una_fascia_con_pochi_esiti_viene_dichiarata_tale():
    """Con otto pronostici al 30%, due in piu' o in meno cambiano tutto."""
    righe = righe_calibrazione(_fasce(
        (MINIMO_PER_FASCIA * 5, 0.30, 0.28),
        (MINIMO_PER_FASCIA - 1, 0.70, 1.00),
    ))

    assert righe[0]["abbastanza"] is True
    assert righe[0]["tono"] == "ok"
    assert righe[1]["abbastanza"] is False
    assert righe[1]["tono"] == "attesa"
    # La riga povera resta in elenco: farla sparire nasconderebbe che il
    # modello in quella fascia ci e' andato, e quante volte.
    assert len(righe) == 2


def test_il_segno_dello_scarto_dice_troppa_sicurezza():
    """Negativo = il modello prometteva piu' di quanto la realta' ha mantenuto."""
    troppo_sicuro, prudente = righe_calibrazione(_fasce(
        (50, 0.60, 0.40),
        (50, 0.40, 0.60),
    ))

    assert troppo_sicuro["scarto"] < 0
    assert prudente["scarto"] > 0


def test_lo_scarto_medio_e_una_distanza_e_non_un_saldo():
    """Sull'1X2 la media col segno vale zero quasi per costruzione.

    Le tre probabilita' di una partita sommano a uno e un esito solo si
    avvera: cio' che una fascia promette di troppo, un'altra lo promette di
    meno. Un riassunto col segno direbbe "in media ci prende" di un modello
    scalibrato in tutte e due le direzioni.
    """
    fasce = _fasce((50, 0.60, 0.40), (50, 0.40, 0.60))

    saldo = sum((f["reale"] - f["previsto"]) * f["n"] for f in fasce)
    verdetto = verdetto_calibrazione(fasce, partite=40)

    assert saldo == pytest.approx(0.0), "premessa del test"
    assert verdetto["scarto_medio"] == pytest.approx(0.20)
    assert verdetto["n"] == 100


def test_senza_fasce_non_c_e_verdetto():
    assert verdetto_calibrazione([], partite=0) is None


def test_la_calibrazione_si_giudica_in_partite_e_non_in_esiti():
    """Sull'1X2 una partita mette tre esiti: contarli triplicava il campione.

    Con venti partite in archivio il conto faceva sessanta, scavalcava
    `SOGLIA_SIGNIFICATIVITA` e l'hub dichiarava valido un numero che non lo
    era — l'avviso "sono troppo poche" non compariva. E' lo stesso difetto
    gia' corretto per l'accuratezza, rimasto qui.
    """
    fasce = _fasce((30, 0.30, 0.28), (30, 0.60, 0.55))

    pochi = verdetto_calibrazione(fasce, partite=20)
    abbastanza = verdetto_calibrazione(fasce, partite=SOGLIA_SIGNIFICATIVITA)

    assert pochi["n"] == 60, "gli esiti restano sessanta: sono veri"
    assert pochi["partite"] == 20
    assert pochi["abbastanza"] is False, "venti partite non bastano"
    assert abbastanza["abbastanza"] is True


def test_l_hub_mostra_la_calibrazione_solo_quando_c_e_qualcosa(motore, monkeypatch):
    """Una tabella vuota si leggerebbe come 'e' scalibrato', che e' diverso."""
    motore.snapshot()
    monkeypatch.setattr(app_module, "_engine", motore)
    client = TestClient(app_module.app)

    assert "succede il 30% delle volte" not in client.get("/").text

    motore.store.upsert_fixtures([_raw(9001, 1, 6, ADESSO + timedelta(days=2), gol=(2, 0))])
    motore.snapshot(force=True)
    pagina = client.get("/").text

    assert "succede il 30% delle volte" in pagina
    # Tre esiti soli: il numero non vale ancora, e la pagina deve dirlo.
    assert "troppo pochi" in pagina or "troppi pochi dati" in pagina


# ---- il registro dei pronostici ---------------------------------------------


@pytest.mark.parametrize("minuti, atteso", [
    (30, "30 min"),
    (89, "89 min"),
    (90, "1 h"),
    (60 * 47, "47 h"),
    (60 * 48, "2 g"),
    (60 * 24 * 9, "9 g"),
])
def test_l_anticipo_si_legge_a_colpo_d_occhio(minuti, atteso):
    assert distanza_breve(timedelta(minutes=minuti)) == atteso


def test_un_pronostico_scritto_dopo_il_via_non_ha_anticipo():
    """Il registro non lo permette: se capita e' un dato che non torna.

    Mostrarlo come "-3 h" gli darebbe l'aria di un numero normale.
    """
    assert distanza_breve(timedelta(hours=-3)) is None


def _voce_registro(fixture_id, mode=MODE_SECCO, scritto=None, risolto=True):
    return {
        "id": fixture_id,
        "fixture_id": fixture_id,
        "mode": mode,
        "scritto_il": scritto or (ADESSO - timedelta(days=3)),
        "versione": "0.1.0",
        "gol_attesi": (1.5, 1.0),
        "selezioni": {
            "home": {"probabilita": 0.5, "hit": True, "brier": 0.25},
            "draw": {"probabilita": 0.3, "hit": False, "brier": 0.09},
            "away": {"probabilita": 0.2, "hit": False, "brier": 0.04},
        },
        "risolto": risolto,
        "brier": 0.38 if risolto else None,
        "riuscite": ["home"] if risolto else [],
    }


def test_il_registro_mette_in_cima_le_partite_ancora_da_giocare(motore):
    """E' la meta' della promessa in cui la risposta non c'e' ancora.

    Ordinare per calcio d'inizio invece che per quando la riga fu scritta e'
    cio' che distingue questa pagina da una tabella di risultati.
    """
    snap = motore.snapshot()
    passata = next(f for f in snap.dataset.finished)
    futura = next(f for f in snap.dataset.upcoming())

    righe = righe_registro(
        [_voce_registro(passata.api_id), _voce_registro(futura.api_id, risolto=False)],
        snap.dataset.by_id,
        snap.dataset.name,
    )

    assert [r.fixture_id for r in righe] == [futura.api_id, passata.api_id]
    assert righe[0].stato == "attesa"
    assert righe[0].conclusa is False
    assert righe[1].stato == "chiuso"
    assert righe[1].risultato


def test_una_partita_finita_senza_esito_scritto_e_un_terzo_stato(motore):
    """Fra il risultato e la ristima successiva c'e' un momento di mezzo.

    Dirlo "in attesa" come una partita da giocare sarebbe falso, e dirlo
    "chiuso" senza punteggio sembrerebbe un guasto: si risolve da solo.
    """
    snap = motore.snapshot()
    passata = next(f for f in snap.dataset.finished)

    riga = righe_registro(
        [_voce_registro(passata.api_id, risolto=False)],
        snap.dataset.by_id, snap.dataset.name,
    )[0]

    assert riga.stato == "aperto"
    assert riga.conclusa is True
    assert riga.brier is None


def test_un_pronostico_senza_partita_in_archivio_resta_in_elenco(motore):
    """Il pronostico e' immutabile, l'archivio delle partite no.

    Farlo sparire cancellerebbe dalla pagina una promessa che era stata data.
    """
    snap = motore.snapshot()

    riga = righe_registro(
        [_voce_registro(999999)], snap.dataset.by_id, snap.dataset.name
    )[0]

    assert riga.fixture_id == 999999
    assert riga.in_archivio is False
    assert riga.ospite == "", "senza partita non si inventano le squadre"
    assert riga.prob_casa == 0.5, "le probabilita' sono cio' che il registro sa"


def test_la_probabilita_sull_accaduto_e_quella_dell_esito_vero(motore):
    snap = motore.snapshot()
    passata = next(f for f in snap.dataset.finished)

    riga = righe_registro(
        [_voce_registro(passata.api_id)], snap.dataset.by_id, snap.dataset.name
    )[0]

    atteso = {"1": riga.prob_casa, "X": riga.prob_pari, "2": riga.prob_ospite}
    assert riga.probabilita_sull_accaduto == atteso[riga.esito_reale]


def test_il_registro_si_ferma_al_limite_ma_dalla_parte_giusta(motore):
    """Troncare l'elenco deve togliere le righe vecchie, non quelle nuove."""
    snap = motore.snapshot()
    tutte = list(snap.dataset.finished)[:5]

    righe = righe_registro(
        [_voce_registro(f.api_id) for f in tutte],
        snap.dataset.by_id, snap.dataset.name,
        limite=2,
    )

    piu_recenti = sorted(tutte, key=lambda f: f.kickoff_utc, reverse=True)[:2]
    assert [r.fixture_id for r in righe] == [f.api_id for f in piu_recenti]


def test_la_pagina_del_registro_elenca_i_pronostici(client, motore):
    motore.snapshot()

    r = client.get("/pronostici")

    assert r.status_code == 200
    assert "Il registro" in r.text
    # Le due partite in programma sono gia' state congelate dallo snapshot.
    assert "da giocare" in r.text
    assert "/partita/9001" in r.text


def test_la_colonna_del_tipo_compare_solo_con_due_modi(client, motore):
    """Finche' le formazioni ufficiali sono spente direbbe 'secco' e basta."""
    motore.snapshot()
    assert "<th>Tipo</th>" not in client.get("/pronostici").text

    snap = motore.snapshot()
    registra_partite(
        motore.pronostici, snap.model, snap.dataset.upcoming(), snap.dataset.name,
        mode=MODE_UFFICIALE,
    )

    pagina = client.get("/pronostici").text
    assert "<th>Tipo</th>" in pagina
    assert "formazioni" in pagina


def test_il_registro_vuoto_lo_dice_invece_di_mostrare_una_tabella(tmp_path, monkeypatch):
    """Su un archivio senza partite future nessuno ha ancora detto niente.

    E' la situazione di chi accende il sistema oggi: la pagina deve spiegarla
    invece di mostrare una tabella con zero righe.
    """
    settings = Settings(
        api_key="k", api_provider="apisports", daily_request_limit=100,
        api_min_interval_seconds=0.0,
        cache_dir=tmp_path / "c", budget_file=tmp_path / "b.json",
        store_dir=tmp_path / "s", storico_dir=tmp_path / "st",
        importato_dir=tmp_path / "imp",
        journal_file=tmp_path / "j.json",
        predictions_db=tmp_path / "pronostici.db",
        database_url=None, league_id=135, seasons=(),
    )
    monkeypatch.setattr(app_module, "_engine", Engine(settings))

    r = TestClient(app_module.app).get("/pronostici")

    assert r.status_code == 200
    assert "Il registro e' vuoto" in r.text


def test_se_il_modello_cambia_idea_nasce_una_riga_nuova(client, motore):
    """Il pronostico vecchio non si aggiorna: se ne aggiunge un altro.

    Ed e' anche il motivo per cui questa pagina mostra sempre e solo il
    registro, partite future comprese: l'hub direbbe cosa pensa il modello di
    adesso, qui si vede cosa fu **scritto**, riga per riga, comprese le volte
    in cui il modello ha cambiato idea prima del fischio d'inizio.
    """
    motore.snapshot()
    prima = [v for v in motore.registro() if v["fixture_id"] == 9001]
    assert len(prima) == 1
    congelata = prima[0]["selezioni"]["home"]["probabilita"]

    # Arriva un risultato nuovo e il modello viene ristimato.
    motore.store.upsert_fixtures(
        [_raw(9500, 1, 6, ADESSO - timedelta(hours=6), gol=(5, 0))]
    )
    motore.snapshot(force=True)

    dopo = [v for v in motore.registro() if v["fixture_id"] == 9001]
    assert len(dopo) == 2
    assert any(
        v["selezioni"]["home"]["probabilita"] == congelata for v in dopo
    ), "la riga di prima deve essere ancora li', identica"

    pagina = client.get("/pronostici").text
    assert pagina.count('href="/partita/9001"') == 2, "in pagina si vedono tutt'e due"


# ---- il sistema dichiara i propri guasti (I2, I4) ---------------------------


def test_lo_stato_dice_se_il_modello_e_convergente(client):
    stato = client.get("/stato").json()
    assert stato["modello_convergente"] is True


def test_un_modello_non_convergente_si_vede_in_pagina(motore, monkeypatch):
    """Il caso che oggi non capita mai, ed e' per questo che va coperto adesso.

    Se l'ottimizzatore sbattesse contro `max_iterations` le pagine avrebbero
    la faccia di sempre: percentuali, verdetti, tutto. E' contro la regola del
    progetto di non nascondere i limiti del modello nell'interfaccia.
    """
    snap = motore.snapshot()
    motore._snapshot = replace(snap, model=replace(snap.model, converged=False))
    monkeypatch.setattr(app_module, "_engine", motore)
    client = TestClient(app_module.app)

    assert client.get("/stato").json()["modello_convergente"] is False
    pagina = client.get("/").text
    assert "non ha raggiunto la convergenza" in pagina
    # Le percentuali restano: "non lo so" e "lo so male" sono due cose diverse.
    assert "avviso-guasto" in pagina


def test_un_modello_convergente_non_allarma_nessuno(client):
    assert "avviso-guasto" not in client.get("/").text


def test_un_registro_illeggibile_non_diventa_zero(motore, monkeypatch):
    """Il difetto I4: `except: return 0` dichiarava un vuoto mai osservato.

    Zero pronostici registrati e' un fatto; un database che non risponde e'
    un'altra cosa, e scriverli uguale e' la bugia piu' facile da raccontare
    con dati veri. E' la stessa regola di `pesi.py`, dove None non deve mai
    diventare 1.0.
    """
    def rotto():
        raise RuntimeError("database occupato")

    monkeypatch.setattr(motore.pronostici, "conteggi", rotto)
    monkeypatch.setattr(app_module, "_engine", motore)
    client = TestClient(app_module.app)

    stato = client.get("/stato").json()
    assert stato["registro_leggibile"] is False
    assert stato["pronostici_registrati"] is None
    assert stato["esiti_risolti"] is None
    assert stato["partite_quotate"] is None
    assert stato["partite_con_indisponibili"] is None

    assert "non si legge" in client.get("/").text


def test_un_registro_sano_non_dice_niente(client):
    stato = client.get("/stato").json()
    assert stato["registro_leggibile"] is True
    # Un numero, quale che sia: il motore ne congela qualcuno da solo alla
    # ristima. Cio' che conta e' che non sia None.
    assert isinstance(stato["pronostici_registrati"], int)
    assert "non si legge" not in client.get("/").text


def test_il_registro_si_legge_una_volta_sola_per_pagina(motore, monkeypatch):
    """Prima ogni voce se lo rileggeva: quattro connessioni sqlite a pagina."""
    letture = []
    vero = motore.pronostici.conteggi

    def contando():
        letture.append(1)
        return vero()

    monkeypatch.setattr(motore.pronostici, "conteggi", contando)
    motore.health()

    assert len(letture) == 1


# ---- il piede di pagina nomina tutte le sorgenti (I3) -----------------------


def test_il_piede_nomina_le_stagioni_importate(motore, monkeypatch):
    """Oggi valgono zero perche' la 2025/26 e' anche ricomprata e vince il
    dato pagato. Si vedrebbe il giorno in cui se ne importasse una senza
    ricomprarla — e allora i numeri del piede non tornerebbero, in silenzio.
    """
    snap = motore.snapshot()
    dataset = replace(snap.dataset, n_importate=380, stagioni_importate=[2021])
    motore._snapshot = replace(snap, dataset=dataset)
    monkeypatch.setattr(app_module, "_engine", motore)

    pagina = TestClient(app_module.app).get("/").text
    assert "dalle stagioni importate da file" in pagina
    assert "2021" in pagina


def test_senza_stagioni_importate_il_piede_non_ne_parla(client):
    """Una riga a zero direbbe che manca qualcosa che nessuno ha chiesto."""
    assert "dalle stagioni importate" not in client.get("/").text


# ---- la data corta sugli schermi stretti (L1) -------------------------------


def test_la_riga_porta_la_data_in_due_forme(client):
    """Tutte e due nell'HTML: a scegliere e' la larghezza vera, non il server.

    Un'ipotesi lato server su "sei da telefono?" sbaglierebbe ogni volta che
    qualcuno gira lo schermo.
    """
    pagina = client.get("/").text
    assert 'class="data-lunga"' in pagina
    assert 'class="data-corta"' in pagina


def test_la_data_corta_e_giorno_e_numero():
    from serie_a_analyzer.web.views import formatta_data, formatta_data_breve

    quando = datetime(2026, 8, 23, 18, 45, tzinfo=timezone.utc)
    assert formatta_data(quando) == "domenica 23 ago, 20:45"
    # Niente ora: su una partita giocata dice poco, e mangiava il 40% della riga.
    assert formatta_data_breve(quando) == "dom 23"


# ---- sfogliare una giornata qualsiasi (L3) ----------------------------------


def test_una_giornata_si_puo_aprire(client, motore):
    stagione = motore.snapshot().dataset.stagioni()[0]
    r = client.get(f"/giornata/1?stagione={stagione}")

    assert r.status_code == 200
    assert "Giornata 1" in r.text


def test_il_calendario_porta_alla_giornata_che_si_sta_per_giocare(client, motore):
    """/calendario non e' una pagina: e' l'indirizzo che sa dove sei arrivato."""
    dataset = motore.snapshot().dataset
    stagione = dataset.stagioni()[0]
    attesa = dataset.giornata_corrente(stagione)

    r = client.get("/calendario", follow_redirects=False)

    assert r.status_code == 307
    assert r.headers["location"] == f"/giornata/{attesa}?stagione={stagione}"


def test_una_giornata_che_non_esiste_e_un_404(client, motore):
    stagione = motore.snapshot().dataset.stagioni()[0]
    assert client.get(f"/giornata/99?stagione={stagione}").status_code == 404


def test_una_giornata_da_giocare_non_si_chiama_sbagliata(client, motore):
    """Il terzo stato della riga, quello che l'hub non aveva mai.

    Sugli ultimi risultati una partita ha due esiti possibili — azzeccata o
    no. Qui ce n'e' un terzo: non ancora giocata. Con la pastiglia vuota si
    leggerebbe come "sbagliato", che e' un giudizio su una partita che nessuno
    ha ancora visto.
    """
    dataset = motore.snapshot().dataset
    stagione = dataset.stagioni()[0]
    futura = next(
        f for f in dataset.fixtures
        if f.status == "scheduled" and f.matchday is not None
    )

    r = client.get(f"/giornata/{futura.matchday}?stagione={stagione}")

    assert r.status_code == 200
    assert "da giocare" in r.text


def test_le_giornate_di_una_stagione_saltano_quelle_senza_numero(motore):
    """Una voce "giornata None" nel menu sarebbe un vicolo cieco."""
    dataset = motore.snapshot().dataset
    stagione = dataset.stagioni()[0]

    giornate = dataset.giornate(stagione)

    assert giornate == sorted(giornate)
    assert None not in giornate
    assert all(isinstance(g, int) for g in giornate)


def test_una_giornata_e_ordinata_per_calcio_d_inizio(motore):
    """Una giornata si spalma su tre giorni: si guarda in ordine di orario."""
    dataset = motore.snapshot().dataset
    stagione = dataset.stagioni()[0]
    numero = dataset.giornate(stagione)[0]

    partite = dataset.giornata(stagione, numero)

    assert partite == sorted(partite, key=lambda f: f.kickoff_utc)
    assert {f.matchday for f in partite} == {numero}


def test_la_testata_porta_al_calendario(client):
    assert 'href="/calendario"' in client.get("/").text


# ---- il foglio di stile deve arrivare al browser ----------------------------


def test_il_foglio_di_stile_porta_una_versione(client):
    """Senza, un CSS appena modificato puo' non essere richiesto affatto.

    E' successo davvero: la pagina di una giornata e' arrivata coi numeri come
    testo sottolineato invece che come bottoni, con il CSS giusto sul server.
    """
    import re

    pagina = client.get("/").text
    trovato = re.search(r'href="/static/style\.css\?v=(\d+)"', pagina)

    assert trovato, "il foglio di stile non porta nessuna versione"
    assert int(trovato.group(1)) > 0


def test_la_versione_cambia_quando_cambia_il_foglio(tmp_path, monkeypatch):
    """Si rilegge a ogni pagina, non a un avvio solo: un sito acceso per
    giorni tornerebbe a servire la versione vecchia dopo ogni ritocco."""
    import os
    from serie_a_analyzer.web.app import versione_statici

    prima = versione_statici()
    foglio = app_module.QUI / "static" / "style.css"
    dopo_mtime = foglio.stat().st_mtime + 10
    os.utime(foglio, (dopo_mtime, dopo_mtime))
    try:
        assert versione_statici() != prima
    finally:
        originale = float(prima)
        os.utime(foglio, (originale, originale))


def test_le_giornate_sono_bottoni_come_quelli_della_testata(client, motore):
    """Non una forma nuova: la stessa `.bottone` del resto del sito.

    Una pagina che disegna un comando in un terzo modo insegna a non
    riconoscerlo.
    """
    stagione = motore.snapshot().dataset.stagioni()[0]
    pagina = client.get(f"/giornata/1?stagione={stagione}").text

    assert 'class="bottone giornata' in pagina
    # La giornata aperta usa lo stesso `.attivo` della pagina in testata.
    assert 'class="bottone giornata attivo"' in pagina
    assert "pastiglia-giornata" not in pagina


# ---- il registro non puo' seppellire le partite gia' giudicate --------------
#
# Il difetto piu' grosso che la pagina abbia avuto: le righe erano ordinate
# per calcio d'inizio decrescente e tagliate a duecento, e da quando
# l'archivio contiene il calendario intero le piu' "recenti" erano quelle di
# maggio. La pagina annunciava «N gia' verificati», spiegava per quattro righe
# come si legge il Brier, e quella colonna era vuota su ognuna delle righe
# mostrate.


def test_il_registro_non_lascia_fuori_le_partite_gia_giudicate(motore):
    """Un calendario lungo non deve mangiarsi la meta' misurata della pagina."""
    snap = motore.snapshot()
    futura = snap.dataset.upcoming()[0]
    passata = snap.dataset.finished[0]
    # Un calendario che va avanti per mesi: cinquanta partite finte, tutte
    # piu' in la' di quella vera.
    lontane = [
        _voce_registro(900000 + i, risolto=False)
        for i in range(50)
    ]
    per_id = dict(snap.dataset.by_id)
    for i, voce in enumerate(lontane):
        per_id[voce["fixture_id"]] = replace(
            futura,
            api_id=voce["fixture_id"],
            kickoff_utc=futura.kickoff_utc + timedelta(days=7 * (i + 1)),
        )

    righe = righe_registro(
        [*lontane, _voce_registro(passata.api_id), _voce_registro(futura.api_id, risolto=False)],
        per_id, snap.dataset.name,
        limite=10, in_attesa=4,
    )

    assert passata.api_id in [r.fixture_id for r in righe], (
        "la partita gia' giudicata deve restare in pagina"
    )
    assert righe[0].fixture_id == futura.api_id, "in cima la piu' vicina, non maggio"
    assert righe[-1].fixture_id == passata.api_id


def test_il_registro_va_avanti_nel_futuro_e_indietro_nel_passato(motore):
    """Due meta', due ordini, e in mezzo il presente.

    In cima le promesse aperte dalla piu' vicina al fischio d'inizio, sotto
    quelle gia' giudicate dalla piu' recente: e' l'unico ordine in cui le due
    righe di confine sono le due partite piu' vicine a oggi.
    """
    snap = motore.snapshot()
    future = snap.dataset.upcoming()[:2]
    passate = sorted(snap.dataset.finished, key=lambda f: f.kickoff_utc, reverse=True)[:2]
    assert len(future) == 2 and len(passate) == 2, "premessa del test"

    righe = righe_registro(
        [_voce_registro(f.api_id, risolto=False) for f in future]
        + [_voce_registro(f.api_id) for f in passate],
        snap.dataset.by_id, snap.dataset.name,
    )

    assert [r.fixture_id for r in righe] == [
        future[0].api_id, future[1].api_id, passate[0].api_id, passate[1].api_id
    ]


def test_la_quota_delle_partite_da_giocare_ha_un_tetto_suo(motore):
    """Senza, bastano sessanta partite in calendario per riempire la pagina."""
    snap = motore.snapshot()
    futura = snap.dataset.upcoming()[0]
    passata = snap.dataset.finished[0]

    righe = righe_registro(
        [_voce_registro(futura.api_id, risolto=False), _voce_registro(passata.api_id)],
        snap.dataset.by_id, snap.dataset.name,
        limite=10, in_attesa=0,
    )

    assert [r.fixture_id for r in righe] == [passata.api_id]


# ---- tre percentuali che sommano a cento ------------------------------------


def test_le_tre_percentuali_di_una_partita_fanno_cento():
    """«45% 27% 27%» sotto un titolo che promette una partizione fa 99."""
    from serie_a_analyzer.web.views import percentuali_intere

    tre = percentuali_intere(0.4489, 0.2733, 0.2778)

    assert sum(tre) == 100
    assert tre == (45, 27, 28), "il punto che avanza va al resto piu' grande"


def test_percentuali_di_niente_non_diventano_numeri():
    from serie_a_analyzer.web.views import percentuali_intere

    assert percentuali_intere(0.0, 0.0, 0.0) == (0, 0, 0)


def test_in_pagina_le_tre_percentuali_di_una_partita_fanno_cento(client, motore):
    """La regola vale dove si vede: nella tabella dei risultati."""
    import re

    motore.snapshot()
    pagina = client.get("/").text
    quanti = [int(x) for x in re.findall(r'class="quanto">(\d+)%', pagina)]

    assert quanti, "nessun trittico in pagina"
    assert len(quanti) % 3 == 0, "un trittico ha tre cifre"
    for i in range(0, len(quanti), 3):
        assert sum(quanti[i:i + 3]) == 100, quanti[i:i + 3]


# ---- una partita da giocare non ha un punteggio, nemmeno vuoto --------------


def test_una_partita_da_giocare_non_mostra_una_pastiglia_vuota(client, motore):
    """Il terzo stato della riga era stato scritto per la colonna sbagliata.

    Con `risultato` vuoto la pastiglia usciva lo stesso: un rettangolino
    grigio pieno, senza cifre, in una colonna che si chiama "Risultato".
    """
    dataset = motore.snapshot().dataset
    stagione = dataset.stagioni()[0]
    futura = next(
        f for f in dataset.fixtures
        if f.status == "scheduled" and f.matchday is not None
    )

    pagina = client.get(f"/giornata/{futura.matchday}?stagione={stagione}").text

    import re

    vuote = re.search(r'class="punteggio[^"]*"[^>]*>\s*</span>', pagina)
    assert vuote is None, f"pastiglia senza testo: {vuote and vuote.group(0)}"
    assert "da giocare" in pagina, "il terzo stato resta detto a parole"


# ---- le due palline: due domande, due colonne -------------------------------
#
# La regressione che le ha fatte nascere e' Juventus-Milan del 6 settembre
# 2026 (vedi ``test_il_punteggio_giusto_col_segno_sbagliato_e_mezzo_verdetto``
# per il caso, qui c'e' la pagina). Questi test guardano l'HTML e non le
# proprieta': il difetto di allora non era nel calcolo — i due booleani
# c'erano gia' e dicevano il vero — ma nel disegno, che li schiacciava in una
# pastiglia sola.

VERDE = "&#128994;"
ROSSO = "&#128308;"


def _larghezze_per_tabella(html):
    """Per ogni tabella, le larghezze delle sue righe (colspan compresi).

    Una pagina intera non ha una larghezza sola — l'hub ne ha cinque tabelle
    di mestieri diversi — quindi il conto va fatto dentro ogni ``<table>``.
    """
    import re

    tabelle = []
    for tabella in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        larghezze = []
        for riga in re.findall(r"<tr>(.*?)</tr>", tabella, re.S):
            celle = re.findall(r"<t[dh]([^>]*)>", riga)
            if not celle:
                continue
            larghezza = 0
            for attributi in celle:
                span = re.search(r'colspan="(\d+)"', attributi)
                larghezza += int(span.group(1)) if span else 1
            larghezze.append(larghezza)
        if larghezze:
            tabelle.append(larghezze)
    return tabelle


def test_i_due_giudizi_hanno_due_colonne(client_col_passato):
    """Una colonna sola non puo' dire due cose che possono discordare."""
    pagina = client_col_passato.get("/").text

    assert '<th class="destra">Esito</th>' in pagina
    assert '<th class="destra">Punteggio</th>' in pagina
    assert VERDE in pagina or ROSSO in pagina, "nessun giudizio disegnato"


def test_ogni_riga_occupa_le_stesse_colonne_dell_intestazione(client_col_passato, motore):
    """La guardia dei colspan, che una colonna in piu' rompe in silenzio.

    Le righe senza pronostico e quelle da giocare accorpano celle: se il
    numero non torna il browser non protesta, disegna storto — ed e' il
    genere di difetto che si vede dal telefono e non dai test. Si guardano
    due pagine perche' sono due stati diversi della riga: l'hub col passato
    congelato ha i giudizi, una giornata futura ha il "da giocare".
    """
    dataset = motore.snapshot().dataset
    stagione = dataset.stagioni()[0]
    futura = next(
        f for f in dataset.fixtures
        if f.status == "scheduled" and f.matchday is not None
    )

    for pagina in (client_col_passato.get("/").text,
                   client_col_passato.get(
                       f"/giornata/{futura.matchday}?stagione={stagione}").text):
        tabelle = _larghezze_per_tabella(pagina)
        assert tabelle, "nessuna tabella nella pagina"
        for larghezze in tabelle:
            assert len(set(larghezze)) == 1, \
                f"righe di larghezza diversa: {sorted(set(larghezze))}"


def test_la_pallina_dice_a_voce_quello_che_il_colore_dice_a_occhio(client_col_passato):
    """Un cerchio non letto e' un cerchio che non c'e'.

    Senza ``role="img"`` e ``aria-label`` un lettore di schermo annuncia
    "cerchio grande verde", cioe' descrive il disegno invece di dire cosa
    significa. La parola e' anche quella del ``title``, che e' quanto vede chi
    ci passa sopra col mouse e non sa cosa sia quella colonna.
    """
    pagina = client_col_passato.get("/").text

    assert 'aria-label="esito: azzeccato"' in pagina or            'aria-label="esito: sbagliato"' in pagina
    assert 'aria-label="punteggio: azzeccato"' in pagina or            'aria-label="punteggio: sbagliato"' in pagina


def test_una_partita_da_giocare_non_prende_palline_grigie(client, motore):
    """Il terzo stato resta uno, e resta detto a parole.

    Cerchi spenti sarebbero giudizi su una partita che nessuno ha ancora
    visto: e' la stessa ragione per cui la pastiglia di prima non poteva
    restare vuota. Le colonne del giudizio sono tre dal 9 settembre 2026 e la
    scritta le occupa tutte — questo assert e' anche cio' che se ne accorge il
    giorno in cui qualcuno ne aggiunge una quarta e dimentica il colspan.
    """
    dataset = motore.snapshot().dataset
    stagione = dataset.stagioni()[0]
    futura = next(
        f for f in dataset.fixtures
        if f.status == "scheduled" and f.matchday is not None
    )

    pagina = client.get(f"/giornata/{futura.matchday}?stagione={stagione}").text

    assert "da giocare" in pagina
    assert 'colspan="3"' in pagina, "il terzo stato occupa tutte le colonne del giudizio"



# ---- la tabella delle forze non e' una classifica della Serie A -------------


def test_le_forze_marcano_chi_non_gioca_quest_anno(client, motore):
    """Il modello conosce quattro stagioni: senza dirlo sembrano tutte di adesso.

    Lo diceva un paragrafo accanto alla tabella, e dal 9 settembre 2026 lo dice
    la pillola sulla riga di chi non c'e' piu'. E' meglio dove sta adesso: la
    marca sta sulla squadra a cui si riferisce, non in un discorso generale.

    Il campionato finto dei test e' tutto della stagione in corso, quindi
    nessuna riga sarebbe marcata: si sposta la stagione avanti di un anno, che
    e' esattamente cio' che accade a fine maggio a ogni squadra retrocessa.
    """
    from serie_a_analyzer.data.ingest import current_season

    motore.snapshot()

    pagina = client.get("/").text
    assert "Forza stimata delle squadre" in pagina
    assert "fuori quest'anno" not in pagina, "premessa: giocano tutte quest'anno"

    patch = pytest.MonkeyPatch()
    patch.setattr(app_module, "current_season", lambda: current_season() + 1)
    try:
        pagina = client.get("/").text
    finally:
        patch.undo()

    assert "fuori quest'anno" in pagina


# ---- vuoto e illeggibile non sono la stessa cosa ----------------------------


def test_un_registro_illeggibile_non_si_chiama_vuoto(motore, monkeypatch):
    """E' la regola del piede di pagina applicata anche al titolo.

    "Il registro e' vuoto" sarebbe un fatto osservato, e nessuno l'ha
    osservato: il database potrebbe essere pieno e semplicemente non
    rispondere.
    """
    motore.snapshot()
    monkeypatch.setattr(
        motore.pronostici, "conteggi",
        lambda: (_ for _ in ()).throw(RuntimeError("database occupato")),
    )
    monkeypatch.setattr(
        motore, "registro", lambda *a, **k: [],
    )
    monkeypatch.setattr(app_module, "_engine", motore)

    pagina = TestClient(app_module.app).get("/pronostici").text

    assert "Il registro non si legge" in pagina
    assert "Il registro e' vuoto" not in pagina


# ---- l'esito a logica ---------------------------------------------------------
#
# Lavoro 9, concordato con l'utente il 9 settembre 2026. E' l'esito matematico
# raccontato a voce umana: si guarda quanto si distaccano le DUE SQUADRE e, se
# sono vicine, al favorito si aggiunge il pareggio.
#
# Le righe qui sotto difendono due cose diverse. Le prime che la regola sia
# quella e non un'altra; l'ultima, che e' la piu' importante, che questa
# lettura NON diventi mai un mercato: nessun Brier, nessuna riga di registro,
# nessuna misura. Il giorno in cui qualcuno la salvasse "per misurarla"
# avremmo due risposte alla stessa domanda contate come due osservazioni, che
# e' il difetto A1 e costo' un anno di misure sbagliate.


# 1 vale 0.60, X 0.22, 2 0.18: le due squadre distano 42 punti, sono lontane.
_LONTANE = {
    (2, 1): 0.20, (1, 0): 0.22, (2, 0): 0.18,
    (1, 1): 0.14, (0, 0): 0.08,
    (0, 1): 0.10, (1, 2): 0.08,
}

# 1 vale 0.38, X 0.28, 2 0.34: distano 4 punti, si somigliano.
_VICINE = {
    (2, 1): 0.14, (1, 0): 0.16, (2, 0): 0.08,
    (1, 1): 0.18, (0, 0): 0.10,
    (0, 1): 0.20, (1, 2): 0.14,
}


def test_due_squadre_lontane_danno_un_esito_secco():
    riga = _riga(_probabilita(_LONTANE), 2, 1)
    assert riga.distacco_fra_le_squadre > 0.10
    assert riga.esito_logico == "1"
    assert riga.esito_logico_col_pareggio is False


def test_due_squadre_vicine_si_prendono_anche_il_pareggio():
    riga = _riga(_probabilita(_VICINE), 1, 1)
    assert riga.distacco_fra_le_squadre < 0.10
    assert riga.esito_logico == "1X"
    assert riga.esito_logico_col_pareggio is True


def test_la_favorita_decide_da_che_parte_sta_il_pareggio():
    """Le stesse due partite, con la trasferta avanti: 2 secco e X2."""
    lontane = {(ospite, casa): p for (casa, ospite), p in _LONTANE.items()}
    vicine = {(ospite, casa): p for (casa, ospite), p in _VICINE.items()}

    assert _riga(_probabilita(lontane), 1, 2).esito_logico == "2"
    assert _riga(_probabilita(vicine), 1, 2).esito_logico == "X2"


def test_il_pareggio_non_e_mai_la_favorita():
    """La domanda e' "chi delle due", e il pareggio non e' una delle due.

    Anche dove la X e' la piu' probabile dei tre — cosa che nei 1154
    pronostici in registro non e' mai successa, ma il codice non puo'
    saperlo — la sigla resta 1X o X2 e mai una "X" da sola: sarebbe una
    terza risposta a una domanda che ne ammette due.
    """
    prob = _probabilita({
        (1, 1): 0.24, (0, 0): 0.16,                    # pareggio: 0.40
        (1, 0): 0.16, (2, 1): 0.15,                    # casa:     0.31
        (0, 1): 0.15, (1, 2): 0.14,                    # trasferta:0.29
    })
    riga = _riga(prob, 1, 1)
    assert riga.prob.draw > riga.prob.home_win
    assert riga.esito_logico == "1X"


def test_la_soglia_separa_davvero_le_due_risposte():
    """Appena sopra i dieci punti secco, appena sotto doppia.

    Il caso dei dieci punti ESATTI non e' difeso apposta, e la ragione e' che
    non esiste: il distacco e' una differenza fra due somme di float, e
    0.35 - 0.25 vale 0.09999999999999998. Pretendere che quel caso cada da un
    lato preciso vorrebbe dire scrivere un test che passa per caso. Quello che
    deve reggere e' che la soglia separi, e che a separarla sia il numero che
    la pagina scrive.
    """
    from serie_a_analyzer.web.views import SOGLIA_ESITO_LOGICO

    for celle, atteso in ((_LONTANE, "1"), (_VICINE, "1X")):
        riga = _riga(_probabilita(celle), 1, 1)
        sopra = riga.distacco_fra_le_squadre >= SOGLIA_ESITO_LOGICO
        assert riga.esito_logico == atteso
        assert sopra == (len(riga.esito_logico) == 1)


def test_senza_pronostico_non_si_scrive_nessun_esito_a_logica():
    """"Non lo so" non diventa una sigla, come sempre in questo progetto."""
    from serie_a_analyzer.web.views import RigaPartita

    riga = RigaPartita(
        id=1, casa="Inter", ospite="Lecce", casa_id=1, ospite_id=6,
        quando="sabato 22 ago, 18:30", giornata=1, conclusa=False,
        gol_casa=None, gol_ospite=None, prob=None,
    )
    assert riga.esito_logico is None
    assert riga.distacco_fra_le_squadre is None
    assert riga.esito_logico_col_pareggio is False


def test_l_esito_a_logica_non_e_un_mercato_e_non_finisce_nel_registro():
    """La regola che tiene in piedi tutto il lavoro 9.

    Se un giorno qualcuno lo aggiungesse a ``righe_mercati`` diventerebbe una
    riga di ``prediction_markets``, poi un esito risolto, poi un Brier — e a
    quel punto ogni partita avrebbe due risposte alla stessa domanda contate
    come due osservazioni. E' il difetto A1, e nessuno se ne accorse per un
    anno. Questo test e' l'unico posto in cui quel divieto e' scritto.
    """
    from serie_a_analyzer.data.predictions import righe_mercati

    prob = _probabilita(_VICINE)
    mercati = righe_mercati(prob)

    assert all("logic" not in mercato for mercato, _, _ in mercati)
    assert all(selezione not in ("1X", "X2") for _, selezione, _ in mercati)


# ---- la terza pallina: il giudizio sulla sigla a logica ---------------------
#
# Chiesta dall'utente il 9 settembre 2026, lo stesso giorno della sigla. Ha la
# forma delle altre due — verde, rosso, o niente finche' la partita non e'
# finita — e la regola e' un'appartenenza: la sigla E' l'elenco dei segni che
# accetta, quindi si guarda se l'esito accaduto sta dentro.
#
# Il confine del lavoro 9 non si muove di un millimetro: disegnare un cerchio
# non e' misurare. Il divieto colpisce chi volesse SOMMARE questi verdi per
# dire quanto vale il metodo, ed e' scritto dove puo' stare — nella docstring
# di `esito_logico_azzeccato` e nel test del mercato qui sopra, che continua a
# valere identico.


def test_la_sigla_secca_si_giudica_come_un_esito_qualunque():
    """Squadre lontane: la sigla dice "1" e vale quanto un 1 secco."""
    riga = _riga(_probabilita(_LONTANE), 2, 1)
    assert riga.esito_logico == "1"
    assert riga.esito_logico_azzeccato is True

    perso = _riga(_probabilita(_LONTANE), 0, 1)
    assert perso.esito_logico_azzeccato is False


def test_il_pareggio_accende_la_terza_pallina_e_spegne_la_prima():
    """Il caso per cui la colonna esiste, ed e' il gemello di Juventus-Milan.

    Squadre vicine, quindi la sigla aggiunge la X: "1X". Finisce in pareggio.
    L'esito preferito dal modello era l'1 — sbagliato — ma il pareggio era fra
    i segni che la sigla nominava, quindi la terza pallina e' verde mentre la
    prima e' rossa. Due domande diverse, due risposte diverse: la stessa
    ragione per cui il 7 settembre 2026 la pastiglia unica e' caduta.
    """
    riga = _riga(_probabilita(_VICINE), 1, 1)
    assert riga.esito_logico == "1X"
    assert riga.esito_previsto == "1"
    assert riga.pronostico_azzeccato is False
    assert riga.esito_logico_azzeccato is True


def test_la_doppia_non_copre_il_terzo_segno():
    """"1X" non e' un salvacondotto: se vince la trasferta e' rossa.

    Serve perche' due segni su tre sono tanti e la colonna sembra facile —
    lo e' — ma non e' gratis: il segno lasciato fuori la spegne come
    qualunque altro.
    """
    riga = _riga(_probabilita(_VICINE), 0, 1)
    assert riga.esito_logico == "1X"
    assert riga.esito_logico_azzeccato is False


def test_la_doppia_dalla_parte_della_trasferta_si_giudica_uguale():
    vicine = {(ospite, casa): p for (casa, ospite), p in _VICINE.items()}
    prob = _probabilita(vicine)

    assert _riga(prob, 1, 1).esito_logico == "X2"
    assert _riga(prob, 1, 1).esito_logico_azzeccato is True
    assert _riga(prob, 1, 2).esito_logico_azzeccato is True
    assert _riga(prob, 2, 1).esito_logico_azzeccato is False


def test_le_tre_palline_nascono_e_spariscono_insieme():
    """Una condizione sola per tre colonne, ed e' voluto.

    Le tre risposte vogliono le stesse due cose — la partita finita e un
    pronostico scritto in tempo — e legarle a una condizione a testa e' il
    modo di ritrovarsi un giorno con due palline e un buco.
    """
    from serie_a_analyzer.web.views import RigaPartita

    da_giocare = RigaPartita(
        id=1, casa="Inter", ospite="Lecce", casa_id=1, ospite_id=6,
        quando="sabato 22 ago, 18:30", giornata=1, conclusa=False,
        gol_casa=None, gol_ospite=None, prob=_probabilita(_VICINE),
    )
    assert da_giocare.giudicabile is False
    assert da_giocare.esito_logico_azzeccato is None

    senza_modello = _riga(None, 1, 1)
    assert senza_modello.giudicabile is False
    assert senza_modello.esito_logico_azzeccato is None


def test_l_hub_mostra_tre_colonne_di_giudizio(client):
    """La colonna c'e' in testata e in riga, su tutte e due le tabelle.

    L'hub e la giornata disegnano le righe con la STESSA macro, quindi una
    intestazione aggiunta a una sola delle due lascerebbe le palline sotto la
    colonna sbagliata senza che niente lo segnali.
    """
    hub = client.get("/").text
    assert ">A logica</th>" in hub

    giornata = client.get("/giornata/1?stagione=2026").text
    assert giornata.count(">A logica</th>") == 1


def test_una_riga_giudicata_porta_tre_palline(client_col_passato):
    """Tre cerchi per riga, non due: si contano nel corpo della tabella.

    Vuole il mondo con i pronostici gia' congelati: senza una riga di registro
    scritta prima del fischio la pagina non giudica niente, ed e' giusto cosi'
    — questo test non lo dimentichi e finisca per passare a vuoto.
    """
    import re

    corpo = re.search(r"<tbody>(.*?)</tbody>", client_col_passato.get("/").text, re.S)
    assert corpo is not None
    righe = re.findall(r"<tr>(.*?)</tr>", corpo.group(1), re.S)
    giudicate = [r for r in righe if "class=\"pallina\"" in r]
    assert giudicate, "nessuna riga giudicata nell'hub"
    for riga in giudicate:
        assert riga.count("class=\"pallina\"") == 3


def test_l_hub_scrive_l_esito_a_logica(client):
    r = client.get("/")
    assert ">A logica<" in r.text


def test_la_scheda_partita_scrive_l_esito_a_logica(client, motore):
    futura = motore.snapshot().dataset.upcoming()[0]
    r = client.get(f"/partita/{futura.api_id}")
    assert ">A logica<" in r.text


def test_l_esito_a_logica_non_prende_una_percentuale_accanto(client, motore):
    """L'assenza e' voluta, e dal 9 settembre 2026 e' l'unica cosa che dice
    che quella sigla non e' misurata.

    Prima glielo diceva un paragrafo accanto; le spiegazioni del sito sono
    finite in ISTRUZIONI.txt, e questa riga e' quel che resta a difendere il
    confine. Le altre due sigle — l'esito e il Gol/NoGol — la percentuale la
    hanno apposta, perche' sono misurate; questa no, perche' non lo e'.
    """
    import re

    futura = motore.snapshot().dataset.upcoming()[0]
    for pagina in (client.get("/").text, client.get(f"/partita/{futura.api_id}").text):
        for blocco in re.findall(r"A logica</span>\s*<b[^>]*>(.*?)</b>", pagina, re.S):
            assert "%" not in blocco, blocco


# ---- le pagine mostrano i numeri, le istruzioni stanno in un file ------------
#
# Deciso dall'utente il 9 settembre 2026: le spiegazioni stavano nella colonna
# accanto a ogni tabella e sporcavano la pagina. Sono state tolte tutte e messe
# in ISTRUZIONI.txt, che si legge quando serve.
#
# Le due righe qui sotto sono il confine, e vale la pena dire perche' sono
# scritte in un test invece che in un commento. Ogni volta che qualcuno
# aggiunge una sezione al sito ha in mano un fatto misurato e la voglia di
# raccontarlo li' accanto: e' cosi' che quelle ottantasei paragrafi sono nati,
# uno per volta e ognuno con una buona ragione. Il posto dove raccontarlo
# adesso e' ISTRUZIONI.txt.
#
# Cio' che PUO' restare in pagina e' un fatto sull'archivio — "troppi pochi
# dati", "tabellino non ancora definitivo", un parametro misurato — e ha una
# classe sua, `.dato`, apposta perche' il nome dica da solo quando e' nel posto
# sbagliato.


def test_i_template_non_contengono_spiegazioni():
    """Nessun `.spiega` e nessuna legenda: quelle classi non esistono piu'."""
    from pathlib import Path

    cartella = Path(app_module.__file__).parent / "templates"
    template = sorted(cartella.glob("*.html"))
    assert template, "premessa: i template si trovano"

    for f in template:
        testo = f.read_text(encoding="utf-8")
        assert 'class="spiega"' not in testo, f.name
        assert 'class="chiave"' not in testo, f.name


def test_il_file_delle_istruzioni_esiste_e_parla_di_ogni_pagina():
    """Toglierle dal sito senza scriverle da nessuna parte sarebbe perderle."""
    from pathlib import Path

    istruzioni = Path(app_module.__file__).parents[3] / "ISTRUZIONI.txt"
    assert istruzioni.exists(), istruzioni

    testo = istruzioni.read_text(encoding="utf-8")
    for pagina in ("LA PRIMA PAGINA", "LA SCHEDA PARTITA", "IL REGISTRO",
                   "IL CALENDARIO", "I GIOCATORI"):
        assert pagina in testo, pagina
