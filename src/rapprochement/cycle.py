"""Orchestration d'un cycle complet (integration des modules du MVP).

Ce module assemble `moteur`, `etats`, `routage`, `cadence` / `relances`,
`depot`, `audit` et `envoi`. Il ne contient aucune regle metier propre hormis
les garde-fous d'assemblage documentes ci-dessous ; les regles restent dans les
modules de leurs experts.

Disposition d'une instance (un repertoire par cabinet) :

    depot.sqlite      etat persistant (pieces, brouillons, envois, file humaine)
    audit.jsonl       journal d'audit chaine
    validateurs.txt   un nom par ligne : seules ces personnes decident
    entrant/          e-mails recus (.eml)
    outbox/           e-mails emis (adaptateur `ExpediteurDossier`)
    sortie/           vues lisibles regenerees a chaque cycle

Algorithme de `executer_cycle` (aucune horloge cachee : `aujourdhui` et
`maintenant` sont des parametres) :

    1. lire les entrees, faire tourner `Moteur` ;
    2. refuser le cycle si une reference d'operation est portee par plusieurs
       operations (deux dossiers, ou deux fois le meme), dans le releve ou entre
       le releve et le depot : `Brouillon.references` et
       `relances.construire_brouillon` indexent par reference seule ;
    4. repercuter les envois effectifs pas encore appliques (`RELANCE_ENVOYEE`),
       de facon idempotente (`depot.marquer_vu("envoi-applique:<id>")`). Execute
       AVANT l'etape 3 : un envoi parti doit etre enregistre avant que la fusion
       ne bloque ou n'exclue la piece (sinon `etats` refuserait de le refleter) ;
    3. creer les `PieceAttendue` des MANQUANT et fusionner avec le depot (une
       piece connue garde son etat) ; PARTIEL / A_VERIFIER ne sont jamais relances
       (`sortie/a_verifier.csv`) ;
    5. router les .eml de `entrant/` : PROPOSEE et NON_ROUTEE vont dans la file
       humaine, rien n'est rattache sans confirmation ;
    6. `cadence.planifier` puis application des transitions de calendrier ;
    7. construire et sauver les brouillons (idempotent), sans jamais les valider ;
    8. ecrire les sorties et le resume.

Ordre depot / journal : chaque etape VALIDE D'ABORD sa transaction SQLite, puis
ecrit ses entrees d'audit. Limite connue : un crash entre le COMMIT et
l'ecriture du journal laisse un evenement sans trace d'audit (jamais l'inverse :
le journal ne decrit jamais un evenement qui n'a pas eu lieu). Les operations
humaines deleguees a `envoi` (valider, rejeter, envoyer, trancher) suivent la
convention de ce module-la, qui journalise DANS la transaction.

Garde-fous d'assemblage (choix de l'integrateur, a revoir par le chef de projet) :

- Une piece connue dont l'operation est devenue JUSTIFIE, PARTIEL ou A_VERIFIER
  ne peut pas etre rattachee automatiquement (`PIECE_RATTACHEE` exige
  `confirme_par` humain). Elle est BLOQUEE (`BLOCAGE_SIGNALE`, motif prefixe
  par `PREFIXE_MOTIF_CYCLE`) : elle n'est plus relancee et attend un humain.
  Le blocage est leve si l'operation redevient MANQUANT, ou apres `rattacher`.
  Si un humain le leve (`debloquer`), le cycle ne le repose plus.
- Une piece connue dont l'operation est devenue HORS_PERIMETRE sort par
  `EXCLUSION_CREEE` (`regle_validee=True` : les regles viennent du referentiel
  general et de `exclusions_dossier.csv`, tenu par le cabinet).
- Une relance planifiee dont une piece figure deja dans un brouillon en attente
  (BROUILLON, VALIDEE, EN_COURS) d'identifiant different n'est pas creee
  (report `BROUILLON_EN_ATTENTE`) : deux brouillons concurrents pour la meme
  piece finiraient par partir tous les deux.
- `envoyer_relance` refuse un envoi si le destinataire a deja recu un e-mail
  dans les 7 derniers jours ou si une piece reclamee n'est plus reclamable :
  `envoi.envoyer` ne connait pas ces regles.
- Les commandes humaines exigent un nom present dans `validateurs.txt`
  (insensible a la casse ; fichier absent ou vide : refus).

Suivi humain : `saisir_promesse`, `arbitrer_piece`, `bloquer_piece`,
`debloquer_piece` (meme garde, audit apres COMMIT, rejeu sans effet) et
`suivi_pieces` (lecture seule, echeances calculees par `cadence.planifier`).
Le cycle ecrit `sortie/escalades.csv` (pieces ESCALADEE) a chaque passage.

Mode test (essais locaux, donnees fictives) : `<instance>/config.json` (`mode_test`,
`adresse_test`) ou drapeau `--mode-test`. Il n'enleve AUCUN garde-fou : il en ajoute un
(`ExpediteurTest` : aucun e-mail n'est jamais adresse au vrai destinataire) et autorise
des strategies d'isolation de lecture plus faibles, toujours annoncees (en tete du
resume, dans `resume.txt`, au journal : `mode_test_actif`, `isolation_degradee`).
Strategies de lecture des .eml : `fork` (Linux, macOS : memoire + CPU + delai),
`spawn` (Windows : delai seulement, SANS limite memoire), `directe` (aucune isolation,
`--sans-isolation`, mode test uniquement). Hors mode test, une plateforme sans
`fork` + `resource` est refusee.

Regles issues de l'audit de conformite (docs/conformite_mvp.md, section 4) :

- A7 / CA-03 : `rattacher_piece` refuse un dossier different de celui de la decision de
  routage, sauf `forcer=True` avec un `motif` (journal `rattachement_force`) ; le refus est
  journalise (`rattachement_refuse`).
- A1 / D6 : hors mode test, un dossier sans circuit explicite est refuse a la lecture ; en
  mode test, C-DIRECT par defaut avec un avertissement (resume, `resume.txt`, journal
  `circuit_par_defaut`).
- A14 : `arbitrer`, `trancher` et `reparer-audit` exigent un nom de `responsables.txt` (absent :
  refus, sauf repli annonce et journalise sur `validateurs.txt` en mode test). Un responsable est
  aussi un validateur.
- A15 : `alias_fournisseurs.csv` (optionnel, lu par `parseurs.lire_alias`) est passe au moteur.
- A17 : `controler_envois` (lecture seule) verifie, pour chaque envoi du journal, la validation
  anterieure du meme brouillon par un nom declare, avec le meme destinataire.
- A10 : `piece_creee` journalise `regle` et `motif` du rapprochement.
- A8 : la file humaine conserve expediteur, date et objet du message en JSON dans `detail`
  (`contexte_file` le relit, tolerante aux anciens textes simples).
- Envoi nominatif : `envoyer_relance(par=...)` journalise `envoye_par` sur les entrees de la
  tentative d'envoi (journal enveloppe : `envoi.py` n'est pas modifie).
- Mode test : `creneau_envoi_libre` et `limite_hebdomadaire` de `config.json` (sans effet hors
  mode test) ; les garde-fous coupes sont journalises (`garde_fous_coupes`) et affiches.

Quatrieme passage de revue :

- MODE FIGE PAR INSTANCE. Le mode (test ou reel) est une propriete de l'instance, enregistree
  par son premier cycle (journal `mode_instance` + marqueur `vus`, que rien ne reecrit). Toute
  operation d'ecriture compare le mode EFFECTIF demande (`config.json` ou `mode_test`) a lui et
  leve `ModeInstanceIncompatible` AVANT toute ecriture s'ils different : une instance de test ne
  devient jamais reelle, et inversement (`cycle --mode-test` ne peut plus preparer des brouillons
  tolerants pour les expedier ensuite au vrai client). Consequence voulue : dans une instance de
  test tout est simule, y compris l'historique des envois, le passage des pieces a DEMANDEE et la
  limite hebdomadaire. `mode_instance` (lecture seule) l'expose. Une configuration illisible
  refuse aussi : sans elle le mode effectif est inconnu.
- Un fichier (`id_piece`) n'appartient qu'a un dossier, `forcer` compris.
- `controler_envois` recoupe le journal avec la base et l'outbox.
- Noms de validateurs et de responsables compares en NFC (espaces de bord retires).

Le cycle ne valide JAMAIS rien lui-meme.
"""

from __future__ import annotations

import atexit
import csv
import email.utils
import dataclasses
import datetime as dt
import io
import json
import math
import multiprocessing
import os
import re
import shutil
import signal
import struct
import sys
import tempfile
import unicodedata
import zoneinfo
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:  # pragma: no cover - depend de la plateforme
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]

from . import envoi as envoi_mod
from . import etats, routage
from .audit import JournalAudit, JournalCorrompu, ResultatVerification
from .cadence import FENETRE_HEBDO_JOURS, Planification, planifier
from .depot import Depot
from .exclusions import Referentiel
from .modeles import (
    ETATS_TERMINAUX,
    Brouillon,
    DecisionRoutage,
    Dossier,
    EtatPeriode,
    EtatPiece,
    Evenement,
    OperationBancaire,
    Piece,
    PieceAttendue,
    Rapprochement,
    Statut,
    StatutBrouillon,
    StatutRoutage,
    MessageEntrant,
    identifiant_relance,
)
from .moteur import Moteur
from .normalisation import normaliser
from .parseurs import ErreurFormat, lire_alias, lire_dossiers, lire_pieces, lire_releve
from .rapport import ecrire_suivi, ligne_sure
from .relances import construire_brouillon

# Gardes AVANT toute analyse d'un .eml : l'analyseur d'en-tetes de la bibliotheque
# standard est quadratique sur les mots encodes RFC 2047 (mesure : 112 Ko d'en-tete
# From -> ~1 a 3 s et ~0,9 Go ; 224 Ko -> 17 s et 3,6 Go). Au-dela, le fichier part
# en quarantaine sans jamais etre analyse.
TAILLE_MAX_EN_TETES_EML = 64 * 1024          # bloc d'en-tetes de plus haut niveau, en octets
TAILLE_MAX_EML = 30 * 1024 * 1024            # fichier .eml complet, en octets

# Ces deux plafonds ne sont qu'un premier filtre bon marche. La vraie protection :
# chaque .eml est analyse dans un PROCESSUS ENFANT a ressources bornees ; le parent
# n'analyse jamais un fichier lui-meme (l'analyseur peut s'emballer ailleurs, par
# exemple sur les en-tetes des parties imbriquees).
LIMITE_MEMOIRE_LECTURE_EML = 1024 ** 3       # octets d'espace d'adressage EN PLUS de celui herite
LIMITE_CPU_LECTURE_EML = 10.0                # secondes de CPU de l'enfant
MARGE_GARDE_LECTURE_EML = 3.0                # secondes reelles ajoutees au CPU avant de tuer

# Strategies de lecture des .eml (voir `choisir_strategie_isolation`).
STRATEGIES_ISOLATION = ("fork", "spawn", "directe")
# Le demarrage d'un interpreteur `spawn` (imports compris) est lent, surtout sous Windows
# avec un antivirus : il ne doit pas etre pris pour un blocage de l'analyse.
MARGE_DEMARRAGE_SPAWN = 5.0
AVERTISSEMENT_SPAWN = "isolation partielle : temps seulement, pas de limite memoire"
AVERTISSEMENT_DIRECTE = ("aucune isolation : les e-mails sont lus dans le processus principal "
                         "(--sans-isolation, mode test uniquement)")

# Mode test. `.invalid` est un TLD reserve (RFC 2606) : jamais une vraie adresse.
NOM_CONFIG = "config.json"
ADRESSE_TEST_DEFAUT = "test@exemple.invalid"
PREFIXE_TEST = "[TEST] "
_ADRESSE_TEST = re.compile(r"[^\s@,;<>()\[\]\\\"]+@[^\s@,;<>()\[\]\\\"]+")

ACTEUR_SYSTEME = "systeme"
FUSEAU = "Europe/Paris"
PREFIXE_MOTIF_CYCLE = "[cycle] "
BROUILLON_EN_ATTENTE = "BROUILLON_EN_ATTENTE"
STATUTS_EN_ATTENTE = (StatutBrouillon.BROUILLON, StatutBrouillon.VALIDEE, StatutBrouillon.EN_COURS)
ETATS_RECLAMABLES = (EtatPiece.ATTENDUE, EtatPiece.DEMANDEE)
CLE_ENVOI_APPLIQUE = "envoi-applique:"

Panne = Callable[[str], None]


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------


class ErreurCycle(Exception):
    """Refus explicite de l'orchestration (rien n'a ete modifie)."""


class CollisionReferences(ErreurCycle):
    """Une meme reference d'operation designe plusieurs operations."""

    def __init__(self, collisions: Mapping[str, Sequence[str]]) -> None:
        self.collisions = {ref: tuple(codes) for ref, codes in sorted(collisions.items())}
        lignes = [f"  {ref} : {', '.join(codes)}" for ref, codes in self.collisions.items()]
        super().__init__(
            f"{len(self.collisions)} reference(s) d'operation partagee(s) entre operations "
            "(dossiers listes ; corriger les references a la source, le cycle ne tranche pas) :\n"
            + "\n".join(lignes)
        )


class ConfigInvalide(ErreurCycle):
    """`config.json` illisible ou de type incorrect : refus avant toute ecriture."""


class ModeInstanceIncompatible(ErreurCycle):
    """Le mode demande (test ou reel) n'est pas celui dans lequel l'instance a ete creee."""


class BaseRecreee(ModeInstanceIncompatible):
    """Le journal d'audit contient des decisions ou des envois que la base ignore (base supprimee,
    recreee ou restauree d'une ancienne sauvegarde) : aucune ecriture n'est permise."""


class JournalNonSain(ErreurCycle):
    """Le journal d'audit ne verifie pas : aucune ecriture n'est faite tant qu'il ne l'est pas."""


class ValidateurRefuse(ErreurCycle):
    """Le nom fourni n'est pas un validateur declare de l'instance."""


class EnvoiRefuse(envoi_mod.EnvoiNonValide):
    """Garde-fou d'assemblage : l'envoi violerait une regle que `envoi` ne connait pas."""


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Instance:
    racine: Path

    @classmethod
    def de(cls, valeur: "Instance | Path | str") -> "Instance":
        return valeur if isinstance(valeur, Instance) else cls(Path(valeur))

    @property
    def depot(self) -> Path:
        return self.racine / "depot.sqlite"

    @property
    def audit(self) -> Path:
        return self.racine / "audit.jsonl"

    @property
    def validateurs(self) -> Path:
        return self.racine / "validateurs.txt"

    @property
    def responsables(self) -> Path:
        return self.racine / "responsables.txt"

    @property
    def config(self) -> Path:
        return self.racine / NOM_CONFIG

    @property
    def entrant(self) -> Path:
        return self.racine / "entrant"

    @property
    def outbox(self) -> Path:
        return self.racine / "outbox"

    @property
    def sortie(self) -> Path:
        return self.racine / "sortie"

    def preparer(self) -> None:
        for chemin in (self.racine, self.entrant, self.outbox, self.sortie):
            chemin.mkdir(parents=True, exist_ok=True)


def charger_config(instance: Instance | Path | str) -> dict[str, Any]:
    """Configuration de l'instance (`<instance>/config.json`), valeurs par defaut comprises.

    Cles connues : `mode_test` (booleen STRICT, defaut False : "false" ou 0 sont refuses
    plutot que lus comme vrais), `adresse_test` (une adresse simple, defaut
    `test@exemple.invalid`), et deux garde-fous d'envoi qui ne jouent qu'EN mode test,
    `creneau_envoi_libre` et `limite_hebdomadaire` (booleens stricts ; leurs defauts, vrai
    et vrai, ne sont pas ajoutes au dictionnaire : voir `reglages_envoi`). Les autres cles sont conservees telles quelles (d'autres
    composants peuvent y ranger les leurs). Fichier absent : mode normal. JSON invalide,
    non UTF-8, ou valeur de mauvais type : `ConfigInvalide` (message lisible, sans trace).
    Lecture seule : ne cree jamais rien. Un BOM UTF-8 (Bloc-notes) est accepte.
    """
    config: dict[str, Any] = {"mode_test": False, "adresse_test": ADRESSE_TEST_DEFAUT}
    chemin = Instance.de(instance).config
    if not chemin.exists():
        return config
    try:
        contenu = json.loads(chemin.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigInvalide(
            f"{NOM_CONFIG} invalide (JSON, ligne {exc.lineno} colonne {exc.colno} : "
            f"{affichable(exc.msg)}) : corriger ou supprimer {chemin}"
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigInvalide(
            f"{NOM_CONFIG} illisible ({type(exc).__name__}) : l'enregistrer en UTF-8 ({chemin})"
        ) from None
    if not isinstance(contenu, dict):
        raise ConfigInvalide(f"{NOM_CONFIG} doit contenir un objet JSON {{...}} ({chemin})")
    config.update(contenu)
    if not isinstance(config["mode_test"], bool):
        raise ConfigInvalide(
            f"{NOM_CONFIG} : `mode_test` doit etre true ou false (booleen JSON), "
            f"pas {affichable(repr(config['mode_test']))}"
        )
    for cle in _GARDE_FOUS_REGLABLES:
        if cle in config and not isinstance(config[cle], bool):
            raise ConfigInvalide(
                f"{NOM_CONFIG} : `{cle}` doit etre true ou false (booleen JSON), "
                f"pas {affichable(repr(config[cle]))}"
            )
    adresse = config["adresse_test"]
    if not isinstance(adresse, str) or not _ADRESSE_TEST.fullmatch(adresse):
        raise ConfigInvalide(
            f"{NOM_CONFIG} : `adresse_test` doit etre une seule adresse simple "
            f"(exemple : {ADRESSE_TEST_DEFAUT}), pas {affichable(repr(adresse))}"
        )
    return config


_GARDE_FOUS_REGLABLES = ("creneau_envoi_libre", "limite_hebdomadaire")


def reglages_envoi(config: Mapping[str, Any], en_mode_test: bool) -> tuple[bool, bool]:
    """(creneau_libre, limite_hebdomadaire) effectifs. HORS mode test : (False, True), quoi
    que dise `config.json`. En mode test : `creneau_envoi_libre` (defaut VRAI : l'application
    web ne peut pas simuler l'heure) et `limite_hebdomadaire` (defaut VRAI) du fichier."""
    if not en_mode_test:
        return False, True
    return bool(config.get("creneau_envoi_libre", True)), bool(config.get("limite_hebdomadaire", True))


def garde_fous_mode_test(instance: Instance | Path | str, mode_test: bool = False) -> tuple[str, ...]:
    """Garde-fous d'ENVOI coupes par le mode test (pour le bandeau de l'application) ; () hors
    mode test. Memes regles que `envoyer_relance`."""
    config = charger_config(instance)
    en_mode_test = bool(mode_test) or config["mode_test"]
    libre, limite = reglages_envoi(config, en_mode_test)
    coupes = []
    if libre:
        coupes.append("creneau d'envoi libre (heures et jours ouvres ignores)")
    if not limite:
        coupes.append("limite hebdomadaire ignoree (plusieurs e-mails par semaine au meme destinataire)")
    return tuple(coupes)


# ---------------------------------------------------------------------------
# Fuseaux horaires : sous Windows, `zoneinfo` n'a PAS de base de fuseaux (elle vient du
# systeme sous Linux/macOS, ou du paquet PyPI `tzdata` que ce projet n'exige pas).
# `cadence` et `envoi` appellent `ZoneInfo("Europe/Paris")` directement : sans base,
# tout envoi leve ZoneInfoNotFoundError. On installe donc, SEULEMENT si la base manque,
# un fichier TZif minimal (regle de l'Union europeenne, valable depuis 1996) dans un
# repertoire prive ajoute au chemin de `zoneinfo` : les trois modules en profitent.
# ---------------------------------------------------------------------------

_FUSEAUX_VERIFIES = False
_REGLE_EUROPE_PARIS = b"CET-1CEST,M3.5.0,M10.5.0/3"


def _tzif_europe_paris() -> bytes:
    """Fichier TZif v2 sans transition : tout est decrit par la regle POSIX finale."""
    abreviation = b"CET\0"
    type_local = struct.pack(">lBB", 3600, 0, 0)
    bloc = (b"TZif2" + b"\0" * 15 + struct.pack(">6l", 0, 0, 0, 0, 1, len(abreviation))
            + type_local + abreviation)
    return bloc + bloc + b"\n" + _REGLE_EUROPE_PARIS + b"\n"


def garantir_fuseaux() -> str:
    """Rend `ZoneInfo("Europe/Paris")` utilisable. Renvoie "systeme" si la base existe,
    "secours" si le fichier de secours a ete installe. Idempotent et bon marche."""
    global _FUSEAUX_VERIFIES
    if _FUSEAUX_VERIFIES:
        return "systeme"
    try:
        zoneinfo.ZoneInfo(FUSEAU)
    except zoneinfo.ZoneInfoNotFoundError:
        dossier = Path(tempfile.mkdtemp(prefix="rapprochement-fuseaux-"))
        atexit.register(shutil.rmtree, dossier, ignore_errors=True)
        cible = dossier.joinpath(*FUSEAU.split("/"))
        cible.parent.mkdir(parents=True)
        cible.write_bytes(_tzif_europe_paris())
        zoneinfo.reset_tzpath(to=[*zoneinfo.TZPATH, str(dossier)])
        zoneinfo.ZoneInfo.clear_cache()
        zoneinfo.ZoneInfo(FUSEAU)   # doit marcher maintenant, sinon l'erreur remonte
        _FUSEAUX_VERIFIES = True
        return "secours"
    _FUSEAUX_VERIFIES = True
    return "systeme"


def exiger_journal_sain(instance: Instance | Path | str) -> JournalAudit:
    """Verifie le journal d'audit AVANT toute ecriture ; leve `JournalNonSain` sinon.

    Sans cette garde, une derniere ligne tronquee (coupure de courant) laisserait le
    cycle suivant valider ses effets en base puis echouer en ecrivant le journal :
    des effets sans trace d'audit, a chaque passage.
    """
    inst = Instance.de(instance)
    journal = JournalAudit(inst.audit)
    r = journal.verifier()
    if not r.ok:
        raise JournalNonSain(
            f"journal d'audit non sain ({affichable(r.raison)}) : aucune ecriture effectuee. "
            "Si seule la derniere ligne est tronquee (coupure pendant une ecriture), lancer "
            "`reparer-audit --par NOM`. Sinon NE PAS modifier le fichier : en garder une copie "
            "et alerter le responsable (alteration possible)."
        )
    return journal


CLE_MODE_INSTANCE = "mode-instance:"
MODES_INSTANCE = ("test", "reel")


def _mode_enregistre(depot: Depot) -> str | None:
    """Mode (« test » ou « reel ») fige par le premier cycle, ou None. Deux marqueurs a la
    fois (base alteree) : refus, jamais de choix arbitraire."""
    presents = [m for m in MODES_INSTANCE if depot.est_vu(CLE_MODE_INSTANCE + m)]
    if len(presents) > 1:
        raise ModeInstanceIncompatible(
            "le mode de l'instance est incoherent (« test » et « reel » sont tous deux enregistres) : "
            "base alteree, restaurer une sauvegarde ou creer une nouvelle instance"
        )
    return presents[0] if presents else None


_MARQUE_MODE_JOURNAL = b'"action":"mode_instance"'


def _modes_du_journal(inst: Instance) -> list[str]:
    """Modes inscrits au journal d'audit par les entrees `mode_instance`, dans l'ordre. Une seule
    passe sur les octets (la forme canonique des lignes rend la recherche fiable : un guillemet
    dans une valeur est echappe). Journal absent : []. Une ligne illisible est ignoree ici ; la
    chaine de hachage est l'affaire de `verifier_audit`."""
    if not inst.audit.exists():
        return []
    modes = []
    for ligne in inst.audit.read_bytes().split(b"\n"):
        if _MARQUE_MODE_JOURNAL not in ligne:
            continue
        try:
            entree = json.loads(ligne)
        except ValueError:
            continue
        if isinstance(entree, dict) and entree.get("action") == "mode_instance":
            details = entree.get("details")
            mode = details.get("mode") if isinstance(details, dict) else None
            if mode in MODES_INSTANCE:
                modes.append(mode)
    return modes


def _mode_etabli(inst: Instance, depot: Depot | None) -> str | None:
    """Mode de l'instance, recoupe BASE et JOURNAL : la base seule tient en une ligne SQL, qu'on peut
    supprimer (ou toute la base recreee, restauree d'une vieille sauvegarde) pour refiger l'instance
    dans l'autre mode. Le journal d'audit, chaine, garde l'entree `mode_instance`. Base et journal
    qui se contredisent, ou un journal qui porte les deux modes : `ModeInstanceIncompatible`.
    Un seul des deux connait le mode : c'est lui qui fait foi."""
    base = _mode_enregistre(depot) if depot is not None else None
    dans_le_journal = set(_modes_du_journal(inst))
    if len(dans_le_journal) > 1:
        raise ModeInstanceIncompatible(
            "le journal d'audit enregistre a la fois le mode test et le mode reel pour cette instance : "
            "journal contradictoire, instance a refuser (creer une nouvelle instance)"
        )
    journal = next(iter(dans_le_journal), None)
    if base is not None and journal is not None and base != journal:
        raise ModeInstanceIncompatible(
            f"le mode de l'instance est contradictoire : la base dit {base}, le journal d'audit dit "
            f"{journal} (base recreee ou restauree, marqueur modifie, journal remplace) : "
            "instance a refuser, creer une nouvelle instance"
        )
    return base or journal


def mode_instance(instance: Instance | Path | str) -> str | None:
    """Mode de l'instance, « test » ou « reel », tel que le premier cycle l'a FIGE ; None si
    aucun cycle n'a encore eu lieu. Lecture seule (ne cree rien) : pour le bandeau de
    l'application web et pour savoir quel mode demander.

    Le mode est une propriete de l'INSTANCE, pas un drapeau de commande : enregistre au premier
    cycle (entree d'audit `mode_instance` + marqueur `vus` du depot, que rien ne reecrit), puis
    toute operation d'ecriture compare le mode demande (`config.json` ou `mode_test`) a lui et
    refuse (`ModeInstanceIncompatible`) AVANT toute ecriture s'ils different. Une instance de
    test ne devient donc JAMAIS reelle, et inversement. Consequence voulue : dans une instance
    de test tout est simule, y compris l'historique des envois, le passage des pieces a
    DEMANDEE et la limite hebdomadaire. La base et le journal sont recoupes : s'ils se
    contredisent (`ModeInstanceIncompatible`), aucune operation n'est possible ; voir
    `controler_mode`."""
    inst = Instance.de(instance)
    if not inst.depot.exists():
        return _mode_etabli(inst, None)
    with Depot(inst.depot) as depot:
        return _mode_etabli(inst, depot)


def _marqueurs_de_mode(inst: Instance) -> list[str]:
    """Marqueurs de mode presents dans la base (vide si la base n'existe pas)."""
    if not inst.depot.exists():
        return []
    with Depot(inst.depot) as depot:
        return [m for m in MODES_INSTANCE if depot.est_vu(CLE_MODE_INSTANCE + m)]


_ACTIONS_DE_BROUILLON = ("brouillon_cree", "valide", "rejete", "envoi_effectue", "envoi_confirme_par_humain",
                         "envoi_repercute", "envoi_refuse", "envoi_mode_test")
_MARQUES_DE_BROUILLON = tuple(f'"action":"{a}"'.encode() for a in _ACTIONS_DE_BROUILLON)


def _brouillons_du_journal(inst: Instance) -> set[str]:
    """Identifiants de brouillons dont le journal d'audit porte une decision ou un envoi
    (creation, validation, rejet, envoi, repercussion). Une passe sur les octets, comme
    `_modes_du_journal` (ligne illisible ignoree : la chaine est l'affaire de `verifier_audit`)."""
    if not inst.audit.exists():
        return set()
    ids: set[str] = set()
    for ligne in inst.audit.read_bytes().split(b"\n"):
        if not any(m in ligne for m in _MARQUES_DE_BROUILLON):
            continue
        try:
            entree = json.loads(ligne)
        except ValueError:
            continue
        if isinstance(entree, dict) and entree.get("action") in _ACTIONS_DE_BROUILLON and entree.get("objet"):
            ids.add(str(entree["objet"]))
    return ids


def _brouillons_en_base(inst: Instance) -> set[str]:
    if not inst.depot.exists():
        return set()
    with Depot(inst.depot) as depot:
        return {b.id_relance for b in depot.lister_brouillons()}


def _decisions_ignorees_de_la_base(inst: Instance, connus: set[str]) -> list[str]:
    """Brouillons dont le journal garde une decision ou un envoi et que la base ignore."""
    return sorted(_brouillons_du_journal(inst) - connus)


def _message_base_recreee(manquants: list[str]) -> str:
    exemples = ", ".join(affichable(m) for m in manquants[:3]) + (" ..." if len(manquants) > 3 else "")
    return (f"base recreee ou restauree : le journal d'audit contient des decisions ou des envois pour "
            f"{len(manquants)} brouillon(s) que la base ignore ({exemples}). Un nouveau cycle perdrait "
            "l'historique des envois et pourrait ecrire une seconde fois au meme destinataire : operation "
            "refusee. Restaurer la base d'origine (sauvegarde), ou creer une nouvelle instance")


def controler_mode(instance: Instance | Path | str) -> tuple[str, ...]:
    """Incoherences du mode fige (lecture seule), pour `controler-envois` et `verifier-audit` :
    deux marqueurs dans la base, journal portant les deux modes, base et journal en desaccord,
    marqueur de la base absent alors que le journal le connait (base recreee), ou entree
    `mode_instance` absente du journal alors que la base le connait. () si tout concorde ou si
    aucun cycle n'a eu lieu."""
    inst = Instance.de(instance)
    problemes: list[str] = []
    marqueurs = _marqueurs_de_mode(inst)
    base = marqueurs[0] if len(marqueurs) == 1 else None
    if len(marqueurs) > 1:
        problemes.append("la base porte a la fois le marqueur test et le marqueur reel")
    dans_le_journal = _modes_du_journal(inst)
    distincts = sorted(set(dans_le_journal))
    journal = distincts[0] if len(distincts) == 1 else None
    if len(distincts) > 1:
        problemes.append("le journal enregistre a la fois le mode test et le mode reel")
    if base and journal and base != journal:
        problemes.append(f"la base dit {base} et le journal dit {journal} (base recreee ou restauree ?)")
    elif base and not dans_le_journal:
        problemes.append(f"la base dit {base} mais le journal ne contient aucune entree mode_instance")
    elif journal and not marqueurs:
        problemes.append(f"le journal dit {journal} mais le marqueur du depot est absent (base recreee ?)")
    manquants = _decisions_ignorees_de_la_base(inst, _brouillons_en_base(inst))
    if manquants:
        problemes.append(_message_base_recreee(manquants))
    return tuple(problemes)


def mode_demande(instance: Instance | Path | str, mode_test: bool = False) -> str:
    """Mode EFFECTIF d'une operation : « test » si `mode_test` est vrai ou si `config.json`
    le dit, sinon « reel »."""
    return "test" if bool(mode_test) or charger_config(instance)["mode_test"] else "reel"


def _refus_mode(enregistre: str, demande: str) -> ModeInstanceIncompatible:
    return ModeInstanceIncompatible(
        f"cette instance a ete creee en mode {enregistre} (operation demandee en mode {demande}) ; "
        "pour changer, creer une nouvelle instance"
    )


def _exiger_mode(inst: Instance, depot: Depot, demande: str) -> str | None:
    enregistre = _mode_etabli(inst, depot)
    if enregistre is not None and enregistre != demande:
        raise _refus_mode(enregistre, demande)
    return enregistre


def _exiger_mode_instance(inst: Instance, mode_test: bool) -> None:
    """Meme verification que `_ouvrir`, faite AVANT un controle de role : quand le mode ET le role
    sont faux, le message du mode (« cette instance a ete creee en mode ... ») est le plus utile."""
    enregistre, demande = mode_instance(inst), mode_demande(inst, mode_test)
    if enregistre is not None and enregistre != demande:
        raise _refus_mode(enregistre, demande)


def _figer_mode(inst: Instance, depot: Depot, journal: JournalAudit, demande: str, source: str,
                aujourdhui: dt.date, maintenant: dt.datetime) -> None:
    """Premier cycle : enregistre le mode dans la base ET au journal (transaction d'abord, journal
    ensuite). Base sans marqueur mais journal qui connait deja ce mode (base recreee) : le marqueur
    est retabli et la reprise est journalisee (`mode_marqueur_retabli`). `_ouvrir` a deja verifie
    que `demande` est le mode de l'instance, base et journal recoupes."""
    base = _mode_enregistre(depot)
    dans_le_journal = _modes_du_journal(inst)
    if base is not None:
        return                                        # deja fige ; un journal sans entree est signale par `controler_mode`
    existantes = len(depot.charger_pieces())
    with depot.transaction():
        depot.marquer_vu(CLE_MODE_INSTANCE + demande)
    if dans_le_journal:
        journal.ecrire(ACTEUR_SYSTEME, "mode_marqueur_retabli", aujourdhui.isoformat(), {
            "mode": demande, "pieces_deja_presentes": existantes,
        }, horodatage=maintenant)
        return
    journal.ecrire(ACTEUR_SYSTEME, "mode_instance", aujourdhui.isoformat(), {
        "mode": demande, "source": source, "pieces_deja_presentes": existantes,
    }, horodatage=maintenant)


@contextmanager
def _ouvrir(instance: Instance, *, ecriture: bool = False,
            mode: str | None = None) -> Iterator[tuple[Depot, JournalAudit]]:
    """`ecriture=True` : refuse (`JournalNonSain`) avant d'ouvrir la base si le journal
    d'audit ne verifie pas. Les lectures (liste, export) restent possibles.

    `mode` (« test » ou « reel », voir `mode_demande`) : refuse (`ModeInstanceIncompatible`)
    si l'instance a ete creee dans l'autre mode, AVANT que l'appelant n'ecrive quoi que ce soit."""
    garantir_fuseaux()
    instance.preparer()
    journal = exiger_journal_sain(instance) if ecriture else JournalAudit(instance.audit)
    depot = Depot(instance.depot)
    try:
        if mode is not None:
            _exiger_mode(instance, depot, mode)
        if ecriture:                                  # base recreee : l'historique des envois est perdu
            manquants = _decisions_ignorees_de_la_base(instance, {b.id_relance for b in depot.lister_brouillons()})
            if manquants:
                raise BaseRecreee(_message_base_recreee(manquants))
        yield depot, journal
    finally:
        depot.fermer()


_CARACTERES_INVISIBLES = ("Cc", "Cf", "Zl", "Zp")


def affichable(valeur: object) -> str:
    """Texte sans caractere de controle ni de mise en forme invisible (ESC, CR, LF,
    U+202E...) : chacun devient une sequence visible `\\xNN` / `\\uNNNN`. Pour tout
    affichage d'une valeur venue de l'exterieur (Message-ID, nom de fichier, libelle,
    motif) : elle ne pilote jamais le terminal de l'operateur."""
    texte = str(valeur)
    sortie = []
    for c in texte:
        if unicodedata.category(c) in _CARACTERES_INVISIBLES:
            sortie.append(f"\\x{ord(c):02x}" if ord(c) < 0x100 else f"\\u{ord(c):04x}")
        else:
            sortie.append(c)
    return "".join(sortie)


def normaliser_nom(nom: str) -> str:
    """Forme canonique d'un nom de personne : NFC (macOS ecrit en NFD, Windows en NFC) et espaces
    de bord retires. Les espaces INTERNES ne sont pas touches : « Marie  Durand » (deux espaces) ou
    « Marie\u00a0Durand » (espace insecable) ne sont pas « Marie Durand » (nom a l'apparence trompeuse).
    Ne change PAS la casse : voir `_cle_nom` pour la comparaison."""
    return unicodedata.normalize("NFC", nom).strip()


def _cle_nom(nom: str) -> str:
    return normaliser_nom(nom).casefold()


def _lire_liste_de_noms(chemin: Path) -> tuple[str, ...]:
    """Lignes non vides, `#` = commentaire ; BOM et CRLF toleres. Fichier absent : ()."""
    if not chemin.exists():
        return ()
    try:
        # utf-8-sig : un BOM (Bloc-notes Windows) ne doit pas coller au premier nom.
        contenu = chemin.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidateurRefuse(
            f"{chemin.name} illisible ({type(exc).__name__}) : enregistrer le fichier en UTF-8"
        ) from None
    noms = []
    for ligne in contenu.splitlines():
        nom = normaliser_nom(ligne)
        if nom and not nom.startswith("#"):
            noms.append(nom)
    return tuple(noms)


def lire_validateurs(instance: Instance | Path | str) -> tuple[str, ...]:
    """Noms declares dans `validateurs.txt` (lignes non vides, `#` = commentaire). Absent : ()."""
    return _lire_liste_de_noms(Instance.de(instance).validateurs)


def lire_responsables(instance: Instance | Path | str) -> tuple[str, ...]:
    """Noms declares dans `responsables.txt` (meme format que `validateurs.txt`). Absent : ()."""
    return _lire_liste_de_noms(Instance.de(instance).responsables)


def _chercher_nom(declares: Sequence[str], nom: str | None, *, source: str) -> str:
    if not isinstance(nom, str) or not nom.strip():
        raise ValidateurRefuse("--par NOM est obligatoire")
    cherche = _cle_nom(nom)
    for declare in declares:
        if _cle_nom(declare) == cherche:
            if not etats.est_acteur_humain(declare):
                raise ValidateurRefuse(f"{declare!r} est un acteur automatique, pas un validateur")
            return declare
    raise ValidateurRefuse(f"{normaliser_nom(nom)!r} n'est pas dans {source}")


def exiger_validateur(instance: Instance | Path | str, nom: str | None) -> str:
    """Renvoie le nom tel que declare, ou leve `ValidateurRefuse`. Jamais d'acceptation par defaut.

    Un validateur est un nom de `validateurs.txt` OU de `responsables.txt` : un responsable
    peut faire tout ce que fait un validateur (cahier des charges, section 4)."""
    inst = Instance.de(instance)
    declares = (*lire_validateurs(inst), *lire_responsables(inst))
    if not declares:
        raise ValidateurRefuse(
            f"aucun validateur declare ({inst.validateurs} absent ou vide) : "
            "toute decision humaine est refusee tant que la liste n'est pas renseignee"
        )
    return _chercher_nom(declares, nom, source=f"{inst.validateurs.name} ni {inst.responsables.name}")


def _nom_responsable(inst: Instance, nom: str | None, mode_test: bool) -> tuple[str, bool]:
    """(nom declare, repli) pour une operation reservee aux responsables (A14).

    `responsables.txt` PRESENT : seuls ses noms sont acceptes (vide : refus, jamais d'acceptation
    par defaut). ABSENT : refus, SAUF en mode test ou l'on se rabat sur les validateurs
    (`repli=True`, a annoncer et a journaliser par l'appelant)."""
    en_test = bool(mode_test) or charger_config(inst)["mode_test"]
    if not inst.responsables.exists():
        if not en_test:
            raise ValidateurRefuse(
                f"arbitrer, trancher un envoi et reparer le journal sont reserves aux responsables : "
                f"{inst.responsables} est absent. Le creer (un nom par ligne), ou utiliser "
                "--mode-test pour un essai local (repli sur validateurs.txt)"
            )
        return exiger_validateur(inst, nom), True
    responsables = lire_responsables(inst)
    if not responsables:
        raise ValidateurRefuse(f"aucun responsable declare ({inst.responsables} vide) : operation refusee")
    return _chercher_nom(responsables, nom, source=f"{inst.responsables.name} (operation reservee aux responsables)"), False


def exiger_responsable(instance: Instance | Path | str, nom: str | None, *, mode_test: bool = False) -> str:
    """Nom d'un responsable declare dans `responsables.txt` ; voir `_nom_responsable`."""
    return _nom_responsable(Instance.de(instance), nom, mode_test)[0]


def avertissement_roles(instance: Instance | Path | str, mode_test: bool = False) -> str | None:
    """Avertissement a afficher quand `responsables.txt` est absent EN MODE TEST (les
    operations de responsable sont alors ouvertes aux validateurs), sinon None."""
    inst = Instance.de(instance)
    if inst.responsables.exists() or not (bool(mode_test) or charger_config(inst)["mode_test"]):
        return None
    return (f"{inst.responsables.name} absent : en mode test, arbitrer, trancher un envoi et "
            "reparer le journal sont ouverts aux validateurs (en production : refus)")


def _noter_repli_roles(journal: JournalAudit, nom: str, operation: str, maintenant: dt.datetime) -> None:
    journal.ecrire(nom, "repli_roles_mode_test", operation, {
        "avertissement": "responsables.txt absent : validateurs.txt utilise (mode test)",
    }, horodatage=maintenant)


# ---------------------------------------------------------------------------
# Entrees
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entrees:
    operations: list[OperationBancaire]
    pieces: list[Piece]
    dossiers: dict[str, Dossier]
    referentiel: Referentiel
    alias: dict[str, str] = field(default_factory=dict)


LONGUEUR_MIN_ALIAS = 3


def _alias_surs(chemin: Path) -> dict[str, str]:
    """`parseurs.lire_alias` + une garde de securite que ce lecteur n'a pas : un alias trop
    court, ou vide une fois normalise (« -- -- »), correspondrait a presque TOUS les libelles du
    fournisseur et ferait declarer justifiee une operation qui ne l'est pas, c'est-a-dire une
    piece manquante ratee (CA-02). Refus lisible plutot que correction silencieuse."""
    alias = lire_alias(chemin)
    for nom in alias:
        if len(normaliser(nom)) < LONGUEUR_MIN_ALIAS:
            raise ErreurFormat(
                f"{chemin.name} : alias {nom!r} trop court "
                f"(au moins {LONGUEUR_MIN_ALIAS} caracteres utiles) : il correspondrait a presque tout"
            )
    return alias


def dossiers_sans_circuit(chemin: Path | str) -> list[str]:
    """Codes des dossiers de `dossiers.csv` dont la colonne `circuit` est absente ou vide
    (ils recoivent C-DIRECT par defaut hors circuit obligatoire). Fichier illisible : []."""
    try:
        with open(chemin, newline="", encoding="utf-8-sig") as f:
            lecteur = csv.DictReader(f)
            return sorted(
                unicodedata.normalize("NFC", (ligne.get("dossier") or "").strip())
                for ligne in lecteur if not (ligne.get("circuit") or "").strip()
            )
    except (OSError, UnicodeDecodeError, csv.Error):
        return []


def _lire(lecteur: Callable[[Path], Any], chemin: Path) -> Any:
    """Toute erreur de lecture d'une entree devient `parseurs.ErreurFormat` (nom du fichier
    en tete) : le CLI la presente comme un refus lisible, sans trace Python."""
    try:
        return lecteur(chemin)
    except ErreurFormat as exc:
        if str(exc).startswith(chemin.name):
            raise
        raise ErreurFormat(f"{chemin.name} : {exc}") from None
    except FileNotFoundError:
        raise ErreurFormat(f"{chemin.name} : fichier d'entree introuvable ({chemin.parent})") from None
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise ErreurFormat(f"{chemin.name} : {type(exc).__name__} : {exc}") from None


def lire_entrees(entree: Path | str, *, circuit_obligatoire: bool = False) -> Entrees:
    """Memes fichiers que `scripts/lancer.py` ; `exclusions_dossier.csv` et
    `alias_fournisseurs.csv` optionnels.

    `circuit_obligatoire=True` (decision D6, defaut du cycle HORS mode test) : un dossier
    sans circuit explicite est refuse au lieu de recevoir C-DIRECT en silence.

    Leve `parseurs.ErreurFormat` pour toute entree illisible ou malformee, AVANT que
    le cycle n'ouvre la base ou le journal."""
    racine = Path(entree)
    return Entrees(
        operations=_lire(lire_releve, racine / "releve_bancaire.csv"),
        pieces=_lire(lire_pieces, racine / "pieces_recues.csv"),
        dossiers=_lire(lambda c: lire_dossiers(c, circuit_obligatoire=circuit_obligatoire),
                       racine / "dossiers.csv"),
        referentiel=_lire(Referentiel.depuis_csv, racine / "exclusions_dossier.csv"),
        alias=_lire(_alias_surs, racine / "alias_fournisseurs.csv"),
    )


def collisions_de_reference(
    operations: Iterable[OperationBancaire], connues: Iterable[PieceAttendue] = ()
) -> dict[str, tuple[str, ...]]:
    """Reference -> dossiers concernes, pour toute reference ambigue.

    Ambigue : portee par deux operations du releve (meme dossier ou non), ou par
    une operation du releve et une piece du depot d'un AUTRE dossier, ou par
    des pieces du depot de dossiers differents.
    """
    par_ref: dict[str, list[str]] = {}
    for op in operations:
        par_ref.setdefault(op.reference, []).append(op.dossier)
    depot_par_ref: dict[str, set[str]] = {}
    for p in connues:
        depot_par_ref.setdefault(p.reference, set()).add(p.dossier)
    resultat: dict[str, tuple[str, ...]] = {}
    for ref in sorted(set(par_ref) | set(depot_par_ref)):
        releve = par_ref.get(ref, [])
        depot = depot_par_ref.get(ref, set())
        tous = set(releve) | depot
        if len(releve) > 1 or len(tous) > 1:
            resultat[ref] = tuple(sorted(releve + sorted(depot - set(releve))))
    return resultat


# ---------------------------------------------------------------------------
# Journal differe : la transaction d'abord, l'audit ensuite
# ---------------------------------------------------------------------------


class _Notes:
    def __init__(self, journal: JournalAudit, maintenant: dt.datetime) -> None:
        self._journal = journal
        self._maintenant = maintenant
        self._attente: list[tuple[str, str, str, dict[str, Any]]] = []

    def noter(self, action: str, objet: str, details: Mapping[str, Any] | None = None,
              acteur: str = ACTEUR_SYSTEME) -> None:
        self._attente.append((acteur, action, objet, dict(details or {})))

    def abandonner(self) -> None:
        self._attente.clear()

    def vider(self) -> None:
        attente, self._attente = self._attente, []
        for acteur, action, objet, details in attente:
            self._journal.ecrire(acteur, action, objet, details, horodatage=self._maintenant)


@contextmanager
def _transaction(depot: Depot, notes: _Notes) -> Iterator[None]:
    """COMMIT du depot, PUIS ecriture des notes d'audit accumulees dans le bloc."""
    notes.abandonner()
    try:
        with depot.transaction():
            yield
    except BaseException:
        notes.abandonner()
        raise
    notes.vider()


def _objet(piece: PieceAttendue) -> str:
    return f"{piece.dossier}/{piece.reference}"


def _transition(piece: PieceAttendue, evenement: Evenement, aujourdhui: dt.date,
                notes: _Notes, acteur: str = ACTEUR_SYSTEME, **contexte: Any) -> PieceAttendue:
    nouvelle = etats.appliquer(piece, evenement, aujourdhui=aujourdhui, **contexte)
    details: dict[str, Any] = {
        "evenement": evenement.value,
        "etat_avant": piece.etat.value,
        "etat_apres": nouvelle.etat.value,
        "nb_relances": nouvelle.nb_relances,
    }
    for cle in ("valide_par", "confirme_par", "controle_par", "arbitre_par", "motif",
                "id_piece", "date_promesse"):
        if cle in contexte:
            details[cle] = contexte[cle]
    notes.noter("transition", _objet(piece), details, acteur=acteur)
    return nouvelle


def _jour_local(maintenant: dt.datetime) -> dt.date:
    garantir_fuseaux()
    return maintenant.astimezone(ZoneInfo(FUSEAU)).date()


def _exiger_temps(aujourdhui: dt.date, maintenant: dt.datetime) -> None:
    if isinstance(aujourdhui, dt.datetime) or not isinstance(aujourdhui, dt.date):
        raise TypeError("aujourdhui doit etre une date")
    if not isinstance(maintenant, dt.datetime) or maintenant.utcoffset() is None:
        raise ValueError("maintenant doit etre un datetime avec fuseau")


def _panne(panne: Panne | None, point: str) -> None:
    if panne is not None:
        panne(point)


# ---------------------------------------------------------------------------
# Resultat
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LigneAVerifier:
    dossier: str
    reference: str
    date_operation: dt.date
    montant: Decimal
    devise: str
    libelle: str
    statut: str
    motif: str


@dataclass
class ResumeCycle:
    aujourdhui: dt.date
    operations: int = 0
    manquants: int = 0
    a_verifier: int = 0
    pieces_creees: int = 0
    pieces_exclues: int = 0
    pieces_bloquees_par_moteur: int = 0
    blocages_leves: int = 0
    envois_repercutes: int = 0
    messages_lus: int = 0
    propositions: int = 0
    non_routees: int = 0
    doublons: int = 0
    transitions: int = 0
    relances_planifiees: int = 0
    brouillons_crees: int = 0
    brouillons_du_jour: list[str] = field(default_factory=list)
    reportees: dict[str, int] = field(default_factory=dict)
    bloquees: int = 0
    anomalies: list[str] = field(default_factory=list)
    quarantaine: int = 0
    mode_test: bool = False
    adresse_test: str = ""
    strategie_isolation: str = ""
    avertissements: list[str] = field(default_factory=list)
    # Alertes de donnees / configuration du mode test (circuit absent, garde-fous coupes) :
    # separees des avertissements d'isolation pour ne pas melanger deux familles de risques.
    alertes: list[str] = field(default_factory=list)
    garde_fous_coupes: list[str] = field(default_factory=list)
    # Regles d'exclusion PROPOSEES (statut `proposee`) : jamais appliquees, en attente du
    # responsable. Noms (ou, a defaut, dossier et expression) affiches dans une ligne du resume.
    regles_provisoires: list[str] = field(default_factory=list)

    def en_dict(self) -> dict[str, Any]:
        return {
            "aujourdhui": self.aujourdhui, "operations": self.operations,
            "manquants": self.manquants, "a_verifier": self.a_verifier,
            "pieces_creees": self.pieces_creees, "pieces_exclues": self.pieces_exclues,
            "pieces_bloquees_par_moteur": self.pieces_bloquees_par_moteur,
            "blocages_leves": self.blocages_leves, "envois_repercutes": self.envois_repercutes,
            "messages_lus": self.messages_lus, "propositions": self.propositions,
            "non_routees": self.non_routees, "doublons": self.doublons,
            "transitions": self.transitions, "relances_planifiees": self.relances_planifiees,
            "brouillons_crees": self.brouillons_crees,
            "brouillons_du_jour": list(self.brouillons_du_jour),
            "reportees": dict(sorted(self.reportees.items())), "bloquees": self.bloquees,
            "anomalies": list(self.anomalies), "quarantaine": self.quarantaine,
            "mode_test": self.mode_test, "strategie_isolation": self.strategie_isolation,
            "regles_provisoires": len(self.regles_provisoires),
        }

    def texte(self) -> str:
        d = self.en_dict()
        lignes = []
        if self.mode_test:
            lignes.append(
                f"*** MODE TEST ACTIF : tout e-mail part vers {affichable(self.adresse_test)}, "
                "jamais vers un client. Donnees fictives uniquement. ***")
        lignes += [f"AVERTISSEMENT : {affichable(a)}" for a in self.avertissements]
        lignes += [f"AVERTISSEMENT : {affichable(a)}" for a in self.alertes]
        lignes += [f"GARDE-FOU DESACTIVE (mode test) : {affichable(g)}" for g in self.garde_fous_coupes]
        lignes.append(f"Cycle du {self.aujourdhui.isoformat()}")
        for cle, valeur in d.items():
            if cle in ("aujourdhui", "anomalies", "brouillons_du_jour", "reportees",
                       "mode_test", "strategie_isolation", "regles_provisoires"):
                continue
            lignes.append(f"  {cle:28s}: {valeur}")
        for raison, n in d["reportees"].items():
            lignes.append(f"  reportees {raison:18s}: {n}")
        if self.regles_provisoires:
            lignes.append(f"  regles provisoires          : {len(self.regles_provisoires)} en attente de "
                          f"validation ({', '.join(affichable(n) for n in self.regles_provisoires)})")
        lignes.append(f"  brouillons du jour          : {len(self.brouillons_du_jour)}")
        lignes += [f"    {i}" for i in self.brouillons_du_jour]
        lignes.append(f"  anomalies                   : {len(self.anomalies)}")
        lignes += [f"    {affichable(a)}" for a in self.anomalies]
        return "\n".join(lignes) + "\n"


# ---------------------------------------------------------------------------
# Etape 4 (et sortie de `envoyer`) : repercussion idempotente des envois
# ---------------------------------------------------------------------------


def repercuter_envois(depot: Depot, journal: JournalAudit, maintenant: dt.datetime) -> tuple[int, list[str]]:
    """Applique `RELANCE_ENVOYEE` pour chaque envoi effectif pas encore reflete.

    Idempotent : `depot.marquer_vu("envoi-applique:<id>")` dans la MEME transaction
    que la mise a jour des pieces. Tout ou rien par envoi : si une piece refuse la
    transition (piece introuvable, plafond, piece bloquee, validateur absent),
    rien n'est applique pour cet envoi, il reste a repercuter et l'anomalie est
    signalee a chaque cycle (jamais de compteur applique deux fois).
    `aujourdhui` de la transition = date effective de l'envoi.
    Renvoie (nombre d'envois appliques, anomalies).
    """
    notes = _Notes(journal, maintenant)
    appliques = 0
    anomalies: list[str] = []
    for e in depot.historique_envois():
        cle = CLE_ENVOI_APPLIQUE + e.id_relance
        if depot.est_vu(cle):
            continue
        brouillon = depot.charger_brouillon(e.id_relance)
        valide_par = brouillon.valide_par if brouillon is not None else ""
        try:
            with _transaction(depot, notes):
                if not depot.marquer_vu(cle):
                    continue
                modifiees = []
                for ref in e.references:
                    trouvees = [
                        p for d in e.dossiers for p in depot.charger_pieces(dossier=d)
                        if p.reference == ref
                    ]
                    if len(trouvees) != 1:
                        raise etats.TransitionInterdite(
                            f"{ref} : {len(trouvees)} piece(s) correspondante(s) dans {e.dossiers}"
                        )
                    modifiees.append(_transition(
                        trouvees[0], Evenement.RELANCE_ENVOYEE, e.date_envoi, notes,
                        valide_par=valide_par,
                    ))
                depot.sauver_pieces(modifiees)
                notes.noter("envoi_repercute", e.id_relance, {
                    "valide_par": valide_par, "date_envoi": e.date_envoi,
                    "references": list(e.references),
                })
        except etats.TransitionInterdite as exc:
            anomalies.append(f"envoi {e.id_relance} non repercute : {exc}")
            continue
        appliques += 1
    return appliques, anomalies


# ---------------------------------------------------------------------------
# Le cycle
# ---------------------------------------------------------------------------


def executer_cycle(
    entree: Path | str,
    instance: Instance | Path | str,
    aujourdhui: dt.date,
    maintenant: dt.datetime,
    *,
    panne: Panne | None = None,
    limite_memoire_eml: int = LIMITE_MEMOIRE_LECTURE_EML,
    limite_cpu_eml: float = LIMITE_CPU_LECTURE_EML,
    mode_test: bool = False,
    strategie_isolation: str | None = None,
) -> ResumeCycle:
    """Execute un cycle complet (voir la docstring du module).

    `mode_test` active le mode test en plus de `config.json` (il ne peut pas le
    desactiver). `strategie_isolation` : None (automatique), "fork", "spawn" ou
    "directe" ; voir `choisir_strategie_isolation`. Tout refus (configuration, strategie)
    intervient AVANT la moindre ecriture, instance non creee comprise.

    Chaque .eml est analyse dans un processus enfant borne a `limite_memoire_eml`
    octets d'espace d'adressage supplementaire et `limite_cpu_eml` secondes de CPU ;
    tout depassement met le fichier en quarantaine. Sans `resource` / `fork`, le
    cycle est refuse d'emblee (`IsolationIndisponible`).

    Rejouable : un second appel le meme jour avec les memes entrees ne cree ni
    piece, ni brouillon, ni decision de plus. `panne(point)` est un point
    d'injection pour les tests de reprise (points : apres_repercussion,
    apres_pieces, apres_routage, apres_transitions, apres_brouillon,
    avant_sorties).

    Limite connue : un crash entre le COMMIT d'une etape et l'ecriture de son
    audit laisse l'evenement sans trace d'audit (jamais l'inverse).
    """
    _exiger_temps(aujourdhui, maintenant)
    inst = Instance.de(instance)
    config = charger_config(inst)
    en_mode_test = bool(mode_test) or config["mode_test"]
    strategie = choisir_strategie_isolation(en_mode_test, strategie_isolation)
    # D6 : hors mode test, un dossier sans circuit explicite est refuse (avant toute ecriture).
    entrees = lire_entrees(entree, circuit_obligatoire=not en_mode_test)
    sans_circuit = dossiers_sans_circuit(Path(entree) / "dossiers.csv") if en_mode_test else []
    if en_mode_test:
        verifier_adresse_test(config["adresse_test"], entrees.dossiers)         # jamais l'adresse d'un client

    # 2. Collisions dans le releve : refus AVANT toute ecriture.
    collisions = collisions_de_reference(entrees.operations)
    if collisions:
        raise CollisionReferences(collisions)

    rapprochements = Moteur(entrees.referentiel, entrees.alias).rapprocher(
        entrees.operations, entrees.pieces)
    resume = ResumeCycle(aujourdhui=aujourdhui, operations=len(rapprochements),
                         mode_test=en_mode_test, adresse_test=config["adresse_test"] if en_mode_test else "",
                         strategie_isolation=strategie)
    if strategie == "spawn":
        resume.avertissements.append(AVERTISSEMENT_SPAWN)
    elif strategie == "directe":
        resume.avertissements.append(AVERTISSEMENT_DIRECTE)
    if sans_circuit:
        resume.alertes.append(
            f"circuit absent pour {len(sans_circuit)} dossier(s), C-DIRECT par defaut : "
            f"{', '.join(sans_circuit)} (hors mode test : refus, decision D6)")
    resume.garde_fous_coupes = list(garde_fous_mode_test(inst, mode_test)) if en_mode_test else []
    resume.regles_provisoires = [
        r.get("nom") or f"{r.get('dossier', '')}/{r.get('motif_regex', '')}"
        for r in getattr(entrees.referentiel, "provisoires", [])
    ]

    # Journal d'audit sain AVANT toute ecriture en base (sinon : effets sans trace).
    mode_cycle = "test" if en_mode_test else "reel"
    with _ouvrir(inst, ecriture=True, mode=mode_cycle) as (depot, journal):
        # 2 bis. Collisions entre le releve et le depot.
        collisions = collisions_de_reference(entrees.operations, depot.charger_pieces())
        if collisions:
            raise CollisionReferences(collisions)
        notes = _Notes(journal, maintenant)
        # Le PREMIER cycle d'une instance fige son mode (test ou reel) ; les suivants, comme
        # toute operation, ont deja ete confrontes a lui par `_ouvrir`.
        _figer_mode(inst, depot, journal, mode_cycle,
                    "config.json et --mode-test" if mode_test and config["mode_test"]
                    else "--mode-test" if mode_test else "config.json" if config["mode_test"] else "defaut",
                    aujourdhui, maintenant)
        if en_mode_test:
            # Une fois par cycle, avant tout effet : le mode ne passe jamais inapercu.
            journal.ecrire(ACTEUR_SYSTEME, "mode_test_actif", aujourdhui.isoformat(), {
                "adresse_test": config["adresse_test"],
                "source": ("config.json et --mode-test" if mode_test and config["mode_test"]
                           else "--mode-test" if mode_test else "config.json"),
            }, horodatage=maintenant)
        if sans_circuit:
            journal.ecrire(ACTEUR_SYSTEME, "circuit_par_defaut", aujourdhui.isoformat(), {
                "dossiers": sans_circuit, "circuit": "C-DIRECT",
                "avertissement": "circuit absent : en production ce cycle serait refuse (D6)",
            }, horodatage=maintenant)
        if strategie != "fork":
            journal.ecrire(ACTEUR_SYSTEME, "isolation_degradee", aujourdhui.isoformat(), {
                "strategie": strategie,
                "niveau": "partielle" if strategie == "spawn" else "aucune",
                "avertissement": resume.avertissements[0],
                "plateforme": sys.platform,
            }, horodatage=maintenant)

        # 4 avant 3 (ecart volontaire a l'ordre du cahier de l'integrateur) : un envoi
        # deja parti doit etre enregistre AVANT que la fusion ne bloque ou n'exclue une
        # piece, sinon `etats` refuserait RELANCE_ENVOYEE sur la piece bloquee et le fait
        # accompli serait perdu. Les pieces visees existent toujours deja en base.
        n, anomalies = repercuter_envois(depot, journal, maintenant)
        resume.envois_repercutes = n
        resume.anomalies += anomalies
        _panne(panne, "apres_repercussion")

        a_verifier = _etape_pieces(depot, notes, rapprochements, aujourdhui, resume)
        _panne(panne, "apres_pieces")

        quarantaine = _etape_routage(depot, notes, inst, entrees.dossiers, aujourdhui, resume,
                                     limite_memoire_eml=limite_memoire_eml,
                                     limite_cpu_eml=limite_cpu_eml, strategie=strategie)
        _panne(panne, "apres_routage")

        pieces_avant = depot.charger_pieces()
        plan = planifier(pieces_avant, entrees.dossiers, depot.historique_envois(), aujourdhui)
        _etape_transitions(depot, notes, plan, pieces_avant, aujourdhui, resume)
        _panne(panne, "apres_transitions")

        _etape_brouillons(depot, notes, plan, pieces_avant, entrees.dossiers, aujourdhui, resume, panne)
        _panne(panne, "avant_sorties")

        ecrire_sorties(inst, depot, rapprochements, a_verifier, resume, quarantaine=quarantaine,
                       regles_provisoires=getattr(entrees.referentiel, "provisoires", []))
        journal.ecrire(ACTEUR_SYSTEME, "cycle_termine", aujourdhui.isoformat(),
                       resume.en_dict(), horodatage=maintenant)
    return resume


def _ligne(op: OperationBancaire, statut: str, motif: str) -> LigneAVerifier:
    return LigneAVerifier(op.dossier, op.reference, op.date_operation, op.montant,
                          op.devise, op.libelle, statut, motif)


def _etape_pieces(depot: Depot, notes: _Notes, rapprochements: list[Rapprochement],
                  aujourdhui: dt.date, resume: ResumeCycle) -> list[LigneAVerifier]:
    """Etape 3 : creation des pieces MANQUANT et fusion avec le depot."""
    connues = {(p.dossier, p.reference): p for p in depot.charger_pieces()}
    a_verifier: list[LigneAVerifier] = []
    a_sauver: list[PieceAttendue] = []
    with _transaction(depot, notes):
        for r in rapprochements:
            op = r.operation
            piece = connues.get((op.dossier, op.reference))
            if r.statut is Statut.MANQUANT:
                resume.manquants += 1
                if piece is None:
                    try:
                        nouvelle = etats.creer_depuis_rapprochement(r)
                    except ValueError as exc:
                        a_verifier.append(_ligne(op, r.statut.value, f"piece non creee : {exc}"))
                        continue
                    a_sauver.append(nouvelle)
                    resume.pieces_creees += 1
                    notes.noter("piece_creee", _objet(nouvelle), {
                        "periode": nouvelle.periode, "montant": nouvelle.montant,
                        "devise": nouvelle.devise, "date_operation": nouvelle.date_operation,
                        # Explicable apres coup sans rejouer le moteur (A10).
                        "regle": r.regle, "motif": r.motif,
                    })
                elif (piece.bloquee and piece.motif_blocage.startswith(PREFIXE_MOTIF_CYCLE)
                      and piece.etat not in ETATS_TERMINAUX):
                    a_sauver.append(_transition(piece, Evenement.BLOCAGE_LEVE, aujourdhui, notes))
                    resume.blocages_leves += 1
                continue

            if r.statut in (Statut.PARTIEL, Statut.A_VERIFIER):
                resume.a_verifier += 1
                a_verifier.append(_ligne(op, r.statut.value, r.motif))
            if piece is None or piece.etat in ETATS_TERMINAUX:
                continue
            if r.statut is Statut.HORS_PERIMETRE:
                try:
                    a_sauver.append(_transition(
                        piece, Evenement.EXCLUSION_CREEE, aujourdhui, notes, regle_validee=True,
                        motif=f"{r.regle} : {r.motif}",
                    ))
                    resume.pieces_exclues += 1
                except etats.TransitionInterdite as exc:
                    a_verifier.append(_ligne(op, r.statut.value, f"exclusion impossible : {exc}"))
                continue
            # JUSTIFIE, PARTIEL, A_VERIFIER sur une piece connue : un humain confirme.
            if r.statut is Statut.JUSTIFIE:
                a_verifier.append(_ligne(
                    op, r.statut.value,
                    "piece connue justifiee par le moteur ("
                    + " + ".join(p.id_piece for p in r.pieces)
                    + ") : confirmer avec `rattacher`",
                ))
            if piece.bloquee or piece.etat is EtatPiece.RECUE:
                continue
            if depot.est_vu(_cle_deblocage_humain(piece)):
                continue                      # un humain a deja leve ce blocage : on ne le remet pas
            motif = (f"{PREFIXE_MOTIF_CYCLE}operation {r.statut.value} au rapprochement "
                     f"({' + '.join(p.id_piece for p in r.pieces) or 'sans piece'}) : a confirmer")
            a_sauver.append(_transition(piece, Evenement.BLOCAGE_SIGNALE, aujourdhui, notes, motif=motif))
            resume.pieces_bloquees_par_moteur += 1
        depot.sauver_pieces(a_sauver)
    return sorted(a_verifier, key=lambda l: (l.dossier, l.date_operation, l.reference, l.statut))


def _cles_message(message: Any) -> list[str]:
    if not message.fichiers:
        return [routage.cle_idempotence(message.message_id, "")]
    return [
        routage.cle_idempotence(message.message_id, routage.empreinte_contenu(f.contenu))
        for f in message.fichiers
    ]


CLES_CONTEXTE_FILE = ("raison", "expediteur", "recu_le", "objet")
LONGUEUR_MAX_OBJET = 500


def contexte_file(decision: DecisionRoutage) -> dict[str, str]:
    """Contexte d'une decision de la file humaine : `raison` (detail d'origine du routage),
    et, si le cycle l'a conserve, `expediteur`, `recu_le`, `objet` du message.

    Tolerante : un `detail` ancien (texte simple, ou JSON qui n'est pas un objet) donne
    `{"raison": detail}`. Toutes les valeurs sont des chaines. Les valeurs viennent de
    l'exterieur : les passer par `affichable` avant tout affichage."""
    brut = decision.detail if isinstance(decision.detail, str) else ""
    try:
        lu = json.loads(brut)
    except ValueError:
        return {"raison": brut}
    if not isinstance(lu, dict) or not isinstance(lu.get("raison", ""), str):
        return {"raison": brut}
    contexte = {cle: str(valeur) for cle, valeur in lu.items() if isinstance(valeur, (str, int, float))}
    contexte.setdefault("raison", "")
    return contexte


def _avec_contexte(d: DecisionRoutage, message: MessageEntrant) -> DecisionRoutage:
    """Copie de `d` dont `detail` porte, en JSON, la raison du routage ET l'origine du message
    (A8). Meme schema de base : rien ne change pour le depot."""
    contexte = {
        "raison": d.detail,
        "expediteur": message.expediteur[:320],
        "recu_le": message.recu_le.isoformat(),
        "objet": " ".join(message.objet.split())[:LONGUEUR_MAX_OBJET],
    }
    return dataclasses.replace(d, detail=json.dumps(contexte, ensure_ascii=False, sort_keys=True))


@dataclass(frozen=True)
class FichierEnQuarantaine:
    nom: str
    empreinte: str        # SHA-256 du fichier brut, "" s'il est illisible sur disque
    raison: str           # type et message de l'exception, sans trace Python


CLE_QUARANTAINE = "eml-quarantaine:"


class EmlRefuse(Exception):
    """Fichier refuse AVANT analyse (taille) : il part en quarantaine."""


class IsolationIndisponible(ErreurCycle):
    """La lecture isolee des .eml exige `resource` et `fork` (Linux, macOS)."""


def _fork_disponible() -> bool:
    return resource is not None and "fork" in multiprocessing.get_all_start_methods()


def choisir_strategie_isolation(mode_test: bool = False, demandee: str | None = None) -> str:
    """Strategie de lecture des .eml ("fork", "spawn" ou "directe") ; refuse sinon.

    - Automatique (`demandee` None) : `fork` + `resource` si la plateforme les a (Linux,
      macOS). Sinon (Windows) : `spawn` EN MODE TEST seulement ; hors mode test, refus.
    - `spawn` (delai de garde, SANS limite memoire) et `directe` (aucune isolation) ne
      sont acceptes qu'en mode test, meme demandes explicitement : une protection plus
      faible ne se choisit jamais en production, ni par accident.
    Une protection qui disparait sans bruit est pire qu'une protection absente : tout
    repli est annonce (resume, `resume.txt`, journal `isolation_degradee`).
    """
    if demandee is not None and demandee not in STRATEGIES_ISOLATION:
        raise ErreurCycle(
            f"strategie d'isolation inconnue {affichable(repr(demandee))} "
            f"(attendu : {', '.join(STRATEGIES_ISOLATION)})"
        )
    refus_plateforme = (
        "la lecture isolee des e-mails entrants exige Linux ou macOS (modules `resource` "
        "et demarrage `fork`) : cycle refuse sur cette plateforme, rien n'a ete ecrit"
    )
    indice = "" if mode_test else " ; utilisez --mode-test pour un essai local"
    if demandee is None:
        if _fork_disponible():
            return "fork"
        if mode_test:
            return "spawn"
        raise IsolationIndisponible(refus_plateforme + indice)
    if demandee == "fork":
        if not _fork_disponible():
            raise IsolationIndisponible(refus_plateforme + indice)
        return "fork"
    if not mode_test:
        raise IsolationIndisponible(
            f"la strategie d'isolation {demandee!r} est plus faible que fork + resource : "
            "elle n'est autorisee qu'en mode test (--mode-test ou config.json) ; "
            "utilisez --mode-test pour un essai local"
        )
    return demandee


def exiger_isolation(mode_test: bool = False, demandee: str | None = None) -> str:
    """Compatibilite : refus explicite si la lecture isolee est impossible ; renvoie
    la strategie retenue (voir `choisir_strategie_isolation`)."""
    return choisir_strategie_isolation(mode_test, demandee)


def _espace_adresses_actuel() -> int:
    """Taille virtuelle du processus (Linux : /proc/self/statm), 0 si inconnue."""
    try:
        with open("/proc/self/statm", "rb") as f:
            return int(f.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def _enfant_lire_eml(connexion: Any, chemin: str, limite_memoire: int, limite_cpu: int) -> None:
    """Corps du processus enfant : borne ses ressources, PUIS analyse, puis renvoie le
    `MessageEntrant` (ou une raison lisible). Ne laisse jamais remonter d'exception
    (multiprocessing imprimerait une trace Python)."""
    try:
        # RLIMIT_AS porte sur tout l'espace d'adressage, y compris celui herite du parent
        # par fork : la limite est donc un SUPPLEMENT au-dessus de l'existant.
        plafond = _espace_adresses_actuel() + limite_memoire
        resource.setrlimit(resource.RLIMIT_AS, (plafond, plafond))
        resource.setrlimit(resource.RLIMIT_CPU, (limite_cpu, limite_cpu + 1))
        message = routage.lire_eml(chemin)
        connexion.send(("ok", message))
    except BaseException as exc:  # noqa: BLE001 - tout echec devient une raison de quarantaine
        try:
            connexion.send(("erreur", _raison(exc)))
        except BaseException:  # noqa: BLE001
            pass
    finally:
        try:
            connexion.close()
        except BaseException:  # noqa: BLE001
            pass


def _lire_eml_isole(chemin: Path, limite_memoire: int, limite_cpu: float) -> MessageEntrant:
    """Analyse `chemin` dans un processus enfant borne (memoire, CPU, delai reel).

    Leve `EmlRefuse` avec une raison lisible pour tout echec : erreur d'analyse,
    MemoryError, depassement CPU (SIGXCPU), enfant tue au delai de garde, sortie
    anormale, resultat absent ou illisible. L'enfant est toujours attendu (`join`) :
    aucun processus ne survit a l'appel.
    """
    cpu = max(1, math.ceil(limite_cpu))
    contexte = multiprocessing.get_context("fork")
    lecture, ecriture = contexte.Pipe(duplex=False)
    enfant = contexte.Process(target=_enfant_lire_eml, daemon=True,
                              args=(ecriture, str(chemin), int(limite_memoire), cpu))
    enfant.start()
    ecriture.close()
    statut, valeur, code = _attendre_enfant(enfant, lecture, cpu + MARGE_GARDE_LECTURE_EML)
    return _message_ou_refus(statut, valeur, code, cpu + MARGE_GARDE_LECTURE_EML, cpu)


def _attendre_enfant(enfant: Any, lecture: Any, delai: float) -> tuple[str | None, Any, int | None]:
    """Attend le resultat de l'enfant au plus `delai` secondes ; le TUE s'il depasse et
    l'attend toujours : aucun processus ne survit. Renvoie (statut, valeur, code de sortie).
    `terminate` d'abord, `kill` si l'enfant ne s'arrete pas (Windows : les deux tuent)."""
    statut: str | None = None
    valeur: Any = None
    try:
        if lecture.poll(delai):
            try:
                statut, valeur = lecture.recv()
            except Exception:  # noqa: BLE001 - EOF (enfant mort) ou resultat illisible
                statut = None
        else:
            statut = "delai"
    finally:
        if enfant.is_alive():
            enfant.terminate()
            enfant.join(2.0)
            if enfant.is_alive():
                enfant.kill()
        enfant.join()
        code = enfant.exitcode
        lecture.close()
        enfant.close()      # libere le descripteur / la poignee du processus (Windows)
    return statut, valeur, code


def _message_ou_refus(statut: str | None, valeur: Any, code: int | None, delai: float,
                      cpu: int) -> MessageEntrant:
    """Traduit le resultat d'un enfant en `MessageEntrant` ou en `EmlRefuse` lisible."""
    if statut == "ok" and isinstance(valeur, MessageEntrant):
        return valeur
    if statut == "erreur":
        raise EmlRefuse(f"lecture isolee en echec : {valeur}")
    if statut == "delai":
        raise EmlRefuse(f"lecture isolee interrompue : plus de {delai:g} s")
    if code is not None and code < 0:
        nom = signal.Signals(-code).name if -code in signal.valid_signals() else str(-code)
        cause = {"SIGXCPU": f"temps CPU depasse (limite {cpu} s)",
                 "SIGKILL": "processus tue (memoire ou temps)"}.get(nom, "signal recu")
        raise EmlRefuse(f"lecture isolee en echec : {cause} [{nom}]")
    raise EmlRefuse(f"lecture isolee en echec : aucun resultat (code de sortie {code})")


def _enfant_lire_eml_spawn(connexion: Any, chemin: str, sys_path: list[str]) -> None:
    """Travailleur `spawn` : fonction de NIVEAU MODULE (serialisable par nom), seul moyen
    pour que `multiprocessing` la retrouve dans l'interpreteur neuf de l'enfant.

    Sous `spawn` l'enfant reimporte le module principal puis ce module : `src/` doit etre
    sur son `sys.path`. `multiprocessing` transmet deja `sys.path` ; on le retransmet
    explicitement (arguments) pour ne pas dependre de ce detail d'implementation.
    Aucune limite memoire possible (pas de `resource` sous Windows) : seul le delai
    de garde du parent borne cet enfant."""
    try:
        for entree in reversed(sys_path):
            if entree not in sys.path:
                sys.path.insert(0, entree)
        connexion.send(("ok", routage.lire_eml(chemin)))
    except BaseException as exc:  # noqa: BLE001 - tout echec devient une raison de quarantaine
        try:
            connexion.send(("erreur", _raison(exc)))
        except BaseException:  # noqa: BLE001
            pass
    finally:
        try:
            connexion.close()
        except BaseException:  # noqa: BLE001
            pass


def _lire_eml_spawn(chemin: Path, limite_cpu: float) -> MessageEntrant:
    """Analyse `chemin` dans un processus `spawn` borne par un DELAI de garde seulement
    (`limite_cpu` + marges, demarrage de l'interpreteur compris). Meme contrat que
    `_lire_eml_isole` : `EmlRefuse` lisible pour tout echec, aucun processus ne survit."""
    cpu = max(1, math.ceil(limite_cpu))
    delai = cpu + MARGE_GARDE_LECTURE_EML + MARGE_DEMARRAGE_SPAWN
    contexte = multiprocessing.get_context("spawn")
    lecture, ecriture = contexte.Pipe(duplex=False)
    enfant = contexte.Process(target=_enfant_lire_eml_spawn, daemon=True,
                              args=(ecriture, str(chemin), list(sys.path)))
    enfant.start()
    ecriture.close()
    statut, valeur, code = _attendre_enfant(enfant, lecture, delai)
    return _message_ou_refus(statut, valeur, code, delai, cpu)


def _lire_eml_direct(chemin: Path) -> MessageEntrant:
    """Lecture dans le processus principal, SANS isolation : memoire et temps non bornes
    (seules les exceptions sont rattrapees). Mode test uniquement (`--sans-isolation`)."""
    try:
        return routage.lire_eml(chemin)
    except Exception as exc:  # noqa: BLE001
        raise EmlRefuse(f"lecture directe en echec : {_raison(exc)}") from None


def _lire_eml(chemin: Path, strategie: str, limite_memoire: int, limite_cpu: float) -> MessageEntrant:
    if strategie == "fork":
        return _lire_eml_isole(chemin, limite_memoire, limite_cpu)
    if strategie == "spawn":
        return _lire_eml_spawn(chemin, limite_cpu)
    return _lire_eml_direct(chemin)


def _garde_eml(chemin: Path) -> None:
    """Leve `EmlRefuse` si le fichier ou son bloc d'en-tetes de plus haut niveau depasse
    les seuils. Ne fait qu'une lecture d'octets : l'analyseur n'est jamais appele.

    Limite connue : les en-tetes des PARTIES imbriquees (nom de fichier encode RFC 2047
    dans Content-Type / Content-Disposition) ne sont pas bornes ici et restent couteux
    a l'analyse (mesure : 109 Ko -> ~2-4 s, 218 Ko -> ~19 s et 1,8 Go).
    """
    taille = chemin.stat().st_size
    if taille > TAILLE_MAX_EML:
        raise EmlRefuse(f"fichier de {taille} octets > {TAILLE_MAX_EML} : non analyse")
    brut = chemin.read_bytes()
    fins = [i for i in (brut.find(b"\r\n\r\n"), brut.find(b"\n\n")) if i >= 0]
    bloc = min(fins) if fins else len(brut)
    if bloc > TAILLE_MAX_EN_TETES_EML:
        raise EmlRefuse(
            f"bloc d'en-tetes de {bloc} octets > {TAILLE_MAX_EN_TETES_EML} : non analyse "
            "(analyseur quadratique sur les en-tetes encodes)"
        )


def _raison(exc: BaseException) -> str:
    message = " ".join(str(exc).split())[:300]
    return f"{type(exc).__name__} : {message}" if message else type(exc).__name__


def _etape_routage(depot: Depot, notes: _Notes, inst: Instance, dossiers: Mapping[str, Dossier],
                   aujourdhui: dt.date, resume: ResumeCycle, *,
                   limite_memoire_eml: int = LIMITE_MEMOIRE_LECTURE_EML,
                   limite_cpu_eml: float = LIMITE_CPU_LECTURE_EML,
                   strategie: str = "fork") -> list[FichierEnQuarantaine]:
    """Etape 5 : routage des .eml ; rien n'est rattache sans confirmation humaine.

    Chaque fichier est lu et route isolement : un .eml pathologique (imbrication
    profonde -> RecursionError, encodage casse...) part en quarantaine
    (`sortie/quarantaine.csv`, journal `eml_quarantaine` une fois par empreinte) et
    les autres messages sont traites normalement. Il est retente a chaque cycle.
    """
    adresses = routage.adresses_par_dossier(dossiers)
    domaines = routage.domaines_par_dossier(dossiers)
    chemins = sorted(
        (p for p in inst.entrant.iterdir() if p.is_file() and p.suffix.lower() == ".eml"),
        key=lambda p: p.name,
    )   # meme selection et meme ordre que routage.lire_dossier_eml
    quarantaine: list[FichierEnQuarantaine] = []
    for chemin in chemins:
        try:
            _garde_eml(chemin)                    # premier filtre, sans analyse
            message = _lire_eml(chemin, strategie, limite_memoire_eml, limite_cpu_eml)
            attendues = depot.charger_pieces()
            deja_vus = {c for c in _cles_message(message) if depot.est_vu(c)}
            decisions = routage.router(
                message, adresses=adresses, attendues=attendues, deja_vus=deja_vus,
                aujourdhui=aujourdhui, domaines=domaines,
            )
        except Exception as exc:  # noqa: BLE001 - un fichier ne doit jamais bloquer les autres
            try:
                empreinte = routage.empreinte_contenu(chemin.read_bytes())
            except OSError:
                empreinte = ""
            q = FichierEnQuarantaine(chemin.name, empreinte, _raison(exc))
            quarantaine.append(q)
            with _transaction(depot, notes):
                if depot.marquer_vu(CLE_QUARANTAINE + (empreinte or "illisible:" + chemin.name)):
                    notes.noter("eml_quarantaine", chemin.name,
                                {"empreinte": empreinte, "raison": q.raison})
            continue
        resume.messages_lus += 1
        with _transaction(depot, notes):
            for d in decisions:
                if d.statut is StatutRoutage.DOUBLON:
                    resume.doublons += 1
                    continue
                if not depot.marquer_vu(routage.cle_idempotence(d.message_id, d.empreinte)):
                    resume.doublons += 1
                    continue
                depot.mettre_en_file(_avec_contexte(d, message))
                details = {
                    "message_id": d.message_id, "fichier": d.nom_fichier, "empreinte": d.empreinte,
                    "dossier": d.dossier or "", "detail": d.detail,
                }
                if d.statut is StatutRoutage.PROPOSEE:
                    resume.propositions += 1
                    details["reference"] = d.reference_operation or ""
                    notes.noter("rattachement_propose", f"{d.dossier}/{d.reference_operation}", details)
                else:
                    resume.non_routees += 1
                    details["motif"] = d.motif.value if d.motif else ""
                    notes.noter("message_en_file_humaine", d.message_id, details)
    resume.quarantaine = len(quarantaine)
    return quarantaine


def _etape_transitions(depot: Depot, notes: _Notes, plan: Planification,
                       pieces: list[PieceAttendue], aujourdhui: dt.date, resume: ResumeCycle) -> None:
    """Etape 6 : transitions de calendrier decidees par `cadence.planifier`."""
    par_ref: dict[str, list[PieceAttendue]] = {}
    for p in pieces:
        par_ref.setdefault(p.reference, []).append(p)
    resume.bloquees = len(plan.bloquees)
    with _transaction(depot, notes):
        a_sauver = []
        for ref, evenement in plan.transitions:
            cibles = par_ref.get(ref, [])
            if len(cibles) != 1:
                resume.anomalies.append(f"transition {evenement.value} ignoree : reference {ref} ambigue")
                continue
            try:
                a_sauver.append(_transition(cibles[0], evenement, aujourdhui, notes))
                resume.transitions += 1
            except etats.TransitionInterdite as exc:
                resume.anomalies.append(f"transition {evenement.value} refusee pour {ref} : {exc}")
        depot.sauver_pieces(a_sauver)


def _etape_brouillons(depot: Depot, notes: _Notes, plan: Planification, pieces: list[PieceAttendue],
                      dossiers: Mapping[str, Dossier], aujourdhui: dt.date, resume: ResumeCycle,
                      panne: Panne | None) -> None:
    """Etape 7 : brouillons (statut BROUILLON). Le cycle ne valide jamais rien."""
    par_ref = {p.reference: p for p in pieces}
    en_attente = [b for b in depot.lister_brouillons() if b.statut in STATUTS_EN_ATTENTE]
    reportees = Counter(r.raison for r in plan.reportees)
    resume.relances_planifiees = len(plan.relances)
    for planifiee in plan.relances:
        id_prevu = identifiant_relance(planifiee.destinataire, planifiee.references,
                                       planifiee.niveau, aujourdhui)
        concurrents = sorted(
            b.id_relance for b in en_attente
            if b.id_relance != id_prevu and set(b.references) & set(planifiee.references)
        )
        if concurrents:
            reportees[BROUILLON_EN_ATTENTE] += 1
            resume.anomalies.append(
                f"relance vers {planifiee.destinataire} non creee : pieces deja dans le(s) "
                f"brouillon(s) en attente {', '.join(concurrents)} (valider/envoyer ou rejeter d'abord)"
            )
            continue
        try:
            b = construire_brouillon(planifiee, par_ref, dossiers, aujourdhui)
        except ValueError as exc:
            resume.anomalies.append(f"brouillon vers {planifiee.destinataire} impossible : {exc}")
            continue
        resume.brouillons_du_jour.append(b.id_relance)
        with _transaction(depot, notes):
            if depot.sauver_brouillon(b):
                resume.brouillons_crees += 1
                notes.noter("brouillon_cree", b.id_relance, {
                    "destinataire": b.destinataire, "niveau": b.niveau,
                    "dossiers": list(b.dossiers), "references": list(b.references),
                })
        _panne(panne, "apres_brouillon")
    resume.reportees = dict(sorted(reportees.items()))


# ---------------------------------------------------------------------------
# Sorties (etape 8) : tout est deterministe, aucune horloge
# ---------------------------------------------------------------------------


def _ecrire_csv(chemin: Path, entetes: Sequence[str], lignes: Iterable[Sequence[Any]]) -> None:
    tampon = io.StringIO()
    ecrivain = csv.writer(tampon, lineterminator="\n")
    ecrivain.writerow(entetes)
    for ligne in lignes:
        # Toute cellule texte est neutralisee (formule de tableur) ; un vrai nombre reste un nombre.
        ecrivain.writerow(ligne_sure(["" if v is None else v for v in ligne]))
    chemin.write_bytes(tampon.getvalue().encode("utf-8"))


def _iso(d: dt.date | dt.datetime | None) -> str:
    return "" if d is None else d.isoformat()


def texte_brouillon(b: Brouillon) -> str:
    return (
        f"Identifiant : {b.id_relance}\n"
        f"Statut      : {b.statut.value}\n"
        f"Cree le     : {b.cree_le.isoformat()}\n"
        f"Niveau      : {b.niveau}\n"
        f"Dossiers    : {', '.join(b.dossiers)}\n"
        f"References  : {', '.join(b.references)}\n"
        f"Valide par  : {b.valide_par or '-'}\n"
        f"A: {b.destinataire}\n"
        f"Objet: {b.objet}\n\n{b.corps}\n"
    )


def ecrire_sorties(inst: Instance, depot: Depot, rapprochements: list[Rapprochement],
                   a_verifier: list[LigneAVerifier], resume: ResumeCycle, *,
                   quarantaine: Sequence[FichierEnQuarantaine] = (),
                   regles_provisoires: Sequence[Mapping[str, str]] = ()) -> None:
    """Toutes les sorties. Chaque CSV passe par `rapport.ligne_sure` (formules de
    tableur neutralisees : libelles, noms de pieces jointes et Message-ID viennent
    de l'exterieur)."""
    sortie = inst.sortie
    sortie.mkdir(parents=True, exist_ok=True)
    ecrire_suivi(rapprochements, sortie / "tableau_suivi.csv")

    pieces = depot.charger_pieces()
    _ecrire_csv(sortie / "pieces_attendues.csv", (
        "dossier", "reference", "periode", "date_operation", "montant", "devise", "libelle",
        "etat", "nb_relances", "date_premiere_demande", "date_derniere_relance",
        "date_promesse", "bloquee", "motif_blocage", "pieces_rattachees",
    ), ([
        p.dossier, p.reference, p.periode, _iso(p.date_operation), str(p.montant), p.devise,
        p.libelle, p.etat.value, p.nb_relances, _iso(p.date_premiere_demande),
        _iso(p.date_derniere_relance), _iso(p.date_promesse), int(p.bloquee),
        p.motif_blocage, " + ".join(p.pieces_rattachees),
    ] for p in pieces))

    periodes: dict[tuple[str, str], list[PieceAttendue]] = {}
    for p in pieces:
        periodes.setdefault((p.dossier, p.periode), []).append(p)
    _ecrire_csv(sortie / "periodes.csv", ("dossier", "periode", "etat", "pieces", "terminales"), ([
        d, per, etats.etat_periode(ps).value, len(ps), sum(p.etat in ETATS_TERMINAUX for p in ps),
    ] for (d, per), ps in sorted(periodes.items())))

    _ecrire_csv(sortie / "a_verifier.csv", (
        "dossier", "reference", "date_operation", "montant", "devise", "libelle", "statut", "motif",
    ), ([
        l.dossier, l.reference, _iso(l.date_operation), str(l.montant), l.devise, l.libelle,
        l.statut, l.motif,
    ] for l in a_verifier))

    _ecrire_csv(sortie / "file_humaine.csv", (
        "message_id", "empreinte", "nom_fichier", "statut", "dossier", "periode",
        "reference_operation", "motif", "detail", "expediteur", "recu_le", "objet",
    ), ([
        d.message_id, d.empreinte, d.nom_fichier, d.statut.value, d.dossier, d.periode,
        d.reference_operation, d.motif.value if d.motif else "", c["raison"],
        c.get("expediteur", ""), c.get("recu_le", ""), c.get("objet", ""),
    ] for d, c in ((d, contexte_file(d)) for d in depot.file_humaine(resolue=False))))

    _ecrire_csv(sortie / "escalades.csv", (
        "dossier", "reference", "periode", "date_operation", "montant", "devise", "libelle",
        "nb_relances", "date_premiere_demande", "date_derniere_relance", "bloquee", "motif_blocage",
    ), ([
        p.dossier, p.reference, p.periode, _iso(p.date_operation), str(p.montant), p.devise,
        p.libelle, p.nb_relances, _iso(p.date_premiere_demande), _iso(p.date_derniere_relance),
        int(p.bloquee), p.motif_blocage,
    ] for p in pieces if p.etat is EtatPiece.ESCALADEE))

    _ecrire_csv(sortie / "regles_provisoires.csv", (
        "dossier", "nom", "motif_regex", "motif", "auteur", "date_activation",
    ), ([r.get("dossier", ""), r.get("nom", ""), r.get("motif_regex", ""), r.get("motif", ""),
         r.get("auteur", ""), r.get("date_activation", "")]
        for r in sorted(regles_provisoires, key=lambda r: (r.get("dossier", ""), r.get("nom", ""),
                                                           r.get("motif_regex", "")))))

    _ecrire_csv(sortie / "quarantaine.csv", ("nom_fichier", "empreinte", "raison"), (
        [q.nom, q.empreinte, q.raison] for q in sorted(quarantaine, key=lambda q: q.nom)
    ))

    dossier_b = sortie / "brouillons"
    dossier_b.mkdir(exist_ok=True)
    for b in depot.lister_brouillons():
        (dossier_b / f"{b.id_relance}.txt").write_bytes(texte_brouillon(b).encode("utf-8"))

    (sortie / "resume.txt").write_bytes(resume.texte().encode("utf-8"))


# ---------------------------------------------------------------------------
# Decisions humaines (appelees par le CLI) : `par` borne a validateurs.txt
# ---------------------------------------------------------------------------


def valider_relance(instance: Instance | Path | str, id_relance: str, par: str, *,
                    maintenant: dt.datetime, mode_test: bool = False) -> Brouillon:
    inst = Instance.de(instance)
    nom = exiger_validateur(inst, par)
    with _ouvrir(inst, ecriture=True, mode=mode_demande(inst, mode_test)) as (depot, journal):
        return envoi_mod.valider(depot, journal, id_relance, nom, maintenant=maintenant)


def rejeter_relance(instance: Instance | Path | str, id_relance: str, par: str, motif: str, *,
                    maintenant: dt.datetime, mode_test: bool = False) -> Brouillon:
    inst = Instance.de(instance)
    nom = exiger_validateur(inst, par)
    with _ouvrir(inst, ecriture=True, mode=mode_demande(inst, mode_test)) as (depot, journal):
        return envoi_mod.rejeter(depot, journal, id_relance, nom, motif, maintenant=maintenant)


ACTIONS_D_ENVOI_NOMINATIVES = frozenset({
    "envoi_demarre", "envoi_effectue", "envoi_incertain", "envoi_echec_certain",
})


class _JournalEnvoyePar:
    """Journal passe a `envoi.envoyer` : il ajoute `envoye_par` (qui a DECLENCHE l'envoi, et pas
    seulement qui l'a valide) aux entrees de la tentative d'envoi. `envoi.py` ecrit ces entrees
    lui-meme avec des details figes ; cette enveloppe evite de le modifier. Tout le reste est
    delegue tel quel au vrai journal."""

    def __init__(self, journal: JournalAudit, envoye_par: str | None) -> None:
        self._journal = journal
        self._envoye_par = envoye_par

    def ecrire(self, acteur: str, action: str, objet: str, details: Mapping[str, Any] | None = None,
               **kw: Any) -> Any:
        if action in ACTIONS_D_ENVOI_NOMINATIVES:
            details = {**(details or {}), "envoye_par": self._envoye_par}
        return self._journal.ecrire(acteur, action, objet, details, **kw)

    def __getattr__(self, nom: str) -> Any:
        return getattr(self._journal, nom)


def _controler_avant_envoi(depot: Depot, journal: JournalAudit, b: Brouillon,
                           maintenant: dt.datetime, *, limite_hebdomadaire: bool = True) -> None:
    """Garde-fous que `envoi.envoyer` ne connait pas (leve `EnvoiRefuse`, journalise).
    `limite_hebdomadaire=False` : mode test explicite, saute UNIQUEMENT la limite par destinataire."""
    jour = _jour_local(maintenant)
    raisons = []
    recents = [] if not limite_hebdomadaire else [
        e for e in depot.historique_envois(destinataire=b.destinataire)
        if e.id_relance != b.id_relance and 0 <= (jour - e.date_envoi).days < FENETRE_HEBDO_JOURS
    ]
    if recents:
        raisons.append(
            f"{b.destinataire} a deja recu {recents[-1].id_relance} le "
            f"{recents[-1].date_envoi.isoformat()} (un e-mail par {FENETRE_HEBDO_JOURS} jours)"
        )
    for ref in b.references:
        trouvees = [p for d in b.dossiers for p in depot.charger_pieces(dossier=d) if p.reference == ref]
        if len(trouvees) != 1:
            raisons.append(f"{ref} : piece introuvable ou ambigue")
            continue
        p = trouvees[0]
        if p.etat not in ETATS_RECLAMABLES or p.bloquee or p.nb_relances >= etats.PLAFOND_RELANCES:
            raisons.append(f"{ref} n'est plus reclamable ({p.etat.value}"
                           f"{', bloquee' if p.bloquee else ''}, {p.nb_relances} relance(s))")
    if raisons:
        exc = EnvoiRefuse(f"envoi de {b.id_relance} refuse : " + " ; ".join(raisons))
        journal.ecrire(ACTEUR_SYSTEME, "envoi_refuse", b.id_relance,
                       {"operation": "envoyer", "raison": str(exc)[:500], "type": "EnvoiRefuse"},
                       horodatage=maintenant)
        raise exc


def _adresses_de(champ: str) -> list[str]:
    """Adresses (minuscules) d'un champ libre : `a@x.fr`, `Nom <a@x.fr>`, `a@x.fr; b@y.fr`."""
    trouvees = []
    for _, spec in email.utils.getaddresses([(champ or "").replace(";", ",")]):
        spec = spec.strip().lower()
        if "@" in spec:
            trouvees.append(spec)
    return trouvees


_BOITES_GMAIL = frozenset({"gmail.com", "googlemail.com"})


def _domaine_comparable(domaine: str) -> str:
    """Domaine en forme normalisee (`routage.normaliser_domaine` : casse, point final) ; un domaine
    accentue est ramene a sa forme punycode (`société.fr` -> `xn--socit-esab.fr`). Sens d'erreur sans
    danger : le codec IDNA 2003 peut replier deux domaines distincts, ce qui REFUSE davantage."""
    brut = (domaine or "").strip().lower().rstrip(".")
    for essai in (brut, _punycode(brut)):
        normal = routage.normaliser_domaine(essai)
        if normal:
            return normal
    return brut


def _punycode(domaine: str) -> str:
    try:
        return domaine.encode("idna").decode("ascii")
    except UnicodeError:
        return domaine


def _boite_comparable(adresse: str) -> str | None:
    """Cle de la BOITE : `routage._cle_boite` (casse, point final, suffixe +tag) ; pour Gmail, les
    points de la partie locale sont sans effet et `googlemail.com` est `gmail.com`."""
    cle = routage._cle_boite(adresse)
    if cle is None:
        return None
    locale, domaine = cle.split("@")
    domaine = _domaine_comparable(domaine)
    if domaine in _BOITES_GMAIL:
        locale, domaine = locale.replace(".", ""), "gmail.com"
    return f"{locale}@{domaine}"


def _controle_adresse_test(adresse_test: str, clients: Iterable[tuple[str, str]],
                           domaines_declares: Iterable[tuple[str, str]] = ()) -> None:
    """Refuse (`ConfigInvalide`) une adresse de test qui est la boite d'un client ou dont le domaine
    est celui d'un client (ou un de ses sous-domaines) : un « envoi de test » partirait alors chez
    lui. `clients` : paires (adresse, origine). Tout est compare en forme NORMALISEE (routage) : casse,
    point final, punycode, suffixe +tag, boites Gmail equivalentes. Le domaine d'un fournisseur
    GRAND PUBLIC (gmail.com...) ne prouve rien et n'est pas compare : seule la boite l'est."""
    boite_test = _boite_comparable(adresse_test)
    domaine_test = _domaine_comparable(adresse_test.rpartition("@")[2])
    commun = routage.est_domaine_grand_public(domaine_test)
    conseil = f"Choisir une adresse sans client (par defaut {ADRESSE_TEST_DEFAUT})"

    def dans(domaine_client: str) -> bool:
        return bool(domaine_client) and (domaine_test == domaine_client or domaine_test.endswith("." + domaine_client))

    for adresse, origine in clients:
        for a in _adresses_de(adresse):
            if boite_test is not None and _boite_comparable(a) == boite_test:
                raise ConfigInvalide(
                    f"adresse_test {adresse_test!r} est l'adresse d'un client ({affichable(origine)}) : un essai "
                    f"lui ecrirait, alors que le mode test promet de ne JAMAIS ecrire a un client. {conseil}"
                )
            if not commun and dans(_domaine_comparable(a.rpartition("@")[2])):
                raise ConfigInvalide(
                    f"adresse_test {adresse_test!r} est dans le domaine d'un client ({affichable(origine)}, "
                    f"{domaine_test}) : un essai risquerait d'atterrir chez lui. {conseil}"
                )
    for domaine, origine in domaines_declares:
        if not commun and dans(_domaine_comparable(domaine)):
            raise ConfigInvalide(
                f"adresse_test {adresse_test!r} est dans le domaine declare d'un client ({affichable(origine)}, "
                f"{domaine_test}). {conseil}"
            )


def verifier_adresse_test(adresse_test: str, dossiers: Mapping[str, Dossier]) -> None:
    """`adresse_test` confrontee aux contacts, relais et domaines declares de TOUS les dossiers
    (`ConfigInvalide` si elle en est une ou s'y trouve). Appelee par `executer_cycle` en mode test,
    avant toute ecriture."""
    clients = [(champ, f"dossier {code}") for code, d in dossiers.items()
               for champ in (d.email_contact, d.email_relais) if champ]
    declares = [(dom, f"dossier {code}") for code, d in dossiers.items() for dom in d.domaines]
    _controle_adresse_test(adresse_test, clients, declares)


class ExpediteurTest:
    """Expediteur du mode test : enveloppe un expediteur reel et lui remet une COPIE du
    brouillon dont l'objet est prefixe `[TEST] `, le destinataire remplace par
    `adresse_test`, et le corps precede de « [TEST] Destinataire d'origine : <adresse> ».

    Le brouillon stocke en base n'est PAS modifie (la limite hebdomadaire continue de
    s'appliquer au vrai destinataire). Garantit qu'un essai n'ecrit jamais a un tiers,
    meme le jour ou un vrai connecteur existe.
    """

    def __init__(self, reel: envoi_mod.Expediteur, adresse_test: str = ADRESSE_TEST_DEFAUT) -> None:
        if not isinstance(adresse_test, str) or not _ADRESSE_TEST.fullmatch(adresse_test):
            raise ErreurCycle(f"adresse de test invalide : {affichable(repr(adresse_test))}")
        self.reel = reel
        self.adresse_test = adresse_test

    def envoyer(self, brouillon: Brouillon) -> str:
        origine = " ".join(str(brouillon.destinataire).split())
        try:                                       # defense en profondeur, rien n'est encore parti
            _controle_adresse_test(self.adresse_test, [(brouillon.destinataire, "destinataire du brouillon")])
        except ConfigInvalide as exc:
            raise envoi_mod.ErreurEnvoiCertaine(str(exc)) from None
        copie = dataclasses.replace(
            brouillon,
            objet=PREFIXE_TEST + brouillon.objet,
            destinataire=self.adresse_test,
            corps=f"{PREFIXE_TEST}Destinataire d'origine : {origine}\n\n{brouillon.corps}",
        )
        return self.reel.envoyer(copie)


def envoyer_relance(
    instance: Instance | Path | str,
    id_relance: str,
    *,
    maintenant: dt.datetime,
    expediteur: envoi_mod.Expediteur | None = None,
    creneau_ok: Callable[[dt.datetime], bool] | None = None,
    panne: Panne | None = None,
    mode_test: bool = False,
    par: str | None = None,
) -> Brouillon:
    """Emet un brouillon VALIDEE (via `envoi.envoyer`, unique chemin d'emission),
    puis applique `RELANCE_ENVOYEE` aux pieces par le mecanisme idempotent de
    l'etape 4. Un crash entre les deux est rattrape par le cycle suivant.

    `par` (optionnel) : la personne qui DECLENCHE l'envoi, distincte de celle qui a valide.
    Elle doit figurer dans `validateurs.txt` ou `responsables.txt` (`ValidateurRefuse`
    sinon, avant tout effet) et est journalisee dans `envoye_par` sur les entrees de la
    tentative d'envoi (`envoi_demarre`, `envoi_effectue`...). Sans `par`, comportement
    inchange et `envoye_par: null`.

    Mode test : `config.json` peut y couper deux garde-fous, journalises (`garde_fous_coupes`)
    et affiches par `garde_fous_mode_test` : `creneau_envoi_libre` (defaut vrai) et
    `limite_hebdomadaire` (defaut vrai ; faux la desactive). Hors mode test ils ne jouent jamais.

    En mode test (`mode_test=True` ou `config.json`), l'expediteur, y compris un
    expediteur passe en argument, est enveloppe dans `ExpediteurTest` : rien n'est
    jamais adresse au vrai destinataire. Une configuration invalide refuse l'envoi.
    """
    inst = Instance.de(instance)
    config = charger_config(inst)
    en_mode_test = bool(mode_test) or config["mode_test"]
    envoyeur = exiger_validateur(inst, par) if par is not None else None
    creneau_libre, limite_hebdomadaire = reglages_envoi(config, en_mode_test)
    with _ouvrir(inst, ecriture=True, mode="test" if en_mode_test else "reel") as (depot, journal):
        b = depot.charger_brouillon(id_relance)
        if b is not None and b.statut is StatutBrouillon.VALIDEE:
            _controler_avant_envoi(depot, journal, b, maintenant, limite_hebdomadaire=limite_hebdomadaire)
        exp = expediteur if expediteur is not None else envoi_mod.ExpediteurDossier(inst.outbox)
        if en_mode_test:
            _controle_adresse_test(config["adresse_test"], [
                (x.destinataire, f"brouillon {x.id_relance}") for x in depot.lister_brouillons()])
            exp = ExpediteurTest(exp, config["adresse_test"])
        if creneau_libre and creneau_ok is None:
            creneau_ok = lambda _instant: True   # noqa: E731 - mode test : creneau libre, annonce
        coupes = garde_fous_mode_test(inst, mode_test) if en_mode_test else ()
        if coupes:
            journal.ecrire(ACTEUR_SYSTEME, "garde_fous_coupes", id_relance, {
                "garde_fous": list(coupes), "envoye_par": envoyeur,
            }, horodatage=maintenant)
        kw_limite = {} if limite_hebdomadaire else {"limite_hebdomadaire": False}
        envoi_mod.envoyer(depot, _JournalEnvoyePar(journal, envoyeur), exp, id_relance,
                          maintenant=maintenant, creneau_ok=creneau_ok, **kw_limite)
        if en_mode_test and b is not None:
            journal.ecrire(ACTEUR_SYSTEME, "envoi_mode_test", id_relance, {
                "destinataire_reel": b.destinataire, "adresse_test": config["adresse_test"],
            }, horodatage=maintenant)
        _panne(panne, "apres_envoi")
        _, anomalies = repercuter_envois(depot, journal, maintenant)
        if anomalies:
            raise ErreurCycle("message emis, mais repercussion refusee : " + " ; ".join(anomalies))
        envoye = depot.charger_brouillon(id_relance)
        assert envoye is not None
        return envoye


def trancher_envoi(instance: Instance | Path | str, id_relance: str, par: str, *, parti: bool,
                   maintenant: dt.datetime, mode_test: bool = False) -> Brouillon:
    """Tranche un envoi incertain. Operation de RESPONSABLE (`responsables.txt`, A14) ; en mode
    test sans `responsables.txt`, repli sur les validateurs (journal `repli_roles_mode_test`)."""
    inst = Instance.de(instance)
    _exiger_mode_instance(inst, mode_test)
    nom, repli = _nom_responsable(inst, par, mode_test)
    with _ouvrir(inst, ecriture=True, mode=mode_demande(inst, mode_test)) as (depot, journal):
        if repli:
            _noter_repli_roles(journal, nom, "trancher", maintenant)
        tranche = envoi_mod.trancher_envoi_incertain(
            depot, journal, id_relance, nom, parti=parti, maintenant=maintenant,
        )
        if parti:
            repercuter_envois(depot, journal, maintenant)
        return tranche


def lister_file(instance: Instance | Path | str) -> list[DecisionRoutage]:
    with _ouvrir(Instance.de(instance)) as (depot, _):
        return depot.file_humaine(resolue=False)


def _piece(depot: Depot, dossier: str, reference: str) -> PieceAttendue:
    trouvees = [p for p in depot.charger_pieces(dossier=dossier) if p.reference == reference]
    if len(trouvees) != 1:
        raise ErreurCycle(f"piece attendue introuvable : {dossier}/{reference}")
    return trouvees[0]


def rattacher_piece(
    instance: Instance | Path | str,
    par: str,
    *,
    maintenant: dt.datetime,
    message_id: str | None = None,
    empreinte: str | None = None,
    dossier: str | None = None,
    reference: str | None = None,
    id_piece: str | None = None,
    forcer: bool = False,
    motif: str = "",
    mode_test: bool = False,
) -> PieceAttendue:
    """Confirme un rattachement (`PIECE_RATTACHEE` avec `confirme_par`).

    Avec `message_id` (+ `empreinte`, prefixe accepte s'il est unique) : confirme
    une entree de la file humaine (proposition PROPOSEE, ou NON_ROUTEE si
    `dossier` et `reference` sont donnes) et la marque resolue. Sans : rattache
    directement `id_piece` a `dossier`/`reference` (piece justifiee par le moteur).

    CA-03 (jamais de rattachement inter-dossiers) : si la decision de routage nomme un
    dossier et que `dossier` en designe un AUTRE, le rattachement est REFUSE (une faute de
    frappe donnerait la piece d'un client au dossier d'un autre). Seul `forcer=True` AVEC un
    `motif` non vide le permet ; il est alors journalise (`rattachement_force` : acteur,
    dossier d'origine, dossier cible, motif). Le refus est journalise lui aussi
    (`rattachement_refuse`). Sans decision de routage (`message_id` absent), il n'y a pas
    de dossier d'origine a comparer.

    Une `id_piece` deja rattachee a une piece d'un AUTRE dossier est refusee, `forcer` compris
    (`rattachement_refuse`) : un fichier n'appartient qu'a un seul dossier.

    `forcer=True` est reserve au RESPONSABLE (`responsables.txt`, cahier v1.1 section 7, regle 7) :
    le cycle l'exige lui-meme, pas seulement l'ecran. Verifie AVANT toute ecriture et AVANT le
    motif : un simple validateur est refuse (`ValidateurRefuse`), sans effet ni entree
    `rattachement_force`. Memes regles que `arbitrer_piece` : `responsables.txt` absent = refus,
    sauf en mode test (`mode_test` ou `config.json`) ou l'on se rabat, avec avertissement et
    journal `repli_roles_mode_test`, sur les validateurs. Un rattachement normal reste ouvert
    aux validateurs.
    """
    inst = Instance.de(instance)
    repli = False
    if forcer:
        _exiger_mode_instance(inst, mode_test)
        nom, repli = _nom_responsable(inst, par, mode_test)
    else:
        nom = exiger_validateur(inst, par)
    aujourdhui = _jour_local(maintenant)
    with _ouvrir(inst, ecriture=True, mode=mode_demande(inst, mode_test)) as (depot, journal):
        notes = _Notes(journal, maintenant)
        entree: DecisionRoutage | None = None
        force_depuis: str | None = None
        details_garde: dict[str, Any] = {}
        if repli:
            _noter_repli_roles(journal, nom, "rattacher --forcer", maintenant)
        if message_id is not None:
            candidates = [
                d for d in depot.file_humaine(resolue=False)
                if d.message_id == message_id and d.empreinte.startswith(empreinte or "")
            ]
            if len(candidates) != 1:
                raise ErreurCycle(
                    f"{len(candidates)} entree(s) non resolue(s) pour {message_id} : preciser --empreinte"
                )
            entree = candidates[0]
            if entree.statut is not StatutRoutage.PROPOSEE and not (dossier and reference):
                raise ErreurCycle("entree NON_ROUTEE : --dossier et --reference sont obligatoires")
            if entree.dossier and dossier and dossier != entree.dossier:
                details_garde = {"message_id": entree.message_id, "dossier_origine": entree.dossier,
                                 "dossier_cible": dossier, "reference": reference or "",
                                 "fichier": entree.nom_fichier}
                if not forcer:
                    journal.ecrire(nom, "rattachement_refuse", entree.message_id, {
                        **details_garde, "raison": "dossier different de la decision de routage"},
                        horodatage=maintenant)
                    raise ErreurCycle(
                        f"rattachement refuse : la decision de routage designe le dossier "
                        f"{affichable(entree.dossier)}, pas {affichable(dossier)} (une piece ne passe "
                        "jamais d'un client a un autre par megarde). Corriger --dossier, ou, si c'est "
                        "voulu, utiliser --forcer avec --motif"
                    )
                if not isinstance(motif, str) or not motif.strip():
                    raise ErreurCycle("--forcer exige un --motif non vide (il est journalise)")
                force_depuis = entree.dossier
                details_garde["motif"] = motif.strip()
            dossier = dossier or entree.dossier
            reference = reference or entree.reference_operation
            id_piece = id_piece or f"{entree.nom_fichier}#{entree.empreinte[:12]}"
        if not (dossier and reference and id_piece):
            raise ErreurCycle("dossier, reference et piece sont obligatoires")
        with _transaction(depot, notes):
            # Un fichier (id_piece) n'appartient qu'a UN dossier, avec ou sans decision de routage,
            # `forcer` compris : il n'existe pas de detachement. Le proprietaire est lu dans
            # `pieces_rattachees` de toutes les pieces attendues.
            proprietaire = next((p2 for p2 in depot.charger_pieces()
                                 if p2.dossier != dossier and id_piece.strip() in p2.pieces_rattachees), None)
            if proprietaire is not None:
                journal.ecrire(nom, "rattachement_refuse", id_piece.strip(), {
                    "raison": "id_piece deja rattachee a un autre dossier",
                    "dossier_proprietaire": proprietaire.dossier, "dossier_cible": dossier,
                    "reference_proprietaire": proprietaire.reference, "forcer": bool(forcer),
                }, horodatage=maintenant)
                raise ErreurCycle(
                    f"rattachement refuse : la piece {affichable(id_piece.strip())!r} est deja rattachee au "
                    f"dossier {affichable(proprietaire.dossier)} (reference {affichable(proprietaire.reference)}) "
                    f"et ne peut pas l'etre au dossier {affichable(dossier)} : un fichier n'appartient qu'a un "
                    "dossier, meme avec --forcer (aucun detachement)"
                )
            piece = _piece(depot, dossier, reference)
            nouvelle = _transition(piece, Evenement.PIECE_RATTACHEE, aujourdhui, notes, acteur=nom,
                                   id_piece=id_piece, confirme_par=nom)
            if nouvelle.bloquee and nouvelle.motif_blocage.startswith(PREFIXE_MOTIF_CYCLE):
                nouvelle = _transition(nouvelle, Evenement.BLOCAGE_LEVE, aujourdhui, notes, acteur=nom)
            depot.sauver_pieces([nouvelle])
            if entree is not None:
                depot.resoudre_file(entree.message_id, entree.empreinte, nom,
                                    f"rattache a {dossier}/{reference}", maintenant=maintenant)
            if force_depuis is not None:
                notes.noter("rattachement_force", _objet(nouvelle), {
                    **details_garde, "par": nom, "id_piece": id_piece}, acteur=nom)
            notes.noter("rattachement_confirme", _objet(nouvelle), {
                "par": nom, "id_piece": id_piece,
                "message_id": entree.message_id if entree else "",
            }, acteur=nom)
        return nouvelle


def controler_piece(instance: Instance | Path | str, dossier: str, reference: str, par: str, *,
                    conforme: bool, maintenant: dt.datetime, motif: str = "",
                    mode_test: bool = False) -> PieceAttendue:
    inst = Instance.de(instance)
    nom = exiger_validateur(inst, par)
    aujourdhui = _jour_local(maintenant)
    with _ouvrir(inst, ecriture=True, mode=mode_demande(inst, mode_test)) as (depot, journal):
        notes = _Notes(journal, maintenant)
        with _transaction(depot, notes):
            piece = _piece(depot, dossier, reference)
            if conforme:
                nouvelle = _transition(piece, Evenement.CONTROLE_CONFORME, aujourdhui, notes,
                                       acteur=nom, controle_par=nom)
            else:
                nouvelle = _transition(piece, Evenement.CONTROLE_NON_CONFORME, aujourdhui, notes,
                                       acteur=nom, controle_par=nom, motif=motif)
            depot.sauver_pieces([nouvelle])
        return nouvelle


# ---------------------------------------------------------------------------
# Suivi humain : promesse, arbitrage, blocage (meme garde que les autres decisions)
# ---------------------------------------------------------------------------

CLE_DEBLOCAGE_HUMAIN = "deblocage-humain:"


def _cle_deblocage_humain(piece: PieceAttendue) -> str:
    return f"{CLE_DEBLOCAGE_HUMAIN}{piece.dossier}/{piece.reference}"


def _decision_sur_piece(
    instance: Instance | Path | str,
    par: str,
    dossier: str,
    reference: str,
    maintenant: dt.datetime,
    evenement: Evenement,
    deja_fait: Callable[[PieceAttendue], bool],
    action: str,
    details: Mapping[str, Any],
    *,
    mode_test: bool = False,
    **contexte: Any,
) -> PieceAttendue:
    """Squelette commun : validateur declare, piece existante, transition `etats`,
    COMMIT puis audit. Idempotent : si la piece est deja dans l'etat vise
    (`deja_fait`), rien n'est ecrit (ni depot ni journal) et la piece est renvoyee.
    Une transition refusee leve `etats.TransitionInterdite` (message de `etats`)."""
    inst = Instance.de(instance)
    nom = exiger_validateur(inst, par)
    if not isinstance(maintenant, dt.datetime) or maintenant.utcoffset() is None:
        raise ValueError("maintenant doit etre un datetime avec fuseau")
    aujourdhui = _jour_local(maintenant)
    with _ouvrir(inst, ecriture=True, mode=mode_demande(inst, mode_test)) as (depot, journal):
        notes = _Notes(journal, maintenant)
        with _transaction(depot, notes):
            piece = _piece(depot, dossier, reference)
            if deja_fait(piece):
                return piece
            nouvelle = _transition(piece, evenement, aujourdhui, notes, acteur=nom, **contexte)
            depot.sauver_pieces([nouvelle])
            if evenement is Evenement.BLOCAGE_LEVE:
                depot.marquer_vu(_cle_deblocage_humain(piece))
            notes.noter(action, _objet(nouvelle), {"par": nom, **details}, acteur=nom)
        return nouvelle


def saisir_promesse(instance: Instance | Path | str, dossier: str, reference: str, par: str, *,
                    date_promesse: dt.date, maintenant: dt.datetime, mode_test: bool = False) -> PieceAttendue:
    """`PROMESSE_SAISIE` : DEMANDEE -> PROMISE (date dans [aujourdhui, +15 j], garde `etats`)."""
    if isinstance(date_promesse, dt.datetime) or not isinstance(date_promesse, dt.date):
        raise ErreurCycle("la date promise doit etre une date AAAA-MM-JJ")
    return _decision_sur_piece(
        instance, par, dossier, reference, maintenant, Evenement.PROMESSE_SAISIE,
        lambda p: p.etat is EtatPiece.PROMISE and p.date_promesse == date_promesse,
        "promesse_saisie", {"date_promesse": date_promesse}, mode_test=mode_test,
        date_promesse=date_promesse,
    )


def arbitrer_piece(instance: Instance | Path | str, dossier: str, reference: str, par: str, *,
                   motif: str, maintenant: dt.datetime, mode_test: bool = False) -> PieceAttendue:
    """`ARBITRAGE_CLASSEMENT` : ESCALADEE -> CLOSE_SANS_SUITE, motif obligatoire.

    Operation de RESPONSABLE (`responsables.txt`, A14) : classer sans suite ce que le cahier
    reserve au responsable de portefeuille. En mode test sans `responsables.txt`, repli sur les
    validateurs (journal `repli_roles_mode_test`)."""
    if not isinstance(motif, str) or not motif.strip():
        raise ErreurCycle("arbitrer : --motif obligatoire et non vide")
    _exiger_mode_instance(Instance.de(instance), mode_test)
    nom, repli = _nom_responsable(Instance.de(instance), par, mode_test)
    if repli:
        with _ouvrir(Instance.de(instance), ecriture=True,
                     mode=mode_demande(instance, mode_test)) as (_, journal):
            _noter_repli_roles(journal, nom, "arbitrer", maintenant)
    return _decision_sur_piece(
        instance, par, dossier, reference, maintenant, Evenement.ARBITRAGE_CLASSEMENT,
        lambda p: p.etat is EtatPiece.CLOSE_SANS_SUITE,
        "arbitrage", {"motif": motif.strip()}, mode_test=mode_test, arbitre_par=nom, motif=motif.strip(),
    )


def bloquer_piece(instance: Instance | Path | str, dossier: str, reference: str, par: str, *,
                  motif: str, maintenant: dt.datetime, mode_test: bool = False) -> PieceAttendue:
    """`BLOCAGE_SIGNALE` : la piece n'est plus relancee tant qu'un humain ne la debloque pas."""
    if not isinstance(motif, str) or not motif.strip():
        raise ErreurCycle("bloquer : --motif obligatoire et non vide")
    motif = motif.strip()
    if motif.startswith(PREFIXE_MOTIF_CYCLE.strip()):
        raise ErreurCycle(f"bloquer : le prefixe {PREFIXE_MOTIF_CYCLE.strip()!r} est reserve au cycle")
    return _decision_sur_piece(
        instance, par, dossier, reference, maintenant, Evenement.BLOCAGE_SIGNALE,
        lambda p: p.bloquee and p.motif_blocage == motif,
        "blocage", {"motif": motif}, mode_test=mode_test, motif=motif,
    )


def debloquer_piece(instance: Instance | Path | str, dossier: str, reference: str, par: str, *,
                    motif: str, maintenant: dt.datetime, mode_test: bool = False) -> PieceAttendue:
    """`BLOCAGE_LEVE`. Leve aussi un blocage pose par le cycle : la decision humaine est
    memorisee et le cycle ne rebloque plus cette piece pour le meme constat du moteur."""
    if not isinstance(motif, str) or not motif.strip():
        raise ErreurCycle("debloquer : --motif obligatoire et non vide (trace d'audit)")
    return _decision_sur_piece(
        instance, par, dossier, reference, maintenant, Evenement.BLOCAGE_LEVE,
        lambda p: not p.bloquee,
        "deblocage", {"motif": motif.strip()}, mode_test=mode_test,
    )


@dataclass(frozen=True)
class LigneSuivi:
    piece: PieceAttendue
    echeance: dt.date | None      # prochaine date ou le cycle agira de lui-meme
    action: str                   # ce qui se passera, ou ce qu'un humain doit faire


@dataclass(frozen=True)
class SuiviPieces:
    aujourdhui: dt.date
    lignes: tuple[LigneSuivi, ...]
    periodes: tuple[tuple[str, str, EtatPeriode], ...]

    def texte(self) -> str:
        sortie = [f"Pieces attendues au {self.aujourdhui.isoformat()} : {len(self.lignes)}"]
        for l in self.lignes:
            p = l.piece
            sortie.append(
                f"  {affichable(p.dossier):14s} {affichable(p.reference):12s} {p.periode} "
                f"{p.etat.value:16s} relances={p.nb_relances} "
                f"echeance={_iso(l.echeance) or '-':10s} "
                f"{p.montant:>10} {affichable(p.devise)}  {affichable(l.action)}"
            )
        sortie.append(f"Periodes (etat calcule) : {len(self.periodes)}")
        sortie += [f"  {affichable(d):14s} {affichable(per)} {e.value}" for d, per, e in self.periodes]
        return "\n".join(sortie) + "\n"


HORIZON_ECHEANCE_JOURS = 120


def _echeances(pieces: list[PieceAttendue], depot: Depot,
               aujourdhui: dt.date) -> dict[tuple[str, str], tuple[dt.date, str]]:
    """Prochaine action automatique, calculee en interrogeant `cadence.planifier` jour
    apres jour : aucune regle de calendrier n'est dupliquee ici (c'est une double regle
    qui avait rendu l'escalade inatteignable). Le destinataire d'une piece est celui
    de son dernier brouillon (garde-fou hebdomadaire) ; a defaut un destinataire propre
    au dossier."""
    destinataires: dict[str, str] = {}
    for b in depot.lister_brouillons():                    # tri (cree_le, id) : le dernier gagne
        for d in b.dossiers:
            destinataires[d] = b.destinataire
    dossiers = {
        p.dossier: Dossier(p.dossier, p.dossier,
                           destinataires.get(p.dossier, f"{p.dossier.lower()}@destinataire.invalid"), "")
        for p in pieces
    }
    historique = depot.historique_envois()
    par_ref = {p.reference: p for p in pieces}
    resultat: dict[tuple[str, str], tuple[dt.date, str]] = {}
    restantes = {p.reference for p in pieces if p.etat not in ETATS_TERMINAUX}
    for n in range(HORIZON_ECHEANCE_JOURS + 1):
        if not restantes:
            break
        jour = aujourdhui + dt.timedelta(days=n)
        plan = planifier([par_ref[r] for r in sorted(restantes)], dossiers, historique, jour)
        for ref, evenement in plan.transitions:
            p = par_ref[ref]
            libelle = ("escalade au responsable" if evenement is Evenement.DELAI_DEPASSE
                       else "promesse depassee : retour en DEMANDEE")
            resultat[(p.dossier, ref)] = (jour, libelle)
            restantes.discard(ref)
        for r in plan.relances:
            for ref in r.references:
                p = par_ref[ref]
                resultat[(p.dossier, ref)] = (jour, f"relance niveau {p.nb_relances + 1}")
                restantes.discard(ref)
        restantes -= set(plan.bloquees)
    return resultat


def _action_humaine(p: PieceAttendue) -> str:
    if p.etat in ETATS_TERMINAUX:
        return "termine"
    if p.bloquee:
        return f"bloquee ({p.motif_blocage}) : debloquer ou rattacher"
    if p.etat is EtatPiece.RECUE:
        return "controle a faire (controler)"
    if p.etat is EtatPiece.ESCALADEE:
        return "arbitrage du responsable (arbitrer, rattacher)"
    return "aucune echeance dans l'horizon"


def suivi_pieces(instance: Instance | Path | str, aujourdhui: dt.date, *,
                 dossier: str | None = None, etat: str | EtatPiece | None = None) -> SuiviPieces:
    """Lecture seule (aucun validateur, aucune ecriture d'audit, aucun etat modifie).

    Tri : (dossier, date_operation, reference), celui du depot. L'etat de chaque
    periode est calcule par `etats.etat_periode` sur TOUTES les pieces de la periode,
    quel que soit le filtre `etat`.
    """
    if isinstance(aujourdhui, dt.datetime) or not isinstance(aujourdhui, dt.date):
        raise TypeError("aujourdhui doit etre une date")
    try:
        filtre = None if etat is None else EtatPiece(etat)
    except ValueError:
        raise ErreurCycle(
            f"etat inconnu {etat!r} (attendu : {', '.join(e.value for e in EtatPiece)})"
        ) from None
    inst = Instance.de(instance)
    if not inst.depot.exists():
        return SuiviPieces(aujourdhui, (), ())
    with Depot(inst.depot) as depot:
        toutes = depot.charger_pieces(dossier=dossier)
        echeances = _echeances(toutes, depot, aujourdhui)
        en_attente = {
            (d, ref): b for b in depot.lister_brouillons() if b.statut in STATUTS_EN_ATTENTE
            for d in b.dossiers for ref in b.references
        }
    lignes = []
    for p in toutes:
        if filtre is not None and p.etat is not filtre:
            continue
        quand, action = echeances.get((p.dossier, p.reference), (None, _action_humaine(p)))
        b = en_attente.get((p.dossier, p.reference))
        if b is not None and p.etat not in ETATS_TERMINAUX and not p.bloquee:
            action = f"brouillon {b.id_relance} {b.statut.value} : valider puis envoyer"
        lignes.append(LigneSuivi(p, quand, action))
    groupes: dict[tuple[str, str], list[PieceAttendue]] = {}
    for p in toutes:
        groupes.setdefault((p.dossier, p.periode), []).append(p)
    periodes = tuple((d, per, etats.etat_periode(ps)) for (d, per), ps in sorted(groupes.items()))
    return SuiviPieces(aujourdhui, tuple(lignes), periodes)


def verifier_audit(instance: Instance | Path | str) -> ResultatVerification:
    inst = Instance.de(instance)
    if not inst.audit.exists():
        return ResultatVerification(False, 0, None, "fichier du journal absent")
    return JournalAudit(inst.audit).verifier()


@dataclass(frozen=True)
class LigneControleEnvoi:
    id_relance: str
    destinataire: str
    valide_par: str
    envoye_le: str
    envoye_par: str
    nature: str            # "effectue" (envoi_effectue) ou "confirme" (envoi_confirme_par_humain)
    anomalies: tuple[str, ...]

    @property
    def verdict(self) -> str:
        return "OK" if not self.anomalies else "ANOMALIE : " + " ; ".join(self.anomalies)


@dataclass(frozen=True)
class ControleEnvois:
    chaine_ok: bool
    raison_chaine: str
    nb_entrees: int
    lignes: tuple[LigneControleEnvoi, ...]
    # Ecarts qui ne se rattachent a aucune ligne d'envoi du journal : .eml de l'outbox sans trace,
    # brouillon ENVOYEE en base sans entree au journal...
    ecarts: tuple[str, ...] = ()

    @property
    def nb_anomalies(self) -> int:
        return (sum(1 for l in self.lignes if l.anomalies) + len(self.ecarts)
                + (0 if self.chaine_ok else 1))

    @property
    def ok(self) -> bool:
        return self.nb_anomalies == 0

    def texte(self) -> str:
        sortie = [f"{'brouillon':18s} {'valide par':16s} {'envoye le':26s} {'envoye par':14s} {'nature':9s} verdict"]
        for l in self.lignes:
            sortie.append(f"{l.id_relance:18s} {l.valide_par or '-':16s} {l.envoye_le:26s} "
                          f"{l.envoye_par or '-':14s} {l.nature:9s} {l.verdict}")
        sortie += [f"ECART : {affichable(e)}" for e in self.ecarts]
        sortie.append(f"chaine du journal : {'saine' if self.chaine_ok else 'NON SAINE'} "
                      f"({self.nb_entrees} entree(s)) ; {self.raison_chaine}")
        sortie.append(f"{len(self.lignes)} envoi(s) controle(s), {self.nb_anomalies} anomalie(s)")
        return "\n".join(sortie) + "\n"


def controler_envois(instance: Instance | Path | str) -> ControleEnvois:
    """Recette de CA-06, LECTURE SEULE. Pour CHAQUE envoi du journal (`envoi_effectue`, et
    `envoi_confirme_par_humain` quand un humain a tranche un envoi incertain) :

    - une entree `valide` ANTERIEURE du MEME brouillon, par un nom de `validateurs.txt` ou
      `responsables.txt`, avec le MEME destinataire, et `valide_par` de l'envoi identique ;
    - aucun `rejete` anterieur du meme brouillon, aucun envoi en double ;
    - le brouillon existe EN BASE, au statut ENVOYEE, avec le meme `valide_par` (une paire
      `valide` + `envoi` forgee mais correctement chainee, pour un brouillon absent ou non
      envoye, est une anomalie) ;
    - `envoye_par` NOMINATIF (un `envoi_effectue` sans declencheur nomme est une anomalie) et
      declare.
    Et, dans l'autre sens : chaque `.eml` de `outbox/` doit avoir son entree d'envoi au journal
    (sinon « .eml sans trace » ; un envoi incertain non tranche est signale comme tel), et
    chaque brouillon ENVOYEE en base doit avoir ses entrees `valide` ET d'envoi.
    La chaine de hachage est verifiee d'abord : un journal non sain est une anomalie a part
    entiere (les lignes sont alors analysees quand meme, au mieux). Ne cree ni n'ecrit rien."""
    inst = Instance.de(instance)
    if not inst.audit.exists():
        return ControleEnvois(False, "fichier du journal absent", 0, ())
    journal = JournalAudit(inst.audit)
    verification = journal.verifier()
    try:
        entrees = journal.lire()
    except JournalCorrompu as exc:
        return ControleEnvois(False, f"{verification.raison} ; lecture impossible : {exc}", 0, ())
    autorises = {_cle_nom(n) for n in (*lire_validateurs(inst), *lire_responsables(inst))}
    en_base: dict[str, Brouillon] = {}
    if inst.depot.exists():
        with Depot(inst.depot) as depot:
            en_base = {b.id_relance: b for b in depot.lister_brouillons()}
    types_d_envoi = {"envoi_effectue": "effectue", "envoi_confirme_par_humain": "confirme"}
    # UNE passe sur le journal : envois, validations et rejets indexes par brouillon. Sans index,
    # chaque envoi balayait tout le journal (quadratique : 3 000 envois dans 66 000 entrees = 15 s).
    envois: list[Any] = []
    valides_par_objet: dict[str, list[Any]] = {}
    rejetes_par_objet: dict[str, list[Any]] = {}
    for x in entrees:
        if x.action in types_d_envoi:
            envois.append(x)
        elif x.action == "valide":
            valides_par_objet.setdefault(x.objet, []).append(x)
        elif x.action == "rejete":
            rejetes_par_objet.setdefault(x.objet, []).append(x)
    nombre = Counter(e.objet for e in envois)
    lignes = []
    for e in envois:
        anomalies: list[str] = []
        valides = [v for v in valides_par_objet.get(e.objet, ()) if v.seq < e.seq]
        valide_par = ""
        if not valides:
            anomalies.append("aucune entree `valide` anterieure pour ce brouillon")
        else:
            v = valides[-1]
            valide_par = normaliser_nom(str(v.details.get("par") or v.acteur))
            if _cle_nom(valide_par) not in autorises:
                anomalies.append(f"valide par {valide_par!r}, absent de validateurs.txt et responsables.txt")
            if _cle_nom(v.acteur) != _cle_nom(valide_par):
                anomalies.append(f"acteur de la validation ({v.acteur!r}) different de `par` ({valide_par!r})")
            if v.details.get("destinataire") != e.details.get("destinataire"):
                anomalies.append(f"destinataire valide {v.details.get('destinataire')!r}, "
                                 f"envoye a {e.details.get('destinataire')!r}")
            if _cle_nom(str(e.details.get("valide_par", ""))) != _cle_nom(valide_par):
                anomalies.append(f"`valide_par` de l'envoi ({e.details.get('valide_par')!r}) "
                                 f"different de la validation ({valide_par!r})")
        rejetes = [x for x in rejetes_par_objet.get(e.objet, ()) if x.seq < e.seq]
        if rejetes:
            anomalies.append(f"brouillon rejete (entree {rejetes[0].seq}) puis envoye")
        if nombre[e.objet] > 1:
            anomalies.append(f"brouillon envoye {nombre[e.objet]} fois")
        brouillon = en_base.get(e.objet)
        if brouillon is None:
            anomalies.append("brouillon absent de la base")
        elif brouillon.statut is not StatutBrouillon.ENVOYEE:
            anomalies.append(f"brouillon au statut {brouillon.statut.value} en base, pas envoyee")
        elif _cle_nom(brouillon.valide_par) != _cle_nom(str(e.details.get("valide_par", ""))):
            anomalies.append(f"`valide_par` en base ({brouillon.valide_par!r}) different du journal "
                             f"({e.details.get('valide_par')!r})")
        envoye_par = e.details.get("envoye_par")
        if e.action == "envoi_effectue" and (envoye_par is None or not str(envoye_par).strip()):
            anomalies.append("envoi non nominatif (`envoye_par` absent : qui l'a declenche ?)")
        elif envoye_par is not None and _cle_nom(str(envoye_par)) not in autorises:
            anomalies.append(f"envoye par {envoye_par!r}, absent de validateurs.txt et responsables.txt")
        lignes.append(LigneControleEnvoi(
            id_relance=e.objet, destinataire=str(e.details.get("destinataire", "")), valide_par=valide_par,
            envoye_le=e.horodatage, envoye_par=str(envoye_par) if envoye_par is not None else "",
            nature=types_d_envoi[e.action], anomalies=tuple(anomalies)))

    ecarts: list[str] = []
    traces = {e.objet for e in envois}
    for fichier in sorted(inst.outbox.glob("*.eml")):
        if fichier.stem in traces:
            continue
        b = en_base.get(fichier.stem)
        if b is not None and b.statut is StatutBrouillon.EN_COURS:
            ecarts.append(f"outbox/{fichier.name} : envoi incertain non tranche (brouillon EN_COURS), "
                          "a trancher par un responsable")
        else:
            ecarts.append(f"outbox/{fichier.name} : .eml sans trace dans le journal "
                          "(aucune entree d'envoi pour ce brouillon)")
    validees = set(valides_par_objet)
    for id_relance, b in sorted(en_base.items()):
        if b.statut is not StatutBrouillon.ENVOYEE:
            continue
        manque = [nom for nom, present in (("valide", id_relance in validees),
                                           ("envoi_effectue", id_relance in traces)) if not present]
        if manque:
            ecarts.append(f"brouillon {id_relance} ENVOYEE en base sans entree {' ni '.join(manque)} au journal")
    ecarts += [f"mode de l'instance : {p}" for p in controler_mode(inst)]
    return ControleEnvois(verification.ok, verification.raison, len(entrees), tuple(lignes), tuple(ecarts))


@dataclass(frozen=True)
class ReparationAudit:
    octets_retires: int
    fragment: str         # nom du fichier ou le fragment retire a ete sauvegarde, "" si rien


def reparer_audit(instance: Instance | Path | str, par: str, *,
                  maintenant: dt.datetime, mode_test: bool = False) -> ReparationAudit:
    """Retire une derniere ligne tronquee du journal (`JournalAudit.reparer_fin_tronquee`),
    puis journalise `journal_repare` (octets retires, fragment sauvegarde) au nom de `par`.

    Validateur declare obligatoire. Journal sain : rien n'est fait, rien n'est ecrit
    (idempotent). Journal altere AILLEURS qu'en fin : `audit.JournalCorrompu` remonte
    tel quel, sans rien modifier (reparer effacerait la preuve d'une falsification)."""
    inst = Instance.de(instance)
    _exiger_mode_instance(inst, mode_test)                   # avant tout, comme `_ouvrir`
    nom, repli = _nom_responsable(inst, par, mode_test)     # operation de RESPONSABLE (A14)
    if not isinstance(maintenant, dt.datetime) or maintenant.utcoffset() is None:
        raise ValueError("maintenant doit etre un datetime avec fuseau")
    journal = JournalAudit(inst.audit)
    motif = f"{inst.audit.name}.fragment-*"
    avant = {p.name for p in inst.racine.glob(motif)}
    octets = journal.reparer_fin_tronquee()
    if octets == 0:
        return ReparationAudit(0, "")
    if repli:
        _noter_repli_roles(journal, nom, "reparer-audit", maintenant)
    nouveaux = sorted(p.name for p in inst.racine.glob(motif) if p.name not in avant)
    fragment = nouveaux[-1] if nouveaux else ""
    journal.ecrire(nom, "journal_repare", inst.audit.name,
                   {"par": nom, "octets_retires": octets, "fragment": fragment},
                   horodatage=maintenant)
    return ReparationAudit(octets, fragment)


def exporter(instance: Instance | Path | str, chemin: Path | str) -> Path:
    with _ouvrir(Instance.de(instance)) as (depot, _):
        return depot.exporter_json(chemin)


def pieces(instance: Instance | Path | str) -> list[PieceAttendue]:
    with _ouvrir(Instance.de(instance)) as (depot, _):
        return depot.charger_pieces()


def brouillons(instance: Instance | Path | str,
               statut: StatutBrouillon | None = None) -> list[Brouillon]:
    with _ouvrir(Instance.de(instance)) as (depot, _):
        return depot.lister_brouillons(statut)


__all__ = [
    "CollisionReferences",
    "ConfigInvalide",
    "ControleEnvois",
    "EnvoiRefuse",
    "ErreurCycle",
    "ExpediteurTest",
    "FichierEnQuarantaine",
    "Instance",
    "IsolationIndisponible",
    "JournalNonSain",
    "LigneControleEnvoi",
    "LigneSuivi",
    "ModeInstanceIncompatible",
    "BaseRecreee",
    "ReparationAudit",
    "ResumeCycle",
    "STRATEGIES_ISOLATION",
    "SuiviPieces",
    "ValidateurRefuse",
    "affichable",
    "arbitrer_piece",
    "avertissement_roles",
    "bloquer_piece",
    "brouillons",
    "charger_config",
    "choisir_strategie_isolation",
    "collisions_de_reference",
    "contexte_file",
    "controler_envois",
    "controler_mode",
    "controler_piece",
    "debloquer_piece",
    "dossiers_sans_circuit",
    "envoyer_relance",
    "executer_cycle",
    "exiger_isolation",
    "exiger_journal_sain",
    "exiger_responsable",
    "exiger_validateur",
    "exporter",
    "garantir_fuseaux",
    "garde_fous_mode_test",
    "lire_entrees",
    "lire_responsables",
    "lire_validateurs",
    "lister_file",
    "mode_demande",
    "mode_instance",
    "normaliser_nom",
    "pieces",
    "rattacher_piece",
    "reglages_envoi",
    "rejeter_relance",
    "reparer_audit",
    "repercuter_envois",
    "saisir_promesse",
    "suivi_pieces",
    "trancher_envoi",
    "valider_relance",
    "verifier_adresse_test",
    "verifier_audit",
]
