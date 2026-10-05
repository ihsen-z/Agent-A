"""Tests de cadence.py et des ajouts de relances.py (contrat 4.5)."""

from __future__ import annotations

import datetime as dt
import random
import re
import sys
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rapprochement import cadence
from rapprochement.cadence import (
    JALON_ESCALADE,
    JALONS,
    Planification,
    RelancePlanifiee,
    ajouter_jours_ouvres,
    est_ouvre,
    heure_d_envoi_valide,
    jours_feries,
    jours_ouvres_entre,
    paques,
    planifier,
)
from rapprochement.modeles import (
    Dossier,
    EnvoiRelance,
    EtatPiece,
    Evenement,
    OperationBancaire,
    PieceAttendue,
    Rapprochement,
    Sens,
    Statut,
    StatutBrouillon,
    identifiant_relance,
)
from rapprochement.relances import (
    Relance,
    construire_brouillon,
    construire_relance,
    construire_toutes,
)

D = dt.date
PARIS = ZoneInfo("Europe/Paris")
LUNDI = D(2026, 10, 5)          # lundi ouvre, sert de "aujourd'hui" par defaut


# ---------------------------------------------------------------------------
# Fabriques
# ---------------------------------------------------------------------------


def dossier(code="D1", email="a@client.fr", **kw) -> Dossier:
    base = dict(
        code=code, raison_sociale=f"Societe {code}", email_contact=email,
        nom_contact="M. Dupont", jour_echeance_tva=15, ton_relance="courtois",
    )
    base.update(kw)
    return Dossier(**base)


def piece(ref="OP1", dos="D1", etat=EtatPiece.ATTENDUE, nb=0, premiere=None, **kw) -> PieceAttendue:
    base = dict(
        reference=ref, dossier=dos, periode="2026-09", montant=Decimal("120.50"),
        date_operation=D(2026, 9, 10), libelle=f"PRLV FOURNISSEUR {ref}",
        etat=etat, nb_relances=nb, date_premiere_demande=premiere,
    )
    base.update(kw)
    return PieceAttendue(**base)


def demandee(ref="OP1", dos="D1", nb=1, premiere=D(2026, 9, 28), **kw) -> PieceAttendue:
    return piece(ref, dos, EtatPiece.DEMANDEE, nb, premiere,
                 date_derniere_relance=premiere, **kw)


def envoi(dest="a@client.fr", jour=D(2026, 9, 28)) -> EnvoiRelance:
    return EnvoiRelance("id", dest, jour, ("D1",), ("OP1",))


def dossiers_de(*ds: Dossier) -> dict[str, Dossier]:
    return {d.code: d for d in ds}


# ---------------------------------------------------------------------------
# (0) zoneinfo
# ---------------------------------------------------------------------------


def test_zoneinfo_europe_paris_disponible():
    assert str(ZoneInfo("Europe/Paris")) == "Europe/Paris"


# ---------------------------------------------------------------------------
# (1) Jours feries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "annee, paques_attendu",
    [
        (2024, D(2024, 3, 31)), (2025, D(2025, 4, 20)), (2026, D(2026, 4, 5)),
        (2027, D(2027, 3, 28)), (2028, D(2028, 4, 16)), (2038, D(2038, 4, 25)),
        (1961, D(1961, 4, 2)),
    ],
)
def test_paques_connues(annee, paques_attendu):
    assert paques(annee) == paques_attendu


@pytest.mark.parametrize(
    "annee, p, lundi, ascension, pentecote",
    [
        (2026, D(2026, 4, 5), D(2026, 4, 6), D(2026, 5, 14), D(2026, 5, 25)),
        (2027, D(2027, 3, 28), D(2027, 3, 29), D(2027, 5, 6), D(2027, 5, 17)),
        (2028, D(2028, 4, 16), D(2028, 4, 17), D(2028, 5, 25), D(2028, 6, 5)),
    ],
)
def test_jours_feries_complets(annee, p, lundi, ascension, pentecote):
    attendu = {
        D(annee, 1, 1), lundi, D(annee, 5, 1), D(annee, 5, 8), ascension, pentecote,
        D(annee, 7, 14), D(annee, 8, 15), D(annee, 11, 1), D(annee, 11, 11), D(annee, 12, 25),
    }
    assert jours_feries(annee) == attendu
    assert len(jours_feries(annee)) == 11
    assert ascension.weekday() == 3 and pentecote.weekday() == 0 and lundi.weekday() == 0
    assert p.weekday() == 6
    assert isinstance(jours_feries(annee), frozenset)


def test_paques_n_est_pas_ferie_mais_son_lundi_oui():
    assert D(2026, 4, 5) not in jours_feries(2026)
    assert D(2026, 4, 6) in jours_feries(2026)


# ---------------------------------------------------------------------------
# (2) Jours ouvres
# ---------------------------------------------------------------------------


def test_est_ouvre():
    assert est_ouvre(D(2026, 10, 5))            # lundi
    assert est_ouvre(D(2026, 10, 9))            # vendredi
    assert not est_ouvre(D(2026, 10, 10))       # samedi
    assert not est_ouvre(D(2026, 10, 11))       # dimanche
    assert not est_ouvre(D(2026, 5, 14))        # Ascension (jeudi)
    assert not est_ouvre(D(2026, 4, 6))         # lundi de Paques
    assert not est_ouvre(D(2026, 12, 25))       # Noel (vendredi)
    assert est_ouvre(D(2026, 12, 24))


def test_ajouter_jours_ouvres_week_end():
    assert ajouter_jours_ouvres(D(2026, 10, 2), 1) == D(2026, 10, 5)     # vendredi -> lundi
    assert ajouter_jours_ouvres(D(2026, 10, 5), 5) == D(2026, 10, 12)
    assert ajouter_jours_ouvres(D(2026, 10, 3), 1) == D(2026, 10, 5)     # depuis un samedi
    assert ajouter_jours_ouvres(D(2026, 10, 5), 0) == D(2026, 10, 5)


def test_ajouter_jours_ouvres_ferie():
    # jeudi 7 mai, vendredi 8 mai ferie, week-end : +1 -> lundi 11 mai
    assert ajouter_jours_ouvres(D(2026, 5, 7), 1) == D(2026, 5, 11)
    assert ajouter_jours_ouvres(D(2026, 4, 3), 1) == D(2026, 4, 7)       # lundi de Paques saute


def test_ajouter_jours_ouvres_pont_de_deux_feries():
    # 2024 : mercredi 8 mai (victoire) et jeudi 9 mai (Ascension) feries consecutifs
    assert D(2024, 5, 8) in jours_feries(2024) and D(2024, 5, 9) in jours_feries(2024)
    assert ajouter_jours_ouvres(D(2024, 5, 7), 1) == D(2024, 5, 10)
    assert ajouter_jours_ouvres(D(2024, 5, 7), 2) == D(2024, 5, 13)
    # Noel 2026 (vendredi) + week-end
    assert ajouter_jours_ouvres(D(2026, 12, 24), 1) == D(2026, 12, 28)


def test_ajouter_jours_ouvres_negatif():
    assert ajouter_jours_ouvres(D(2026, 10, 5), -1) == D(2026, 10, 2)
    assert ajouter_jours_ouvres(D(2026, 5, 11), -1) == D(2026, 5, 7)


def test_jours_ouvres_entre_bornes():
    assert jours_ouvres_entre(D(2026, 10, 5), D(2026, 10, 5)) == 0
    assert jours_ouvres_entre(D(2026, 10, 5), D(2026, 10, 6)) == 1      # debut exclu, fin incluse
    assert jours_ouvres_entre(D(2026, 10, 2), D(2026, 10, 5)) == 1      # vendredi -> lundi
    assert jours_ouvres_entre(D(2026, 10, 3), D(2026, 10, 4)) == 0      # samedi -> dimanche
    assert jours_ouvres_entre(D(2026, 10, 5), D(2026, 10, 2)) == 0      # ordre inverse : 0
    assert jours_ouvres_entre(D(2026, 10, 5), D(2026, 10, 12)) == 5
    assert jours_ouvres_entre(D(2026, 5, 7), D(2026, 5, 11)) == 1       # ferie + week-end
    assert jours_ouvres_entre(D(2024, 5, 7), D(2024, 5, 10)) == 1       # pont de deux feries
    # debut ferie : exclu de toute facon
    assert jours_ouvres_entre(D(2026, 5, 14), D(2026, 5, 15)) == 1


def test_jours_ouvres_entre_coherent_avec_force_brute_et_avec_ajouter():
    rnd = random.Random(7)
    for _ in range(400):
        debut = D(2026, 1, 1) + dt.timedelta(days=rnd.randrange(0, 1200))
        fin = debut + dt.timedelta(days=rnd.randrange(0, 90))
        brute = sum(
            1 for i in range(1, (fin - debut).days + 1)
            if est_ouvre(debut + dt.timedelta(days=i))
        )
        assert jours_ouvres_entre(debut, fin) == brute
        n = rnd.randrange(0, 30)
        assert jours_ouvres_entre(debut, ajouter_jours_ouvres(debut, n)) == n


# ---------------------------------------------------------------------------
# (3) Creneau d'envoi
# ---------------------------------------------------------------------------


def paris(*args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=PARIS)


@pytest.mark.parametrize(
    "h, m, attendu",
    [(7, 59, False), (8, 0, True), (12, 0, True), (17, 59, True), (18, 0, False), (23, 0, False)],
)
@pytest.mark.parametrize("jour", [D(2026, 1, 12), D(2026, 7, 13)], ids=["hiver", "ete"])
def test_creneau_bornes_heure_locale(h, m, attendu, jour):
    assert heure_d_envoi_valide(paris(jour.year, jour.month, jour.day, h, m)) is attendu


def test_creneau_samedi_dimanche_et_ferie():
    assert not heure_d_envoi_valide(paris(2026, 1, 17, 10, 0))      # samedi
    assert not heure_d_envoi_valide(paris(2026, 1, 18, 10, 0))      # dimanche
    assert not heure_d_envoi_valide(paris(2026, 5, 14, 10, 0))      # Ascension
    assert not heure_d_envoi_valide(paris(2026, 7, 14, 10, 0))      # 14 juillet


def test_creneau_datetime_naif_leve_valueerror():
    with pytest.raises(ValueError):
        heure_d_envoi_valide(dt.datetime(2026, 1, 12, 10, 0))


def test_creneau_decalage_utc_differe_hiver_ete():
    hiver = paris(2026, 1, 12, 8, 0)
    ete = paris(2026, 7, 13, 8, 0)
    assert hiver.utcoffset() == dt.timedelta(hours=1)
    assert ete.utcoffset() == dt.timedelta(hours=2)
    utc = dt.timezone.utc
    # janvier : 08:00 locale = 07:00 UTC
    assert not heure_d_envoi_valide(dt.datetime(2026, 1, 12, 6, 59, tzinfo=utc))
    assert heure_d_envoi_valide(dt.datetime(2026, 1, 12, 7, 0, tzinfo=utc))
    assert heure_d_envoi_valide(dt.datetime(2026, 1, 12, 16, 59, tzinfo=utc))
    assert not heure_d_envoi_valide(dt.datetime(2026, 1, 12, 17, 0, tzinfo=utc))
    # juillet : 08:00 locale = 06:00 UTC
    assert not heure_d_envoi_valide(dt.datetime(2026, 7, 13, 5, 59, tzinfo=utc))
    assert heure_d_envoi_valide(dt.datetime(2026, 7, 13, 6, 0, tzinfo=utc))
    assert heure_d_envoi_valide(dt.datetime(2026, 7, 13, 15, 59, tzinfo=utc))
    assert not heure_d_envoi_valide(dt.datetime(2026, 7, 13, 16, 0, tzinfo=utc))
    # 07:00 UTC en juillet = 09:00 locale : valide, alors que 07:00 UTC en janvier = 08:00
    assert heure_d_envoi_valide(dt.datetime(2026, 7, 13, 7, 0, tzinfo=utc))


def test_creneau_jour_local_et_non_jour_utc():
    utc = dt.timezone.utc
    # vendredi 23:30 UTC = samedi 00:30 a Paris
    assert not heure_d_envoi_valide(dt.datetime(2026, 1, 16, 23, 30, tzinfo=utc))
    # lundi 05:30 UTC en juillet = 07:30 a Paris : trop tot
    assert not heure_d_envoi_valide(dt.datetime(2026, 7, 13, 5, 30, tzinfo=utc))


def test_creneau_autre_fuseau():
    utc = dt.timezone.utc
    # 14:00 UTC en janvier = 09:00 a New York
    assert heure_d_envoi_valide(dt.datetime(2026, 1, 12, 14, 0, tzinfo=utc), "America/New_York")
    assert not heure_d_envoi_valide(dt.datetime(2026, 1, 12, 14, 0, tzinfo=utc), "Asia/Tokyo")


# ---------------------------------------------------------------------------
# (4) Regles de planifier
# ---------------------------------------------------------------------------


def test_constantes_du_contrat():
    assert JALONS == (0, 3, 7)
    assert JALON_ESCALADE == 14
    assert cadence.FENETRE_HEBDO_JOURS == 7


def test_plafond_aligne_sur_etats_si_present():
    etats = pytest.importorskip("rapprochement.etats")
    assert cadence.PLAFOND_RELANCES == etats.PLAFOND_RELANCES


def test_piece_jamais_demandee_due_tout_de_suite():
    plan = planifier([piece("OP1")], dossiers_de(dossier()), [], LUNDI)
    assert plan.relances == [RelancePlanifiee("a@client.fr", ("D1",), ("OP1",), 1)]
    assert plan.reportees == [] and plan.transitions == [] and plan.bloquees == []


def test_jalon_3_jours_ouvres():
    # premiere demande lundi 28/09 : +3 jours ouvres = jeudi 01/10
    p = demandee(nb=1, premiere=D(2026, 9, 28))
    ds = dossiers_de(dossier())
    assert planifier([p], ds, [], D(2026, 9, 30)).relances == []
    plan = planifier([p], ds, [], D(2026, 10, 1))
    assert len(plan.relances) == 1 and plan.relances[0].niveau == 2


def test_jalon_3_jours_ouvres_avec_ferie():
    # demande jeudi 07/05/2026, vendredi 08/05 ferie : T+3 ouvres = jeudi 14/05 est ferie aussi
    p = demandee(nb=1, premiere=D(2026, 5, 7))
    ds = dossiers_de(dossier())
    # lun 11, mar 12, mer 13 : 3 jours ouvres ecoules
    assert planifier([p], ds, [], D(2026, 5, 12)).relances == []
    assert len(planifier([p], ds, [], D(2026, 5, 13)).relances) == 1


def test_jalon_7_jours_ouvres():
    p = demandee(nb=2, premiere=D(2026, 9, 28))
    ds = dossiers_de(dossier())
    assert planifier([p], ds, [], D(2026, 10, 6)).relances == []      # 6 ouvres
    plan = planifier([p], ds, [], D(2026, 10, 7))                     # 7 ouvres
    assert len(plan.relances) == 1 and plan.relances[0].niveau == 3


def test_escalade_a_14_jours_ouvres():
    p = demandee(nb=3, premiere=D(2026, 9, 28))
    ds = dossiers_de(dossier())
    avant = planifier([p], ds, [], D(2026, 10, 15))                   # 13 ouvres
    assert avant.relances == [] and avant.transitions == []
    plan = planifier([p], ds, [], D(2026, 10, 16))                    # 14 ouvres
    assert plan.relances == []
    assert plan.transitions == [("OP1", Evenement.DELAI_DEPASSE)]


def test_plafond_atteint_escalade_sans_attendre():
    p = demandee(nb=4, premiere=D(2026, 10, 1))
    plan = planifier([p], dossiers_de(dossier()), [], LUNDI)
    assert plan.transitions == [("OP1", Evenement.DELAI_DEPASSE)]
    assert plan.relances == []


def test_jamais_plus_de_trois_envois_planifies():
    # nb_relances = 3 : plus aucun jalon de relance, quelle que soit l'anciennete
    p = demandee(nb=3, premiere=D(2026, 1, 5))
    plan = planifier([p], dossiers_de(dossier()), [], LUNDI)
    assert plan.relances == []


def promise(jour: D, ref="OP1") -> PieceAttendue:
    return piece(ref, etat=EtatPiece.PROMISE, nb=1, premiere=D(2026, 9, 21), date_promesse=jour)


def test_promesse_echue_donne_transition_sans_relance():
    # promesse mercredi 30/09 : +2 jours ouvres = vendredi 02/10 ; le 05/10 est apres
    plan = planifier([promise(D(2026, 9, 30))], dossiers_de(dossier()), [], LUNDI)
    assert plan.transitions == [("OP1", Evenement.DATE_PROMISE_DEPASSEE)]
    assert plan.relances == [] and plan.reportees == []


@pytest.mark.parametrize(
    "promesse, aujourdhui, emise",
    [
        (D(2026, 9, 30), D(2026, 9, 30), False),   # le jour promis
        (D(2026, 9, 30), D(2026, 10, 1), False),   # +1 ouvre
        (D(2026, 9, 30), D(2026, 10, 2), False),   # +2 ouvres : pas encore
        (D(2026, 9, 30), D(2026, 10, 3), True),    # lendemain de la limite (samedi)
        (D(2026, 10, 2), D(2026, 10, 5), False),   # promesse vendredi : +1 ouvre (lundi)
        (D(2026, 10, 2), D(2026, 10, 6), False),   # +2 ouvres (mardi) : pas encore
        (D(2026, 10, 2), D(2026, 10, 7), True),    # +3 ouvres : oui, a travers le week-end
        (D(2026, 10, 3), D(2026, 10, 6), False),   # promesse un samedi : limite mardi 06/10
        (D(2026, 10, 3), D(2026, 10, 7), True),
    ],
)
def test_promesse_borne_date_promise_plus_2_jours_ouvres(promesse, aujourdhui, emise):
    plan = planifier([promise(promesse)], dossiers_de(dossier()), [], aujourdhui)
    assert plan.transitions == ([("OP1", Evenement.DATE_PROMISE_DEPASSEE)] if emise else [])
    assert plan.relances == []


def test_promesse_avec_ferie_dans_le_delai():
    # promesse jeudi 07/05/2026 : vendredi 08/05 ferie, week-end -> limite = mardi 12/05
    assert ajouter_jours_ouvres(D(2026, 5, 7), 2) == D(2026, 5, 12)
    ds = dossiers_de(dossier())
    assert planifier([promise(D(2026, 5, 7))], ds, [], D(2026, 5, 12)).transitions == []
    assert planifier([promise(D(2026, 5, 7))], ds, [], D(2026, 5, 13)).transitions != []


def test_promesse_sans_date_est_bloquee():
    p = piece("OP1", etat=EtatPiece.PROMISE, nb=1, premiere=D(2026, 9, 21))
    plan = planifier([p], dossiers_de(dossier()), [], LUNDI)
    assert plan.bloquees == ["OP1"] and plan.relances == [] and plan.transitions == []


def test_piece_bloquee_listee_et_jamais_relancee():
    ds = dossiers_de(dossier())
    bloquee = piece("OP1", bloquee=True, motif_blocage="litige")
    libre = piece("OP2")
    plan = planifier([bloquee, libre], ds, [], LUNDI)
    assert plan.bloquees == ["OP1"]
    assert plan.relances[0].references == ("OP2",)
    # bloquee et echue : reste bloquee, pas de transition
    p = demandee(nb=3, premiere=D(2026, 1, 5), bloquee=True)
    plan = planifier([p], ds, [], LUNDI)
    assert plan.bloquees == ["OP1"] and plan.transitions == []


@pytest.mark.parametrize(
    "etat",
    [EtatPiece.ESCALADEE, EtatPiece.RECUE, EtatPiece.VALIDEE, EtatPiece.HORS_PERIMETRE,
     EtatPiece.CLOSE_SANS_SUITE],
)
def test_etats_jamais_relances(etat):
    p = piece("OP1", etat=etat, nb=1, premiere=D(2026, 1, 5))
    plan = planifier([p], dossiers_de(dossier()), [], LUNDI)
    assert plan == Planification([], [], [], [])


def test_terminale_marquee_bloquee_n_est_pas_listee():
    p = piece("OP1", etat=EtatPiece.VALIDEE, bloquee=True)
    assert planifier([p], dossiers_de(dossier()), [], LUNDI).bloquees == []


def test_demandee_sans_date_premiere_demande_est_bloquee_sans_exception():
    p = piece("OP1", etat=EtatPiece.DEMANDEE, nb=1)     # incoherent
    plan = planifier([p], dossiers_de(dossier()), [], LUNDI)
    assert plan.bloquees == ["OP1"] and plan.relances == []


def test_regroupement_deux_dossiers_meme_destinataire():
    ds = dossiers_de(dossier("D1", "Compta@Client.fr"), dossier("D2", " compta@client.fr "))
    pieces = [piece("OP2", "D2"), piece("OP1", "D1"), demandee("OP3", "D1", nb=1, premiere=D(2026, 9, 28))]
    plan = planifier(pieces, ds, [], LUNDI)
    assert plan.relances == [
        RelancePlanifiee("compta@client.fr", ("D1", "D2"), ("OP1", "OP2", "OP3"), 2)
    ]


def test_regroupement_destinataires_distincts_tries():
    ds = dossiers_de(dossier("D1", "z@client.fr"), dossier("D2", "b@client.fr"))
    plan = planifier([piece("OP1", "D1"), piece("OP2", "D2")], ds, [], LUNDI)
    assert [r.destinataire for r in plan.relances] == ["b@client.fr", "z@client.fr"]


def test_relais_utilise_email_relais_et_regroupe_avec_les_dossiers_du_relais():
    ds = dossiers_de(
        dossier("D1", "client1@x.fr", circuit="C-RELAIS", email_relais="relais@cabinet.fr"),
        dossier("D2", "client2@x.fr", circuit="C-RELAIS", email_relais="relais@cabinet.fr"),
    )
    plan = planifier([piece("OP1", "D1"), piece("OP2", "D2")], ds, [], LUNDI)
    assert len(plan.relances) == 1 and plan.relances[0].destinataire == "relais@cabinet.fr"


def test_destinataire_vide_est_bloque():
    ds = dossiers_de(dossier("D1", circuit="C-RELAIS", email_relais=""))
    plan = planifier([piece("OP1", "D1")], ds, [], LUNDI)
    assert plan.bloquees == ["OP1"] and plan.relances == [] and plan.reportees == []


def test_limite_hebdomadaire_6_jours_reporte_7_jours_autorise():
    ds = dossiers_de(dossier())
    p = [piece("OP1")]
    plan = planifier(p, ds, [envoi(jour=D(2026, 9, 29))], LUNDI)        # il y a 6 jours
    assert plan.relances == []
    assert plan.reportees == [cadence.Report("a@client.fr", ("OP1",), "LIMITE_HEBDOMADAIRE")]
    plan = planifier(p, ds, [envoi(jour=D(2026, 9, 28))], LUNDI)        # il y a 7 jours
    assert len(plan.relances) == 1 and plan.reportees == []


def test_limite_hebdomadaire_porte_sur_le_destinataire_tous_dossiers():
    ds = dossiers_de(dossier("D1", "a@client.fr"), dossier("D2", "a@client.fr"))
    hist = [EnvoiRelance("x", "A@client.fr", D(2026, 10, 2), ("D1",), ("OPX",))]
    plan = planifier([piece("OP2", "D2")], ds, hist, LUNDI)
    assert plan.relances == [] and plan.reportees[0].raison == "LIMITE_HEBDOMADAIRE"


def test_limite_hebdomadaire_ignore_les_autres_destinataires_et_envoi_futur_bloque():
    ds = dossiers_de(dossier())
    assert len(planifier([piece("OP1")], ds, [envoi("autre@x.fr", D(2026, 10, 2))], LUNDI).relances) == 1
    # envoi date dans le futur (donnee incoherente) : prudence, on reporte
    assert planifier([piece("OP1")], ds, [envoi(jour=D(2026, 10, 9))], LUNDI).relances == []


def test_limite_hebdomadaire_gagne_sur_le_jalon_et_les_transitions_restent():
    ds = dossiers_de(dossier())
    p = [demandee("OP1", nb=1, premiere=D(2026, 9, 28)),
         demandee("OP2", nb=3, premiere=D(2026, 9, 1))]
    plan = planifier(p, ds, [envoi(jour=D(2026, 10, 2))], LUNDI)
    assert plan.relances == []
    assert plan.reportees[0].references == ("OP1",)
    assert plan.transitions == [("OP2", Evenement.DELAI_DEPASSE)]


def test_jour_non_ouvre_reporte_tout_mais_garde_les_transitions():
    ds = dossiers_de(dossier("D1", "a@x.fr"), dossier("D2", "b@x.fr"))
    p = [piece("OP1", "D1"), piece("OP2", "D2"),
         demandee("OP3", "D1", nb=3, premiere=D(2026, 1, 5))]
    for jour in (D(2026, 10, 3), D(2026, 10, 4), D(2026, 5, 14)):
        plan = planifier(p, ds, [], jour)
        assert plan.relances == []
        assert [(r.destinataire, r.references, r.raison) for r in plan.reportees] == [
            ("a@x.fr", ("OP1",), "JOUR_NON_OUVRE"),
            ("b@x.fr", ("OP2",), "JOUR_NON_OUVRE"),
        ]
        assert plan.transitions == [("OP3", Evenement.DELAI_DEPASSE)]


def test_dossier_inconnu_ne_leve_pas():
    ds = dossiers_de(dossier("D1"))
    plan = planifier([piece("OP1", "D1"), piece("OP2", "FANTOME"), piece("OP3", "FANTOME")], ds, [], LUNDI)
    assert [r.references for r in plan.relances] == [("OP1",)]
    assert plan.reportees == [cadence.Report("", ("OP2", "OP3"), "DOSSIER_INCONNU")]
    assert planifier([piece("OP2", "FANTOME")], {}, [], LUNDI).reportees[0].raison == "DOSSIER_INCONNU"


def test_references_ambigues_sont_bloquees():
    ds = dossiers_de(dossier("D1", "a@x.fr"), dossier("D2", "b@x.fr"))
    plan = planifier([piece("OPX", "D1"), piece("OPX", "D2"), piece("OP2", "D1")], ds, [], LUNDI)
    assert plan.bloquees == ["OPX"]
    assert [r.references for r in plan.relances] == [("OP2",)]
    # meme cle, contenu different
    p1, p2 = piece("OP1"), piece("OP1", montant=Decimal("1"))
    plan = planifier([p1, p2], dossiers_de(dossier()), [], LUNDI)
    assert plan.bloquees == ["OP1"] and plan.relances == []
    # meme piece fournie deux fois : dedoublonnee
    plan = planifier([p1, p1], dossiers_de(dossier()), [], LUNDI)
    assert plan.relances[0].references == ("OP1",)


def test_planifier_ne_leve_jamais_sur_donnees_incoherentes():
    ds = dossiers_de(dossier())
    cas = [
        piece("A", etat=EtatPiece.DEMANDEE, nb=-3),
        piece("B", etat=EtatPiece.DEMANDEE, nb=99, premiere=D(2030, 1, 1)),
        piece("C", etat=EtatPiece.PROMISE),
        piece("D", premiere=D(2099, 1, 1), nb=7),
        demandee("E", nb=1, premiere=D(2099, 1, 1)),
    ]
    plan = planifier(cas, ds, [envoi(jour=D(1900, 1, 1))], LUNDI)
    assert isinstance(plan, Planification)


def test_niveau_est_1_plus_le_plus_grand_nb_relances():
    ds = dossiers_de(dossier())
    p = [piece("OP1"), demandee("OP2", nb=2, premiere=D(2026, 9, 1))]
    assert planifier(p, ds, [], LUNDI).relances[0].niveau == 3


# --- effet du garde-fou hebdomadaire sur la cadence (simulation) -------------


def simuler(depart: D, jours: int = 40):
    """Rejoue planifier chaque jour apres une demande initiale envoyee `depart`."""
    ds = dossiers_de(dossier())
    p = demandee("OP1", nb=1, premiere=depart)
    hist = [envoi(jour=depart)]
    envois, escalade = [depart], None
    for i in range(1, jours + 1):
        jour = depart + dt.timedelta(days=i)
        plan = planifier([p], ds, hist, jour)
        if plan.transitions and escalade is None:
            escalade = jour
        if plan.relances:
            envois.append(jour)
            hist.append(envoi(jour=jour))
            p = demandee("OP1", nb=p.nb_relances + 1, premiere=depart)
    return envois, escalade


def test_cadence_reelle_le_garde_fou_gagne_sur_les_jalons():
    envois, escalade = simuler(D(2026, 9, 28))      # lundi
    # demande, +7 jours calendaires (pas T+3 ouvres), +7 encore, puis escalade a T+14 ouvres
    assert envois == [D(2026, 9, 28), D(2026, 10, 5), D(2026, 10, 12)]
    assert escalade == D(2026, 10, 16)
    assert (envois[1] - envois[0]).days == 7


def test_cadence_reelle_premiere_relance_jamais_avant_7_jours_calendaires():
    for depart in (D(2026, 9, 25), D(2026, 9, 28), D(2026, 10, 1), D(2026, 12, 21)):
        envois, _ = simuler(depart)
        assert (envois[1] - envois[0]).days >= 7


def test_cadence_reelle_ferie_le_jour_de_la_fenetre():
    # 07/07/2026 + 7 = 14/07 (ferie) : la relance part le lendemain
    envois, _ = simuler(D(2026, 7, 7))
    assert envois[1] == D(2026, 7, 15)


# ---------------------------------------------------------------------------
# (5) Determinisme
# ---------------------------------------------------------------------------


def jeu_complexe():
    ds = dossiers_de(
        dossier("D1", "a@x.fr"), dossier("D2", "a@x.fr"), dossier("D3", "b@x.fr"),
        dossier("D4", "c@x.fr"),
    )
    p = [
        piece("OP1", "D1"), piece("OP2", "D2"), piece("OP3", "D3"),
        demandee("OP4", "D3", nb=2, premiere=D(2026, 9, 28)),
        demandee("OP5", "D4", nb=3, premiere=D(2026, 9, 1)),
        piece("OP6", "D4", etat=EtatPiece.PROMISE, date_promesse=D(2026, 9, 28), nb=1,
              premiere=D(2026, 9, 1)),
        piece("OP7", "D4", bloquee=True), piece("OP8", "INCONNU"),
        piece("OP9", "D1", etat=EtatPiece.RECUE),
    ]
    hist = [envoi("c@x.fr", D(2026, 10, 1))]
    return p, ds, hist


def test_determinisme_deux_appels_identiques():
    p, ds, hist = jeu_complexe()
    assert planifier(p, ds, hist, LUNDI) == planifier(p, ds, hist, LUNDI)


def test_determinisme_ordre_d_entree_sans_effet():
    p, ds, hist = jeu_complexe()
    reference = planifier(p, ds, hist, LUNDI)
    rnd = random.Random(42)
    for _ in range(30):
        melange = p[:]
        rnd.shuffle(melange)
        assert planifier(melange, ds, hist, LUNDI) == reference
        assert planifier(iter(melange), ds, hist, LUNDI) == reference
    assert [r.destinataire for r in reference.relances] == ["a@x.fr", "b@x.fr"]


# ---------------------------------------------------------------------------
# (6) CA-07 : aucune ligne sans operation bancaire source
# ---------------------------------------------------------------------------

LIGNE = re.compile(r"^  - (\d{2}/\d{2}/\d{4})  (.+) EUR  -  (.*)$")


def formater_montant(m: Decimal) -> str:
    return f"{m:,.2f}".replace(",", " ").replace(".", ",")


def libelle_court(libelle: str) -> str:
    return libelle if len(libelle) <= 48 else libelle[:45] + "..."


def lignes_du_corps(corps: str) -> list[tuple[str, str, str]]:
    sortie = []
    for ligne in corps.split("\n"):
        if ligne.startswith("  - "):
            m = LIGNE.match(ligne)
            assert m, f"ligne reclamee mal formee : {ligne!r}"
            sortie.append(m.groups())
    return sortie


def attendu_depuis(p: PieceAttendue) -> tuple[str, str, str]:
    return (p.date_operation.strftime("%d/%m/%Y"), formater_montant(p.montant),
            libelle_court(p.libelle))


def jeu_aleatoire(graine: int):
    rnd = random.Random(graine)
    nb_dossiers = rnd.randint(1, 4)
    boites = ["a@x.fr", "b@x.fr"]
    ds = {}
    for i in range(nb_dossiers):
        code = f"D{i}"
        ds[code] = dossier(code, rnd.choice(boites), jour_echeance_tva=rnd.choice([1, 8, 15, 24]),
                           ton_relance=rnd.choice(["courtois", "direct"]),
                           nom_contact=rnd.choice(["", "M. Dupont", "Mme Durand"]))
    pieces = []
    for j in range(rnd.randint(1, 25)):
        etat = rnd.choice([EtatPiece.ATTENDUE, EtatPiece.ATTENDUE, EtatPiece.DEMANDEE,
                           EtatPiece.RECUE, EtatPiece.ESCALADEE])
        premiere = D(2026, 9, 1) + dt.timedelta(days=rnd.randrange(0, 30))
        libelle = "".join(rnd.choice("ABCDEFGH 0123-/") for _ in range(rnd.randint(3, 80))).strip() or "X"
        pieces.append(PieceAttendue(
            reference=f"OP{graine}-{j}", dossier=rnd.choice(list(ds)), periode="2026-09",
            montant=Decimal(rnd.randint(1, 5_000_000)) / 100,
            date_operation=D(2026, 8, 1) + dt.timedelta(days=rnd.randrange(0, 60)),
            libelle=libelle, etat=etat,
            nb_relances=0 if etat is EtatPiece.ATTENDUE else rnd.randint(1, 2),
            date_premiere_demande=None if etat is EtatPiece.ATTENDUE else premiere,
        ))
    return pieces, ds


@pytest.mark.parametrize("graine", range(60))
def test_ca07_chaque_ligne_a_une_piece_source_et_inversement(graine):
    pieces, ds = jeu_aleatoire(graine)
    par_ref = {p.reference: p for p in pieces}
    plan = planifier(pieces, ds, [], LUNDI)
    for rel in plan.relances:
        b = construire_brouillon(rel, par_ref, ds, LUNDI)
        lignes = lignes_du_corps(b.corps)
        sources = [attendu_depuis(par_ref[r]) for r in b.references]
        # chaque ligne a une source ...
        for ligne in lignes:
            assert ligne in sources
        # ... et chaque source donne exactement une ligne : ni ajout, ni perte, ni doublon
        assert sorted(lignes) == sorted(sources)
        assert len(lignes) == len(b.references) == len(set(b.references))
        # aucune piece hors planification dans le texte
        autres = [p for r, p in par_ref.items() if r not in b.references]
        corps_lignes = [l for l in b.corps.split("\n") if l.startswith("  - ")]
        for p in autres:
            if attendu_depuis(p) in sources:
                continue          # meme triplet qu'une piece reclamee : indiscernable
            assert not any(attendu_depuis(p)[2] in l and attendu_depuis(p)[1] in l
                           for l in corps_lignes)


def test_ca07_sur_au_moins_un_jeu_regroupant_plusieurs_dossiers():
    vus_multi = False
    for graine in range(60):
        pieces, ds = jeu_aleatoire(graine)
        par_ref = {p.reference: p for p in pieces}
        for rel in planifier(pieces, ds, [], LUNDI).relances:
            vus_multi |= len(rel.dossiers) > 1
    assert vus_multi


def test_ca07_reference_sans_piece_source_est_refusee():
    ds = dossiers_de(dossier())
    rel = RelancePlanifiee("a@client.fr", ("D1",), ("OP1", "FANTOME"), 1)
    with pytest.raises(ValueError):
        construire_brouillon(rel, {"OP1": piece("OP1")}, ds, LUNDI)


def test_ca07_refus_de_donnees_incoherentes():
    ds = dossiers_de(dossier("D1", "a@x.fr"), dossier("D2", "b@x.fr"))
    pieces = {"OP1": piece("OP1", "D1"), "OP2": piece("OP2", "D2"),
              "OP3": piece("OP3", "D1", etat=EtatPiece.VALIDEE),
              "OP4": piece("OP4", "D1", bloquee=True)}
    ok = RelancePlanifiee("a@x.fr", ("D1",), ("OP1",), 1)
    construire_brouillon(ok, pieces, ds, LUNDI)
    mauvais = [
        RelancePlanifiee("a@x.fr", ("D1",), ("OP1", "OP2"), 1),        # piece d'un autre dossier
        RelancePlanifiee("a@x.fr", ("D1", "D2"), ("OP1", "OP2"), 1),   # D2 n'est pas a cette adresse
        RelancePlanifiee("b@x.fr", ("D1",), ("OP1",), 1),              # mauvais destinataire
        RelancePlanifiee("a@x.fr", ("INCONNU",), ("OP1",), 1),
        RelancePlanifiee("a@x.fr", ("D1",), ("OP1", "OP1"), 1),        # doublon
        RelancePlanifiee("a@x.fr", ("D1",), (), 1),
        RelancePlanifiee("a@x.fr", ("D1",), ("OP3",), 1),              # terminale
        RelancePlanifiee("a@x.fr", ("D1",), ("OP4",), 1),              # bloquee
        RelancePlanifiee("a@x.fr", ("D1", "D2"), ("OP1",), 1),         # dossier sans piece
    ]
    for rel in mauvais:
        with pytest.raises(ValueError):
            construire_brouillon(rel, pieces, ds, LUNDI)


# ---------------------------------------------------------------------------
# (7) construire_brouillon
# ---------------------------------------------------------------------------


def rapprochement_de(p: PieceAttendue) -> Rapprochement:
    return Rapprochement(
        OperationBancaire(p.reference, p.dossier, p.date_operation, p.libelle, p.montant, Sens.DEBIT),
        Statut.MANQUANT,
    )


def test_brouillon_un_dossier_equivalent_a_construire_relance_niveau_1():
    d = dossier("D1", jour_echeance_tva=24, ton_relance="direct")
    ps = [piece("OP2", montant=Decimal("1234.50"), date_operation=D(2026, 9, 3)),
          piece("OP1", libelle="X" * 70)]
    rel = RelancePlanifiee("a@client.fr", ("D1",), ("OP1", "OP2"), 1)
    b = construire_brouillon(rel, {p.reference: p for p in ps}, {"D1": d}, LUNDI)
    attendu = construire_relance(d, [rapprochement_de(p) for p in ps], niveau=1,
                                 date_demande=LUNDI, aujourdhui=LUNDI)
    assert b.corps == attendu.corps and b.objet == attendu.objet
    assert b.destinataire == attendu.destinataire == "a@client.fr"
    assert "1 234,50 EUR" in b.corps and "..." in b.corps


def test_brouillon_un_dossier_equivalent_a_construire_relance_niveau_2_et_urgent():
    d = dossier("D1", jour_echeance_tva=8)          # echeance 8/10 : J-3, ton urgent
    ps = [demandee("OP1", nb=1, premiere=D(2026, 9, 28)), demandee("OP2", nb=1, premiere=D(2026, 9, 25))]
    rel = RelancePlanifiee("a@client.fr", ("D1",), ("OP1", "OP2"), 2)
    b = construire_brouillon(rel, {p.reference: p for p in ps}, {"D1": d}, LUNDI)
    attendu = construire_relance(d, [rapprochement_de(p) for p in ps], niveau=2,
                                 date_demande=D(2026, 9, 25), aujourdhui=LUNDI)
    assert b.corps == attendu.corps and b.objet == attendu.objet
    assert "25/09/2026" in b.corps and b.objet.startswith("Relance")
    assert "avant le 08/10/2026" in b.corps


def test_brouillon_plusieurs_dossiers_une_section_par_dossier():
    d1 = dossier("D1", "a@x.fr", raison_sociale="Alpha SARL")
    d2 = dossier("D2", "a@x.fr", raison_sociale="Beta SAS")
    ps = {p.reference: p for p in [
        piece("OP1", "D1", libelle="LIBELLE ALPHA"), piece("OP2", "D2", libelle="LIBELLE BETA 1"),
        piece("OP3", "D2", libelle="LIBELLE BETA 2", date_operation=D(2026, 9, 20))]}
    rel = RelancePlanifiee("a@x.fr", ("D1", "D2"), ("OP1", "OP2", "OP3"), 1)
    b = construire_brouillon(rel, ps, {"D1": d1, "D2": d2}, LUNDI)
    lignes = b.corps.split("\n")
    assert lignes.count("Alpha SARL") == 1 and lignes.count("Beta SAS") == 1
    i_a, i_b = lignes.index("Alpha SARL"), lignes.index("Beta SAS")
    assert i_a < i_b
    section_a, section_b = lignes[i_a:i_b], lignes[i_b:]
    assert sum(l.startswith("  - ") for l in section_a) == 1
    assert sum(l.startswith("  - ") for l in section_b) == 2
    assert "LIBELLE ALPHA" in "\n".join(section_a) and "LIBELLE ALPHA" not in "\n".join(section_b)
    assert "LIBELLE BETA 1" in "\n".join(section_b)
    assert b.dossiers == ("D1", "D2") and b.references == ("OP1", "OP2", "OP3")
    assert b.objet == "Justificatifs - 3 justificatif(s) manquant(s) - 2 dossiers"
    assert b.corps.startswith("Bonjour M. Dupont,\n")


def test_brouillon_plusieurs_dossiers_contacts_differents_et_pied_urgent():
    d1 = dossier("D1", "a@x.fr", nom_contact="Alice", jour_echeance_tva=15)
    d2 = dossier("D2", "a@x.fr", nom_contact="Bob", jour_echeance_tva=8)
    ps = {"OP1": piece("OP1", "D1"), "OP2": piece("OP2", "D2")}
    rel = RelancePlanifiee("a@x.fr", ("D1", "D2"), ("OP1", "OP2"), 2)
    b = construire_brouillon(rel, ps, {"D1": d1, "D2": d2}, LUNDI)
    assert b.corps.startswith("Bonjour,\n")
    assert "avant le 08/10/2026" in b.corps
    assert b.objet.startswith("Relance - ")


def test_brouillon_identifiant_statut_et_metadonnees():
    d = dossier("D1")
    rel = RelancePlanifiee("a@client.fr", ("D1",), ("OP2", "OP1"), 2)
    ps = {"OP1": demandee("OP1"), "OP2": demandee("OP2")}
    b = construire_brouillon(rel, ps, {"D1": d}, LUNDI)
    assert b.id_relance == identifiant_relance("a@client.fr", ("OP1", "OP2"), 2, LUNDI)
    assert b.statut is StatutBrouillon.BROUILLON
    assert b.valide_par == "" and b.valide_le is None and b.envoye_le is None
    assert b.references == ("OP1", "OP2") and b.cree_le == LUNDI and b.niveau == 2
    # rejouable : meme entree, meme brouillon
    assert construire_brouillon(rel, ps, {"D1": d}, LUNDI) == b
    # un autre jour : autre identifiant
    assert construire_brouillon(rel, ps, {"D1": d}, D(2026, 10, 6)).id_relance != b.id_relance


def test_de_bout_en_bout_planifier_puis_construire():
    p, ds, hist = jeu_complexe()
    par_ref = {x.reference: x for x in p}
    plan = planifier(p, ds, hist, LUNDI)
    brouillons = [construire_brouillon(r, par_ref, ds, LUNDI) for r in plan.relances]
    assert [b.destinataire for b in brouillons] == ["a@x.fr", "b@x.fr"]
    assert len({b.id_relance for b in brouillons}) == 2


# ---------------------------------------------------------------------------
# (8) Fonctions existantes de relances.py inchangees
# ---------------------------------------------------------------------------


def test_construire_relance_existante_inchangee():
    d = dossier("D1", jour_echeance_tva=15, ton_relance="direct")
    op = OperationBancaire("OP1", "D1", D(2026, 9, 10), "PRLV FOURNISSEUR", Decimal("1234.5"), Sens.DEBIT)
    r = construire_relance(d, [Rapprochement(op, Statut.MANQUANT)], aujourdhui=LUNDI)
    assert isinstance(r, Relance)
    assert r.corps == "\n".join([
        "Bonjour M. Dupont,",
        "",
        "Nous finalisons la comptabilite de Societe D1 et il nous manque 1 justificatif(s) "
        "pour boucler la periode.",
        "",
        "Il s'agit de paiements visibles sur le compte bancaire pour lesquels nous n'avons pas "
        "la facture correspondante :",
        "",
        "  - 10/09/2026  1 234,50 EUR  -  PRLV FOURNISSEUR",
        "",
        "Un envoi par retour de cet email suffit (photo lisible acceptee).",
        "",
        "Merci de nous les transmettre des que possible,",
    ])
    assert r.objet == "Justificatifs - 1 justificatif(s) manquant(s) - Societe D1"
    assert (r.dossier, r.destinataire, r.nombre_pieces, r.niveau, r.est_relance) == (
        "D1", "a@client.fr", 1, 1, False)
    assert r.montant_total == Decimal("1234.5")
    assert construire_relance(d, [], aujourdhui=LUNDI) is None
    justifie = Rapprochement(op, Statut.JUSTIFIE)
    assert construire_relance(d, [justifie], aujourdhui=LUNDI) is None


def test_construire_toutes_existante_inchangee():
    ds = {"B": dossier("B", "b@x.fr"), "A": dossier("A", "a@x.fr")}

    def r(dos, ref, statut=Statut.MANQUANT):
        return Rapprochement(OperationBancaire(ref, dos, D(2026, 9, 1), "L", Decimal("10"), Sens.DEBIT), statut)

    res = construire_toutes(
        ds, [r("B", "1"), r("A", "2"), r("A", "3"), r("A", "4", Statut.JUSTIFIE), r("Z", "5")],
        aujourdhui=LUNDI,
    )
    assert [(x.dossier, x.nombre_pieces) for x in res] == [("A", 2), ("B", 1)]
    assert all(x.niveau == 1 for x in res)


def test_relance_niveau_2_est_une_relance():
    assert Relance("D", "a@x.fr", "o", "c", 1, Decimal("1"), 2).est_relance


# ---------------------------------------------------------------------------
# Contrat avec etats.py : DELAI_DEPASSE = escalade, emis a ce moment-la seulement
# ---------------------------------------------------------------------------


def test_delai_depasse_n_est_emis_qu_a_l_escalade():
    """Contrat 4.5 : DELAI_DEPASSE signifie ESCALADEE (etats). planifier ne l'emet que si
    nb_relances >= len(JALONS) ET >= JALON_ESCALADE jours ouvres depuis la premiere demande,
    ou des que nb_relances >= PLAFOND_RELANCES. Jamais avant."""
    ds = dossiers_de(dossier())
    depart = D(2026, 9, 28)

    def emis(p, jour):
        return [t for t in planifier([p], ds, [], jour).transitions if t[1] is Evenement.DELAI_DEPASSE]

    veille, jour_j = D(2026, 10, 15), D(2026, 10, 16)
    assert jours_ouvres_entre(depart, veille) == JALON_ESCALADE - 1
    assert jours_ouvres_entre(depart, jour_j) == JALON_ESCALADE
    for nb in (1, 2):                       # des jalons restent : on relance, on n'escalade pas
        assert emis(demandee(nb=nb, premiere=depart), D(2026, 12, 1)) == []
    assert emis(demandee(nb=3, premiere=depart), veille) == []
    assert emis(demandee(nb=3, premiere=depart), jour_j) == [("OP1", Evenement.DELAI_DEPASSE)]
    assert emis(demandee(nb=cadence.PLAFOND_RELANCES, premiere=D(2026, 10, 2)), LUNDI) != []
    # ni ATTENDUE, ni PROMISE, ni ESCALADEE, ni RECUE ne produisent DELAI_DEPASSE
    for etat in (EtatPiece.ATTENDUE, EtatPiece.PROMISE, EtatPiece.ESCALADEE, EtatPiece.RECUE):
        p = piece("OP1", etat=etat, nb=3, premiere=depart, date_promesse=D(2026, 1, 1))
        assert emis(p, D(2026, 12, 1)) == []


# ---------------------------------------------------------------------------
# Devise : la ligne reclamee porte la devise de la piece, jamais EUR code en dur
# ---------------------------------------------------------------------------


def test_ligne_en_usd_affiche_usd_et_jamais_eur():
    d = dossier("D1")
    ps = {"OP1": piece("OP1", devise="USD", libelle="AWS INVOICE", montant=Decimal("99.90"))}
    rel = RelancePlanifiee("a@client.fr", ("D1",), ("OP1",), 1)
    b = construire_brouillon(rel, ps, {"D1": d}, LUNDI)
    ligne = [l for l in b.corps.split("\n") if l.startswith("  - ")]
    assert ligne == ["  - 10/09/2026  99,90 USD  -  AWS INVOICE"]
    assert "EUR" not in b.corps


def test_ligne_en_usd_dans_un_brouillon_multi_dossiers():
    d1, d2 = dossier("D1", "a@x.fr"), dossier("D2", "a@x.fr")
    ps = {"OP1": piece("OP1", "D1", devise="USD"), "OP2": piece("OP2", "D2")}
    b = construire_brouillon(RelancePlanifiee("a@x.fr", ("D1", "D2"), ("OP1", "OP2"), 1),
                             ps, {"D1": d1, "D2": d2}, LUNDI)
    lignes = [l for l in b.corps.split("\n") if l.startswith("  - ")]
    assert "120,50 USD" in lignes[0] and "120,50 EUR" in lignes[1]
    assert b.corps.count("USD") == 1
