# Serie A Analyzer

Analisi dei dati di squadre e giocatori della Serie A per produrre **probabilita'
di esito** delle partite e di singoli eventi di gioco, con tracciamento onesto
di quanto quei pronostici si avvicinino alla realta'.

## Idea centrale

Il sistema non calcola un modello per ogni tipo di pronostico. Calcola **un solo
oggetto** — la distribuzione di probabilita' su tutti i risultati esatti — e da
quello deriva tutto il resto sommando celle:

```
matrice[x][y] = P(casa segna x gol E ospite ne segna y)

  1X2          -> triangoli e diagonale della matrice
  Over 2.5     -> celle con x + y >= 3
  Gol/NoGol    -> sottomatrice [1:, 1:]
  Clean sheet  -> prima riga / prima colonna
  Handicap     -> diagonali spostate
```

Questo garantisce **coerenza per costruzione**: e' impossibile che l'1X2
contraddica l'Over/Under, perche' vengono dalla stessa sorgente.

## Stato

| Incremento | Contenuto | Stato |
|---|---|---|
| 1 | Motore statistico + test | ✅ completo e verificato |
| 2 | Schema DB + ingestione API-Football | ✅ completo, verificato su dati veri |
| 2b | Raccolta giornaliera della stagione in corso | ✅ pronta, in attesa del 22/08 |
| 3 | Persistenza su Supabase | ⬜ |
| 4 | Backtest e calibrazione su stagioni passate | ✅ primo giro fatto (vedi sotto) |
| 5 | Hub partite (FastAPI + Jinja2) | ✅ prima versione |
| 6 | Strato giocatori (probabilita' marcatore) | ⬜ |
| 7 | Pipeline di fine partita e tracking | ⬜ |
| 8 | Simulazione stagione, bot Telegram | ⬜ |

## Avvio rapido

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"

pytest                          # 532 test
python scripts/demo_engine.py   # dimostrazione su dati sintetici
```

La demo non richiede credenziali: genera un campionato da forze note, stima il
modello e verifica che le ritrovi.

Per lavorare sui dati veri:

```bash
copy .env.example .env          # e inserisci API_FOOTBALL_KEY
python scripts/predict_next_round.py
```

## Vivere con 100 richieste al giorno

Il piano gratuito di API-Football concede 100 richieste al giorno. Non e' un
dettaglio operativo, e' un vincolo di progetto — e l'architettura e' costruita
intorno a un fatto molto favorevole:

> **Limite piu' serio del budget: le stagioni.** Il piano gratuito espone solo le
> stagioni **2022, 2023 e 2024** (verificato il 18/08/2026). Richiedere il 2025
> o il 2026 risponde `Free plans do not have access to this season, try from
> 2022 to 2024`. Bastano per addestrare e validare il modello, non per
> pronosticare la stagione in corso. La via d'uscita e' la
> [raccolta giornaliera](#la-stagione-in-corso-si-raccoglie-un-giorno-alla-volta).

**Il modello di squadra costa quasi nulla.** L'endpoint `/fixtures` con lega e
stagione restituisce tutte le ~380 partite con i risultati, e i nomi delle
squadre, in **una singola richiesta**. Tre stagioni di addestramento = 3
richieste. Non serve nemmeno chiamare `/teams`.

**Lo strato giocatori e' caro.** Le statistiche per giocatore richiedono una
richiesta per partita: una stagione vale quasi quattro giorni di budget. Si
riempie con una coda di backfill, dalla partita piu' recente alla piu' vecchia.

Tre meccanismi tengono insieme la cosa:

| Meccanismo | Cosa fa |
|---|---|
| `cache.py` | Salva ogni risposta grezza su disco. Una richiesta ripetuta e ancora fresca costa zero. |
| `budget.py` | Contatore persistente con **riserve per priorita'**: il backfill storico si ferma da solo prima di intaccare le richieste destinate alle formazioni ufficiali. |
| TTL per endpoint | Una stagione conclusa non scade mai; le formazioni scadono in 5 minuti. |

La riserva e' la parte che conta davvero: senza, un backfill lanciato di sabato
mattina lascia l'applicazione a secco la domenica alle 14:30, cioe' quando
escono le formazioni ufficiali e i pronostici diventano interessanti.

## La stagione in corso si raccoglie un giorno alla volta

Il piano gratuito nega la stagione corrente **per stagione**, non per data. Una
richiesta senza il parametro `league` passa:

```
/fixtures?league=135&season=2026   ->  bloccato
/fixtures?date=2026-08-22          ->  OK, tutte le leghe, risultati inclusi
```

Aggiungere `league` obbliga l'API a pretendere anche `season`, e li' si viene
bloccati. Chiedendo la sola data la risposta arriva completa e **la Serie A si
filtra in locale**, gratis.

La finestra concessa e' di tre giorni — ieri, oggi, domani — e questo divide i
dati in due categorie con proprieta' opposte:

| | Come si legge | Se lo perdi |
|---|---|---|
| **Id delle partite** | solo entro la finestra di 3 giorni | **perso per sempre** |
| Formazioni, statistiche, eventi | dall'id della partita, senza scadenza | si recuperano con calma |

Verificato: i dettagli di una partita giocata *ieri*, stagione in corso,
arrivano completi. E' il motivo per cui `daily.py` separa nettamente le due
cose. La raccolta giornaliera deve **sempre** catturare gli id; se il budget
finisce a meta' di una giornata di campionato, `backfill_details` completa il
resto domani senza che si sia perso nulla.

```bash
python scripts/daily_collect.py       # ieri, oggi, domani + dettagli mancanti
python scripts/daily_collect.py --solo-al-momento-giusto   # solo se serve adesso
.\scripts\schedule_windows.ps1        # registra tutte le attivita' pianificate
```

### Non un orario, un momento

Due sole esecuzioni hanno un orario fisso, e fanno due cose che nessuna partita
puo' decidere: alle **08:00** si legge il calendario (gli id vivono solo nella
finestra di tre giorni, e persi sono persi) e alle **23:45** si chiude la
giornata.

Tutto il resto e' agganciato alle partite, perche' quote e indisponibili
valgono quanto sono **vicini al fischio d'inizio**: le quote dopo il via non
esistono piu', e una lista di assenti letta a partita finita sa chi non e'
sceso in campo davvero — usarla per pronosticare sarebbe barare. Un orario
fisso ci passa vicino per caso: il 22 agosto 2026 la corsa delle 20:00 ha letto
insieme le quote di una partita delle 20:45 e quelle di una del giorno dopo,
che sarebbero state rilette comunque.

Un'attivita' pianificata gira quindi ogni quarto d'ora dalle 11:00 alle 23:30 e
chiede a `momento_utile` se questo istante serve a qualcosa:

| momento | quando | cosa prende |
|---|---|---|
| **vigilia** | la partita comincia entro **40 minuti** | quote e indisponibili, solo delle partite imminenti |
| **dopo** | la partita e' cominciata da **2h15** (90' + 15' + mezz'ora di margine) | risultato definitivo e dettagli |
| *niente* | tutto il resto, cioe' quasi sempre | esce senza spedire una richiesta |

La vigilia batte il dopo: se una partita e' appena finita e un'altra sta per
cominciare, cio' che si perde per sempre sono le quote della seconda.

Un giorno le cui partite sono tutte in stato definitivo viene marcato
**chiuso** sul `DayJournal` e non viene piu' richiesto: le esecuzioni
successive costano zero. Un giorno mai raccolto compare come buco nel resoconto
— non per rifarlo, perche' non si puo', ma per sapere quanto fidarsi delle
statistiche costruite sopra.

Il computer non e' sempre acceso, e la raccolta **non lo accende**: le
esecuzioni che cadono a macchina spenta o sospesa si recuperano dopo, quando
la si riaccende. Una seconda attivita' pianificata parte all'accesso e al
risveglio dalla sospensione (che non e' un accesso, e Windows da solo non
sempre se ne accorge), confronta l'ultima raccolta finita bene con l'ultimo
orario previsto e raccoglie solo se manca qualcosa: se non manca niente esce
senza spendere una richiesta.

### Le notifiche su Telegram

Facoltative: senza `TELEGRAM_BOT_TOKEN` in `.env` il sistema funziona identico
e in silenzio. Quando ci sono, arrivano **tre messaggi che si vogliono
leggere** e **sei allarmi**, e nient'altro:

| messaggio | quando |
|---|---|
| i pronostici della giornata | la mattina, se si gioca e non e' ancora cominciato niente |
| com'e' andata | quando l'ultima partita del giorno e' finita |
| **tabellini completi** | di una giornata sono arrivati tutti, e sono quelli veri |
| quote perse per sempre | una partita e' cominciata senza che ne avessimo una quota |
| raccolta fallita | un giorno della finestra non e' entrato in archivio |
| la raccolta e' ferma | nessuna esecuzione riuscita da 16 ore |
| budget quasi esaurito | restano meno di 15 richieste |
| tabellini rimasti a meta' | l'API non li ha consolidati e i tentativi sono finiti |
| **le formazioni si sono accese** | i tabellini bastano: da oggi due pronostici per partita |

Sui tabellini i messaggi sono **due e opposti**, e una giornata ne prende uno
solo: "completi" quando sono arrivati tutti, "rimasti a meta'" quando il
sistema ha smesso di poter rimediare. Il primo conta perche' e' il momento in
cui quelle partite **cominciano a servire** — il risultato c'era gia', minuti
gol assist e voti no, e sono quelli che alimentano le schede giocatore e il
peso di chi scende in campo.

Nessuno di questi e' legato a un orario. Si decide da **cosa c'e' sul disco e
cosa non e' ancora stato detto**, ed e' per questo che il resoconto della sera
arriva la mattina dopo se il computer era spento — senza che quel caso sia
previsto da nessuna parte. Un registro degli invii (`data/notifiche.json`)
impedisce di ripetersi.

Le ~90 esecuzioni quotidiane che non fanno niente **non mandano niente**: un
bot che ripete la stessa riga ogni quarto d'ora e' un log con le notifiche
accese, e si finisce per silenziarlo proprio quando avrebbe qualcosa da dire.
Per i primi giorni si puo' accendere `TELEGRAM_VERBOSO=1` e vedere tutto.

> ⚠️ **`data/serie_a/` e' l'unica copia esistente della stagione in corso.**
> Non e' una cache: cancellarla significa perdere i giorni gia' passati, che
> l'API non restituira' mai piu'. Finche' non c'e' la persistenza su Supabase
> (incremento 3), quella cartella va trattata come un archivio da salvare
> altrove, non come dati rigenerabili.

### Cosa resta fuori portata

- Il calendario oltre domani: la scheda squadra non puo' mostrare "tutti i
  prossimi incontri" (Fase 3, punto 5).
- I pronostici escono ~24 ore prima della partita, non con giorni di anticipo.
- I **tabellini** di una stagione che non abbiamo raccolto giorno per giorno:
  formazioni, voti e marcatori si comprano, non si trovano in un file di
  risultati. Vedi [Ricomprare una stagione passata](#ricomprare-una-stagione-passata).

## Importare una stagione da un file

Il buco fra lo storico 2022-2024 e la raccolta giornaliera si puo' chiudere
**senza spendere niente**, se di quella stagione esiste un file di risultati
(i calendari in CSV che si scaricano in giro vanno benissimo).

```bash
python scripts/importa_risultati.py serie-a-2025-UTC.csv --solo-verifica
python scripts/importa_risultati.py serie-a-2025-UTC.csv
```

Il primo comando non scrive niente: legge il file, aggancia ogni squadra al suo
id e mostra cosa entrerebbe. Il secondo importa davvero, in
`data/importato/<anno>/`, e da li' la stagione entra nel modello da sola.

**Perche' basta un file di risultati.** Il modello di squadra impara da cinque
cose per partita — data, le due squadre, i due gol — e un calendario le ha
tutte. Quello che manca (formazioni, tabellini, voti) non entra nella stima
delle forze: serve alle pagine dei giocatori, e quello si compra.

Due cose che il comando fa e che sembrano dettagli:

- **aggancia i nomi agli id senza indovinare.** Il file scrive "Milan", l'API
  dice "AC Milan": riconosciuti come la stessa squadra, altrimenti sarebbero
  due meta' di storia e due forze deboli. Una squadra che non riesce a
  riconoscere **ferma l'importazione** invece di entrare con un id inventato;
  in quel caso si passa a mano, `--squadra "Pisa=801"`;
- **controlla che il file sia un campionato.** N squadre, N*(N-1) partite,
  ognuna N-1 in casa e N-1 fuori, nessun accoppiamento ripetuto. Un file monco
  si dichiara e non entra, a meno di chiederlo con `--anche-se-incompleto`.

Le partite importate hanno id riconoscibili (`92025001`) e vivono in una
cartella separata da quelle comprate. Se un giorno la stessa stagione viene
ricomprata davvero, **vince il dato pagato** e l'importato si fa da parte da
solo: non serve cancellare niente.

## Ricomprare una stagione passata

Un file di risultati da' i gol, non i tabellini. Per avere formazioni, voti e
marcatori di una stagione passata bisogna comprarla: e' il filtro per stagione
del piano gratuito, non una limitazione del codice. Il giorno in cui il piano
cambia, il lavoro e' due comandi.

```bash
python scripts/riempi_storico.py --solo-verifica   # il piano copre la stagione?
python scripts/riempi_storico.py                   # la ricompra tutta
```

Il primo interroga l'endpoint di stato, l'unico che dice in chiaro che piano e'
attivo: una stagione non coperta **non da' errore**, risponde con zero partite,
e prenderla per buona sarebbe il modo piu' rapido per archiviare il vuoto. Il
secondo scarica il calendario in una richiesta e poi i dettagli, tre per partita
conclusa: circa **1140 richieste per stagione**. Si interrompe con ctrl-c e si
rilancia quando si vuole, anche il giorno dopo: riprende da dove era.

Le partite finiscono in `data/storico/<anno>/` e da li' entrano nel modello e
nelle schede giocatore da sole, senza toccare `.env` e senza riavviare il sito.

Cio' che non torna indietro per quanto si paghi: le **quote** delle partite gia'
giocate (API-Football non le archivia) e i **pronostici** di quella stagione,
che valgono solo se congelati prima del fischio d'inizio. Si guadagna un modello
piu' informato, non uno storico di accuratezza piu' lungo.

## L'applicazione web

```bash
python scripts/serve.py        # http://127.0.0.1:8000
```

Due pagine, per ora:

- **Hub** — le partite in programma con 1X2, Over 2.5, Gol/NoGol e risultato
  piu' probabile. Quando non c'e' niente in programma mostra gli ultimi
  risultati e la tabella delle forze, invece di una pagina vuota.
- **Scheda partita** — tutti gli esiti derivati, la griglia completa dei
  risultati esatti e gli scontri diretti.

Il modello viene ristimato **solo quando l'archivio della raccolta giornaliera
cambia**, non a ogni richiesta HTTP: stimarlo costa un paio di secondi e fra
una visita e l'altra i dati quasi sempre non si sono mossi. `/stato` restituisce
in JSON cosa sa il sistema e quanto budget resta; `/aggiorna` forza la
ricostruzione.

Due cose che la pagina dice invece di nascondere: quando una squadra non ha
storia sufficiente non compare un pronostico inventato ma il motivo, e accanto
all'Over/Under c'e' scritto che su quegli esiti il modello dichiara quasi
sempre la media di campionato — con il perche', e col totale atteso che ne
esce volutamente schiacciato verso quella media.

## Il modello

Dixon-Coles su scala logaritmica:

```
log(gol attesi casa)      = base + attacco[casa]  - difesa[ospite] + fattore_campo
log(gol attesi trasferta) = base + attacco[ospite] - difesa[casa]
```

Quattro elementi lo rendono utilizzabile invece che ingenuo:

- **Correzione di Dixon-Coles** (`rho`) sui risultati bassi. Due Poisson
  indipendenti sottostimano 0-0 e 1-1: nel calcio reale il punteggio corrente
  cambia l'atteggiamento delle squadre.
- **Decadimento temporale**. Ogni partita pesa `exp(-xi * giorni_fa)`. Una
  partita di dieci mesi fa descrive una squadra che non esiste piu'.
- **Shrinkage**. Una penalita' ridge tira attacco e difesa verso la media del
  campionato. Senza, dopo tre giornate il modello e' convinto che la neopromossa
  che ha vinto due volte sia da scudetto.
- **Compressione dei gol totali**. L'unico dei quattro che non sta nella stima
  ma nel pronostico: il totale dei gol attesi si avvicina alla media di lega,
  il rapporto fra le due squadre resta intatto. Le forze dicono bene chi vince
  e male quanti gol, e il backtest lo misura. Vedi i risultati piu' sotto.

### Perche' non lo storico giocatore-contro-avversario

Un giocatore affronta un dato avversario due volte a stagione. Su quel campione
la varianza schiaccia qualsiasi segnale. Le probabilita' di marcatore
(incremento 5) si costruiscono invece su tre livelli: gol attesi della squadra,
quota di partecipazione del giocatore stimata su tutta la stagione, e minuti
attesi. L'avversario entra come forza difensiva aggregata, non come storia
personale.

## Come si misura

Non con la "percentuale di pronostici azzeccati": predire sempre "1" in Serie A
da' gia' circa il 45%. Le metriche sono **Brier score**, **curva di calibrazione**
(quando dico 30%, succede il 30% delle volte?) e confronto con le quote di
chiusura come baseline. Lo schema prevede questo fin dall'inizio.

### Risultati del backtest (stagioni 2024/25 e 2025/26)

`scripts/backtest.py` rigioca una stagione giornata per giornata: per ogni
giornata stima il modello **solo** sulle partite concluse prima del primo calcio
d'inizio, poi pronostica. Il modello non vede mai il futuro, nemmeno di un'ora.
Zero richieste API: legge dalle stesse sorgenti del sito.

```bash
python scripts/backtest.py                  # l'ultima stagione completa
python scripts/backtest.py --stagione 2024  # una in particolare
python scripts/backtest.py --compressione 1 # senza compressione dei gol
python scripts/backtest.py --cerca-compressione   # quanto conviene comprimere
```

| 1X2 | 2024/25 | 2025/26 |
|---|---|---|
| **Modello Dixon-Coles** | **0.5886** | **0.5942** |
| sempre 1/3 - 1/3 - 1/3 | 0.6667 | 0.6667 |
| sempre le frequenze di lega | 0.6602 | 0.6575 |
| guadagno sul caso puro | 11.7% | 10.9% |

Log loss 0.9839 e 0.9960. Esito piu' probabile azzeccato nel 51.7% e 52.8% dei
casi. Le due stagioni sono state rigiocate in modo indipendente: il taglio e'
sull'ora di inizio, quindi nessuna partita del 2025/26 entra nella stima di una
del 2024/25 (il numero del 2024/25 infatti non si e' mosso di un millesimo dopo
aver aggiunto in archivio la stagione successiva).

**Sull'1X2 il modello aggiunge informazione reale, e lo fa due volte su due.**
Sui gol totali no: e' il limite che ha dato origine alla **compressione dei
gol** descritta qui sotto.

### I mercati dei gol, e perche' il modello ci dice poco

Rigiocate quattro stagioni, Over 2.5 e Gol/NoGol facevano **peggio** che
dichiarare ogni volta la media di campionato. La diagnosi non e' quella che
sembra: scomponendo il Brier, sui gol la *risoluzione* del modello e' quasi
zero e la correlazione con cio' che accade oscilla intorno allo zero cambiando
segno da una stagione all'altra. Il modello varia le sue previsioni sui gol, e
quelle variazioni sono **rumore**: la baseline non si muove e non paga niente,
il modello si muove a vuoto e paga ogni movimento.

Sapere chi e' piu' forte dice *chi vince*, non *quanti gol*.

La cura non e' una distribuzione a coda piu' grassa — nei nostri dati i gol
totali sono **sotto**-dispersi (varianza minore della media in tutte e quattro
le stagioni) e il modello e' gia' troppo largo. E' togliere il movimento che
non serve: i gol attesi si leggono come *totale* piu' *rapporto*, si comprime
solo il totale verso la media di lega e il rapporto resta identico.

    totale' = media_lega + f * (totale - media_lega)

Il fattore `f` esce dal backtest scelto **sulle stagioni diverse da quella su
cui si misura** (`--cerca-compressione`), e vale **0.20**: del divario sui gol
totali il modello si tiene un quinto. Cosa cambia, in Brier medio sulle
quattro stagioni:

| | variazione |
|---|---|
| Over 2.5 | **-0.0031** |
| Gol/NoGol | **-0.0038** |
| 1X2 (la guardia) | +0.0008 |

I gol guadagnano dieci volte quello che l'1X2 perde. La condizione era posta
prima di cominciare — il mercato che funziona vale piu' di quello che non
funziona — e se il conto fosse uscito al contrario il lavoro sarebbe stato
buttato.

Dove si arriva, stagione per stagione, contro la baseline:

| | 2022/23 | 2023/24 | 2024/25 | 2025/26 |
|---|---|---|---|---|
| Over 2.5 | 0.2470 / 0.2431 | 0.2479 / 0.2499 | 0.2489 / 0.2496 | 0.2495 / 0.2483 |
| Gol/NoGol | 0.2518 / 0.2492 | 0.2494 / 0.2495 | 0.2490 / 0.2498 | 0.2491 / 0.2476 |

(modello / baseline; prima erano otto sconfitte su otto, ora sono tre vittorie,
un pareggio e quattro sconfitte)

**Il punto onesto: si pareggia con la baseline, non la si batte.** La
compressione toglie un danno, non aggiunge una capacita'. Chi si aspettava di
saper pronosticare i gol resta deluso, e le pagine lo dicono con queste parole.

La calibrazione mostra il prima e il dopo meglio di ogni altra cosa. Sull'1X2 e'
buona nella fascia centrale (scarti dell'1-3%) e peggiora agli estremi, dove i
campioni sono piccoli. Sull'Over 2.5, **prima**, sbandava: nel 2025/26 quando il
modello dava 35.5% l'Over si avverava il 48.4% delle volte, e quando dava 63.9%
si avverava il 45.5% — non incertezza, storture. **Dopo**, tutti i 379
pronostici della stagione stanno in una fascia sola:

| fascia | n | previsto | reale | scarto |
|---|---|---|---|---|
| 40%-60% | 379 | 44.8% | 45.9% | +1.1% |

Non e' un modello diventato bravo sui gol: e' un modello che ha smesso di
fingere di saperne.

Manca ancora il confronto con le quote di chiusura, che e' la baseline seria:
senza quello sappiamo di battere l'ignoranza, non il mercato.

### La stessa misura, ma sul campo

Il backtest rigioca il passato con il modello di oggi. In esercizio vale una
misura piu' severa: ogni pronostico viene **congelato prima** del fischio
d'inizio e confrontato col risultato vero quando arriva. E' cio' che l'hub
mostra sotto "Come stanno andando i pronostici" e che
`scripts/accuratezza.py` stampa in dettaglio, calibrazione compresa.

Accanto a ogni mercato sta la sua **baseline**: il punteggio di chi ripete
sempre la frequenza media di quell'esito senza guardare le squadre. Si calcola
per singola selezione e poi si media — su Over/Under la linea 0.5 si avvera
quasi sempre e la 4.5 quasi mai, e mescolarle darebbe al modello un muro molto
piu' basso di quello vero. Sotto i 30 esiti misurati il verdetto dice "troppi
pochi dati" invece di un numero: a inizio stagione e' l'unica risposta onesta.

### Il confronto con le quote

Battere le baseline dice che il modello sa qualcosa; battere il **mercato**
direbbe che sa qualcosa che gli altri non sanno. Le quote di chiusura
incorporano infortuni, formazioni e il denaro di chi scommette, e pareggiare
con loro e' gia' un ottimo esito.

La raccolta giornaliera legge le quote delle partite che si giocano entro 48
ore (una richiesta per partita, con un tetto) e le archivia tutte: la
"chiusura" e' l'ultima lettura prima del fischio d'inizio. Le quote **non si
recuperano** — API-Football non le tiene — quindi questo confronto si
costruisce solo in avanti, partita dopo partita, e all'inizio sara' vuoto.

Il margine del banco viene tolto normalizzando dentro ogni gruppo esaustivo, e
fra i bookmaker si prende la mediana. Il confronto e' ristretto agli esiti per
cui esistono entrambe le cose: pronostico congelato prima del via e quota.

### Chi non gioca

La raccolta legge anche gli **indisponibili** delle partite entro 48 ore:
infortunati, squalificati e giocatori in dubbio, una richiesta per partita.
Compaiono nella scheda partita, e sono l'unica cosa che quella pagina sappia
dire su una gara non ancora giocata.

Il pronostico **non ne tiene conto**, e la pagina lo dichiara. Il modello stima
la forza di una squadra, e la squadra e' la stessa che ci giochi il suo
migliore o la sua riserva: sapere chi manca e' meta' dell'informazione, l'altra
meta' — quanto pesa chi manca — e' un lavoro ancora da fare.

Due differenze rispetto alle quote, che cambiano come vengono trattati:

* **si recuperano.** Una partita del 2024 risponde ancora oggi. Quindi si
  chiedono a priorita' bassa, e una raccolta saltata non fa danni;
* **il momento della lettura conta lo stesso.** La lista di una partita
  conclusa sa chi non e' sceso in campo davvero, e usarla per pronosticare
  sarebbe barare. Di ogni lettura si conserva quanto mancava al fischio
  d'inizio, e le pagine guardano solo l'ultima lettura *precedente* al via.

### Le formazioni ufficiali (predisposto, e per ora spento)

Il pronostico normale non sa chi gioca: stima la forza di una squadra, e la
squadra e' la stessa che ci giochi il suo migliore o la sua riserva. Una
mezz'ora prima del via, pero', le formazioni escono.

    python scripts/formazioni_ufficiali.py --stato

Quel comando dice a che punto e' la macchina, senza spendere richieste. Oggi
risponde che e' **spenta**, ed e' la risposta giusta: il peso di un giocatore
si misura da quanto ha prodotto — gol e assist per minuto — e quella
produzione sta nei tabellini, che si accumulano una giornata alla volta con la
raccolta. Finche' non ce ne sono abbastanza non viene scritto **niente**, e
non e' un guasto: i pronostici sono immutabili, e una riga "con le formazioni"
identica a quella secca direbbe "misurato" dove non c'era niente da misurare.

Si accende da sola. Quando i tabellini bastano, ogni partita ha due pronostici
invece di uno — quello secco e quello con le formazioni — e dopo qualche mese
si confrontano i loro Brier. Se il secondo non e' migliore, questo lavoro si
butta senza rimpianti: e' per questo che le due modalita' esistono separate.

Il metodo **vede gli attaccanti, non i difensori**: gol e assist misurano chi
sta davanti, e un centrale che salta la partita risulta quasi gratis. Va
saputo leggendo il numero.

## Struttura

```
src/serie_a_analyzer/
  config.py          configurazione da .env
  model/
    scoreline.py     matrice dei risultati esatti
    markets.py       derivazione degli esiti dalla matrice
    dixon_coles.py   stima delle forze
    simulation.py    campionati sintetici per la validazione
  data/
    schema.sql       schema Postgres / Supabase
    client.py        client API-Football (cache + budget)
    cache.py         cache su disco delle risposte grezze
    budget.py        contabilita' delle richieste giornaliere
    parsers.py       JSON di API-Football -> oggetti del dominio
    ingest.py        orchestrazione dell'ingestione
    daily.py         raccolta giornaliera della stagione in corso
    predictions.py   storico immutabile dei pronostici (SQLite)
    outcomes.py      confronto fra pronostico e risultato, Brier e calibrazione
    odds.py          quote dei bookmaker: il metro di paragone del modello
    indisponibili.py infortunati, squalificati e giocatori in dubbio
    pesi.py          quanto pesa un giocatore, dai gol e assist che ha prodotto
    ufficiali.py     il pronostico rifatto quando escono le formazioni
    store.py         archivio della stagione e diario dei giorni raccolti
    dataset.py       unione di storico e archivio corrente
  web/
    app.py           rotte FastAPI
    engine.py        dataset e modello tenuti caldi fra le richieste
    views.py         dal dominio a cio' che i template disegnano
    templates/       hub e scheda partita
    static/          foglio di stile
tests/               591 test
scripts/
  demo_engine.py         dimostrazione su dati sintetici
  backtest.py            il modello contro una stagione vera, giornata per giornata
  predict_next_round.py  i pronostici dell'hub, dal terminale (stesso motore)
  daily_collect.py       raccolta giornaliera (da schedulare)
  formazioni_ufficiali.py pronostico rifatto a ridosso del calcio d'inizio
  verifica_raccolta.py   controllo di prontezza, senza spendere richieste
  storico_pronostici.py  cosa c'e' nel registro dei pronostici
  accuratezza.py         quanto valgono i pronostici gia' verificati
  run_daily.ps1          wrapper con log, per l'Utilita' di pianificazione
  run_momento.ps1        wrapper della raccolta agganciata alle partite
  orari_raccolta.ps1     i due orari fissi, letti da chi registra e da chi recupera
  schedule_windows.ps1   registra le attivita' pianificate
  serve.py               avvia l'applicazione web
```

## Regola non negoziabile dello schema

I pronostici sono **immutabili** e legati a una `model_version`. Rigenerare un
pronostico significa inserire una nuova riga, mai aggiornare quella vecchia. Se
un pronostico potesse essere riscritto dopo aver visto il risultato, lo storico
di accuratezza varrebbe zero — e non ce ne accorgeremmo.
