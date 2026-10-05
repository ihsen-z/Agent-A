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

Le cycle ne valide JAMAIS rien lui-meme.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import envoi as envoi_mod
from . import etats, routage
from .audit import JournalAudit, ResultatVerification
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
    identifiant_relance,
)
from .moteur import Moteur
from .parseurs import lire_dossiers, lire_pieces, lire_releve
from .rapport import ecrire_suivi
from .relances import construire_brouillon

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


@contextmanager
def _ouvrir(instance: Instance) -> Iterator[tuple[Depot, JournalAudit]]:
    instance.preparer()
    depot = Depot(instance.depot)
    try:
        yield depot, JournalAudit(instance.audit)
    finally:
        depot.fermer()


def lire_validateurs(instance: Instance | Path | str) -> tuple[str, ...]:
    """Noms declares (lignes non vides, `#` = commentaire). Fichier absent : ()."""
    chemin = Instance.de(instance).validateurs
    if not chemin.exists():
        return ()
    noms = []
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        nom = ligne.strip()
        if nom and not nom.startswith("#"):
            noms.append(nom)
    return tuple(noms)


def exiger_validateur(instance: Instance | Path | str, nom: str | None) -> str:
    """Renvoie le nom tel que declare, ou leve `ValidateurRefuse`. Jamais d'acceptation par defaut."""
    inst = Instance.de(instance)
    declares = lire_validateurs(inst)
    if not declares:
        raise ValidateurRefuse(
            f"aucun validateur declare ({inst.validateurs} absent ou vide) : "
            "toute decision humaine est refusee tant que la liste n'est pas renseignee"
        )
    if not isinstance(nom, str) or not nom.strip():
        raise ValidateurRefuse("--par NOM est obligatoire")
    cherche = nom.strip().casefold()
    for declare in declares:
        if declare.casefold() == cherche:
            if not etats.est_acteur_humain(declare):
                raise ValidateurRefuse(f"{declare!r} est un acteur automatique, pas un validateur")
            return declare
    raise ValidateurRefuse(f"{nom.strip()!r} n'est pas dans {inst.validateurs.name}")


# ---------------------------------------------------------------------------
# Entrees
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entrees:
    operations: list[OperationBancaire]
    pieces: list[Piece]
    dossiers: dict[str, Dossier]
    referentiel: Referentiel


def lire_entrees(entree: Path | str) -> Entrees:
    """Memes fichiers que `scripts/lancer.py` ; `exclusions_dossier.csv` optionnel."""
    racine = Path(entree)
    return Entrees(
        operations=lire_releve(racine / "releve_bancaire.csv"),
        pieces=lire_pieces(racine / "pieces_recues.csv"),
        dossiers=lire_dossiers(racine / "dossiers.csv"),
        referentiel=Referentiel.depuis_csv(racine / "exclusions_dossier.csv"),
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
            "anomalies": list(self.anomalies),
        }

    def texte(self) -> str:
        d = self.en_dict()
        lignes = [f"Cycle du {self.aujourdhui.isoformat()}"]
        for cle, valeur in d.items():
            if cle in ("aujourdhui", "anomalies", "brouillons_du_jour", "reportees"):
                continue
            lignes.append(f"  {cle:28s}: {valeur}")
        for raison, n in d["reportees"].items():
            lignes.append(f"  reportees {raison:18s}: {n}")
        lignes.append(f"  brouillons du jour          : {len(self.brouillons_du_jour)}")
        lignes += [f"    {i}" for i in self.brouillons_du_jour]
        lignes.append(f"  anomalies                   : {len(self.anomalies)}")
        lignes += [f"    {a}" for a in self.anomalies]
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
) -> ResumeCycle:
    """Execute un cycle complet (voir la docstring du module).

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
    entrees = lire_entrees(entree)

    # 2. Collisions dans le releve : refus AVANT toute ecriture.
    collisions = collisions_de_reference(entrees.operations)
    if collisions:
        raise CollisionReferences(collisions)

    rapprochements = Moteur(entrees.referentiel).rapprocher(entrees.operations, entrees.pieces)
    resume = ResumeCycle(aujourdhui=aujourdhui, operations=len(rapprochements))

    with _ouvrir(inst) as (depot, journal):
        # 2 bis. Collisions entre le releve et le depot.
        collisions = collisions_de_reference(entrees.operations, depot.charger_pieces())
        if collisions:
            raise CollisionReferences(collisions)
        notes = _Notes(journal, maintenant)

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

        _etape_routage(depot, notes, inst, entrees.dossiers, aujourdhui, resume)
        _panne(panne, "apres_routage")

        pieces_avant = depot.charger_pieces()
        plan = planifier(pieces_avant, entrees.dossiers, depot.historique_envois(), aujourdhui)
        _etape_transitions(depot, notes, plan, pieces_avant, aujourdhui, resume)
        _panne(panne, "apres_transitions")

        _etape_brouillons(depot, notes, plan, pieces_avant, entrees.dossiers, aujourdhui, resume, panne)
        _panne(panne, "avant_sorties")

        ecrire_sorties(inst, depot, rapprochements, a_verifier, resume)
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


def _etape_routage(depot: Depot, notes: _Notes, inst: Instance, dossiers: Mapping[str, Dossier],
                   aujourdhui: dt.date, resume: ResumeCycle) -> None:
    """Etape 5 : routage des .eml ; rien n'est rattache sans confirmation humaine."""
    messages = routage.lire_dossier_eml(inst.entrant)
    adresses = routage.adresses_par_dossier(dossiers)
    resume.messages_lus = len(messages)
    for message in messages:
        attendues = depot.charger_pieces()
        deja_vus = {c for c in _cles_message(message) if depot.est_vu(c)}
        decisions = routage.router(
            message, adresses=adresses, attendues=attendues, deja_vus=deja_vus, aujourdhui=aujourdhui,
        )
        with _transaction(depot, notes):
            for d in decisions:
                if d.statut is StatutRoutage.DOUBLON:
                    resume.doublons += 1
                    continue
                if not depot.marquer_vu(routage.cle_idempotence(d.message_id, d.empreinte)):
                    resume.doublons += 1
                    continue
                depot.mettre_en_file(d)
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
        ecrivain.writerow(["" if v is None else v for v in ligne])
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
                   a_verifier: list[LigneAVerifier], resume: ResumeCycle) -> None:
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
        "reference_operation", "motif", "detail",
    ), ([
        d.message_id, d.empreinte, d.nom_fichier, d.statut.value, d.dossier, d.periode,
        d.reference_operation, d.motif.value if d.motif else "", d.detail,
    ] for d in depot.file_humaine(resolue=False)))

    _ecrire_csv(sortie / "escalades.csv", (
        "dossier", "reference", "periode", "date_operation", "montant", "devise", "libelle",
        "nb_relances", "date_premiere_demande", "date_derniere_relance", "bloquee", "motif_blocage",
    ), ([
        p.dossier, p.reference, p.periode, _iso(p.date_operation), str(p.montant), p.devise,
        p.libelle, p.nb_relances, _iso(p.date_premiere_demande), _iso(p.date_derniere_relance),
        int(p.bloquee), p.motif_blocage,
    ] for p in pieces if p.etat is EtatPiece.ESCALADEE))

    dossier_b = sortie / "brouillons"
    dossier_b.mkdir(exist_ok=True)
    for b in depot.lister_brouillons():
        (dossier_b / f"{b.id_relance}.txt").write_bytes(texte_brouillon(b).encode("utf-8"))

    (sortie / "resume.txt").write_bytes(resume.texte().encode("utf-8"))


# ---------------------------------------------------------------------------
# Decisions humaines (appelees par le CLI) : `par` borne a validateurs.txt
# ---------------------------------------------------------------------------


def valider_relance(instance: Instance | Path | str, id_relance: str, par: str, *,
                    maintenant: dt.datetime) -> Brouillon:
    inst = Instance.de(instance)
    nom = exiger_validateur(inst, par)
    with _ouvrir(inst) as (depot, journal):
        return envoi_mod.valider(depot, journal, id_relance, nom, maintenant=maintenant)


def rejeter_relance(instance: Instance | Path | str, id_relance: str, par: str, motif: str, *,
                    maintenant: dt.datetime) -> Brouillon:
    inst = Instance.de(instance)
    nom = exiger_validateur(inst, par)
    with _ouvrir(inst) as (depot, journal):
        return envoi_mod.rejeter(depot, journal, id_relance, nom, motif, maintenant=maintenant)


def _controler_avant_envoi(depot: Depot, journal: JournalAudit, b: Brouillon,
                           maintenant: dt.datetime) -> None:
    """Garde-fous que `envoi.envoyer` ne connait pas (leve `EnvoiRefuse`, journalise)."""
    jour = _jour_local(maintenant)
    raisons = []
    recents = [
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


def envoyer_relance(
    instance: Instance | Path | str,
    id_relance: str,
    *,
    maintenant: dt.datetime,
    expediteur: envoi_mod.Expediteur | None = None,
    creneau_ok: Callable[[dt.datetime], bool] | None = None,
    panne: Panne | None = None,
) -> Brouillon:
    """Emet un brouillon VALIDEE (via `envoi.envoyer`, unique chemin d'emission),
    puis applique `RELANCE_ENVOYEE` aux pieces par le mecanisme idempotent de
    l'etape 4. Un crash entre les deux est rattrape par le cycle suivant.
    """
    inst = Instance.de(instance)
    with _ouvrir(inst) as (depot, journal):
        b = depot.charger_brouillon(id_relance)
        if b is not None and b.statut is StatutBrouillon.VALIDEE:
            _controler_avant_envoi(depot, journal, b, maintenant)
        exp = expediteur if expediteur is not None else envoi_mod.ExpediteurDossier(inst.outbox)
        envoi_mod.envoyer(depot, journal, exp, id_relance, maintenant=maintenant, creneau_ok=creneau_ok)
        _panne(panne, "apres_envoi")
        _, anomalies = repercuter_envois(depot, journal, maintenant)
        if anomalies:
            raise ErreurCycle("message emis, mais repercussion refusee : " + " ; ".join(anomalies))
        envoye = depot.charger_brouillon(id_relance)
        assert envoye is not None
        return envoye


def trancher_envoi(instance: Instance | Path | str, id_relance: str, par: str, *, parti: bool,
                   maintenant: dt.datetime) -> Brouillon:
    inst = Instance.de(instance)
    nom = exiger_validateur(inst, par)
    with _ouvrir(inst) as (depot, journal):
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
) -> PieceAttendue:
    """Confirme un rattachement (`PIECE_RATTACHEE` avec `confirme_par`).

    Avec `message_id` (+ `empreinte`, prefixe accepte s'il est unique) : confirme
    une entree de la file humaine (proposition PROPOSEE, ou NON_ROUTEE si
    `dossier` et `reference` sont donnes) et la marque resolue. Sans : rattache
    directement `id_piece` a `dossier`/`reference` (piece justifiee par le moteur).
    """
    inst = Instance.de(instance)
    nom = exiger_validateur(inst, par)
    aujourdhui = _jour_local(maintenant)
    with _ouvrir(inst) as (depot, journal):
        notes = _Notes(journal, maintenant)
        entree: DecisionRoutage | None = None
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
            dossier = dossier or entree.dossier
            reference = reference or entree.reference_operation
            id_piece = id_piece or f"{entree.nom_fichier}#{entree.empreinte[:12]}"
        if not (dossier and reference and id_piece):
            raise ErreurCycle("dossier, reference et piece sont obligatoires")
        with _transaction(depot, notes):
            piece = _piece(depot, dossier, reference)
            nouvelle = _transition(piece, Evenement.PIECE_RATTACHEE, aujourdhui, notes, acteur=nom,
                                   id_piece=id_piece, confirme_par=nom)
            if nouvelle.bloquee and nouvelle.motif_blocage.startswith(PREFIXE_MOTIF_CYCLE):
                nouvelle = _transition(nouvelle, Evenement.BLOCAGE_LEVE, aujourdhui, notes, acteur=nom)
            depot.sauver_pieces([nouvelle])
            if entree is not None:
                depot.resoudre_file(entree.message_id, entree.empreinte, nom,
                                    f"rattache a {dossier}/{reference}", maintenant=maintenant)
            notes.noter("rattachement_confirme", _objet(nouvelle), {
                "par": nom, "id_piece": id_piece,
                "message_id": entree.message_id if entree else "",
            }, acteur=nom)
        return nouvelle


def controler_piece(instance: Instance | Path | str, dossier: str, reference: str, par: str, *,
                    conforme: bool, maintenant: dt.datetime, motif: str = "") -> PieceAttendue:
    inst = Instance.de(instance)
    nom = exiger_validateur(inst, par)
    aujourdhui = _jour_local(maintenant)
    with _ouvrir(inst) as (depot, journal):
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
    with _ouvrir(inst) as (depot, journal):
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
                    date_promesse: dt.date, maintenant: dt.datetime) -> PieceAttendue:
    """`PROMESSE_SAISIE` : DEMANDEE -> PROMISE (date dans [aujourdhui, +15 j], garde `etats`)."""
    if isinstance(date_promesse, dt.datetime) or not isinstance(date_promesse, dt.date):
        raise ErreurCycle("la date promise doit etre une date AAAA-MM-JJ")
    return _decision_sur_piece(
        instance, par, dossier, reference, maintenant, Evenement.PROMESSE_SAISIE,
        lambda p: p.etat is EtatPiece.PROMISE and p.date_promesse == date_promesse,
        "promesse_saisie", {"date_promesse": date_promesse}, date_promesse=date_promesse,
    )


def arbitrer_piece(instance: Instance | Path | str, dossier: str, reference: str, par: str, *,
                   motif: str, maintenant: dt.datetime) -> PieceAttendue:
    """`ARBITRAGE_CLASSEMENT` : ESCALADEE -> CLOSE_SANS_SUITE, motif obligatoire."""
    if not isinstance(motif, str) or not motif.strip():
        raise ErreurCycle("arbitrer : --motif obligatoire et non vide")
    nom = exiger_validateur(instance, par)
    return _decision_sur_piece(
        instance, par, dossier, reference, maintenant, Evenement.ARBITRAGE_CLASSEMENT,
        lambda p: p.etat is EtatPiece.CLOSE_SANS_SUITE,
        "arbitrage", {"motif": motif.strip()}, arbitre_par=nom, motif=motif.strip(),
    )


def bloquer_piece(instance: Instance | Path | str, dossier: str, reference: str, par: str, *,
                  motif: str, maintenant: dt.datetime) -> PieceAttendue:
    """`BLOCAGE_SIGNALE` : la piece n'est plus relancee tant qu'un humain ne la debloque pas."""
    if not isinstance(motif, str) or not motif.strip():
        raise ErreurCycle("bloquer : --motif obligatoire et non vide")
    motif = motif.strip()
    if motif.startswith(PREFIXE_MOTIF_CYCLE.strip()):
        raise ErreurCycle(f"bloquer : le prefixe {PREFIXE_MOTIF_CYCLE.strip()!r} est reserve au cycle")
    return _decision_sur_piece(
        instance, par, dossier, reference, maintenant, Evenement.BLOCAGE_SIGNALE,
        lambda p: p.bloquee and p.motif_blocage == motif,
        "blocage", {"motif": motif}, motif=motif,
    )


def debloquer_piece(instance: Instance | Path | str, dossier: str, reference: str, par: str, *,
                    motif: str, maintenant: dt.datetime) -> PieceAttendue:
    """`BLOCAGE_LEVE`. Leve aussi un blocage pose par le cycle : la decision humaine est
    memorisee et le cycle ne rebloque plus cette piece pour le meme constat du moteur."""
    if not isinstance(motif, str) or not motif.strip():
        raise ErreurCycle("debloquer : --motif obligatoire et non vide (trace d'audit)")
    return _decision_sur_piece(
        instance, par, dossier, reference, maintenant, Evenement.BLOCAGE_LEVE,
        lambda p: not p.bloquee,
        "deblocage", {"motif": motif.strip()},
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
                f"  {p.dossier:14s} {p.reference:12s} {p.periode} {p.etat.value:16s} "
                f"relances={p.nb_relances} echeance={_iso(l.echeance) or '-':10s} "
                f"{p.montant:>10} {p.devise}  {l.action}"
            )
        sortie.append(f"Periodes (etat calcule) : {len(self.periodes)}")
        sortie += [f"  {d:14s} {per} {e.value}" for d, per, e in self.periodes]
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
        if b is not None and p.etat not in ETATS_TERMINAUX:
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
    "CollisionReferences", "EnvoiRefuse", "ErreurCycle", "Instance", "LigneSuivi",
    "ResumeCycle", "SuiviPieces", "ValidateurRefuse", "arbitrer_piece", "bloquer_piece",
    "brouillons", "collisions_de_reference", "controler_piece", "debloquer_piece",
    "saisir_promesse", "suivi_pieces",
    "envoyer_relance", "executer_cycle", "exiger_validateur", "exporter", "lire_entrees",
    "lire_validateurs", "lister_file", "pieces", "rattacher_piece", "rejeter_relance",
    "repercuter_envois", "trancher_envoi", "valider_relance", "verifier_audit",
]
