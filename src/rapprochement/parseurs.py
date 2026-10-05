"""Lecture des entrees : releve bancaire, pieces recues, referentiel dossiers.

Le format CSV pivot est documente dans data/schemas/. Chaque banque exporte
differemment ; on convertit vers le pivot plutot que de multiplier les chemins
dans le moteur.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .modeles import CIRCUITS, Dossier, OperationBancaire, Piece, Sens

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


def _nfc(texte: str) -> str:
    """Forme de composition unique : 'e' + accent combinant devient 'e accentue'.

    Sans cela, deux codes dossier qui s'affichent a l'identique sont deux cles
    differentes, et le client dont le code est ecrit autrement n'est jamais relance.
    """
    return unicodedata.normalize("NFC", texte.strip())


def _reference_de_repli(
    dossier: str,
    date_operation: dt.date,
    libelle: str,
    montant: Decimal,
    sens: Sens,
    rang: int,
) -> str:
    """Reference stable pour une ligne de releve qui n'en porte pas.

    Derivee du CONTENU de la ligne, jamais de son numero : un numero de ligne
    change d'un export a l'autre, deux operations de mois differents finissent
    alors par partager la meme reference, et la seconde n'est jamais reclamee.
    Deux lignes strictement identiques (meme jour, meme libelle, meme montant)
    restent distinguees par leur rang d'apparition.
    """
    graine = "|".join(
        (dossier, date_operation.isoformat(), libelle, str(montant), sens.value)
    )
    empreinte = hashlib.sha256(graine.encode("utf-8")).hexdigest()[:12]
    return f"H{empreinte}-{rang}"


def lire_releve(chemin: Path | str, dossier_defaut: str | None = None) -> list[OperationBancaire]:
    """releve_bancaire.csv : dossier,date_operation,libelle,debit,credit,reference[,devise,date_valeur]"""
    chemin = Path(chemin)
    operations: list[OperationBancaire] = []
    rangs: dict[str, int] = {}
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
            dossier = _nfc(ligne.get("dossier") or dossier_defaut or "")
            if not dossier:
                raise ErreurFormat(
                    f"{chemin.name} ligne {numero} : dossier absent et aucun dossier par defaut"
                )
            date_valeur = ligne.get("date_valeur", "").strip()
            date_operation = lire_date(ligne["date_operation"])
            libelle = (ligne["libelle"] or "").strip()
            reference = (ligne["reference"] or "").strip()
            if not reference:
                cle = f"{dossier}|{date_operation}|{libelle}|{montant}|{sens.value}"
                rangs[cle] = rangs.get(cle, 0) + 1
                reference = _reference_de_repli(
                    dossier, date_operation, libelle, montant, sens, rangs[cle]
                )
            operations.append(
                OperationBancaire(
                    reference=reference,
                    dossier=dossier,
                    date_operation=date_operation,
                    libelle=libelle,
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
                    dossier=_nfc(ligne["dossier"]),
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
    """dossiers.csv : dossier,raison_sociale,email_contact,nom_contact[,jour_echeance_tva,ton_relance,circuit,email_relais,domaines]"""
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
            code = _nfc(ligne["dossier"])
            circuit = (ligne.get("circuit") or "C-DIRECT").strip().upper()
            email_relais = (ligne.get("email_relais") or "").strip()
            if circuit not in CIRCUITS:
                raise ErreurFormat(
                    f"{chemin.name} : circuit '{circuit}' inconnu pour {code} "
                    f"(attendu : {', '.join(CIRCUITS)})"
                )
            if circuit == "C-RELAIS" and not email_relais:
                raise ErreurFormat(
                    f"{chemin.name} : le dossier {code} est en C-RELAIS "
                    "mais email_relais est vide"
                )
            domaines = tuple(
                d for d in (x.strip().lower() for x in (ligne.get("domaines") or "").split(";")) if d
            )
            for domaine in domaines:
                if not domaine.isascii():
                    raise ErreurFormat(
                        f"{chemin.name} : domaine '{domaine}' non ASCII pour {code}. "
                        "Declarez sa forme punycode (xn--...) : la conversion automatique "
                        "peut confondre deux domaines distincts (straße.de et strasse.de)"
                    )
                if "@" in domaine or " " in domaine or "." not in domaine:
                    raise ErreurFormat(
                        f"{chemin.name} : domaine '{domaine}' invalide pour {code} "
                        "(attendu : un nom de domaine comme cabinet-martin.fr, "
                        "plusieurs separes par des points-virgules)"
                    )
            dossiers[code] = Dossier(
                code=code,
                raison_sociale=(ligne["raison_sociale"] or "").strip(),
                email_contact=(ligne["email_contact"] or "").strip(),
                nom_contact=(ligne.get("nom_contact") or "").strip(),
                jour_echeance_tva=int(ligne.get("jour_echeance_tva") or 15),
                ton_relance=(ligne.get("ton_relance") or "courtois").strip(),
                circuit=circuit,
                email_relais=email_relais,
                domaines=domaines,
            )
    return dossiers
