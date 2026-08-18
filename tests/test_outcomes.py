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
    esito_verificato,
    risolvi_partita,
    risolvi_pronostici,
)
from serie_a_analyzer.data.predictions import PredictionStore, righe_mercati
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


def _pronostica(store, fixture_id=1, modello=None):
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
