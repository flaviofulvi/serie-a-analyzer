"""Test dell'importazione di una stagione da un file di risultati.

Un'importazione sbagliata non si vede: il modello continua a rispondere, le
pagine si aprono, e i pronostici sono peggiori senza che niente lo dichiari.
Questi test coprono i modi in cui puo' andare storta in silenzio.

* una squadra scritta in modo diverso ("Milan" / "AC Milan") deve restare una
  squadra sola: sdoppiata, sarebbero due meta' di storia e due forze deboli;
* un nome che non si riconosce non deve entrare con un id inventato, e deve
  fermare tutto — meglio nessuna stagione che una stagione con un fantasma;
* un file monco o con righe doppie non deve finire in archivio di nascosto;
* le partite importate devono arrivare fino al modello, che e' l'unico motivo
  per cui si importano;
* e non devono mai raddoppiare quelle vere, il giorno in cui la stessa
  stagione venisse ricomprata sul serio.
"""

from datetime import datetime, timedelta, timezone

import pytest

from serie_a_analyzer.data.dataset import load_dataset
from serie_a_analyzer.data.importato import (
    FileIllegibile,
    e_importata,
    firma_importato,
    id_sintetico,
    importa,
    indice_per_nome,
    normalizza,
    prepara,
    squadre_viste,
    stagioni_importate,
    store_stagione,
)
from serie_a_analyzer.data.parsers import parse_fixtures
from serie_a_analyzer.data.store import SeasonStore

STAGIONE = 2025

# Quattro squadre bastano per un campionato all'italiana completo: 4*3 = 12
# partite, tre in casa e tre fuori per ognuna.
NOMI = {505: "Inter", 489: "AC Milan", 496: "Juventus", 497: "AS Roma"}
NEL_FILE = {505: "Inter", 489: "Milan", 496: "Juventus", 497: "Roma"}

INTESTAZIONE = "Match Number,Round Number,Date,Location,Home Team,Away Team,Result"


PRIMO_GIORNO = datetime(2025, 8, 23, 16, 30, tzinfo=timezone.utc)


def _campionato(nomi=None, salta=(), extra=()):
    """Le righe di un campionato completo fra le squadre indicate."""
    nomi = nomi or NEL_FILE
    squadre = list(nomi.values())
    righe = []
    numero = 0
    for i, casa in enumerate(squadre):
        for ospite in squadre:
            if casa == ospite:
                continue
            numero += 1
            if numero in salta:
                continue
            quando = PRIMO_GIORNO + timedelta(days=numero)
            righe.append(
                f"{numero},{(numero - 1) // len(squadre) + 1},"
                f"{quando.strftime('%d/%m/%Y %H:%M')},Stadio,"
                f"{casa},{ospite},{i % 3} - 1"
            )
    return righe + list(extra)


def scrivi(tmp_path, righe, nome="serie-a-2025.csv"):
    percorso = tmp_path / nome
    percorso.write_text("\n".join([INTESTAZIONE, *righe]), encoding="utf-8")
    return percorso


# ---- identita' delle squadre -----------------------------------------------


def test_grafie_diverse_sono_la_stessa_squadra():
    """"Milan" e "AC Milan" devono arrivare allo stesso id.

    E' il rischio principale dell'importazione: due grafie non riconosciute
    creerebbero due squadre con meta' partite ciascuna, e il modello le
    stimerebbe entrambe deboli senza che nessuno se ne accorga.
    """
    assert normalizza("AC Milan") == normalizza("Milan")
    assert normalizza("AS Roma") == normalizza("Roma")
    assert normalizza("Hellas Verona") == normalizza("hellas verona")


def test_una_sigla_non_svuota_un_nome_corto():
    """"Inter" non deve diventare una stringa vuota togliendo le sigle."""
    assert normalizza("Inter") == "inter"
    assert normalizza("AC") == "ac"


def test_squadre_diverse_restano_diverse():
    """La squadra femminile non e' quella maschile."""
    assert normalizza("AC Milan W") != normalizza("AC Milan")
    assert normalizza("Milan U19") != normalizza("Milan")


def test_un_nome_ambiguo_non_si_aggancia():
    """Due id per lo stesso nome: meglio non saperlo che indovinare."""
    indice = indice_per_nome({1: "Pisa", 2: "Pisa", 3: "Inter"})
    assert "pisa" not in indice
    assert indice["inter"] == 3


def test_squadre_viste_legge_solo_i_club(tmp_path):
    """Nei JSON su disco si riconoscono i club, non leghe e giocatori."""
    import json

    percorso = tmp_path / "buste.json"
    percorso.write_text(json.dumps({
        "response": [{
            "league": {"id": 135, "name": "Serie A", "season": 2025},
            "teams": {
                "home": {"id": 801, "name": "Pisa", "logo": "x.png"},
                "away": {"id": 870, "name": "Padova", "logo": "y.png"},
            },
        }]
    }), encoding="utf-8")

    trovate = squadre_viste([percorso])
    assert trovate == {801: "Pisa", 870: "Padova"}


# ---- id sintetici -----------------------------------------------------------


def test_gli_id_sintetici_non_collidono_con_quelli_veri():
    """Gli id veri di API-Football stanno intorno a 1.550.000."""
    sintetico = id_sintetico(STAGIONE, 1)
    assert sintetico > 90_000_000
    assert e_importata(sintetico)
    assert not e_importata(1_550_092)


def test_reimportare_non_duplica(tmp_path):
    """Lo stesso file, due volte: le stesse righe, non il doppio."""
    percorso = scrivi(tmp_path, _campionato())
    store = SeasonStore(root=tmp_path / "archivio")

    primo = importa(percorso, store, STAGIONE, NOMI)
    secondo = importa(percorso, store, STAGIONE, NOMI)

    assert primo.nuove == 12 and primo.scritto
    assert secondo.nuove == 0 and secondo.aggiornate == 0
    assert len(store.load_fixtures()) == 12


# ---- controlli sul file -----------------------------------------------------


def test_un_nome_sconosciuto_ferma_tutto(tmp_path):
    """Nessun id inventato: senza aggancio non si importa niente."""
    nomi_file = {**NEL_FILE, 999: "Squadra Misteriosa"}
    percorso = scrivi(tmp_path, _campionato(nomi_file))
    store = SeasonStore(root=tmp_path / "archivio")

    rapporto = importa(percorso, store, STAGIONE, NOMI)

    assert not rapporto.scritto
    assert "Squadra Misteriosa" in rapporto.sconosciute
    assert not store.load_fixtures()


def test_un_nome_si_puo_agganciare_a_mano(tmp_path):
    """La corrispondenza passata a mano risolve chi non si riconosce."""
    nomi_file = {**NEL_FILE, 801: "Pisa"}
    percorso = scrivi(tmp_path, _campionato(nomi_file))
    store = SeasonStore(root=tmp_path / "archivio")

    rapporto = importa(
        percorso, store, STAGIONE, NOMI, aggiunte={"Pisa": 801}
    )

    assert rapporto.scritto
    assert rapporto.squadre["Pisa"] == 801


def test_un_file_monco_non_entra_di_nascosto(tmp_path):
    """Meno partite del dovuto: si dichiara e non si scrive."""
    percorso = scrivi(tmp_path, _campionato(salta=(3, 4)))
    store = SeasonStore(root=tmp_path / "archivio")

    rapporto = importa(percorso, store, STAGIONE, NOMI)

    assert not rapporto.scritto
    assert rapporto.avvisi
    assert not store.load_fixtures()


def test_un_file_monco_si_puo_importare_se_lo_si_chiede(tmp_path):
    """La scelta resta possibile, ma deve essere esplicita."""
    percorso = scrivi(tmp_path, _campionato(salta=(3, 4)))
    store = SeasonStore(root=tmp_path / "archivio")

    rapporto = importa(
        percorso, store, STAGIONE, NOMI, anche_se_incompleto=True
    )

    assert rapporto.scritto
    assert rapporto.avvisi  # dichiarato comunque
    assert len(store.load_fixtures()) == 10


def test_un_accoppiamento_ripetuto_e_bloccante(tmp_path):
    """Due risultati per la stessa partita: non c'e' modo di sapere quale vale."""
    doppia = "99,1,25/08/2025 16:30,Stadio,Inter,Juventus,3 - 0"
    percorso = scrivi(tmp_path, _campionato(extra=(doppia,)))
    store = SeasonStore(root=tmp_path / "archivio")

    rapporto = importa(percorso, store, STAGIONE, NOMI, anche_se_incompleto=True)

    assert not rapporto.scritto
    assert any("ripetuti" in p for p in rapporto.problemi)


def test_una_riga_illeggibile_si_dichiara(tmp_path):
    """Un risultato malformato non si indovina."""
    rotta = "99,1,25/08/2025 16:30,Stadio,Inter,Roma,tre a zero"
    percorso = scrivi(tmp_path, [*_campionato(salta=(2,)), rotta])
    store = SeasonStore(root=tmp_path / "archivio")

    rapporto = importa(percorso, store, STAGIONE, NOMI, anche_se_incompleto=True)

    assert not rapporto.scritto
    assert any("illeggibile" in p for p in rapporto.problemi)


def test_un_file_senza_le_colonne_giuste_lo_dice(tmp_path):
    """Un file di tutt'altro genere non deve produrre un archivio vuoto."""
    percorso = tmp_path / "altro.csv"
    percorso.write_text("a,b,c\n1,2,3\n", encoding="utf-8")

    with pytest.raises(FileIllegibile):
        prepara(percorso, STAGIONE, NOMI)


# ---- la busta prodotta ------------------------------------------------------


def test_la_busta_e_leggibile_dai_parser_veri(tmp_path):
    """Cio' che scriviamo deve passare da `parse_fixture` come una busta API."""
    percorso = scrivi(tmp_path, _campionato())
    buste, rapporto = prepara(percorso, STAGIONE, NOMI)

    assert rapporto.valido
    partite = parse_fixtures(buste)
    assert len(partite) == len(buste)

    una = partite[0]
    assert una.season == STAGIONE
    assert una.league_id == 135
    assert una.is_finished
    assert una.kickoff_utc.tzinfo is not None
    assert una.matchday == 1
    # Il nome in archivio e' quello canonico dell'API, non quello del file.
    assert una.home_team_name in NOMI.values()


def test_una_partita_senza_risultato_resta_da_giocare(tmp_path):
    """Una riga senza gol e' una partita in programma, non uno 0-0."""
    riga = "99,1,25/08/2025 16:30,Stadio,Inter,Roma,"
    percorso = scrivi(tmp_path, [riga])
    buste, rapporto = prepara(percorso, STAGIONE, NOMI)

    partite = parse_fixtures(buste)
    assert rapporto.da_giocare == 1
    assert partite[0].status == "scheduled"
    assert partite[0].home_goals is None


def test_la_data_senza_fuso_si_legge_come_utc(tmp_path):
    """L'archivio conserva le date in UTC: e' il fuso dell'API."""
    percorso = scrivi(tmp_path, ["1,1,23/08/2025 16:30,Stadio,Inter,Roma,1 - 0"])
    buste, _ = prepara(percorso, STAGIONE, NOMI)

    quando = parse_fixtures(buste)[0].kickoff_utc
    assert quando == datetime(2025, 8, 23, 16, 30, tzinfo=timezone.utc)


# ---- l'aggancio col resto ---------------------------------------------------


class _ClientFinto:
    """Un client che non ha rete: il dataset non deve chiedergli niente."""

    class _Budget:
        used = 0

    budget = _Budget()


def test_le_partite_importate_arrivano_al_modello(tmp_path):
    """E' l'unico motivo per cui si importano."""
    percorso = scrivi(tmp_path, _campionato())
    store_imp = store_stagione(tmp_path / "importato", STAGIONE)
    importa(percorso, store_imp, STAGIONE, NOMI)

    dataset = load_dataset(
        _ClientFinto(), SeasonStore(root=tmp_path / "serie_a"), seasons=(),
        importati=stagioni_importate(tmp_path / "importato"),
    )

    assert dataset.n_importate == 12
    assert dataset.stagioni_importate == [STAGIONE]
    assert len(dataset.matches()) == 12
    # Le squadre hanno il nome, non l'id come stringa di ripiego.
    assert "Inter" in {m.home for m in dataset.matches()}


def test_il_dato_pagato_vince_sull_importato(tmp_path):
    """La stessa stagione in due archivi non deve diventare due campionati.

    Gli id delle partite importate sono sintetici: senza una regola esplicita
    non verrebbero sovrascritti da quelli veri ma affiancati, e il modello
    vedrebbe ogni partita due volte.
    """
    percorso = scrivi(tmp_path, _campionato())
    store_imp = store_stagione(tmp_path / "importato", STAGIONE)
    importa(percorso, store_imp, STAGIONE, NOMI)

    # La stessa stagione, ricomprata: id veri, una partita sola per semplicita'.
    store_pagato = SeasonStore(root=tmp_path / "storico" / str(STAGIONE))
    store_pagato.upsert_fixtures([{
        "fixture": {"id": 1_550_092, "date": "2025-08-23T16:30:00+00:00",
                    "status": {"short": "FT"}},
        "league": {"id": 135, "season": STAGIONE, "round": "Regular Season - 1"},
        "teams": {"home": {"id": 505, "name": "Inter"},
                  "away": {"id": 497, "name": "AS Roma"}},
        "goals": {"home": 2, "away": 0},
        "score": {"halftime": {"home": 1, "away": 0}},
    }])

    dataset = load_dataset(
        _ClientFinto(), SeasonStore(root=tmp_path / "serie_a"), seasons=(),
        storici={STAGIONE: store_pagato},
        importati=stagioni_importate(tmp_path / "importato"),
    )

    assert dataset.n_importate == 0
    assert dataset.stagioni_importate == []
    assert len(dataset.fixtures) == 1


def test_la_firma_cambia_quando_si_importa(tmp_path):
    """Il sito ristima il modello solo se l'impronta si muove."""
    radice = tmp_path / "importato"
    assert firma_importato(radice) == ()

    percorso = scrivi(tmp_path, _campionato())
    importa(percorso, store_stagione(radice, STAGIONE), STAGIONE, NOMI)

    assert firma_importato(radice) != ()
    assert list(stagioni_importate(radice)) == [STAGIONE]


def test_una_cartella_vuota_non_e_una_stagione(tmp_path):
    """Il residuo di un tentativo fallito non deve contare come stagione."""
    (tmp_path / "importato" / "2025").mkdir(parents=True)
    assert stagioni_importate(tmp_path / "importato") == {}


def test_il_file_di_partenza_resta_accanto_all_archivio(tmp_path):
    """Reimportare deve essere possibile anche se il CSV originale sparisce."""
    percorso = scrivi(tmp_path, _campionato())
    store = SeasonStore(root=tmp_path / "archivio")

    importa(percorso, store, STAGIONE, NOMI)

    conservato = store.root / "sorgente.csv"
    assert conservato.exists()
    assert conservato.read_text(encoding="utf-8") == percorso.read_text(encoding="utf-8")
