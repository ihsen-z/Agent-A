"""Tests de la persistance SQLite (contrat 4.2)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import json
import sqlite3
import sys
import threading
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rapprochement.depot import (  # noqa: E402
    SCHEMA_VERSION,
    TRANSITIONS_BROUILLON,
    Depot,
    ErreurBase,
    VersionSchemaInconnue,
)
from rapprochement.modeles import (  # noqa: E402
    Brouillon,
    DecisionRoutage,
    EnvoiRelance,
    EtatPiece,
    MotifNonRoute,
    PieceAttendue,
    StatutBrouillon,
    StatutRoutage,
)

S = StatutBrouillon
UTC = dt.timezone.utc


def piece(ref: str = "OP1", dossier: str = "D1", **kw) -> PieceAttendue:
    base = dict(
        reference=ref, dossier=dossier, periode="2026-04", montant=Decimal("89.90"),
        date_operation=dt.date(2026, 4, 15), libelle="PRLV ACME",
    )
    base.update(kw)
    return PieceAttendue(**base)


def brouillon(id_relance: str = "r1", **kw) -> Brouillon:
    base = dict(
        id_relance=id_relance, destinataire="client@exemple.fr", objet="Pieces manquantes",
        corps="Bonjour,\nmerci de nous envoyer...", dossiers=("D1",), references=("OP1", "OP2"),
        niveau=1, cree_le=dt.date(2026, 4, 20),
    )
    base.update(kw)
    return Brouillon(**base)


def decision(message_id: str = "m1", empreinte: str = "e1", **kw) -> DecisionRoutage:
    base = dict(
        message_id=message_id, nom_fichier="facture.pdf", empreinte=empreinte,
        statut=StatutRoutage.NON_ROUTEE, motif=MotifNonRoute.DOSSIER_INCONNU, detail="?",
    )
    base.update(kw)
    return DecisionRoutage(**base)


def envoi(id_relance: str = "r1", dest: str = "client@exemple.fr",
          jour: dt.date = dt.date(2026, 4, 21), **kw) -> EnvoiRelance:
    base = dict(id_relance=id_relance, destinataire=dest, date_envoi=jour,
                dossiers=("D1",), references=("OP1",))
    base.update(kw)
    return EnvoiRelance(**base)


@pytest.fixture
def depot(tmp_path):
    d = Depot(tmp_path / "depot.db")
    yield d
    d.fermer()


@pytest.fixture
def chemin_db(tmp_path) -> Path:
    return tmp_path / "base" / "depot.db"


def tout_l_etat(d: Depot) -> dict:
    return {
        "pieces": d.charger_pieces(),
        "brouillons": d.lister_brouillons(),
        "file": d.file_humaine(resolue=None),
        "envois": d.historique_envois(),
        "vu": d.est_vu("k"),
    }


# --- (1) aller-retour exact -------------------------------------------------


@pytest.mark.parametrize(
    "montant",
    ["0.10", "12345678.99", "0.1", "0", "0.00", "100", "1E+2", "9999999999999999.9999999999",
     "0.0000000001", "-5.50", "1.10"],
)
def test_decimal_aller_retour_exact(depot, montant):
    depot.sauver_pieces([piece(montant=Decimal(montant))])
    relu = depot.charger_pieces()[0].montant
    assert isinstance(relu, Decimal)
    assert relu == Decimal(montant)
    assert str(relu) == str(Decimal(montant))  # meme representation : ni float, ni perte


def test_montant_stocke_en_texte_jamais_en_reel(depot, tmp_path):
    depot.sauver_pieces([piece(montant=Decimal("0.10"))])
    brut = sqlite3.connect(tmp_path / "depot.db")
    (type_sql, valeur), = brut.execute("SELECT typeof(montant), montant FROM pieces").fetchall()
    brut.close()
    assert (type_sql, valeur) == ("text", "0.10")


def test_float_refuse_comme_montant(depot):
    with pytest.raises(TypeError):
        depot.sauver_pieces([piece(montant=0.1)])  # type: ignore[arg-type]
    assert depot.charger_pieces() == []


def test_montant_non_fini_refuse(depot):
    with pytest.raises(ValueError):
        depot.sauver_pieces([piece(montant=Decimal("NaN"))])


def test_piece_tous_champs_aller_retour(depot):
    p = piece(
        etat=EtatPiece.DEMANDEE, nb_relances=2,
        date_premiere_demande=dt.date(2026, 4, 1), date_derniere_relance=dt.date(2026, 4, 9),
        date_promesse=dt.date(2026, 4, 20), bloquee=True, motif_blocage="litige été",
        pieces_rattachees=("P1", "P2"),
    )
    depot.sauver_pieces([p])
    assert depot.charger_pieces() == [p]


def test_devise_aller_retour(depot):
    depot.sauver_pieces([piece("U", montant=Decimal("10.00"), devise="USD"), piece("E")])
    par_ref = {p.reference: p for p in depot.charger_pieces()}
    assert par_ref["U"].devise == "USD" and par_ref["E"].devise == "EUR"
    depot.sauver_pieces([piece("U", montant=Decimal("10.00"), devise="GBP")])  # upsert
    assert {p.reference: p.devise for p in depot.charger_pieces()}["U"] == "GBP"


def test_devise_vide_refusee(depot):
    with pytest.raises(ValueError):
        depot.sauver_pieces([piece(devise="")])


def test_devise_survit_a_l_export_et_a_l_import(depot, tmp_path):
    depot.sauver_pieces([piece("U", devise="USD")])
    export = depot.exporter_json(tmp_path / "e.json")
    assert json.loads(export.read_text())["donnees"]["pieces"][0]["devise"] == "USD"
    neuf = Depot(tmp_path / "n.db")
    neuf.importer_json(export)
    assert neuf.charger_pieces()[0].devise == "USD"
    neuf.fermer()


def test_piece_champs_none_et_defauts(depot):
    p = piece()
    depot.sauver_pieces([p])
    relue = depot.charger_pieces()[0]
    assert relue == p
    assert relue.date_premiere_demande is None and relue.date_promesse is None
    assert relue.pieces_rattachees == () and relue.bloquee is False


@pytest.mark.parametrize("etat", list(EtatPiece))
def test_piece_chaque_etat(depot, etat):
    p = piece(etat=etat)
    depot.sauver_pieces([p])
    assert depot.charger_pieces() == [p]


def test_datetime_refuse_a_la_place_d_une_date(depot):
    with pytest.raises(TypeError):
        depot.sauver_pieces([piece(date_operation=dt.datetime(2026, 4, 15, 10))])  # type: ignore[arg-type]


def test_brouillon_tous_champs_aller_retour(depot):
    b = brouillon(
        statut=S.ENVOYEE, valide_par="Marie Dupont",
        valide_le=dt.datetime(2026, 4, 21, 9, 15, 30, 123456, tzinfo=UTC),
        envoye_le=dt.datetime(2026, 4, 21, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=2))),
        niveau=3, dossiers=("D1", "D2"), references=("A", "B", "C"),
    )
    assert depot.sauver_brouillon(b)
    relu = depot.charger_brouillon("r1")
    assert relu == b
    assert relu.valide_le.utcoffset() == dt.timedelta(0)
    assert relu.envoye_le.utcoffset() == dt.timedelta(hours=2)


def test_brouillon_champs_none(depot):
    b = brouillon()
    depot.sauver_brouillon(b)
    relu = depot.charger_brouillon("r1")
    assert relu == b and relu.valide_le is None and relu.envoye_le is None
    assert relu.statut is S.BROUILLON and relu.valide_par == ""


def test_brouillon_tuples_vides(depot):
    b = brouillon(dossiers=(), references=())
    depot.sauver_brouillon(b)
    assert depot.charger_brouillon("r1") == b


def test_charger_brouillon_inconnu(depot):
    assert depot.charger_brouillon("absent") is None


def test_decision_tous_champs_aller_retour(depot):
    d = decision(
        statut=StatutRoutage.PROPOSEE, dossier="D1", periode="2026-04",
        reference_operation="OP9", motif=None, detail="montant 89,90 EUR",
    )
    depot.mettre_en_file(d)
    assert depot.file_humaine() == [d]


def test_decision_champs_none(depot):
    d = decision(empreinte="", nom_fichier="")
    depot.mettre_en_file(d)
    relue = depot.file_humaine()[0]
    assert relue == d and relue.dossier is None and relue.periode is None
    assert relue.reference_operation is None


@pytest.mark.parametrize("motif", list(MotifNonRoute))
def test_decision_chaque_motif(depot, motif):
    d = decision(motif=motif)
    depot.mettre_en_file(d)
    assert depot.file_humaine() == [d]


def test_envoi_aller_retour(depot):
    e = envoi(dossiers=("D1", "D2"), references=("A", "B"))
    depot.enregistrer_envoi(e)
    assert depot.historique_envois() == [e]


# --- pieces : upsert, filtres, tri -----------------------------------------


def test_upsert_remplace_sur_dossier_reference(depot):
    depot.sauver_pieces([piece()])
    depot.sauver_pieces([piece(etat=EtatPiece.DEMANDEE, nb_relances=1, libelle="MAJ")])
    pieces = depot.charger_pieces()
    assert len(pieces) == 1 and pieces[0].etat is EtatPiece.DEMANDEE and pieces[0].libelle == "MAJ"


def test_meme_reference_dossiers_differents_coexistent(depot):
    depot.sauver_pieces([piece("OP1", "D1"), piece("OP1", "D2")])
    assert len(depot.charger_pieces()) == 2
    assert [p.dossier for p in depot.charger_pieces(dossier="D2")] == ["D2"]


def test_tri_dossier_date_reference(depot):
    ps = [
        piece("b", "D2", date_operation=dt.date(2026, 4, 1)),
        piece("z", "D1", date_operation=dt.date(2026, 4, 9)),
        piece("a", "D1", date_operation=dt.date(2026, 4, 9)),
        piece("m", "D1", date_operation=dt.date(2026, 4, 2)),
    ]
    depot.sauver_pieces(ps)
    assert [(p.dossier, p.reference) for p in depot.charger_pieces()] == [
        ("D1", "m"), ("D1", "a"), ("D1", "z"), ("D2", "b"),
    ]


def test_filtres_dossier_periode_etats(depot):
    depot.sauver_pieces([
        piece("a", "D1", periode="2026-03"),
        piece("b", "D1", periode="2026-04", etat=EtatPiece.DEMANDEE),
        piece("c", "D2", periode="2026-04", etat=EtatPiece.RECUE),
    ])
    assert [p.reference for p in depot.charger_pieces(dossier="D1")] == ["a", "b"]
    assert [p.reference for p in depot.charger_pieces(periode="2026-04")] == ["b", "c"]
    assert [p.reference for p in depot.charger_pieces(etats={EtatPiece.RECUE})] == ["c"]
    assert [p.reference for p in depot.charger_pieces(
        dossier="D1", periode="2026-04", etats=[EtatPiece.DEMANDEE, EtatPiece.RECUE])] == ["b"]
    assert depot.charger_pieces(etats=[]) == []
    assert depot.charger_pieces(dossier="inconnu") == []


def test_sauver_pieces_est_atomique(depot):
    with pytest.raises(TypeError):
        depot.sauver_pieces([piece("ok"), piece("ko", montant=1.5)])  # type: ignore[arg-type]
    assert depot.charger_pieces() == []


def test_sauver_pieces_accepte_un_generateur(depot):
    depot.sauver_pieces(piece(f"R{i}") for i in range(3))
    assert len(depot.charger_pieces()) == 3


# --- (2) marquer_vu ---------------------------------------------------------


def test_marquer_vu_premiere_fois_puis_non(depot):
    assert depot.est_vu("k") is False
    assert depot.marquer_vu("k") is True
    assert depot.est_vu("k") is True
    assert depot.marquer_vu("k") is False
    assert depot.marquer_vu("k") is False
    assert depot.marquer_vu("autre") is True


@pytest.mark.parametrize("cle", ["", None, 3])
def test_marquer_vu_cle_invalide(depot, cle):
    with pytest.raises(ValueError):
        depot.marquer_vu(cle)  # type: ignore[arg-type]


def test_marquer_vu_persiste_apres_reouverture(chemin_db):
    d1 = Depot(chemin_db)
    assert d1.marquer_vu("k")
    d1.fermer()
    d2 = Depot(chemin_db)
    assert d2.est_vu("k") and d2.marquer_vu("k") is False
    d2.fermer()


@pytest.mark.parametrize("nb_threads", [2, 8])
def test_marquer_vu_concurrent_exactement_un_gagnant(chemin_db, nb_threads):
    Depot(chemin_db).fermer()  # cree le schema une fois
    for tour in range(10):
        cle = f"cle-{nb_threads}-{tour}"
        barriere = threading.Barrier(nb_threads)
        resultats: list[bool] = []
        erreurs: list[BaseException] = []

        def travail():
            try:
                d = Depot(chemin_db)  # une connexion par thread
                try:
                    barriere.wait(timeout=10)
                    resultats.append(d.marquer_vu(cle))
                finally:
                    d.fermer()
            except BaseException as exc:  # noqa: BLE001
                erreurs.append(exc)

        threads = [threading.Thread(target=travail) for _ in range(nb_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not erreurs
        assert sorted(resultats) == [False] * (nb_threads - 1) + [True]


def test_ouverture_concurrente_d_une_base_neuve(chemin_db):
    """Creation du schema par plusieurs connexions simultanees : aucune erreur."""
    barriere = threading.Barrier(6)
    erreurs: list[BaseException] = []

    def travail():
        try:
            barriere.wait(timeout=10)
            Depot(chemin_db).fermer()
        except BaseException as exc:  # noqa: BLE001
            erreurs.append(exc)

    threads = [threading.Thread(target=travail) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not erreurs


def test_depot_partage_entre_threads(depot):
    gagnants: list[bool] = []

    def travail():
        gagnants.append(depot.marquer_vu("partagee"))

    threads = [threading.Thread(target=travail) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert gagnants.count(True) == 1


def test_ecritures_concurrentes_ne_perdent_rien(chemin_db):
    Depot(chemin_db).fermer()
    erreurs: list[BaseException] = []

    def travail(k: int):
        try:
            d = Depot(chemin_db)
            for i in range(20):
                d.sauver_pieces([piece(f"T{k}-{i}", f"D{k}")])
                d.marquer_vu(f"{k}-{i}")
            d.fermer()
        except BaseException as exc:  # noqa: BLE001
            erreurs.append(exc)

    threads = [threading.Thread(target=travail, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not erreurs
    d = Depot(chemin_db)
    assert len(d.charger_pieces()) == 80
    d.fermer()


# --- (3) transactions -------------------------------------------------------


class Boum(Exception):
    pass


def test_transaction_rollback_sur_exception(depot):
    with pytest.raises(Boum):
        with depot.transaction():
            depot.sauver_pieces([piece()])
            assert depot.marquer_vu("k") is True
            depot.sauver_brouillon(brouillon())
            depot.mettre_en_file(decision())
            depot.enregistrer_envoi(envoi())
            raise Boum()
    assert tout_l_etat(depot) == {
        "pieces": [], "brouillons": [], "file": [], "envois": [], "vu": False,
    }
    assert depot.marquer_vu("k") is True  # la cle n'a pas ete consommee


def test_transaction_commit(depot):
    with depot.transaction():
        depot.sauver_pieces([piece()])
        depot.marquer_vu("k")
    assert len(depot.charger_pieces()) == 1 and depot.est_vu("k")


def test_transaction_rollback_sur_keyboardinterrupt(depot):
    with pytest.raises(KeyboardInterrupt):
        with depot.transaction():
            depot.sauver_pieces([piece()])
            raise KeyboardInterrupt()
    assert depot.charger_pieces() == []
    with depot.transaction():  # le depot reste utilisable
        depot.sauver_pieces([piece()])
    assert len(depot.charger_pieces()) == 1


def test_transaction_non_visible_avant_commit_pour_une_autre_connexion(chemin_db):
    d1, d2 = Depot(chemin_db), Depot(chemin_db)
    with d1.transaction():
        d1.sauver_pieces([piece()])
        assert d2.charger_pieces() == []  # lecture non bloquee (WAL), rien de visible
    assert len(d2.charger_pieces()) == 1
    d1.fermer()
    d2.fermer()


def test_transaction_imbriquee_rollback_interieur_seul(depot):
    with depot.transaction():
        depot.sauver_pieces([piece("externe")])
        with pytest.raises(Boum):
            with depot.transaction():
                depot.sauver_pieces([piece("interne")])
                raise Boum()
        depot.marquer_vu("k")
    assert [p.reference for p in depot.charger_pieces()] == ["externe"]
    assert depot.est_vu("k")


def test_transaction_imbriquee_exception_propagee_annule_tout(depot):
    with pytest.raises(Boum):
        with depot.transaction():
            depot.sauver_pieces([piece("externe")])
            with depot.transaction():
                depot.sauver_pieces([piece("interne")])
            raise Boum()
    assert depot.charger_pieces() == []


def test_methode_a_plusieurs_ecritures_est_atomique_dans_un_bloc(depot):
    with pytest.raises(Boum):
        with depot.transaction():
            depot.sauver_brouillon(brouillon())
            depot.maj_brouillon(dataclasses.replace(brouillon(), statut=S.VALIDEE, valide_par="m"))
            raise Boum()
    assert depot.charger_brouillon("r1") is None


def test_transaction_sur_memoire():
    d = Depot(":memory:")
    with pytest.raises(Boum):
        with d.transaction():
            d.sauver_pieces([piece()])
            raise Boum()
    assert d.charger_pieces() == []
    d.fermer()


def test_donnees_committees_survivent_a_la_reouverture(chemin_db):
    d = Depot(chemin_db)
    d.sauver_pieces([piece()])
    d.sauver_brouillon(brouillon())
    d.fermer()
    d = Depot(chemin_db)
    assert len(d.charger_pieces()) == 1 and d.charger_brouillon("r1") is not None
    d.fermer()


def test_transaction_abandonnee_sans_commit_n_est_pas_persistee(chemin_db):
    """Simule une panne : connexion SQLite brute coupee au milieu d'une transaction."""
    d = Depot(chemin_db)
    d.sauver_pieces([piece("avant")])
    d._conn.execute("BEGIN IMMEDIATE")
    d._conn.execute("DELETE FROM pieces")
    d._conn.close()  # coupure sans COMMIT
    d2 = Depot(chemin_db)
    assert [p.reference for p in d2.charger_pieces()] == ["avant"]
    d2.fermer()


def test_mode_wal_et_cles_etrangeres(depot, tmp_path):
    assert depot._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert depot._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert depot._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_memoire_acceptee():
    d = Depot(":memory:")
    d.sauver_pieces([piece()])
    assert len(d.charger_pieces()) == 1
    d.fermer()


def test_fermer_est_idempotent(tmp_path):
    d = Depot(tmp_path / "x.db")
    d.fermer()
    d.fermer()


# --- (4) sauver_brouillon n'ecrase jamais -----------------------------------


def test_sauver_brouillon_ne_remplace_jamais(depot):
    original = brouillon(corps="texte original")
    assert depot.sauver_brouillon(original) is True
    assert depot.sauver_brouillon(brouillon(corps="texte different", objet="autre")) is False
    assert depot.sauver_brouillon(original) is False
    assert depot.charger_brouillon("r1") == original
    assert len(depot.lister_brouillons()) == 1


def test_sauver_brouillon_n_ecrase_pas_un_envoye(depot):
    envoye = brouillon(statut=S.ENVOYEE, valide_par="m")
    depot.sauver_brouillon(envoye)
    assert depot.sauver_brouillon(brouillon()) is False
    assert depot.charger_brouillon("r1").statut is S.ENVOYEE


def test_sauver_brouillon_concurrent_un_seul_createur(chemin_db):
    Depot(chemin_db).fermer()
    barriere = threading.Barrier(6)
    resultats: list[bool] = []

    def travail(k: int):
        d = Depot(chemin_db)
        barriere.wait(timeout=10)
        resultats.append(d.sauver_brouillon(brouillon(corps=f"version {k}")))
        d.fermer()

    threads = [threading.Thread(target=travail, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert resultats.count(True) == 1


def test_lister_brouillons_filtre_et_tri(depot):
    depot.sauver_brouillon(brouillon("b", cree_le=dt.date(2026, 4, 2)))
    depot.sauver_brouillon(brouillon("a", cree_le=dt.date(2026, 4, 2)))
    depot.sauver_brouillon(brouillon("c", cree_le=dt.date(2026, 4, 1), statut=S.REJETEE))
    assert [b.id_relance for b in depot.lister_brouillons()] == ["c", "a", "b"]
    assert [b.id_relance for b in depot.lister_brouillons(S.BROUILLON)] == ["a", "b"]
    assert [b.id_relance for b in depot.lister_brouillons(S.REJETEE)] == ["c"]
    assert depot.lister_brouillons(S.ENVOYEE) == []


# --- (5) transitions de maj_brouillon ---------------------------------------

AUTORISEES = [
    (S.BROUILLON, S.VALIDEE), (S.BROUILLON, S.REJETEE),
    (S.VALIDEE, S.EN_COURS), (S.VALIDEE, S.REJETEE),
    (S.EN_COURS, S.ENVOYEE), (S.EN_COURS, S.REJETEE), (S.EN_COURS, S.VALIDEE),
]
TOUTES = list(itertools.product(S, S))
INTERDITES = [c for c in TOUTES if c not in AUTORISEES]
T_VALIDE = dt.datetime(2026, 4, 21, 9, 0, tzinfo=UTC)
T_ENVOI = dt.datetime(2026, 4, 21, 10, 0, tzinfo=UTC)


def depart(statut: S) -> Brouillon:
    """Brouillon stocke dans `statut`, avec les champs coherents pour ce statut."""
    if statut is S.BROUILLON:
        return brouillon()
    if statut is S.ENVOYEE:
        return brouillon(statut=statut, valide_par="marie", valide_le=T_VALIDE, envoye_le=T_ENVOI)
    if statut is S.REJETEE:
        return brouillon(statut=statut)
    return brouillon(statut=statut, valide_par="marie", valide_le=T_VALIDE)


def cible(statut: S) -> Brouillon:
    return dataclasses.replace(
        depart(statut), valide_par="marie", valide_le=T_VALIDE,
        envoye_le=T_ENVOI if statut is S.ENVOYEE else None,
    )


def test_le_produit_cartesien_est_complet():
    assert len(TOUTES) == 25 and len(AUTORISEES) == 7 and len(INTERDITES) == 18
    assert set(AUTORISEES) == set(TRANSITIONS_BROUILLON)


@pytest.mark.parametrize("de,vers", AUTORISEES, ids=lambda s: s.name)
def test_transition_autorisee(depot, de, vers):
    depot.sauver_brouillon(depart(de))
    depot.maj_brouillon(cible(vers))
    relu = depot.charger_brouillon("r1")
    assert relu.statut is vers
    assert relu == cible(vers)


@pytest.mark.parametrize("de,vers", INTERDITES, ids=lambda s: s.name)
def test_transition_interdite(depot, de, vers):
    avant = depart(de)
    depot.sauver_brouillon(avant)
    with pytest.raises(ValueError):
        depot.maj_brouillon(cible(vers))
    assert depot.charger_brouillon("r1") == avant  # rien n'a bouge


@pytest.mark.parametrize("vers", list(S), ids=lambda s: s.name)
def test_brouillon_envoye_immuable(depot, vers):
    envoye = depart(S.ENVOYEE)
    depot.sauver_brouillon(envoye)
    with pytest.raises(ValueError):
        depot.maj_brouillon(dataclasses.replace(envoye, statut=vers))
    assert depot.charger_brouillon("r1") == envoye


def test_envoye_immuable_meme_au_niveau_sql(depot):
    depot.sauver_brouillon(depart(S.ENVOYEE))
    with pytest.raises(sqlite3.DatabaseError):
        depot._conn.execute("UPDATE brouillons SET corps = 'pirate'")
    with pytest.raises(sqlite3.DatabaseError):
        depot._conn.execute("DELETE FROM brouillons")
    assert depot.charger_brouillon("r1").corps != "pirate"


def test_maj_brouillon_inconnu(depot):
    with pytest.raises(KeyError):
        depot.maj_brouillon(cible(S.VALIDEE))


def test_maj_ne_peut_pas_changer_le_texte_valide(depot):
    depot.sauver_brouillon(depart(S.VALIDEE))
    for champ, valeur in [
        ("corps", "autre texte"), ("objet", "autre"), ("destinataire", "pirate@x.fr"),
        ("dossiers", ("D9",)), ("references", ("Z",)), ("niveau", 9),
        ("cree_le", dt.date(2020, 1, 1)),
    ]:
        with pytest.raises(ValueError, match=champ):
            depot.maj_brouillon(dataclasses.replace(cible(S.EN_COURS), **{champ: valeur}))
    assert depot.charger_brouillon("r1") == depart(S.VALIDEE)


def test_maj_ne_peut_pas_changer_le_valideur(depot):
    depot.sauver_brouillon(depart(S.VALIDEE))
    with pytest.raises(ValueError):
        depot.maj_brouillon(dataclasses.replace(cible(S.EN_COURS), valide_par="autre"))
    with pytest.raises(ValueError):
        depot.maj_brouillon(
            dataclasses.replace(cible(S.EN_COURS), valide_le=T_VALIDE + dt.timedelta(hours=1))
        )


def test_passage_a_validee_exige_un_valideur(depot):
    depot.sauver_brouillon(brouillon())
    for vide in ("", "   "):
        with pytest.raises(ValueError):
            depot.maj_brouillon(dataclasses.replace(brouillon(), statut=S.VALIDEE, valide_par=vide))
    assert depot.charger_brouillon("r1").statut is S.BROUILLON


def test_cycle_complet_et_reprise_apres_echec_certain(depot):
    depot.sauver_brouillon(brouillon())
    b = brouillon()
    b = dataclasses.replace(b, statut=S.VALIDEE, valide_par="marie", valide_le=T_VALIDE)
    depot.maj_brouillon(b)
    b = dataclasses.replace(b, statut=S.EN_COURS)
    depot.maj_brouillon(b)
    b = dataclasses.replace(b, statut=S.VALIDEE)  # echec certain
    depot.maj_brouillon(b)
    b = dataclasses.replace(b, statut=S.EN_COURS)
    depot.maj_brouillon(b)
    b = dataclasses.replace(b, statut=S.ENVOYEE, envoye_le=T_ENVOI)
    depot.maj_brouillon(b)
    assert depot.charger_brouillon("r1") == b


def test_maj_concurrente_une_seule_transition_gagne(chemin_db):
    """Deux processus legers valident en meme temps : EN_COURS ne peut etre pris qu'une fois."""
    d0 = Depot(chemin_db)
    d0.sauver_brouillon(depart(S.VALIDEE))
    d0.fermer()
    barriere = threading.Barrier(6)
    ok: list[int] = []
    refus: list[int] = []

    def travail(k: int):
        d = Depot(chemin_db)
        barriere.wait(timeout=10)
        try:
            d.maj_brouillon(cible(S.EN_COURS))
            ok.append(k)
        except ValueError:
            refus.append(k)
        d.fermer()

    threads = [threading.Thread(target=travail, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 1 and len(refus) == 5


def test_deux_connexions_validee_vers_en_cours_une_seule_reussit(chemin_db):
    """Le second maj_brouillon relit le statut ACTUEL en base : il trouve EN_COURS."""
    d1, d2 = Depot(chemin_db), Depot(chemin_db)
    d1.sauver_brouillon(depart(S.VALIDEE))
    # d2 a charge le brouillon quand il etait VALIDEE ; d1 passe ensuite a EN_COURS.
    obsolete = d2.charger_brouillon("r1")
    assert obsolete.statut is S.VALIDEE
    d1.maj_brouillon(cible(S.EN_COURS))
    with pytest.raises(ValueError):
        d2.maj_brouillon(cible(S.EN_COURS))  # l'objet recu dit VALIDEE, la base dit EN_COURS
    assert d2.charger_brouillon("r1").statut is S.EN_COURS
    d1.fermer()
    d2.fermer()


def test_transaction_pose_le_verrou_d_ecriture_des_le_debut(chemin_db):
    """BEGIN IMMEDIATE : une seconde connexion ne peut pas ecrire pendant le bloc,
    meme si le premier n'a encore rien ecrit."""
    d1, d2 = Depot(chemin_db), Depot(chemin_db)
    d2._conn.execute("PRAGMA busy_timeout = 100")
    with d1.transaction():
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            d2.transaction().__enter__()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            d2.marquer_vu("k")
    assert d2.marquer_vu("k") is True  # verrou libere apres le bloc
    d1.fermer()
    d2.fermer()


def test_lire_puis_decider_dans_transaction_serialise_deux_connexions(chemin_db):
    """Schema de envoyer() : lire le statut puis passer EN_COURS dans une transaction.
    Le second arrive apres le COMMIT du premier et voit EN_COURS."""
    d0 = Depot(chemin_db)
    d0.sauver_brouillon(depart(S.VALIDEE))
    d0.fermer()
    depart_ensemble = threading.Barrier(2)
    issues: list[str] = []

    def travail():
        d = Depot(chemin_db)
        depart_ensemble.wait(timeout=10)
        try:
            with d.transaction():
                b = d.charger_brouillon("r1")
                if b.statut is not S.VALIDEE:
                    issues.append("deja")
                    return
                d.maj_brouillon(dataclasses.replace(b, statut=S.EN_COURS))
                issues.append("pris")
        finally:
            d.fermer()

    threads = [threading.Thread(target=travail) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(issues) == ["deja", "pris"]


# --- (6) file humaine -------------------------------------------------------


def test_mettre_en_file_idempotent(depot):
    d = decision()
    depot.mettre_en_file(d)
    depot.mettre_en_file(d)
    depot.mettre_en_file(dataclasses.replace(d, detail="autre detail"))  # meme cle
    assert depot.file_humaine() == [d]


def test_cle_de_file_est_message_et_empreinte(depot):
    depot.mettre_en_file(decision("m1", "e1"))
    depot.mettre_en_file(decision("m1", "e2"))
    depot.mettre_en_file(decision("m2", "e1"))
    assert [(x.message_id, x.empreinte) for x in depot.file_humaine()] == [
        ("m1", "e1"), ("m1", "e2"), ("m2", "e1"),
    ]


def test_resoudre_file_retire_de_la_file(depot):
    depot.mettre_en_file(decision("m1", "e1"))
    depot.mettre_en_file(decision("m2", "e2"))
    depot.resoudre_file("m1", "e1", par="marie", note="rattache a la main")
    assert [x.message_id for x in depot.file_humaine()] == ["m2"]
    assert [x.message_id for x in depot.file_humaine(resolue=False)] == ["m2"]
    assert [x.message_id for x in depot.file_humaine(resolue=True)] == ["m1"]
    assert [x.message_id for x in depot.file_humaine(resolue=None)] == ["m1", "m2"]


def test_resolue_n_est_pas_rouverte_par_une_remise_en_file(depot):
    depot.mettre_en_file(decision())
    depot.resoudre_file("m1", "e1", par="marie")
    depot.mettre_en_file(decision())
    assert depot.file_humaine() == []


def test_resoudre_deux_fois_conserve_la_premiere_resolution(depot, tmp_path):
    depot.mettre_en_file(decision())
    depot.resoudre_file("m1", "e1", par="marie", note="premiere", maintenant=T_VALIDE)
    depot.resoudre_file("m1", "e1", par="paul", note="seconde")
    brut = sqlite3.connect(tmp_path / "depot.db")
    ligne = brut.execute("SELECT resolue_par, resolue_note, resolue_le FROM file_humaine").fetchone()
    brut.close()
    assert ligne == ("marie", "premiere", T_VALIDE.isoformat())


def test_resoudre_inconnu_leve_keyerror(depot):
    with pytest.raises(KeyError):
        depot.resoudre_file("nope", "nope", par="marie")


@pytest.mark.parametrize("par", ["", "  "])
def test_resoudre_exige_un_acteur(depot, par):
    depot.mettre_en_file(decision())
    with pytest.raises(ValueError):
        depot.resoudre_file("m1", "e1", par=par)
    assert len(depot.file_humaine()) == 1


# --- (7) historique des envois ----------------------------------------------


def test_historique_filtre_destinataire_et_date(depot):
    depot.enregistrer_envoi(envoi("r1", "a@x.fr", dt.date(2026, 4, 1)))
    depot.enregistrer_envoi(envoi("r2", "b@x.fr", dt.date(2026, 4, 10)))
    depot.enregistrer_envoi(envoi("r3", "a@x.fr", dt.date(2026, 4, 20)))
    ids = lambda **kw: [e.id_relance for e in depot.historique_envois(**kw)]  # noqa: E731
    assert ids() == ["r1", "r2", "r3"]
    assert ids(destinataire="a@x.fr") == ["r1", "r3"]
    assert ids(destinataire="b@x.fr") == ["r2"]
    assert ids(destinataire="inconnu@x.fr") == []
    assert ids(depuis=dt.date(2026, 4, 10)) == ["r2", "r3"]  # borne incluse
    assert ids(depuis=dt.date(2026, 4, 11)) == ["r3"]
    assert ids(depuis=dt.date(2026, 5, 1)) == []
    assert ids(destinataire="a@x.fr", depuis=dt.date(2026, 4, 2)) == ["r3"]


def test_historique_destinataire_insensible_a_la_casse(depot):
    depot.enregistrer_envoi(envoi("r1", "Client@Exemple.FR"))
    assert len(depot.historique_envois(destinataire="  client@exemple.fr ")) == 1
    assert depot.historique_envois()[0].destinataire == "Client@Exemple.FR"  # relu tel quel


def test_historique_tri_par_date_puis_id(depot):
    depot.enregistrer_envoi(envoi("b", jour=dt.date(2026, 4, 2)))
    depot.enregistrer_envoi(envoi("c", jour=dt.date(2026, 4, 1)))
    depot.enregistrer_envoi(envoi("a", jour=dt.date(2026, 4, 2)))
    assert [e.id_relance for e in depot.historique_envois()] == ["c", "a", "b"]


def test_historique_refuse_un_datetime_pour_depuis(depot):
    with pytest.raises(TypeError):
        depot.historique_envois(depuis=dt.datetime(2026, 4, 1, 10))  # type: ignore[arg-type]


def test_enregistrer_envoi_idempotent(depot):
    e = envoi()
    depot.enregistrer_envoi(e)
    depot.enregistrer_envoi(e)
    depot.enregistrer_envoi(envoi(jour=dt.date(2030, 1, 1), dest="autre@x.fr"))  # meme id
    assert depot.historique_envois() == [e]


def test_envoi_enregistre_est_immuable_au_niveau_sql(depot):
    depot.enregistrer_envoi(envoi())
    with pytest.raises(sqlite3.DatabaseError):
        depot._conn.execute("DELETE FROM envois")
    with pytest.raises(sqlite3.DatabaseError):
        depot._conn.execute("UPDATE envois SET date_envoi = '2000-01-01'")


# --- (8) export -------------------------------------------------------------


def peupler(d: Depot) -> None:
    d.sauver_pieces([
        piece("OP1", "D1", montant=Decimal("0.10")),
        piece("OP2", "D1", montant=Decimal("12345678.99"), etat=EtatPiece.DEMANDEE,
              nb_relances=1, date_premiere_demande=dt.date(2026, 4, 1),
              date_derniere_relance=dt.date(2026, 4, 1), pieces_rattachees=("P1",),
              bloquee=True, motif_blocage="litige"),
        piece("OP1", "D2", date_promesse=dt.date(2026, 4, 30), etat=EtatPiece.PROMISE),
    ])
    d.marquer_vu("cle-b")
    d.marquer_vu("cle-a")
    d.mettre_en_file(decision("m1", "e1"))
    d.mettre_en_file(decision("m2", "e2", statut=StatutRoutage.PROPOSEE, motif=None,
                              dossier="D1", periode="2026-04", reference_operation="OP1"))
    d.resoudre_file("m1", "e1", par="marie", note="fait", maintenant=T_VALIDE)
    d.sauver_brouillon(depart(S.ENVOYEE))
    d.sauver_brouillon(brouillon("r2", destinataire="x'; DROP TABLE pieces; --"))
    d.enregistrer_envoi(envoi("r1"))
    d.enregistrer_envoi(envoi("r9", dest="z@x.fr", dossiers=("D1", "D2"), references=("A", "B")))


def test_export_fichier_relu_schema_present(depot, tmp_path):
    peupler(depot)
    sortie = depot.exporter_json(tmp_path / "out" / "export.json")
    assert sortie == tmp_path / "out" / "export.json" and sortie.exists()
    doc = json.loads(sortie.read_text(encoding="utf-8"))
    assert set(doc) == {"schema", "donnees"}
    assert doc["schema"]["version"] == SCHEMA_VERSION
    assert set(doc["schema"]["tables"]) == set(doc["donnees"]) == {
        "pieces", "vus", "file_humaine", "brouillons", "envois"}
    for table, colonnes in doc["schema"]["tables"].items():
        for ligne in doc["donnees"][table]:
            assert set(ligne) == set(colonnes), table


def test_export_decimal_en_chaine_et_rien_de_perdu(depot, tmp_path):
    peupler(depot)
    doc = json.loads(depot.exporter_json(tmp_path / "e.json").read_text(encoding="utf-8"))
    montants = {(p["dossier"], p["reference"]): p["montant"] for p in doc["donnees"]["pieces"]}
    assert montants[("D1", "OP1")] == "0.10"
    assert montants[("D1", "OP2")] == "12345678.99"
    d = doc["donnees"]
    assert len(d["pieces"]) == 3 and len(d["vus"]) == 2 and len(d["file_humaine"]) == 2
    assert len(d["brouillons"]) == 2 and len(d["envois"]) == 2
    assert [v["cle"] for v in d["vus"]] == ["cle-a", "cle-b"]
    resolue = next(x for x in d["file_humaine"] if x["message_id"] == "m1")
    assert resolue["resolue"] is True and resolue["resolue_par"] == "marie"
    assert any(b["destinataire"] == "x'; DROP TABLE pieces; --" for b in d["brouillons"])


def test_export_deux_fois_octets_identiques(depot, tmp_path):
    peupler(depot)
    a = depot.exporter_json(tmp_path / "a.json").read_bytes()
    b = depot.exporter_json(tmp_path / "b.json").read_bytes()
    assert a == b


def test_export_independant_de_l_ordre_d_insertion(tmp_path):
    d1, d2 = Depot(tmp_path / "1.db"), Depot(tmp_path / "2.db")
    ps = [piece(f"R{i}", f"D{i % 2}") for i in range(6)]
    d1.sauver_pieces(ps)
    d2.sauver_pieces(reversed(ps))
    d1.sauver_brouillon(brouillon("a"))
    d1.sauver_brouillon(brouillon("b"))
    d2.sauver_brouillon(brouillon("b"))
    d2.sauver_brouillon(brouillon("a"))
    a = json.loads(d1.exporter_json(tmp_path / "a.json").read_text())
    b = json.loads(d2.exporter_json(tmp_path / "b.json").read_text())
    assert a == b
    d1.fermer()
    d2.fermer()


def test_export_ecrase_atomiquement_sans_residu(depot, tmp_path):
    cible_ = tmp_path / "export.json"
    cible_.write_text("ancien")
    depot.exporter_json(cible_)
    assert json.loads(cible_.read_text())["schema"]
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".tmp") == []


def test_export_base_vide(depot, tmp_path):
    doc = json.loads(depot.exporter_json(tmp_path / "v.json").read_text())
    assert all(v == [] for v in doc["donnees"].values())


def test_export_puis_import_reproduit_tout(depot, tmp_path):
    peupler(depot)
    export = depot.exporter_json(tmp_path / "export.json")
    neuf = Depot(tmp_path / "neuf.db")
    neuf.importer_json(export)
    assert neuf.charger_pieces() == depot.charger_pieces()
    assert neuf.lister_brouillons() == depot.lister_brouillons()
    assert neuf.file_humaine(resolue=None) == depot.file_humaine(resolue=None)
    assert neuf.file_humaine() == depot.file_humaine()
    assert neuf.historique_envois() == depot.historique_envois()
    assert neuf.est_vu("cle-a") and neuf.est_vu("cle-b") and not neuf.est_vu("cle-c")
    assert neuf.exporter_json(tmp_path / "re.json").read_bytes() == export.read_bytes()
    neuf.fermer()


def test_import_refuse_un_depot_non_vide(depot, tmp_path):
    peupler(depot)
    export = depot.exporter_json(tmp_path / "export.json")
    with pytest.raises(ValueError):
        depot.importer_json(export)


def test_import_refuse_un_fichier_inconnu(depot, tmp_path):
    f = tmp_path / "x.json"
    f.write_text(json.dumps({"schema": {"format": "autre"}, "donnees": {}}))
    with pytest.raises(ValueError):
        depot.importer_json(f)


def test_import_invalide_n_importe_rien(depot, tmp_path):
    peupler(depot)
    doc = json.loads(depot.exporter_json(tmp_path / "e.json").read_text())
    doc["donnees"]["envois"][0]["date_envoi"] = "pas une date"
    f = tmp_path / "casse.json"
    f.write_text(json.dumps(doc))
    neuf = Depot(tmp_path / "neuf.db")
    with pytest.raises(ValueError):
        neuf.importer_json(f)
    assert neuf.charger_pieces() == [] and neuf.lister_brouillons() == []
    neuf.fermer()


# --- (9) version de schema --------------------------------------------------


def test_base_de_version_future_refusee_clairement(chemin_db):
    Depot(chemin_db).fermer()
    brut = sqlite3.connect(chemin_db)
    brut.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    brut.commit()
    brut.close()
    with pytest.raises(VersionSchemaInconnue, match=str(SCHEMA_VERSION + 1)):
        Depot(chemin_db)
    brut = sqlite3.connect(chemin_db)
    assert brut.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1  # intacte
    brut.close()


def test_version_future_n_ecrit_rien_dans_la_base(chemin_db):
    chemin_db.parent.mkdir(parents=True)
    brut = sqlite3.connect(chemin_db)
    brut.execute("CREATE TABLE autre (x)")
    brut.execute("PRAGMA user_version = 99")
    brut.commit()
    brut.close()
    with pytest.raises(VersionSchemaInconnue):
        Depot(chemin_db)
    brut = sqlite3.connect(chemin_db)
    tables = [r[0] for r in brut.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    brut.close()
    assert tables == ["autre"]


def test_base_etrangere_non_versionnee_refusee(chemin_db):
    chemin_db.parent.mkdir(parents=True)
    brut = sqlite3.connect(chemin_db)
    brut.execute("CREATE TABLE pieces (x)")
    brut.commit()
    brut.close()
    with pytest.raises(ErreurBase):
        Depot(chemin_db)


def test_fichier_qui_n_est_pas_sqlite(chemin_db):
    chemin_db.parent.mkdir(parents=True)
    chemin_db.write_bytes(b"ceci n'est pas une base SQLite " * 50)
    with pytest.raises(ErreurBase):
        Depot(chemin_db)


def test_reouverture_meme_version_ok(chemin_db):
    Depot(chemin_db).fermer()
    Depot(chemin_db).fermer()


# --- (10) injection ---------------------------------------------------------

CHARGES = [
    "x'; DROP TABLE pieces; --",
    "\"; DROP TABLE brouillons; --",
    "' OR '1'='1",
    "Robert'); DROP TABLE envois;--",
    "a\x00b",
    "%_\\",
]


@pytest.mark.parametrize("charge", CHARGES)
def test_injection_destinataire_stocke_et_relu_tel_quel(depot, charge):
    depot.sauver_brouillon(brouillon(destinataire=charge))
    depot.enregistrer_envoi(envoi(dest=charge))
    depot.sauver_pieces([piece(dossier=charge, libelle=charge, reference=charge)])
    depot.marquer_vu(charge)
    depot.mettre_en_file(decision(message_id=charge, empreinte=charge, detail=charge))
    assert depot.charger_brouillon("r1").destinataire == charge
    assert depot.historique_envois()[0].destinataire == charge
    assert [e.destinataire for e in depot.historique_envois(destinataire=charge)] == [charge]
    assert depot.charger_pieces(dossier=charge)[0].libelle == charge
    assert depot.est_vu(charge)
    assert depot.file_humaine()[0].message_id == charge
    depot.resoudre_file(charge, charge, par=charge, note=charge)
    # les tables existent toujours et rien d'autre n'a ete touche
    assert len(depot.charger_pieces()) == 1


def test_injection_dans_les_filtres_ne_correspond_a_rien(depot):
    depot.sauver_pieces([piece()])
    depot.enregistrer_envoi(envoi())
    assert depot.charger_pieces(dossier="D1' OR '1'='1") == []
    assert depot.charger_pieces(periode="2026-04' OR '1'='1") == []
    assert depot.historique_envois(destinataire="x' OR '1'='1") == []
    assert len(depot.charger_pieces()) == 1


def test_injection_dans_la_cle_de_marquer_vu(depot):
    assert depot.marquer_vu("a'); DELETE FROM vus; --") is True
    assert depot.marquer_vu("autre") is True
    assert depot.est_vu("a'); DELETE FROM vus; --")


def test_destinataire_malicieux_survit_a_l_export_et_au_reimport(depot, tmp_path):
    depot.sauver_brouillon(brouillon(destinataire=CHARGES[0]))
    export = depot.exporter_json(tmp_path / "e.json")
    neuf = Depot(tmp_path / "n.db")
    neuf.importer_json(export)
    assert neuf.charger_brouillon("r1").destinataire == CHARGES[0]
    neuf.fermer()


# --- divers -----------------------------------------------------------------


def test_context_manager(tmp_path):
    with Depot(tmp_path / "c.db") as d:
        d.sauver_pieces([piece()])
    with pytest.raises(sqlite3.ProgrammingError):
        d.charger_pieces()


def test_unicode_aller_retour(depot):
    p = piece(libelle="Énergie € café \U0001F600", dossier="Société é")
    depot.sauver_pieces([p])
    assert depot.charger_pieces() == [p]
