"""Moteur de rapprochement operation bancaire <-> piece justificative.

Strategie, dans l'ordre :
  0. exclusion      : l'operation n'exige aucune piece (referentiel).
  1. appariement 1-1: une piece au meme montant, datee avant le paiement.
  2. paiement groupe: une operation = somme de 2 a 4 pieces (cas frequent des
                      virements fournisseurs mensuels regroupes).
  3. acompte        : la piece est plus chere que l'operation -> paiement partiel.
  4. manquant       : rien de credible -> a relancer.

Aucun appel a un modele de langage : la decision doit etre reproductible et
justifiable ligne par ligne devant un comptable.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from difflib import SequenceMatcher
from itertools import combinations

from .exclusions import Referentiel
from .modeles import (
    OperationBancaire,
    Piece,
    Rapprochement,
    Sens,
    Statut,
)
from .normalisation import jeton_fort_commun, jetons_significatifs, normaliser

# Une facture est datee AVANT son paiement, jusqu'a 90 jours (delai de paiement
# legal maximal en France : 60 jours, + marge pour les retards).
FENETRE_AMONT_JOURS = 90
# Tolerance : on paie parfois quelques jours avant de recevoir la facture.
FENETRE_AVAL_JOURS = 5
# Ecart de montant accepte (arrondis, frais de change).
TOLERANCE_ABSOLUE = Decimal("0.02")
TOLERANCE_RELATIVE = Decimal("0.005")   # 0,5 %

# Un acompte n'est retenu que si le libelle le dit. Sans cet indice, tout
# paiement inferieur a une facture du meme fournisseur recurrent (Orange, EDF)
# passe pour un acompte et n'est plus relance.
INDICES_ACOMPTE = (
    r"\bacompte\b", r"\bavance\b", r"\bpartiel", r"\barrhes\b",
    r"\b\d{1,2}\s?%", r"\bsitu(ation)?\s?\d", r"\b1\s?er\s+versement\b",
)
INDICES_GROUPEMENT = (
    r"\bgroupe", r"\breglt\b", r"\breglement\s+(multiple|global)\b",
    r"\bbordereau\b", r"\bmulti", r"\bfactures\b", r"\bglobal\b",
)

RATIO_ACOMPTE_MIN = 0.10
RATIO_ACOMPTE_MAX = 0.95

SEUIL_CONFIANCE = 0.55
SEUIL_CERTITUDE = 0.80
MAX_PIECES_GROUPEES = 4


def _montants_compatibles(a: Decimal, b: Decimal) -> bool:
    ecart = abs(a - b)
    return ecart <= max(TOLERANCE_ABSOLUE, b * TOLERANCE_RELATIVE)


def _montants_identiques(a: Decimal, b: Decimal) -> bool:
    """Tolerance au centime seulement.

    Utilisee pour les paiements groupes : avec une tolerance relative, deux
    factures d'un meme fournisseur finissent par totaliser par hasard le montant
    d'un troisieme paiement, et le systeme cesse de relancer une piece
    reellement manquante.
    """
    return abs(a - b) <= TOLERANCE_ABSOLUE


def _similarite(libelle: str, fournisseur: str) -> float:
    """0 a 1. Combine recouvrement de jetons et similarite de chaine."""
    jetons_l = jetons_significatifs(libelle)
    jetons_f = jetons_significatifs(fournisseur)
    if jetons_f and jetons_l:
        communs = jetons_l & jetons_f
        if communs:
            recouvrement = len(communs) / len(jetons_f)
        else:
            recouvrement = 0.0
    else:
        recouvrement = 0.0

    chaine = SequenceMatcher(
        None, normaliser(libelle), normaliser(fournisseur)
    ).ratio()
    # Un nom de fournisseur present tel quel dans le libelle est le signal fort.
    if normaliser(fournisseur) and normaliser(fournisseur) in normaliser(libelle):
        return 1.0
    # Un seul mot distinctif suffit : "LOXAM" identifie "Loxam Location".
    fort = 0.85 if jeton_fort_commun(libelle, fournisseur) else 0.0
    return max(recouvrement, chaine * 0.9, fort)


def _dans_la_fenetre(operation: OperationBancaire, piece: Piece) -> bool:
    delta = (operation.date_operation - piece.date_facture).days
    return -FENETRE_AVAL_JOURS <= delta <= FENETRE_AMONT_JOURS


def _score_date(operation: OperationBancaire, piece: Piece) -> float:
    """1.0 le jour du paiement, decroit lineairement sur la fenetre."""
    delta = abs((operation.date_operation - piece.date_facture).days)
    return max(0.0, 1.0 - delta / FENETRE_AMONT_JOURS)


def _score(operation: OperationBancaire, pieces: list[Piece]) -> float:
    """Confiance globale : le montant pese le plus, puis le libelle, puis la date."""
    total = sum((p.montant_ttc for p in pieces), Decimal("0"))
    score_montant = 1.0 if _montants_compatibles(operation.montant, total) else 0.0
    score_libelle = max(_similarite(operation.libelle, p.fournisseur) for p in pieces)
    score_dates = sum(_score_date(operation, p) for p in pieces) / len(pieces)
    # Un groupement est moins sur qu'un appariement direct.
    penalite = 0.0 if len(pieces) == 1 else 0.12 * (len(pieces) - 1)
    return max(0.0, 0.55 * score_montant + 0.30 * score_libelle + 0.15 * score_dates - penalite)


def _contient(libelle: str, motifs: tuple[str, ...]) -> bool:
    formes = (normaliser(libelle), normaliser(libelle, retirer_prefixes=False))
    return any(re.search(m, f) for f in formes for m in motifs)


class Moteur:
    """Rapproche un lot d'operations. Une piece ne sert qu'une seule fois.

    L'affectation est globale et non chronologique : on classe tous les couples
    (operation, piece) credibles par confiance decroissante, puis on affecte. Un
    appariement au centime pres l'emporte ainsi sur un appariement approximatif
    qui, traite dans l'ordre des dates, aurait consomme la piece le premier.
    """

    def __init__(
        self,
        referentiel: Referentiel | None = None,
        alias: dict[str, str] | None = None,
    ) -> None:
        self.referentiel = referentiel or Referentiel()
        # alias -> nom de fournisseur ("amzn mktplace" -> "Amazon EU Sarl")
        self.alias = alias or {}

    # -- API ---------------------------------------------------------------

    def rapprocher(
        self,
        operations: list[OperationBancaire],
        pieces: list[Piece],
    ) -> list[Rapprochement]:
        resultats: dict[str, Rapprochement] = {}
        a_apparier: list[OperationBancaire] = []

        for operation in operations:
            tranche = self._trancher_sans_piece(operation)
            if tranche is not None:
                resultats[operation.reference] = tranche
            else:
                a_apparier.append(operation)

        disponibles: dict[str, list[Piece]] = {}
        for piece in pieces:
            disponibles.setdefault(piece.dossier, []).append(piece)
        consommees: set[str] = set()

        self._passe_appariement(a_apparier, disponibles, consommees, resultats)
        self._passe_groupement(a_apparier, disponibles, consommees, resultats)
        self._passe_acompte(a_apparier, disponibles, consommees, resultats)

        for operation in a_apparier:
            resultats.setdefault(
                operation.reference,
                Rapprochement(
                    operation=operation,
                    statut=Statut.MANQUANT,
                    motif="Aucune piece justificative recue pour ce paiement",
                    regle="aucun_candidat",
                ),
            )
        return [resultats[o.reference] for o in operations]

    # -- decisions sans piece ---------------------------------------------

    def _trancher_sans_piece(self, operation: OperationBancaire) -> Rapprochement | None:
        exclue, motif, regle = self.referentiel.exclure(operation)
        if exclue:
            return Rapprochement(
                operation=operation,
                statut=Statut.HORS_PERIMETRE,
                confiance=1.0,
                motif=motif,
                regle=regle,
            )
        if operation.sens is Sens.CREDIT:
            return Rapprochement(
                operation=operation,
                statut=Statut.A_VERIFIER,
                motif="Encaissement non identifie : a rattacher a une facture de vente",
                regle="credit_non_identifie",
            )
        return None

    # -- passes d'appariement ---------------------------------------------

    def _candidates(
        self,
        operation: OperationBancaire,
        disponibles: dict[str, list[Piece]],
        consommees: set[str],
    ) -> list[Piece]:
        return [
            p
            for p in disponibles.get(operation.dossier, [])
            if p.id_piece not in consommees and _dans_la_fenetre(operation, p)
        ]

    def _similarite(self, operation: OperationBancaire, piece: Piece) -> float:
        """Similarite libelle/fournisseur, enrichie du referentiel d'alias."""
        direct = _similarite(operation.libelle, piece.fournisseur)
        for alias, fournisseur in self.alias.items():
            if fournisseur != piece.fournisseur:
                continue
            if _contient(operation.libelle, (re.escape(normaliser(alias)),)):
                return 1.0
        return direct

    def _score(self, operation: OperationBancaire, pieces: list[Piece]) -> float:
        total = sum((p.montant_ttc for p in pieces), Decimal("0"))
        score_montant = 1.0 if _montants_compatibles(operation.montant, total) else 0.0
        score_libelle = max(self._similarite(operation, p) for p in pieces)
        score_dates = sum(_score_date(operation, p) for p in pieces) / len(pieces)
        penalite = 0.0 if len(pieces) == 1 else 0.12 * (len(pieces) - 1)
        return max(
            0.0,
            0.55 * score_montant + 0.30 * score_libelle + 0.15 * score_dates - penalite,
        )

    def _passe_appariement(
        self,
        operations: list[OperationBancaire],
        disponibles: dict[str, list[Piece]],
        consommees: set[str],
        resultats: dict[str, Rapprochement],
    ) -> None:
        """Appariements un pour un, par confiance decroissante sur tout le lot."""
        couples: list[tuple[float, int, str, str, OperationBancaire, Piece]] = []
        for operation in operations:
            for piece in self._candidates(operation, disponibles, consommees):
                if not _montants_compatibles(operation.montant, piece.montant_ttc):
                    continue
                score = self._score(operation, [piece])
                if score < SEUIL_CONFIANCE:
                    continue
                ecart_jours = abs((operation.date_operation - piece.date_facture).days)
                # Cles de tri supplementaires pour un resultat deterministe.
                couples.append(
                    (-score, ecart_jours, operation.reference, piece.id_piece, operation, piece)
                )

        for _, _, _, _, operation, piece in sorted(couples, key=lambda c: c[:4]):
            if operation.reference in resultats or piece.id_piece in consommees:
                continue
            score = self._score(operation, [piece])
            consommees.add(piece.id_piece)
            certain = score >= SEUIL_CERTITUDE
            resultats[operation.reference] = Rapprochement(
                operation=operation,
                statut=Statut.JUSTIFIE if certain else Statut.A_VERIFIER,
                pieces=[piece],
                confiance=round(score, 3),
                motif="" if certain else "Rapprochement probable a confirmer",
                regle="appariement_1_1",
            )

    def _passe_groupement(
        self,
        operations: list[OperationBancaire],
        disponibles: dict[str, list[Piece]],
        consommees: set[str],
        resultats: dict[str, Rapprochement],
    ) -> None:
        """Une operation = somme de plusieurs pieces.

        Exigences deliberement strictes : total au centime pres, combinaison
        unique, et soit 3 pieces ou plus, soit un libelle qui annonce un
        reglement groupe. Sans cela, deux factures d'un fournisseur recurrent
        totalisent par hasard un troisieme paiement et la piece reellement
        manquante n'est plus relancee.
        """
        for operation in operations:
            if operation.reference in resultats:
                continue
            candidates = [
                p
                for p in self._candidates(operation, disponibles, consommees)
                if p.montant_ttc < operation.montant
            ]
            if len(candidates) < 2:
                continue
            candidates = sorted(
                candidates, key=lambda p: p.montant_ttc, reverse=True
            )[:12]

            indice = _contient(operation.libelle, INDICES_GROUPEMENT)
            lots: list[tuple[tuple[Piece, ...], float]] = []
            for taille in range(2, MAX_PIECES_GROUPEES + 1):
                if taille == 2 and not indice:
                    continue
                for lot in combinations(candidates, taille):
                    total = sum((p.montant_ttc for p in lot), Decimal("0"))
                    if not _montants_identiques(operation.montant, total):
                        continue
                    score = self._score(operation, list(lot))
                    if score >= SEUIL_CONFIANCE:
                        lots.append((lot, score))

            if len(lots) != 1:
                # Aucune combinaison, ou plusieurs : l'ambiguite ne se tranche
                # pas toute seule. On relance, quitte a ce que le client
                # reponde qu'il a deja envoye : c'est le sens de l'erreur le
                # moins couteux.
                continue

            lot, score = lots[0]
            for piece in lot:
                consommees.add(piece.id_piece)
            certain = score >= SEUIL_CERTITUDE
            resultats[operation.reference] = Rapprochement(
                operation=operation,
                statut=Statut.JUSTIFIE if certain else Statut.A_VERIFIER,
                pieces=list(lot),
                confiance=round(score, 3),
                motif=f"Reglement groupe de {len(lot)} factures"
                + ("" if certain else " - a confirmer"),
                regle=f"paiement_groupe_{len(lot)}",
            )

    def _passe_acompte(
        self,
        operations: list[OperationBancaire],
        disponibles: dict[str, list[Piece]],
        consommees: set[str],
        resultats: dict[str, Rapprochement],
    ) -> None:
        """Paiement partiel d'une facture plus chere.

        Le libelle doit l'annoncer (acompte, avance, 30 %, situation 2...). Sans
        cet indice, chaque paiement mensuel d'un fournisseur recurrent passe pour
        l'acompte d'une facture plus ancienne et cesse d'etre relance.
        """
        for operation in operations:
            if operation.reference in resultats:
                continue
            if not _contient(operation.libelle, INDICES_ACOMPTE):
                continue

            meilleur: tuple[Piece, float] | None = None
            for piece in self._candidates(operation, disponibles, consommees):
                if piece.montant_ttc <= operation.montant:
                    continue
                ratio = float(operation.montant / piece.montant_ttc)
                if not RATIO_ACOMPTE_MIN <= ratio <= RATIO_ACOMPTE_MAX:
                    continue
                similarite = self._similarite(operation, piece)
                if similarite < 0.70:
                    continue
                if meilleur is None or similarite > meilleur[1]:
                    meilleur = (piece, similarite)

            if meilleur is None:
                continue
            piece, similarite = meilleur
            # La piece n'est PAS consommee : le solde restera a rattacher.
            resultats[operation.reference] = Rapprochement(
                operation=operation,
                statut=Statut.PARTIEL,
                pieces=[piece],
                confiance=round(similarite, 3),
                motif=(
                    f"Paiement partiel de la facture {piece.montant_ttc} "
                    f"{piece.devise} ({piece.fournisseur}) : solde a suivre"
                ),
                regle="acompte",
            )
