#!/usr/bin/env python3
"""Mesure le moteur sur N corpus synthetiques (une graine chacun) et agrege les deux
chiffres qui comptent. Sort en erreur si un manque est rate (CA-02 : zero) ou si les
fausses alertes depassent 2 % (CA-01).

Un seul corpus ne prouve rien : le moteur a longtemps ete juge sur UNE graine, et un
balayage de 60 a revele 0,89 % de manques rates. Utilisation :

    python3 scripts/balayage_graines.py                       # 40 graines a 18 % et 40 %
    python3 scripts/balayage_graines.py --nombre 200 --taux 0.18 0.40 0.70
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]


def mesurer(graine: int, taux: float) -> tuple[int, int, int, int]:
    """(fausses alertes, operations evaluees, manques rates, pieces manquantes)."""
    with tempfile.TemporaryDirectory() as dossier:
        generation = subprocess.run(
            [sys.executable, str(RACINE / "scripts" / "generer_corpus.py"), "--sortie", dossier,
             "--graine", str(graine), "--taux-manquant", str(taux)],
            capture_output=True, text=True,
        )
        if generation.returncode != 0:
            raise SystemExit(f"generation impossible (graine {graine}) :\n{generation.stderr}")
        sortie = subprocess.run(
            [sys.executable, str(RACINE / "scripts" / "evaluer.py"), "--entree", dossier],
            capture_output=True, text=True,
        ).stdout
    fausses = re.search(r"Fausses alertes\s*:\s*(\d+)/(\d+)", sortie)
    manques = re.search(r"Manques rates\s*:\s*(\d+)/(\d+)", sortie)
    if not fausses or not manques:
        raise SystemExit(f"sortie de evaluer.py illisible (graine {graine}) :\n{sortie}")
    return int(fausses.group(1)), int(fausses.group(2)), int(manques.group(1)), int(manques.group(2))


def main() -> int:
    parseur = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parseur.add_argument("--nombre", type=int, default=40, help="graines par taux (defaut 40)")
    parseur.add_argument("--depart", type=int, default=1, help="premiere graine (defaut 1)")
    parseur.add_argument("--taux", type=float, nargs="+", default=[0.18, 0.40], help="taux de pieces manquantes")
    args = parseur.parse_args()

    echec = False
    for taux in args.taux:
        fa = op = mr = pm = 0
        fautives: list[int] = []
        for graine in range(args.depart, args.depart + args.nombre):
            a, b, c, d = mesurer(graine, taux)
            fa, op, mr, pm = fa + a, op + b, mr + c, pm + d
            if a or c:
                fautives.append(graine)
        part_fausses = 100 * fa / op if op else 0.0
        print(f"taux {taux:.2f}  {args.nombre} corpus  "
              f"manques rates {mr}/{pm}   fausses alertes {fa}/{op} ({part_fausses:.2f} %)"
              + (f"   graines fautives : {fautives}" if fautives else ""))
        if mr > 0 or part_fausses >= 2.0:
            echec = True
    print("ECHEC : un critere bloquant n'est pas tenu" if echec else "OK : zero manque rate, fausses alertes < 2 %")
    return 1 if echec else 0


if __name__ == "__main__":
    raise SystemExit(main())
