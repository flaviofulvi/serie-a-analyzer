# Il sito acceso in permanenza, per l'Utilita' di pianificazione di Windows.
#
# Serve a una cosa sola: che il telefono possa aprire il sito da fuori casa
# passando per Tailscale. Il PC di casa e' il server vero - non c'e' nessuna
# copia da tenere aggiornata da qualche altra parte.
#
# DUE COSE CHE SEMBRANO DETTAGLI E NON LO SONO.
#
# 1. Il server resta in ascolto su 127.0.0.1, esattamente come quando si
#    sviluppa. NON si mette --host 0.0.0.0: quello lo aprirebbe anche a
#    chiunque sia sullo stesso Wi-Fi (scuola, bar, hotspot). A farlo arrivare
#    al telefono ci pensa `tailscale serve`, che e' configurato una volta sola
#    e vive dentro Tailscale, non qui dentro:
#
#        tailscale serve --bg 8000
#
#    Il telefono apre https://<nome-pc>.<tailnet>.ts.net e vede il sito, con
#    certificato valido. Fuori dal tailnet non risponde niente a nessuno.
#
# 2. Questo e' l'unico wrapper del progetto che avvia un processo che NON
#    finisce. Gli altri girano un minuto e scrivono una riga di log; qui il
#    log si riempie finche' il server e' acceso. Da qui le due differenze:
#    l'attivita' pianificata non ha limite di durata (le altre si spengono
#    dopo 10-30 minuti, e qui ucciderebbe il sito), e il log e' scritto man
#    mano invece che tutto insieme alla fine.
#
# Registrazione (una sola volta, da un PowerShell come amministratore):
#
#   .\scripts\schedule_windows.ps1
#
# Esecuzione manuale:
#
#   .\scripts\run_web.ps1

param(
    [int]$Porta = 8000
)

$ErrorActionPreference = "Stop"

$Radice = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Radice ".venv\Scripts\python.exe"
$Script = Join-Path $Radice "scripts\serve.py"
$LogDir = Join-Path $Radice "data\logs"

if (-not (Test-Path $Python)) {
    Write-Error "Ambiente virtuale non trovato in $Python. Creare .venv e installare le dipendenze."
    exit 1
}

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# Il nome del file e' il giorno in cui il server e' PARTITO, non il giorno che
# sta scrivendo: un processo acceso da una settimana scrive tutto li'. E'
# voluto - cosi' un file corrisponde a un'accensione, ed e' la cosa che si va
# a cercare quando ci si chiede "da quando e' su?".
$Log = Join-Path $LogDir ("web-" + (Get-Date -Format "yyyy-MM-dd-HHmm") + ".log")

function Scrivi([string]$Testo) {
    Add-Content -Path $Log -Encoding utf8 -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Testo)
}

# Porta gia' occupata: quasi sempre significa che il sito e' gia' acceso -
# l'attivita' pianificata e' partita mentre c'era ancora un `serve.py` avviato
# a mano. Non e' un guasto e non va trattato come tale: uvicorn morirebbe con
# un errore, l'attivita' risulterebbe fallita e Windows la riproverebbe in
# loop. Si esce dicendo perche', con esito 0.
$Occupata = $null
try {
    $Occupata = Get-NetTCPConnection -LocalPort $Porta -State Listen -ErrorAction Stop
} catch {
    $Occupata = $null
}
if ($Occupata) {
    Scrivi "porta $Porta gia' in ascolto: c'e' gia' un server. Non ne avvio un altro."
    exit 0
}

# Diagnostica di Tailscale, e non e' pignoleria: se il login scade o la
# configurazione di `serve` sparisce, il sito continua a funzionare sul PC e
# smette di funzionare sul telefono. Senza questa riga si andrebbe a cercare
# il guasto nel posto sbagliato. Non blocca mai l'avvio: il sito in locale
# vale comunque.
$Tailscale = "C:\Program Files\Tailscale\tailscale.exe"
if (Test-Path $Tailscale) {
    $ErrorActionPreference = "Continue"
    $Stato = (& $Tailscale status 2>&1 | ForEach-Object { $_.ToString() }) -join " "
    $Portiere = (& $Tailscale serve status 2>&1 | ForEach-Object { $_.ToString() }) -join " "
    $ErrorActionPreference = "Stop"
    if ($Stato -match "Logged out") {
        Scrivi "ATTENZIONE: Tailscale e' scollegato. Il sito sara' raggiungibile solo da questo computer. Rimedio: tailscale up"
    } elseif ($Portiere -notmatch [string]$Porta) {
        Scrivi "ATTENZIONE: il portiere di Tailscale non risulta puntare alla porta $Porta. Rimedio: tailscale serve --bg $Porta"
    }
} else {
    Scrivi "Tailscale non installato: il sito resta raggiungibile solo da questo computer."
}

Scrivi "avvio del sito su 127.0.0.1:$Porta (fuori di qui ci pensa Tailscale)"

$ErrorActionPreference = "Continue"
$env:PYTHONUNBUFFERED = "1"

# uvicorn scrive i suoi log su stderr: il 2>&1 non e' un errore da correggere,
# e' l'unico modo di vederli. Il ToString() toglie l'involucro che PowerShell 5
# mette intorno alle righe di un programma esterno.
# La pipe scrive man mano, quindi il log si legge mentre il server gira.
& $Python $Script --host 127.0.0.1 --porta $Porta 2>&1 |
    ForEach-Object { "{0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $_.ToString() } |
    Add-Content -Path $Log -Encoding utf8

$Esito = $LASTEXITCODE
$ErrorActionPreference = "Stop"

# Ci si arriva solo se il server e' morto: o l'ha spento qualcuno, o e'
# caduto. Nel secondo caso l'esito diverso da zero fa scattare il riavvio
# automatico dell'attivita' pianificata.
Scrivi "server terminato (codice $Esito)"
exit $Esito
