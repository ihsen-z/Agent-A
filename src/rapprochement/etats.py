"""Machine d'etats d'une piece attendue (contrat : docs/architecture_mvp.md, 4.3).

Principes :
  - La table de transitions est de la DONNEE : le dictionnaire `TABLE`, indexe par
    (etat, evenement). Toute combinaison absente leve `TransitionInterdite`.
    `appliquer` ne contient aucune cascade de `if` sur les etats.
  - Les fonctions sont pures : une transition renvoie un NOUVEL objet
    (`dataclasses.replace`), la piece d'origine n'est jamais modifiee, et
    `aujourdhui` est un parametre (jamais d'horloge cachee).
  - Les gardes qui exigent une decision humaine refusent les acteurs
    automatiques (decision D3 du cahier des charges).
  - Aucune garde ne repare une donnee incoherente : en cas de doute on refuse.

L'etat d'une PERIODE n'est jamais stocke : `etat_periode` le calcule.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from .modeles import (
    ETATS_TERMINAUX,
    EtatPeriode,
    EtatPiece,
    Evenement,
    PieceAttendue,
    Rapprochement,
    Statut,
)


class TransitionInterdite(Exception):
    """Combinaison (etat, evenement) absente de la table, ou garde non satisfaite."""


PLAFOND_RELANCES = 4

# Fenetre dans laquelle une promesse du client est acceptee : [aujourdhui, +15 j].
FENETRE_PROMESSE_JOURS = 15

# Acteurs automatiques, refuses partout ou une decision humaine est exigee.
# Comparaison exacte apres normalisation (espaces, casse, accents).
ACTEURS_AUTOMATIQUES = frozenset(
    {"systeme", "system", "auto", "automatique", "robot", "cron", "bot"}
)

# Une piece `bloquee` n'accepte que ces evenements (et seulement si la table les
# autorise depuis son etat).
EVENEMENTS_SI_BLOQUEE = frozenset(
    {
        Evenement.BLOCAGE_LEVE,
        Evenement.PIECE_RATTACHEE,
        Evenement.EXCLUSION_CREEE,
        Evenement.ARBITRAGE_CLASSEMENT,
    }
)


# ---------------------------------------------------------------------------
# Gardes
# ---------------------------------------------------------------------------

def _normaliser_acteur(valeur: str) -> str:
    sans_accents = "".join(
        c for c in unicodedata.normalize("NFKD", valeur) if not unicodedata.combining(c)
    )
    return sans_accents.strip().casefold()


def est_acteur_humain(valeur: object) -> bool:
    """Vrai si `valeur` est un nom non vide qui n'est pas un acteur automatique."""
    if not isinstance(valeur, str):
        return False
    nom = _normaliser_acteur(valeur)
    return bool(nom) and nom not in ACTEURS_AUTOMATIQUES


def _texte_non_vide(valeur: object) -> bool:
    return isinstance(valeur, str) and bool(valeur.strip())


@dataclass(frozen=True)
class Garde:
    """Condition d'une transition. `verifier` leve TransitionInterdite si elle echoue."""

    nom: str
    verifier: Callable[[PieceAttendue, dt.date, Mapping[str, Any]], None]


def _garde_humain(cle: str) -> Garde:
    def verifier(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> None:
        if cle not in ctx:
            raise TransitionInterdite(f"contexte manquant : {cle}")
        if not est_acteur_humain(ctx[cle]):
            raise TransitionInterdite(
                f"{cle} doit etre un acteur humain nomme (recu : {ctx[cle]!r})"
            )

    return Garde(cle, verifier)


def _garde_texte(cle: str) -> Garde:
    def verifier(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> None:
        if cle not in ctx:
            raise TransitionInterdite(f"contexte manquant : {cle}")
        if not _texte_non_vide(ctx[cle]):
            raise TransitionInterdite(f"{cle} ne peut pas etre vide")

    return Garde(cle, verifier)


def _verifier_regle_validee(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> None:
    if ctx.get("regle_validee") is not True:
        raise TransitionInterdite("regle_validee=True est exige")


def _verifier_plafond(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> None:
    if piece.nb_relances >= PLAFOND_RELANCES:
        raise TransitionInterdite(
            f"plafond de {PLAFOND_RELANCES} relances atteint : escalade requise (DELAI_DEPASSE)"
        )


def _verifier_bloquee(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> None:
    if not piece.bloquee:
        raise TransitionInterdite(f"{piece.reference} : la piece n'est pas bloquee")


def _est_date(valeur: object) -> bool:
    # `datetime` est une sous-classe de `date` : on l'exclut, la comparaison avec
    # une `date` leverait TypeError.
    return isinstance(valeur, dt.date) and not isinstance(valeur, dt.datetime)


def _verifier_fenetre_promesse(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> None:
    if "date_promesse" not in ctx:
        raise TransitionInterdite("contexte manquant : date_promesse")
    promesse = ctx["date_promesse"]
    if not _est_date(promesse):
        raise TransitionInterdite(f"date_promesse doit etre une date (recu : {promesse!r})")
    limite = aujourdhui + dt.timedelta(days=FENETRE_PROMESSE_JOURS)
    if not aujourdhui <= promesse <= limite:
        raise TransitionInterdite(
            f"date_promesse {promesse} hors de [{aujourdhui}, {limite}]"
        )


def _verifier_promesse_depassee(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> None:
    if piece.date_promesse is None:
        raise TransitionInterdite("la piece n'a pas de date_promesse")
    if not aujourdhui > piece.date_promesse:
        raise TransitionInterdite(
            f"date promise {piece.date_promesse} non depassee au {aujourdhui}"
        )


G_VALIDE_PAR = _garde_humain("valide_par")
G_CONFIRME_PAR = _garde_humain("confirme_par")
G_CONTROLE_PAR = _garde_humain("controle_par")
G_ARBITRE_PAR = _garde_humain("arbitre_par")
G_ID_PIECE = _garde_texte("id_piece")
G_MOTIF = _garde_texte("motif")
G_REGLE_VALIDEE = Garde("regle_validee", _verifier_regle_validee)
G_PLAFOND = Garde("plafond", _verifier_plafond)
G_BLOQUEE = Garde("bloquee", _verifier_bloquee)
G_FENETRE_PROMESSE = Garde("fenetre_promesse", _verifier_fenetre_promesse)
G_PROMESSE_DEPASSEE = Garde("promesse_depassee", _verifier_promesse_depassee)


# ---------------------------------------------------------------------------
# Effets : chacun renvoie les champs a remplacer sur la piece
# ---------------------------------------------------------------------------

Effet = Callable[[PieceAttendue, dt.date, Mapping[str, Any]], dict[str, Any]]


def _aucun(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {}


def _premiere_demande(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "nb_relances": 1,
        "date_premiere_demande": aujourdhui,
        "date_derniere_relance": aujourdhui,
    }


def _relance_suivante(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {"nb_relances": piece.nb_relances + 1, "date_derniere_relance": aujourdhui}


def _rattacher(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {"pieces_rattachees": piece.pieces_rattachees + (ctx["id_piece"].strip(),)}


def _rattacher_depuis_promesse(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {**_rattacher(piece, aujourdhui, ctx), "date_promesse": None}


def _promettre(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {"date_promesse": ctx["date_promesse"]}


def _oublier_promesse(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {"date_promesse": None}


def _rejeter_piece(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> dict[str, Any]:
    champs: dict[str, Any] = {"pieces_rattachees": (), "date_promesse": None}
    if piece.date_premiere_demande is None:
        # Piece arrivee avant toute relance : le delai part d'aujourd'hui.
        champs["date_premiere_demande"] = aujourdhui
    return champs


def _bloquer(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {"bloquee": True, "motif_blocage": ctx["motif"].strip()}


def _lever_blocage(piece: PieceAttendue, aujourdhui: dt.date, ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {"bloquee": False, "motif_blocage": ""}


# ---------------------------------------------------------------------------
# Table de transitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Regle:
    """Une cellule de la table.

    `cible=None` signifie « meme etat ».
    """

    cible: EtatPiece | None
    gardes: tuple[Garde, ...] = ()
    effet: Effet = _aucun

    def noms_gardes(self) -> tuple[str, ...]:
        return tuple(g.nom for g in self.gardes)

    def cible_pour(self, piece: PieceAttendue) -> EtatPiece:
        return self.cible if self.cible is not None else piece.etat


E = EtatPiece
V = Evenement

_TABLE_EXPLICITE: dict[tuple[EtatPiece, Evenement], Regle] = {
    (E.ATTENDUE, V.RELANCE_ENVOYEE): Regle(E.DEMANDEE, (G_VALIDE_PAR,), _premiere_demande),
    (E.ATTENDUE, V.EXCLUSION_CREEE): Regle(E.HORS_PERIMETRE, (G_REGLE_VALIDEE,)),
    (E.ATTENDUE, V.PIECE_RATTACHEE): Regle(E.RECUE, (G_ID_PIECE, G_CONFIRME_PAR), _rattacher),
    (E.DEMANDEE, V.RELANCE_ENVOYEE): Regle(
        E.DEMANDEE, (G_VALIDE_PAR, G_PLAFOND), _relance_suivante
    ),
    (E.DEMANDEE, V.PIECE_RATTACHEE): Regle(E.RECUE, (G_ID_PIECE, G_CONFIRME_PAR), _rattacher),
    (E.DEMANDEE, V.PROMESSE_SAISIE): Regle(E.PROMISE, (G_FENETRE_PROMESSE,), _promettre),
    # Toujours ESCALADEE : c'est cadence.planifier qui decide QUAND l'escalade est due.
    (E.DEMANDEE, V.DELAI_DEPASSE): Regle(E.ESCALADEE),
    (E.DEMANDEE, V.EXCLUSION_CREEE): Regle(E.HORS_PERIMETRE, (G_REGLE_VALIDEE,)),
    (E.PROMISE, V.DATE_PROMISE_DEPASSEE): Regle(
        E.DEMANDEE, (G_PROMESSE_DEPASSEE,), _oublier_promesse
    ),
    (E.PROMISE, V.PIECE_RATTACHEE): Regle(
        E.RECUE, (G_ID_PIECE, G_CONFIRME_PAR), _rattacher_depuis_promesse
    ),
    (E.PROMISE, V.EXCLUSION_CREEE): Regle(E.HORS_PERIMETRE, (G_REGLE_VALIDEE,)),
    (E.RECUE, V.CONTROLE_CONFORME): Regle(E.VALIDEE, (G_CONTROLE_PAR,)),
    (E.RECUE, V.CONTROLE_NON_CONFORME): Regle(
        E.DEMANDEE, (G_CONTROLE_PAR, G_MOTIF), _rejeter_piece
    ),
    (E.ESCALADEE, V.ARBITRAGE_CLASSEMENT): Regle(E.CLOSE_SANS_SUITE, (G_ARBITRE_PAR, G_MOTIF)),
    (E.ESCALADEE, V.PIECE_RATTACHEE): Regle(E.RECUE, (G_ID_PIECE, G_CONFIRME_PAR), _rattacher),
    (E.ESCALADEE, V.EXCLUSION_CREEE): Regle(E.HORS_PERIMETRE, (G_REGLE_VALIDEE,)),
}

# « Tout non terminal » : developpe sur chaque etat non terminal.
_TABLE_BLOCAGE: dict[tuple[EtatPiece, Evenement], Regle] = {}
for _etat in EtatPiece:
    if _etat in ETATS_TERMINAUX:
        continue
    _TABLE_BLOCAGE[(_etat, V.BLOCAGE_SIGNALE)] = Regle(None, (G_MOTIF,), _bloquer)
    _TABLE_BLOCAGE[(_etat, V.BLOCAGE_LEVE)] = Regle(None, (G_BLOQUEE,), _lever_blocage)

TABLE: Mapping[tuple[EtatPiece, Evenement], Regle] = MappingProxyType(
    {**_TABLE_EXPLICITE, **_TABLE_BLOCAGE}
)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def creer_depuis_rapprochement(r: Rapprochement) -> PieceAttendue:
    """Cree la piece attendue d'un rapprochement MANQUANT (ValueError sinon).

    PARTIEL et A_VERIFIER vont en file humaine : jamais de relance automatique.
    """
    if r.statut is not Statut.MANQUANT:
        raise ValueError(
            f"Seul MANQUANT donne une piece attendue (recu : {r.statut.value})"
        )
    op = r.operation
    if op.montant <= 0:
        raise ValueError(f"Montant non positif pour {op.reference} : {op.montant}")
    if not op.reference or not op.dossier:
        raise ValueError("Operation sans reference ou sans dossier")
    return PieceAttendue(
        reference=op.reference,
        dossier=op.dossier,
        periode=f"{op.date_operation.year:04d}-{op.date_operation.month:02d}",
        montant=op.montant,
        date_operation=op.date_operation,
        libelle=op.libelle,
        devise=op.devise,
    )


def appliquer(
    piece: PieceAttendue,
    evenement: Evenement,
    *,
    aujourdhui: dt.date,
    **contexte: Any,
) -> PieceAttendue:
    """Applique un evenement. Renvoie une NOUVELLE piece ou leve TransitionInterdite."""
    if not _est_date(aujourdhui):
        raise TypeError(f"aujourdhui doit etre une date (recu : {aujourdhui!r})")
    try:
        evenement = Evenement(evenement)
    except ValueError:
        raise TransitionInterdite(f"evenement inconnu : {evenement!r}") from None

    if piece.etat in ETATS_TERMINAUX:
        raise TransitionInterdite(
            f"{piece.reference} : etat terminal {piece.etat.value}, aucun evenement accepte"
        )
    regle = TABLE.get((piece.etat, evenement))
    if regle is None:
        raise TransitionInterdite(
            f"{piece.reference} : {evenement.value} interdit depuis {piece.etat.value}"
        )
    if piece.bloquee and evenement not in EVENEMENTS_SI_BLOQUEE:
        raise TransitionInterdite(
            f"{piece.reference} : piece bloquee, {evenement.value} refuse"
        )
    for garde in regle.gardes:
        garde.verifier(piece, aujourdhui, contexte)

    champs = regle.effet(piece, aujourdhui, contexte)
    cible = regle.cible_pour(piece)
    if cible in ETATS_TERMINAUX:
        # Un drapeau de blocage ne survit jamais a la cloture de la piece.
        champs = {**champs, "bloquee": False, "motif_blocage": ""}
    return replace(piece, etat=cible, **champs)


def etat_periode(
    pieces: Iterable[PieceAttendue],
    *,
    pieces_creees: bool = True,
    cloturee: bool = False,
) -> EtatPeriode:
    """Etat d'une periode, calcule a partir de ses pieces (jamais stocke)."""
    if not pieces_creees:
        if cloturee:
            raise ValueError("Periode non cloturable : les pieces ne sont pas creees")
        return EtatPeriode.OUVERTE
    toutes_terminales = all(p.etat in ETATS_TERMINAUX for p in pieces)
    if cloturee and not toutes_terminales:
        raise ValueError("Periode non cloturable : au moins une piece n'est pas terminale")
    if not toutes_terminales:
        return EtatPeriode.EN_COLLECTE
    return EtatPeriode.CLOTUREE if cloturee else EtatPeriode.COMPLETE
