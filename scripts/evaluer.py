#!/usr/bin/env python3
"""Mesure la qualite du rapprochement contre la verite terrain.

C'est le script qui produit les chiffres vendables. Les deux seuls qui comptent
commercialement :

  - fausses alertes : une operation signalee "manquante" alors qu'elle ne l'est
    pas. C'est ce qui fait abandonner l'outil. Objectif : < 2 %.
  - manques rates   : une piece reellement manquante non signalee. C'est ce qui
    fait rater une declaration. Objectif : 0 %.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement.exclusions import Referentiel          # noqa: E402
from rapprochement.modeles import Statut                   # noqa: E402
from rapprochement.moteur import Moteur                    # noqa: E402
from rapprochement.parseurs import lire_pieces, lire_releve  # noqa: E402

# Le comptable ne distingue pas "justifie" de "a verifier" : dans les deux cas
# il ne relance pas le client. On evalue donc sur l'action, pas sur l'etiquette.
def action(statut: Statut) -> str:
    if statut is Statut.MANQUANT:
        return "relancer"
    if statut is Statut.HORS_PERIMETRE:
        return "ignorer"
    return "traiter"


ACTION_ATTENDUE = {
    "manquant": "relancer",
    "hors_perimetre": "ignorer",
    "justifie": "traiter",
    "partiel": "traiter",
}


def main() -> int:
    parseur = argparse.ArgumentParser(description=__doc__)
    parseur.add_argument("--entree", default=str(RACINE / "data" / "corpus"))
    parseur.add_argument("--detail", action="store_true", help="liste les erreurs")
    args = parseur.parse_args()

    entree = Path(args.entree)
    operations = lire_releve(entree / "releve_bancaire.csv")
    pieces = lire_pieces(entree / "pieces_recues.csv")

    with open(entree / "verite_terrain.csv", newline="", encoding="utf-8") as f:
        verite = {l["reference"]: l for l in csv.DictReader(f)}

    moteur = Moteur(Referentiel.depuis_csv(entree / "exclusions_dossier.csv"))
    rapprochements = moteur.rapprocher(operations, pieces)

    matrice: Counter[tuple[str, str]] = Counter()
    erreurs: list[str] = []
    par_statut_attendu: Counter[str] = Counter()
    justes_par_statut: Counter[str] = Counter()

    for r in rapprochements:
        attendu_brut = verite.get(r.operation.reference, {}).get("statut")
        if attendu_brut is None:
            continue
        attendu = ACTION_ATTENDUE[attendu_brut]
        obtenu = action(r.statut)
        matrice[(attendu, obtenu)] += 1
        par_statut_attendu[attendu_brut] += 1
        if attendu == obtenu:
            justes_par_statut[attendu_brut] += 1
        else:
            erreurs.append(
                f"  {r.operation.reference}  attendu={attendu_brut:14s} "
                f"obtenu={r.statut.value:14s} regle={r.regle:22s} "
                f"| {r.operation.libelle[:52]}"
            )

    total = sum(matrice.values()) or 1
    justes = sum(v for (a, o), v in matrice.items() if a == o)

    fausses_alertes = matrice[("ignorer", "relancer")] + matrice[("traiter", "relancer")]
    a_relancer = sum(v for (a, _), v in matrice.items() if a == "relancer") or 1
    manques_rates = sum(
        v for (a, o), v in matrice.items() if a == "relancer" and o != "relancer"
    )
    non_relancer = total - a_relancer

    print(f"Operations evaluees : {total}")
    print(f"Decisions correctes : {justes} ({100 * justes / total:.1f} %)")
    print()
    print("Par categorie attendue :")
    for statut, n in sorted(par_statut_attendu.items()):
        print(f"  {statut:16s} {justes_par_statut[statut]:4d}/{n:<4d}  "
              f"({100 * justes_par_statut[statut] / n:5.1f} %)")
    print()
    print("Les deux chiffres qui comptent :")
    print(
        f"  Fausses alertes  : {fausses_alertes}/{non_relancer} "
        f"({100 * fausses_alertes / max(non_relancer, 1):.2f} %)   cible < 2 %"
    )
    print(
        f"  Manques rates    : {manques_rates}/{a_relancer} "
        f"({100 * manques_rates / a_relancer:.2f} %)   cible 0 %"
    )

    if args.detail and erreurs:
        print(f"\nDetail des {len(erreurs)} erreurs :")
        for ligne in erreurs[:40]:
            print(ligne)
        if len(erreurs) > 40:
            print(f"  ... et {len(erreurs) - 40} autres")

    return 0 if fausses_alertes / max(non_relancer, 1) < 0.02 and manques_rates == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
