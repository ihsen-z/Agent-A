"""Tests du routage des pieces entrantes (contrat 4.4, critere CA-03 bloquant).

Ecrits en adversaire : chaque test cherche une facon d'envoyer la piece d'un
client dans le dossier d'un autre, ou de perdre une piece en silence.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
import sys
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rapprochement.modeles import (
    ETATS_TERMINAUX,
    DecisionRoutage,
    Dossier,
    EtatPiece,
    FichierEntrant,
    MessageEntrant,
    MotifNonRoute,
    PieceAttendue,
    StatutRoutage,
)
from rapprochement.moteur import montants_compatibles
from rapprochement.routage import (
    DOMAINES_GRAND_PUBLIC,
    DOMAINES_GRAND_PUBLIC_CONTRAT,
    adresses_par_dossier,
    cle_idempotence,
    domaines_par_dossier,
    extraire_montants,
    lire_dossier_eml,
    lire_eml,
    router,
)

AUJOURDHUI = dt.date(2026, 5, 4)
RECU = dt.datetime(2026, 5, 4, 9, 0, tzinfo=dt.timezone.utc)
PDF = b"%PDF-1.4 facture"


# ---------------------------------------------------------------------------
# Fabriques
# ---------------------------------------------------------------------------


def attendue(
    reference: str,
    dossier: str,
    montant: str,
    date_operation: dt.date = dt.date(2026, 4, 15),
    etat: EtatPiece = EtatPiece.ATTENDUE,
    periode: str | None = None,
) -> PieceAttendue:
    return PieceAttendue(
        reference=reference,
        dossier=dossier,
        periode=periode or f"{date_operation.year:04d}-{date_operation.month:02d}",
        montant=Decimal(montant),
        date_operation=date_operation,
        libelle=f"OPERATION {reference}",
        etat=etat,
    )


def message(
    expediteur: str,
    *noms: str,
    objet: str = "Votre demande",
    corps: str = "Bonjour, ci-joint.",
    message_id: str = "<m1@client>",
    contenus: tuple[bytes, ...] | None = None,
) -> MessageEntrant:
    if contenus is None:
        contenus = tuple(PDF + n.encode() for n in noms)
    return MessageEntrant(
        message_id=message_id,
        expediteur=expediteur,
        recu_le=RECU,
        objet=objet,
        corps=corps,
        fichiers=tuple(FichierEntrant(nom=n, contenu=c) for n, c in zip(noms, contenus)),
    )


def route_un(msg: MessageEntrant, adresses, attendues, deja_vus=(), domaines=None) -> DecisionRoutage:
    decisions = router(msg, adresses=adresses, attendues=attendues, deja_vus=deja_vus,
                       aujourdhui=AUJOURDHUI, domaines=domaines)
    assert len(decisions) == 1
    return decisions[0]


def assert_non_routee(d: DecisionRoutage, motif: MotifNonRoute) -> None:
    assert d.statut is StatutRoutage.NON_ROUTEE, d
    assert d.motif is motif, d
    assert d.reference_operation is None


# ---------------------------------------------------------------------------
# 1. extraire_montants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texte, attendu",
    [
        ("1 234,56", ["1234.56"]),
        ("1.234,56", ["1234.56"]),
        ("1234.56", ["1234.56"]),
        ("1,234.56", ["1234.56"]),
        ("89,90 EUR", ["89.90"]),
        ("89.90 €", ["89.90"]),
        ("89,90€", ["89.90"]),
        ("€89,90", ["89.90"]),
        ("EUR 89,90", ["89.90"]),
        ("89,90 euros", ["89.90"]),
        ("1 234,56", ["1234.56"]),          # espace insecable
        ("1 234,56 €", ["1234.56"]),        # espace fine insecable
        ("1 234,56 €", ["1234.56"]),
        ("Total : 1.234.567,89 EUR", ["1234567.89"]),
        ("89 €", ["89.00"]),                    # entier + devise : montant
        ("€ 1 234", ["1234.00"]),
        ("1,5 €", ["1.50"]),
        ("facture_89.90.pdf", ["89.90"]),
        ("2026-04-12_89,90.pdf", ["89.90"]),
        ("Facture 2026 n°4471 : 89,90 €", ["89.90"]),
        ("HT 75,00 TTC 90,00", ["75.00", "90.00"]),
        ("89,90 puis 89,90", ["89.90", "89.90"]),
        ("", []),
    ],
)
def test_extraire_montants_formats(texte: str, attendu: list[str]) -> None:
    assert extraire_montants(texte) == [Decimal(x) for x in attendu]


@pytest.mark.parametrize(
    "texte",
    [
        "2026",
        "facture 20260412",
        "TVA 20%",
        "TVA 20 %",
        "TVA 20,00 %",
        "TVA 5,50 %",
        "taux 5,5",
        "12/04",
        "le 12/04/2026",
        "12.04.2026",
        "12.04.26",
        "2026-04-12",
        "n°4471",
        "N° 4471",
        "FA2026.04",
        "ref #12,50",
        "14:30",
        "1234.567",
        "1,234,56",                      # forme incoherente : rien plutot qu'un montant invente
        "0.500 €",
        "06 12 34 56 78",
        "FR76 3000 6000 0112 3456 7890 189",
        "FOURNISSEUR 89",                # EUR au milieu d'un mot n'est pas une devise
        "89 Europe",
        "89,90kg",
        "10,00/12,00",
        "٨٩,٩٠ €",  # chiffres non ASCII
        "facture 202604",
    ],
)
def test_extraire_montants_non_montants(texte: str) -> None:
    assert extraire_montants(texte) == []


# ---------------------------------------------------------------------------
# 2. Isolement des dossiers
# ---------------------------------------------------------------------------

ADRESSES = {
    "A": frozenset({"compta@alpha.fr"}),
    "B": frozenset({"compta@beta.fr"}),
}


def test_expediteur_de_a_ne_vise_jamais_b_meme_seul_candidat() -> None:
    # B est le SEUL dossier a avoir une piece attendue a ce montant.
    pieces = [attendue("B-1", "B", "89.90")]
    d = route_un(message("compta@alpha.fr", "facture_89,90.pdf"), ADRESSES, pieces)
    assert_non_routee(d, MotifNonRoute.AUCUN_CANDIDAT)
    assert d.dossier == "A"


def test_deux_dossiers_meme_montant_chacun_le_sien() -> None:
    pieces = [attendue("A-1", "A", "89.90"), attendue("B-1", "B", "89.90")]
    da = route_un(message("compta@alpha.fr", "facture_89,90.pdf"), ADRESSES, pieces)
    db = route_un(message("compta@beta.fr", "facture_89,90.pdf"), ADRESSES, pieces)
    assert (da.statut, da.dossier, da.reference_operation) == (StatutRoutage.PROPOSEE, "A", "A-1")
    assert (db.statut, db.dossier, db.reference_operation) == (StatutRoutage.PROPOSEE, "B", "B-1")


def test_meme_reference_dans_deux_dossiers() -> None:
    pieces = [attendue("OP-1", "B", "89.90"), attendue("OP-1", "A", "120.00")]
    d = route_un(message("compta@alpha.fr", "f_89,90.pdf"), ADRESSES, pieces)
    assert_non_routee(d, MotifNonRoute.AUCUN_CANDIDAT)


def test_attendues_d_un_dossier_absent_des_adresses_ignorees() -> None:
    pieces = [attendue("Z-1", "Z", "89.90")]
    d = route_un(message("x@zeta.fr", "f_89,90.pdf"), ADRESSES, pieces)
    assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)
    assert d.dossier is None


@pytest.mark.parametrize(
    "expediteur",
    ["", "   ", "pas-une-adresse", "a@b@alpha.fr", "Jean <compta@alpha.fr>",
     "compta@alpha.fr, x@beta.fr", "compta@", "@alpha.fr", "+tag@alpha.fr", "compta@alpha.fr.."],
)
def test_expediteur_illisible_jamais_route(expediteur: str) -> None:
    pieces = [attendue("A-1", "A", "89.90")]
    d = route_un(message(expediteur, "f_89,90.pdf"), ADRESSES, pieces)
    assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)


@pytest.mark.parametrize("exp", ["jean+x@client.fr", "Jean+X@CLIENT.FR", "jean@client.fr.",
                                 "jean+a+b@client.fr"])
def test_suffixe_plus_meme_boite(exp: str) -> None:
    adresses = {"A": frozenset({"jean@client.fr"}), "B": frozenset({"paul@beta.fr"})}
    pieces = [attendue("A-1", "A", "89.90"), attendue("B-1", "B", "89.90")]
    d = route_un(message(exp, "f_89,90.pdf"), adresses, pieces)
    assert (d.statut, d.dossier, d.reference_operation) == (StatutRoutage.PROPOSEE, "A", "A-1")


def test_suffixe_plus_cote_referentiel() -> None:
    adresses = {"A": frozenset({"jean+cabinet@client.fr"})}
    d = route_un(message("jean@client.fr", "f_89,90.pdf"), adresses, [attendue("A-1", "A", "89.90")])
    assert (d.statut, d.dossier) == (StatutRoutage.PROPOSEE, "A")


@pytest.mark.parametrize("exp", ["jean+x@autre.fr", "j.ean@client.fr", "jean@sub.client.fr",
                                 "jeanx@client.fr", "x+jean@client.fr"])
def test_suffixe_plus_rien_d_autre_n_est_normalise(exp: str) -> None:
    adresses = {"A": frozenset({"jean@client.fr"})}
    d = route_un(message(exp, "f_89,90.pdf"), adresses, [attendue("A-1", "A", "89.90")])
    assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)


def test_contacts_identiques_apres_suffixe_plus_ambigu() -> None:
    adresses = {"A": frozenset({"jean+a@client.fr"}), "B": frozenset({"JEAN+b@client.fr"})}
    pieces = [attendue("A-1", "A", "89.90")]
    for exp in ("jean@client.fr", "jean+a@client.fr", "jean+b@client.fr"):
        d = route_un(message(exp, "f_89,90.pdf"), adresses, pieces)
        assert_non_routee(d, MotifNonRoute.DOSSIER_AMBIGU)
        assert d.dossier is None


def test_casse_et_espaces_de_l_expediteur() -> None:
    pieces = [attendue("A-1", "A", "89.90")]
    d = route_un(message("  Compta@ALPHA.fr ", "f_89,90.pdf"), ADRESSES, pieces)
    assert (d.statut, d.dossier) == (StatutRoutage.PROPOSEE, "A")


# ---------------------------------------------------------------------------
# 3. Domaines grand public
# ---------------------------------------------------------------------------


# Messageries absentes de toute liste noire (revue adversariale) : seule la
# regle "domaine DECLARE uniquement" les neutralise.
HORS_LISTE = ["hotmail.ca", "yahoo.ca", "live.co.uk", "t-online.de", "libero.it", "videotron.ca"]


@pytest.mark.parametrize("domaine", HORS_LISTE)
def test_messagerie_hors_liste_noire_ne_capte_rien(domaine: str) -> None:
    # Le contact de A est chez ce fournisseur ; A a la seule piece au bon montant.
    adresses = {"A": frozenset({f"gerant.a@{domaine}"}), "B": frozenset({"paul@client-b.fr"})}
    pieces = [attendue("A-1", "A", "89.90"), attendue("B-1", "B", "120.00")]
    for domaines in (None, {}, {"B": frozenset({"client-b.fr"})}):
        d = route_un(message(f"inconnu@{domaine}", "facture_89,90.pdf"), adresses, pieces,
                     domaines=domaines)
        assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)
        assert d.dossier is None
    # L'adresse exacte du contact, elle, reste une preuve.
    d = route_un(message(f"gerant.a@{domaine}", "facture_89,90.pdf"), adresses, pieces)
    assert (d.statut, d.dossier, d.reference_operation) == (StatutRoutage.PROPOSEE, "A", "A-1")


@pytest.mark.parametrize("domaine", sorted(DOMAINES_GRAND_PUBLIC_CONTRAT) + ["live.fr", "msn.com", "mail.gmail.com"])
def test_domaine_grand_public_jamais_une_preuve(domaine: str) -> None:
    # A est le SEUL dossier sur ce domaine, et A a une piece au bon montant.
    adresses = {"A": frozenset({f"dirigeant@{domaine}"}), "B": frozenset({"compta@beta.fr"})}
    pieces = [attendue("A-1", "A", "89.90")]
    d = route_un(message(f"inconnu@{domaine}", "facture_89,90.pdf"), adresses, pieces)
    assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)
    assert d.dossier is None


@pytest.mark.parametrize("domaine", ["gmail.com", "GMAIL.com.", "orange.fr", "msn.com", "mail.gmail.com"])
def test_domaine_grand_public_declare_par_erreur_refuse(domaine: str) -> None:
    adresses = {"A": frozenset({"compta@alpha.fr"})}
    pieces = [attendue("A-1", "A", "89.90")]
    exp = "inconnu@" + domaine.lower().rstrip(".")
    d = route_un(message(exp, "facture_89,90.pdf"), adresses, pieces, domaines={"A": [domaine]})
    assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)
    assert domaines_par_dossier({"A": dossier("A", "compta@alpha.fr", domaines=(domaine,))}) == {"A": frozenset()}


def test_adresse_exacte_grand_public_reste_valable() -> None:
    adresses = {"A": frozenset({"dirigeant@gmail.com"}), "B": frozenset({"compta@beta.fr"})}
    pieces = [attendue("A-1", "A", "89.90")]
    d = route_un(message("dirigeant@gmail.com", "facture_89,90.pdf"), adresses, pieces)
    assert (d.statut, d.dossier, d.reference_operation) == (StatutRoutage.PROPOSEE, "A", "A-1")


def test_liste_du_contrat_incluse() -> None:
    assert DOMAINES_GRAND_PUBLIC_CONTRAT <= DOMAINES_GRAND_PUBLIC
    assert len(DOMAINES_GRAND_PUBLIC_CONTRAT) == 18


# ---------------------------------------------------------------------------
# 4. Domaine d'entreprise partage, adresse partagee
# ---------------------------------------------------------------------------


def test_domaine_declare_par_deux_dossiers_ambigu() -> None:
    adresses = {"A": frozenset({"pdg@groupe.fr"}), "B": frozenset({"daf@groupe.fr"})}
    domaines = {"A": ["groupe.fr"], "B": ["Groupe.FR."]}
    pieces = [attendue("A-1", "A", "89.90")]
    d = route_un(message("compta@groupe.fr", "f_89,90.pdf"), adresses, pieces, domaines=domaines)
    assert_non_routee(d, MotifNonRoute.DOSSIER_AMBIGU)
    assert d.dossier is None


def test_domaine_commun_non_declare_inconnu() -> None:
    adresses = {"A": frozenset({"pdg@groupe.fr"}), "B": frozenset({"daf@groupe.fr"})}
    d = route_un(message("compta@groupe.fr", "f_89,90.pdf"), adresses, [attendue("A-1", "A", "89.90")])
    assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)


def test_domaine_partage_mais_adresse_exacte_unique() -> None:
    adresses = {"A": frozenset({"pdg@groupe.fr"}), "B": frozenset({"daf@groupe.fr"})}
    pieces = [attendue("A-1", "A", "89.90"), attendue("B-1", "B", "89.90")]
    d = route_un(message("daf@groupe.fr", "f_89,90.pdf"), adresses, pieces,
                 domaines={"A": ["groupe.fr"], "B": ["groupe.fr"]})
    assert (d.statut, d.dossier, d.reference_operation) == (StatutRoutage.PROPOSEE, "B", "B-1")


def test_adresse_exacte_dans_deux_dossiers_ambigu() -> None:
    adresses = {"A": frozenset({"compta@holding.fr"}), "B": frozenset({"compta@holding.fr"})}
    pieces = [attendue("A-1", "A", "89.90")]
    d = route_un(message("compta@holding.fr", "f_89,90.pdf"), adresses, pieces)
    assert_non_routee(d, MotifNonRoute.DOSSIER_AMBIGU)


def test_adresse_grand_public_partagee_ambigue() -> None:
    adresses = {"A": frozenset({"famille@gmail.com"}), "B": frozenset({"famille@gmail.com"})}
    d = route_un(message("famille@gmail.com", "f_89,90.pdf"), adresses, [attendue("A-1", "A", "89.90")])
    assert_non_routee(d, MotifNonRoute.DOSSIER_AMBIGU)
    assert d.dossier is None


@pytest.mark.parametrize("declare", ["alpha.fr", "ALPHA.fr", "alpha.fr.", " alpha.fr "])
def test_domaine_declare_route(declare: str) -> None:
    pieces = [attendue("A-1", "A", "89.90"), attendue("B-1", "B", "89.90")]
    d = route_un(message("assistante@alpha.fr", "f_89,90.pdf"), ADRESSES, pieces,
                 domaines={"A": [declare], "B": ["beta.fr"]})
    assert (d.statut, d.dossier, d.reference_operation) == (StatutRoutage.PROPOSEE, "A", "A-1")


def test_domaine_declare_punycode_route() -> None:
    pieces = [attendue("A-1", "A", "89.90")]
    for declare, exp in (("xn--strae-oqa.de", "x@xn--strae-oqa.de"), ("XN--Strae-oqa.DE.", "x@xn--strae-oqa.de.")):
        d = route_un(message(exp, "f_89,90.pdf"), {"A": frozenset()}, pieces, domaines={"A": [declare]})
        assert (d.statut, d.dossier) == (StatutRoutage.PROPOSEE, "A")


def test_domaine_non_ascii_jamais_compare() -> None:
    pieces = [attendue("A-1", "A", "89.90")]
    vide = {"A": frozenset()}
    # IDNA 2003 replierait straße.de sur strasse.de : un autre domaine.
    for exp in ("x@strasse.de", "x@straße.de", "x@xn--strae-oqa.de"):
        d = route_un(message(exp, "f_89,90.pdf"), vide, pieces, domaines={"A": ["straße.de"]})
        assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)
    # Expediteur non ASCII : ne correspond a aucun domaine declare, meme punycode.
    for exp in ("x@straße.de", "x@société.fr"):
        d = route_un(message(exp, "f_89,90.pdf"), vide, pieces,
                     domaines={"A": ["xn--strae-oqa.de", "xn--socit-esab.fr", "strasse.de"]})
        assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)
    # Un domaine non ASCII ignore n'empeche pas les autres domaines du dossier.
    d = route_un(message("x@alpha.fr", "f_89,90.pdf"), vide, pieces, domaines={"A": ["straße.de", "alpha.fr"]})
    assert (d.statut, d.dossier) == (StatutRoutage.PROPOSEE, "A")
    assert domaines_par_dossier(
        {"A": dossier("A", "a@alpha.fr", domaines=("straße.de", "société.fr", "Alpha.FR."))}
    ) == {"A": frozenset({"alpha.fr"})}
    # Homoglyphe cyrillique : autre domaine, aucune correspondance.
    d = route_un(message("x@\u0430lpha.fr", "f_89,90.pdf"), ADRESSES, pieces, domaines={"A": ["alpha.fr"]})
    assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)


@pytest.mark.parametrize("domaines", [None, {}, {"A": []}, {"B": ["beta.fr"]}])
def test_domaine_non_declare_ne_route_plus(domaines) -> None:
    # alpha.fr est le domaine du contact de A, mais personne ne l'a declare.
    pieces = [attendue("A-1", "A", "89.90")]
    d = route_un(message("assistante@alpha.fr", "f_89,90.pdf"), ADRESSES, pieces, domaines=domaines)
    assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)
    assert d.dossier is None


def test_adresse_exacte_prime_sur_domaine_declare() -> None:
    # B a declare alpha.fr (filiale) mais compta@alpha.fr est le contact exact de A.
    pieces = [attendue("A-1", "A", "89.90"), attendue("B-1", "B", "89.90")]
    d = route_un(message("compta@alpha.fr", "f_89,90.pdf"), ADRESSES, pieces, domaines={"B": ["alpha.fr"]})
    assert (d.statut, d.dossier) == (StatutRoutage.PROPOSEE, "A")


def test_sous_domaine_n_est_pas_le_domaine() -> None:
    pieces = [attendue("A-1", "A", "89.90")]
    for exp in ("x@mail.alpha.fr", "x@alpha.fr.evil.com", "x@xalpha.fr"):
        d = route_un(message(exp, "f_89,90.pdf"), ADRESSES, pieces, domaines={"A": ["alpha.fr"]})
        assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)
    # Et un domaine parent n'est pas couvert par un sous-domaine declare.
    d = route_un(message("x@alpha.fr", "f_89,90.pdf"), {"A": frozenset()}, pieces,
                 domaines={"A": ["compta.alpha.fr"]})
    assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)


def test_domaine_inconnu() -> None:
    d = route_un(message("x@inconnu.fr", "f_89,90.pdf"), ADRESSES, [attendue("A-1", "A", "89.90")])
    assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)


def test_adresses_mal_formees_dans_le_referentiel_ignorees() -> None:
    adresses = {"A": ["", "  ", "n'importe quoi", "COMPTA@Alpha.fr "], "B": frozenset({"compta@beta.fr"})}
    d = route_un(message("compta@alpha.fr", "f_89,90.pdf"), adresses, [attendue("A-1", "A", "89.90")])
    assert (d.statut, d.dossier) == (StatutRoutage.PROPOSEE, "A")


# ---------------------------------------------------------------------------
# adresses_par_dossier
# ---------------------------------------------------------------------------


def dossier(code: str, contact: str, circuit: str = "C-DIRECT", relais: str = "",
            domaines: tuple[str, ...] = ()) -> Dossier:
    return Dossier(code=code, raison_sociale=code, email_contact=contact, nom_contact="",
                   circuit=circuit, email_relais=relais, domaines=domaines)


def test_domaines_par_dossier() -> None:
    dossiers = {
        "A": dossier("A", "compta@alpha.fr", domaines=("Alpha.FR.", "alpha-groupe.fr")),
        "B": dossier("B", "x@hotmail.ca"),                                 # rien n'est deduit
        "C": dossier("C", "c@gamma.fr", domaines=("gmail.com", "pas un domaine", "gamma.fr")),
        "D": dossier("D", "d@delta.fr", domaines=("société.fr",)),
    }
    assert domaines_par_dossier(dossiers) == {
        "A": frozenset({"alpha.fr", "alpha-groupe.fr"}),
        "B": frozenset(),
        "C": frozenset({"gamma.fr"}),
        "D": frozenset(),                                                 # non ASCII : ignore
    }
    with pytest.raises(ValueError):
        domaines_par_dossier({"A": dossier("B", "x@beta.fr")})


def test_adresses_par_dossier_ne_derive_aucun_domaine() -> None:
    adresses = adresses_par_dossier({"A": dossier("A", "gerant@hotmail.ca", domaines=("alpha.fr",))})
    assert adresses == {"A": frozenset({"gerant@hotmail.ca"})}


def test_adresses_par_dossier_normalise_et_exclut_relais_et_interne() -> None:
    dossiers = {
        "A": dossier("A", "Jean Martin <Compta@Alpha.FR>"),
        "B": dossier("B", "client@beta.fr", circuit="C-RELAIS", relais="tenue@cabinet-donneur.fr"),
        "C": dossier("C", "collaborateur@notre-cabinet.fr", circuit="C-INTERNE"),
        "D": dossier("D", "a@delta.fr; b@delta.fr"),
        "E": dossier("E", ""),
    }
    assert adresses_par_dossier(dossiers) == {
        "A": frozenset({"compta@alpha.fr"}),
        "B": frozenset({"client@beta.fr"}),
        "C": frozenset(),
        "D": frozenset({"a@delta.fr", "b@delta.fr"}),
        "E": frozenset(),
    }


def test_relais_unique_ne_capte_pas_les_pieces_de_ses_autres_clients() -> None:
    # Le cabinet donneur d'ordre n'est relais que de B dans NOTRE referentiel,
    # mais il envoie aussi les pieces d'autres clients.
    dossiers = {
        "A": dossier("A", "compta@alpha.fr"),
        "B": dossier("B", "client@beta.fr", circuit="C-RELAIS", relais="tenue@donneur.fr"),
    }
    adresses = adresses_par_dossier(dossiers)
    pieces = [attendue("B-1", "B", "89.90")]
    for exp in ("tenue@donneur.fr", "autre@donneur.fr"):
        d = route_un(message(exp, "f_89,90.pdf"), adresses, pieces)
        assert_non_routee(d, MotifNonRoute.DOSSIER_INCONNU)


def test_adresses_par_dossier_cle_incoherente() -> None:
    with pytest.raises(ValueError):
        adresses_par_dossier({"A": dossier("B", "x@beta.fr")})


# ---------------------------------------------------------------------------
# Periode
# ---------------------------------------------------------------------------

DEUX_PERIODES = [
    attendue("A-MARS", "A", "89.90", dt.date(2026, 3, 20)),
    attendue("A-AVRIL", "A", "89.90", dt.date(2026, 4, 15)),
]


@pytest.mark.parametrize(
    "nom, reference",
    [
        ("facture_2026-04_89,90.pdf", "A-AVRIL"),
        ("facture_2026-04-10_89,90.pdf", "A-AVRIL"),
        ("facture_202604_89,90.pdf", "A-AVRIL"),
        ("facture_2026-03-15_89,90.pdf", "A-MARS"),
        ("facture_202603_89,90.pdf", "A-MARS"),
    ],
)
def test_periode_arbitree_par_le_nom(nom: str, reference: str) -> None:
    d = route_un(message("compta@alpha.fr", nom), ADRESSES, DEUX_PERIODES)
    assert (d.statut, d.reference_operation) == (StatutRoutage.PROPOSEE, reference)
    assert d.periode == ("2026-04" if reference == "A-AVRIL" else "2026-03")


@pytest.mark.parametrize(
    "nom",
    [
        "facture_89,90.pdf",                      # aucune date
        "facture_20260410_89,90.pdf",             # AAAAMMJJ : hors formats du contrat
        "facture_2026-02_89,90.pdf",              # periode non ouverte
        "releve_2026-03_2026-04_89,90.pdf",       # deux periodes
        "facture_2026-13_89,90.pdf",              # mois invalide
    ],
)
def test_periode_ambigue(nom: str) -> None:
    d = route_un(message("compta@alpha.fr", nom), ADRESSES, DEUX_PERIODES)
    assert_non_routee(d, MotifNonRoute.PERIODE_AMBIGUE)
    assert d.dossier == "A"


def test_periode_d_un_autre_dossier_n_ajoute_pas_d_ambiguite() -> None:
    pieces = [attendue("A-1", "A", "89.90", dt.date(2026, 4, 15)),
              attendue("B-1", "B", "89.90", dt.date(2026, 3, 15))]
    d = route_un(message("compta@alpha.fr", "f_89,90.pdf"), ADRESSES, pieces)
    assert (d.statut, d.reference_operation) == (StatutRoutage.PROPOSEE, "A-1")


def test_pieces_terminales_ignorees() -> None:
    pieces = [attendue(f"A-{e.name}", "A", "89.90", dt.date(2026, 3, 1), etat=e) for e in ETATS_TERMINAUX]
    pieces.append(attendue("A-OUVERTE", "A", "89.90", dt.date(2026, 4, 15)))
    d = route_un(message("compta@alpha.fr", "f_89,90.pdf"), ADRESSES, pieces)
    assert (d.statut, d.periode, d.reference_operation) == (StatutRoutage.PROPOSEE, "2026-04", "A-OUVERTE")


def test_dossier_sans_piece_ouverte() -> None:
    pieces = [attendue("A-1", "A", "89.90", etat=EtatPiece.VALIDEE)]
    d = route_un(message("compta@alpha.fr", "f_89,90.pdf"), ADRESSES, pieces)
    assert_non_routee(d, MotifNonRoute.AUCUN_CANDIDAT)


# ---------------------------------------------------------------------------
# Montant et operation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nom, objet, corps",
    [
        ("facture.pdf", "Facture", "Bonjour"),                          # aucun montant
        ("facture_89,90.pdf", "Facture", "dont TVA 14,98 EUR"),         # deux montants
        ("facture_20260412.pdf", "Facture 4471", "le 12/04, TVA 20%"),  # que des non-montants
    ],
)
def test_montant_inconnu(nom: str, objet: str, corps: str) -> None:
    d = route_un(message("compta@alpha.fr", nom, objet=objet, corps=corps), ADRESSES,
                 [attendue("A-1", "A", "89.90")])
    assert_non_routee(d, MotifNonRoute.MONTANT_INCONNU)


def test_meme_montant_repete_compte_une_fois() -> None:
    d = route_un(message("compta@alpha.fr", "f_89,90.pdf", objet="Facture 89,90 EUR",
                         corps="Montant : 89.90 €"), ADRESSES, [attendue("A-1", "A", "89.90")])
    assert d.statut is StatutRoutage.PROPOSEE


def test_montant_lu_dans_le_corps() -> None:
    d = route_un(message("compta@alpha.fr", "facture.pdf", corps="Montant TTC : 1 234,56 €"),
                 ADRESSES, [attendue("A-1", "A", "1234.56")])
    assert (d.statut, d.reference_operation) == (StatutRoutage.PROPOSEE, "A-1")


def test_tolerance_du_moteur() -> None:
    pieces = [attendue("A-1", "A", "100.00")]
    ok = route_un(message("compta@alpha.fr", "f_100,02.pdf"), ADRESSES, pieces)
    ko = route_un(message("compta@alpha.fr", "f_100,60.pdf"), ADRESSES, pieces)
    assert ok.statut is StatutRoutage.PROPOSEE
    assert_non_routee(ko, MotifNonRoute.AUCUN_CANDIDAT)


def test_candidats_multiples() -> None:
    pieces = [attendue("A-1", "A", "89.90", dt.date(2026, 4, 3)),
              attendue("A-2", "A", "89.90", dt.date(2026, 4, 20))]
    d = route_un(message("compta@alpha.fr", "f_89,90.pdf"), ADRESSES, pieces)
    assert_non_routee(d, MotifNonRoute.CANDIDATS_MULTIPLES)
    assert "A-1" in d.detail and "A-2" in d.detail


def test_doublon_de_piece_attendue_jamais_tranche() -> None:
    p = attendue("A-1", "A", "89.90")
    d = route_un(message("compta@alpha.fr", "f_89,90.pdf"), ADRESSES, [p, p])
    assert_non_routee(d, MotifNonRoute.CANDIDATS_MULTIPLES)


# ---------------------------------------------------------------------------
# 5. Fenetre de dates : [-60 j, +15 j], bornes incluses
# ---------------------------------------------------------------------------

OPERATION = dt.date(2026, 4, 15)


@pytest.mark.parametrize(
    "date_nom, accepte",
    [
        (OPERATION - dt.timedelta(days=60), True),
        (OPERATION - dt.timedelta(days=61), False),
        (OPERATION + dt.timedelta(days=15), True),
        (OPERATION + dt.timedelta(days=16), False),
        (OPERATION, True),
    ],
)
def test_bornes_de_la_fenetre(date_nom: dt.date, accepte: bool) -> None:
    pieces = [attendue("A-1", "A", "89.90", OPERATION)]
    d = route_un(message("compta@alpha.fr", f"facture_{date_nom.isoformat()}_89,90.pdf"), ADRESSES, pieces)
    if accepte:
        assert (d.statut, d.reference_operation) == (StatutRoutage.PROPOSEE, "A-1")
    else:
        assert_non_routee(d, MotifNonRoute.AUCUN_CANDIDAT)


def test_fenetre_au_mois() -> None:
    pieces = [attendue("A-1", "A", "89.90", OPERATION)]
    # Fevrier 2026 contient le 14/02 (J-60) : compatible. Janvier 2026 : non.
    assert route_un(message("compta@alpha.fr", "f_2026-02_89,90.pdf"), ADRESSES, pieces).statut \
        is StatutRoutage.PROPOSEE
    assert_non_routee(route_un(message("compta@alpha.fr", "f_202601_89,90.pdf"), ADRESSES, pieces),
                      MotifNonRoute.AUCUN_CANDIDAT)


def test_fenetre_departage_deux_operations() -> None:
    pieces = [attendue("A-1", "A", "89.90", dt.date(2026, 4, 2)),
              attendue("A-2", "A", "89.90", dt.date(2026, 4, 28))]
    # 2026-04-20 : dans la fenetre de A-2 (J-8), hors de celle de A-1 (J+18).
    d = route_un(message("compta@alpha.fr", "f_2026-04-20_89,90.pdf"), ADRESSES, pieces)
    assert (d.statut, d.reference_operation) == (StatutRoutage.PROPOSEE, "A-2")


# ---------------------------------------------------------------------------
# 6. Idempotence
# ---------------------------------------------------------------------------


def test_rejeu_donne_doublon() -> None:
    pieces = [attendue("A-1", "A", "89.90")]
    msg = message("compta@alpha.fr", "f_89,90.pdf")
    premiere = route_un(msg, ADRESSES, pieces)
    assert premiere.statut is StatutRoutage.PROPOSEE
    vus = {cle_idempotence(premiere.message_id, premiere.empreinte)}
    rejeu = route_un(msg, ADRESSES, pieces, deja_vus=vus)
    assert rejeu.statut is StatutRoutage.DOUBLON
    assert (rejeu.dossier, rejeu.reference_operation, rejeu.motif) == (None, None, None)
    assert rejeu.empreinte == premiere.empreinte


def test_rejeu_d_un_message_non_route_et_sans_piece() -> None:
    sans = message("compta@alpha.fr")
    d = route_un(sans, ADRESSES, [])
    assert_non_routee(d, MotifNonRoute.SANS_PIECE_JOINTE)
    assert d.empreinte == ""
    assert route_un(sans, ADRESSES, [], deja_vus=[cle_idempotence(sans.message_id, "")]).statut \
        is StatutRoutage.DOUBLON
    inconnu = message("x@gmail.com", "f.pdf")
    d = route_un(inconnu, ADRESSES, [])
    assert route_un(inconnu, ADRESSES, [], deja_vus=[cle_idempotence(d.message_id, d.empreinte)]).statut \
        is StatutRoutage.DOUBLON


def test_meme_contenu_autre_message_n_est_pas_un_doublon() -> None:
    pieces = [attendue("A-1", "A", "89.90")]
    m1 = message("compta@alpha.fr", "f_89,90.pdf", message_id="<1@x>")
    m2 = message("compta@alpha.fr", "f_89,90.pdf", message_id="<2@x>")
    d1 = route_un(m1, ADRESSES, pieces)
    d2 = route_un(m2, ADRESSES, pieces, deja_vus={cle_idempotence(d1.message_id, d1.empreinte)})
    assert d2.statut is StatutRoutage.PROPOSEE


def test_meme_fichier_joint_deux_fois_un_seul_effet() -> None:
    msg = message("compta@alpha.fr", "a_89,90.pdf", "b_89,90.pdf", contenus=(PDF, PDF))
    d = router(msg, adresses=ADRESSES, attendues=[attendue("A-1", "A", "89.90")], aujourdhui=AUJOURDHUI)
    assert [x.statut for x in d] == [StatutRoutage.PROPOSEE, StatutRoutage.DOUBLON]


def test_cle_idempotence() -> None:
    e = hashlib.sha256(PDF).hexdigest()
    cle = cle_idempotence("<m@x>", e)
    assert len(cle) == 64 and int(cle, 16) >= 0
    assert cle == cle_idempotence("<m@x>", e)
    assert cle != cle_idempotence("<m@y>", e)
    assert cle != cle_idempotence("<m@x>", hashlib.sha256(b"autre").hexdigest())
    # Un Message-ID qui embarque une empreinte ne collisionne pas.
    assert cle_idempotence("<m@x>" + e, "") != cle_idempotence("<m@x>", e)


def test_message_id_vide_refuse() -> None:
    with pytest.raises(ValueError):
        router(message("compta@alpha.fr", "f.pdf", message_id="  "), adresses=ADRESSES,
               attendues=[], aujourdhui=AUJOURDHUI)


def test_une_decision_par_fichier_dans_l_ordre() -> None:
    pieces = [attendue("A-1", "A", "89.90"), attendue("A-2", "A", "120.00")]
    msg = message("compta@alpha.fr", "z_120,00.pdf", "a_89,90.pdf", "notes.txt", corps="")
    d = router(msg, adresses=ADRESSES, attendues=pieces, aujourdhui=AUJOURDHUI)
    assert [x.nom_fichier for x in d] == ["z_120,00.pdf", "a_89,90.pdf", "notes.txt"]
    assert [x.reference_operation for x in d] == ["A-2", "A-1", None]
    assert d[2].motif is MotifNonRoute.MONTANT_INCONNU
    assert all(x.empreinte == hashlib.sha256(f.contenu).hexdigest() for x, f in zip(d, msg.fichiers))


def test_attendues_generateur_consomme_une_fois() -> None:
    gen = (p for p in [attendue("A-1", "A", "89.90")])
    msg = message("compta@alpha.fr", "a_89,90.pdf", "b_89,90.pdf")
    d = router(msg, adresses=ADRESSES, attendues=gen, aujourdhui=AUJOURDHUI)
    assert [x.reference_operation for x in d] == ["A-1", "A-1"]


# ---------------------------------------------------------------------------
# 7. Lecture .eml
# ---------------------------------------------------------------------------


def ecrire(tmp_path: Path, nom: str, contenu: bytes) -> Path:
    chemin = tmp_path / nom
    chemin.write_bytes(contenu)
    return chemin


def mail(expediteur: str = "Jean Martin <Compta@Alpha.FR>", message_id: str | None = "<abc@alpha.fr>") -> EmailMessage:
    m = EmailMessage()
    m["From"] = expediteur
    m["To"] = "pieces@cabinet.fr"
    m["Subject"] = "Facture avril 89,90 EUR"
    m["Date"] = "Mon, 04 May 2026 10:15:00 +0200"
    if message_id is not None:
        m["Message-ID"] = message_id
    m.set_content("Bonjour,\nci-joint la facture.\n")
    return m


def test_lire_eml_simple(tmp_path: Path) -> None:
    m = mail()
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="facture.pdf")
    lu = lire_eml(ecrire(tmp_path, "a.eml", m.as_bytes()))
    assert lu.message_id == "<abc@alpha.fr>"
    assert lu.expediteur == "compta@alpha.fr"
    assert lu.objet == "Facture avril 89,90 EUR"
    assert "ci-joint la facture" in lu.corps
    assert lu.recu_le == dt.datetime(2026, 5, 4, 8, 15, tzinfo=dt.timezone.utc)
    assert lu.recu_le.tzinfo is not None
    assert lu.fichiers == (FichierEntrant(nom="facture.pdf", contenu=PDF),)


def test_lire_eml_nom_rfc2047(tmp_path: Path) -> None:
    m = mail()
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="REMPLACE.pdf")
    encode = "=?utf-8?b?" + __import__("base64").b64encode("Facture été 89,90 €.pdf".encode()).decode() + "?="
    brut = m.as_bytes().replace(b'filename="REMPLACE.pdf"', f'filename="{encode}"'.encode())
    assert encode.encode() in brut
    lu = lire_eml(ecrire(tmp_path, "a.eml", brut))
    assert [f.nom for f in lu.fichiers] == ["Facture été 89,90 €.pdf"]


def test_lire_eml_nom_rfc2231(tmp_path: Path) -> None:
    m = mail()
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="Reçu n°2.pdf")
    lu = lire_eml(ecrire(tmp_path, "a.eml", m.as_bytes()))
    assert [f.nom for f in lu.fichiers] == ["Reçu n°2.pdf"]


@pytest.mark.parametrize(
    "nom, attendu",
    [
        ("../../etc/passwd", "passwd"),
        ("/etc/passwd", "passwd"),
        ("..\\..\\Windows\\facture.pdf", "facture.pdf"),
        ("dossier/sous/../f.pdf", "f.pdf"),
        ("..", "piece_jointe_1"),
        ("../", "piece_jointe_1"),
    ],
)
def test_lire_eml_nom_malveillant(tmp_path: Path, nom: str, attendu: str) -> None:
    m = mail()
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename=nom)
    lu = lire_eml(ecrire(tmp_path, "a.eml", m.as_bytes()))
    assert [f.nom for f in lu.fichiers] == [attendu]
    assert "/" not in lu.fichiers[0].nom and "\\" not in lu.fichiers[0].nom


def test_lire_eml_sans_message_id(tmp_path: Path) -> None:
    m = mail(message_id=None)
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="f.pdf")
    brut = m.as_bytes()
    lu = lire_eml(ecrire(tmp_path, "a.eml", brut))
    assert lu.message_id == "sha256:" + hashlib.sha256(brut).hexdigest()
    # Deterministe : relu, meme identifiant, donc rejeu = DOUBLON.
    assert lire_eml(ecrire(tmp_path, "b.eml", brut)).message_id == lu.message_id


def test_lire_eml_multipart_imbrique(tmp_path: Path) -> None:
    m = mail()
    m.add_alternative("<p>Bonjour <b>HTML</b></p>", subtype="html")
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="un.pdf")
    m.add_attachment(b"PNG", maintype="image", subtype="png", filename="deux.png")
    transfere = EmailMessage()
    transfere["From"] = "fournisseur@loxam.fr"
    transfere["Subject"] = "Votre facture"
    transfere.set_content("Facture jointe")
    transfere.add_attachment(b"%PDF interne", maintype="application", subtype="pdf", filename="trois.pdf")
    m.add_attachment(transfere)
    brut = m.as_bytes()
    assert b"multipart/alternative" in brut and b"message/rfc822" in brut
    lu = lire_eml(ecrire(tmp_path, "a.eml", brut))
    assert [f.nom for f in lu.fichiers] == ["un.pdf", "deux.png", "trois.pdf"]
    assert lu.fichiers[2].contenu == b"%PDF interne"
    assert "ci-joint la facture" in lu.corps            # partie texte preferee
    assert lu.expediteur == "compta@alpha.fr"           # pas l'expediteur du message transfere


def test_lire_eml_html_seul(tmp_path: Path) -> None:
    m = mail()
    m.set_content("<html><style>p{}</style><p>Montant&nbsp;: 89,90&nbsp;&euro;</p></html>", subtype="html")
    lu = lire_eml(ecrire(tmp_path, "a.eml", m.as_bytes()))
    assert "<p>" not in lu.corps
    assert extraire_montants(lu.corps) == [Decimal("89.90")]


def test_lire_eml_sans_piece_jointe_puis_routage(tmp_path: Path) -> None:
    lu = lire_eml(ecrire(tmp_path, "a.eml", mail().as_bytes()))
    assert lu.fichiers == ()
    d = router(lu, adresses=ADRESSES, attendues=[], aujourdhui=AUJOURDHUI)
    assert [(x.statut, x.motif) for x in d] == [(StatutRoutage.NON_ROUTEE, MotifNonRoute.SANS_PIECE_JOINTE)]


def test_lire_eml_piece_jointe_sans_nom(tmp_path: Path) -> None:
    m = mail()
    m.add_attachment(PDF, maintype="application", subtype="pdf")
    lu = lire_eml(ecrire(tmp_path, "a.eml", m.as_bytes()))
    assert [f.nom for f in lu.fichiers] == ["piece_jointe_1"]


@pytest.mark.parametrize(
    "entete",
    [
        "compta@alpha.fr, pirate@beta.fr",         # deux expediteurs
        "Pas une adresse",
        "",
    ],
)
def test_lire_eml_expediteur_douteux_vide(tmp_path: Path, entete: str) -> None:
    brut = mail().as_bytes().replace(b"From: Jean Martin <Compta@Alpha.FR>", f"From: {entete}".encode())
    lu = lire_eml(ecrire(tmp_path, "a.eml", brut))
    assert lu.expediteur == ""
    d = router(lu, adresses=ADRESSES, attendues=[], aujourdhui=AUJOURDHUI)
    assert all(x.statut is not StatutRoutage.PROPOSEE for x in d)


def test_lire_eml_deux_entetes_from(tmp_path: Path) -> None:
    brut = mail().as_bytes().replace(b"To:", b"From: pirate@beta.fr\nTo:", 1)
    assert lire_eml(ecrire(tmp_path, "a.eml", brut)).expediteur == ""


def test_lire_eml_nom_affiche_trompeur(tmp_path: Path) -> None:
    brut = mail().as_bytes().replace(
        b"From: Jean Martin <Compta@Alpha.FR>", b'From: "compta@alpha.fr" <pirate@beta.fr>')
    assert lire_eml(ecrire(tmp_path, "a.eml", brut)).expediteur == "pirate@beta.fr"


def test_lire_dossier_eml_trie_par_nom(tmp_path: Path) -> None:
    for i, nom in enumerate(["c.eml", "a.eml", "b.EML", "notes.txt"]):
        ecrire(tmp_path, nom, mail(message_id=f"<{nom}@x>").as_bytes())
    (tmp_path / "sous.eml").mkdir()
    lus = lire_dossier_eml(tmp_path)
    assert [m.message_id for m in lus] == ["<a.eml@x>", "<b.EML@x>", "<c.eml@x>"]


# ---------------------------------------------------------------------------
# 8. Propriete : jamais de rattachement inter-dossiers (donnees denses)
# ---------------------------------------------------------------------------

MONTANTS_DENSES = ("89.90", "120.00", "45.50", "1234.56", "89.92")
DOMAINE_PARTAGE = "groupe-commun.fr"


Referentiel = tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]


def _referentiel_dense(rng: random.Random) -> Referentiel:
    """(adresses, domaines declares). Volontairement piege :
    contacts sur messageries hors liste noire, domaine declare par plusieurs
    dossiers, adresse partagee, gmail.com declare par erreur, domaines de
    contact NON declares."""
    adresses: dict[str, set[str]] = {}
    domaines: dict[str, set[str]] = {}
    for i in range(16):
        code = f"D{i:02d}"
        adrs = {f"compta@d{i:02d}.fr"}
        doms: set[str] = set()
        if i % 4 == 0:
            doms.add(f"d{i:02d}.fr")                          # domaine declare unique
        if i % 4 == 1:
            adrs = {f"dirigeant{i}@{HORS_LISTE[i % len(HORS_LISTE)]}"}   # messagerie hors liste
        if i % 4 == 2:
            adrs.add(f"dg{i}@{DOMAINE_PARTAGE}")
            doms |= {DOMAINE_PARTAGE, f"d{i:02d}.fr"}          # domaine declare partage
        if i in (3, 7):
            adrs.add("commun@holding-x.fr")                   # adresse partagee
        if i == 5:
            doms.add("gmail.com")                             # erreur de saisie
        adresses[code] = adrs
        if rng.random() < 0.9:                                # parfois aucun domaine declare
            domaines[code] = doms
    return ({k: frozenset(v) for k, v in adresses.items()},
            {k: frozenset(v) for k, v in domaines.items()})


def _proprietaire(expediteur: str, ref: Referentiel) -> str | None:
    """Oracle independant : dossier d'une adresse exacte OU d'un domaine declare."""
    adresses, domaines = ref
    exp = expediteur.strip().lower()
    exacts = [d for d, a in adresses.items() if exp in a]
    if exacts:
        return exacts[0] if len(exacts) == 1 else None
    dom = exp.rpartition("@")[2]
    if dom in DOMAINES_GRAND_PUBLIC or any(dom.endswith("." + g) for g in DOMAINES_GRAND_PUBLIC):
        return None
    par_dom = [d for d, doms in domaines.items() if dom in doms]
    return par_dom[0] if len(par_dom) == 1 else None


def _attendues_denses(rng: random.Random, codes: list[str]) -> list[PieceAttendue]:
    pieces = []
    for code in codes:
        for j in range(rng.randint(0, 7)):
            jour = dt.date(2026, rng.choice((3, 4)), rng.randint(1, 28))
            pieces.append(attendue(
                f"{code}-{j}", code, rng.choice(MONTANTS_DENSES), jour,
                etat=rng.choice(list(EtatPiece)),
            ))
    rng.shuffle(pieces)
    return pieces


def _expediteur_aleatoire(rng: random.Random, ref: Referentiel) -> str:
    connues = sorted(a for v in ref[0].values() for a in v)
    return rng.choice([
        f"w{rng.randint(0, 9)}@" + rng.choice(HORS_LISTE),
        rng.choice(connues),
        rng.choice(connues).upper(),
        f"inconnu{rng.randint(0, 9)}@" + rng.choice(connues).rpartition("@")[2],
        f"x{rng.randint(0, 9)}@" + rng.choice(sorted(DOMAINES_GRAND_PUBLIC)),
        f"y@{DOMAINE_PARTAGE}",
        "commun@holding-x.fr",
        "z@nulle-part.fr",
    ])


def _nom_aleatoire(rng: random.Random) -> str:
    montant = rng.choice(MONTANTS_DENSES).replace(".", ",")
    date = rng.choice(["", "_2026-03", "_2026-04", "_202604", f"_2026-04-{rng.randint(1, 28):02d}",
                       f"_2026-0{rng.choice((2, 3, 5))}-{rng.randint(1, 28):02d}"])
    return f"facture{date}_{montant}.pdf"


def test_propriete_jamais_inter_dossiers() -> None:
    rng = random.Random(20260504)
    nb_proposees = 0
    nb_cas = 0
    nb_pieges = 0
    nb_par_domaine = 0
    for _ in range(120):
        ref = _referentiel_dense(rng)
        adresses, domaines = ref
        pieces = _attendues_denses(rng, sorted(adresses))
        cles = {(p.dossier, p.reference): p for p in pieces}
        for _ in range(12):
            exp = _expediteur_aleatoire(rng, ref)
            noms = [_nom_aleatoire(rng) for _ in range(rng.randint(0, 3))]
            corps = rng.choice(["", "", "Bonjour", "Total 89,90 EUR", "Montant : 120,00 €"])
            msg = message(exp, *noms, corps=corps, message_id=f"<{nb_cas}@x>")
            decisions = router(msg, adresses=adresses, attendues=pieces, aujourdhui=AUJOURDHUI,
                               domaines=domaines)
            nb_cas += 1
            assert len(decisions) == max(1, len(noms))
            proprietaire = _proprietaire(exp, ref)
            exact = any(exp.strip().lower() in a for a in adresses.values())
            for d in decisions:
                montants_lus = set(extraire_montants(f"{d.nom_fichier} {msg.objet} {msg.corps}"))
                if len(montants_lus) == 1 and any(
                    p.dossier != proprietaire and p.etat not in ETATS_TERMINAUX
                    and montants_compatibles(p.montant, next(iter(montants_lus)))
                    for p in pieces
                ):
                    nb_pieges += 1   # un autre dossier avait une piece au bon montant
                if d.dossier is not None:
                    assert d.dossier == proprietaire, (exp, d)
                if d.statut is StatutRoutage.PROPOSEE:
                    nb_proposees += 1
                    nb_par_domaine += not exact
                    assert proprietaire is not None
                    # adresse exacte du dossier, ou domaine qu'IL a declare
                    dom = exp.strip().lower().rpartition("@")[2]
                    assert exp.strip().lower() in adresses[d.dossier] or dom in domaines[d.dossier]
                    assert not any(dom.endswith(h) for h in HORS_LISTE) or exact
                    piece = cles[(d.dossier, d.reference_operation)]
                    assert piece.dossier == proprietaire
                    assert piece.etat not in ETATS_TERMINAUX
                    assert d.motif is None
                    montants = set(extraire_montants(f"{d.nom_fichier} {msg.objet} {msg.corps}"))
                    assert len(montants) == 1
                    assert montants_compatibles(piece.montant, montants.pop())
                else:
                    assert d.reference_operation is None
                    assert (d.motif is None) == (d.statut is StatutRoutage.DOUBLON)
    assert nb_cas >= 500
    assert nb_pieges >= 500, "le generateur doit offrir des candidates dans d'autres dossiers"
    assert nb_proposees >= 50, "le generateur doit vraiment exercer le cas PROPOSEE"
    assert nb_par_domaine >= 10, "le repli par domaine declare doit etre exerce"


def test_propriete_expediteur_grand_public_inconnu_jamais_route() -> None:
    rng = random.Random(7)
    for i in range(500):
        adresses, domaines = _referentiel_dense(rng)
        pieces = _attendues_denses(rng, sorted(adresses))
        exp = f"client{i}@" + rng.choice(sorted(DOMAINES_GRAND_PUBLIC) + HORS_LISTE)
        msg = message(exp, _nom_aleatoire(rng), message_id=f"<gp{i}@x>")
        for d in router(msg, adresses=adresses, attendues=pieces, aujourdhui=AUJOURDHUI,
                        domaines=domaines):
            assert d.statut is StatutRoutage.NON_ROUTEE
            assert d.motif is MotifNonRoute.DOSSIER_INCONNU
            assert d.dossier is None


# ---------------------------------------------------------------------------
# 9. Determinisme
# ---------------------------------------------------------------------------


def test_determinisme() -> None:
    rng = random.Random(42)
    ref = _referentiel_dense(rng)
    adresses, domaines = ref
    pieces = _attendues_denses(rng, sorted(adresses))
    msgs = [message(_expediteur_aleatoire(rng, ref), *[_nom_aleatoire(rng) for _ in range(3)],
                    message_id=f"<{i}@x>") for i in range(100)]

    def tout(attendues, adrs, doms=domaines):
        return [router(m, adresses=adrs, attendues=attendues, aujourdhui=AUJOURDHUI, domaines=doms)
                for m in msgs]

    premier = tout(pieces, adresses)
    assert premier == tout(list(pieces), dict(adresses))
    # L'ordre des entrees ne change rien.
    melange = list(pieces)
    random.Random(1).shuffle(melange)
    inverse = {k: adresses[k] for k in reversed(list(adresses))}
    assert premier == tout(melange, inverse, {k: domaines[k] for k in reversed(list(domaines))})
