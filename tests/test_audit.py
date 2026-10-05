"""Tests du journal d'audit chaine (contrat 4.1)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import threading
from decimal import Decimal
from enum import Enum
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rapprochement.audit import (  # noqa: E402
    HASH_INITIAL,
    EntreeAudit,
    JournalAudit,
    JournalCorrompu,
    ResultatVerification,
)

T0 = dt.datetime(2026, 4, 1, 9, 0, tzinfo=dt.timezone.utc)


class Couleur(Enum):
    ROUGE = "rouge"


def remplir(chemin: Path, n: int = 5) -> JournalAudit:
    j = JournalAudit(chemin)
    for i in range(1, n + 1):
        j.ecrire(
            "alice", f"action{i}", f"objet{i}", {"valeur": i, "texte": f"t{i}"},
            horodatage=T0 + dt.timedelta(minutes=i),
        )
    return j


def lignes(chemin: Path) -> list[bytes]:
    return chemin.read_bytes().splitlines(keepends=True)


def reecrire(chemin: Path, nouvelles: list[bytes]) -> None:
    chemin.write_bytes(b"".join(nouvelles))


def modifier_ligne(brut: bytes, fonction) -> bytes:
    donnees = json.loads(brut)
    fonction(donnees)
    return (json.dumps(donnees, sort_keys=True, separators=(",", ":")) + "\n").encode()


@pytest.fixture
def chemin(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "journal.jsonl"


# --- cas nominal ------------------------------------------------------------


def test_chaine_nominale(chemin):
    j = remplir(chemin)
    r = j.verifier()
    assert r == ResultatVerification(True, 5, None, r.raison)
    entrees = j.lire()
    assert [e.seq for e in entrees] == [1, 2, 3, 4, 5]
    assert entrees[0].hash_precedent == HASH_INITIAL == "0" * 64
    for prec, cour in zip(entrees, entrees[1:]):
        assert cour.hash_precedent == prec.hash
    assert all(isinstance(e, EntreeAudit) for e in entrees)


def test_hash_est_sha256_du_json_canonique_sans_hash(chemin):
    j = JournalAudit(chemin)
    e = j.ecrire("alice", "creation", "piece:1", {"b": 2, "a": 1}, horodatage=T0)
    corps = {
        "seq": 1, "horodatage": T0.isoformat(), "acteur": "alice", "action": "creation",
        "objet": "piece:1", "details": {"a": 1, "b": 2}, "hash_precedent": "0" * 64,
    }
    attendu = hashlib.sha256(
        json.dumps(corps, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert e.hash == attendu


def test_ecrire_renvoie_l_entree_ecrite(chemin):
    j = JournalAudit(chemin)
    e = j.ecrire("systeme", "demarrage", "cycle", horodatage=T0)
    assert e.details == {} and e.seq == 1 and e.acteur == "systeme"
    assert j.lire() == [e]


def test_horodatage_par_defaut_est_aware(chemin):
    j = JournalAudit(chemin)
    e = j.ecrire("systeme", "a", "b")
    assert dt.datetime.fromisoformat(e.horodatage).tzinfo is not None


def test_journal_rouvert_continue_la_chaine(chemin):
    remplir(chemin, 3)
    j2 = JournalAudit(chemin)  # nouvelle instance, comme apres un redemarrage
    e = j2.ecrire("bob", "suite", "x", horodatage=T0)
    assert e.seq == 4
    assert e.hash_precedent == j2.lire()[2].hash
    j3 = JournalAudit(chemin)
    j3.ecrire("bob", "suite2", "x", horodatage=T0)
    r = JournalAudit(chemin).verifier()
    assert r.ok and r.nb_entrees == 5


def test_deux_instances_alternees_gardent_une_chaine_valide(chemin):
    a, b = JournalAudit(chemin), JournalAudit(chemin)
    for i in range(10):
        (a if i % 2 else b).ecrire("alice", "x", str(i), horodatage=T0)
    assert a.verifier().ok and a.verifier().nb_entrees == 10


def test_ecritures_concurrentes_threads(chemin):
    journaux = [JournalAudit(chemin) for _ in range(4)]

    def travail(j: JournalAudit, k: int) -> None:
        for i in range(15):
            j.ecrire(f"t{k}", "x", str(i), {"i": i}, horodatage=T0)

    threads = [threading.Thread(target=travail, args=(j, k)) for k, j in enumerate(journaux)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    r = journaux[0].verifier()
    assert r.ok and r.nb_entrees == 60
    assert [e.seq for e in journaux[0].lire()] == list(range(1, 61))


# --- fichier vide -----------------------------------------------------------


def test_fichier_vide_est_un_journal_valide(chemin):
    j = JournalAudit(chemin)
    assert j.lire() == []
    r = j.verifier()
    assert r.ok and r.nb_entrees == 0 and r.premiere_erreur_seq is None


def test_fichier_vide_ecrit_la_premiere_entree(chemin):
    j = JournalAudit(chemin)
    chemin.write_bytes(b"")
    assert j.ecrire("alice", "a", "b", horodatage=T0).seq == 1


def test_fichier_supprime_est_signale_sans_lever(chemin):
    j = remplir(chemin, 2)
    chemin.unlink()
    r = j.verifier()
    assert not r.ok and r.premiere_erreur_seq is None and r.raison


# --- alterations ------------------------------------------------------------


def test_entree_modifiee_dans_details(chemin):
    j = remplir(chemin)
    ls = lignes(chemin)
    ls[2] = modifier_ligne(ls[2], lambda d: d["details"].__setitem__("valeur", 999))
    reecrire(chemin, ls)
    r = j.verifier()
    assert not r.ok
    assert r.premiere_erreur_seq == 3
    assert r.nb_entrees == 2
    assert "modifi" in r.raison


@pytest.mark.parametrize("champ", ["acteur", "action", "objet", "horodatage"])
def test_entree_modifiee_champ_simple(chemin, champ):
    j = remplir(chemin)
    ls = lignes(chemin)
    ls[1] = modifier_ligne(ls[1], lambda d: d.__setitem__(champ, "autre"))
    reecrire(chemin, ls)
    assert j.verifier().premiere_erreur_seq == 2


def test_entree_modifiee_avec_hash_recalcule_rompt_la_suite(chemin):
    """Un faussaire qui recalcule le hash de la ligne modifiee casse l'entree suivante."""
    j = remplir(chemin)
    ls = lignes(chemin)

    def falsifier(d):
        d["details"]["valeur"] = 999
        sans = {k: v for k, v in d.items() if k != "hash"}
        d["hash"] = hashlib.sha256(
            json.dumps(sans, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    ls[1] = modifier_ligne(ls[1], falsifier)
    reecrire(chemin, ls)
    r = j.verifier()
    assert not r.ok and r.premiere_erreur_seq == 3


def test_entree_supprimee_au_milieu(chemin):
    j = remplir(chemin)
    ls = lignes(chemin)
    del ls[2]  # l'entree seq 3 disparait
    reecrire(chemin, ls)
    r = j.verifier()
    assert not r.ok
    assert r.premiere_erreur_seq == 3  # rang de la ligne fautive = seq manquant
    assert r.nb_entrees == 2
    assert "seq" in r.raison


def test_premiere_entree_supprimee(chemin):
    j = remplir(chemin)
    ls = lignes(chemin)
    del ls[0]
    reecrire(chemin, ls)
    assert j.verifier().premiere_erreur_seq == 1


def test_deux_entrees_echangees(chemin):
    j = remplir(chemin)
    ls = lignes(chemin)
    ls[1], ls[2] = ls[2], ls[1]
    reecrire(chemin, ls)
    r = j.verifier()
    assert not r.ok and r.premiere_erreur_seq == 2


def test_seq_non_contigu_avec_chaine_et_hash_refaits(chemin):
    """seq saute de 3 a 5 alors que hash_precedent et hash sont coherents."""
    j = remplir(chemin, 3)
    ls = lignes(chemin)
    prec = json.loads(ls[1])["hash"]
    corps = {
        "seq": 5, "horodatage": T0.isoformat(), "acteur": "alice", "action": "x",
        "objet": "y", "details": {}, "hash_precedent": prec,
    }
    h = hashlib.sha256(json.dumps(corps, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    ls[2] = (json.dumps({**corps, "hash": h}, sort_keys=True, separators=(",", ":")) + "\n").encode()
    reecrire(chemin, ls)
    r = j.verifier()
    assert not r.ok and r.premiere_erreur_seq == 3 and "seq" in r.raison


def test_derniere_ligne_tronquee(chemin):
    j = remplir(chemin)
    brut = chemin.read_bytes()
    chemin.write_bytes(brut[:-40])
    r = j.verifier()
    assert not r.ok and r.premiere_erreur_seq == 5 and r.nb_entrees == 4
    assert "tronqu" in r.raison


def test_derniere_ligne_coupee_juste_avant_le_saut_de_ligne(chemin):
    j = remplir(chemin)
    chemin.write_bytes(chemin.read_bytes()[:-1])
    r = j.verifier()
    assert not r.ok and r.premiere_erreur_seq == 5 and "tronqu" in r.raison


def test_ligne_non_json_au_milieu(chemin):
    j = remplir(chemin)
    ls = lignes(chemin)
    ls[3] = b"ceci n'est pas du JSON\n"
    reecrire(chemin, ls)
    r = j.verifier()
    assert not r.ok and r.premiere_erreur_seq == 4 and "JSON" in r.raison


def test_ligne_vide_au_milieu(chemin):
    j = remplir(chemin)
    ls = lignes(chemin)
    ls.insert(2, b"\n")
    reecrire(chemin, ls)
    assert j.verifier().premiere_erreur_seq == 3


def test_ligne_json_mais_pas_un_objet(chemin):
    j = remplir(chemin, 2)
    ls = lignes(chemin)
    ls[1] = b"[1,2,3]\n"
    reecrire(chemin, ls)
    assert j.verifier().premiere_erreur_seq == 2


def test_octets_non_utf8(chemin):
    j = remplir(chemin, 2)
    ls = lignes(chemin)
    ls[0] = b"\xff\xfe\xfd\n"
    reecrire(chemin, ls)
    r = j.verifier()
    assert not r.ok and r.premiere_erreur_seq == 1


def test_champ_manquant_ou_en_trop(chemin):
    j = remplir(chemin, 3)
    ls = lignes(chemin)
    ls[1] = modifier_ligne(ls[1], lambda d: d.pop("acteur"))
    reecrire(chemin, ls)
    assert j.verifier().premiere_erreur_seq == 2


def test_octets_alteres_sans_changer_le_contenu_logique(chemin):
    """Reformater une ligne (espaces) est detecte : le journal est canonique."""
    j = remplir(chemin, 3)
    ls = lignes(chemin)
    ls[1] = ls[1].replace(b",", b", ", 1)
    reecrire(chemin, ls)
    assert j.verifier().premiere_erreur_seq == 2


def test_ligne_inseree_au_milieu(chemin):
    j = remplir(chemin, 4)
    ls = lignes(chemin)
    ls.insert(2, ls[0])
    reecrire(chemin, ls)
    assert j.verifier().premiere_erreur_seq == 3


def test_premiere_erreur_est_la_plus_precoce(chemin):
    j = remplir(chemin)
    ls = lignes(chemin)
    ls[3] = b"junk\n"
    ls[1] = modifier_ligne(ls[1], lambda d: d["details"].__setitem__("valeur", 0))
    reecrire(chemin, ls)
    assert j.verifier().premiere_erreur_seq == 2


# --- verifier ne leve jamais ------------------------------------------------


@pytest.mark.parametrize(
    "contenu",
    [
        b"\x00\x01\x02",
        b"\n\n\n",
        b"{",
        b"null\n",
        b'{"seq": "a"}\n',
        os.urandom(2000),
        b"[" * 100000 + b"\n",
        b'{"seq":1e999}\n',
    ],
)
def test_verifier_ne_leve_jamais(chemin, contenu):
    j = JournalAudit(chemin)
    chemin.write_bytes(contenu)
    r = j.verifier()
    assert isinstance(r, ResultatVerification)
    assert not r.ok and r.raison


def test_verifier_ne_leve_pas_si_le_chemin_est_un_repertoire(tmp_path):
    j = JournalAudit(tmp_path / "j.jsonl")
    (tmp_path / "j.jsonl").unlink()
    (tmp_path / "j.jsonl").mkdir()
    r = j.verifier()
    assert isinstance(r, ResultatVerification) and not r.ok


def test_lire_leve_sur_ligne_illisible(chemin):
    j = remplir(chemin, 2)
    ls = lignes(chemin)
    ls[1] = b"junk\n"
    reecrire(chemin, ls)
    with pytest.raises(JournalCorrompu):
        j.lire()


# --- ecriture sur un journal abime ------------------------------------------


def test_ecrire_refuse_d_etendre_une_ligne_tronquee(chemin):
    j = remplir(chemin, 3)
    chemin.write_bytes(chemin.read_bytes()[:-10])
    avant = chemin.read_bytes()
    with pytest.raises(JournalCorrompu):
        j.ecrire("alice", "x", "y")
    assert chemin.read_bytes() == avant  # rien n'a ete ajoute
    assert not j.verifier().ok


def test_ecrire_refuse_d_etendre_une_derniere_entree_modifiee(chemin):
    j = remplir(chemin, 3)
    ls = lignes(chemin)
    ls[2] = modifier_ligne(ls[2], lambda d: d["details"].__setitem__("valeur", 0))
    reecrire(chemin, ls)
    with pytest.raises(JournalCorrompu):
        j.ecrire("alice", "x", "y")


# --- acteur et details ------------------------------------------------------


@pytest.mark.parametrize("acteur", ["", "   ", "\t\n"])
def test_acteur_vide_leve_value_error(chemin, acteur):
    j = JournalAudit(chemin)
    with pytest.raises(ValueError):
        j.ecrire(acteur, "action", "objet")
    assert j.lire() == []  # rien d'ecrit


def test_acteur_non_chaine_refuse(chemin):
    with pytest.raises(ValueError):
        JournalAudit(chemin).ecrire(None, "a", "b")  # type: ignore[arg-type]


def test_systeme_est_un_acteur_valide(chemin):
    assert JournalAudit(chemin).ecrire("systeme", "a", "b").acteur == "systeme"


DETAILS_RICHES = {
    "montant": Decimal("12.50"),
    "jour": dt.date(2026, 4, 1),
    "instant": dt.datetime(2026, 4, 1, 9, 30, tzinfo=dt.timezone.utc),
    "couleur": Couleur.ROUGE,
    "paire": (1, "deux", Decimal("3.0")),
    "ensemble": {"c", "a", "b"},
    "fige": frozenset({3, 1, 2}),
    "imbrique": {"liste": [Decimal("0.10"), (dt.date(2026, 1, 2),)]},
}


def test_types_riches_donnent_un_json_deterministe(tmp_path):
    j1 = JournalAudit(tmp_path / "a.jsonl")
    j2 = JournalAudit(tmp_path / "b.jsonl")
    # Ensembles construits dans des ordres d'insertion differents.
    d2 = dict(reversed(list(DETAILS_RICHES.items())))
    d2["ensemble"] = {"b", "a", "c"}
    j1.ecrire("alice", "a", "o", DETAILS_RICHES, horodatage=T0)
    j2.ecrire("alice", "a", "o", d2, horodatage=T0)
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()
    assert j1.verifier().ok


def test_types_riches_valeurs_attendues(chemin):
    e = JournalAudit(chemin).ecrire("alice", "a", "o", DETAILS_RICHES, horodatage=T0)
    assert e.details == {
        "montant": "12.50",
        "jour": "2026-04-01",
        "instant": "2026-04-01T09:30:00+00:00",
        "couleur": "rouge",
        "paire": [1, "deux", "3.0"],
        "ensemble": ["a", "b", "c"],
        "fige": [1, 2, 3],
        "imbrique": {"liste": ["0.10", ["2026-01-02"]]},
    }
    assert JournalAudit(chemin).lire()[0].details == e.details


def test_decimal_garde_ses_decimales(chemin):
    e = JournalAudit(chemin).ecrire("a", "b", "c", {"x": Decimal("0.10")})
    assert e.details["x"] == "0.10"


def test_ensemble_de_types_mixtes_est_deterministe(chemin):
    e = JournalAudit(chemin).ecrire("a", "b", "c", {"s": {1, "a", None, Decimal("2")}})
    f = JournalAudit(chemin.with_name("autre.jsonl")).ecrire(
        "a", "b", "c", {"s": {Decimal("2"), None, "a", 1}}, horodatage=dt.datetime.fromisoformat(e.horodatage)
    )
    assert e.details == f.details


def test_deux_ecritures_identiques_memes_octets(tmp_path):
    for nom in ("x.jsonl", "y.jsonl"):
        j = JournalAudit(tmp_path / nom)
        j.ecrire("alice", "a", "o", DETAILS_RICHES, horodatage=T0)
        j.ecrire("bob", "b", "p", {"k": Decimal("1.0")}, horodatage=T0)
    assert (tmp_path / "x.jsonl").read_bytes() == (tmp_path / "y.jsonl").read_bytes()


def test_unicode_est_conserve(chemin):
    j = JournalAudit(chemin)
    e = j.ecrire("zoe", "relance envoyee", "client été €", {"nom": "Crème \U0001F600"})
    assert j.lire() == [e]
    assert j.verifier().ok
    chemin.read_bytes().decode("ascii")  # fichier ASCII pur : aucun souci d'encodage


@pytest.mark.parametrize(
    "valeur",
    [b"octets", object(), 1 + 2j, lambda: 0, Path("x"), dt.timedelta(1), [object()], {"a": {object()}}],
)
def test_type_inconnu_leve_type_error(chemin, valeur):
    j = JournalAudit(chemin)
    with pytest.raises(TypeError):
        j.ecrire("alice", "a", "o", {"v": valeur})
    assert j.lire() == []


def test_cle_non_chaine_leve_type_error(chemin):
    with pytest.raises(TypeError):
        JournalAudit(chemin).ecrire("a", "b", "c", {1: "x"})  # type: ignore[dict-item]


def test_type_error_nomme_le_chemin_fautif(chemin):
    with pytest.raises(TypeError, match=r"details\.a\.b"):
        JournalAudit(chemin).ecrire("a", "b", "c", {"a": {"b": object()}})


@pytest.mark.parametrize("flottant", [float("nan"), float("inf")])
def test_flottant_non_fini_refuse(chemin, flottant):
    with pytest.raises(ValueError):
        JournalAudit(chemin).ecrire("a", "b", "c", {"x": flottant})


def test_decimal_non_fini_refuse(chemin):
    with pytest.raises(ValueError):
        JournalAudit(chemin).ecrire("a", "b", "c", {"x": Decimal("NaN")})


def test_details_circulaires_refuses(chemin):
    boucle: list = []
    boucle.append(boucle)
    with pytest.raises(ValueError):
        JournalAudit(chemin).ecrire("a", "b", "c", {"x": boucle})


def test_details_non_mapping_refuse(chemin):
    with pytest.raises(TypeError):
        JournalAudit(chemin).ecrire("a", "b", "c", [1, 2])  # type: ignore[arg-type]


def test_ecriture_refusee_ne_laisse_aucune_trace(chemin):
    j = remplir(chemin, 2)
    avant = chemin.read_bytes()
    with pytest.raises(TypeError):
        j.ecrire("alice", "a", "o", {"v": object()})
    assert chemin.read_bytes() == avant
    assert j.verifier().ok


def test_echec_d_ecriture_disque_retablit_le_fichier(chemin, monkeypatch):
    j = remplir(chemin, 2)
    avant = chemin.read_bytes()

    def echec(fd):
        raise OSError("disque plein")

    monkeypatch.setattr(os, "fsync", echec)
    with pytest.raises(OSError):
        j.ecrire("alice", "a", "o")
    monkeypatch.undo()
    assert chemin.read_bytes() == avant
    assert j.verifier().ok
    assert j.ecrire("alice", "a", "o").seq == 3


def test_fsync_a_chaque_ecriture(chemin, monkeypatch):
    appels: list[int] = []
    vrai = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (appels.append(fd), vrai(fd))[1])
    j = JournalAudit(chemin)
    n0 = len(appels)
    j.ecrire("a", "b", "c")
    j.ecrire("a", "b", "c")
    assert len(appels) - n0 == 2


def test_details_non_mutes_par_l_appelant_apres_ecriture(chemin):
    d = {"liste": [1, 2]}
    e = JournalAudit(chemin).ecrire("a", "b", "c", d)
    d["liste"].append(3)
    assert e.details == {"liste": [1, 2]}


def test_journal_cree_les_repertoires_manquants(tmp_path):
    j = JournalAudit(tmp_path / "a" / "b" / "c.jsonl")
    j.ecrire("a", "b", "c")
    assert (tmp_path / "a" / "b" / "c.jsonl").exists()


def test_chemin_str_accepte(tmp_path):
    j = JournalAudit(str(tmp_path / "s.jsonl"))
    assert j.ecrire("a", "b", "c").seq == 1


def test_gros_journal_ecriture_lit_seulement_la_fin(chemin):
    """Ecrire apres un fichier a grande ligne finale reste correct (lecture par la fin)."""
    j = JournalAudit(chemin)
    j.ecrire("a", "b", "c", {"gros": "x" * 200_000})
    e = j.ecrire("a", "b", "c")
    assert e.seq == 2 and j.verifier().ok


# --- reparer_fin_tronquee ---------------------------------------------------


def fragments(chemin: Path) -> list[Path]:
    return sorted(chemin.parent.glob(chemin.name + ".fragment-*"))


def test_reparation_fin_tronquee_puis_la_chaine_continue(chemin):
    j = remplir(chemin, 4)
    brut = chemin.read_bytes()
    chemin.write_bytes(brut[:-25])
    assert not j.verifier().ok
    retires = j.reparer_fin_tronquee()
    assert retires > 0
    r = j.verifier()
    assert r.ok and r.nb_entrees == 3
    e = j.ecrire("alice", "suite", "x", horodatage=T0)
    assert e.seq == 4 and e.hash_precedent == j.lire()[2].hash
    assert j.verifier().ok and j.verifier().nb_entrees == 4


def test_reparation_sauvegarde_le_fragment(chemin):
    j = remplir(chemin, 4)
    brut = chemin.read_bytes()
    coupe = brut[:-25]
    chemin.write_bytes(coupe)
    debut_fragment = coupe.rfind(b"\n") + 1
    retires = j.reparer_fin_tronquee()
    (frag,) = fragments(chemin)
    assert frag.read_bytes() == coupe[debut_fragment:]
    assert retires == len(coupe) - debut_fragment
    assert chemin.read_bytes() == coupe[:debut_fragment]
    assert frag.name.split(".fragment-")[1].endswith("Z")


def test_entree_valide_sans_saut_de_ligne_est_completee(chemin):
    j = remplir(chemin, 3)
    complet = chemin.read_bytes()
    chemin.write_bytes(complet[:-1])
    assert not j.verifier().ok
    assert j.reparer_fin_tronquee() == 0
    assert chemin.read_bytes() == complet  # rien de retire, seul "\n" ajoute
    assert fragments(chemin) == []
    assert j.verifier().ok and j.verifier().nb_entrees == 3
    assert j.ecrire("alice", "suite", "x", horodatage=T0).seq == 4
    assert j.verifier().ok


def test_entree_sans_saut_de_ligne_hash_faux_est_un_fragment(chemin):
    j = remplir(chemin, 3)
    ls = lignes(chemin)
    ls[2] = modifier_ligne(ls[2], lambda d: d["details"].__setitem__("valeur", 0))[:-1]
    reecrire(chemin, ls)
    fragment = ls[2]
    assert j.reparer_fin_tronquee() == len(fragment)
    assert [p.read_bytes() for p in fragments(chemin)] == [fragment]
    assert j.verifier().ok and j.verifier().nb_entrees == 2


def test_entree_sans_saut_de_ligne_seq_saute_est_un_fragment(chemin):
    j = remplir(chemin, 3)
    ls = lignes(chemin)
    prec = json.loads(ls[1])["hash"]
    corps = {
        "seq": 7, "horodatage": T0.isoformat(), "acteur": "alice", "action": "x",
        "objet": "y", "details": {}, "hash_precedent": prec,
    }
    h = hashlib.sha256(json.dumps(corps, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    ls[2] = json.dumps({**corps, "hash": h}, sort_keys=True, separators=(",", ":")).encode()
    reecrire(chemin, ls)
    assert j.reparer_fin_tronquee() == len(ls[2])
    assert len(fragments(chemin)) == 1
    assert j.verifier().ok and j.verifier().nb_entrees == 2


def test_completion_du_saut_de_ligne_idempotente(chemin):
    j = remplir(chemin, 3)
    chemin.write_bytes(chemin.read_bytes()[:-1])
    assert j.reparer_fin_tronquee() == 0
    apres = chemin.read_bytes()
    assert j.reparer_fin_tronquee() == 0
    assert chemin.read_bytes() == apres and fragments(chemin) == []


def test_reparation_json_invalide_en_derniere_ligne(chemin):
    j = remplir(chemin, 3)
    with open(chemin, "ab") as f:
        f.write(b"ceci n'est pas du JSON\n")
    retires = j.reparer_fin_tronquee()
    assert retires == len(b"ceci n'est pas du JSON\n")
    assert j.verifier().ok and j.verifier().nb_entrees == 3
    assert fragments(chemin)[0].read_bytes() == b"ceci n'est pas du JSON\n"


def test_reparation_seule_ligne_tronquee(chemin):
    j = remplir(chemin, 1)
    chemin.write_bytes(chemin.read_bytes()[:20])
    assert j.reparer_fin_tronquee() == 20
    assert chemin.read_bytes() == b""
    assert j.ecrire("a", "b", "c").seq == 1


def test_reparation_refuse_si_une_entree_du_milieu_est_alteree(chemin):
    j = remplir(chemin, 5)
    ls = lignes(chemin)
    ls[1] = modifier_ligne(ls[1], lambda d: d["details"].__setitem__("valeur", 999))
    reecrire(chemin, ls)
    chemin.write_bytes(chemin.read_bytes()[:-25])  # fin tronquee EN PLUS
    avant = chemin.read_bytes()
    with pytest.raises(JournalCorrompu):
        j.reparer_fin_tronquee()
    assert chemin.read_bytes() == avant
    assert fragments(chemin) == []


def test_reparation_refuse_si_la_chaine_est_rompue_ailleurs(chemin):
    j = remplir(chemin, 5)
    ls = lignes(chemin)
    del ls[2]
    reecrire(chemin, ls)
    avant = chemin.read_bytes()
    with pytest.raises(JournalCorrompu):
        j.reparer_fin_tronquee()
    assert chemin.read_bytes() == avant and fragments(chemin) == []


def test_reparation_refuse_si_derniere_entree_complete_est_modifiee(chemin):
    """Ligne terminee, JSON valide mais hash faux : falsification, pas troncature."""
    j = remplir(chemin, 3)
    ls = lignes(chemin)
    ls[2] = modifier_ligne(ls[2], lambda d: d["details"].__setitem__("valeur", 0))
    reecrire(chemin, ls)
    avant = chemin.read_bytes()
    with pytest.raises(JournalCorrompu):
        j.reparer_fin_tronquee()
    assert chemin.read_bytes() == avant and fragments(chemin) == []


def test_reparation_idempotente(chemin):
    j = remplir(chemin, 3)
    chemin.write_bytes(chemin.read_bytes()[:-25])
    assert j.reparer_fin_tronquee() > 0
    apres = chemin.read_bytes()
    assert j.reparer_fin_tronquee() == 0
    assert chemin.read_bytes() == apres
    assert len(fragments(chemin)) == 1


def test_reparation_fichier_vide(chemin):
    j = JournalAudit(chemin)
    assert j.reparer_fin_tronquee() == 0
    assert chemin.read_bytes() == b"" and fragments(chemin) == []


def test_reparation_journal_sain_ne_touche_a_rien(chemin):
    j = remplir(chemin, 4)
    avant = chemin.read_bytes()
    assert j.reparer_fin_tronquee() == 0
    assert chemin.read_bytes() == avant and fragments(chemin) == []


def test_deux_reparations_dans_la_meme_seconde_ne_s_ecrasent_pas(chemin):
    j = remplir(chemin, 3)
    for _ in range(2):
        chemin.write_bytes(chemin.read_bytes() + b'{"seq": 9')
        j.reparer_fin_tronquee()
    assert len(fragments(chemin)) == 2
    assert all(p.read_bytes() == b'{"seq": 9' for p in fragments(chemin))
    assert j.verifier().ok
