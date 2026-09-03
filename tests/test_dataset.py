"""Test dell'insieme di partite su cui lavorano modello e pagine.

Le regole di UNIONE fra le sorgenti — chi sovrascrive chi, e perche' una
stagione presente due volte non deve risultare giocata due volte — stanno in
tests/test_storico.py e tests/test_importato.py, accanto alle sorgenti che le
producono. Qui c'e' cio' che il `Dataset` sa fare **una volta** che le partite
sono dentro, e che nessun altro test guardava.

La finestra della "prossima giornata" arriva da tests/test_ingest.py, dove
provava la vecchia `ingest.next_matchday`: quella funzione e' stata tolta il
25 agosto 2026 perche' la usava soltanto uno script che stimava il modello per
conto suo, su meno partite del sito. La regola pero' e' viva e vale ancora,
solo che adesso vive qui.
"""

from datetime import datetime, timedelta, timezone

from serie_a_analyzer.data.dataset import Dataset
from serie_a_analyzer.data.parsers import FixtureRecord

ADESSO = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

SQUADRE = {1: "Inter", 2: "Milan", 3: "Roma", 4: "Lazio", 5: "Napoli", 6: "Lecce"}


def _partita(fixture_id, casa, ospite, quando, gol=None):
    conclusa = gol is not None
    return FixtureRecord(
        api_id=fixture_id,
        season=2026,
        league_id=135,
        round_label="Regular Season - 1",
        matchday=1,
        kickoff_utc=quando,
        status="finished" if conclusa else "scheduled",
        home_team_id=casa,
        home_team_name=SQUADRE[casa],
        away_team_id=ospite,
        away_team_name=SQUADRE[ospite],
        home_goals=gol[0] if conclusa else None,
        away_goals=gol[1] if conclusa else None,
        home_goals_ht=None,
        away_goals_ht=None,
        venue_name="Stadio",
    )


def _dataset(partite):
    return Dataset(fixtures=list(partite), team_names=dict(SQUADRE))


def test_la_prossima_giornata_e_una_finestra_non_un_numero():
    """Una giornata di Serie A si spalma da venerdi' a lunedi'.

    Non si filtra per numero di giornata: i recuperi slittano anche di mesi, e
    una "giornata" per numero conterrebbe partite lontanissime fra loro.
    """
    dati = _dataset([
        # La prossima giornata: venerdi' -> lunedi'.
        _partita(1, 1, 2, ADESSO + timedelta(days=2)),
        _partita(2, 3, 4, ADESSO + timedelta(days=3)),
        _partita(3, 5, 6, ADESSO + timedelta(days=4)),
        # Quella dopo, una settimana piu' in la': non deve entrare.
        _partita(4, 1, 3, ADESSO + timedelta(days=9)),
        # Gia' giocata: esclusa.
        _partita(5, 2, 4, ADESSO - timedelta(days=3), gol=(1, 0)),
    ])

    prossime = dati.next_matchday(now=ADESSO)

    assert [f.api_id for f in prossime] == [1, 2, 3]


def test_un_recupero_lontano_non_trascina_dentro_la_giornata_dopo():
    """Se la prima partita in programma e' un recupero isolato, la finestra
    parte da lui: il blocco e' "il primo blocco", non "i prossimi 5 giorni da
    oggi"."""
    dati = _dataset([
        _partita(1, 1, 2, ADESSO + timedelta(days=20)),
        _partita(2, 3, 4, ADESSO + timedelta(days=22)),
        _partita(3, 5, 6, ADESSO + timedelta(days=40)),
    ])

    prossime = dati.next_matchday(now=ADESSO)

    assert [f.api_id for f in prossime] == [1, 2]


def test_senza_partite_in_programma_non_si_inventa_una_giornata():
    dati = _dataset([_partita(1, 1, 2, ADESSO - timedelta(days=3), gol=(2, 1))])

    assert dati.next_matchday(now=ADESSO) == []
    assert dati.upcoming(now=ADESSO) == []


def test_gli_ultimi_risultati_arrivano_dal_piu_recente():
    dati = _dataset([
        _partita(1, 1, 2, ADESSO - timedelta(days=10), gol=(1, 0)),
        _partita(2, 3, 4, ADESSO - timedelta(days=2), gol=(2, 2)),
        _partita(3, 5, 6, ADESSO + timedelta(days=2)),
    ])

    recenti = dati.recent_results(limit=5)

    assert [f.api_id for f in recenti] == [2, 1]


# ---- cose che le pagine chiedono al Dataset e che possono mentire -----------


def test_le_partite_per_id_si_costruiscono_una_volta_sola():
    """Era una property che rifaceva un dizionario di 1900 partite ogni volta.

    Un Dataset non cambia dopo essere stato costruito — lo si butta e se ne fa
    un altro quando l'archivio si muove — quindi la cache non puo' invecchiare.
    """
    dati = _dataset([
        _partita(1, 1, 2, ADESSO - timedelta(days=3), gol=(1, 0)),
        _partita(2, 3, 4, ADESSO + timedelta(days=2)),
    ])

    primo = dati.by_id

    assert primo is dati.by_id
    assert set(primo) == {1, 2}


def test_una_stagione_finita_non_ha_una_giornata_corrente():
    """Cerchiare la 38a del 2023 come "quella che si sta per giocare" e' falso.

    `/calendario` deve arrivare da qualche parte e quindi ripiega sull'ultima
    giocata; la pagina di una giornata invece cerchia il presente mentre si
    sfoglia, e li' il ripiego mette un segno su una data di tre anni fa.
    """
    finita = _dataset([
        _partita(1, 1, 2, ADESSO - timedelta(days=400), gol=(1, 0)),
        _partita(2, 3, 4, ADESSO - timedelta(days=399), gol=(0, 0)),
    ])

    assert finita.giornata_corrente(2026, now=ADESSO) == 1, "il ripiego resta"
    assert finita.giornata_corrente(2026, now=ADESSO, ripiega=False) is None


def test_una_stagione_in_corso_ha_sempre_una_giornata_corrente():
    """Col futuro davanti le due risposte coincidono: il ripiego non c'entra."""
    dati = _dataset([
        _partita(1, 1, 2, ADESSO - timedelta(days=3), gol=(1, 0)),
        _partita(2, 3, 4, ADESSO + timedelta(days=2)),
    ])

    assert dati.giornata_corrente(2026, now=ADESSO) == 1
    assert dati.giornata_corrente(2026, now=ADESSO, ripiega=False) == 1
