#!/usr/bin/env python3
"""Cycle du MVP : detection, relances validees par un humain, rattachement des pieces.

Sous-commandes : cycle, valider, rejeter, envoyer, trancher, file, rattacher,
controler, promesse, arbitrer, bloquer, debloquer, pieces, verifier-audit,
exporter. Toute decision humaine (valider, rejeter, trancher, rattacher,
controler, promesse, arbitrer, bloquer, debloquer) exige `--par NOM`, NOM etant
declare dans `<instance>/validateurs.txt`. Le cycle ne valide jamais rien lui-meme.
`pieces` est en lecture seule.

Exception de nommage : pour `promesse`, `--date` est la date PROMISE par le
client (comme demande) ; le jour courant s'y donne avec `--le`.

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
from rapprochement.modeles import EtatPiece           # noqa: E402

FUSEAU = ZoneInfo("Europe/Paris")


def _date(valeur: str) -> dt.date:
    try:
        return dt.date.fromisoformat(valeur)
    except ValueError:
        raise argparse.ArgumentTypeError(f"date invalide {valeur!r} (attendu AAAA-MM-JJ)") from None


def _heure(valeur: str) -> str:
    try:
        dt.time.fromisoformat(valeur)
    except ValueError:
        raise argparse.ArgumentTypeError(f"heure invalide {valeur!r} (attendu HH:MM)") from None
    return valeur


def _instants(args: argparse.Namespace) -> tuple[dt.date, dt.datetime]:
    if args.jour:
        jour = args.jour
        heure = dt.time.fromisoformat(args.heure) if args.heure else dt.time(10, 0)
        return jour, dt.datetime.combine(jour, heure, tzinfo=FUSEAU)
    maintenant = dt.datetime.now(FUSEAU)
    if args.heure:
        maintenant = dt.datetime.combine(maintenant.date(), dt.time.fromisoformat(args.heure), tzinfo=FUSEAU)
    return maintenant.date(), maintenant


def _parseur() -> argparse.ArgumentParser:
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--instance", required=True, help="repertoire de l'instance du cabinet")
    base.add_argument("--heure", type=_heure, help="HH:MM, heure locale Europe/Paris")
    commun = argparse.ArgumentParser(add_help=False, parents=[base])
    commun.add_argument("--date", dest="jour", type=_date, help="AAAA-MM-JJ (defaut : aujourd'hui)")
    humain = argparse.ArgumentParser(add_help=False)
    humain.add_argument("--par", required=True, help="nom declare dans validateurs.txt")
    piece = argparse.ArgumentParser(add_help=False)
    piece.add_argument("--dossier", required=True)
    piece.add_argument("--reference", required=True)

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

    s = sous.add_parser("promesse", parents=[base, humain, piece],
                        help="saisit la date promise par le client (DEMANDEE -> PROMISE)")
    s.add_argument("--date", dest="date_promesse", type=_date, required=True,
                   help="date PROMISE, AAAA-MM-JJ, dans [aujourd'hui, +15 jours]")
    s.add_argument("--le", dest="jour", type=_date, help="jour courant AAAA-MM-JJ (defaut : aujourd'hui)")
    s = sous.add_parser("arbitrer", parents=[commun, humain, piece],
                        help="classe sans suite une piece ESCALADEE")
    s.add_argument("--motif", required=True)
    s = sous.add_parser("bloquer", parents=[commun, humain, piece], help="bloque une piece")
    s.add_argument("--motif", required=True)
    s = sous.add_parser("debloquer", parents=[commun, humain, piece], help="debloque une piece")
    s.add_argument("--motif", required=True)
    s = sous.add_parser("pieces", parents=[commun], help="liste les pieces et l'etat des periodes")
    s.add_argument("--dossier")
    s.add_argument("--etat", help="filtre : " + ", ".join(e.value for e in EtatPiece))

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
    if c in ("promesse", "arbitrer", "bloquer", "debloquer"):
        if c == "promesse":
            p = cycle.saisir_promesse(inst, args.dossier, args.reference, args.par,
                                      date_promesse=args.date_promesse, maintenant=maintenant)
        else:
            fonction = {"arbitrer": cycle.arbitrer_piece, "bloquer": cycle.bloquer_piece,
                        "debloquer": cycle.debloquer_piece}[c]
            p = fonction(inst, args.dossier, args.reference, args.par, motif=args.motif,
                         maintenant=maintenant)
        drapeau = " (bloquee)" if p.bloquee else ""
        promesse = f" promise le {p.date_promesse.isoformat()}" if p.date_promesse else ""
        print(f"{p.dossier}/{p.reference} -> {p.etat.value}{promesse}{drapeau}")
        return 0
    if c == "pieces":
        print(cycle.suivi_pieces(inst, aujourdhui, dossier=args.dossier, etat=args.etat).texte(), end="")
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
