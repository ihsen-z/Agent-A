"""Validation humaine et emission des relances (contrat 4.6).

Deux regles absolues :

* RIEN NE PART SANS RELECTURE HUMAINE : `envoyer` refuse tout brouillon qui n'est
  pas `VALIDEE` par un humain nomme, et c'est le SEUL endroit qui appelle
  `Expediteur.envoyer` (l'adaptateur `ExpediteurDossier` re-verifie de son cote).
* JAMAIS DEUX ENVOIS DU MEME MESSAGE : le passage `VALIDEE -> EN_COURS` est
  persiste (et journalise) AVANT l'appel a l'expediteur. Un envoi dont le
  resultat est inconnu reste `EN_COURS` et n'est jamais renvoye seul : seul un
  humain le tranche. Mieux vaut un e-mail perdu signale qu'un client relance deux fois.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable
from email import policy
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

from .etats import est_acteur_humain
from .modeles import Brouillon, EnvoiRelance, StatutBrouillon

if TYPE_CHECKING:  # pas d'import a l'execution : depot.py et audit.py sont ecrits en parallele
    from .audit import JournalAudit
    from .depot import Depot

# Acteur des etapes automatiques du journal. Il ne VALIDE jamais rien : la
# validation humaine est portee par `valide_par`, repris dans les details.
ACTEUR_SYSTEME = "systeme"

# Fuseau de reference pour la date d'un envoi confirme a la main.
FUSEAU_DEFAUT = "Europe/Paris"


class EnvoiNonValide(Exception):
    """Le brouillon n'est pas dans un etat qui autorise l'operation demandee."""


class EnvoiLimiteHebdomadaire(EnvoiNonValide):
    """Un message est deja parti vers ce destinataire dans la fenetre de 7 jours glissants."""


class EnvoiIncertainEnAttente(EnvoiNonValide):
    """Un autre envoi vers ce destinataire est EN_COURS : un humain doit d'abord le trancher."""


class EnvoiHorsCreneau(Exception):
    """Hors du creneau d'envoi : rien n'est parti, le brouillon reste VALIDEE."""


class ErreurEnvoi(Exception):
    """Echec d'envoi dont l'issue n'est pas garantie : le brouillon reste EN_COURS."""


class ErreurEnvoiCertaine(ErreurEnvoi):
    """Echec certain, rien n'est parti : on peut reessayer."""


class Expediteur(Protocol):
    def envoyer(self, brouillon: Brouillon) -> str:
        """Emet le message et renvoie un identifiant externe."""
        ...


# ---------------------------------------------------------------------------
# Controles communs
# ---------------------------------------------------------------------------


def _exiger_humain(par: object) -> str:
    """Renvoie `par` nettoye, ou leve EnvoiNonValide si ce n'est pas un humain nomme.

    La liste des acteurs automatiques vit dans `etats` (une seule source de verite).
    """
    if not isinstance(par, str) or not est_acteur_humain(par):
        raise EnvoiNonValide(
            "la decision doit venir d'un humain nomme (pas vide, pas d'acteur automatique)"
        )
    return par.strip()


def _exiger_horodatage(maintenant: dt.datetime) -> None:
    if not isinstance(maintenant, dt.datetime) or maintenant.tzinfo is None or maintenant.utcoffset() is None:
        raise ValueError("`maintenant` doit etre un datetime avec fuseau")


def _exiger_brouillon(depot: Depot, id_relance: str) -> Brouillon:
    b = depot.charger_brouillon(id_relance)
    if b is None:
        raise EnvoiNonValide(f"brouillon introuvable : {id_relance}")
    return b


def _exiger_envoyable(b: Brouillon) -> None:
    """Un brouillon n'est envoyable que VALIDEE, par un humain nomme."""
    if b.statut is not StatutBrouillon.VALIDEE:
        raise EnvoiNonValide(
            f"brouillon {b.id_relance} au statut {b.statut.value} : seul un brouillon "
            f"{StatutBrouillon.VALIDEE.value} peut partir"
        )
    # Defense en profondeur : un statut VALIDEE forge a la main ne suffit pas.
    try:
        _exiger_humain(b.valide_par)
    except EnvoiNonValide:
        raise EnvoiNonValide(
            f"brouillon {b.id_relance} : valide_par vide ou automatique, envoi refuse"
        ) from None


def _refus(
    journal: JournalAudit,
    operation: str,
    id_relance: object,
    exc: Exception,
    maintenant: object,
    par: object = None,
) -> None:
    """Journalise `envoi_refuse` AVANT que l'appelant ne releve `exc`.

    Acteur "systeme" : c'est le systeme qui refuse ; la personne qui tentait
    l'operation est dans `par`. Une panne du journal ne masque pas le refus.
    """
    horodatage = (
        maintenant
        if isinstance(maintenant, dt.datetime) and maintenant.utcoffset() is not None
        else None
    )
    try:
        journal.ecrire(
            ACTEUR_SYSTEME,
            "envoi_refuse",
            str(id_relance),
            {
                "operation": operation,
                "par": par[:200] if isinstance(par, str) else "",
                "type": type(exc).__name__,
                "raison": str(exc)[:500],
            },
            horodatage=horodatage,
        )
    except Exception as exc_journal:
        exc.add_note(f"journal envoi_refuse non ecrit : {exc_journal!r}")


def _maj(depot: Depot, b: Brouillon) -> None:
    try:
        depot.maj_brouillon(b)
    except ValueError as exc:  # transition refusee par le depot
        raise EnvoiNonValide(str(exc)) from exc


# ---------------------------------------------------------------------------
# Validation et rejet
# ---------------------------------------------------------------------------


def valider(
    depot: Depot, journal: JournalAudit, id_relance: str, par: str, *, maintenant: dt.datetime
) -> Brouillon:
    """BROUILLON -> VALIDEE, par un humain nomme. Ecrit `valide` dans le journal."""
    try:
        return _valider(depot, journal, id_relance, par, maintenant)
    except EnvoiNonValide as exc:
        _refus(journal, "valider", id_relance, exc, maintenant, par)
        raise


def _valider(
    depot: Depot, journal: JournalAudit, id_relance: str, par: str, maintenant: dt.datetime
) -> Brouillon:
    par = _exiger_humain(par)
    _exiger_horodatage(maintenant)
    with depot.transaction():
        b = _exiger_brouillon(depot, id_relance)
        if b.statut is not StatutBrouillon.BROUILLON:
            raise EnvoiNonValide(
                f"brouillon {id_relance} au statut {b.statut.value} : seul un brouillon "
                f"{StatutBrouillon.BROUILLON.value} peut etre valide"
            )
        valide = dataclasses.replace(
            b, statut=StatutBrouillon.VALIDEE, valide_par=par, valide_le=maintenant
        )
        _maj(depot, valide)
        journal.ecrire(
            par,
            "valide",
            id_relance,
            {"par": par, "destinataire": b.destinataire, "niveau": b.niveau},
            horodatage=maintenant,
        )
    return valide


def rejeter(
    depot: Depot,
    journal: JournalAudit,
    id_relance: str,
    par: str,
    motif: str,
    *,
    maintenant: dt.datetime,
) -> Brouillon:
    """BROUILLON ou VALIDEE -> REJETEE. Motif obligatoire, journalise (`rejete`)."""
    try:
        return _rejeter(depot, journal, id_relance, par, motif, maintenant)
    except EnvoiNonValide as exc:
        _refus(journal, "rejeter", id_relance, exc, maintenant, par)
        raise


def _rejeter(
    depot: Depot, journal: JournalAudit, id_relance: str, par: str, motif: str, maintenant: dt.datetime
) -> Brouillon:
    par = _exiger_humain(par)
    if not isinstance(motif, str) or not motif.strip():
        raise EnvoiNonValide("le motif de rejet est obligatoire")
    motif = motif.strip()
    _exiger_horodatage(maintenant)
    with depot.transaction():
        b = _exiger_brouillon(depot, id_relance)
        if b.statut not in (StatutBrouillon.BROUILLON, StatutBrouillon.VALIDEE):
            raise EnvoiNonValide(
                f"brouillon {id_relance} au statut {b.statut.value} : on ne rejette qu'un "
                f"brouillon ou un brouillon valide"
            )
        rejete = dataclasses.replace(b, statut=StatutBrouillon.REJETEE)
        _maj(depot, rejete)
        journal.ecrire(
            par,
            "rejete",
            id_relance,
            {"par": par, "motif": motif, "statut_avant": b.statut.value},
            horodatage=maintenant,
        )
    return rejete


# ---------------------------------------------------------------------------
# Emission : l'unique chemin
# ---------------------------------------------------------------------------


def _journaliser_incertain(
    journal: JournalAudit, b: Brouillon, cause: BaseException, maintenant: dt.datetime, detail: str
) -> None:
    """Journalise `envoi_incertain` sans jamais masquer l'exception d'origine."""
    try:
        journal.ecrire(
            ACTEUR_SYSTEME,
            "envoi_incertain",
            b.id_relance,
            {
                "valide_par": b.valide_par,
                "erreur": type(cause).__name__,
                "message": str(cause)[:500],
                "detail": detail,
            },
            horodatage=maintenant,
        )
    except Exception as exc_journal:  # le brouillon reste EN_COURS : il est visible quand meme
        cause.add_note(f"journal envoi_incertain non ecrit : {exc_journal!r}")


def _preparer_envoi(
    depot: Depot,
    journal: JournalAudit,
    id_relance: str,
    maintenant: dt.datetime,
    jour_envoi: dt.date,
    fuseau: str,
    creneau_ok: Callable[[dt.datetime], bool] | None,
) -> Brouillon:
    """Etapes 1 a 3 : controles, creneau, limite hebdomadaire, puis VALIDEE -> EN_COURS. N'emet rien."""
    # 1. Statut et validation humaine.
    _exiger_envoyable(_exiger_brouillon(depot, id_relance))

    # 2. Creneau. Import tardif : cadence.py est ecrit en parallele. Une erreur
    #    d'import fait echouer l'envoi AVANT toute emission (echec ferme).
    if creneau_ok is None:
        from .cadence import heure_d_envoi_valide

        def creneau_ok(instant: dt.datetime) -> bool:
            return heure_d_envoi_valide(instant, fuseau)

    if not creneau_ok(maintenant):
        raise EnvoiHorsCreneau(f"{maintenant.isoformat()} est hors du creneau d'envoi")

    # 3. Limite hebdomadaire et envoi incertain en attente, au dernier maillon (garde-fou non parametrable) puis
    #    VALIDEE -> EN_COURS, persiste et journalise AVANT d'emettre. On relit dans la
    #    transaction : deux appels qui se chevauchent ne passent pas tous les deux.
    #    Import tardif de la fenetre (comme le creneau) : cadence.py est ecrit en parallele.
    from .cadence import FENETRE_HEBDO_JOURS

    with depot.transaction():
        courant = _exiger_brouillon(depot, id_relance)
        _exiger_envoyable(courant)
        # Meme definition que `cadence.planifier` : refuse si (jour - date_envoi).days < 7,
        # donc 6 jours ecoules = refuse, 7 = autorise. Dates locales du fuseau de l'appel.
        depuis = jour_envoi - dt.timedelta(days=FENETRE_HEBDO_JOURS - 1)
        deja = depot.historique_envois(destinataire=courant.destinataire, depuis=depuis)
        if deja:
            dernier = max(deja, key=lambda e: e.date_envoi)
            raise EnvoiLimiteHebdomadaire(
                f"un message est deja parti vers ce destinataire le {dernier.date_envoi.isoformat()} "
                f"(relance {dernier.id_relance}) : limite d'un e-mail par {FENETRE_HEBDO_JOURS} jours"
            )
        # Un envoi dont on ignore l'issue (EN_COURS) est peut-etre deja parti : pas de fenetre
        # de temps, un humain doit le trancher (`trancher_envoi_incertain`) avant tout nouvel
        # envoi vers le meme destinataire (casse et espaces de bord ignores).
        cible = courant.destinataire.strip().casefold()
        for autre in depot.lister_brouillons(StatutBrouillon.EN_COURS):
            if autre.id_relance != courant.id_relance and autre.destinataire.strip().casefold() == cible:
                raise EnvoiIncertainEnAttente(
                    f"l'envoi {autre.id_relance} vers ce destinataire est EN_COURS (resultat inconnu) : "
                    "a trancher par un humain avant tout nouvel envoi"
                )
        en_cours = dataclasses.replace(courant, statut=StatutBrouillon.EN_COURS)
        _maj(depot, en_cours)
        journal.ecrire(
            ACTEUR_SYSTEME,
            "envoi_demarre",
            id_relance,
            {"valide_par": en_cours.valide_par, "destinataire": en_cours.destinataire},
            horodatage=maintenant,
        )
    return en_cours


def envoyer(
    depot: Depot,
    journal: JournalAudit,
    expediteur: Expediteur,
    id_relance: str,
    *,
    maintenant: dt.datetime,
    fuseau: str = "Europe/Paris",
    creneau_ok: Callable[[dt.datetime], bool] | None = None,
) -> Brouillon:
    """Emet un brouillon VALIDEE. UNIQUE chemin d'emission du module.

    Un brouillon dont l'envoi a ete tente (EN_COURS, ENVOYEE) est refuse :
    c'est la garantie de zero doublon. Tout refus est journalise (`envoi_refuse`).
    """
    _exiger_horodatage(maintenant)
    jour_envoi = maintenant.astimezone(ZoneInfo(fuseau)).date()  # avant tout effet : echec = rien ne part
    try:
        en_cours = _preparer_envoi(depot, journal, id_relance, maintenant, jour_envoi, fuseau, creneau_ok)
    except (EnvoiNonValide, EnvoiHorsCreneau) as exc:
        _refus(journal, "envoyer", id_relance, exc, maintenant)
        raise

    # 4. Emission. Rien ici ne doit pouvoir la rejouer.
    try:
        identifiant_externe = expediteur.envoyer(en_cours)
    except ErreurEnvoiCertaine as exc:
        # 6. Rien n'est parti : retour a VALIDEE, l'humain (ou un cycle) peut reessayer.
        with depot.transaction():
            _maj(depot, dataclasses.replace(en_cours, statut=StatutBrouillon.VALIDEE))
            journal.ecrire(
                ACTEUR_SYSTEME,
                "envoi_echec_certain",
                id_relance,
                {"valide_par": en_cours.valide_par, "erreur": type(exc).__name__, "message": str(exc)[:500]},
                horodatage=maintenant,
            )
        raise
    except BaseException as exc:
        # 7. Resultat inconnu (TimeoutError, coupure, interruption...) : le
        #    brouillon RESTE EN_COURS, on ne rejoue jamais.
        _journaliser_incertain(journal, en_cours, exc, maintenant, "resultat de l'emission inconnu")
        raise

    # 5. Succes. Si l'enregistrement echoue alors que le message est parti, le
    #    brouillon reste EN_COURS (donc jamais renvoye) et on le signale.
    envoye = dataclasses.replace(en_cours, statut=StatutBrouillon.ENVOYEE, envoye_le=maintenant)
    try:
        with depot.transaction():
            _maj(depot, envoye)
            depot.enregistrer_envoi(
                EnvoiRelance(
                    id_relance=envoye.id_relance,
                    destinataire=envoye.destinataire,
                    date_envoi=jour_envoi,
                    dossiers=envoye.dossiers,
                    references=envoye.references,
                )
            )
    except BaseException as exc:
        _journaliser_incertain(
            journal, en_cours, exc, maintenant, "message emis mais enregistrement de l'envoi echoue"
        )
        raise
    journal.ecrire(
        ACTEUR_SYSTEME,
        "envoi_effectue",
        id_relance,
        {
            "valide_par": envoye.valide_par,
            "destinataire": envoye.destinataire,
            "identifiant_externe": str(identifiant_externe),
        },
        horodatage=maintenant,
    )
    return envoye


def envois_incertains(depot: Depot) -> list[Brouillon]:
    """Brouillons EN_COURS : emission commencee, resultat inconnu, a trancher par un humain."""
    return depot.lister_brouillons(StatutBrouillon.EN_COURS)


def trancher_envoi_incertain(
    depot: Depot,
    journal: JournalAudit,
    id_relance: str,
    par: str,
    *,
    parti: bool,
    maintenant: dt.datetime,
) -> Brouillon:
    """Seul moyen de sortir de EN_COURS : un humain constate ce qui s'est passe.

    `parti=True`  : le message est bien parti -> ENVOYEE, envoi enregistre.
    `parti=False` : le message n'est pas parti -> VALIDEE (un nouvel envoi redevient possible,
                    par decision humaine explicite et tracee).
    """
    if not isinstance(parti, bool):
        raise TypeError("`parti` doit etre un booleen : la decision humaine est explicite")
    try:
        return _trancher(depot, journal, id_relance, par, parti, maintenant)
    except EnvoiNonValide as exc:
        _refus(journal, "trancher_envoi_incertain", id_relance, exc, maintenant, par)
        raise


def _trancher(
    depot: Depot, journal: JournalAudit, id_relance: str, par: str, parti: bool, maintenant: dt.datetime
) -> Brouillon:
    par = _exiger_humain(par)
    _exiger_horodatage(maintenant)
    with depot.transaction():
        b = _exiger_brouillon(depot, id_relance)
        if b.statut is not StatutBrouillon.EN_COURS:
            raise EnvoiNonValide(
                f"brouillon {id_relance} au statut {b.statut.value} : seul un envoi "
                f"{StatutBrouillon.EN_COURS.value} se tranche"
            )
        details = {"par": par, "valide_par": b.valide_par, "destinataire": b.destinataire}
        if parti:
            tranche = dataclasses.replace(b, statut=StatutBrouillon.ENVOYEE, envoye_le=maintenant)
            _maj(depot, tranche)
            depot.enregistrer_envoi(
                EnvoiRelance(
                    id_relance=b.id_relance,
                    destinataire=b.destinataire,
                    date_envoi=maintenant.astimezone(ZoneInfo(FUSEAU_DEFAUT)).date(),
                    dossiers=b.dossiers,
                    references=b.references,
                )
            )
            action = "envoi_confirme_par_humain"
        else:
            tranche = dataclasses.replace(b, statut=StatutBrouillon.VALIDEE)
            _maj(depot, tranche)
            action = "envoi_declare_non_parti"
        # Dans la transaction : pas de decision humaine sans trace d'audit.
        journal.ecrire(par, action, id_relance, details, horodatage=maintenant)
    return tranche


# ---------------------------------------------------------------------------
# Adaptateur de test et de demonstration : un .eml par relance
# ---------------------------------------------------------------------------

_ID_SUR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ADRESSE = re.compile(r"[^\s@,;<>()\[\]\\\"]+@[^\s@,;<>()\[\]\\\"]+")
DOMAINE_MESSAGE_ID = "relances.invalid"


def _champ_une_ligne(nom: str, valeur: str) -> str:
    """Refuse tout caractere de controle ou saut de ligne : pas d'injection d'en-tete."""
    if not isinstance(valeur, str) or any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in valeur):
        raise ErreurEnvoiCertaine(f"{nom} : saut de ligne ou caractere de controle interdit")
    return valeur


class ExpediteurDossier:
    """Ecrit `<id_relance>.eml` (RFC 5322) dans un repertoire. Ne touche pas au reseau."""

    def __init__(self, repertoire: Path | str, *, expediteur: str = "relances@cabinet.invalid") -> None:
        self._repertoire = Path(repertoire)
        self._expediteur = expediteur

    def envoyer(self, brouillon: Brouillon) -> str:
        # Defense en profondeur : cet adaptateur n'emet que ce que `envoyer`
        # lui remet, c'est-a-dire un brouillon EN_COURS valide par un humain.
        if brouillon.statut is not StatutBrouillon.EN_COURS:
            raise ErreurEnvoiCertaine(f"statut {brouillon.statut.value} : emission refusee")
        try:
            _exiger_humain(brouillon.valide_par)
        except EnvoiNonValide:
            raise ErreurEnvoiCertaine("valide_par vide ou automatique : emission refusee") from None
        if not _ID_SUR.fullmatch(brouillon.id_relance):
            raise ErreurEnvoiCertaine("id_relance inutilisable comme nom de fichier")

        destinataire = _champ_une_ligne("destinataire", brouillon.destinataire).strip()
        if not _ADRESSE.fullmatch(destinataire):
            raise ErreurEnvoiCertaine("destinataire : une seule adresse simple est attendue")
        objet = _champ_une_ligne("objet", brouillon.objet)
        expediteur = _champ_une_ligne("expediteur", self._expediteur)

        identifiant = f"<{brouillon.id_relance}@{DOMAINE_MESSAGE_ID}>"
        date = brouillon.valide_le or dt.datetime.combine(brouillon.cree_le, dt.time(), tzinfo=dt.timezone.utc)

        msg = EmailMessage(policy=policy.SMTP)
        msg["From"] = expediteur
        msg["To"] = destinataire
        msg["Subject"] = objet
        msg["Date"] = date
        msg["Message-ID"] = identifiant
        msg.set_content(brouillon.corps)  # le corps n'est jamais interprete comme en-tete
        donnees = msg.as_bytes()

        cible = self._repertoire / f"{brouillon.id_relance}.eml"
        try:
            self._repertoire.mkdir(parents=True, exist_ok=True)
            # Fichier temporaire complet puis lien : jamais de .eml partiel, et
            # `os.link` echoue si la cible existe (pas d'ecrasement possible).
            fd, tmp = tempfile.mkstemp(dir=self._repertoire, prefix=".tmp-", suffix=".part")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(donnees)
                    f.flush()
                    os.fsync(f.fileno())
                os.link(tmp, cible)
            finally:
                os.unlink(tmp)
        except FileExistsError:
            # Un .eml existe deja : l'envoi a peut-etre eu lieu. Incertain, donc ErreurEnvoi simple.
            raise ErreurEnvoi(f"{cible.name} existe deja : refus d'ecraser") from None
        except OSError as exc:
            raise ErreurEnvoi(f"ecriture impossible : {exc}") from exc
        return identifiant
