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

# Sono DUE, non piu' quattro, e le due che restano fanno due lavori che un
# orario relativo alla partita non puo' fare:
#
#   08:00  legge il calendario della finestra ieri/oggi/domani. Gli id delle
#          partite si leggono solo li' dentro e persi sono persi, quindi
#          questa corsa non dipende da nessuna partita: deve esserci anche il
#          lunedi' che non si gioca.
#   23:45  chiude la giornata, quando anche il posticipo delle 20:45 e' finito
#          da un pezzo e i risultati sono definitivi. Solo i RISULTATI: i
#          tabellini del posticipo li lascia alle 08:00 del giorno dopo (vedi
#          `dettagli_possono_aspettare` in data/daily.py).
#
# Tutto il resto — quote, indisponibili, dettagli — non ha piu' un orario: lo
# decide ogni partita, e ci pensa l'attivita' "Momento" (run_momento.ps1), che
# gira ogni quarto d'ora e quasi sempre non fa niente. Le 14:00 e le 20:00
# erano un modo di sperare che un orario fisso capitasse vicino a un fischio
# d'inizio.
#
# Le 08:00 hanno pero' acquistato un secondo compito, il 23 agosto 2026: sono
# la corsa che raccoglie i tabellini delle partite serali. Non perche' sia un
# bell'orario, ma perche' e' la prima raccolta dopo il reset del budget (le
# 02:00 italiane) e quindi l'unica che quelle dodici richieste se le puo'
# permettere. Se un giorno questa lista cambiasse, e' questo che va tenuto:
# deve esistere una corsa a budget intero dopo la mezzanotte UTC.
$OrariRaccolta = @("08:00", "23:45")
