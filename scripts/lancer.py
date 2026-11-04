#!/usr/bin/env python3
"""Lance un cycle de rapprochement et produit le tableau de suivi + les relances."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement.cycle import FICHIERS, executer        # noqa: E402
from rapprochement.parseurs import ErreurFormat            # noqa: E402
from rapprochement.rapport import ecrire_suivi, resume     # noqa: E402


def main() -> int:
    parseur = argparse.ArgumentParser(description=__doc__)
    parseur.add_argument("--entree", default=str(RACINE / "data" / "corpus"))
    parseur.add_argument("--sortie", default=str(RACINE / "data" / "sorties"))
    parseur.add_argument(
        "--exclusions",
        default=None,
        help=f"exclusions apprises par dossier (defaut : <entree>/{FICHIERS['exclusions']})",
    )
    parseur.add_argument(
        "--alias",
        default=None,
        help=f"alias des libelles fournisseurs (defaut : <entree>/{FICHIERS['alias']})",
    )
    parseur.add_argument(
        "--ecrire-relances",
        action="store_true",
        help="ecrit les brouillons de relance dans sortie/relances/",
    )
    args = parseur.parse_args()

    entree, sortie = Path(args.entree), Path(args.sortie)
    try:
        cycle = executer(entree, exclusions=args.exclusions, alias=args.alias)
    except (ErreurFormat, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2

    regles_apprises = sum(len(r) for r in cycle.referentiel.apprises.values())
    print(f"Référentiel : {len(cycle.alias)} alias fournisseur(s), "
          f"{regles_apprises} règle(s) apprise(s) par dossier")
    print()

    chemin_suivi = ecrire_suivi(cycle.rapprochements, sortie / "tableau_suivi.csv")
    print(resume(cycle.rapprochements))
    print()
    print(f"Tableau de suivi  : {chemin_suivi}")
    print(f"Relances à envoyer: {len(cycle.relances)}")
    for relance in cycle.relances:
        print(
            f"  {relance.dossier:16s} {relance.nombre_pieces:3d} pièce(s)  "
            f"{relance.montant_total:>10.2f} EUR  -> {relance.destinataire}"
        )

    if args.ecrire_relances:
        dossier_relances = sortie / "relances"
        dossier_relances.mkdir(parents=True, exist_ok=True)
        horodatage = dt.date.today().isoformat()
        for relance in cycle.relances:
            chemin = dossier_relances / f"{horodatage}_{relance.dossier}.txt"
            chemin.write_text(
                f"À : {relance.destinataire}\nObjet : {relance.objet}\n\n{relance.corps}\n",
                encoding="utf-8",
            )
        print(f"\nBrouillons écrits dans {dossier_relances}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
