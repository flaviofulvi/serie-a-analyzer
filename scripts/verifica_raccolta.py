"""Controllo di prontezza della raccolta giornaliera. Non spende richieste.

La raccolta e' un lavoro non sorvegliato che deve reggere per mesi, e i suoi
fallimenti sono silenziosi: l'attivita' pianificata che non parte, la chiave
API scaduta, un giorno chiuso per errore. Non compare un errore a schermo —
compare solo un archivio che smette di crescere, e quando ce ne si accorge il
dato e' gia' perso, perche' la finestra del piano gratuito e' di tre giorni.

Questo script risponde a una domanda sola: **se domani si gioca, lo prendiamo?**
Legge solo file gia' presenti sul disco e lo stato dell'Utilita' di
pianificazione: non contatta l'API e non consuma budget.

    python scripts/verifica_raccolta.py

Uscita 0 se tutto e' a posto, 1 se c'e' qualcosa da sistemare.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADICE / "src"))

from serie_a_analyzer.config import ConfigError, load_settings  # noqa: E402
from serie_a_analyzer.data.daily import window_days  # noqa: E402
from serie_a_analyzer.data.store import DayJournal, SeasonStore  # noqa: E402

NOME_TASK = "SerieAAnalyzer-Raccolta"
NOME_TASK_RECUPERO = "SerieAAnalyzer-Recupero"

# Da quanti giorni indietro cercare buchi. Stesso orizzonte di daily_collect.
FINESTRA_CONTROLLO_BUCHI = 30

# Windows non segnala il successo solo con 0: i codici SCHED_S_* sono
# informativi, e leggerli come errori fa gridare al guasto quando non c'e'.
# 267011 (mai eseguita) non compare qui perche' in quel caso non esiste
# nemmeno una data di ultima esecuzione da commentare.
ESITI_NON_FALLIMENTI = {
    267008: "attivita' pronta",
    267009: "in esecuzione proprio adesso",
    267012: "nessuna esecuzione futura in programma",
}

# Oltre questo ritardo dall'ultima raccolta si e' gia' persa una finestra.
ORE_MASSIME_SENZA_RACCOLTA = 24

problemi: list[str] = []
avvisi: list[str] = []


def ok(testo: str) -> None:
    print(f"  [ok] {testo}")


def avviso(testo: str) -> None:
    avvisi.append(testo)
    print(f"  [!]  {testo}")


def problema(testo: str) -> None:
    problemi.append(testo.splitlines()[0])
    print(f"  [X]  {testo}")


def sezione(titolo: str) -> None:
    print(f"\n{titolo}\n{'-' * len(titolo)}")


def powershell(comando: str):
    """Interroga l'Utilita' di pianificazione. None se non e' interrogabile."""
    if sys.platform != "win32":
        return None
    try:
        esito = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", comando],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if esito.returncode != 0 or not esito.stdout.strip():
        return None
    try:
        return json.loads(esito.stdout)
    except json.JSONDecodeError:
        return None


def _durata(delta: timedelta) -> str:
    ore = int(delta.total_seconds() // 3600)
    if ore < 1:
        return f"{int(delta.total_seconds() // 60)} min"
    if ore < 48:
        return f"{ore} h"
    return f"{ore // 24} giorni"


# ---- controlli --------------------------------------------------------------


def controlla_configurazione():
    sezione("Configurazione")
    try:
        settings = load_settings()
    except ConfigError as exc:
        problema(f"configurazione incompleta: {str(exc).splitlines()[0]}")
        return None
    ok(f"chiave API presente ({settings.api_provider}), lega {settings.league_id}")
    ok(f"archivio: {settings.store_dir}")
    return settings


# Una sola interrogazione a PowerShell, che restituisce tutto cio' che serve
# sapere dell'attivita' pianificata.
_QUERY_TASK = """
$t = Get-ScheduledTask -TaskName "{nome}" -ErrorAction SilentlyContinue
if (-not $t) {{ '{{"assente": true}}' }} else {{
  $i = Get-ScheduledTaskInfo -TaskName "{nome}"
  [pscustomobject]@{{
    abilitata       = [bool]$t.Settings.Enabled
    batteria_blocca = [bool]$t.Settings.DisallowStartIfOnBatteries
    batteria_ferma  = [bool]$t.Settings.StopIfGoingOnBatteries
    sveglia         = [bool]$t.Settings.WakeToRun
    recupera        = [bool]$t.Settings.StartWhenAvailable
    trigger         = @($t.Triggers | ForEach-Object {{ $_.StartBoundary }})
    # 30/11/1999 e' il segnaposto di Windows per "mai eseguita", non una data.
    ultima          = if ($i.LastRunTime.Year -ge 2000) {{ $i.LastRunTime.ToString("s") }} else {{ $null }}
    esito           = $i.LastTaskResult
    prossima        = if ($i.NextRunTime) {{ $i.NextRunTime.ToString("s") }} else {{ $null }}
  }} | ConvertTo-Json -Compress
}}
"""


def controlla_attivita_pianificata() -> None:
    sezione("Attivita' pianificata")
    dati = powershell(_QUERY_TASK.format(nome=NOME_TASK))

    if dati is None:
        avviso("stato dell'attivita' pianificata non verificabile da qui")
        return
    if not isinstance(dati, dict) or dati.get("assente"):
        problema(
            f"attivita' '{NOME_TASK}' non registrata: la raccolta non parte da sola.\n"
            "       Registrala con:  .\\scripts\\schedule_windows.ps1"
        )
        return

    trigger = dati.get("trigger") or []
    if dati.get("abilitata"):
        orari = ", ".join(str(t)[11:16] for t in trigger)
        ok(f"registrata e abilitata — {len(trigger)} esecuzioni al giorno ({orari})")
    else:
        problema("attivita' registrata ma DISABILITATA: non partira' mai")

    if dati.get("batteria_blocca") or dati.get("batteria_ferma"):
        problema(
            "non parte (o si interrompe) quando il computer e' a batteria.\n"
            "       Ri-registrala con:  .\\scripts\\schedule_windows.ps1"
        )
    else:
        ok("parte anche a batteria")

    if dati.get("recupera"):
        ok("recupera le esecuzioni perse a computer spento")
    else:
        problema("le esecuzioni perse a computer spento non vengono recuperate")

    # La sveglia e' spenta apposta: la raccolta non deve accendere il computer
    # da sola. Se ricompare, qualcuno ha ri-registrato l'attivita' con una
    # versione vecchia dello script.
    if dati.get("sveglia"):
        avviso("l'attivita' accende il computer da sola all'ora prevista: non e' voluto")

    ultima, esito = dati.get("ultima"), dati.get("esito")
    if not ultima:
        # Anche solo ri-registrare l'attivita' ne azzera lo storico: e' lo
        # stato normale subito dopo schedule_windows.ps1, non un guasto.
        avviso("mai eseguita da quando e' stata registrata: il primo turno e' quello utile")
    else:
        quando = datetime.fromisoformat(ultima)
        ritardo = datetime.now() - quando
        testo = f"ultima esecuzione {quando:%d/%m %H:%M} ({_durata(ritardo)} fa)"
        if esito == 0:
            ok(f"{testo}, esito ok")
        elif esito in ESITI_NON_FALLIMENTI:
            ok(f"{testo}, {ESITI_NON_FALLIMENTI[esito]}")
        else:
            problema(f"{testo}, esito FALLITO (codice {esito}) — vedi data/logs/")
        if ritardo > timedelta(hours=ORE_MASSIME_SENZA_RACCOLTA):
            problema("l'ultima esecuzione risale a piu' di un giorno fa: finestra persa")

    if dati.get("prossima"):
        ok(f"prossima esecuzione: {datetime.fromisoformat(dati['prossima']):%d/%m %H:%M}")


def controlla_attivita_recupero() -> None:
    """L'attivita' che rimedia alle esecuzioni cadute a computer spento.

    Senza sveglia e' l'unica rete rimasta: su un computer che non sta acceso
    tutto il giorno, un'esecuzione notturna che nessuno recupera e' un giorno
    di partite perso e basta.
    """
    sezione("Recupero all'accensione")
    dati = powershell(_QUERY_TASK.format(nome=NOME_TASK_RECUPERO))

    if dati is None:
        avviso("stato dell'attivita' di recupero non verificabile da qui")
    elif not isinstance(dati, dict) or dati.get("assente"):
        problema(
            f"attivita' '{NOME_TASK_RECUPERO}' non registrata: le esecuzioni cadute a\n"
            "       computer spento non vengono recuperate.\n"
            "       Registrala con:  .\\scripts\\schedule_windows.ps1"
        )
    elif not dati.get("abilitata"):
        problema("attivita' di recupero registrata ma DISABILITATA")
    else:
        ok(f"registrata e abilitata — {len(dati.get('trigger') or [])} trigger "
           "(accesso e risveglio dalla sospensione)")

    # Il marcatore e' cio' su cui il recupero decide: se manca o non si legge,
    # la raccolta riparte comunque. Meglio una richiesta in piu' che un giorno
    # in meno.
    marcatore = RADICE / "data" / "logs" / "ultima-esecuzione.txt"
    if not marcatore.exists():
        avviso("nessuna raccolta riuscita registrata: al prossimo avvio si raccoglie")
        return
    try:
        quando = datetime.strptime(
            marcatore.read_text(encoding="utf-8-sig").strip(), "%Y-%m-%d %H:%M:%S"
        )
    except (ValueError, OSError):
        avviso(f"{marcatore.name} illeggibile: al prossimo avvio si raccoglie")
        return
    ok(f"ultima raccolta riuscita: {quando:%d/%m %H:%M} "
       f"({_durata(datetime.now() - quando)} fa)")


def controlla_ultimo_log() -> None:
    sezione("Ultimo log")
    logs = sorted((RADICE / "data" / "logs").glob("raccolta-*.log"))
    if not logs:
        avviso("nessun log: la raccolta non ha mai scritto nulla")
        return

    ultimo = logs[-1]
    righe = ultimo.read_text(encoding="utf-8", errors="replace").splitlines()
    esiti = [r.strip() for r in righe if r.strip().startswith("ESITO:")]
    ok(f"{ultimo.name} — {len(esiti)} esecuzioni registrate")
    if not esiti:
        avviso("il log non contiene alcun esito: il wrapper si e' interrotto prima")
    elif esiti[-1] != "ESITO: ok":
        problema(f"l'ultima esecuzione nel log e' andata male ({esiti[-1]})")
    else:
        ok("l'ultima esecuzione nel log e' andata a buon fine")


def controlla_diario(settings):
    sezione("Diario della raccolta")
    journal = DayJournal(path=settings.journal_file)
    raccolti = journal.collected_days()
    if not raccolti:
        avviso("nessun giorno raccolto finora")
        return journal

    oggi = datetime.now(timezone.utc).date()
    ok(f"{len(raccolti)} giorni raccolti, dal {raccolti[0]} al {raccolti[-1]}")

    inizio = max(raccolti[0], oggi - timedelta(days=FINESTRA_CONTROLLO_BUCHI))
    buchi = [g for g in journal.missing_days(inizio, oggi) if g <= oggi]
    if buchi:
        problema(
            f"{len(buchi)} giorni mai raccolti e non piu' recuperabili: "
            + ", ".join(g.isoformat() for g in buchi[:5])
            + (" ..." if len(buchi) > 5 else "")
        )
    else:
        ok(f"nessun buco negli ultimi {(oggi - inizio).days + 1} giorni")

    if raccolti[-1] < oggi - timedelta(days=1):
        problema(f"l'ultimo giorno raccolto e' il {raccolti[-1]}: la finestra e' scaduta")
    return journal


def controlla_finestra(journal) -> None:
    """La domanda vera: alla prossima esecuzione, quali giorni verranno guardati?"""
    sezione("Cosa fara' la prossima esecuzione")
    oggi = datetime.now(timezone.utc).date()
    etichette = {
        oggi - timedelta(days=1): "ieri",
        oggi: "oggi",
        oggi + timedelta(days=1): "domani",
    }
    da_richiedere = 0
    for giorno in window_days(oggi):
        etichetta = etichette.get(giorno, "")
        if journal.is_closed(giorno, today=oggi):
            voce = journal.entry(giorno) or {}
            print(f"  {giorno} ({etichetta:6}) completo: "
                  f"{voce.get('fixtures', 0)} partite, non si richiede")
        else:
            da_richiedere += 1
            print(f"  {giorno} ({etichetta:6}) verra' richiesto")

    if da_richiedere == 0:
        problema("nessun giorno verra' richiesto: oggi e domani risultano gia' chiusi")
    else:
        ok(f"{da_richiedere} richieste per la finestra, il resto del budget va ai dettagli")


def controlla_budget(settings) -> None:
    sezione("Budget e archivio")
    stato = {}
    if settings.budget_file.exists():
        try:
            stato = json.loads(settings.budget_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            avviso("file del budget illeggibile: verra' ricreato")

    usate = int(stato.get("used", 0)) if stato.get("day") == date.today().isoformat() else 0
    residuo = settings.daily_request_limit - usate
    if residuo <= 10:
        avviso(f"budget quasi esaurito: {residuo}/{settings.daily_request_limit} richieste")
    else:
        ok(f"budget: {residuo}/{settings.daily_request_limit} richieste disponibili oggi")

    s = SeasonStore(root=settings.store_dir).stats()
    ok(f"archivio: {s['partite']} partite, {s['con_dettagli']} con dettagli")
    if s["partite"] == 0:
        print("       (normale finche' il campionato non e' cominciato)")


def controlla_storico(settings) -> None:
    """Le stagioni ricomprate con un piano a pagamento, se ce ne sono.

    Non e' un controllo di salute della raccolta: e' il promemoria di un
    lavoro che si fa a pezzi. Un riempimento interrotto non rompe niente e non
    si segnala da solo — resta li', e senza questa riga nessuno si ricorda che
    mancano ancora quattrocento tabellini.
    """
    from serie_a_analyzer.data.storico import dettagli_in_sospeso, stagioni_riempite

    riempite = stagioni_riempite(settings.storico_dir)
    if not riempite:
        return

    sezione("Stagioni ricomprate")
    for stagione, store in sorted(riempite.items()):
        s = store.stats()
        sospeso = dettagli_in_sospeso(store)
        if sospeso:
            avviso(
                f"{stagione}: {s['partite']} partite, {s['con_dettagli']} con dettagli — "
                f"mancano {sospeso} richieste"
            )
        else:
            ok(f"{stagione}: {s['partite']} partite, completa")
    if any(dettagli_in_sospeso(store) for store in riempite.values()):
        print("       riprendi con: python scripts/riempi_storico.py")


def main() -> int:
    print("=" * 72)
    print("VERIFICA DELLA RACCOLTA — nessuna richiesta all'API")
    print("=" * 72)

    settings = controlla_configurazione()
    controlla_attivita_pianificata()
    controlla_attivita_recupero()
    controlla_ultimo_log()
    if settings is not None:
        journal = controlla_diario(settings)
        controlla_finestra(journal)
        controlla_budget(settings)
        controlla_storico(settings)

    sezione("Verdetto")
    if problemi:
        print(f"  {len(problemi)} problemi da sistemare:")
        for p in problemi:
            print(f"    - {p}")
        return 1
    if avvisi:
        print(f"  Nessun problema bloccante, {len(avvisi)} avvisi.")
    print("  La raccolta e' in grado di prendere il prossimo turno.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
