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

pytest                          # 240 test
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
.\scripts\schedule_windows.ps1        # registra 4 esecuzioni al giorno
```

Un giorno le cui partite sono tutte in stato definitivo viene marcato
**chiuso** sul `DayJournal` e non viene piu' richiesto: le esecuzioni
successive costano zero. Un giorno mai raccolto compare come buco nel resoconto
— non per rifarlo, perche' non si puo', ma per sapere quanto fidarsi delle
statistiche costruite sopra.

> ⚠️ **`data/serie_a/` e' l'unica copia esistente della stagione in corso.**
> Non e' una cache: cancellarla significa perdere i giorni gia' passati, che
> l'API non restituira' mai piu'. Finche' non c'e' la persistenza su Supabase
> (incremento 3), quella cartella va trattata come un archivio da salvare
> altrove, non come dati rigenerabili.

### Cosa resta fuori portata

- Il calendario oltre domani: la scheda squadra non puo' mostrare "tutti i
  prossimi incontri" (Fase 3, punto 5).
- I pronostici escono ~24 ore prima della partita, non con giorni di anticipo.
- La stagione 2025/26 non e' recuperabile in nessun modo: resta un buco fra lo
  storico 2022-2024 e la raccolta che parte adesso.

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
all'Over/Under c'e' l'avvertenza che nel backtest quel mercato non ha battuto
la media di campionato.

## Il modello

Dixon-Coles su scala logaritmica:

```
log(gol attesi casa)      = base + attacco[casa]  - difesa[ospite] + fattore_campo
log(gol attesi trasferta) = base + attacco[ospite] - difesa[casa]
```

Tre elementi lo rendono utilizzabile invece che ingenuo:

- **Correzione di Dixon-Coles** (`rho`) sui risultati bassi. Due Poisson
  indipendenti sottostimano 0-0 e 1-1: nel calcio reale il punteggio corrente
  cambia l'atteggiamento delle squadre.
- **Decadimento temporale**. Ogni partita pesa `exp(-xi * giorni_fa)`. Una
  partita di dieci mesi fa descrive una squadra che non esiste piu'.
- **Shrinkage**. Una penalita' ridge tira attacco e difesa verso la media del
  campionato. Senza, dopo tre giornate il modello e' convinto che la neopromossa
  che ha vinto due volte sia da scudetto.

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

### Risultati del backtest (stagione 2024/25)

`scripts/backtest.py` rigioca la stagione giornata per giornata: per ogni
giornata stima il modello **solo** sulle partite concluse prima del primo calcio
d'inizio, poi pronostica. Il modello non vede mai il futuro, nemmeno di un'ora.
377 partite pronosticate, 38 giornate, zero richieste API (legge dalla cache).

| 1X2 | Brier | guadagno del modello |
|---|---|---|
| **Modello Dixon-Coles** | **0.5862** | — |
| sempre 1/3 - 1/3 - 1/3 | 0.6667 | 12.1% |
| sempre le frequenze di lega | 0.6602 | 11.2% |

Log loss 0.9798. Esito piu' probabile azzeccato nel 52.0% dei casi.

**Sull'1X2 il modello aggiunge informazione reale.** Sui gol totali no: Over 2.5
sta a 0.2530 contro una baseline di 0.2496, Gol/NoGol a 0.2556 contro 0.2498 —
cioe' leggermente **peggio** che dire sempre la media di campionato. La
distribuzione dei gol totali e' quasi tutta rumore in Serie A; sapere chi e' piu'
forte sposta il *chi vince*, non il *quanti gol*. E' un limite del modello, non
un bug, e va detto invece che nascosto dietro il buon numero dell'1X2.

La calibrazione dell'1X2 e' buona nella fascia centrale (scarti dell'1-3%) e
peggiora agli estremi, dove i campioni sono piccoli. Manca ancora il confronto
con le quote di chiusura, che e' la baseline seria: senza quello sappiamo di
battere l'ignoranza, non il mercato.

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
    store.py         archivio della stagione e diario dei giorni raccolti
    dataset.py       unione di storico e archivio corrente
  web/
    app.py           rotte FastAPI
    engine.py        dataset e modello tenuti caldi fra le richieste
    views.py         dal dominio a cio' che i template disegnano
    templates/       hub e scheda partita
    static/          foglio di stile
tests/               240 test
scripts/
  demo_engine.py         dimostrazione su dati sintetici
  backtest.py            il modello contro una stagione vera, giornata per giornata
  predict_next_round.py  pronostici sui dati reali
  daily_collect.py       raccolta giornaliera (da schedulare)
  verifica_raccolta.py   controllo di prontezza, senza spendere richieste
  storico_pronostici.py  cosa c'e' nel registro dei pronostici
  accuratezza.py         quanto valgono i pronostici gia' verificati
  run_daily.ps1          wrapper con log, per l'Utilita' di pianificazione
  schedule_windows.ps1   registra l'attivita' pianificata
  serve.py               avvia l'applicazione web
```

## Regola non negoziabile dello schema

I pronostici sono **immutabili** e legati a una `model_version`. Rigenerare un
pronostico significa inserire una nuova riga, mai aggiornare quella vecchia. Se
un pronostico potesse essere riscritto dopo aver visto il risultato, lo storico
di accuratezza varrebbe zero — e non ce ne accorgeremmo.
