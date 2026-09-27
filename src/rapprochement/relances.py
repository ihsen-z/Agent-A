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

from .modeles import Dossier, Rapprochement, Statut

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
