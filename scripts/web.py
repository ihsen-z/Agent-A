#!/usr/bin/env python3
"""Interface web locale du MVP et gestion de ses comptes.

Sous-commandes :
  servir                 lance l'application (127.0.0.1:8000 par defaut)
  creer-utilisateur      cree un compte (mot de passe saisi au clavier, jamais en argument)
                         et AJOUTE son nom affiche a <instance>/validateurs.txt
  lister-utilisateurs    liste les comptes (jamais les hachages)
  changer-mot-de-passe   change le mot de passe d'un compte
  supprimer-utilisateur  supprime un compte et RETIRE son nom de validateurs.txt

Les comptes ne se gerent qu'ici : l'application web n'a pas d'ecran d'administration.
Aucun envoi reel : un envoi depose un fichier .eml dans <instance>/outbox/.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement import cycle                         # noqa: E402
from rapprochement.web import comptes                   # noqa: E402
from rapprochement.web.application import Application   # noqa: E402
from rapprochement.web.serveur import (                 # noqa: E402
    HOTE_DEFAUT,
    PORT_DEFAUT,
    creer_serveur,
    est_loopback,
)


def _parseur() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sous = p.add_subparsers(dest="commande", required=True)
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--instance", required=True, help="repertoire de l'instance du cabinet")

    s = sous.add_parser("servir", parents=[base], help="lance l'application web")
    s.add_argument("--hote", default=None,
                   help=f"adresse d'ecoute (defaut {HOTE_DEFAUT} ; une adresse non locale affiche un avertissement)")
    s.add_argument("--port", type=int, default=PORT_DEFAUT, help=f"port d'ecoute (defaut {PORT_DEFAUT})")
    s.add_argument("--https", action="store_true",
                   help="ajoute Secure aux cookies (a utiliser derriere un mandataire TLS)")
    s.add_argument("--entree", default=str(RACINE / "data" / "corpus"),
                   help="repertoire des entrees du cycle (dossiers.csv : circuit et raison sociale affiches)")
    s.add_argument("--hote-autorise", action="append", default=[], metavar="NOM",
                   help="nom d'hote supplementaire accepte dans l'en-tete Host (repetable)")

    s = sous.add_parser("creer-utilisateur", parents=[base], help="cree un compte")
    s.add_argument("--identifiant", required=True)
    s.add_argument("--nom", required=True, help="nom affiche, inscrit dans validateurs.txt")
    s.add_argument("--role", required=True, choices=comptes.ROLES)
    sous.add_parser("lister-utilisateurs", parents=[base], help="liste les comptes")
    s = sous.add_parser("changer-mot-de-passe", parents=[base], help="change un mot de passe")
    s.add_argument("--identifiant", required=True)
    s = sous.add_parser("supprimer-utilisateur", parents=[base], help="supprime un compte")
    s.add_argument("--identifiant", required=True)
    return p


def _saisir_mot_de_passe() -> str:
    """Deux saisies masquees identiques ; jamais d'argument de ligne de commande (historique)."""
    premier = getpass.getpass("Mot de passe (10 caracteres au moins) : ")
    comptes.exiger_mot_de_passe(premier)
    second = getpass.getpass("Confirmer le mot de passe : ")
    if premier != second:
        raise comptes.ErreurCompte("les deux saisies different")
    return premier


def _instance(chemin: str, *, exister: bool = True) -> cycle.Instance:
    inst = cycle.Instance(Path(chemin))
    if exister and not inst.racine.is_dir():
        raise comptes.ErreurCompte(f"instance introuvable : {inst.racine}")
    return inst


def _servir(args: argparse.Namespace) -> int:
    inst = _instance(args.instance)
    hote = args.hote or HOTE_DEFAUT
    hotes = {"127.0.0.1", "localhost", "::1", *(h.lower() for h in args.hote_autorise)}
    if not est_loopback(hote):
        hotes.add(hote.strip("[]").lower())
        print(f"AVERTISSEMENT : ecoute sur {hote}, adresse NON locale. L'application est alors joignable "
              "depuis le reseau, sans chiffrement (pas de TLS). A reserver a un reseau de confiance.",
              file=sys.stderr)
    if not comptes.lire_utilisateurs(inst):
        print("AVERTISSEMENT : aucun compte. Creer d'abord un utilisateur avec `creer-utilisateur`.",
              file=sys.stderr)
    entree = Path(args.entree)
    application = Application(inst, https=args.https, hotes=frozenset(hotes),
                              entree=entree if entree.is_dir() else None)
    serveur = creer_serveur(application, hote, args.port)
    adresse = f"[{hote.strip('[]')}]" if ":" in hote else hote
    print(f"Application disponible sur http{'s' if args.https else ''}://{adresse}:{serveur.server_port}/ "
          "(Ctrl+C pour arreter)")
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        print("Arret.")
    finally:
        serveur.server_close()
        application.fermer()
    return 0


def _executer(args: argparse.Namespace) -> int:
    c = args.commande
    if c == "servir":
        return _servir(args)
    inst = _instance(args.instance)
    if c == "creer-utilisateur":
        comptes.exiger_identifiant(args.identifiant)
        comptes.exiger_nom(args.nom)
        mot_de_passe = _saisir_mot_de_passe()
        u = comptes.creer_utilisateur(inst, args.identifiant, args.nom, args.role, mot_de_passe)
        print(f"compte {u.identifiant} cree ({u.role}) ; {cycle.affichable(u.nom)!s} ajoute a "
              f"{inst.validateurs.name} s'il n'y figurait pas")
        return 0
    if c == "lister-utilisateurs":
        utilisateurs = comptes.lire_utilisateurs(inst)
        for u in sorted(utilisateurs.values(), key=lambda u: u.identifiant):
            print(f"{u.identifiant:20s} {u.role:12s} {cycle.affichable(u.nom)}")
        print(f"{len(utilisateurs)} compte(s)")
        return 0
    if c == "changer-mot-de-passe":
        ident = comptes.normaliser_identifiant(args.identifiant)
        if ident not in comptes.lire_utilisateurs(inst):
            raise comptes.ErreurCompte(f"identifiant inconnu : {ident!r}")
        u = comptes.changer_mot_de_passe(inst, ident, _saisir_mot_de_passe())
        print(f"mot de passe de {u.identifiant} change (ses sessions ouvertes sont fermees)")
        return 0
    if c == "supprimer-utilisateur":
        u = comptes.supprimer_utilisateur(inst, args.identifiant)
        print(f"compte {u.identifiant} supprime ; {cycle.affichable(u.nom)} retire de {inst.validateurs.name}")
        return 0
    raise AssertionError(c)


def main(argv: list[str] | None = None) -> int:
    args = _parseur().parse_args(argv)
    try:
        return _executer(args)
    except comptes.ErreurCompte as exc:
        print(f"REFUS : {cycle.affichable(exc)}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"REFUS ({type(exc).__name__}) : {cycle.affichable(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
