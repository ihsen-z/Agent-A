"""Generation des relances client pour les pieces manquantes.

Principe : une seule relance par dossier et par cycle, qui regroupe toutes les
pieces manquantes. Envoyer un email par piece est le meilleur moyen de se faire
filtrer par le client.

Aucun modele de langage : un gabarit suffit et reste previsible. Le passage a
une redaction generee (v2) n'a d'interet que pour adapter le ton par dossier.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .modeles import Dossier, Rapprochement, Statut
from .modeles import (  # ajouts du MVP (construire_brouillon)
    Brouillon,
    EtatPiece,
    OperationBancaire,
    PieceAttendue,
    Sens,
    StatutBrouillon,
    identifiant_relance,
)
from .cadence import RelancePlanifiee

# Cadence de relance, en jours depuis la premiere demande.
CADENCE = (0, 3, 7, 14)


@dataclass
class Relance:
    dossier: str
    destinataire: str
    objet: str
    corps: str
    nombre_pieces: int
    montant_total: Decimal
    niveau: int          # 1 = premiere demande, 2+ = relance

    @property
    def est_relance(self) -> bool:
        return self.niveau > 1


_EN_TETE = {
    1: "Nous finalisons la comptabilite de {societe} et il nous manque "
       "{nombre} justificatif(s) pour boucler la periode.",
    2: "Sauf erreur, nous n'avons pas encore recu les {nombre} justificatif(s) "
       "ci-dessous, demandes le {date_demande}.",
}

_PIED = {
    "courtois": "Un grand merci d'avance,",
    "direct": "Merci de nous les transmettre des que possible,",
    "urgent": "Ces pieces conditionnent le depot de la declaration : "
              "merci de nous les faire parvenir avant le {echeance}.",
}


def _ligne_piece(rapprochement: Rapprochement) -> str:
    operation = rapprochement.operation
    montant = f"{operation.montant:,.2f}".replace(",", " ").replace(".", ",")
    return (
        f"  - {operation.date_operation.strftime('%d/%m/%Y')}  "
        f"{montant} {operation.devise}  -  {operation.libelle_court}"
    )


def construire_relance(
    dossier: Dossier,
    manquants: list[Rapprochement],
    niveau: int = 1,
    date_demande: dt.date | None = None,
    aujourdhui: dt.date | None = None,
) -> Relance | None:
    """Construit une relance unique regroupant toutes les pieces manquantes."""
    manquants = [r for r in manquants if r.statut is Statut.MANQUANT]
    if not manquants:
        return None

    aujourdhui = aujourdhui or dt.date.today()
    manquants = sorted(manquants, key=lambda r: r.operation.date_operation)
    total = sum((r.operation.montant for r in manquants), Decimal("0"))

    echeance = _prochaine_echeance(dossier.jour_echeance_tva, aujourdhui)
    urgence = (echeance - aujourdhui).days <= 5
    ton = "urgent" if urgence else dossier.ton_relance

    en_tete = _EN_TETE[min(niveau, 2)].format(
        societe=dossier.raison_sociale,
        nombre=len(manquants),
        date_demande=(date_demande or aujourdhui).strftime("%d/%m/%Y"),
    )

    salutation = f"Bonjour {dossier.nom_contact}," if dossier.nom_contact else "Bonjour,"
    pied = _PIED.get(ton, _PIED["courtois"]).format(
        echeance=echeance.strftime("%d/%m/%Y")
    )

    corps = "\n".join(
        [
            salutation,
            "",
            en_tete,
            "",
            "Il s'agit de paiements visibles sur le compte bancaire pour lesquels "
            "nous n'avons pas la facture correspondante :",
            "",
            *[_ligne_piece(r) for r in manquants],
            "",
            "Un envoi par retour de cet email suffit (photo lisible acceptee).",
            "",
            pied,
        ]
    )

    prefixe = "Relance" if niveau > 1 else "Justificatifs"
    objet = (
        f"{prefixe} - {len(manquants)} justificatif(s) manquant(s) "
        f"- {dossier.raison_sociale}"
    )

    return Relance(
        dossier=dossier.code,
        destinataire=dossier.email_contact,
        objet=objet,
        corps=corps,
        nombre_pieces=len(manquants),
        montant_total=total,
        niveau=niveau,
    )


def _prochaine_echeance(jour: int, aujourdhui: dt.date) -> dt.date:
    """Prochaine echeance TVA mensuelle du dossier."""
    jour = max(1, min(28, jour))
    if aujourdhui.day < jour:
        return aujourdhui.replace(day=jour)
    mois = aujourdhui.month + 1
    annee = aujourdhui.year + (1 if mois > 12 else 0)
    return dt.date(annee, 1 if mois > 12 else mois, jour)


def construire_toutes(
    dossiers: dict[str, Dossier],
    rapprochements: list[Rapprochement],
    aujourdhui: dt.date | None = None,
) -> list[Relance]:
    par_dossier: dict[str, list[Rapprochement]] = {}
    for r in rapprochements:
        if r.statut is Statut.MANQUANT:
            par_dossier.setdefault(r.operation.dossier, []).append(r)

    relances: list[Relance] = []
    for code, manquants in sorted(par_dossier.items()):
        dossier = dossiers.get(code)
        if dossier is None:
            continue
        relance = construire_relance(dossier, manquants, aujourdhui=aujourdhui)
        if relance is not None:
            relances.append(relance)
    return relances


# ---------------------------------------------------------------------------
# Ajouts du MVP : brouillon (un ou plusieurs dossiers) pour un destinataire
# ---------------------------------------------------------------------------

_ETATS_RECLAMABLES = (EtatPiece.ATTENDUE, EtatPiece.DEMANDEE)


def _rapprochement_depuis_piece(piece: PieceAttendue) -> Rapprochement:
    """Vue `Rapprochement` d'une piece attendue, pour reutiliser le gabarit.

    Seuls date, montant, libelle et reference viennent de la `PieceAttendue` :
    c'est la source unique de chaque ligne reclamee (CA-07). La devise est celle de la piece (jamais EUR code en dur).
    """
    operation = OperationBancaire(
        reference=piece.reference,
        dossier=piece.dossier,
        date_operation=piece.date_operation,
        libelle=piece.libelle,
        montant=piece.montant,
        sens=Sens.DEBIT,
        devise=piece.devise,
    )
    return Rapprochement(operation=operation, statut=Statut.MANQUANT)


def _date_demande(pieces: list[PieceAttendue], aujourdhui: dt.date) -> dt.date:
    dates = [p.date_premiere_demande for p in pieces if p.date_premiere_demande]
    return min(dates) if dates else aujourdhui


def _tri_pieces(pieces: list[PieceAttendue]) -> list[PieceAttendue]:
    return sorted(pieces, key=lambda p: (p.date_operation, p.reference))


def construire_brouillon(
    planifiee: RelancePlanifiee,
    pieces: Mapping[str, PieceAttendue],
    dossiers: Mapping[str, Dossier],
    aujourdhui: dt.date,
) -> Brouillon:
    """Construit le brouillon d'une relance planifiee (statut BROUILLON).

    - Un seul dossier : meme texte que `construire_relance` (meme gabarit, meme
      ton, meme calcul d'urgence TVA).
    - Plusieurs dossiers : une section par dossier, titree par la raison
      sociale ; le pied est commun (urgent si un dossier est a J-5 de son
      echeance TVA, avec l'echeance la plus proche).
    - CA-07 : chaque ligne reclamee vient d'une `PieceAttendue` de `pieces`
      (date, montant, libelle). Aucune ligne sans operation source.
    - L'identifiant est `identifiant_relance(destinataire, references, niveau,
      aujourdhui)`.

    Leve `ValueError` si la planification est incoherente avec les donnees
    (reference inconnue ou en double, dossier inconnu ou sans piece, piece d'un
    autre dossier, piece non reclamable, destinataire different de celui du
    dossier). Ces controles empechent d'envoyer les donnees d'un client a
    l'adresse d'un autre. `planifier` ne produit jamais de telle planification.
    """
    references = tuple(sorted(planifiee.references))
    if not references:
        raise ValueError("aucune reference a reclamer")
    if len(set(references)) != len(references):
        raise ValueError("references en double")
    codes = tuple(sorted(planifiee.dossiers))
    if len(set(codes)) != len(codes):
        raise ValueError("dossiers en double")

    par_dossier: dict[str, list[PieceAttendue]] = {c: [] for c in codes}
    for reference in references:
        piece = pieces.get(reference)
        if piece is None:
            raise ValueError(f"reference sans piece attendue : {reference}")
        if piece.reference != reference:
            raise ValueError(f"piece incoherente pour la reference {reference}")
        if piece.dossier not in par_dossier:
            raise ValueError(f"piece {reference} : dossier {piece.dossier} non planifie")
        if piece.etat not in _ETATS_RECLAMABLES or piece.bloquee:
            raise ValueError(f"piece {reference} non reclamable (etat {piece.etat.value})")
        par_dossier[piece.dossier].append(piece)

    cible = planifiee.destinataire.strip().lower()
    for code in codes:
        dossier = dossiers.get(code)
        if dossier is None:
            raise ValueError(f"dossier inconnu : {code}")
        if not par_dossier[code]:
            raise ValueError(f"dossier {code} sans piece a reclamer")
        if dossier.destinataire.strip().lower() != cible:
            raise ValueError(f"dossier {code} : destinataire different de {planifiee.destinataire}")

    niveau = planifiee.niveau
    if len(codes) == 1:
        dossier = dossiers[codes[0]]
        liste = _tri_pieces(par_dossier[codes[0]])
        relance = construire_relance(
            dossier,
            [_rapprochement_depuis_piece(p) for p in liste],
            niveau=niveau,
            date_demande=_date_demande(liste, aujourdhui),
            aujourdhui=aujourdhui,
        )
        assert relance is not None
        objet, corps = relance.objet, relance.corps
    else:
        objet, corps = _composer_multi(codes, par_dossier, dossiers, niveau, aujourdhui)

    return Brouillon(
        id_relance=identifiant_relance(planifiee.destinataire, references, niveau, aujourdhui),
        destinataire=planifiee.destinataire,
        objet=objet,
        corps=corps,
        dossiers=codes,
        references=references,
        niveau=niveau,
        cree_le=aujourdhui,
        statut=StatutBrouillon.BROUILLON,
    )


def _composer_multi(
    codes: tuple[str, ...],
    par_dossier: dict[str, list[PieceAttendue]],
    dossiers: Mapping[str, Dossier],
    niveau: int,
    aujourdhui: dt.date,
) -> tuple[str, str]:
    """Objet et corps d'un message regroupant plusieurs dossiers."""
    total_pieces = sum(len(par_dossier[c]) for c in codes)
    contacts = {dossiers[c].nom_contact for c in codes}
    contact = next(iter(contacts)) if len(contacts) == 1 else ""
    salutation = f"Bonjour {contact}," if contact else "Bonjour,"

    echeances = {c: _prochaine_echeance(dossiers[c].jour_echeance_tva, aujourdhui) for c in codes}
    urgents = [c for c in codes if (echeances[c] - aujourdhui).days <= 5]
    tons = {dossiers[c].ton_relance for c in codes}
    if urgents:
        ton = "urgent"
        echeance = min(echeances[c] for c in urgents)
    else:
        ton = tons.pop() if len(tons) == 1 else "courtois"
        echeance = min(echeances.values())
    pied = _PIED.get(ton, _PIED["courtois"]).format(echeance=echeance.strftime("%d/%m/%Y"))

    lignes = [
        salutation,
        "",
        (
            f"Nous vous ecrivons pour {len(codes)} dossiers : il nous manque "
            f"{total_pieces} justificatif(s) au total."
        ),
    ]
    for code in codes:
        dossier = dossiers[code]
        liste = _tri_pieces(par_dossier[code])
        en_tete = _EN_TETE[min(niveau, 2)].format(
            societe=dossier.raison_sociale,
            nombre=len(liste),
            date_demande=_date_demande(liste, aujourdhui).strftime("%d/%m/%Y"),
        )
        titre = dossier.raison_sociale
        lignes += [
            "",
            titre,
            "-" * len(titre),
            en_tete,
            "",
            "Il s'agit de paiements visibles sur le compte bancaire pour lesquels "
            "nous n'avons pas la facture correspondante :",
            "",
            *[_ligne_piece(_rapprochement_depuis_piece(p)) for p in liste],
        ]
    lignes += [
        "",
        "Un envoi par retour de cet email suffit (photo lisible acceptee).",
        "",
        pied,
    ]

    prefixe = "Relance" if niveau > 1 else "Justificatifs"
    objet = f"{prefixe} - {total_pieces} justificatif(s) manquant(s) - {len(codes)} dossiers"
    return objet, "\n".join(lignes)
