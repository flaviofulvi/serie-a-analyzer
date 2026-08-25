"""Test della classifica del campionato.

Tre cose vanno difese qui, e sono tutte cose che sbagliate darebbero una
tabella **plausibile e falsa** — il tipo di errore che nessuno scopre guardando
la pagina.

La prima e' la stagione: l'archivio ne contiene cinque, e sommarle darebbe una
squadra con trecento punti. La seconda sono le parita', che la Serie A rompe
con gli scontri diretti e non con la differenza reti, ma solo quando quegli
scontri sono stati giocati tutti. La terza e' il vuoto: prima della prima
giornata la classifica **non esiste**, e non e' la stessa cosa di venti squadre
a zero punti.
"""

from datetime import datetime, timedelta, timezone

from serie_a_analyzer.data.classifica import classifica
from serie_a_analyzer.data.parsers import FixtureRecord

INIZIO = datetime(2026, 8, 22, 18, 0, tzinfo=timezone.utc)

SQUADRE = {1: "Inter", 2: "Milan", 3: "Roma", 4: "Lazio"}


def _partita(fixture_id, casa, ospite, gol_casa, gol_ospite, giorni=0, stagione=2026):
    conclusa = gol_casa is not None
    return FixtureRecord(
        api_id=fixture_id,
        season=stagione,
        league_id=135,
        round_label="Regular Season - 1",
        matchday=1,
        kickoff_utc=INIZIO + timedelta(days=giorni),
        status="finished" if conclusa else "scheduled",
        home_team_id=casa,
        home_team_name=SQUADRE[casa],
        away_team_id=ospite,
        away_team_name=SQUADRE[ospite],
        home_goals=gol_casa,
        away_goals=gol_ospite,
        home_goals_ht=None,
        away_goals_ht=None,
        venue_name="Stadio",
    )


def _per_nome(righe):
    return {r.squadra: r for r in righe}


def test_i_punti_si_contano_come_nel_calcio():
    tabella = _per_nome(classifica([
        _partita(1, 1, 2, 2, 0),          # Inter vince
        _partita(2, 3, 4, 1, 1, giorni=1),  # pareggio
    ], stagione=2026))

    assert tabella["Inter"].punti == 3
    assert tabella["Milan"].punti == 0
    assert tabella["Roma"].punti == 1
    assert tabella["Lazio"].punti == 1
    assert tabella["Inter"].vinte == 1 and tabella["Milan"].perse == 1
    assert tabella["Roma"].pari == 1


def test_la_classifica_e_di_una_stagione_sola():
    """La trappola numero uno: cinque campionati in archivio.

    Senza il filtro l'Inter uscirebbe con sei punti dopo due partite giocate in
    due stagioni diverse, e la tabella sarebbe plausibile e falsa.
    """
    partite = [
        _partita(1, 1, 2, 2, 0, stagione=2026),
        _partita(2, 1, 2, 3, 0, stagione=2025),
    ]

    corrente = _per_nome(classifica(partite, stagione=2026))

    assert corrente["Inter"].punti == 3
    assert corrente["Inter"].giocate == 1
    assert corrente["Inter"].fatti == 2


def test_le_partite_non_giocate_non_entrano():
    tabella = classifica([
        _partita(1, 1, 2, 2, 0),
        _partita(2, 3, 4, None, None, giorni=1),
    ], stagione=2026)

    assert {r.squadra for r in tabella} == {"Inter", "Milan"}


def test_senza_partite_la_classifica_non_esiste():
    """Prima della prima giornata non e' piatta: non c'e'.

    Venti squadre a zero punti sarebbero un'informazione — "hanno giocato e
    non hanno fatto punti" — e sarebbe falsa.
    """
    assert classifica([], stagione=2026) == []
    assert classifica([_partita(1, 1, 2, None, None)], stagione=2026) == []


def test_a_parita_di_punti_decide_la_differenza_reti_se_manca_il_ritorno():
    """A stagione in corso e' il caso normale, e va detto in pagina.

    Inter e Roma sono a tre punti; si sono affrontate una volta sola, quindi
    gli scontri diretti non sono applicabili e decide la differenza reti.
    """
    tabella = classifica([
        _partita(1, 1, 3, 1, 0),           # Inter batte Roma: +1
        _partita(2, 3, 2, 4, 0, giorni=1),  # Roma ne fa 4 al Milan: +4
        _partita(3, 4, 2, 0, 1, giorni=2),  # il Milan ne vince una
    ], stagione=2026)

    ordine = [r.squadra for r in tabella]
    assert ordine[0] == "Roma", "differenza reti +4 contro +1"
    assert ordine[1] == "Inter"
    assert tabella[0].criterio == "differenza reti"


def test_a_ritorno_completato_decidono_gli_scontri_diretti():
    """La regola vera della Serie A, che non e' la differenza reti.

    Inter e Roma finiscono a pari punti. Negli scontri diretti l'Inter ha
    fatto meglio, ma la Roma ha una differenza reti generale piu' alta: se
    vincesse la differenza reti l'ordine sarebbe rovesciato, ed e' esattamente
    l'errore che questo test impedisce.
    """
    tabella = classifica([
        # Scontri diretti completati: Inter 4 punti, Roma 1.
        _partita(1, 1, 3, 2, 0),
        _partita(2, 3, 1, 1, 1, giorni=1),
        # Col Milan la Roma raccoglie tre punti piu' dell'Inter, che li
        # riporta in parita', e li raccoglie dilagando: differenza reti sua.
        _partita(3, 3, 2, 5, 0, giorni=2),
        _partita(4, 2, 3, 1, 0, giorni=3),
        _partita(5, 2, 1, 1, 0, giorni=4),
        _partita(6, 1, 2, 0, 1, giorni=5),
    ], stagione=2026)

    per_nome = _per_nome(tabella)
    assert per_nome["Inter"].punti == per_nome["Roma"].punti, "premessa del test"
    assert per_nome["Roma"].differenza > per_nome["Inter"].differenza, "premessa"

    ordine = [r.squadra for r in tabella]
    assert ordine.index("Inter") < ordine.index("Roma")
    assert per_nome["Inter"].criterio == "scontri diretti"


def test_la_posizione_e_gia_scritta_nelle_righe():
    tabella = classifica([
        _partita(1, 1, 2, 3, 0),
        _partita(2, 3, 4, 1, 0, giorni=1),
    ], stagione=2026)

    assert [r.posizione for r in tabella] == [1, 2, 3, 4]


def test_il_nome_puo_arrivare_dall_archivio_invece_che_dalla_partita():
    """Un club rinominato a stagione in corso comparirebbe due volte."""
    tabella = classifica(
        [_partita(1, 1, 2, 1, 0)],
        stagione=2026,
        nome=lambda team_id: f"Squadra {team_id}",
    )

    assert {r.squadra for r in tabella} == {"Squadra 1", "Squadra 2"}
