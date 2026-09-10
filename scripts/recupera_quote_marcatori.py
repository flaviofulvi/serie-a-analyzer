"""Recupera le quote sui marcatori rimaste nella cache. Non spende richieste.

Fino all'11 settembre 2026 la raccolta leggeva le quote sui marcatori insieme
alle altre — stessa risposta, stessa richiesta — e le buttava. Di ogni partita
e' rimasta in cache l'ultima lettura: la cache delle quote ha una scadenza di
mezz'ora, ma una partita gia' giocata non viene piu' richiesta, quindi la sua
ultima risposta resta li' finche' nessuno la cancella. E' l'unica copia che
esista: API-Football le quote non le archivia.

    python scripts/recupera_quote_marcatori.py --solo-verifica   # cosa entrerebbe
    python scripts/recupera_quote_marcatori.py                   # le salva

Si puo' rilanciare quante volte si vuole: la stessa lettura non si duplica.
L'istante della lettura e' quello in cui la risposta e' entrata in cache, e i
minuti al via si calcolano da li' — cosi' una lettura fatta a partita iniziata
resta riconoscibile e non passa per una chiusura.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADICE / "src"))

from serie_a_analyzer.config import ConfigError, build_prediction_store, load_settings  # noqa: E402
from serie_a_analyzer.data.odds import parse_marcatori  # noqa: E402


def letture_in_cache(cartella: Path, league_id: int):
    """Ogni partita di campionato con quote sui marcatori: (id, via, letta_il, quote)."""
    for file in sorted(cartella.glob("odds__*.json")):
        try:
            busta = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  saltato {file.name}: {exc}")
            continue
        payload = busta.get("payload") or {}
        risposta = payload.get("response") if isinstance(payload, dict) else payload
        letta_il = datetime.fromtimestamp(float(busta["cached_at"]), tz=timezone.utc)
        for elemento in risposta or []:
            # Una richiesta per data risponde con tutti i campionati: qui
            # interessa solo il nostro.
            if (elemento.get("league") or {}).get("id") != league_id:
                continue
            partita = elemento.get("fixture") or {}
            quote = parse_marcatori(elemento)
            if partita.get("id") is None or not partita.get("date") or not quote:
                continue
            via = datetime.fromisoformat(partita["date"])
            yield int(partita["id"]), via, letta_il, quote


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--solo-verifica", action="store_true",
        help="mostra cosa entrerebbe senza scrivere niente",
    )
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc)
        return 1

    letture = list(letture_in_cache(settings.cache_dir, settings.league_id))
    if not letture:
        print("Nessuna quota sui marcatori in cache.")
        return 0

    registro = None if args.solo_verifica else build_prediction_store(settings)
    salvate = 0
    print(f"{'partita':>9}  {'via (UTC)':<16}  {'letta (UTC)':<16}  {'min. al via':>11}  quote")
    for fixture_id, via, letta_il, quote in sorted(letture, key=lambda r: r[1]):
        minuti = int((via - letta_il).total_seconds() // 60)
        if registro is not None:
            salvate += registro.salva_quote_marcatori(
                fixture_id=fixture_id, kickoff_utc=via, quote=quote, now=letta_il
            )
        print(f"{fixture_id:>9}  {via:%Y-%m-%d %H:%M}  {letta_il:%Y-%m-%d %H:%M}  "
              f"{minuti:>11}  {len(quote)}")

    totale = sum(len(r[3]) for r in letture)
    print(f"\nPartite: {len(letture)}   quote lette: {totale}")
    if registro is None:
        print("Solo verifica: niente e' stato scritto.")
    else:
        print(f"Quote nuove nel registro: {salvate} (le altre c'erano gia')")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
