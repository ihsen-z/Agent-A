#!/usr/bin/env python3
"""Mesure la qualite du rapprochement contre la verite terrain.

C'est le script qui produit les chiffres vendables. Les deux seuls qui comptent
commercialement :

  - fausses alertes : une operation signalee "manquante" alors qu'elle ne l'est
    pas. C'est ce qui fait abandonner l'outil. Objectif : < 2 %.
  - manques rates   : une piece reellement manquante non signalee. C'est ce qui
    fait rater une declaration. Objectif : 0 %.

Les alias et les exclusions apprises du dossier d'entree sont pris en compte,
comme dans scripts/lancer.py et dans l'interface.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement.cycle import FICHIERS, executer         # noqa: E402
from rapprochement.evaluation import evaluer, lire_verite   # noqa: E402


def main() -> int:
    parseur = argparse.ArgumentParser(description=__doc__)
    parseur.add_argument("--entree", default=str(RACINE / "data" / "corpus"))
    parseur.add_argument("--detail", action="store_true", help="liste les erreurs")
    args = parseur.parse_args()

    entree = Path(args.entree)
    cycle = executer(entree, exiger_dossiers=False)
    ev = evaluer(cycle.rapprochements, lire_verite(entree / FICHIERS["verite"]))

    print(f"Operations evaluees : {ev.total}")
    print(f"Decisions correctes : {ev.justes} ({100 * ev.taux_correct:.1f} %)")
    print()
    print("Par categorie attendue :")
    for statut, (justes, n) in ev.par_statut.items():
        print(f"  {statut:16s} {justes:4d}/{n:<4d}  ({100 * justes / n:5.1f} %)")
    print()
    print("Les deux chiffres qui comptent :")
    print(
        f"  Fausses alertes  : {ev.fausses_alertes}/{ev.non_relancer} "
        f"({100 * ev.taux_fausses_alertes:.2f} %)   cible < 2 %"
    )
    print(
        f"  Manques rates    : {ev.manques_rates}/{ev.a_relancer} "
        f"({100 * ev.taux_manques_rates:.2f} %)   cible 0 %"
    )

    if args.detail and ev.erreurs:
        print(f"\nDetail des {len(ev.erreurs)} erreurs :")
        for e in ev.erreurs[:40]:
            r = e.rapprochement
            print(
                f"  {r.operation.reference}  attendu={e.attendu:14s} "
                f"obtenu={r.statut.value:14s} regle={r.regle:22s} "
                f"| {r.operation.libelle[:52]}"
            )
        if len(ev.erreurs) > 40:
            print(f"  ... et {len(ev.erreurs) - 40} autres")

    return 0 if ev.cibles_atteintes else 1


if __name__ == "__main__":
    raise SystemExit(main())
