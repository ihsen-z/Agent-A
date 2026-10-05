"""Journal d'audit chaine (contrat 4.1 de docs/architecture_mvp.md).

Format : JSONL, ajout seulement. Chaque ligne est le JSON canonique (cles
triees, separateurs compacts, ASCII pur) d'une entree :

    {"acteur", "action", "details", "hash", "hash_precedent", "horodatage",
     "objet", "seq"}

`hash` = SHA-256 du JSON canonique de l'entree SANS le champ `hash`.
`hash_precedent` = `hash` de l'entree precedente ("0" * 64 pour la premiere).
Modifier, retirer, inserer ou reordonner une ligne casse la chaine.

Convention de `premiere_erreur_seq` : numero de la PREMIERE ligne fautive,
compte a partir de 1. Comme une ligne saine porte `seq` == son rang, c'est aussi
le `seq` que cette ligne aurait du porter. Pour une entree supprimee au milieu,
c'est donc le `seq` de l'entree manquante (la ligne qui la suit porte `seq`+1).

Limites connues :
- Une chaine de hachage ne detecte pas la suppression des DERNIERES entrees
  (coupure nette sur une frontiere de ligne), ni la reecriture complete du
  fichier par quelqu'un qui recalcule toute la chaine. Pour cela il faut ancrer
  le dernier hash hors du fichier (hors perimetre du MVP).
- Le verrou exclusif repose sur `fcntl.flock` (Linux/macOS). Sans `fcntl`, seul
  un verrou de thread protege les ecritures d'un meme processus.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

try:  # pragma: no cover - depend de la plateforme
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

HASH_INITIAL = "0" * 64

_CLES_ENTREE = frozenset(
    {"seq", "horodatage", "acteur", "action", "objet", "details", "hash_precedent", "hash"}
)


class JournalCorrompu(ValueError):
    """Le fichier du journal n'a pas la structure attendue."""


@dataclass(frozen=True)
class EntreeAudit:
    seq: int
    horodatage: str
    acteur: str
    action: str
    objet: str
    details: dict
    hash_precedent: str
    hash: str


@dataclass(frozen=True)
class ResultatVerification:
    """`nb_entrees` = nombre d'entrees verifiees et saines (toutes si `ok`,
    celles qui precedent la premiere erreur sinon)."""

    ok: bool
    nb_entrees: int
    premiere_erreur_seq: int | None
    raison: str


# ---------------------------------------------------------------------------
# Serialisation deterministe
# ---------------------------------------------------------------------------


def _canonique(objet: Any) -> str:
    return json.dumps(
        objet, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _normaliser(valeur: Any, chemin: str) -> Any:
    """Convertit `valeur` en types JSON natifs, de facon deterministe."""
    if isinstance(valeur, Enum):  # avant str/int : un Enum peut en heriter
        return _normaliser(valeur.value, chemin)
    if valeur is None or isinstance(valeur, (bool, int, str)):
        return valeur
    if isinstance(valeur, float):
        if valeur != valeur or valeur in (float("inf"), float("-inf")):
            raise ValueError(f"{chemin} : flottant non fini ({valeur!r}) non serialisable")
        return valeur
    if isinstance(valeur, Decimal):
        if not valeur.is_finite():
            raise ValueError(f"{chemin} : Decimal non fini ({valeur!r}) non serialisable")
        return str(valeur)
    if isinstance(valeur, dt.datetime):  # avant date : datetime en herite
        return valeur.isoformat()
    if isinstance(valeur, dt.date):
        return valeur.isoformat()
    if isinstance(valeur, Mapping):
        sortie: dict[str, Any] = {}
        for cle, v in valeur.items():
            if not isinstance(cle, str):
                raise TypeError(
                    f"{chemin} : cle de dictionnaire de type {type(cle).__name__} "
                    "(str attendu)"
                )
            sortie[cle] = _normaliser(v, f"{chemin}.{cle}")
        return sortie
    if isinstance(valeur, (list, tuple)):
        return [_normaliser(v, f"{chemin}[{i}]") for i, v in enumerate(valeur)]
    if isinstance(valeur, (set, frozenset)):
        elements = [_normaliser(v, f"{chemin}{{}}") for v in valeur]
        return sorted(elements, key=_canonique)
    raise TypeError(
        f"{chemin} : type {type(valeur).__name__} non serialisable dans le journal d'audit"
    )


def _calculer_hash(entree_sans_hash: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonique(entree_sans_hash).encode("ascii")).hexdigest()


def _analyser_ligne(brut: bytes) -> tuple[EntreeAudit, dict[str, Any]]:
    """Decode une ligne. Leve `JournalCorrompu` si elle n'a pas la bonne forme."""
    try:
        texte = brut.decode("utf-8")
        donnees = json.loads(texte)
    except Exception as exc:  # UnicodeDecodeError, JSONDecodeError, RecursionError...
        raise JournalCorrompu(f"ligne illisible (JSON invalide) : {exc}") from exc
    if not isinstance(donnees, dict):
        raise JournalCorrompu("la ligne n'est pas un objet JSON")
    if set(donnees) != _CLES_ENTREE:
        raise JournalCorrompu(
            "champs inattendus ou manquants : " + ", ".join(sorted(set(donnees) ^ _CLES_ENTREE))
        )
    seq = donnees["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise JournalCorrompu("seq n'est pas un entier")
    for nom in ("horodatage", "acteur", "action", "objet", "hash_precedent", "hash"):
        if not isinstance(donnees[nom], str):
            raise JournalCorrompu(f"{nom} n'est pas une chaine")
    if not isinstance(donnees["details"], dict):
        raise JournalCorrompu("details n'est pas un objet")
    entree = EntreeAudit(
        seq=seq,
        horodatage=donnees["horodatage"],
        acteur=donnees["acteur"],
        action=donnees["action"],
        objet=donnees["objet"],
        details=donnees["details"],
        hash_precedent=donnees["hash_precedent"],
        hash=donnees["hash"],
    )
    return entree, donnees


def _sans_hash(donnees: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in donnees.items() if k != "hash"}


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class JournalAudit:
    def __init__(self, chemin: Path | str) -> None:
        self.chemin = Path(chemin)
        self._verrou_thread = threading.Lock()
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        existait = self.chemin.exists()
        # Cree le fichier s'il manque : un journal qui disparait ensuite est
        # signale par verifier() au lieu d'etre pris pour un journal neuf.
        with open(self.chemin, "ab"):
            pass
        if not existait:
            self._fsync_repertoire()

    # -- verrous -----------------------------------------------------------

    @staticmethod
    def _verrouiller(f: Any, exclusif: bool) -> None:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX if exclusif else fcntl.LOCK_SH)

    def _fsync_repertoire(self) -> None:
        try:
            fd = os.open(self.chemin.parent, os.O_RDONLY)
        except OSError:  # plateformes sans fsync de repertoire
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # -- ecriture ----------------------------------------------------------

    def ecrire(
        self,
        acteur: str,
        action: str,
        objet: str,
        details: Mapping[str, Any] | None = None,
        *,
        horodatage: dt.datetime | None = None,
    ) -> EntreeAudit:
        if not isinstance(acteur, str) or not acteur.strip():
            raise ValueError("acteur obligatoire et non vide ('systeme' est un acteur valide)")
        if not isinstance(action, str) or not isinstance(objet, str):
            raise TypeError("action et objet doivent etre des chaines")
        if details is None:
            details = {}
        if not isinstance(details, Mapping):
            raise TypeError("details doit etre un dictionnaire")
        if horodatage is None:
            horodatage = dt.datetime.now(dt.timezone.utc)
        if not isinstance(horodatage, dt.datetime):
            raise TypeError("horodatage doit etre un datetime")
        try:
            details_n = _normaliser(details, "details")
        except RecursionError as exc:
            raise ValueError("details contient une reference circulaire") from exc

        with self._verrou_thread, open(self.chemin, "a+b") as f:
            self._verrouiller(f, exclusif=True)  # relache a la fermeture du fichier
            taille = f.seek(0, os.SEEK_END)
            dernier_seq, dernier_hash = self._dernier(f, taille)
            corps = {
                "seq": dernier_seq + 1,
                "horodatage": horodatage.isoformat(),
                "acteur": acteur,
                "action": action,
                "objet": objet,
                "details": details_n,
                "hash_precedent": dernier_hash,
            }
            complet = {**corps, "hash": _calculer_hash(corps)}
            ligne = (_canonique(complet) + "\n").encode("ascii")
            try:
                f.write(ligne)
                f.flush()
                os.fsync(f.fileno())
            except BaseException:
                # Ne laisse pas de ligne a moitie ecrite qui casserait la chaine.
                try:
                    f.truncate(taille)
                    f.flush()
                except Exception:
                    pass
                raise
        entree, _ = _analyser_ligne(ligne[:-1])
        return entree

    def reparer_fin_tronquee(self) -> int:
        """Retire une derniere ligne incomplete laissee par une coupure.

        Repare SI ET SEULEMENT SI la derniere ligne est incomplete (pas de saut
        de ligne final, ou JSON invalide) ET que toutes les lignes completes qui
        la precedent verifient (chaine intacte). Le fragment est d'abord copie
        dans `<chemin>.fragment-<AAAAMMJJTHHMMSSZ>` (jamais de suppression
        silencieuse), puis le fichier est tronque juste avant et synchronise.
        Renvoie le nombre d'octets retires.

        Exception : si le fragment final est en fait une entree COMPLETE et
        authentique (seq, chainage, hash corrects) a laquelle il ne manque que
        le saut de ligne, on ajoute ce saut de ligne : rien n'est retire, aucun
        fichier fragment, retour 0.

        Renvoie 0, sans rien toucher, si le journal est vide ou sain. Leve
        `JournalCorrompu`, sans RIEN modifier, si une entree complete est
        alteree ou si la chaine est rompue ailleurs qu'en fin : reparer
        effacerait la preuve d'une falsification. Meme verrou exclusif que
        `ecrire` ; idempotent.
        """
        with self._verrou_thread, open(self.chemin, "a+b") as f:
            self._verrouiller(f, exclusif=True)
            f.seek(0)
            donnees = f.read()
            if not donnees:
                return 0
            lignes = donnees.split(b"\n")
            termine = lignes[-1] == b""
            if termine:
                lignes.pop()
            candidate = lignes[-1]
            precedentes = lignes[:-1]

            precedent = HASH_INITIAL
            for rang, brut in enumerate(precedentes, start=1):
                try:
                    precedent = self._controler(brut, rang, precedent)
                except JournalCorrompu as exc:
                    raise JournalCorrompu(
                        f"ligne {rang} alteree, reparation refusee (rien modifie) : {exc}"
                    ) from exc

            rang = len(precedentes) + 1
            if termine:
                try:
                    self._controler(candidate, rang, precedent)
                    return 0  # derniere ligne complete et valide : journal sain
                except JournalCorrompu as exc:
                    try:
                        _analyser_ligne(candidate)
                    except JournalCorrompu:
                        pass  # JSON invalide : fin abimee, a reparer
                    else:
                        raise JournalCorrompu(
                            f"ligne {rang} alteree, reparation refusee (rien modifie) : {exc}"
                        ) from exc
            if not termine:
                try:
                    self._controler(candidate, rang, precedent)
                except JournalCorrompu:
                    pass  # vrai fragment : traite ci-dessous
                else:
                    # Entree authentique (seq, chainage, hash, forme) : il ne
                    # manque que le saut de ligne. On le complete, rien n'est retire.
                    f.write(b"\n")
                    f.flush()
                    os.fsync(f.fileno())
                    return 0
            longueur = len(candidate) + (1 if termine else 0)
            debut = len(donnees) - longueur
            fragment = donnees[debut:]
            self._sauver_fragment(fragment)
            f.truncate(debut)
            f.flush()
            os.fsync(f.fileno())
            return len(fragment)

    @staticmethod
    def _controler(brut: bytes, rang: int, precedent: str) -> str:
        """Verifie une ligne complete ; renvoie son hash ou leve `JournalCorrompu`."""
        entree, brutes = _analyser_ligne(brut)
        if entree.seq != rang:
            raise JournalCorrompu(f"seq non contigu (attendu {rang}, trouve {entree.seq})")
        if entree.hash_precedent != precedent:
            raise JournalCorrompu("chaine rompue")
        if _calculer_hash(_sans_hash(brutes)) != entree.hash:
            raise JournalCorrompu("entree modifiee")
        if brut != _canonique(brutes).encode("ascii"):
            raise JournalCorrompu("ligne non canonique")
        return entree.hash

    def _sauver_fragment(self, fragment: bytes) -> Path:
        horodatage = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = self.chemin.with_name(f"{self.chemin.name}.fragment-{horodatage}")
        for n in range(1000):
            cible = base if n == 0 else base.with_name(f"{base.name}-{n}")
            try:
                with open(cible, "xb") as g:
                    g.write(fragment)
                    g.flush()
                    os.fsync(g.fileno())
                self._fsync_repertoire()
                return cible
            except FileExistsError:
                continue
        raise OSError("impossible de creer un fichier fragment")

    @staticmethod
    def _dernier(f: Any, taille: int) -> tuple[int, str]:
        """(seq, hash) de la derniere entree, lue par la fin du fichier.

        Refuse (`JournalCorrompu`) d'etendre un journal dont la fin est abimee :
        ajouter apres une ligne tronquee la figerait au milieu de la chaine.
        """
        if taille == 0:
            return 0, HASH_INITIAL
        pos = taille
        tampon = b""
        while True:
            pas = min(65536, pos)
            pos -= pas
            f.seek(pos)
            tampon = f.read(pas) + tampon
            if not tampon.endswith(b"\n"):
                raise JournalCorrompu(
                    "la derniere ligne du journal n'est pas terminee (ecriture interrompue ?) : "
                    "ecriture refusee, lancer verifier()"
                )
            corps = tampon[:-1]
            idx = corps.rfind(b"\n")
            if idx >= 0 or pos == 0:
                derniere = corps[idx + 1 :]
                break
        try:
            entree, donnees = _analyser_ligne(derniere)
        except JournalCorrompu as exc:
            raise JournalCorrompu(
                f"derniere ligne du journal illisible : ecriture refusee ({exc})"
            ) from exc
        if _calculer_hash(_sans_hash(donnees)) != entree.hash:
            raise JournalCorrompu(
                f"derniere entree (seq {entree.seq}) alteree : ecriture refusee, lancer verifier()"
            )
        return entree.seq, entree.hash

    # -- lecture -----------------------------------------------------------

    def _octets(self) -> bytes:
        with open(self.chemin, "rb") as f:
            try:
                self._verrouiller(f, exclusif=False)
            except OSError:
                pass
            return f.read()

    def lire(self) -> list[EntreeAudit]:
        """Toutes les entrees, dans l'ordre. Ne verifie PAS la chaine (voir
        `verifier`) ; leve `JournalCorrompu` si une ligne est illisible."""
        donnees = self._octets()
        if not donnees:
            return []
        lignes = donnees.split(b"\n")
        if lignes[-1] == b"":
            lignes.pop()
        sortie = []
        for rang, brut in enumerate(lignes, start=1):
            try:
                sortie.append(_analyser_ligne(brut)[0])
            except JournalCorrompu as exc:
                raise JournalCorrompu(f"ligne {rang} : {exc}") from exc
        return sortie

    def verifier(self) -> ResultatVerification:
        """Verifie la chaine. Ne leve JAMAIS."""
        try:
            return self._verifier()
        except Exception as exc:  # noqa: BLE001 - contrat : ne jamais lever
            return ResultatVerification(
                False, 0, None, f"verification impossible : {type(exc).__name__} : {exc}"
            )

    def _verifier(self) -> ResultatVerification:
        if not self.chemin.exists():
            return ResultatVerification(False, 0, None, "fichier du journal absent")
        donnees = self._octets()
        if not donnees:
            return ResultatVerification(True, 0, None, "journal vide")
        lignes = donnees.split(b"\n")
        termine = lignes[-1] == b""
        if termine:
            lignes.pop()

        def erreur(rang: int, raison: str) -> ResultatVerification:
            return ResultatVerification(False, rang - 1, rang, f"ligne {rang} : {raison}")

        precedent = HASH_INITIAL
        for rang, brut in enumerate(lignes, start=1):
            derniere_non_terminee = (not termine) and rang == len(lignes)
            try:
                entree, brutes = _analyser_ligne(brut)
            except JournalCorrompu as exc:
                if derniere_non_terminee:
                    return erreur(rang, f"derniere ligne tronquee ou invalide ({exc})")
                return erreur(rang, str(exc))
            if entree.seq != rang:
                return erreur(
                    rang,
                    f"seq non contigu (attendu {rang}, trouve {entree.seq} : "
                    "entree supprimee, inseree ou deplacee)",
                )
            if entree.hash_precedent != precedent:
                return erreur(rang, "chaine rompue (hash_precedent different du hash precedent)")
            sans_hash = _sans_hash(brutes)
            if _calculer_hash(sans_hash) != entree.hash:
                return erreur(rang, "entree modifiee (hash recalcule different)")
            canonique = _canonique(brutes).encode("ascii")
            if brut != canonique:
                return erreur(rang, "ligne non canonique (octets alteres, meme contenu logique)")
            if derniere_non_terminee:
                return erreur(rang, "derniere ligne non terminee par un saut de ligne (tronquee)")
            precedent = entree.hash
        return ResultatVerification(True, len(lignes), None, "chaine intacte")
