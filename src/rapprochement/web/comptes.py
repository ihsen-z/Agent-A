"""Comptes locaux de l'application web : `<instance>/utilisateurs.json`.

- Mot de passe hache par PBKDF2-HMAC-SHA256, sel aleatoire de 16 octets,
  `ITERATIONS` (>= 600 000) ; les parametres sont stockes avec le hachage.
- Ecriture atomique (fichier temporaire + fsync + `os.replace`) : le serveur peut
  relire le fichier pendant qu'une commande le modifie.
- `validateurs.txt` reste la barriere du cycle (source unique) : creer un compte y
  AJOUTE le nom affiche, le supprimer l'en RETIRE. L'application web ne la contourne
  jamais : chaque decision passe `par` = nom affiche a une fonction de `cycle`, qui
  verifie ce nom dans `validateurs.txt`.

Ordre des ecritures (en cas d'echec au milieu, on perd un droit, jamais l'inverse) :
creation = compte puis validateur ; suppression = validateur puis compte.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import etats
from ..cycle import Instance

FICHIER_UTILISATEURS = "utilisateurs.json"
ALGORITHME = "pbkdf2_sha256"
ITERATIONS = 600_000
TAILLE_SEL = 16
LONGUEUR_MIN_MOT_DE_PASSE = 10
LONGUEUR_MAX_MOT_DE_PASSE = 1024
LONGUEUR_MAX_NOM = 80
RESPONSABLE = "responsable"
COMPTABLE = "comptable"
ROLES = (RESPONSABLE, COMPTABLE)
_IDENTIFIANT = re.compile(r"[a-z0-9][a-z0-9._-]{1,31}")


class ErreurCompte(Exception):
    """Refus lisible d'une operation sur les comptes (rien n'a ete modifie)."""


@dataclass(frozen=True)
class Utilisateur:
    identifiant: str
    nom: str
    role: str
    hachage: Mapping[str, Any]

    @property
    def est_responsable(self) -> bool:
        return self.role == RESPONSABLE


# ---------------------------------------------------------------------------
# Hachage
# ---------------------------------------------------------------------------


def hacher(mot_de_passe: str, *, iterations: int = ITERATIONS, sel: bytes | None = None) -> dict[str, Any]:
    if sel is None:
        sel = secrets.token_bytes(TAILLE_SEL)
    valeur = hashlib.pbkdf2_hmac("sha256", mot_de_passe.encode("utf-8", "surrogatepass"), sel, iterations)
    return {"algorithme": ALGORITHME, "iterations": iterations, "sel": sel.hex(), "valeur": valeur.hex()}


def verifier_mot_de_passe(mot_de_passe: str, hachage: Mapping[str, Any]) -> bool:
    """Comparaison en temps constant ; tout hachage malforme vaut un refus."""
    try:
        if hachage.get("algorithme") != ALGORITHME:
            return False
        iterations = int(hachage["iterations"])
        sel = bytes.fromhex(str(hachage["sel"]))
        attendu = bytes.fromhex(str(hachage["valeur"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    if iterations < 1 or len(attendu) != 32:
        return False
    calcule = hashlib.pbkdf2_hmac("sha256", mot_de_passe.encode("utf-8", "surrogatepass"), sel, iterations)
    return hmac.compare_digest(calcule, attendu)


_FACTICE: dict[str, Any] | None = None


def hachage_factice() -> dict[str, Any]:
    """Hachage d'un secret aleatoire, aux parametres courants : verifie contre un identifiant
    inconnu, il egalise le temps de reponse (pas d'enumeration des comptes)."""
    global _FACTICE
    if _FACTICE is None or _FACTICE["iterations"] != ITERATIONS:
        _FACTICE = hacher(secrets.token_urlsafe(32))
    return _FACTICE


# ---------------------------------------------------------------------------
# Controles de saisie
# ---------------------------------------------------------------------------


def normaliser_identifiant(identifiant: str) -> str:
    return identifiant.strip().lower() if isinstance(identifiant, str) else ""


def exiger_identifiant(identifiant: str) -> str:
    ident = normaliser_identifiant(identifiant)
    if not _IDENTIFIANT.fullmatch(ident):
        raise ErreurCompte(
            "identifiant invalide : 2 a 32 caracteres parmi a-z, 0-9, point, tiret, souligne "
            "(commence par une lettre ou un chiffre)"
        )
    return ident


def exiger_nom(nom: str) -> str:
    """Le nom affiche devient une ligne de validateurs.txt : une seule ligne, sans controle."""
    if not isinstance(nom, str):
        raise ErreurCompte("nom affiche obligatoire")
    propre = unicodedata.normalize("NFC", nom.strip())
    if not propre:
        raise ErreurCompte("nom affiche obligatoire")
    if len(propre) > LONGUEUR_MAX_NOM:
        raise ErreurCompte(f"nom affiche trop long (au plus {LONGUEUR_MAX_NOM} caracteres)")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in propre):
        raise ErreurCompte("nom affiche : caractere de controle ou saut de ligne interdit")
    if propre.startswith("#"):
        raise ErreurCompte("nom affiche : ne doit pas commencer par # (commentaire de validateurs.txt)")
    if not etats.est_acteur_humain(propre):
        raise ErreurCompte(f"nom affiche {propre!r} : reserve aux acteurs automatiques")
    return propre


def exiger_role(role: str) -> str:
    if role not in ROLES:
        raise ErreurCompte(f"role invalide {role!r} (attendu : {', '.join(ROLES)})")
    return role


def exiger_mot_de_passe(mot_de_passe: str) -> str:
    if not isinstance(mot_de_passe, str) or len(mot_de_passe) < LONGUEUR_MIN_MOT_DE_PASSE:
        raise ErreurCompte(f"mot de passe trop court (au moins {LONGUEUR_MIN_MOT_DE_PASSE} caracteres)")
    if len(mot_de_passe) > LONGUEUR_MAX_MOT_DE_PASSE:
        raise ErreurCompte(f"mot de passe trop long (au plus {LONGUEUR_MAX_MOT_DE_PASSE} caracteres)")
    return mot_de_passe


# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------


def chemin_utilisateurs(instance: Instance | Path | str) -> Path:
    return Instance.de(instance).racine / FICHIER_UTILISATEURS


def ecrire_atomique(chemin: Path, contenu: bytes) -> None:
    """Fichier temporaire dans le meme repertoire, fsync, puis `os.replace` (Windows compris)."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=chemin.parent, prefix=f".{chemin.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(contenu)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, chemin)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def lire_utilisateurs(instance: Instance | Path | str) -> dict[str, Utilisateur]:
    """Identifiant -> Utilisateur. Fichier absent : {}. Fichier illisible : `ErreurCompte`."""
    chemin = chemin_utilisateurs(instance)
    if not chemin.exists():
        return {}
    try:
        donnees = json.loads(chemin.read_text(encoding="utf-8-sig"))
        resultat = {}
        for d in donnees["utilisateurs"]:
            u = Utilisateur(str(d["identifiant"]), str(d["nom"]), str(d["role"]), dict(d["hachage"]))
            resultat[u.identifiant] = u
        return resultat
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ErreurCompte(f"{chemin.name} illisible ({type(exc).__name__})") from None


def ecrire_utilisateurs(instance: Instance | Path | str, utilisateurs: Mapping[str, Utilisateur]) -> None:
    document = {
        "version": 1,
        "utilisateurs": [
            {"identifiant": u.identifiant, "nom": u.nom, "role": u.role, "hachage": dict(u.hachage)}
            for u in sorted(utilisateurs.values(), key=lambda u: u.identifiant)
        ],
    }
    texte = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ecrire_atomique(chemin_utilisateurs(instance), texte.encode("utf-8"))


def _lignes_validateurs(instance: Instance) -> list[str]:
    if not instance.validateurs.exists():
        return []
    try:
        return instance.validateurs.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ErreurCompte(f"{instance.validateurs.name} illisible ({type(exc).__name__})") from None


def ajouter_validateur(instance: Instance | Path | str, nom: str) -> bool:
    """Ajoute `nom` a validateurs.txt s'il n'y est pas (insensible a la casse). True si ajoute."""
    inst = Instance.de(instance)
    lignes = _lignes_validateurs(inst)
    cherche = nom.strip().casefold()
    if any(l.strip().casefold() == cherche for l in lignes if not l.strip().startswith("#")):
        return False
    lignes.append(nom.strip())
    ecrire_atomique(inst.validateurs, ("\n".join(lignes) + "\n").encode("utf-8"))
    return True


def retirer_validateur(instance: Instance | Path | str, nom: str) -> bool:
    """Retire toute ligne egale a `nom` (insensible a la casse). True si retire."""
    inst = Instance.de(instance)
    lignes = _lignes_validateurs(inst)
    cherche = nom.strip().casefold()
    gardees = [l for l in lignes if l.strip().startswith("#") or l.strip().casefold() != cherche]
    if len(gardees) == len(lignes):
        return False
    ecrire_atomique(inst.validateurs, ("\n".join(gardees) + "\n" if gardees else "").encode("utf-8"))
    return True


# ---------------------------------------------------------------------------
# Operations (appelees par scripts/web.py)
# ---------------------------------------------------------------------------


def creer_utilisateur(instance: Instance | Path | str, identifiant: str, nom: str, role: str,
                      mot_de_passe: str) -> Utilisateur:
    ident = exiger_identifiant(identifiant)
    nom = exiger_nom(nom)
    role = exiger_role(role)
    exiger_mot_de_passe(mot_de_passe)
    utilisateurs = lire_utilisateurs(instance)
    if ident in utilisateurs:
        raise ErreurCompte(f"l'identifiant {ident!r} existe deja")
    if any(u.nom.casefold() == nom.casefold() for u in utilisateurs.values()):
        raise ErreurCompte(f"le nom affiche {nom!r} est deja pris par un autre compte")
    u = Utilisateur(ident, nom, role, hacher(mot_de_passe))
    utilisateurs[ident] = u
    ecrire_utilisateurs(instance, utilisateurs)
    ajouter_validateur(instance, nom)
    return u


def changer_mot_de_passe(instance: Instance | Path | str, identifiant: str, mot_de_passe: str) -> Utilisateur:
    ident = normaliser_identifiant(identifiant)
    exiger_mot_de_passe(mot_de_passe)
    utilisateurs = lire_utilisateurs(instance)
    if ident not in utilisateurs:
        raise ErreurCompte(f"identifiant inconnu : {ident!r}")
    ancien = utilisateurs[ident]
    u = Utilisateur(ancien.identifiant, ancien.nom, ancien.role, hacher(mot_de_passe))
    utilisateurs[ident] = u
    ecrire_utilisateurs(instance, utilisateurs)
    return u


def supprimer_utilisateur(instance: Instance | Path | str, identifiant: str) -> Utilisateur:
    ident = normaliser_identifiant(identifiant)
    utilisateurs = lire_utilisateurs(instance)
    if ident not in utilisateurs:
        raise ErreurCompte(f"identifiant inconnu : {ident!r}")
    u = utilisateurs.pop(ident)
    retirer_validateur(instance, u.nom)
    ecrire_utilisateurs(instance, utilisateurs)
    return u


__all__ = [
    "COMPTABLE", "ErreurCompte", "ITERATIONS", "RESPONSABLE", "ROLES", "Utilisateur",
    "ajouter_validateur", "changer_mot_de_passe", "creer_utilisateur", "hacher",
    "hachage_factice", "lire_utilisateurs", "retirer_validateur", "supprimer_utilisateur",
    "verifier_mot_de_passe",
]
