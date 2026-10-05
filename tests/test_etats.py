"""Tests de la machine d'etats d'une piece attendue (contrat 4.3).

La table du CONTRAT est recopiee ici a la main, independamment de `etats.TABLE` :
si le code et le contrat divergent, au moins un test echoue.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement import etats
from rapprochement.etats import (
    PLAFOND_RELANCES,
    TABLE,
    TransitionInterdite,
    appliquer,
    creer_depuis_rapprochement,
    est_acteur_humain,
    etat_periode,
)
from rapprochement.exclusions import Referentiel
from rapprochement.modeles import (
    ETATS_TERMINAUX,
    EtatPeriode,
    EtatPiece,
    Evenement,
    OperationBancaire,
    PieceAttendue,
    Rapprochement,
    Sens,
    Statut,
)
from rapprochement.moteur import Moteur
from rapprochement.parseurs import lire_pieces, lire_releve

E = EtatPiece
V = Evenement
AUJ = dt.date(2026, 10, 5)  # un lundi

# Contexte valide pour TOUS les evenements : une garde ne peut donc echouer que
# par l'etat ou par la piece, pas par le contexte.
CTX = {
    "valide_par": "Alice Martin",
    "confirme_par": "Alice Martin",
    "controle_par": "Alice Martin",
    "arbitre_par": "Alice Martin",
    "id_piece": "P-00042",
    "regle_validee": True,
    "date_promesse": AUJ + dt.timedelta(days=5),
    "motif": "motif de test",
}

# ---------------------------------------------------------------------------
# Table du contrat 4.3, recopiee : (etat, evenement) -> (cible, gardes)
#   cible None = « meme etat »
# ---------------------------------------------------------------------------

CONTRAT: dict[tuple[EtatPiece, Evenement], tuple[EtatPiece | None, tuple[str, ...]]] = {
    (E.ATTENDUE, V.RELANCE_ENVOYEE): (E.DEMANDEE, ("valide_par",)),
    (E.ATTENDUE, V.EXCLUSION_CREEE): (E.HORS_PERIMETRE, ("regle_validee",)),
    (E.ATTENDUE, V.PIECE_RATTACHEE): (E.RECUE, ("id_piece", "confirme_par")),
    (E.DEMANDEE, V.RELANCE_ENVOYEE): (E.DEMANDEE, ("valide_par", "plafond")),
    (E.DEMANDEE, V.PIECE_RATTACHEE): (E.RECUE, ("id_piece", "confirme_par")),
    (E.DEMANDEE, V.PROMESSE_SAISIE): (E.PROMISE, ("fenetre_promesse",)),
    (E.DEMANDEE, V.DELAI_DEPASSE): (E.ESCALADEE, ()),
    (E.DEMANDEE, V.EXCLUSION_CREEE): (E.HORS_PERIMETRE, ("regle_validee",)),
    (E.PROMISE, V.EXCLUSION_CREEE): (E.HORS_PERIMETRE, ("regle_validee",)),
    (E.ESCALADEE, V.EXCLUSION_CREEE): (E.HORS_PERIMETRE, ("regle_validee",)),
    (E.PROMISE, V.DATE_PROMISE_DEPASSEE): (E.DEMANDEE, ("promesse_depassee",)),
    (E.PROMISE, V.PIECE_RATTACHEE): (E.RECUE, ("id_piece", "confirme_par")),
    (E.RECUE, V.CONTROLE_CONFORME): (E.VALIDEE, ("controle_par",)),
    (E.RECUE, V.CONTROLE_NON_CONFORME): (E.DEMANDEE, ("controle_par", "motif")),
    (E.ESCALADEE, V.ARBITRAGE_CLASSEMENT): (E.CLOSE_SANS_SUITE, ("arbitre_par", "motif")),
    (E.ESCALADEE, V.PIECE_RATTACHEE): (E.RECUE, ("id_piece", "confirme_par")),
}
# « tout non terminal » : BLOCAGE_SIGNALE (motif) et BLOCAGE_LEVE, meme etat.
for _e in EtatPiece:
    if _e not in ETATS_TERMINAUX:
        CONTRAT[(_e, V.BLOCAGE_SIGNALE)] = (None, ("motif",))
        CONTRAT[(_e, V.BLOCAGE_LEVE)] = (None, ("bloquee",))

COUPLES_CONTRAT = frozenset(CONTRAT)
EVENEMENTS_SI_BLOQUEE = frozenset(
    {V.BLOCAGE_LEVE, V.PIECE_RATTACHEE, V.EXCLUSION_CREEE, V.ARBITRAGE_CLASSEMENT}
)
PRODUIT = list(itertools.product(EtatPiece, Evenement))
NON_TERMINAUX = [e for e in EtatPiece if e not in ETATS_TERMINAUX]


def piece(etat: EtatPiece = E.ATTENDUE, **kw) -> PieceAttendue:
    base = dict(
        reference="OP000001",
        dossier="TEST-1",
        periode="2026-09",
        montant=Decimal("120.50"),
        date_operation=dt.date(2026, 9, 12),
        libelle="PRLV SEPA FOURNISSEUR",
        etat=etat,
    )
    base.update(kw)
    return PieceAttendue(**base)


def piece_complete(etat: EtatPiece, **kw) -> PieceAttendue:
    """Piece dont la date promise est depassee : DATE_PROMISE_DEPASSEE est valide."""
    return piece(etat, date_promesse=AUJ - dt.timedelta(days=1), **kw)


def tente(p: PieceAttendue, evt: Evenement, **ctx) -> PieceAttendue:
    return appliquer(p, evt, aujourdhui=AUJ, **{**CTX, **ctx})


def refuse(p: PieceAttendue, evt: Evenement, **ctx) -> None:
    with pytest.raises(TransitionInterdite):
        appliquer(p, evt, aujourdhui=AUJ, **ctx)


def autorise(p: PieceAttendue, evt: Evenement) -> bool:
    try:
        tente(p, evt)
    except TransitionInterdite:
        return False
    return True


def ident(couple) -> str:
    return f"{couple[0].value}-{couple[1].value}"


# ---------------------------------------------------------------------------
# 1. Exhaustivite
# ---------------------------------------------------------------------------

def test_le_contrat_recopie_compte_24_cellules():
    # 16 transitions explicites + 2 evenements de blocage x 5 etats non terminaux.
    assert len(COUPLES_CONTRAT) == 16 + 2 * 5 == 26
    assert len(PRODUIT) == 88 == 8 * 11


def autorises_pour(drapeau: bool) -> set[tuple[EtatPiece, Evenement]]:
    return {
        (e, v)
        for e, v in PRODUIT
        if autorise(piece_complete(e, bloquee=drapeau, motif_blocage="x" if drapeau else ""), v)
    }


# BLOCAGE_LEVE n'est accepte que sur une piece bloquee : un couple est « autorise »
# s'il l'est pour au moins un des deux etats du drapeau.
@pytest.mark.parametrize("couple", PRODUIT, ids=ident)
def test_couple_autorise_si_et_seulement_si_dans_le_contrat(couple):
    etat, evt = couple
    libre = autorise(piece_complete(etat), evt)
    bloque = autorise(piece_complete(etat, bloquee=True, motif_blocage="x"), evt)
    assert (libre or bloque) == (couple in COUPLES_CONTRAT)


def test_ensemble_des_couples_autorises_egal_au_contrat():
    autorises = autorises_pour(False) | autorises_pour(True)
    assert autorises == COUPLES_CONTRAT, (
        f"ajoutes a tort : {sorted(map(ident, autorises - COUPLES_CONTRAT))} ; "
        f"oublies : {sorted(map(ident, COUPLES_CONTRAT - autorises))}"
    )


def test_ensemble_autorise_piece_non_bloquee():
    attendu = {c for c in COUPLES_CONTRAT if c[1] is not V.BLOCAGE_LEVE}
    assert autorises_pour(False) == attendu


def test_la_table_de_donnees_egale_la_table_du_contrat():
    assert set(TABLE) == COUPLES_CONTRAT
    for couple, (cible, gardes) in CONTRAT.items():
        regle = TABLE[couple]
        assert regle.cible == cible, ident(couple)
        assert regle.noms_gardes() == gardes, ident(couple)


def test_la_table_n_est_pas_modifiable():
    with pytest.raises(TypeError):
        TABLE[(E.VALIDEE, V.BLOCAGE_LEVE)] = TABLE[(E.ATTENDUE, V.BLOCAGE_LEVE)]  # type: ignore[index]


@pytest.mark.parametrize("couple", PRODUIT, ids=ident)
def test_couple_autorise_pour_piece_bloquee(couple):
    etat, evt = couple
    attendu = couple in COUPLES_CONTRAT and evt in EVENEMENTS_SI_BLOQUEE
    assert autorise(piece_complete(etat, bloquee=True, motif_blocage="x"), evt) == attendu


def test_ensemble_autorise_piece_bloquee_egal_contrat_filtre():
    assert autorises_pour(True) == {c for c in COUPLES_CONTRAT if c[1] in EVENEMENTS_SI_BLOQUEE}


# ---------------------------------------------------------------------------
# 2. Chaque cellule : cible et effets
# ---------------------------------------------------------------------------

D0 = dt.date(2026, 9, 28)  # date de premiere demande des pieces deja relancees


def demandee(nb: int = 1, **kw) -> PieceAttendue:
    return piece(
        E.DEMANDEE,
        nb_relances=nb,
        date_premiere_demande=D0,
        date_derniere_relance=D0,
        **kw,
    )


def test_attendue_relance_envoyee():
    p = piece(E.ATTENDUE)
    n = tente(p, V.RELANCE_ENVOYEE)
    assert n.etat is E.DEMANDEE
    assert n.nb_relances == 1
    assert n.date_premiere_demande == AUJ
    assert n.date_derniere_relance == AUJ
    assert n.pieces_rattachees == () and n.date_promesse is None and not n.bloquee


def test_attendue_exclusion_creee():
    n = tente(piece(E.ATTENDUE), V.EXCLUSION_CREEE)
    assert n.etat is E.HORS_PERIMETRE
    assert n == piece(E.HORS_PERIMETRE)


@pytest.mark.parametrize("etat", [E.ATTENDUE, E.DEMANDEE, E.PROMISE, E.ESCALADEE])
def test_exclusion_creee_depuis_les_quatre_etats_sources(etat):
    assert tente(piece(etat), V.EXCLUSION_CREEE).etat is E.HORS_PERIMETRE


def test_exclusion_creee_refusee_depuis_recue():
    refuse(piece(E.RECUE), V.EXCLUSION_CREEE, **CTX)
    refuse(piece(E.RECUE, bloquee=True), V.EXCLUSION_CREEE, **CTX)


def test_exclusion_depuis_promise_ne_touche_pas_a_la_promesse():
    # Sortie vers un etat terminal : le contrat ne dit rien sur date_promesse.
    p = piece(E.PROMISE, nb_relances=1, date_promesse=AUJ)
    assert tente(p, V.EXCLUSION_CREEE) == replace(p, etat=E.HORS_PERIMETRE)


@pytest.mark.parametrize("etat", [E.ATTENDUE, E.DEMANDEE, E.PROMISE, E.ESCALADEE])
def test_piece_rattachee_depuis_chaque_etat_source(etat):
    n = tente(piece(etat), V.PIECE_RATTACHEE)
    assert n.etat is E.RECUE
    assert n.pieces_rattachees == ("P-00042",)
    assert n.nb_relances == 0
    assert n.date_promesse is None


def test_sortir_de_promise_vers_recue_vide_la_promesse():
    p = piece(E.PROMISE, nb_relances=1, date_premiere_demande=D0, date_promesse=AUJ + dt.timedelta(days=3))
    n = tente(p, V.PIECE_RATTACHEE)
    assert n.etat is E.RECUE and n.date_promesse is None
    assert n.date_premiere_demande == D0


def test_demandee_relance_envoyee_incremente():
    n = tente(demandee(nb=1), V.RELANCE_ENVOYEE)
    assert n.etat is E.DEMANDEE
    assert n.nb_relances == 2
    assert n.date_derniere_relance == AUJ
    assert n.date_premiere_demande == D0  # inchangee


def test_demandee_promesse_saisie():
    promesse = AUJ + dt.timedelta(days=7)
    n = tente(demandee(), V.PROMESSE_SAISIE, date_promesse=promesse)
    assert n.etat is E.PROMISE
    assert n.date_promesse == promesse
    assert n.nb_relances == 1


@pytest.mark.parametrize("nb", [0, 1, PLAFOND_RELANCES - 1, PLAFOND_RELANCES, PLAFOND_RELANCES + 1])
def test_demandee_delai_depasse_escalade_toujours(nb):
    p = demandee(nb=nb)
    n = tente(p, V.DELAI_DEPASSE)
    assert n.etat is E.ESCALADEE
    assert n == replace(p, etat=E.ESCALADEE)  # aucun autre effet


def test_demandee_exclusion_creee():
    assert tente(demandee(), V.EXCLUSION_CREEE).etat is E.HORS_PERIMETRE


def test_promise_date_promise_depassee():
    p = piece(E.PROMISE, nb_relances=1, date_premiere_demande=D0, date_promesse=AUJ - dt.timedelta(days=1))
    n = tente(p, V.DATE_PROMISE_DEPASSEE)
    assert n.etat is E.DEMANDEE
    assert n.date_promesse is None
    assert n.nb_relances == 1  # aucune relance n'est comptee

def test_recue_controle_conforme():
    p = piece(E.RECUE, pieces_rattachees=("P-1",))
    n = tente(p, V.CONTROLE_CONFORME)
    assert n.etat is E.VALIDEE
    assert n.pieces_rattachees == ("P-1",)


def test_recue_controle_non_conforme_vide_les_rattachements():
    p = piece(E.RECUE, nb_relances=2, date_premiere_demande=D0, pieces_rattachees=("P-1",))
    n = tente(p, V.CONTROLE_NON_CONFORME)
    assert n.etat is E.DEMANDEE
    assert n.pieces_rattachees == ()
    assert n.nb_relances == 2
    assert n.date_premiere_demande == D0  # deja renseignee : inchangee


def test_non_conforme_vide_aussi_la_promesse():
    p = piece(E.RECUE, nb_relances=1, date_premiere_demande=D0,
              date_promesse=AUJ + dt.timedelta(days=2), pieces_rattachees=("P-1",))
    assert tente(p, V.CONTROLE_NON_CONFORME).date_promesse is None


def test_non_conforme_fixe_la_premiere_demande_si_piece_arrivee_avant_toute_relance():
    p = tente(piece(E.ATTENDUE), V.PIECE_RATTACHEE)
    assert p.date_premiere_demande is None and p.nb_relances == 0
    n = tente(p, V.CONTROLE_NON_CONFORME)
    assert n.etat is E.DEMANDEE
    assert n.date_premiere_demande == AUJ
    assert n.nb_relances == 0
    assert n.date_derniere_relance is None


def test_escaladee_arbitrage_classement():
    n = tente(piece(E.ESCALADEE, nb_relances=4), V.ARBITRAGE_CLASSEMENT)
    assert n.etat is E.CLOSE_SANS_SUITE


@pytest.mark.parametrize("etat", NON_TERMINAUX)
def test_blocage_signale_et_leve_gardent_l_etat(etat):
    p = piece(etat, nb_relances=2, date_premiere_demande=D0)
    bloquee = tente(p, V.BLOCAGE_SIGNALE, motif="  client injoignable ")
    assert bloquee.etat is etat
    assert bloquee.bloquee is True
    assert bloquee.motif_blocage == "client injoignable"
    assert bloquee.nb_relances == 2
    libre = tente(bloquee, V.BLOCAGE_LEVE)
    assert libre.etat is etat
    assert libre.bloquee is False
    assert libre.motif_blocage == ""
    assert libre == p


# ---------------------------------------------------------------------------
# 3. Gardes
# ---------------------------------------------------------------------------

# (etat de depart, evenement, cle de contexte humaine)
GARDES_HUMAINES = [
    (E.ATTENDUE, V.RELANCE_ENVOYEE, "valide_par"),
    (E.DEMANDEE, V.RELANCE_ENVOYEE, "valide_par"),
    (E.ATTENDUE, V.PIECE_RATTACHEE, "confirme_par"),
    (E.DEMANDEE, V.PIECE_RATTACHEE, "confirme_par"),
    (E.PROMISE, V.PIECE_RATTACHEE, "confirme_par"),
    (E.ESCALADEE, V.PIECE_RATTACHEE, "confirme_par"),
    (E.RECUE, V.CONTROLE_CONFORME, "controle_par"),
    (E.RECUE, V.CONTROLE_NON_CONFORME, "controle_par"),
    (E.ESCALADEE, V.ARBITRAGE_CLASSEMENT, "arbitre_par"),
]
ACTEURS_AUTOMATIQUES = [
    f"{pre}{nom}{post}"
    for nom in ("systeme", "SYSTEME", "Systeme", "system", "System", "automatique", "AUTOMATIQUE", "auto", "AUTO", "Auto", "robot", "ROBOT",
                "cron", "Cron", "bot", "BOT", "Bot", "systeme")
    for pre, post in (("", ""), (" ", " "), ("\t", "\n"), ("  ", ""), ("", "  "))
] + ["Système", "SYSTÈME"]
ACTEURS_INVALIDES = ["", " ", "   ", "\t\n", None, 0, 42, b"alice", ["alice"]]


def avec(ctx: dict, **kw) -> dict:
    nouveau = dict(ctx)
    nouveau.update(kw)
    return nouveau


@pytest.mark.parametrize("etat,evt,cle", GARDES_HUMAINES)
def test_garde_humaine_contexte_manquant(etat, evt, cle):
    ctx = {k: v for k, v in CTX.items() if k != cle}
    refuse(piece_complete(etat), evt, **ctx)


@pytest.mark.parametrize("etat,evt,cle", GARDES_HUMAINES)
@pytest.mark.parametrize("valeur", ACTEURS_INVALIDES, ids=repr)
def test_garde_humaine_valeur_vide_ou_invalide(etat, evt, cle, valeur):
    refuse(piece_complete(etat), evt, **avec(CTX, **{cle: valeur}))


@pytest.mark.parametrize("etat,evt,cle", GARDES_HUMAINES)
@pytest.mark.parametrize("valeur", ACTEURS_AUTOMATIQUES, ids=repr)
def test_garde_humaine_refuse_les_acteurs_automatiques(etat, evt, cle, valeur):
    refuse(piece_complete(etat), evt, **avec(CTX, **{cle: valeur}))


@pytest.mark.parametrize("etat,evt,cle", GARDES_HUMAINES)
def test_garde_humaine_accepte_un_nom_propre(etat, evt, cle):
    for nom in ("Alice Martin", "j.dupont", " Jean ", "Robotti", "Botte"):
        assert piece_complete(etat) is not tente(piece_complete(etat), evt, **avec(CTX, **{cle: nom}))


def test_acteurs_automatiques_union_des_listes_du_contrat_et_d_envoi():
    assert etats.ACTEURS_AUTOMATIQUES == {
        "systeme", "system", "auto", "automatique", "robot", "cron", "bot"
    }


def test_est_acteur_humain():
    assert est_acteur_humain("Alice")
    assert not est_acteur_humain("  Systeme ")
    assert not est_acteur_humain("System")
    assert not est_acteur_humain("Automatique")
    assert not est_acteur_humain("")
    assert not est_acteur_humain(None)


@pytest.mark.parametrize("etat", [E.ATTENDUE, E.DEMANDEE, E.PROMISE, E.ESCALADEE])
def test_piece_rattachee_exige_id_piece(etat):
    sans = {k: v for k, v in CTX.items() if k != "id_piece"}
    refuse(piece(etat), V.PIECE_RATTACHEE, **sans)
    for vide in ("", "   ", None, 12):
        refuse(piece(etat), V.PIECE_RATTACHEE, **avec(CTX, id_piece=vide))


@pytest.mark.parametrize("etat", [E.ATTENDUE, E.DEMANDEE])
@pytest.mark.parametrize("valeur", [False, None, 0, 1, "oui", "True", [True]], ids=repr)
def test_exclusion_exige_regle_validee_strictement_vraie(etat, valeur):
    refuse(piece(etat), V.EXCLUSION_CREEE, **avec(CTX, regle_validee=valeur))


@pytest.mark.parametrize("etat", [E.ATTENDUE, E.DEMANDEE])
def test_exclusion_sans_contexte(etat):
    refuse(piece(etat), V.EXCLUSION_CREEE)


@pytest.mark.parametrize(
    "decalage,ok",
    [(-1, False), (0, True), (1, True), (15, True), (16, False), (365, False), (-365, False)],
)
def test_promesse_bornes(decalage, ok):
    ctx = avec(CTX, date_promesse=AUJ + dt.timedelta(days=decalage))
    if ok:
        assert tente(demandee(), V.PROMESSE_SAISIE, **ctx).date_promesse == AUJ + dt.timedelta(days=decalage)
    else:
        refuse(demandee(), V.PROMESSE_SAISIE, **ctx)


@pytest.mark.parametrize(
    "valeur",
    [None, "2026-10-10", 20261010, dt.datetime(2026, 10, 10, 9, 0), dt.datetime(2026, 10, 5)],
    ids=repr,
)
def test_promesse_type_invalide(valeur):
    refuse(demandee(), V.PROMESSE_SAISIE, **avec(CTX, date_promesse=valeur))


def test_promesse_contexte_manquant():
    refuse(demandee(), V.PROMESSE_SAISIE, **{k: v for k, v in CTX.items() if k != "date_promesse"})


def test_la_promesse_est_comparee_a_aujourdhui_passe_en_parametre():
    autre = dt.date(2027, 1, 4)
    promesse = autre + dt.timedelta(days=15)
    n = appliquer(demandee(), V.PROMESSE_SAISIE, aujourdhui=autre, date_promesse=promesse)
    assert n.date_promesse == promesse
    with pytest.raises(TransitionInterdite):
        appliquer(demandee(), V.PROMESSE_SAISIE, aujourdhui=autre, date_promesse=AUJ)


@pytest.mark.parametrize(
    "decalage,ok", [(-30, True), (-1, True), (0, False), (1, False), (30, False)]
)
def test_date_promise_depassee_strictement(decalage, ok):
    p = piece(E.PROMISE, nb_relances=1, date_promesse=AUJ + dt.timedelta(days=decalage))
    if ok:
        assert tente(p, V.DATE_PROMISE_DEPASSEE).etat is E.DEMANDEE
    else:
        refuse(p, V.DATE_PROMISE_DEPASSEE, **CTX)


def test_date_promise_depassee_sans_date_promesse():
    refuse(piece(E.PROMISE, nb_relances=1, date_promesse=None), V.DATE_PROMISE_DEPASSEE)


@pytest.mark.parametrize(
    "evt,etat",
    [
        (V.BLOCAGE_SIGNALE, E.ATTENDUE),
        (V.BLOCAGE_SIGNALE, E.RECUE),
        (V.CONTROLE_NON_CONFORME, E.RECUE),
        (V.ARBITRAGE_CLASSEMENT, E.ESCALADEE),
    ],
)
@pytest.mark.parametrize("valeur", ["", " ", "\t\n", None, 3], ids=repr)
def test_motif_vide_ou_invalide(evt, etat, valeur):
    refuse(piece(etat), evt, **avec(CTX, motif=valeur))


@pytest.mark.parametrize(
    "evt,etat",
    [
        (V.BLOCAGE_SIGNALE, E.DEMANDEE),
        (V.CONTROLE_NON_CONFORME, E.RECUE),
        (V.ARBITRAGE_CLASSEMENT, E.ESCALADEE),
    ],
)
def test_motif_manquant(evt, etat):
    refuse(piece(etat), evt, **{k: v for k, v in CTX.items() if k != "motif"})


def test_aujourdhui_doit_etre_une_date():
    with pytest.raises(TypeError):
        appliquer(piece(), V.RELANCE_ENVOYEE, aujourdhui=dt.datetime(2026, 10, 5, 9), **CTX)
    with pytest.raises(TypeError):
        appliquer(piece(), V.RELANCE_ENVOYEE, aujourdhui="2026-10-05", **CTX)  # type: ignore[arg-type]


def test_evenement_inconnu_est_interdit():
    with pytest.raises(TransitionInterdite):
        appliquer(piece(), "n_importe_quoi", aujourdhui=AUJ, **CTX)  # type: ignore[arg-type]


def test_evenement_donne_comme_chaine_est_accepte():
    n = appliquer(piece(), "relance_envoyee", aujourdhui=AUJ, **CTX)  # type: ignore[arg-type]
    assert n.etat is E.DEMANDEE


def test_contexte_superflu_est_ignore():
    n = tente(piece(E.RECUE), V.CONTROLE_CONFORME, **avec(CTX, inutile=1))
    assert n.etat is E.VALIDEE


# ---------------------------------------------------------------------------
# 4. Plafond de relances
# ---------------------------------------------------------------------------

def test_plafond_vaut_4():
    assert PLAFOND_RELANCES == 4


@pytest.mark.parametrize("nb", [PLAFOND_RELANCES, PLAFOND_RELANCES + 1, 99])
def test_au_plafond_la_relance_est_refusee(nb):
    refuse(demandee(nb=nb), V.RELANCE_ENVOYEE, **CTX)


@pytest.mark.parametrize("nb", range(0, PLAFOND_RELANCES))
def test_sous_le_plafond_relance_permise(nb):
    assert tente(demandee(nb=nb), V.RELANCE_ENVOYEE).nb_relances == nb + 1


@pytest.mark.parametrize("nb", range(0, PLAFOND_RELANCES + 2))
def test_le_plafond_ne_gouverne_pas_delai_depasse(nb):
    assert tente(demandee(nb=nb), V.DELAI_DEPASSE).etat is E.ESCALADEE


def test_la_derniere_relance_amene_au_plafond_puis_la_suivante_est_refusee():
    p = demandee(nb=PLAFOND_RELANCES - 1)
    p = tente(p, V.RELANCE_ENVOYEE)
    assert p.nb_relances == PLAFOND_RELANCES
    refuse(p, V.RELANCE_ENVOYEE, **CTX)
    assert tente(p, V.DELAI_DEPASSE).etat is E.ESCALADEE


# ---------------------------------------------------------------------------
# 5. Etats terminaux et pieces bloquees
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("etat", sorted(ETATS_TERMINAUX, key=lambda e: e.value))
@pytest.mark.parametrize("evt", list(Evenement))
def test_un_etat_terminal_n_accepte_aucun_evenement(etat, evt):
    refuse(piece(etat, date_promesse=AUJ - dt.timedelta(days=1)), evt, **CTX)
    refuse(piece(etat, bloquee=True), evt, **CTX)


def test_les_trois_etats_terminaux():
    assert ETATS_TERMINAUX == {E.VALIDEE, E.HORS_PERIMETRE, E.CLOSE_SANS_SUITE}


@pytest.mark.parametrize("etat", NON_TERMINAUX)
@pytest.mark.parametrize("evt", sorted(set(Evenement) - EVENEMENTS_SI_BLOQUEE, key=lambda e: e.value))
def test_piece_bloquee_refuse_les_autres_evenements(etat, evt):
    refuse(piece_complete(etat, bloquee=True, motif_blocage="x"), evt, **CTX)


def test_piece_bloquee_ne_peut_pas_etre_bloquee_deux_fois():
    p = tente(piece(E.DEMANDEE, nb_relances=1), V.BLOCAGE_SIGNALE, motif="premier")
    refuse(p, V.BLOCAGE_SIGNALE, **CTX)
    assert p.motif_blocage == "premier"


def test_piece_bloquee_demandee_accepte_rattachement_et_exclusion():
    p = demandee(bloquee=True, motif_blocage="x")
    assert tente(p, V.PIECE_RATTACHEE).etat is E.RECUE
    assert tente(p, V.EXCLUSION_CREEE).etat is E.HORS_PERIMETRE
    assert tente(p, V.BLOCAGE_LEVE).bloquee is False


def test_piece_bloquee_rattachee_reste_bloquee_jusqu_a_la_levee():
    n = tente(demandee(bloquee=True, motif_blocage="x"), V.PIECE_RATTACHEE)
    assert n.etat is E.RECUE and n.bloquee and n.motif_blocage == "x"


@pytest.mark.parametrize(
    "etat,evt",
    [(E.DEMANDEE, V.EXCLUSION_CREEE), (E.PROMISE, V.EXCLUSION_CREEE),
     (E.ESCALADEE, V.EXCLUSION_CREEE), (E.ATTENDUE, V.EXCLUSION_CREEE),
     (E.ESCALADEE, V.ARBITRAGE_CLASSEMENT)],
)
def test_entrer_dans_un_etat_terminal_leve_le_drapeau_de_blocage(etat, evt):
    p = piece(etat, nb_relances=4, bloquee=True, motif_blocage="litige")
    n = tente(p, evt)
    assert n.etat in ETATS_TERMINAUX
    assert n.bloquee is False and n.motif_blocage == ""
    assert p.bloquee is True  # origine intacte


def test_piece_bloquee_escaladee_accepte_arbitrage():
    p = piece(E.ESCALADEE, nb_relances=4, bloquee=True, motif_blocage="x")
    assert tente(p, V.ARBITRAGE_CLASSEMENT).etat is E.CLOSE_SANS_SUITE


def test_piece_bloquee_recue_n_accepte_que_la_levee():
    p = piece(E.RECUE, bloquee=True, pieces_rattachees=("P-1",))
    for evt in Evenement:
        assert autorise(p, evt) == (evt is V.BLOCAGE_LEVE), evt


@pytest.mark.parametrize("etat", NON_TERMINAUX)
def test_blocage_leve_sur_une_piece_non_bloquee_est_interdit(etat):
    refuse(piece(etat, nb_relances=1), V.BLOCAGE_LEVE, **CTX)


def test_blocage_leve_vide_le_motif():
    p = piece(E.DEMANDEE, nb_relances=1, bloquee=True, motif_blocage="litige")
    n = tente(p, V.BLOCAGE_LEVE)
    assert (n.bloquee, n.motif_blocage) == (False, "")


# ---------------------------------------------------------------------------
# 6. Immutabilite
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("couple", sorted(COUPLES_CONTRAT, key=ident), ids=ident)
def test_la_piece_d_origine_n_est_jamais_modifiee(couple):
    etat, evt = couple
    origine = piece_complete(etat, nb_relances=1, date_premiere_demande=D0,
                             pieces_rattachees=("P-0",),
                             bloquee=evt is V.BLOCAGE_LEVE, motif_blocage="x")
    copie = PieceAttendue(**{f: getattr(origine, f) for f in origine.__dataclass_fields__})
    resultat = tente(origine, evt)
    assert resultat is not origine
    assert origine == copie
    assert vars(origine) == vars(copie)


def test_piece_attendue_est_figee():
    p = piece()
    with pytest.raises(Exception):
        p.etat = E.VALIDEE  # type: ignore[misc]


def test_une_garde_qui_echoue_ne_modifie_rien():
    p = demandee(nb=PLAFOND_RELANCES)
    avant = vars(p).copy()
    refuse(p, V.RELANCE_ENVOYEE, **CTX)
    assert vars(p) == avant


def test_pure_memes_entrees_memes_sorties():
    p = piece(E.ATTENDUE)
    assert tente(p, V.RELANCE_ENVOYEE) == tente(p, V.RELANCE_ENVOYEE)


def test_le_module_n_utilise_pas_l_horloge():
    source = Path(etats.__file__).read_text(encoding="utf-8")
    assert "today()" not in source and "now()" not in source


# ---------------------------------------------------------------------------
# 7. etat_periode
# ---------------------------------------------------------------------------

def test_periode_pieces_non_creees_est_ouverte():
    assert etat_periode([], pieces_creees=False) is EtatPeriode.OUVERTE
    assert etat_periode([piece(E.VALIDEE)], pieces_creees=False) is EtatPeriode.OUVERTE
    assert etat_periode([piece(E.ATTENDUE)], pieces_creees=False) is EtatPeriode.OUVERTE


def test_periode_liste_vide_est_complete():
    assert etat_periode([]) is EtatPeriode.COMPLETE


def test_periode_liste_vide_cloturable():
    assert etat_periode([], cloturee=True) is EtatPeriode.CLOTUREE


@pytest.mark.parametrize("etat", NON_TERMINAUX)
def test_periode_une_piece_non_terminale_est_en_collecte(etat):
    assert etat_periode([piece(etat)]) is EtatPeriode.EN_COLLECTE
    assert etat_periode([piece(E.VALIDEE), piece(etat)]) is EtatPeriode.EN_COLLECTE


@pytest.mark.parametrize("etat", sorted(ETATS_TERMINAUX, key=lambda e: e.value))
def test_periode_toutes_terminales_est_complete(etat):
    assert etat_periode([piece(etat)]) is EtatPeriode.COMPLETE
    assert etat_periode([piece(etat), piece(E.VALIDEE), piece(E.HORS_PERIMETRE)]) is EtatPeriode.COMPLETE


def test_periode_complete_et_cloturee():
    lot = [piece(E.VALIDEE), piece(E.CLOSE_SANS_SUITE), piece(E.HORS_PERIMETRE)]
    assert etat_periode(lot, cloturee=True) is EtatPeriode.CLOTUREE


@pytest.mark.parametrize("etat", NON_TERMINAUX)
def test_periode_cloture_impossible_si_piece_non_terminale(etat):
    with pytest.raises(ValueError):
        etat_periode([piece(E.VALIDEE), piece(etat)], cloturee=True)
    with pytest.raises(ValueError):
        etat_periode([piece(etat)], pieces_creees=True, cloturee=True)


def test_periode_cloture_impossible_si_pieces_non_creees():
    with pytest.raises(ValueError):
        etat_periode([], pieces_creees=False, cloturee=True)
    with pytest.raises(ValueError):
        etat_periode([piece(E.VALIDEE)], pieces_creees=False, cloturee=True)


def test_periode_accepte_un_generateur_et_le_consomme_une_fois():
    assert etat_periode(p for p in [piece(E.VALIDEE), piece(E.ATTENDUE)]) is EtatPeriode.EN_COLLECTE
    assert etat_periode((p for p in [piece(E.VALIDEE)]), cloturee=True) is EtatPeriode.CLOTUREE


def test_piece_bloquee_non_terminale_garde_la_periode_en_collecte():
    assert etat_periode([piece(E.DEMANDEE, bloquee=True)]) is EtatPeriode.EN_COLLECTE


def test_etat_periode_est_calcule_pas_stocke():
    assert not hasattr(PieceAttendue, "etat_periode")
    assert "periode_etat" not in PieceAttendue.__dataclass_fields__


# ---------------------------------------------------------------------------
# creer_depuis_rapprochement
# ---------------------------------------------------------------------------

def operation(ref="OP1", jour=dt.date(2026, 3, 9), montant="89.90") -> OperationBancaire:
    return OperationBancaire(
        reference=ref,
        dossier="TEST-1",
        date_operation=jour,
        libelle="PRLV SEPA ORANGE",
        montant=Decimal(montant),
        sens=Sens.DEBIT,
    )


def test_creer_depuis_manquant():
    p = creer_depuis_rapprochement(Rapprochement(operation(), Statut.MANQUANT))
    assert p == PieceAttendue(
        reference="OP1",
        dossier="TEST-1",
        periode="2026-03",
        montant=Decimal("89.90"),
        date_operation=dt.date(2026, 3, 9),
        libelle="PRLV SEPA ORANGE",
    )
    assert p.etat is E.ATTENDUE and p.nb_relances == 0 and not p.bloquee


def test_creer_reprend_la_devise_de_l_operation():
    op = replace(operation(), devise="USD")
    assert creer_depuis_rapprochement(Rapprochement(op, Statut.MANQUANT)).devise == "USD"
    assert creer_depuis_rapprochement(Rapprochement(operation(), Statut.MANQUANT)).devise == "EUR"


@pytest.mark.parametrize(
    "statut", [Statut.JUSTIFIE, Statut.HORS_PERIMETRE, Statut.A_VERIFIER, Statut.PARTIEL]
)
def test_creer_refuse_les_autres_statuts(statut):
    with pytest.raises(ValueError):
        creer_depuis_rapprochement(Rapprochement(operation(), statut))


@pytest.mark.parametrize(
    "jour,periode",
    [(dt.date(2026, 1, 1), "2026-01"), (dt.date(2026, 12, 31), "2026-12"),
     (dt.date(2025, 2, 28), "2025-02"), (dt.date(2026, 10, 5), "2026-10")],
)
def test_creer_periode_aaaa_mm(jour, periode):
    assert creer_depuis_rapprochement(
        Rapprochement(operation(jour=jour), Statut.MANQUANT)
    ).periode == periode


@pytest.mark.parametrize("montant", ["0", "0.00", "-12.30"])
def test_creer_refuse_un_montant_non_positif(montant):
    with pytest.raises(ValueError):
        creer_depuis_rapprochement(Rapprochement(operation(montant=montant), Statut.MANQUANT))


# ---------------------------------------------------------------------------
# 8. Donnees reelles : data/corpus
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rapprochements() -> list[Rapprochement]:
    corpus = RACINE / "data" / "corpus"
    operations = lire_releve(corpus / "releve_bancaire.csv")
    pieces = lire_pieces(corpus / "pieces_recues.csv")
    return Moteur(Referentiel()).rapprocher(operations, pieces)


def test_corpus_couvre_tous_les_statuts(rapprochements):
    # Garde-fou : sans MANQUANT ni PARTIEL ni autres statuts, les tests ci-dessous seraient vides.
    presents = {r.statut for r in rapprochements}
    assert {Statut.MANQUANT, Statut.PARTIEL} <= presents
    assert presents - {Statut.MANQUANT, Statut.PARTIEL}


def test_corpus_manquants_donnent_une_piece_valide(rapprochements):
    concernes = [r for r in rapprochements if r.statut is Statut.MANQUANT]
    attendues = [creer_depuis_rapprochement(r) for r in concernes]
    assert len(attendues) == len(concernes) > 0
    for r, p in zip(concernes, attendues):
        assert re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", p.periode), p
        assert p.periode == r.operation.date_operation.strftime("%Y-%m")
        assert isinstance(p.montant, Decimal) and p.montant > 0
        assert p.montant == r.operation.montant
        assert p.reference == r.operation.reference and p.reference
        assert p.dossier == r.operation.dossier and p.dossier
        assert p.date_operation == r.operation.date_operation
        assert p.libelle == r.operation.libelle
        assert p.devise == r.operation.devise
        assert p.etat is E.ATTENDUE
        assert (p.nb_relances, p.date_promesse, p.pieces_rattachees, p.bloquee) == (0, None, (), False)
    cles = [(p.dossier, p.reference) for p in attendues]
    assert len(set(cles)) == len(cles), "reference en double dans un meme dossier"


def test_corpus_les_autres_statuts_levent_value_error(rapprochements):
    autres = [r for r in rapprochements if r.statut is not Statut.MANQUANT]
    assert {Statut.PARTIEL, Statut.A_VERIFIER} <= {r.statut for r in autres}
    assert autres
    for r in autres:
        with pytest.raises(ValueError):
            creer_depuis_rapprochement(r)


def test_corpus_cycle_complet_sur_toutes_les_pieces(rapprochements):
    attendues = [
        creer_depuis_rapprochement(r) for r in rapprochements if r.statut is Statut.MANQUANT
    ]
    assert etat_periode(attendues) is EtatPeriode.EN_COLLECTE
    assert etat_periode(attendues, pieces_creees=False) is EtatPeriode.OUVERTE
    validees = []
    for p in attendues:
        p = tente(p, V.RELANCE_ENVOYEE)
        p = tente(p, V.PIECE_RATTACHEE)
        validees.append(tente(p, V.CONTROLE_CONFORME))
    assert all(p.etat is E.VALIDEE for p in validees)
    assert etat_periode(validees) is EtatPeriode.COMPLETE
    assert etat_periode(validees, cloturee=True) is EtatPeriode.CLOTUREE
    with pytest.raises(ValueError):
        etat_periode(attendues, cloturee=True)


# ---------------------------------------------------------------------------
# 9. Parcours complets
# ---------------------------------------------------------------------------

def test_parcours_attendue_demandee_promise_demandee_recue_validee():
    j1 = dt.date(2026, 10, 5)
    p = piece(E.ATTENDUE)
    assert (p.etat, p.nb_relances) == (E.ATTENDUE, 0)

    p = appliquer(p, V.RELANCE_ENVOYEE, aujourdhui=j1, valide_par="Alice Martin")
    assert p.etat is E.DEMANDEE
    assert (p.nb_relances, p.date_premiere_demande, p.date_derniere_relance) == (1, j1, j1)

    j2 = dt.date(2026, 10, 8)
    promesse = dt.date(2026, 10, 14)
    p = appliquer(p, V.PROMESSE_SAISIE, aujourdhui=j2, date_promesse=promesse)
    assert p.etat is E.PROMISE
    assert (p.nb_relances, p.date_promesse, p.date_premiere_demande) == (1, promesse, j1)

    j3 = dt.date(2026, 10, 15)
    p = appliquer(p, V.DATE_PROMISE_DEPASSEE, aujourdhui=j3)
    assert p.etat is E.DEMANDEE
    assert (p.nb_relances, p.date_promesse, p.date_derniere_relance) == (1, None, j1)

    j4 = dt.date(2026, 10, 16)
    p = appliquer(p, V.PIECE_RATTACHEE, aujourdhui=j4, id_piece="P-00007", confirme_par="Alice Martin")
    assert p.etat is E.RECUE
    assert (p.nb_relances, p.pieces_rattachees) == (1, ("P-00007",))

    j5 = dt.date(2026, 10, 19)
    p = appliquer(p, V.CONTROLE_CONFORME, aujourdhui=j5, controle_par="Bruno Leroy")
    assert p.etat is E.VALIDEE
    assert (p.nb_relances, p.pieces_rattachees) == (1, ("P-00007",))
    assert etat_periode([p]) is EtatPeriode.COMPLETE

    for evt in Evenement:
        refuse(p, evt, **CTX)


def test_parcours_avec_relance_apres_promesse_non_tenue():
    p = piece(E.ATTENDUE)
    p = appliquer(p, V.RELANCE_ENVOYEE, aujourdhui=dt.date(2026, 10, 5), valide_par="Alice")
    p = appliquer(p, V.PROMESSE_SAISIE, aujourdhui=dt.date(2026, 10, 6), date_promesse=dt.date(2026, 10, 9))
    p = appliquer(p, V.DATE_PROMISE_DEPASSEE, aujourdhui=dt.date(2026, 10, 10))
    p = appliquer(p, V.RELANCE_ENVOYEE, aujourdhui=dt.date(2026, 10, 12), valide_par="Alice")
    assert (p.etat, p.nb_relances) == (E.DEMANDEE, 2)
    assert p.date_premiere_demande == dt.date(2026, 10, 5)
    assert p.date_derniere_relance == dt.date(2026, 10, 12)


def test_parcours_controle_non_conforme_puis_nouvelle_piece():
    p = appliquer(piece(E.ATTENDUE), V.RELANCE_ENVOYEE, aujourdhui=AUJ, valide_par="Alice")
    p = tente(p, V.PIECE_RATTACHEE, id_piece="P-1")
    p = tente(p, V.CONTROLE_NON_CONFORME, motif="facture illisible")
    assert (p.etat, p.pieces_rattachees, p.nb_relances) == (E.DEMANDEE, (), 1)
    p = tente(p, V.PIECE_RATTACHEE, id_piece="P-2")
    assert p.pieces_rattachees == ("P-2",)
    assert tente(p, V.CONTROLE_CONFORME).etat is E.VALIDEE


def test_parcours_jusqu_au_plafond_puis_escalade_et_classement():
    p = piece(E.ATTENDUE)
    for i in range(PLAFOND_RELANCES):
        p = tente(p, V.RELANCE_ENVOYEE)
        assert p.nb_relances == i + 1
    refuse(p, V.RELANCE_ENVOYEE, **CTX)
    p = tente(p, V.DELAI_DEPASSE)
    assert (p.etat, p.nb_relances) == (E.ESCALADEE, PLAFOND_RELANCES)
    assert etat_periode([p]) is EtatPeriode.EN_COLLECTE
    p = tente(p, V.ARBITRAGE_CLASSEMENT, motif="client injoignable")
    assert p.etat is E.CLOSE_SANS_SUITE
    assert etat_periode([p], cloturee=True) is EtatPeriode.CLOTUREE


def test_parcours_escalade_avant_le_plafond_puis_piece_recue():
    p = tente(piece(E.ATTENDUE), V.RELANCE_ENVOYEE)
    p = tente(p, V.DELAI_DEPASSE)  # cadence.planifier a juge l'escalade due
    assert (p.etat, p.nb_relances) == (E.ESCALADEE, 1)
    refuse(p, V.RELANCE_ENVOYEE, **CTX)
    p = tente(p, V.PIECE_RATTACHEE)
    assert p.etat is E.RECUE
    assert tente(p, V.CONTROLE_CONFORME).etat is E.VALIDEE


def test_parcours_blocage_en_cours_de_route():
    p = tente(piece(E.ATTENDUE), V.RELANCE_ENVOYEE)
    p = tente(p, V.BLOCAGE_SIGNALE, motif="litige avec le client")
    refuse(p, V.RELANCE_ENVOYEE, **CTX)
    refuse(p, V.PROMESSE_SAISIE, **CTX)
    p = tente(p, V.BLOCAGE_LEVE)
    assert tente(p, V.RELANCE_ENVOYEE).nb_relances == 2
