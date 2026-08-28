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
    """
    r = client.get("/partita/9001")
    assert "Attenzione all'Over/Under" in r.text


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
    assert "La barra da sola non basta" in r.text


def test_un_archivio_fresco_non_grida_al_lupo(client):
    """L'avviso sulla vecchiaia compare solo quando c'e' vecchiaia.

    Un avviso che c'e' sempre non lo legge piu' nessuno, ed e' esattamente
    cosi' che un limite dichiarato torna a essere un limite nascosto.
    """
    r = client.get("/partita/9001")
    assert "Su cosa si basa" in r.text
    assert "La barra da sola non basta" not in r.text
    assert "dati vecchi" not in r.text


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
    assert client.get("/aggiorna").status_code == 200
    assert motore.snapshot() is not primo


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
    """Una classifica senza annata sopra sarebbe la somma di quattro campionati."""
    r = client.get("/giocatori")

    assert r.status_code == 200
    assert "un campionato per volta" in r.text


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
    assert "non e&#39; ancora definitivo" in r.text or "non e' ancora definitivo" in r.text


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


# ---- i tre gradini del giudizio ---------------------------------------------
#
# Nell'elenco degli ultimi risultati la pastiglia in fondo alla riga dice
# quanto ci ha preso il modello, e sono tre cose diverse: aver nominato il
# punteggio, aver azzeccato solo chi vince, non aver azzeccato niente. I test
# lavorano su matrici costruite a mano perche' e' l'unico modo di decidere
# quale sia il risultato piu' probabile invece di sperarci.


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


def test_il_punteggio_nominato_e_il_gradino_alto():
    riga = _riga(_probabilita(_DAVA_2_1), 2, 1)
    assert riga.precisione == "esatto"
    assert riga.precisione_testo == "azzeccato"


def test_azzeccare_solo_chi_vince_e_il_gradino_di_mezzo():
    """Vittoria interna si', ma 3-0 non e' il 2-1 che il modello aveva nominato."""
    riga = _riga(_probabilita(_DAVA_2_1), 3, 0)
    assert riga.precisione == "esito"
    # La parola e' la stessa del gradino sopra: a distinguerli e' il colore.
    assert riga.precisione_testo == "azzeccato"


def test_l_esito_sbagliato_e_il_gradino_basso():
    riga = _riga(_probabilita(_DAVA_2_1), 0, 2)
    assert riga.precisione == "niente"
    assert riga.precisione_testo == "sbagliato"


def test_senza_pronostico_non_si_giudica_niente():
    """Il modello non conosceva le squadre: non c'e' niente da promuovere."""
    riga = _riga(None, 2, 1)
    assert riga.precisione is None
    assert riga.precisione_testo == ""


def test_una_partita_da_giocare_non_si_giudica():
    from serie_a_analyzer.web.views import RigaPartita

    riga = RigaPartita(
        id=1, casa="Inter", ospite="Lecce", casa_id=1, ospite_id=6,
        quando="sabato 22 ago, 18:30", giornata=1, conclusa=False,
        gol_casa=None, gol_ospite=None, prob=_probabilita(_DAVA_2_1),
    )
    assert riga.precisione is None


def test_il_punteggio_giusto_col_segno_sbagliato_resta_il_gradino_alto():
    """Il caso raro in cui i due controlli non dicono la stessa cosa.

    Il punteggio piu' probabile e' 1-1, ma sommando *tutte* le vittorie interne
    si supera la somma di *tutti* i pareggi: il favorito e' l'1 mentre il
    risultato nominato e' un pareggio. Finisce 1-1: il modello aveva detto
    come sarebbe andata a finire, e la pagina non puo' chiamarlo sbagliato.
    Per questo l'etichetta parla di risultato e non di esito.
    """
    prob = _probabilita({
        (1, 1): 0.20, (0, 0): 0.05, (2, 2): 0.05,      # pareggi: 0.30
        (1, 0): 0.12, (2, 0): 0.12, (2, 1): 0.12, (3, 0): 0.06,  # casa: 0.42
        (0, 1): 0.16, (0, 2): 0.12,                    # trasferta: 0.28
    })
    riga = _riga(prob, 1, 1)

    assert riga.esito_previsto == "1"
    assert riga.pronostico_azzeccato is False
    assert riga.precisione == "esatto"


def test_l_hub_disegna_i_tre_gradini(client_col_passato):
    """La pastiglia c'e' e porta la classe del gradino, che e' cio' che tinge."""
    r = client_col_passato.get("/")
    assert 'class="verdetto esatto"' in r.text \
        or 'class="verdetto esito"' in r.text \
        or 'class="verdetto niente"' in r.text
    # Il verdetto non prende piu' l'alfabeto degli esiti: indaco, grigio e
    # arancio restano ai fatti (punteggio e casella accaduta), qui si giudica.
    assert "verdetto ok tinta-" not in r.text


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


def test_la_scheda_dice_quale_dei_due_e_il_pronostico_grande(motore, monkeypatch):
    """Se non lo dicesse, i due numeri diversi in pagina si contraddirebbero."""
    _pronostico_con_le_formazioni(motore, 9001)
    monkeypatch.setattr(app_module, "_engine", motore)

    pagina = TestClient(app_module.app).get("/partita/9001").text

    assert "in cima alla pagina e' quello <b>secco</b>" in pagina


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
    verdetto = verdetto_calibrazione(fasce)

    assert saldo == pytest.approx(0.0), "premessa del test"
    assert verdetto["scarto_medio"] == pytest.approx(0.20)
    assert verdetto["n"] == 100


def test_senza_fasce_non_c_e_verdetto():
    assert verdetto_calibrazione([]) is None


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
