"""Test della stima delle forze.

Il test centrale e' quello di recupero: si genera una stagione da forze note e
si verifica che il modello le ritrovi. E' l'unico modo per sapere se il motore
funziona prima di avere dati reali.
"""

from datetime import date, timedelta

import numpy as np
import pytest

from serie_a_analyzer.model.dixon_coles import Match, comprimi_totale, fit_dixon_coles
from serie_a_analyzer.model.simulation import generate_league


@pytest.fixture(scope="module")
def lega():
    # Tre stagioni: con una sola il rumore su 380 partite e' ancora alto.
    return generate_league(n_teams=20, n_rounds=6, seed=7)


@pytest.fixture(scope="module")
def modello(lega):
    # Emivita lunga: qui le forze vere sono costanti nel tempo, non vogliamo
    # scartare meta' del campione.
    return fit_dixon_coles(lega.matches, half_life_days=2000.0, regularization=0.5)


def test_il_modello_converge(modello, lega):
    assert modello.converged
    assert modello.n_matches == len(lega.matches)
    assert set(modello.teams) == set(lega.true_attack)


def test_recupera_le_forze_di_attacco(modello, lega):
    stimato = np.array([modello.attack[t] for t in modello.teams])
    vero = np.array([lega.true_attack[t] for t in modello.teams])
    correlazione = np.corrcoef(stimato, vero)[0, 1]
    assert correlazione > 0.9, f"correlazione attacco troppo bassa: {correlazione:.3f}"


def test_recupera_le_forze_di_difesa(modello, lega):
    stimato = np.array([modello.defense[t] for t in modello.teams])
    vero = np.array([lega.true_defense[t] for t in modello.teams])
    correlazione = np.corrcoef(stimato, vero)[0, 1]
    assert correlazione > 0.9, f"correlazione difesa troppo bassa: {correlazione:.3f}"


def test_recupera_il_fattore_campo(modello, lega):
    assert modello.home_advantage == pytest.approx(lega.true_home_advantage, abs=0.06)


def test_recupera_il_livello_di_gol(modello, lega):
    assert modello.base == pytest.approx(lega.true_base, abs=0.08)


def test_parametri_centrati_a_somma_zero(modello):
    assert sum(modello.attack.values()) == pytest.approx(0.0, abs=1e-8)
    assert sum(modello.defense.values()) == pytest.approx(0.0, abs=1e-8)


def test_il_fattore_campo_alza_i_gol_attesi(modello):
    a, b = modello.teams[0], modello.teams[1]
    lam_casa, _ = modello.expected_goals(a, b)
    _, mu_trasferta = modello.expected_goals(b, a)
    # Stesse squadre, ruoli invertiti: 'a' segna di piu' quando gioca in casa.
    assert lam_casa > mu_trasferta


def test_shrinkage_con_pochissimi_dati():
    """Con due partite le forze devono restare vicine alla media della lega."""
    partite = [
        Match("Alfa", "Beta", 5, 0, date(2025, 8, 24)),
        Match("Gamma", "Delta", 0, 4, date(2025, 8, 24)),
    ]
    m = fit_dixon_coles(partite, regularization=5.0)
    # Senza shrinkage il modello direbbe che l'Alfa e' infinitamente forte.
    assert all(abs(v) < 0.6 for v in m.attack.values())
    assert all(abs(v) < 0.6 for v in m.defense.values())


def test_le_partite_recenti_pesano_di_piu():
    """Stessa squadra, prima scarsa e poi fortissima: deve risultare forte."""
    oggi = date(2025, 8, 24)
    partite = []
    # Un anno fa: Alfa perde sempre.
    for k in range(12):
        partite.append(Match("Alfa", "Beta", 0, 3, oggi - timedelta(days=400 + k)))
        partite.append(Match("Gamma", "Delta", 1, 1, oggi - timedelta(days=400 + k)))
    # Ultimo mese: Alfa vince sempre e goleada.
    for k in range(12):
        partite.append(Match("Alfa", "Beta", 4, 0, oggi - timedelta(days=k)))
        partite.append(Match("Gamma", "Delta", 1, 1, oggi - timedelta(days=k)))

    recente = fit_dixon_coles(partite, as_of=oggi, half_life_days=60.0, regularization=0.5)
    lungo = fit_dixon_coles(partite, as_of=oggi, half_life_days=5000.0, regularization=0.5)

    # Con memoria corta conta la forma attuale; con memoria lunga i due periodi
    # si compensano e l'Alfa risulta molto piu' vicina alla media.
    assert recente.attack["Alfa"] > lungo.attack["Alfa"]


def test_campione_efficace_minore_del_numero_di_partite(lega):
    m = fit_dixon_coles(lega.matches, half_life_days=180.0)
    assert 0 < m.effective_sample_size < m.n_matches


def test_squadra_sconosciuta(modello):
    with pytest.raises(KeyError):
        modello.expected_goals("Inter", modello.teams[0])


def test_serve_almeno_una_partita():
    with pytest.raises(ValueError):
        fit_dixon_coles([])


def test_rifiuta_partite_nel_futuro():
    partite = [Match("Alfa", "Beta", 1, 0, date(2025, 9, 1))]
    with pytest.raises(ValueError):
        fit_dixon_coles(partite, as_of=date(2025, 8, 1))


# ---- su cosa poggia la stima -------------------------------------------------
#
# Il modello risponde sempre, anche su una squadra che non ha mai visto quasi
# giocare: lo shrinkage la tiene sulla media di lega e ne esce un pronostico
# prudente, che da fuori non si distingue da uno informato. Questi test
# coprono cio' che rende visibile la differenza.


def test_ogni_squadra_del_modello_ha_la_sua_evidenza(modello):
    """Nessuna squadra deve restare senza: la pagina la mostrerebbe vuota."""
    assert set(modello.evidence) == set(modello.teams)


def test_sostanza_e_tiro_sono_complementari(modello):
    for e in modello.evidence.values():
        assert 0.0 < e.pull < 1.0
        assert e.sostanza == pytest.approx(1.0 - e.pull)


def test_senza_shrinkage_la_stima_e_tutta_dei_dati(lega):
    """Tolta la penalita' non c'e' piu' niente che tiri verso la media."""
    m = fit_dixon_coles(lega.matches, half_life_days=2000.0, regularization=0.0)
    for e in m.evidence.values():
        assert e.pull == pytest.approx(0.0)
        assert e.sostanza == pytest.approx(1.0)


def test_piu_shrinkage_tira_di_piu(lega):
    debole = fit_dixon_coles(lega.matches, half_life_days=2000.0, regularization=0.5)
    forte = fit_dixon_coles(lega.matches, half_life_days=2000.0, regularization=8.0)
    for squadra in debole.teams:
        assert forte.evidence[squadra].pull > debole.evidence[squadra].pull


def test_la_squadra_con_meno_partite_e_tirata_di_piu():
    """La neopromossa: stessa lega, un quarto delle partite alle spalle.

    E' il caso che ha motivato l'indicatore. Il pronostico su di lei esce
    prudente non perche' la partita sia equilibrata, ma perche' il modello
    non sa quasi nulla — e questo si deve poter leggere prima del fischio
    d'inizio, non mesi dopo dall'accuratezza.
    """
    oggi = date(2025, 8, 24)
    partite = []
    for k in range(24):
        partite.append(Match("Vecchia", "Media", 2, 1, oggi - timedelta(days=3 * k)))
        partite.append(Match("Media", "Vecchia", 1, 1, oggi - timedelta(days=3 * k + 1)))
    for k in range(6):
        partite.append(Match("Neopromossa", "Media", 1, 1, oggi - timedelta(days=3 * k)))

    m = fit_dixon_coles(partite, as_of=oggi, half_life_days=180.0)
    assert m.evidence["Neopromossa"].n_matches == 6
    assert m.evidence["Vecchia"].n_matches == 48
    assert m.evidence["Neopromossa"].pull > m.evidence["Vecchia"].pull


def test_le_partite_vecchie_contano_meno_anche_qui():
    """Stesso numero di partite, eta' diversa: chi ha dati freschi e' tirato meno.

    Il decadimento temporale entra nell'indicatore per la stessa ragione per
    cui entra nella stima: quaranta partite di due anni fa non descrivono la
    squadra di oggi, e dirlo e' il senso di tutto l'indicatore.
    """
    oggi = date(2025, 8, 24)
    partite = []
    for k in range(20):
        partite.append(Match("Fresca", "Media", 1, 1, oggi - timedelta(days=2 * k)))
        partite.append(Match("Stantia", "Media", 1, 1, oggi - timedelta(days=700 + 2 * k)))

    m = fit_dixon_coles(partite, as_of=oggi, half_life_days=180.0)
    fresca, stantia = m.evidence["Fresca"], m.evidence["Stantia"]
    assert fresca.n_matches == stantia.n_matches == 20
    assert fresca.effective_sample_size > stantia.effective_sample_size
    assert fresca.pull < stantia.pull


def test_l_ultima_partita_e_quella_giusta():
    """L'ultima partita nota per squadra, non l'ultima del campione.

    E' l'unico dato dell'indicatore che non dipende da ``as_of``, ed e' per
    questo che c'e': dice quanto e' vecchia la fotografia, mentre il tiro
    verso la media misura solo quanta ce n'e'.
    """
    partite = [
        Match("Alfa", "Beta", 1, 0, date(2025, 5, 1)),
        Match("Beta", "Gamma", 2, 2, date(2025, 8, 1)),
        Match("Gamma", "Alfa", 0, 0, date(2025, 3, 1)),
    ]
    m = fit_dixon_coles(partite, as_of=date(2025, 8, 24), half_life_days=180.0)
    assert m.evidence["Alfa"].last_match == date(2025, 5, 1)
    assert m.evidence["Beta"].last_match == date(2025, 8, 1)
    assert m.evidence["Gamma"].last_match == date(2025, 8, 1)


def test_il_tiro_verso_la_media_non_misura_la_vecchiaia_dei_dati():
    """Il limite che l'indicatore da solo non vede, fissato in un test.

    Il decadimento e' relativo alla partita piu' recente del campione: sposta
    tutto l'archivio indietro di un anno e i pesi *relativi* non cambiano di
    una virgola, quindi il tiro verso la media resta identico. E' il motivo
    per cui la pagina mostra sempre anche la data dell'ultima partita: senza,
    un archivio fermo da un anno sembrerebbe solido quanto uno di ieri.
    """
    recente = [
        Match("Alfa", "Beta", 2, 1, date(2025, 8, 1) - timedelta(days=7 * k))
        for k in range(20)
    ]
    vecchio = [
        Match(m.home, m.away, m.home_goals, m.away_goals, m.played_on - timedelta(days=365))
        for m in recente
    ]

    a = fit_dixon_coles(recente, half_life_days=180.0)
    b = fit_dixon_coles(vecchio, half_life_days=180.0)

    assert b.evidence["Alfa"].pull == pytest.approx(a.evidence["Alfa"].pull, abs=1e-6)
    # Cio' che cambia e' solo questo, ed e' cio' che la pagina deve mostrare.
    assert b.evidence["Alfa"].last_match < a.evidence["Alfa"].last_match


def test_la_tabella_parte_dalle_squadre_meno_note(modello):
    tabella = modello.evidence_table()
    assert len(tabella) == len(modello.teams)
    assert tabella[0].pull >= tabella[-1].pull


def test_evidence_for_su_squadra_sconosciuta(modello):
    """Come ``predict``, ma senza eccezione: qui il vuoto e' una risposta."""
    assert modello.evidence_for("Squadra Che Non Esiste") is None


# ---- compressione dei gol totali -------------------------------------------
#
# Le forze delle squadre dicono bene chi vince e male quanti gol: il totale
# atteso si comprime verso la media di lega, il rapporto fra le due squadre
# no. Questi test difendono le due meta' di quella frase.


def test_compressione_a_uno_non_tocca_niente():
    assert comprimi_totale(2.0, 1.0, 2.6, 1.0) == (2.0, 1.0)


def test_compressione_a_zero_dichiara_la_media_di_lega():
    lam, mu = comprimi_totale(2.0, 1.0, 2.6, 0.0)
    assert lam + mu == pytest.approx(2.6)


@pytest.mark.parametrize("fattore", [0.0, 0.2, 0.5, 0.9, 1.0])
def test_il_rapporto_fra_le_squadre_resta_identico(fattore):
    """E' cio' che tiene l'1X2 al riparo: si comprime il totale, non la forza."""
    lam, mu = comprimi_totale(2.4, 0.6, 2.6, fattore)
    assert lam / mu == pytest.approx(2.4 / 0.6)


def test_comprime_da_tutte_e_due_le_parti():
    tanti, _ = comprimi_totale(2.5, 1.5, 2.6, 0.2)
    pochi, _ = comprimi_totale(0.6, 0.4, 2.6, 0.2)
    # Una partita da tanti gol scende verso la media, una da pochi sale.
    assert tanti < 2.5
    assert pochi > 0.6


def test_senza_media_di_lega_non_si_comprime():
    """Zero significa "non la so", e non e' una media verso cui tirare.

    Comprimere verso un centro inventato sposterebbe ogni pronostico senza
    che nessuno l'abbia mai misurato: e' la stessa distinzione fra `None` e
    1.0 dei pesi dei giocatori.
    """
    assert comprimi_totale(2.0, 1.0, 0.0, 0.0) == (2.0, 1.0)


@pytest.mark.parametrize("fattore", [-0.1, 1.5])
def test_un_fattore_fuori_scala_e_un_errore(fattore):
    with pytest.raises(ValueError):
        comprimi_totale(2.0, 1.0, 2.6, fattore)


def test_la_media_di_lega_esce_dal_campione(modello, lega):
    """Stimata dai dati, non scritta a mano: cambia da stagione a stagione."""
    osservata = sum(m.home_goals + m.away_goals for m in lega.matches) / len(lega.matches)
    # L'emivita del fixture e' lunghissima, quindi i pesi sono quasi uniformi
    # e la media pesata coincide con quella semplice.
    assert modello.league_total_goals == pytest.approx(osservata, abs=0.02)


def test_la_compressione_stringe_i_totali_previsti(lega):
    """Il totale previsto varia meno da partita a partita, ed e' il punto.

    Il modello si muove sui gol e quei movimenti sono rumore: la compressione
    li toglie. Qui si verifica che li tolga davvero, e che il pronostico su
    CHI VINCE resti quello di prima.
    """
    secco = fit_dixon_coles(
        lega.matches, half_life_days=2000.0, regularization=0.5, goal_compression=1.0
    )
    compresso = fit_dixon_coles(
        lega.matches, half_life_days=2000.0, regularization=0.5, goal_compression=0.2
    )
    coppie = [
        (a, b) for a in secco.teams[:6] for b in secco.teams[:6] if a != b
    ]

    def totali(m):
        return np.array([sum(m.expected_goals(a, b)) for a, b in coppie])

    assert totali(compresso).std() < totali(secco).std() / 3

    # Sull'1X2 il rapporto intatto NON basta a lasciare tutto immobile: a
    # parita' di rapporto, meno gol attesi vogliono dire piu' pareggi, e su
    # una partita molto sbilanciata la probabilita' del favorito scende di
    # qualche punto. Il backtest dice che questo prezzo e' piccolo (Brier
    # 1X2 +0.0008 contro -0.0031 e -0.0038 sui gol), ma esiste e non va
    # nascosto: quello che deve restare intatto e' il GIUDIZIO, cioe' chi il
    # modello ritiene favorito e di quanto rispetto alle altre partite.
    for a, b in coppie:
        prima, dopo = secco.predict(a, b), compresso.predict(a, b)
        assert (prima.home_win > prima.away_win) == (dopo.home_win > dopo.away_win)

        # E lo spostamento resta piccolo. Sulle squadre vere di Serie A, con
        # il fattore di default, P(1) si muove in media di 0.008 e al massimo
        # di 0.077 — quest'ultimo su un accoppiamento sbilanciato come non se
        # ne vedono in campionato.
        assert abs(dopo.home_win - prima.home_win) < 0.10
