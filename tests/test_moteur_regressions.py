"""Regressions du moteur d'appariement, issues de l'audit de conformite.

CA-02 exige zero piece manquante ratee. L'audit a montre que le moteur en ratait sur
son propre corpus synthetique (0,89 % sur 60 graines). Chaque test ci-dessous
reproduit une CAUSE trouvee, pas un symptome. Le dernier rejoue les graines qui ont
deja fait echouer le moteur.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement.modeles import OperationBancaire, Piece, Sens, Statut  # noqa: E402
from rapprochement import moteur as module_moteur  # noqa: E402
from rapprochement.moteur import SEUIL_CERTITUDE, Moteur  # noqa: E402


def operation(ref, date, libelle, montant, dossier="D1"):
    return OperationBancaire(
        reference=ref, dossier=dossier, date_operation=dt.date.fromisoformat(date),
        libelle=libelle, montant=Decimal(montant), sens=Sens.DEBIT,
    )


def piece(id_piece, date, fournisseur, montant, dossier="D1"):
    return Piece(
        id_piece=id_piece, dossier=dossier, fichier=f"{id_piece}.pdf", fournisseur=fournisseur,
        date_facture=dt.date.fromisoformat(date), montant_ttc=Decimal(montant),
    )


def statuts(ops, pieces):
    return {r.operation.reference: r.statut for r in Moteur().rapprocher(ops, pieces)}


def rapprochements(ops, pieces):
    return {r.operation.reference: r for r in Moteur().rapprocher(ops, pieces)}


# ------------------------------------------------------------------------------
# Cause 1 : un montant approche, plus proche en date, battait un montant exact.
# ------------------------------------------------------------------------------
def test_un_montant_exact_l_emporte_sur_un_montant_approche_plus_proche_en_date():
    ops = [
        operation("OP093", "2026-03-10", "PRLV SEPA SG CREDIT BAIL ID827462", "628.75"),
        operation("OP119", "2026-03-18", "PRLV SEPA SG CREDIT BAIL ID361317", "625.82"),
    ]
    pieces = [piece("P34", "2026-03-05", "Societe Generale Credit-Bail", "625.82")]
    resultat = statuts(ops, pieces)
    assert resultat["OP119"] is Statut.JUSTIFIE, "le paiement exact au centime doit avoir la facture"
    assert resultat["OP093"] is Statut.MANQUANT, "628,75 n'est pas 625,82 : il reste a reclamer"


# ------------------------------------------------------------------------------
# Cause 2 : a un centime pres, la date departageait au hasard.
# ------------------------------------------------------------------------------
def test_identique_au_centime_l_emporte_sur_un_ecart_d_un_centime_plus_proche_en_date():
    ops = [
        operation("OP274", "2026-04-22", "CARTE 11/04 TOTAL RELAIS 4471", "69.60"),
        operation("OP276", "2026-05-04", "CARTE 13/04 TOTAL RELAIS 4471", "69.61"),
    ]
    pieces = [piece("P94", "2026-04-20", "Total Relais", "69.61")]
    resultat = statuts(ops, pieces)
    assert resultat["OP276"] is Statut.JUSTIFIE
    assert resultat["OP274"] is Statut.MANQUANT


# ------------------------------------------------------------------------------
# Cause 3 : une correspondance incertaine consommait la facture d'un reglement groupe.
# ------------------------------------------------------------------------------
def test_une_correspondance_incertaine_ne_prive_pas_un_reglement_groupe_de_ses_factures():
    pieces = [
        piece("G0", "2026-03-22", "Point P Materiaux", "848.13"),
        piece("G1", "2026-03-18", "Point P Materiaux", "438.16"),
        piece("G2", "2026-03-14", "Point P Materiaux", "347.33"),
    ]
    assert sum(p.montant_ttc for p in pieces) == Decimal("1633.62")
    ops = [
        operation("OPGRP", "2026-04-03", "VIR SEPA POINT P MATERIAUX REGLT GROUPE 03/04", "1633.62"),
        operation("OPSEUL", "2026-03-20", "VIR SEPA POINT P MATERIAUX 12/03", "848.47"),
    ]
    resultat = rapprochements(ops, pieces)
    groupe = resultat["OPGRP"]
    # Un reglement groupe est toujours « a verifier » (la penalite de groupement le
    # maintient sous le seuil de certitude) ; ce qui compte est qu'il garde ses 3 factures.
    assert groupe.statut in (Statut.JUSTIFIE, Statut.A_VERIFIER)
    assert {p.id_piece for p in groupe.pieces} == {"G0", "G1", "G2"}, "le groupe doit garder ses 3 factures"
    assert resultat["OPSEUL"].statut is Statut.MANQUANT, "848,47 n'a aucune facture a lui : a reclamer"


def test_un_montant_approche_ne_peut_jamais_etre_une_certitude():
    """Garde-fou structurel : meme fournisseur identique et meme jour, le score reste
    sous SEUIL_CERTITUDE, donc la correspondance est traitee APRES les reglements groupes."""
    assert 0.55 * module_moteur.SCORE_MONTANT_APPROCHE + 0.30 + 0.15 < SEUIL_CERTITUDE
    op = operation("OP1", "2026-03-18", "VIR SEPA POINT P MATERIAUX", "848.47")
    p = piece("P1", "2026-03-18", "Point P Materiaux", "848.13")
    assert Moteur()._score(op, [p]) < SEUIL_CERTITUDE


def test_un_ecart_d_arrondi_de_deux_centimes_reste_justifie():
    """La tolerance d'arrondi du cahier (0,02 EUR) ne doit pas etre degradee."""
    ops = [operation("OP1", "2026-03-18", "PRLV SEPA ORANGE FRANCE ID1", "89.91")]
    pieces = [piece("P1", "2026-03-16", "Orange France", "89.90")]
    assert statuts(ops, pieces)["OP1"] is Statut.JUSTIFIE


# ------------------------------------------------------------------------------
# Cause 5 : deux paiements equivalents, une seule facture : on ne tranche pas.
# ------------------------------------------------------------------------------
def test_deux_paiements_equivalents_pour_une_seule_facture_ne_sont_pas_tranches():
    ops = [
        operation("OP129", "2026-04-02", "CARTE 03/03 AMZN MKTPLACE FR", "447.03"),
        operation("OP140", "2026-04-10", "CARTE 16/03 AMZN MKTPLACE FR", "447.03"),
    ]
    pieces = [piece("P39", "2026-03-16", "AMZN MKTPLACE FR", "447.03")]
    resultat = rapprochements(ops, pieces)
    assert resultat["OP129"].statut is Statut.MANQUANT
    assert resultat["OP140"].statut is Statut.MANQUANT, "on ne sait pas lequel des deux est justifie"
    assert "ambiguite_non_tranchee" in {r.regle for r in resultat.values()}


def test_deux_paiements_assez_espaces_restent_departages_par_la_date():
    ops = [
        operation("OP1", "2026-04-02", "PRLV SEPA OVH", "83.99"),
        operation("OP2", "2026-05-20", "PRLV SEPA OVH", "83.99"),
    ]
    pieces = [piece("P1", "2026-03-30", "OVH SAS", "83.99")]
    resultat = statuts(ops, pieces)
    assert resultat["OP1"] is Statut.JUSTIFIE
    assert resultat["OP2"] is Statut.MANQUANT


def test_chaque_paiement_garde_sa_facture_quand_il_y_en_a_autant():
    ops = [
        operation("OP1", "2026-04-02", "PRLV SEPA OVH", "83.99"),
        operation("OP2", "2026-04-10", "PRLV SEPA OVH", "83.99"),
    ]
    pieces = [piece("P1", "2026-03-30", "OVH SAS", "83.99"), piece("P2", "2026-04-08", "OVH SAS", "83.99")]
    resultat = statuts(ops, pieces)
    assert resultat == {"OP1": Statut.JUSTIFIE, "OP2": Statut.JUSTIFIE}


def test_un_paiement_mensuel_identique_sans_facture_au_milieu_n_est_pas_ambigu():
    ops = [
        operation("JAN", "2026-01-05", "PRLV SEPA ORANGE FRANCE", "100.00"),
        operation("FEV", "2026-02-05", "PRLV SEPA ORANGE FRANCE", "100.00"),
        operation("MAR", "2026-03-05", "PRLV SEPA ORANGE FRANCE", "100.00"),
    ]
    pieces = [piece("PJ", "2026-01-03", "Orange France", "100.00"), piece("PM", "2026-03-03", "Orange France", "100.00")]
    resultat = statuts(ops, pieces)
    assert resultat == {"JAN": Statut.JUSTIFIE, "FEV": Statut.MANQUANT, "MAR": Statut.JUSTIFIE}


# ------------------------------------------------------------------------------
# Reglement groupe : les trois exigences du cahier (section 6, regle 5).
# ------------------------------------------------------------------------------
def _groupe(libelle, total="1633.62", pieces_montants=("848.13", "438.16", "347.33")):
    pieces = [piece(f"G{i}", f"2026-03-{22 - 4 * i:02d}", "Point P Materiaux", m) for i, m in enumerate(pieces_montants)]
    return rapprochements([operation("OPGRP", "2026-04-03", libelle, total)], pieces)["OPGRP"]


def test_un_reglement_groupe_exige_un_indice_dans_le_libelle_meme_a_trois_factures():
    assert _groupe("VIR SEPA POINT P MATERIAUX REGLT GROUPE 03/04").statut is not Statut.MANQUANT
    assert _groupe("VIR SEPA POINT P MATERIAUX 03/04").statut is Statut.MANQUANT, (
        "trois factures sans indice de groupement : un total fortuit pourrait masquer une piece manquante"
    )


def test_un_reglement_groupe_exige_un_total_exact_au_centime():
    libelle = "VIR SEPA POINT P MATERIAUX REGLT GROUPE 03/04"
    assert _groupe(libelle, total="1633.62").statut is not Statut.MANQUANT
    assert _groupe(libelle, total="1633.63").statut is Statut.MANQUANT, "un centime d'ecart n'est pas un total exact"
    assert _groupe(libelle, total="1633.61").statut is Statut.MANQUANT


# ------------------------------------------------------------------------------
# Les graines qui ont deja fait echouer le moteur. CA-02 = zero manque rate.
# ------------------------------------------------------------------------------
GRAINES_A_PROBLEME = [
    (4, 0.18), (12, 0.18), (18, 0.18), (19, 0.18), (24, 0.18), (31, 0.18),
    (35, 0.18), (60, 0.18), (1153, 0.18), (1167, 0.18), (7071, 0.40), (60021, 0.40),
]


@pytest.mark.parametrize("graine,taux", GRAINES_A_PROBLEME)
def test_aucun_manque_rate_sur_les_graines_qui_ont_deja_fait_echouer_le_moteur(tmp_path, graine, taux):
    subprocess.run(
        [sys.executable, str(RACINE / "scripts" / "generer_corpus.py"), "--sortie", str(tmp_path),
         "--graine", str(graine), "--taux-manquant", str(taux)],
        check=True, capture_output=True,
    )
    sortie = subprocess.run(
        [sys.executable, str(RACINE / "scripts" / "evaluer.py"), "--entree", str(tmp_path), "--detail"],
        capture_output=True, text=True,
    )
    # CA-02 : zero manque rate, SANS tolerance. CA-01 : fausses alertes < 2 %. Une fausse
    # alerte isolee est le prix d'une ambiguite non tranchee (cahier, section 6, regle 7).
    assert re.search(r"Manques rates\s*:\s*0/", sortie.stdout), sortie.stdout
    fausses = re.search(r"Fausses alertes\s*:\s*(\d+)/(\d+)", sortie.stdout)
    assert fausses, sortie.stdout
    assert int(fausses.group(1)) / int(fausses.group(2)) < 0.02, sortie.stdout
