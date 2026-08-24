"""Test dei messaggi su Telegram.

Cio' che si verifica non e' l'estetica del testo ma le tre cose che rendono un
bot utile invece che ignorato: che non mandi due volte la stessa cosa, che
decida da cio' che c'e' sul disco invece che dall'orologio (cosi' un messaggio
perso a computer spento arriva dopo), e che un guasto del canale non possa
fermare la raccolta.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx

from serie_a_analyzer.notifiche import (
    API_TELEGRAM,
    MAX_CARATTERI,
    RegistroInvii,
    RigaEsito,
    RigaPronostico,
    Telegram,
    allarme_quote_perse,
    allarme_tabellini_fermi,
    giorno_da_pronosticare,
    giorno_da_riepilogare,
    invia_quel_che_serve,
    messaggio_pronostici,
    messaggio_resoconto,
    messaggio_tabellini_pronti,
    partite_senza_quote,
)

ORA = datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc)


@dataclass
class PartitaFinta:
    """Il minimo che le notifiche guardano di una partita."""

    api_id: int
    kickoff_utc: datetime
    home_team_name: str = "Inter"
    away_team_name: str = "Monza"
    home_goals: int | None = None
    away_goals: int | None = None
    casa: str = "Inter"
    ospite: str = "Monza"

    @property
    def is_finished(self) -> bool:
        return self.home_goals is not None


def _partita(ore_da_ora, fid=1, gol=None):
    return PartitaFinta(
        api_id=fid,
        kickoff_utc=ORA + timedelta(hours=ore_da_ora),
        home_goals=None if gol is None else gol[0],
        away_goals=None if gol is None else gol[1],
    )


@pytest.fixture
def registro(tmp_path):
    return RegistroInvii(path=tmp_path / "notifiche.json")


@pytest.fixture
def telegram():
    return Telegram(token="t", chat_id="c")


def _rotta(status=200):
    return respx.post(url__startswith=f"{API_TELEGRAM}/bot").mock(
        return_value=httpx.Response(status, json={"ok": True})
    )


# ---- il canale --------------------------------------------------------------


def test_senza_token_non_manda_e_non_esplode():
    """Il silenzio e' il default: senza configurazione il sistema gira uguale."""
    assert Telegram().manda("ciao") is False


@respx.mock
def test_manda_un_messaggio(telegram):
    rotta = _rotta()
    assert telegram.manda("ciao") is True
    assert rotta.call_count == 1
    assert rotta.calls[0].request.url.path.endswith("/sendMessage")


@respx.mock
def test_una_rete_giu_non_solleva(telegram):
    """Perdere un messaggio e' un fastidio, fermare la raccolta e' un guasto."""
    respx.post(url__startswith=f"{API_TELEGRAM}/bot").mock(
        side_effect=httpx.ConnectError("niente rete")
    )
    assert telegram.manda("ciao") is False


@respx.mock
def test_un_errore_di_telegram_non_solleva(telegram):
    _rotta(status=400)
    assert telegram.manda("ciao") is False


@respx.mock
def test_un_messaggio_lunghissimo_viene_troncato(telegram):
    """Telegram rifiuta oltre i 4096 caratteri: meglio monco che mai arrivato."""
    rotta = _rotta()
    telegram.manda("x" * (MAX_CARATTERI * 2))
    mandato = rotta.calls[0].request.content.decode("utf-8")
    assert "troncato" in mandato


@respx.mock
def test_il_rumore_di_fondo_parte_solo_se_verboso():
    zitto, chiacchierone = Telegram(token="t", chat_id="c"), Telegram(
        token="t", chat_id="c", verboso=True
    )
    _rotta()
    assert zitto.manda_se_verboso("non e' il momento") is False
    assert chiacchierone.manda_se_verboso("non e' il momento") is True


# ---- il registro degli invii ------------------------------------------------


def test_il_registro_ricorda_cosa_e_gia_stato_detto(registro):
    assert registro.gia_inviato("pronostici:2026-08-23") is False
    registro.segna("pronostici:2026-08-23")
    assert registro.gia_inviato("pronostici:2026-08-23") is True


def test_un_registro_illeggibile_non_ferma_niente(tmp_path):
    """Al massimo si riceve un messaggio due volte: il danno piu' piccolo."""
    percorso = tmp_path / "rotto.json"
    percorso.write_text("{ questo non e' json", encoding="utf-8")
    assert RegistroInvii(path=percorso).gia_inviato("qualsiasi") is False


# ---- cosa c'e' da dire ------------------------------------------------------


def test_si_pronostica_il_giorno_che_deve_ancora_giocarsi(registro):
    partite = [_partita(+9)]
    assert giorno_da_pronosticare(partite, registro, ORA) == ORA.date()


def test_a_giornata_cominciata_non_si_pronostica_piu(registro):
    """Un pronostico dopo il via non e' un pronostico: e' la regola del registro."""
    assert giorno_da_pronosticare([_partita(-1)], registro, ORA) is None


def test_i_pronostici_non_si_mandano_due_volte(registro):
    registro.segna(f"pronostici:{ORA.date()}")
    assert giorno_da_pronosticare([_partita(+9)], registro, ORA) is None


def test_il_resoconto_arriva_il_giorno_dopo_se_il_pc_era_spento(registro):
    """La prova della regola: si decide dal contenuto, non dall'orologio.

    Le partite di ieri sono finite e nessuno le ha raccontate, perche' alle
    23:45 il computer era spento. Alle 9 del mattino dopo il resoconto parte,
    e nessuna riga di codice ha dovuto prevedere quel caso.
    """
    ieri = [_partita(-15, fid=1, gol=(2, 0)), _partita(-13, fid=2, gol=(1, 1))]
    assert giorno_da_riepilogare(ieri, registro, ORA) == (ORA - timedelta(days=1)).date()


def test_non_si_riepiloga_un_giorno_ancora_in_corso(registro):
    partite = [_partita(-3, fid=1, gol=(2, 0)), _partita(-1, fid=2)]
    assert giorno_da_riepilogare(partite, registro, ORA) is None


def test_non_si_riepiloga_un_giorno_col_posticipo_ancora_da_giocare(registro):
    """Successo davvero il 24 agosto 2026: alle 20:32 il resoconto della
    domenica parti' con la sola Bologna-Lazio, mentre Roma-Fiorentina doveva
    ancora cominciare. Il guaio non e' l'anticipo ma il seguito: segnata la
    chiave, il posticipo non sarebbe stato raccontato mai piu'."""
    partite = [_partita(-3, fid=1, gol=(2, 0)), _partita(+1, fid=2)]
    assert giorno_da_riepilogare(partite, registro, ORA) is None


def test_non_si_torna_indietro_a_riepilogare_l_archeologia(registro):
    """Raccontato il piu' recente, i precedenti restano dove sono."""
    partite = [_partita(-15, fid=1, gol=(2, 0)), _partita(-40, fid=2, gol=(0, 0))]
    registro.segna(f"resoconto:{(ORA - timedelta(days=1)).date()}")
    assert giorno_da_riepilogare(partite, registro, ORA) is None


def test_le_partite_senza_quote_si_riconoscono():
    partite = [_partita(-2, fid=7), _partita(-2, fid=8)]
    assert [p.api_id for p in partite_senza_quote(partite, [8])] == [7]


# ---- i messaggi -------------------------------------------------------------


def test_il_messaggio_del_mattino_dichiara_cosa_non_sa():
    """Alle 8 non ci sono ne' formazioni ne' quote: dirlo e' parte del messaggio."""
    riga = RigaPronostico(ora="20:45", casa="Genoa", ospite="Napoli",
                          prob_1=0.23, prob_x=0.26, prob_2=0.51)
    testo = messaggio_pronostici(date(2026, 8, 23), [riga])
    assert "Genoa - Napoli" in testo
    assert "secco" in testo
    assert "media di campionato" in testo


def test_il_messaggio_dice_perche_una_partita_non_ha_pronostico():
    """Una riga vuota si leggerebbe come "non sappiamo", che e' meno di quanto sappiamo."""
    riga = RigaPronostico(ora="18:30", casa="Pisa", ospite="Como",
                          motivo="senza storia sufficiente")
    assert "senza storia sufficiente" in messaggio_pronostici(date(2026, 8, 23), [riga])


def test_il_resoconto_mette_il_brier_prima_degli_azzeccati():
    """Predire sempre "1" ne azzecca il 45%: mettere quel numero per primo
    insegnerebbe a leggere la cosa sbagliata."""
    righe = [RigaEsito("Inter", "Monza", 2, 0, prob_1=0.62, prob_x=0.22, prob_2=0.16)]
    testo = messaggio_resoconto(date(2026, 8, 22), righe)
    assert testo.index("Brier") < testo.index("azzeccato")


def test_il_brier_di_una_partita_e_quello_multiclasse():
    riga = RigaEsito("Inter", "Monza", 2, 0, prob_1=1.0, prob_x=0.0, prob_2=0.0)
    assert riga.brier == pytest.approx(0.0)
    assert riga.prob_del_vero == pytest.approx(1.0)


def test_una_partita_senza_pronostico_congelato_non_entra_nel_brier():
    """Non si puo' misurare cio' che non e' stato detto prima del via."""
    righe = [RigaEsito("Inter", "Monza", 2, 0)]
    testo = messaggio_resoconto(date(2026, 8, 22), righe)
    assert "Brier" not in testo
    assert "congelato" in testo


def test_l_allarme_sulle_quote_dice_che_e_definitivo():
    testo = allarme_quote_perse([_partita(-2)])
    assert "non si recuperano" in testo


# ---- il giro completo -------------------------------------------------------


@respx.mock
def test_il_giro_manda_i_pronostici_una_volta_sola(telegram, registro):
    rotta = _rotta()
    partite = [_partita(+9)]
    righe = [RigaPronostico(ora="18:00", casa="Inter", ospite="Monza", prob_1=0.6,
                            prob_x=0.2, prob_2=0.2)]

    primo = invia_quel_che_serve(
        telegram, registro, partite, pronostici=lambda _: righe, adesso=ORA
    )
    secondo = invia_quel_che_serve(
        telegram, registro, partite, pronostici=lambda _: righe, adesso=ORA
    )
    assert primo == [f"pronostici:{ORA.date()}"]
    assert secondo == []
    assert rotta.call_count == 1


@respx.mock
def test_un_messaggio_non_partito_non_risulta_mandato(telegram, registro):
    """Altrimenti il resoconto perso in un momento di rete assente non
    arriverebbe mai piu'."""
    _rotta(status=500)
    partite = [_partita(+9)]
    righe = [RigaPronostico(ora="18:00", casa="Inter", ospite="Monza", prob_1=0.6)]
    assert invia_quel_che_serve(
        telegram, registro, partite, pronostici=lambda _: righe, adesso=ORA
    ) == []
    assert registro.gia_inviato(f"pronostici:{ORA.date()}") is False


@respx.mock
def test_il_modello_non_viene_stimato_se_non_c_e_niente_da_dire(telegram, registro):
    """La ragione per cui i pronostici arrivano da una funzione e non da un
    argomento: questo giro passa ogni quarto d'ora, e stimare il modello per
    scoprire che non serviva costerebbe due secondi novanta volte al giorno.
    """
    _rotta()
    chiamate = []

    def pronostici(partite):
        chiamate.append(partite)
        return []

    invia_quel_che_serve(
        telegram, registro, [_partita(-3, gol=(1, 0))], pronostici=pronostici, adesso=ORA
    )
    assert chiamate == []


@respx.mock
def test_il_giro_avvisa_delle_quote_perse(telegram, registro):
    rotta = _rotta()
    cominciata = _partita(-2, fid=7, gol=(1, 0))
    mandati = invia_quel_che_serve(
        telegram, registro, [cominciata],
        pronostici=lambda _: [], quotate=[], adesso=ORA,
    )
    assert any(c.startswith("quote-perse") for c in mandati)
    assert rotta.call_count >= 1


@respx.mock
def test_senza_canale_attivo_il_giro_non_fa_niente(registro):
    assert invia_quel_che_serve(
        Telegram(), registro, [_partita(+9)], pronostici=lambda _: [], adesso=ORA
    ) == []


# ---- tabellini rimasti a meta' partita ---------------------------------------
#
# L'allarme arriva tardi apposta. Un tabellino provvisorio nelle ore dopo la
# partita e' normale e si risolve da solo: dirlo ogni sera insegnerebbe a
# ignorare il messaggio, che e' esattamente il difetto che questo modulo
# cerca di non avere.


@respx.mock
def test_i_tabellini_fermi_si_dicono_una_volta_sola(telegram, registro):
    rotta = _rotta()
    fermo = _partita(-30, fid=1550091)

    for _ in range(3):
        invia_quel_che_serve(
            telegram, registro, [fermo],
            pronostici=lambda _p: [],
            tabellini_fermi=[fermo],
            adesso=ORA,
        )

    assert rotta.call_count == 1


@respx.mock
def test_senza_tabellini_fermi_non_si_dice_niente(telegram, registro):
    rotta = _rotta()

    mandati = invia_quel_che_serve(
        telegram, registro, [_partita(-30)],
        pronostici=lambda _p: [],
        tabellini_fermi=[],
        adesso=ORA,
    )

    assert mandati == []
    assert rotta.call_count == 0


@respx.mock
def test_una_partita_nuova_che_si_arrende_viene_detta(telegram, registro):
    """La chiave porta gli id, non la data: e' un fatto sulle partite."""
    rotta = _rotta()
    prima = _partita(-30, fid=1550091)
    seconda = _partita(-28, fid=1550093)

    invia_quel_che_serve(
        telegram, registro, [prima, seconda],
        pronostici=lambda _p: [], tabellini_fermi=[prima], adesso=ORA,
    )
    invia_quel_che_serve(
        telegram, registro, [prima, seconda],
        pronostici=lambda _p: [], tabellini_fermi=[prima, seconda], adesso=ORA,
    )

    assert rotta.call_count == 2


def test_il_messaggio_dice_le_partite_e_cosa_significa():
    testo = allarme_tabellini_fermi([_partita(-30, fid=1550091)])

    assert "1550091" in testo
    assert "Inter" in testo and "Monza" in testo
    # Deve dire che NON e' un guasto della raccolta, o si va a cercare un
    # errore dove non c'e'.
    assert "guasto" in testo


# ---- tabellini arrivati tutti ------------------------------------------------
#
# L'altra faccia dell'allarme qui sopra, e il momento in cui vale la pena
# dirlo: non quando finisce un'attesa ma quando quelle partite COMINCIANO A
# SERVIRE — il risultato c'era gia', minuti gol assist e voti no, e sono
# quelli che alimentano le schede giocatore e il peso delle formazioni.


@respx.mock
def test_i_tabellini_arrivati_si_dicono_una_volta_sola(telegram, registro):
    rotta = _rotta()
    finita = _partita(-30, fid=11, gol=(2, 1))

    for _ in range(3):
        invia_quel_che_serve(
            telegram, registro, [finita],
            pronostici=lambda _p: [],
            tabellini_pronti=[11],
            adesso=ORA,
        )

    assert rotta.call_count == 1


@respx.mock
def test_una_giornata_incompleta_non_si_annuncia(telegram, registro):
    """Basta un tabellino che manca e la giornata non e' pronta: dirlo lo
    stesso significherebbe promettere dati che non ci sono."""
    rotta = _rotta()
    una = _partita(-30, fid=11, gol=(2, 1))
    altra = _partita(-28, fid=12, gol=(0, 0))

    mandati = invia_quel_che_serve(
        telegram, registro, [una, altra],
        pronostici=lambda _p: [],
        tabellini_pronti=[11],
        adesso=ORA,
    )

    assert not any(c.startswith("tabellini-pronti") for c in mandati)
    assert rotta.call_count == 0


@respx.mock
def test_quando_arriva_anche_lultimo_la_giornata_si_annuncia(telegram, registro):
    rotta = _rotta()
    una = _partita(-30, fid=11, gol=(2, 1))
    altra = _partita(-28, fid=12, gol=(0, 0))

    invia_quel_che_serve(
        telegram, registro, [una, altra],
        pronostici=lambda _p: [], tabellini_pronti=[11], adesso=ORA,
    )
    mandati = invia_quel_che_serve(
        telegram, registro, [una, altra],
        pronostici=lambda _p: [], tabellini_pronti=[11, 12], adesso=ORA,
    )

    assert any(c.startswith("tabellini-pronti") for c in mandati)
    assert rotta.call_count == 1


@respx.mock
def test_una_partita_ancora_da_giocare_non_blocca_il_giorno_prima(telegram, registro):
    """Il giorno di ieri e' concluso e completo; oggi si gioca ancora. Il
    messaggio riguarda ieri, e non deve aspettare stasera."""
    rotta = _rotta()
    ieri = _partita(-30, fid=11, gol=(2, 1))
    oggi = _partita(+5, fid=12)

    mandati = invia_quel_che_serve(
        telegram, registro, [ieri, oggi],
        pronostici=lambda _p: [], tabellini_pronti=[11], adesso=ORA,
    )

    # Parte anche il messaggio dei pronostici di oggi: qui interessa solo che
    # quello dei tabellini non abbia aspettato la fine della giornata.
    assert any(c.startswith("tabellini-pronti") for c in mandati)
    assert rotta.call_count == 2


@respx.mock
def test_un_giorno_rimasto_indietro_viene_annunciato_quando_arriva(
    telegram, registro
):
    """E' la ragione per cui questa notifica non si ferma al giorno piu'
    recente, ed e' successo davvero: il 23 agosto 2026 le partite della
    domenica avevano gia' i tabellini mentre due del sabato erano ferme alla
    fotografia del feed live. Fermandosi al piu' recente, il sabato — cioe'
    proprio quello che si stava aspettando — non verrebbe annunciato mai."""
    rotta = _rotta()
    sabato = _partita(-40, fid=21, gol=(1, 1))
    domenica = _partita(-20, fid=22, gol=(2, 0))

    primi = invia_quel_che_serve(
        telegram, registro, [sabato, domenica],
        pronostici=lambda _p: [], tabellini_pronti=[22], adesso=ORA,
    )
    dopo = invia_quel_che_serve(
        telegram, registro, [sabato, domenica],
        pronostici=lambda _p: [], tabellini_pronti=[21, 22], adesso=ORA,
    )

    assert primi == [f"tabellini-pronti:{domenica.kickoff_utc.date()}"]
    assert dopo == [f"tabellini-pronti:{sabato.kickoff_utc.date()}"]
    assert rotta.call_count == 2


@respx.mock
def test_una_partita_ancora_in_campo_non_rende_pronta_la_giornata(
    telegram, registro
):
    """Il messaggio elenca tutte le partite della data: se a decidere bastano
    quelle finite, la prima riga dell'elenco e' gia' una bugia.

    E' il 24 agosto 2026 alle 20:48. Bologna-Lazio ha il suo tabellino,
    Roma-Fiorentina e' al primo minuto — e su Telegram arrivo' "Tutte e 2 le
    partite hanno il tabellino definitivo", con la Roma in elenco. Il tabellino
    di una partita in corso non e' provvisorio: non esiste.
    """
    rotta = _rotta()
    finita = _partita(-2, fid=11, gol=(2, 1))
    in_campo = _partita(-0.05, fid=12)

    mandati = invia_quel_che_serve(
        telegram, registro, [finita, in_campo],
        pronostici=lambda _p: [], tabellini_pronti=[11], adesso=ORA,
    )

    assert not any(c.startswith("tabellini-pronti") for c in mandati)
    assert rotta.call_count == 0


@respx.mock
def test_la_giornata_si_annuncia_quando_anche_il_posticipo_ha_finito(
    telegram, registro
):
    """La stessa giornata a fine serata: il posticipo e' finito, il suo
    tabellino c'e', e adesso l'elenco dice il vero."""
    rotta = _rotta()
    finita = _partita(-4, fid=11, gol=(2, 1))
    posticipo = _partita(-2, fid=12, gol=(1, 1))

    mandati = invia_quel_che_serve(
        telegram, registro, [finita, posticipo],
        pronostici=lambda _p: [], tabellini_pronti=[11, 12], adesso=ORA,
    )

    assert mandati == [f"tabellini-pronti:{finita.kickoff_utc.date()}"]
    assert rotta.call_count == 1


@respx.mock
def test_le_giornate_archeologiche_restano_fuori(telegram, registro):
    """Il prezzo di non fermarsi al giorno piu' recente sarebbe un messaggio
    per ogni giornata gia' archiviata, la prima volta che si accende questa
    notifica. Lo paga la finestra: oltre una settimana non e' piu' una
    novita'."""
    rotta = _rotta()
    vecchie = [
        _partita(-24 * giorni, fid=100 + giorni, gol=(1, 0))
        for giorni in range(8, 30)
    ]
    recente = _partita(-30, fid=11, gol=(2, 1))

    mandati = invia_quel_che_serve(
        telegram, registro, vecchie + [recente],
        pronostici=lambda _p: [],
        tabellini_pronti=[f.api_id for f in vecchie] + [11],
        adesso=ORA,
    )

    assert [c for c in mandati if c.startswith("tabellini-pronti")] == [
        f"tabellini-pronti:{recente.kickoff_utc.date()}"
    ]
    assert rotta.call_count == 1


@respx.mock
def test_senza_lelenco_dei_pronti_non_si_dice_niente(telegram, registro):
    """Chi non passa l'argomento non deve ricevere il messaggio: e' la stessa
    regola di `esiti_congelati`, che spegne il resoconto se non c'e'."""
    rotta = _rotta()

    mandati = invia_quel_che_serve(
        telegram, registro, [_partita(-30, fid=11, gol=(2, 1))],
        pronostici=lambda _p: [], adesso=ORA,
    )

    assert not any(c.startswith("tabellini-pronti") for c in mandati)
    assert rotta.call_count == 0


def test_il_messaggio_dice_quante_partite_e_a_cosa_servono():
    da = datetime(2026, 8, 23, 18, 30, tzinfo=timezone.utc).date()
    testo = messaggio_tabellini_pronti(da, [_partita(-30, fid=11, gol=(2, 1))])

    assert "23/08/2026" in testo
    assert "Inter" in testo and "Monza" in testo
    # Deve dire a cosa servono, altrimenti e' un messaggio che non chiede
    # niente e non spiega niente.
    assert "schede giocatore" in testo
