#!/usr/bin/env python3
"""Cycle du MVP : detection, relances validees par un humain, rattachement des pieces.

Sous-commandes : cycle, valider, rejeter, envoyer, trancher, file, rattacher,
controler, promesse, arbitrer, bloquer, debloquer, pieces, verifier-audit,
controler-envois, reparer-audit, exporter. Toute decision humaine (valider, rejeter,
envoyer, trancher, rattacher, controler, promesse, arbitrer, bloquer, debloquer,
reparer-audit) exige `--par NOM`, NOM etant declare dans `<instance>/validateurs.txt` (ou
`responsables.txt`). `arbitrer`, `trancher` et `reparer-audit` sont reservees aux
RESPONSABLES (`responsables.txt` ; absent : refus, sauf repli annonce en mode test).
`controler-envois` (recette de CA-06) et `pieces` sont en lecture seule. Le cycle ne valide jamais rien lui-meme.
`pieces` est en lecture seule.

Exception de nommage : pour `promesse`, `--date` est la date PROMISE par le
client (comme demande) ; le jour courant s'y donne avec `--le`.

`--date` vaut aujourd'hui par defaut (seul endroit ou l'horloge est lue) ;
l'heure de `maintenant` vient de `--heure` (defaut : l'heure courante si
`--date` est absent, 10:00 sinon), dans le fuseau Europe/Paris.

Mode test (essais locaux, donnees fictives) : `--mode-test` ou `"mode_test": true` dans
`<instance>/config.json`. Le mode est une propriete de l'INSTANCE, fige par son premier cycle :
toute commande d'ecriture doit demander le meme mode (drapeau ou config.json), sinon refus
(« cette instance a ete creee en mode ... »). Il ne se change pas : creer une nouvelle instance. Il est affiche partout, journalise, et
tout e-mail part vers `adresse_test` (jamais vers le vrai destinataire). `--sans-isolation`
(mode test uniquement) lit les .eml sans processus enfant ; `--isolation fork|spawn|directe`
choisit la strategie (par defaut : fork sous Linux/macOS, spawn sous Windows en mode test,
refus sous Windows hors mode test).
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
from rapprochement.audit import JournalCorrompu       # noqa: E402
from rapprochement.etats import TransitionInterdite   # noqa: E402
from rapprochement.parseurs import ErreurFormat       # noqa: E402
from rapprochement.modeles import EtatPiece           # noqa: E402



def _fuseau() -> ZoneInfo:
    """Europe/Paris, avec le repli de `cycle.garantir_fuseaux` quand le systeme n'a pas de
    base de fuseaux (Windows sans le paquet `tzdata`). Paresseux : aucun effet a l'import."""
    cycle.garantir_fuseaux()
    return ZoneInfo(cycle.FUSEAU)


def _console_utf8() -> None:
    """Console Windows en cp1252 : un caractere hors de cette page de codes (cyrillique,
    CJK, emoji...) leverait UnicodeEncodeError en plein cycle, apres des ecritures. On
    force UTF-8 avec remplacement : l'affichage peut etre degrade, jamais interrompu.
    Sans effet si le flux n'a pas `reconfigure` (flux de remplacement, tests)."""
    for flux in (sys.stdout, sys.stderr):
        reconfigurer = getattr(flux, "reconfigure", None)
        if reconfigurer is None:
            continue
        try:
            reconfigurer(encoding="utf-8", errors="replace")
        except (ValueError, OSError):   # flux ferme ou deja utilise sans pouvoir changer
            pass


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
        return jour, dt.datetime.combine(jour, heure, tzinfo=_fuseau())
    maintenant = dt.datetime.now(_fuseau())
    if args.heure:
        maintenant = dt.datetime.combine(maintenant.date(), dt.time.fromisoformat(args.heure),
                                         tzinfo=_fuseau())
    return maintenant.date(), maintenant


def _parseur() -> argparse.ArgumentParser:
    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--instance", required=True, help="repertoire de l'instance du cabinet")
    base.add_argument("--heure", type=_heure, help="HH:MM, heure locale Europe/Paris")
    commun = argparse.ArgumentParser(add_help=False, parents=[base])
    commun.add_argument("--date", dest="jour", type=_date, help="AAAA-MM-JJ (defaut : aujourd'hui)")
    humain = argparse.ArgumentParser(add_help=False)
    humain.add_argument("--par", required=True,
                        help="nom declare dans validateurs.txt (ou responsables.txt)")
    humain.add_argument("--mode-test", action="store_true",
                        help="mode demande pour cette commande ; DOIT etre celui de l'instance "
                             "(fige au premier cycle) sinon refus. Repli sur validateurs.txt si "
                             "responsables.txt est absent (arbitrer, trancher, reparer-audit, --forcer)")
    piece = argparse.ArgumentParser(add_help=False)
    piece.add_argument("--dossier", required=True)
    piece.add_argument("--reference", required=True)

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sous = p.add_subparsers(dest="commande", required=True)

    s = sous.add_parser("cycle", parents=[commun], help="execute un cycle")
    s.add_argument("--entree", default=str(RACINE / "data" / "corpus"))
    s.add_argument("--mode-test", action="store_true",
                   help="mode test : annonce partout, e-mails vers l'adresse de test (config.json)")
    s.add_argument("--isolation", choices=cycle.STRATEGIES_ISOLATION,
                   help="strategie de lecture des .eml (defaut : automatique)")
    s.add_argument("--sans-isolation", action="store_true",
                   help="lit les .eml sans processus enfant (mode test uniquement)")

    s = sous.add_parser("valider", parents=[commun, humain], help="valide un brouillon")
    s.add_argument("id_relance")
    s = sous.add_parser("rejeter", parents=[commun, humain], help="rejette un brouillon")
    s.add_argument("id_relance")
    s.add_argument("--motif", required=True)
    s = sous.add_parser("envoyer", parents=[commun, humain], help="emet un brouillon valide")
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
    s.add_argument("--forcer", action="store_true",
                   help="autorise un dossier DIFFERENT de celui de la decision de routage "
                        "(RESPONSABLE seulement ; exige --motif)")
    s.add_argument("--motif", default="", help="motif obligatoire avec --forcer (journalise)")

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
                        help="classe sans suite une piece ESCALADEE (responsable)")
    s.add_argument("--motif", required=True)
    s = sous.add_parser("bloquer", parents=[commun, humain, piece], help="bloque une piece")
    s.add_argument("--motif", required=True)
    s = sous.add_parser("debloquer", parents=[commun, humain, piece], help="debloque une piece")
    s.add_argument("--motif", required=True)
    s = sous.add_parser("pieces", parents=[commun], help="liste les pieces et l'etat des periodes")
    s.add_argument("--dossier")
    s.add_argument("--etat", help="filtre : " + ", ".join(e.value for e in EtatPiece))

    sous.add_parser("verifier-audit", parents=[commun], help="verifie le journal d'audit")
    s = sous.add_parser("reparer-audit", parents=[commun, humain],
                        help="retire une derniere ligne tronquee du journal (coupure), jamais plus (responsable)")
    sous.add_parser("controler-envois", parents=[commun],
                    help="recette de CA-06 : chaque envoi a une validation anterieure par un validateur declare")
    s = sous.add_parser("exporter", parents=[commun], help="export JSON complet du depot")
    s.add_argument("--vers", required=True)
    return p


def _avertir_roles(inst: cycle.Instance, args: argparse.Namespace) -> None:
    """Annonce le repli sur les validateurs quand `responsables.txt` manque EN MODE TEST."""
    avertissement = cycle.avertissement_roles(inst, getattr(args, "mode_test", False))
    if avertissement:
        _afficher(f"AVERTISSEMENT : {avertissement}")


def _executer(args: argparse.Namespace) -> int:
    inst = cycle.Instance(Path(args.instance))
    aujourdhui, maintenant = _instants(args)
    c = args.commande
    if c == "cycle":
        if args.sans_isolation and args.isolation not in (None, "directe"):
            raise cycle.ErreurCycle("--sans-isolation contredit --isolation " + args.isolation)
        strategie = "directe" if args.sans_isolation else args.isolation
        resume = cycle.executer_cycle(Path(args.entree), inst, aujourdhui, maintenant,
                                      mode_test=args.mode_test, strategie_isolation=strategie)
        _afficher(resume.texte(), end="")
        _afficher(f"Sorties : {inst.sortie}")
        return 0
    if c == "valider":
        b = cycle.valider_relance(inst, args.id_relance, args.par, maintenant=maintenant,
                                  mode_test=args.mode_test)
        _afficher(f"{b.id_relance} valide par {b.valide_par}")
        return 0
    if c == "rejeter":
        b = cycle.rejeter_relance(inst, args.id_relance, args.par, args.motif, maintenant=maintenant,
                                  mode_test=args.mode_test)
        _afficher(f"{b.id_relance} rejete")
        return 0
    if c == "envoyer":
        b = cycle.envoyer_relance(inst, args.id_relance, maintenant=maintenant,
                                  mode_test=args.mode_test, par=args.par)
        config = cycle.charger_config(inst)
        if args.mode_test or config["mode_test"]:
            _afficher(f"[MODE TEST] {b.id_relance} envoye a {config['adresse_test']} "
                      f"(destinataire reel, NON contacte : {b.destinataire}) par {args.par}")
            for coupe in cycle.garde_fous_mode_test(inst, args.mode_test):
                _afficher(f"GARDE-FOU DESACTIVE (mode test) : {coupe}")
        else:
            _afficher(f"{b.id_relance} envoye a {b.destinataire} (declenche par {args.par})")
        return 0
    if c == "trancher":
        _avertir_roles(inst, args)
        b = cycle.trancher_envoi(inst, args.id_relance, args.par, parti=args.parti,
                                 maintenant=maintenant, mode_test=args.mode_test)
        _afficher(f"{b.id_relance} -> {b.statut.value}")
        return 0
    if c == "file":
        entrees = cycle.lister_file(inst)
        for d in entrees:
            v = cycle.affichable   # Message-ID, nom de fichier, detail : choisis par l'expediteur
            contexte = cycle.contexte_file(d)
            origine = ""
            if "expediteur" in contexte:
                origine = (f" [de {v(contexte['expediteur'])} le {v(contexte.get('recu_le', ''))}"
                           f" : {v(contexte.get('objet', ''))}]")
            _afficher(f"{d.statut.value:10s} {d.motif.value if d.motif else '-':20s} "
                      f"{v(d.dossier or '-'):14s} {v(d.reference_operation or '-'):10s} "
                      f"{v(d.message_id)} {v(d.empreinte[:12] or '-')} {v(d.nom_fichier or '-')} "
                      f": {v(contexte['raison'])}{origine}")
        _afficher(f"{len(entrees)} entree(s) a traiter")
        return 0
    if c == "rattacher":
        if args.motif and not args.forcer:
            raise cycle.ErreurCycle("--motif n'a de sens qu'avec --forcer")
        if args.forcer:
            _avertir_roles(inst, args)
        p = cycle.rattacher_piece(
            inst, args.par, maintenant=maintenant, message_id=args.message_id,
            empreinte=args.empreinte, dossier=args.dossier, reference=args.reference,
            id_piece=args.piece, forcer=args.forcer, motif=args.motif,
            mode_test=args.mode_test,
        )
        _afficher(f"{p.dossier}/{p.reference} -> {p.etat.value}")
        return 0
    if c == "controler":
        p = cycle.controler_piece(inst, args.dossier, args.reference, args.par,
                                  conforme=args.conforme, motif=args.motif, maintenant=maintenant,
                                  mode_test=args.mode_test)
        _afficher(f"{p.dossier}/{p.reference} -> {p.etat.value}")
        return 0
    if c in ("promesse", "arbitrer", "bloquer", "debloquer"):
        if c == "arbitrer":
            _avertir_roles(inst, args)
            p = cycle.arbitrer_piece(inst, args.dossier, args.reference, args.par, motif=args.motif,
                                     maintenant=maintenant, mode_test=args.mode_test)
        elif c == "promesse":
            p = cycle.saisir_promesse(inst, args.dossier, args.reference, args.par,
                                      date_promesse=args.date_promesse, maintenant=maintenant,
                                      mode_test=args.mode_test)
        else:
            fonction = {"bloquer": cycle.bloquer_piece, "debloquer": cycle.debloquer_piece}[c]
            p = fonction(inst, args.dossier, args.reference, args.par, motif=args.motif,
                         maintenant=maintenant, mode_test=args.mode_test)
        drapeau = " (bloquee)" if p.bloquee else ""
        promesse = f" promise le {p.date_promesse.isoformat()}" if p.date_promesse else ""
        _afficher(f"{p.dossier}/{p.reference} -> {p.etat.value}{promesse}{drapeau}")
        return 0
    if c == "pieces":
        _afficher(cycle.suivi_pieces(inst, aujourdhui, dossier=args.dossier, etat=args.etat).texte(), end="")
        return 0
    if c == "verifier-audit":
        r = cycle.verifier_audit(inst)
        _afficher(f"{'OK' if r.ok else 'ALTERE'} : {r.nb_entrees} entree(s) saine(s) ; {r.raison}")
        return 0 if r.ok else 1
    if c == "reparer-audit":
        _avertir_roles(inst, args)
        r = cycle.reparer_audit(inst, args.par, maintenant=maintenant, mode_test=args.mode_test)
        if r.octets_retires:
            _afficher(f"journal repare : {r.octets_retires} octet(s) retire(s), sauvegardes dans {r.fragment}")
        else:
            _afficher("journal sain : rien a reparer")
        return 0
    if c == "controler-envois":
        controle = cycle.controler_envois(inst)
        _afficher(controle.texte(), end="")
        return 0 if controle.ok else 1
    if c == "exporter":
        _afficher(cycle.exporter(inst, args.vers))
        return 0
    raise AssertionError(c)


def _afficher(texte: object = "", *, end: str = "\n", file=None) -> None:
    """print() dont chaque ligne passe par `cycle.affichable` : aucune valeur venue de
    l'exterieur (Message-ID, nom de fichier, libelle, motif) ne pilote le terminal."""
    propre = "\n".join(cycle.affichable(ligne) for ligne in str(texte).split("\n"))
    print(propre, end=end, file=file if file is not None else sys.stdout)


def main(argv: list[str] | None = None) -> int:
    _console_utf8()
    args = _parseur().parse_args(argv)
    try:
        return _executer(args)
    except ErreurFormat as exc:
        # Entree illisible (releve, pieces, dossiers) : refus lisible, rien n'a ete ecrit.
        _afficher(f"REFUS [cycle] entree invalide (ErreurFormat) : {exc}", file=sys.stderr)
        return 1
    except JournalCorrompu as exc:
        # Alteration ailleurs qu'en fin : affichee telle quelle, jamais contournee.
        _afficher(f"REFUS (JournalCorrompu) : {exc}", file=sys.stderr)
        return 1
    except (cycle.ErreurCycle, EnvoiNonValide, EnvoiHorsCreneau, ErreurEnvoi,
            TransitionInterdite) as exc:
        _afficher(f"REFUS ({type(exc).__name__}) : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
