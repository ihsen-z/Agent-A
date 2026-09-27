"""Modele de donnees du rapprochement bancaire / pieces justificatives.

Le vocabulaire est celui du metier comptable francais (dossier, piece,
libelle) parce que c'est celui du cabinet qui lira les rapports.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Sens(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class Statut(str, Enum):
    JUSTIFIE = "justifie"           # une piece a ete rattachee
    MANQUANT = "manquant"           # aucune piece, et il en faut une
    HORS_PERIMETRE = "hors_perimetre"  # aucune piece necessaire (salaire, impot...)
    PARTIEL = "partiel"             # piece trouvee mais montant incomplet (acompte)
    A_VERIFIER = "a_verifier"       # rapprochement possible mais peu sur


@dataclass(frozen=True)
class OperationBancaire:
    reference: str
    dossier: str
    date_operation: dt.date
    libelle: str
    montant: Decimal          # toujours positif
    sens: Sens
    devise: str = "EUR"
    date_valeur: dt.date | None = None

    @property
    def libelle_court(self) -> str:
        return self.libelle if len(self.libelle) <= 48 else self.libelle[:45] + "..."


@dataclass(frozen=True)
class Piece:
    id_piece: str
    dossier: str
    fichier: str
    fournisseur: str
    date_facture: dt.date
    montant_ttc: Decimal
    devise: str = "EUR"
    date_reception: dt.date | None = None
    canal: str = "email"


@dataclass(frozen=True)
class Dossier:
    code: str
    raison_sociale: str
    email_contact: str
    nom_contact: str
    jour_echeance_tva: int = 15
    ton_relance: str = "courtois"


@dataclass
class Rapprochement:
    """Resultat pour une operation bancaire."""

    operation: OperationBancaire
    statut: Statut
    pieces: list[Piece] = field(default_factory=list)
    confiance: float = 0.0
    motif: str = ""
    regle: str = ""           # nom de la regle qui a tranche, pour l'audit

    @property
    def montant_rattache(self) -> Decimal:
        return sum((p.montant_ttc for p in self.pieces), Decimal("0"))

    @property
    def reste_a_justifier(self) -> Decimal:
        return self.operation.montant - self.montant_rattache
