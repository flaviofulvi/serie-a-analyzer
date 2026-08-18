# Registra la raccolta giornaliera nell'Utilita' di pianificazione di Windows.
#
# Da eseguire UNA SOLA VOLTA, in un PowerShell aperto come amministratore:
#
#   .\scripts\schedule_windows.ps1
#
# Per rimuoverla:
#
#   Unregister-ScheduledTask -TaskName "SerieAAnalyzer-Raccolta" -Confirm:$false
#
# Perche' quattro esecuzioni al giorno e non una sola: la finestra del piano
# gratuito e' di tre giorni, quindi una singola esecuzione mancata non e'
# ancora un danno, ma due di fila lo diventano. Quattro tentativi distribuiti
# rendono improbabile perdere un giorno anche col computer spento per qualche
# ora. Il costo e' basso: un giorno gia' chiuso non spende richieste, e le
# partite concluse non vengono riscaricate.

$ErrorActionPreference = "Stop"

$NomeTask = "SerieAAnalyzer-Raccolta"
$Radice = Split-Path -Parent $PSScriptRoot
$Wrapper = Join-Path $Radice "scripts\run_daily.ps1"

if (-not (Test-Path $Wrapper)) {
    Write-Error "Non trovo $Wrapper"
    exit 1
}

$Azione = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Wrapper`"" `
    -WorkingDirectory $Radice

# 08:00, 14:00, 20:00, 23:45. L'ultima serve a chiudere le partite serali,
# che in Serie A finiscono verso le 22:45.
$Orari = @("08:00", "14:00", "20:00", "23:45")
$Trigger = $Orari | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }

$Impostazioni = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -RestartCount 3

# Queste impostazioni non sono cosmetiche: sono i modi in cui una raccolta
# giornaliera fallisce in silenzio su un portatile.
#
#   -StartWhenAvailable        se il computer era spento all'orario previsto,
#                              l'esecuzione parte appena si riaccende;
#   -AllowStartIfOnBatteries   altrimenti Windows salta l'esecuzione ogni
#   -DontStopIfGoingOnBatteries volta che il portatile e' staccato dalla presa,
#                              ed e' il comportamento predefinito: una domenica
#                              pomeriggio sul divano diventerebbe un buco;
#   -WakeToRun                 sveglia il computer sospeso all'ora prevista.
#
# Le partite serali finiscono verso le 22:45 e i loro id restano leggibili per
# poco piu' di 24 ore: nessuna di queste e' una rifinitura.

if (Get-ScheduledTask -TaskName $NomeTask -ErrorAction SilentlyContinue) {
    Write-Host "Attivita' gia' presente: la sostituisco."
    Unregister-ScheduledTask -TaskName $NomeTask -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $NomeTask `
    -Action $Azione `
    -Trigger $Trigger `
    -Settings $Impostazioni `
    -Description "Raccolta giornaliera della Serie A da API-Football (finestra di 3 giorni del piano gratuito)." | Out-Null

Write-Host ""
Write-Host "Attivita' registrata: $NomeTask"
Write-Host "Orari: $($Orari -join ', ')"
Write-Host ""
Write-Host "Verifica:      Get-ScheduledTask -TaskName $NomeTask"
Write-Host "Prova subito:  Start-ScheduledTask -TaskName $NomeTask"
Write-Host "Log:           $Radice\data\logs\"
