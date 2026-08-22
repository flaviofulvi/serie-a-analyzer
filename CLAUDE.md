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
.\.venv\Scripts\python.exe .\scripts\daily_collect.py       # raccolta giornaliera (spende budget)
.\.venv\Scripts\python.exe .\scripts\verifica_raccolta.py   # la raccolta e' sana? (non spende)
.\.venv\Scripts\python.exe .\scripts\storico_pronostici.py  # cosa c'e' nel registro dei pronostici
.\.venv\Scripts\python.exe .\scripts\accuratezza.py         # quanto valgono i pronostici verificati
.\.venv\Scripts\python.exe .\scripts\riempi_storico.py --solo-verifica  # il piano copre le stagioni mancanti?
.\.venv\Scripts\python.exe .\scripts\riempi_storico.py     # ricompra la scorsa stagione (a pagamento)
.\.venv\Scripts\python.exe .\scripts\importa_risultati.py file.csv --solo-verifica  # cosa entrerebbe
.\.venv\Scripts\python.exe .\scripts\importa_risultati.py file.csv  # importa una stagione (gratis)
.\scripts\schedule_windows.ps1                              # 4 esecuzioni al giorno + recupero
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
`model/simulation.py` genera campionati sintetici da forze note: è il modo in
cui la stima viene validata nei test e nella demo.

### Lo strato dati esiste per un vincolo di budget

Il piano gratuito di API-Football dà 100 richieste/giorno **e solo le stagioni
2022-2024**. Da qui l'intera forma di `data/`:

- `client.py` — unico punto d'accesso all'API; ogni metodo dichiara TTL e
  `Priority`. Non chiamare `httpx` direttamente da altrove.
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
- `store.py` — archivio della stagione corrente + `DayJournal` dei giorni
  raccolti. Un giorno viene chiuso — cioè non richiesto più — solo se è
  **passato** *e* aveva tutte le partite in stato definitivo. Oggi e domani non
  si chiudono mai, per quanto completi sembrino: un giorno ancora da giocare
  risulta "tutto definitivo" anche solo perché è vuoto, e il calendario della
  Serie A cambia (recuperi, anticipi). Chiuderlo in anticipo significherebbe
  perdere quelle partite per sempre. La regola sta in `is_closed()`, in
  lettura, non nel dato scritto: così nemmeno un diario vecchio può farla
  saltare.
- `storico.py` — il riempimento di una stagione passata, per il giorno in cui
  si compra un piano superiore. Prima `/status` (l'unico endpoint che dice in
  chiaro che piano è attivo: una stagione non coperta non dà errore, dà zero
  partite), poi il calendario in **una** richiesta, poi i dettagli. Le partite
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

`web/views.py` traduce il dominio in ciò che i template disegnano; `web/app.py`
espone hub, scheda partita, `/giocatori` (marcatori, assist, voti),
`/giocatore/<id>`, `/squadra/<id>`, `/stato` (JSON: cosa sa il sistema, budget
residuo, pronostici registrati, esiti risolti) e `/aggiorna` (forza la
ricostruzione). La scheda partita mostra anche gol, formazioni e pagelle
quando i dettagli ci sono, e dice quando non ci sono.
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
  batte le baseline (Brier 0.586 nel 2024/25 e 0.596 nel 2025/26, contro
  0.667 di chi tira a caso), ma Over 2.5 e Gol/NoGol fanno
  leggermente *peggio* della media di campionato: le pagine lo dichiarano
  accanto al mercato. Stessa regola quando una squadra non ha storia
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
concordati con l'utente, sette in tutto: pronostici con le formazioni
ufficiali; schede giocatore (**fatte**, vedi `data/players.py`); infortuni e
squalifiche; il riempimento della stagione 2025/26 (**metà fatto**: i risultati
sono entrati gratis da un file, vedi `data/importato.py`; i tabellini restano
a pagamento, vedi `data/storico.py`); i tabellini delle stagioni 2022-2024, che
oggi abbiamo solo come risultati; l'impatto dell'allenatore, che si ricava
dalle formazioni senza spendere richieste; i mercati dei gol, l'unico che nasce
da una misura invece che da un'idea.

Contiene per ognuno cosa esiste già nel codice e quali sono le insidie — fra
cui tre che sembrano buone idee e non lo sono: i tabellini vecchi non
migliorano i pronostici; una "forza dell'allenatore" che viaggia da una squadra
all'altra non è identificabile con un campionato solo; e i mercati dei gol
**non** si curano con una distribuzione a coda più grassa, perché nei nostri
dati i gol totali sono *sotto*-dispersi (varianza sempre minore della media,
quattro stagioni su quattro) e il modello è già troppo largo. Se il file non
c'è, questa è l'unica traccia che rimane di quell'elenco.
