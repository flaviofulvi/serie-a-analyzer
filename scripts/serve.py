"""Avvia l'applicazione web in locale.

    python scripts/serve.py             # http://127.0.0.1:8000
    python scripts/serve.py --ricarica  # si riavvia a ogni modifica del codice

Equivale a `uvicorn serie_a_analyzer.web.app:app`, ma sistema da solo il
percorso dei sorgenti: cosi' funziona anche senza aver installato il pacchetto.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


def main() -> int:
    parser = argparse.ArgumentParser(description="Serie A Analyzer — server locale")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--porta", type=int, default=8000)
    parser.add_argument(
        "--ricarica",
        action="store_true",
        help="riavvia automaticamente quando il codice cambia (per sviluppare)",
    )
    args = parser.parse_args()

    import uvicorn

    print(f"\n  Serie A Analyzer -> http://{args.host}:{args.porta}\n")
    uvicorn.run(
        "serie_a_analyzer.web.app:app",
        host=args.host,
        port=args.porta,
        reload=args.ricarica,
        reload_dirs=[str(SRC)] if args.ricarica else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
