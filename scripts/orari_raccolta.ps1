# Gli orari della raccolta giornaliera, in un posto solo.
#
# Li leggono due script che devono per forza essere d'accordo:
#
#   schedule_windows.ps1  registra i trigger a questi orari;
#   run_daily.ps1         con -SoloSeMancata deve capire se un'esecuzione e'
#                         stata saltata, e per saperlo deve sapere a che ora
#                         sarebbe dovuta avvenire.
#
# Se le due liste divergessero, il recupero all'accensione smetterebbe di
# recuperare senza dirlo a nessuno: esattamente il guasto silenzioso che qui si
# cerca di evitare.

$OrariRaccolta = @("08:00", "14:00", "20:00", "23:45")
