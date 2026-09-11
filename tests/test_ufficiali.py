"""Test del pronostico con le formazioni ufficiali.

Due comportamenti valgono piu' di tutti gli altri messi insieme, e sono
entrambi dei "non fare".

**Non scrivere quando non si sa.** I pronostici sono immutabili e non si
cancellano: una riga `formazione_ufficiale` identica a quella secca non e'
innocua, e' un dato falso nel confronto fra le due modalita' — la partita
risulterebbe "misurata" quando non c'era niente da misurare. Finche' i
tabellini sul disco non bastano, qui non deve comparire nessuna riga. E' anche
la condizione di oggi, non un caso di scuola.

**Non salvare una formazione vuota.** Prima che escano, l'API risponde con una
lista vuota. Salvarla come dettaglio farebbe scattare il contatore dei
tentativi, e dopo tre letture anticipate le formazioni vere — quelle di fine
serata — non verrebbero piu' chieste da nessuno. Si perderebbe un dato per aver
guardato troppo presto.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from serie_a_analyzer.data.pesi import GIOCATORI_IN_CAMPO
from serie_a_analyzer.data.players import ArchivioGiocatori, Prestazione
from serie_a_analyzer.data.predictions import (
    MODE_SECCO,
    MODE_UFFICIALE,
    PredictionStore,
)
from serie_a_analyzer.data.store import SeasonStore
from serie_a_analyzer.data.ufficiali import (
    MACCHINA_ACCESA as ACCESA_NEL_CODICE,  # letta prima che la fixture la accenda
    leggi_formazioni,
    partite_imminenti,
    pronostica_con_formazioni,
)
from serie_a_analyzer.model.dixon_coles import FittedModel

ADESSO = datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc)
FISCHIO = datetime(2026, 8, 22, 16, 30, tzinfo=timezone.utc)
CASA, OSPITE = 505, 503


@pytest.fixture(autouse=True)
def macchina_accesa(monkeypatch):
    """I test di questo file descrivono la macchina ACCESA.

    Cioe' come si comporterebbe il giorno in cui un metodo nuovo la
    riaccendesse. Dall'11 settembre 2026 e' spenta per misura
    (`MACCHINA_ACCESA`), quindi qui va accesa apposta; la macchina spenta ha i
    suoi test in fondo, e passano ``accesa=False`` per esteso.
    """
    monkeypatch.setattr("serie_a_analyzer.data.ufficiali.MACCHINA_ACCESA", True)


@dataclass
class PartitaFinta:
    api_id: int = 1
    kickoff_utc: datetime = FISCHIO
    home_team_id: int = CASA
    away_team_id: int = OSPITE


class DatasetFinto:
    def __init__(self, partite):
        self._partite = partite
        self._nomi = {CASA: "Inter", OSPITE: "Torino"}

    def upcoming(self):
        return list(self._partite)

    def name(self, team_id):
        return self._nomi[team_id]


class ClienteFinto:
    """Restituisce le formazioni che gli si danno, e conta le chiamate."""

    def __init__(self, payload):
        self.payload = payload
        self.chiamate = 0

    def lineups(self, fixture_id):
        self.chiamate += 1
        return self.payload


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


def _blocco(team_id, nome, titolari):
    def voce(pid):
        return {"player": {"id": pid, "name": f"Giocatore {pid}", "number": None,
                           "pos": "M", "grid": None}}

    # La panchina non serve ai pesi — quelli guardano chi scende in campo — ma
    # una distinta senza riserve l'archivio la considera arrivata a meta'
    # (`formazione_provvisoria` in data/store.py), quindi una formazione
    # "vera" senza panchina qui direbbe il contrario del suo nome.
    return {
        "team": {"id": team_id, "name": nome},
        "formation": "4-3-3",
        "coach": {"id": 99, "name": "Un Allenatore"},
        "startXI": [voce(pid) for pid in titolari],
        "substitutes": [voce(team_id * 1000 + i) for i in range(12)],
    }


def _prestazione(fixture_id, player_id, team_id, minuti=90, gol=0):
    return Prestazione(
        fixture_id=fixture_id,
        quando=ADESSO - timedelta(days=fixture_id),
        stagione=2026,
        player_id=player_id,
        nome=f"Giocatore {player_id}",
        team_id=team_id,
        squadra="Squadra",
        minuti=minuti,
        titolare=True,
        posizione="M",
        numero=None,
        gol=gol,
        assist=0,
        gialli=0,
        rossi=0,
        tiri=None,
        tiri_in_porta=None,
        passaggi_chiave=None,
        voto=None,
    )


def _archivio_pieno():
    """Dieci giornate per entrambe le squadre, con un bomber per parte."""
    righe = []
    for f in range(1, 11):
        for i in range(GIOCATORI_IN_CAMPO):
            righe.append(_prestazione(f, 100 + i, CASA, gol=3 if i == 0 else 0))
            righe.append(_prestazione(f, 200 + i, OSPITE, gol=3 if i == 0 else 0))
    return ArchivioGiocatori(prestazioni=righe)


@pytest.fixture
def store(tmp_path):
    return SeasonStore(root=tmp_path / "serie_a")


@pytest.fixture
def registro(tmp_path):
    return PredictionStore(path=tmp_path / "pronostici.db")


@pytest.fixture
def formazioni_vere():
    """Entrambe schierano il bomber in panchina: la produzione cala."""
    casa = [100 + i for i in range(1, GIOCATORI_IN_CAMPO + 1)]
    ospite = [200 + i for i in range(1, GIOCATORI_IN_CAMPO + 1)]
    return [_blocco(CASA, "Inter", casa), _blocco(OSPITE, "Torino", ospite)]


# ---- la finestra -------------------------------------------------------------


def test_solo_le_partite_che_stanno_per_cominciare():
    partite = [
        PartitaFinta(1, ADESSO + timedelta(minutes=30)),
        PartitaFinta(2, ADESSO + timedelta(minutes=10)),
        PartitaFinta(3, ADESSO + timedelta(hours=5)),    # troppo lontana
        PartitaFinta(4, ADESSO - timedelta(minutes=5)),  # gia' iniziata
    ]
    scelte = partite_imminenti(partite, now=ADESSO, entro_minuti=75)
    assert [p.api_id for p in scelte] == [2, 1]


# ---- la trappola della formazione vuota --------------------------------------


def test_una_formazione_vuota_non_si_salva(store):
    """Salvarla brucerebbe i tentativi e farebbe perdere quelle vere."""
    cliente = ClienteFinto([])
    assert leggi_formazioni(cliente, store, 1) == []
    assert cliente.chiamate == 1
    # Nessuna busta scritta: la partita resta da leggere.
    assert store.detail_envelope(1, "lineups") is None
    assert store.needs_detail(1, "lineups")


def test_una_formazione_vera_si_salva(store, formazioni_vere):
    formazioni = leggi_formazioni(ClienteFinto(formazioni_vere), store, 1)
    assert len(formazioni) == 2
    assert store.has_detail(1, "lineups")


def test_una_formazione_letta_a_meta_si_salva_ma_resta_da_richiedere(store):
    """Il 29 agosto 2026, e la ragione per cui le due meta' devono convivere.

    A quindici minuti dal via l'API pubblica la distinta un pezzo per volta.
    Salvarla e' giusto — il pronostico con le formazioni non ha altro momento
    per nascere, e dopo il fischio non varrebbe piu' niente — ma l'archivio
    deve sapere che quella non e' l'ultima parola: la raccolta del dopopartita
    la richiede e la sostituisce con quella intera.
    """
    a_meta = [_blocco(CASA, "Inter", [101, 102, 103]),
              _blocco(OSPITE, "Torino", [201, 202, 203])]

    assert leggi_formazioni(ClienteFinto(a_meta), store, 1)
    assert store.has_detail(1, "lineups")
    assert store.needs_detail(1, "lineups")


def test_una_formazione_intera_letta_prima_del_via_chiude_il_caso(
    store, formazioni_vere
):
    """Il caso normale: la distinta esce intera e non si spende piu' niente."""
    assert leggi_formazioni(ClienteFinto(formazioni_vere), store, 1)
    assert not store.needs_detail(1, "lineups")


def test_un_errore_dell_api_non_propaga(store):
    class ClienteRotto:
        def lineups(self, fixture_id):
            raise RuntimeError("500")

    assert leggi_formazioni(ClienteRotto(), store, 1) == []
    assert store.needs_detail(1, "lineups")


# ---- la macchina spenta ------------------------------------------------------


def test_senza_tabellini_non_si_registra_niente(store, registro, formazioni_vere):
    """La condizione di oggi: l'archivio e' vuoto, e va bene cosi'."""
    esito = pronostica_con_formazioni(
        ClienteFinto(formazioni_vere), store, registro,
        model=_modello(),
        dataset=DatasetFinto([PartitaFinta()]),
        archivio=ArchivioGiocatori(prestazioni=[]),
        now=ADESSO,
    )
    assert esito["registrate"] == 0
    assert esito["senza_pesi"] == 1
    assert registro.ultimo_per_partita(1, mode=MODE_UFFICIALE) is None


def test_senza_formazioni_non_si_registra_niente(store, registro):
    esito = pronostica_con_formazioni(
        ClienteFinto([]), store, registro,
        model=_modello(),
        dataset=DatasetFinto([PartitaFinta()]),
        archivio=_archivio_pieno(),
        now=ADESSO,
    )
    assert esito["registrate"] == 0
    assert esito["senza_formazioni"] == 1


def test_una_formazione_identica_alla_tipo_non_merita_una_riga(store, registro):
    """Se schieri gli undici di sempre, non c'e' niente da aggiungere."""
    archivio = _archivio_pieno()
    tipo_casa = [100 + i for i in range(GIOCATORI_IN_CAMPO)]
    tipo_ospite = [200 + i for i in range(GIOCATORI_IN_CAMPO)]
    payload = [_blocco(CASA, "Inter", tipo_casa), _blocco(OSPITE, "Torino", tipo_ospite)]

    esito = pronostica_con_formazioni(
        ClienteFinto(payload), store, registro,
        model=_modello(),
        dataset=DatasetFinto([PartitaFinta()]),
        archivio=archivio,
        now=ADESSO,
    )
    assert esito["registrate"] == 0
    assert esito["senza_pesi"] == 1


# ---- la distinta a meta' non pronostica --------------------------------------


def test_una_distinta_a_meta_non_scrive_niente(store, registro):
    """Dieci titolari su undici, e nessuno se ne accorgerebbe.

    E' il caso vero del 29 agosto 2026. La produzione in campo scenderebbe
    della quota di chi manca — un undicesimo per un titolare qualunque — per
    **pura aritmetica**, abbastanza da superare la soglia di `vale_la_pena`, e
    il pronostico direbbe per sempre che il modello aveva visto mancare
    qualcuno che invece giocava. I pronostici non si
    cancellano: meglio un buco.
    """
    archivio = _archivio_pieno()
    dieci_casa = [100 + i for i in range(GIOCATORI_IN_CAMPO - 1)]
    tipo_ospite = [200 + i for i in range(GIOCATORI_IN_CAMPO)]
    payload = [_blocco(CASA, "Inter", dieci_casa),
               _blocco(OSPITE, "Torino", tipo_ospite)]

    esito = pronostica_con_formazioni(
        ClienteFinto(payload), store, registro,
        model=_modello(),
        dataset=DatasetFinto([PartitaFinta()]),
        archivio=archivio,
        now=ADESSO,
    )
    assert esito["formazioni_a_meta"] == 1
    assert esito["registrate"] == 0
    assert registro.ultimo_per_partita(1, mode=MODE_UFFICIALE) is None
    # Salvata pero' si': la raccolta del dopopartita la sostituira'.
    assert store.has_detail(1, "lineups")


def test_una_distinta_senza_panchina_non_scrive_niente(store, registro):
    """Juventus-Parma: gli undici titolari giusti e nessun panchinaro.

    Qui i titolari sono undici e i pesi sarebbero giusti, ma la distinta non
    e' finita di uscire: chi manca dalla panchina puo' comparire da un momento
    all'altro anche fra i titolari, ed e' successo nelle altre due partite
    della stessa sera. Si aspetta il giro dopo.
    """
    tipo_casa = [100 + i for i in range(GIOCATORI_IN_CAMPO)]
    tipo_ospite = [200 + i for i in range(GIOCATORI_IN_CAMPO)]
    payload = [_blocco(CASA, "Inter", tipo_casa), _blocco(OSPITE, "Torino", tipo_ospite)]
    for blocco in payload:
        blocco.pop("substitutes")

    esito = pronostica_con_formazioni(
        ClienteFinto(payload), store, registro,
        model=_modello(),
        dataset=DatasetFinto([PartitaFinta()]),
        archivio=_archivio_pieno(),
        now=ADESSO,
    )
    assert esito["formazioni_a_meta"] == 1
    assert esito["registrate"] == 0


def test_al_giro_dopo_la_distinta_intera_si_pronostica(store, registro,
                                                       formazioni_vere):
    """Saltare non e' rinunciare: la finestra dei 75 minuti ha cinque giri.

    Il primo trova dieci titolari e non scrive; il secondo trova la distinta
    completa — con il bomber in panchina — e scrive.
    """
    dieci = [_blocco(CASA, "Inter", [100 + i for i in range(GIOCATORI_IN_CAMPO - 1)]),
             _blocco(OSPITE, "Torino", [200 + i for i in range(GIOCATORI_IN_CAMPO)])]
    argomenti = dict(
        model=_modello(),
        dataset=DatasetFinto([PartitaFinta()]),
        archivio=_archivio_pieno(),
        now=ADESSO,
    )

    primo = pronostica_con_formazioni(ClienteFinto(dieci), store, registro,
                                      **argomenti)
    assert primo["formazioni_a_meta"] == 1 and primo["registrate"] == 0

    secondo = pronostica_con_formazioni(ClienteFinto(formazioni_vere), store,
                                        registro, **argomenti)
    assert secondo["formazioni_a_meta"] == 0
    assert secondo["registrate"] == 1
    assert registro.ultimo_per_partita(1, mode=MODE_UFFICIALE) is not None


# ---- la macchina accesa ------------------------------------------------------


def test_col_bomber_in_panchina_si_registra_un_pronostico_diverso(
    store, registro, formazioni_vere
):
    modello = _modello()
    partita = PartitaFinta()

    # Il pronostico secco, quello che esisterebbe comunque.
    lam, mu = modello.expected_goals("Inter", "Torino")
    registro.registra(
        fixture_id=partita.api_id, kickoff_utc=FISCHIO,
        home_team_id=CASA, away_team_id=OSPITE,
        model_version_id=registro.versione_modello(modello),
        matrice=modello.scoreline("Inter", "Torino"),
        probabilita=modello.predict("Inter", "Torino"),
        lambda_home=lam, lambda_away=mu, rho=modello.rho,
        mode=MODE_SECCO, now=ADESSO,
    )

    esito = pronostica_con_formazioni(
        ClienteFinto(formazioni_vere), store, registro,
        model=modello,
        dataset=DatasetFinto([partita]),
        archivio=_archivio_pieno(),
        now=ADESSO,
    )

    assert esito["registrate"] == 1
    secco = registro.ultimo_per_partita(1, mode=MODE_SECCO)
    ufficiale = registro.ultimo_per_partita(1, mode=MODE_UFFICIALE)
    assert ufficiale is not None
    # Senza il suo marcatore la squadra di casa segna meno.
    assert ufficiale["lambda_home"] < secco["lambda_home"]


def test_non_si_riscrive_una_partita_gia_fatta(store, registro, formazioni_vere):
    cliente = ClienteFinto(formazioni_vere)
    argomenti = dict(
        model=_modello(),
        dataset=DatasetFinto([PartitaFinta()]),
        archivio=_archivio_pieno(),
        now=ADESSO,
    )
    primo = pronostica_con_formazioni(cliente, store, registro, **argomenti)
    secondo = pronostica_con_formazioni(cliente, store, registro, **argomenti)

    assert primo["registrate"] == 1
    assert secondo["registrate"] == 0
    assert secondo["gia_registrate"] == 1
    # E la seconda volta non ha nemmeno chiesto le formazioni.
    assert cliente.chiamate == 1


def test_una_squadra_senza_storia_non_si_pronostica(store, registro, formazioni_vere):
    class DatasetConSconosciuta(DatasetFinto):
        def name(self, team_id):
            return "Inter" if team_id == CASA else "Neopromossa"

    esito = pronostica_con_formazioni(
        ClienteFinto(formazioni_vere), store, registro,
        model=_modello(),
        dataset=DatasetConSconosciuta([PartitaFinta()]),
        archivio=_archivio_pieno(),
        now=ADESSO,
    )
    assert esito["registrate"] == 0
    assert esito["senza_modello"] == 1


def test_il_tetto_di_letture_si_rispetta(store, registro, formazioni_vere):
    cliente = ClienteFinto(formazioni_vere)
    partite = [
        PartitaFinta(api_id=i, kickoff_utc=ADESSO + timedelta(minutes=10 * i))
        for i in range(1, 6)
    ]
    esito = pronostica_con_formazioni(
        cliente, store, registro,
        model=_modello(),
        dataset=DatasetFinto(partite),
        archivio=_archivio_pieno(),
        now=ADESSO,
        entro_minuti=75,
        max_richieste=2,
    )
    assert cliente.chiamate == 2
    assert esito["letture"] == 2


def test_il_fattore_a_zero_spegne_l_aggiustamento(store, registro, formazioni_vere):
    """Serve al backtest: e' il confronto "con e senza" a un parametro solo."""
    esito = pronostica_con_formazioni(
        ClienteFinto(formazioni_vere), store, registro,
        model=_modello(),
        dataset=DatasetFinto([PartitaFinta()]),
        archivio=_archivio_pieno(),
        now=ADESSO,
        fattore=0.0,
    )
    modello = _modello()
    lam, _ = modello.expected_goals("Inter", "Torino")
    ufficiale = registro.ultimo_per_partita(1, mode=MODE_UFFICIALE)
    assert esito["registrate"] == 1
    assert ufficiale["lambda_home"] == pytest.approx(lam)


# ---- la macchina spenta ------------------------------------------------------
#
# Dall'11 settembre 2026, per misura: rigiocate quattro stagioni il pronostico
# con le formazioni non batte il secco (`backtest.py --cerca-fattore`).


def _spenta(cliente, store, registro):
    return pronostica_con_formazioni(
        cliente, store, registro,
        model=_modello(),
        dataset=DatasetFinto([PartitaFinta()]),
        archivio=_archivio_pieno(),
        now=ADESSO,
        accesa=False,
    )


def test_la_macchina_vera_e_spenta():
    """La costante com'e' nel codice, non come la accende la fixture qui sopra.

    Chi la rimette a True deve aver rifatto la misura con un metodo nuovo: dal
    primo pronostico scritto non si torna indietro.
    """
    assert ACCESA_NEL_CODICE is False


def test_spenta_non_scrive_niente_nemmeno_coi_pesi(store, registro, formazioni_vere):
    """Pesi pronti, distinta intera, bomber in panchina: da accesa scriverebbe."""
    esito = _spenta(ClienteFinto(formazioni_vere), store, registro)
    assert esito["registrate"] == 0
    assert esito["spenta"] == 1
    assert registro.ultimo_per_partita(1, mode=MODE_UFFICIALE) is None


def test_spenta_la_distinta_si_salva_lo_stesso(store, registro, formazioni_vere):
    """Serve ai probabili marcatori della scheda partita e all'archivio."""
    _spenta(ClienteFinto(formazioni_vere), store, registro)
    assert store.load_detail(1, "lineups")


def test_spenta_una_distinta_intera_non_si_rilegge(store, registro, formazioni_vere):
    """Da accesa la riga scritta chiudeva il caso; da spenta lo chiude la distinta.

    Senza, sul gratuito, ogni partita si rileggerebbe a ogni quarto d'ora
    della finestra: cinque richieste per sapere una cosa gia' saputa.
    """
    cliente = ClienteFinto(formazioni_vere)
    _spenta(cliente, store, registro)
    secondo = _spenta(cliente, store, registro)
    assert cliente.chiamate == 1
    assert secondo["gia_lette"] == 1
    assert secondo["letture"] == 0


def test_spenta_una_distinta_a_meta_si_rilegge(store, registro, formazioni_vere):
    dieci = [_blocco(CASA, "Inter", [100 + i for i in range(1, 11)]), formazioni_vere[1]]
    primo = _spenta(ClienteFinto(dieci), store, registro)
    assert primo["formazioni_a_meta"] == 1

    cliente = ClienteFinto(formazioni_vere)
    secondo = _spenta(cliente, store, registro)
    assert cliente.chiamate == 1
    assert secondo["spenta"] == 1
