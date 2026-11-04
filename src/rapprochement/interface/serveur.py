"""Interface graphique locale : un petit serveur web sans dependance.

Lancement : python scripts/interface.py   (ouvre le navigateur)

Choix delibere :
  - bibliotheque standard uniquement : rien a installer sur le poste du
    commercial ou du cabinet, Python 3.11+ suffit ;
  - aucune ressource externe (ni CDN, ni police en ligne) : la demonstration
    fonctionne hors connexion, dans une salle de reunion sans wifi ;
  - ecoute sur 127.0.0.1 seulement : les releves bancaires ne quittent pas le
    poste. C'est un argument, pas un detail.

Le serveur travaille dans un espace de travail (data/sorties/interface/) : une
copie du jeu de demonstration ou des fichiers importes. Le corpus versionne
n'est jamais modifie, meme quand le comptable apprend une regle.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import mimetypes
import re
import shutil
import threading
from collections import Counter
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..cycle import FICHIERS, OBLIGATOIRES, Cycle, executer
from ..evaluation import evaluer, lire_verite
from ..exclusions import CONTRE_EXEMPLES, REGLES, Referentiel, regle_apprise_correspond
from ..modeles import Rapprochement, Statut
from ..normalisation import normaliser
from ..parseurs import ErreurFormat
from ..rapport import ecrire_suivi, statistiques
from ..relances import formater_montant, prochaine_echeance

STATIQUE = Path(__file__).resolve().parent / "static"
RACINE_PROJET = Path(__file__).resolve().parents[3]
CORPUS = RACINE_PROJET / "data" / "corpus"
SCHEMAS = RACINE_PROJET / "data" / "schemas"
ESPACE_DEFAUT = RACINE_PROJET / "data" / "sorties" / "interface"

TAILLE_MAX_REQUETE = 25 * 1024 * 1024
# Une regle apprise qui ecarterait plus de cette part des operations d'un
# dossier est refusee : elle masquerait de vraies pieces manquantes.
PART_MAX_REGLE = 0.30

CONTRE_EXEMPLES_LISIBLES = (
    "Crédit-bail",
    "Leasing",
    "LOA (location avec option d'achat)",
    "Location financière ou longue durée",
)


class ErreurUtilisateur(ValueError):
    """Erreur a afficher telle quelle dans l'interface."""


# -- espace de travail -------------------------------------------------------


class Espace:
    """Les fichiers sur lesquels travaille l'interface."""

    def __init__(self, dossier: Path | str = ESPACE_DEFAUT, corpus: Path | str = CORPUS) -> None:
        self.dossier = Path(dossier)
        self.corpus = Path(corpus)
        self.verrou = threading.RLock()
        self.meta = self._lire_meta()
        if not all((self.dossier / FICHIERS[c]).exists() for c in OBLIGATOIRES):
            self.charger_demo()

    # meta : d'ou viennent les fichiers de l'espace
    def _lire_meta(self) -> dict:
        try:
            return json.loads((self.dossier / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"source": "demo"}

    def _ecrire_meta(self) -> None:
        self.dossier.mkdir(parents=True, exist_ok=True)
        (self.dossier / "meta.json").write_text(json.dumps(self.meta), encoding="utf-8")

    def charger_demo(self) -> None:
        with self.verrou:
            if self.dossier.exists():
                shutil.rmtree(self.dossier)
            self.dossier.mkdir(parents=True)
            for nom in FICHIERS.values():
                if (self.corpus / nom).exists():
                    shutil.copy(self.corpus / nom, self.dossier / nom)
            self.meta = {"source": "demo"}
            self._ecrire_meta()

    def importer(self, contenus: dict[str, str]) -> None:
        """Remplace l'espace par des fichiers importes, apres validation."""
        manquants = [c for c in OBLIGATOIRES if not (contenus.get(c) or "").strip()]
        if manquants:
            noms = {"releve": "le relevé bancaire", "pieces": "les pièces reçues",
                    "dossiers": "les dossiers clients"}
            raise ErreurUtilisateur(
                "Fichier obligatoire manquant : " + ", ".join(noms[c] for c in manquants) + "."
            )
        with self.verrou:
            essai = self.dossier.with_name(self.dossier.name + "_import")
            if essai.exists():
                shutil.rmtree(essai)
            essai.mkdir(parents=True)
            for cle, nom in FICHIERS.items():
                texte = contenus.get(cle)
                if texte and texte.strip():
                    (essai / nom).write_text(texte.lstrip("﻿"), encoding="utf-8")
            try:
                cycle = executer(essai)
                if not cycle.operations:
                    raise ErreurUtilisateur("Le relevé bancaire ne contient aucune opération.")
            except Exception:
                shutil.rmtree(essai, ignore_errors=True)
                raise
            if self.dossier.exists():
                shutil.rmtree(self.dossier)
            essai.rename(self.dossier)
            self.meta = {"source": "import"}
            self._ecrire_meta()

    def cycle(self) -> Cycle:
        with self.verrou:
            return executer(self.dossier)

    # -- regles apprises ---------------------------------------------------

    def apprendre(self, dossier: str, motif_regex: str, motif: str) -> int:
        """Ajoute une regle apprise. Renvoie le nombre d'operations reclassees."""
        motif_regex = (motif_regex or "").strip().lower()
        if not motif_regex:
            raise ErreurUtilisateur("Indiquez les mots du libellé qui identifient l'opération.")
        if len(motif_regex) > 200:
            raise ErreurUtilisateur("Expression trop longue (200 caractères au plus).")
        try:
            re.compile(motif_regex)
        except re.error as exc:
            raise ErreurUtilisateur(f"Expression invalide : {exc}.") from exc

        with self.verrou:
            avant = self.cycle()
            operations = [o for o in avant.operations if o.dossier == dossier]
            if not operations:
                raise ErreurUtilisateur(f"Dossier inconnu : {dossier}.")
            touchees = sum(1 for o in operations if regle_apprise_correspond(motif_regex, o))
            if touchees > max(3, PART_MAX_REGLE * len(operations)):
                raise ErreurUtilisateur(
                    f"Cette expression est trop large : elle écarterait {touchees} opérations "
                    f"sur {len(operations)} du dossier. Précisez-la avec d'autres mots du libellé."
                )
            avant_n = _compte_apprises(avant, dossier)
            referentiel = avant.referentiel
            referentiel.ajouter(dossier, motif_regex, motif)
            referentiel.ecrire_csv(self.dossier / FICHIERS["exclusions"])
            return _compte_apprises(self.cycle(), dossier) - avant_n

    def oublier(self, dossier: str, motif_regex: str) -> None:
        with self.verrou:
            referentiel = Referentiel.depuis_csv(self.dossier / FICHIERS["exclusions"])
            if not referentiel.retirer(dossier, motif_regex):
                raise ErreurUtilisateur("Cette règle n'existe plus.")
            referentiel.ecrire_csv(self.dossier / FICHIERS["exclusions"])

    # -- exports -----------------------------------------------------------

    def suivi_csv(self) -> bytes:
        with self.verrou:
            chemin = ecrire_suivi(self.cycle().rapprochements, self.dossier / "tableau_suivi.csv")
            return chemin.read_bytes()

    def relance_txt(self, code: str) -> bytes:
        for relance in self.cycle().relances:
            if relance.dossier == code:
                texte = f"À : {relance.destinataire}\nObjet : {relance.objet}\n\n{relance.corps}\n"
                return texte.encode("utf-8")
        raise ErreurUtilisateur(f"Aucune relance pour le dossier {code}.")

    # -- analyse complete pour l'interface ----------------------------------

    def analyse(self) -> dict:
        with self.verrou:
            cycle = self.cycle()
            verite_chemin = self.dossier / FICHIERS["verite"]
            verite = lire_verite(verite_chemin) if verite_chemin.exists() else None
            return construire_analyse(cycle, self.meta.get("source", "demo"), verite)


def _compte_apprises(cycle: Cycle, dossier: str) -> int:
    return sum(1 for r in cycle.rapprochements if r.regle == f"apprise:{dossier}")


# -- serialisation -----------------------------------------------------------


def _m(montant: Decimal) -> str:
    return f"{montant:.2f}"


def _eur(montant: Decimal) -> str:
    return f"{formater_montant(montant)} €"


def suggestion_regle(libelle: str) -> str:
    """Mots porteurs d'un libelle, pour pre-remplir une regle apprise."""
    mots = [m for m in normaliser(libelle).split() if m.isalpha() and len(m) >= 3]
    return " ".join(mots[:4])


def _operation(r: Rapprochement) -> dict:
    o = r.operation
    donnees = {
        "reference": o.reference,
        "dossier": o.dossier,
        "date": o.date_operation.isoformat(),
        "libelle": o.libelle,
        "montant": _m(o.montant),
        "sens": o.sens.value,
        "devise": o.devise,
        "statut": r.statut.value,
        "confiance": round(r.confiance, 3),
        "motif": r.motif,
        "regle": r.regle,
        "pieces": [
            {
                "id": p.id_piece,
                "fournisseur": p.fournisseur,
                "fichier": p.fichier,
                "date": p.date_facture.isoformat(),
                "montant": _m(p.montant_ttc),
                "devise": p.devise,
                "canal": p.canal,
            }
            for p in r.pieces
        ],
    }
    if r.statut is Statut.MANQUANT:
        donnees["suggestion"] = suggestion_regle(o.libelle)
    return donnees


def _est_contre_exemple(libelle: str) -> bool:
    formes = (normaliser(libelle), normaliser(libelle, retirer_prefixes=False))
    return any(re.search(p, f) for f in formes for p in CONTRE_EXEMPLES)


def faits_marquants(rapprochements: list[Rapprochement], limite: int = 6) -> list[dict]:
    """Les cas difficiles resolus dans ce cycle : de quoi raconter la demo."""
    ordre = ("groupe", "acompte", "contre_exemple", "apprise", "arrondi")
    par_genre: dict[str, list[dict]] = {g: [] for g in ordre}

    def ajouter(genre: str, titre: str, detail: str, r: Rapprochement) -> None:
        par_genre[genre].append({"genre": genre, "titre": titre, "detail": detail,
                                 "reference": r.operation.reference})

    for r in sorted(rapprochements, key=lambda x: x.operation.date_operation):
        o = r.operation
        if r.regle.startswith("paiement_groupe") and r.pieces:
            ajouter(
                "groupe", "Virement groupé reconnu",
                f"Un virement de {_eur(o.montant)} réglant {len(r.pieces)} factures "
                f"{r.pieces[0].fournisseur}.", r,
            )
        elif r.regle == "acompte" and r.pieces:
            ajouter(
                "acompte", "Acompte identifié",
                f"{_eur(o.montant)} versés sur une facture de {_eur(r.pieces[0].montant_ttc)} "
                f"({r.pieces[0].fournisseur}) : le solde reste suivi.", r,
            )
        elif r.regle.startswith("apprise:"):
            ajouter("apprise", "Règle du cabinet appliquée",
                    f"« {o.libelle_court} » : {r.motif}", r)
        elif r.statut is Statut.JUSTIFIE and r.pieces and _est_contre_exemple(o.libelle):
            ajouter(
                "contre_exemple", "Crédit-bail : facture exigée",
                f"« {o.libelle_court} » ressemble à une échéance de prêt, mais le crédit-bail "
                f"exige une facture : rattachée à {r.pieces[0].fournisseur}.", r,
            )
        elif (
            r.regle == "appariement_1_1" and len(r.pieces) == 1
            and r.pieces[0].montant_ttc != o.montant
        ):
            ajouter(
                "arrondi", "Écart d'arrondi toléré",
                f"{_eur(o.montant)} payés pour une facture de {_eur(r.pieces[0].montant_ttc)} "
                f"({r.pieces[0].fournisseur}).", r,
            )
    # Le credit-bail le plus parlant est celui dont le libelle dit "echeance".
    par_genre["contre_exemple"].sort(key=lambda f: "ECHEANCE" not in f["detail"].upper())
    # Un exemple de chaque genre d'abord, puis les suivants s'il reste de la place.
    faits: list[dict] = []
    rang = 0
    while len(faits) < limite and any(len(par_genre[g]) > rang for g in ordre):
        # Au-dela du premier exemple, seules les regles du cabinet ajoutent de
        # l'information (deux credit-bail se ressemblent, deux regles non).
        genres = ordre if rang == 0 else ("apprise",)
        faits += [par_genre[g][rang] for g in genres if len(par_genre[g]) > rang]
        rang += 1
        if rang > 1 and len(par_genre["apprise"]) <= rang:
            break
    faits = faits[:limite]
    return sorted(faits, key=lambda f: ordre.index(f["genre"]))


def construire_analyse(
    cycle: Cycle,
    source: str,
    verite: dict[str, dict[str, str]] | None = None,
    aujourdhui: dt.date | None = None,
) -> dict:
    aujourdhui = aujourdhui or dt.date.today()
    rapprochements = cycle.rapprochements
    stats = statistiques(rapprochements)
    stats["montant_manquant"] = _m(stats["montant_manquant"])

    dates = [r.operation.date_operation for r in rapprochements]
    relances_par_dossier = {r.dossier: r for r in cycle.relances}

    par_dossier = []
    codes = sorted({r.operation.dossier for r in rapprochements})
    for code in codes:
        lignes = [r for r in rapprochements if r.operation.dossier == code]
        compte = Counter(r.statut for r in lignes)
        dossier = cycle.dossiers.get(code)
        relance = relances_par_dossier.get(code)
        echeance = relance.echeance if relance else (
            prochaine_echeance(dossier.jour_echeance_tva, aujourdhui) if dossier else None
        )
        par_dossier.append(
            {
                "code": code,
                "raison_sociale": dossier.raison_sociale if dossier else code,
                "operations": len(lignes),
                **{s.value: compte[s] for s in Statut},
                "montant_manquant": _m(sum(
                    (r.operation.montant for r in lignes if r.statut is Statut.MANQUANT),
                    Decimal("0"),
                )),
                "taux_automatisation": round(
                    100 * (compte[Statut.HORS_PERIMETRE] + compte[Statut.JUSTIFIE]) / len(lignes), 1
                ),
                "echeance_tva": echeance.isoformat() if echeance else None,
                "a_une_relance": relance is not None,
            }
        )

    regles_vues = Counter(r.regle for r in rapprochements)
    hors = Counter(r.regle for r in rapprochements if r.statut is Statut.HORS_PERIMETRE)
    motifs = {r.regle: r.motif for r in rapprochements if r.statut is Statut.HORS_PERIMETRE}

    apprises = []
    for dossier_code, regles in sorted(cycle.referentiel.apprises.items()):
        for motif_regex, motif in regles:
            n = sum(
                1 for r in rapprochements
                if r.operation.dossier == dossier_code
                and r.regle == f"apprise:{dossier_code}"
                and regle_apprise_correspond(motif_regex, r.operation)
            )
            apprises.append({"dossier": dossier_code, "motif_regex": motif_regex,
                             "motif": motif, "operations": n})

    analyse = {
        "genere_le": dt.datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "periode": {
            "debut": min(dates).isoformat() if dates else None,
            "fin": max(dates).isoformat() if dates else None,
        },
        "volumes": {
            "operations": len(cycle.operations),
            "pieces": len(cycle.pieces),
            "dossiers": len(codes),
        },
        "stats": stats,
        "par_regle": [
            {"regle": regle, "motif": motifs.get(regle, ""), "operations": n}
            for regle, n in hors.most_common()
        ],
        "par_dossier": par_dossier,
        "faits_marquants": faits_marquants(rapprochements),
        "operations": [
            _operation(r)
            for r in sorted(rapprochements, key=lambda x: (x.operation.dossier, x.operation.date_operation))
        ],
        "relances": [
            {
                "dossier": rel.dossier,
                "raison_sociale": cycle.dossiers[rel.dossier].raison_sociale,
                "nom_contact": cycle.dossiers[rel.dossier].nom_contact,
                "destinataire": rel.destinataire,
                "objet": rel.objet,
                "corps": rel.corps,
                "nombre_pieces": rel.nombre_pieces,
                "montant_total": _m(rel.montant_total),
                "ton": rel.ton,
                "echeance": rel.echeance.isoformat() if rel.echeance else None,
            }
            for rel in cycle.relances
        ],
        "referentiel": {
            "generales": [
                {"nom": regle.nom, "motif": regle.motif, "operations": regles_vues[regle.nom]}
                for regle in REGLES
            ],
            "contre_exemples": list(CONTRE_EXEMPLES_LISIBLES),
            "apprises": apprises,
            "alias": [{"alias": a, "fournisseur": f} for a, f in sorted(cycle.alias.items())],
        },
        "evaluation": None,
    }

    if verite:
        ev = evaluer(rapprochements, verite)
        if ev.total:
            analyse["evaluation"] = {
                "total": ev.total,
                "justes": ev.justes,
                "taux_correct": ev.taux_correct,
                "fausses_alertes": ev.fausses_alertes,
                "non_relancer": ev.non_relancer,
                "taux_fausses_alertes": ev.taux_fausses_alertes,
                "manques_rates": ev.manques_rates,
                "a_relancer": ev.a_relancer,
                "taux_manques_rates": ev.taux_manques_rates,
                "cibles_atteintes": ev.cibles_atteintes,
                "pieces_controlees": ev.pieces_controlees,
                "pieces_erronees": ev.pieces_erronees,
                "par_statut": [
                    {"statut": s, "justes": j, "total": n} for s, (j, n) in ev.par_statut.items()
                ],
                "erreurs": [
                    {
                        "reference": e.rapprochement.operation.reference,
                        "dossier": e.rapprochement.operation.dossier,
                        "libelle": e.rapprochement.operation.libelle,
                        "attendu": e.attendu,
                        "obtenu": e.rapprochement.statut.value,
                        "regle": e.rapprochement.regle,
                    }
                    for e in ev.erreurs
                ],
            }
    return analyse


# -- HTTP --------------------------------------------------------------------

MODELES = {
    "releve_bancaire.csv", "pieces_recues.csv", "dossiers.csv",
    "alias_fournisseurs.csv", "exclusions_dossier.csv", "verite_terrain.csv",
}


def creer_gestionnaire(espace: Espace) -> type[BaseHTTPRequestHandler]:
    class Gestionnaire(BaseHTTPRequestHandler):
        server_version = "Rapprochement/1.0"

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            pass  # console silencieuse pendant la demonstration

        # -- reponses ----------------------------------------------------

        def _envoyer(self, statut: int, corps: bytes, type_contenu: str,
                     entetes: dict[str, str] | None = None) -> None:
            self.send_response(statut)
            self.send_header("Content-Type", type_contenu)
            self.send_header("Content-Length", str(len(corps)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for cle, valeur in (entetes or {}).items():
                self.send_header(cle, valeur)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(corps)

        def _json(self, donnees: object, statut: int = 200) -> None:
            corps = json.dumps(donnees, ensure_ascii=False).encode("utf-8")
            self._envoyer(statut, corps, "application/json; charset=utf-8")

        def _erreur(self, message: str, statut: int = 400) -> None:
            self._json({"erreur": message}, statut)

        def _fichier(self, chemin: Path, telechargement: str | None = None) -> None:
            type_contenu = mimetypes.guess_type(chemin.name)[0] or "application/octet-stream"
            if type_contenu.startswith("text/") or type_contenu.endswith("javascript"):
                type_contenu += "; charset=utf-8"
            entetes = {}
            if telechargement:
                entetes["Content-Disposition"] = f'attachment; filename="{telechargement}"'
            self._envoyer(200, chemin.read_bytes(), type_contenu, entetes)

        def _proteger(self, action) -> None:
            try:
                action()
            except (ErreurUtilisateur, ErreurFormat) as exc:
                self._erreur(str(exc))
            except (ValueError, KeyError, re.error, UnicodeError, csv.Error) as exc:
                self._erreur(f"Fichier illisible : {exc}")
            except Exception as exc:  # pragma: no cover - filet de securite
                print(f"[interface] erreur inattendue : {exc!r}")
                self._erreur(f"Erreur inattendue : {exc}", 500)

        # -- routes ------------------------------------------------------

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            chemin = unquote(urlparse(self.path).path)
            if chemin in ("/", "/index.html"):
                return self._fichier(STATIQUE / "index.html")
            if chemin.startswith("/static/"):
                cible = (STATIQUE / chemin.removeprefix("/static/")).resolve()
                if STATIQUE in cible.parents and cible.is_file():
                    return self._fichier(cible)
                return self._erreur("Introuvable", 404)
            if chemin.startswith("/modeles/"):
                nom = chemin.removeprefix("/modeles/")
                if nom in MODELES and (SCHEMAS / nom).exists():
                    return self._fichier(SCHEMAS / nom, telechargement=f"modele_{nom}")
                return self._erreur("Modèle introuvable", 404)
            if chemin == "/api/analyse":
                return self._proteger(lambda: self._json(espace.analyse()))
            if chemin == "/api/export/tableau_suivi.csv":
                return self._proteger(lambda: self._envoyer(
                    200, espace.suivi_csv(), "text/csv; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="tableau_suivi.csv"'},
                ))
            m = re.fullmatch(r"/api/export/relance/([\w.-]+)\.txt", chemin)
            if m:
                code = m.group(1)
                return self._proteger(lambda: self._envoyer(
                    200, espace.relance_txt(code), "text/plain; charset=utf-8",
                    {"Content-Disposition": f'attachment; filename="relance_{code}.txt"'},
                ))
            return self._erreur("Introuvable", 404)

        def do_POST(self) -> None:  # noqa: N802
            chemin = urlparse(self.path).path
            longueur = int(self.headers.get("Content-Length") or 0)
            if longueur > TAILLE_MAX_REQUETE:
                return self._erreur("Fichiers trop volumineux (25 Mo au plus).", 413)
            try:
                corps = json.loads(self.rfile.read(longueur) or b"{}")
                if not isinstance(corps, dict):
                    raise ValueError
            except ValueError:
                return self._erreur("Requête illisible.")

            def demo() -> None:
                espace.charger_demo()
                self._json(espace.analyse())

            def importer() -> None:
                espace.importer({c: corps.get(c) or "" for c in FICHIERS})
                self._json(espace.analyse())

            def apprendre() -> None:
                n = espace.apprendre(
                    str(corps.get("dossier") or ""),
                    str(corps.get("motif_regex") or ""),
                    str(corps.get("motif") or ""),
                )
                self._json({"reclassees": n, "analyse": espace.analyse()})

            def oublier() -> None:
                espace.oublier(str(corps.get("dossier") or ""), str(corps.get("motif_regex") or ""))
                self._json(espace.analyse())

            routes = {
                "/api/demo": demo,
                "/api/importer": importer,
                "/api/regles/ajouter": apprendre,
                "/api/regles/retirer": oublier,
            }
            action = routes.get(chemin)
            if action is None:
                return self._erreur("Introuvable", 404)
            self._proteger(action)

    return Gestionnaire


def creer_serveur(
    hote: str = "127.0.0.1",
    port: int = 8765,
    espace: Espace | None = None,
    essais: int = 20,
) -> ThreadingHTTPServer:
    """Cree le serveur sur le premier port libre a partir de `port`."""
    gestionnaire = creer_gestionnaire(espace or Espace())
    derniere: OSError | None = None
    for p in range(port, port + max(1, essais)):
        try:
            serveur = ThreadingHTTPServer((hote, p), gestionnaire)
            serveur.daemon_threads = True
            return serveur
        except OSError as exc:
            derniere = exc
    raise OSError(f"Aucun port libre entre {port} et {port + essais - 1}") from derniere


__all__ = ["Espace", "construire_analyse", "creer_serveur"]
