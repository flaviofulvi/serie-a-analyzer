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
