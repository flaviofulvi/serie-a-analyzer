"""La classifica del campionato: punti, non forze stimate.

E' la cosa piu' semplice del progetto e l'unica che il sito non sapeva dire.
L'hub mostrava le **forze del modello** — chi e' forte — e non la classifica —
chi ha fatto punti. Sono due domande diverse, e la seconda e' quella che si fa
per prima aprendo un sito di calcio. Le due insieme dicono una terza cosa che
nessuna delle due dice da sola: chi il modello considera sopra o sotto il posto
che occupa davvero.

Non costa niente: i risultati sono gia' tutti sul disco, pagati dalla raccolta
giornaliera.

**La trappola sta nella stagione.** L'archivio contiene quattro campionati piu'
quello in corso, e sommarli darebbe una squadra con 300 punti: la classifica
si calcola sempre dentro **una** stagione. E' la stessa trappola gia' evitata
in `players.py` per la classifica dei marcatori, dove un totale su quattro
stagioni sarebbe stato "un numero vero al posto sbagliato".

**Il secondo punto delicato sono le parita'.** La Serie A non le rompe con la
differenza reti ma con gli **scontri diretti**, e solo quando le squadre in
parita' hanno completato fra loro tutte le partite: a stagione in corso capita
di continuo che non sia cosi'. Qui si applica la regola vera quando e'
applicabile e si ripiega sulla differenza reti quando non lo e' — e la riga
dice **quale delle due** ha deciso, perche' un ordine che nessuno sa spiegare
e' peggio di un ordine dichiarato.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# Tre punti a vittoria: la Serie A ci e' passata nel 1994/95, e tutte le
# stagioni che questo progetto vedra' sono successive.
PUNTI_VITTORIA = 3
PUNTI_PAREGGIO = 1


@dataclass
class RigaClassifica:
    """Una squadra nella classifica, coi conti che la compongono."""

    squadra_id: int
    squadra: str
    giocate: int = 0
    vinte: int = 0
    pari: int = 0
    perse: int = 0
    fatti: int = 0
    subiti: int = 0
    # Come si sono rotte le parita' che riguardano questa squadra: None se non
    # era in parita' con nessuno. Serve alla pagina per dichiararlo.
    criterio: str | None = None
    posizione: int = 0

    @property
    def punti(self) -> int:
        return self.vinte * PUNTI_VITTORIA + self.pari * PUNTI_PAREGGIO

    @property
    def differenza(self) -> int:
        return self.fatti - self.subiti


def _aggiungi(riga: RigaClassifica, fatti: int, subiti: int) -> None:
    riga.giocate += 1
    riga.fatti += fatti
    riga.subiti += subiti
    if fatti > subiti:
        riga.vinte += 1
    elif fatti == subiti:
        riga.pari += 1
    else:
        riga.perse += 1


def _scontri_diretti(
    squadre: Sequence[int], partite: Sequence[Any]
) -> dict[int, RigaClassifica] | None:
    """La classifica avulsa fra un gruppo di squadre in parita'.

    Torna ``None`` quando il gruppo **non ha finito** di giocarsi fra se': la
    Serie A applica gli scontri diretti solo a confronti completati, e a
    stagione in corso l'andata e ritorno fra due squadre spesso non c'e'
    ancora. Applicarla lo stesso darebbe un ordine deciso da mezza
    informazione, che e' peggio della differenza reti.
    """
    fra_loro = [
        f for f in partite
        if f.home_team_id in squadre and f.away_team_id in squadre
    ]
    # Ogni coppia deve essersi affrontata due volte: n squadre, n*(n-1) partite.
    attese = len(squadre) * (len(squadre) - 1)
    if len(fra_loro) != attese:
        return None

    mini = {sq: RigaClassifica(squadra_id=sq, squadra="") for sq in squadre}
    for f in fra_loro:
        _aggiungi(mini[f.home_team_id], f.home_goals, f.away_goals)
        _aggiungi(mini[f.away_team_id], f.away_goals, f.home_goals)
    return mini


def classifica(
    fixtures: Iterable[Any],
    stagione: int,
    nome=None,
) -> list[RigaClassifica]:
    """La classifica di **una** stagione, dalle partite concluse.

    Args:
        fixtures: tutte le partite conosciute. Vengono filtrate qui: passare
            l'archivio intero e' il caso normale, e la stagione e' il filtro.
        stagione: l'anno che identifica il campionato (la 2026/27 e' ``2026``).
        nome: come si chiama una squadra dato il suo id. Se manca si usa il
            nome scritto nella partita, che va sempre bene tranne quando un
            club e' stato rinominato a stagione in corso.

    Returns:
        Le righe ordinate, con ``posizione`` gia' scritta dentro. Vuoto se di
        quella stagione non risulta giocata nemmeno una partita — e va
        mostrato come un vuoto, non come venti squadre a zero punti: prima
        della prima giornata la classifica non esiste, non e' piatta.
    """
    concluse = [
        f for f in fixtures
        if f.season == stagione and f.is_finished
    ]
    if not concluse:
        return []

    righe: dict[int, RigaClassifica] = {}

    def riga_di(team_id: int, team_name: str) -> RigaClassifica:
        if team_id not in righe:
            righe[team_id] = RigaClassifica(
                squadra_id=team_id,
                squadra=nome(team_id) if nome else team_name,
            )
        return righe[team_id]

    for f in concluse:
        casa = riga_di(f.home_team_id, f.home_team_name)
        ospite = riga_di(f.away_team_id, f.away_team_name)
        _aggiungi(casa, f.home_goals, f.away_goals)
        _aggiungi(ospite, f.away_goals, f.home_goals)

    return _ordina(list(righe.values()), concluse)


def _ordina(
    righe: list[RigaClassifica], partite: Sequence[Any]
) -> list[RigaClassifica]:
    """Ordina per punti, rompendo le parita' come le rompe la Serie A.

    Per primo vengono i punti. A parita' di punti si guardano gli **scontri
    diretti** fra le sole squadre in parita' (punti, poi differenza reti, poi
    gol fatti in quel mini-campionato); se quelle squadre non hanno ancora
    completato i confronti fra loro si passa alla **differenza reti** generale
    e poi ai gol fatti.

    L'ultimo criterio e' il nome, e non e' una regola del calcio: e' il modo di
    non far ballare due righe identiche da un caricamento all'altro.
    """
    ordinate: list[RigaClassifica] = []

    per_punti: dict[int, list[RigaClassifica]] = {}
    for riga in righe:
        per_punti.setdefault(riga.punti, []).append(riga)

    for punti in sorted(per_punti, reverse=True):
        gruppo = per_punti[punti]
        if len(gruppo) == 1:
            ordinate.extend(gruppo)
            continue

        mini = _scontri_diretti([r.squadra_id for r in gruppo], partite)
        if mini is not None:
            for riga in gruppo:
                riga.criterio = "scontri diretti"
            gruppo.sort(key=lambda r: (
                -mini[r.squadra_id].punti,
                -mini[r.squadra_id].differenza,
                -mini[r.squadra_id].fatti,
                -r.differenza,
                -r.fatti,
                r.squadra,
            ))
        else:
            for riga in gruppo:
                riga.criterio = "differenza reti"
            gruppo.sort(key=lambda r: (-r.differenza, -r.fatti, r.squadra))
        ordinate.extend(gruppo)

    for posizione, riga in enumerate(ordinate, start=1):
        riga.posizione = posizione
    return ordinate
