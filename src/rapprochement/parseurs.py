"""Lecture des entrees : releve bancaire, pieces recues, referentiel dossiers.

Le format CSV pivot est documente dans data/schemas/. Chaque banque exporte
differemment ; on convertit vers le pivot plutot que de multiplier les chemins
dans le moteur.
"""

from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .modeles import Dossier, OperationBancaire, Piece, Sens

_FORMATS_DATE = ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y/%m/%d")


class ErreurFormat(ValueError):
    """Le fichier fourni ne respecte pas le schema attendu."""


def lire_date(valeur: str) -> dt.date:
    valeur = (valeur or "").strip()
    for fmt in _FORMATS_DATE:
        try:
            return dt.datetime.strptime(valeur, fmt).date()
        except ValueError:
            continue
    raise ErreurFormat(f"Date illisible : {valeur!r}")


def lire_montant(valeur: str) -> Decimal:
    """Accepte 1 234,56 / 1.234,56 / 1234.56 / (1234,56) / -1 234,56."""
    brut = (valeur or "").strip().replace(" ", "").replace(" ", "")
    if not brut:
        return Decimal("0")
    negatif = brut.startswith("(") and brut.endswith(")")
    brut = brut.strip("()").replace("EUR", "").replace("€", "")
    if "," in brut and "." in brut:
        brut = brut.replace(".", "").replace(",", ".")   # 1.234,56
    else:
        brut = brut.replace(",", ".")
    try:
        montant = Decimal(brut)
    except InvalidOperation as exc:
        raise ErreurFormat(f"Montant illisible : {valeur!r}") from exc
    return -montant if negatif else montant


def _exiger(entetes: set[str], attendues: set[str], fichier: str) -> None:
    manquantes = attendues - entetes
    if manquantes:
        raise ErreurFormat(
            f"{fichier} : colonnes manquantes {sorted(manquantes)}. "
            f"Colonnes trouvees : {sorted(entetes)}"
        )


def lire_releve(chemin: Path | str, dossier_defaut: str | None = None) -> list[OperationBancaire]:
    """releve_bancaire.csv : dossier,date_operation,libelle,debit,credit,reference[,devise,date_valeur]"""
    chemin = Path(chemin)
    operations: list[OperationBancaire] = []
    with open(chemin, newline="", encoding="utf-8-sig") as f:
        lecteur = csv.DictReader(f)
        _exiger(
            set(lecteur.fieldnames or []),
            {"date_operation", "libelle", "reference"},
            chemin.name,
        )
        for numero, ligne in enumerate(lecteur, start=2):
            debit = lire_montant(ligne.get("debit", ""))
            credit = lire_montant(ligne.get("credit", ""))
            if debit == 0 and credit == 0:
                continue
            sens = Sens.DEBIT if debit != 0 else Sens.CREDIT
            montant = abs(debit) if sens is Sens.DEBIT else abs(credit)
            dossier = (ligne.get("dossier") or dossier_defaut or "").strip()
            if not dossier:
                raise ErreurFormat(
                    f"{chemin.name} ligne {numero} : dossier absent et aucun dossier par defaut"
                )
            date_valeur = ligne.get("date_valeur", "").strip()
            operations.append(
                OperationBancaire(
                    reference=(ligne["reference"] or f"L{numero}").strip(),
                    dossier=dossier,
                    date_operation=lire_date(ligne["date_operation"]),
                    libelle=(ligne["libelle"] or "").strip(),
                    montant=montant,
                    sens=sens,
                    devise=(ligne.get("devise") or "EUR").strip() or "EUR",
                    date_valeur=lire_date(date_valeur) if date_valeur else None,
                )
            )
    return operations


def lire_pieces(chemin: Path | str) -> list[Piece]:
    """pieces_recues.csv : id_piece,dossier,fichier,fournisseur,date_facture,montant_ttc[,devise,date_reception,canal]"""
    chemin = Path(chemin)
    pieces: list[Piece] = []
    with open(chemin, newline="", encoding="utf-8-sig") as f:
        lecteur = csv.DictReader(f)
        _exiger(
            set(lecteur.fieldnames or []),
            {"id_piece", "dossier", "fournisseur", "date_facture", "montant_ttc"},
            chemin.name,
        )
        for ligne in lecteur:
            reception = (ligne.get("date_reception") or "").strip()
            pieces.append(
                Piece(
                    id_piece=ligne["id_piece"].strip(),
                    dossier=ligne["dossier"].strip(),
                    fichier=(ligne.get("fichier") or "").strip(),
                    fournisseur=(ligne["fournisseur"] or "").strip(),
                    date_facture=lire_date(ligne["date_facture"]),
                    montant_ttc=abs(lire_montant(ligne["montant_ttc"])),
                    devise=(ligne.get("devise") or "EUR").strip() or "EUR",
                    date_reception=lire_date(reception) if reception else None,
                    canal=(ligne.get("canal") or "email").strip(),
                )
            )
    return pieces


def lire_dossiers(chemin: Path | str) -> dict[str, Dossier]:
    """dossiers.csv : dossier,raison_sociale,email_contact,nom_contact[,jour_echeance_tva,ton_relance]"""
    chemin = Path(chemin)
    dossiers: dict[str, Dossier] = {}
    with open(chemin, newline="", encoding="utf-8-sig") as f:
        lecteur = csv.DictReader(f)
        _exiger(
            set(lecteur.fieldnames or []),
            {"dossier", "raison_sociale", "email_contact"},
            chemin.name,
        )
        for ligne in lecteur:
            code = ligne["dossier"].strip()
            dossiers[code] = Dossier(
                code=code,
                raison_sociale=(ligne["raison_sociale"] or "").strip(),
                email_contact=(ligne["email_contact"] or "").strip(),
                nom_contact=(ligne.get("nom_contact") or "").strip(),
                jour_echeance_tva=int(ligne.get("jour_echeance_tva") or 15),
                ton_relance=(ligne.get("ton_relance") or "courtois").strip(),
            )
    return dossiers
