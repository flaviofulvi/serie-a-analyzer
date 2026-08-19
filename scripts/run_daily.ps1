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

[CmdletBinding()]
param(
    # Modalita' di recupero, usata dall'attivita' che parte all'accensione e al
    # risveglio del computer: la raccolta parte solo se l'ultima esecuzione
    # riuscita e' anteriore all'ultimo orario previsto, cioe' solo se c'e'
    # davvero qualcosa da recuperare. Senza questo controllo ogni accensione
    # spenderebbe richieste per rifare un lavoro appena fatto.
    [switch]$SoloSeMancata
)

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

# Quando e' finita bene l'ultima raccolta. Sta in un file suo e non nel log
# perche' il recupero deve poterlo leggere senza interpretare del testo, e
# perche' i log sono divisi per giorno mentre l'ultima esecuzione utile puo'
# essere di ieri sera.
$Marcatore = Join-Path $LogDir "ultima-esecuzione.txt"

function Ultimo-OrarioPrevisto {
    # L'orario di raccolta piu' recente che e' gia' passato. Si guarda anche a
    # ieri: alle 3 di notte l'ultimo orario previsto e' il 23:45 del giorno
    # prima, non un orario di oggi.
    param([string[]]$Orari)

    $adesso = Get-Date
    $istanti = foreach ($giorno in 0, -1) {
        foreach ($ora in $Orari) {
            [datetime]::ParseExact(
                $adesso.AddDays($giorno).ToString("yyyy-MM-dd") + " " + $ora,
                "yyyy-MM-dd HH:mm",
                [Globalization.CultureInfo]::InvariantCulture)
        }
    }
    ($istanti | Where-Object { $_ -le $adesso } | Sort-Object)[-1]
}

if ($SoloSeMancata) {
    $FileOrari = Join-Path $PSScriptRoot "orari_raccolta.ps1"
    if (-not (Test-Path $FileOrari)) {
        Write-Error "Non trovo ${FileOrari}: senza gli orari non posso sapere cosa e' stato saltato."
        exit 1
    }
    . $FileOrari

    $Previsto = Ultimo-OrarioPrevisto -Orari $OrariRaccolta

    # Marcatore assente o illeggibile: si raccoglie. Una raccolta in piu' costa
    # qualche richiesta, una in meno puo' costare un giorno di partite.
    $Ultima = $null
    if (Test-Path $Marcatore) {
        try {
            $Ultima = [datetime]::ParseExact(
                (Get-Content -Path $Marcatore -Raw).Trim(),
                "yyyy-MM-dd HH:mm:ss",
                [Globalization.CultureInfo]::InvariantCulture)
        } catch {
            $Ultima = $null
        }
    }

    if ($Ultima -and $Ultima -ge $Previsto) {
        Add-Content -Path $Log -Encoding utf8 -Value (
            "`n===== recupero ${Inizio}: niente da fare " +
            "(ultimo orario previsto $($Previsto.ToString('yyyy-MM-dd HH:mm')), " +
            "ultima raccolta $($Ultima.ToString('yyyy-MM-dd HH:mm:ss'))) =====")
        exit 0
    }

    $Mancata = if ($Ultima) { $Ultima.ToString('yyyy-MM-dd HH:mm:ss') } else { "mai" }
    Add-Content -Path $Log -Encoding utf8 -Value (
        "`n===== recupero ${Inizio}: esecuzione mancata " +
        "(previsto $($Previsto.ToString('yyyy-MM-dd HH:mm')), ultima raccolta $Mancata) =====")
} else {
    Add-Content -Path $Log -Encoding utf8 -Value "`n===== esecuzione $Inizio ====="
}

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
    # Solo dopo un esito buono: se la raccolta e' fallita, la prossima
    # accensione deve considerarla ancora da recuperare.
    Set-Content -Path $Marcatore -Encoding utf8 -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
}

exit $Esito
