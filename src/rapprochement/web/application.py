"""Application WSGI de l'interface (bibliotheque standard uniquement).

REGLE D'OR : aucune logique metier ici. Chaque action appelle la MEME fonction de
`cycle` que la ligne de commande `scripts/cycle.py`, avec `par` = nom affiche de
l'utilisateur CONNECTE ; `cycle` verifie ce nom dans `validateurs.txt`. L'application
n'ecrit jamais dans le depot elle-meme : elle le LIT (une connexion SQLite par
requete, jamais partagee entre fils d'execution) et delegue toute ecriture.

Securite (voir docs/application_web.md) :
- en-tetes de securite sur TOUTES les reponses (erreurs et redirections comprises) ;
- controle de l'en-tete Host sur toute requete (rebinding DNS) ;
- toute action est un POST : jeton CSRF de session (comparaison en temps constant),
  `Origin` (s'il est present et n'est pas `null`) et `Sec-Fetch-Site` controles ;
  un GET n'a jamais d'effet de bord sur les donnees ;
- corps de requete plafonne a 64 Kio ; methodes inattendues : 405 ;
- une seule ressource statique, servie depuis un chemin FIXE ;
- erreur du domaine : message en francais dans la page ; erreur interne : page
  generique, detail dans `<instance>/logs/web.log` (jamais de mot de passe ni de jeton).

Les decisions sont serialisees par un verrou d'ecriture (defense en profondeur, en plus
du verrou de fichier du journal d'audit). Les droits web (role du compte) doublent ceux
du cycle (`responsables.txt`) : une operation de responsable est refusee des deux cotes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import http
import json
import logging
import re
import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode
from zoneinfo import ZoneInfo

from .. import cycle
from ..audit import JournalAudit, JournalCorrompu
from ..depot import Depot, ErreurBase, VersionSchemaInconnue
from ..envoi import EnvoiHorsCreneau, EnvoiNonValide, ErreurEnvoi
from ..etats import TransitionInterdite
from ..modeles import Dossier, EtatPiece, StatutBrouillon, StatutRoutage
from ..parseurs import ErreurFormat, lire_dossiers
from . import comptes, pages
from .comptes import ErreurCompte, Utilisateur
from .pages import Contexte
from .sessions import Horloge, LimiteurConnexions, MagasinSessions, Session

FUSEAU = ZoneInfo("Europe/Paris")
TAILLE_MAX_CORPS = 64 * 1024
CSP = ("default-src 'none'; style-src 'self'; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")
FEUILLE_DE_STYLE = Path(__file__).resolve().parent / "static" / "style.css"   # chemin FIXE
HOTES_LOCAUX = frozenset({"127.0.0.1", "localhost", "::1"})
COOKIE_SESSION = "session"
COOKIE_PRE = "pre_session"
LONGUEUR_MAX_CHAMP = 500
MAX_ENTREES_AUDIT_AFFICHEES = 200
_ID_BROUILLON = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,64}")

# Erreurs ATTENDUES du domaine : message lisible, jamais de trace ni de 500.
ERREURS_DOMAINE: tuple[type[BaseException], ...] = (
    cycle.ErreurCycle, EnvoiNonValide, EnvoiHorsCreneau, ErreurEnvoi, TransitionInterdite,
    JournalCorrompu, ErreurBase, VersionSchemaInconnue, ErreurCompte,
    ErreurFormat,              # fichier d'entree illisible (exclusions, avoir, doublon de dossier...)
)


def horloge_systeme() -> dt.datetime:
    return dt.datetime.now(FUSEAU)


class ErreurHttp(Exception):
    def __init__(self, statut: int, titre: str, message: str, *, retour: str | None = None,
                 entetes: list[tuple[str, str]] | None = None) -> None:
        super().__init__(message)
        self.statut, self.titre, self.message, self.retour = statut, titre, message, retour
        self.entetes = entetes or []


@dataclass
class Reponse:
    statut: int
    corps: bytes = b""
    type_contenu: str = "text/html; charset=utf-8"
    entetes: list[tuple[str, str]] = field(default_factory=list)
    cache: str = "no-store"


@dataclass
class Requete:
    environ: dict[str, Any]
    methode: str
    chemin: str
    parametres: dict[str, list[str]]
    cookies: dict[str, str]
    formulaire: dict[str, list[str]] = field(default_factory=dict)
    jeton: str | None = None
    session: Session | None = None
    utilisateur: Utilisateur | None = None

    def q(self, nom: str) -> str:
        return (self.parametres.get(nom) or [""])[0]

    def f(self, nom: str, *, requis: bool = False, max_: int = LONGUEUR_MAX_CHAMP) -> str:
        valeur = (self.formulaire.get(nom) or [""])[0]
        if len(valeur) > max_:
            raise ErreurHttp(400, "Saisie refusée", f"Le champ « {nom} » dépasse {max_} caractères.")
        if requis and not valeur.strip():
            raise ErreurHttp(400, "Saisie incomplète", f"Le champ « {nom} » est obligatoire.")
        return valeur

    @property
    def adresse(self) -> str:
        return str(self.environ.get("REMOTE_ADDR") or "inconnue")


@dataclass(frozen=True)
class Route:
    motif: re.Pattern[str]
    get: Callable[..., Reponse] | None = None
    post: Callable[..., Reponse] | None = None
    public: bool = False
    responsable: bool = False          # reserve au role responsable (403 sinon)

    @property
    def methodes(self) -> list[str]:
        return (["GET", "HEAD"] if self.get else []) + (["POST"] if self.post else [])


def cle_entree(message_id: str, empreinte: str) -> str:
    """Identifiant opaque d'une entree de la file humaine : un Message-ID choisi par
    l'expediteur ne fait jamais l'aller-retour par un formulaire."""
    return hashlib.sha256(f"{message_id}\0{empreinte}".encode("utf-8", "surrogatepass")).hexdigest()


def _empreinte_compte(u: Utilisateur) -> str:
    brut = json.dumps(dict(u.hachage), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(brut.encode("ascii")).hexdigest()


def _hote(valeur: str) -> str | None:
    """Nom d'hote d'un en-tete Host (sans port), en minuscules ; None si malforme."""
    v = valeur.strip().lower()
    if v.startswith("["):
        fin = v.find("]")
        if fin < 0:
            return None
        nom, reste = v[1:fin], v[fin + 1:]
    else:
        nom, _, port = v.partition(":")
        reste = ":" + port if port else ""
    if reste and not re.fullmatch(r":\d{1,5}", reste):
        return None
    return nom or None


class Application:
    """Application WSGI. `horloge` est injectee (tests : temps controle)."""

    def __init__(self, instance: cycle.Instance | Path | str, *, horloge: Horloge | None = None,
                 https: bool = False, hotes: frozenset[str] | set[str] | None = None,
                 entree: Path | str | None = None) -> None:
        self.instance = cycle.Instance.de(instance)
        self.horloge: Horloge = horloge or horloge_systeme
        self.https = https
        self.hotes = frozenset(h.lower() for h in (hotes or HOTES_LOCAUX))
        self.entree = Path(entree) if entree is not None else None
        self.sessions = MagasinSessions(self.horloge)
        self.limiteur = LimiteurConnexions(self.horloge)
        self._secret = secrets.token_bytes(32)          # signe les jetons de pre-session
        self._verrou_ecriture = threading.Lock()
        self._feuille = FEUILLE_DE_STYLE.read_bytes()
        self.journal = self._journal_web()
        comptes.hachage_factice()                        # calcule une fois, au demarrage
        self._routes = self._table_des_routes()

    # -- journal technique ---------------------------------------------------

    def _journal_web(self) -> logging.Logger:
        journal = logging.getLogger(f"rapprochement.web.{id(self):x}")
        journal.setLevel(logging.INFO)
        journal.propagate = False
        dossier = self.instance.racine / "logs"
        dossier.mkdir(parents=True, exist_ok=True)
        gestionnaire = logging.FileHandler(dossier / "web.log", encoding="utf-8", delay=True)
        gestionnaire.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        journal.addHandler(gestionnaire)
        return journal

    def fermer(self) -> None:
        for g in list(self.journal.handlers):
            g.close()
            self.journal.removeHandler(g)

    # -- table des routes ----------------------------------------------------

    def _table_des_routes(self) -> list[Route]:
        r = re.compile
        b = _ID_BROUILLON
        return [
            Route(r(r"/connexion"), get=self.get_connexion, post=self.post_connexion, public=True),
            Route(r(r"/deconnexion"), get=self.get_deconnexion, post=self.post_deconnexion),
            Route(r(r"/"), get=self.get_accueil),
            Route(r(r"/brouillons"), get=self.get_brouillons),
            Route(r(rf"/brouillons/(?P<ident>{b})"), get=self.get_brouillon),
            Route(r(rf"/brouillons/(?P<ident>{b})/valider"), post=self.post_valider),
            Route(r(rf"/brouillons/(?P<ident>{b})/rejeter"), post=self.post_rejeter),
            Route(r(rf"/brouillons/(?P<ident>{b})/envoyer"), get=self.get_envoyer, post=self.post_envoyer),
            Route(r(rf"/brouillons/(?P<ident>{b})/trancher"), post=self.post_trancher, responsable=True),
            Route(r(r"/non-rattaches"), get=self.get_non_rattaches),
            Route(r(r"/non-rattaches/confirmer"), post=self.post_rattacher),
            Route(r(r"/pieces"), get=self.get_pieces),
            Route(r(r"/pieces/controler"), post=self.post_controler),
            Route(r(r"/pieces/promesse"), post=self.post_promesse),
            Route(r(r"/pieces/bloquer"), post=self.post_bloquer),
            Route(r(r"/pieces/debloquer"), post=self.post_debloquer),
            Route(r(r"/pieces/arbitrer"), post=self.post_arbitrer, responsable=True),
            Route(r(r"/escalades"), get=self.get_escalades),
            Route(r(r"/audit"), get=self.get_audit, responsable=True),
            Route(r(r"/audit/exporter"), post=self.post_exporter, responsable=True),
            Route(r(r"/audit/reparer"), post=self.post_reparer, responsable=True),
        ]

    # -- point d'entree WSGI -------------------------------------------------

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        methode = str(environ.get("REQUEST_METHOD", "GET")).upper()
        req: Requete | None = None
        try:
            req = self._requete(environ, methode)
            reponse = self._traiter(req)
        except ErreurHttp as exc:
            reponse = self._page_erreur(req, exc)
        except Exception:  # noqa: BLE001 - toute erreur inattendue : page generique, detail au journal
            reponse = self._erreur_interne(req, environ)
        statut = f"{reponse.statut} {http.HTTPStatus(reponse.statut).phrase}"
        start_response(statut, self._entetes(reponse))
        if methode == "HEAD":
            return []
        return [reponse.corps]

    def _entetes(self, r: Reponse) -> list[tuple[str, str]]:
        """En-tetes de securite poses ICI, pour toute reponse sans exception."""
        return [
            ("Content-Type", r.type_contenu),
            ("Content-Length", str(len(r.corps))),
            ("Content-Security-Policy", CSP),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Cross-Origin-Opener-Policy", "same-origin"),
            ("Cache-Control", r.cache),
            *r.entetes,
        ]

    def _requete(self, environ: dict[str, Any], methode: str) -> Requete:
        try:
            parametres = parse_qs(str(environ.get("QUERY_STRING", "")), keep_blank_values=True,
                                  max_num_fields=50, errors="strict")
        except (ValueError, UnicodeDecodeError):
            raise ErreurHttp(400, "Requête invalide", "Paramètres d’adresse illisibles.") from None
        return Requete(environ, methode, str(environ.get("PATH_INFO") or "/"), parametres,
                       self._cookies(str(environ.get("HTTP_COOKIE", ""))))

    @staticmethod
    def _cookies(entete: str) -> dict[str, str]:
        resultat: dict[str, str] = {}
        for morceau in entete.split(";"):
            nom, egal, valeur = morceau.strip().partition("=")
            if egal and nom and nom not in resultat:
                resultat[nom] = valeur.strip().strip('"')
        return resultat

    def _traiter(self, req: Requete) -> Reponse:
        if req.methode not in ("GET", "HEAD", "POST"):
            raise ErreurHttp(405, "Méthode non autorisée", "Cette méthode HTTP n’est pas acceptée.",
                             entetes=[("Allow", "GET, HEAD, POST")])
        self._controler_hote(req)
        if req.chemin == "/style.css":
            if req.methode == "POST":
                raise ErreurHttp(405, "Méthode non autorisée", "Ressource en lecture seule.",
                                 entetes=[("Allow", "GET, HEAD")])
            return Reponse(200, self._feuille, "text/css; charset=utf-8", cache="max-age=300")

        req.jeton = req.cookies.get(COOKIE_SESSION) or None
        req.session = self.sessions.obtenir(req.jeton)
        if req.session is not None:
            req.utilisateur = comptes.lire_utilisateurs(self.instance).get(req.session.identifiant)
            # Compte supprime, ou mot de passe change en ligne de commande : la session meurt.
            if req.utilisateur is None or _empreinte_compte(req.utilisateur) != req.session.empreinte_compte:
                self.sessions.detruire(req.jeton)
                req.session, req.utilisateur = None, None

        route, groupes = self._trouver(req.chemin)
        if route is None or not route.public:
            if req.utilisateur is None:
                return self._rediriger("/connexion")
        if route is None:
            raise ErreurHttp(404, "Page introuvable", "Cette page n’existe pas.", retour="/")
        gestionnaire = route.get if req.methode in ("GET", "HEAD") else route.post
        if gestionnaire is None:
            raise ErreurHttp(405, "Méthode non autorisée",
                             "Cette page n’accepte pas cette méthode (toute action passe par un formulaire).",
                             entetes=[("Allow", ", ".join(route.methodes))])
        if route.responsable and not (req.utilisateur and req.utilisateur.est_responsable):
            self.journal.warning("refus 403 %s %s pour %s", req.methode, req.chemin,
                                 req.utilisateur.identifiant if req.utilisateur else "-")
            raise ErreurHttp(403, "Accès refusé", "Cette action est réservée au responsable.", retour="/")
        if req.methode == "POST":
            req.formulaire = self._lire_corps(req)
            self._controler_csrf(req, connexion=route.public)
            with self._verrou_ecriture:
                return gestionnaire(req, **groupes)
        return gestionnaire(req, **groupes)

    def _trouver(self, chemin: str) -> tuple[Route | None, dict[str, str]]:
        for route in self._routes:
            m = route.motif.fullmatch(chemin)
            if m:
                return route, m.groupdict()
        return None, {}

    # -- controles de securite ---------------------------------------------

    def _controler_hote(self, req: Requete) -> None:
        brut = str(req.environ.get("HTTP_HOST") or req.environ.get("SERVER_NAME") or "")
        if _hote(brut) not in self.hotes:
            raise ErreurHttp(400, "Hôte non autorisé", "En-tête Host inattendu : requête refusée.")

    def _origine_attendue(self, req: Requete) -> str:
        schema = "https" if self.https else "http"
        return f"{schema}://{str(req.environ.get('HTTP_HOST', '')).strip().lower()}"

    def _lire_corps(self, req: Requete) -> dict[str, list[str]]:
        try:
            longueur = int(req.environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            raise ErreurHttp(400, "Requête invalide", "Longueur de corps illisible.") from None
        if longueur < 0:
            raise ErreurHttp(400, "Requête invalide", "Longueur de corps illisible.")
        if longueur > TAILLE_MAX_CORPS:
            raise ErreurHttp(413, "Requête trop volumineuse", "Le formulaire dépasse 64 Kio.")
        type_contenu = str(req.environ.get("CONTENT_TYPE", "")).split(";")[0].strip().lower()
        if longueur and type_contenu != "application/x-www-form-urlencoded":
            raise ErreurHttp(415, "Format refusé", "Seuls les formulaires HTML classiques sont acceptés.")
        brut = req.environ["wsgi.input"].read(longueur) if longueur else b""
        try:
            return parse_qs(brut.decode("ascii"), keep_blank_values=True, max_num_fields=100,
                            encoding="utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError):
            raise ErreurHttp(400, "Requête invalide", "Formulaire illisible.") from None

    def _jeton_pre_session(self, nonce: str) -> str:
        return hmac.new(self._secret, nonce.encode("ascii"), hashlib.sha256).hexdigest()

    def _controler_csrf(self, req: Requete, *, connexion: bool) -> None:
        refus = ErreurHttp(403, "Requête refusée",
                           "Jeton de sécurité absent, invalide ou expiré. Rechargez la page puis recommencez.",
                           retour="/")
        site = str(req.environ.get("HTTP_SEC_FETCH_SITE", "")).strip().lower()
        if site and site not in ("same-origin", "none"):
            self.journal.warning("refus CSRF (Sec-Fetch-Site=%s) %s", site[:40], req.chemin)
            raise refus
        origine = str(req.environ.get("HTTP_ORIGIN", "")).strip().lower()
        # `Origin: null` est ce qu'envoie un navigateur pour un POST quand la page impose
        # `Referrer-Policy: no-referrer` : on s'en remet alors au jeton et au cookie SameSite=Strict.
        if origine and origine != "null" and origine != self._origine_attendue(req):
            self.journal.warning("refus CSRF (Origin inattendu) %s", req.chemin)
            raise refus
        recu = req.f("csrf", max_=200)
        if connexion:
            nonce = req.cookies.get(COOKIE_PRE, "")
            attendu = self._jeton_pre_session(nonce) if _NONCE.fullmatch(nonce) else ""
        else:
            attendu = req.session.csrf if req.session else ""
        if not attendu or not recu or not hmac.compare_digest(recu.encode("utf-8", "surrogatepass"),
                                                              attendu.encode("ascii")):
            self.journal.warning("refus CSRF (jeton) %s", req.chemin)
            raise refus

    # -- utilitaires de reponse ----------------------------------------------

    def _cookie(self, nom: str, valeur: str, *, supprimer: bool = False) -> tuple[str, str]:
        morceaux = [f"{nom}={valeur}"]
        if supprimer:
            morceaux.append("Max-Age=0")
        morceaux += ["HttpOnly", "SameSite=Strict", "Path=/"]
        if self.https:
            morceaux.append("Secure")
        return ("Set-Cookie", "; ".join(morceaux))

    def _rediriger(self, cible: str, entetes: list[tuple[str, str]] | None = None) -> Reponse:
        corps = pages.gabarit('<!doctype html><title>Redirection</title><p><a href="{c}">Continuer</a></p>',
                              c=cible)
        return Reponse(303, corps.encode("utf-8"), entetes=[("Location", cible), *(entetes or [])])

    def mode_test(self) -> bool:
        """`<instance>/config.json`, cle booleenne `mode_test` ; absent ou illisible : faux."""
        chemin = self.instance.racine / "config.json"
        try:
            if not chemin.exists():
                return False
            donnees = json.loads(chemin.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return False
        return isinstance(donnees, dict) and donnees.get("mode_test") is True

    def _cadre(self) -> tuple[bool, tuple[str, ...], tuple[str, ...], str | None]:
        """(mode test, garde-fous d'envoi coupes, alertes, mode fige de l'instance) pour le
        cadre de CHAQUE page.

        Le mode fige (`cycle.mode_instance`), les garde-fous coupes et l'avertissement sur les
        roles viennent de `cycle` : memes regles que les operations d'ecriture."""
        mode = self.mode_test()
        garde_fous: tuple[str, ...] = ()
        alertes: list[str] = []
        fige: str | None = None
        try:
            fige = cycle.mode_instance(self.instance)          # lecture seule ; None avant le 1er cycle
        except cycle.ModeInstanceIncompatible as exc:
            alertes.append(f"{exc}. Toute écriture est refusée.")
        except Exception:  # noqa: BLE001 - le cadre ne doit jamais empecher d'afficher une page
            self.journal.warning("cadre : mode de l'instance illisible", exc_info=True)
        if fige == "test":
            mode = True                                         # une instance de test reste de test
        try:
            cycle.charger_config(self.instance)               # valide config.json (ConfigInvalide)
            demande = cycle.mode_demande(self.instance)
            if fige is not None and fige != demande:
                alertes.append(
                    f"Cette instance a été créée en mode {fige} mais config.json demande le mode {demande} : "
                    "toute décision est refusée. Le mode d’une instance ne change jamais ; pour changer, "
                    "créer une nouvelle instance (ou rétablir config.json).")
            if mode:
                garde_fous = tuple(cycle.garde_fous_mode_test(self.instance))
            avertissement = cycle.avertissement_roles(self.instance)
            if avertissement:
                alertes.append(avertissement)
        except cycle.ConfigInvalide as exc:
            alertes.append(f"{exc} Les envois et décisions sont refusés tant qu’il n’est pas corrigé.")
        except Exception:  # noqa: BLE001 - le bandeau ne doit jamais empecher d'afficher une page
            self.journal.warning("bandeau : lecture de la configuration impossible", exc_info=True)
        return mode, garde_fous, tuple(alertes), fige

    def _contexte(self, req: Requete | None, actif: str = "", csrf: str | None = None) -> Contexte:
        utilisateur = req.utilisateur if req is not None else None
        if csrf is None:
            csrf = req.session.csrf if req is not None and req.session is not None else ""
        mode, garde_fous, alertes, fige = self._cadre()
        return Contexte(utilisateur, csrf, mode, actif, garde_fous=garde_fous, alertes=alertes,
                        mode_instance=fige)

    def _html(self, contenu: str, statut: int = 200, entetes: list[tuple[str, str]] | None = None) -> Reponse:
        return Reponse(statut, contenu.encode("utf-8"), entetes=entetes or [])

    def _page(self, req: Requete, actif: str, titre: str, contenu: pages.Html, statut: int = 200) -> Reponse:
        message = pages.message_fait(req.q("fait"))
        return self._html(pages.document(self._contexte(req, actif), titre, contenu, message=message), statut)

    def _page_erreur(self, req: Requete | None, exc: ErreurHttp) -> Reponse:
        try:
            ctx = self._contexte(req)
        except Exception:  # noqa: BLE001 - une page d'erreur ne doit jamais echouer
            ctx = Contexte(None, "", False)
        corps = pages.page_erreur(ctx, exc.titre, exc.message, retour=exc.retour)
        return self._html(corps, exc.statut, exc.entetes)

    def _erreur_interne(self, req: Requete | None, environ: dict[str, Any]) -> Reponse:
        incident = secrets.token_hex(4)
        try:
            self.journal.error("erreur interne [%s] %s %s", incident, environ.get("REQUEST_METHOD"),
                               environ.get("PATH_INFO"), exc_info=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx = self._contexte(req)
        except Exception:  # noqa: BLE001
            ctx = Contexte(None, "", False)
        corps = pages.page_erreur(
            ctx, "Erreur interne",
            f"Une erreur inattendue est survenue (incident {incident}). Rien n’a été affiché de son détail ; "
            "il est consigné dans logs/web.log de l’instance.", retour="/")
        return self._html(corps, 500)

    def _refus_domaine(self, exc: BaseException, retour: str) -> Reponse:
        raise ErreurHttp(409, "Action refusée", _message_domaine(exc), retour=retour)

    # -- lecture du depot ------------------------------------------------------

    @contextmanager
    def _lecture(self) -> Iterator[Depot | None]:
        """Une connexion par requete, fermee a la sortie. Depot absent : None (rien n'est cree)."""
        if not self.instance.depot.exists():
            yield None
            return
        try:
            depot = Depot(self.instance.depot)
        except (ErreurBase, VersionSchemaInconnue) as exc:
            raise ErreurHttp(503, "Dépôt illisible", _message_domaine(exc)) from None
        try:
            yield depot
        finally:
            depot.fermer()

    def _maintenant(self) -> dt.datetime:
        return self.horloge()

    def _aujourdhui(self) -> dt.date:
        return self.horloge().astimezone(FUSEAU).date()

    def _dossiers(self) -> tuple[dict[str, Dossier], str]:
        """(referentiel des dossiers, message d'erreur) lu dans `--entree` s'il est fourni. Un
        fichier refuse par `parseurs` (doublon de dossier...) donne un message lisible, jamais 500."""
        if self.entree is None or not (self.entree / "dossiers.csv").exists():
            return {}, ""
        try:
            return lire_dossiers(self.entree / "dossiers.csv"), ""
        except ErreurFormat as exc:
            return {}, f"Fichier d’entrée refusé : {exc}. Circuit et raison sociale non affichés."
        except (OSError, ValueError, KeyError) as exc:
            return {}, f"dossiers.csv illisible ({type(exc).__name__}) : circuit et raison sociale non affichés."

    def _action(self, req: Requete, nom: str, retour: str, appel: Callable[[], Any]) -> Any:
        """Execute une decision deleguee a `cycle` ; une erreur attendue devient une page 409."""
        u = req.utilisateur
        assert u is not None
        try:
            resultat = appel()
        except ERREURS_DOMAINE as exc:
            self.journal.info("action %s refusee pour %s (%s)", nom, u.identifiant, type(exc).__name__)
            self._refus_domaine(exc, retour)
        self.journal.info("action %s par %s", nom, u.identifiant)
        return resultat

    # =====================================================================
    # Ecrans
    # =====================================================================

    def get_connexion(self, req: Requete) -> Reponse:
        if req.utilisateur is not None:
            return self._rediriger("/")
        return self._formulaire_connexion(req)

    def _formulaire_connexion(self, req: Requete, *, erreur: str = "", identifiant: str = "",
                              statut: int = 200) -> Reponse:
        nonce = req.cookies.get(COOKIE_PRE, "")
        if not _NONCE.fullmatch(nonce):
            nonce = secrets.token_urlsafe(24)
        mode, garde_fous, alertes, fige = self._cadre()
        ctx = Contexte(None, self._jeton_pre_session(nonce), mode, garde_fous=garde_fous, alertes=alertes,
                       mode_instance=fige)
        corps = pages.page_connexion(ctx, erreur=erreur, identifiant=identifiant)
        return self._html(corps, statut, [self._cookie(COOKIE_PRE, nonce)])

    def post_connexion(self, req: Requete) -> Reponse:
        identifiant = comptes.normaliser_identifiant(req.f("identifiant", max_=64))
        mot_de_passe = req.f("mot_de_passe", max_=comptes.LONGUEUR_MAX_MOT_DE_PASSE)
        cles = (f"id:{identifiant}", f"ip:{req.adresse}")
        if self.limiteur.bloque(cles):
            self.journal.warning("connexion bloquee (trop d'echecs) depuis %s", req.adresse)
            return self._formulaire_connexion(
                req, statut=429, erreur="Trop de tentatives échouées. Réessayez dans une dizaine de minutes.")
        utilisateur = comptes.lire_utilisateurs(self.instance).get(identifiant)
        hachage = utilisateur.hachage if utilisateur is not None else comptes.hachage_factice()
        valide = comptes.verifier_mot_de_passe(mot_de_passe, hachage)
        if utilisateur is None or not valide:
            self.limiteur.echec(cles)
            # L'identifiant saisi n'est journalise que s'il existe : un mot de passe tape dans le
            # mauvais champ ne finit jamais dans le journal.
            self.journal.warning("echec de connexion (%s) depuis %s",
                                 f"compte {identifiant}" if utilisateur else "identifiant inconnu", req.adresse)
            return self._formulaire_connexion(req, erreur="Identifiant ou mot de passe incorrect.")
        self.limiteur.succes(f"id:{identifiant}")
        self.sessions.detruire(req.jeton)                 # jamais de reprise d'un jeton existant
        jeton, _ = self.sessions.creer(utilisateur.identifiant, _empreinte_compte(utilisateur))
        self.journal.info("connexion de %s depuis %s", utilisateur.identifiant, req.adresse)
        return self._rediriger("/", [self._cookie(COOKIE_SESSION, jeton),
                                     self._cookie(COOKIE_PRE, "", supprimer=True)])

    def get_deconnexion(self, req: Requete) -> Reponse:
        return self._html(pages.page_deconnexion(self._contexte(req)))

    def post_deconnexion(self, req: Requete) -> Reponse:
        self.sessions.detruire(req.jeton)
        if req.utilisateur:
            self.journal.info("deconnexion de %s", req.utilisateur.identifiant)
        return self._rediriger("/connexion", [self._cookie(COOKIE_SESSION, "", supprimer=True)])

    def get_accueil(self, req: Requete) -> Reponse:
        with self._lecture() as depot:
            pieces = depot.charger_pieces() if depot else []
            brouillons = depot.lister_brouillons() if depot else []
            file = depot.file_humaine(resolue=False) if depot else []
        verification = cycle.verifier_audit(self.instance)
        return self._page(req, "accueil", "Tableau de bord", pages.page_accueil(
            self._contexte(req), pieces=pieces, brouillons=brouillons, file=file, verification=verification))

    def get_brouillons(self, req: Requete) -> Reponse:
        statut = req.q("statut")
        try:
            filtre = StatutBrouillon(statut) if statut else None
        except ValueError:
            raise ErreurHttp(400, "Filtre invalide", "Statut de brouillon inconnu.", retour="/brouillons") from None
        with self._lecture() as depot:
            brouillons = depot.lister_brouillons(filtre) if depot else []
        return self._page(req, "brouillons", "Brouillons de relance", pages.page_brouillons(brouillons, statut))

    def _brouillon(self, ident: str) -> tuple[Any, dict[tuple[str, str], Any]]:
        with self._lecture() as depot:
            b = depot.charger_brouillon(ident) if depot else None
            if b is None:
                raise ErreurHttp(404, "Brouillon introuvable", "Aucun brouillon ne porte cet identifiant.",
                                 retour="/brouillons")
            pieces = {(p.dossier, p.reference): p for d in b.dossiers for p in depot.charger_pieces(dossier=d)}
        return b, pieces

    def _envoye_par(self, ident: str) -> str:
        """Qui a DECLENCHE l'envoi de ce brouillon, lu dans le journal d'audit (`envoye_par`,
        ecrit par `cycle.envoyer_relance`) ; ou qui a tranche un envoi incertain comme parti."""
        if not self.instance.audit.exists():
            return ""
        try:
            entrees = JournalAudit(self.instance.audit).lire()
        except JournalCorrompu:
            return ""
        for e in reversed(entrees):
            if e.objet != ident:
                continue
            if e.action == "envoi_effectue":
                return str(e.details.get("envoye_par") or "")
            if e.action == "envoi_confirme_par_humain":
                return f"{e.acteur} (envoi incertain tranché comme parti)"
        return ""

    def get_brouillon(self, req: Requete, ident: str) -> Reponse:
        b, pieces = self._brouillon(ident)
        dossiers, erreur_dossiers = self._dossiers()
        contenu = pages.page_brouillon(self._contexte(req), b, pieces=pieces, dossiers=dossiers,
                                       envoye_par=self._envoye_par(ident), erreur_dossiers=erreur_dossiers)
        return self._page(req, "brouillons", "Brouillon de relance", contenu)

    def get_envoyer(self, req: Requete, ident: str) -> Reponse:
        b, _ = self._brouillon(ident)
        return self._page(req, "brouillons", "Confirmer l’envoi",
                          pages.page_confirmation_envoi(self._contexte(req), b))

    def _vers_brouillon(self, ident: str, fait: str) -> Reponse:
        return self._rediriger(f"/brouillons/{quote(ident, safe='')}?fait={fait}")

    def post_valider(self, req: Requete, ident: str) -> Reponse:
        u = req.utilisateur
        assert u is not None
        self._action(req, "valider", f"/brouillons/{ident}", lambda: cycle.valider_relance(
            self.instance, ident, u.nom, maintenant=self._maintenant()))
        return self._vers_brouillon(ident, "valide")

    def post_rejeter(self, req: Requete, ident: str) -> Reponse:
        u = req.utilisateur
        assert u is not None
        motif = req.f("motif", requis=True)
        self._action(req, "rejeter", f"/brouillons/{ident}", lambda: cycle.rejeter_relance(
            self.instance, ident, u.nom, motif, maintenant=self._maintenant()))
        return self._vers_brouillon(ident, "rejete")

    def post_envoyer(self, req: Requete, ident: str) -> Reponse:
        if req.f("confirmation") != "oui":
            raise ErreurHttp(400, "Confirmation manquante",
                             "L’envoi doit être confirmé depuis la page de confirmation.",
                             retour=f"/brouillons/{ident}/envoyer")
        u = req.utilisateur
        assert u is not None
        # Envoi NOMINATIF : le journal d'audit nomme la personne qui declenche (`envoye_par`).
        self._action(req, "envoyer", f"/brouillons/{ident}", lambda: cycle.envoyer_relance(
            self.instance, ident, maintenant=self._maintenant(), par=u.nom))
        return self._vers_brouillon(ident, "envoye")

    def post_trancher(self, req: Requete, ident: str) -> Reponse:
        u = req.utilisateur
        assert u is not None
        choix = req.f("parti")
        if choix not in ("oui", "non"):
            raise ErreurHttp(400, "Saisie invalide", "Indiquez si le message est parti ou non.",
                             retour=f"/brouillons/{ident}")
        self._action(req, "trancher", f"/brouillons/{ident}", lambda: cycle.trancher_envoi(
            self.instance, ident, u.nom, parti=choix == "oui", maintenant=self._maintenant()))
        return self._vers_brouillon(ident, "tranche_parti" if choix == "oui" else "tranche_non_parti")

    def get_non_rattaches(self, req: Requete) -> Reponse:
        with self._lecture() as depot:
            file = depot.file_humaine(resolue=False) if depot else []
            pieces = {(p.dossier, p.reference): p for p in depot.charger_pieces()} if depot else {}
        entrees = [(cle_entree(d.message_id, d.empreinte), d, cycle.contexte_file(d)) for d in file]
        return self._page(req, "non-rattaches", "Messages non rattachés",
                          pages.page_non_rattaches(self._contexte(req), entrees, pieces))

    def post_rattacher(self, req: Requete) -> Reponse:
        u = req.utilisateur
        assert u is not None
        cle = req.f("cle", requis=True, max_=64)
        with self._lecture() as depot:
            file = depot.file_humaine(resolue=False) if depot else []
        trouvees = [d for d in file if hmac.compare_digest(cle_entree(d.message_id, d.empreinte), cle)]
        if len(trouvees) != 1:
            raise ErreurHttp(409, "Action refusée", "Entrée introuvable ou déjà traitée.", retour="/non-rattaches")
        d = trouvees[0]
        forcer = req.f("forcer") == "oui"
        if forcer and not u.est_responsable:
            self.journal.warning("refus 403 forcage de rattachement pour %s", u.identifiant)
            raise ErreurHttp(403, "Accès refusé", "Forcer un rattachement vers un autre dossier est réservé "
                             "au responsable.", retour="/non-rattaches")
        motif = req.f("motif", requis=forcer).strip()
        dossier = req.f("dossier", max_=100).strip() or None
        reference = req.f("reference", max_=100).strip() or None
        if (d.statut is not StatutRoutage.PROPOSEE or dossier or reference) and not (dossier and reference):
            raise ErreurHttp(400, "Saisie incomplète", "Indiquez le dossier ET la référence d’opération.",
                             retour="/non-rattaches")
        try:
            self._action(req, "rattacher-force" if forcer else "rattacher", "/non-rattaches",
                         lambda: cycle.rattacher_piece(
                             self.instance, u.nom, maintenant=self._maintenant(), message_id=d.message_id,
                             empreinte=d.empreinte, dossier=dossier, reference=reference,
                             forcer=forcer, motif=motif))
        except ErreurHttp as exc:
            if exc.statut == 409 and not forcer and d.dossier and dossier and dossier != d.dossier:
                suite = ("Si ce transfert est voulu, cochez « Forcer » et indiquez un motif : il sera journalisé."
                         if u.est_responsable else
                         "Seul un responsable peut forcer ce rattachement, avec un motif journalisé.")
                raise ErreurHttp(
                    409, "Rattachement refusé",
                    f"Rattachement refusé : cette pièce a été reçue pour le dossier « {d.dossier} », pas "
                    f"« {dossier} ». Une pièce ne passe jamais d’un client à un autre par mégarde : corrigez "
                    f"le dossier. {suite}", retour="/non-rattaches") from None
            raise
        return self._rediriger("/non-rattaches?fait=" + ("rattache_force" if forcer else "rattache"))

    def get_pieces(self, req: Requete) -> Reponse:
        dossier, etat = req.q("dossier"), req.q("etat")
        if etat and etat not in {e.value for e in EtatPiece}:
            raise ErreurHttp(400, "Filtre invalide", "État de pièce inconnu.", retour="/pieces")
        with self._lecture() as depot:
            dossiers = sorted({p.dossier for p in depot.charger_pieces()}) if depot else []
        suivi = cycle.suivi_pieces(self.instance, self._aujourdhui(), dossier=dossier or None, etat=etat or None)
        return self._page(req, "pieces", "Pièces attendues", pages.page_pieces(
            self._contexte(req), suivi.lignes, dossiers=dossiers, dossier=dossier, etat=etat))

    def _retour_pieces(self, req: Requete, fait: str) -> str:
        if req.f("retour") == "escalades":
            return f"/escalades?fait={fait}"
        filtres = {k: v for k, v in (("dossier", req.f("f_dossier", max_=100)), ("etat", req.f("f_etat", max_=40)))
                   if v}
        return "/pieces?" + urlencode({**filtres, "fait": fait})

    def _piece_ciblee(self, req: Requete) -> tuple[str, str]:
        return req.f("dossier", requis=True, max_=100), req.f("reference", requis=True, max_=100)

    def post_controler(self, req: Requete) -> Reponse:
        u = req.utilisateur
        assert u is not None
        dossier, reference = self._piece_ciblee(req)
        decision = req.f("decision")
        if decision not in ("conforme", "non_conforme"):
            raise ErreurHttp(400, "Saisie invalide", "Décision de contrôle inconnue.", retour="/pieces")
        motif = req.f("motif", requis=decision == "non_conforme")
        self._action(req, "controler", "/pieces", lambda: cycle.controler_piece(
            self.instance, dossier, reference, u.nom, conforme=decision == "conforme",
            motif=motif, maintenant=self._maintenant()))
        return self._rediriger(self._retour_pieces(req, "controle"))

    def post_promesse(self, req: Requete) -> Reponse:
        u = req.utilisateur
        assert u is not None
        dossier, reference = self._piece_ciblee(req)
        try:
            date = dt.date.fromisoformat(req.f("date", requis=True, max_=10))
        except ValueError:
            raise ErreurHttp(400, "Saisie invalide", "Date promise illisible (attendu AAAA-MM-JJ).",
                             retour="/pieces") from None
        self._action(req, "promesse", "/pieces", lambda: cycle.saisir_promesse(
            self.instance, dossier, reference, u.nom, date_promesse=date, maintenant=self._maintenant()))
        return self._rediriger(self._retour_pieces(req, "promesse"))

    def _decision_motivee(self, req: Requete, nom: str, fonction: Callable[..., Any], fait: str) -> Reponse:
        u = req.utilisateur
        assert u is not None
        dossier, reference = self._piece_ciblee(req)
        motif = req.f("motif", requis=True)
        retour = "/escalades" if req.f("retour") == "escalades" else "/pieces"
        self._action(req, nom, retour, lambda: fonction(
            self.instance, dossier, reference, u.nom, motif=motif, maintenant=self._maintenant()))
        return self._rediriger(self._retour_pieces(req, fait))

    def post_bloquer(self, req: Requete) -> Reponse:
        return self._decision_motivee(req, "bloquer", cycle.bloquer_piece, "bloque")

    def post_debloquer(self, req: Requete) -> Reponse:
        return self._decision_motivee(req, "debloquer", cycle.debloquer_piece, "debloque")

    def post_arbitrer(self, req: Requete) -> Reponse:
        return self._decision_motivee(req, "arbitrer", cycle.arbitrer_piece, "arbitre")

    def get_escalades(self, req: Requete) -> Reponse:
        with self._lecture() as depot:
            pieces = depot.charger_pieces(etats=[EtatPiece.ESCALADEE]) if depot else []
            incertains = depot.lister_brouillons(StatutBrouillon.EN_COURS) if depot else []
        return self._page(req, "escalades", "Escalades",
                          pages.page_escalades(self._contexte(req), pieces, incertains))

    def get_audit(self, req: Requete) -> Reponse:
        verification = cycle.verifier_audit(self.instance)
        controle = cycle.controler_envois(self.instance)          # lecture seule (recette CA-06)
        entrees, erreur, total = None, "", 0
        if self.instance.audit.exists():
            try:
                toutes = JournalAudit(self.instance.audit).lire()
                total = len(toutes)
                entrees = list(reversed(toutes[-MAX_ENTREES_AUDIT_AFFICHEES:]))
            except JournalCorrompu as exc:
                erreur = f"Lecture du journal impossible : {exc}"
        return self._page(req, "audit", "Journal d’audit",
                          pages.page_audit(self._contexte(req), verification, entrees, erreur, total,
                                           controle=controle))

    def post_exporter(self, req: Requete) -> Reponse:
        horodatage = self._maintenant().astimezone(FUSEAU).strftime("%Y%m%d-%H%M%S")
        dossier = self.instance.racine / "exports"
        cible = dossier / f"depot-{horodatage}.json"
        n = 1
        while cible.exists():
            cible = dossier / f"depot-{horodatage}-{n}.json"
            n += 1
        chemin = self._action(req, "exporter", "/audit", lambda: cycle.exporter(self.instance, cible))
        return self._html(pages.page_resultat(
            self._contexte(req, "audit"), "Export écrit",
            f"Export complet du dépôt écrit dans {Path(chemin).relative_to(self.instance.racine).as_posix()} "
            "(dans le répertoire de l’instance).", "/audit"))

    def post_reparer(self, req: Requete) -> Reponse:
        u = req.utilisateur
        assert u is not None
        r = self._action(req, "reparer-audit", "/audit", lambda: cycle.reparer_audit(
            self.instance, u.nom, maintenant=self._maintenant()))
        message = (f"Journal réparé : {r.octets_retires} octet(s) retiré(s), sauvegardés dans {r.fragment}."
                   if r.octets_retires else "Journal sain : rien à réparer.")
        return self._html(pages.page_resultat(self._contexte(req, "audit"), "Réparation du journal", message,
                                              "/audit"))


_MESSAGES_TYPE = {
    "ValidateurRefuse": "Votre nom n’est pas déclaré dans la liste qu’exige cette opération (validateurs.txt ; "
                        "responsables.txt pour arbitrer, trancher un envoi ou réparer le journal) : décision refusée.",
    "ConfigInvalide": "config.json de l’instance est invalide : action refusée tant qu’il n’est pas corrigé.",
    "ModeInstanceIncompatible": "Le mode de cette instance (test ou réel) est figé depuis son premier cycle et "
                                "ne correspond pas au mode demandé : rien n’a été écrit.",
    "ErreurFormat": "Un fichier d’entrée est illisible ou mal formé : corrigez-le puis recommencez. "
                    "Rien n’a été écrit.",
    "EnvoiHorsCreneau": "Envoi hors créneau : les relances ne partent que les jours ouvrés, de 8 h à 18 h "
                        "(heure de Paris). Le brouillon reste validé.",
    "EnvoiLimiteHebdomadaire": "Limite d’un e-mail par destinataire sur 7 jours glissants atteinte.",
    "JournalNonSain": "Le journal d’audit n’est pas sain : aucune écriture n’est possible. Prévenez le responsable.",
}


def _message_domaine(exc: BaseException) -> str:
    """Message francais lisible pour une erreur attendue (jamais de trace Python)."""
    nom = type(exc).__name__
    detail = " ".join(str(exc).split())[:600]
    if isinstance(exc, ErreurEnvoi) and nom == "ErreurEnvoi":
        entete = ("Résultat de l’envoi incertain : le brouillon reste « Envoi incertain » et ne sera jamais "
                  "renvoyé seul. Un responsable doit vérifier outbox/ puis trancher.")
    else:
        entete = _MESSAGES_TYPE.get(nom, "Action refusée par les règles du dossier.")
    return f"{entete} Détail : {detail}" if detail else entete


def creer_application(instance: cycle.Instance | Path | str, **options: Any) -> Application:
    return Application(instance, **options)


__all__ = ["Application", "CSP", "TAILLE_MAX_CORPS", "cle_entree", "creer_application", "horloge_systeme"]
