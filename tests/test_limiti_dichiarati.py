"""I limiti che l'API dichiara di se' (C1, 11 settembre 2026).

Il giorno che conta e' il 24 settembre 2026, quando il piano Pro finisce: il
.env dira' ancora 7500 richieste al giorno e un secondo di freno, il server
100 al giorno e dieci al minuto. Molti di questi test sono quel giorno,
rigiocato — e la cosa da verificare e' che non serva ricordarsi di niente.

Gli header sono quelli letti da una risposta vera di `/status` l'11 settembre
2026, piano Pro.
"""

import json
from datetime import timedelta

import httpx
import pytest
import respx

from serie_a_analyzer.data.budget import (
    BudgetExhausted,
    LimitiDichiarati,
    Priority,
    RequestBudget,
)
from serie_a_analyzer.data.cache import ResponseCache
from serie_a_analyzer.data.client import (
    API_SPORTS_BASE_URL,
    ApiFootballClient,
    ApiFootballError,
    intervallo_per_tetto,
    limiti_dagli_header,
)
from serie_a_analyzer.notifiche import API_TELEGRAM, RegistroInvii, Telegram, invia_quel_che_serve

PRO = {
    "x-ratelimit-requests-limit": "7500",
    "x-ratelimit-requests-remaining": "7494",
    "x-ratelimit-limit": "300",
    "x-ratelimit-remaining": "299",
}


def _gratuito(rimaste_giorno=99, rimaste_minuto=9):
    return {
        "x-ratelimit-requests-limit": "100",
        "x-ratelimit-requests-remaining": str(rimaste_giorno),
        "x-ratelimit-limit": "10",
        "x-ratelimit-remaining": str(rimaste_minuto),
    }


# ---- leggere gli header -------------------------------------------------------


def test_gli_header_veri_si_leggono():
    assert limiti_dagli_header(httpx.Headers(PRO)) == LimitiDichiarati(
        giorno=7500, rimaste_giorno=7494, minuto=300, rimaste_minuto=299
    )


def test_le_maiuscole_non_contano():
    """Il server scrive `X-RateLimit-Limit` in una documentazione e minuscolo in un'altra."""
    limiti = limiti_dagli_header(httpx.Headers({"X-RateLimit-Limit": "10"}))
    assert limiti.minuto == 10


def test_una_risposta_senza_header_non_dichiara_niente():
    assert limiti_dagli_header(httpx.Headers({})) is None
    assert limiti_dagli_header(httpx.Headers({"x-ratelimit-requests-limit": "tanti"})) is None


def test_un_tetto_a_zero_e_un_header_rotto_non_un_piano():
    """Nessun piano concede zero richieste: crederci fermerebbe tutto."""
    assert limiti_dagli_header(httpx.Headers({"x-ratelimit-requests-limit": "0"})) is None


def test_una_rimanenza_a_zero_invece_e_vera():
    limiti = limiti_dagli_header(
        httpx.Headers({"x-ratelimit-limit": "10", "x-ratelimit-remaining": "0"})
    )
    assert limiti.rimaste_minuto == 0


def test_il_freno_si_ricava_dal_tetto_con_la_proporzione_del_gratuito():
    assert intervallo_per_tetto(10) == pytest.approx(7.0)
    assert intervallo_per_tetto(300) < 1.0


# ---- il contatore -------------------------------------------------------------


@pytest.fixture
def budget_pro(tmp_path):
    """Il .env di oggi: 7500 al giorno."""
    return RequestBudget(state_path=tmp_path / "budget.json", daily_limit=7500)


def test_un_limite_dichiarato_piu_basso_vince(budget_pro):
    budget_pro.registra_dichiarazione(LimitiDichiarati(giorno=100))

    assert budget_pro.limite_in_vigore == 100
    assert budget_pro.remaining == 100
    # Le riserve scendono col tetto: su 7500 quella delle formazioni varrebbe
    # da sola 1875 richieste, cioe' diciotto giornate del gratuito.
    assert budget_pro.reserved_for(Priority.CRITICAL) == 25

    budget_pro.spend(Priority.CRITICAL, 100)
    with pytest.raises(BudgetExhausted):
        budget_pro.spend(Priority.CRITICAL)


def test_un_limite_dichiarato_piu_alto_non_si_prende_da_solo(tmp_path):
    """Salire resta un gesto: chi scrive 100 nel .env puo' volerlo."""
    budget = RequestBudget(state_path=tmp_path / "b.json", daily_limit=100)
    budget.registra_dichiarazione(LimitiDichiarati(giorno=7500))
    assert budget.limite_in_vigore == 100


def test_il_contatore_si_allinea_al_server_solo_verso_l_alto(budget_pro):
    budget_pro.spend(Priority.NORMAL, 3)

    budget_pro.registra_dichiarazione(LimitiDichiarati(giorno=100, rimaste_giorno=90))
    assert budget_pro.used == 10

    # Un conto del server piu' basso del nostro e' una richiesta ancora in
    # volo, non un errore nostro: abbassare ridarebbe via proprio quella.
    budget_pro.registra_dichiarazione(LimitiDichiarati(giorno=100, rimaste_giorno=95))
    assert budget_pro.used == 10


def test_il_conto_di_ieri_non_entra_nel_giorno_nuovo(budget_pro):
    """Una risposta spedita alle 23:59:59 e letta dopo mezzanotte.

    Col contatore che non scende mai, portarsi dietro le 85 richieste di ieri
    vorrebbe dire perdere la giornata intera sul gratuito.
    """
    ieri = budget_pro._today() - timedelta(days=1)
    budget_pro.registra_dichiarazione(
        LimitiDichiarati(giorno=100, rimaste_giorno=15), giorno=ieri
    )
    assert budget_pro.used == 0
    # Il piano invece vale a qualunque ora.
    assert budget_pro.limite_in_vigore == 100


def test_il_piano_si_ricorda_dopo_mezzanotte(tmp_path):
    percorso = tmp_path / "budget.json"
    RequestBudget(state_path=percorso, daily_limit=7500).registra_dichiarazione(
        LimitiDichiarati(giorno=100, rimaste_giorno=60, minuto=10)
    )
    stato = json.loads(percorso.read_text())
    stato["day"] = "2020-01-01"
    percorso.write_text(json.dumps(stato))

    domani = RequestBudget(state_path=percorso, daily_limit=7500)
    assert domani.used == 0
    assert domani.limite_in_vigore == 100
    assert domani.piano_dichiarato()["minuto"] == 10


def test_il_reset_non_dimentica_il_piano(budget_pro):
    budget_pro.registra_dichiarazione(LimitiDichiarati(giorno=100))
    budget_pro.spend(Priority.NORMAL, 5)
    budget_pro.reset()
    assert budget_pro.used == 0
    assert budget_pro.limite_in_vigore == 100


def test_una_dichiarazione_identica_non_riscrive_il_file(budget_pro, monkeypatch):
    """Succede a quasi ogni risposta: ogni scrittura in piu' e' contesa coi lettori."""
    budget_pro.registra_dichiarazione(LimitiDichiarati(giorno=7500, minuto=300))

    scritture = []
    salva = budget_pro._save
    monkeypatch.setattr(budget_pro, "_save", lambda s: (scritture.append(s), salva(s)))
    prima, dopo = budget_pro.registra_dichiarazione(LimitiDichiarati(giorno=7500, minuto=300))

    assert scritture == []
    assert prima["giorno"] == dopo["giorno"] == 7500


def test_senza_dichiarazioni_vale_il_env(budget_pro):
    assert budget_pro.piano_dichiarato() is None
    assert budget_pro.limite_in_vigore == 7500


# ---- il client ----------------------------------------------------------------


@pytest.fixture
def orologio(monkeypatch):
    """Un orologio finto, cosi' i test del freno non dormono davvero."""

    class Orologio:
        def __init__(self):
            self.adesso = 1000.0
            self.dormite = []

        def monotonic(self):
            return self.adesso

        def sleep(self, secondi):
            self.dormite.append(secondi)
            self.adesso += secondi

    falso = Orologio()
    monkeypatch.setattr("serie_a_analyzer.data.client.time.monotonic", falso.monotonic)
    monkeypatch.setattr("serie_a_analyzer.data.client.time.sleep", falso.sleep)
    return falso


def _client(tmp_path, secondi=1.0, limite=7500):
    """Il client con il .env del Pro: 7500 al giorno, un secondo di freno."""
    return ApiFootballClient(
        api_key="k",
        cache=ResponseCache(root=tmp_path / "cache"),
        budget=RequestBudget(state_path=tmp_path / "budget.json", daily_limit=limite),
        min_interval_seconds=secondi,
    )


def _rotta(payload, headers, status=200):
    return respx.get(url__startswith=f"{API_SPORTS_BASE_URL}/fixtures").mock(
        return_value=httpx.Response(status, json=payload, headers=headers)
    )


@respx.mock
def test_il_giorno_del_downgrade_non_serve_toccare_il_env(tmp_path, orologio, risposta_api):
    """Il .env dice ancora Pro, il server dice gratuito: vince il server."""
    _rotta(risposta_api, _gratuito())
    c = _client(tmp_path)

    c.fixtures(season=2024)
    c.fixtures(season=2023)

    assert c.budget.limite_in_vigore == 100
    # Un secondo, come dice il .env, avrebbe preso un 429 alla undicesima.
    assert orologio.dormite == [pytest.approx(7.0)]
    assert c.intervallo_in_vigore == pytest.approx(7.0)


@respx.mock
def test_sul_pro_vince_il_freno_del_env(tmp_path, orologio, risposta_api):
    """Il server lascerebbe correre a 0,23 secondi: nessuno ha fretta."""
    _rotta(risposta_api, PRO)
    c = _client(tmp_path)

    c.fixtures(season=2024)
    c.fixtures(season=2023)

    assert orologio.dormite == [pytest.approx(1.0)]
    assert c.budget.limite_in_vigore == 7500


@respx.mock
def test_il_contatore_prende_il_conto_del_server(tmp_path, orologio, risposta_api):
    _rotta(risposta_api, _gratuito(rimaste_giorno=60))
    c = _client(tmp_path)
    c.fixtures(season=2024)
    assert c.budget.used == 40


@respx.mock
def test_col_minuto_esaurito_si_aspetta_un_minuto_intero(tmp_path, orologio, risposta_api):
    """Il freno di un processo non vede gli altri due: il server si'."""
    _rotta(risposta_api, _gratuito(rimaste_minuto=0))
    c = _client(tmp_path)

    c.fixtures(season=2024)
    c.fixtures(season=2023)

    assert orologio.dormite == [pytest.approx(60.0)]


@respx.mock
def test_anche_un_429_insegna_i_limiti(tmp_path, orologio):
    _rotta({}, _gratuito(rimaste_minuto=0), status=429)
    c = _client(tmp_path)

    with pytest.raises(ApiFootballError, match="dichiara di 10"):
        c.fixtures(season=2024)

    assert c.budget.limite_in_vigore == 100
    assert c.intervallo_in_vigore == pytest.approx(7.0)


@respx.mock
def test_un_processo_nuovo_ricorda_il_tetto_al_minuto(tmp_path, orologio, risposta_api):
    """`/stato` mostra il freno del sito, che puo' non fare richieste per giorni."""
    _rotta(risposta_api, _gratuito())
    _client(tmp_path).fixtures(season=2024)

    nuovo = _client(tmp_path)
    assert nuovo.intervallo_in_vigore == pytest.approx(7.0)


@respx.mock
def test_senza_header_resta_tutto_come_prima(tmp_path, orologio, risposta_api):
    _rotta(risposta_api, {})
    c = _client(tmp_path)
    c.fixtures(season=2024)

    assert c.budget.piano_dichiarato() is None
    assert c.budget.limite_in_vigore == 7500
    assert c.intervallo_in_vigore == pytest.approx(1.0)


@respx.mock
def test_un_guasto_nel_leggere_i_limiti_non_costa_la_risposta(
    tmp_path, orologio, risposta_api, monkeypatch
):
    """La richiesta e' gia' pagata: il dato arriva comunque."""
    _rotta(risposta_api, _gratuito())
    c = _client(tmp_path)

    def rotto(*_a, **_k):
        raise OSError("disco pieno")

    monkeypatch.setattr(c.budget, "registra_dichiarazione", rotto)
    assert len(c.fixtures(season=2024)) == 2


@respx.mock
def test_il_freno_spento_resta_spento(tmp_path, orologio, risposta_api):
    """Zero nel .env vale solo nei test, e deve valere anche con gli header."""
    _rotta(risposta_api, _gratuito(rimaste_minuto=0))
    c = _client(tmp_path, secondi=0.0)
    c.fixtures(season=2024)
    c.fixtures(season=2023)
    assert orologio.dormite == []


# ---- il messaggio -------------------------------------------------------------


@pytest.fixture
def telegram():
    return Telegram(token="t", chat_id="c")


@pytest.fixture
def registro(tmp_path):
    return RegistroInvii(path=tmp_path / "notifiche.json")


def _telegram_finto():
    return respx.post(url__startswith=f"{API_TELEGRAM}/bot").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )


def _giro(telegram, registro, budget):
    return invia_quel_che_serve(telegram, registro, [], pronostici=lambda _p: [], budget=budget)


@respx.mock
def test_il_piano_cambiato_si_dice_una_volta_sola(telegram, registro, budget_pro):
    rotta = _telegram_finto()
    budget_pro.registra_dichiarazione(LimitiDichiarati(giorno=100, minuto=10))

    for _ in range(3):
        _giro(telegram, registro, budget_pro)

    assert rotta.call_count == 1
    testo = json.loads(rotta.calls[0].request.content)["text"]
    assert "Non serve fare niente" in testo
    assert "DAILY_REQUEST_LIMIT=100" in testo
    assert "API_MIN_INTERVAL_SECONDS=7" in testo


@respx.mock
def test_un_piano_uguale_al_env_non_dice_niente(telegram, registro, budget_pro):
    rotta = _telegram_finto()
    budget_pro.registra_dichiarazione(LimitiDichiarati(giorno=7500, minuto=300))

    assert _giro(telegram, registro, budget_pro) == []
    assert rotta.call_count == 0


@respx.mock
def test_un_piano_piu_alto_chiede_di_alzare_il_env(telegram, registro, tmp_path):
    rotta = _telegram_finto()
    budget = RequestBudget(state_path=tmp_path / "b.json", daily_limit=100)
    budget.registra_dichiarazione(LimitiDichiarati(giorno=7500, minuto=300))

    _giro(telegram, registro, budget)

    testo = json.loads(rotta.calls[0].request.content)["text"]
    assert "non si prende da solo" in testo
    assert "DAILY_REQUEST_LIMIT=7500" in testo
