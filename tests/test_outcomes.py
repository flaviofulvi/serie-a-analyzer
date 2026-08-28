"""Test del confronto fra pronostico e realta'.

Due cose vanno difese qui. La prima e' che ogni esito venga risolto **giusto**:
un segno sbagliato in una di queste funzioni produrrebbe un'accuratezza
plausibile e falsa, il tipo di errore che non si scopre mai. La seconda e' che
ogni mercato che il sistema sa salvare lo sappia anche risolvere — altrimenti
resterebbe nello storico senza entrare mai nel conto, e nessuno se ne
accorgerebbe.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from serie_a_analyzer.data.outcomes import (
    SOGLIA_SIGNIFICATIVITA,
    EsitoNonRisolvibile,
    accuratezza_per_mercato,
    accuratezza_per_selezione,
    brier,
    brier_1x2_multiclasse,
    calibrazione,
    confronto_fra_modi,
    elenco_pronostici,
    esito_verificato,
    risolvi_partita,
    risolvi_pronostici,
)
from serie_a_analyzer.data.predictions import (
    MODE_SECCO,
    MODE_UFFICIALE,
    PredictionStore,
    righe_mercati,
)
from serie_a_analyzer.model.dixon_coles import FittedModel
from serie_a_analyzer.model.markets import derive_markets

ADESSO = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
FISCHIO = ADESSO + timedelta(days=2)
DOPO = FISCHIO + timedelta(hours=2)


@dataclass
class PartitaFinta:
    api_id: int
    kickoff_utc: datetime
    home_team_id: int
    away_team_id: int
    home_goals: int | None = None
    away_goals: int | None = None

    @property
    def is_finished(self) -> bool:
        return self.home_goals is not None and self.away_goals is not None


def _modello(base: float = 0.2) -> FittedModel:
    return FittedModel(
        teams=("Inter", "Torino"),
        attack={"Inter": 0.35, "Torino": -0.10},
        defense={"Inter": 0.20, "Torino": -0.05},
        home_advantage=0.25,
        base=base,
        rho=-0.05,
        n_matches=380,
        effective_sample_size=210.0,
        fitted_on=date(2026, 8, 20),
        converged=True,
    )


@pytest.fixture
def store(tmp_path):
    return PredictionStore(path=tmp_path / "pronostici.db")


def _pronostica(store, fixture_id=1, modello=None, mode=MODE_SECCO):
    modello = modello or _modello()
    matrice = modello.scoreline("Inter", "Torino")
    lam, mu = modello.expected_goals("Inter", "Torino")
    return store.registra(
        fixture_id=fixture_id,
        kickoff_utc=FISCHIO,
        home_team_id=505,
        away_team_id=503,
        model_version_id=store.versione_modello(modello),
        matrice=matrice,
        probabilita=derive_markets(matrice),
        lambda_home=lam,
        lambda_away=mu,
        rho=modello.rho,
        mode=mode,
        now=ADESSO,
    )


# ---- si e' verificato? ------------------------------------------------------


@pytest.mark.parametrize("market, selection, casa, ospite, atteso", [
    # 1X2 sul 2-1, poi sul pareggio, poi sulla sconfitta interna.
    ("1x2", "home", 2, 1, True),
    ("1x2", "draw", 2, 1, False),
    ("1x2", "away", 2, 1, False),
    ("1x2", "draw", 1, 1, True),
    ("1x2", "away", 0, 3, True),
    # Doppia chance: il pareggio sta dalla parte di entrambe.
    ("doppia_chance", "home_or_draw", 1, 1, True),
    ("doppia_chance", "away_or_draw", 1, 1, True),
    ("doppia_chance", "home_or_away", 1, 1, False),
    ("doppia_chance", "home_or_draw", 0, 1, False),
    ("doppia_chance", "home_or_away", 2, 1, True),
    # Over/Under: linee semi-intere, nessun pareggio possibile.
    ("over_under", "over_2.5", 2, 1, True),
    ("over_under", "under_2.5", 2, 1, False),
    ("over_under", "over_2.5", 1, 1, False),
    ("over_under", "under_2.5", 1, 1, True),
    ("over_under", "over_0.5", 0, 0, False),
    ("over_under", "under_0.5", 0, 0, True),
    ("over_under", "over_4.5", 3, 2, True),
    # Gol/NoGol.
    ("btts", "yes", 1, 1, True),
    ("btts", "yes", 3, 0, False),
    ("btts", "no", 3, 0, True),
    ("btts", "no", 0, 0, True),
    # Clean sheet: quello di casa e' l'ospite che non segna.
    ("clean_sheet", "home", 2, 0, True),
    ("clean_sheet", "home", 2, 1, False),
    ("clean_sheet", "away", 0, 1, True),
    ("clean_sheet", "away", 1, 1, False),
    # Risultato esatto, nel verso giusto.
    ("risultato_esatto", "2-1", 2, 1, True),
    ("risultato_esatto", "2-1", 1, 2, False),
    ("risultato_esatto", "0-0", 0, 0, True),
])
def test_gli_esiti_vengono_risolti_correttamente(market, selection, casa, ospite, atteso):
    assert esito_verificato(market, selection, casa, ospite) is atteso


def test_ogni_mercato_salvato_sa_risolversi():
    """La rete di sicurezza: un mercato nuovo senza risolutore fa fallire qui.

    Senza questo test, aggiungere un mercato e dimenticare il suo risolutore
    darebbe un sistema che funziona, salva, e non misura mai quel mercato.
    """
    modello = _modello()
    probabilita = derive_markets(modello.scoreline("Inter", "Torino"))

    for market, selection, _ in righe_mercati(probabilita):
        esito = esito_verificato(market, selection, 2, 1)
        assert isinstance(esito, bool), f"{market}/{selection} non risolto"


def test_un_mercato_sconosciuto_si_fa_sentire():
    with pytest.raises(EsitoNonRisolvibile, match="mercato senza risolutore"):
        esito_verificato("marcatore", "player:278", 2, 1)


def test_una_selezione_malformata_si_fa_sentire():
    with pytest.raises(EsitoNonRisolvibile):
        esito_verificato("over_under", "over_due-e-mezzo", 2, 1)


def test_il_brier_premia_la_fiducia_meritata():
    assert brier(1.0, True) == 0.0
    assert brier(0.0, True) == 1.0
    assert brier(0.5, True) == pytest.approx(0.25)
    assert brier(0.5, False) == pytest.approx(0.25)
    # Dire 90% e azzeccare costa molto meno che dire 60% e azzeccare.
    assert brier(0.9, True) < brier(0.6, True)


# ---- risoluzione -------------------------------------------------------------


def test_risolvere_una_partita_scrive_tutti_i_suoi_mercati(store):
    pid = _pronostica(store)
    attesi = len(store.mercati(pid))

    scritti = risolvi_partita(store, fixture_id=1, gol_casa=2, gol_ospite=1, now=DOPO)

    assert scritti == attesi
    assert store.conteggi()["esiti_risolti"] == attesi


def test_ripassare_sulla_stessa_partita_non_riscrive_nulla(store):
    """Un esito registrato e' un fatto: non si ricalcola a ogni giro."""
    _pronostica(store)
    risolvi_partita(store, 1, 2, 1, now=DOPO)

    assert risolvi_partita(store, 1, 2, 1, now=DOPO) == 0


def test_un_esito_registrato_resta_quello_che_era(store):
    """Anche se qualcuno ripassasse con un risultato diverso."""
    _pronostica(store)
    risolvi_partita(store, 1, 2, 1, now=DOPO)
    prima = {r["id"]: r["hit"] for r in store.esiti_risolti(market="1x2")}

    risolvi_partita(store, 1, 0, 3, now=DOPO)

    assert {r["id"]: r["hit"] for r in store.esiti_risolti(market="1x2")} == prima


def test_l_1x2_ha_esattamente_un_esito_verificato(store):
    """Gli esiti sono mutuamente esclusivi: uno vero e due falsi, sempre."""
    _pronostica(store)
    risolvi_partita(store, 1, 2, 1, now=DOPO)

    esiti = store.esiti_risolti(market="1x2")
    assert sum(1 for r in esiti if r["hit"]) == 1
    assert next(r["selection"] for r in esiti if r["hit"]) == "home"


def test_si_risolvono_solo_le_partite_finite(store):
    _pronostica(store, fixture_id=1)
    _pronostica(store, fixture_id=2)

    esito = risolvi_pronostici(store, [
        PartitaFinta(1, FISCHIO, 505, 503, home_goals=2, away_goals=1),
        PartitaFinta(2, FISCHIO, 505, 503),   # non ancora giocata
    ], now=DOPO)

    assert esito["partite"] == 1
    assert esito["ancora_in_attesa"] == 1
    assert store.mercati_da_risolvere(1) == []
    assert store.mercati_da_risolvere(2) != []


def test_senza_pronostici_in_sospeso_non_si_fa_nulla(store):
    assert risolvi_pronostici(store, [], now=DOPO) == {
        "partite": 0, "esiti": 0, "ancora_in_attesa": 0
    }


def test_una_partita_senza_pronostico_non_diventa_un_esito(store):
    """Lo storico misura cio' che era stato detto, non cio' che si sa oggi."""
    _pronostica(store, fixture_id=1)

    risolvi_pronostici(store, [
        PartitaFinta(1, FISCHIO, 505, 503, home_goals=2, away_goals=1),
        PartitaFinta(77, FISCHIO, 505, 503, home_goals=1, away_goals=0),
    ], now=DOPO)

    assert {r["fixture_id"] for r in store.esiti_risolti()} == {1}


# ---- metriche ----------------------------------------------------------------


def test_l_accuratezza_riporta_ogni_mercato_misurato(store):
    _pronostica(store)
    risolvi_partita(store, 1, 2, 1, now=DOPO)

    per_mercato = {v["mercato"]: v for v in accuratezza_per_mercato(store)}

    assert set(per_mercato) == {
        "1x2", "doppia_chance", "over_under", "btts", "clean_sheet", "risultato_esatto"
    }
    uno_x_due = per_mercato["1x2"]
    assert uno_x_due["n"] == 3
    assert 0.0 <= uno_x_due["brier"] <= 1.0
    assert uno_x_due["tasso_reale"] == pytest.approx(1 / 3)


def test_pochi_esiti_non_fanno_una_misura(store):
    """Il numero c'e', ma dichiara di non valere: e' il punto."""
    _pronostica(store)
    risolvi_partita(store, 1, 2, 1, now=DOPO)

    for voce in accuratezza_per_mercato(store):
        assert voce["n"] < SOGLIA_SIGNIFICATIVITA
        assert voce["significativo"] is False


def test_una_misura_diventa_significativa_con_abbastanza_partite(store):
    for fixture_id in range(1, 21):
        _pronostica(store, fixture_id=fixture_id)
        risolvi_partita(store, fixture_id, 2, 1, now=DOPO)

    uno_x_due = next(v for v in accuratezza_per_mercato(store) if v["mercato"] == "1x2")

    assert uno_x_due["n"] == 60           # 20 partite x 3 esiti
    assert uno_x_due["significativo"] is True


def test_il_brier_dell_1x2_e_confrontabile_col_backtest(store):
    """Somma sui tre esiti, per partita: la stessa forma del backtest."""
    _pronostica(store, fixture_id=1)
    _pronostica(store, fixture_id=2)
    risolvi_partita(store, 1, 2, 1, now=DOPO)
    risolvi_partita(store, 2, 0, 2, now=DOPO)

    multi = brier_1x2_multiclasse(store)
    per_selezione = next(v for v in accuratezza_per_mercato(store) if v["mercato"] == "1x2")

    assert multi["n_partite"] == 2
    # Tre selezioni per partita: la somma per partita e' il triplo della media.
    assert multi["brier"] == pytest.approx(per_selezione["brier"] * 3)
    assert multi["baseline_caso"] == pytest.approx(2 / 3)


def test_senza_esiti_non_si_inventa_un_punteggio(store):
    assert accuratezza_per_mercato(store) == []
    assert brier_1x2_multiclasse(store) is None
    assert calibrazione(store) == []


def test_la_calibrazione_raggruppa_per_fascia_di_fiducia(store):
    for fixture_id in range(1, 11):
        _pronostica(store, fixture_id=fixture_id)
        # Meta' vittorie interne, meta' no: le fasce hanno qualcosa da dire.
        gol = (2, 1) if fixture_id % 2 else (0, 1)
        risolvi_partita(store, fixture_id, *gol, now=DOPO)

    fasce = calibrazione(store, market="1x2", selection="home")

    assert fasce, "nessuna fascia calcolata"
    assert sum(f["n"] for f in fasce) == 10
    for f in fasce:
        assert 0.0 <= f["previsto"] <= 1.0
        assert 0.0 <= f["reale"] <= 1.0


def test_la_baseline_di_un_mercato_non_mescola_le_sue_linee(store):
    """Over 0.5 e Over 4.5 hanno frequenze opposte: mediarle regala un muro basso.

    Sommate insieme, le linee di Over/Under si compensano fino a una frequenza
    del 50% — che darebbe baseline 0.25, molto piu' generosa del vero. La
    baseline va calcolata per singola selezione e poi mediata.
    """
    for fixture_id in range(1, 21):
        _pronostica(store, fixture_id=fixture_id)
        risolvi_partita(store, fixture_id, 2, 1, now=DOPO)

    per_mercato = {v["mercato"]: v for v in accuratezza_per_mercato(store)}
    over_under = per_mercato["over_under"]

    # Ogni partita finisce 2-1: over 0.5/1.5/2.5 sempre veri, over 3.5/4.5 mai.
    # Ogni singola selezione ha quindi frequenza 0 o 1, e baseline 0.
    assert over_under["tasso_reale"] == pytest.approx(0.5)
    assert over_under["baseline"] == pytest.approx(0.0), "baseline mediata sulle linee"


def test_l_accuratezza_per_selezione_separa_le_linee(store):
    for fixture_id in range(1, 6):
        _pronostica(store, fixture_id=fixture_id)
        risolvi_partita(store, fixture_id, 2, 1, now=DOPO)

    per_selezione = {
        (v["mercato"], v["selezione"]): v for v in accuratezza_per_selezione(store)
    }

    assert per_selezione[("over_under", "over_2.5")]["tasso_reale"] == 1.0
    assert per_selezione[("over_under", "over_4.5")]["tasso_reale"] == 0.0
    assert per_selezione[("1x2", "home")]["tasso_reale"] == 1.0
    assert per_selezione[("1x2", "away")]["tasso_reale"] == 0.0


# ---- i due tipi di pronostico non si mescolano ------------------------------
#
# Una partita puo' averne due: quello secco e quello rifatto quando escono le
# formazioni ufficiali. Sono due risposte alla stessa domanda, non due
# osservazioni della stessa cosa, e nessuna media puo' sommarle. Il giorno in
# cui `data/ufficiali.py` comincia a scrivere, questi test sono l'unica cosa
# che impedisce a ogni numero mostrato di raddoppiare in silenzio.


def _due_modi(store, fixture_id, casa=2, ospite=1):
    """La stessa partita pronosticata in tutti e due i modi, poi risolta.

    I due pronostici partono da modelli diversi — basta un `base` diverso —
    perche' altrimenti avrebbero la stessa versione del modello e il registro
    rifiuterebbe il secondo come duplicato.
    """
    _pronostica(store, fixture_id=fixture_id, mode=MODE_SECCO)
    _pronostica(
        store, fixture_id=fixture_id, modello=_modello(base=0.35), mode=MODE_UFFICIALE
    )
    risolvi_partita(store, fixture_id, casa, ospite, now=DOPO)


def test_gli_esiti_risolti_dicono_da_quale_modo_vengono(store):
    _due_modi(store, fixture_id=1)

    modi = {r["mode"] for r in store.esiti_risolti()}
    assert modi == {MODE_SECCO, MODE_UFFICIALE}

    solo_secco = store.esiti_risolti(mode=MODE_SECCO)
    assert solo_secco, "il filtro non deve svuotare quello che c'e'"
    assert {r["mode"] for r in solo_secco} == {MODE_SECCO}


def test_l_accuratezza_non_conta_due_volte_la_stessa_partita(store):
    """Il difetto che questo test esiste per impedire.

    Prima del filtro su `mode`, una partita con tutti e due i pronostici
    entrava due volte in ogni media: il numero di esiti raddoppiava e i due
    Brier — che sono proprio quelli da confrontare — finivano mediati fra loro.
    """
    for fixture_id in (1, 2, 3):
        _due_modi(store, fixture_id=fixture_id)

    solo_secco = {v["mercato"]: v for v in accuratezza_per_mercato(store)}
    entrambi = {
        v["mercato"]: v
        for v in accuratezza_per_mercato(store, mode=MODE_UFFICIALE)
    }

    # Tre partite, tre selezioni di 1X2 ciascuna: nove esiti, non diciotto.
    assert solo_secco["1x2"]["n"] == 9
    assert entrambi["1x2"]["n"] == 9
    # E i due Brier restano due numeri distinti, non uno solo mediato.
    assert solo_secco["1x2"]["brier"] != pytest.approx(entrambi["1x2"]["brier"])


def test_il_brier_per_partita_conta_partite_e_non_pronostici(store):
    """`n_partite` deve dire partite anche quando i pronostici sono il doppio."""
    for fixture_id in (1, 2):
        _due_modi(store, fixture_id=fixture_id)

    assert brier_1x2_multiclasse(store)["n_partite"] == 2
    assert brier_1x2_multiclasse(store, mode=MODE_UFFICIALE)["n_partite"] == 2


def test_la_calibrazione_guarda_un_modo_alla_volta(store):
    for fixture_id in (1, 2):
        _due_modi(store, fixture_id=fixture_id)

    fasce = calibrazione(store, market="1x2", selection="home")
    assert sum(f["n"] for f in fasce) == 2, "una fascia per partita, non per pronostico"


def test_il_confronto_fra_modi_usa_solo_le_partite_in_comune(store):
    """Il secco esiste sempre, l'ufficiale solo dove i pesi c'erano.

    Misurarli su popolazioni diverse direbbe quale gruppo di partite era piu'
    facile da indovinare, non quale pronostico era migliore.
    """
    _due_modi(store, fixture_id=1)
    _due_modi(store, fixture_id=2)
    # Questa ha solo il pronostico secco: deve restare fuori dal confronto.
    _pronostica(store, fixture_id=3, mode=MODE_SECCO)
    risolvi_partita(store, 3, 0, 3, now=DOPO)

    confronto = confronto_fra_modi(store)

    assert confronto["partite"] == 2
    uno_x_due = next(v for v in confronto["mercati"] if v["mercato"] == "1x2")
    assert uno_x_due["n"] == 6, "tre selezioni per due partite in comune"
    # Il secco da solo ne avrebbe tre in piu': la terza partita e' esclusa.
    assert accuratezza_per_mercato(store)[0]["n"] > uno_x_due["n"]


def test_il_confronto_fra_modi_tace_finche_non_c_e_niente_da_confrontare(store):
    """E' la situazione di oggi: nessun pronostico con le formazioni."""
    _pronostica(store, fixture_id=1)
    risolvi_partita(store, 1, 2, 1, now=DOPO)

    confronto = confronto_fra_modi(store)

    assert confronto["totale"] is None
    assert confronto["mercati"] == []
    assert confronto["partite"] == 0


def test_il_segno_dello_scarto_dice_chi_ha_fatto_meglio(store):
    """Negativo = le formazioni hanno migliorato. Un segno girato qui mentirebbe."""
    # Il modello 'ufficiale' e' piu' sbilanciato verso la casa, e la casa vince:
    # quindi deve risultare migliore, cioe' scarto negativo.
    _pronostica(store, fixture_id=1, mode=MODE_SECCO)
    _pronostica(
        store, fixture_id=1, modello=_modello(base=0.6), mode=MODE_UFFICIALE
    )
    risolvi_partita(store, 1, 3, 0, now=DOPO)

    totale = confronto_fra_modi(store)["totale"]

    atteso = totale["brier_secondo"] - totale["brier_primo"]
    assert totale["scarto"] == pytest.approx(atteso)
    assert totale["il_secondo_e_migliore"] is (totale["scarto"] < 0)


# ---- una partita ri-pronosticata non conta piu' volte ------------------------
#
# Il difetto gemello di quello qui sopra, e piu' insidioso perche' non aspetta
# nessuna accensione: succede gia'. Il registro scrive una riga per ogni
# versione del modello, quindi una partita vista con qualche giorno di anticipo
# ne accumula quante sono le ristime che la precedono — il 22 agosto 2026
# Genoa-Lecce ne aveva cinque, scritte tutte nello stesso giorno. Sono cinque
# tentativi sulla stessa domanda, non cinque osservazioni: mediarli conta la
# partita cinque volte e ci mette dentro anche la prima stima acerba col peso
# di quelle mature.
#
# Il danno vero e' che gonfia `n`, e sopra `n` sta la soglia che decide se una
# misura merita un verdetto: con dieci partite in archivio il sito dichiarava
# "meglio della baseline" su ogni mercato.


def _due_versioni(store, fixture_id, casa=2, ospite=1, primo_base=1.60):
    """La stessa partita pronosticata due volte prima del via, poi risolta.

    Il primo pronostico esce da un modello volutamente storto (`base` alto
    significa una valanga di gol attesi), il secondo da quello assestato: e'
    la situazione vera del 22 agosto, dove la prima stima della giornata
    dichiarava 2.76 gol attesi in casa e l'ultima 1.60.

    I due `now` sono diversi perche' e' `created_at` a dire quale sia
    l'ultima, ed e' l'ordinamento che conta.
    """
    _pronostica(store, fixture_id=fixture_id, modello=_modello(base=primo_base))
    ultimo = _pronostica(store, fixture_id=fixture_id, modello=_modello())
    risolvi_partita(store, fixture_id, casa, ospite, now=DOPO)
    return ultimo


def test_gli_esiti_tengono_una_riga_sola_per_partita(store):
    """E dev'essere quella che le pagine mostrano, non una qualsiasi."""
    _due_versioni(store, fixture_id=1)

    righe = store.esiti_risolti(market="1x2")
    assert len({r["prediction_id"] for r in righe}) == 1
    assert len(righe) == 3, "i tre esiti dell'1X2, una volta sola"

    # La riga tenuta e' la stessa che `_riga` in web/app.py mette in pagina:
    # si misura cio' che si e' mostrato, altrimenti sono due sistemi diversi.
    atteso = store.ultimo_per_partita(1)["id"]
    assert {r["prediction_id"] for r in righe} == {atteso}


def test_ri_pronosticare_non_gonfia_i_conti(store):
    """Due versioni non devono valere due partite in nessuna misura."""
    for fixture_id in (1, 2, 3):
        _due_versioni(store, fixture_id=fixture_id)

    per_mercato = {v["mercato"]: v for v in accuratezza_per_mercato(store)}
    assert per_mercato["1x2"]["n"] == 9, "tre partite per tre selezioni, non sei"
    assert brier_1x2_multiclasse(store)["n_partite"] == 3

    fasce = calibrazione(store, market="1x2", selection="home")
    assert sum(f["n"] for f in fasce) == 3

    per_selezione = {
        (v["mercato"], v["selezione"]): v for v in accuratezza_per_selezione(store)
    }
    assert per_selezione[("1x2", "home")]["n"] == 3


def test_la_stima_acerba_non_pesa_sulla_media(store):
    """Il punto sostanziale, non il conteggio.

    Un pronostico buttato via prima del fischio d'inizio non deve lasciare
    traccia nel voto finale: il modello che si giudica e' quello che ha detto
    l'ultima parola. Qui il primo pronostico e' vistosamente storto, e il
    Brier deve venire identico a quello di una partita pronosticata **una
    volta sola** col modello buono.
    """
    _due_versioni(store, fixture_id=1)

    solo_buono = PredictionStore(path=store.path.parent / "confronto.db")
    _pronostica(solo_buono, fixture_id=1)
    risolvi_partita(solo_buono, 1, 2, 1, now=DOPO)

    assert brier_1x2_multiclasse(store)["brier"] == pytest.approx(
        brier_1x2_multiclasse(solo_buono)["brier"]
    )


def test_chi_vuole_tutte_le_versioni_puo_ancora_chiederle(store):
    """Il taglio e' il default, non un muro: serve a misurare, non a nascondere.

    Guardare *come* il modello ha cambiato idea avvicinandosi alla partita e'
    una domanda legittima — e' il motivo per cui il registro conserva tutte le
    righe. Semplicemente non e' una misura.
    """
    _due_versioni(store, fixture_id=1)

    tutte = store.esiti_risolti(market="1x2", tutte_le_versioni=True)
    assert len({r["prediction_id"] for r in tutte}) == 2
    assert len(tutte) == 6


def test_il_taglio_non_confonde_i_due_modi(store):
    """Una riga per partita **e per modo**, mai una per partita e basta.

    Se il taglio guardasse il solo `fixture_id`, l'ultimo pronostico scritto
    cancellerebbe l'altro modo e `confronto_fra_modi` — che esiste per
    metterli a confronto — non troverebbe piu' niente da confrontare.
    """
    _pronostica(store, fixture_id=1, modello=_modello(base=1.60))
    _pronostica(store, fixture_id=1)
    _pronostica(store, fixture_id=1, modello=_modello(base=0.35), mode=MODE_UFFICIALE)
    risolvi_partita(store, 1, 2, 1, now=DOPO)

    righe = store.esiti_risolti(market="1x2")
    assert {r["mode"] for r in righe} == {MODE_SECCO, MODE_UFFICIALE}
    # Due modi, tre selezioni: sei righe. La versione scartata e' solo quella
    # vecchia del secco.
    assert len(righe) == 6
    assert len({r["prediction_id"] for r in righe}) == 2


# ---- il registro, riga per riga ---------------------------------------------
#
# Non e' una misura e per questo va provato a parte: le misure devono saltare
# cio' che non si e' ancora potuto misurare, il registro deve mostrarlo. Le due
# regole si assomigliano abbastanza da poter essere scambiate per sbaglio.


def test_l_elenco_mostra_anche_i_pronostici_non_ancora_verificati(store):
    """Una partita da giocare non e' misurabile, ma e' registrata: si vede.

    E' la differenza fra questa funzione e `esiti_risolti`, ed e' anche la
    ragione per cui la pagina esiste: la promessa "non lo cambiero' piu'" e'
    interessante soprattutto **prima** che si sappia come e' andata.
    """
    _pronostica(store, fixture_id=1)
    _pronostica(store, fixture_id=2)
    risolvi_partita(store, 1, 2, 1, now=DOPO)

    elenco = {v["fixture_id"]: v for v in elenco_pronostici(store)}

    assert set(elenco) == {1, 2}
    assert elenco[1]["risolto"] is True
    assert elenco[2]["risolto"] is False
    assert elenco[2]["brier"] is None, "senza esito non esiste un punteggio"
    assert elenco[2]["riuscite"] == []
    # Le probabilita' ci sono comunque: sono cio' che il pronostico dice.
    assert set(elenco[2]["selezioni"]) == {"home", "draw", "away"}


def test_il_brier_di_riga_e_la_somma_dei_tre_esiti(store):
    """La stessa forma del backtest: 0 perfetto, 0.667 tirando a caso."""
    _pronostica(store, fixture_id=1)
    risolvi_partita(store, 1, 2, 1, now=DOPO)

    voce = elenco_pronostici(store)[0]

    atteso = sum(
        r["brier"] for r in store.esiti_risolti(market="1x2")
        if r["fixture_id"] == 1
    )
    assert voce["brier"] == pytest.approx(atteso)
    assert 0.0 <= voce["brier"] <= 2.0
    assert voce["riuscite"] == ["home"], "la casa ha vinto 2-1"


def test_l_elenco_non_mescola_i_due_modi_ma_li_mostra_tutti_e_due(store):
    """Qui i due tipi convivono, ed e' l'opposto di cio' che fanno le misure.

    Una media che li sommasse conterebbe due volte la stessa partita; un
    elenco che ne nascondesse uno nasconderebbe proprio la riga che il lavoro
    delle formazioni ufficiali esiste per produrre.
    """
    _due_modi(store, fixture_id=1)

    elenco = elenco_pronostici(store)

    assert len(elenco) == 2
    assert {v["mode"] for v in elenco} == {MODE_SECCO, MODE_UFFICIALE}
    assert {v["fixture_id"] for v in elenco} == {1}
    # Nessuno dei due punteggi e' la media dell'altro: sono due righe intere.
    assert all(v["brier"] is not None for v in elenco)


def test_l_elenco_sa_dire_quando_e_stato_scritto(store):
    """Serve alla pagina per dire di quanto ha preceduto il fischio d'inizio."""
    _pronostica(store, fixture_id=1)

    voce = elenco_pronostici(store)[0]

    assert voce["scritto_il"] == ADESSO
    assert voce["scritto_il"] < FISCHIO


def test_un_elenco_vuoto_non_e_un_guasto(store):
    assert elenco_pronostici(store) == []
