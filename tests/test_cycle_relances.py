"""Tests du cycle complet (chargement des referentiels) et des relances.

Deux defauts corriges ici :
  - les alias fournisseurs et les exclusions apprises n'etaient charges par
    aucun script : les fichiers existaient mais etaient ignores ;
  - les relances, premier contact du cabinet avec son client, etaient ecrites
    sans accents ("comptabilite", "periode") et sans elision ("de Atelier 9").
"""

from __future__ import annotations

import datetime as dt
import shutil
import sys
from decimal import Decimal
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement.cycle import executer
from rapprochement.evaluation import evaluer, lire_verite
from rapprochement.exclusions import Referentiel
from rapprochement.modeles import Dossier, OperationBancaire, Rapprochement, Sens, Statut
from rapprochement.parseurs import ErreurFormat, lire_alias
from rapprochement.relances import construire_relance, de

CORPUS = RACINE / "data" / "corpus"


def manquant(libelle: str, montant: str, jour: int = 10) -> Rapprochement:
    return Rapprochement(
        operation=OperationBancaire(
            reference=f"OP-{jour}",
            dossier="D1",
            date_operation=dt.date(2026, 4, jour),
            libelle=libelle,
            montant=Decimal(montant),
            sens=Sens.DEBIT,
        ),
        statut=Statut.MANQUANT,
    )


def dossier(raison: str = "Atelier 9 SASU", ton: str = "courtois", jour_tva: int = 15) -> Dossier:
    return Dossier(
        code="D1",
        raison_sociale=raison,
        email_contact="contact@exemple.fr",
        nom_contact="",
        jour_echeance_tva=jour_tva,
        ton_relance=ton,
    )


# --- cycle : les referentiels du dossier d'entree sont charges -----------

def test_le_cycle_charge_alias_et_exclusions_du_dossier_d_entree():
    cycle = executer(CORPUS)
    assert cycle.alias, "alias_fournisseurs.csv doit etre lu"
    assert cycle.referentiel.apprises, "exclusions_dossier.csv doit etre lu"
    regles = {r.regle for r in cycle.rapprochements}
    assert "apprise:MARTIN-BTP" in regles


def test_sans_exclusions_apprises_les_operations_propres_au_dossier_sont_relancees(tmp_path):
    """Preuve que le second niveau du referentiel sert : sans lui, le syndic
    et la cotisation ordinale deviennent deux fausses alertes."""
    for nom in ("releve_bancaire.csv", "pieces_recues.csv", "dossiers.csv", "alias_fournisseurs.csv"):
        shutil.copy(CORPUS / nom, tmp_path / nom)
    avec = executer(CORPUS)
    sans = executer(tmp_path)
    compte = lambda c: sum(r.statut is Statut.MANQUANT for r in c.rapprochements)  # noqa: E731
    assert compte(sans) == compte(avec) + 2


def test_le_corpus_de_demonstration_atteint_les_cibles():
    cycle = executer(CORPUS)
    ev = evaluer(cycle.rapprochements, lire_verite(CORPUS / "verite_terrain.csv"))
    assert ev.cibles_atteintes
    assert ev.fausses_alertes == 0 and ev.manques_rates == 0


def test_fichier_obligatoire_absent_donne_une_erreur_lisible(tmp_path):
    with pytest.raises(ErreurFormat, match="releve_bancaire.csv"):
        executer(tmp_path)


def test_lire_alias(tmp_path):
    chemin = tmp_path / "alias_fournisseurs.csv"
    chemin.write_text("﻿alias,fournisseur\namzn mktplace,Amazon EU Sarl\n,\n", encoding="utf-8")
    assert lire_alias(chemin) == {"amzn mktplace": "Amazon EU Sarl"}
    assert lire_alias(tmp_path / "absent.csv") == {}


# --- referentiel : apprendre et oublier une regle -------------------------

def test_regle_apprise_ajoutee_puis_retiree(tmp_path):
    ref = Referentiel()
    ref.ajouter("D1", "syndic copropriete", "Appel de fonds du syndic")
    ref.ajouter("D1", "syndic copropriete", "doublon ignore")
    assert ref.apprises == {"D1": [("syndic copropriete", "Appel de fonds du syndic")]}

    chemin = ref.ecrire_csv(tmp_path / "exclusions_dossier.csv")
    relu = Referentiel.depuis_csv(chemin)
    assert relu.apprises == ref.apprises

    assert relu.retirer("D1", "syndic copropriete")
    assert relu.apprises == {}
    assert not relu.retirer("D1", "syndic copropriete")


def test_expression_invalide_refusee(tmp_path):
    with pytest.raises(Exception):
        Referentiel().ajouter("D1", "syndic (", "")
    chemin = tmp_path / "exclusions_dossier.csv"
    chemin.write_text("dossier,motif_regex,motif\nD1,syndic (,x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ligne 2"):
        Referentiel.depuis_csv(chemin)


# --- relances : un francais correct ---------------------------------------

@pytest.mark.parametrize(
    "nom,attendu",
    [
        ("Atelier 9 SASU", "d'Atelier 9 SASU"),
        ("Électricité Dubois", "d'Électricité Dubois"),
        ("Lopez Conseil SAS", "de Lopez Conseil SAS"),
        ("Martin Bâtiment SARL", "de Martin Bâtiment SARL"),
    ],
)
def test_elision(nom, attendu):
    assert de(nom) == attendu


def test_relance_accentuee_et_elidee():
    relance = construire_relance(
        dossier(),
        [manquant("PRLV SEPA OVH", "53.31", 10), manquant("CARTE BUREAU VALLEE", "1238.97", 12)],
        aujourdhui=dt.date(2026, 5, 1),
    )
    assert "la comptabilité d'Atelier 9 SASU" in relance.corps
    assert "2 justificatifs pour clôturer la période" in relance.corps
    assert "1 238,97 EUR" in relance.corps
    assert "une photo lisible est acceptée" in relance.corps
    assert relance.objet == "2 justificatifs manquants – Atelier 9 SASU"
    for faute in ("comptabilite", "periode", "acceptee", "des que", "(s)"):
        assert faute not in relance.corps + relance.objet


def test_relance_au_singulier():
    relance = construire_relance(
        dossier(ton="direct"), [manquant("PRLV SEPA OVH", "53.31")],
        aujourdhui=dt.date(2026, 5, 1),
    )
    assert "il nous manque 1 justificatif pour" in relance.corps
    assert "d'un paiement visible" in relance.corps
    assert "Merci de nous le transmettre dès que possible." in relance.corps
    assert relance.objet == "1 justificatif manquant – Atelier 9 SASU"


def test_relance_urgente_a_l_approche_de_l_echeance_tva():
    relance = construire_relance(
        dossier(jour_tva=15), [manquant("PRLV SEPA OVH", "53.31")],
        aujourdhui=dt.date(2026, 5, 12),
    )
    assert relance.ton == "urgent"
    assert relance.echeance == dt.date(2026, 5, 15)
    assert "avant le 15/05/2026" in relance.corps


def test_seconde_relance():
    relance = construire_relance(
        dossier(), [manquant("PRLV SEPA OVH", "53.31"), manquant("CARTE X", "10.00", 11)],
        niveau=2, date_demande=dt.date(2026, 4, 20), aujourdhui=dt.date(2026, 5, 1),
    )
    assert relance.objet.startswith("Relance : 2 justificatifs manquants")
    assert "les 2 justificatifs ci-dessous, demandés le 20/04/2026" in relance.corps
