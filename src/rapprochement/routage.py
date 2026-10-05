"""Routage des pieces entrantes : rattacher un fichier recu par e-mail au bon dossier.

Cahier des charges section 7, contrat `docs/architecture_mvp.md` section 4.4.

C'est le seul module ou une erreur expose les donnees d'un client a un autre
(critere CA-03, bloquant). Le parti pris est donc inverse de celui du moteur :
la prudence passe avant la completude. Tout ce qui n'est pas certain part en
file humaine (`NON_ROUTEE`), et meme une decision `PROPOSEE` n'est qu'une
proposition qu'un humain confirme.

Garanties structurelles :
  - le dossier est resolu a partir de l'expediteur SEUL, avant de regarder une
    seule piece attendue ;
  - les candidates sont filtrees sur ce dossier AVANT toute comparaison de
    montant : une piece d'un autre dossier ne peut pas etre proposee, quelle que
    soit la qualite de la correspondance ;
  - un domaine n'identifie un dossier que s'il a ete DECLARE par le cabinet
    (`Dossier.domaines`) : jamais deduit de l'adresse d'un contact, car une
    liste noire de messageries grand public est incomplete par nature
    (hotmail.ca, t-online.de...). Un domaine grand public declare par erreur
    reste refuse ;
  - aucune ambiguite n'est tranchee : ni pour le dossier, ni pour la periode, ni
    pour le montant, ni pour l'operation.

Module pur pour `router`, `extraire_montants`, `cle_idempotence`,
`adresses_par_dossier` et `domaines_par_dossier` (pas d'horloge, pas de disque). Seules `lire_eml` et
`lire_dossier_eml` lisent le disque.
"""

from __future__ import annotations

import datetime as dt
import email
import email.header
import email.policy
import email.utils
import hashlib
import html
import json
import os
import posixpath
import re
from collections.abc import Collection, Iterable, Mapping
from decimal import Decimal
from email.message import EmailMessage, Message
from pathlib import Path

from .modeles import (
    ETATS_TERMINAUX,
    DecisionRoutage,
    Dossier,
    FichierEntrant,
    MessageEntrant,
    MotifNonRoute,
    PieceAttendue,
    StatutRoutage,
)
from .moteur import montants_compatibles

# ---------------------------------------------------------------------------
# Parametres
# ---------------------------------------------------------------------------

# Fenetre autour de la date d'operation dans laquelle doit tomber la date lue
# dans le nom du fichier (cahier des charges section 7). Bornes incluses.
FENETRE_AVANT_JOURS = 60
FENETRE_APRES_JOURS = 15

# Garde-fou EN PROFONDEUR seulement : la defense principale est qu'aucun domaine
# n'est deduit (voir `domaines_par_dossier`). Cette liste empeche qu'une erreur
# de saisie du cabinet (declarer gmail.com pour un dossier) ouvre la fuite.
# Liste du contrat 4.4, reprise a l'identique.
DOMAINES_GRAND_PUBLIC_CONTRAT = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "outlook.fr",
        "hotmail.com", "hotmail.fr", "live.com", "yahoo.com", "yahoo.fr",
        "icloud.com", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr",
        "laposte.net", "gmx.com", "proton.me", "protonmail.com",
    }
)

# Complement volontaire : la liste du contrat est un exemple, la regle est
# "un domaine grand public ne prouve rien". Un dossier dont le contact est en
# @live.fr ou @msn.com capterait sinon tout expediteur de ce domaine. Ajouter
# un domaine ici ne peut que renvoyer plus de cas en file humaine : c'est le
# sens d'erreur sans danger.
DOMAINES_GRAND_PUBLIC_COMPLEMENT = frozenset(
    {
        "live.fr", "live.be", "msn.com", "hotmail.be", "hotmail.co.uk",
        "hotmail.de", "hotmail.it", "hotmail.es", "outlook.be", "outlook.de",
        "outlook.es", "outlook.it", "yahoo.co.uk", "yahoo.de", "yahoo.es",
        "yahoo.it", "ymail.com", "rocketmail.com", "aol.com", "aol.fr",
        "me.com", "mac.com", "neuf.fr", "club-internet.fr", "aliceadsl.fr",
        "numericable.fr", "bbox.fr", "cegetel.net", "noos.fr", "voila.fr",
        "gmx.fr", "gmx.de", "gmx.net", "web.de", "protonmail.ch", "pm.me",
        "tutanota.com", "tuta.io", "zoho.com", "yandex.com", "yandex.ru",
        "mail.com", "mailo.com", "skynet.be", "telenet.be", "bluewin.ch",
        "sfr.net", "free.com",
    }
)

DOMAINES_GRAND_PUBLIC = DOMAINES_GRAND_PUBLIC_CONTRAT | DOMAINES_GRAND_PUBLIC_COMPLEMENT

# Circuits dont l'adresse `email_contact` n'identifie PAS un seul client.
# C-INTERNE : le destinataire est un collaborateur du cabinet (section 4 du
# cahier des charges) ; son adresse et surtout son domaine (celui du cabinet)
# couvrent tous les dossiers. Les rattacher a un dossier ferait de ce dossier
# la destination de tout courrier interne.
CIRCUITS_SANS_ADRESSE_CLIENT = frozenset({"C-INTERNE"})

# ---------------------------------------------------------------------------
# Montants
# ---------------------------------------------------------------------------

_ESPACES = "    "
_DEVISE = r"(?:€|(?<![^\W\d_])(?:euros?|eur)(?![^\W\d_]))"
_MOTIF_MONTANT = re.compile(
    rf"""
    (?:(?P<avant>{_DEVISE})[{_ESPACES}]?)?
    (?<![0-9])(?<![0-9][.,])                      # pas en plein milieu d'un nombre
    (?P<entier>
        [0-9]{{1,3}}(?P<sep>[{_ESPACES}.,])[0-9]{{3}}(?:(?P=sep)[0-9]{{3}})*
      | [0-9]+
    )
    (?:(?P<dec>[.,])(?P<frac>[0-9]{{1,2}}))?
    (?![0-9])
    (?![.,/:\-][0-9])                             # date, heure, numero : 12.04.2026, 12/04
    (?:[{_ESPACES}]?(?P<apres>{_DEVISE}))?
    """,
    re.VERBOSE | re.IGNORECASE,
)
# Caracteres qui, colles a gauche d'un nombre, en font une reference
# (n°4471, FA2026.04, #12,50) et non un montant.
_GAUCHE_INTERDITE = set("°#/.,")
_POURCENT = re.compile(rf"[{_ESPACES}]*[%‰]")
_CENTIME = Decimal("0.01")


def _montant_depuis(m: re.Match[str], texte: str) -> Decimal | None:
    entier, sep, dec, frac = m["entier"], m["sep"], m["dec"], m["frac"]
    devise = bool(m["avant"] or m["apres"])

    debut = m.start("entier") if not m["avant"] else m.start("avant")
    if debut > 0:
        gauche = texte[debut - 1]
        if gauche in _GAUCHE_INTERDITE or (gauche.isalnum() and not m["avant"]):
            return None
    if not m["apres"] and m.end() < len(texte) and texte[m.end()].isalnum():
        return None                                     # 89,90kg, 12.50x
    if _POURCENT.match(texte, m.end()):
        return None                                     # TVA 20,00 %

    if sep is not None:
        if dec is not None and dec == sep:
            return None                                 # 1,234,56 : incoherent
        if entier[0] == "0":
            return None                                 # 0.500 : pas un separateur de milliers
        entier = entier.replace(sep, "")
    if frac is None:
        if not devise:
            return None                                 # entier nu : 2026, n° de facture
        valeur = Decimal(entier)
    else:
        if len(frac) != 2 and not devise:
            return None                                 # 5,5 sans devise : taux, version...
        valeur = Decimal(f"{entier}.{frac}")
    return valeur.quantize(_CENTIME)


def extraire_montants(texte: str) -> list[Decimal]:
    """Montants lisibles dans un texte, dans l'ordre d'apparition (doublons compris).

    Un montant porte toujours deux decimales ("89,90", "1 234,56", "1.234,56",
    "1,234.56", "1234.56") ou un symbole de devise ("89 EUR", "€ 1 234", "1,5 €").
    Ne sont jamais lus comme montants : un entier nu (2026, 20260412, n°4471), un
    pourcentage (TVA 20 %, 5,50 %), une date ou une heure (12/04, 12.04.2026,
    14:30), un nombre colle a une reference (FA2026.04). En cas de forme
    incoherente (1,234,56), rien n'est lu : un montant manquant envoie en file
    humaine, un montant invente pourrait proposer la mauvaise operation.
    Resultat quantifie au centime.
    """
    if not texte:
        return []
    montants: list[Decimal] = []
    for m in _MOTIF_MONTANT.finditer(texte):
        valeur = _montant_depuis(m, texte)
        if valeur is not None:
            montants.append(valeur)
    return montants


# ---------------------------------------------------------------------------
# Dates lisibles dans un nom de fichier
# ---------------------------------------------------------------------------

_DATE_JOUR = re.compile(r"(?<![0-9])(20[0-9]{2})-([0-9]{2})-([0-9]{2})(?![0-9])")
_DATE_MOIS_TIRET = re.compile(r"(?<![0-9])(20[0-9]{2})-([0-9]{2})(?![0-9])(?!-[0-9])")
_DATE_MOIS_COLLE = re.compile(r"(?<![0-9])(20[0-9]{2})([0-9]{2})(?![0-9])")


def _fin_de_mois(annee: int, mois: int) -> dt.date:
    if mois == 12:
        return dt.date(annee, 12, 31)
    return dt.date(annee, mois + 1, 1) - dt.timedelta(days=1)


def _dates_du_nom(nom: str) -> list[tuple[dt.date, dt.date]]:
    """Dates lues dans un nom de fichier, sous forme d'intervalles [debut, fin].

    Formats du contrat : AAAA-MM-JJ (un jour), AAAA-MM et AAAAMM (un mois
    entier). Une forme invalide (mois 13, 30 fevrier) est ignoree. Tri et
    dedoublonnage pour le determinisme.
    """
    intervalles: set[tuple[dt.date, dt.date]] = set()
    for m in _DATE_JOUR.finditer(nom):
        try:
            jour = dt.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            continue
        intervalles.add((jour, jour))
    for motif in (_DATE_MOIS_TIRET, _DATE_MOIS_COLLE):
        for m in motif.finditer(nom):
            annee, mois = int(m[1]), int(m[2])
            if not 1 <= mois <= 12:
                continue
            intervalles.add((dt.date(annee, mois, 1), _fin_de_mois(annee, mois)))
    return sorted(intervalles)


def _periode(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _dans_la_fenetre(intervalles: list[tuple[dt.date, dt.date]], piece: PieceAttendue) -> bool:
    """Toutes les dates du nom doivent pouvoir tomber dans la fenetre (bornes incluses).

    Une date au mois pres (AAAA-MM) est compatible si un jour de ce mois l'est.
    """
    debut_fenetre = piece.date_operation - dt.timedelta(days=FENETRE_AVANT_JOURS)
    fin_fenetre = piece.date_operation + dt.timedelta(days=FENETRE_APRES_JOURS)
    return all(debut <= fin_fenetre and fin >= debut_fenetre for debut, fin in intervalles)


# ---------------------------------------------------------------------------
# Adresses
# ---------------------------------------------------------------------------

_CARACTERES_INTERDITS_ADRESSE = re.compile(r"[\s<>()\[\],;:\"\\]")


def _normaliser_adresse(adresse: str) -> str | None:
    """Adresse seule, en minuscules, ou None si elle n'est pas exploitable.

    Exactement un "@", partie locale et domaine non vides, aucun caractere de
    structure (chevrons, virgule, espace...). Une adresse douteuse ne prouve rien.
    """
    a = (adresse or "").strip().lower()
    if a.count("@") != 1 or _CARACTERES_INTERDITS_ADRESSE.search(a):
        return None
    locale, domaine = a.split("@")
    if domaine.endswith("."):
        domaine = domaine[:-1]                      # point final du FQDN : meme domaine
    if not locale or not domaine or domaine.startswith(".") or domaine.endswith("."):
        return None
    return f"{locale}@{domaine}"


def _cle_boite(adresse: str) -> str | None:
    """Cle de comparaison d'ADRESSE EXACTE : meme boite aux lettres.

    Adresse normalisee (casse, point final du domaine) dont la partie locale
    perd son suffixe `+tag` (tout ce qui suit le premier "+"). Rien d'autre
    n'est normalise : `j.ean@` et `jean@` restent distinctes, et le domaine est
    compare tel quel.
    """
    a = _normaliser_adresse(adresse)
    if a is None:
        return None
    locale, domaine = a.split("@")
    base = locale.split("+", 1)[0]
    if not base:
        return None
    return f"{base}@{domaine}"


def _domaine(adresse: str) -> str:
    return adresse.rsplit("@", 1)[1]


_CARACTERES_INTERDITS_DOMAINE = re.compile(r"[\s@<>()\[\],;:\"\\/]")


def normaliser_domaine(domaine: str) -> str | None:
    """Domaine en minuscules, sans point final, ou None s'il n'est pas comparable.

    Seuls les domaines ASCII sont comparables (forme punycode `xn--...` comprise).
    Aucune conversion IDNA : le codec `idna` de Python applique IDNA 2003, qui
    replie `straße.de` sur `strasse.de`, un AUTRE domaine enregistrable. Un
    domaine non ASCII, avec un caractere interdit, un label vide ou sans point
    ne prouve donc rien.
    """
    d = (domaine or "").strip().lower()
    if d.endswith("."):
        d = d[:-1]
    if not d or not d.isascii() or "." not in d or _CARACTERES_INTERDITS_DOMAINE.search(d):
        return None
    if any(not label for label in d.split(".")):
        return None
    return d


def est_domaine_grand_public(domaine: str) -> bool:
    """Domaine de messagerie grand public, ou sous-domaine d'un tel domaine."""
    d = normaliser_domaine(domaine) or (domaine or "").strip().lower().rstrip(".")
    return any(d == g or d.endswith("." + g) for g in DOMAINES_GRAND_PUBLIC)


def _adresses_du_champ(champ: str) -> list[str]:
    """Adresses d'un champ libre : "a@x.fr", "Nom <a@x.fr>", "a@x.fr; b@y.fr"."""
    trouvees: list[str] = []
    for _, spec in email.utils.getaddresses([(champ or "").replace(";", ",")]):
        a = _normaliser_adresse(spec)
        if a is not None:
            trouvees.append(a)
    return trouvees


def adresses_par_dossier(dossiers: Mapping[str, Dossier]) -> dict[str, frozenset[str]]:
    """Adresses qui identifient un dossier, normalisees (minuscules, adresse seule).

    Seules les adresses du CLIENT sont retenues : `email_contact`, sauf pour les
    circuits ou ce champ designe un collaborateur du cabinet (C-INTERNE).
    `email_relais` n'est JAMAIS retenu : en C-RELAIS c'est l'adresse du cabinet
    donneur d'ordre, qui envoie les pieces de plusieurs clients ; la retenir
    rattacherait au seul dossier enregistre les pieces de tous ses autres
    clients (et son domaine ferait de meme pour tous ses collaborateurs).
    Les pieces envoyees par un relais partent donc en file humaine.

    La cle du mapping doit etre le code du dossier (`ValueError` sinon) : c'est
    ce code qui filtre les pieces attendues, un decalage enverrait les pieces
    d'un dossier vers un autre.
    """
    resultat: dict[str, frozenset[str]] = {}
    for cle in sorted(dossiers):
        dossier = dossiers[cle]
        _verifier_cle(cle, dossier)
        if dossier.circuit in CIRCUITS_SANS_ADRESSE_CLIENT:
            resultat[cle] = frozenset()
        else:
            resultat[cle] = frozenset(_adresses_du_champ(dossier.email_contact))
    return resultat


def _verifier_cle(cle: str, dossier: Dossier) -> None:
    if cle != dossier.code:
        raise ValueError(
            f"Referentiel incoherent : cle {cle!r} pour le dossier {dossier.code!r}"
        )


def _domaines_valides(domaines: Iterable[str]) -> frozenset[str]:
    """Domaines declares, normalises ; les invalides et les grand public sont ecartes."""
    retenus: set[str] = set()
    for brut in domaines:
        d = normaliser_domaine(brut)
        if d is not None and not est_domaine_grand_public(d):
            retenus.add(d)
    return frozenset(retenus)


def domaines_par_dossier(dossiers: Mapping[str, Dossier]) -> dict[str, frozenset[str]]:
    """Domaines DECLARES par le cabinet pour chaque dossier (`Dossier.domaines`).

    Seule source admise pour le routage par domaine : rien n'est deduit de
    `email_contact`. Normalises (minuscules, sans point final). Un domaine non
    ASCII est ignore, sans exception (ecrire sa forme punycode `xn--...`). Un domaine
    de messagerie grand public declare par erreur est ecarte (garde-fou en
    profondeur), un domaine illisible aussi. Cle du mapping = code du dossier
    (`ValueError` sinon).
    """
    resultat: dict[str, frozenset[str]] = {}
    for cle in sorted(dossiers):
        dossier = dossiers[cle]
        _verifier_cle(cle, dossier)
        resultat[cle] = _domaines_valides(dossier.domaines)
    return resultat


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def empreinte_contenu(contenu: bytes) -> str:
    """SHA-256 hexadecimal du contenu d'un fichier."""
    return hashlib.sha256(contenu).hexdigest()


def cle_idempotence(message_id: str, empreinte: str) -> str:
    """SHA-256 de (message_id, empreinte du fichier).

    Les deux champs sont encodes sans ambiguite (liste JSON) plutot que
    concatenes : avec une simple concatenation, un Message-ID choisi par
    l'expediteur et se terminant par 64 caracteres hexadecimaux produirait la
    meme cle qu'un autre couple, et une piece serait ecartee comme DOUBLON.
    """
    charge = json.dumps([message_id, empreinte], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(charge.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Routage
# ---------------------------------------------------------------------------


def _resoudre_dossier(
    expediteur: str,
    adresses: Mapping[str, Collection[str]],
    domaines: Mapping[str, Collection[str]] | None,
) -> tuple[str | None, MotifNonRoute | None, str]:
    """(dossier, motif, detail). Exactement l'un de dossier / motif est renseigne.

    1. adresse exacte : un seul dossier -> lui, plusieurs -> DOSSIER_AMBIGU ;
    2. sinon domaine de l'expediteur DECLARE (`domaines`) par un seul dossier,
       en correspondance exacte (un sous-domaine ne correspond pas) ;
       plusieurs -> DOSSIER_AMBIGU. `domaines` None ou vide : aucun repli.
    Le domaine des adresses n'est JAMAIS utilise.
    """
    exp = _normaliser_adresse(expediteur)
    if exp is None:
        return None, MotifNonRoute.DOSSIER_INCONNU, f"expediteur illisible : {expediteur!r}"

    cle_exp = _cle_boite(exp)
    exacts: list[str] = []
    if cle_exp is not None:
        for code in sorted(adresses):
            if any(_cle_boite(x) == cle_exp for x in adresses[code]):
                exacts.append(code)
    if len(exacts) == 1:
        return exacts[0], None, f"adresse exacte {exp}"
    if len(exacts) > 1:
        return None, MotifNonRoute.DOSSIER_AMBIGU, (
            f"adresse {exp} presente dans {len(exacts)} dossiers : {', '.join(exacts)}"
        )

    if not domaines:
        return None, MotifNonRoute.DOSSIER_INCONNU, (
            f"adresse {exp} inconnue, aucun domaine declare : pas de repli par domaine"
        )
    domaine = normaliser_domaine(_domaine(exp))
    if domaine is None:
        return None, MotifNonRoute.DOSSIER_INCONNU, f"domaine illisible : {exp}"
    if est_domaine_grand_public(domaine):
        return None, MotifNonRoute.DOSSIER_INCONNU, (
            f"adresse {exp} inconnue, domaine grand public {domaine} : aucune preuve"
        )
    par_domaine = [code for code in sorted(domaines) if domaine in _domaines_valides(domaines[code])]
    if len(par_domaine) == 1:
        return par_domaine[0], None, f"domaine declare {domaine}"
    if len(par_domaine) > 1:
        return None, MotifNonRoute.DOSSIER_AMBIGU, (
            f"domaine {domaine} partage par {len(par_domaine)} dossiers : {', '.join(par_domaine)}"
        )
    return None, MotifNonRoute.DOSSIER_INCONNU, f"adresse {exp} inconnue, domaine {domaine} non declare"


def _decision_fichier(
    message: MessageEntrant,
    nom: str,
    empreinte: str,
    dossier: str,
    ouvertes: list[PieceAttendue],
) -> DecisionRoutage:
    """Periode puis operation, pour un fichier dont le dossier est deja resolu.

    `ouvertes` ne contient QUE des pieces non terminales du dossier resolu.
    """

    def non_routee(motif: MotifNonRoute, detail: str, periode: str | None = None) -> DecisionRoutage:
        return DecisionRoutage(
            message_id=message.message_id, nom_fichier=nom, empreinte=empreinte,
            statut=StatutRoutage.NON_ROUTEE, dossier=dossier, periode=periode,
            motif=motif, detail=detail,
        )

    # Defense en profondeur : l'appelant a deja filtre, on reverifie.
    ouvertes = [p for p in ouvertes if p.dossier == dossier and p.etat not in ETATS_TERMINAUX]

    periodes = sorted({p.periode for p in ouvertes})
    dates_nom = _dates_du_nom(nom)
    if not periodes:
        return non_routee(MotifNonRoute.AUCUN_CANDIDAT, "aucune piece attendue ouverte pour ce dossier")
    if len(periodes) == 1:
        periode = periodes[0]
    else:
        periodes_nom = sorted({_periode(debut) for debut, _ in dates_nom} | {_periode(fin) for _, fin in dates_nom})
        if len(periodes_nom) == 1 and periodes_nom[0] in periodes:
            periode = periodes_nom[0]
        else:
            lues = ", ".join(periodes_nom) if periodes_nom else "aucune date lisible"
            return non_routee(
                MotifNonRoute.PERIODE_AMBIGUE,
                f"periodes ouvertes {', '.join(periodes)} ; nom du fichier : {lues}",
            )

    texte = f"{nom} {message.objet} {message.corps}"
    distincts = sorted(set(extraire_montants(texte)))
    if len(distincts) != 1:
        lus = ", ".join(str(m) for m in distincts) if distincts else "aucun"
        return non_routee(MotifNonRoute.MONTANT_INCONNU, f"montants lus : {lus}", periode)
    montant = distincts[0]

    candidates = sorted(
        (
            p for p in ouvertes
            if p.periode == periode
            and montants_compatibles(p.montant, montant)
            and _dans_la_fenetre(dates_nom, p)
        ),
        key=lambda p: (p.date_operation, p.reference),
    )
    if not candidates:
        return non_routee(
            MotifNonRoute.AUCUN_CANDIDAT, f"aucune piece attendue a {montant} sur {periode}", periode
        )
    if len(candidates) > 1:
        refs = ", ".join(p.reference for p in candidates)
        return non_routee(
            MotifNonRoute.CANDIDATS_MULTIPLES, f"{len(candidates)} candidates a {montant} : {refs}", periode
        )
    retenue = candidates[0]
    return DecisionRoutage(
        message_id=message.message_id, nom_fichier=nom, empreinte=empreinte,
        statut=StatutRoutage.PROPOSEE, dossier=dossier, periode=periode,
        reference_operation=retenue.reference,
        detail=f"montant {montant} ~ {retenue.montant} du {retenue.date_operation.isoformat()}"
        " : proposition a confirmer par un humain",
    )


def router(
    message: MessageEntrant,
    *,
    adresses: Mapping[str, Collection[str]],
    attendues: Iterable[PieceAttendue],
    deja_vus: Collection[str] = (),
    aujourdhui: dt.date,
    domaines: Mapping[str, Collection[str]] | None = None,
) -> list[DecisionRoutage]:
    """Une decision par fichier joint, dans l'ordre des fichiers du message.

    `adresses` : adresses exactes par dossier (`adresses_par_dossier`).
    `domaines` : domaines DECLARES par dossier (`domaines_par_dossier`) ; None
    ou vide desactive tout routage par domaine.

    Un message sans fichier produit une decision `NON_ROUTEE / SANS_PIECE_JOINTE`
    (ou `DOUBLON` s'il a deja ete vu). Aucune decision n'est jamais une
    confirmation : `PROPOSEE` attend la validation d'un humain.

    `aujourdhui` fait partie du contrat mais aucune regle 4.4 ne l'utilise.
    """
    del aujourdhui  # voir docstring : parametre du contrat, sans regle associee
    if not (message.message_id or "").strip():
        raise ValueError("message_id vide : l'idempotence ne peut pas etre garantie")

    vus = set(deja_vus)

    if not message.fichiers:
        cle = cle_idempotence(message.message_id, "")
        if cle in vus:
            return [DecisionRoutage(
                message_id=message.message_id, nom_fichier="", empreinte="",
                statut=StatutRoutage.DOUBLON, detail="message deja traite",
            )]
        return [DecisionRoutage(
            message_id=message.message_id, nom_fichier="", empreinte="",
            statut=StatutRoutage.NON_ROUTEE, motif=MotifNonRoute.SANS_PIECE_JOINTE,
            detail="message sans piece jointe",
        )]

    # 1. Le dossier ne depend que de l'expediteur : resolu une fois, AVANT de
    #    regarder la moindre piece attendue.
    dossier, motif, detail_dossier = _resoudre_dossier(message.expediteur, adresses, domaines)

    # 2. Filtre sur le dossier AVANT toute comparaison de montant (regle 5).
    ouvertes: list[PieceAttendue] = []
    if dossier is not None:
        ouvertes = [
            p for p in attendues if p.dossier == dossier and p.etat not in ETATS_TERMINAUX
        ]

    decisions: list[DecisionRoutage] = []
    for fichier in message.fichiers:
        empreinte = empreinte_contenu(fichier.contenu)
        cle = cle_idempotence(message.message_id, empreinte)
        if cle in vus:
            decisions.append(DecisionRoutage(
                message_id=message.message_id, nom_fichier=fichier.nom, empreinte=empreinte,
                statut=StatutRoutage.DOUBLON, detail="fichier deja traite pour ce message",
            ))
            continue
        vus.add(cle)  # meme contenu joint deux fois au meme message : un seul effet
        if dossier is None:
            assert motif is not None
            decisions.append(DecisionRoutage(
                message_id=message.message_id, nom_fichier=fichier.nom, empreinte=empreinte,
                statut=StatutRoutage.NON_ROUTEE, motif=motif, detail=detail_dossier,
            ))
            continue
        decisions.append(_decision_fichier(message, fichier.nom, empreinte, dossier, ouvertes))
    return decisions


# ---------------------------------------------------------------------------
# Lecture des .eml
# ---------------------------------------------------------------------------

_CONTROLE = re.compile(r"[\x00-\x1f\x7f]")
_MOT_ENCODE = re.compile(r"=\?[^?]+\?[bBqQ]\?[^?]*\?=")
_EPOQUE = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _nom_sur(brut: str | None, rang: int) -> str:
    """Nom de fichier sans chemin, decode, sans caractere de controle."""
    nom = brut or ""
    if _MOT_ENCODE.search(nom):
        try:
            nom = str(email.header.make_header(email.header.decode_header(nom)))
        except (ValueError, LookupError, UnicodeError):
            pass
    nom = _CONTROLE.sub("", nom).replace("\\", "/")
    nom = posixpath.basename(os.path.basename(nom)).strip()
    if nom in ("", ".", ".."):
        return f"piece_jointe_{rang}"
    return nom


def _expediteur(msg: Message) -> str:
    """Adresse seule, en minuscules ; "" si absente, multiple ou douteuse.

    Deux analyseurs (en-tete type de `policy.default` et `email.utils`) doivent
    donner la meme adresse unique : un en-tete From construit pour etre lu
    differemment par deux logiciels n'est pas une preuve d'identite.
    """
    try:
        valeurs = msg.get_all("From") or []
        if len(valeurs) != 1:
            return ""
        entete = valeurs[0]
        if getattr(entete, "defects", ()):
            return ""
        adresses = getattr(entete, "addresses", None)
        if adresses is None or len(adresses) != 1:
            return ""
        principale = _normaliser_adresse(adresses[0].addr_spec)
        brut = [v for k, v in msg.raw_items() if k.lower() == "from"]
        secondes = email.utils.getaddresses([str(b) for b in brut])
    except Exception:  # en-tete malforme : file humaine, jamais d'exception
        return ""
    if principale is None or len(secondes) != 1:
        return ""
    if _normaliser_adresse(secondes[0][1]) != principale:
        return ""
    return principale


def _message_id(msg: Message, brut: bytes) -> str:
    try:
        valeur = msg.get("Message-ID")
        texte = "".join(str(valeur).split()) if valeur is not None else ""
    except Exception:
        texte = ""
    return texte or "sha256:" + hashlib.sha256(brut).hexdigest()


def _recu_le(msg: Message) -> dt.datetime:
    """Date de l'en-tete Date, avec fuseau ; epoque UTC si absente ou illisible."""
    try:
        valeur = msg.get("Date")
        if valeur is None:
            return _EPOQUE
        quand = getattr(valeur, "datetime", None) or email.utils.parsedate_to_datetime(str(valeur))
    except Exception:
        return _EPOQUE
    if quand is None:
        return _EPOQUE
    if quand.tzinfo is None:
        quand = quand.replace(tzinfo=dt.timezone.utc)
    return quand


def _texte_partie(partie: Message) -> str:
    try:
        contenu = partie.get_content()  # type: ignore[attr-defined]
        if isinstance(contenu, str):
            return contenu
    except Exception:
        pass
    charge = partie.get_payload(decode=True) or b""
    if isinstance(charge, bytes):
        return charge.decode(partie.get_content_charset() or "utf-8", errors="replace")
    return ""


def _corps(msg: EmailMessage) -> str:
    try:
        partie = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        partie = None
    if partie is None:
        return ""
    texte = _texte_partie(partie)
    if partie.get_content_subtype() == "html":
        texte = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", texte)
        texte = html.unescape(re.sub(r"(?s)<[^>]+>", " ", texte))
    return texte


def _est_piece_jointe(partie: Message) -> tuple[bool, str | None]:
    try:
        nom = partie.get_filename()
    except Exception:
        nom = None
    try:
        disposition = partie.get_content_disposition()
    except Exception:
        disposition = None
    return (disposition == "attachment" or nom is not None), nom


def lire_eml(chemin: Path | str) -> MessageEntrant:
    """Lit un fichier .eml (RFC 5322) en `MessageEntrant`.

    Pieces jointes : parties feuilles avec `Content-Disposition: attachment` ou un
    nom de fichier, y compris dans un message transfere (message/rfc822), dans
    l'ordre du message. Nom : decode (RFC 2047 / 2231), sans chemin.
    """
    brut = Path(chemin).read_bytes()
    msg = email.message_from_bytes(brut, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)

    fichiers: list[FichierEntrant] = []
    for partie in msg.walk():
        if partie.is_multipart():
            continue
        jointe, nom = _est_piece_jointe(partie)
        if not jointe:
            continue
        charge = partie.get_payload(decode=True)
        contenu = charge if isinstance(charge, bytes) else b""
        fichiers.append(FichierEntrant(nom=_nom_sur(nom, len(fichiers) + 1), contenu=contenu))

    try:
        objet = str(msg.get("Subject") or "")
    except Exception:
        objet = ""
    return MessageEntrant(
        message_id=_message_id(msg, brut),
        expediteur=_expediteur(msg),
        recu_le=_recu_le(msg),
        objet=objet,
        corps=_corps(msg),
        fichiers=tuple(fichiers),
    )


def lire_dossier_eml(repertoire: Path | str) -> list[MessageEntrant]:
    """Tous les .eml d'un repertoire (non recursif), tries par nom de fichier."""
    chemins = sorted(
        (p for p in Path(repertoire).iterdir() if p.is_file() and p.suffix.lower() == ".eml"),
        key=lambda p: p.name,
    )
    return [lire_eml(p) for p in chemins]
