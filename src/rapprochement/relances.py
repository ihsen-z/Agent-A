"""Generation des relances client pour les pieces manquantes.

Principe : une seule relance par dossier et par cycle, qui regroupe toutes les
pieces manquantes. Envoyer un email par piece est le meilleur moyen de se faire
filtrer par le client.

Aucun modele de langage : un gabarit suffit et reste previsible. Le passage a
une redaction generee (v2) n'a d'interet que pour adapter le ton par dossier.

Les textes destines au client sont en francais correct (accents, elision,
accords) : c'est le premier contact du cabinet avec son client, une faute s'y
voit plus que partout ailleurs.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from .modeles import Dossier, Rapprochement, Statut

# Cadence de relance, en jours depuis la premiere demande.
CADENCE = (0, 3, 7, 14)

# Nombre de jours avant l'echeance TVA a partir duquel le ton devient urgent.
JOURS_URGENCE = 5


@dataclass
class Relance:
    dossier: str
    destinataire: str
    objet: str
    corps: str
    nombre_pieces: int
    montant_total: Decimal
    niveau: int          # 1 = premiere demande, 2+ = relance
    ton: str = "courtois"
    echeance: dt.date | None = None

    @property
    def est_relance(self) -> bool:
        return self.niveau > 1


_VOYELLES = set("aeiouyàâäéèêëîïôöùûüÿœæ")


def de(nom: str) -> str:
    """Preposition "de" avec elision : "d'Atelier 9", "de Lopez Conseil"."""
    nom = nom.strip()
    if nom and nom[0].lower() in _VOYELLES:
        return f"d'{nom}"
    return f"de {nom}"


def justificatifs(nombre: int) -> str:
    """"1 justificatif" / "4 justificatifs"."""
    return f"{nombre} justificatif" + ("s" if nombre > 1 else "")


def _en_tete(niveau: int, societe: str, nombre: int, date_demande: dt.date) -> str:
    if niveau <= 1:
        return (
            f"Nous finalisons la comptabilité {de(societe)} et il nous manque "
            f"{justificatifs(nombre)} pour clôturer la période."
        )
    if nombre == 1:
        return (
            "Sauf erreur de notre part, nous n'avons pas encore reçu le "
            f"justificatif ci-dessous, demandé le {date_demande:%d/%m/%Y}."
        )
    return (
        "Sauf erreur de notre part, nous n'avons pas encore reçu les "
        f"{justificatifs(nombre)} ci-dessous, demandés le {date_demande:%d/%m/%Y}."
    )


def _introduction(nombre: int) -> str:
    if nombre == 1:
        return (
            "Il s'agit d'un paiement visible sur votre compte bancaire pour lequel "
            "nous n'avons pas reçu la facture correspondante :"
        )
    return (
        "Il s'agit de paiements visibles sur votre compte bancaire pour lesquels "
        "nous n'avons pas reçu la facture correspondante :"
    )


def _pied(ton: str, nombre: int, echeance: dt.date) -> str:
    seul = nombre == 1
    if ton == "urgent":
        if seul:
            return (
                "Cette pièce conditionne le dépôt de la déclaration de TVA : merci "
                f"de nous la faire parvenir avant le {echeance:%d/%m/%Y}."
            )
        return (
            "Ces pièces conditionnent le dépôt de la déclaration de TVA : merci "
            f"de nous les faire parvenir avant le {echeance:%d/%m/%Y}."
        )
    if ton == "direct":
        return f"Merci de nous {'le' if seul else 'les'} transmettre dès que possible."
    return "Un grand merci d'avance,"


def formater_montant(montant: Decimal) -> str:
    """1238.97 -> "1 238,97" (separateurs francais)."""
    return f"{montant:,.2f}".replace(",", " ").replace(".", ",")


def _ligne_piece(rapprochement: Rapprochement) -> str:
    operation = rapprochement.operation
    return (
        f"  - {operation.date_operation:%d/%m/%Y}  "
        f"{formater_montant(operation.montant)} {operation.devise}  -  "
        f"{operation.libelle_court}"
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
    nombre = len(manquants)

    echeance = prochaine_echeance(dossier.jour_echeance_tva, aujourdhui)
    urgence = (echeance - aujourdhui).days <= JOURS_URGENCE
    ton = "urgent" if urgence else dossier.ton_relance
    if ton not in ("courtois", "direct", "urgent"):
        ton = "courtois"

    salutation = f"Bonjour {dossier.nom_contact}," if dossier.nom_contact else "Bonjour,"

    corps = "\n".join(
        [
            salutation,
            "",
            _en_tete(niveau, dossier.raison_sociale, nombre, date_demande or aujourdhui),
            "",
            _introduction(nombre),
            "",
            *[_ligne_piece(r) for r in manquants],
            "",
            "Un simple envoi en réponse à cet e-mail suffit (une photo lisible est acceptée).",
            "",
            _pied(ton, nombre, echeance),
        ]
    )

    sujet = f"{justificatifs(nombre)} manquant{'s' if nombre > 1 else ''}"
    if niveau > 1:
        objet = f"Relance : {sujet} – {dossier.raison_sociale}"
    else:
        objet = f"{sujet[0].upper()}{sujet[1:]} – {dossier.raison_sociale}"

    return Relance(
        dossier=dossier.code,
        destinataire=dossier.email_contact,
        objet=objet,
        corps=corps,
        nombre_pieces=nombre,
        montant_total=total,
        niveau=niveau,
        ton=ton,
        echeance=echeance,
    )


def prochaine_echeance(jour: int, aujourdhui: dt.date) -> dt.date:
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
