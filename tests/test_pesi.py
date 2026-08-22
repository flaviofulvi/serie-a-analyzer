"""Test dei pesi dei giocatori.

Il rischio che questi test presidiano non e' un errore di calcolo: e' che il
metodo dica numeri **fiduciosi su niente**. Un giocatore entrato dieci minuti e
andato a segno, preso alla lettera, varrebbe piu' di tutta la sua squadra; una
squadra con tre partite in archivio darebbe pesi che raccontano solo chi ha
segnato per primo. Da qui la maggior parte dei casi qui sotto: verificano che
il modulo **si rifiuti** di rispondere, o risponda piano, quando non sa.
"""

from datetime import datetime, timedelta, timezone

import pytest

from serie_a_analyzer.data.pesi import (
    GIOCATORI_IN_CAMPO,
    LIMITE_SPOSTAMENTO,
    MASSIMO_SCONOSCIUTI,
    MINIMO_PARTITE,
    Rapporto,
    aggiusta_gol_attesi,
    partite_recenti,
    pesi_squadra,
    rapporto_produzione,
    vale_la_pena,
)
from serie_a_analyzer.data.players import ArchivioGiocatori, Prestazione

ADESSO = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
CASA = 505


def _prestazione(fixture_id, player_id, minuti, gol=0, assist=0, quando=None):
    return Prestazione(
        fixture_id=fixture_id,
        quando=quando or ADESSO - timedelta(days=fixture_id),
        player_id=player_id,
        nome=f"Giocatore {player_id}",
        team_id=CASA,
        squadra="Casa",
        minuti=minuti,
        titolare=minuti >= 60,
        posizione="M",
        numero=None,
        gol=gol,
        assist=assist,
        gialli=0,
        rossi=0,
        tiri=None,
        tiri_in_porta=None,
        voto=None,
    )


def _archivio(n_partite=10, marcatore=1, gol_del_marcatore=1):
    """Una squadra di 11 titolari fissi, uno dei quali segna sempre lui."""
    righe = []
    for f in range(1, n_partite + 1):
        for pid in range(1, GIOCATORI_IN_CAMPO + 1):
            righe.append(_prestazione(
                f, pid, 90,
                gol=gol_del_marcatore if pid == marcatore else 0,
            ))
    return ArchivioGiocatori(prestazioni=righe)


# ---- quando il modulo deve tacere -------------------------------------------


def test_senza_tabellini_non_si_stima_niente():
    """E' il caso di oggi: l'archivio e' vuoto e la macchina resta spenta."""
    assert pesi_squadra(ArchivioGiocatori(prestazioni=[]), CASA) is None


def test_sotto_il_minimo_di_partite_non_si_stima_niente():
    pochi = _archivio(n_partite=MINIMO_PARTITE - 1)
    assert pesi_squadra(pochi, CASA) is None

    quanto_basta = _archivio(n_partite=MINIMO_PARTITE)
    assert pesi_squadra(quanto_basta, CASA) is not None


def test_una_squadra_che_non_esiste_nell_archivio():
    assert pesi_squadra(_archivio(), team_id=999) is None


# ---- lo shrinkage ------------------------------------------------------------


def test_una_meteora_non_vale_piu_di_tutta_la_squadra():
    """Dieci minuti e un gol: il tasso grezzo e' assurdo, quello usato no."""
    righe = []
    for f in range(1, 11):
        for pid in range(1, GIOCATORI_IN_CAMPO + 1):
            righe.append(_prestazione(f, pid, 90, gol=1 if pid == 1 else 0))
    # La meteora: una sola apparizione, dieci minuti, un gol.
    righe.append(_prestazione(11, 99, 10, gol=1))
    pesi = pesi_squadra(ArchivioGiocatori(prestazioni=righe), CASA)

    meteora = pesi.pesi[99]
    bomber = pesi.pesi[1]

    # Senza shrinkage varrebbe 9 gol a partita.
    assert meteora.grezzo_per_90 == pytest.approx(9.0)
    # Con lo shrinkage vale meno di chi segna ogni domenica da dieci giornate.
    assert meteora.per_90 < bomber.per_90
    assert meteora.per_90 < 1.0


def test_chi_gioca_sempre_resta_vicino_al_suo_tasso():
    pesi = pesi_squadra(_archivio(n_partite=30), CASA)
    bomber = pesi.pesi[1]
    # Un gol a partita, trenta partite: lo shrinkage lo sposta, non lo cancella.
    assert bomber.grezzo_per_90 == pytest.approx(1.0)
    assert 0.75 < bomber.per_90 < 1.0


# ---- formazione tipo e rapporto ---------------------------------------------


def test_la_formazione_tipo_sono_gli_undici_piu_impiegati():
    righe = []
    for f in range(1, 11):
        for pid in range(1, GIOCATORI_IN_CAMPO + 1):
            righe.append(_prestazione(f, pid, 90))
        righe.append(_prestazione(f, 50, 5))     # una riserva fissa
    pesi = pesi_squadra(ArchivioGiocatori(prestazioni=righe), CASA)

    assert len(pesi.formazione_tipo) == GIOCATORI_IN_CAMPO
    assert 50 not in pesi.formazione_tipo


def test_la_formazione_tipo_da_rapporto_uno():
    pesi = pesi_squadra(_archivio(), CASA)
    r = rapporto_produzione(pesi, pesi.formazione_tipo)
    assert r.valore == pytest.approx(1.0)
    assert r.sconosciuti == 0


def test_senza_il_migliore_la_produzione_cala():
    """Il caso Nico Paz: sta dentro quasi tutto quello che la squadra crea."""
    righe = []
    for f in range(1, 11):
        for pid in range(1, GIOCATORI_IN_CAMPO + 1):
            # Il numero 1 fa tutto: 2 gol a partita, gli altri niente.
            righe.append(_prestazione(f, pid, 90, gol=2 if pid == 1 else 0))
        righe.append(_prestazione(f, 50, 10))    # la riserva che lo sostituira'
    pesi = pesi_squadra(ArchivioGiocatori(prestazioni=righe), CASA)

    senza_di_lui = [p for p in pesi.formazione_tipo if p != 1] + [50]
    r = rapporto_produzione(pesi, senza_di_lui)

    assert r.valore < 1.0
    assert r.variazione_percentuale < 0


def test_un_acquisto_nuovo_vale_la_media_non_zero():
    """Non sapere nulla di lui non e' sapere che non vale niente."""
    pesi = pesi_squadra(_archivio(), CASA)
    # Si sostituisce un titolare qualunque (non il marcatore) con uno ignoto.
    sostituito = [p for p in pesi.formazione_tipo if p != 1][0]
    schierati = [p for p in pesi.formazione_tipo if p != sostituito] + [777]

    r = rapporto_produzione(pesi, schierati)
    assert r.sconosciuti == 1
    assert r.noti == GIOCATORI_IN_CAMPO - 1
    # Il rapporto si muove di poco, non crolla.
    assert 0.9 < r.valore < 1.1


def test_troppi_sconosciuti_rendono_il_rapporto_inaffidabile():
    pesi = pesi_squadra(_archivio(), CASA)
    ignoti = list(range(900, 900 + MASSIMO_SCONOSCIUTI + 1))
    schierati = list(pesi.formazione_tipo[: GIOCATORI_IN_CAMPO - len(ignoti)]) + ignoti

    r = rapporto_produzione(pesi, schierati)
    assert r.sconosciuti == MASSIMO_SCONOSCIUTI + 1
    assert not r.affidabile


def test_nessun_titolare_nessun_rapporto():
    pesi = pesi_squadra(_archivio(), CASA)
    assert rapporto_produzione(pesi, []) is None


# ---- l'aggiustamento ---------------------------------------------------------


def test_senza_rapporto_i_gol_attesi_non_si_toccano():
    """Non sapere e' un motivo per non toccare niente, non per indovinare."""
    assert aggiusta_gol_attesi(1.40, None) == 1.40


def test_un_rapporto_inaffidabile_non_muove_niente():
    inaffidabile = Rapporto(valore=0.5, noti=2, sconosciuti=9)
    assert aggiusta_gol_attesi(1.40, inaffidabile) == 1.40


def test_l_aggiustamento_segue_la_direzione_del_rapporto():
    meno = Rapporto(valore=0.80, noti=11, sconosciuti=0)
    piu = Rapporto(valore=1.20, noti=11, sconosciuti=0)
    assert aggiusta_gol_attesi(1.40, meno, fattore=1.0) < 1.40
    assert aggiusta_gol_attesi(1.40, piu, fattore=1.0) > 1.40


def test_il_fattore_smorza_l_aggiustamento():
    r = Rapporto(valore=0.80, noti=11, sconosciuti=0)
    pieno = aggiusta_gol_attesi(1.40, r, fattore=1.0)
    meta = aggiusta_gol_attesi(1.40, r, fattore=0.5)
    assert pieno < meta < 1.40


def test_il_tetto_impedisce_i_numeri_da_circo():
    """Una squadra senza dieci undicesimi non diventa un'altra squadra."""
    crollo = Rapporto(valore=0.10, noti=11, sconosciuti=0)
    lam = aggiusta_gol_attesi(1.40, crollo, fattore=1.0)
    assert lam == pytest.approx(1.40 * (1 - LIMITE_SPOSTAMENTO))

    boom = Rapporto(valore=3.0, noti=11, sconosciuti=0)
    assert aggiusta_gol_attesi(1.40, boom, fattore=1.0) == pytest.approx(
        1.40 * (1 + LIMITE_SPOSTAMENTO)
    )


# ---- quando vale la pena scrivere una riga ----------------------------------


def test_non_si_scrive_niente_se_non_cambia_niente():
    """Una riga identica a quella secca sporca il confronto fra le modalita'."""
    uguale = Rapporto(valore=1.001, noti=11, sconosciuti=0)
    assert not vale_la_pena(uguale, uguale)


def test_non_si_scrive_niente_senza_pesi():
    """E' cio' che tiene spenta la macchina finche' i tabellini non bastano."""
    assert not vale_la_pena(None, None)


def test_basta_una_squadra_che_cambia():
    cambia = Rapporto(valore=0.85, noti=11, sconosciuti=0)
    ferma = Rapporto(valore=1.0, noti=11, sconosciuti=0)
    assert vale_la_pena(cambia, ferma)
    assert vale_la_pena(None, cambia)


def test_un_rapporto_inaffidabile_non_giustifica_una_riga():
    inaffidabile = Rapporto(valore=0.5, noti=1, sconosciuti=10)
    assert not vale_la_pena(inaffidabile, None)


# ---- la finestra -------------------------------------------------------------


class PartitaFinta:
    def __init__(self, giorni_fa, finita=True):
        self.kickoff_utc = ADESSO - timedelta(days=giorni_fa)
        self.is_finished = finita


def test_la_finestra_prende_solo_le_concluse_e_recenti():
    partite = [
        PartitaFinta(5),
        PartitaFinta(200),               # troppo vecchia
        PartitaFinta(10, finita=False),  # non ancora giocata
    ]
    scelte = partite_recenti(partite, ADESSO, giorni=120)
    assert len(scelte) == 1
    assert scelte[0].kickoff_utc == ADESSO - timedelta(days=5)
