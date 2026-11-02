"""Tests de l'interface web (src/rapprochement/web/, scripts/web.py).

Sans reseau pour l'essentiel : l'application WSGI est appelee directement par un petit
client de test (environ + start_response), enveloppe dans `wsgiref.validate.validator`.
L'etat de depart est seme DIRECTEMENT dans le depot (les tests ont le droit d'ecrire
dans la base, l'application non) : ces tests ne dependent pas de `executer_cycle`, qui
exige `fork` et ne tourne pas sous Windows. Un test lance le vrai serveur sur un port
choisi dynamiquement et l'interroge avec `urllib`.

Les tests marques `xfail(strict=True)` reproduisent un defaut d'un AUTRE module.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator
from zoneinfo import ZoneInfo

import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement import cycle  # noqa: E402
from rapprochement.audit import JournalAudit  # noqa: E402
from rapprochement.depot import Depot  # noqa: E402
from rapprochement.modeles import (  # noqa: E402
    Brouillon,
    DecisionRoutage,
    EtatPiece,
    MotifNonRoute,
    PieceAttendue,
    StatutBrouillon,
    StatutRoutage,
    identifiant_relance,
)
from rapprochement.web import comptes  # noqa: E402
from rapprochement.web.application import CSP, Application, cle_entree  # noqa: E402
from rapprochement.web.gabarits import Html, echapper, gabarit  # noqa: E402
from rapprochement.web.serveur import creer_serveur, est_loopback  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
J0 = dt.date(2026, 10, 5)                                     # lundi
LUNDI_10H = dt.datetime(2026, 10, 5, 10, 0, tzinfo=PARIS)
MDP = "motdepasse-de-test-1"
ALICE, BOB = "alice", "bob"
NOM_ALICE, NOM_BOB = "Alice Durand", "Bob Martin"

LIBELLE_XSS = "<script>alert(1)</script>"
NOM_FICHIER_XSS = '"><img src=x onerror=alert(1)>.pdf'
MOTIF_GABARIT = "{{7*7}}"
MESSAGE_ID_GUILLEMETS = '<"x" onmouseover="alert(1)"@evil.example>'
DEST_ALPHA = "compta@alpha-sarl.fr"
ID_BROUILLON = identifiant_relance(DEST_ALPHA, ("A-001",), 1, J0)
EMPREINTE_NR = "ab" * 32
EMPREINTE_PR = "cd" * 32

FRAGMENTS_INTERDITS = (
    LIBELLE_XSS,
    "<img src=x onerror",
    '"x" onmouseover',
    "<b>inconnu</b>",
    "<script",
)
_ATTRIBUT_EVENEMENT = re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Horloge injectee et client de test
# ---------------------------------------------------------------------------


class Horloge:
    def __init__(self, t: dt.datetime = LUNDI_10H) -> None:
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t

    def avancer(self, **kw: float) -> None:
        self.t = self.t + dt.timedelta(**kw)


@dataclass
class Resultat:
    statut: int
    entetes: list[tuple[str, str]]
    texte: str

    def entete(self, nom: str) -> str | None:
        for k, v in self.entetes:
            if k.lower() == nom.lower():
                return v
        return None

    def entetes_nommes(self, nom: str) -> list[str]:
        return [v for k, v in self.entetes if k.lower() == nom.lower()]


AUTO = object()


class Client:
    def __init__(self, app: Application, *, hote: str = "127.0.0.1:8000", adresse: str = "127.0.0.1") -> None:
        self.app = app
        self.hote = hote
        self.adresse = adresse
        self.cookies: dict[str, str] = {}
        self.reponses: list[Resultat] = []

    def requete(self, methode: str, chemin: str, *, corps: bytes = b"", type_contenu: str = "",
                origine: object = AUTO, entetes: dict[str, str] | None = None) -> Resultat:
        chemin_seul, _, qs = chemin.partition("?")
        environ: dict = {
            "REQUEST_METHOD": methode, "SCRIPT_NAME": "", "PATH_INFO": chemin_seul, "QUERY_STRING": qs,
            "HTTP_HOST": self.hote, "REMOTE_ADDR": self.adresse, "wsgi.input": io.BytesIO(corps),
            "CONTENT_LENGTH": str(len(corps)) if corps else "",
        }
        if type_contenu:
            environ["CONTENT_TYPE"] = type_contenu
        if self.cookies:
            environ["HTTP_COOKIE"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if origine is AUTO and methode == "POST":
            environ["HTTP_ORIGIN"] = f"http://{self.hote}"
        elif isinstance(origine, str):
            environ["HTTP_ORIGIN"] = origine
        for k, v in (entetes or {}).items():
            environ["HTTP_" + k.upper().replace("-", "_")] = v
        setup_testing_defaults(environ)
        recu: dict = {}

        def start_response(statut, entetes_rep, exc_info=None):
            recu["statut"], recu["entetes"] = statut, entetes_rep
            return lambda _donnees: None

        iterable = validator(self.app)(environ, start_response)
        try:
            corps_rep = b"".join(iterable)
        finally:
            iterable.close()
        r = Resultat(int(recu["statut"].split()[0]), list(recu["entetes"]), corps_rep.decode("utf-8"))
        for valeur in r.entetes_nommes("Set-Cookie"):
            nom, _, reste = valeur.partition("=")
            val = reste.split(";")[0]
            if "Max-Age=0" in valeur:
                self.cookies.pop(nom, None)
            else:
                self.cookies[nom] = val
        self.reponses.append(r)
        return r

    def get(self, chemin: str, **kw) -> Resultat:
        return self.requete("GET", chemin, **kw)

    def post(self, chemin: str, donnees: dict[str, str], **kw) -> Resultat:
        return self.requete("POST", chemin, corps=urlencode(donnees).encode("ascii"),
                            type_contenu="application/x-www-form-urlencoded", **kw)

    def csrf(self, chemin: str = "/") -> str:
        return extraire_csrf(self.get(chemin).texte)

    def action(self, chemin: str, donnees: dict[str, str] | None = None, **kw) -> Resultat:
        return self.post(chemin, {"csrf": self.csrf(), **(donnees or {})}, **kw)


def extraire_csrf(texte: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', texte)
    assert m, "aucun jeton CSRF dans la page"
    return m.group(1)


def connecter(client: Client, identifiant: str, mot_de_passe: str = MDP) -> Resultat:
    jeton = extraire_csrf(client.get("/connexion").texte)
    return client.post("/connexion", {"identifiant": identifiant, "mot_de_passe": mot_de_passe, "csrf": jeton})


# ---------------------------------------------------------------------------
# Instance semee
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def modele_comptes(tmp_path_factory) -> Path:
    """Comptes crees UNE fois (PBKDF2 a 600 000 iterations coute ~0,2 s par hachage)."""
    rep = tmp_path_factory.mktemp("comptes")
    comptes.creer_utilisateur(rep, ALICE, NOM_ALICE, comptes.RESPONSABLE, MDP)
    comptes.creer_utilisateur(rep, BOB, NOM_BOB, comptes.COMPTABLE, MDP)
    return rep


def semer(inst: cycle.Instance) -> None:
    inst.preparer()
    pieces = [
        PieceAttendue("A-001", "ALPHA", "2026-09", Decimal("120.00"), dt.date(2026, 9, 3), LIBELLE_XSS),
        PieceAttendue("A-002", "ALPHA", "2026-09", Decimal("59.90"), dt.date(2026, 9, 17), "PRLV ORANGE PRO",
                      etat=EtatPiece.DEMANDEE, nb_relances=1, date_premiere_demande=dt.date(2026, 9, 28),
                      date_derniere_relance=dt.date(2026, 9, 28)),
        PieceAttendue("A-003", "ALPHA", "2026-08", Decimal("1234.50"), dt.date(2026, 8, 12), "CB BUREAU VALLEE",
                      etat=EtatPiece.RECUE, pieces_rattachees=("facture.pdf#abc",)),
        PieceAttendue("B-001", "BETA", "2026-09", Decimal("230.40"), dt.date(2026, 9, 10),
                      f"CB METRO {MOTIF_GABARIT}", etat=EtatPiece.ESCALADEE, nb_relances=3,
                      date_premiere_demande=dt.date(2026, 9, 1), date_derniere_relance=dt.date(2026, 9, 21)),
        PieceAttendue("G-001", "GAMMA", "2026-09", Decimal("145.00"), dt.date(2026, 9, 11), "CB CASTORAMA"),
    ]
    brouillon = Brouillon(
        id_relance=ID_BROUILLON, destinataire=DEST_ALPHA,
        objet=f"Pieces manquantes {LIBELLE_XSS}",
        corps=f"Bonjour,\n\n- 03/09/2026 120,00 EUR {LIBELLE_XSS}\n- motif {MOTIF_GABARIT}\n\nCordialement",
        dossiers=("ALPHA",), references=("A-001",), niveau=1, cree_le=J0,
    )
    decisions = [
        DecisionRoutage(MESSAGE_ID_GUILLEMETS, NOM_FICHIER_XSS, EMPREINTE_NR, StatutRoutage.NON_ROUTEE,
                        motif=MotifNonRoute.DOSSIER_INCONNU, detail=f"expediteur {MOTIF_GABARIT} <b>inconnu</b>"),
        DecisionRoutage("<m2@gamma-eurl.fr>", "facture-2026-09.pdf", EMPREINTE_PR, StatutRoutage.PROPOSEE,
                        dossier="GAMMA", periode="2026-09", reference_operation="G-001", detail="montant 145,00"),
    ]
    with Depot(inst.depot) as d:
        d.sauver_pieces(pieces)
        d.sauver_brouillon(brouillon)
        for dec in decisions:
            d.mettre_en_file(dec)
    JournalAudit(inst.audit).ecrire("systeme", "message_en_file_humaine", MESSAGE_ID_GUILLEMETS,
                                    {"fichier": NOM_FICHIER_XSS, "detail": MOTIF_GABARIT}, horodatage=LUNDI_10H)


@pytest.fixture
def inst(tmp_path: Path, modele_comptes: Path) -> cycle.Instance:
    instance = cycle.Instance(tmp_path / "instance")
    semer(instance)
    shutil.copy(modele_comptes / "utilisateurs.json", instance.racine / "utilisateurs.json")
    shutil.copy(modele_comptes / "validateurs.txt", instance.validateurs)
    return instance


@pytest.fixture
def horloge() -> Horloge:
    return Horloge()


@pytest.fixture
def app(inst: cycle.Instance, horloge: Horloge):
    application = Application(inst, horloge=horloge)
    yield application
    application.fermer()


@pytest.fixture
def client(app: Application) -> Client:
    return Client(app)


def connecte(app: Application, identifiant: str) -> Client:
    c = Client(app)
    r = connecter(c, identifiant)
    assert r.statut == 303, r.texte
    return c


def brouillon(inst: cycle.Instance, ident: str = ID_BROUILLON) -> Brouillon:
    with Depot(inst.depot) as d:
        b = d.charger_brouillon(ident)
    assert b is not None
    return b


def piece(inst: cycle.Instance, dossier: str, reference: str) -> PieceAttendue:
    with Depot(inst.depot) as d:
        return [p for p in d.charger_pieces(dossier=dossier) if p.reference == reference][0]


def emls(inst: cycle.Instance) -> list[str]:
    return sorted(p.name for p in inst.outbox.glob("*.eml"))


def instantane(inst: cycle.Instance) -> tuple:
    with Depot(inst.depot) as d:
        etat = (d.charger_pieces(), d.lister_brouillons(), d.file_humaine(resolue=None), d.historique_envois(),
                d.exporter_json(inst.racine.parent / "instantane.json").read_bytes())
    fichiers = sorted(str(p.relative_to(inst.racine)) for p in inst.racine.rglob("*")
                      if p.is_file() and not p.name.startswith("depot.sqlite") and p.parent.name != "logs")
    return etat, inst.audit.read_bytes(), fichiers, inst.validateurs.read_bytes(),\
        (inst.racine / "utilisateurs.json").read_bytes()


PAGES = ("/", "/brouillons", "/brouillons?statut=brouillon", f"/brouillons/{ID_BROUILLON}",
         f"/brouillons/{ID_BROUILLON}/envoyer", "/non-rattaches", "/pieces", "/pieces?dossier=ALPHA",
         "/pieces?etat=escaladee", "/escalades", "/audit", "/deconnexion")


# ---------------------------------------------------------------------------
# Echappement central
# ---------------------------------------------------------------------------


def test_echappement_central_et_gabarit():
    assert echapper('<a href="x">') == "&lt;a href=&quot;x&quot;&gt;"
    assert echapper("l'apostrophe") == "l&#x27;apostrophe"
    assert echapper(None) == ""
    assert echapper("abc‮def") == "abc\\u202edef"        # retournement visuel neutralise
    assert echapper("ligne 1\nligne 2") == "ligne 1\nligne 2"
    assert gabarit("<p>{x}</p>", x=MOTIF_GABARIT) == "<p>{{7*7}}</p>"
    assert gabarit("<p>{x}</p>", x="{y}") == "<p>{y}</p>"
    assert gabarit("<p>{x}</p>", x=Html("<em>sur</em>")) == "<p><em>sur</em></p>"


# ---------------------------------------------------------------------------
# Cycle complet : brouillon -> valider -> confirmer -> envoyer
# ---------------------------------------------------------------------------


def test_cycle_complet_valider_confirmer_envoyer(app, inst):
    bob = connecte(app, BOB)
    page = bob.get(f"/brouillons/{ID_BROUILLON}")
    assert page.statut == 200
    assert "Valider ce brouillon" in page.texte and DEST_ALPHA in page.texte
    assert '<pre class="corps">' in page.texte

    r = bob.action(f"/brouillons/{ID_BROUILLON}/valider")
    assert r.statut == 303 and r.entete("Location") == f"/brouillons/{ID_BROUILLON}?fait=valide"
    page = bob.get(r.entete("Location"))
    assert "Brouillon validé" in page.texte and NOM_BOB in page.texte
    assert brouillon(inst).statut is StatutBrouillon.VALIDEE and brouillon(inst).valide_par == NOM_BOB

    confirmation = bob.get(f"/brouillons/{ID_BROUILLON}/envoyer")
    assert confirmation.statut == 200 and "Confirmer l’envoi" in confirmation.texte
    assert emls(inst) == []                                   # la page de confirmation n'envoie rien

    sans_confirmation = bob.post(f"/brouillons/{ID_BROUILLON}/envoyer", {"csrf": extraire_csrf(confirmation.texte)})
    assert sans_confirmation.statut == 400 and emls(inst) == []

    r = bob.post(f"/brouillons/{ID_BROUILLON}/envoyer",
                 {"csrf": extraire_csrf(confirmation.texte), "confirmation": "oui"})
    assert r.statut == 303, r.texte
    assert emls(inst) == [f"{ID_BROUILLON}.eml"]
    assert brouillon(inst).statut is StatutBrouillon.ENVOYEE
    p = piece(inst, "ALPHA", "A-001")
    assert p.etat is EtatPiece.DEMANDEE and p.nb_relances == 1

    entrees = JournalAudit(inst.audit).lire()
    assert any(e.acteur == NOM_BOB and e.action == "valide" and e.objet == ID_BROUILLON for e in entrees)
    assert any(e.action == "envoi_effectue" and e.details.get("valide_par") == NOM_BOB for e in entrees)
    assert JournalAudit(inst.audit).verifier().ok


def test_envoi_sans_validation_refuse(app, inst):
    bob = connecte(app, BOB)
    r = bob.action(f"/brouillons/{ID_BROUILLON}/envoyer", {"confirmation": "oui"})
    assert r.statut == 409
    assert "seul un brouillon" in r.texte and "Traceback" not in r.texte
    assert emls(inst) == [] and brouillon(inst).statut is StatutBrouillon.BROUILLON
    page = bob.get(f"/brouillons/{ID_BROUILLON}/envoyer")
    assert "n’est pas envoyable" in page.texte and 'name="confirmation"' not in page.texte


def test_double_envoi_refuse(app, inst):
    bob = connecte(app, BOB)
    bob.action(f"/brouillons/{ID_BROUILLON}/valider")
    assert bob.action(f"/brouillons/{ID_BROUILLON}/envoyer", {"confirmation": "oui"}).statut == 303
    second = bob.action(f"/brouillons/{ID_BROUILLON}/envoyer", {"confirmation": "oui"})
    assert second.statut == 409 and "envoyee" in second.texte
    assert emls(inst) == [f"{ID_BROUILLON}.eml"]


def test_rejet_avec_motif_obligatoire(app, inst):
    bob = connecte(app, BOB)
    assert bob.action(f"/brouillons/{ID_BROUILLON}/rejeter", {"motif": "  "}).statut == 400
    assert brouillon(inst).statut is StatutBrouillon.BROUILLON
    assert bob.action(f"/brouillons/{ID_BROUILLON}/rejeter", {"motif": "montant faux"}).statut == 303
    assert brouillon(inst).statut is StatutBrouillon.REJETEE


def test_envoi_hors_creneau_message_clair(app, inst, horloge):
    horloge.t = dt.datetime(2026, 10, 5, 17, 50, tzinfo=PARIS)
    bob = connecte(app, BOB)
    bob.action(f"/brouillons/{ID_BROUILLON}/valider")
    horloge.avancer(minutes=15)                                   # 18 h 05 : hors creneau
    r = bob.action(f"/brouillons/{ID_BROUILLON}/envoyer", {"confirmation": "oui"})
    assert r.statut == 409 and "hors créneau" in r.texte
    assert emls(inst) == [] and brouillon(inst).statut is StatutBrouillon.VALIDEE


def test_validateur_retire_de_validateurs_txt_refuse(app, inst):
    bob = connecte(app, BOB)
    inst.validateurs.write_text(f"{NOM_ALICE}\n", encoding="utf-8")
    r = bob.action(f"/brouillons/{ID_BROUILLON}/valider")
    assert r.statut == 409 and "validateurs.txt" in r.texte
    assert brouillon(inst).statut is StatutBrouillon.BROUILLON


def test_trancher_un_envoi_incertain_par_le_responsable(app, inst):
    incertain = Brouillon("incertain-1", "contact@gamma-eurl.fr", "Relance", "Corps", ("GAMMA",), ("G-001",), 1, J0,
                          statut=StatutBrouillon.EN_COURS, valide_par=NOM_ALICE, valide_le=LUNDI_10H)
    with Depot(inst.depot) as d:
        d.sauver_brouillon(incertain)
    bob = connecte(app, BOB)
    assert "À trancher par un responsable" in bob.get("/brouillons/incertain-1").texte
    assert bob.action("/brouillons/incertain-1/trancher", {"parti": "non"}).statut == 403
    alice = connecte(app, ALICE)
    assert "incertain-1" in alice.get("/escalades").texte
    r = alice.action("/brouillons/incertain-1/trancher", {"parti": "non"})
    assert r.statut == 303
    assert brouillon(inst, "incertain-1").statut is StatutBrouillon.VALIDEE
    assert any(e.acteur == NOM_ALICE and e.action == "envoi_declare_non_parti" for e in JournalAudit(inst.audit).lire())


# ---------------------------------------------------------------------------
# Pieces, file humaine, escalades : chaque action passe par cycle
# ---------------------------------------------------------------------------


def test_actions_sur_les_pieces(app, inst):
    bob = connecte(app, BOB)
    assert bob.action("/pieces/controler", {"dossier": "ALPHA", "reference": "A-003",
                                             "decision": "conforme"}).statut == 303
    assert piece(inst, "ALPHA", "A-003").etat is EtatPiece.VALIDEE
    r = bob.action("/pieces/promesse", {"dossier": "ALPHA", "reference": "A-002", "date": "2026-10-09",
                                         "f_dossier": "ALPHA"})
    assert r.statut == 303 and r.entete("Location") == "/pieces?dossier=ALPHA&fait=promesse"
    p = piece(inst, "ALPHA", "A-002")
    assert p.etat is EtatPiece.PROMISE and p.date_promesse == dt.date(2026, 10, 9)
    assert bob.action("/pieces/bloquer", {"dossier": "ALPHA", "reference": "A-001", "motif": "litige"}).statut == 303
    assert piece(inst, "ALPHA", "A-001").bloquee
    assert "litige" in bob.get("/pieces?dossier=ALPHA").texte
    assert bob.action("/pieces/debloquer", {"dossier": "ALPHA", "reference": "A-001", "motif": "regle"}).statut == 303
    assert not piece(inst, "ALPHA", "A-001").bloquee


def test_actions_pieces_erreurs_attendues(app, inst):
    bob = connecte(app, BOB)
    assert bob.action("/pieces/promesse", {"dossier": "ALPHA", "reference": "A-002", "date": "demain"}).statut == 400
    trop_loin = bob.action("/pieces/promesse", {"dossier": "ALPHA", "reference": "A-002", "date": "2027-01-01"})
    assert trop_loin.statut == 409 and "Traceback" not in trop_loin.texte
    assert bob.action("/pieces/controler", {"dossier": "ALPHA", "reference": "A-003",
                                             "decision": "non_conforme", "motif": ""}).statut == 400
    inconnue = bob.action("/pieces/bloquer", {"dossier": "ZZZ", "reference": "nope", "motif": "x"})
    assert inconnue.statut == 409 and "introuvable" in inconnue.texte
    assert bob.get("/pieces?etat=nimporte").statut == 400


def test_rattachement_d_une_proposition_et_d_un_message_non_route(app, inst):
    bob = connecte(app, BOB)
    page = bob.get("/non-rattaches")
    assert "Confirmer le rattachement" in page.texte and "CB CASTORAMA" in page.texte
    cle = cle_entree("<m2@gamma-eurl.fr>", EMPREINTE_PR)
    assert bob.action("/non-rattaches/confirmer", {"cle": cle}).statut == 303
    assert piece(inst, "GAMMA", "G-001").etat is EtatPiece.RECUE
    assert bob.action("/non-rattaches/confirmer", {"cle": cle}).statut == 409       # deja traitee

    cle_nr = cle_entree(MESSAGE_ID_GUILLEMETS, EMPREINTE_NR)
    assert bob.action("/non-rattaches/confirmer", {"cle": cle_nr}).statut == 400    # dossier obligatoire
    r = bob.action("/non-rattaches/confirmer", {"cle": cle_nr, "dossier": "ALPHA", "reference": "A-001"})
    assert r.statut == 303
    p = piece(inst, "ALPHA", "A-001")
    assert p.etat is EtatPiece.RECUE and p.pieces_rattachees
    with Depot(inst.depot) as d:
        assert d.file_humaine(resolue=False) == []
    assert any(e.acteur == NOM_BOB and e.action == "rattachement_confirme" for e in JournalAudit(inst.audit).lire())


def test_arbitrage_par_le_responsable(app, inst):
    alice = connecte(app, ALICE)
    r = alice.action("/pieces/arbitrer", {"dossier": "BETA", "reference": "B-001", "motif": "montant non significatif",
                                           "retour": "escalades"})
    assert r.statut == 303 and r.entete("Location") == "/escalades?fait=arbitre"
    assert piece(inst, "BETA", "B-001").etat is EtatPiece.CLOSE_SANS_SUITE


# ---------------------------------------------------------------------------
# Connexion, sessions, droits
# ---------------------------------------------------------------------------


def test_acces_sans_connexion_redirige_vers_la_connexion(client, inst):
    avant = instantane(inst)
    for chemin in PAGES + ("/inexistant", "/brouillons/zzz"):
        r = client.get(chemin)
        assert r.statut == 303 and r.entete("Location") == "/connexion", chemin
    r = client.post(f"/brouillons/{ID_BROUILLON}/valider", {"csrf": "x"})
    assert r.statut == 303 and r.entete("Location") == "/connexion"
    assert instantane(inst) == avant
    assert client.get("/connexion").statut == 200


def test_echecs_de_connexion_message_generique(app):
    c = Client(app)
    inconnu = connecter(c, "personne")
    mauvais = connecter(c, ALICE, "mauvais-mot-de-passe")
    assert inconnu.statut == mauvais.statut == 200
    message = "Identifiant ou mot de passe incorrect."
    assert message in inconnu.texte and message in mauvais.texte
    for r in (inconnu, mauvais):
        assert "inconnu" not in r.texte.lower() and "session" not in c.cookies
    assert connecter(c, "  ALICE ").statut == 303                 # identifiant normalise


def test_blocage_apres_cinq_echecs_par_identifiant(app, horloge):
    c = Client(app)
    for _ in range(5):
        assert connecter(c, ALICE, "mauvais-mot-de-passe").statut == 200
    bloque = connecter(c, ALICE)
    assert bloque.statut == 429 and "Trop de tentatives" in bloque.texte and "session" not in c.cookies
    autre_poste = Client(app, adresse="10.0.0.9")
    assert connecter(autre_poste, ALICE).statut == 429           # le blocage suit l'identifiant
    assert connecter(autre_poste, BOB).statut == 303
    horloge.avancer(minutes=10, seconds=1)
    assert connecter(c, ALICE).statut == 303


def test_blocage_par_adresse(app, horloge):
    attaquant = Client(app, adresse="10.0.0.66")
    for i in range(5):
        connecter(attaquant, f"inconnu{i}")
    assert connecter(attaquant, ALICE).statut == 429
    assert connecter(Client(app, adresse="10.0.0.7"), ALICE).statut == 303
    horloge.avancer(minutes=11)
    assert connecter(attaquant, ALICE).statut == 303


def test_echecs_espaces_au_dela_de_dix_minutes_ne_bloquent_pas(app, horloge):
    c = Client(app)
    for _ in range(4):
        connecter(c, ALICE, "mauvais-mot-de-passe")
    horloge.avancer(minutes=11)
    connecter(c, ALICE, "mauvais-mot-de-passe")
    assert connecter(c, ALICE).statut == 303


def test_expiration_par_inactivite(app, horloge):
    c = connecte(app, BOB)
    horloge.avancer(minutes=29)
    assert c.get("/").statut == 200
    horloge.avancer(minutes=29)
    assert c.get("/").statut == 200                               # l'activite repousse l'echeance
    horloge.avancer(minutes=30)
    r = c.get("/")
    assert r.statut == 303 and r.entete("Location") == "/connexion"


def test_expiration_absolue(app, horloge):
    c = connecte(app, BOB)
    for _ in range(23):                                           # 23 x 20 min = 7 h 40
        horloge.avancer(minutes=20)
        assert c.get("/").statut == 200
    horloge.avancer(minutes=20)                                   # 8 h
    assert c.get("/").statut == 303


def test_fixation_de_session_impossible(app):
    c = Client(app)
    impose = "jeton-impose-par-un-attaquant-0123456789abcdef"
    c.cookies["session"] = impose
    assert connecter(c, BOB).statut == 303
    assert c.cookies["session"] != impose
    victime_vue_par_l_attaquant = Client(app)
    victime_vue_par_l_attaquant.cookies["session"] = impose
    assert victime_vue_par_l_attaquant.get("/").statut == 303

    premier = c.cookies["session"]
    del c.cookies["session"]
    jeton = extraire_csrf(c.get("/connexion").texte)
    c.cookies["session"] = premier                                # nouvelle connexion dans la meme session
    assert c.post("/connexion", {"identifiant": BOB, "mot_de_passe": MDP, "csrf": jeton}).statut == 303
    assert c.cookies["session"] != premier
    ancien = Client(app)
    ancien.cookies["session"] = premier
    assert ancien.get("/").statut == 303


def test_cookie_de_session_et_deconnexion(app):
    c = Client(app)
    r = connecter(c, BOB)
    cookie = [v for v in r.entetes_nommes("Set-Cookie") if v.startswith("session=")][0]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie and "Secure" not in cookie
    assert len(c.cookies["session"]) >= 43                         # 32 octets en base64
    jeton = c.cookies["session"]
    assert c.get("/deconnexion").statut == 200 and c.get("/").statut == 200   # le GET ne deconnecte pas
    r = c.action("/deconnexion")
    assert r.statut == 303 and r.entete("Location") == "/connexion" and "session" not in c.cookies
    rejeu = Client(app)
    rejeu.cookies["session"] = jeton
    assert rejeu.get("/").statut == 303                            # detruite cote serveur


def test_option_https_ajoute_secure(inst, horloge):
    app = Application(inst, horloge=horloge, https=True)
    try:
        c = Client(app)
        jeton = extraire_csrf(c.get("/connexion").texte)
        r = c.post("/connexion", {"identifiant": BOB, "mot_de_passe": MDP, "csrf": jeton},
                   origine="https://127.0.0.1:8000")
        assert r.statut == 303
        assert all("Secure" in v for v in r.entetes_nommes("Set-Cookie"))
    finally:
        app.fermer()


def test_compte_supprime_ou_mot_de_passe_change_ferme_la_session(app, inst):
    bob = connecte(app, BOB)
    alice = connecte(app, ALICE)
    comptes.changer_mot_de_passe(inst, BOB, "nouveau-mot-de-passe")
    assert bob.get("/").statut == 303
    comptes.supprimer_utilisateur(inst, ALICE)
    assert alice.get("/").statut == 303


def test_comptable_403_sur_les_actions_du_responsable(app, inst):
    bob = connecte(app, BOB)
    avant = instantane(inst)
    assert bob.action("/pieces/arbitrer", {"dossier": "BETA", "reference": "B-001", "motif": "x"}).statut == 403
    assert bob.action("/audit/reparer").statut == 403
    assert bob.action("/audit/exporter").statut == 403
    assert bob.action(f"/brouillons/{ID_BROUILLON}/trancher", {"parti": "oui"}).statut == 403
    assert bob.get("/audit").statut == 403
    assert instantane(inst) == avant
    assert piece(inst, "BETA", "B-001").etat is EtatPiece.ESCALADEE
    assert 'href="/audit"' not in bob.get("/").texte
    assert "Arbitrage réservé au responsable" in bob.get("/escalades").texte


# ---------------------------------------------------------------------------
# CSRF et en-tetes Origin / Host
# ---------------------------------------------------------------------------


def test_post_sans_jeton_csrf_refuse(app, inst):
    bob = connecte(app, BOB)
    r = bob.post(f"/brouillons/{ID_BROUILLON}/valider", {})
    assert r.statut == 403 and "Jeton de sécurité" in r.texte
    assert bob.post(f"/brouillons/{ID_BROUILLON}/valider", {"csrf": "faux"}).statut == 403
    alice = connecte(app, ALICE)
    assert bob.post(f"/brouillons/{ID_BROUILLON}/valider", {"csrf": alice.csrf()}).statut == 403
    assert brouillon(inst).statut is StatutBrouillon.BROUILLON


def test_post_avec_mauvaise_origine_refuse(app, inst):
    bob = connecte(app, BOB)
    jeton = bob.csrf()
    for origine in ("http://evil.example", "http://127.0.0.1:9999", "https://127.0.0.1:8000"):
        r = bob.post(f"/brouillons/{ID_BROUILLON}/valider", {"csrf": jeton}, origine=origine)
        assert r.statut == 403, origine
    r = bob.post(f"/brouillons/{ID_BROUILLON}/valider", {"csrf": jeton},
                 entetes={"Sec-Fetch-Site": "cross-site"})
    assert r.statut == 403
    assert brouillon(inst).statut is StatutBrouillon.BROUILLON
    # Navigateur reel avec `Referrer-Policy: no-referrer` : `Origin: null` sur un POST
    # same-origin ; accepte avec un jeton valide.
    r = bob.post(f"/brouillons/{ID_BROUILLON}/valider", {"csrf": jeton}, origine="null",
                 entetes={"Sec-Fetch-Site": "same-origin"})
    assert r.statut == 303 and brouillon(inst).statut is StatutBrouillon.VALIDEE


def test_connexion_exige_le_jeton_de_pre_session(app):
    c = Client(app)
    r = c.post("/connexion", {"identifiant": BOB, "mot_de_passe": MDP})
    assert r.statut == 403 and "session" not in c.cookies
    jeton = extraire_csrf(c.get("/connexion").texte)
    autre = Client(app)                                           # jeton vole sans le cookie associe
    assert autre.post("/connexion", {"identifiant": BOB, "mot_de_passe": MDP, "csrf": jeton}).statut == 403
    assert c.post("/connexion", {"identifiant": BOB, "mot_de_passe": MDP, "csrf": jeton},
                  origine="http://evil.example").statut == 403


def test_hote_inattendu_refuse(app):
    c = Client(app, hote="evil.example:8000")
    r = c.get("/connexion")
    assert r.statut == 400 and r.entete("Content-Security-Policy") == CSP
    assert Client(app, hote="localhost:8000").get("/connexion").statut == 200


def test_chaque_formulaire_post_porte_le_jeton(app):
    alice = connecte(app, ALICE)
    for chemin in PAGES:
        texte = alice.get(chemin).texte
        for formulaire in re.findall(r'<form[^>]*method="post".*?</form>', texte, re.S):
            assert 'name="csrf"' in formulaire, chemin
        assert not re.search(r'<form[^>]*method="get"[^>]*action="/(?!brouillons"|pieces")', texte)


# ---------------------------------------------------------------------------
# Injection : aucun texte externe n'apparait non echappe
# ---------------------------------------------------------------------------


def _verifier_echappe(r: Resultat, contexte: str) -> None:
    for fragment in FRAGMENTS_INTERDITS:
        assert fragment not in r.texte, (contexte, fragment)
    assert not _ATTRIBUT_EVENEMENT.search(r.texte), contexte
    assert " style=" not in r.texte, contexte


def test_injection_jamais_rendue_non_echappee(app, inst):
    alice = connecte(app, ALICE)
    bob = connecte(app, BOB)
    reponses = []
    for c in (alice, bob):
        for chemin in PAGES + (f"/pieces?dossier={LIBELLE_XSS}", f"/brouillons?statut={LIBELLE_XSS}",
                               "/inexistant"):
            reponses.append((chemin, c.get(chemin)))
    # Erreur du domaine dont le message reprend une saisie hostile.
    cle_nr = cle_entree(MESSAGE_ID_GUILLEMETS, EMPREINTE_NR)
    reponses.append(("rattacher", alice.action("/non-rattaches/confirmer",
                                                {"cle": cle_nr, "dossier": LIBELLE_XSS, "reference": NOM_FICHIER_XSS})))
    reponses.append(("bloquer", alice.action("/pieces/bloquer",
                                              {"dossier": LIBELLE_XSS, "reference": "x", "motif": NOM_FICHIER_XSS})))
    for chemin, r in reponses:
        _verifier_echappe(r, chemin)
    assert reponses[-1][1].statut == 409 and "&lt;script&gt;" in reponses[-1][1].texte

    pieces_page = alice.get("/pieces").texte
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in pieces_page and MOTIF_GABARIT in pieces_page
    file_page = alice.get("/non-rattaches").texte
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;.pdf" in file_page
    assert "&lt;&quot;x&quot; onmouseover=&quot;alert(1)&quot;@evil.example&gt;" in file_page
    assert MOTIF_GABARIT in file_page and "&lt;b&gt;inconnu&lt;/b&gt;" in file_page
    audit_page = alice.get("/audit").texte
    assert "onmouseover=&quot;alert(1)&quot;" in audit_page
    detail = alice.get(f"/brouillons/{ID_BROUILLON}").texte
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in detail and MOTIF_GABARIT in detail


def test_message_id_hostile_ne_fait_jamais_l_aller_retour(app):
    alice = connecte(app, ALICE)
    page = alice.get("/non-rattaches").texte
    assert MESSAGE_ID_GUILLEMETS not in page
    valeurs = re.findall(r'name="cle" value="([^"]*)"', page)
    assert valeurs and all(re.fullmatch(r"[0-9a-f]{64}", v) for v in valeurs)


# ---------------------------------------------------------------------------
# En-tetes de securite, methodes, taille, statique
# ---------------------------------------------------------------------------


def _verifier_entetes(r: Resultat, contexte: str) -> None:
    assert r.entete("Content-Security-Policy") == CSP, contexte
    assert r.entete("X-Content-Type-Options") == "nosniff", contexte
    assert r.entete("X-Frame-Options") == "DENY", contexte
    assert r.entete("Referrer-Policy") == "no-referrer", contexte
    assert r.entete("Content-Type"), contexte


def test_entetes_de_securite_sur_toutes_les_reponses(app, inst, monkeypatch):
    anonyme = Client(app)
    alice = connecte(app, ALICE)
    bob = connecte(app, BOB)
    anonyme.get("/connexion")
    anonyme.get("/")                                              # 303
    anonyme.get("/style.css")
    anonyme.requete("PUT", "/")                                   # 405
    anonyme.post("/connexion", {"identifiant": "x"})              # 403 (jeton)
    hostile = Client(app, hote="evil.example")
    hostile.get("/")                                              # 400
    for chemin in PAGES:
        alice.get(chemin)
    alice.get("/inexistant")                                      # 404
    alice.get("/brouillons/absent")                               # 404
    alice.post("/pieces", {"csrf": alice.csrf()})                 # 405
    alice.requete("POST", "/brouillons/x/valider", corps=b"a" * (64 * 1024 + 1),
                  type_contenu="application/x-www-form-urlencoded")   # 413
    alice.action("/pieces/bloquer", {"dossier": "Z", "reference": "Z", "motif": "m"})   # 409
    alice.action(f"/brouillons/{ID_BROUILLON}/valider")           # 303
    bob.get("/audit")                                             # 403

    def panne(*a, **k):
        raise RuntimeError("panne simulee")

    monkeypatch.setattr(cycle, "rejeter_relance", panne)
    alice.action(f"/brouillons/{ID_BROUILLON}/rejeter", {"motif": "x"})   # 500
    statuts = set()
    for client in (anonyme, alice, bob, hostile):
        for r in client.reponses:
            _verifier_entetes(r, f"{r.statut}")
            statuts.add(r.statut)
            if r.entete("Content-Type").startswith("text/html"):
                assert r.entete("Cache-Control") == "no-store"
    assert {200, 303, 400, 403, 404, 405, 409, 413, 500} <= statuts


def test_methodes_inattendues_405(app):
    alice = connecte(app, ALICE)
    r = alice.requete("DELETE", "/brouillons")
    assert r.statut == 405 and r.entete("Allow") == "GET, HEAD, POST"
    r = alice.post("/brouillons", {"csrf": alice.csrf()})
    assert r.statut == 405 and r.entete("Allow") == "GET, HEAD"
    r = alice.get(f"/brouillons/{ID_BROUILLON}/valider")
    assert r.statut == 405 and r.entete("Allow") == "POST"
    assert alice.post("/style.css", {}).statut == 405
    tete = alice.requete("HEAD", "/")
    assert tete.statut == 200 and tete.texte == ""


def test_corps_plafonne_et_format_controle(app, inst):
    alice = connecte(app, ALICE)
    jeton = alice.csrf()
    gros = urlencode({"csrf": jeton, "motif": "x" * (64 * 1024)}).encode()
    r = alice.requete("POST", f"/brouillons/{ID_BROUILLON}/rejeter", corps=gros,
                      type_contenu="application/x-www-form-urlencoded")
    assert r.statut == 413
    r = alice.requete("POST", f"/brouillons/{ID_BROUILLON}/rejeter", corps=b'{"csrf": "x"}',
                      type_contenu="application/json")
    assert r.statut == 415
    assert brouillon(inst).statut is StatutBrouillon.BROUILLON


def test_feuille_de_style_seule_ressource_statique(app):
    c = Client(app)
    r = c.get("/style.css")
    assert r.statut == 200 and r.entete("Content-Type") == "text/css; charset=utf-8"
    assert "--accent: #104281" in r.texte and "prefers-color-scheme: dark" in r.texte
    for chemin in ("/static/style.css", "/style.css/../comptes.py", "/../src/rapprochement/web/comptes.py",
                   "/static/", "/web/application.py"):
        r = c.get(chemin)
        assert r.statut == 303 and "def " not in r.texte, chemin
    alice = connecte(app, ALICE)
    for chemin in ("/static/style.css", "/static/", "/utilisateurs.json"):
        assert alice.get(chemin).statut == 404


def test_pages_sans_style_ni_script_en_ligne(app):
    alice = connecte(app, ALICE)
    for chemin in PAGES:
        texte = alice.get(chemin).texte
        assert "<script" not in texte and " style=" not in texte and "<style" not in texte, chemin
        assert '<link rel="stylesheet" href="/style.css">' in texte


# ---------------------------------------------------------------------------
# GET sans effet de bord
# ---------------------------------------------------------------------------


def test_get_sans_effet_de_bord(app, inst):
    alice = connecte(app, ALICE)
    bob = connecte(app, BOB)
    avant = instantane(inst)
    for c in (alice, bob):
        for chemin in PAGES + ("/pieces?dossier=BETA&etat=escaladee", "/brouillons/absent", "/style.css",
                               f"/brouillons/{ID_BROUILLON}/valider", "/audit/exporter"):
            c.get(chemin)
            c.requete("HEAD", chemin)
    assert instantane(inst) == avant
    assert not (inst.racine / "exports").exists()
    assert alice.get("/").statut == 200                           # GET /deconnexion n'a pas deconnecte


def test_get_sans_depot_ne_cree_rien(tmp_path, modele_comptes, horloge):
    inst = cycle.Instance(tmp_path / "vide")
    inst.racine.mkdir()
    shutil.copy(modele_comptes / "utilisateurs.json", inst.racine / "utilisateurs.json")
    shutil.copy(modele_comptes / "validateurs.txt", inst.validateurs)
    app = Application(inst, horloge=horloge)
    try:
        alice = connecte(app, ALICE)
        for chemin in PAGES:
            assert alice.get(chemin).statut in (200, 404), chemin
        assert not inst.depot.exists() and not inst.audit.exists()
    finally:
        app.fermer()


# ---------------------------------------------------------------------------
# Mode test
# ---------------------------------------------------------------------------


def test_mode_test_bandeau_sur_chaque_page(app, inst):
    anonyme = Client(app)
    assert "MODE TEST" not in anonyme.get("/connexion").texte
    (inst.racine / "config.json").write_text(json.dumps({"mode_test": True}), encoding="utf-8")
    alice = connecte(app, ALICE)
    bob = connecte(app, BOB)
    reponses = [anonyme.get("/connexion"), anonyme.requete("PUT", "/"), alice.get("/inexistant"),
                bob.get("/audit"), alice.action("/pieces/bloquer", {"dossier": "Z", "reference": "Z", "motif": "m"})]
    reponses += [alice.get(chemin) for chemin in PAGES]
    for r in reponses:
        assert "MODE TEST : DONNEES FICTIVES" in r.texte, r.statut
        assert r.texte.index("MODE TEST") < r.texte.index("<main")
    for valeur in ("oui", 1, None):
        (inst.racine / "config.json").write_text(json.dumps({"mode_test": valeur}), encoding="utf-8")
        assert "MODE TEST" not in alice.get("/").texte
    (inst.racine / "config.json").write_text("{illisible", encoding="utf-8")
    assert "MODE TEST" not in alice.get("/").texte


# ---------------------------------------------------------------------------
# Erreurs internes et journal technique
# ---------------------------------------------------------------------------


def test_erreur_interne_page_generique_detail_dans_le_journal(app, inst, monkeypatch):
    def panne(*a, **k):
        raise RuntimeError("detail-interne-secret")

    monkeypatch.setattr(cycle, "valider_relance", panne)
    bob = connecte(app, BOB)
    r = bob.action(f"/brouillons/{ID_BROUILLON}/valider")
    assert r.statut == 500
    assert "detail-interne-secret" not in r.texte and "Traceback" not in r.texte and "RuntimeError" not in r.texte
    assert "Erreur interne" in r.texte
    journal = (inst.racine / "logs" / "web.log").read_text(encoding="utf-8")
    assert "detail-interne-secret" in journal and "Traceback" in journal


def test_mot_de_passe_jamais_dans_les_journaux_ni_les_reponses(app, inst, monkeypatch):
    c = Client(app)
    connecter(c, ALICE, "mauvais-mot-de-passe-123")
    connecter(c, MDP, MDP)                                        # mot de passe saisi dans le champ identifiant
    connecter(c, "personne", MDP)
    assert connecter(c, ALICE).statut == 303
    jeton_session, csrf = c.cookies["session"], c.csrf()

    def panne(*a, **k):
        raise RuntimeError("panne")

    monkeypatch.setattr(cycle, "valider_relance", panne)
    c.action(f"/brouillons/{ID_BROUILLON}/valider")
    journal = (inst.racine / "logs" / "web.log").read_text(encoding="utf-8")
    for secret in (MDP, "mauvais-mot-de-passe-123", jeton_session, csrf):
        assert secret not in journal
    for r in c.reponses:
        assert MDP not in r.texte and "mauvais-mot-de-passe-123" not in r.texte
    assert "echec de connexion" in journal and "connexion de alice" in journal
    assert MDP not in (inst.racine / "utilisateurs.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Audit (responsable)
# ---------------------------------------------------------------------------


def test_audit_verification_export_et_reparation(app, inst):
    alice = connecte(app, ALICE)
    page = alice.get("/audit")
    assert page.statut == 200 and "Intègre" in page.texte and "message_en_file_humaine" in page.texte
    assert "Réparer la fin du journal" not in page.texte
    r = alice.action("/audit/exporter")
    assert r.statut == 200 and "exports/depot-20261005-100000.json" in r.texte
    export = json.loads((inst.racine / "exports" / "depot-20261005-100000.json").read_text(encoding="utf-8"))
    assert export["schema"]["format"] == "rapprochement-depot"
    assert alice.action("/audit/exporter").statut == 200
    assert (inst.racine / "exports" / "depot-20261005-100000-1.json").exists()

    with open(inst.audit, "ab") as f:                             # coupure pendant une ecriture
        f.write(b'{"seq": 9, "tronq')
    assert "Altéré" in alice.get("/").texte
    page = alice.get("/audit")
    assert "Altéré" in page.texte and "Réparer la fin du journal" in page.texte
    refus = alice.action(f"/brouillons/{ID_BROUILLON}/valider")
    assert refus.statut == 409 and "journal d’audit n’est pas sain" in refus.texte
    r = alice.action("/audit/reparer")
    assert r.statut == 200 and "Journal réparé" in r.texte
    assert JournalAudit(inst.audit).verifier().ok
    assert any(e.acteur == NOM_ALICE and e.action == "journal_repare" for e in JournalAudit(inst.audit).lire())
    assert "rien à réparer" in alice.action("/audit/reparer").texte


def test_accueil_resume_les_files(app):
    bob = connecte(app, BOB)
    texte = bob.get("/").texte
    assert "Brouillons à relire" in texte and "Messages non rattachés" in texte and "Escalades" in texte
    assert "Pièces par état" in texte and "Journal d’audit" in texte and "Intègre" in texte
    assert NOM_BOB in texte and "comptable" in texte


def test_montants_alignes_et_etats_ecrits(app):
    alice = connecte(app, ALICE)
    texte = alice.get("/pieces").texte
    assert '<span class="montant">1 234,50&nbsp;EUR</span>' in texte
    for etat in ("Attendue", "Demandée", "Reçue", "Escaladée"):
        assert f">{etat}</span>" in texte


# ---------------------------------------------------------------------------
# Comptes : scripts/web.py
# ---------------------------------------------------------------------------


def _charger_cli():
    import importlib.util

    spec = importlib.util.spec_from_file_location("cli_web", RACINE / "scripts" / "web.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_creer_utilisateur_et_validateurs_txt(tmp_path, monkeypatch, capsys):
    cli = _charger_cli()
    inst = cycle.Instance(tmp_path / "inst")
    inst.preparer()
    inst.validateurs.write_text("# equipe\nMarie Durand\n", encoding="utf-8")
    saisies = iter(["court", "court"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _invite="": next(saisies))
    args = ["creer-utilisateur", "--instance", str(inst.racine), "--identifiant", "claire",
            "--nom", "Claire Petit", "--role", "comptable"]
    assert cli.main(args) == 1 and "trop court" in capsys.readouterr().err
    saisies = iter(["un-long-mot-de-passe", "un-autre-mot-de-passe"])
    assert cli.main(args) == 1 and "different" in capsys.readouterr().err
    assert not (inst.racine / "utilisateurs.json").exists()

    saisies = iter(["un-long-mot-de-passe", "un-long-mot-de-passe"])
    assert cli.main(args) == 0
    assert cycle.lire_validateurs(inst) == ("Marie Durand", "Claire Petit")
    assert cycle.exiger_validateur(inst, "claire petit") == "Claire Petit"
    brut = (inst.racine / "utilisateurs.json").read_text(encoding="utf-8")
    assert "un-long-mot-de-passe" not in brut
    h = json.loads(brut)["utilisateurs"][0]["hachage"]
    assert h["algorithme"] == "pbkdf2_sha256" and h["iterations"] >= 600_000 and len(bytes.fromhex(h["sel"])) == 16
    assert comptes.verifier_mot_de_passe("un-long-mot-de-passe", h)
    assert not comptes.verifier_mot_de_passe("un-long-mot-de-passE", h)

    saisies = iter(["un-long-mot-de-passe", "un-long-mot-de-passe"])
    assert cli.main(args) == 1 and "existe deja" in capsys.readouterr().err

    assert cli.main(["lister-utilisateurs", "--instance", str(inst.racine)]) == 0
    sortie = capsys.readouterr().out
    assert "claire" in sortie and "comptable" in sortie and h["valeur"] not in sortie and h["sel"] not in sortie

    saisies = iter(["encore-un-mot-de-passe", "encore-un-mot-de-passe"])
    assert cli.main(["changer-mot-de-passe", "--instance", str(inst.racine), "--identifiant", "claire"]) == 0
    assert comptes.verifier_mot_de_passe("encore-un-mot-de-passe", comptes.lire_utilisateurs(inst)["claire"].hachage)

    assert cli.main(["supprimer-utilisateur", "--instance", str(inst.racine), "--identifiant", "claire"]) == 0
    assert cycle.lire_validateurs(inst) == ("Marie Durand",)
    assert "# equipe" in inst.validateurs.read_text(encoding="utf-8")
    assert comptes.lire_utilisateurs(inst) == {}


def test_noms_et_identifiants_refuses(tmp_path):
    inst = cycle.Instance(tmp_path / "inst")
    inst.preparer()
    for nom in ("Eve\nSysteme", "  ", "systeme", "# commentaire", "Eve‮X"):
        with pytest.raises(comptes.ErreurCompte):
            comptes.creer_utilisateur(inst, "eve", nom, comptes.COMPTABLE, "un-long-mot-de-passe")
    for ident in ("a", "Eve Martin", "../x", "é"):
        with pytest.raises(comptes.ErreurCompte):
            comptes.creer_utilisateur(inst, ident, "Eve Martin", comptes.COMPTABLE, "un-long-mot-de-passe")
    with pytest.raises(comptes.ErreurCompte):
        comptes.creer_utilisateur(inst, "eve", "Eve Martin", "admin", "un-long-mot-de-passe")
    assert not inst.validateurs.exists() and not (inst.racine / "utilisateurs.json").exists()


def test_le_mot_de_passe_n_est_jamais_un_argument():
    aide = subprocess.run([sys.executable, str(RACINE / "scripts" / "web.py"), "creer-utilisateur", "--help"],
                          capture_output=True, text=True, check=True).stdout
    assert "--mot-de-passe" not in aide and "--password" not in aide


def test_aide_du_script():
    r = subprocess.run([sys.executable, str(RACINE / "scripts" / "web.py"), "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "servir" in r.stdout and "creer-utilisateur" in r.stdout


def test_reseau_loopback_par_defaut():
    assert est_loopback("127.0.0.1") and est_loopback("localhost") and est_loopback("::1")
    assert not est_loopback("0.0.0.0") and not est_loopback("192.168.1.10")


def test_avertissement_pour_une_adresse_non_locale(tmp_path, monkeypatch, capsys):
    cli = _charger_cli()
    inst = cycle.Instance(tmp_path / "inst")
    inst.preparer()
    lances = []

    class FauxServeur:
        server_port = 8123

        def serve_forever(self):
            lances.append(True)

        def server_close(self):
            pass

    monkeypatch.setattr(cli, "creer_serveur", lambda app, hote, port: (lances.append(hote), FauxServeur())[1])
    assert cli.main(["servir", "--instance", str(inst.racine), "--hote", "0.0.0.0", "--port", "8123"]) == 0
    err = capsys.readouterr().err
    assert "AVERTISSEMENT" in err and "NON locale" in err
    assert cli.main(["servir", "--instance", str(inst.racine)]) == 0
    assert "NON locale" not in capsys.readouterr().err
    assert lances[0] == "0.0.0.0" and "127.0.0.1" in lances


# ---------------------------------------------------------------------------
# Vrai serveur sur un port libre
# ---------------------------------------------------------------------------


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def test_serveur_reel_repond(inst, horloge):
    (inst.racine / "config.json").write_text('{"mode_test": true}', encoding="utf-8")
    app = Application(inst, horloge=horloge)
    serveur = creer_serveur(app, "127.0.0.1", 0)
    fil = threading.Thread(target=serveur.serve_forever, daemon=True)
    fil.start()
    try:
        base = f"http://127.0.0.1:{serveur.server_port}"
        with urllib.request.urlopen(f"{base}/connexion", timeout=10) as r:
            corps = r.read().decode("utf-8")
            assert r.status == 200 and r.headers["Content-Security-Policy"] == CSP
            assert "MODE TEST : DONNEES FICTIVES" in corps
            cookie = r.headers["Set-Cookie"].split(";")[0]
        jeton = extraire_csrf(corps)
        ouvreur = urllib.request.build_opener(_SansRedirection)
        donnees = urlencode({"identifiant": BOB, "mot_de_passe": MDP, "csrf": jeton}).encode()
        requete = urllib.request.Request(f"{base}/connexion", data=donnees, headers={
            "Cookie": cookie, "Origin": base, "Content-Type": "application/x-www-form-urlencoded"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            ouvreur.open(requete, timeout=10)
        assert exc.value.code == 303 and exc.value.headers["Location"] == "/"
        session = [v for v in exc.value.headers.get_all("Set-Cookie") if v.startswith("session=")][0].split(";")[0]
        with urllib.request.urlopen(urllib.request.Request(f"{base}/pieces", headers={"Cookie": session}),
                                    timeout=10) as r:
            assert r.status == 200 and "Pièces attendues" in r.read().decode("utf-8")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(urllib.request.Request(f"{base}/", method="PUT"), timeout=10)
        assert exc.value.code == 405
    finally:
        serveur.shutdown()
        serveur.server_close()
        app.fermer()
    assert "acces 127.0.0.1" in (inst.racine / "logs" / "web.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Defauts d'autres modules (xfail strict : rouges le jour ou ils sont corriges)
# ---------------------------------------------------------------------------


def test_regression_audit_sans_fcntl_deux_journaux_concurrents(tmp_path, monkeypatch):
    from rapprochement import audit as audit_mod

    monkeypatch.setattr(audit_mod, "fcntl", None)
    chemin = tmp_path / "audit.jsonl"
    premier, second = JournalAudit(chemin), JournalAudit(chemin)
    premier.ecrire("systeme", "init", "x")
    barriere = threading.Barrier(2, timeout=1.0)
    origine = JournalAudit._dernier

    def dernier(f, taille):
        resultat = origine(f, taille)
        try:
            barriere.wait()                   # les deux ecrivains ont lu la meme fin de journal
        except threading.BrokenBarrierError:
            pass                              # verrou correct : le second attendait le premier
        return resultat

    monkeypatch.setattr(JournalAudit, "_dernier", staticmethod(dernier))
    fils = [threading.Thread(target=j.ecrire, args=("systeme", "action", "x")) for j in (premier, second)]
    for t in fils:
        t.start()
    for t in fils:
        t.join(10)
    assert JournalAudit(chemin).verifier().ok
