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
.\scripts\schedule_windows.ps1                              # 4 esecuzioni al giorno
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
  prima di spendere su dettagli.
- `store.py` — archivio della stagione corrente + `DayJournal` dei giorni
  raccolti. Un giorno viene chiuso — cioè non richiesto più — solo se è
  **passato** *e* aveva tutte le partite in stato definitivo. Oggi e domani non
  si chiudono mai, per quanto completi sembrino: un giorno ancora da giocare
  risulta "tutto definitivo" anche solo perché è vuoto, e il calendario della
  Serie A cambia (recuperi, anticipi). Chiuderlo in anticipo significherebbe
  perdere quelle partite per sempre. La regola sta in `is_closed()`, in
  lettura, non nel dato scritto: così nemmeno un diario vecchio può farla
  saltare.
- `dataset.py` — unisce storico 2022-2024 (dalla cache) e archivio corrente in
  un unico `Dataset`, che è ciò che il modello e il web consumano.
- `predictions.py` — registro dei pronostici in SQLite (`data/pronostici.db`).
  Append-only: `UPDATE` e `DELETE` sono **vietati da trigger**, non solo per
  convenzione. Si salva la matrice dei risultati, da cui ogni mercato si può
  riderivare mesi dopo, più i mercati materializzati per le query di
  calibrazione. Due regole nel codice: non si registra una partita già
  iniziata (sarebbe barare: il modello è stimato su quella partita), e si
  scrive **una riga per partita e versione del modello** — l'impronta dei
  parametri stimati dice se il modello è cambiato, così ricaricare l'hub non
  moltiplica righe identiche.
- `parsers.py` — JSON di API-Football → oggetti di dominio. I test coprono i
  parser con payload reali riprodotti in `tests/conftest.py`, così nessun test
  spende richieste.

⚠️ `data/serie_a/` **non è una cache**: è l'unica copia esistente della
stagione in corso e l'API non restituirà mai più i giorni passati. Non
cancellarla né suggerire di rigenerarla.

### Web

`web/engine.py` tiene dati e modello caldi in uno `Snapshot`, ricostruito solo
quando cambia l'impronta dell'archivio (mtime+size di `fixtures.json`), non a
ogni richiesta HTTP — la stima costa un paio di secondi. Un lock protegge da
stime duplicate su richieste concorrenti. Quando il modello viene
ristimato — l'unico momento in cui i pronostici possono cambiare — l'engine
congela i pronostici delle partite in programma nel registro. `web/views.py`
traduce il dominio in ciò che i template disegnano; `web/app.py` espone hub, scheda partita, `/stato`
(JSON: cosa sa il sistema, budget residuo) e `/aggiorna` (forza la ricostruzione).

## Vincoli di progetto

- **I pronostici sono immutabili**, legati a una `model_version`
  (`data/schema.sql`). Rigenerarne uno significa inserire una nuova riga, mai
  aggiornare quella vecchia: uno storico riscrivibile a posteriori vale zero.
- **Non nascondere i limiti del modello nell'interfaccia.** Nel backtest l'1X2
  batte le baseline (Brier 0.586 vs 0.667), ma Over 2.5 e Gol/NoGol fanno
  leggermente *peggio* della media di campionato: le pagine lo dichiarano
  accanto al mercato. Stessa regola quando una squadra non ha storia
  sufficiente: si mostra il motivo, non un pronostico inventato.
- La valutazione è **Brier score, log loss e calibrazione**, non "percentuale
  di pronostici azzeccati" (predire sempre "1" dà già ~45%).
- La stagione 2025/26 è un buco irrecuperabile fra lo storico e la raccolta
  giornaliera; non cercare modi per riempirlo.
- Servono `.env` con `API_FOOTBALL_KEY` solo per gli script sui dati veri; test
  e demo girano senza.
