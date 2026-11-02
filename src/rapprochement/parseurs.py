"""Lecture des entrees : releve bancaire, pieces recues, referentiel dossiers.

Le format CSV pivot est documente dans data/schemas/. Chaque banque exporte
differemment ; on convertit vers le pivot plutot que de multiplier les chemins
dans le moteur.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .normalisation import normaliser
from .modeles import CIRCUITS, Dossier, OperationBancaire, Piece, Sens

_ADRESSE = re.compile(r"[^@\s,;<>()]+@[^@\s,;<>()]+\.[^@\s,;<>()]+")
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


_MONTANT_BRUT = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)")
_MILLIERS_VIRGULE = re.compile(r"\d{1,3}(,\d{3})+\.\d+")
_TROIS_DECIMALES = re.compile(r"[+-]?\d+[.,]\d{3}")
_SYMBOLE_DEVISE = re.compile(r"^(EUR|\u20ac)|(EUR|\u20ac)$")


def lire_montant(valeur: str) -> Decimal:
    """Accepte 1 234,56 / 1.234,56 / 1,234.56 / 1234.56 / (1234,56) / -1 234,56 / 12 EUR.

    Refuse `NaN`, `Infinity`, la notation scientifique (`1E5`), un symbole de devise au
    milieu du nombre (`12€50`), des parentheses mal appariees et tout format ambigu
    (`1,234` : mille deux cent trente-quatre ou un virgule deux trois quatre ?) : un
    montant mal lu est PIRE qu'un refus (il justifierait ou manquerait a tort un
    paiement, sans aucune alerte).
    """
    brut = (valeur or "").strip().replace(" ", "").replace(" ", "")
    if not brut:
        return Decimal("0")
    ouvrante, fermante = brut.startswith("("), brut.endswith(")")
    if ouvrante != fermante or brut.count("(") != ouvrante or brut.count(")") != fermante:
        raise ErreurFormat(f"Montant illisible : {valeur!r} (parentheses mal appariees)")
    negatif = ouvrante
    brut = brut[1:-1] if negatif else brut
    brut = _SYMBOLE_DEVISE.sub("", brut)
    if negatif and brut.startswith(("-", "+")):
        raise ErreurFormat(f"Montant illisible : {valeur!r} (parentheses et signe)")
    if "," in brut and "." in brut:
        if _MILLIERS_VIRGULE.fullmatch(brut.lstrip("+-")):
            brut = brut.replace(",", "")                       # 1,234.56
        elif brut.rfind(",") > brut.rfind("."):
            brut = brut.replace(".", "").replace(",", ".")     # 1.234,56
        else:
            raise ErreurFormat(f"Montant ambigu : {valeur!r} (separateurs melanges)")
    else:
        if _TROIS_DECIMALES.fullmatch(brut):
            raise ErreurFormat(
                f"Montant ambigu : {valeur!r} (1,234 est-il 1 234 ou 1,234 ? ecrivez 1234,00 ou 1,23)"
            )
        brut = brut.replace(",", ".")
    if not _MONTANT_BRUT.fullmatch(brut):
        raise ErreurFormat(f"Montant illisible : {valeur!r}")
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


def _entete(nom: str) -> str:
    """`Débit`, ` DEBIT ` et `debit` designent la meme colonne."""
    sans_accent = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode("ascii")
    return sans_accent.strip().lower().replace(" ", "_")


def _lignes(f, fichier: str):
    """Lit un CSV : renvoie (en-tetes normalises, iterateur de (numero, ligne)). Refuse tout ce
    qui serait sinon lu de travers en silence (ligne trop courte ou trop longue, champ geant)."""
    lecteur = csv.DictReader(f)
    try:
        entetes = [_entete(c) for c in (lecteur.fieldnames or [])]
    except csv.Error as erreur:
        raise ErreurFormat(f"{fichier} : en-tete illisible ({erreur})") from None
    doublons = sorted({c for c in entetes if entetes.count(c) > 1})
    if doublons:
        raise ErreurFormat(
            f"{fichier} : colonne(s) en double apres normalisation : {', '.join(doublons)} "
            "(`Débit` et `debit` sont la meme colonne). La derniere ecraserait la premiere en silence"
        )
    lecteur.fieldnames = entetes

    def iterer():
        numero = 1
        while True:
            try:
                ligne = next(lecteur)
            except StopIteration:
                return
            except csv.Error as erreur:
                raise ErreurFormat(f"{fichier} ligne {numero + 1} : fichier illisible ({erreur})") from None
            numero += 1
            if None in ligne:
                raise ErreurFormat(f"{fichier} ligne {numero} : plus de cellules que de colonnes")
            if any(v is None for v in ligne.values()):
                raise ErreurFormat(
                    f"{fichier} ligne {numero} : moins de cellules que de colonnes (export tronque ?). "
                    "Une ligne sautee en silence serait une operation jamais justifiee"
                )
            yield numero, ligne

    return entetes, iterer()


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
        entetes, lignes = _lignes(f, chemin.name)
        _exiger(set(entetes), {"date_operation", "libelle", "reference", "debit", "credit"}, chemin.name)
        for numero, ligne in lignes:
            debit = lire_montant(ligne.get("debit", ""))
            credit = lire_montant(ligne.get("credit", ""))
            if debit == 0 and credit == 0:
                continue
            if debit != 0 and credit != 0:
                raise ErreurFormat(
                    f"{chemin.name} ligne {numero} : debit ET credit renseignes sur la meme "
                    "ligne. Une operation n'a qu'un sens : separez-la en deux lignes"
                )
            # Un debit NEGATIF est un remboursement recu (donc un credit), et inversement :
            # l'ancienne lecture par valeur absolue le comptait comme une depense a justifier.
            if debit != 0:
                sens, montant = (Sens.DEBIT, debit) if debit > 0 else (Sens.CREDIT, -debit)
            else:
                sens, montant = (Sens.CREDIT, credit) if credit > 0 else (Sens.DEBIT, -credit)
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
        entetes, lignes = _lignes(f, chemin.name)
        _exiger(set(entetes), {"id_piece", "dossier", "fournisseur", "date_facture", "montant_ttc"}, chemin.name)
        for numero, ligne in lignes:
            reception = (ligne.get("date_reception") or "").strip()
            montant_piece = lire_montant(ligne["montant_ttc"])
            if montant_piece < 0:
                raise ErreurFormat(
                    f"{chemin.name} ligne {numero} : montant_ttc negatif ({montant_piece}) pour "
                    f"la piece {ligne['id_piece'].strip()}. Un avoir ne justifie pas un paiement : "
                    "il ne doit pas etre lu comme une facture. Retirez-le de ce fichier ou "
                    "traitez-le a part"
                )
            pieces.append(
                Piece(
                    id_piece=ligne["id_piece"].strip(),
                    dossier=_nfc(ligne["dossier"]),
                    fichier=(ligne.get("fichier") or "").strip(),
                    fournisseur=(ligne["fournisseur"] or "").strip(),
                    date_facture=lire_date(ligne["date_facture"]),
                    montant_ttc=montant_piece,
                    devise=(ligne.get("devise") or "EUR").strip() or "EUR",
                    date_reception=lire_date(reception) if reception else None,
                    canal=(ligne.get("canal") or "email").strip(),
                )
            )
    return pieces


def lire_alias(chemin: Path | str | None) -> dict[str, str]:
    """alias_fournisseurs.csv : alias,fournisseur  ->  {alias: fournisseur}.

    Resout un libelle bancaire abrege (« AMZN MKTPLACE ») vers le nom de fournisseur
    de la facture (« Amazon EU Sarl »). Fichier absent ou chemin `None` : aucun alias,
    sans erreur. Fichier mal forme : `ErreurFormat` lisible, avec le numero de ligne.
    """
    if not chemin or not Path(chemin).exists():
        return {}
    chemin = Path(chemin)
    alias: dict[str, str] = {}
    vus: dict[str, tuple[str, int]] = {}      # alias normalise -> (fournisseur, ligne)
    with open(chemin, newline="", encoding="utf-8-sig") as f:
        entetes, lignes = _lignes(f, chemin.name)
        _exiger(set(entetes), {"alias", "fournisseur"}, chemin.name)
        for numero, ligne in lignes:
            cle = (ligne.get("alias") or "").strip()
            valeur = (ligne.get("fournisseur") or "").strip()
            if not cle or not valeur:
                raise ErreurFormat(
                    f"{chemin.name} ligne {numero} : `alias` et `fournisseur` sont obligatoires"
                )
            forme = normaliser(cle)
            if not forme:
                raise ErreurFormat(
                    f"{chemin.name} ligne {numero} : l'alias « {cle} » est vide une fois normalise "
                    "(il ne contient que des mots de canal ou de la ponctuation) : il apparierait "
                    "n'importe quel libelle"
                )
            if forme in vus and vus[forme][0] != valeur:
                raise ErreurFormat(
                    f"{chemin.name} ligne {numero} : l'alias « {cle} » est deja defini ligne "
                    f"{vus[forme][1]} pour « {vus[forme][0]} » : deux fournisseurs pour un meme alias"
                )
            vus.setdefault(forme, (valeur, numero))
            alias[cle] = valeur
    return alias


def lire_dossiers(
    chemin: Path | str, *, circuit_obligatoire: bool = False
) -> dict[str, Dossier]:
    """dossiers.csv : dossier,raison_sociale,email_contact,nom_contact[,jour_echeance_tva,ton_relance,circuit,email_relais,domaines]

    `circuit_obligatoire` (decision D6 du cahier des charges) : qui relance qui est un
    parametre OBLIGATOIRE du dossier. Un dossier sans circuit explicite est alors
    refuse, au lieu de recevoir C-DIRECT en silence, c'est-a-dire d'ecrire directement
    au client final, ce que D6 veut precisement eviter. Le defaut `False` conserve le
    comportement historique (corpus anterieurs, mode test).
    """
    chemin = Path(chemin)
    dossiers: dict[str, Dossier] = {}
    with open(chemin, newline="", encoding="utf-8-sig") as f:
        entetes, lignes = _lignes(f, chemin.name)
        _exiger(set(entetes), {"dossier", "raison_sociale", "email_contact"}, chemin.name)
        for numero, ligne in lignes:
            code = _nfc(ligne["dossier"])
            if code in dossiers:
                raise ErreurFormat(
                    f"{chemin.name} ligne {numero} : le dossier {code} est declare deux fois. "
                    "La derniere ligne ecraserait la premiere en silence (circuit, adresse "
                    "relais, domaines) : gardez une seule ligne par dossier"
                )
            circuit_saisi = (ligne.get("circuit") or "").strip().upper()
            if circuit_obligatoire and not circuit_saisi:
                raise ErreurFormat(
                    f"{chemin.name} : circuit manquant pour {code}. Le circuit de relance "
                    f"est obligatoire ({', '.join(CIRCUITS)}) : il dit a QUI les relances "
                    "sont envoyees (client final, cabinet donneur d'ordre ou collaborateur)"
                )
            circuit = circuit_saisi or "C-DIRECT"
            email_relais = (ligne.get("email_relais") or "").strip()
            if circuit not in CIRCUITS:
                raise ErreurFormat(
                    f"{chemin.name} : circuit '{circuit}' inconnu pour {code} "
                    f"(attendu : {', '.join(CIRCUITS)})"
                )
            if circuit == "C-DIRECT" and not _ADRESSE.fullmatch((ligne["email_contact"] or "").strip()):
                raise ErreurFormat(
                    f"{chemin.name} ligne {numero} : dossier {code} en C-DIRECT sans adresse "
                    f"email_contact valide ({(ligne['email_contact'] or '').strip()!r}) : une seule "
                    "adresse du type nom@domaine.fr est attendue"
                )
            if circuit == "C-RELAIS" and not email_relais:
                raise ErreurFormat(
                    f"{chemin.name} : le dossier {code} est en C-RELAIS "
                    "mais email_relais est vide"
                )
            if email_relais and not _ADRESSE.fullmatch(email_relais):
                raise ErreurFormat(
                    f"{chemin.name} ligne {numero} : email_relais invalide pour {code} ({email_relais!r})"
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
