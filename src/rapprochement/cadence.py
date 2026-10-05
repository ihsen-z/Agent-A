"""Calendrier ouvre et planification des relances (contrat 4.5).

Module pur : aucun acces disque, aucune horloge cachee. La date du jour et
l'heure courante sont toujours des parametres.

Deux familles de fonctions :

- le calendrier : jours feries francais, jours ouvres, creneau d'envoi ;
- `planifier`, qui decide QUI relancer et QUAND, sans jamais lever d'exception
  sur des donnees incoherentes : le cas douteux est classe dans `reportees` ou
  `bloquees` avec une raison, il part en file humaine.

Effet du garde-fou hebdomadaire (a lire avant de toucher aux jalons)
--------------------------------------------------------------------
Les jalons du cahier des charges (relance a 3 puis 7 jours ouvres) se
contredisent avec « un e-mail par destinataire et par semaine ». Le MVP applique
la regle la plus prudente : le garde-fou GAGNE. Consequence concrete : une
demande initiale envoyee le jour J bloque tout nouvel envoi vers ce destinataire
tant que moins de 7 jours calendaires (`FENETRE_HEBDO_JOURS`) se sont ecoules.
Le jalon de 3 jours ouvres est donc atteint (la piece est eligible) mais il est
reporte (`LIMITE_HEBDOMADAIRE`) ; la premiere relance part au plus tot 7 jours
calendaires apres la demande initiale, et seulement si ce jour-la est ouvre. Le
jalon de 3 jours ouvres ne s'execute jamais en pratique, et le jalon de 7 jours
ouvres (9 a 11 jours calendaires) ne peut partir qu'une fois la fenetre
hebdomadaire de la relance precedente ecoulee. Cette contradiction est un point
ouvert a trancher par le chef de projet, pas un defaut a corriger ici.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .modeles import (
    ETATS_TERMINAUX,
    Dossier,
    EnvoiRelance,
    EtatPiece,
    Evenement,
    PieceAttendue,
)

JALONS = (0, 3, 7)            # jours ouvres depuis la premiere demande : envois n1, n2, n3
JALON_ESCALADE = 14
FENETRE_HEBDO_JOURS = 7

# Plafond de relances par piece (identique a `etats.PLAFOND_RELANCES`). Redefini
# ici pour ne pas dependre d'un module ecrit en parallele ; un test garde les
# deux valeurs alignees quand `etats` existe.
PLAFOND_RELANCES = 4

HEURE_DEBUT = dt.time(8, 0)
HEURE_FIN = dt.time(18, 0)

# ---------------------------------------------------------------------------
# Calendrier
# ---------------------------------------------------------------------------


def paques(annee: int) -> dt.date:
    """Date de Paques (calendrier gregorien), algorithme de Meeus/Jones/Butcher."""
    a = annee % 19
    b, c = divmod(annee, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mois, jour = divmod(h + l - 7 * m + 114, 31)
    return dt.date(annee, mois, jour + 1)


@lru_cache(maxsize=None)
def jours_feries(annee: int) -> frozenset[dt.date]:
    """Jours feries francais metropolitains de l'annee (11 jours)."""
    p = paques(annee)
    return frozenset(
        {
            dt.date(annee, 1, 1),
            p + dt.timedelta(days=1),     # lundi de Paques
            dt.date(annee, 5, 1),
            dt.date(annee, 5, 8),
            p + dt.timedelta(days=39),    # Ascension (jeudi)
            p + dt.timedelta(days=50),    # lundi de Pentecote
            dt.date(annee, 7, 14),
            dt.date(annee, 8, 15),
            dt.date(annee, 11, 1),
            dt.date(annee, 11, 11),
            dt.date(annee, 12, 25),
        }
    )


def est_ouvre(d: dt.date) -> bool:
    """Lundi a vendredi, hors jours feries."""
    return d.weekday() < 5 and d not in jours_feries(d.year)


def ajouter_jours_ouvres(d: dt.date, n: int) -> dt.date:
    """Date obtenue en avancant de `n` jours ouvres (recule si `n` < 0).

    `d` peut etre un jour non ouvre : on compte alors a partir du prochain (ou
    precedent) jour ouvre. `n == 0` renvoie `d` tel quel.
    """
    pas = dt.timedelta(days=1 if n >= 0 else -1)
    reste = abs(n)
    courant = d
    while reste > 0:
        courant += pas
        if est_ouvre(courant):
            reste -= 1
    return courant


def jours_ouvres_entre(debut: dt.date, fin: dt.date) -> int:
    """Nombre de jours ouvres ecoules : ]debut, fin] (debut exclu, fin inclus).

    Vaut 0 si `fin <= debut` : jamais de valeur negative.
    """
    total = (fin - debut).days
    if total <= 0:
        return 0
    semaines, reste = divmod(total, 7)
    nombre = semaines * 5
    for i in range(1, reste + 1):
        if (debut + dt.timedelta(days=7 * semaines + i)).weekday() < 5:
            nombre += 1
    # On retire les feries tombant un jour de semaine dans l'intervalle.
    for annee in range(debut.year, fin.year + 1):
        for ferie in jours_feries(annee):
            if debut < ferie <= fin and ferie.weekday() < 5:
                nombre -= 1
    return nombre


def heure_d_envoi_valide(maintenant: dt.datetime, fuseau: str = "Europe/Paris") -> bool:
    """Jour ouvre ET 08:00 <= heure locale < 18:00 dans `fuseau`.

    `maintenant` doit porter un fuseau : un datetime naif leve `ValueError`
    (on ne devine jamais l'heure locale d'un envoi). Un nom de fuseau inconnu
    leve `zoneinfo.ZoneInfoNotFoundError`, volontairement non masquee.
    """
    if maintenant.tzinfo is None or maintenant.utcoffset() is None:
        raise ValueError("datetime naif : un fuseau est obligatoire")
    local = maintenant.astimezone(ZoneInfo(fuseau))
    return est_ouvre(local.date()) and HEURE_DEBUT <= local.time() < HEURE_FIN


# ---------------------------------------------------------------------------
# Planification
# ---------------------------------------------------------------------------

LIMITE_HEBDOMADAIRE = "LIMITE_HEBDOMADAIRE"
JOUR_NON_OUVRE = "JOUR_NON_OUVRE"
DOSSIER_INCONNU = "DOSSIER_INCONNU"


@dataclass(frozen=True)
class RelancePlanifiee:
    destinataire: str
    dossiers: tuple[str, ...]
    references: tuple[str, ...]
    niveau: int


@dataclass(frozen=True)
class Report:
    destinataire: str
    references: tuple[str, ...]
    raison: str       # LIMITE_HEBDOMADAIRE, JOUR_NON_OUVRE ou DOSSIER_INCONNU


@dataclass
class Planification:
    relances: list[RelancePlanifiee] = field(default_factory=list)
    reportees: list[Report] = field(default_factory=list)
    transitions: list[tuple[str, Evenement]] = field(default_factory=list)
    bloquees: list[str] = field(default_factory=list)


def _cle_destinataire(adresse: str) -> str:
    return adresse.strip().lower()


def _a_traiter(piece: PieceAttendue, aujourdhui: dt.date) -> str:
    """Classe une piece : "relance", "transition_delai", "transition_promesse",
    "bloquee" ou "rien". Ne leve jamais."""
    etat = piece.etat
    if etat in ETATS_TERMINAUX:
        return "rien"
    if piece.bloquee:
        return "bloquee"
    if etat is EtatPiece.ATTENDUE:
        return "relance"
    if etat is EtatPiece.DEMANDEE:
        premiere = piece.date_premiere_demande
        if premiere is None or piece.nb_relances < 0:
            return "bloquee"                      # donnees incoherentes
        ecoules = jours_ouvres_entre(premiere, aujourdhui)
        if piece.nb_relances >= PLAFOND_RELANCES:
            return "transition_delai"             # sans attendre le jalon
        if piece.nb_relances >= len(JALONS):
            return "transition_delai" if ecoules >= JALON_ESCALADE else "rien"
        return "relance" if ecoules >= JALONS[piece.nb_relances] else "rien"
    if etat is EtatPiece.PROMISE:
        if piece.date_promesse is None:
            return "bloquee"
        # Cahier des charges : delai reporte a la date promise + 2 jours ouvres.
        limite = ajouter_jours_ouvres(piece.date_promesse, 2)
        return "transition_promesse" if aujourdhui > limite else "rien"
    return "rien"                                 # ESCALADEE, RECUE : jamais relancees


def planifier(
    pieces: Iterable[PieceAttendue],
    dossiers: Mapping[str, Dossier],
    historique: Sequence[EnvoiRelance],
    aujourdhui: dt.date,
) -> Planification:
    """Decide quelles relances preparer aujourd'hui. Ne leve jamais d'exception.

    Regles (contrat 4.5) : voir la docstring du module pour l'effet du
    garde-fou hebdomadaire sur les jalons. Choix de lecture documentes :

    - Les `transitions` (DELAI_DEPASSE, DATE_PROMISE_DEPASSEE) sont des
      constats de calendrier, pas des envois : elles sont emises meme un jour
      non ouvre, et meme si le dossier est inconnu. Seuls les e-mails sont
      reportes.
    - Une piece non terminale `bloquee`, ou aux donnees incoherentes (DEMANDEE
      sans `date_premiere_demande`, PROMISE sans `date_promesse`), va dans
      `bloquees`.
    - Une `reference` portee par deux pieces differentes (deux dossiers, ou la
      meme cle deux fois avec un contenu different) ne peut pas etre designee
      sans ambiguite dans `RelancePlanifiee.references` : ces pieces vont dans
      `bloquees`.
    - Un dossier dont le destinataire est vide (C-RELAIS sans `email_relais`)
      n'a personne a qui ecrire : ses pieces vont dans `bloquees`.
    - Un dossier inconnu produit un `Report(destinataire="", ...)` par dossier.
    - Le destinataire est normalise (strip + minuscules) : deux dossiers dont
      les adresses ne different que par la casse sont regroupes.
    """
    plan = Planification()

    # Dedoublonnage et detection des references ambigues.
    uniques: dict[tuple[str, str], PieceAttendue] = {}
    ambigues: set[tuple[str, str]] = set()
    for p in pieces:
        cle = (p.dossier, p.reference)
        if cle in uniques and uniques[cle] != p:
            ambigues.add(cle)
        uniques.setdefault(cle, p)
    dossiers_par_reference: dict[str, set[str]] = {}
    for dossier_code, reference in uniques:
        dossiers_par_reference.setdefault(reference, set()).add(dossier_code)
    for reference, codes in dossiers_par_reference.items():
        if len(codes) > 1:
            ambigues.update((c, reference) for c in codes)

    bloquees: set[str] = set()
    transitions: set[tuple[str, str]] = set()   # (reference, valeur) pour tri stable
    a_relancer: list[PieceAttendue] = []

    for cle in sorted(uniques):
        piece = uniques[cle]
        genre = _a_traiter(piece, aujourdhui)
        if genre == "rien":
            continue
        if cle in ambigues:
            if piece.etat not in ETATS_TERMINAUX:
                bloquees.add(piece.reference)
            continue
        if genre == "bloquee":
            bloquees.add(piece.reference)
        elif genre == "transition_delai":
            transitions.add((piece.reference, Evenement.DELAI_DEPASSE.value))
        elif genre == "transition_promesse":
            transitions.add((piece.reference, Evenement.DATE_PROMISE_DEPASSEE.value))
        else:
            a_relancer.append(piece)

    plan.bloquees = sorted(bloquees)
    plan.transitions = [(ref, Evenement(val)) for ref, val in sorted(transitions)]

    # Regroupement par destinataire.
    groupes: dict[str, list[PieceAttendue]] = {}
    inconnus: dict[str, list[str]] = {}
    for piece in a_relancer:
        dossier = dossiers.get(piece.dossier)
        if dossier is None:
            inconnus.setdefault(piece.dossier, []).append(piece.reference)
            continue
        cle_dest = _cle_destinataire(dossier.destinataire)
        if not cle_dest:
            plan.bloquees.append(piece.reference)
            continue
        groupes.setdefault(cle_dest, []).append(piece)
    plan.bloquees = sorted(set(plan.bloquees))

    for code in sorted(inconnus):
        plan.reportees.append(Report("", tuple(sorted(inconnus[code])), DOSSIER_INCONNU))

    envois_recents = {
        _cle_destinataire(e.destinataire)
        for e in historique
        if (aujourdhui - e.date_envoi).days < FENETRE_HEBDO_JOURS
    }
    ouvre = est_ouvre(aujourdhui)

    for destinataire in sorted(groupes):
        groupe = groupes[destinataire]
        references = tuple(sorted(p.reference for p in groupe))
        if not ouvre:
            plan.reportees.append(Report(destinataire, references, JOUR_NON_OUVRE))
        elif destinataire in envois_recents:
            plan.reportees.append(Report(destinataire, references, LIMITE_HEBDOMADAIRE))
        else:
            plan.relances.append(
                RelancePlanifiee(
                    destinataire=destinataire,
                    dossiers=tuple(sorted({p.dossier for p in groupe})),
                    references=references,
                    niveau=1 + max(p.nb_relances for p in groupe),
                )
            )
    return plan
