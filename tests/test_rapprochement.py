"""Tests de non-regression.

Chaque test correspond a un defaut reel trouve par l'evaluation contre la verite
terrain. Ils sont ecrits pour echouer si la correction est perdue.
"""

from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rapprochement.exclusions import Referentiel
from rapprochement.modeles import OperationBancaire, Piece, Sens, Statut
from rapprochement.moteur import Moteur
from rapprochement.normalisation import jeton_fort_commun, normaliser
from rapprochement.parseurs import ErreurFormat, lire_montant

DOSSIER = "TEST-1"


def op(libelle: str, montant: str, jour: int = 15, sens: Sens = Sens.DEBIT) -> OperationBancaire:
    return OperationBancaire(
        reference=f"OP{jour:04d}-{libelle[:6]}",
        dossier=DOSSIER,
        date_operation=dt.date(2026, 4, jour),
        libelle=libelle,
        montant=Decimal(montant),
        sens=sens,
    )


def piece(id_piece: str, fournisseur: str, montant: str, jour: int = 5) -> Piece:
    return Piece(
        id_piece=id_piece,
        dossier=DOSSIER,
        fichier=f"{id_piece}.pdf",
        fournisseur=fournisseur,
        date_facture=dt.date(2026, 4, jour),
        montant_ttc=Decimal(montant),
    )


def statut(operation: OperationBancaire, pieces: list[Piece], **kw) -> Statut:
    return Moteur(**kw).rapprocher([operation], pieces)[0].statut


# --- normalisation --------------------------------------------------------

@pytest.mark.parametrize(
    "brut,attendu",
    [
        ("PRLV SEPA ORANGE FRANCE ID447102", "orange france"),
        ("CARTE 12/03 AMZN MKTPLACE FR", "amzn mktplace"),
        ("VIR SEPA POINT P MATERIAUX 03/04", "point p materiaux"),
    ],
)
def test_normalisation_retire_le_bruit_bancaire(brut, attendu):
    assert normaliser(brut) == attendu


def test_normalisation_peut_conserver_le_prefixe():
    """Sans le prefixe, "VIREMENT INTERNE" perd le mot qui le qualifie."""
    assert "virement" not in normaliser("VIREMENT INTERNE VERS COMPTE EPARGNE")
    assert "virement" in normaliser(
        "VIREMENT INTERNE VERS COMPTE EPARGNE", retirer_prefixes=False
    )


def test_jeton_fort_identifie_un_fournisseur_multi_mots():
    assert jeton_fort_commun("VIR SEPA LOXAM ACOMPTE 30%", "Loxam Location")
    assert not jeton_fort_commun("PRLV SEPA EDF", "Loxam Location")


# --- exclusions -----------------------------------------------------------

@pytest.mark.parametrize(
    "libelle,regle",
    [
        ("VIR SALAIRE MARS DUPONT J", "salaires"),
        ("PRLV SEPA URSSAF IDF ID884412", "charges_sociales"),
        ("PRLV SEPA DGFIP TVA 3310 ID551204", "impots_taxes"),
        ("FRAIS DE TENUE DE COMPTE", "frais_bancaires"),
        ("ECHEANCE PRET N 4471029 CAPITAL+INT", "emprunts"),
        # Ces deux libelles ont pour mot porteur le prefixe de canal lui-meme :
        # ils ne sont reconnus que si la forme non elaguee est testee.
        ("VIREMENT INTERNE VERS COMPTE EPARGNE", "mouvements_internes"),
        ("VIR COMPTE COURANT D ASSOCIE", "mouvements_internes"),
    ],
)
def test_operations_sans_piece_justificative(libelle, regle):
    exclue, _, nom = Referentiel().exclure(op(libelle, "1000"))
    assert exclue, f"{libelle} devrait etre hors perimetre"
    assert nom == regle


def test_encaissement_client_exclu_en_credit():
    exclue, _, nom = Referentiel().exclure(
        op("VIR RECU CLIENT 9097", "5000", sens=Sens.CREDIT)
    )
    assert exclue and nom == "encaissements_clients"


def test_credit_bail_exige_une_facture_malgre_le_libelle_d_echeance():
    """Contre-exemple : le credit-bail emet une facture, l'emprunt non."""
    exclue, _, _ = Referentiel().exclure(
        op("PRLV SEPA SG CREDIT BAIL ECHEANCE 04 ID884412", "612.44")
    )
    assert not exclue


def test_exclusion_apprise_par_dossier(tmp_path):
    chemin = tmp_path / "exclusions_dossier.csv"
    chemin.write_text(
        "dossier,motif_regex,motif\n"
        f"{DOSSIER},syndic copropriete,Appel de fonds du syndic\n",
        encoding="utf-8",
    )
    referentiel = Referentiel.depuis_csv(chemin)
    exclue, motif, nom = referentiel.exclure(op("PRLV SYNDIC COPROPRIETE T2", "480"))
    assert exclue and motif == "Appel de fonds du syndic"
    assert nom == f"apprise:{DOSSIER}"


# --- appariement ----------------------------------------------------------

def test_appariement_simple():
    assert statut(
        op("PRLV SEPA OVH ID551204", "83.99"), [piece("P1", "OVH SAS", "83.99")]
    ) is Statut.JUSTIFIE


def test_ecart_d_arrondi_accepte():
    assert statut(
        op("PRLV SEPA OVH ID551204", "84.00"), [piece("P1", "OVH SAS", "83.99")]
    ) is Statut.JUSTIFIE


def test_aucune_piece_donne_manquant():
    assert statut(op("CARTE 12/04 LEROY MERLIN", "240.00"), []) is Statut.MANQUANT


def test_facture_posterieure_au_paiement_hors_fenetre():
    """Une facture datee 40 jours APRES le paiement ne le justifie pas."""
    tardive = piece("P1", "OVH SAS", "83.99", jour=25)
    assert statut(op("PRLV SEPA OVH", "83.99", jour=1), [tardive]) is Statut.MANQUANT


def test_affectation_globale_prefere_le_meilleur_couple():
    """Deux operations, deux pieces : la piece va a l'operation la plus proche.

    Traite chronologiquement, le premier paiement consommerait la piece du
    second (les montants sont compatibles a 0,5 %).
    """
    pieces = [
        piece("P-EXACT", "OVH SAS", "100.00", jour=2),
        piece("P-AUTRE", "OVH SAS", "100.40", jour=2),
    ]
    resultats = Moteur().rapprocher(
        [op("PRLV SEPA OVH", "100.40", jour=10), op("PRLV SEPA OVH", "100.00", jour=12)],
        pieces,
    )
    rattachees = {r.operation.montant: r.pieces[0].id_piece for r in resultats}
    assert rattachees[Decimal("100.00")] == "P-EXACT"
    assert rattachees[Decimal("100.40")] == "P-AUTRE"


# --- paiements groupes ----------------------------------------------------

def test_paiement_groupe_avec_indice_dans_le_libelle():
    pieces = [
        piece("P1", "Point P Materiaux", "300.00", jour=2),
        piece("P2", "Point P Materiaux", "450.00", jour=4),
    ]
    resultat = Moteur().rapprocher(
        [op("VIR SEPA POINT P MATERIAUX REGLT GROUPE", "750.00")], pieces
    )[0]
    assert resultat.statut in (Statut.JUSTIFIE, Statut.A_VERIFIER)
    assert len(resultat.pieces) == 2


def test_deux_factures_qui_totalisent_par_hasard_ne_sont_pas_groupees():
    """Sans indice de groupement, un total fortuit ne doit pas etre retenu :
    sinon la piece reellement manquante n'est plus relancee."""
    pieces = [
        piece("P1", "Assurance AXA Pro", "300.00", jour=2),
        piece("P2", "Assurance AXA Pro", "450.00", jour=4),
    ]
    assert statut(op("PRLV SEPA AXA FRANCE IARD ID632208", "750.00"), pieces) is Statut.MANQUANT


def test_combinaison_ambigue_reste_manquante():
    """Plusieurs combinaisons possibles : l'ambiguite ne se tranche pas seule."""
    pieces = [
        piece("P1", "Point P Materiaux", "300.00", jour=2),
        piece("P2", "Point P Materiaux", "450.00", jour=3),
        piece("P3", "Point P Materiaux", "300.00", jour=4),
        piece("P4", "Point P Materiaux", "450.00", jour=5),
    ]
    assert statut(op("VIR POINT P REGLT GROUPE", "750.00"), pieces) is Statut.MANQUANT


# --- acomptes -------------------------------------------------------------

def test_acompte_reconnu_quand_le_libelle_l_annonce():
    resultat = Moteur().rapprocher(
        [op("VIR SEPA LOXAM ACOMPTE 30%", "1440.00")],
        [piece("P1", "Loxam Location", "4800.00", jour=8)],
    )[0]
    assert resultat.statut is Statut.PARTIEL
    assert resultat.reste_a_justifier < 0


def test_paiement_recurrent_inferieur_n_est_pas_un_acompte():
    """Sans indice, un paiement mensuel plus petit qu'une facture anterieure du
    meme fournisseur doit rester une piece manquante."""
    assert statut(
        op("PRLV SEPA ORANGE FRANCE ID150692", "62.00"),
        [piece("P1", "Orange France", "189.00", jour=2)],
    ) is Statut.MANQUANT


def test_une_piece_ne_sert_qu_une_fois():
    resultats = Moteur().rapprocher(
        [op("PRLV SEPA OVH", "83.99", jour=10), op("PRLV SEPA OVH", "83.99", jour=20)],
        [piece("P1", "OVH SAS", "83.99")],
    )
    statuts = sorted(r.statut.value for r in resultats)
    assert statuts == ["justifie", "manquant"]


# --- alias fournisseurs ---------------------------------------------------

def test_alias_resout_un_libelle_abrege():
    """Les libelles bancaires abregent : AMZN MKTPLACE pour Amazon."""
    operation = op("CARTE 09/04 AMZN MKTPLACE FR", "340.80")
    p = piece("P1", "Amazon EU Sarl", "340.80")
    sans = Moteur().rapprocher([operation], [p])[0]
    avec = Moteur(alias={"amzn mktplace": "Amazon EU Sarl"}).rapprocher([operation], [p])[0]
    assert avec.confiance > sans.confiance
    assert avec.statut is Statut.JUSTIFIE


# --- parseurs -------------------------------------------------------------

@pytest.mark.parametrize(
    "brut,attendu",
    [
        ("1 234,56", "1234.56"),
        ("1.234,56", "1234.56"),
        ("1234.56", "1234.56"),
        ("(1 234,56)", "-1234.56"),
        ("340,80 EUR", "340.80"),
        ("", "0"),
    ],
)
def test_lecture_des_montants(brut, attendu):
    assert lire_montant(brut) == Decimal(attendu)


def test_montant_illisible_leve_une_erreur_explicite():
    with pytest.raises(ErreurFormat):
        lire_montant("douze euros")
