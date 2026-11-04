"""Un cycle complet : lecture des entrees, rapprochement, relances.

Point d'entree unique de la ligne de commande (scripts/lancer.py), de
l'evaluation (scripts/evaluer.py) et de l'interface graphique : les trois
voient exactement le meme resultat sur les memes fichiers.

Un dossier d'entree contient les fichiers ci-dessous. Les trois premiers sont
obligatoires ; les alias et les exclusions apprises sont lus s'ils sont
presents, ce qui evite qu'ils soient silencieusement ignores.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from .exclusions import Referentiel
from .modeles import Dossier, OperationBancaire, Piece, Rapprochement
from .moteur import Moteur
from .parseurs import ErreurFormat, lire_alias, lire_dossiers, lire_pieces, lire_releve
from .relances import Relance, construire_toutes

FICHIERS = {
    "releve": "releve_bancaire.csv",
    "pieces": "pieces_recues.csv",
    "dossiers": "dossiers.csv",
    "alias": "alias_fournisseurs.csv",
    "exclusions": "exclusions_dossier.csv",
    "verite": "verite_terrain.csv",
}
OBLIGATOIRES = ("releve", "pieces", "dossiers")


@dataclass
class Cycle:
    operations: list[OperationBancaire]
    pieces: list[Piece]
    dossiers: dict[str, Dossier]
    alias: dict[str, str]
    referentiel: Referentiel
    rapprochements: list[Rapprochement]
    relances: list[Relance]


def executer(
    entree: Path | str,
    exclusions: Path | str | None = None,
    alias: Path | str | None = None,
    aujourdhui: dt.date | None = None,
    exiger_dossiers: bool = True,
) -> Cycle:
    """Rapproche le releve et les pieces d'un dossier d'entree.

    exclusions et alias remplacent, s'ils sont fournis, les fichiers du meme
    nom dans le dossier d'entree.
    """
    entree = Path(entree)
    requis = OBLIGATOIRES if exiger_dossiers else ("releve", "pieces")
    absents = [FICHIERS[c] for c in requis if not (entree / FICHIERS[c]).exists()]
    if absents:
        raise ErreurFormat(f"Fichier(s) introuvable(s) dans {entree} : {', '.join(absents)}")

    operations = lire_releve(entree / FICHIERS["releve"])
    pieces = lire_pieces(entree / FICHIERS["pieces"])
    chemin_dossiers = entree / FICHIERS["dossiers"]
    dossiers = lire_dossiers(chemin_dossiers) if chemin_dossiers.exists() else {}

    table_alias = lire_alias(alias or entree / FICHIERS["alias"])
    referentiel = Referentiel.depuis_csv(exclusions or entree / FICHIERS["exclusions"])

    rapprochements = Moteur(referentiel, alias=table_alias).rapprocher(operations, pieces)
    relances = construire_toutes(dossiers, rapprochements, aujourdhui=aujourdhui)
    return Cycle(
        operations=operations,
        pieces=pieces,
        dossiers=dossiers,
        alias=table_alias,
        referentiel=referentiel,
        rapprochements=rapprochements,
        relances=relances,
    )
