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
- Verrouillage des ecritures (et des lectures de `verifier`/`lire`) :
  * un verrou de FIL partage au niveau du module, indexe par chemin absolu
    resolu : deux objets `JournalAudit` sur le meme fichier se serialisent ;
  * avec `fcntl` : `flock` exclusif en plus (entre processus) ;
  * sans `fcntl` (Windows) : fichier `<journal>.lock` cree en `O_CREAT|O_EXCL`
    (PID + horodatage), attente bornee (`delai_verrou`, defaut 10 s, sinon
    `JournalVerrouille`), toujours libere dans un `finally`. Un verrou de plus
    de 30 s dont le PID n'existe plus est repris ; un verrou recent n'est jamais
    ecrase. Limite : la reprise d'un verrou perime n'est pas strictement
    atomique si deux processus la tentent exactement en meme temps.
  Dans tous les cas le dernier `seq` et le dernier hash sont relus dans le
  fichier APRES l'obtention du verrou, jamais memorises dans l'objet.
- Le chemin Windows (`O_EXCL`, test de PID via ctypes) n'a ete teste que sous
  Linux, avec `fcntl` rendu indisponible.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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


class JournalVerrouille(TimeoutError):
    """Le verrou d'ecriture du journal n'a pas pu etre obtenu dans le delai."""


DELAI_VERROU_DEFAUT = 10.0     # secondes d'attente maximale du verrou fichier
AGE_VERROU_PERIME = 30.0       # secondes : en dessous, un verrou n'est jamais repris

_VERROUS_FIL: dict[str, threading.Lock] = {}
_GARDE_VERROUS_FIL = threading.Lock()


def _verrou_fil(cle: str) -> threading.Lock:
    """Verrou de fil partage par tous les objets visant le meme fichier."""
    with _GARDE_VERROUS_FIL:
        verrou = _VERROUS_FIL.get(cle)
        if verrou is None:
            verrou = _VERROUS_FIL[cle] = threading.Lock()
        return verrou


def _pid_vivant(pid: int) -> bool:
    """Le processus `pid` existe-t-il ? En cas de doute : oui (verrou respecte)."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - non testable sous Linux
        # os.kill(pid, 0) TUERAIT le processus sous Windows : on passe par l'API.
        try:
            import ctypes

            noyau = ctypes.windll.kernel32  # type: ignore[attr-defined]
            poignee = noyau.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if poignee:
                noyau.CloseHandle(poignee)
                return True
            return noyau.GetLastError() == 5  # acces refuse : il existe
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


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


@contextmanager
def _sans_verrou() -> Iterator[None]:
    yield


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class JournalAudit:
    def __init__(self, chemin: Path | str, *, delai_verrou: float = DELAI_VERROU_DEFAUT) -> None:
        self.chemin = Path(chemin)
        self.delai_verrou = delai_verrou
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        existait = self.chemin.exists()
        # Cree le fichier s'il manque : un journal qui disparait ensuite est
        # signale par verifier() au lieu d'etre pris pour un journal neuf.
        with open(self.chemin, "ab"):
            pass
        if not existait:
            self._fsync_repertoire()
        self._cle_verrou = os.path.normcase(os.path.realpath(os.path.abspath(self.chemin)))
        self.chemin_verrou = self.chemin.with_name(self.chemin.name + ".lock")

    # -- verrous -----------------------------------------------------------

    @staticmethod
    def _verrouiller(f: Any, exclusif: bool) -> None:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX if exclusif else fcntl.LOCK_SH)

    @contextmanager
    def _verrou_ecriture(self) -> Iterator[None]:
        """Verrou exclusif valable entre objets, fils et processus."""
        with _verrou_fil(self._cle_verrou):
            if fcntl is not None:
                yield  # le flock est pris sur le fichier ouvert, par l'appelant
                return
            jeton = self._prendre_verrou_fichier()
            try:
                yield
            finally:
                self._rendre_verrou_fichier(jeton)

    def _prendre_verrou_fichier(self) -> str:
        jeton = f"{os.getpid()}-{uuid.uuid4().hex}"
        contenu = _canonique(
            {"pid": os.getpid(), "horodatage": time.time(), "jeton": jeton}
        ).encode("ascii")
        limite = time.monotonic() + self.delai_verrou
        pause = 0.002
        while True:
            try:
                fd = os.open(
                    self.chemin_verrou, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
                )
            except FileExistsError:
                if self._reprendre_si_perime():
                    continue
                if time.monotonic() >= limite:
                    raise JournalVerrouille(
                        f"verrou {self.chemin_verrou} detenu depuis plus de "
                        f"{self.delai_verrou:g} s : un autre processus ecrit dans le journal"
                    ) from None
                time.sleep(pause)
                pause = min(pause * 1.5, 0.05)
                continue
            try:
                os.write(fd, contenu)
            except BaseException:
                os.close(fd)
                self._supprimer_verrou()
                raise
            os.close(fd)
            return jeton

    def _supprimer_verrou(self) -> None:
        try:
            os.unlink(self.chemin_verrou)
        except FileNotFoundError:
            pass

    def _rendre_verrou_fichier(self, jeton: str) -> None:
        try:
            actuel = json.loads(self.chemin_verrou.read_bytes())
        except FileNotFoundError:
            return
        except Exception:
            actuel = {}
        # Ne supprime pas le verrou d'un autre (repris a tort apres un gel).
        if not isinstance(actuel, dict) or actuel.get("jeton") in (jeton, None):
            self._supprimer_verrou()

    def _reprendre_si_perime(self) -> bool:
        """Retire le verrou s'il est perime (> 30 s et PID absent ou inconnu).

        Jamais pour un verrou recent. Le fichier est d'abord renomme (atomique) :
        si ce n'etait pas un verrou perime, on le remet en place.
        """
        try:
            age_fichier = time.time() - self.chemin_verrou.stat().st_mtime
        except FileNotFoundError:
            return True  # libere entre-temps : reessayer
        if age_fichier < AGE_VERROU_PERIME:
            return False
        if not self._verrou_perime():
            return False
        ecarte = self.chemin_verrou.with_name(
            f"{self.chemin_verrou.name}.perime-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        try:
            os.replace(self.chemin_verrou, ecarte)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            if self._verrou_perime(ecarte):
                return True
            # On a ecarte un verrou vivant par mauvaise chance : on le restitue.
            try:
                os.link(ecarte, self.chemin_verrou)
            except OSError:
                pass
            return False
        finally:
            try:
                os.unlink(ecarte)
            except OSError:
                pass

    def _verrou_perime(self, chemin: Path | None = None) -> bool:
        chemin = chemin or self.chemin_verrou
        try:
            donnees = json.loads(chemin.read_bytes())
            pid = int(donnees["pid"])
            horodatage = float(donnees["horodatage"])
        except FileNotFoundError:
            return True
        except Exception:
            # Contenu illisible : PID inconnu, seul l'age du fichier compte.
            try:
                return time.time() - chemin.stat().st_mtime >= AGE_VERROU_PERIME
            except OSError:
                return False
        if time.time() - horodatage < AGE_VERROU_PERIME:
            return False
        return not _pid_vivant(pid)

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

        with self._verrou_ecriture(), open(self.chemin, "a+b") as f:
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
        with self._verrou_ecriture(), open(self.chemin, "a+b") as f:
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
        # Sans fcntl, une lecture coherente exige le meme verrou que l'ecriture
        # (sinon on peut lire une ligne a moitie ecrite).
        with self._verrou_ecriture() if fcntl is None else _sans_verrou():
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
