"""Tests de l'interface graphique : espace de travail, API HTTP, et du defaut
de rapprochement qu'elle a revele (deux factures echangees)."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import threading
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement.evaluation import evaluer
from rapprochement.interface.serveur import Espace, ErreurUtilisateur, creer_serveur
from rapprochement.modeles import OperationBancaire, Piece, Sens
from rapprochement.moteur import Moteur
from rapprochement.parseurs import ErreurFormat

CORPUS = RACINE / "data" / "corpus"


@pytest.fixture
def espace(tmp_path):
    return Espace(tmp_path / "espace", corpus=CORPUS)


# --- moteur : un montant au centime l'emporte sur la tolerance -------------

def test_deux_factures_voisines_ne_s_echangent_pas():
    """Cas reel du corpus : OVH a 95,53 et 95,62. Les dates favorisaient le
    croisement, et chaque paiement etait justifie par la facture de l'autre."""
    def op(ref, jour, montant):
        return OperationBancaire(ref, "D", dt.date(2026, 2, 1) + dt.timedelta(days=jour),
                                 "PRLV SEPA OVH ID1", Decimal(montant), Sens.DEBIT)

    def piece(id_piece, jour, montant):
        return Piece(id_piece, "D", "", "OVH SAS",
                     dt.date(2026, 2, 1) + dt.timedelta(days=jour), Decimal(montant))

    resultats = Moteur().rapprocher(
        [op("A", 22, "95.53"), op("B", 38, "95.62")],
        [piece("P-9553", 3, "95.53"), piece("P-9562", 9, "95.62")],
    )
    rattachees = {r.operation.reference: r.pieces[0].id_piece for r in resultats}
    assert rattachees == {"A": "P-9553", "B": "P-9562"}


def test_l_evaluation_detecte_une_piece_mal_rattachee():
    r = Moteur().rapprocher(
        [OperationBancaire("A", "D", dt.date(2026, 2, 20), "PRLV SEPA OVH", Decimal("10.00"), Sens.DEBIT)],
        [Piece("P-1", "D", "", "OVH SAS", dt.date(2026, 2, 10), Decimal("10.00"))],
    )
    ev = evaluer(r, {"A": {"statut": "justifie", "id_piece_rattachee": "P-2"}})
    assert ev.cibles_atteintes            # l'action est juste...
    assert ev.pieces_erronees == 1        # ...mais pas la piece


# --- espace de travail ---------------------------------------------------------

def test_analyse_du_jeu_de_demonstration(espace):
    a = espace.analyse()
    assert a["source"] == "demo"
    assert a["stats"]["manquant"] == 23
    assert len(a["relances"]) == 3
    assert a["evaluation"]["cibles_atteintes"]
    assert a["evaluation"]["pieces_erronees"] == 0
    genres = {f["genre"] for f in a["faits_marquants"]}
    assert {"groupe", "acompte", "contre_exemple", "apprise"} <= genres
    manquante = next(o for o in a["operations"] if o["statut"] == "manquant")
    assert manquante["suggestion"]
    json.dumps(a)  # entierement serialisable


def test_apprendre_puis_oublier_une_regle_sans_toucher_au_corpus(espace):
    reference = (CORPUS / "exclusions_dossier.csv").read_bytes()

    espace.oublier("MARTIN-BTP", "syndic copropriete")
    assert espace.analyse()["stats"]["manquant"] == 24   # le syndic redevient une alerte

    assert espace.apprendre("MARTIN-BTP", "syndic copropriete", "Appel de fonds") == 1
    assert espace.analyse()["stats"]["manquant"] == 23

    assert (CORPUS / "exclusions_dossier.csv").read_bytes() == reference


def test_une_regle_trop_large_est_refusee(espace):
    with pytest.raises(ErreurUtilisateur, match="trop large"):
        espace.apprendre("MARTIN-BTP", "sepa", "")


@pytest.mark.parametrize("expression", ["", "syndic ("])
def test_une_regle_invalide_est_refusee(espace, expression):
    with pytest.raises(ErreurUtilisateur):
        espace.apprendre("MARTIN-BTP", expression, "")


def test_un_import_invalide_laisse_l_espace_intact(espace):
    contenus = {c: (CORPUS / n).read_text(encoding="utf-8") for c, n in (
        ("releve", "releve_bancaire.csv"), ("pieces", "pieces_recues.csv"), ("dossiers", "dossiers.csv"))}
    contenus["releve"] = "Date;Libellé;Débit;Crédit\n04/03/2026;PRLV SEPA ORANGE;44,31;\n"
    with pytest.raises(ErreurFormat, match="colonnes manquantes"):
        espace.importer(contenus)
    assert espace.analyse()["source"] == "demo"


def test_import_valide(espace):
    contenus = {c: (CORPUS / n).read_text(encoding="utf-8") for c, n in (
        ("releve", "releve_bancaire.csv"), ("pieces", "pieces_recues.csv"), ("dossiers", "dossiers.csv"))}
    espace.importer(contenus)
    a = espace.analyse()
    assert a["source"] == "import"
    assert a["evaluation"] is None                     # pas de verite terrain importee
    assert a["referentiel"]["apprises"] == []          # ni de regles apprises
    espace.charger_demo()
    assert espace.analyse()["source"] == "demo"


def test_import_sans_fichier_obligatoire(espace):
    with pytest.raises(ErreurUtilisateur, match="relevé bancaire"):
        espace.importer({"pieces": "x", "dossiers": "y"})


# --- serveur HTTP ---------------------------------------------------------------

@pytest.fixture
def serveur(tmp_path):
    srv = creer_serveur("127.0.0.1", 0, Espace(tmp_path / "espace", corpus=CORPUS), essais=1)
    fil = threading.Thread(target=srv.serve_forever, daemon=True)
    fil.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as rep:
        return rep.status, rep.headers.get("Content-Type"), rep.read()


def _post(url, donnees):
    requete = urllib.request.Request(
        url, data=json.dumps(donnees).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(requete, timeout=5) as rep:
            return rep.status, json.loads(rep.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_pages_et_api(serveur):
    statut, type_contenu, corps = _get(serveur + "/")
    assert statut == 200 and "text/html" in type_contenu and b"app.js" in corps
    assert _get(serveur + "/static/app.js")[0] == 200
    statut, _, corps = _get(serveur + "/api/analyse")
    assert json.loads(corps)["volumes"]["operations"] == 286
    statut, type_contenu, corps = _get(serveur + "/api/export/tableau_suivi.csv")
    assert "text/csv" in type_contenu and corps.startswith(b"\xef\xbb\xbf")   # BOM pour Excel
    statut, _, corps = _get(serveur + "/api/export/relance/MARTIN-BTP.txt")
    assert "Martin Bâtiment SARL".encode() in corps


def test_erreurs_lisibles_et_chemins_proteges(serveur):
    statut, rep = _post(serveur + "/api/regles/ajouter", {"dossier": "MARTIN-BTP", "motif_regex": "sepa"})
    assert statut == 400 and "trop large" in rep["erreur"]
    for chemin in ("/static/../serveur.py", "/static/%2e%2e/serveur.py", "/modeles/../../README.md"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(serveur + chemin)
        assert exc.value.code == 404


def test_apprendre_par_l_api(serveur):
    _post(serveur + "/api/regles/retirer", {"dossier": "MARTIN-BTP", "motif_regex": "cotisation ordre"})
    statut, rep = _post(serveur + "/api/regles/ajouter",
                        {"dossier": "MARTIN-BTP", "motif_regex": "cotisation ordre", "motif": "Ordinale"})
    assert statut == 200 and rep["reclassees"] == 1
    assert rep["analyse"]["stats"]["manquant"] == 23
