# Avvio del pronostico con le formazioni ufficiali, per l'Utilita' di
# pianificazione di Windows.
#
# Gira spesso — ogni quarto d'ora nella fascia in cui si gioca — perche' le
# formazioni escono una mezz'ora prima del via e la finestra utile si chiude
# al fischio d'inizio. Quasi tutte quelle esecuzioni non fanno niente: lo
# script esce subito quando non c'e' nessuna partita imminente, senza stimare
# il modello e senza toccare l'API.
#
# Registrazione (una sola volta, da un PowerShell come amministratore):
#
#   .\scripts\schedule_windows.ps1
#
# Esecuzione manuale:
#
#   .\scripts\run_formazioni.ps1

$ErrorActionPreference = "Stop"

$Radice = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Radice ".venv\Scripts\python.exe"
$Script = Join-Path $Radice "scripts\formazioni_ufficiali.py"
$LogDir = Join-Path $Radice "data\logs"

if (-not (Test-Path $Python)) {
    Write-Error "Ambiente virtuale non trovato in $Python. Creare .venv e installare le dipendenze."
    exit 1
}

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# Un file separato da quello della raccolta: qui le righe sono molte e quasi
# tutte dicono "niente da fare". Mescolarle al log della raccolta renderebbe
# illeggibile proprio quello che si va a guardare quando qualcosa va storto.
$Log = Join-Path $LogDir ("formazioni-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

$ErrorActionPreference = "Continue"
$env:PYTHONUNBUFFERED = "1"

$Uscita = & $Python $Script 2>&1 | ForEach-Object { $_.ToString() }
$Esito = $LASTEXITCODE
$ErrorActionPreference = "Stop"

# Il caso normale e' "nessuna partita entro N minuti": una riga sola, non un
# blocco. Un log che ripete dieci righe ogni quarto d'ora per mesi non lo
# legge nessuno, ed e' come non averlo.
$Inizio = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
if ($Esito -eq 0 -and $Uscita.Count -le 2) {
    Add-Content -Path $Log -Encoding utf8 -Value "$Inizio  $($Uscita -join ' ')".Trim()
} else {
    Add-Content -Path $Log -Encoding utf8 -Value "`n===== esecuzione $Inizio ====="
    Add-Content -Path $Log -Encoding utf8 -Value $Uscita
    if ($Esito -ne 0) {
        Add-Content -Path $Log -Encoding utf8 -Value "ESITO: FALLITO (codice $Esito)"
    }
}

exit $Esito
