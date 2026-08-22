# Raccolta "al momento giusto", per l'Utilita' di pianificazione di Windows.
#
# Gira ogni quarto d'ora nella fascia in cui si gioca, e quasi sempre non fa
# niente: esce subito, senza toccare l'API, se non c'e' una partita che sta per
# cominciare o che e' appena finita.
#
# Perche' non a orari fissi. Le due cose che questa raccolta prende — le quote
# e gli indisponibili — valgono quanto sono VICINE al fischio d'inizio: le
# quote dopo il via non esistono piu', e una lista di assenti letta a partita
# finita sa chi non e' sceso in campo davvero. Un orario fisso ci passa vicino
# per caso: il 22 agosto 2026 la corsa delle 20:00 ha letto insieme le quote di
# una partita delle 20:45 e quelle di una del giorno dopo, che verranno
# rilette. Il momento invece lo decide ogni partita.
#
# Registrazione (una sola volta, da un PowerShell come amministratore):
#
#   .\scripts\schedule_windows.ps1
#
# Esecuzione manuale:
#
#   .\scripts\run_momento.ps1

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

# Stesso file della raccolta a orari fissi: e' la stessa raccolta, decisa in un
# altro modo. Chi va a vedere perche' manca una quota vuole trovare tutto li'.
$Log = Join-Path $LogDir ("raccolta-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

$ErrorActionPreference = "Continue"
$env:PYTHONUNBUFFERED = "1"

$Uscita = & $Python $Script --solo-al-momento-giusto 2>&1 | ForEach-Object { $_.ToString() }
$Esito = $LASTEXITCODE
$ErrorActionPreference = "Stop"

# Il caso normale e' "non e' il momento": una riga sola, non un blocco. Un log
# che ripete dieci righe ogni quarto d'ora per mesi non lo legge nessuno.
$Inizio = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
if ($Esito -eq 0 -and $Uscita.Count -le 2) {
    Add-Content -Path $Log -Encoding utf8 -Value "$Inizio  $($Uscita -join ' ')".Trim()
} else {
    Add-Content -Path $Log -Encoding utf8 -Value "`n===== momento $Inizio ====="
    Add-Content -Path $Log -Encoding utf8 -Value $Uscita
    if ($Esito -ne 0) {
        Add-Content -Path $Log -Encoding utf8 -Value "ESITO: FALLITO (codice $Esito)"
    }
}

exit $Esito
