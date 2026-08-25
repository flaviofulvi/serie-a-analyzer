# Registra la raccolta giornaliera nell'Utilita' di pianificazione di Windows.
#
# Da eseguire UNA SOLA VOLTA, in un PowerShell aperto come amministratore:
#
#   .\scripts\schedule_windows.ps1
#
# Per rimuoverla:
#
#   Unregister-ScheduledTask -TaskName "SerieAAnalyzer-Raccolta" -Confirm:$false
#   Unregister-ScheduledTask -TaskName "SerieAAnalyzer-Recupero" -Confirm:$false
#   Unregister-ScheduledTask -TaskName "SerieAAnalyzer-Formazioni" -Confirm:$false
#   Unregister-ScheduledTask -TaskName "SerieAAnalyzer-Momento" -Confirm:$false
#   Unregister-ScheduledTask -TaskName "SerieAAnalyzer-Web" -Confirm:$false
#
# Perche' quattro esecuzioni al giorno e non una sola: la finestra del piano
# gratuito e' di tre giorni, quindi una singola esecuzione mancata non e'
# ancora un danno, ma due di fila lo diventano. Quattro tentativi distribuiti
# rendono improbabile perdere un giorno anche col computer spento per qualche
# ora. Il costo e' basso: un giorno gia' chiuso non spende richieste, e le
# partite concluse non vengono riscaricate.

$ErrorActionPreference = "Stop"

$NomeTask = "SerieAAnalyzer-Raccolta"
$NomeRecupero = "SerieAAnalyzer-Recupero"
$NomeFormazioni = "SerieAAnalyzer-Formazioni"
$NomeMomento = "SerieAAnalyzer-Momento"
$NomeWeb = "SerieAAnalyzer-Web"
$Radice = Split-Path -Parent $PSScriptRoot
$Wrapper = Join-Path $Radice "scripts\run_daily.ps1"
$WrapperFormazioni = Join-Path $Radice "scripts\run_formazioni.ps1"
$WrapperMomento = Join-Path $Radice "scripts\run_momento.ps1"
$WrapperWeb = Join-Path $Radice "scripts\run_web.ps1"
$FileOrari = Join-Path $PSScriptRoot "orari_raccolta.ps1"

if (-not (Test-Path $Wrapper)) {
    Write-Error "Non trovo $Wrapper"
    exit 1
}
if (-not (Test-Path $FileOrari)) {
    Write-Error "Non trovo $FileOrari"
    exit 1
}
if (-not (Test-Path $WrapperFormazioni)) {
    Write-Error "Non trovo $WrapperFormazioni"
    exit 1
}
if (-not (Test-Path $WrapperMomento)) {
    Write-Error "Non trovo $WrapperMomento"
    exit 1
}
if (-not (Test-Path $WrapperWeb)) {
    Write-Error "Non trovo $WrapperWeb"
    exit 1
}

. $FileOrari

# --- chi esegue, e perche' non si vede --------------------------------------
#
# S4U ("esegui sia che l'utente abbia effettuato l'accesso o no") non e' un
# dettaglio di sicurezza: e' l'unica impostazione che rende queste attivita'
# davvero invisibili. Con il tipo di accesso predefinito, "Interattivo", ogni
# esecuzione apre una finestra di PowerShell dentro la sessione dell'utente:
# dura un secondo, ma prende il fuoco, e un gioco a schermo intero viene
# ridotto a icona. Ogni quarto d'ora, tutto il pomeriggio.
# Con S4U il processo gira in una sessione non interattiva: nessuna finestra,
# nessun lampo, niente fuoco rubato. -WindowStyle Hidden da solo non basta —
# la console viene creata e poi nascosta, e il lampo c'e' lo stesso.
#
# Non serve salvare nessuna password (S4U si basa sul solo nome utente) ne'
# -RunLevel Highest: la raccolta scrive soltanto dentro la cartella del
# progetto. L'unica cosa che S4U non porta con se' sono le credenziali di rete
# verso altre macchine, e qui non servono: si esce su internet in HTTPS con la
# chiave che sta nel file .env.
#
# Se registri da un account amministratore diverso dal tuo, correggi -UserId:
# le attivita' devono girare come l'utente proprietario della cartella.
$Principale = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U `
    -RunLevel Limited

$Azione = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Wrapper`"" `
    -WorkingDirectory $Radice

# 08:00, 14:00, 20:00, 23:45 (definiti in orari_raccolta.ps1). L'ultima serve a
# chiudere le partite serali, che in Serie A finiscono verso le 22:45.
$Trigger = $OrariRaccolta | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }

$Impostazioni = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
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
#   -MultipleInstances IgnoreNew  due raccolte in parallelo scriverebbero sullo
#                              stesso archivio: la seconda non parte.
#
# NON si usa -WakeToRun, ed e' una scelta, non una dimenticanza: accenderebbe
# il computer da solo all'ora prevista. Il computer e' dell'utente, non della
# raccolta. Le esecuzioni che cadono a macchina spenta si recuperano dopo,
# quando la si accende — vedi l'attivita' qui sotto. Se qualcuno rimettesse
# -WakeToRun, verifica_raccolta.py lo segnala.

# --- attivita' di recupero -------------------------------------------------
#
# Senza sveglia, tutto il recupero passa da qui. -StartWhenAvailable da solo
# non basta: e' Windows a decidere se e quando considerare "mancata" una
# esecuzione, e il risveglio dalla sospensione non e' un accesso, quindi ci
# sono risvegli in cui non riprende niente.
#
# Questa attivita' non chiede niente a Windows: guarda quando e' finita bene
# l'ultima raccolta e a che ora sarebbe dovuta partire l'ultima prevista. Se
# manca qualcosa la rifa', se non manca niente esce senza spendere una
# richiesta — quindi puo' scattare quante volte vuole, e ogni decisione resta
# scritta nel log.
$AzioneRecupero = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Wrapper`" -SoloSeMancata" `
    -WorkingDirectory $Radice

# All'accesso: copre l'accensione da spento.
$TriggerAccesso = New-ScheduledTaskTrigger -AtLogOn
$TriggerAccesso.Delay = "PT3M"   # il tempo che la rete torni su

# Al risveglio dalla sospensione, che non genera nessun accesso. I due eventi
# non sono equivalenti su tutte le macchine: su alcune il risveglio lascia solo
# Power-Troubleshooter/1, su altre anche Kernel-Power/107. Si ascoltano
# entrambi, tanto un doppio scatto e' innocuo.
$Sottoscrizione = @"
<QueryList>
  <Query Id="0" Path="System">
    <Select Path="System">*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and (EventID=1)]]</Select>
    <Select Path="System">*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=107)]]</Select>
  </Query>
</QueryList>
"@

$TriggerRisveglio = New-CimInstance `
    -CimClass (Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler) `
    -ClientOnly
$TriggerRisveglio.Enabled = $true
$TriggerRisveglio.Subscription = $Sottoscrizione
$TriggerRisveglio.Delay = "PT2M"

$ImpostazioniRecupero = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -RestartCount 3

# --- formazioni ufficiali ---------------------------------------------------
#
# Un ritmo completamente diverso dalla raccolta, per una ragione precisa: le
# formazioni escono una mezz'ora prima del calcio d'inizio e il registro
# rifiuta ogni pronostico datato dopo il via. La finestra utile dura minuti,
# non ore: qui non servono quattro esecuzioni al giorno ma un passaggio ogni
# quarto d'ora nella fascia in cui si gioca.
#
# Costa quasi niente. Senza partite imminenti lo script esce prima di stimare
# il modello e senza toccare l'API; e finche' i tabellini sul disco non
# bastano non scrive comunque nulla, per scelta (`--stato` dice a che punto e').
$AzioneFormazioni = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$WrapperFormazioni`"" `
    -WorkingDirectory $Radice

# Dalle 11 alle 23: il primo anticipo e' alle 12:30 e le formazioni escono
# un'ora prima, l'ultimo posticipo alle 20:45.
$TriggerFormazioni = New-ScheduledTaskTrigger -Daily -At "11:00"
$TriggerFormazioni.Repetition = (New-ScheduledTaskTrigger `
    -Once -At "11:00" `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Hours 12)).Repetition

# --- raccolta al momento giusto ---------------------------------------------
#
# Quote e indisponibili valgono quanto sono vicini al fischio d'inizio, e un
# orario fisso ci passa vicino per caso: il 22 agosto 2026 la corsa delle 20:00
# ha letto insieme le quote di una partita delle 20:45 e quelle di una del
# giorno dopo, che verranno rilette comunque. Questa attivita' gira spesso e
# decide da se': le quote e gli assenti delle partite che cominciano entro 40
# minuti, i dettagli e i risultati di quelle finite da circa due ore e un
# quarto.
#
# Costa quasi niente. Quando non e' il momento — cioe' quasi sempre — lo script
# esce senza spedire una sola richiesta.
$AzioneMomento = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$WrapperMomento`"" `
    -WorkingDirectory $Radice

# Dalle 11 alle 23:30. Il primo anticipo e' alle 12:30, quindi la sua vigilia
# comincia alle 11:50; l'ultimo posticipo delle 20:45 smette di interessarci
# verso le 23:00, piu' mezz'ora di margine per i recuperi.
$TriggerMomento = New-ScheduledTaskTrigger -Daily -At "11:00"
$TriggerMomento.Repetition = (New-ScheduledTaskTrigger `
    -Once -At "11:00" `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Minutes 750)).Repetition

# Piu' generoso del limite delle formazioni: una raccolta "dopo" scarica i
# dettagli di tutte le partite appena finite, e con il freno del client fra
# una richiesta e l'altra ci mette qualche minuto.
$ImpostazioniMomento = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

$ImpostazioniFormazioni = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# --- il sito sempre acceso --------------------------------------------------
#
# Non e' una raccolta: e' un processo che non finisce. Serve perche' il
# telefono possa aprire il sito passando per Tailscale, e per farlo il server
# deve essere gia' su quando qualcuno lo chiede - nessuno accende un computer
# per guardare una pagina.
#
# Il server ascolta solo su 127.0.0.1, come quando si sviluppa: e' Tailscale
# che lo pubblica dentro il tailnet (`tailscale serve --bg 8000`, configurato
# una volta sola). Con --host 0.0.0.0 il sito si aprirebbe anche a chiunque
# fosse sullo stesso Wi-Fi, e non e' quello che si vuole.
$AzioneWeb = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$WrapperWeb`"" `
    -WorkingDirectory $Radice

# All'accensione, che e' il momento vero: il sito deve esserci anche se
# nessuno ha ancora fatto l'accesso a Windows. L'accesso e' la seconda rete di
# sicurezza, per la macchina riaccesa senza passare di li'. Se il sito e' gia'
# su, run_web.ps1 se ne accorge dalla porta occupata ed esce senza fare nulla.
$TriggerWebAvvio = New-ScheduledTaskTrigger -AtStartup
$TriggerWebAvvio.Delay = "PT1M"    # il tempo che Tailscale salga
$TriggerWebAccesso = New-ScheduledTaskTrigger -AtLogOn
$TriggerWebAccesso.Delay = "PT1M"

# La differenza che conta rispetto a tutte le altre attivita': nessun limite
# di durata. Le altre si spengono da sole dopo 10-30 minuti perche' una
# raccolta che dura mezz'ora e' una raccolta bloccata; qui lo stesso valore
# spegnerebbe il sito ogni mezz'ora. PT0S significa "senza limite".
#
# Il riavvio automatico invece serve per davvero: se uvicorn cade, l'unico
# modo di accorgersene sarebbe il telefono che non apre piu' la pagina.
$ImpostazioniWeb = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 5

# --- registrazione ----------------------------------------------------------

if (Get-ScheduledTask -TaskName $NomeTask -ErrorAction SilentlyContinue) {
    Write-Host "Attivita' gia' presente: la sostituisco."
    Unregister-ScheduledTask -TaskName $NomeTask -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $NomeTask `
    -Action $Azione `
    -Trigger $Trigger `
    -Settings $Impostazioni `
    -Principal $Principale `
    -Description "Raccolta giornaliera della Serie A da API-Football (finestra di 3 giorni del piano gratuito)." | Out-Null

if (Get-ScheduledTask -TaskName $NomeRecupero -ErrorAction SilentlyContinue) {
    Write-Host "Attivita' di recupero gia' presente: la sostituisco."
    Unregister-ScheduledTask -TaskName $NomeRecupero -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $NomeRecupero `
    -Action $AzioneRecupero `
    -Trigger @($TriggerAccesso, $TriggerRisveglio) `
    -Settings $ImpostazioniRecupero `
    -Principal $Principale `
    -Description "Recupera la raccolta della Serie A saltata mentre il computer era spento o sospeso. Non accende il computer e non spende richieste se non manca niente." | Out-Null

if (Get-ScheduledTask -TaskName $NomeMomento -ErrorAction SilentlyContinue) {
    Write-Host "Attivita' del momento gia' presente: la sostituisco."
    Unregister-ScheduledTask -TaskName $NomeMomento -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $NomeMomento `
    -Action $AzioneMomento `
    -Trigger $TriggerMomento `
    -Settings $ImpostazioniMomento `
    -Principal $Principale `
    -Description "Quote e indisponibili nei 40 minuti prima del via, dettagli e risultati due ore e un quarto dopo. Non spende nulla quando non e' il momento." | Out-Null

if (Get-ScheduledTask -TaskName $NomeWeb -ErrorAction SilentlyContinue) {
    Write-Host "Attivita' del sito gia' presente: la sostituisco."
    Unregister-ScheduledTask -TaskName $NomeWeb -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $NomeWeb `
    -Action $AzioneWeb `
    -Trigger @($TriggerWebAvvio, $TriggerWebAccesso) `
    -Settings $ImpostazioniWeb `
    -Principal $Principale `
    -Description "Tiene acceso il sito su 127.0.0.1:8000, perche' il telefono lo raggiunga via Tailscale. Non espone niente a internet." | Out-Null

if (Get-ScheduledTask -TaskName $NomeFormazioni -ErrorAction SilentlyContinue) {
    Write-Host "Attivita' delle formazioni gia' presente: la sostituisco."
    Unregister-ScheduledTask -TaskName $NomeFormazioni -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $NomeFormazioni `
    -Action $AzioneFormazioni `
    -Trigger $TriggerFormazioni `
    -Settings $ImpostazioniFormazioni `
    -Principal $Principale `
    -Description "Pronostico rifatto con le formazioni ufficiali, nella mezz'ora prima del calcio d'inizio. Non fa nulla se non ci sono partite imminenti." | Out-Null

Write-Host ""
Write-Host "Attivita' registrata: $NomeTask"
Write-Host "Orari: $($OrariRaccolta -join ', ')  (il computer non viene acceso da solo)"
Write-Host "Attivita' registrata: $NomeRecupero (all'accesso e al risveglio, solo se manca qualcosa)"
Write-Host "Attivita' registrata: $NomeFormazioni (ogni 15 minuti dalle 11 alle 23)"
Write-Host "Attivita' registrata: $NomeMomento (ogni 15 minuti dalle 11 alle 23:30, agisce solo a ridosso delle partite)"
Write-Host "Attivita' registrata: $NomeWeb (il sito sempre acceso, all'accensione e all'accesso)"
Write-Host ""
Write-Host "Verifica:      .\.venv\Scripts\python.exe .\scripts\verifica_raccolta.py"
Write-Host "Formazioni:    .\.venv\Scripts\python.exe .\scripts\formazioni_ufficiali.py --stato"
Write-Host "Prova subito:  Start-ScheduledTask -TaskName $NomeRecupero"
Write-Host "Log:           $Radice\data\logs\"
Write-Host "Sito:          http://127.0.0.1:8000 - dal telefono, l'indirizzo dato da: tailscale serve status"
Write-Host ""
Write-Host "Tutte girano senza aprire nessuna finestra (accesso S4U)."
