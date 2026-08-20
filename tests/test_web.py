"""Test delle pagine web.

Il motore viene costruito su un archivio finto in una cartella temporanea e con
``seasons=()``, cosi' le pagine non toccano mai la rete: l'intero percorso
archivio -> dataset -> modello -> HTML gira in memoria.

Cio' che si verifica non e' l'aspetto grafico ma le tre cose che rompono
davvero una pagina di pronostici: che i numeri arrivino, che l'assenza di dati
venga detta invece che nascosta, e che il modello non venga ristimato a ogni
richiesta.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from serie_a_analyzer.config import Settings
from serie_a_analyzer.web import app as app_module
from serie_a_analyzer.web.engine import Engine

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
        cache_dir=tmp_path / "cache",
        budget_file=tmp_path / "budget.json",
        store_dir=tmp_path / "serie_a",
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


@pytest.fixture
def client(motore, monkeypatch):
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
        api_key="k", api_provider="apisports", daily_request_limit=100,
        cache_dir=tmp_path / "c", budget_file=tmp_path / "b.json",
        store_dir=tmp_path / "s", journal_file=tmp_path / "j.json",
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
