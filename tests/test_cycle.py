"""Tests de bout en bout de l'assemblage (src/rapprochement/cycle.py, scripts/cycle.py).

Les tests marques `xfail(strict=True)` reproduisent des defauts ou contradictions
trouves dans les modules des autres experts : ils deviendront rouges le jour ou
le defaut sera corrige (il faudra alors retirer le marqueur).
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
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

    # Jour 18 : 14 jours ouvres depuis la premiere demande -> ESCALADE reelle.
    r18 = lancer(entree, inst, jour(18))
    assert r18.transitions == 5 and r18.brouillons_crees == 0
    assert _etats(inst) == {(EtatPiece.ESCALADEE, 3)}

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
