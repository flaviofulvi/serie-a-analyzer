"""Trasformazione dei dati del dominio in oggetti pronti per le pagine.

I template non devono fare calcoli. Qui una partita diventa un blocco di
etichette e percentuali gia' formattate, e chi scrive l'HTML si occupa solo di
dove metterle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from ..data.parsers import FixtureRecord
from ..model.markets import MatchProbabilities

FUSO_ITALIA = ZoneInfo("Europe/Rome")

_GIORNI = ("lunedi'", "martedi'", "mercoledi'", "giovedi'", "venerdi'", "sabato", "domenica")
_MESI = ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic")


def ora_italiana(quando: datetime) -> datetime:
    return quando.astimezone(FUSO_ITALIA)


def formatta_data(quando: datetime) -> str:
    """'sabato 22 ago, 18:30' — in ora italiana, non UTC."""
    locale = ora_italiana(quando)
    return f"{_GIORNI[locale.weekday()]} {locale.day} {_MESI[locale.month - 1]}, {locale:%H:%M}"


def formatta_giorno(quando: datetime) -> str:
    locale = ora_italiana(quando)
    return f"{locale.day} {_MESI[locale.month - 1]} {locale.year}"


@dataclass
class RigaPartita:
    """Una partita come appare in elenco, con o senza pronostico."""

    id: int
    casa: str
    ospite: str
    casa_id: int
    ospite_id: int
    quando: str
    giornata: int | None
    conclusa: bool
    gol_casa: int | None
    gol_ospite: int | None

    # Presenti solo se il modello conosce entrambe le squadre.
    prob: MatchProbabilities | None = None
    gol_attesi_casa: float | None = None
    gol_attesi_ospite: float | None = None

    @property
    def ha_pronostico(self) -> bool:
        return self.prob is not None

    @property
    def risultato(self) -> str:
        if not self.conclusa or self.gol_casa is None:
            return ""
        return f"{self.gol_casa}-{self.gol_ospite}"

    @property
    def esito_reale(self) -> str | None:
        """'1', 'X' o '2' se la partita e' finita."""
        if not self.conclusa or self.gol_casa is None:
            return None
        if self.gol_casa > self.gol_ospite:
            return "1"
        if self.gol_casa < self.gol_ospite:
            return "2"
        return "X"

    @property
    def esito_previsto(self) -> str | None:
        if self.prob is None:
            return None
        trio = {"1": self.prob.home_win, "X": self.prob.draw, "2": self.prob.away_win}
        return max(trio, key=trio.get)

    @property
    def pronostico_azzeccato(self) -> bool | None:
        reale, previsto = self.esito_reale, self.esito_previsto
        if reale is None or previsto is None:
            return None
        return reale == previsto

    @property
    def risultato_probabile(self) -> str:
        if self.prob is None:
            return ""
        (casa, ospite), _ = self.prob.top_scorelines[0]
        return f"{casa}-{ospite}"


def riga_partita(
    fixture: FixtureRecord,
    nome,
    prob: MatchProbabilities | None = None,
    gol_attesi: tuple[float, float] | None = None,
) -> RigaPartita:
    return RigaPartita(
        id=fixture.api_id,
        casa=nome(fixture.home_team_id),
        ospite=nome(fixture.away_team_id),
        casa_id=fixture.home_team_id,
        ospite_id=fixture.away_team_id,
        quando=formatta_data(fixture.kickoff_utc),
        giornata=fixture.matchday,
        conclusa=fixture.is_finished,
        gol_casa=fixture.home_goals,
        gol_ospite=fixture.away_goals,
        prob=prob,
        gol_attesi_casa=gol_attesi[0] if gol_attesi else None,
        gol_attesi_ospite=gol_attesi[1] if gol_attesi else None,
    )


def griglia_risultati(matrice, massimo: int = 5) -> list[dict]:
    """La matrice dei risultati esatti, ritagliata e pronta da disegnare.

    Si mostrano solo i punteggi fino a ``massimo`` gol per squadra: oltre, le
    probabilita' sono cosi' piccole da essere rumore visivo. La cella piu'
    probabile viene marcata, ed e' l'informazione che l'occhio cerca per prima.
    """
    ritaglio = matrice[: massimo + 1, : massimo + 1]
    picco = float(ritaglio.max())
    righe = []
    for casa in range(ritaglio.shape[0]):
        celle = []
        for ospite in range(ritaglio.shape[1]):
            p = float(ritaglio[casa, ospite])
            celle.append({
                "casa": casa,
                "ospite": ospite,
                # Intensita' relativa al massimo: con valori assoluti la
                # griglia sarebbe quasi tutta bianca.
                "intensita": p / picco if picco else 0.0,
                "prob": p,
                "massimo": p == picco,
                "esito": "1" if casa > ospite else ("2" if casa < ospite else "X"),
            })
        righe.append({"casa": casa, "celle": celle})
    return righe


# Nomi leggibili dei mercati. I template non devono conoscere le chiavi del
# database, e "doppia_chance" in pagina non lo capirebbe nessuno.
ETICHETTE_MERCATI = {
    "1x2": "Esito finale (1X2)",
    "doppia_chance": "Doppia chance",
    "over_under": "Over/Under",
    "btts": "Gol / NoGol",
    "clean_sheet": "Porta inviolata",
    "risultato_esatto": "Risultato esatto",
}


def righe_accuratezza(accuratezza: dict) -> list[dict]:
    """L'accuratezza misurata, pronta da disegnare — verdetto compreso.

    Il verdetto e' la parte che conta: un Brier da solo non dice niente a chi
    legge. Accanto sta sempre il confronto con la baseline (dire ogni volta la
    frequenza media di quell'esito) e, quando i dati sono pochi, il fatto che
    siano pochi. Nel backtest l'1X2 batte le baseline mentre Over 2.5 e
    Gol/NoGol fanno leggermente peggio: nascondere lo stesso esito qui
    trasformerebbe un limite noto del modello in una bugia.
    """
    righe = []
    for voce in accuratezza.get("mercati", []):
        if not voce["significativo"]:
            verdetto, tono = "troppi pochi dati", "attesa"
        elif voce["batte_baseline"]:
            verdetto, tono = "meglio della baseline", "ok"
        else:
            verdetto, tono = "peggio della baseline", "no"

        righe.append({
            "mercato": ETICHETTE_MERCATI.get(voce["mercato"], voce["mercato"]),
            "n": voce["n"],
            "brier": voce["brier"],
            "baseline": voce["baseline"],
            "tasso_reale": voce["tasso_reale"],
            "probabilita_media": voce["probabilita_media"],
            "verdetto": verdetto,
            "tono": tono,
        })
    return righe


def righe_confronto_mercato(accuratezza: dict) -> list[dict]:
    """Modello contro quote di chiusura, mercato per mercato.

    Le quote sono il metro di paragone vero: chiunque batta "il caso" non ha
    dimostrato niente. Qui il verdetto e' volutamente severo — pareggiare col
    mercato e' gia' un buon risultato, e va detto cosi'.
    """
    confronto = accuratezza.get("mercato") or {}
    righe = []
    for voce in confronto.get("mercati", []):
        if not voce["significativo"]:
            verdetto, tono = "troppi pochi dati", "attesa"
        elif voce["batte_il_mercato"]:
            verdetto, tono = "meglio del mercato", "ok"
        else:
            verdetto, tono = "il mercato fa meglio", "no"

        righe.append({
            "mercato": ETICHETTE_MERCATI.get(voce["mercato"], voce["mercato"]),
            "n": voce["n"],
            "brier_modello": voce["brier_modello"],
            "brier_mercato": voce["brier_mercato"],
            "scarto": voce["scarto"],
            "verdetto": verdetto,
            "tono": tono,
        })
    return righe


# ---- giocatori ---------------------------------------------------------------
#
# Le prestazioni arrivano dai dettagli delle partite e non sanno nulla del
# contesto: chi era l'avversario, com'e' finita. Quel contesto sta nelle
# partite, e qui le due cose si rimettono insieme.


@dataclass
class RigaPrestazione:
    """Una partita nella scheda di un giocatore."""

    fixture_id: int
    quando: str
    avversario: str
    avversario_id: int
    in_casa: bool
    risultato: str
    minuti: int
    titolare: bool
    gol: int
    assist: int
    gialli: int
    rossi: int
    voto: float | None


def riga_prestazione(prestazione, fixture: FixtureRecord | None, nome) -> RigaPrestazione:
    """Una prestazione piu' il contesto della partita in cui e' avvenuta."""
    avversario_id = 0
    in_casa = False
    risultato = ""
    if fixture is not None:
        in_casa = fixture.home_team_id == prestazione.team_id
        avversario_id = fixture.away_team_id if in_casa else fixture.home_team_id
        if fixture.home_goals is not None:
            risultato = f"{fixture.home_goals}-{fixture.away_goals}"

    return RigaPrestazione(
        fixture_id=prestazione.fixture_id,
        quando=formatta_giorno(prestazione.quando),
        avversario=nome(avversario_id) if avversario_id else "—",
        avversario_id=avversario_id,
        in_casa=in_casa,
        risultato=risultato,
        minuti=prestazione.minuti,
        titolare=prestazione.titolare,
        gol=prestazione.gol,
        assist=prestazione.assist,
        gialli=prestazione.gialli,
        rossi=prestazione.rossi,
        voto=prestazione.voto,
    )


def righe_prestazioni(scheda, per_id: dict[int, FixtureRecord], nome) -> list[RigaPrestazione]:
    return [
        riga_prestazione(p, per_id.get(p.fixture_id), nome) for p in scheda.prestazioni
    ]


def riga_giocatore(scheda) -> dict:
    """Un giocatore in elenco: rosa di una squadra o classifica."""
    squadra = scheda.squadra_attuale
    return {
        "id": scheda.player_id,
        "nome": scheda.nome,
        "ruolo": scheda.ruolo,
        "squadra": squadra[1] if squadra else "—",
        "squadra_id": squadra[0] if squadra else 0,
        "presenze": scheda.presenze,
        "da_titolare": scheda.da_titolare,
        "minuti": scheda.minuti,
        "gol": scheda.gol,
        "assist": scheda.assist,
        "gialli": scheda.gialli,
        "rossi": scheda.rossi,
        "voto_medio": scheda.voto_medio,
        "partite_con_voto": scheda.partite_con_voto,
    }


def blocchi_formazione(formazioni, prestazioni) -> list[dict]:
    """Le formazioni con accanto, dove esiste, cosa ha fatto ogni giocatore.

    Le due sorgenti restano distinte anche qui: la formazione dice chi c'era,
    le statistiche dicono come e' andata. Se le seconde mancano — succede, e
    non e' un guasto — restano i nomi, che e' comunque piu' di quanto la scheda
    partita mostrasse prima.
    """
    per_giocatore = {p.player_id: p for p in prestazioni}

    def voce(riga) -> dict:
        prestazione = per_giocatore.get(riga.player_id)
        return {
            "id": riga.player_id,
            "nome": riga.nome,
            "numero": riga.numero,
            "ruolo": riga.ruolo,
            "minuti": prestazione.minuti if prestazione else None,
            "gol": prestazione.gol if prestazione else 0,
            "assist": prestazione.assist if prestazione else 0,
            "gialli": prestazione.gialli if prestazione else 0,
            "rossi": prestazione.rossi if prestazione else 0,
            "voto": prestazione.voto if prestazione else None,
            "sceso_in_campo": prestazione is not None,
        }

    return [
        {
            "squadra": f.squadra,
            "squadra_id": f.team_id,
            "modulo": f.modulo,
            "allenatore": f.allenatore,
            "titolari": [voce(r) for r in f.titolari],
            "panchina": [voce(r) for r in f.panchina],
        }
        for f in formazioni
    ]


def righe_gol(gol, casa_id: int) -> list[dict]:
    """I gol di una partita, gia' schierati sul lato giusto."""
    return [
        {
            "minuto": g.minuto,
            "marcatore": g.marcatore or "—",
            "marcatore_id": g.marcatore_id,
            "assist": g.assist,
            "assist_id": g.assist_id,
            "squadra": g.squadra,
            "in_casa": g.team_id == casa_id,
            "nota": "rigore" if g.su_rigore else ("autogol" if g.autogol else None),
        }
        for g in gol
    ]
