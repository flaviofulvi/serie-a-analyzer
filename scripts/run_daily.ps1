# Avvio della raccolta giornaliera, pensato per l'Utilita' di pianificazione
# di Windows.
#
# Rispetto al lanciare direttamente daily_collect.py, questo wrapper aggiunge
# le due cose che servono a un lavoro non sorvegliato:
#
#   * un log su file, perche' un errore alle 3 di notte deve lasciare traccia;
#   * un codice di uscita corretto, cosi' l'Utilita' di pianificazione sa se
#     e' andata male e puo' riprovare.
#
# Registrazione (una sola volta, da un PowerShell come amministratore):
#
#   .\scripts\schedule_windows.ps1
#
# Esecuzione manuale:
#
#   .\scripts\run_daily.ps1

$ErrorActionPreference = "Stop"

$Radice = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Radice ".venv\Scripts\python.exe"
$Script = Join-Path $Radice "scripts\daily_collect.py"
$LogDir = Join-Path $Radice "data\logs"

if (-not (Test-Path $Python)) {
    Write-Error "Ambiente virtuale non trovato in $Python. Creare .venv e installare le dipendenze."
    exit 1
}

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# Un file per giorno: si trova subito quello del giorno in cui e' andata male,
# e non cresce all'infinito in un unico file da centinaia di MB.
$Log = Join-Path $LogDir ("raccolta-" + (Get-Date -Format "yyyy-MM-dd") + ".log")
$Inizio = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

Add-Content -Path $Log -Encoding utf8 -Value "`n===== esecuzione $Inizio ====="

# PowerShell 5.1 trasforma ogni riga che un programma esterno scrive su stderr
# in un errore vero e proprio: con ErrorActionPreference "Stop" il primo
# messaggio di avanzamento della raccolta uccideva il wrapper prima ancora che
# scrivesse una riga di log. Qui serve il comportamento opposto: raccogliere
# tutto nel file e giudicare solo dal codice di uscita.
$ErrorActionPreference = "Continue"

# Senza questo, il riepilogo (stdout, bufferizzato) finisce nel log DOPO le
# righe di avanzamento (stderr, non bufferizzato): un log da leggere a
# ritroso, proprio quando serve capire in fretta cosa e' andato storto.
$env:PYTHONUNBUFFERED = "1"

& $Python $Script 2>&1 | ForEach-Object { $_.ToString() } | Add-Content -Path $Log -Encoding utf8 -PassThru
$Esito = $LASTEXITCODE
$ErrorActionPreference = "Stop"

if ($Esito -ne 0) {
    Add-Content -Path $Log -Encoding utf8 -Value "ESITO: FALLITO (codice $Esito)"
} else {
    Add-Content -Path $Log -Encoding utf8 -Value "ESITO: ok"
}

exit $Esito
