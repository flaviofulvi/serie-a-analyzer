# Serie A Analyzer — Prompt operativo per Claude Code

> **Come usarlo:** incolla questo documento come primo messaggio a Claude Code, oppure salvalo come `CLAUDE.md` nella root del repository così viene letto automaticamente a ogni sessione.

## Missione
Costruire **Serie A Analyzer**, un'applicazione web in Python che genera pronostici sulle partite di Serie A basandosi su modelli statistici e dati storici, calcola ogni possibile esito di una partita (es. probabilità che un giocatore segni contro un determinato avversario, in base a momentum e andamento delle squadre) e mantiene uno storico "pronostico↔realtà" per tracciare nel tempo l'accuratezza dei propri pronostici, aggiornandosi a fine di ogni giornata di campionato.

## Principio di sviluppo non negoziabile
Costruisci per incrementi piccoli, funzionanti e verificabili — mai un blocco unico che rischia di restare rotto a metà strada. Ogni fase della roadmap sotto deve produrre qualcosa di eseguibile (anche minimale, anche con dati di esempio) prima di passare alla fase successiva. Meglio molte versioni semplici che funzionano che una versione completa piena di bug.

## Stack tecnico
- **Linguaggio:** Python
- **Interfaccia:** pagina web
- **Fonte dati:** API-Football
- **Database:** Supabase (Postgres gestito)
- **Per il resto (framework web, deployment): proponi tu la scelta migliore all'inizio del lavoro**, spiegando brevemente il perché, e chiedi conferma prima di procedere se hai dubbi tra due opzioni valide.
- Obiettivo finale: applicazione che gira su un server acceso 24/7. All'inizio va bene farla girare in locale.

## Entità dati principali (da dedurre dalle sezioni sotto)
Squadra, Giocatore (con storico trasferimenti), Partita (storiche e future), Formazione titolare ufficiale (pubblicata a ridosso della partita), Pronostico — sia "secco" sia da formazione ufficiale, con esito reale collegato a posteriori per il tracking di accuratezza.

## Roadmap a fasi

**Fase 1 — Fondamenta dati**
Setup database e schema iniziale; script di ingestione dati storici e della prossima giornata dalla fonte dati scelta. Nessuna UI ancora: verifica la correttezza dei dati via query dirette.

**Fase 2 — Scheletro dell'app + Home page**
1. Pulsante/zona per andare alle previsioni della prossima giornata
2. Pulsante/zona per andare alla scheda giocatore
3. Pulsante/zona per andare alla scheda squadra
4. Indicatore che si colora da rosso a verde in base alla percentuale di precisione dei pronostici (può partire con valore placeholder finché non c'è storico reale)
5. La sezione entrate/uscite/spese/rientri resta solo un placeholder non funzionante per ora: è prevista in futuro, non va implementata subito

**Fase 3 — Scheda squadra**
1. Forma squadra: ultime 5 partite
2. Prossima partita, con indicazione casa/fuori casa
3. Posizione in classifica
4. Pronostici per la prossima giornata di campionato
5. Calendario di tutti i prossimi incontri della squadra
6. Altezza media dei giocatori della squadra

**Fase 4 — Scheda giocatore**
1. Squadra del giocatore
2. Storico trasferimenti del giocatore
3. Anagrafica: altezza, numero di maglia, età, piede preferito, nazionalità, posizione in campo
4. Statistiche del campionato attuale: gol, assist, partite giocate, minuti giocati, ammonizioni, espulsioni — più le versioni "per 90 minuti" di ciascuna
5. Storico delle singole partite giocate: minuti giocati, gol fatti, assist fatti, gialli presi, rossi presi, risultato della partita
6. Statistiche dei campionati passati: come al punto 4, più expected goals, tiri, tiri in porta

**Fase 5 — Motore pronostici**
Proponi tu il modello statistico più adatto, spiegando le assunzioni principali prima di implementarlo. I pronostici vanno gestiti in **due modalità**:
1. **Pronostici secchi**: generati in base a storico e statistiche, disponibili non appena sono pronti i dati della prossima giornata (probabilità di esito, over/under gol, probabilità di marcatore in base allo storico giocatore-vs-avversario, momentum e andamento squadra).
2. **Pronostici da formazione ufficiale**: rigenerati/aggiornati quando escono le formazioni titolari ufficiali, circa mezz'ora prima del calcio d'inizio, usando i giocatori realmente in campo invece delle probabili formazioni.

**Fase 6 — Hub partite della giornata — SEZIONE PRIORITARIA, è il cuore dell'app**
Vista con tutte le partite della prossima giornata. Cliccando su una partita: storico della partita (scontri diretti), pronostici sul risultato (sia secchi che, quando disponibili, da formazione ufficiale), altre possibili situazioni di partita (es. "finisce Over 2.5", "segna Lautaro Martinez").

**Fase 7 — Pipeline di fine giornata**
A ogni fine giornata di Serie A: raccogli i dati della giornata, aggiorna database e statistiche, mantieni lo storico "pronostico↔realtà" per aggiornare l'indicatore di precisione in home.

**Fase 8 — Bot Telegram**
Bot che tiene l'utente aggiornato sulle attività dell'applicazione. Per ora deve notificare quando:
- sono pronti i pronostici secchi della prossima giornata
- sono pronti i pronostici aggiornati con le formazioni ufficiali
- il database viene aggiornato a fine giornata
- un pronostico risulta azzeccato

In futuro potrà essere esteso per coprire altre attività dell'applicazione: progetta l'integrazione in modo che aggiungere nuovi tipi di notifica in seguito sia semplice.

**Fase 9 (futuro, fuori scope MVP) — Modulo finanziario**
Sistema che tenga conto di entrate, uscite, spese e rientri. Non va implementato da subito.

**Fase 10 (futuro, fuori scope MVP) — Multi-campionato**
Estensione a Ligue 1, Bundesliga, Premier League, LaLiga, se il progetto funziona. Se non costa complessità aggiuntiva significativa, evita assunzioni hard-coded "solo Serie A" nello schema dati — ma non progettare attivamente per questo scenario finché non richiesto esplicitamente.

## Quando fermarsi e chiedere
Se una decisione tecnica non è coperta da questo documento (formula esatta di un modello, struttura precisa di una tabella, scelta tra due librerie equivalenti), proponi un'opzione ragionata e chiedi conferma invece di procedere alla cieca — soprattutto per scelte difficili da cambiare in seguito (data source, database, deployment).