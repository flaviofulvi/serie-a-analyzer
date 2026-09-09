"""Trasformazione dei dati del dominio in oggetti pronti per le pagine.

I template non devono fare calcoli. Qui una partita diventa un blocco di
etichette e percentuali gia' formattate, e chi scrive l'HTML si occupa solo di
dove metterle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..data.indisponibili import ASSENTE, traduci_motivo
from ..data.outcomes import SOGLIA_SIGNIFICATIVITA
from ..data.panchine import Panchine
from ..data.parsers import FixtureRecord
from ..model.dixon_coles import TeamEvidence
from ..model.markets import MatchProbabilities

FUSO_ITALIA = ZoneInfo("Europe/Rome")

_GIORNI = ("lunedi'", "martedi'", "mercoledi'", "giovedi'", "venerdi'", "sabato", "domenica")
_GIORNI_BREVI = ("lun", "mar", "mer", "gio", "ven", "sab", "dom")
_MESI = ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic")


def ora_italiana(quando: datetime) -> datetime:
    return quando.astimezone(FUSO_ITALIA)


def formatta_data(quando: datetime) -> str:
    """'sabato 22 ago, 18:30' — in ora italiana, non UTC."""
    locale = ora_italiana(quando)
    return f"{_GIORNI[locale.weekday()]} {locale.day} {_MESI[locale.month - 1]}, {locale:%H:%M}"


def formatta_data_breve(quando: datetime) -> str:
    """'sab 22' — la stessa data, per uno schermo da telefono.

    Nasce da una misura fatta a 414px, che e' come il sito viene guardato
    davvero: nella tabella degli ultimi risultati "domenica 23 ago, 20:45" si
    mangiava il 40% della riga e spingeva fuori schermo le probabilita' 1-X-2
    e il verdetto, cioe' il motivo per cui quella tabella esiste. Il problema
    non era lo scorrimento ma quale colonna se lo meritasse.

    L'ora sparisce e non e' una perdita: su una partita gia' giocata dice
    poco, e chi la vuole la trova nella scheda partita. Il giorno della
    settimana resta perche' e' quello che orienta ("ah, sabato"), il numero
    perche' distingue due sabati.
    """
    locale = ora_italiana(quando)
    return f"{_GIORNI_BREVI[locale.weekday()]} {locale.day}"


def formatta_giorno(quando: datetime) -> str:
    locale = ora_italiana(quando)
    return f"{locale.day} {_MESI[locale.month - 1]} {locale.year}"


def distanza_breve(quanto: timedelta) -> str | None:
    """"3 g", "18 h", "25 min": quanto tempo, alla scala grossa.

    Serve a dire di quanto un pronostico ha preceduto il fischio d'inizio, ed
    e' il genere di numero che va letto a colpo d'occhio e mai al secondo.
    Un tempo negativo restituisce ``None``: il registro non accetta pronostici
    su partite gia' cominciate, quindi non e' un caso da formattare ma un
    dato che non torna, e va mostrato come assente invece che come "-3 h".
    """
    minuti = int(quanto.total_seconds() // 60)
    if minuti < 0:
        return None
    if minuti < 90:
        return f"{minuti} min"
    ore = minuti // 60
    if ore < 48:
        return f"{ore} h"
    return f"{ore // 24} g"


def percentuali_intere(*quote: float) -> tuple[int, ...]:
    """Percentuali arrotondate che sommano esattamente a 100.

    Arrotondare tre probabilita' una per una e' il modo piu' semplice di
    scrivere «45% 27% 27%» sotto un titolo che promette una partizione: fa
    99. Non e' un caso raro — basta che i tre resti stiano tutti sotto la
    meta' — e chi legge non ha modo di sapere se manchi un punto o se il
    conto sia sbagliato.

    Il metodo e' quello dei resti piu' grandi: si tiene la parte intera di
    ognuno e i punti che avanzano vanno a chi aveva il resto maggiore. Le
    **barre** restano disegnate sui valori veri: qui si aggiusta soltanto
    l'etichetta, che e' l'unica cosa che deve tornare a occhio.

    Serve un gruppo esaustivo per avere senso, e infatti si usa solo sul
    trittico 1X2. Su Over/Under, dove le linee non partizionano niente, non
    va usata: normalizzerebbe numeri che non sono parti di un tutto.
    """
    totale = sum(quote)
    if totale <= 0:
        return tuple(0 for _ in quote)
    esatte = [q / totale * 100 for q in quote]
    interi = [int(v) for v in esatte]
    avanzo = 100 - sum(interi)
    ordine = sorted(
        range(len(esatte)), key=lambda i: esatte[i] - interi[i], reverse=True
    )
    for i in ordine[:avanzo]:
        interi[i] += 1
    return tuple(interi)


def esito_gol(gol_casa: int, gol_ospite: int) -> str:
    """'1', 'X' o '2' a partire dai gol. Il segno, non il giudizio."""
    if gol_casa > gol_ospite:
        return "1"
    if gol_casa < gol_ospite:
        return "2"
    return "X"


def margine_gol(gol_casa: int, gol_ospite: int) -> float:
    """Quanto e' netto un risultato, da 0 a 1.

    E' l'intensita' con cui le pagine tingono il punteggio: uno scarto di
    tre gol o piu' e' colore pieno, un pareggio e' spento. Sta qui, in un
    posto solo, perche' lo stesso 2-0 deve avere lo stesso colore
    nell'elenco dell'hub, nella scheda squadra e in quella del giocatore.
    """
    return min(abs(gol_casa - gol_ospite), 3) / 3


# Da quanto in su un risultato esatto merita di essere nominato in pagina.
#
# Nasce da una domanda dell'utente — "il modello dice sempre 1-0 o 1-1, si
# spinge mai oltre?" — e la risposta misurata e' che il piu' probabile vale
# fra l'11.9% e il 16.5%, mentre i primi cinque insieme fanno circa il 53%.
# Nominarne uno solo racconta quindi un ottavo della partita e nasconde che
# spesso il secondo vale quasi quanto il primo. Al 10% si tiene chi e' davvero
# in gara: sotto, i risultati diventano tanti e tutti equivalenti, e un elenco
# lungo si legge come "il modello non sa", che e' il contrario di quello che
# sta succedendo.
#
# Non e' una soglia sulla certezza ed e' bene che nessuno la legga cosi': due
# risultati sopra il 10% non sono due pronostici, sono due celle vicine della
# stessa matrice.
#
# Dal 3 settembre 2026 questo numero decide DUE cose e non piu' una: quali
# punteggi si leggono prima della partita e quali valgono "azzeccato" dopo
# (vedi ``risultato_azzeccato``). E' voluto che sia lo stesso: promettere una
# rosa di punteggi e poi giudicarne uno solo sarebbe cambiare la domanda a
# partita finita. Chi lo tocca sposta anche il verdetto su tutto lo storico.
SOGLIA_RISULTATO_PROBABILE = 0.10


# Quanto devono distaccarsi le due squadre perche' l'esito a logica smetta di
# nominare il pareggio (lavoro 9, concordato il 9 settembre 2026). E' il
# distacco fra le DUE SQUADRE, |P(1) - P(2)|, col pareggio fuori dal conto:
# non e' una delle due, e' cio' che si aggiunge quando le due non si
# distinguono.
#
# I dieci punti non sono un gusto, vengono dal difetto P1: rigiocate quattro
# stagioni, sotto i 10 punti di distacco il modello dichiara la X al 27.9%
# mentre nella realta' esce il 34.8% delle volte (244 partite, 2.3 errori
# standard), e sopra i 10 lo scarto sparisce (+1.6) per poi cambiare segno.
# La soglia sta dove sta il difetto.
#
# Chi la sposta sappia in che direzione sbaglia. Sotto i 5 punti scatta
# sull'11% delle partite e lascia fuori gare in cui il difetto c'e' ancora
# tutto; sopra i 15 arriva al 31% e comincia a comprendere gare dove lo
# scarto e' gia' negativo, cioe' dove nominare la X e' peggio che tacerla.
# A 10 punti scatta sul 20%, che e' la proprieta' che serve: una regola che
# scattasse su meta' delle partite non starebbe scegliendo niente.
SOGLIA_ESITO_LOGICO = 0.10


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

    # La stessa data in forma corta, per gli schermi stretti. Viaggia insieme
    # a `quando` invece di sostituirla perche' le due servono a due larghezze
    # diverse e la scelta la fa il CSS, non il server: una pagina che decide
    # in base a un "sei da telefono?" indovinato lato server sbaglierebbe
    # ogni volta che qualcuno gira lo schermo.
    quando_breve: str = ""

    # Presenti solo se il modello conosce entrambe le squadre.
    prob: MatchProbabilities | None = None
    gol_attesi_casa: float | None = None
    gol_attesi_ospite: float | None = None

    # Da dove viene il pronostico qui sopra. Su una partita gia' cominciata
    # deve venire dal registro, mai da un ricalcolo: il modello caldo e' stato
    # ristimato includendo quel risultato. Chi costruisce la riga e' `_righe`
    # in web/app.py; qui si conserva solo per poterlo dire in pagina.
    iniziata: bool = False
    congelato: bool = False
    quando_congelato: str | None = None

    @property
    def ha_pronostico(self) -> bool:
        return self.prob is not None

    @property
    def pronostico_perduto(self) -> bool:
        """Partita gia' cominciata e nessun pronostico congelato prima del via.

        Va tenuto distinto dal "il modello non conosce le squadre": sono due
        assenze diverse e dirle uguali sarebbe sbagliato. Questa significa che
        nessuno era li' a guardare in tempo — di solito perche' la partita e'
        anteriore al registro — e non c'e' modo di rimediarla adesso.
        """
        return self.iniziata and self.prob is None

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
        return esito_gol(self.gol_casa, self.gol_ospite)

    @property
    def esito_previsto(self) -> str | None:
        """L'esito che il modello sceglie fra 1, X e 2: il piu' probabile.

        Lo leggono in tre — il velo sul favorito nella scheda squadra, il
        verdetto di fine partita e, dal 5 settembre 2026, la scritta accanto
        ai punteggi probabili — e devono leggere la STESSA cosa. Un secondo
        argmax scritto altrove sarebbe identico oggi e divergerebbe al primo
        ritocco, con la pagina che nomina un esito e la pastiglia che ne
        giudica un altro: e' la trappola gia' vista coi cinque punteggi.

        La "X" non esce quasi mai, e non e' un guasto da cercare. Perche'
        esca deve essere il pareggio il piu' probabile dei tre, e nei 1154
        pronostici in registro il pareggio non ha mai passato il 33.5% mentre
        il prescelto sta in mediana al 49%: zero volte su 1154 (misurato il 5
        settembre 2026). E' una proprieta' del calcio prima che del modello —
        il pareggio e' l'esito piu' frequente di nessuno — e chi la incontra
        deve poterla riconoscere invece di aprire il codice.
        """
        if self.prob is None:
            return None
        trio = {"1": self.prob.home_win, "X": self.prob.draw, "2": self.prob.away_win}
        return max(trio, key=trio.get)

    @property
    def probabilita_esito_previsto(self) -> float | None:
        """Quanto vale l'esito scelto qui sopra, da 0 a 1.

        Viaggia sempre insieme alla lettera e non e' un ornamento. Il
        prescelto ha una mediana del 49% e in una partita su sette sta sotto
        il 40% (stessa misura, 5 settembre 2026): scrivere "1" da solo
        direbbe che il modello ha deciso, mentre il piu' delle volte sta
        indicando il meno incerto dei tre. E' la regola gia' applicata ai
        punteggi probabili, che sono piu' d'uno proprio per non far sembrare
        deciso un modello che deciso non e'.
        """
        if self.prob is None:
            return None
        return max(self.prob.home_win, self.prob.draw, self.prob.away_win)

    @property
    def gol_nogol_previsto(self) -> str | None:
        """«GG» o «NG»: il modello se l'aspetta con gol da entrambe, o no.

        E' l'equivalente di ``esito_previsto`` sul Gol/NoGol, e sta accanto a
        quello in pagina su richiesta dell'utente (7 settembre 2026). Non e'
        un mercato nuovo: e' la stessa ``prob.btts`` che la scheda partita
        gia' mostrava, detta come scelta invece che come probabilita' secca.

        **Va letto sapendo tre cose, e la pagina le dichiara.** La prima e'
        che qui il modello non sceglie granche': misurato sui 1170 pronostici
        in registro il 7 settembre 2026, dice **NG 1071 volte su 1170** e la
        P(GG) non ha mai passato il 55.2%, con mediana 47.6%. La seconda e'
        aritmetica e vale sempre: GG e NG sommano a uno, quindi la
        percentuale del prescelto **non puo' scendere sotto il 50%** — la sua
        mediana e' 52.4% e in tre casi su quattro sta sotto il 55%. E' lo
        stesso pavimento che ha fatto scartare la doppia chance, un piano
        piu' in basso: un numero che parte da meta' sembra una decisione e
        non lo e'. La terza e' il limite noto del modello, gia' scritto nella
        scheda partita: sui mercati dei gol, dopo la compressione, dichiara
        quasi sempre la media di campionato, perche' e' quanto sa davvero.

        Il pareggio fra i due va a GG, e non capita: servirebbe una P(GG) di
        esattamente 0.5.
        """
        if self.prob is None:
            return None
        return "GG" if self.prob.btts >= self.prob.no_btts else "NG"

    @property
    def probabilita_gol_nogol_previsto(self) -> float | None:
        """Quanto vale la scelta qui sopra, da 0 a 1. Mai sotto 0.5.

        Viaggia sempre insieme alla sigla, per la stessa ragione per cui la
        percentuale sta accanto all'1X2: «NG» da solo si legge come una
        decisione. Qui serve anche di piu', perche' il pavimento e' il doppio
        — vedi ``gol_nogol_previsto``.
        """
        if self.prob is None:
            return None
        return max(self.prob.btts, self.prob.no_btts)

    @property
    def esito_logico(self) -> str | None:
        """L'esito matematico detto a voce umana: «1», «2», «1X» o «X2».

        Concordato con l'utente il 9 settembre 2026 (lavoro 9). Guarda una
        cosa sola, il distacco fra le **due squadre**: se sono lontane nomina
        la favorita e basta, se sono vicine le aggiunge il pareggio.

        **Non e' un mercato, ed e' la regola che lo tiene in piedi.** Non ha
        un Brier, non entra nel registro, non lo risolve ``outcomes.py``, non
        compare nell'accuratezza ne' nella calibrazione. E' un calcolo che
        avviene DOPO il modello e come conseguenza di cio' che il modello ha
        detto: se un giorno il modello cambia idea, cambia idea anche questo,
        senza che nessuno lo ritocchi. A migliorare ci pensa l'esito
        matematico; questo lo racconta.

        Il divieto non e' pedanteria. Un secondo esito in pagina ha l'aspetto
        di un secondo pronostico, e il giorno in cui qualcuno lo salvasse
        «per misurarlo» avremmo due risposte alla stessa domanda contate come
        due osservazioni: e' il difetto A1, che costo' un anno di misure
        sbagliate prima che qualcuno se ne accorgesse.

        Perche' non e' un vezzo, e qui sta la parte misurata: nelle partite in
        cui questa regola aggiunge il pareggio, il pareggio e' PIU' probabile
        di quanto il modello dichiari — 27.9% detto contro 34.8% accaduto, che
        e' il difetto P1. La riga non inventa niente, mette in parole una cosa
        che sappiamo e che il modello non sa ancora dire da solo. Il giorno in
        cui P1 verra' curato, ``esito_previsto`` dira' «X» per conto suo e
        questa smettera' di aggiungere qualcosa: ed e' il modo giusto in cui
        deve finire.

        La favorita e' sempre una delle due squadre, mai il pareggio, e non
        e' una svista: ``esito_previsto`` puo' valere «X» in teoria, ma qui la
        domanda e' «chi delle due», e il pareggio e' cio' che si aggiunge a
        quella risposta. Nei 1154 pronostici in registro la X non e' comunque
        mai stata la piu' probabile, nemmeno una volta.
        """
        if self.prob is None:
            return None
        favorita = "1" if self.prob.home_win >= self.prob.away_win else "2"
        distacco = abs(self.prob.home_win - self.prob.away_win)
        if distacco >= SOGLIA_ESITO_LOGICO:
            return favorita
        return "1X" if favorita == "1" else "X2"

    @property
    def distacco_fra_le_squadre(self) -> float | None:
        """Quanto si distaccano le due squadre, da 0 a 1, pareggio escluso.

        E' il numero su cui ``esito_logico`` decide, e la scheda partita lo
        scrive: una sigla che cambia senza dire in base a cosa si legge come
        un capriccio. Il pareggio non entra qui per la stessa ragione per cui
        non entra nella soglia — non e' una delle due squadre.
        """
        if self.prob is None:
            return None
        return abs(self.prob.home_win - self.prob.away_win)

    @property
    def esito_logico_col_pareggio(self) -> bool:
        """L'esito a logica ha aggiunto la X, cioe' le due squadre son vicine.

        Serve al template per dire *perche'* c'e' scritto «1X» invece di «1»,
        che e' l'unica cosa che quella sigla non sa spiegare da sola.
        """
        logico = self.esito_logico
        return logico is not None and len(logico) == 2

    @property
    def pronostico_azzeccato(self) -> bool | None:
        """L'esito accaduto era quello che il modello preferiva.

        Dal 7 settembre 2026 ha **una colonna sua**, accanto a quella del
        punteggio, e il perche' e' una partita sola: Juventus-Milan del 6
        settembre 2026 e' finita 1-1, con il modello che dava l'1 al 45% e
        nominava 1-0 e 1-1 fra i punteggi probabili. La pastiglia unica di
        allora guardava il punteggio per primo, trovava l'1-1 nella rosa e
        scriveva "azzeccato" in verde su una partita in cui il segno era
        sbagliato.

        Non era un difetto di quel codice ma della domanda che gli si faceva.
        Una pastiglia sola deve mettere i due giudizi in fila, e dal 3
        settembre 2026 in fila non stanno piu': i punteggi nominati sono fino
        a cinque e **possono avere segni diversi fra loro**, quindi "il
        punteggio era fra quelli detti" non implica piu' "il segno era
        quello". Due domande diverse vogliono due risposte diverse.

        Vale ``None`` finche' la partita non e' finita, o se il pronostico non
        fu scritto in tempo: "non lo so" e "sbagliato" non si disegnano
        uguale.
        """
        reale, previsto = self.esito_reale, self.esito_previsto
        if reale is None or previsto is None:
            return None
        return reale == previsto

    @property
    def margine(self) -> float:
        """Quanto e' netto il risultato, da 0 a 1."""
        if not self.conclusa or self.gol_casa is None:
            return 0.0
        return margine_gol(self.gol_casa, self.gol_ospite)

    @property
    def risultato_probabile(self) -> str:
        """Il singolo punteggio piu' probabile.

        **Non e' piu' il metro del verdetto**: dal 3 settembre 2026 la
        colonna del punteggio guarda tutti quelli nominati e non solo il primo
        (il perche' sta in ``risultato_azzeccato``). Il suo unico compito
        adesso e' fare da rete a ``punteggi_probabili`` quando nessuna cella
        arriva alla soglia: la pagina non resta mai senza un punteggio, che si
        leggerebbe come "il modello non sa dirlo".
        """
        if self.prob is None:
            return ""
        (casa, ospite), _ = self.prob.top_scorelines[0]
        return f"{casa}-{ospite}"

    @property
    def risultati_probabili(self) -> str:
        """I punteggi sopra la soglia, dal piu' probabile: "1-0/2-0".

        Serve a non far sembrare deciso un modello che deciso non e': su una
        partita equilibrata il primo vale il 12% e il secondo l'11%, e
        mostrarne uno solo suggerisce una certezza che quel numero non ha.

        **Non e' mai vuoto.** Se nessun punteggio arriva alla soglia si torna
        al piu' probabile da solo: uno spazio bianco si leggerebbe come "il
        modello non sa dirlo", che sarebbe falso — la matrice c'e' ed e'
        semplicemente piu' larga del solito. E' la regola di sempre, quella
        per cui "non lo so" e "non ce n'e' abbastanza" non si scrivono uguale.

        L'elenco arriva da ``top_scorelines``, che ne tiene cinque: sono
        cinque punteggi sopra il 10%, cioe' meta' della partita in cinque
        celle, e i gol attesi della Serie A non producono una matrice cosi'
        stretta. Il tetto c'e' ma non morde.
        """
        return "/".join(self.punteggi_probabili)

    @property
    def punteggi_probabili(self) -> list[str]:
        """Gli stessi punteggi, uno per elemento invece che in una stringa.

        Esiste perche' li leggono in due — l'anteprima della partita e il
        verdetto di quando e' finita — e devono leggere la STESSA cosa. Una
        seconda lista ricalcolata con la stessa soglia sarebbe uguale oggi e
        divergerebbe al primo ritocco, con la pagina che nomina cinque
        punteggi e la pastiglia che ne premia quattro.
        """
        if self.prob is None:
            return []
        sopra = [
            f"{casa}-{ospite}"
            for (casa, ospite), p in self.prob.top_scorelines
            if p > SOGLIA_RISULTATO_PROBABILE
        ]
        return sopra or [self.risultato_probabile]

    @property
    def piu_di_un_risultato_probabile(self) -> bool:
        """Se l'etichetta va messa al plurale.

        Il conto sta qui perche' i template non fanno calcoli; la parola sta
        nel template perche' e' testo che si legge in pagina, e il testo si
        cerca dove si scrive l'HTML. Passandola da qui verrebbe anche
        escapata, e "Piu&#39; probabili" nel sorgente non lo troverebbe piu'
        nessuno.
        """
        return len(self.punteggi_probabili) > 1

    @property
    def risultato_azzeccato(self) -> bool | None:
        """Il punteggio finito sul tabellone era fra quelli nominati in pagina.

        Fino al 3 settembre 2026 il confronto era col solo punteggio piu'
        probabile. Cambiato su richiesta dell'utente, e la ragione tiene:
        prima della partita la pagina non nomina un punteggio, ne nomina fino
        a cinque — tutti sopra il 10%, messi li' apposta per dire che il
        modello fra quelli non sta scegliendo. Giudicare poi sul solo primo
        vorrebbe dire cambiare la domanda a partita finita, e chiamare
        sbagliato un 2-0 che la pagina aveva scritto.

        La lista e' esattamente quella mostrata (``punteggi_probabili``), non
        una ricalcolata: pastiglia e anteprima non possono divergere.

        Due cose da tenere in mente, e nessuna delle due e' un guasto.
        Questa colonna e' **piu' facile** di un confronto col solo punteggio
        piu' probabile, quindi i verdi scritti prima del 3 settembre 2026 e
        quelli di dopo non si contano insieme. E questo non e' una misura del
        modello: le misure stanno in ``/pronostici``, nel Brier e nella
        calibrazione, e nessuna passa di qui — questa e' la riga che racconta
        com'e' andata a chi guarda.

        Puo' dire di si' mentre la colonna accanto dice di no, ed e' il caso
        che ha fatto nascere le due colonne: il perche' sta in
        ``pronostico_azzeccato``.
        """
        if self.prob is None or not self.conclusa or self.gol_casa is None:
            return None
        return self.risultato in self.punteggi_probabili

    @property
    def giudicabile(self) -> bool:
        """Se di questa riga si puo' dire com'e' andata.

        Le due palline in fondo rispondono a due domande diverse, ma nascono
        e spariscono insieme: nessuna delle due ha risposta finche' la partita
        non e' finita, e un cerchio rosso su una partita da giocare direbbe
        "sbagliato" di una cosa che nessuno ha ancora visto. E' il terzo stato
        della riga, quello che si continua a dire a parole.

        Sta qui e non nel template perche' i template non fanno calcoli, e
        perche' le due colonne devono comparire insieme: chiederlo due volte,
        una per colonna, e' il modo di ritrovarsene un giorno una sola.
        """
        return self.risultato_azzeccato is not None


def riga_partita(
    fixture: FixtureRecord,
    nome,
    prob: MatchProbabilities | None = None,
    gol_attesi: tuple[float, float] | None = None,
    iniziata: bool = False,
    congelato: bool = False,
    quando_congelato: str | None = None,
) -> RigaPartita:
    return RigaPartita(
        id=fixture.api_id,
        casa=nome(fixture.home_team_id),
        ospite=nome(fixture.away_team_id),
        casa_id=fixture.home_team_id,
        ospite_id=fixture.away_team_id,
        quando=formatta_data(fixture.kickoff_utc),
        quando_breve=formatta_data_breve(fixture.kickoff_utc),
        giornata=fixture.matchday,
        conclusa=fixture.is_finished,
        gol_casa=fixture.home_goals,
        gol_ospite=fixture.away_goals,
        prob=prob,
        gol_attesi_casa=gol_attesi[0] if gol_attesi else None,
        gol_attesi_ospite=gol_attesi[1] if gol_attesi else None,
        iniziata=iniziata,
        congelato=congelato,
        quando_congelato=quando_congelato,
    )


# ---- affidabilita' di un pronostico ------------------------------------------
#
# L'accuratezza in fondo all'hub dice quanto valgono i pronostici *gia'
# verificati*: e' la misura onesta, ma arriva mesi dopo e su decine di esiti.
# Qui si risponde alla domanda che uno si fa guardando una partita di domani —
# "di queste due squadre il modello sa qualcosa?" — e la risposta e' gia'
# calcolabile adesso, perche' non dipende da come andra' a finire.
#
# Le cose da dire sono **due**, e mostrarne una sola ingannerebbe piu' che
# tacere entrambe:
#
# 1. quanto la forza stimata viene dai dati della squadra invece che dalla
#    media di lega (``TeamEvidence.pull``);
# 2. quanto sono **vecchi** quei dati, misurati da oggi.
#
# Il punto 2 non e' contenuto nel punto 1: il decadimento temporale e' calcolato
# rispetto alla partita piu' recente del campione, non rispetto a oggi, quindi
# un archivio fermo da un anno risulta comunque "pieno di dati freschi". Sono
# freschi rispetto a se stessi. Un pronostico stimato su un campionato finito
# quindici mesi fa puo' avere il ``pull`` di una stima solida ed essere, di
# fatto, la fotografia di squadre che non esistono piu'.

# La soglia dell'invecchiamento e' l'emivita del decadimento: oltre quella, per
# costruzione del modello, ogni partita del campione pesa meno della meta' di
# quanto peserebbe una giocata oggi.
GIORNI_FRESCHI = 180

# Le soglie del giudizio, sul tiro verso la media di lega. Sono grossolane di
# proposito: un confine al punto percentuale suggerirebbe una precisione che
# un'approssimazione quadratica non ha.
_SOGLIE_AFFIDABILITA = (
    (0.15, "stima solida", "ok"),
    (0.30, "stima discreta", "ok"),
    (0.50, "stima debole", "attesa"),
)
_AFFIDABILITA_MINIMA = ("il modello sa poco", "attesa")


@dataclass
class Affidabilita:
    """Quanto il modello sa di una squadra, pronto da disegnare."""

    squadra: str
    squadra_id: int
    n_partite: int
    ultima: str
    giorni: int
    sostanza: float
    tiro: float
    verdetto: str
    tono: str

    @property
    def stantia(self) -> bool:
        """L'ultima partita nota e' piu' vecchia di un'emivita."""
        return self.giorni > GIORNI_FRESCHI


def affidabilita_squadra(
    evidenza: TeamEvidence | None,
    squadra: str,
    squadra_id: int,
    oggi: date,
) -> Affidabilita | None:
    """Una riga di affidabilita', o ``None`` se il modello non conosce la squadra.

    Il caso ``None`` non va riempito con uno zero: "non lo so" e "lo so male"
    sono cose diverse, e la scheda partita ha gia' il suo modo di dire la
    prima.

    Il verdetto guarda prima l'eta' dei dati e poi la loro quantita', in
    quest'ordine e non nell'altro: mille partite di due stagioni fa non fanno
    una stima solida, e chiamarla cosi' sarebbe il modo piu' efficace di
    nascondere il limite proprio nel posto costruito per dichiararlo.
    """
    if evidenza is None:
        return None

    giorni = (oggi - evidenza.last_match).days

    if giorni > GIORNI_FRESCHI:
        verdetto, tono = "dati vecchi", "attesa"
    else:
        verdetto, tono = _AFFIDABILITA_MINIMA
        for limite, testo, colore in _SOGLIE_AFFIDABILITA:
            if evidenza.pull < limite:
                verdetto, tono = testo, colore
                break

    return Affidabilita(
        squadra=squadra,
        squadra_id=squadra_id,
        n_partite=evidenza.n_matches,
        ultima=(
            f"{evidenza.last_match.day} {_MESI[evidenza.last_match.month - 1]} "
            f"{evidenza.last_match.year}"
        ),
        giorni=giorni,
        sostanza=evidenza.sostanza,
        tiro=evidenza.pull,
        verdetto=verdetto,
        tono=tono,
    )


def righe_affidabilita(
    evidenze: tuple[TeamEvidence | None, TeamEvidence | None],
    riga: RigaPartita,
    oggi: date | None = None,
) -> list[Affidabilita]:
    """Le due squadre di una partita, quando di entrambe c'e' qualcosa da dire.

    O tutte e due o niente: mostrarne una sola farebbe sembrare l'altra
    solidissima, mentre il motivo per cui manca e' l'opposto.
    """
    quando = oggi or date.today()
    righe = [
        affidabilita_squadra(evidenze[0], riga.casa, riga.casa_id, quando),
        affidabilita_squadra(evidenze[1], riga.ospite, riga.ospite_id, quando),
    ]
    if any(r is None for r in righe):
        return []
    return righe  # type: ignore[return-value]


@dataclass
class CambioPanchina:
    """Una squadra che ha cambiato allenatore di recente, pronta da disegnare.

    Non e' un giudizio sull'allenatore — quello il modello non lo sa dare, e
    non ci prova. E' un avviso su cosa c'e' dentro la stima: le partite di
    prima del cambio le ha giocate una squadra guidata da un altro, e il
    modello le conta comunque perche' pesa solo l'eta' di una partita.
    """

    squadra: str
    squadra_id: int
    allenatore: str
    dal: str
    giorni: int
    partite: int
    precedente: str | None
    # Se il modello ne tenga conto oppure no. Non e' un dettaglio di
    # presentazione: dire "ne tiene conto" quando il fattore e' spento sarebbe
    # la stessa bugia che la pagina evita sugli indisponibili.
    conta: bool


def cambio_panchina(
    panchine: Panchine | None,
    squadra: str,
    squadra_id: int,
    oggi: date,
    conta: bool,
) -> CambioPanchina | None:
    """Il cambio di allenatore di una squadra, se c'e' stato e se pesa ancora.

    Due condizioni, e servono tutte e due. **C'e' stato un cambio**: una
    squadra con un allenatore solo in archivio non ha niente da dichiarare.
    **E' recente**: oltre un'emivita le partite della gestione precedente
    pesano gia' quasi zero da sole, e dirlo sarebbe allarmare per un problema
    che il decadimento temporale ha gia' risolto.
    """
    if panchine is None:
        return None
    tratti = panchine.cronologia(squadra_id)
    if len(tratti) < 2:
        return None
    attuale = tratti[-1]
    giorni = (oggi - attuale.dal).days
    if giorni > GIORNI_FRESCHI:
        return None
    return CambioPanchina(
        squadra=squadra,
        squadra_id=squadra_id,
        allenatore=attuale.nome,
        dal=f"{attuale.dal.day} {_MESI[attuale.dal.month - 1]} {attuale.dal.year}",
        giorni=giorni,
        partite=attuale.partite,
        precedente=tratti[-2].nome,
        conta=conta,
    )


def righe_panchina(
    panchine: Panchine | None,
    riga: RigaPartita,
    conta: bool,
    oggi: date | None = None,
) -> list[CambioPanchina]:
    """Le squadre di questa partita che hanno cambiato allenatore di recente.

    Qui, al contrario dell'affidabilita', va bene mostrarne **una sola**: che
    il Milan abbia cambiato allenatore non dice niente di falso sull'Udinese.
    L'affidabilita' e' un confronto e va mostrata a coppie, questo e' un fatto
    su una squadra.
    """
    quando = oggi or date.today()
    righe = [
        cambio_panchina(panchine, riga.casa, riga.casa_id, quando, conta),
        cambio_panchina(panchine, riga.ospite, riga.ospite_id, quando, conta),
    ]
    return [r for r in righe if r is not None]


@dataclass(frozen=True)
class ConfrontoUfficiale:
    """I due pronostici sulla stessa partita, messi uno accanto all'altro.

    Sono il pronostico **secco** — scritto appena il modello ha visto la
    partita in calendario — e quello **rifatto quando sono uscite le
    formazioni ufficiali**, che pesa chi gioca davvero (`data/ufficiali.py`).

    Esistono tutti e due apposta, e questa e' la pagina in cui si vedono
    insieme: due pronostici sulla stessa partita sono l'unico modo di scoprire
    se rifarli serve. Il verdetto vero non e' qui ma nell'hub, dove i Brier di
    tutte le partite si confrontano; qui si vede **cosa** e' cambiato su
    questa, che e' la domanda che ci si fa guardandola.

    Il pronostico grande in cima alla pagina resta il **secco**, ed e' detto in
    chiaro nel template. Non perche' sia migliore — non si sa ancora, e' tutto
    il punto — ma perche' e' l'unico che esiste per ogni partita: se la testata
    cambiasse fonte a seconda di cosa c'e' sul disco, due partite della stessa
    giornata mostrerebbero due cose diverse senza dirlo.
    """

    esiti: list[dict]
    quando_secco: str | None
    quando_ufficiale: str | None
    gol_attesi_secco: tuple[float, float]
    gol_attesi_ufficiale: tuple[float, float]

    @property
    def spostamento_massimo(self) -> float:
        """Di quanto si e' mosso l'esito che si e' mosso di piu'.

        Serve al template per dire la cosa piu' utile in una riga sola: le
        formazioni hanno cambiato qualcosa, o hanno confermato? Un pronostico
        ufficiale viene scritto solo se sposta qualcosa (`vale_la_pena`), ma
        "qualcosa" puo' essere mezzo punto percentuale.
        """
        return max((abs(e["scarto"]) for e in self.esiti), default=0.0)


def confronto_ufficiale(secco, ufficiale) -> ConfrontoUfficiale | None:
    """Il confronto fra i due pronostici registrati, o ``None`` se non si puo'.

    Servono **entrambi**: mostrare l'ufficiale da solo lo farebbe sembrare il
    pronostico e basta, e sparirebbe proprio l'informazione per cui e' stato
    scritto — di quanto ha spostato le cose sapere chi gioca.

    Prende due ``Congelato`` (`web/engine.py`), cioe' due righe di registro:
    mai il modello caldo. Il secco ricalcolato adesso e l'ufficiale congelato
    ieri non sono confrontabili — uno dei due avrebbe visto piu' partite — e
    la differenza fra i due non direbbe piu' niente sulle formazioni.
    """
    if secco is None or ufficiale is None:
        return None

    coppie = (
        ("1", "home_win"),
        ("X", "draw"),
        ("2", "away_win"),
    )
    esiti = [
        {
            "esito": etichetta,
            "secco": getattr(secco.prob, campo),
            "ufficiale": getattr(ufficiale.prob, campo),
            "scarto": getattr(ufficiale.prob, campo) - getattr(secco.prob, campo),
        }
        for etichetta, campo in coppie
    ]
    return ConfrontoUfficiale(
        esiti=esiti,
        quando_secco=formatta_data(secco.quando) if secco.quando else None,
        quando_ufficiale=(
            formatta_data(ufficiale.quando) if ufficiale.quando else None
        ),
        gol_attesi_secco=secco.gol_attesi,
        gol_attesi_ufficiale=ufficiale.gol_attesi,
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
    siano pochi — misurati in **partite**, non in esiti, perche' le dieci
    linee Over/Under di una gara sono decise dallo stesso punteggio. Per
    questo la riga mostra tutti e due i numeri: "200 esiti" accanto a "troppi
    pochi dati" si leggerebbe come un guasto, "20 partite" no. Nel backtest l'1X2 batte le baseline mentre Over 2.5 e
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
            "partite": voce["partite"],
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
            "partite": voce["partite"],
            "brier_modello": voce["brier_modello"],
            "brier_mercato": voce["brier_mercato"],
            "scarto": voce["scarto"],
            "verdetto": verdetto,
            "tono": tono,
        })
    return righe


# Sotto questi esiti in una fascia, la frequenza reale e' un tiro di dadi:
# con otto pronostici al 30% ne bastano due in piu' o in meno per passare dal
# 12% al 50%. La riga si mostra lo stesso — sparirla nasconderebbe che quella
# fascia esiste — ma dichiarata come non ancora leggibile.
MINIMO_PER_FASCIA = 10


def righe_calibrazione(fasce: list[dict]) -> list[dict]:
    """Le fasce di calibrazione pronte da disegnare.

    Ogni riga dice due numeri e la loro distanza: quanto il modello dichiarava
    (``previsto``) e quante volte e' poi successo (``reale``). Lo ``scarto``
    ha un verso che conta piu' del suo valore assoluto: **negativo** vuol dire
    che il modello prometteva piu' di quanto la realta' abbia mantenuto, cioe'
    troppa sicurezza, ed e' il difetto che il Brier da solo non fa vedere.
    """
    righe = []
    for f in fasce:
        abbastanza = f["n"] >= MINIMO_PER_FASCIA
        righe.append({
            "fascia": f["fascia"],
            "n": f["n"],
            # Quante gare distinte hanno prodotto quegli esiti: sull'1X2 una
            # partita ne mette tre, e la riga deve poterlo dire.
            "partite": f.get("partite", 0),
            "previsto": f["previsto"],
            "reale": f["reale"],
            "scarto": f["reale"] - f["previsto"],
            "abbastanza": abbastanza,
            "tono": "ok" if abbastanza else "attesa",
        })
    return righe


def verdetto_calibrazione(fasce: list[dict], partite: int) -> dict | None:
    """Il riassunto onesto delle fasce: quanto sbaglia in media, e se conta.

    Lo scarto medio si prende in **valore assoluto**, e non e' un dettaglio.
    Sull'1X2 la media con segno vale zero quasi per costruzione: le tre
    probabilita' di una partita sommano a uno e un esito solo si avvera,
    quindi cio' che una fascia promette di troppo un'altra lo promette di
    meno. Un "in media ci prende" sarebbe vero e non direbbe niente: la
    calibrazione si legge fascia per fascia, e il numero che la riassume deve
    essere una distanza, non un saldo.

    ``partite`` e' un argomento e non una somma delle fasce, ed e' la
    correzione che questa funzione aspettava. La soglia del progetto si conta
    in **partite** e non in esiti (`SOGLIA_SIGNIFICATIVITA`), ma qui si
    guardava `n`: sull'1X2 ogni gara mette tre esiti in tre fasce diverse,
    quindi con venti partite in archivio il conto faceva sessanta, scavalcava
    la soglia, e l'hub dichiarava valido un numero che non lo era — proprio
    il difetto gia' corretto per l'accuratezza. Sommare le partite delle
    fasce non funzionerebbe: la stessa gara compare in piu' fasce e verrebbe
    contata due o tre volte. Il conto delle gare distinte lo sa solo chi ha
    letto gli esiti, e infatti arriva da li'.
    """
    if not fasce:
        return None
    n = sum(f["n"] for f in fasce)
    return {
        "n": n,
        "partite": partite,
        "scarto_medio": sum(
            abs(f["reale"] - f["previsto"]) * f["n"] for f in fasce
        ) / n,
        # La stessa soglia dell'accuratezza, e per la stessa ragione: sotto,
        # il numero e' rumore che ha l'aria di una misura.
        "abbastanza": partite >= SOGLIA_SIGNIFICATIVITA,
        "fasce_povere": sum(1 for f in fasce if f["n"] < MINIMO_PER_FASCIA),
    }


def righe_classifica(
    classifica: list, forze: list[tuple] | None = None
) -> list[dict]:
    """La classifica pronta da disegnare, col posto che il modello darebbe.

    La colonna in piu' e' la ragione per cui questa tabella vale la pena: da
    sola la classifica la sanno tutti, e il sito non aggiunge niente a
    guardarla. Accanto al **posto secondo le forze stimate** dice invece una
    cosa che nessun altro dice — chi sta sopra o sotto quello che il modello si
    aspetterebbe — e quella e' la lettura per cui uno apre questo sito e non un
    altro.

    Attenzione a cosa NON e' quella colonna: non e' una previsione di dove la
    squadra finira'. E' dove starebbe **oggi** se contasse la forza stimata
    invece dei punti fatti, e le due divergono anche solo per il calendario.

    Le forze arrivano da `FittedModel.strength_table()`, che copre tutte le
    squadre di tutte le stagioni in archivio: qui si tiene solo l'ordine
    relativo fra quelle del campionato in corso, altrimenti una squadra di
    Serie A risulterebbe quindicesima fra ventisette.
    """
    posto_modello: dict[str, int] = {}
    if forze:
        nomi_in_classifica = {r.squadra for r in classifica}
        solo_queste = [f for f in forze if f[0] in nomi_in_classifica]
        for posizione, voce in enumerate(solo_queste, start=1):
            posto_modello[voce[0]] = posizione

    righe = []
    for r in classifica:
        secondo_il_modello = posto_modello.get(r.squadra)
        righe.append({
            "posizione": r.posizione,
            "squadra": r.squadra,
            "squadra_id": r.squadra_id,
            "giocate": r.giocate,
            "vinte": r.vinte,
            "pari": r.pari,
            "perse": r.perse,
            "fatti": r.fatti,
            "subiti": r.subiti,
            "differenza": r.differenza,
            "punti": r.punti,
            "criterio": r.criterio,
            "posizione_modello": secondo_il_modello,
            # Positivo = il modello la mette piu' in alto di dov'e'.
            "scarto_modello": (
                r.posizione - secondo_il_modello
                if secondo_il_modello is not None else None
            ),
        })
    return righe


def righe_confronto_modi(confronto: dict | None) -> list[dict]:
    """Il pronostico secco contro quello rifatto con le formazioni ufficiali.

    E' il verdetto sul lavoro delle formazioni, ed e' l'unico posto in cui
    quel lavoro puo' morire: se dopo qualche mese il Brier non migliora, non
    e' un'opinione contro un'altra: e' il modello che dice che quel peso non
    c'era.

    Resta vuoto finche' non esiste nemmeno una partita con tutti e due i
    pronostici — cioe' oggi — e l'hub non mostra la sezione affatto: una
    tabella vuota si leggerebbe come "provato e non ha reso", che e' una cosa
    diversa da "non ancora provato".
    """
    righe = []
    for voce in (confronto or {}).get("mercati", []):
        if not voce["significativo"]:
            verdetto, tono = "troppi pochi dati", "attesa"
        elif voce["il_secondo_e_migliore"]:
            verdetto, tono = "le formazioni aiutano", "ok"
        else:
            verdetto, tono = "il secco fa meglio", "no"

        righe.append({
            "mercato": ETICHETTE_MERCATI.get(voce["mercato"], voce["mercato"]),
            "n": voce["n"],
            "partite": voce["partite"],
            "brier_secco": voce["brier_primo"],
            "brier_ufficiale": voce["brier_secondo"],
            "scarto": voce["scarto"],
            "verdetto": verdetto,
            "tono": tono,
        })
    return righe


# ---- il registro dei pronostici ----------------------------------------------
#
# La pagina che rende visibile la promessa del progetto: i pronostici sono
# immutabili e si giudicano dopo. Le righe arrivano dal registro — sempre e
# solo da li', anche per le partite che devono ancora cominciare — e il
# contesto (chi giocava, com'e' finita) dal dataset, come per le prestazioni
# dei giocatori qui sotto.

# Quante righe mostrare al massimo, e — la parte che conta davvero — come
# dividerle fra le due meta' della pagina.
#
# Il tetto da solo non basta, ed e' il difetto con cui e' nato: le righe
# venivano ordinate per calcio d'inizio decrescente, e da quando l'archivio
# contiene il **calendario intero** le piu' "recenti" sono quelle di maggio.
# Con 1150 righe su 380 partite, le prime duecento coprivano settanta gare
# del 2027, tutte ancora da giocare: la pagina annunciava «N gia' verificati»,
# spiegava per quattro righe come si legge il Brier, e quella colonna era
# «—» su ognuna delle righe mostrate. Le partite misurate — le uniche su cui
# la promessa ha gia' una risposta — non erano raggiungibili in nessun modo.
#
# La pagina ha due meta' dichiarate, e adesso ognuna ha la sua quota: le
# promesse ancora aperte, dalla piu' vicina al fischio d'inizio, e quelle
# gia' giudicate, dalla piu' recente. Nessuna delle due puo' mangiarsi
# l'altra, per quanto lungo diventi il calendario in archivio.
LIMITE_REGISTRO = 200
LIMITE_IN_ATTESA = 60


@dataclass(frozen=True)
class RigaRegistro:
    """Un pronostico com'e' rimasto scritto, e com'e' andata a finire."""

    fixture_id: int
    casa: str
    ospite: str
    quando: str
    giornata: int | None
    modo: str
    scritto: str | None
    anticipo: str | None
    prob_casa: float
    prob_pari: float
    prob_ospite: float
    conclusa: bool
    gol_casa: int | None
    gol_ospite: int | None
    brier: float | None
    # La partita esiste ancora nell'archivio? Se no, di lei non si sa nemmeno
    # se sia stata giocata, e "da giocare" sarebbe un'affermazione inventata.
    in_archivio: bool
    # A che punto e' il giro: "attesa" (si deve ancora giocare), "aperto"
    # (giocata, esito non ancora scritto nel registro) o "chiuso".
    stato: str

    @property
    def risultato(self) -> str:
        if not self.conclusa or self.gol_casa is None:
            return ""
        return f"{self.gol_casa}-{self.gol_ospite}"

    @property
    def esito_reale(self) -> str | None:
        if not self.conclusa or self.gol_casa is None:
            return None
        return esito_gol(self.gol_casa, self.gol_ospite)

    @property
    def margine(self) -> float:
        if not self.conclusa or self.gol_casa is None:
            return 0.0
        return margine_gol(self.gol_casa, self.gol_ospite)

    @property
    def esito_previsto(self) -> str:
        trio = {"1": self.prob_casa, "X": self.prob_pari, "2": self.prob_ospite}
        return max(trio, key=trio.get)

    @property
    def probabilita_sull_accaduto(self) -> float | None:
        """Quanta fiducia il modello aveva messo su cio' che poi e' successo.

        E' l'unico numero che si puo' leggere su una riga sola senza sbagliare
        mestiere: dire "azzeccato" o "sbagliato" su un pronostico
        probabilistico e' proprio l'errore che il Brier esiste per evitare.
        """
        reale = self.esito_reale
        if reale is None:
            return None
        return {"1": self.prob_casa, "X": self.prob_pari, "2": self.prob_ospite}[reale]


def riga_registro(
    voce: dict, fixture: FixtureRecord | None, nome
) -> RigaRegistro:
    """Una riga di registro piu' la partita a cui si riferisce.

    ``fixture`` puo' mancare: un pronostico e' immutabile e l'archivio delle
    partite no — una stagione potrebbe non essere piu' in linea. In quel caso
    si mostra cio' che il registro sa (le probabilita', quando furono scritte)
    e si tace il resto, invece di far sparire la riga: un pronostico
    dimenticato dal dataset resta un pronostico dato.
    """
    prob = {
        selezione: voce["selezioni"].get(selezione, {}).get("probabilita", 0.0)
        for selezione in ("home", "draw", "away")
    }
    scritto = voce["scritto_il"]
    anticipo = (
        distanza_breve(fixture.kickoff_utc - scritto)
        if fixture is not None and scritto is not None else None
    )
    conclusa = bool(fixture and fixture.is_finished)
    return RigaRegistro(
        fixture_id=voce["fixture_id"],
        casa=nome(fixture.home_team_id) if fixture else f"partita {voce['fixture_id']}",
        ospite=nome(fixture.away_team_id) if fixture else "",
        quando=formatta_data(fixture.kickoff_utc) if fixture else "",
        giornata=fixture.matchday if fixture else None,
        modo=voce["mode"],
        scritto=formatta_data(scritto) if scritto else None,
        anticipo=anticipo,
        prob_casa=prob["home"],
        prob_pari=prob["draw"],
        prob_ospite=prob["away"],
        conclusa=conclusa,
        gol_casa=fixture.home_goals if conclusa else None,
        gol_ospite=fixture.away_goals if conclusa else None,
        brier=voce["brier"],
        in_archivio=fixture is not None,
        stato=("chiuso" if voce["risolto"] else ("aperto" if conclusa else "attesa")),
    )


def righe_registro(
    elenco: list[dict],
    per_id: dict[int, FixtureRecord],
    nome,
    limite: int = LIMITE_REGISTRO,
    in_attesa: int = LIMITE_IN_ATTESA,
) -> list[RigaRegistro]:
    """Il registro in due meta': promesse aperte in cima, gia' giudicate sotto.

    L'ordine e' il **calcio d'inizio** e non il momento in cui la riga fu
    scritta, cosi' le partite ancora da giocare stanno in cima: sono quelle su
    cui la promessa e' ancora aperta, ed e' li' che questa pagina si distingue
    da una tabella di risultati.

    Le due meta' pero' non si ordinano allo stesso modo, e prima si faceva un
    ordine solo. **In cima si va all'indietro nel futuro** — la partita che si
    sta per vedere, non quella di maggio — e **sotto si va all'indietro nel
    passato**, dalla piu' recente. In mezzo alle due c'e' l'unico salto della
    pagina, ed e' proprio il momento presente.

    E ognuna ha la sua quota (`LIMITE_IN_ATTESA`), che e' cio' che impedisce a
    un calendario di 380 partite di seppellire le venti gia' giocate: con un
    tetto solo, le righe mostrate erano tutte e sole quelle del 2027 e la
    meta' misurata della pagina spariva. Vedi `LIMITE_REGISTRO`.

    Chi non ha piu' una partita in archivio sta con le giudicate e si ordina
    per quando fu scritto, che e' l'unico momento che gli resta: di lui non si
    sa nemmeno se abbia giocato, e metterlo fra le promesse aperte
    inventerebbe un futuro.
    """
    def quando(voce: dict) -> datetime:
        fixture = per_id.get(voce["fixture_id"])
        if fixture is not None:
            return fixture.kickoff_utc
        return voce["scritto_il"] or datetime.min.replace(tzinfo=timezone.utc)

    def aperta(voce: dict) -> bool:
        """La partita esiste in archivio e non e' ancora finita."""
        fixture = per_id.get(voce["fixture_id"])
        return fixture is not None and not fixture.is_finished

    # Il tetto vero e' il piu' stretto fra la quota dichiarata e **meta'**
    # pagina: cosi' la meta' gia' giudicata ha sempre spazio, qualunque siano
    # i due numeri. Con i valori del progetto (60 su 200) il secondo non
    # morde mai, ed e' li' apposta per il giorno in cui qualcuno li cambia.
    tetto = min(in_attesa, limite // 2)
    attesa = sorted((v for v in elenco if aperta(v)), key=quando)[:tetto]
    giudicate = sorted(
        (v for v in elenco if not aperta(v)), key=quando, reverse=True
    )[: max(0, limite - len(attesa))]

    return [
        riga_registro(voce, per_id.get(voce["fixture_id"]), nome)
        for voce in (*attesa, *giudicate)
    ]


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
    esito: str | None
    margine: float
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
    esito = None
    margine = 0.0
    if fixture is not None:
        in_casa = fixture.home_team_id == prestazione.team_id
        avversario_id = fixture.away_team_id if in_casa else fixture.home_team_id
        if fixture.home_goals is not None:
            risultato = f"{fixture.home_goals}-{fixture.away_goals}"
            # Il punteggio resta scritto casa-trasferta anche qui, quindi si
            # tinge con la stessa regola di ogni altra pagina: 1 alla casa, 2
            # alla trasferta. Da che parte stava il giocatore lo dice gia' la
            # pillola "in casa" / "in trasferta" accanto all'avversario.
            esito = esito_gol(fixture.home_goals, fixture.away_goals)
            margine = margine_gol(fixture.home_goals, fixture.away_goals)

    return RigaPrestazione(
        fixture_id=prestazione.fixture_id,
        quando=formatta_giorno(prestazione.quando),
        avversario=nome(avversario_id) if avversario_id else "—",
        avversario_id=avversario_id,
        in_casa=in_casa,
        risultato=risultato,
        esito=esito,
        margine=margine,
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

    `sceso_in_campo` ha tre valori e non due, ed e' la parte che conta.
    Senza tabellino l'assenza di una prestazione non significa "non e'
    entrato": significa che non lo sappiamo. Distinguerli non e' pignoleria —
    in Genoa-Napoli del 22 agosto 2026, con il tabellino fermo alla lettura
    del feed live, la panchina dichiarava "non entrato" accanto a De Bruyne e
    Vergara, cioe' ai due che erano entrati al 67' e avevano segnato
    entrambi i gol mostrati dalla stessa pagina poche righe sopra.

    La condizione e' "nessuna prestazione in tutta la partita" e non "il
    tabellino e' provvisorio", perche' le cause sono due — mai raccolto, o
    raccolto e scartato — e portano alla stessa ignoranza. Se il tabellino
    c'e' ed e' buono, un panchinaro che non compare non e' entrato davvero:
    l'endpoint elenca anche i subentrati mai usati, con i minuti a ``null``.
    """
    per_giocatore = {p.player_id: p for p in prestazioni}
    # Una partita conclusa col tabellino buono ha almeno i ventidue titolari:
    # se qui non c'e' nessuno, di questa partita non sappiamo niente.
    tabellino_noto = bool(per_giocatore)

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
            "sceso_in_campo": (prestazione is not None) if tabellino_noto else None,
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


def blocchi_indisponibili(righe, casa_id: int, casa: str, ospite: str) -> list[dict]:
    """Chi non gioca, diviso per squadra e con le assenze certe davanti.

    Assenti e "in dubbio" restano separati anche qui: sono due informazioni
    diverse, e presentarle nella stessa lista farebbe leggere come sicuro un
    giocatore che potrebbe scendere in campo.

    Il numero di assenze non e' un giudizio sulla partita: questa pagina dice
    **chi** manca, non **quanto** pesa: quello e' un altro lavoro, e finche'
    non c'e' e' meglio non alludervi.
    """
    per_squadra: dict[int, dict] = {}
    for r in righe:
        team_id = int(r["team_id"])
        blocco = per_squadra.setdefault(team_id, {
            "squadra_id": team_id,
            "squadra": r.get("team_name") or (casa if team_id == casa_id else ospite),
            "in_casa": team_id == casa_id,
            "assenti": [],
            "in_dubbio": [],
        })
        voce = {
            "id": int(r["player_id"]),
            "nome": r["player_name"],
            "motivo": r["motivo"],
            "spiegazione": traduci_motivo(r.get("descrizione")),
        }
        blocco["assenti" if r["stato"] == ASSENTE else "in_dubbio"].append(voce)

    for blocco in per_squadra.values():
        for chiave in ("assenti", "in_dubbio"):
            blocco[chiave].sort(key=lambda v: v["nome"])

    # Prima la squadra di casa, come ovunque nel progetto.
    return sorted(per_squadra.values(), key=lambda b: not b["in_casa"])


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
