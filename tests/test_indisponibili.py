"""Test degli indisponibili: infortunati, squalificati e giocatori in dubbio.

Il payload riproduce la forma reale di ``/injuries?fixture=`` verificata il
22/08/2026 su due risposte vere — Inter-Monza di quel giorno e una gara del
17/08/2024 — compresa la stranezza che conta di piu': **l'API ripete ogni
riga due volte**. Su Inter-Monza restituisce 16 record per 8 giocatori, a
coppie identiche. Un conteggio che non deduplicasse direbbe il doppio degli
assenti senza che niente segnali l'errore.

Il secondo rischio silenzioso e' l'opposto: un giocatore recuperato. Le liste
sono fotografie, e chi guarisce sparisce dalla fotografia dopo senza che
nessuna riga lo dichiari. Cercare "l'ultima notizia su ogni giocatore" invece
dell'ultima fotografia lo lascerebbe infortunato per sempre.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from serie_a_analyzer.data.indisponibili import (
    ALTRO,
    ASSENTE,
    Indisponibile,
    IN_DUBBIO,
    INFORTUNIO,
    SQUALIFICA,
    classifica_motivo,
    parse_indisponibili,
    partite_da_controllare,
    per_squadra,
    raccogli_indisponibili,
    traduci_motivo,
)
from serie_a_analyzer.data.predictions import PredictionStore

ADESSO = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
FISCHIO = datetime(2026, 8, 22, 16, 30, tzinfo=timezone.utc)


@dataclass
class PartitaFinta:
    api_id: int
    kickoff_utc: datetime
    home_team_id: int = 505
    away_team_id: int = 1579


def _voce(player_id, nome, team_id, squadra, tipo, motivo):
    """Un record come lo scrive API-Football."""
    return {
        "player": {
            "id": player_id, "name": nome,
            "photo": f"https://media.api-sports.io/football/players/{player_id}.png",
            "type": tipo, "reason": motivo,
        },
        "team": {"id": team_id, "name": squadra},
        "fixture": {"id": 1550092, "date": "2026-08-22T16:30:00+00:00"},
        "league": {"id": 135, "season": 2026, "name": "Serie A"},
    }


# Gli otto veri di Inter-Monza del 22/08/2026, con i motivi come li scrive l'API.
_OTTO = [
    _voce(1457, "H. Mkhitaryan", 505, "Inter", "Missing Fixture", "Suspended"),
    _voce(2001, "E. Akinsanmiro", 1579, "Monza", "Missing Fixture", "Muscle Injury"),
    _voce(2002, "P. Ciurria", 1579, "Monza", "Missing Fixture", "Inactive"),
    _voce(2003, "L. Colombo", 1579, "Monza", "Missing Fixture", "Injury"),
    _voce(2004, "D. Mota", 1579, "Monza", "Missing Fixture", "Suspended"),
    _voce(2005, "M. Pessina", 1579, "Monza", "Missing Fixture", "Knee Injury"),
    _voce(2006, "D. Thiam", 1579, "Monza", "Missing Fixture", "Yellow Cards"),
    _voce(2007, "I. Toure", 1579, "Monza", "Questionable", "Injury"),
]


@pytest.fixture
def risposta():
    """Come risponde davvero l'API: la stessa lista, due volte."""
    return _OTTO + [dict(v) for v in _OTTO]


@pytest.fixture
def store(tmp_path):
    return PredictionStore(path=tmp_path / "pronostici.db")


# ---- lettura -----------------------------------------------------------------


def test_le_righe_duplicate_dall_api_non_diventano_due_assenze(risposta):
    """16 record in ingresso, 8 giocatori in uscita."""
    assert len(risposta) == 16
    letti = parse_indisponibili(risposta)
    assert len(letti) == 8
    assert len({i.player_id for i in letti}) == 8


def test_assente_e_in_dubbio_restano_distinti(risposta):
    letti = {i.player_id: i for i in parse_indisponibili(risposta)}
    assert letti[1457].stato == ASSENTE
    assert letti[1457].certo
    assert letti[2007].stato == IN_DUBBIO
    assert not letti[2007].certo


def test_una_voce_senza_id_non_si_aggancia_al_nome():
    """Senza id non c'e' identita', e il nome non la sostituisce."""
    rotta = _voce(None, "Chi Sara'", 505, "Inter", "Missing Fixture", "Injury")
    assert parse_indisponibili([rotta]) == []


@pytest.mark.parametrize("reason, atteso", [
    ("Suspended", SQUALIFICA),
    ("Yellow Cards", SQUALIFICA),
    ("Red Card", SQUALIFICA),
    ("Injury", INFORTUNIO),
    ("Muscle Injury", INFORTUNIO),
    ("Thigh Injury", INFORTUNIO),
    ("Shoulder Injury", INFORTUNIO),
    ("Illness", ALTRO),
    ("Inactive", ALTRO),
    ("", ALTRO),
    (None, ALTRO),
])
def test_classificazione_dei_motivi(reason, atteso):
    assert classifica_motivo(reason) == atteso


def test_un_motivo_sconosciuto_si_mostra_com_e_scritto():
    """Meglio una parola in inglese che una categoria inventata."""
    assert traduci_motivo("Sprained Wrist") == "Sprained Wrist"
    assert traduci_motivo("Suspended") == "squalificato"
    assert traduci_motivo(None) == "motivo non dichiarato"


def test_raggruppamento_per_squadra_con_i_certi_davanti(risposta):
    gruppi = per_squadra(parse_indisponibili(risposta))
    assert set(gruppi) == {505, 1579}
    assert len(gruppi[1579]) == 7
    # L'unico in dubbio del Monza sta in fondo al suo gruppo.
    assert gruppi[1579][-1].player_id == 2007
    assert all(i.certo for i in gruppi[1579][:-1])

    solo_certi = per_squadra(parse_indisponibili(risposta), solo_certi=True)
    assert len(solo_certi[1579]) == 6


# ---- archivio ----------------------------------------------------------------


def test_si_legge_la_fotografia_piu_vicina_al_fischio(store, risposta):
    letti = parse_indisponibili(risposta)
    store.salva_indisponibili(
        fixture_id=1550092, kickoff_utc=FISCHIO, voci=letti,
        now=FISCHIO - timedelta(hours=40),
    )
    store.salva_indisponibili(
        fixture_id=1550092, kickoff_utc=FISCHIO, voci=letti[:3],
        now=FISCHIO - timedelta(minutes=45),
    )

    vigilia = store.indisponibili_alla_vigilia(1550092)
    assert len(vigilia) == 3
    assert {r["minuti_al_via"] for r in vigilia} == {45}


def test_un_giocatore_recuperato_sparisce_dalla_lista(store, risposta):
    """La trappola vera: chi guarisce non lascia una riga che lo dica."""
    letti = parse_indisponibili(risposta)
    store.salva_indisponibili(
        fixture_id=1550092, kickoff_utc=FISCHIO, voci=letti,
        now=FISCHIO - timedelta(hours=40),
    )
    # Alla rifinitura Mkhitaryan non e' piu' nella lista.
    rimasti = [i for i in letti if i.player_id != 1457]
    store.salva_indisponibili(
        fixture_id=1550092, kickoff_utc=FISCHIO, voci=rimasti,
        now=FISCHIO - timedelta(minutes=30),
    )

    vigilia = store.indisponibili_alla_vigilia(1550092)
    assert 1457 not in {r["player_id"] for r in vigilia}
    assert len(vigilia) == 7


def test_una_lettura_dopo_il_fischio_non_e_la_vigilia(store, risposta):
    """A partita finita si sa chi non e' sceso in campo: sarebbe barare."""
    letti = parse_indisponibili(risposta)
    store.salva_indisponibili(
        fixture_id=1550092, kickoff_utc=FISCHIO, voci=letti,
        now=FISCHIO + timedelta(hours=3),
    )
    assert store.indisponibili_alla_vigilia(1550092) == []
    # La riga pero' esiste: il backtest puo' guardarla, sapendo cos'e'.
    assert store.partite_con_indisponibili() == [1550092]


def test_la_stessa_lettura_due_volte_non_raddoppia_le_righe(store, risposta):
    letti = parse_indisponibili(risposta)
    quando = FISCHIO - timedelta(hours=2)
    primo = store.salva_indisponibili(
        fixture_id=1550092, kickoff_utc=FISCHIO, voci=letti, now=quando
    )
    secondo = store.salva_indisponibili(
        fixture_id=1550092, kickoff_utc=FISCHIO, voci=letti, now=quando
    )
    assert primo == 8
    assert secondo == 0


def test_uno_stato_inventato_si_ferma_subito(risposta):
    """Un dubbio non deve poter diventare un'assenza per distrazione."""
    valido = parse_indisponibili(risposta)[0]
    campi = valido.__dict__

    with pytest.raises(ValueError, match="stato"):
        Indisponibile(**{**campi, "stato": "forse_boh"})
    with pytest.raises(ValueError, match="motivo"):
        Indisponibile(**{**campi, "motivo": "acciacco"})


def test_una_riga_malformata_non_entra_in_archivio(store, risposta):
    """La rete sotto: `insert or ignore` scarta, ma scarta in silenzio.

    E' il motivo per cui il controllo vero sta nella dataclass. Qui si verifica
    solo che, se una riga sbagliata arrivasse comunque fin qui, l'archivio non
    la accetti.
    """
    letto = parse_indisponibili(risposta)[0]
    object.__setattr__(letto, "stato", "forse_boh")
    scritte = store.salva_indisponibili(
        fixture_id=1550092, kickoff_utc=FISCHIO, voci=[letto], now=ADESSO
    )
    assert scritte == 0
    assert store.indisponibili_alla_vigilia(1550092) == []


def test_i_conteggi_includono_gli_indisponibili(store, risposta):
    store.salva_indisponibili(
        fixture_id=1550092, kickoff_utc=FISCHIO,
        voci=parse_indisponibili(risposta), now=ADESSO,
    )
    conteggi = store.conteggi()
    assert conteggi["indisponibili"] == 8
    assert conteggi["partite_con_indisponibili"] == 1


# ---- raccolta ----------------------------------------------------------------


def test_finestra_di_raccolta_dalla_piu_imminente():
    partite = [
        PartitaFinta(1, ADESSO + timedelta(hours=70)),   # troppo lontana
        PartitaFinta(2, ADESSO + timedelta(hours=30)),
        PartitaFinta(3, ADESSO + timedelta(hours=4)),
        PartitaFinta(4, ADESSO - timedelta(hours=1)),    # gia' iniziata
    ]
    scelte = partite_da_controllare(partite, now=ADESSO)
    assert [p.api_id for p in scelte] == [3, 2]


def test_la_raccolta_non_muore_se_l_api_sbaglia(store, risposta):
    """Un indisponibile mancato si riprende domani; una raccolta interrotta no."""

    class ClienteCapriccioso:
        def __init__(self):
            self.chiamate = 0

        def injuries_fixture(self, fixture_id):
            self.chiamate += 1
            if fixture_id == 3:
                raise RuntimeError("500 dall'API")
            return risposta

    cliente = ClienteCapriccioso()
    partite = [
        PartitaFinta(3, ADESSO + timedelta(hours=4)),
        PartitaFinta(2, ADESSO + timedelta(hours=30)),
    ]
    esito = raccogli_indisponibili(cliente, store, partite, now=ADESSO)

    assert cliente.chiamate == 2
    assert esito["letture"] == 1            # quella fallita non si conta
    assert esito["partite"] == 1
    assert esito["indisponibili"] == 8


def test_una_lista_vuota_non_scrive_niente(store):
    """"Nessun infortunato" e "l'API non ce l'ha ancora" da fuori sono uguali."""

    class ClienteMuto:
        def injuries_fixture(self, fixture_id):
            return []

    esito = raccogli_indisponibili(
        ClienteMuto(), store, [PartitaFinta(9, ADESSO + timedelta(hours=4))], now=ADESSO
    )
    assert esito == {
        "partite": 0, "indisponibili": 0, "senza_lista": 1,
        "letture": 1, "richieste": 1,
    }
    assert store.partite_con_indisponibili() == []


def test_il_tetto_di_richieste_si_rispetta(store, risposta):
    class ClienteContato:
        def __init__(self):
            self.chiamate = 0

        def injuries_fixture(self, fixture_id):
            self.chiamate += 1
            return risposta

    cliente = ClienteContato()
    partite = [
        PartitaFinta(i, ADESSO + timedelta(hours=i)) for i in range(1, 8)
    ]
    esito = raccogli_indisponibili(
        cliente, store, partite, now=ADESSO, max_richieste=3
    )
    assert cliente.chiamate == 3
    assert esito["letture"] == 3


def test_una_risposta_gia_in_cache_non_si_conta_come_richiesta(store, risposta):
    """Letture e richieste sono due numeri diversi, e il riepilogo li distingue.

    Il client mette in cache le risposte per mezz'ora: alla seconda esecuzione
    della giornata molte partite non costano niente. Contare le letture come
    richieste farebbe sembrare questo passo il doppio piu' caro di quanto sia.
    """

    class BudgetFinto:
        def __init__(self):
            self.used = 0

    class ClienteConCache:
        def __init__(self):
            self.budget = BudgetFinto()
            self.viste = set()

        def injuries_fixture(self, fixture_id):
            if fixture_id not in self.viste:
                self.viste.add(fixture_id)
                self.budget.used += 1      # solo la prima volta si paga
            return risposta

    cliente = ClienteConCache()
    partite = [PartitaFinta(1, ADESSO + timedelta(hours=4))]
    raccogli_indisponibili(cliente, store, partite, now=ADESSO)
    esito = raccogli_indisponibili(cliente, store, partite, now=ADESSO)

    assert esito["letture"] == 1
    assert esito["richieste"] == 0
