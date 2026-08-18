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
| 2 | Schema DB + ingestione API-Football | ✅ completo, in attesa di chiave API |
| 3 | Persistenza su Supabase | ⬜ |
| 4 | Backtest e calibrazione su stagioni passate | ⬜ |
| 5 | Hub partite (FastAPI + Jinja2 + HTMX) | ⬜ |
| 6 | Strato giocatori (probabilita' marcatore) | ⬜ |
| 7 | Pipeline di fine partita e tracking | ⬜ |
| 8 | Simulazione stagione, bot Telegram | ⬜ |

## Avvio rapido

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"

pytest                          # 92 test
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
tests/               92 test
scripts/
  demo_engine.py         dimostrazione su dati sintetici
  predict_next_round.py  pronostici sui dati reali
```

## Regola non negoziabile dello schema

I pronostici sono **immutabili** e legati a una `model_version`. Rigenerare un
pronostico significa inserire una nuova riga, mai aggiornare quella vecchia. Se
un pronostico potesse essere riscritto dopo aver visto il risultato, lo storico
di accuratezza varrebbe zero — e non ce ne accorgeremmo.
