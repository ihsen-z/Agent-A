"""Rejeu des scenarios principaux de envoi.py avec les VRAIS Depot et JournalAudit.

Ignore automatiquement tant que `depot.py` n'existe pas.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("rapprochement.depot")
pytest.importorskip("rapprochement.audit")

from rapprochement.audit import JournalAudit
from rapprochement.depot import Depot
from rapprochement.envoi import (
    EnvoiHorsCreneau,
    EnvoiNonValide,
    ErreurEnvoi,
    ErreurEnvoiCertaine,
    ExpediteurDossier,
    envoyer,
    envois_incertains,
    rejeter,
    trancher_envoi_incertain,
    valider,
)
from rapprochement.modeles import Brouillon, StatutBrouillon, identifiant_relance

S = StatutBrouillon

MARDI_10H = dt.datetime(2026, 10, 6, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
MARDI_20H = dt.datetime(2026, 10, 6, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
SAMEDI_10H = dt.datetime(2026, 10, 10, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))


class Espion:
    def __init__(self, leve: BaseException | None = None) -> None:
        self.n = 0
        self.leve = leve

    def envoyer(self, brouillon: Brouillon) -> str:
        self.n += 1
        if self.leve is not None:
            raise self.leve
        return "ext-1"


@pytest.fixture
def depot() -> Any:
    d = Depot(":memory:")
    yield d
    d.fermer()


@pytest.fixture
def journal(tmp_path: Path) -> JournalAudit:
    return JournalAudit(tmp_path / "audit.jsonl")


@pytest.fixture
def b(depot: Any) -> Brouillon:
    br = Brouillon(
        id_relance=identifiant_relance("client@exemple.fr", ("OP1", "OP2"), 1, dt.date(2026, 10, 5)),
        destinataire="client@exemple.fr",
        objet="Pieces manquantes",
        corps="Bonjour,\n\nIl nous manque deux pieces.\n",
        dossiers=("D1",),
        references=("OP1", "OP2"),
        niveau=1,
        cree_le=dt.date(2026, 10, 5),
    )
    assert depot.sauver_brouillon(br)
    return br


def statut(depot: Any, b: Brouillon) -> StatutBrouillon:
    return depot.charger_brouillon(b.id_relance).statut


def actions(journal: JournalAudit) -> list[tuple[str, str]]:
    return [(e.acteur, e.action) for e in journal.lire()]


def test_cycle_complet_avec_le_creneau_par_defaut(depot, journal, b, tmp_path) -> None:
    sortie = tmp_path / "boite"
    valider(depot, journal, b.id_relance, "Alice Martin", maintenant=MARDI_10H)
    r = envoyer(depot, journal, ExpediteurDossier(sortie), b.id_relance, maintenant=MARDI_10H)
    assert r.statut is S.ENVOYEE and r.envoye_le == MARDI_10H
    assert depot.charger_brouillon(b.id_relance) == r
    (e,) = depot.historique_envois(destinataire="client@exemple.fr")
    assert e.id_relance == b.id_relance and e.date_envoi == dt.date(2026, 10, 6)
    assert len(list(sortie.glob("*.eml"))) == 1
    assert actions(journal) == [
        ("Alice Martin", "valide"), ("systeme", "envoi_demarre"), ("systeme", "envoi_effectue"),
    ]
    assert journal.verifier().ok


def test_double_appel_un_seul_envoi(depot, journal, b) -> None:
    valider(depot, journal, b.id_relance, "Alice", maintenant=MARDI_10H)
    exp = Espion()
    envoyer(depot, journal, exp, b.id_relance, maintenant=MARDI_10H)
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, b.id_relance, maintenant=MARDI_10H)
    assert exp.n == 1 and len(depot.historique_envois()) == 1


def test_sans_validation_rien_ne_part(depot, journal, b) -> None:
    exp = Espion()
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, b.id_relance, maintenant=MARDI_10H)
    for par in ["", "systeme", "AUTO", " robot ", "cron", "bot"]:
        with pytest.raises(EnvoiNonValide):
            valider(depot, journal, b.id_relance, par, maintenant=MARDI_10H)
    assert exp.n == 0 and statut(depot, b) is S.BROUILLON and {e.action for e in journal.lire()} == {"envoi_refuse"}


def test_validee_forgee_sans_humain_refusee(depot, journal) -> None:
    forge = Brouillon(
        id_relance="forge01", destinataire="c@x.fr", objet="o", corps="c", dossiers=("D",),
        references=("R",), niveau=1, cree_le=dt.date(2026, 10, 5), statut=S.VALIDEE, valide_par="",
    )
    assert depot.sauver_brouillon(forge)  # le depot ne controle pas valide_par : envoi.py doit le faire
    exp = Espion()
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, "forge01", maintenant=MARDI_10H)
    assert exp.n == 0 and statut(depot, forge) is S.VALIDEE


def test_issue_incertaine_reste_en_cours_et_n_est_jamais_renvoyee(depot, journal, b) -> None:
    valider(depot, journal, b.id_relance, "Alice", maintenant=MARDI_10H)
    exp = Espion(leve=TimeoutError("pas de reponse"))
    with pytest.raises(TimeoutError):
        envoyer(depot, journal, exp, b.id_relance, maintenant=MARDI_10H)
    assert statut(depot, b) is S.EN_COURS
    assert [x.id_relance for x in envois_incertains(depot)] == [b.id_relance]
    assert depot.historique_envois() == []
    assert actions(journal)[-1] == ("systeme", "envoi_incertain")
    exp.leve = None
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, b.id_relance, maintenant=MARDI_10H)
    assert exp.n == 1
    assert actions(journal)[-1] == ("systeme", "envoi_refuse")
    assert journal.verifier().ok


def test_erreur_certaine_permet_de_reessayer(depot, journal, b) -> None:
    valider(depot, journal, b.id_relance, "Alice", maintenant=MARDI_10H)
    exp = Espion(leve=ErreurEnvoiCertaine("refus"))
    with pytest.raises(ErreurEnvoiCertaine):
        envoyer(depot, journal, exp, b.id_relance, maintenant=MARDI_10H)
    assert statut(depot, b) is S.VALIDEE and envois_incertains(depot) == []
    exp.leve = None
    envoyer(depot, journal, exp, b.id_relance, maintenant=MARDI_10H)
    assert exp.n == 2 and statut(depot, b) is S.ENVOYEE


@pytest.mark.parametrize("instant", [MARDI_20H, SAMEDI_10H], ids=["soir", "samedi"])
def test_hors_creneau_par_defaut(depot, journal, b, instant) -> None:
    valider(depot, journal, b.id_relance, "Alice", maintenant=MARDI_10H)
    exp = Espion()
    with pytest.raises(EnvoiHorsCreneau):
        envoyer(depot, journal, exp, b.id_relance, maintenant=instant)
    assert exp.n == 0 and statut(depot, b) is S.VALIDEE
    assert ("systeme", "envoi_demarre") not in actions(journal)


def test_hors_creneau_injecte(depot, journal, b) -> None:
    valider(depot, journal, b.id_relance, "Alice", maintenant=MARDI_10H)
    exp = Espion()
    with pytest.raises(EnvoiHorsCreneau):
        envoyer(depot, journal, exp, b.id_relance, maintenant=MARDI_10H, creneau_ok=lambda _: False)
    assert exp.n == 0 and statut(depot, b) is S.VALIDEE


def test_rejet(depot, journal, b) -> None:
    with pytest.raises(EnvoiNonValide):
        rejeter(depot, journal, b.id_relance, "Bob", "  ", maintenant=MARDI_10H)
    r = rejeter(depot, journal, b.id_relance, "Bob", "client en litige", maintenant=MARDI_10H)
    assert r.statut is S.REJETEE and statut(depot, b) is S.REJETEE
    assert actions(journal) == [("systeme", "envoi_refuse"), ("Bob", "rejete")]
    e = journal.lire()[-1]
    assert (e.acteur, e.action) == ("Bob", "rejete") and e.details["motif"] == "client en litige"
    with pytest.raises(EnvoiNonValide):
        rejeter(depot, journal, b.id_relance, "Bob", "encore", maintenant=MARDI_10H)
    with pytest.raises(EnvoiNonValide):
        valider(depot, journal, b.id_relance, "Alice", maintenant=MARDI_10H)


def test_le_depot_refuse_de_sortir_de_envoyee(depot, journal, b) -> None:
    """Le garde-fou de la base double celui du module."""
    import dataclasses

    valider(depot, journal, b.id_relance, "Alice", maintenant=MARDI_10H)
    envoye = envoyer(depot, journal, Espion(), b.id_relance, maintenant=MARDI_10H)
    with pytest.raises(ValueError):
        depot.maj_brouillon(dataclasses.replace(envoye, statut=S.VALIDEE))


def test_eml_existant_laisse_en_cours(depot, journal, b, tmp_path) -> None:
    (tmp_path / f"{b.id_relance}.eml").write_bytes(b"deja la")
    valider(depot, journal, b.id_relance, "Alice", maintenant=MARDI_10H)
    with pytest.raises(ErreurEnvoi):
        envoyer(depot, journal, ExpediteurDossier(tmp_path), b.id_relance, maintenant=MARDI_10H)
    assert statut(depot, b) is S.EN_COURS
    assert (tmp_path / f"{b.id_relance}.eml").read_bytes() == b"deja la"


@pytest.mark.parametrize("parti,final", [(True, S.ENVOYEE), (False, S.VALIDEE)])
def test_trancher_un_envoi_incertain(depot, journal, b, parti, final) -> None:
    valider(depot, journal, b.id_relance, "Alice", maintenant=MARDI_10H)
    with pytest.raises(TimeoutError):
        envoyer(depot, journal, Espion(leve=TimeoutError("t")), b.id_relance, maintenant=MARDI_10H)
    with pytest.raises(EnvoiNonValide):
        trancher_envoi_incertain(depot, journal, b.id_relance, "robot", parti=parti, maintenant=MARDI_10H)
    assert statut(depot, b) is S.EN_COURS
    r = trancher_envoi_incertain(depot, journal, b.id_relance, "Alice", parti=parti, maintenant=MARDI_10H)
    assert r.statut is final and statut(depot, b) is final and envois_incertains(depot) == []
    assert len(depot.historique_envois()) == (1 if parti else 0)
    assert actions(journal)[-1] == (
        "Alice", "envoi_confirme_par_humain" if parti else "envoi_declare_non_parti"
    )
    with pytest.raises(EnvoiNonValide):  # ce qui n'est plus EN_COURS ne se tranche plus
        trancher_envoi_incertain(depot, journal, b.id_relance, "Alice", parti=parti, maintenant=MARDI_10H)
    assert journal.verifier().ok


def test_refus_journalise_et_chaine_valide(depot, journal, b) -> None:
    with pytest.raises(EnvoiNonValide):
        valider(depot, journal, b.id_relance, "AUTO", maintenant=MARDI_10H)
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, Espion(), b.id_relance, maintenant=MARDI_10H)
    assert actions(journal) == [("systeme", "envoi_refuse")] * 2
    assert journal.verifier().ok
