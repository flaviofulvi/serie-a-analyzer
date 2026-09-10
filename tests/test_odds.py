"""Test delle quote del mercato.

Il payload riproduce la forma reale di ``/odds`` di API-Football, verificata
il 18/08/2026 su una risposta vera: stessi nomi di mercato, stessi valori,
stesse quote come stringhe.

Il rischio da presidiare qui e' silenzioso. Se le selezioni ricavate dalle
quote non combaciassero con quelle salvate nei pronostici, il confronto col
mercato non aggancerebbe nulla: nessun errore, nessun crash, solo una tabella
che resta vuota per sempre senza che si capisca perche'.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from serie_a_analyzer.data.odds import (
    MINIMO_BOOKMAKER,
    QuotaLetta,
    consenso,
    consenso_per_partita,
    parse_marcatori,
    parse_quote,
    partite_da_quotare,
    raccogli_quote,
)
from serie_a_analyzer.data.outcomes import confronto_col_mercato, risolvi_partita
from serie_a_analyzer.data.predictions import PredictionStore, righe_mercati
from serie_a_analyzer.model.dixon_coles import FittedModel
from serie_a_analyzer.model.markets import derive_markets

ADESSO = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
FISCHIO = ADESSO + timedelta(days=1)


@dataclass
class PartitaFinta:
    api_id: int
    kickoff_utc: datetime
    home_team_id: int = 505
    away_team_id: int = 503
    home_goals: int | None = None
    away_goals: int | None = None

    @property
    def is_finished(self) -> bool:
        return self.home_goals is not None and self.away_goals is not None


def _bookmaker(nome: str, casa="2.42", pari="3.90", fuori="2.50"):
    """Un bookmaker con i mercati che il registro sa usare."""
    return {
        "id": 1,
        "name": nome,
        "bets": [
            {"id": 1, "name": "Match Winner", "values": [
                {"value": "Home", "odd": casa},
                {"value": "Draw", "odd": pari},
                {"value": "Away", "odd": fuori},
            ]},
            {"id": 5, "name": "Goals Over/Under", "values": [
                {"value": "Over 1.5", "odd": "1.12"},
                {"value": "Under 1.5", "odd": "6.00"},
                {"value": "Over 2.5", "odd": "1.40"},
                {"value": "Under 2.5", "odd": "2.90"},
                {"value": "Over 3.5", "odd": "2.00"},
                {"value": "Under 3.5", "odd": "1.80"},
            ]},
            {"id": 8, "name": "Both Teams Score", "values": [
                {"value": "Yes", "odd": "1.35"},
                {"value": "No", "odd": "2.95"},
            ]},
            {"id": 27, "name": "Clean Sheet - Home", "values": [
                {"value": "Yes", "odd": "4.75"},
                {"value": "No", "odd": "1.15"},
            ]},
            {"id": 28, "name": "Clean Sheet - Away", "values": [
                {"value": "Yes", "odd": "5.00"},
                {"value": "No", "odd": "1.14"},
            ]},
            # Mercati che il registro non conosce: vanno ignorati in silenzio.
            {"id": 12, "name": "Double Chance", "values": [
                {"value": "Home/Draw", "odd": "1.48"},
            ]},
            {"id": 4, "name": "Asian Handicap", "values": [
                {"value": "Home -0.5", "odd": "2.42"},
            ]},
        ],
    }


@pytest.fixture
def risposta_quote():
    return {
        "league": {"id": 135, "name": "Serie A", "season": 2026},
        "fixture": {"id": 1490391, "date": "2026-08-21T18:30:00+00:00"},
        "update": "2026-08-20T09:00:00+00:00",
        "bookmakers": [
            _bookmaker("Pinnacle"),
            _bookmaker("Bet365", casa="2.40", pari="3.95", fuori="2.55"),
            _bookmaker("William Hill", casa="2.45", pari="3.80", fuori="2.50"),
        ],
    }


@pytest.fixture
def store(tmp_path):
    return PredictionStore(path=tmp_path / "pronostici.db")


def _modello() -> FittedModel:
    return FittedModel(
        teams=("Inter", "Torino"),
        attack={"Inter": 0.35, "Torino": -0.10},
        defense={"Inter": 0.20, "Torino": -0.05},
        home_advantage=0.25,
        base=0.2,
        rho=-0.05,
        n_matches=380,
        effective_sample_size=210.0,
        fitted_on=date(2026, 8, 20),
        converged=True,
    )


def _pronostica(store, fixture_id=1):
    modello = _modello()
    matrice = modello.scoreline("Inter", "Torino")
    lam, mu = modello.expected_goals("Inter", "Torino")
    return store.registra(
        fixture_id=fixture_id, kickoff_utc=FISCHIO,
        home_team_id=505, away_team_id=503,
        model_version_id=store.versione_modello(modello),
        matrice=matrice, probabilita=derive_markets(matrice),
        lambda_home=lam, lambda_away=mu, rho=modello.rho, now=ADESSO,
    )


# ---- lettura delle quote -----------------------------------------------------


def test_dalle_quote_escono_i_mercati_che_il_registro_conosce(risposta_quote):
    lette = parse_quote(risposta_quote)
    mercati = {q.market for q in lette}

    assert mercati == {"1x2", "over_under", "btts", "clean_sheet"}


def test_i_mercati_estranei_vengono_ignorati_senza_rumore(risposta_quote):
    """Un bookmaker pubblica decine di mercati: solo alcuni ci riguardano."""
    lette = parse_quote(risposta_quote)
    assert all(q.market in ("1x2", "over_under", "btts", "clean_sheet") for q in lette)


def test_le_selezioni_combaciano_con_quelle_dei_pronostici(risposta_quote):
    """Il test che tiene in piedi tutto il confronto col mercato.

    Se un giorno le due parti divergessero — 'over_2.5' contro 'Over 2.5', o
    'home' contro 'casa' — nulla si romperebbe: il confronto smetterebbe
    semplicemente di trovare corrispondenze, e la pagina resterebbe vuota.
    """
    probabilita = derive_markets(_modello().scoreline("Inter", "Torino"))
    dal_registro = {(m, s) for m, s, _ in righe_mercati(probabilita)}
    dalle_quote = {(q.market, q.selection) for q in parse_quote(risposta_quote)}

    in_comune = dal_registro & dalle_quote
    assert ("1x2", "home") in in_comune
    assert ("1x2", "draw") in in_comune
    assert ("1x2", "away") in in_comune
    assert ("over_under", "over_2.5") in in_comune
    assert ("over_under", "under_2.5") in in_comune
    assert ("btts", "yes") in in_comune
    assert ("clean_sheet", "home") in in_comune
    assert ("clean_sheet", "away") in in_comune

    # Nessuna selezione delle quote deve restare orfana per un mercato che il
    # registro conosce: sarebbe una corrispondenza persa in silenzio.
    mercati_comuni = {m for m, _ in dal_registro}
    orfane = {
        (m, s) for m, s in dalle_quote
        if m in mercati_comuni and (m, s) not in dal_registro
    }
    assert orfane == set(), f"selezioni senza corrispondenza nel registro: {orfane}"


def test_il_margine_del_banco_viene_tolto(risposta_quote):
    """Le inverse delle quote sommano a piu' di 1: la differenza e' il margine."""
    lette = [q for q in parse_quote(risposta_quote) if q.bookmaker == "Pinnacle"]

    uno_x_due = [q for q in lette if q.market == "1x2"]
    grezza = sum(1.0 / q.decimal_odds for q in uno_x_due)
    assert grezza > 1.0, "il payload di prova non ha margine: il test non proverebbe nulla"
    assert sum(q.probabilita for q in uno_x_due) == pytest.approx(1.0)


def test_ogni_linea_di_over_under_si_normalizza_da_sola(risposta_quote):
    """Sommare tutte le linee darebbe un overround senza significato."""
    lette = [q for q in parse_quote(risposta_quote)
             if q.bookmaker == "Pinnacle" and q.market == "over_under"]

    per_linea: dict[str, float] = {}
    for q in lette:
        linea = q.selection.split("_", 1)[1]
        per_linea[linea] = per_linea.get(linea, 0.0) + q.probabilita

    assert set(per_linea) == {"1.5", "2.5", "3.5"}
    for linea, totale in per_linea.items():
        assert totale == pytest.approx(1.0), f"linea {linea}"


def test_il_clean_sheet_tiene_solo_il_lato_che_serve(risposta_quote):
    """Il "No" serve a normalizzare e poi esce di scena."""
    lette = [q for q in parse_quote(risposta_quote)
             if q.bookmaker == "Pinnacle" and q.market == "clean_sheet"]

    assert {q.selection for q in lette} == {"home", "away"}
    casa = next(q for q in lette if q.selection == "home")
    # 4.75 e 1.15: il margine tolto lascia circa il 19%.
    assert casa.probabilita == pytest.approx(1 / 4.75 / (1 / 4.75 + 1 / 1.15), abs=1e-9)


def test_un_gruppo_incompleto_viene_scartato():
    """Normalizzare su meta' degli esiti darebbe numeri plausibili e falsi."""
    solo_uno = {
        "bookmakers": [{
            "name": "Tizio",
            "bets": [{"name": "Match Winner", "values": [{"value": "Home", "odd": "1.50"}]}],
        }]
    }
    assert parse_quote(solo_uno) == []


def test_le_quote_malformate_non_fanno_saltare_la_lettura():
    sporco = {
        "bookmakers": [{
            "name": "Tizio",
            "bets": [{"name": "Match Winner", "values": [
                {"value": "Home", "odd": "non-un-numero"},
                {"value": "Draw", "odd": "3.90"},
                {"value": "Away", "odd": "2.50"},
                {"value": "Away", "odd": None},
            ]}],
        }]
    }
    lette = parse_quote(sporco)
    assert {q.selection for q in lette} == {"draw", "away"}
    assert sum(q.probabilita for q in lette) == pytest.approx(1.0)


def test_un_bookmaker_senza_nome_viene_saltato():
    anonimo = {"bookmakers": [{"name": "  ", "bets": [
        {"name": "Both Teams Score", "values": [
            {"value": "Yes", "odd": "1.35"}, {"value": "No", "odd": "2.95"},
        ]},
    ]}]}
    assert parse_quote(anonimo) == []


# ---- consenso ----------------------------------------------------------------


def test_il_consenso_e_la_mediana_fra_bookmaker(risposta_quote):
    accordo = consenso(parse_quote(risposta_quote))

    assert ("1x2", "home") in accordo
    assert 0.0 < accordo[("1x2", "home")] < 1.0
    # Tre bookmaker con quote vicine: la mediana sta fra il minimo e il massimo.
    valori = [q.probabilita for q in parse_quote(risposta_quote)
              if (q.market, q.selection) == ("1x2", "home")]
    assert min(valori) <= accordo[("1x2", "home")] <= max(valori)


def test_una_quota_fuori_linea_non_sposta_il_consenso(risposta_quote):
    """La mediana e' scelta apposta: un bookmaker sbagliato non deve contare."""
    sano = consenso(parse_quote(risposta_quote))

    risposta_quote["bookmakers"].append(
        _bookmaker("Sbagliato", casa="1.01", pari="50.0", fuori="50.0")
    )
    con_l_errore = consenso(parse_quote(risposta_quote))

    assert con_l_errore[("1x2", "home")] == pytest.approx(sano[("1x2", "home")], abs=0.05)


def test_pochi_bookmaker_non_fanno_un_consenso(risposta_quote):
    risposta_quote["bookmakers"] = risposta_quote["bookmakers"][:MINIMO_BOOKMAKER - 1]
    assert consenso(parse_quote(risposta_quote)) == {}


# ---- raccolta ----------------------------------------------------------------


def test_si_quotano_solo_le_partite_imminenti_non_ancora_iniziate():
    partite = [
        PartitaFinta(1, ADESSO + timedelta(hours=6)),
        PartitaFinta(2, ADESSO + timedelta(hours=30)),
        PartitaFinta(3, ADESSO + timedelta(days=10)),   # troppo lontana
        PartitaFinta(4, ADESSO - timedelta(hours=1)),   # gia' iniziata
    ]

    scelte = partite_da_quotare(partite, now=ADESSO, entro_ore=48)

    assert [p.api_id for p in scelte] == [1, 2]


def test_le_partite_piu_vicine_si_quotano_per_prime():
    """Se il budget finisce, a restare senza e' quella che ha piu' tempo davanti."""
    partite = [
        PartitaFinta(1, ADESSO + timedelta(hours=40)),
        PartitaFinta(2, ADESSO + timedelta(hours=2)),
    ]
    assert [p.api_id for p in partite_da_quotare(partite, now=ADESSO)] == [2, 1]


class ClientFinto:
    def __init__(self, risposta, esplode=False):
        self.risposta = risposta
        self.esplode = esplode
        self.chiamate = []

    def odds(self, fixture_id):
        self.chiamate.append(fixture_id)
        if self.esplode:
            raise RuntimeError("API giu'")
        return [self.risposta]


def test_la_raccolta_archivia_le_quote(store, risposta_quote):
    client = ClientFinto(risposta_quote)

    esito = raccogli_quote(
        client, store, [PartitaFinta(1, FISCHIO)], now=ADESSO
    )

    assert esito["partite"] == 1
    assert esito["quote"] > 0
    assert store.conteggi()["quote"] == esito["quote"]
    assert store.partite_quotate() == [1]


def test_la_raccolta_rispetta_il_tetto_di_richieste(store, risposta_quote):
    client = ClientFinto(risposta_quote)
    partite = [PartitaFinta(i, ADESSO + timedelta(hours=i)) for i in range(1, 6)]

    esito = raccogli_quote(client, store, partite, now=ADESSO, max_richieste=2)

    assert esito["richieste"] == 2
    assert len(client.chiamate) == 2


def test_un_guasto_dell_api_non_ferma_la_raccolta(store, risposta_quote):
    """Le quote sono un di piu': non devono far fallire la raccolta giornaliera."""
    client = ClientFinto(risposta_quote, esplode=True)

    esito = raccogli_quote(client, store, [PartitaFinta(1, FISCHIO)], now=ADESSO)

    assert esito["partite"] == 0
    assert store.conteggi()["quote"] == 0


def test_letture_ripetute_si_accumulano_senza_sovrascriversi(store, risposta_quote):
    """Serve a sapere come si e' mosso il mercato avvicinandosi alla partita."""
    client = ClientFinto(risposta_quote)
    partita = [PartitaFinta(1, FISCHIO)]

    raccogli_quote(client, store, partita, now=ADESSO)
    prime = store.conteggi()["quote"]
    raccogli_quote(client, store, partita, now=ADESSO + timedelta(hours=6))

    assert store.conteggi()["quote"] == prime * 2


# ---- quote sui marcatori -----------------------------------------------------


def _marcatori(nome="Bet365"):
    """I mercati su chi segna, con i nomi veri di ``/odds`` (verificati l'11/09/2026)."""
    return {
        "id": 8,
        "name": nome,
        "bets": [
            {"id": 92, "name": "Anytime Goal Scorer", "values": [
                {"value": "Lautaro Martinez", "odd": "2.20"},
                {"value": "Marcus Thuram", "odd": "2.75"},
            ]},
            {"name": "First Goal Scorer", "values": [
                {"value": "Lautaro Martinez", "odd": "4.50"},
            ]},
            {"name": "Home Anytime Goal Scorer", "values": [
                {"value": "Lautaro Martinez", "odd": "2.10"},
            ]},
            {"name": "Away Last Goal Scorer", "values": [
                {"value": "Che Adams", "odd": "9.00"},
            ]},
        ],
    }


def test_le_quote_sui_marcatori_si_leggono_col_loro_tipo_e_lato():
    lette = parse_marcatori({"bookmakers": [_marcatori()]})

    assert {(q.tipo, q.lato, q.giocatore): q.decimal_odds for q in lette} == {
        ("segna", "partita", "Lautaro Martinez"): 2.20,
        ("segna", "partita", "Marcus Thuram"): 2.75,
        ("primo", "partita", "Lautaro Martinez"): 4.50,
        ("segna", "casa", "Lautaro Martinez"): 2.10,
        ("ultimo", "trasferta", "Che Adams"): 9.00,
    }
    assert {q.bookmaker for q in lette} == {"Bet365"}


def test_i_marcatori_non_entrano_fra_i_mercati_del_registro():
    """Nel registro non avrebbero una selezione con cui agganciarsi."""
    assert parse_quote({"bookmakers": [_marcatori()]}) == []


def test_una_quota_marcatore_malformata_viene_saltata():
    bookmaker = {"name": "Bet365", "bets": [{"name": "Anytime Goal Scorer", "values": [
        {"value": "Lautaro Martinez", "odd": "abc"},
        {"value": "", "odd": "3.00"},
        {"value": "Marcus Thuram", "odd": "1.00"},
        {"value": "Nicolo Barella"},
        {"value": "Hakan Calhanoglu", "odd": "5.00"},
    ]}]}

    lette = parse_marcatori({"bookmakers": [bookmaker]})

    assert [q.giocatore for q in lette] == ["Hakan Calhanoglu"]


def test_la_raccolta_archivia_anche_i_marcatori(store, risposta_quote):
    risposta_quote["bookmakers"].append(_marcatori())

    esito = raccogli_quote(
        ClientFinto(risposta_quote), store, [PartitaFinta(1, FISCHIO)], now=ADESSO
    )

    assert esito["marcatori"] == 5
    assert store.conteggi()["quote_marcatori"] == 5
    assert store.partite_con_marcatori() == [1]
    # Le quote di sempre restano quelle di sempre: i marcatori non ci finiscono.
    assert store.conteggi()["quote"] == esito["quote"]
    assert {r["market"] for r in store.quote_di_chiusura(fixture_id=1)} == {
        "1x2", "over_under", "btts", "clean_sheet",
    }


def test_i_marcatori_si_salvano_anche_senza_gli_altri_mercati(store):
    """Una risposta con i soli marcatori non si butta: quelle quote non tornano."""
    risposta = {"fixture": {"id": 1}, "bookmakers": [_marcatori()]}

    esito = raccogli_quote(
        ClientFinto(risposta), store, [PartitaFinta(1, FISCHIO)], now=ADESSO
    )

    assert esito["marcatori"] == 5
    assert esito["senza_quote"] == 1
    assert store.partite_quotate() == []


def test_letture_ripetute_dei_marcatori_si_accumulano(store):
    risposta = {"bookmakers": [_marcatori()]}
    partita = [PartitaFinta(1, FISCHIO)]

    raccogli_quote(ClientFinto(risposta), store, partita, now=ADESSO)
    raccogli_quote(ClientFinto(risposta), store, partita, now=ADESSO + timedelta(hours=6))

    assert store.conteggi()["quote_marcatori"] == 10


def test_la_stessa_lettura_dei_marcatori_non_si_duplica(store):
    """E' cio' che rende innocuo rilanciare il recupero dalla cache."""
    lette = parse_marcatori({"bookmakers": [_marcatori()]})

    primo = store.salva_quote_marcatori(
        fixture_id=1, kickoff_utc=FISCHIO, quote=lette, now=ADESSO
    )
    secondo = store.salva_quote_marcatori(
        fixture_id=1, kickoff_utc=FISCHIO, quote=lette, now=ADESSO
    )

    assert (primo, secondo) == (5, 0)


# ---- chiusura e confronto ----------------------------------------------------


def test_la_chiusura_e_l_ultima_lettura_prima_del_via(store, risposta_quote):
    client = ClientFinto(risposta_quote)
    partita = [PartitaFinta(1, FISCHIO)]

    raccogli_quote(client, store, partita, now=FISCHIO - timedelta(hours=20))
    raccogli_quote(client, store, partita, now=FISCHIO - timedelta(minutes=30))

    chiusura = store.quote_di_chiusura(fixture_id=1)
    assert chiusura, "nessuna quota di chiusura"
    assert {r["minuti_al_via"] for r in chiusura} == {30}


def test_una_lettura_dopo_il_fischio_d_inizio_non_e_una_chiusura(store, risposta_quote):
    """Un ritardo o un fuso sbagliato non devono inquinare il confronto."""
    client = ClientFinto(risposta_quote)
    partita = [PartitaFinta(1, FISCHIO)]

    raccogli_quote(client, store, partita, now=FISCHIO - timedelta(hours=2))
    # Registrata a mano: la raccolta normale non quoterebbe una partita iniziata.
    store.salva_quote(
        fixture_id=1, kickoff_utc=FISCHIO,
        quote=parse_quote(risposta_quote), now=FISCHIO + timedelta(minutes=10),
    )

    chiusura = store.quote_di_chiusura(fixture_id=1)
    assert all(r["minuti_al_via"] >= 0 for r in chiusura)
    assert {r["minuti_al_via"] for r in chiusura} == {120}


def test_il_confronto_col_mercato_usa_gli_stessi_esiti(store, risposta_quote):
    _pronostica(store, fixture_id=1)
    raccogli_quote(ClientFinto(risposta_quote), store, [PartitaFinta(1, FISCHIO)], now=ADESSO)
    risolvi_partita(store, 1, 2, 1, now=FISCHIO + timedelta(hours=2))

    confronto = confronto_col_mercato(store)

    assert confronto["partite"] == 1
    assert confronto["totale"]["n"] > 0
    mercati = {v["mercato"] for v in confronto["mercati"]}
    # Solo i mercati quotati: la doppia chance e il risultato esatto restano fuori.
    assert mercati == {"1x2", "over_under", "btts", "clean_sheet"}
    for voce in confronto["mercati"]:
        assert voce["scarto"] == pytest.approx(
            voce["brier_modello"] - voce["brier_mercato"]
        )


def test_senza_quote_il_confronto_e_vuoto_invece_che_falso(store):
    _pronostica(store, fixture_id=1)
    risolvi_partita(store, 1, 2, 1, now=FISCHIO + timedelta(hours=2))

    confronto = confronto_col_mercato(store)

    assert confronto == {"mercati": [], "totale": None, "partite": 0}


def test_le_partite_senza_esito_non_entrano_nel_confronto(store, risposta_quote):
    """Quotata ma non ancora giocata: non c'e' niente da confrontare."""
    _pronostica(store, fixture_id=1)
    raccogli_quote(ClientFinto(risposta_quote), store, [PartitaFinta(1, FISCHIO)], now=ADESSO)

    assert confronto_col_mercato(store)["partite"] == 0


def test_il_consenso_per_partita_tiene_separate_le_partite(store, risposta_quote):
    client = ClientFinto(risposta_quote)
    raccogli_quote(client, store, [
        PartitaFinta(1, FISCHIO), PartitaFinta(2, FISCHIO + timedelta(hours=3)),
    ], now=ADESSO)

    accordo = consenso_per_partita(store.quote_di_chiusura())

    assert (1, "1x2", "home") in accordo
    assert (2, "1x2", "home") in accordo
