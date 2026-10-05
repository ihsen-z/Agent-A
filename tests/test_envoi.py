"""Tests de envoi.py : CA-05 (zero doublon d'envoi) et CA-06 (zero envoi sans validation).

Ces tests n'utilisent NI depot.py NI audit.py : ils tournent avec des doubles
locaux qui respectent les signatures du contrat 4.1 / 4.2, y compris les
transitions autorisees de `maj_brouillon`. Le rejeu avec les vrais objets est
dans test_envoi_integration.py.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import datetime as dt
import email
import email.policy
import email.utils
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rapprochement import envoi, etats
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
from rapprochement.modeles import Brouillon, EnvoiRelance, StatutBrouillon, identifiant_relance

S = StatutBrouillon

# Un mardi, 10h a Paris (heure d'ete : UTC+2).
MAINTENANT = dt.datetime(2026, 10, 6, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

TRANSITIONS = {
    (S.BROUILLON, S.VALIDEE),
    (S.BROUILLON, S.REJETEE),
    (S.VALIDEE, S.EN_COURS),
    (S.VALIDEE, S.REJETEE),
    (S.EN_COURS, S.ENVOYEE),
    (S.EN_COURS, S.REJETEE),
    (S.EN_COURS, S.VALIDEE),
}


# ---------------------------------------------------------------------------
# Doubles conformes au contrat
# ---------------------------------------------------------------------------


class FauxDepot:
    """Sous-ensemble de `Depot` (contrat 4.2) utilise par envoi.py, en memoire."""

    def __init__(self) -> None:
        self.brouillons: dict[str, Brouillon] = {}
        self.envois: dict[str, EnvoiRelance] = {}
        self.echec_enregistrer_envoi: Exception | None = None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        avant = (dict(self.brouillons), dict(self.envois))
        try:
            yield
        except BaseException:
            self.brouillons, self.envois = avant
            raise

    def sauver_brouillon(self, b: Brouillon) -> bool:
        if b.id_relance in self.brouillons:
            return False
        self.brouillons[b.id_relance] = b
        return True

    def charger_brouillon(self, id_relance: str) -> Brouillon | None:
        return self.brouillons.get(id_relance)

    def lister_brouillons(self, statut: StatutBrouillon | None = None) -> list[Brouillon]:
        return sorted(
            (b for b in self.brouillons.values() if statut is None or b.statut is statut),
            key=lambda b: b.id_relance,
        )

    def maj_brouillon(self, b: Brouillon) -> None:
        actuel = self.brouillons.get(b.id_relance)
        if actuel is None:
            raise ValueError("brouillon inconnu")
        if (actuel.statut, b.statut) not in TRANSITIONS:
            raise ValueError(f"transition interdite {actuel.statut.value} -> {b.statut.value}")
        self.brouillons[b.id_relance] = b

    def enregistrer_envoi(self, e: EnvoiRelance) -> None:
        if self.echec_enregistrer_envoi is not None:
            raise self.echec_enregistrer_envoi
        self.envois.setdefault(e.id_relance, e)

    def historique_envois(
        self, *, destinataire: str | None = None, depuis: dt.date | None = None
    ) -> list[EnvoiRelance]:
        return [e for e in self.envois.values() if destinataire in (None, e.destinataire)]


@dataclasses.dataclass(frozen=True)
class FausseEntree:
    acteur: str
    action: str
    objet: str
    details: dict[str, Any]


class FauxJournal:
    """Sous-ensemble de `JournalAudit` (contrat 4.1) utilise par envoi.py."""

    def __init__(self, echec_sur: str | None = None) -> None:
        self.entrees: list[FausseEntree] = []
        self.echec_sur = echec_sur

    def ecrire(
        self,
        acteur: str,
        action: str,
        objet: str,
        details: Mapping[str, Any] | None = None,
        *,
        horodatage: dt.datetime | None = None,
    ) -> FausseEntree:
        if not acteur:
            raise ValueError("acteur obligatoire")
        if action == self.echec_sur:
            raise OSError("disque plein")
        e = FausseEntree(acteur, action, objet, dict(details or {}))
        self.entrees.append(e)
        return e

    def lire(self) -> list[FausseEntree]:
        return list(self.entrees)

    def actions(self) -> list[str]:
        return [e.action for e in self.entrees]


class Espion:
    """Expediteur qui compte ses appels et peut echouer a la demande."""

    def __init__(self, *, leve: BaseException | None = None, pendant: Any = None) -> None:
        self.appels: list[Brouillon] = []
        self.leve = leve
        self.pendant = pendant  # callable appele pendant l'emission (pour sonder l'etat)

    def envoyer(self, brouillon: Brouillon) -> str:
        self.appels.append(brouillon)
        if self.pendant is not None:
            self.pendant(brouillon)
        if self.leve is not None:
            raise self.leve
        return f"ext-{brouillon.id_relance}"

    @property
    def n(self) -> int:
        return len(self.appels)


class Coupure(BaseException):
    """Interruption brutale (pas une Exception) pendant l'emission."""


def faire_brouillon(**kw: Any) -> Brouillon:
    base: dict[str, Any] = dict(
        id_relance=identifiant_relance("client@exemple.fr", ("OP1", "OP2"), 1, dt.date(2026, 10, 5)),
        destinataire="client@exemple.fr",
        objet="Pieces manquantes - septembre",
        corps="Bonjour,\n\nIl nous manque deux pieces.\n\nCordialement",
        dossiers=("D1",),
        references=("OP1", "OP2"),
        niveau=1,
        cree_le=dt.date(2026, 10, 5),
    )
    base.update(kw)
    return Brouillon(**base)


@pytest.fixture
def depot() -> FauxDepot:
    return FauxDepot()


@pytest.fixture
def journal() -> FauxJournal:
    return FauxJournal()


@pytest.fixture
def b(depot: FauxDepot) -> Brouillon:
    br = faire_brouillon()
    assert depot.sauver_brouillon(br)
    return br


@pytest.fixture
def valide(depot: FauxDepot, journal: FauxJournal, b: Brouillon) -> Brouillon:
    return valider(depot, journal, b.id_relance, "Alice Martin", maintenant=MAINTENANT)


def statut(depot: FauxDepot, b: Brouillon) -> StatutBrouillon:
    c = depot.charger_brouillon(b.id_relance)
    assert c is not None
    return c.statut


# ---------------------------------------------------------------------------
# Les doubles respectent le contrat (garde-fou contre des tests trop laxistes)
# ---------------------------------------------------------------------------


def test_le_faux_depot_refuse_les_transitions_hors_contrat(depot: FauxDepot, b: Brouillon) -> None:
    with pytest.raises(ValueError):
        depot.maj_brouillon(dataclasses.replace(b, statut=S.EN_COURS))
    envoye = dataclasses.replace(b, statut=S.ENVOYEE)
    depot.brouillons[b.id_relance] = envoye
    with pytest.raises(ValueError):  # un brouillon ENVOYEE est immuable
        depot.maj_brouillon(dataclasses.replace(b, statut=S.VALIDEE))


# ---------------------------------------------------------------------------
# CA-06 : aucun envoi sans validation humaine
# ---------------------------------------------------------------------------


def test_envoyer_un_brouillon_non_valide_est_refuse(depot, journal, b) -> None:
    exp = Espion()
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, b.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 0
    assert statut(depot, b) is S.BROUILLON
    assert journal.actions() == ["envoi_refuse"]


@pytest.mark.parametrize("valide_par", ["", "   ", "systeme", "Robot", " cron "])
def test_envoyer_refuse_un_statut_validee_forge_sans_humain(depot, journal, valide_par) -> None:
    forge = faire_brouillon(statut=S.VALIDEE, valide_par=valide_par, valide_le=MAINTENANT)
    depot.sauver_brouillon(forge)
    exp = Espion()
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, forge.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 0
    assert statut(depot, forge) is S.VALIDEE
    assert journal.actions() == ["envoi_refuse"]


def test_envoyer_brouillon_introuvable(depot, journal) -> None:
    exp = Espion()
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, "inconnu", maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 0


@pytest.mark.parametrize("etat", [S.REJETEE, S.ENVOYEE, S.EN_COURS])
def test_envoyer_refuse_les_autres_statuts(depot, journal, etat) -> None:
    br = faire_brouillon(statut=etat, valide_par="Alice", valide_le=MAINTENANT)
    depot.sauver_brouillon(br)
    exp = Espion()
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, br.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 0
    assert statut(depot, br) is etat


@pytest.mark.parametrize("par", ["", "   ", "systeme", "Systeme", "SYSTEME", "système", "AUTO", " robot ", "cron", "bot", "Bot\t", "system", "Automatique"])
def test_valider_refuse_un_acteur_non_humain(depot, journal, b, par) -> None:
    with pytest.raises(EnvoiNonValide):
        valider(depot, journal, b.id_relance, par, maintenant=MAINTENANT)
    assert statut(depot, b) is S.BROUILLON
    assert depot.charger_brouillon(b.id_relance).valide_par == ""
    assert journal.actions() == ["envoi_refuse"]


def test_valider_refuse_un_par_qui_n_est_pas_une_chaine(depot, journal, b) -> None:
    with pytest.raises(EnvoiNonValide):
        valider(depot, journal, b.id_relance, None, maintenant=MAINTENANT)  # type: ignore[arg-type]
    assert statut(depot, b) is S.BROUILLON


def test_valider_renseigne_valide_par_valide_le_et_journalise(depot, journal, b) -> None:
    v = valider(depot, journal, b.id_relance, "  Alice Martin ", maintenant=MAINTENANT)
    assert v.statut is S.VALIDEE and v.valide_par == "Alice Martin" and v.valide_le == MAINTENANT
    assert depot.charger_brouillon(b.id_relance) == v
    (e,) = journal.entrees
    assert (e.acteur, e.action, e.objet) == ("Alice Martin", "valide", b.id_relance)
    assert e.details["par"] == "Alice Martin"


@pytest.mark.parametrize("etat", [S.VALIDEE, S.EN_COURS, S.ENVOYEE, S.REJETEE])
def test_valider_seul_un_brouillon_peut_etre_valide(depot, journal, etat) -> None:
    br = faire_brouillon(statut=etat, valide_par="Alice", valide_le=MAINTENANT)
    depot.sauver_brouillon(br)
    with pytest.raises(EnvoiNonValide):
        valider(depot, journal, br.id_relance, "Bob", maintenant=MAINTENANT)
    assert depot.charger_brouillon(br.id_relance) == br  # intact : valide_par n'est pas ecrase
    assert journal.actions() == ["envoi_refuse"]


def test_valider_introuvable(depot, journal) -> None:
    with pytest.raises(EnvoiNonValide):
        valider(depot, journal, "inconnu", "Alice", maintenant=MAINTENANT)


def test_valider_sans_audit_ne_valide_pas(depot, b) -> None:
    """Si le journal ne peut pas ecrire, la validation est annulee (pas de validation non tracee)."""
    with pytest.raises(OSError):
        valider(depot, FauxJournal(echec_sur="valide"), b.id_relance, "Alice", maintenant=MAINTENANT)
    assert statut(depot, b) is S.BROUILLON


def test_horodatage_naif_refuse_partout(depot, journal, b) -> None:
    naif = dt.datetime(2026, 10, 6, 10, 0)
    with pytest.raises(ValueError):
        valider(depot, journal, b.id_relance, "Alice", maintenant=naif)
    with pytest.raises(ValueError):
        rejeter(depot, journal, b.id_relance, "Alice", "motif", maintenant=naif)
    v = valider(depot, journal, b.id_relance, "Alice", maintenant=MAINTENANT)
    exp = Espion()
    with pytest.raises(ValueError):
        envoyer(depot, journal, exp, v.id_relance, maintenant=naif, creneau_ok=lambda _: True)
    assert exp.n == 0 and statut(depot, v) is S.VALIDEE


def test_un_seul_site_d_emission_dans_le_code() -> None:
    """Lecture du code : `.envoyer(` n'est appele que dans la fonction `envoyer`, apres
    les controles ; le seul autre `def envoyer` est la methode de l'adaptateur."""
    arbre = ast.parse(Path(envoi.__file__).read_text(encoding="utf-8"))
    appels: list[str] = []

    class Visiteur(ast.NodeVisitor):
        def __init__(self) -> None:
            self.pile: list[str] = []

        def visit_FunctionDef(self, n: ast.FunctionDef) -> None:
            self.pile.append(n.name)
            self.generic_visit(n)
            self.pile.pop()

        def visit_Call(self, n: ast.Call) -> None:
            if isinstance(n.func, ast.Attribute) and n.func.attr == "envoyer":
                appels.append(self.pile[-1] if self.pile else "<module>")
            self.generic_visit(n)

    Visiteur().visit(arbre)
    assert appels == ["envoyer"]


# ---------------------------------------------------------------------------
# CA-05 : jamais deux envois du meme message
# ---------------------------------------------------------------------------


def test_deux_appels_successifs_un_seul_envoi(depot, journal, valide) -> None:
    exp = Espion()
    envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 1
    assert statut(depot, valide) is S.ENVOYEE
    assert len(depot.historique_envois()) == 1


def test_un_brouillon_en_cours_n_est_jamais_renvoye(depot, journal) -> None:
    br = faire_brouillon(statut=S.EN_COURS, valide_par="Alice", valide_le=MAINTENANT)
    depot.sauver_brouillon(br)
    exp = Espion()
    for _ in range(3):
        with pytest.raises(EnvoiNonValide):
            envoyer(depot, journal, exp, br.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 0
    assert statut(depot, br) is S.EN_COURS


def test_appels_en_boucle_avec_toutes_les_issues_un_seul_succes(depot, journal, valide) -> None:
    """Quoi qu'il arrive, au plus une emission REUSSIE (et jamais deux emissions apres incertitude)."""
    exp = Espion(leve=ErreurEnvoiCertaine("refus"))
    for _ in range(2):
        with pytest.raises(ErreurEnvoiCertaine):
            envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 2  # reessais autorises : rien n'etait parti
    exp.leve = None
    envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    for _ in range(3):
        with pytest.raises(EnvoiNonValide):
            envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 3
    assert len(depot.historique_envois()) == 1


def test_etat_en_cours_persiste_avant_l_emission_et_reentrance_refusee(depot, journal, valide) -> None:
    vu: dict[str, Any] = {}

    def pendant(brouillon: Brouillon) -> None:
        vu["statut_remis"] = brouillon.statut
        vu["statut_depot"] = statut(depot, valide)
        vu["audit"] = journal.actions()
        # Un second appel pendant l'emission (autre processus, double clic) est refuse.
        with pytest.raises(EnvoiNonValide):
            envoyer(depot, journal, interne, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)

    interne = Espion()
    exp = Espion(pendant=pendant)
    envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert vu["statut_remis"] is S.EN_COURS and vu["statut_depot"] is S.EN_COURS
    assert vu["audit"][-1] == "envoi_demarre"
    assert exp.n == 1 and interne.n == 0


def test_journal_indisponible_avant_emission_rien_ne_part(depot, valide) -> None:
    exp = Espion()
    with pytest.raises(OSError):
        envoyer(depot, FauxJournal(echec_sur="envoi_demarre"), exp, valide.id_relance,
                maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 0
    assert statut(depot, valide) is S.VALIDEE  # transaction annulee


# ---------------------------------------------------------------------------
# Les trois issues d'un envoi
# ---------------------------------------------------------------------------


def test_succes(depot, journal, valide) -> None:
    exp = Espion()
    r = envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert r.statut is S.ENVOYEE and r.envoye_le == MAINTENANT and r.valide_par == "Alice Martin"
    assert depot.charger_brouillon(valide.id_relance) == r
    (envoi_enregistre,) = depot.historique_envois()
    assert envoi_enregistre == EnvoiRelance(
        id_relance=valide.id_relance,
        destinataire="client@exemple.fr",
        date_envoi=dt.date(2026, 10, 6),
        dossiers=("D1",),
        references=("OP1", "OP2"),
    )
    assert envois_incertains(depot) == []
    assert exp.appels[0].statut is S.EN_COURS


def test_date_envoi_est_la_date_locale_du_fuseau(depot, journal, valide) -> None:
    # 23h30 UTC le 6 = 01h30 le 7 a Paris.
    tard = dt.datetime(2026, 10, 6, 23, 30, tzinfo=dt.timezone.utc)
    envoyer(depot, journal, Espion(), valide.id_relance, maintenant=tard, creneau_ok=lambda _: True)
    assert depot.historique_envois()[0].date_envoi == dt.date(2026, 10, 7)


def test_erreur_certaine_retour_a_validee_et_exception_relancee(depot, journal, valide) -> None:
    exp = Espion(leve=ErreurEnvoiCertaine("serveur refuse la connexion"))
    with pytest.raises(ErreurEnvoiCertaine):
        envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert statut(depot, valide) is S.VALIDEE
    assert depot.historique_envois() == []
    assert envois_incertains(depot) == []
    assert journal.actions()[-2:] == ["envoi_demarre", "envoi_echec_certain"]
    assert "envoi_incertain" not in journal.actions()


@pytest.mark.parametrize(
    "erreur",
    [TimeoutError("lu 30 s sans reponse"), ConnectionResetError("coupure"), ErreurEnvoi("etat inconnu"),
     RuntimeError("bug"), Coupure()],
    ids=["timeout", "reset", "ErreurEnvoi", "runtime", "BaseException"],
)
def test_toute_autre_exception_laisse_en_cours_et_ne_renvoie_jamais(depot, journal, valide, erreur) -> None:
    exp = Espion(leve=erreur)
    with pytest.raises(type(erreur)):
        envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert statut(depot, valide) is S.EN_COURS
    assert [x.id_relance for x in envois_incertains(depot)] == [valide.id_relance]
    assert depot.historique_envois() == []
    assert journal.actions()[-1] == "envoi_incertain"
    assert journal.entrees[-1].details["erreur"] == type(erreur).__name__

    # Nouvel appel : refuse, et l'expediteur n'est pas rappele.
    exp.leve = None
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 1
    assert statut(depot, valide) is S.EN_COURS


def test_message_parti_mais_enregistrement_echoue_reste_en_cours(depot, journal, valide) -> None:
    depot.echec_enregistrer_envoi = OSError("base verrouillee")
    exp = Espion()
    with pytest.raises(OSError):
        envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 1
    assert statut(depot, valide) is S.EN_COURS  # transaction annulee : ni ENVOYEE ni envoi enregistre
    assert envois_incertains(depot)[0].id_relance == valide.id_relance
    assert journal.actions()[-1] == "envoi_incertain"
    depot.echec_enregistrer_envoi = None
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 1


def test_journal_en_panne_ne_masque_pas_l_exception_d_origine(depot, valide) -> None:
    exp = Espion(leve=TimeoutError("delai"))
    with pytest.raises(TimeoutError) as info:
        envoyer(depot, FauxJournal(echec_sur="envoi_incertain"), exp, valide.id_relance,
                maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert any("envoi_incertain" in n for n in info.value.__notes__)
    assert statut(depot, valide) is S.EN_COURS


def test_journal_en_panne_apres_succes_l_etat_reste_exact(depot, valide) -> None:
    exp = Espion()
    with pytest.raises(OSError):
        envoyer(depot, FauxJournal(echec_sur="envoi_effectue"), exp, valide.id_relance,
                maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert statut(depot, valide) is S.ENVOYEE
    assert exp.n == 1


# ---------------------------------------------------------------------------
# Creneau
# ---------------------------------------------------------------------------


def test_hors_creneau_rien_ne_part_et_reste_validee(depot, journal, valide) -> None:
    exp = Espion()
    appels: list[dt.datetime] = []

    def non(instant: dt.datetime) -> bool:
        appels.append(instant)
        return False

    with pytest.raises(EnvoiHorsCreneau):
        envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=non)
    assert appels == [MAINTENANT]
    assert exp.n == 0
    assert statut(depot, valide) is S.VALIDEE
    assert "envoi_demarre" not in journal.actions()
    # Et l'envoi reste possible dans le creneau.
    envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 1


def test_creneau_par_defaut_vient_de_cadence_et_n_est_importe_qu_a_l_appel(depot, journal, valide, monkeypatch) -> None:
    """Sans `creneau_ok`, on appelle `cadence.heure_d_envoi_valide(maintenant, fuseau)`."""
    import types

    recu: list[tuple[Any, ...]] = []
    faux = types.ModuleType("rapprochement.cadence")
    faux.heure_d_envoi_valide = lambda m, f="Europe/Paris": recu.append((m, f)) or False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rapprochement.cadence", faux)
    monkeypatch.setattr("rapprochement.cadence", faux, raising=False)
    exp = Espion()
    with pytest.raises(EnvoiHorsCreneau):
        envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, fuseau="Europe/Lisbon")
    assert recu == [(MAINTENANT, "Europe/Lisbon")]
    assert exp.n == 0 and statut(depot, valide) is S.VALIDEE


def test_creneau_par_defaut_indisponible_echoue_ferme(depot, journal, valide, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "rapprochement.cadence", None)  # import impossible
    exp = Espion()
    with pytest.raises(ImportError):
        envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT)
    assert exp.n == 0 and statut(depot, valide) is S.VALIDEE


# ---------------------------------------------------------------------------
# Rejet
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("depart", [S.BROUILLON, S.VALIDEE])
def test_rejeter_depuis_brouillon_ou_validee(depot, journal, depart) -> None:
    br = faire_brouillon(statut=depart, valide_par="Alice" if depart is S.VALIDEE else "")
    depot.sauver_brouillon(br)
    r = rejeter(depot, journal, br.id_relance, " Bob ", "  client en litige  ", maintenant=MAINTENANT)
    assert r.statut is S.REJETEE and statut(depot, br) is S.REJETEE
    (e,) = journal.entrees
    assert (e.acteur, e.action) == ("Bob", "rejete")
    assert e.details["par"] == "Bob" and e.details["motif"] == "client en litige"
    # Un brouillon rejete ne part plus.
    exp = Espion()
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, br.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 0


@pytest.mark.parametrize("etat", [S.EN_COURS, S.ENVOYEE, S.REJETEE])
def test_rejeter_refuse_les_autres_statuts(depot, journal, etat) -> None:
    br = faire_brouillon(statut=etat, valide_par="Alice")
    depot.sauver_brouillon(br)
    with pytest.raises(EnvoiNonValide):
        rejeter(depot, journal, br.id_relance, "Bob", "motif", maintenant=MAINTENANT)
    assert statut(depot, br) is etat
    assert journal.actions() == ["envoi_refuse"]


@pytest.mark.parametrize("motif", ["", "   ", "\n\t", None])
def test_rejeter_exige_un_motif(depot, journal, b, motif) -> None:
    with pytest.raises(EnvoiNonValide):
        rejeter(depot, journal, b.id_relance, "Bob", motif, maintenant=MAINTENANT)  # type: ignore[arg-type]
    assert statut(depot, b) is S.BROUILLON and journal.actions() == ["envoi_refuse"]


@pytest.mark.parametrize("par", ["", "systeme", "BOT", " cron "])
def test_rejeter_exige_un_humain(depot, journal, b, par) -> None:
    with pytest.raises(EnvoiNonValide):
        rejeter(depot, journal, b.id_relance, par, "motif", maintenant=MAINTENANT)
    assert statut(depot, b) is S.BROUILLON and journal.actions() == ["envoi_refuse"]


def test_rejeter_introuvable(depot, journal) -> None:
    with pytest.raises(EnvoiNonValide):
        rejeter(depot, journal, "inconnu", "Bob", "motif", maintenant=MAINTENANT)


# ---------------------------------------------------------------------------
# Journal : chaque etape, bon acteur
# ---------------------------------------------------------------------------


def test_journal_de_bout_en_bout(depot, journal, b) -> None:
    valider(depot, journal, b.id_relance, "Alice Martin", maintenant=MAINTENANT)
    envoyer(depot, journal, Espion(), b.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert [(e.acteur, e.action, e.objet) for e in journal.entrees] == [
        ("Alice Martin", "valide", b.id_relance),
        ("systeme", "envoi_demarre", b.id_relance),
        ("systeme", "envoi_effectue", b.id_relance),
    ]
    # L'humain responsable figure dans chaque etape d'envoi, et l'acteur systeme ne valide jamais.
    assert all(e.details.get("par") == "Alice Martin" or e.details.get("valide_par") == "Alice Martin"
               for e in journal.entrees)
    assert journal.entrees[-1].details["identifiant_externe"] == f"ext-{b.id_relance}"


def test_journal_rejet_et_incertain(depot, journal) -> None:
    a = faire_brouillon(id_relance="aaaa")
    c = faire_brouillon(id_relance="cccc")
    depot.sauver_brouillon(a)
    depot.sauver_brouillon(c)
    rejeter(depot, journal, "aaaa", "Bob", "doublon manuel", maintenant=MAINTENANT)
    valider(depot, journal, "cccc", "Alice", maintenant=MAINTENANT)
    with pytest.raises(TimeoutError):
        envoyer(depot, journal, Espion(leve=TimeoutError("t")), "cccc", maintenant=MAINTENANT,
                creneau_ok=lambda _: True)
    assert [(e.acteur, e.action) for e in journal.entrees] == [
        ("Bob", "rejete"), ("Alice", "valide"), ("systeme", "envoi_demarre"), ("systeme", "envoi_incertain"),
    ]
    assert journal.entrees[-1].details["valide_par"] == "Alice"


# ---------------------------------------------------------------------------
# ExpediteurDossier
# ---------------------------------------------------------------------------


def en_cours(**kw: Any) -> Brouillon:
    return faire_brouillon(statut=S.EN_COURS, valide_par="Alice", valide_le=MAINTENANT, **kw)


def relire(chemin: Path) -> email.message.Message:
    with open(chemin, "rb") as f:
        return email.message_from_binary_file(f)


def test_eml_valide_et_relisible(tmp_path) -> None:
    br = en_cours()
    ident = ExpediteurDossier(tmp_path / "sortie").envoyer(br)
    chemin = tmp_path / "sortie" / f"{br.id_relance}.eml"
    m = relire(chemin)
    assert m["To"] == "client@exemple.fr"
    assert m["Subject"] == "Pieces manquantes - septembre"
    assert m["From"] == "relances@cabinet.invalid"
    assert m["Message-ID"] == f"<{br.id_relance}@relances.invalid>" == ident
    assert m["Date"] and email.utils.parsedate_to_datetime(m["Date"]) == MAINTENANT
    assert not m.defects
    assert m.get_payload(decode=True).decode("utf-8").replace("\r\n", "\n").strip() == br.corps
    assert [p.name for p in chemin.parent.iterdir()] == [chemin.name]  # pas de fichier temporaire


def test_message_id_deterministe(tmp_path) -> None:
    br = en_cours()
    a = ExpediteurDossier(tmp_path / "a").envoyer(br)
    b2 = ExpediteurDossier(tmp_path / "b").envoyer(br)
    assert a == b2


def test_eml_accents_et_corps_multilignes(tmp_path) -> None:
    br = en_cours(objet="Relance : pièces manquantes de décembre", corps="Bonjour,\n\nMerci d'envoyer la facture « Électricité ».\n")
    ExpediteurDossier(tmp_path).envoyer(br)
    with open(tmp_path / f"{br.id_relance}.eml", "rb") as f:
        m = email.message_from_binary_file(f, policy=email.policy.default)
    assert m["Subject"] == br.objet
    assert m.get_content().replace("\r\n", "\n") == br.corps


def test_refuse_d_ecraser_un_eml_existant(tmp_path) -> None:
    br = en_cours()
    exp = ExpediteurDossier(tmp_path)
    exp.envoyer(br)
    chemin = tmp_path / f"{br.id_relance}.eml"
    avant = chemin.read_bytes()
    with pytest.raises(ErreurEnvoi) as info:
        exp.envoyer(dataclasses.replace(br, corps="autre texte"))
    assert not isinstance(info.value, ErreurEnvoiCertaine)  # incertain : le brouillon resterait EN_COURS
    assert chemin.read_bytes() == avant
    assert [p.name for p in tmp_path.iterdir()] == [chemin.name]


@pytest.mark.parametrize(
    "objet",
    ["Subject: x\nBcc: pirate@x.fr", "x\r\nBcc: pirate@x.fr", "x\rBcc: pirate@x.fr", "x Bcc: pirate@x.fr",
     "x\x00y", "x\x85Bcc: pirate@x.fr"],
)
def test_objet_avec_saut_de_ligne_n_injecte_aucun_entete(tmp_path, objet) -> None:
    br = en_cours(objet=objet)
    with pytest.raises(ErreurEnvoiCertaine):
        ExpediteurDossier(tmp_path).envoyer(br)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "dest",
    ["client@exemple.fr\nBcc: pirate@x.fr", "a@x.fr, b@y.fr", "a@x.fr;b@y.fr", "Nom <a@x.fr>", "sans-arobase", "", "a@x.fr b@y.fr"],
)
def test_destinataire_unique_et_sans_injection(tmp_path, dest) -> None:
    with pytest.raises(ErreurEnvoiCertaine):
        ExpediteurDossier(tmp_path).envoyer(en_cours(destinataire=dest))
    assert list(tmp_path.iterdir()) == []


def test_corps_avec_faux_entetes_reste_du_corps(tmp_path) -> None:
    corps = "Bonjour\nBcc: pirate@x.fr\r\nSubject: autre\n\nFin"
    br = en_cours(corps=corps)
    ExpediteurDossier(tmp_path).envoyer(br)
    m = relire(tmp_path / f"{br.id_relance}.eml")
    assert m["Bcc"] is None and m.get_all("Subject") == [br.objet] and m.get_all("To") == [br.destinataire]
    assert "Bcc: pirate@x.fr" in m.get_payload(decode=True).decode()


@pytest.mark.parametrize("ident", ["../evasion", "a/b", "a\\b", "", ".", "..", ".cache", "a\nb", "x" * 200])
def test_id_relance_dangereux_refuse(tmp_path, ident) -> None:
    sortie = tmp_path / "sortie"
    with pytest.raises(ErreurEnvoiCertaine):
        ExpediteurDossier(sortie).envoyer(en_cours(id_relance=ident))
    assert not any(p.suffix == ".eml" for p in tmp_path.rglob("*"))


@pytest.mark.parametrize("etat", [S.BROUILLON, S.VALIDEE, S.ENVOYEE, S.REJETEE])
def test_l_adaptateur_n_emet_que_un_brouillon_en_cours(tmp_path, etat) -> None:
    """Defense en profondeur : meme appele directement, il ne contourne pas la validation."""
    br = faire_brouillon(statut=etat, valide_par="Alice")
    with pytest.raises(ErreurEnvoiCertaine):
        ExpediteurDossier(tmp_path).envoyer(br)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("par", ["", "systeme", "AUTO"])
def test_l_adaptateur_refuse_un_en_cours_sans_humain(tmp_path, par) -> None:
    with pytest.raises(ErreurEnvoiCertaine):
        ExpediteurDossier(tmp_path).envoyer(faire_brouillon(statut=S.EN_COURS, valide_par=par))
    assert list(tmp_path.iterdir()) == []


def test_cycle_complet_avec_l_adaptateur_dossier(depot, journal, b, tmp_path) -> None:
    exp = ExpediteurDossier(tmp_path / "boite")
    valider(depot, journal, b.id_relance, "Alice", maintenant=MAINTENANT)
    envoyer(depot, journal, exp, b.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, b.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert len(list((tmp_path / "boite").glob("*.eml"))) == 1


def test_eml_deja_present_apres_validee_laisse_en_cours(depot, journal, b, tmp_path) -> None:
    """Un .eml deja la (envoi precedent peut-etre abouti) : ErreurEnvoi simple -> EN_COURS, pas de rejeu."""
    exp = ExpediteurDossier(tmp_path)
    (tmp_path / f"{b.id_relance}.eml").write_bytes(b"deja la")
    valider(depot, journal, b.id_relance, "Alice", maintenant=MAINTENANT)
    with pytest.raises(ErreurEnvoi):
        envoyer(depot, journal, exp, b.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert statut(depot, b) is S.EN_COURS
    assert (tmp_path / f"{b.id_relance}.eml").read_bytes() == b"deja la"


class DepotPermissif(FauxDepot):
    """Depot sans controle de transition : le module doit refuser par lui-meme."""

    def maj_brouillon(self, b: Brouillon) -> None:
        self.brouillons[b.id_relance] = b


@pytest.mark.parametrize("etat", [S.BROUILLON, S.EN_COURS, S.ENVOYEE, S.REJETEE])
def test_le_refus_ne_depend_pas_des_controles_du_depot(journal, etat) -> None:
    depot = DepotPermissif()
    br = faire_brouillon(statut=etat, valide_par="Alice", valide_le=MAINTENANT)
    depot.sauver_brouillon(br)
    exp = Espion()
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, br.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 0 and depot.charger_brouillon(br.id_relance) == br


# ---------------------------------------------------------------------------
# Acteurs : une seule source de verite (etats)
# ---------------------------------------------------------------------------


def test_la_garde_humaine_vient_de_etats() -> None:
    assert envoi.est_acteur_humain is etats.est_acteur_humain
    assert not any("ACTEURS" in nom for nom in vars(envoi))  # aucune liste dupliquee


# ---------------------------------------------------------------------------
# envoi_refuse : toute tentative refusee laisse une trace, avant l'exception
# ---------------------------------------------------------------------------


def refus(journal: FauxJournal) -> list[FausseEntree]:
    return [e for e in journal.entrees if e.action == "envoi_refuse"]


def test_refus_valider_acteur_non_humain(depot, journal, b) -> None:
    with pytest.raises(EnvoiNonValide):
        valider(depot, journal, b.id_relance, " Robot ", maintenant=MAINTENANT)
    (e,) = refus(journal)
    assert (e.acteur, e.objet) == ("systeme", b.id_relance)
    assert e.details["operation"] == "valider" and e.details["par"] == " Robot "
    assert e.details["type"] == "EnvoiNonValide" and e.details["raison"]


def test_refus_valider_acteur_vide_ne_fait_pas_echouer_le_journal(depot, journal, b) -> None:
    """Un `par` vide serait refuse par le journal comme acteur : l'acteur du refus est "systeme"."""
    with pytest.raises(EnvoiNonValide):
        valider(depot, journal, b.id_relance, "", maintenant=MAINTENANT)
    (e,) = refus(journal)
    assert e.acteur == "systeme" and e.details["par"] == ""


def test_refus_rejeter(depot, journal, b) -> None:
    with pytest.raises(EnvoiNonValide):
        rejeter(depot, journal, b.id_relance, "Bob", "", maintenant=MAINTENANT)
    (e,) = refus(journal)
    assert e.details["operation"] == "rejeter" and e.details["par"] == "Bob"
    assert "motif" in e.details["raison"]


def test_refus_envoyer_non_valide_introuvable_et_double(depot, journal, valide) -> None:
    exp = Espion()
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, "inconnu", maintenant=MAINTENANT, creneau_ok=lambda _: True)
    envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    r = refus(journal)
    assert [x.objet for x in r] == ["inconnu", valide.id_relance]
    assert all(x.acteur == "systeme" and x.details["operation"] == "envoyer" for x in r)
    assert journal.actions()[-1] == "envoi_refuse"
    assert exp.n == 1


def test_refus_hors_creneau_journalise(depot, journal, valide) -> None:
    exp = Espion()
    with pytest.raises(EnvoiHorsCreneau):
        envoyer(depot, journal, exp, valide.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: False)
    (e,) = refus(journal)
    assert e.details["type"] == "EnvoiHorsCreneau" and "creneau" in e.details["raison"]
    assert e.details["operation"] == "envoyer"
    assert exp.n == 0 and statut(depot, valide) is S.VALIDEE


def test_refus_valide_forge_sans_humain_journalise(depot, journal) -> None:
    forge = faire_brouillon(statut=S.VALIDEE, valide_par="systeme")
    depot.sauver_brouillon(forge)
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, Espion(), forge.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert "valide_par" in refus(journal)[0].details["raison"]


def test_le_refus_est_ecrit_avant_l_exception(depot, b) -> None:
    ordre: list[str] = []

    class Journal(FauxJournal):
        def ecrire(self, *a: Any, **k: Any) -> FausseEntree:
            ordre.append("journal")
            return super().ecrire(*a, **k)

    with pytest.raises(EnvoiNonValide):
        try:
            valider(depot, Journal(), b.id_relance, "bot", maintenant=MAINTENANT)
        finally:
            ordre.append("exception_vue")
    assert ordre == ["journal", "exception_vue"]


def test_journal_en_panne_ne_masque_pas_le_refus(depot, b) -> None:
    with pytest.raises(EnvoiNonValide) as info:
        valider(depot, FauxJournal(echec_sur="envoi_refuse"), b.id_relance, "bot", maintenant=MAINTENANT)
    assert any("envoi_refuse" in n for n in info.value.__notes__)


def test_aucun_refus_journalise_pour_un_succes(depot, journal, b) -> None:
    valider(depot, journal, b.id_relance, "Alice", maintenant=MAINTENANT)
    envoyer(depot, journal, Espion(), b.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert refus(journal) == []


# ---------------------------------------------------------------------------
# trancher_envoi_incertain : seul moyen de sortir de EN_COURS
# ---------------------------------------------------------------------------


@pytest.fixture
def incertain(depot, journal, valide) -> Brouillon:
    with pytest.raises(TimeoutError):
        envoyer(depot, journal, Espion(leve=TimeoutError("t")), valide.id_relance,
                maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert statut(depot, valide) is S.EN_COURS
    return valide


LENDEMAIN = MAINTENANT + dt.timedelta(days=1, hours=1)


def test_trancher_parti_confirme_l_envoi(depot, journal, incertain) -> None:
    r = trancher_envoi_incertain(depot, journal, incertain.id_relance, " Alice Martin ", parti=True,
                                 maintenant=LENDEMAIN)
    assert r.statut is S.ENVOYEE and r.envoye_le == LENDEMAIN and r.valide_par == "Alice Martin"
    assert depot.charger_brouillon(incertain.id_relance) == r
    (e,) = depot.historique_envois()
    assert e.id_relance == incertain.id_relance and e.date_envoi == dt.date(2026, 10, 7)
    assert envois_incertains(depot) == []
    last = journal.entrees[-1]
    assert (last.acteur, last.action) == ("Alice Martin", "envoi_confirme_par_humain")
    assert last.details["par"] == "Alice Martin"
    # Envoye : on ne renvoie plus jamais.
    exp = Espion()
    with pytest.raises(EnvoiNonValide):
        envoyer(depot, journal, exp, incertain.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 0


def test_trancher_non_parti_rend_la_main(depot, journal, incertain) -> None:
    r = trancher_envoi_incertain(depot, journal, incertain.id_relance, "Alice", parti=False,
                                 maintenant=LENDEMAIN)
    assert r.statut is S.VALIDEE and r.envoye_le is None and r.valide_par == "Alice Martin"
    assert statut(depot, incertain) is S.VALIDEE
    assert depot.historique_envois() == [] and envois_incertains(depot) == []
    last = journal.entrees[-1]
    assert (last.acteur, last.action) == ("Alice", "envoi_declare_non_parti")
    exp = Espion()
    envoyer(depot, journal, exp, incertain.id_relance, maintenant=MAINTENANT, creneau_ok=lambda _: True)
    assert exp.n == 1 and statut(depot, incertain) is S.ENVOYEE


@pytest.mark.parametrize("parti", [True, False])
@pytest.mark.parametrize("par", ["", "   ", "systeme", " robot ", "AUTO", "cron", "bot", "system", "automatique"])
def test_trancher_exige_un_humain(depot, journal, incertain, par, parti) -> None:
    avant = len(journal.entrees)
    with pytest.raises(EnvoiNonValide):
        trancher_envoi_incertain(depot, journal, incertain.id_relance, par, parti=parti, maintenant=LENDEMAIN)
    assert statut(depot, incertain) is S.EN_COURS
    assert depot.historique_envois() == []
    assert [e.action for e in journal.entrees[avant:]] == ["envoi_refuse"]
    assert journal.entrees[-1].details["operation"] == "trancher_envoi_incertain"


@pytest.mark.parametrize("etat", [S.BROUILLON, S.VALIDEE, S.ENVOYEE, S.REJETEE])
@pytest.mark.parametrize("parti", [True, False])
def test_trancher_refuse_tout_brouillon_non_en_cours(depot, journal, etat, parti) -> None:
    br = faire_brouillon(statut=etat, valide_par="Alice", valide_le=MAINTENANT)
    depot.sauver_brouillon(br)
    with pytest.raises(EnvoiNonValide):
        trancher_envoi_incertain(depot, journal, br.id_relance, "Bob", parti=parti, maintenant=LENDEMAIN)
    assert depot.charger_brouillon(br.id_relance) == br
    assert depot.historique_envois() == []
    assert journal.actions() == ["envoi_refuse"]


def test_trancher_introuvable(depot, journal) -> None:
    with pytest.raises(EnvoiNonValide):
        trancher_envoi_incertain(depot, journal, "inconnu", "Bob", parti=True, maintenant=LENDEMAIN)
    assert journal.actions() == ["envoi_refuse"]


def test_trancher_deux_fois_la_seconde_est_refusee(depot, journal, incertain) -> None:
    trancher_envoi_incertain(depot, journal, incertain.id_relance, "Alice", parti=True, maintenant=LENDEMAIN)
    with pytest.raises(EnvoiNonValide):
        trancher_envoi_incertain(depot, journal, incertain.id_relance, "Alice", parti=False, maintenant=LENDEMAIN)
    assert statut(depot, incertain) is S.ENVOYEE
    assert len(depot.historique_envois()) == 1


@pytest.mark.parametrize("parti", [None, 1, "oui"])
def test_trancher_exige_un_booleen_explicite(depot, journal, incertain, parti) -> None:
    with pytest.raises(TypeError):
        trancher_envoi_incertain(depot, journal, incertain.id_relance, "Alice", parti=parti,  # type: ignore[arg-type]
                                 maintenant=LENDEMAIN)
    assert statut(depot, incertain) is S.EN_COURS


def test_trancher_horodatage_naif_refuse(depot, journal, incertain) -> None:
    with pytest.raises(ValueError):
        trancher_envoi_incertain(depot, journal, incertain.id_relance, "Alice", parti=True,
                                 maintenant=dt.datetime(2026, 10, 7, 10, 0))
    assert statut(depot, incertain) is S.EN_COURS


@pytest.mark.parametrize("parti,action", [(True, "envoi_confirme_par_humain"), (False, "envoi_declare_non_parti")])
def test_trancher_sans_audit_ne_change_rien(depot, valide, parti, action) -> None:
    depot.maj_brouillon(dataclasses.replace(valide, statut=S.EN_COURS))
    with pytest.raises(OSError):
        trancher_envoi_incertain(depot, FauxJournal(echec_sur=action), valide.id_relance, "Alice",
                                 parti=parti, maintenant=LENDEMAIN)
    assert statut(depot, valide) is S.EN_COURS
    assert depot.historique_envois() == []


def test_trancher_parti_enregistrement_en_echec_reste_en_cours(depot, journal, incertain) -> None:
    depot.echec_enregistrer_envoi = OSError("base verrouillee")
    with pytest.raises(OSError):
        trancher_envoi_incertain(depot, journal, incertain.id_relance, "Alice", parti=True, maintenant=LENDEMAIN)
    assert statut(depot, incertain) is S.EN_COURS
    assert "envoi_confirme_par_humain" not in journal.actions()
