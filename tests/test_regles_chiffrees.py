"""Bornes des regles chiffrees du cahier des charges (section 6) et du referentiel (section 8).

L'audit de conformite a MUTE le code (tolerance, ordre des exclusions, contre-exemples) et
aucun test n'a rougi : ces regles pouvaient changer en silence. Chaque test ci-dessous tient
une borne a l'unite pres.
"""

from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rapprochement import exclusions  # noqa: E402
from rapprochement.exclusions import Referentiel  # noqa: E402
from rapprochement.modeles import OperationBancaire, Piece, Sens, Statut  # noqa: E402
from rapprochement.moteur import Moteur  # noqa: E402


def operation(libelle, montant="500.00", dossier="D1"):
    return OperationBancaire(
        reference="R1", dossier=dossier, date_operation=dt.date(2026, 4, 12),
        libelle=libelle, montant=Decimal(montant), sens=Sens.DEBIT,
    )


def statut(montant_operation, montant_piece):
    op = operation("PRLV SEPA ORANGE FRANCE ID1", montant_operation)
    facture = Piece("P1", "D1", "f.pdf", "Orange France", dt.date(2026, 4, 10), Decimal(montant_piece))
    return Moteur().rapprocher([op], [facture])[0].statut


# ------------------------------------------------------------------------------
# Tolerance d'arrondi : « 0,02 EUR ou 0,5 %, la plus grande des deux » (section 6, regle 3).
# ------------------------------------------------------------------------------
@pytest.mark.parametrize("operation_montant,attendu", [
    ("1000.00", Statut.JUSTIFIE),      # au centime
    ("1000.02", Statut.JUSTIFIE),      # 2 centimes : arrondi
    ("1005.00", Statut.A_VERIFIER),    # exactement 0,5 % : toleree, mais jamais une certitude
    ("1005.01", Statut.MANQUANT),      # un centime de plus : hors tolerance
    ("995.00", Statut.A_VERIFIER),     # symetrique, par en dessous
    ("994.99", Statut.MANQUANT),
])
def test_la_tolerance_relative_est_tenue_a_un_centime_pres(operation_montant, attendu):
    assert statut(operation_montant, "1000.00") is attendu


@pytest.mark.parametrize("operation_montant,attendu", [
    ("10.02", Statut.JUSTIFIE),        # tolerance absolue : 0,02 EUR
    ("10.05", Statut.A_VERIFIER),      # 0,5 % de 10 = 0,05 : la PLUS GRANDE des deux s'applique
    ("10.06", Statut.MANQUANT),
])
def test_sur_un_petit_montant_la_tolerance_absolue_et_relative_se_combinent(operation_montant, attendu):
    assert statut(operation_montant, "10.00") is attendu


# ------------------------------------------------------------------------------
# Ordre des exclusions (section 8 et regle 2 de la section 6) :
#   contre-exemple  >  regle apprise du dossier  >  regle generale.
# ------------------------------------------------------------------------------
def test_une_regle_apprise_du_dossier_passe_avant_la_regle_generale():
    ref = Referentiel({"D1": [("salaire", "Motif propre au cabinet", "perso")]})
    exclue, motif, nom = ref.exclure(operation("VIR SALAIRE MARS"))
    assert (exclue, nom) == (True, "apprise:D1:perso")
    assert motif == "Motif propre au cabinet"


def test_une_regle_apprise_ne_s_applique_qu_a_son_dossier():
    ref = Referentiel({"D1": [("salaire", "Motif propre au cabinet", "perso")]})
    assert ref.exclure(operation("VIR SALAIRE MARS", dossier="D2"))[2] == "salaires"


def test_un_contre_exemple_l_emporte_meme_sur_une_regle_apprise():
    """Une regle apprise trop large ne doit pas faire disparaitre un credit-bail,
    qui exige bien une facture chaque mois."""
    ref = Referentiel({"D1": [("credit bail", "Echeance", "")]})
    assert ref.exclure(operation("PRLV SEPA SG CREDIT BAIL ID1"))[0] is False


# ------------------------------------------------------------------------------
# Contre-exemple du credit-bail (section 8) : ressemble a une echeance d'emprunt.
# ------------------------------------------------------------------------------
def test_un_credit_bail_n_est_pas_une_echeance_d_emprunt():
    libelle = "PRLV SEPA SG ECHEANCE CREDIT BAIL VEHICULE"
    assert Referentiel().exclure(operation(libelle))[0] is False


def test_sans_le_contre_exemple_ce_meme_libelle_serait_exclu_a_tort(monkeypatch):
    """Preuve de pertinence du test precedent : si le contre-exemple disparait, le credit-bail
    est classe comme une echeance d'emprunt et plus jamais reclame."""
    monkeypatch.setattr(exclusions, "CONTRE_EXEMPLES", ())
    exclue, _, nom = Referentiel().exclure(operation("PRLV SEPA SG ECHEANCE CREDIT BAIL VEHICULE"))
    assert exclue is True and nom == "emprunts"


def test_une_vraie_echeance_d_emprunt_reste_exclue():
    exclue, _, nom = Referentiel().exclure(operation("PRLV SEPA SG ECHEANCE PRET 123456"))
    assert (exclue, nom) == (True, "emprunts")
