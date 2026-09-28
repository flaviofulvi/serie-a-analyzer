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
.\scripts\run_web.ps1                                       # il sito acceso in permanenza (telefono via Tailscale)
.\.venv\Scripts\python.exe .\scripts\demo_engine.py         # demo, nessuna credenziale
.\.venv\Scripts\python.exe .\scripts\backtest.py            # walk-forward, legge dalla cache
.\.venv\Scripts\python.exe .\scripts\backtest.py --cerca-compressione  # quanto comprimere i gol
.\.venv\Scripts\python.exe .\scripts\backtest.py --cerca-allenatore    # quanto pesa il cambio di panchina
.\.venv\Scripts\python.exe .\scripts\backtest.py --cerca-emivita       # quanto in fretta invecchia una partita
.\.venv\Scripts\python.exe .\scripts\backtest.py --cerca-shrinkage     # quanto tirare verso la media di lega
.\.venv\Scripts\python.exe .\scripts\backtest.py --cerca-emivita-lega  # quanto ricordano vantaggio di casa, base e rho
.\.venv\Scripts\python.exe .\scripts\backtest.py --cerca-fattore       # quanto credere alle formazioni ufficiali
.\.venv\Scripts\python.exe .\scripts\backtest.py --marcatori           # quanto vale il pronostico su chi segna
.\.venv\Scripts\python.exe .\scripts\backtest.py --cerca-correzione-marcatori  # quanto correggere la sovrastima dei marcatori
.\.venv\Scripts\python.exe .\scripts\marcatori_contro_quote.py  # chi segna, contro Bet365 (gratis)
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
.\.venv\Scripts\python.exe .\scripts\recupera_quote_marcatori.py  # le quote marcatori rimaste in cache (gratis)
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

I due numeri che governano la stima — l'**emivita** del decadimento temporale
e lo **shrinkage** verso la media di lega — non sono più scelti a occhio:
`backtest.py --cerca-emivita` e `--cerca-shrinkage` li cercano col metodo del
progetto (una griglia, ogni stagione rigiocata una volta per valore, il valore
scelto sulle stagioni *diverse* da quella su cui lo si misura). Misurati il 25
agosto 2026: **restano 180 giorni e 2.0**. Lo shrinkage è l'unico parametro
del progetto con un ottimo interno vero (0.5968 a 2, peggio da entrambi i
lati), ed è stato **rimisurato il 3 settembre 2026 con lo stesso esito** —
non per un cambio di codice ma perché l'archivio è cresciuto da tre stagioni a
quattro, e lo shrinkage *effettivo* dipende da quanta massa pesata c'è nel
campione: il numero poteva significare qualcosa di diverso pur restando
scritto uguale. Non è successo, e la nota resta valida per la prossima volta; l'emivita invece ha una curva piatta da 120 giorni in su, e l'ottimo
apparente a 365 sparisce appena lo si sceglie senza aver visto la risposta —
peggiora di 0.0005. Quello che la misura dice davvero è che **sotto i due mesi
si fa danno** (0.6042 a 60 giorni): il modello vuole ricordare quasi tutto.
Le tre ricerche che toccano la stima (allenatore, emivita, shrinkage) passano
tutte da `cerca_nella_stima`, e il verdetto obbedisce alla **misura onesta**,
non all'ottimo: un valore migliore sulle stagioni messe insieme ma perdente
scelto sulle altre viene dichiarato rumore e non proposto.

### Lo strato dati esiste per un vincolo di budget

**Il piano è cambiato, la forma no, e capire perché è il modo giusto di
leggere tutto questo capitolo.** `data/` è stato costruito contro il piano
gratuito di API-Football: 100 richieste al giorno e solo le stagioni
2022-2024. Dal 24 agosto al 24 settembre 2026 è stato attivo un piano **Pro**
— 7500 richieste al giorno, tutte le stagioni — e con quello sono state
comprate le quattro stagioni in archivio. **Dal 24 settembre 2026 si è tornati
al gratuito**, per decisione dell'utente, e il vincolo è di nuovo quello vero.

Il vincolo durante il Pro era sospeso, non tolto, e niente qui va rilassato
«perché un giorno ci sarà spazio». Le tre forme che ne discendono — il
contatore col budget a riserve, la cache che non scade, la finestra di tre
giorni — restano giuste con qualunque piano.

Il downgrade è andato come previsto: dalla prima risposta del 25 settembre il
contatore ha letto da solo 100 al giorno e 10 al minuto (`data/budget.json`,
voce `piano`), il `.env` ha le due righe qui sotto e il wrapper della raccolta
passa `--no-calendario`. Quello che segue racconta perché **non serviva
ricordarsi di niente**, ed è la cosa
nuova dall'11 settembre 2026 (lavoro C1). API-Football dichiara i suoi limiti
negli header di ogni risposta e il client li legge: il contatore usa il più
basso fra `.env` e server, il freno il più prudente. Il Pro concede **300**
richieste al minuto — letto dagli header, non le 450 che si credevano — e il
gratuito dieci: col `.env` fermo a 1 secondo il freno si alza da solo a 7. Le
due righe nel `.env` restano da scrivere, ma per mettere in pari la
configurazione e non per evitare un danno (un messaggio su Telegram lo
ricorda):

    DAILY_REQUEST_LIMIT=100
    API_MIN_INTERVAL_SECONDS=7

Prima di C1 la seconda era quella che si dimentica, e lasciarla a 1 faceva
prendere 429 alla raccolta — **e un 429 viene contato lo stesso**. L'unica
cosa da fare a mano era `--no-calendario` nel wrapper della raccolta, fatta il
24 settembre: il calendario di stagione è l'unica richiesta che passi dal
filtro `league+season`, e sul gratuito risponde zero partite senza errore.

Cosa **non** smette di funzionare, così nessuno si spaventa: gli endpoint per
singola partita rispondono anche sul gratuito, stagione in corso compresa. A
sparire è solo il «a stagione intera» — e a stagione intera non serve più
niente, perché le quattro stagioni sono in archivio complete.

Da qui l'intera forma di `data/`:

- `client.py` — unico punto d'accesso all'API; ogni metodo dichiara TTL e
  `Priority`. Non chiamare `httpx` direttamente da altrove. Qui vive anche il
  **freno**: API-Football ha un tetto **al minuto** oltre a quello giornaliero
  (10/min sul piano gratuito), e superarlo fa rispondere 429 mentre la
  richiesta viene contata lo stesso — si paga e non si riceve il dato. Sta nel
  client per la stessa ragione del budget: è l'unico punto da cui passano
  tutte le richieste. Vale solo su quelle davvero spedite (una risposta dalla
  cache non aspetta), e si regola da `.env` con `API_MIN_INTERVAL_SECONDS` —
  7 secondi di default, di nuovo a 7 dal ritorno al gratuito (col Pro era
  a 1). Dall'11 settembre 2026 il
  client legge anche i **limiti dichiarati** negli header di ogni risposta
  (`limiti_dagli_header`, `_impara_i_limiti`), e il freno in vigore è il più
  prudente fra `.env` e tetto al minuto del server (`intervallo_in_vigore`,
  con la stessa proporzione dei 7 secondi del gratuito): il giorno del
  downgrade si rialza da solo, e se il server dice che il minuto è finito si
  aspetta un minuto intero — l'unico modo di accorgersi degli altri due
  processi che spendono insieme. Vale in una direzione sola, e vale per tutto
  C1: **da soli si diventa più prudenti, mai meno**. Leggere gli header non
  solleva mai, perché la risposta è già pagata. Con 7 secondi un riempimento
  storico vuole nove ore invece di venti minuti, ed è il motivo per cui il
  `.env` è stato abbassato.
- `budget.py` — contatore persistente (`data/budget.json`) con **riserve per
  priorità**: `BACKFILL` si ferma prima di intaccare la quota di `CRITICAL`
  (formazioni ufficiali). Toccando le priorità si rischia di restare a secco
  la domenica alle 14:30.
  Il tetto giornaliero ha **due fonti** dall'11 settembre 2026 e vale la più
  bassa (`limite_in_vigore`): il `.env` e l'ultima dichiarazione del server,
  che si ricorda nel file anche dopo mezzanotte (`registra_dichiarazione`).
  L'asimmetria è voluta: un server che concede meno ha sempre ragione, uno che
  concede di più no — il `.env` è una scelta, e un tetto che si alza da solo
  è il modo di scoprirlo a budget speso. Le **riserve** si calcolano sul
  limite in vigore, altrimenti il 24 settembre quella delle formazioni
  varrebbe da sola diciotto giornate del gratuito. Il conto delle richieste si
  allinea a quello del server **solo verso l'alto**: un conto locale più alto
  è una richiesta in volo di un altro processo, e abbassarlo la ridarebbe via.
  E non si allinea su una risposta spedita ieri: col contatore che non scende
  mai, il conto di ieri portato nel giorno nuovo costerebbe la giornata
  intera.
  Ci scrivono **tre processi insieme** — il sito, la raccolta giornaliera e la
  corsa del quarto d'ora — e contare una richiesta è leggi-modifica-scrivi,
  quindi il lucchetto (`_blocco`) è un file creato con `O_EXCL`, l'unica
  primitiva che si comporta uguale su Windows e altrove. Se non lo prende
  **prosegue lo stesso**, ed è voluto: perdere un conteggio è un fastidio,
  bloccare la lettura delle formazioni mezz'ora prima del fischio costa una
  giornata di dati. Il 4 settembre 2026 quella scelta è stata trovata
  **esplosiva invece che innocua**, e la correzione vale la pena di essere
  raccontata perché è tutta di forma, non di logica. Su Windows un file appena
  cancellato resta «in cancellazione» per un istante, e in quella finestra
  aprirlo non dà `FileExistsError` ma un permesso negato: finiva nel ramo
  «filesystem che non collabora», che usciva dalla sezione critica **subito e
  senza dire niente** (8 volte su 3192 prese; il warning del timeout non è mai
  scattato). I due scrittori che entravano insieme si pestavano poi lo stesso
  file provvisorio, e ne usciva un `PermissionError` che **nessun chiamante
  cattura** — `client.py` chiama `spend()` prima di spedire e chi chiama prende
  solo `BudgetExhausted`. Da qui le tre regole di oggi: un permesso negato è un
  lucchetto **occupato** e non un guasto (lo si distingue solo aspettando —
  nemmeno `exists()` sa rispondere in quella finestra), il file provvisorio ha
  un nome per **scrittore** così il ripiego resta innocuo com'era promesso, e
  il ramo del guasto vero adesso **lo dice**. La terza regola non c'entra col
  lucchetto ed è la più pericolosa: i **lettori** stanno fuori dalla sezione
  critica — `client.py` scrive `budget.used` nel log a ogni richiesta, `/stato`
  lo mostra a ogni pagina — e su Windows chi tiene aperto un file fa fallire
  chi lo sostituisce (con quattro lettori addosso, 2932 scritture su 3000
  fallivano). Perciò lettura e rinomina **insistono** (`_riprovando`), e `_load`
  separa «non c'è» da «non si riesce»: solo il primo è uno zero, il secondo è
  una rinuncia — e metterli nello stesso ramo faceva rispondere «usate zero
  richieste», cioè regalare un budget giornaliero intero. Il limite è
  dichiarato e non nascosto: sotto quattro lettori in ciclo stretto — che
  nessun processo di questo progetto fa — un conteggio su cinque si perde
  comunque, sempre dicendolo.
- `cache.py` — risposte grezze su disco (`data/cache/`); TTL per endpoint, una
  stagione conclusa non scade mai.
- `daily.py` — la stagione in corso si raccoglie in **due modi diversi**, e la
  differenza è la sola cosa da ricordare. Il **calendario** — chi gioca contro
  chi, e quando — arriva intero in **una richiesta**
  (`collect_season_calendar`, `/fixtures?league=&season=`): non è deperibile,
  cambia con settimane di preavviso, e da quando c'è il sito mostra la giornata
  vera invece di un vuoto e i pronostici si registrano con giorni di anticipo
  invece che con ore. Gli **stati** — una partita che comincia, un risultato
  che arriva — no: si leggono con `/fixtures?date=...`
  (senza `league`, altrimenti l'API pretende `season` e blocca), finestra di
  **tre giorni**.
  Il calendario è l'unica richiesta della raccolta che passi dal filtro
  `league+season`, cioè esattamente ciò che il piano gratuito chiude: il giorno
  del downgrade smetterà di rispondere — con **zero partite e nessun errore**,
  non con un guasto — e tutto il resto continuerà identico, perché è così che
  si è sempre lavorato. Quante volte chiederlo non lo decide un orologio ma il
  TTL di **un giorno** (`TTL_CALENDARIO_STAGIONE`): le quattro raccolte
  quotidiane lo chiedono tutte, la prima lo paga e le altre lo leggono dalla
  cache. Fuori dalle raccolte piene non entra mai — le corse a ridosso del via
  valgono quanto sono vicine al fischio, e infilarci dentro una cosa senza
  fretta è il modo di far saltare quella che ce l'ha.
  La finestra separa deliberatamente due cose: gli **id delle partite**,
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
  raccolti. Da quando le sorgenti che ci scrivono sono **due**,
  `upsert_fixtures` ha un `mai_regredire`: una partita già conclusa in archivio
  non viene sostituita da una versione in cui conclusa non è. Serve perché il
  calendario di stagione passa da una cache di sei ore e può quindi essere più
  vecchio dell'archivio — una lettura del pomeriggio riscriverebbe come «da
  giocare» le partite della sera, e questa cartella è l'unica copia esistente.
  È la regola già scritta in `dataset.py` per la lettura (vince la raccolta
  giornaliera) applicata in scrittura: **una partita non torna mai indietro**.
  Il default non cambia, ed è voluto: la finestra di tre giorni è sempre la più
  fresca, ed è il chiamante nuovo che ha bisogno della protezione. Un giorno viene chiuso — cioè non richiesto più — solo se è
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
  parta davvero. Quella correzione copriva però solo il dettaglio **pieno e
  sbagliato**, e il 28 settembre 2026 si è scoperta l'altra metà (difetto
  Q1): una copia **vuota** in cache fa lo stesso danno. La lettura delle
  formazioni prima del via usa la stessa chiave di cache della raccolta, e
  quando la distinta non è ancora uscita ci lascia una risposta vuota che il
  TTL infinito serve per sempre — tre partite si sono arrese così senza una
  sola richiesta vera, e anche i tentativi dopo un primo vuoto vero venivano
  serviti dalla cache. Da allora si scavalca la cache quando esiste già una
  busta o quando la copia è vuota (`_vuota_in_cache`); una copia **piena** si
  usa, perché è ciò che permette di ricostruire un archivio senza ripagarlo.
  La stessa forma una terza volta, il 30 agosto 2026, e stavolta la colpa non
  e' dell'API: le **formazioni** si pubblicano un pezzo per volta, e
  `data/ufficiali.py` deve per forza leggerle prima del fischio d'inizio —
  dopo non servirebbero piu' a niente. A quindici minuti dal via capita di
  prendere una distinta ancora in costruzione: il 29 agosto e' successo su
  **tre partite su quattro**, con Fiorentina-Frosinone a dieci titolari per
  parte e Juventus-Parma con la panchina proprio assente. La lettura
  anticipata va fatta lo stesso e va salvata lo stesso; a non andare bene era
  che quella fosse l'ultima parola. `formazione_provvisoria` la rimette in
  coda, e la raccolta del dopopartita la sostituisce con quella intera.
  I segnali sono **meno di undici titolari** e **panchina vuota**, e non c'e'
  una soglia sul numero di riserve: il Monza in Inter-Monza del 22 agosto ne
  ha nove in una distinta letta a partita finita, cioe' vera, e una soglia la
  marcherebbe come rotta per sempre. Il tetto e' piu' basso di quello dei
  tabellini (`MAX_FORMAZIONE_ATTEMPTS = 8`) e il perche' e' l'unica cosa
  delicata: il contatore dei tentativi e' **uno solo**, e le letture prima del
  via lo hanno gia' fatto salire — fino a cinque, nella finestra dei 75
  minuti. Un tetto stretto verrebbe consumato dalle letture che il guasto lo
  hanno causato invece che da quelle che lo curano; uno largo si
  moltiplicherebbe per le ~380 partite di un riempimento storico.
- `storico.py` — il riempimento di una stagione passata. **Ha già fatto il suo
  lavoro**: le stagioni 2022, 2023, 2024 e 2025 sono in archivio complete di
  tabellini, formazioni, eventi e statistiche, comprate fra il 24 e il 25
  agosto 2026. Resta perché il giorno di un altro piano si rifà, e perché il
  perché delle sue scelte vale ancora. Prima `/status` (l'unico endpoint che
  dice in chiaro che piano è attivo: una stagione non coperta non dà errore,
  dà zero partite), poi il calendario in **una** richiesta, poi i dettagli.
  Dentro il triennio 2022-2024 funziona anche sul piano gratuito — il muro è
  il filtro `league+season`, non gli endpoint per partita: è così che i
  tabellini si sarebbero potuti prendere gratis, a ~1140 richieste a stagione
  e mesi di pazienza, ed è la ragione per cui vale la pena ricordare la
  distinzione. Le partite
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
  A queste se ne sono aggiunte due, nate col calendario di stagione: si
  registra solo ciò che si gioca entro **otto giorni** (`GIORNI_DI_REGISTRO`) e
  solo se l'1X2 si è mosso di almeno mezzo punto percentuale
  (`SOGLIA_MOVIMENTO`). Prima non servivano, perché «le partite in programma»
  erano quelle che la finestra di tre giorni riusciva a vedere: una decina. Ora
  sono 370, e le ristime in una stagione sono ~380 — senza i due freni farebbe
  decine di migliaia di righe, per una partita di maggio ri-pronosticata
  trecento volte. Il primo freno è sul *quando*: un pronostico di maggio scritto
  in agosto non è informazione, il modello non ha ancora visto le partite che
  glielo faranno cambiare. Il secondo è la regola di `vale_la_pena` in
  `pesi.py`: una riga identica alla precedente non è inutile, è dannosa —
  dirà per sempre che lì il modello aveva cambiato idea. Ognuno dei due ha il
  suo contatore nell'esito (`oltre_orizzonte`, `fermi`) perché un freno che non
  si vede è un freno di cui nessuno si fida, e «fermo» non si confonde con
  «già presente»: il primo vuol dire che il modello è cambiato e il pronostico
  no, il secondo che il modello non è cambiato affatto.
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
  Ogni misura guarda anche **una partita per volta**, e questa e' la regola
  gemella che per un anno e' mancata. Il registro scrive una riga per ogni
  versione del modello, quindi una partita vista con qualche giorno di
  anticipo ne accumula quante sono le ristime che la precedono — Genoa-Lecce
  del 22 agosto 2026 ne aveva **cinque**, scritte fra le 10:13 e le 21:22
  dello stesso giorno, e la prima dichiarava 2.76 gol attesi in casa contro
  gli 1.60 su cui il modello si e' poi assestato. Sono cinque tentativi sulla
  stessa domanda, non cinque osservazioni: mediarli conta quella partita
  cinque volte e da' alla stima acerba il peso di quelle mature. Il taglio sta
  in `esiti_risolti` (una riga per **partita e modo**, l'ultima prima del via,
  con lo stesso ordinamento di `ultimo_per_partita`) e il default e' quello
  sicuro apposta: il difetto e' nato perche' la cosa giusta andava chiesta
  invece di essere data. Chi vuole tutte le righe passa da
  `tutte_le_versioni=True`, che serve a guardare *come* il modello ha cambiato
  idea — non e' una misura. Il danno non era la media un po' storta ma il
  **silenzio**: gonfiando `n` faceva scavalcare `SOGLIA_SIGNIFICATIVITA`, e
  con dieci partite in archivio il sito dichiarava «meglio della baseline» su
  ogni mercato. Corretto il 25 agosto 2026; il conto vero e' che Gol/NoGol e
  Porta inviolata tornano a dire «troppi pochi dati», e Gol/NoGol da vincente
  diventa perdente contro la sua baseline. Il registro `/pronostici` continua
  a mostrarle tutte, ed e' giusto: li' la domanda non e' quanto vale il
  modello ma cosa fu scritto.

  Ogni misura guarda **un solo tipo di pronostico alla volta**
  (`MODO_PREDEFINITO`, cioè il secco). Non è un'opzione ma un vincolo: una
  partita può averne due — il secco e quello rifatto con le formazioni
  ufficiali — e sono due risposte alla stessa domanda, non due osservazioni
  della stessa cosa. Sommarle conta ogni partita due volte, fa dire "partite"
  a un conteggio di pronostici (`brier_1x2_multiclasse`), mette due pronostici
  contro una sola quota di chiusura, e soprattutto media fra loro proprio i
  due Brier che il lavoro delle formazioni esiste per confrontare. Il default
  è il secco perché è l'unico che esiste per **ogni** partita: l'ufficiale
  compare solo dove i pesi c'erano e spostavano qualcosa, quindi su un
  sottoinsieme che si allarga da solo — usarlo come numero di riferimento
  vorrebbe dire cambiare popolazione ogni settimana. Il confronto vero sta in
  `confronto_fra_modi`, che li mette accanto **solo sugli esiti che hanno
  entrambi**, con la stessa regola già usata contro le quote: misurarli su
  popolazioni diverse direbbe quale gruppo di partite era più facile da
  indovinare, non quale pronostico era migliore. Oggi tutto questo non si vede
  — di righe ufficiali non ce n'è ancora nessuna — ed è il punto: la regola è
  stata scritta *prima*, perché il giorno dell'accensione i numeri
  raddoppierebbero senza che niente lo segnali.
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
  Dall'11 settembre 2026 si salvano anche le **quote sui marcatori**
  (`parse_marcatori`, tabella `quote_marcatori`). Arrivavano già nella stessa
  risposta pagata e venivano buttate, e come tutte le quote non tornano. Stanno
  in una tabella loro per tre ragioni: non hanno una probabilità («chi segna»
  non è un gruppo esaustivo — possono segnare in tre o nessuno — quindi il
  margine non si toglie normalizzando), non hanno una selezione di
  `righe_mercati` con cui agganciarsi, e nominano il giocatore **per nome**,
  senza id: abbinarlo all'archivio è lavoro di chi le userà, non di chi le
  salva. Le offre Bet365 su tutte le partite e Betano solo nelle varianti
  casa/trasferta: uno o due bookmaker, quindi di consenso non si parla.
  Arrivano anche sul **piano gratuito** — le letture del 18-23 agosto 2026,
  prima del Pro, le contengono già. Sono la base del lavoro sui probabili
  marcatori: il metro di paragone, come le altre quote lo sono per l'1X2.
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
- `pesi.py` — quanto pesa un giocatore, misurato da cio' che ha **fatto
  davanti** (gol, assist e **tiri** per minuto, vedi `produzione`), non da come
  va la squadra quando c'è o non c'è: quella strada è rumore travestito da
  misura, perché un titolare salta cinque partite l'anno e sta fuori proprio
  nei periodi storti. I tiri ci sono per una ragione misurata, non per
  completezza: un titolare produce 0,08 gol a partita, e su quaranta episodi in
  una stagione non si distinguono quattro giocatori. Il tiro è lo stesso gesto
  un passo prima, capita otto volte più spesso, e indovina meglio la produzione
  futura — r da 0.645 a 0.728 su quattro stagioni, con lo shrinkage già
  applicato e il vantaggio positivo in tutte e quattro
  (`scripts/prova_pesi.py`, 24 agosto 2026). `PESO_PRODUZIONE = 2` è scelto
  con la regola del progetto (il peso migliore sulle *altre* stagioni, poi
  misurato su quella tenuta fuori) e l'ottimo è **piatto fra 1 e 3**. Il
  **bersaglio** però non cambia mai: restano gol e assist ciò che si vuole
  prevedere, i tiri sono il modo meno rumoroso di arrivarci. Due cose provate e
  scartate: i **passaggi chiave** non aggiungono niente sopra i tiri (0.701
  contro 0.711) e da soli sono molto peggio (0.504); i **tiri da soli** perdono
  contro la somma (0.711 contro 0.728), quindi l'esito qualcosa aggiunge. Il
  tasso di ognuno
  passa per uno **shrinkage** verso la media della squadra pesato sui minuti —
  senza, chi entra dieci minuti e segna varrebbe più di tutta la squadra. Due
  regole di onestà: sotto `MINIMO_PARTITE` partite con tabellino non si stima
  niente e si restituisce `None`, che significa "non lo so" e non deve mai
  diventare 1.0, che significa "so che non cambia"; e un titolare sconosciuto
  all'archivio vale la produzione **media** di un titolare, non zero — non
  sapere nulla di lui non è sapere che non vale niente. Terza regola, arrivata
  il 30 agosto 2026: i titolari passati a `rapporto_produzione` devono essere
  **undici**, altrimenti la risposta è `None`. Il denominatore è la produzione
  di undici giocatori, quindi un numeratore che ne somma dieci non dà un
  rapporto più basso, dà un rapporto fra cose diverse — e non lo segnala
  nessuno, perché `affidabile` conta i giocatori *sconosciuti*, non quelli
  mancanti: una distinta a dieci ha esattamente l'aspetto di una completa. Il metodo **vede gli
  attaccanti, non i difensori**, ed è un limite da dichiarare in pagina, non da
  compensare con un'ipotesi — i tiri **non** lo curano, misurano chi sta
  davanti esattamente come gol e assist. `FATTORE_PREDEFINITO` è stato
  **misurato l'11 settembre 2026** (`backtest.py --cerca-fattore`) ed è
  piatto: vedi `ufficiali.py` qui sotto.
- `ufficiali.py` — il pronostico rifatto quando escono le formazioni, in
  `MODE_UFFICIALE` **accanto** a quello secco e mai al suo posto: due
  pronostici sulla stessa partita sono l'unico modo di scoprire se il lavoro
  serve, confrontando i Brier fra qualche mese.
  **Dall'11 settembre 2026 è spenta per misura** (`MACCHINA_ACCESA = False`),
  prima ancora di scrivere una riga. Rigiocate quattro stagioni
  (`backtest.py --cerca-fattore`, 1124 partite), il pronostico con le
  formazioni non batte il secco: fattore migliore 0.25, 0.58 errori standard
  nella prova appaiata, e **peggio** del secco proprio sulle formazioni più
  lontane dal solito (+1.22). I pesi puntano nella direzione giusta ma
  spostano troppo poco — una formazione si scosta da quella tipo del 6% in
  mediana — e non vedono difensori e portiere. Il confronto dal vivo non
  sarebbe mai arrivato a un verdetto. Spenta, legge comunque la distinta
  **una volta per partita**, appena è completa: serve ai probabili marcatori
  e all'archivio, e rileggerla a ogni giro (la cache delle formazioni dura
  cinque minuti) sarebbe sul gratuito una manciata di richieste buttate per
  partita. Il resto di questo paragrafo descrive la macchina accesa, e resta
  vero per il giorno in cui un metodo nuovo la riaccendesse — rifacendo prima
  la misura. Non scrive **nessuna riga**
  finché i pesi non esistono o non spostano niente (`vale_la_pena`): i
  pronostici sono immutabili, e una riga identica a quella secca non sarebbe
  inutile ma dannosa — direbbe "misurato" dove non c'era niente da misurare.
  È anche ciò che tiene la macchina spenta finché i tabellini non bastano, da
  sola e senza che nessuno debba accenderla. Una risposta di formazioni
  **vuota non si salva**: farebbe scattare `MAX_DETAIL_ATTEMPTS` e le
  formazioni vere, quelle di fine serata, non verrebbero più chieste. Una
  **a metà si salva e non si usa**, ed è l'altra faccia della stessa trappola:
  l'archivio la vuole (`formazione_provvisoria` in `data/store.py` la rimette
  in coda per il dopopartita), il pronostico no. Dieci titolari su undici
  fanno scendere la produzione in campo della quota di chi manca — un
  undicesimo per un titolare qualunque — per **pura aritmetica**: abbastanza
  da far scattare `vale_la_pena` e da spostare i gol attesi, e
  indistinguibile da un'assenza vera. Quella riga direbbe per sempre che il
  modello aveva visto mancare qualcuno che invece giocava, e i pronostici non
  si cancellano. Si salta, si conta (`formazioni_a_meta` nell'esito) e si
  riprova al giro dopo: l'attività pianificata passa ogni quarto d'ora e nella
  finestra dei 75 minuti ci sono cinque occasioni. Se non si completa in tempo
  quella partita resta col solo pronostico secco — che è la regola di sempre:
  meglio un buco che una riga scritta male.
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
- `panchine.py` — chi allenava chi, e da quando. Come `players.py` non chiede
  niente all'API: l'allenatore sta già dentro ogni busta `/fixtures/lineups`,
  con un **id** che è la sua identità (il nome no: l'archivio contiene sia
  "Ivan Jurić" sia "Ivan Juric"). Serve a una cosa sola e piccola: il modello
  pesa una partita solo per l'**età**, quindi una squadra che ha cambiato
  allenatore tre giornate fa si porta dietro il girone precedente a peso pieno
  come se fosse la stessa squadra. Da qui `_pesi_allenatore` in
  `model/dixon_coles.py`, che moltiplica per un fattore < 1 il peso delle
  partite giocate sotto un'altra guida. **Quel fattore è misurato e vale 1.00,
  cioè la correzione è spenta**: rigiocate quattro stagioni (25 agosto 2026,
  `backtest.py --cerca-allenatore`), il Brier 1X2 peggiora *monotonicamente*
  allontanandosi da 1 — 0.5968 a fattore 1.00, 0.6003 a fattore 0.00 — e vale
  in tutte e quattro separatamente. La ragione è che la correzione fa due cose
  insieme: toglie una squadra che non esiste più e toglie **partite vere**. La
  seconda si paga sempre, la prima rende solo se il cambio sposta la forza più
  di quanto quelle partite insegnino — e non lo fa, anche perché lo shrinkage
  già tira verso la media chi ha poche partite recenti, e l'esonero arriva dopo
  una serie anormalmente brutta che rientrerebbe da sola. Il codice resta
  perché è **un parametro solo** e la misura si rifà in venti minuti quando i
  dati saranno di più; chi lo rialza deve rifare quella misura, non fidarsi di
  questa. Due trappole coperte da test: una partita **senza formazioni non
  spezza** un tratto di panchina ("non lo so" non è "è cambiato", e inventare
  esoneri farebbe dimenticare al modello mezzo campionato), e il
  **traghettatore** di una giornata — il vice per una squalifica — è un tratto
  vero e non si ricuce con una soglia inventata sulla lunghezza; se capita di
  essere l'ultima partita nota, quella squadra pesa meno per una giornata e
  torna com'era.
- `marcatori.py` — chi segna (lavoro 10, 11 settembre 2026). Non è un modello
  nuovo: la matrice dice già quanti gol farà la squadra, qui si stima solo la
  **quota** di quei gol che spetta a ciascun giocatore, e la probabilità che
  segni è una somma pesata di celle della stessa matrice (`player_scores` in
  `model/markets.py`: in un risultato con k gol della sua squadra segna con
  probabilità 1 − (1 − quota)^k). È la regola dei mercati applicata a un
  giocatore — non può segnare dove la sua squadra resta a zero. La quota è il
  suo tasso (gol veri e gol «da tiri», metà e metà, tirato verso la media del
  ruolo) per i minuti attesi, diviso i gol **veri** della squadra: autogol a
  favore compresi, perché la matrice li conta. Due modi, perché le formazioni
  escono quaranta minuti prima del via: coi titolari noti, e prima delle
  formazioni (chi ha giocato nelle ultime cinque partite, meno gli assenti
  certi). `StoriaMarcatori` risponde sempre «prima di un istante», ed è ciò
  che rende onesta la misura. I parametri sono **scelti a priori** e non
  cercati. Misurato con `backtest.py --marcatori` sul 2023-2025, log loss su
  24926 titolari: tutti uguali 0.2900, ruolo 0.2618, il giocatore 0.2531, il
  giocatore dentro la matrice 0.2524 — **+13.0%**, fra 11.6% e 14.0% per
  stagione; prima delle formazioni +11.5%, coi candidati che coprono il 97.3%
  dei gol. Un candidato di prima delle formazioni deve aver giocato la sua
  **ultima partita con quella maglia** (`squadra_attuale`): l'11 settembre
  2026 Gaetano compariva sia nell'Atalanta sia nel Cagliari, ceduto a fine
  mercato ma ancora nelle ultime cinque partite di chi l'aveva venduto. Due cose da non perdere. **La partita aggiunge pochissimo** al
  giocatore (0.2531 → 0.2524), coerente con quanto già misurato sui gol: il
  valore sta nel sapere chi segna di solito, non contro chi. E il pronostico
  **sovrastima di circa il 7%** (2273 marcatori dichiarati contro 2110),
  spalmato su tutte le fasce: non è corretto, perché una correzione è un
  parametro e va scelta col metodo del progetto. Il 28 settembre 2026 quel
  metodo è stato applicato (`backtest.py --cerca-correzione-marcatori`, il
  parametro è `CORREZIONE_QUOTA`) e la correzione **non passa**: il fattore
  migliore sta fra 0.92 e 0.94 in tutte e tre le stagioni, ma la prova
  appaiata per partita si ferma a −1.86 errori standard coi titolari e −0.90
  prima delle formazioni. È il caso più vicino alla soglia mai visto qui, con
  una forma stabile, e si rimisura a fine stagione insieme a P1. Il metro
  vero è il bookmaker (`scripts/marcatori_contro_quote.py`): sulle 46 partite
  2026 con le quote, matrice congelata e titolari veri, il pronostico
  **pareggia** con Bet365 anche riscalata a posteriori — 0.2825 contro 0.2849
  di log loss, −0.66 errori standard — ma su 85 gol è un aneddoto, e lo
  script non dichiara un verdetto sotto i 200. I nomi del bookmaker si
  abbinano agli id dentro le due distinte della partita (88.6% dei titolari;
  fuori quasi solo portieri, che Bet365 non quota).
- `notifiche.py` (fuori da `data/`, perché non è una sorgente ma un'uscita) —
  i messaggi su Telegram. Quattro regole, tutte difensive: **il silenzio è il
  default** (senza token il sistema gira identico), **non solleva mai** (un
  messaggio perso è un fastidio, una raccolta interrotta può costare un giorno
  di partite), si decide **dal contenuto e non dall'orologio** — nessun
  messaggio è legato a un orario, si guarda cosa c'è sul disco e cosa non è
  ancora stato detto, ed è così che il resoconto delle 23:45 arriva la mattina
  dopo se il computer era spento — e **niente si manda due volte**
  (`RegistroInvii`, un JSON in `data/notifiche.json`). Si mandano tre messaggi
  che si vogliono leggere (i pronostici del mattino, com'è andata la sera, e i
  **tabellini di una giornata arrivati tutti**) e sei allarmi che si devono
  leggere. Sui tabellini i messaggi sono **due e opposti**, e una giornata ne
  prende uno solo. `tabellini_fermi` arriva **tardi apposta**: un tabellino
  provvisorio nelle ore dopo la partita
  e' normale e si risolve da solo, e dirlo ogni sera insegnerebbe a ignorare il
  messaggio. Parte quando i tentativi sono finiti (`tabellini_arresi` in
  `data/store.py`), cioe' nell'unico momento in cui il sistema ha smesso di
  poter rimediare da solo. `tabellini_pronti` è il caso opposto e vale la pena
  dirlo perché è il momento in cui quelle partite **cominciano a servire**: il
  risultato c'era già, minuti/gol/assist/voti no, e sono quelli che alimentano
  `players.py` e `pesi.py`. Un dettaglio che sembra un dettaglio e non lo è:
  quel messaggio **non** usa la regola di `giorno_da_riepilogare` («il più
  recente, e se è già stato detto fermati»). I risultati arrivano in ordine, i
  tabellini no — il 22 agosto 2026 due partite del sabato erano ferme mentre
  la domenica era a posto — e fermarsi al più recente lascerebbe il giorno
  rimasto indietro senza annuncio **per sempre**, cioè proprio quello che si
  stava aspettando. Si guardano quindi tutti i giorni di una finestra di
  sette, che è ciò che impedisce alla prima esecuzione di annunciare l'intera
  stagione; le ~90 esecuzioni quotidiane
  che non fanno niente restano mute, perché dirle insegnerebbe a ignorare le
  altre. Il modello non entra nel modulo: `invia_quel_che_serve` riceve i
  pronostici come **funzione**, così si decide se c'è qualcosa da dire prima
  di pagare i due secondi della stima.
  Una regola che vale per entrambi i messaggi di fine giornata, nata il 24
  agosto 2026: **un giorno si giudica su tutte le partite in calendario**, non
  su quelle già cominciate. Quella sera `giorno_da_riepilogare` e
  `giorni_coi_tabellini_pronti` filtravano per `kickoff_utc <= ora`, quindi
  Roma-Fiorentina delle 20:45 non entrava nel conto e la domenica risultava
  finita alle 20:32 con la sola Bologna-Lazio. Il messaggio però elenca tutte
  le partite della data: alle 20:48, con la Roma al primo minuto, su Telegram
  è arrivato «tutte e 2 le partite hanno il tabellino definitivo» con la Roma
  in elenco — chi decide e chi scrive guardavano due insiemi diversi, e il
  disallineamento produce una bugia. Il danno peggiore però è il seguito: la
  chiave finiva nel registro e il messaggio vero — quello col posticipo — non
  sarebbe arrivato **mai più**. Una partita che deve ancora cominciare vale
  esattamente come una in corso: il giorno non è finito.
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
quella di `data/storico/` **più** quella di `data/importato/` **più** quella
dei dettagli), non a ogni richiesta HTTP — la stima costa un paio di secondi. I pezzi oltre il primo
servono al riempimento storico e all'importazione: senza, il modello resterebbe
fermo alle stagioni di prima anche dopo aver ricomprato o importato un
campionato intero, con il sito aperto e nessun motivo apparente per riavviarlo.
L'ultimo pezzo — i dettagli — è entrato quando il modello ha cominciato a
guardare chi sedeva in panchina (`data/panchine.py`): prima le formazioni erano
roba delle sole pagine dei giocatori, che hanno un'impronta loro, e una stima
che non se ne accorgesse resterebbe ferma all'allenatore di prima. Un lock
protegge da stime duplicate su richieste concorrenti.

L'impronta si rilegge **a ogni richiesta HTTP**, e la tentazione di tenerla in
caldo qualche secondo va scartata: ritarderebbe la ristima su un risultato
appena arrivato, cioè proprio la cosa che qualcuno può stare aspettando con la
pagina aperta. Si paga rendendola economica, non vecchia: `details_signature`
(`data/store.py`) legge le ~1530 cartelle dei dettagli con **`os.scandir`** e
non con `iterdir()` più `stat()` — una chiamata di sistema per cartella contro
dati che `scandir` si porta già dietro, misurati 30 ms contro 1 ms. Non si
guarda invece la data della sola cartella `details`, che sarebbe ancora più
rapida e perderebbe il caso che conta: un tabellino **riscritto** non crea
nessuna cartella. Funziona perché il salvataggio è atomico (file temporaneo e
`replace`), e quel rename tocca la cartella che lo contiene.

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
espone hub, scheda partita, `/pronostici` (il registro riga per riga),
`/giornata/<n>?stagione=<anno>` e `/calendario`,
`/giocatori` (marcatori, assist, voti),
`/giocatore/<id>`, `/squadra/<id>`, `/stato` (JSON: cosa sa il sistema, budget
residuo, pronostici registrati, esiti risolti) e `/aggiorna` (forza la
ricostruzione). `/aggiorna` e' un **POST** e non un GET, ed e' voluto:
ricostruire non guarda soltanto — ristima il modello, registra i pronostici
delle partite in programma e risolve gli esiti — e un GET che scrive lo
esegue da solo il precaricamento del browser. Da terminale vuole quindi
`curl -X POST`; dal sito e' il bottone della testata, che dopo il lavoro
rimanda alla pagina da cui si e' partiti.

Due campi di `/stato` esistono per dichiarare un **guasto** e non un dato, e
tutti e due compaiono come avviso in cima a ogni pagina quando scattano.
`modello_convergente` dice se l'ottimizzatore ha raggiunto l'ottimo o ha
sbattuto contro `max_iterations`: senza, un modello non convergente
produrrebbe pagine con la faccia di sempre, contro la regola di non nascondere
i limiti del modello. Le percentuali restano visibili anche allora, ed è
voluto — nasconderle direbbe «non lo so» al posto di «lo so male».
`registro_leggibile` distingue un registro **vuoto** da uno **illeggibile**:
`_conteggi` restituisce `None` e non zero, perché «0 pronostici registrati»
sarebbe un fatto osservato che nessuno ha osservato. È la stessa regola di
`pesi.py`, dove `None` non deve mai diventare 1.0.

Il foglio di stile viaggia con una versione (`style.css?v=<mtime>`,
`versione_statici` in `web/app.py`), e non è un vezzo: senza `Cache-Control`
esplicito i browser applicano una scadenza inventata da loro, e un CSS appena
modificato può non essere richiesto affatto — è successo il 3 settembre 2026,
con la pagina delle giornate arrivata all'utente senza i suoi stili mentre sul
server erano giusti. Si rilegge a ogni pagina e non a un avvio solo, altrimenti
un sito acceso per giorni tornerebbe a servire la versione vecchia dopo ogni
ritocco. Dal 4 settembre 2026 il numero non è più l'mtime del solo `style.css`
ma **il più recente di tutta la cartella `static/`**: da quando i file sono
due, col vecchio criterio un logo sostituito sarebbe rimasto quello di prima
finché nessuno toccava il CSS — cioè esattamente la trappola che quella riga
esiste per chiudere. È un numero solo per tutti i file e non uno per file: si
paga una richiesta di troppo ogni tanti mesi e si toglie di mezzo un argomento
che qualcuno prima o poi dimenticherebbe di passare.

Il colore della pagina è stato **rifatto il 4 settembre 2026**, ed è un
cambio di sole tinte: nessun riquadro spostato, nessuna tabella cambiata di
forma. Tutte le tinte del sito vivono in una quarantina di variabili in
`:root` più il blocco `prefers-color-scheme: dark`, e nei template non c'è un
solo colore scritto a mano — è quello che rende il restyle una riscrittura di
variabili e non un rifacimento. L'alfabeto dei fatti era indaco/grigio/arancio
e ora è **grafite = 1, cenere = X, magenta = 2**: la scala va dal piombo al
colore invece che da una tinta a un'altra, cioè spende **un colore vivo solo**
in tutta la pagina. La conseguenza è voluta: con tutto il resto in carta e
inchiostro, il verde e il rosso del giudizio diventano l'unica altra tinta che
si vede. Quei colori **non sono cambiati** ed è una richiesta esplicita
dell'utente — sono l'unica cosa che il lettore ha già imparato a leggere.
Dal 7 settembre 2026 però non escono più da `:root`: il giudizio è disegnato
con due emoji (vedi «le due palline» qui sotto), quindi il verde e il rosso li
sceglie il sistema operativo. È l'unico punto del sito in cui un colore non è
una variabile, ed è dichiarato qui perché una regola con un'eccezione taciuta
non è più una regola. L'oro se n'è andato con la pastiglia a tre gradini, e
con lui le variabili `--esatto` e `--parziale`; `--allarme` resta, lo usa il
muro dei Brier. L'arancio non torna comunque: era il vicino di casa di
quell'oro, e a undici pixel le due colonne devono restare distinguibili.
Chi tocca la palette doveva fin qui cambiare anche le **parole**, perché
quattro template nominavano i colori a voce («grafite ha vinto la casa,
magenta l'ospite»). Dal 9 settembre 2026 quel vincolo non c'è più, e non
perché qualcuno l'abbia allentato: le spiegazioni sono uscite dal sito (vedi
«le pagine mostrano i numeri» qui sotto) e con loro le frasi che nominavano i
colori. Il legame fra tinta e significato adesso vive in un posto solo, la
testa del foglio di stile, più `ISTRUZIONI.txt` per chi legge il sito. Chi
cambia la palette cambia due file, non sei.

**Le pagine mostrano i numeri, le istruzioni stanno in un file.** Deciso
dall'utente il 9 settembre 2026, ed è il cambio più grosso mai fatto
all'aspetto del sito: **86 paragrafi** tolti dai template e riscritti interi
in `ISTRUZIONI.txt`, che si legge quando serve. Ogni `.blocco` era due
colonne — a sinistra il titolo con la sua spiegazione, ferma mentre si
scorreva la tabella; a destra i dati — e adesso è una sola: il titolo passa
sopra, i dati prendono tutta la larghezza. La classe `.spiega` non esiste più,
né nei template né nel foglio di stile, e nemmeno `.chiave`, che disegnava le
legende sotto i grafici.

Quello che è rimasto in pagina è **un fatto sull'archivio, mai una
spiegazione**, e ha una classe sua (`.dato`) apposta perché il nome dica da
solo quando qualcosa è nel posto sbagliato: «30 partite col tabellino su 30
concluse», «tabellino non ancora definitivo», «troppi pochi dati», i tre
parametri misurati sotto le forze, la data in cui un pronostico fu congelato.
Sono rimasti anche i **limiti che il progetto pretende accanto al numero** —
il Gol/NoGol e l'Over/Under, gli indisponibili e la panchina che il pronostico
non usa, i pesi che non vedono i difensori — ma ridotti a una riga: il
ragionamento dietro sta nel file.

Due test tengono il confine (`test_i_template_non_contengono_spiegazioni`,
`test_il_file_delle_istruzioni_esiste_e_parla_di_ogni_pagina`), e il perché
sta scritto lì: quei paragrafi sono nati **uno per volta e ognuno con una
buona ragione**, ogni volta che qualcuno aveva in mano un fatto misurato e la
voglia di raccontarlo lì accanto. È esattamente così che ricomincerebbero. Il
posto dove raccontarlo adesso è `ISTRUZIONI.txt`, che va tenuto aggiornato
insieme alla pagina: un sito senza spiegazioni e un file che non le contiene
sono la stessa cosa che averle perse.

Il sito ha un **logo** (`static/logo.png`) e da lì un nome: **Il Pinguino
Fatto Meglio**. L'immagine sta in tre posti — la testata, `rel="icon"` e
`rel="apple-touch-icon"` — e il terzo non è di troppo: il sito si guarda dal
telefono via Tailscale, e su iOS è l'unica icona che Safari usa quando la
pagina finisce sulla schermata Home. Il nome è cambiato solo dove il sito lo
mostra (testata, `<title>` di ogni pagina, titolo FastAPI, banner di
`scripts/serve.py`, README); il **pacchetto resta `serie_a_analyzer`** e la
cartella `serie-a-analyzer`, perché rinominarli romperebbe import, percorsi
delle attività pianificate e ogni comando scritto qui sopra, in cambio di
niente.

**I template si rileggono dal disco a ogni richiesta, il codice Python no.**
Non è una scelta ma un fatto di Jinja, e va conosciuto perché ha morso: quel
3 settembre un `base.html` che chiamava una funzione nuova è entrato subito in
servizio nel processo acceso, che quella funzione non ce l'aveva, e ogni
pagina HTML ha risposto 500 mentre `/stato` — che è JSON e non passa da un
template — continuava a rispondere 200. Toccando un template servito da un
sito acceso, il riavvio non è facoltativo.
`/giornata` è ciò che rende sfogliabili le ~1900 partite in archivio, che
prima si vedevano solo dieci per volta dall'hub o dalla scheda squadra; è
quasi tutta codice che esisteva già, perché la regola di `_riga` (congelato
dopo il via, modello caldo prima) vale identica su una giornata del 2023 — che
infatti mostra i risultati e **trattini** al posto dei pronostici, perché
allora nessuno li scrisse. `/calendario` non è una pagina ma un rimando alla
giornata che si sta per giocare: chi ci arriva non deve sapere a che numero è
arrivato il campionato. La riga di quelle tabelle è una macro condivisa con
l'hub (`riga_risultato` in `forme.html`) e ha **tre** stati e non due: il
terzo, «da giocare», non esisteva perché l'hub mostra solo partite concluse, e
senza di esso una partita futura avrebbe una pastiglia vuota — che si legge
come «sbagliato».

In fondo alla riga il giudizio sta in **tre colonne** e non in una:
l'**esito** (il segno accaduto era quello che il modello preferiva) e il
**punteggio** (il risultato era fra quelli che aveva nominato) dal 7 settembre
2026, e dal 9 — su richiesta dell'utente — la sigla **a logica** (il segno
accaduto era fra quelli che nominava: uno se secca, due se col pareggio,
`esito_logico_azzeccato`). Ognuna è un cerchio pieno, verde o rosso — `pallina` in
`forme.html`, `pronostico_azzeccato` e `risultato_azzeccato` in
`web/views.py`. Prima era una pastiglia sola a tre gradini, verde/oro/rosso, e
a farla cadere è stata **una partita**: Juventus-Milan del 6 settembre 2026,
finita 1-1, col modello che dava l'1 al 45% e nominava 1-0 e 1-1 fra i
punteggi probabili. La pastiglia guardava il punteggio per primo, trovava
l'1-1 nella rosa e scriveva «azzeccato» in verde su una riga in cui il segno
era sbagliato. Non era un difetto di quel codice ma della domanda: una scala
sola deve mettere i due giudizi **in fila**, e in fila non stanno da quando i
punteggi nominati sono fino a cinque e **possono avere segni diversi fra
loro** — «il punteggio era fra quelli detti» non implica più «il segno era
quello». Due domande, due risposte, due colonne, e nessuna promuove l'altra.

Il resto della regola del 3 settembre 2026 vale identico, ed è quello che ha
reso possibile la discordanza: il verde del punteggio conta **ogni punteggio
nominato in pagina** e non solo il primo — prima della partita l'anteprima ne
scrive fino a cinque, tutti sopra il 10% e messi lì apposta per dire che fra
quelli il modello non sta scegliendo, e giudicare poi sul solo primo
cambierebbe la domanda a partita finita. La lista è **la stessa** che
l'anteprima mostra (`punteggi_probabili` in `web/views.py`), non una
ricalcolata con la stessa soglia: due liste sarebbero uguali oggi e
divergerebbero al primo ritocco, con la pagina che nomina cinque punteggi e la
pallina che ne premia quattro. Due conseguenze da non scambiare per guasti:
quella colonna è più facile di un confronto col solo punteggio più probabile,
quindi i verdi di prima del 3 settembre e quelli di dopo non si contano
insieme; e non è una misura del modello — quelle stanno in `/pronostici`, nel
Brier e nella calibrazione, e nessuna passa di qui. Il terzo stato della riga
resta uno solo e resta detto **a parole**: «da giocare» occupa tutte le
colonne del giudizio, perché cerchi spenti si leggerebbero come giudizi.

Che le colonne siano cresciute da una a tre senza toccare né il foglio di
stile né la macro `pallina` è la prova che la forma regge, e due regole la
tengono. `giudicabile` resta **una domanda sola** per tutte e tre: le tre
risposte hanno le stesse condizioni — partita finita e pronostico scritto in
tempo — e una condizione per colonna le farebbe divergere al primo ritocco.
E le tre **non hanno la stessa difficoltà**, che è la cosa da non dimenticare
leggendole: l'esito è un segno su tre, il punteggio fino a cinque celle, la
sigla a logica **due segni su tre** quando aggiunge il pareggio. Tre verdi in
fila non vogliono dire tre volte bravo, e `ISTRUZIONI.txt` lo dichiara in una
tabellina apposta. La terza colonna **non allenta il divieto del lavoro 9**:
disegnare un cerchio non è misurare, ma sommare quei cerchi lo sarebbe — il
divieto colpisce chi li contasse, e sta scritto nella docstring di
`esito_logico_azzeccato` perché in un test non ci starebbe.
Accanto ai punteggi probabili la pagina scrive anche **l'esito che il modello
sceglie** — 1, X o 2 — con la sua percentuale a fianco (`esito_previsto` e
`probabilita_esito_previsto` in `web/views.py`, sull'hub e sulla scheda
partita). Tre cose da sapere. Non è un mercato nuovo: è la stessa somma di
celle che già tingeva il favorito nella scheda squadra e che decide la pallina
dell'esito, e resta **una sola** — un secondo argmax scritto altrove sarebbe
uguale oggi e divergerebbe al primo ritocco, con la pagina che nomina un esito
e la pallina che ne premia un altro. La percentuale non è un
ornamento: il prescelto ha una mediana del 49% e in una partita su sette sta
sotto il 40% (misurato sui 1154 pronostici in registro il 5 settembre 2026),
quindi «1» da solo direbbe che il modello ha deciso mentre quasi sempre sta
indicando il meno incerto dei tre. E la **X non esce quasi mai** — zero volte
su 1154, perché il pareggio non ha mai passato il 33.5% — il che non è un
guasto ma una proprietà del calcio: chi la incontra deve poterla riconoscere
invece di andare a cercare il bug.

Accanto a quell'esito, dal 7 settembre 2026 e su richiesta dell'utente, la
stessa forma sul **Gol/NoGol**: «NG · 52%» (`gol_nogol_previsto` e
`probabilita_gol_nogol_previsto`). Non è un mercato nuovo — è la `prob.btts`
che la scheda partita già mostrava, detta come scelta invece che come
probabilità secca, e la cella grezza di prima è stata sostituita, non
affiancata. Ma una scelta ha un'autorità che una probabilità non ha, e qui il
modello sceglie pochissimo: misurato sui 1170 pronostici in registro il 7
settembre 2026, dice **NG 1071 volte su 1170** (il 92%), la P(GG) non ha mai
passato il 55.2% e la percentuale del prescelto sta in mediana al **52.4%**,
sotto il 55% in tre casi su quattro. C'è anche un pavimento aritmetico: GG e
NG sommano a uno, quindi il numero scritto **non può scendere sotto il 50%**
— è la stessa cosa che ha fatto scartare la doppia chance, un piano più in
basso. Da qui la regola che resta in pagina: la percentuale non si può
togliere, perché «NG» da solo si leggerebbe come una decisione presa.
L'hub dichiarava anche il limite a parole, accanto alle partite; **dal 9
settembre 2026 non più**, ed è una decisione dell'utente e non una svista —
quel paragrafo era fra quelli che ha chiesto di togliere per nome. Il limite
resta dichiarato nella **scheda partita**, accanto all'Over/Under e con le
parole di sempre, e per esteso in `ISTRUZIONI.txt`; il test
`test_il_limite_del_gol_nogol_e_dichiarato_dove_stanno_i_numeri` sorveglia
quello che è rimasto. Chi rimette una riga sull'hub non stia sbagliando: sta
tornando indietro su una scelta presa, e va chiesto prima.
La sigla **non prende il velo colorato** dell'esito, ed è la regola del colore
applicata: grafite, cenere e magenta vogliono dire casa, pareggio e trasferta,
e tingere «NG» con una di quelle direbbe una terza cosa in un alfabeto che ne
dice due.

Accanto a quei due, dal 9 settembre 2026 e su richiesta dell'utente, una terza
sigla: l'**esito a logica** (`esito_logico`, `distacco_fra_le_squadre` e
`SOGLIA_ESITO_LOGICO` in `web/views.py`). Guarda una cosa sola — quanto si
distaccano le **due squadre**, `|P(1) − P(2)|`, col pareggio fuori dal conto —
e scrive «1» o «2» quando sono lontane, «1X» o «X2» quando sono vicine. Non è
un mercato nuovo e nemmeno un secondo pronostico: è l'esito matematico
raccontato a voce umana, e se il modello cambia idea cambia idea anche lui,
senza che nessuno lo ritocchi.

Tre cose lo tengono al suo posto, e la prima è la sola che conti davvero.
**Non entra in nessuna misura**: niente `righe_mercati`, niente registro,
niente `outcomes.py`, nessun Brier. Un secondo esito in pagina ha l'aspetto di
un secondo pronostico, e il giorno in cui qualcuno lo salvasse «per misurarlo»
ogni partita avrebbe due risposte alla stessa domanda contate come due
osservazioni — è il difetto A1, che costò un anno di misure sbagliate; il
divieto è scritto in un test apposta, perché è l'unico posto in cui può
stare. Seconda: **non ha una percentuale accanto**, al contrario delle altre
due sigle, e l'assenza è voluta — una cifra lo farebbe sembrare misurato.
Terza: niente velo colorato, per la ragione già detta sopra.

Dal 9 settembre la sigla ha però **una pallina sua** in fondo alla riga, come
l'esito e il punteggio, e il confine va detto con precisione perché è sottile:
disegnare un cerchio su una partita non è misurare — le altre due colonne già
non lo sono — mentre **sommare quei cerchi lo sarebbe**, e sarebbe anche un
conto gonfiato, perché dove la sigla aggiunge il pareggio copre due segni su
tre. Il divieto della prima regola colpisce quindi chi li contasse, non chi li
disegna, e sta nella docstring di `esito_logico_azzeccato`: in un test non ci
starebbe, perché ciò che va impedito è un conto che nessuno ha ancora scritto.

La soglia è **10 punti** e non è un gusto: viene dal difetto P1, che misura lo
scarto della X a +7.7 sotto i 5 punti di distacco, +4.9 fra 5 e 10, e lo trova
sparito sopra (+1.6, poi negativo). Scatta sul 20% delle partite; sotto i 5
scenderebbe all'11% lasciando fuori gare in cui il difetto c'è tutto, sopra i
15 salirebbe al 31% comprendendo gare in cui nominare la X è peggio che
tacerla. La favorita è sempre **una delle due squadre e mai il pareggio**: la
domanda è «chi delle due», e la X è ciò che si aggiunge a quella risposta, non
una terza risposta.

E c'è una cosa misurata dietro, che la scheda partita dichiara: proprio nelle
partite in cui questa riga aggiunge il pareggio, il pareggio esce **più spesso
di quanto il modello dica** — 27.9% dichiarato contro 34.8% accaduto su quattro
stagioni rigiocate. È il difetto P1, non curato. Il giorno in cui lo sarà,
`esito_previsto` dirà «X» per conto suo e questa riga smetterà di aggiungere
qualcosa, ed è il modo giusto in cui deve finire.
La scheda partita mostra anche gol, formazioni e pagelle
quando i dettagli ci sono, e dice quando non ci sono. Mostra inoltre gli
**indisponibili**, che sono l'unica cosa che quella pagina sappia dire su una
partita non ancora giocata — e dichiara che il pronostico **non ne tiene
conto**, perché è vero: quanto pesi chi manca è il lavoro che deve ancora
arrivare. Se di quella partita non esiste nessuna lettura la sezione non
compare affatto: una sezione vuota si leggerebbe come "non manca nessuno", che
è un'informazione, e sarebbe inventata.
Con la stessa regola, e per la stessa ragione, dichiara il **cambio di
allenatore**: se una delle due squadre ha cambiato guida negli ultimi sei mesi
lo dice, e dice che il pronostico **non ne tiene conto** — vero, misurato, e
spiegato in pagina. Oltre i sei mesi non compare: le partite della gestione
precedente pesano già quasi zero per conto loro, e allarmare allora
significherebbe allarmare per un problema che il decadimento temporale ha già
risolto. A differenza dell'affidabilità basta una squadra sola: che il Milan
abbia cambiato allenatore non dice niente di falso sull'avversario, mentre
l'affidabilità è un confronto e va mostrata a coppie.
Dall'11 settembre 2026 la scheda mostra anche i **probabili marcatori**
(`Engine.probabili_marcatori`, `blocchi_marcatori` in `web/views.py`), cinque
per squadra e **solo prima del fischio d'inizio**: non vengono registrati,
quindi dopo il via l'unico modo di mostrarli sarebbe un ricalcolo — è la
regola di `_riga`. Coi titolari ufficiali quando la distinta è intera (undici
per parte e non `formazione_provvisoria`), altrimenti coi candidati delle
ultime partite meno gli assenti certi, e la pagina dice quale dei due. Accanto,
in una riga, la sovrastima misurata (`SOVRASTIMA_MISURATA`), che il numero non
corregge. L'indice (`StoriaMarcatori`) sta in cache nel motore con l'impronta
dell'archivio giocatori più quella degli archivi: un punteggio nuovo cambia
quanto segna di solito una squadra anche prima che arrivi il tabellino.
Quando di una partita esiste anche il pronostico **rifatto con le formazioni
ufficiali**, la scheda mostra i due affiancati (`confronto_ufficiale` in
`web/views.py`): 1/X/2 dell'uno, dell'altro e la differenza. Tre regole, tutte
già in codice. Il confronto usa **due righe di registro**, mai il modello
caldo: il secco ricalcolato adesso avrebbe visto più partite dell'ufficiale
congelato ieri, e la loro differenza racconterebbe il tempo passato invece
delle formazioni. Il pronostico grande in cima alla pagina **resta il secco**,
ed è detto in chiaro — non perché sia migliore (è esattamente ciò che non si sa
ancora) ma perché è l'unico che esiste per ogni partita: una testata che cambia
fonte a seconda di cosa c'è sul disco mostrerebbe due cose diverse su due
partite della stessa giornata. E la sezione **dichiara il limite del metodo**,
come si fa per panchina e indisponibili: i pesi si misurano da gol, assist e
tiri, quindi vedono gli attaccanti e quasi non i difensori — un centrale o un
portiere che salta la partita risultano lì «quasi gratis», e non è vero. Se i
due pronostici coincidono a meno di un punto percentuale la pagina dice che le
formazioni hanno **confermato**: «non è cambiato niente» è un esito, non un
vuoto.
L'hub mostra, sotto le partite: accuratezza per mercato con la sua baseline,
la **calibrazione**, il confronto con le quote di chiusura e — quando esiste
qualche partita pronosticata in entrambi i modi — il **verdetto sulle
formazioni ufficiali**
(`confronto_fra_modi`, `Engine.confronto_modi`), cioè i due Brier a confronto
sulle sole partite che hanno tutti e due. Tutte le sezioni compaiono solo
quando c'è qualcosa di misurato, e dichiarano quando i dati non bastano: una
tabella vuota si leggerebbe come «provato e non ha reso», che è diverso da
«non ancora provato». Oggi nessuna delle due sezioni nuove compare, ed è il
comportamento giusto — di pronostici ufficiali non ne esiste ancora nessuno.
Sono state scritte e provate **prima** perché il giorno dell'accensione il
lavoro partirebbe, scriverebbe nel registro, e sul sito non cambierebbe
niente.

La **calibrazione** (`calibrazione` in `data/outcomes.py`, `righe_calibrazione`
e `verdetto_calibrazione` in `web/views.py`) è la seconda lettura degli stessi
esiti che l'accuratezza riassume in un Brier, e sta accanto e non al posto di
quella: si può avere un buon Brier ed essere sistematicamente troppo sicuri di
sé. Tre regole in codice. Il mercato è l'**1X2**, perché è quello in cui il
modello sa qualcosa e le sue tre probabilità coprono una gamma larga — su
Over/Under, dopo la compressione, le fasce si accalcherebbero tutte in una. Il
riassunto è lo scarto **in valore assoluto** e mai col segno: sull'1X2 le tre
probabilità di una partita sommano a uno e un esito solo si avvera, quindi il
saldo con segno vale zero *per costruzione* e direbbe «in media ci prende» di
un modello scalibrato in tutte e due le direzioni. E una fascia con meno di
`MINIMO_PER_FASCIA` esiti resta in tabella ma dichiarata: sparirla nasconderebbe
che il modello in quella fascia ci è andato, e quante volte.

`/pronostici` è il **registro riga per riga**: cosa era stato detto, quando, e
com'è andata (`elenco_pronostici` in `data/outcomes.py`, `righe_registro` in
`web/views.py`). È l'unica cosa di `outcomes.py` che non è una misura, e la
differenza sta tutta in una parola: una misura salta ciò che non si è ancora
potuto misurare, un registro deve mostrarlo — da qui il `left join` di
`mercati_con_esito` e le partite **ancora da giocare in cima all'elenco**, che
sono la metà della promessa in cui la risposta non c'è ancora. È anche l'unica
pagina che legge dal registro *anche prima* del fischio d'inizio, al contrario
della regola di `_riga`: qui la domanda non è «cosa penso adesso» ma «cosa fu
scritto», e la pagina lo dichiara perché i due numeri possono divergere. Il
giudizio di riga è il Brier della singola partita nella forma del backtest
(somma sui tre esiti), e non esiste una colonna «azzeccato»: su un pronostico
probabilistico è la domanda sbagliata. La colonna del **tipo** compare solo se
di tipi ce n'è più d'uno, altrimenti direbbe «secco» su ogni riga.

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

Il sito si guarda anche **dal telefono**, e il come e' una scelta di sicurezza
prima che di comodita': il server resta in ascolto su **127.0.0.1** com'e'
sempre stato, e a pubblicarlo dentro la rete privata ci pensa Tailscale
(`tailscale serve --bg 8000`, dato una volta sola, vive fuori dal progetto).
Con `--host 0.0.0.0` il sito si aprirebbe a chiunque fosse sullo stesso Wi-Fi;
cosi' invece risponde solo ai dispositivi dell'utente, in HTTPS con
certificato valido, e a internet non e' esposto niente. Tenerlo acceso e'
compito di `scripts/run_web.ps1` e dell'attivita' `SerieAAnalyzer-Web`, che e'
l'unica del progetto **senza limite di durata** — le altre si spengono da sole
dopo 10-30 minuti perche' una raccolta lunga e' una raccolta bloccata, e lo
stesso valore qui spegnerebbe il sito ogni mezz'ora. Il limite che resta e'
dichiarato e non aggirabile: se il computer e' spento, dal telefono non si
vede niente — nessuna sveglia automatica, per la stessa regola che vale per la
raccolta.

## Vincoli di progetto

- **I pronostici sono immutabili**, legati a una `model_version`
  (`data/schema.sql`). Rigenerarne uno significa inserire una nuova riga, mai
  aggiornare quella vecchia: uno storico riscrivibile a posteriori vale zero.
- **Non nascondere i limiti del modello nell'interfaccia.** Nel backtest l'1X2
  batte le baseline (Brier 0.589 nel 2024/25 e 0.594 nel 2025/26, contro
  0.667 di chi tira a caso), mentre su Over 2.5 e Gol/NoGol il modello — dopo
  la compressione dei gol — *pareggia* con la media di campionato invece di
  batterla: la scheda partita lo dichiara accanto al mercato, con queste
  parole («su questi esiti dichiara quasi sempre la media di campionato,
  perché è quanto sa davvero»). Stessa regola quando una squadra non ha storia
  sufficiente: si mostra il motivo, non un pronostico inventato.
  Dal 9 settembre 2026 questa regola ha una **forma nuova e un confine più
  stretto**, e sono due cose diverse. La forma: il limite è una riga sola
  accanto al numero, non un paragrafo — il ragionamento dietro sta in
  `ISTRUZIONI.txt`. Il confine: la regola vale dove il numero si legge per
  esteso, cioè la scheda partita, e **non più su ogni pagina che lo nomini di
  passaggio** — l'hub mostra «NG · 52%» senza la riga accanto, per decisione
  dell'utente. Chi allarga di nuovo il confine sta rifacendo una scelta già
  presa, non correggendo un difetto.
- La valutazione è **Brier score, log loss e calibrazione**, non "percentuale
  di pronostici azzeccati" (predire sempre "1" dà già ~45%).
- Due misure diverse, da non confondere: il **backtest** rigioca il passato con
  il modello di oggi, l'**accuratezza** (`/`, `scripts/accuratezza.py`) usa solo
  pronostici congelati prima del fischio d'inizio. La seconda è la misura
  onesta e all'inizio della stagione sarà povera; sotto le 30 **partite** per
  mercato l'interfaccia dichiara che il numero non vale ancora. Partite e non
  esiti, dal 3 settembre 2026: le dieci linee Over/Under di una gara sono
  decise dallo stesso punteggio, quindi «Over/Under, 200 esiti» su venti
  partite contava dieci volte la sostanza vera e faceva scattare il verdetto
  dove doveva restare «troppi pochi dati». È la gemella della regola di
  `esiti_risolti`: quella toglie il doppio conteggio nel *tempo* (più ristime
  della stessa gara), questa nello *spazio* (più mercati sulla stessa gara), e
  ognuna da sola lascia in piedi l'altra. Le tabelle mostrano tutti e due i
  numeri, perché «200 esiti» accanto a «troppi pochi dati» si legge come un
  guasto.
- **Il confronto che conta è con le quote**, non con le baseline: battere chi
  tira a caso non dimostra nulla, pareggiare col mercato è già un buon
  risultato. Il confronto usa solo gli esiti per cui esistono *entrambe* le
  cose, pronostico congelato e quota di consenso — misurare il modello su
  tutte le partite e il mercato su un sottoinsieme diverso darebbe un numero
  privo di significato.
- La stagione 2025/26 era un buco fra lo storico e la raccolta giornaliera, ed
  è stata chiusa **due volte**. Prima il 22 agosto 2026 importando un file di
  risultati (380 partite, zero richieste: `data/importato.py`), poi il 24
  agosto ricomprandola col piano Pro, tabellini compresi
  (`scripts/riempi_storico.py`). Le due cartelle **convivono** ed è corretto:
  `dataset.py` prende quella stagione **solo dal dato pagato**, e la regola è
  esplicita perché gli id sintetici dell'importazione non verrebbero
  sovrascritti da quelli veri ma affiancati — il campionato risulterebbe
  giocato due volte. Conseguenza da tenere a mente leggendo il piede di
  pagina: le partite «da stagioni importate» oggi valgono **zero**, e non è un
  guasto.
- Servono `.env` con `API_FOOTBALL_KEY` solo per gli script sui dati veri; test
  e demo girano senza.

## Lavori futuri

`LAVORI.txt` (file locale, volutamente non tracciato) tiene l'elenco: i
**lavori concordati** con l'utente e i **difetti trovati leggendo il codice**,
ognuno col tempo che vuole, la sua scadenza e quante richieste API costa. È
nato il 7 settembre 2026 dalla fusione di `DA_FARE.txt` e `DA_SISTEMARE.txt`,
che sono stati cancellati — erano già pieni di rimandi incrociati e la
scadenza del 24 settembre stava scritta in tutti e due. Se quel file non ci
fosse, questa sezione è l'unica traccia che ne rimane.

**I numeri dei lavori non si riusano**: quelli chiusi lasciano il posto vuoto,
così i riferimenti nei commenti del codice continuano a puntare alla cosa
giusta. Vale anche per i numeri dei difetti, che nel file unito conservano le
lettere che avevano. Al 28 settembre 2026 — chiusi **C1**, l'**1**, l'**1b**
e il **2** (misurati e spenti), il **6** (misurato e piatto), il **5**
(provato: l'xG c'è e non si accende) e il **10** tranne due ripetizioni, più
il giorno del downgrade e il difetto **Q1** (corretto e ricuperato) —
resta: il confronto con Bet365 da ripetere coi 200 gol, e a fine stagione **P1** insieme alla correzione dei
marcatori, perché prima di allora rifarli darebbe lo stesso numero. Il
perché sta in `LAVORI.txt`.

Il 9 — chiuso lo stesso giorno in cui è nato — saltava il **7**, e P1 salta
la **O**: i due motivi sono diversi. Del 7
non si può sapere se `DA_FARE.txt` l'avesse già usato — quel file non era
tracciato da git — e un posto vuoto in più costa meno di un riferimento che
punta a due cose; «O1» e «01», invece, in un commento di codice sono la
stessa cosa.

**6 — il vantaggio di casa. CHIUSO l'11 settembre 2026: misurato, piatto, e
il modello non cambia** (i numeri stanno sopra `DEFAULT_EMIVITA_LEGA` in
`model/dixon_coles.py`: da 180 a 1825 giorni il Brier 1X2 va da 0.5968 a
0.5965, e la prova appaiata si ferma a −0.38 errori standard). Com'era posta
la domanda, che resta vera: `home_advantage` è l'ultimo
parametro del progetto mai passato dal backtest, e vale 0.0798 (la casa segna
×1.08) contro il ×1.16 di tutto l'archivio. Non è un errore — con l'emivita di
180 giorni il campione efficace è di 234 partite equivalenti su 1541, e lì
dentro il rapporto vale davvero quello — ma su quel campione l'errore standard
è **grande quanto il parametro stesso**. La domanda è di modello: l'emivita
serve a dimenticare la *forma di una squadra*, e viene applicata anche a
`home_advantage`, `base` e `rho`, che sono costanti di lega e cambiano con gli
anni, non con le settimane. La prova è un'emivita separata per i parametri di
lega, misurata col metodo del progetto. Se è piatta, muore.

**1, 1b — le formazioni ufficiali. CHIUSI l'11 settembre 2026.** Il fattore
è stato misurato prima che la macchina scrivesse una riga, sulle quattro
stagioni ricomprate (che hanno formazioni e tabellini di ogni partita), ed è
piatto: vedi `ufficiali.py` qui sopra e le idee morte qui sotto. La macchina
è spenta (`MACCHINA_ACCESA`), il codice resta.

**2 — gli indisponibili dentro il pronostico. CHIUSO con l'1.** Si
raccolgono e si vedono (`data/indisponibili.py`), e la scheda partita
dichiara che il pronostico non ne tiene conto. L'altra metà — quanto pesa chi
manca — era esattamente il metodo del lavoro 1 applicato a un'altra lista, e
quel metodo non migliora il pronostico.

**5 — gli xG della stagione in corso. PROVATO il 28 settembre 2026.** Una
richiesta, Milan-Lecce del 20 settembre: il gratuito l'xG lo dà (2.19 a
0.21, salvato in archivio). La raccolta quotidiana **non** li prende lo
stesso: una giornata spende già 50-90 richieste su 100, nessuna parte del
modello legge l'xG, e le statistiche si rileggono dall'id quando servono —
a fine stagione 380 richieste nei giorni di sosta, se una misura nuova le
chiedesse.

**Q1 — le distinte vuote di prima del via. CHIUSO il 28 settembre 2026.**
Vedi `store.py` qui sopra: la copia vuota in cache della lettura anticipata
ha fatto perdere le formazioni di tre partite. Il codice è sistemato e le
tre partite (1550098, 1550117, 1550133) sono state ricuperate lo stesso
giorno, col sì dell'utente: tolte le buste vuote a tre tentativi, tre
richieste, formazioni intere. Se capitasse di nuovo, il ricupero è quello —
e va chiesto, perché `data/serie_a/details/` è la cartella che non si
rigenera.

**P1 — il pareggio nelle partite equilibrate.** Un difetto **misurato e non
curato**, ed è la differenza che conta. Dove le due squadre si somigliano
(`|P(1) − P(2)|` sotto i 10 punti) il modello mette il pareggio ultimo fra i
tre mentre nella realtà arriva primo: su 244 partite dice X al 27.9% contro
un 34.8% reale, +6.9 punti e 2.3 errori standard. Il buco sta tutto nelle
quattro caselle che `rho` governa — 0-0 e 1-1 valgono +5.9 dei +6.9, dal 2-2
in su restano +0.4 — quindi la leva è dentro la matrice, che è anche l'unico
posto in cui potrebbe stare senza rompere la regola dei mercati come somme di
celle. La cura provata (uno scostamento di `rho` che agisce solo dove le
squadre si somigliano) ha perfino un **ottimo interno** a −0.20 e migliora la
misura onesta di 0.0010, ma la prova appaiata partita per partita si ferma a
**1.33 errori standard** e con la regola del progetto non passa: è rumore
finché non c'è più archivio. Si rimisura a **fine 2026/27**, con una quinta
stagione mai vista. Tre cose da non perdere per strada: la fascia è stata
scelta *dopo* aver visto i dati; il difetto è di **calibrazione**, che la
correzione migliora davvero (da +6.9 a +2.7), e chi cerca la metrica che gli
dà ragione la trova sempre; i mercati dei gol non si muovono, coerentemente
con quanto già verificato il 22 agosto 2026. Un'ultima cosa che lega P1 al
lavoro 6: il `rho` del modello acceso oggi vale **+0.0340**, cioè il verso
che i pareggi li toglie invece di aggiungerli, ed è lo stesso `rho` che il 6
elenca fra i parametri di lega a cui l'emivita si applica per sbaglio. Il 6
è stato fatto l'11 settembre 2026 e ha risposto anche su questo: con la
memoria di lega lunga `rho` diventa negativo (−0.035 a cinque anni), ma il
pareggio dichiarato non si muove — 25.8% contro 25.7%, accaduto 27.7%. Il
`rho` positivo non era la causa di P1.

### Cosa è già stato fatto, perché nessuno lo rifaccia

  schede giocatore        20/08/2026   `data/players.py`, `/giocatori`
  mercati dei gol         22/08/2026   compressione 0.20
  la 2025/26              22 e 24/08   prima da file, poi ricomprata
  tabellini provvisori    23/08/2026   `tabellino_provvisorio`
  tabellini 2022-2024     24/08/2026   1141 richieste, **non** erano un lavoro
                                       da mesi: il gratuito blocca il filtro
                                       `league+season`, non gli endpoint per
                                       partita
  pesi giocatore coi tiri 24/08/2026   (gol+assist)×2 + tiri
  il sito dal telefono    25/08/2026   Tailscale
  lineups ed events       25/08/2026   quattro stagioni complete
  emivita e shrinkage     25/08/2026   misurati; shrinkage rimisurato il
                                       03/09 sull'archivio cresciuto: 2.0
  le misure per partita   25/08 e 03/09  prima le ristime, poi i mercati
  il calendario 2026      28/08/2026   380 partite, 1 richiesta
  formazioni a metà       30/08/2026   `formazione_provvisoria`
  sfogliare le giornate   03/09/2026   `/giornata`, `/calendario`
  il verdetto verde       03/09/2026   vale per ogni punteggio nominato in
                                       pagina, non solo il primo
  la palette «piombo»     04/09/2026   grafite/cenere/magenta al posto di
                                       indaco/grigio/arancio; verde, oro e
                                       rosso del giudizio **invariati**
  logo e nome             04/09/2026   `static/logo.png`, «Il Pinguino Fatto
                                       Meglio»; il pacchetto non si rinomina
  l'esito scritto         05/09/2026   1, X o 2 accanto ai punteggi, con la
                                       sua percentuale; la doppia chance
                                       resta senza regola di scelta
  le due palline          07/09/2026   il giudizio in due colonne — esito e
                                       punteggio — al posto della pastiglia a
                                       tre gradini; le fa cadere Juventus-
                                       Milan 1-1 del 6 settembre
  il Gol/NoGol scelto     07/09/2026   «NG · 52%» accanto all'esito sull'hub,
                                       col limite dichiarato: NG nel 92% dei
                                       pronostici, e il 50% e' un pavimento
  il sito senza istruzioni 09/09/2026  86 paragrafi tolti dai template e
                                       riscritti in `ISTRUZIONI.txt`; il
                                       titolo sopra i dati, niente colonna
                                       laterale; resta solo `.dato`, cioè un
                                       fatto sull'archivio
  l'esito a logica        09/09/2026   1 o 2 secco se le due squadre distano
                                       ≥10 punti, 1X o X2 se meno. Non è un
                                       mercato: fuori da ogni misura. La
                                       soglia esce dal difetto P1
  la terza pallina        09/09/2026   il giudizio sulla sigla a logica,
                                       accanto a esito e punteggio. Le tre
                                       colonne non hanno la stessa
                                       difficoltà e i verdi non si sommano
  le quote sui marcatori  11/09/2026   salvate invece che buttate; le 31
                                       letture rimaste in cache (13784
                                       quote) recuperate a zero richieste
  il pronostico marcatori 11/09/2026   dentro la matrice; +13% sul
                                       costante, la sovrastima del 7%
                                       dichiarata e non corretta
  i limiti dagli header   11/09/2026   C1: contatore e freno seguono il
                                       server, e il 24 settembre non va
                                       ricordato niente; salire resta a mano
  le formazioni ufficiali 11/09/2026   1b misurato e piatto: macchina
                                       spenta, la distinta si legge una
                                       volta per partita (lavori 1 e 2
                                       chiusi con lei)
  il vantaggio di casa    11/09/2026   lavoro 6: emivita di lega misurata
                                       e piatta, il modello non cambia
  il ritorno al gratuito  24/09/2026   `--no-calendario`; contatore e freno
                                       si sono adeguati da soli il 25
  l'xG sul gratuito       28/09/2026   lavoro 5: una richiesta, c'e'; non
                                       si raccoglie ogni giorno
  la sovrastima marcatori 28/09/2026   correzione misurata: 0.92-0.94,
                                       -1.86 errori standard, non passa
  marcatori contro Bet365 28/09/2026   pari su 46 partite e 85 gol: un
                                       aneddoto fino a 200 gol
  le distinte vuote       28/09/2026   Q1: la copia vuota in cache non si
                                       serve piu'; le tre partite ricuperate
  casa e fuori            28/09/2026   il fattore per squadra peggiora; resta
                                       una lettura nella scheda squadra

### Le idee che sembrano buone e non lo sono

Sono misurate, non discusse, e ognuna è morta con un numero. Il posto dove
stanno per esteso è `LAVORI.txt`, sezione COSE DA NON FARE MAI; qui c'è
l'elenco, perché è la parte che si tende a riproporre.

- **il modello a tiri e l'xG.** Misurati due volte il 24 agosto 2026, zero
  richieste. Sui gol totali i tiri danno +0.012 di correlazione e l'xG +0.010,
  col segno che cambia da una stagione all'altra: è rumore intorno allo zero.
  L'unica cosa vera che ne resta è che l'xG prevede i gol **fatti** meglio dei
  gol (+0.026, stabile) e sui gol **subiti** non fa niente.
- **l'impatto dell'allenatore.** La macchina c'è (`data/panchine.py`) e il
  fattore è **1.00, cioè spento**: il Brier 1X2 peggiora monotonicamente
  allontanandosi da 1, in tutte e quattro le stagioni separatamente. La
  correzione fa due cose insieme — toglie una squadra che non esiste più e
  toglie **partite vere** — e la seconda si paga sempre.
- **una regola che sceglie fra secco e doppia chance** (scrivere 1X o X2 al
  posto di 1 quando il modello è indeciso). Misurata il 5 settembre 2026 sui
  1154 pronostici in registro, zero richieste, e cade su tre numeri. Le tre
  doppie sommano a **2 per costruzione** — ogni esito è contato due volte —
  quindi la più alta non può scendere sotto i due terzi: minimo osservato
  69.6%, mediana 74.9%, e una soglia sotto il 70% scatta sempre, cioè non è
  una soglia. «12» è ≥67% nel **99.9%** delle partite, cioè equivale a
  scrivere «qualcuno vincerà». E soprattutto: quando il modello è indeciso lo
  è anche la doppia — se 1≈X≈2≈33% ogni doppia vale circa 67% e non arriva a
  nessuna soglia seria. **La doppia chance non copre l'incertezza, sposta solo
  il pavimento da un terzo a due terzi**, e una regola così finirebbe per
  ri-etichettare le partite che il modello aveva già deciso. Le tre
  percentuali restano dove sono — scheda partita e registro — senza che
  nessuno scelga al posto di chi legge. **Da non confondere col lavoro 9**,
  che è nato dopo e non è la stessa idea: quello scrive la doppia *accanto* al
  secco e non al suo posto, non viene misurato da niente, e soprattutto la sua
  soglia non sta sulla probabilità della doppia — il numero col pavimento a
  due terzi che uccide questa — ma sul distacco fra le due squadre, dove P1 ha
  misurato un difetto vero.
- **una «forza dell'allenatore»** che viaggia da una squadra all'altra: non è
  identificabile con un campionato solo. Non è nemmeno la strada provata.
- **i mercati dei gol con una coda più grassa** (binomiale negativa o simili):
  nei nostri dati i gol totali sono *sotto*-dispersi, quattro stagioni su
  quattro, e il modello è già troppo largo.
- **ritoccare l'emivita o lo shrinkage.** Misurati, e lo shrinkage è l'unico
  parametro con un ottimo interno vero. Chi li tocca deve rifare la misura.
- **accendere le formazioni ufficiali col metodo dei pesi.** Misurato l'11
  settembre 2026 (`backtest.py --cerca-fattore`), zero richieste: pesare chi
  scende in campo da gol, assist e tiri non migliora il pronostico, né in
  media (0.58 errori standard) né sulle formazioni più lontane dal solito,
  dove fa **peggio** (+1.22, pendenza −0.05 ± 0.24). Non vuol dire che chi
  gioca non conti — i bookmaker spostano le quote quando escono le
  formazioni — ma che questo metodo ne coglie troppo poco: vede la direzione
  giusta (pendenza +0.28 ± 0.18 su tutto) e non vede difensori e portiere.
  Chi riprova porti un'idea nuova — i difensori, il movimento delle quote fra
  vigilia e fischio — e rifaccia la misura prima di accendere.
- **una memoria più lunga per i parametri di lega** (`emivita_lega`, il
  lavoro 6). Misurata l'11 settembre 2026, zero richieste: il vantaggio di
  casa passa da ×1.07 a ×1.14 e `rho` diventa negativo, ma i pronostici non se
  ne accorgono — Brier 1X2 da 0.5968 a 0.5965, minimo che salta da una
  stagione all'altra, −0.38 errori standard nella prova appaiata — e il
  pareggio dichiarato nemmeno. Il codice resta come parametro, come per
  l'allenatore: si rimisura quando l'archivio cresce.
- **un vantaggio di casa per ogni squadra.** Misurato il 28 settembre 2026,
  zero richieste, fuori dal progetto: un delta per squadra con uno shrinkage
  suo, e più libertà gli si dà più il Brier 1X2 peggiora — 0.5968 senza,
  0.6031 al più libero, monotono in tutte e quattro le stagioni, +3.14 errori
  standard di peggioramento. Lo scarto casa-fuori di una squadra contro il
  pronostico varia quanto varierebbe per caso e non si ripete da un anno
  all'altro (r = +0.11 ± 0.14). Qui, al contrario dell'allenatore, il codice
  **non** è entrato nel modello: è rimasta una lettura nella scheda squadra
  (`righe_casa_e_fuori`), dai soli pronostici congelati, fuori da ogni misura.

Sui mercati dei gol, due cose da ricordare ora che il lavoro è fatto. La
compressione **toglie un danno, non aggiunge una capacità**: si arriva a
pareggiare con la baseline, e chi la ritocca aspettandosi di saper
pronosticare i gol insegue qualcosa che nei dati non c'è. E il fattore si
sceglie **sulle stagioni diverse da quella su cui lo si misura**, guardando
sempre anche l'1X2 — è il mercato che funziona, e vale più di quello che non
funziona. Verificato il 22 agosto 2026: `rho` **non** va ristimato dopo la
compressione, muoverlo di ±0.10 sposta i mercati dei gol di meno di 0.0005 di
Brier e in direzioni opposte da una stagione all'altra.
