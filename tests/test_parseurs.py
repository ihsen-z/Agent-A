"""Lecture du releve : la reference de repli doit etre stable.

Defaut trouve a l'integration : une reference vide devenait `L<numero de ligne>`.
Le numero change d'un export a l'autre, donc deux operations de mois differents
se retrouvaient avec la meme reference et la seconde n'etait jamais reclamee.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rapprochement.parseurs import lire_releve  # noqa: E402

ENTETE = "dossier,date_operation,libelle,debit,credit,reference\n"


def _releve(tmp_path, lignes, nom="releve.csv"):
    chemin = tmp_path / nom
    chemin.write_text(ENTETE + "\n".join(lignes) + "\n", encoding="utf-8")
    return lire_releve(chemin)


def test_reference_explicite_conservee(tmp_path):
    ops = _releve(tmp_path, ["D1,2026-04-12,PRLV ORANGE,89.90,,OP42"])
    assert ops[0].reference == "OP42"


def test_reference_de_repli_ne_depend_pas_de_la_position(tmp_path):
    ligne = "D1,2026-04-12,PRLV ORANGE,89.90,,"
    autre = "D1,2026-04-15,CARTE BUREAU VALLEE,23.73,,"
    seule = _releve(tmp_path, [ligne], "a.csv")[0]
    apres = _releve(tmp_path, [autre, autre.replace("04-15", "04-16"), ligne], "b.csv")[2]
    assert seule.reference == apres.reference
    assert seule.reference.startswith("H")


def test_deux_mois_differents_ne_partagent_pas_de_reference(tmp_path):
    ops = _releve(
        tmp_path,
        ["D1,2026-03-12,PRLV ORANGE,89.90,,", "D1,2026-04-12,PRLV ORANGE,89.90,,"],
    )
    assert ops[0].reference != ops[1].reference


def test_deux_lignes_strictement_identiques_restent_distinctes(tmp_path):
    ligne = "D1,2026-04-12,CARTE BUREAU VALLEE,23.73,,"
    ops = _releve(tmp_path, [ligne, ligne])
    assert ops[0].reference != ops[1].reference


def test_rang_des_lignes_identiques_stable_si_une_autre_ligne_s_intercale(tmp_path):
    ligne = "D1,2026-04-12,CARTE BUREAU VALLEE,23.73,,"
    intrus = "D1,2026-04-13,PRLV OVH,53.31,,"
    nu = _releve(tmp_path, [ligne, ligne], "a.csv")
    melange = _releve(tmp_path, [intrus, ligne, intrus.replace("04-13", "04-14"), ligne], "b.csv")
    assert [nu[0].reference, nu[1].reference] == [melange[1].reference, melange[3].reference]


def test_dossiers_differents_ne_partagent_pas_de_reference(tmp_path):
    ops = _releve(
        tmp_path,
        ["D1,2026-04-12,PRLV ORANGE,89.90,,", "D2,2026-04-12,PRLV ORANGE,89.90,,"],
    )
    assert ops[0].reference != ops[1].reference
