"""Modele de donnees du rapprochement bancaire / pieces justificatives.

Le vocabulaire est celui du metier comptable francais (dossier, piece,
libelle) parce que c'est celui du cabinet qui lira les rapports.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Sens(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class Statut(str, Enum):
    JUSTIFIE = "justifie"           # une piece a ete rattachee
    MANQUANT = "manquant"           # aucune piece, et il en faut une
    HORS_PERIMETRE = "hors_perimetre"  # aucune piece necessaire (salaire, impot...)
    PARTIEL = "partiel"             # piece trouvee mais montant incomplet (acompte)
    A_VERIFIER = "a_verifier"       # rapprochement possible mais peu sur


@dataclass(frozen=True)
class OperationBancaire:
    reference: str
    dossier: str
    date_operation: dt.date
    libelle: str
    montant: Decimal          # toujours positif
    sens: Sens
    devise: str = "EUR"
    date_valeur: dt.date | None = None

    @property
    def libelle_court(self) -> str:
        return self.libelle if len(self.libelle) <= 48 else self.libelle[:45] + "..."


@dataclass(frozen=True)
class Piece:
    id_piece: str
    dossier: str
    fichier: str
    fournisseur: str
    date_facture: dt.date
    montant_ttc: Decimal
    devise: str = "EUR"
    date_reception: dt.date | None = None
    canal: str = "email"


@dataclass(frozen=True)
class Dossier:
    code: str
    raison_sociale: str
    email_contact: str
    nom_contact: str
    jour_echeance_tva: int = 15
    ton_relance: str = "courtois"
    # Circuit de relance (cahier des charges v1.0, section 4 et decision D6).
    # Les dossiers anterieurs au MVP n'ont pas cette colonne : ils sont lus
    # comme C-DIRECT, ce qui reproduit le comportement historique.
    circuit: str = "C-DIRECT"
    email_relais: str = ""
    # Domaines d'entreprise que le cabinet DECLARE comme appartenant a ce client.
    # Seul moyen d'identifier un dossier par le domaine d'un expediteur : une liste
    # noire de messageries grand public serait incomplete par nature (hotmail.ca,
    # t-online.de...). Vide par defaut : on ne route alors que par adresse exacte.
    domaines: tuple[str, ...] = ()

    @property
    def destinataire(self) -> str:
        """Adresse a laquelle partent les relances de ce dossier."""
        return self.email_relais if self.circuit == "C-RELAIS" else self.email_contact


@dataclass
class Rapprochement:
    """Resultat pour une operation bancaire."""

    operation: OperationBancaire
    statut: Statut
    pieces: list[Piece] = field(default_factory=list)
    confiance: float = 0.0
    motif: str = ""
    regle: str = ""           # nom de la regle qui a tranche, pour l'audit

    @property
    def montant_rattache(self) -> Decimal:
        return sum((p.montant_ttc for p in self.pieces), Decimal("0"))

    @property
    def reste_a_justifier(self) -> Decimal:
        return self.operation.montant - self.montant_rattache


# ---------------------------------------------------------------------------
# Types du MVP (cahier des charges v1.0, sections 5, 7, 9 et 10)
#
# Ces types sont le CONTRAT entre les modules. Ils sont figes : un module qui
# a besoin d'un champ de plus le demande au chef de projet, il ne le cree pas.
# ---------------------------------------------------------------------------

CIRCUITS = ("C-DIRECT", "C-RELAIS", "C-INTERNE")


class EtatPiece(str, Enum):
    ATTENDUE = "attendue"                 # creee par le moteur, jamais demandee
    DEMANDEE = "demandee"                 # une relance validee est partie
    PROMISE = "promise"                   # le client a annonce un envoi
    RECUE = "recue"                       # piece rattachee, controle comptable attendu
    VALIDEE = "validee"                   # terminal : piece conforme
    ESCALADEE = "escaladee"               # intervention du responsable
    HORS_PERIMETRE = "hors_perimetre"     # terminal : une regle d'exclusion s'applique
    CLOSE_SANS_SUITE = "close_sans_suite" # terminal : arbitrage du responsable


ETATS_TERMINAUX = frozenset(
    {EtatPiece.VALIDEE, EtatPiece.HORS_PERIMETRE, EtatPiece.CLOSE_SANS_SUITE}
)


class Evenement(str, Enum):
    RELANCE_ENVOYEE = "relance_envoyee"
    EXCLUSION_CREEE = "exclusion_creee"
    PIECE_RATTACHEE = "piece_rattachee"
    PROMESSE_SAISIE = "promesse_saisie"
    DELAI_DEPASSE = "delai_depasse"
    DATE_PROMISE_DEPASSEE = "date_promise_depassee"
    CONTROLE_CONFORME = "controle_conforme"
    CONTROLE_NON_CONFORME = "controle_non_conforme"
    ARBITRAGE_CLASSEMENT = "arbitrage_classement"
    BLOCAGE_SIGNALE = "blocage_signale"
    BLOCAGE_LEVE = "blocage_leve"


class EtatPeriode(str, Enum):
    OUVERTE = "ouverte"
    EN_COLLECTE = "en_collecte"
    COMPLETE = "complete"
    CLOTUREE = "cloturee"


@dataclass(frozen=True)
class PieceAttendue:
    """Un justificatif que le moteur a juge necessaire. Cle : (dossier, reference).

    Immuable : une transition renvoie un nouvel objet (dataclasses.replace).
    `reference` est celle de l'operation bancaire qui a cree l'obligation.
    """

    reference: str
    dossier: str
    periode: str                          # "AAAA-MM", mois de l'operation
    montant: Decimal                      # montant de l'operation, positif
    date_operation: dt.date
    libelle: str
    etat: EtatPiece = EtatPiece.ATTENDUE
    nb_relances: int = 0                  # envois, demande initiale comprise
    date_premiere_demande: dt.date | None = None
    date_derniere_relance: dt.date | None = None
    date_promesse: dt.date | None = None
    bloquee: bool = False                 # drapeau, pas un etat (section 9)
    motif_blocage: str = ""
    pieces_rattachees: tuple[str, ...] = ()   # id_piece
    devise: str = "EUR"                   # celle de l'operation : une relance ne ment pas sur la monnaie


class StatutRoutage(str, Enum):
    PROPOSEE = "proposee"       # rattachement propose, a confirmer par un humain
    NON_ROUTEE = "non_routee"   # file humaine
    DOUBLON = "doublon"         # deja traitee, ne produit aucun effet


class MotifNonRoute(str, Enum):
    DOSSIER_INCONNU = "dossier_inconnu"
    DOSSIER_AMBIGU = "dossier_ambigu"
    PERIODE_AMBIGUE = "periode_ambigue"
    MONTANT_INCONNU = "montant_inconnu"
    AUCUN_CANDIDAT = "aucun_candidat"
    CANDIDATS_MULTIPLES = "candidats_multiples"
    SANS_PIECE_JOINTE = "sans_piece_jointe"


@dataclass(frozen=True)
class FichierEntrant:
    nom: str
    contenu: bytes


@dataclass(frozen=True)
class MessageEntrant:
    message_id: str
    expediteur: str                       # adresse seule, en minuscules
    recu_le: dt.datetime
    objet: str
    corps: str
    fichiers: tuple[FichierEntrant, ...] = ()


@dataclass(frozen=True)
class DecisionRoutage:
    """Une decision par fichier joint (ou une pour un message sans piece jointe)."""

    message_id: str
    nom_fichier: str
    empreinte: str                        # SHA-256 hexadecimal du contenu, "" si aucun fichier
    statut: StatutRoutage
    dossier: str | None = None
    periode: str | None = None
    reference_operation: str | None = None   # renseignee seulement si PROPOSEE
    motif: MotifNonRoute | None = None       # renseigne seulement si NON_ROUTEE
    detail: str = ""


class StatutBrouillon(str, Enum):
    BROUILLON = "brouillon"
    VALIDEE = "validee"         # relue et approuvee par un humain nomme
    EN_COURS = "en_cours"       # emission commencee, resultat inconnu : JAMAIS renvoye seul
    ENVOYEE = "envoyee"
    REJETEE = "rejetee"


@dataclass(frozen=True)
class Brouillon:
    id_relance: str                       # voir identifiant_relance()
    destinataire: str
    objet: str
    corps: str
    dossiers: tuple[str, ...]
    references: tuple[str, ...]           # references des PieceAttendue reclamees
    niveau: int                           # 1 = demande initiale, 2+ = relance
    cree_le: dt.date
    statut: StatutBrouillon = StatutBrouillon.BROUILLON
    valide_par: str = ""
    valide_le: dt.datetime | None = None
    envoye_le: dt.datetime | None = None


@dataclass(frozen=True)
class EnvoiRelance:
    """Trace d'un envoi effectif : sert a la limite d'un e-mail par semaine."""

    id_relance: str
    destinataire: str
    date_envoi: dt.date
    dossiers: tuple[str, ...]
    references: tuple[str, ...]


def identifiant_relance(
    destinataire: str, references: tuple[str, ...] | list[str], niveau: int, jour: dt.date
) -> str:
    """Identifiant deterministe d'une relance.

    Meme destinataire, memes pieces, meme niveau, meme jour : meme identifiant.
    C'est ce qui rend un cycle rejouable sans creer de second brouillon, donc
    sans risque de double envoi.
    """
    import hashlib

    graine = f"{destinataire.strip().lower()}|{niveau}|{','.join(sorted(references))}|{jour.isoformat()}"
    return hashlib.sha256(graine.encode("utf-8")).hexdigest()[:16]

