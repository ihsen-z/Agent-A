#!/usr/bin/env python3
"""Lance un cycle de rapprochement et produit le tableau de suivi + les relances."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement.exclusions import Referentiel          # noqa: E402
from rapprochement.moteur import Moteur                    # noqa: E402
from rapprochement.parseurs import (                       # noqa: E402
    lire_dossiers,
    lire_pieces,
    lire_releve,
)
from rapprochement.rapport import ecrire_suivi, resume     # noqa: E402
from rapprochement.relances import construire_toutes       # noqa: E402


def main() -> int:
    parseur = argparse.ArgumentParser(description=__doc__)
    parseur.add_argument("--entree", default=str(RACINE / "data" / "corpus"))
    parseur.add_argument("--sortie", default=str(RACINE / "data" / "sorties"))
    parseur.add_argument(
        "--exclusions",
        default=str(RACINE / "data" / "corpus" / "exclusions_dossier.csv"),
        help="exclusions apprises par dossier (optionnel)",
    )
    parseur.add_argument(
        "--ecrire-relances",
        action="store_true",
        help="ecrit les brouillons de relance dans sortie/relances/",
    )
    args = parseur.parse_args()

    entree, sortie = Path(args.entree), Path(args.sortie)
    operations = lire_releve(entree / "releve_bancaire.csv")
    pieces = lire_pieces(entree / "pieces_recues.csv")
    dossiers = lire_dossiers(entree / "dossiers.csv")

    moteur = Moteur(Referentiel.depuis_csv(args.exclusions))
    rapprochements = moteur.rapprocher(operations, pieces)

    chemin_suivi = ecrire_suivi(rapprochements, sortie / "tableau_suivi.csv")
    relances = construire_toutes(dossiers, rapprochements)

    print(resume(rapprochements))
    print()
    print(f"Tableau de suivi  : {chemin_suivi}")
    print(f"Relances a envoyer: {len(relances)}")
    for relance in relances:
        print(
            f"  {relance.dossier:16s} {relance.nombre_pieces:3d} piece(s)  "
            f"{relance.montant_total:>10.2f} EUR  -> {relance.destinataire}"
        )

    if args.ecrire_relances:
        dossier_relances = sortie / "relances"
        dossier_relances.mkdir(parents=True, exist_ok=True)
        horodatage = dt.date.today().isoformat()
        for relance in relances:
            chemin = dossier_relances / f"{horodatage}_{relance.dossier}.txt"
            chemin.write_text(
                f"A: {relance.destinataire}\nObjet: {relance.objet}\n\n{relance.corps}\n",
                encoding="utf-8",
            )
        print(f"\nBrouillons ecrits dans {dossier_relances}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
