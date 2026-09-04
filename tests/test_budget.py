"""Test della contabilita' del budget giornaliero."""

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import serie_a_analyzer
from serie_a_analyzer.data.budget import (
    BudgetExhausted,
    Priority,
    RequestBudget,
)


@pytest.fixture
def budget(tmp_path):
    return RequestBudget(state_path=tmp_path / "budget.json", daily_limit=100)


def test_parte_da_zero(budget):
    assert budget.used == 0
    assert budget.remaining == 100


def test_il_consumo_si_accumula(budget):
    budget.spend(Priority.NORMAL, 3)
    budget.spend(Priority.NORMAL)
    assert budget.used == 4
    assert budget.remaining == 96


def test_lo_stato_sopravvive_al_riavvio(tmp_path):
    percorso = tmp_path / "budget.json"
    RequestBudget(state_path=percorso).spend(Priority.NORMAL, 7)
    # Nuova istanza: simula un processo riavviato.
    assert RequestBudget(state_path=percorso).used == 7


def test_la_riserva_protegge_le_priorita_alte(budget):
    """Il backfill deve fermarsi lasciando margine alle formazioni ufficiali."""
    budget.spend(Priority.BACKFILL, 55)
    # Riserva di default su 100: 25% CRITICAL + 15% NORMAL = 40 protette.
    assert budget.allowance(Priority.BACKFILL) == 100 - 55 - 40
    assert budget.allowance(Priority.CRITICAL) == 45

    with pytest.raises(BudgetExhausted):
        budget.spend(Priority.BACKFILL, 10)

    # Le richieste critiche passano ancora: e' esattamente lo scopo della riserva.
    budget.spend(Priority.CRITICAL, 10)
    assert budget.used == 65


def test_il_limite_complessivo_e_invalicabile(budget):
    budget.spend(Priority.CRITICAL, 100)
    assert budget.remaining == 0
    with pytest.raises(BudgetExhausted):
        budget.spend(Priority.CRITICAL)


def test_uso_per_priorita(budget):
    budget.spend(Priority.CRITICAL, 2)
    budget.spend(Priority.BACKFILL, 5)
    assert budget.usage_by_priority() == {"CRITICAL": 2, "BACKFILL": 5}


def test_il_contatore_si_azzera_al_cambio_di_giorno(tmp_path):
    percorso = tmp_path / "budget.json"
    b = RequestBudget(state_path=percorso)
    b.spend(Priority.NORMAL, 40)
    # Riscriviamo lo stato come se fosse di ieri.
    stato = json.loads(percorso.read_text())
    stato["day"] = "2020-01-01"
    percorso.write_text(json.dumps(stato))
    assert RequestBudget(state_path=percorso).used == 0


def test_stato_corrotto_non_blocca(tmp_path):
    percorso = tmp_path / "budget.json"
    percorso.write_text("{non e' json")
    assert RequestBudget(state_path=percorso).used == 0


def test_rifiuta_quantita_non_positive(budget):
    with pytest.raises(ValueError):
        budget.spend(Priority.NORMAL, 0)


def test_reset(budget):
    budget.spend(Priority.NORMAL, 10)
    budget.reset()
    assert budget.used == 0


def test_la_riserva_scala_col_limite_giornaliero(tmp_path):
    """Con un piano diverso la riserva deve restare proporzionata, non fissa."""
    piccolo = RequestBudget(state_path=tmp_path / "p.json", daily_limit=10)
    assert piccolo.reserved_for(Priority.CRITICAL) == 2
    # Con riserve assolute tarate su 100 richieste, qui non passerebbe nulla.
    assert piccolo.allowance(Priority.NORMAL) == 8
    piccolo.spend(Priority.NORMAL, 8)
    assert piccolo.used == 8

    grande = RequestBudget(state_path=tmp_path / "g.json", daily_limit=7500)
    assert grande.reserved_for(Priority.CRITICAL) == 1875


def test_riserve_incoerenti_rifiutate(tmp_path):
    with pytest.raises(ValueError, match="riserve"):
        RequestBudget(
            state_path=tmp_path / "x.json",
            reserve_fraction={Priority.CRITICAL: 0.7, Priority.NORMAL: 0.5},
        )


# ---- il contatore regge processi che girano insieme -------------------------
#
# I processi che spendono sono tre e non uno: il sito, la raccolta giornaliera
# e la corsa del quarto d'ora. Contare una richiesta e' leggi-modifica-scrivi,
# e senza lucchetto due che lo fanno insieme leggono lo stesso numero e ne
# scrivono uno solo: una richiesta spesa che risulta mai avvenuta.


def test_due_processi_che_spendono_insieme_contano_tutto(tmp_path):
    """Nessuna richiesta puo' sparire dal conto."""
    import threading

    contatore = RequestBudget(state_path=tmp_path / "b.json", daily_limit=1000)
    quanti = 8
    per_thread = 10

    def spendi():
        for _ in range(per_thread):
            RequestBudget(
                state_path=tmp_path / "b.json", daily_limit=1000
            ).spend(Priority.NORMAL)

    fili = [threading.Thread(target=spendi) for _ in range(quanti)]
    for f in fili:
        f.start()
    for f in fili:
        f.join()

    assert contatore.used == quanti * per_thread


def test_il_contatore_non_resta_mai_scritto_a_meta(tmp_path):
    """Si scrive di fianco e si rinomina: chi legge vede il prima o il dopo.

    Un file troncato `_load` lo legge come "usate zero richieste", cioe' un
    budget intero regalato proprio nel giorno in cui qualcosa era andato
    storto.
    """
    percorso = tmp_path / "b.json"
    contatore = RequestBudget(state_path=percorso, daily_limit=100)
    contatore.spend(Priority.NORMAL, 5)

    assert json.loads(percorso.read_text(encoding="utf-8"))["used"] == 5
    assert not list(tmp_path.glob("*.tmp")), "nessun file provvisorio rimasto"
    assert not list(tmp_path.glob("*.lock")), "il lucchetto viene restituito"


def test_processi_veri_che_spendono_insieme_contano_tutto(tmp_path):
    """Come sopra, ma con PROCESSI: e' cio' che il lucchetto esiste per proteggere.

    Il lucchetto e' un file creato con ``O_EXCL``, cioe' una primitiva **fra
    processi**, e i processi che spendono sullo stesso `data/budget.json` sono
    tre: il sito, la raccolta giornaliera e la corsa del quarto d'ora. Otto
    thread dentro un interprete solo provano qualcosa, ma non quello. Il test
    coi thread era giusto e insufficiente insieme; questo chiude la meta' che
    mancava, ed e' quello che ha visto per primo il difetto del 4 settembre
    2026 anche fuori dai thread.
    """
    import subprocess
    import sys

    figlio = tmp_path / "figlio.py"
    figlio.write_text(
        """
import sys
sys.path.insert(0, sys.argv[2])
from serie_a_analyzer.data.budget import RequestBudget, Priority

for _ in range(10):
    RequestBudget(state_path=sys.argv[1], daily_limit=1000).spend(Priority.NORMAL)
""",
        encoding="utf-8",
    )
    stato = tmp_path / "b.json"
    radice = str(Path(serie_a_analyzer.__file__).parent.parent)

    processi = [
        subprocess.Popen(
            [sys.executable, str(figlio), str(stato), radice],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(4)
    ]
    for p in processi:
        _, errore = p.communicate(timeout=120)
        assert p.returncode == 0, f"un processo e' morto spendendo: {errore}"

    assert RequestBudget(state_path=stato, daily_limit=1000).used == 40


def test_un_lucchetto_negato_per_un_istante_non_e_un_lucchetto_mollato(
    tmp_path, monkeypatch, caplog
):
    """Su Windows un lucchetto appena tolto nega l'accesso invece di esistere.

    E' il difetto del 4 settembre 2026: quel permesso negato finiva nel ramo
    "filesystem che non collabora", che esce dalla sezione critica **subito e
    in silenzio**. Otto volte su 3192 prese, e ogni volta due scrittori
    entravano insieme nel contatore.
    """
    vero_open = os.open
    tentativi = []

    def open_che_nega_una_volta(percorso, *args, **kwargs):
        if str(percorso).endswith(".lock"):
            tentativi.append(percorso)
            if len(tentativi) == 1:
                raise PermissionError(13, "Accesso negato")
        return vero_open(percorso, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_che_nega_una_volta)
    contatore = RequestBudget(state_path=tmp_path / "b.json", daily_limit=100)
    with caplog.at_level("WARNING"):
        contatore.spend(Priority.NORMAL)

    # Il punto e' che ci ha RIPROVATO: e' l'unico modo di distinguere una
    # finestra che si chiude da sola da un permesso mancante per davvero.
    # Contare i tentativi e non guardare i log, perche' il ramo sbagliato
    # taceva — e un test che cerca il silenzio lo trova anche li'.
    assert len(tentativi) > 1, "il lucchetto e' stato mollato al primo rifiuto"
    assert contatore.used == 1
    assert "si prosegue senza" not in caplog.text


def test_un_filesystem_che_non_collabora_lo_dice(tmp_path, monkeypatch, caplog):
    """Rinunciare al lucchetto resta lecito, farlo in silenzio no."""
    vero_open = os.open

    def open_rotto(percorso, *args, **kwargs):
        if str(percorso).endswith(".lock"):
            raise OSError(5, "il filesystem non collabora")
        return vero_open(percorso, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_rotto)
    contatore = RequestBudget(state_path=tmp_path / "b.json", daily_limit=100)
    with caplog.at_level("WARNING"):
        contatore.spend(Priority.NORMAL)

    assert contatore.used == 1, "senza lucchetto si spende lo stesso"
    assert "si prosegue senza" in caplog.text


def test_due_scrittori_non_si_pestano_il_file_provvisorio(tmp_path):
    """Il file di fianco ha un nome per scrittore, non uno per contatore.

    Il lucchetto e' volutamente a perdere, quindi due scrittori possono
    coesistere per disegno. Con un `.tmp` solo per tutti si troncavano a
    vicenda e ne usciva un ``PermissionError`` che nessun chiamante cattura:
    la raccolta moriva li'.
    """
    percorso = tmp_path / "b.json"
    contatore = RequestBudget(state_path=percorso, daily_limit=1000)
    nomi = set()

    def annota():
        nomi.add(contatore._provvisorio().name)

    fili = [threading.Thread(target=annota) for _ in range(4)]
    for f in fili:
        f.start()
    for f in fili:
        f.join()

    assert len(nomi) == 4, f"scrittori diversi, stesso file provvisorio: {nomi}"
    assert all(n.endswith(".tmp") for n in nomi)


def test_senza_lucchetto_due_scrittori_non_si_fanno_male(tmp_path, monkeypatch):
    """Il ripiego del lucchetto deve essere innocuo, ed e' il suo unico scopo.

    Qui il lucchetto e' tolto del tutto, cioe' si mette in scena esattamente
    cio' che `_blocco` si concede di fare quando rinuncia. Prima, con un solo
    file provvisorio per tutti, ne usciva un ``PermissionError`` che nessun
    chiamante cattura: `client.py` chiama `spend` prima di spedire la
    richiesta, e i chiamanti prendono solo ``BudgetExhausted``. La raccolta
    moriva li', che e' il contrario di quello che il ripiego prometteva.
    """
    @contextmanager
    def senza_lucchetto(self):
        yield

    monkeypatch.setattr(RequestBudget, "_blocco", senza_lucchetto)
    percorso = tmp_path / "b.json"
    esplosioni = []

    def spendi():
        for _ in range(25):
            try:
                RequestBudget(
                    state_path=percorso, daily_limit=1000
                ).spend(Priority.NORMAL)
            except Exception as exc:  # pragma: no cover - non deve succedere
                esplosioni.append(exc)

    fili = [threading.Thread(target=spendi) for _ in range(4)]
    for f in fili:
        f.start()
    for f in fili:
        f.join()

    assert not esplosioni, f"spend ha sollevato: {esplosioni[:3]}"


def test_un_lettore_non_fa_perdere_conteggi(tmp_path, monkeypatch):
    """I lettori stanno fuori dal lucchetto, e su Windows bloccavano chi scrive.

    `client.py` scrive `budget.used` nel log dopo ogni richiesta e `/stato` lo
    mostra a ogni pagina: sono letture che avvengono mentre la raccolta scrive,
    e su Windows chi tiene aperto un file fa fallire chi lo sostituisce.
    Misurato il 4 settembre 2026, prima della correzione: con quattro lettori
    addosso fallivano 2932 scritture su 3000, e l'eccezione usciva da `spend`
    dove nessun chiamante la cattura.

    Il lettore qui e' finto, e di proposito: una corsa vera riproduce il
    difetto una volta ogni tanto, cioe' e' esattamente il tipo di test che
    resta verde mentre il difetto c'e'.
    """
    vero_replace = os.replace
    rifiuti = []

    def replace_occupato_le_prime_volte(origine, destinazione, *args, **kwargs):
        if len(rifiuti) < 3:
            rifiuti.append(origine)
            raise PermissionError(5, "Accesso negato")
        return vero_replace(origine, destinazione, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace_occupato_le_prime_volte)
    contatore = RequestBudget(state_path=tmp_path / "b.json", daily_limit=100)
    contatore.spend(Priority.NORMAL, 7)

    assert len(rifiuti) == 3, "il test non ha simulato niente"
    assert contatore.used == 7


def test_una_lettura_negata_non_diventa_zero(tmp_path, monkeypatch):
    """"Non riesco a leggere" non e' "sono state usate zero richieste".

    E' la piu' pericolosa delle tre: un permesso negato passeggero — che su
    Windows arriva da un lettore mentre un altro processo sostituisce il file
    — faceva rispondere zero, cioe' regalava un intero budget giornaliero.
    """
    percorso = tmp_path / "b.json"
    RequestBudget(state_path=percorso, daily_limit=100).spend(Priority.NORMAL, 40)

    vero_read = Path.read_text
    negate = []

    def read_che_nega_una_volta(self, *args, **kwargs):
        if self == percorso and not negate:
            negate.append(self)
            raise PermissionError(13, "Accesso negato")
        return vero_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_che_nega_una_volta)
    assert RequestBudget(state_path=percorso, daily_limit=100).used == 40
    assert negate, "il test non ha simulato niente"


def test_uno_stato_illeggibile_riparte_da_zero_ma_lo_dice(tmp_path, caplog):
    """Ripartire da zero regala cento richieste: e' una notizia, non un dettaglio."""
    percorso = tmp_path / "b.json"
    percorso.write_text('{"day": "2026-0', encoding="utf-8")

    with caplog.at_level("WARNING"):
        usate = RequestBudget(state_path=percorso, daily_limit=100).used

    assert usate == 0
    assert "illeggibile" in caplog.text
