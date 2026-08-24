# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Lingua

Codice, docstring, commenti, template web e README sono **in italiano** (nomi di
rotte e variabili di dominio compresi: `partita`, `righe`, `motore`). Mantieni
questa convenzione. Le spiegazioni all'utente vanno in italiano semplice.

## Comandi

Ambiente virtuale in `.venv` (Windows, PowerShell):

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest                       # tutta la suite
.\.venv\Scripts\python.exe -m pytest tests/test_daily.py -k finestra -x
.\.venv\Scripts\python.exe .\scripts\serve.py               # web su 127.0.0.1:8000
.\.venv\Scripts\python.exe .\scripts\demo_engine.py         # demo, nessuna credenziale
.\.venv\Scripts\python.exe .\scripts\backtest.py            # walk-forward, legge dalla cache
.\.venv\Scripts\python.exe .\scripts\backtest.py --cerca-compressione  # quanto comprimere i gol
.\.venv\Scripts\python.exe .\scripts\daily_collect.py       # raccolta giornaliera (spende budget)
.\.venv\Scripts\python.exe .\scripts\daily_collect.py --solo-al-momento-giusto # raccoglie solo se serve adesso
.\.venv\Scripts\python.exe .\scripts\verifica_raccolta.py   # la raccolta e' sana? (non spende)
.\.venv\Scripts\python.exe .\scripts\formazioni_ufficiali.py --stato  # i tabellini bastano gia'?
.\.venv\Scripts\python.exe .\scripts\formazioni_ufficiali.py  # pronostico con le formazioni (a ridosso del via)
.\.venv\Scripts\python.exe .\scripts\storico_pronostici.py  # cosa c'e' nel registro dei pronostici
.\.venv\Scripts\python.exe .\scripts\accuratezza.py         # quanto valgono i pronostici verificati
.\.venv\Scripts\python.exe .\scripts\riempi_storico.py --solo-verifica  # il piano copre le stagioni mancanti?
.\.venv\Scripts\python.exe .\scripts\riempi_storico.py     # ricompra la scorsa stagione (a pagamento)
.\.venv\Scripts\python.exe .\scripts\importa_risultati.py file.csv --solo-verifica  # cosa entrerebbe
.\.venv\Scripts\python.exe .\scripts\importa_risultati.py file.csv  # importa una stagione (gratis)
.\scripts\schedule_windows.ps1                              # registra le attivita' pianificate
```

Nessun linter o formatter configurato. `pytest` usa `pythonpath=["src"]`, quindi
i test girano anche senza installare il pacchetto.

## Architettura

Tutta la logica dei pronostici passa da **un solo oggetto**: la matrice dei
risultati esatti. `model/scoreline.py` la costruisce (Dixon-Coles su due
Poisson con correzione `rho` sui risultati bassi), `model/markets.py` deriva
1X2, Over/Under, Gol/NoGol, handicap e clean sheet **sommando celle** di quella
matrice. Non aggiungere mercati con formule indipendenti: la coerenza fra esiti
è garantita per costruzione solo se ogni mercato resta una somma di celle.

`model/dixon_coles.py` stima attacco/difesa per squadra con decadimento
temporale (`exp(-xi * giorni)`) e shrinkage ridge verso la media di lega.
Fa anche una cosa che **non** sta nella stima ma nel pronostico: in
`expected_goals` comprime il **totale** dei gol attesi verso la media di lega
(`comprimi_totale`, `DEFAULT_GOAL_COMPRESSION = 0.2`) lasciando intatto il
*rapporto* fra le due squadre. Nasce da una misura, non da un'idea: sui gol la
risoluzione del modello è quasi zero e le sue variazioni sono rumore che il
Brier paga (vedi «i mercati dei gol» sotto). Sta lì e non a valle sui singoli
mercati per la regola qui sopra — schiacciare P(Over 2.5) direttamente lo
scollegherebbe dalla somma dei risultati esatti che lo compongono. La media di
lega è stimata dallo stesso campione della stima, con lo stesso decadimento:
una costante scritta a mano invecchierebbe (2.43-2.61 gol nelle quattro
stagioni che abbiamo).
`model/simulation.py` genera campionati sintetici da forze note: è il modo in
cui la stima viene validata nei test e nella demo.

### Lo strato dati esiste per un vincolo di budget

Il piano gratuito di API-Football dà 100 richieste/giorno **e solo le stagioni
2022-2024**. Da qui l'intera forma di `data/`:

- `client.py` — unico punto d'accesso all'API; ogni metodo dichiara TTL e
  `Priority`. Non chiamare `httpx` direttamente da altrove. Qui vive anche il
  **freno**: API-Football ha un tetto **al minuto** oltre a quello giornaliero
  (10/min sul piano gratuito), e superarlo fa rispondere 429 mentre la
  richiesta viene contata lo stesso — si paga e non si riceve il dato. Sta nel
  client per la stessa ragione del budget: è l'unico punto da cui passano
  tutte le richieste. Vale solo su quelle davvero spedite (una risposta dalla
  cache non aspetta), e si regola da `.env` con `API_MIN_INTERVAL_SECONDS` —
  7 secondi di default, da abbassare il giorno di un piano a pagamento,
  altrimenti un riempimento storico vorrebbe nove ore invece di venti minuti.
- `budget.py` — contatore persistente (`data/budget.json`) con **riserve per
  priorità**: `BACKFILL` si ferma prima di intaccare la quota di `CRITICAL`
  (formazioni ufficiali). Toccando le priorità si rischia di restare a secco
  la domenica alle 14:30.
- `cache.py` — risposte grezze su disco (`data/cache/`); TTL per endpoint, una
  stagione conclusa non scade mai.
- `daily.py` — la stagione in corso si raccoglie con `/fixtures?date=...`
  (senza `league`, altrimenti l'API pretende `season` e blocca), finestra di
  **tre giorni**. Separa deliberatamente due cose: gli **id delle partite**,
  leggibili solo entro la finestra e **persi per sempre** se mancati, e i
  dettagli (formazioni, eventi, statistiche), recuperabili in qualsiasi momento
  dall'id via `backfill_details`. La raccolta deve sempre catturare gli id
  prima di spendere su dettagli. `daily_collect.py` chiude il giro chiedendo
  anche le **quote** delle partite entro 48 ore (`odds.py`): come gli id, non
  sono recuperabili dopo — ma sempre *dopo* i dati delle partite, mai prima.
  Per ultimi gli **indisponibili** (`indisponibili.py`), ed è voluto: sono
  l'unica cosa raccolta qui che si rilegge anche a partita finita, quindi se il
  budget stringe è giusto che a saltare siano loro e non le quote.
  **Quando** raccogliere non è più un orario ma un momento (`momento_utile`):
  due sole corse hanno un'ora fissa — le 08:00 per il calendario, che nessuna
  partita può decidere perché gli id vivono solo nella finestra di tre giorni,
  e le 23:45 per chiudere la giornata — mentre quote e indisponibili si
  prendono **entro 40 minuti dal via** e i dettagli **2h15 dopo**. È la stessa
  regola di sempre applicata al calendario invece che all'orologio: quelle due
  cose valgono quanto sono vicine al fischio, e un orario fisso ci passa vicino
  per caso. La vigilia batte il dopo, perché i dettagli si rileggono dall'id e
  le quote no. Chi chiama (`run_momento.ps1`, ogni 15 minuti) non decide
  niente: chiede e quasi sempre esce senza spendere.
  Il «2h15 dopo» ha un'eccezione, decisa il 23 agosto 2026: nelle ultime
  **4 ore prima che il budget si azzeri** i dettagli non si prendono affatto e
  si lasciano alla raccolta delle 08:00 (`dettagli_possono_aspettare`). Sono
  l'unica cosa che il sistema raccoglie a non avere né scadenza né fretta — si
  rileggono dall'id quando si vuole — e a fine giornata si pagavano due volte:
  con le richieste avanzate (la sera del 22 agosto 2026 la corsa delle 23:45
  trovò 75/100 già spese e non prese niente) e col rischio di salvare la
  fotografia del feed live invece del tabellino. La soglia è ancorata alla
  **mezzanotte UTC** e non a un'ora italiana, perché il posticipo delle 20:45
  cade alle 18:45 UTC d'estate e alle 19:45 d'inverno: quattro ore separano
  lui — e la corsa delle 23:45 — da tutto il resto in entrambi i fusi, mentre
  una soglia in ora locale sbaglierebbe metà stagione. La rete di sicurezza è
  `backfill_details`: la corsa serale chiude il giorno senza dettagli, e un
  giorno chiuso non viene più richiesto, quindi i tabellini arrivano soltanto
  perché l'id non scade. Non è una cura per i tabellini provvisori e non va
  raccontata come tale — quelli del 22 agosto erano fermi ancora **quattro
  ore** dopo il fischio: riduce quante volte succede, a ripescarli resta
  `needs_detail`.
- `store.py` — archivio della stagione corrente + `DayJournal` dei giorni
  raccolti. Un giorno viene chiuso — cioè non richiesto più — solo se è
  **passato** *e* aveva tutte le partite in stato definitivo. Oggi e domani non
  si chiudono mai, per quanto completi sembrino: un giorno ancora da giocare
  risulta "tutto definitivo" anche solo perché è vuoto, e il calendario della
  Serie A cambia (recuperi, anticipi). Chiuderlo in anticipo significherebbe
  perdere quelle partite per sempre. La regola sta in `is_closed()`, in
  lettura, non nel dato scritto: così nemmeno un diario vecchio può farla
  saltare.
  Stessa forma per una seconda regola, nata il 22 agosto 2026: un **tabellino
  può esserci ed essere sbagliato**. Per Genoa-Napoli e Parma-Cagliari
  `/fixtures/players` ha servito per ore l'ultima lettura del feed live —
  ventidue titolari fermi al 64' e al 61', zero gol, zero sostituzioni —
  mentre `/fixtures/events`, chiesto nello stesso minuto, raccontava la
  partita intera. Il guasto è dell'API; diventa nostro perché `needs_detail`
  considerava concluso qualunque dettaglio non vuoto, e quel tabellino non
  sarebbe stato richiesto mai più. Il segnale sono i **minuti** e non i gol
  (`tabellino_provvisorio`): in una partita finita qualcuno arriva sempre in
  fondo, mentre confrontare i gol col risultato marcherebbe come provvisoria
  ogni partita con un **autogol**, che al marcatore non viene contato. La
  metà meno visibile della correzione sta in `collect_details_for`: la cache
  dei dettagli non scade mai, quindi una ri-richiesta va fatta con
  `force_refresh`, altrimenti verrebbe servita dalla copia sbagliata e il
  contatore dei tentativi salirebbe fino al tetto senza che una sola chiamata
  parta davvero.
- `storico.py` — il riempimento di una stagione passata, per il giorno in cui
  si compra un piano superiore. Prima `/status` (l'unico endpoint che dice in
  chiaro che piano è attivo: una stagione non coperta non dà errore, dà zero
  partite), poi il calendario in **una** richiesta, poi i dettagli. Dentro il
  triennio 2022-2024 funziona anche sul piano gratuito — il muro è il filtro
  `league+season`, non gli endpoint per partita — ma a ~1140 richieste a
  stagione serve `--max-richieste` e mesi di pazienza. Le partite
  finiscono in `data/storico/<anno>/`, un `SeasonStore` come quello della
  stagione in corso: non in cache, perché una cartella con un TTL è una
  garanzia troppo debole per un dato pagato. Il riempimento è **una coda
  interrompibile**, non un'operazione atomica — ~1140 richieste per stagione —
  e al giro dopo salta ciò che ha già. I dettagli si chiedono tutti a priorità
  `BACKFILL`, formazioni comprese: sono lavoro di un anno fa, e la riserva
  `CRITICAL` protegge le formazioni di *oggi*. Quote e pronostici di quella
  stagione restano irrecuperabili: si guadagna un modello più informato, non
  uno storico di accuratezza più lungo.
- `importato.py` — una stagione presa da un **file di risultati** invece che
  dall'API, cioè il modo gratuito di chiudere il buco che `storico.py` chiude
  a pagamento. Funziona perché il modello di squadra mangia cinque cose per
  partita (data, le due squadre, i due gol) e un calendario le ha tutte:
  quello che manca — formazioni, tabellini, voti — non entra nella stima delle
  forze. Il punto delicato è **l'identità delle squadre**: il file scrive
  "Milan", l'API "AC Milan", e se i due non si riconoscono il club si sdoppia
  in due entità con metà storia ciascuna senza che niente lo segnali. Da qui
  la regola: gli id non si inventano mai, si cercano prima fra le squadre di
  Serie A note e poi — solo se il nome è univoco — fra tutte quelle nominate
  sul disco (è così che si trova una neopromossa poi retrocessa, senza
  spendere richieste); un nome che non si aggancia **ferma l'importazione**.
  Le partite ricevono id sintetici (`92025001`) e vivono in
  `data/importato/<anno>/`, separate da quelle comprate. Il file si verifica
  prima di scriverlo, con i controlli che un calendario fa su se stesso: N
  squadre, N*(N-1) partite, N-1 per parte, nessun accoppiamento ripetuto.
- `dataset.py` — unisce storico 2022-2024 (dalla cache), stagioni importate
  (da `data/importato/`), stagioni ricomprate (da `data/storico/`) e archivio
  corrente in un unico `Dataset`, che è ciò che il modello e il web consumano.
  L'ordine di sovrascrittura va dal più vecchio al più fresco: vince sempre la
  raccolta giornaliera, l'unica sorgente che vede una partita cambiare stato
  durante il pomeriggio. Una stagione presente sia importata che ricomprata si
  prende **solo dal dato pagato**, e serve una regola esplicita: gli id
  sintetici non verrebbero sovrascritti da quelli veri ma affiancati, e il
  campionato risulterebbe giocato due volte.
- `predictions.py` — registro dei pronostici in SQLite (`data/pronostici.db`).
  Append-only: `UPDATE` e `DELETE` sono **vietati da trigger**, non solo per
  convenzione. Si salva la matrice dei risultati, da cui ogni mercato si può
  riderivare mesi dopo, più i mercati materializzati per le query di
  calibrazione. Due regole nel codice: non si registra una partita già
  iniziata (sarebbe barare: il modello è stimato su quella partita), e si
  scrive **una riga per partita e versione del modello** — l'impronta dei
  parametri stimati dice se il modello è cambiato, così ricaricare l'hub non
  moltiplica righe identiche.
- `outcomes.py` — il confronto fra pronostico e realtà: a partita conclusa
  ogni esito registrato prima del fischio d'inizio diventa `hit` + contributo
  al Brier. Gli esiti si scrivono **una volta sola** (`insert or ignore`): un
  esito è un fatto osservato, non una previsione, ma ricalcolarlo a ogni giro
  aprirebbe la porta a riscritture. Ogni mercato salvato deve avere il suo
  risolutore — un test lo verifica per costruzione, perché un mercato che si
  sa salvare ma non risolvere resterebbe fuori dal conto senza che nessuno lo
  noti. La **baseline si calcola per singola selezione e poi si media**: su
  Over/Under le linee 0.5 e 4.5 hanno frequenze opposte e mescolarle darebbe
  al modello un muro molto più basso di quello vero.
- `odds.py` — le quote dei bookmaker, cioè il metro di paragone vero del
  modello. Tre vincoli danno forma al modulo: le quote **non si recuperano**
  (API-Football non le archivia: o si prendono prima della partita o sono perse,
  come gli id), la **chiusura non è un campo ma un momento** (si salva ogni
  lettura con i minuti che mancavano al via, e la chiusura è l'ultima lettura
  prima del fischio), e una **quota non è una probabilità** (va tolto il
  margine del banco, normalizzando dentro ogni gruppo esaustivo — le linee
  Over/Under si normalizzano una per una, mai tutte insieme). Il consenso fra
  bookmaker è la **mediana**, che un book fuori linea non sposta. Le selezioni
  ricavate dalle quote devono combaciare esattamente con quelle di
  `righe_mercati`: se divergessero il confronto non aggancerebbe nulla e la
  pagina resterebbe vuota senza errori — un test lo impedisce.
- `indisponibili.py` — chi non gioca: infortunati, squalificati e giocatori in
  dubbio. Si chiedono **per partita** (`/injuries?fixture=`), mai per stagione:
  il filtro `league+season` è bloccato sul piano gratuito esattamente come per
  le fixtures. A differenza delle quote **si recuperano anche dopo la partita**
  (verificato su una gara del 2024), quindi vanno a priorità `BACKFILL` e non
  hanno fretta: se una raccolta salta, si riprende domani. Il momento della
  lettura conta lo stesso, però, e per la ragione di sempre: la lista letta a
  partita finita sa chi non è sceso in campo davvero, e usarla per
  pronosticare sarebbe barare. Di ogni lettura si salva `minuti_al_via`, e
  `indisponibili_alla_vigilia` prende solo l'ultima **precedente** al fischio
  d'inizio. Due trappole, entrambe coperte da un test: l'API **duplica le
  righe** (16 record per 8 giocatori su Inter-Monza del 22/08/2026), e una
  lista è una **fotografia** — chi guarisce sparisce dalla foto dopo senza che
  nessuna riga lo dica, quindi si legge la lettura intera e mai "l'ultima
  notizia su ogni giocatore", che lo lascerebbe infortunato per sempre.
  `assente` e `in_dubbio` non si mescolano: solo il primo è un fatto.
- `pesi.py` — quanto pesa un giocatore, misurato da cio' che ha **prodotto**
  (gol e assist per minuto), non da come va la squadra quando c'è o non c'è:
  quella strada è rumore travestito da misura, perché un titolare salta cinque
  partite l'anno e sta fuori proprio nei periodi storti. Il tasso di ognuno
  passa per uno **shrinkage** verso la media della squadra pesato sui minuti —
  senza, chi entra dieci minuti e segna varrebbe più di tutta la squadra. Due
  regole di onestà: sotto `MINIMO_PARTITE` partite con tabellino non si stima
  niente e si restituisce `None`, che significa "non lo so" e non deve mai
  diventare 1.0, che significa "so che non cambia"; e un titolare sconosciuto
  all'archivio vale la produzione **media** di un titolare, non zero — non
  sapere nulla di lui non è sapere che non vale niente. Il metodo **vede gli
  attaccanti, non i difensori**, ed è un limite da dichiarare in pagina, non da
  compensare con un'ipotesi. `FATTORE_PREDEFINITO` **non è calibrato**: dovrà
  uscire dal backtest.
- `ufficiali.py` — il pronostico rifatto quando escono le formazioni, in
  `MODE_UFFICIALE` **accanto** a quello secco e mai al suo posto: due
  pronostici sulla stessa partita sono l'unico modo di scoprire se il lavoro
  serve, confrontando i Brier fra qualche mese. Non scrive **nessuna riga**
  finché i pesi non esistono o non spostano niente (`vale_la_pena`): i
  pronostici sono immutabili, e una riga identica a quella secca non sarebbe
  inutile ma dannosa — direbbe "misurato" dove non c'era niente da misurare.
  È anche ciò che tiene la macchina spenta finché i tabellini non bastano, da
  sola e senza che nessuno debba accenderla. Una risposta di formazioni
  **vuota non si salva**: farebbe scattare `MAX_DETAIL_ATTEMPTS` e le
  formazioni vere, quelle di fine serata, non verrebbero più chieste.
- `players.py` — le schede giocatore, ricavate **senza spendere richieste**:
  formazioni, tabellini ed eventi sono gia' su disco, pagati dalla raccolta
  giornaliera. Un dettaglio si legge sempre dalla busta (`load_detail`), e una
  busta con payload vuoto significa "l'API non lo copre". L'identita' di un
  giocatore e' l'id numerico, mai il nome — stessa regola delle squadre. I
  numeri vengono da **una sorgente sola**: minuti, gol, assist, cartellini e
  voti da `players`, mentre gli eventi raccontano soltanto *quando* si e'
  segnato; sommare le due cose conterebbe ogni gol due volte. L'archivio sa su
  quante partite concluse il tabellino esiste davvero e le pagine lo
  dichiarano: nel conto entrano solo le partite di cui possediamo l'archivio
  grezzo (la raccolta giornaliera e le stagioni eventualmente ricomprate),
  perche' lo storico 2022-2024 arriva dalla cache come elenco di risultati e i
  tabellini non li ha mai avuti — contarlo farebbe sembrare parziale una
  copertura piena.
  Un tabellino **provvisorio non entra affatto** (`tabellino_provvisorio`): non
  e' un tabellino parziale, e' un tabellino sbagliato, e vale come "non ancora
  raccolto" perche' e' la verita' — arrivera'. Senza questa regola il Napoli
  del 22 agosto 2026 entrava con 704 minuti, zero gol e zero assist, e i pesi
  di `pesi.py` ne uscivano schiacciati verso il basso **senza che niente lo
  segnalasse**: quella partita contava anche nelle cinque che accendono le
  formazioni ufficiali, quindi acceleravano l'accensione e ne avvelenavano
  l'ingresso insieme. Le partite scartate si **contano** (`partite_provvisorie`)
  e si dichiarano in `--stato` e nella scheda partita: "manca" e "l'API non
  l'ha ancora consolidato" sono due risposte diverse, e la seconda si risolve
  da sola.
- `notifiche.py` (fuori da `data/`, perché non è una sorgente ma un'uscita) —
  i messaggi su Telegram. Quattro regole, tutte difensive: **il silenzio è il
  default** (senza token il sistema gira identico), **non solleva mai** (un
  messaggio perso è un fastidio, una raccolta interrotta può costare un giorno
  di partite), si decide **dal contenuto e non dall'orologio** — nessun
  messaggio è legato a un orario, si guarda cosa c'è sul disco e cosa non è
  ancora stato detto, ed è così che il resoconto delle 23:45 arriva la mattina
  dopo se il computer era spento — e **niente si manda due volte**
  (`RegistroInvii`, un JSON in `data/notifiche.json`). Si mandano due messaggi
  al giorno che si vogliono leggere (i pronostici del mattino, com'è andata la
  sera) e sei allarmi che si devono leggere — l'ultimo, `tabellini_fermi`,
  arriva **tardi apposta**: un tabellino provvisorio nelle ore dopo la partita
  e' normale e si risolve da solo, e dirlo ogni sera insegnerebbe a ignorare il
  messaggio. Parte quando i tentativi sono finiti (`tabellini_arresi` in
  `data/store.py`), cioe' nell'unico momento in cui il sistema ha smesso di
  poter rimediare da solo; le ~90 esecuzioni quotidiane
  che non fanno niente restano mute, perché dirle insegnerebbe a ignorare le
  altre. Il modello non entra nel modulo: `invia_quel_che_serve` riceve i
  pronostici come **funzione**, così si decide se c'è qualcosa da dire prima
  di pagare i due secondi della stima.
- `parsers.py` — JSON di API-Football → oggetti di dominio. I test coprono i
  parser con payload reali riprodotti in `tests/conftest.py`, così nessun test
  spende richieste.

⚠️ `data/serie_a/` **non è una cache**: è l'unica copia esistente della
stagione in corso e l'API non restituirà mai più i giorni passati. Non
cancellarla né suggerire di rigenerarla. `data/storico/` è meno drammatico —
si può riscaricare finché il piano a pagamento è attivo — ma buttarlo significa
rispendere mille richieste, quindi merita la stessa prudenza.
`data/importato/` è l'unica cartella davvero rigenerabile: basta rilanciare
`scripts/importa_risultati.py` sul file di partenza, a costo zero — a patto che
quel file ci sia ancora.

### Web

`web/engine.py` tiene dati e modello caldi in uno `Snapshot`, ricostruito solo
quando cambia l'impronta degli archivi (mtime+size di `fixtures.json` **più**
quella di `data/storico/` **più** quella di `data/importato/`), non a ogni
richiesta HTTP — la stima costa un paio di secondi. I pezzi oltre il primo
servono al riempimento storico e all'importazione: senza, il modello resterebbe
fermo alle stagioni di prima anche dopo aver ricomprato o importato un
campionato intero, con il sito aperto e nessun motivo apparente per riavviarlo.
Un lock protegge da stime duplicate su richieste concorrenti.

Alla ristima — l'unico momento in cui i pronostici possono cambiare — l'engine
fa due cose in fila: congela i pronostici delle partite in programma e risolve
gli esiti di quelle ormai concluse. Entrambe protette da `try/except`: una
pagina senza storico è un disservizio, una pagina che non si apre è un guasto.

L'archivio dei giocatori ha invece una vita sua: si costruisce **su richiesta**
(l'hub non ne ha bisogno) e ha un'impronta separata, `details_signature()`,
perché i dettagli arrivano dopo le partite — spesso giorni dopo — senza che
`fixtures.json` si muova di un byte. Legge da uno `StoreUnito`, cioè da tutti
gli archivi che possono contenere un tabellino messi in fila: un giocatore ha
una carriera sola anche se metà dei suoi tabellini è stata pagata a parte. Le
stagioni **importate da un file non entrano** in quella fila: hanno i risultati
e non avranno mai i tabellini, e contarle direbbe "copertura parziale" su un
campionato coperto per intero — la stessa trappola già evitata per lo storico
2022-2024.

Da quale fonte esca un pronostico dipende da **quando** lo si guarda, e la
regola sta in `_riga` (`web/app.py`): prima del fischio d'inizio dal modello
caldo, dopo dal registro e mai da un ricalcolo (`Engine.congelato`). È la
stessa regola che `data/predictions.py` applica già in scrittura, applicata
finalmente anche in lettura — mancava, e il 24 agosto 2026 l'utente se ne
accorse da solo: Venezia-Lecce diceva 40-31-29 il sabato e 34-28-38 a partita
finita, con la vittoria del Lecce cresciuta di nove punti *dopo* che il Lecce
aveva vinto. Non è un dettaglio di presentazione: il modello viene ristimato su
ogni risultato nuovo, quello della partita guardata compreso, quindi un
ricalcolo è un ricordo travestito da previsione, ed è la differenza fra un
modello che ci ha preso e uno che sembra averci preso. Le misure (`accuratezza`,
Brier) non erano toccate — leggono dal database — ma era proprio la pagina a
raccontare la storia sbagliata.
Tre corollari, tutti già in codice: la **griglia dei risultati esatti** viene
dalla matrice congelata e non da una nuova (percentuali di allora accanto a una
griglia di oggi sarebbero due pronostici sulla stessa pagina); l'**affidabilità**
sparisce sulle partite già cominciate, perché misura il modello di *adesso* e
accanto a un pronostico di allora descriverebbe una stima diversa e più
informata; e una partita giocata **senza** riga nel registro non mostra nessun
pronostico e dice perché — "non lo so" e "nessuno lo chiese in tempo" sono due
assenze diverse, e la seconda non si rimedia più. Vale per tutte le stagioni
precedenti al registro, che infatti in pagina non hanno percentuali.

`web/views.py` traduce il dominio in ciò che i template disegnano; `web/app.py`
espone hub, scheda partita, `/giocatori` (marcatori, assist, voti),
`/giocatore/<id>`, `/squadra/<id>`, `/stato` (JSON: cosa sa il sistema, budget
residuo, pronostici registrati, esiti risolti) e `/aggiorna` (forza la
ricostruzione). La scheda partita mostra anche gol, formazioni e pagelle
quando i dettagli ci sono, e dice quando non ci sono. Mostra inoltre gli
**indisponibili**, che sono l'unica cosa che quella pagina sappia dire su una
partita non ancora giocata — e dichiara che il pronostico **non ne tiene
conto**, perché è vero: quanto pesi chi manca è il lavoro che deve ancora
arrivare. Se di quella partita non esiste nessuna lettura la sezione non
compare affatto: una sezione vuota si leggerebbe come "non manca nessuno", che
è un'informazione, e sarebbe inventata.
L'hub mostra, sotto le partite: accuratezza per mercato con la sua baseline e
il confronto con le quote di chiusura — entrambe le sezioni compaiono solo
quando c'è qualcosa di misurato, e dichiarano quando i dati non bastano.

La scheda partita dichiara anche **su cosa poggia** il pronostico
(`TeamEvidence` in `model/dixon_coles.py`, `righe_affidabilita` in
`web/views.py`). Serve perché il modello risponde sempre: su una squadra che
conosce poco lo shrinkage la incolla alla media di lega e ne esce un pronostico
prudente — spesso 1-1 o 1-0 — identico a occhio a uno informato. L'indicatore
dice **due** cose, e mostrarne una sola inganna più che tacerle entrambe:
quanto la forza venga dai dati della squadra invece che dalla media (il
`pull`, dalla curvatura della verosimiglianza contro quella della penalità
ridge), e **quanto sono vecchi quei dati misurati da oggi**. La seconda non è
contenuta nella prima: il decadimento temporale è calcolato rispetto alla
partita più recente del campione, non rispetto a oggi, quindi un archivio fermo
da un anno ha pesi identici a uno di ieri e risulterebbe "solido". Da qui la
regola nel verdetto: oltre un'emivita dall'ultima partita nota nessuna stima si
chiama solida, per quante partite abbia dietro.

## Vincoli di progetto

- **I pronostici sono immutabili**, legati a una `model_version`
  (`data/schema.sql`). Rigenerarne uno significa inserire una nuova riga, mai
  aggiornare quella vecchia: uno storico riscrivibile a posteriori vale zero.
- **Non nascondere i limiti del modello nell'interfaccia.** Nel backtest l'1X2
  batte le baseline (Brier 0.589 nel 2024/25 e 0.594 nel 2025/26, contro
  0.667 di chi tira a caso), mentre su Over 2.5 e Gol/NoGol il modello — dopo
  la compressione dei gol — *pareggia* con la media di campionato invece di
  batterla: le pagine lo dichiarano accanto al mercato, con queste parole
  («su questi esiti dichiara quasi sempre la media di campionato, perché è
  quanto sa davvero»). Stessa regola quando una squadra non ha storia
  sufficiente: si mostra il motivo, non un pronostico inventato.
- La valutazione è **Brier score, log loss e calibrazione**, non "percentuale
  di pronostici azzeccati" (predire sempre "1" dà già ~45%).
- Due misure diverse, da non confondere: il **backtest** rigioca il passato con
  il modello di oggi, l'**accuratezza** (`/`, `scripts/accuratezza.py`) usa solo
  pronostici congelati prima del fischio d'inizio. La seconda è la misura
  onesta e all'inizio della stagione sarà povera; sotto i 30 esiti per mercato
  l'interfaccia dichiara che il numero non vale ancora.
- **Il confronto che conta è con le quote**, non con le baseline: battere chi
  tira a caso non dimostra nulla, pareggiare col mercato è già un buon
  risultato. Il confronto usa solo gli esiti per cui esistono *entrambe* le
  cose, pronostico congelato e quota di consenso — misurare il modello su
  tutte le partite e il mercato su un sottoinsieme diverso darebbe un numero
  privo di significato.
- La stagione 2025/26 era un buco fra lo storico e la raccolta giornaliera.
  **È stata chiusa il 22 agosto 2026 importando un file di risultati** (380
  partite, zero richieste): vedi `data/importato.py`. Dall'API resta
  irrecuperabile sul piano gratuito, e non ci sono scorciatoie da cercare —
  quella strada passa da `scripts/riempi_storico.py` e da un piano a pagamento.
  Cosa manca ancora di quella stagione: i **tabellini**, che un file di
  risultati non ha e non avrà mai. Quelli si comprano.
- Servono `.env` con `API_FOOTBALL_KEY` solo per gli script sui dati veri; test
  e demo girano senza.

## Lavori futuri

`DA_FARE.txt` (file locale, volutamente non tracciato) elenca i lavori
concordati con l'utente, otto in tutto (l'ottavo nato il 22 agosto 2026): pronostici con le formazioni
ufficiali (**predisposti e spenti**, vedi `data/pesi.py` e `data/ufficiali.py`:
la macchina c'è tutta e non scrive niente finché i tabellini non bastano — non
è un bug da sistemare, è la regola); schede giocatore (**fatte**, vedi
`data/players.py`); infortuni e
squalifiche (**metà fatti**: si raccolgono e si vedono, vedi
`data/indisponibili.py`; entrare nel pronostico dipende dalle formazioni); il
riempimento della stagione 2025/26 (**metà fatto**: i risultati sono entrati
gratis da un file, vedi `data/importato.py`; i tabellini restano a pagamento,
vedi `data/storico.py`); i tabellini delle stagioni 2022-2024, che oggi
abbiamo solo come risultati; l'impatto dell'allenatore, che si ricava dalle
formazioni senza spendere richieste; i mercati dei gol (**fatti** il 22 agosto
2026 con la compressione dei totali, vedi `comprimi_totale` in
`model/dixon_coles.py` e `backtest.py --cerca-compressione`); e il **modello
a tiri**, che è l'unica strada rimasta per sapere davvero qualcosa sui gol —
i tiri sono già dentro le buste `/fixtures/players` che la raccolta scarica,
quindi non costano richieste, ma i tabellini non ci sono ancora.

L'ordine suggerito sta in cima a `DA_FARE.txt`. Il pezzo che conta: i
**tabellini 2022-2024** sono il collo di bottiglia — sbloccano formazioni,
allenatore e tiri — e la decisione di rimandarli era stata presa quando
sembravano servire solo alle pagine. Col modello a tiri sono diventati materia
prima, e vanno avviati per primi perché sono l'unico lavoro che impiega mesi.

Su quest'ultimo elenco una correzione verificata il 22 agosto 2026: i
**tabellini 2022-2024 non richiedono un piano a pagamento**. Il piano gratuito
blocca il filtro `league+season`, non gli endpoint per singola partita, e
quelle tre stagioni sono fra le concesse — `/fixtures/players`,
`/fixtures/lineups` e `/fixtures/events` rispondono con il payload pieno. Il
lavoro è quindi gratis ma lungo (~1140 richieste a stagione, ~2 mesi per i soli
tabellini) e l'utente ha deciso di **rimandarlo al giorno del piano**, che lo
fa in una giornata. Non proporlo come lavoro gratuito senza ricordare i tempi.

Contiene per ognuno cosa esiste già nel codice e quali sono le insidie — fra
cui tre che sembrano buone idee e non lo sono: i tabellini vecchi non
migliorano i pronostici; una "forza dell'allenatore" che viaggia da una squadra
all'altra non è identificabile con un campionato solo; e i mercati dei gol
**non** si curano con una distribuzione a coda più grassa, perché nei nostri
dati i gol totali sono *sotto*-dispersi (varianza sempre minore della media,
quattro stagioni su quattro) e il modello è già troppo largo. Se il file non
c'è, questa è l'unica traccia che rimane di quell'elenco.

Sui mercati dei gol, ora che il lavoro è stato fatto, restano due cose da
ricordare. La prima: la compressione **toglie un danno, non aggiunge una
capacità** — si arriva a pareggiare con la baseline, e chi la ritocca
aspettandosi di saper pronosticare i gol sta inseguendo qualcosa che nei dati
non c'è. La seconda: il fattore si sceglie **sulle stagioni diverse da quella
su cui lo si misura**, e ogni volta si guarda anche l'1X2 — è il mercato che
funziona, e vale più di quello che non funziona. Verificato il 22 agosto 2026:
`rho` **non** va ristimato dopo la compressione, muoverlo di ±0.10 sposta i
mercati dei gol di meno di 0.0005 di Brier e in direzioni opposte da una
stagione all'altra.
