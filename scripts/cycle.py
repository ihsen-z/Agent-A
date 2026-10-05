#!/usr/bin/env python3
"""Cycle du MVP : detection, relances validees par un humain, rattachement des pieces.

Sous-commandes : cycle, valider, rejeter, envoyer, trancher, file, rattacher,
controler, verifier-audit, exporter. `valider`, `rejeter`, `trancher`,
`rattacher` et `controler` exigent `--par NOM`, NOM etant declare dans
`<instance>/validateurs.txt`. Le cycle ne valide jamais rien lui-meme.

`--date` vaut aujourd'hui par defaut (seul endroit ou l'horloge est lue) ;
l'heure de `maintenant` vient de `--heure` (defaut : l'heure courante si
`--date` est absent, 10:00 sinon), dans le fuseau Europe/Paris.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement import cycle                       # noqa: E402
from rapprochement.envoi import (                     # noqa: E402
    EnvoiHorsCreneau,
    EnvoiNonValide,
    ErreurEnvoi,
)
from rapprochement.etats import TransitionInterdite   # noqa: E402

FUSEAU = ZoneInfo("Europe/Paris")


def _instants(args: argparse.Namespace) -> tuple[dt.date, dt.datetime]:
    if args.date:
        jour = dt.date.fromisoformat(args.date)
        heure = dt.time.fromisoformat(args.heure) if args.heure else dt.time(10, 0)
        return jour, dt.datetime.combine(jour, heure, tzinfo=FUSEAU)
    maintenant = dt.datetime.now(FUSEAU)
    if args.heure:
        maintenant = dt.datetime.combine(maintenant.date(), dt.time.fromisoformat(args.heure), tzinfo=FUSEAU)
    return maintenant.date(), maintenant


def _parseur() -> argparse.ArgumentParser:
    commun = argparse.ArgumentParser(add_help=False)
    commun.add_argument("--instance", required=True, help="repertoire de l'instance du cabinet")
    commun.add_argument("--date", help="AAAA-MM-JJ (defaut : aujourd'hui)")
    commun.add_argument("--heure", help="HH:MM, heure locale Europe/Paris")
    humain = argparse.ArgumentParser(add_help=False)
    humain.add_argument("--par", required=True, help="nom declare dans validateurs.txt")

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sous = p.add_subparsers(dest="commande", required=True)

    s = sous.add_parser("cycle", parents=[commun], help="execute un cycle")
    s.add_argument("--entree", default=str(RACINE / "data" / "corpus"))

    s = sous.add_parser("valider", parents=[commun, humain], help="valide un brouillon")
    s.add_argument("id_relance")
    s = sous.add_parser("rejeter", parents=[commun, humain], help="rejette un brouillon")
    s.add_argument("id_relance")
    s.add_argument("--motif", required=True)
    s = sous.add_parser("envoyer", parents=[commun], help="emet un brouillon valide")
    s.add_argument("id_relance")
    s = sous.add_parser("trancher", parents=[commun, humain], help="tranche un envoi incertain")
    s.add_argument("id_relance")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--parti", dest="parti", action="store_true")
    g.add_argument("--non-parti", dest="parti", action="store_false")

    sous.add_parser("file", parents=[commun], help="liste la file humaine")

    s = sous.add_parser("rattacher", parents=[commun, humain], help="confirme un rattachement")
    s.add_argument("--message-id")
    s.add_argument("--empreinte", help="empreinte (ou prefixe unique) du fichier")
    s.add_argument("--dossier")
    s.add_argument("--reference")
    s.add_argument("--piece", help="identifiant de la piece (defaut : fichier#empreinte)")

    s = sous.add_parser("controler", parents=[commun, humain], help="controle une piece recue")
    s.add_argument("--dossier", required=True)
    s.add_argument("--reference", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--conforme", dest="conforme", action="store_true")
    g.add_argument("--non-conforme", dest="conforme", action="store_false")
    s.add_argument("--motif", default="")

    sous.add_parser("verifier-audit", parents=[commun], help="verifie le journal d'audit")
    s = sous.add_parser("exporter", parents=[commun], help="export JSON complet du depot")
    s.add_argument("--vers", required=True)
    return p


def _executer(args: argparse.Namespace) -> int:
    inst = cycle.Instance(Path(args.instance))
    aujourdhui, maintenant = _instants(args)
    c = args.commande
    if c == "cycle":
        resume = cycle.executer_cycle(Path(args.entree), inst, aujourdhui, maintenant)
        print(resume.texte(), end="")
        print(f"Sorties : {inst.sortie}")
        return 0
    if c == "valider":
        b = cycle.valider_relance(inst, args.id_relance, args.par, maintenant=maintenant)
        print(f"{b.id_relance} valide par {b.valide_par}")
        return 0
    if c == "rejeter":
        b = cycle.rejeter_relance(inst, args.id_relance, args.par, args.motif, maintenant=maintenant)
        print(f"{b.id_relance} rejete")
        return 0
    if c == "envoyer":
        b = cycle.envoyer_relance(inst, args.id_relance, maintenant=maintenant)
        print(f"{b.id_relance} envoye a {b.destinataire}")
        return 0
    if c == "trancher":
        b = cycle.trancher_envoi(inst, args.id_relance, args.par, parti=args.parti, maintenant=maintenant)
        print(f"{b.id_relance} -> {b.statut.value}")
        return 0
    if c == "file":
        entrees = cycle.lister_file(inst)
        for d in entrees:
            print(f"{d.statut.value:10s} {d.motif.value if d.motif else '-':20s} "
                  f"{d.dossier or '-':14s} {d.reference_operation or '-':10s} "
                  f"{d.message_id} {d.empreinte[:12] or '-'} {d.nom_fichier or '-'} : {d.detail}")
        print(f"{len(entrees)} entree(s) a traiter")
        return 0
    if c == "rattacher":
        p = cycle.rattacher_piece(
            inst, args.par, maintenant=maintenant, message_id=args.message_id,
            empreinte=args.empreinte, dossier=args.dossier, reference=args.reference,
            id_piece=args.piece,
        )
        print(f"{p.dossier}/{p.reference} -> {p.etat.value}")
        return 0
    if c == "controler":
        p = cycle.controler_piece(inst, args.dossier, args.reference, args.par,
                                  conforme=args.conforme, motif=args.motif, maintenant=maintenant)
        print(f"{p.dossier}/{p.reference} -> {p.etat.value}")
        return 0
    if c == "verifier-audit":
        r = cycle.verifier_audit(inst)
        print(f"{'OK' if r.ok else 'ALTERE'} : {r.nb_entrees} entree(s) saine(s) ; {r.raison}")
        return 0 if r.ok else 1
    if c == "exporter":
        print(cycle.exporter(inst, args.vers))
        return 0
    raise AssertionError(c)


def main(argv: list[str] | None = None) -> int:
    args = _parseur().parse_args(argv)
    try:
        return _executer(args)
    except (cycle.ErreurCycle, EnvoiNonValide, EnvoiHorsCreneau, ErreurEnvoi,
            TransitionInterdite) as exc:
        print(f"REFUS ({type(exc).__name__}) : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
