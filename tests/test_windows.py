"""Audit de portabilite Windows, SIMULE (on ne peut pas executer Windows ici).

Ce que ces tests prouvent : les differences de plateforme que Python permet de reproduire sous
Linux (pas de `resource`, pas de `fcntl`, pas de `fork`, pas de base de fuseaux horaires,
console en cp1252, chemins avec espaces et accents, fichiers CRLF + BOM, noms de fichiers
illegaux, descripteurs encore ouverts). Ce qu'ils ne peuvent PAS prouver : le comportement
reel de NTFS (verrous de fichiers ouverts, `os.link`, longueur des chemins), du systeme
d'exploitation et de l'antivirus. Voir le rapport de l'integrateur.

Ce fichier ne depend d'aucun autre fichier de tests (helpers recopies).
"""

from __future__ import annotations

import csv
import datetime as dt
import gc
import json
import os
import shutil
import subprocess
import sys
import textwrap
import warnings
import zoneinfo
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

RACINE = Path(__file__).resolve().parents[1]
SRC = RACINE / "src"
sys.path.insert(0, str(SRC))

from rapprochement import cycle  # noqa: E402
from rapprochement.audit import JournalAudit  # noqa: E402
from rapprochement.modeles import EtatPiece, StatutBrouillon  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
J0 = dt.date(2026, 10, 5)            # lundi
VALIDEUSE = "Marie Durand"
RESPONSABLE = "Paul Martin"
CORPUS = RACINE / "data" / "corpus"


def a_10h(jour: dt.date) -> dt.datetime:
    return dt.datetime.combine(jour, dt.time(10, 0), tzinfo=PARIS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DOSSIERS = [
    ["ALPHA", "Alpha SARL", "compta@alpha-sarl.fr", "M. Alpha", "15", "courtois", "C-DIRECT", "", ""],
    ["BETA", "Beta SAS", "contact@beta-sas.fr", "Mme Beta", "20", "courtois", "C-RELAIS",
     "pieces@cabinet-relais.fr", ""],
]
OPERATIONS = [
    ["ALPHA", "2026-09-03", "CB LOXAM LOCATION", "120.00", "A-001"],
    ["ALPHA", "2026-09-17", "PRLV ORANGE PRO", "59.90", "A-002"],
    ["BETA", "2026-09-10", "CB METRO CASH", "230.40", "B-001"],
]


def ecrire_entree(rep: Path, *, dossiers=DOSSIERS, operations=OPERATIONS, pieces=(),
                  fin_de_ligne: str = "\n", encodage: str = "utf-8") -> Path:
    """Les trois CSV d'entree. `fin_de_ligne="\\r\\n"` + `encodage="utf-8-sig"` : export Excel."""
    rep.mkdir(parents=True, exist_ok=True)

    def ecrire(nom: str, entetes: list[str], lignes) -> None:
        with open(rep / nom, "w", newline="", encoding=encodage) as f:
            w = csv.writer(f, lineterminator=fin_de_ligne)
            w.writerow(entetes)
            w.writerows(lignes)

    ecrire("dossiers.csv", ["dossier", "raison_sociale", "email_contact", "nom_contact", "jour_echeance_tva",
                            "ton_relance", "circuit", "email_relais", "domaines"], dossiers)
    ecrire("releve_bancaire.csv", ["dossier", "date_operation", "date_valeur", "libelle", "debit", "credit",
                                   "devise", "reference"],
           [[d, date, date, lib, montant, "", "EUR", ref] for d, date, lib, montant, ref in operations])
    ecrire("pieces_recues.csv", ["id_piece", "dossier", "fichier", "fournisseur", "date_facture",
                                 "montant_ttc", "devise", "date_reception", "canal"], pieces)
    return rep


def nouvelle_instance(rep: Path) -> cycle.Instance:
    inst = cycle.Instance(rep)
    inst.preparer()
    inst.validateurs.write_text(f"{VALIDEUSE}\nPaul Martin\n", encoding="utf-8")
    inst.responsables.write_text("Paul Martin\n", encoding="utf-8")     # A14 : operations de responsable
    return inst


def ecrire_eml(rep: Path, nom: str, *, expediteur: str, message_id: str, corps: str = "",
               fichiers: tuple[tuple[str, bytes], ...] = ()) -> Path:
    msg = EmailMessage()
    msg["From"] = expediteur
    msg["To"] = "pieces@cabinet.invalid"
    msg["Subject"] = "Justificatif"
    msg["Message-ID"] = message_id
    msg["Date"] = "Wed, 07 Oct 2026 09:00:00 +0200"
    msg.set_content(corps)
    for nom_f, contenu in fichiers:
        msg.add_attachment(contenu, maintype="application", subtype="pdf", filename=nom_f)
    chemin = rep / nom
    chemin.write_bytes(bytes(msg))
    return chemin


def deposer_emls(inst: cycle.Instance) -> None:
    ecrire_eml(inst.entrant, "1.eml", expediteur="compta@alpha-sarl.fr", message_id="<a@a>", corps="120,00 EUR",
               fichiers=(("loxam_2026-09-03.pdf", b"%PDF-1"),))
    ecrire_eml(inst.entrant, "2.eml", expediteur="compta@alpha-sarl.fr", message_id="<b@a>", corps="demain")
    ecrire_eml(inst.entrant, "3.eml", expediteur="inconnu@gmail.com", message_id="<c@x>",
               fichiers=(("x.pdf", b"%PDF-2"),))


def lancer(entree: Path, inst: cycle.Instance, jour: dt.date = J0, **kw) -> cycle.ResumeCycle:
    return cycle.executer_cycle(entree, inst, jour, a_10h(jour), **kw)


def sortie_stable(inst: cycle.Instance) -> dict[str, bytes]:
    """Tout ce que le cycle ecrit sous sortie/, sauf resume.txt (qui nomme la strategie)."""
    return {str(p.relative_to(inst.sortie)): p.read_bytes()
            for p in sorted(inst.sortie.rglob("*")) if p.is_file() and p.name != "resume.txt"}


def sous_processus(args: list[str], **env: str) -> subprocess.CompletedProcess:
    """Python neuf, sans PYTHONPATH : `rapprochement` n'est pas installe, seul le code du
    script ajoute `src/` a `sys.path` (comme `scripts/cycle.py`)."""
    environnement = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    environnement.update(env)
    return subprocess.run([sys.executable, *args], capture_output=True, env=environnement, timeout=240)


# ---------------------------------------------------------------------------
# (a) Cycle complet sans `resource`, strategie spawn, memes chiffres que le corpus
# ---------------------------------------------------------------------------

CHIFFRES_CORPUS = {"operations": 284, "manquants": 23, "a_verifier": 8, "pieces_creees": 23,
                   "brouillons_crees": 3, "messages_lus": 3, "quarantaine": 0}


def test_a_cycle_complet_sans_resource_en_mode_test_reproduit_le_corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "resource", None)
    monkeypatch.setattr(cycle.multiprocessing, "get_all_start_methods", lambda: ["spawn"])
    inst = nouvelle_instance(tmp_path / "windows")
    deposer_emls(inst)
    r = lancer(CORPUS, inst, mode_test=True)
    assert r.strategie_isolation == "spawn"
    assert {cle: r.en_dict()[cle] for cle in CHIFFRES_CORPUS} == CHIFFRES_CORPUS


SIMULATION_WINDOWS = textwrap.dedent('''
    """Interpreteur "Windows" : ni `resource`, ni `fcntl` ; `src/` ajoute a l'execution
    seulement (le travailleur `spawn` doit le retrouver)."""
    import datetime as dt, json, sys, threading
    from pathlib import Path
    sys.modules["resource"] = None          # import resource -> ImportError
    sys.modules["fcntl"] = None
    sys.path.insert(0, sys.argv[1])


    def principal():
        from rapprochement import audit, cycle
        assert audit.fcntl is None and cycle.resource is None
        entree, racine, atelier = Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
        resultat = {}

        # (b) journal d'audit sans fcntl : verrou de thread seul, chaine intacte, reparation
        journal = audit.JournalAudit(atelier / "a.jsonl")
        def ecrire(n):
            for i in range(25):
                journal.ecrire("systeme", "test", f"t{n}-{i}", {"i": i})
        fils = [threading.Thread(target=ecrire, args=(n,)) for n in range(4)]
        [f.start() for f in fils]; [f.join() for f in fils]
        resultat["audit_entrees"] = len(journal.lire())
        resultat["audit_ok"] = journal.verifier().ok
        with open(atelier / "a.jsonl", "ab") as f:
            f.write(b'{"acteur":"sys')
        resultat["audit_tronque_detecte"] = not journal.verifier().ok
        resultat["audit_octets_retires"] = journal.reparer_fin_tronquee()
        resultat["audit_repare"] = journal.verifier().ok

        # cycle complet en mode test : strategie automatique -> spawn
        inst = cycle.Instance(racine)
        maintenant = dt.datetime(2026, 10, 5, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
        r = cycle.executer_cycle(entree, inst, dt.date(2026, 10, 5), maintenant, mode_test=True)
        resultat.update(strategie=r.strategie_isolation, avertissements=r.avertissements,
                        chiffres=r.en_dict(), audit_cycle_ok=cycle.verifier_audit(inst).ok)
        print(json.dumps(resultat, default=str))


    if __name__ == "__main__":
        principal()
''')


def test_a_b_interpreteur_sans_resource_ni_fcntl_cycle_et_audit(tmp_path):
    inst = nouvelle_instance(tmp_path / "instance")
    deposer_emls(inst)
    atelier = tmp_path / "atelier"
    atelier.mkdir()
    script = tmp_path / "simulation_windows.py"
    script.write_text(SIMULATION_WINDOWS, encoding="utf-8")
    r = sous_processus([str(script), str(SRC), str(CORPUS), str(inst.racine), str(atelier)])
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    resultat = json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])
    # (b) audit.py fonctionne sans fcntl (a l'interieur d'un meme processus)
    assert resultat["audit_entrees"] == 100 and resultat["audit_ok"]
    assert resultat["audit_tronque_detecte"] and resultat["audit_octets_retires"] > 0 and resultat["audit_repare"]
    # (a) le cycle : strategie spawn choisie seule, chiffres du corpus, enfants `spawn` sans `src/` dans l'environnement
    assert resultat["strategie"] == "spawn" and resultat["audit_cycle_ok"]
    assert resultat["avertissements"] == ["isolation partielle : temps seulement, pas de limite memoire"]
    assert {cle: resultat["chiffres"][cle] for cle in CHIFFRES_CORPUS} == CHIFFRES_CORPUS


# ---------------------------------------------------------------------------
# (c) Chemins avec espaces et accents
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategie", [
    pytest.param("fork", marks=pytest.mark.skipif(sys.platform == "win32", reason="fork : POSIX seulement")),
    "spawn",
])
def test_c_chemins_avec_espaces_et_accents(tmp_path, strategie):
    entree = ecrire_entree(tmp_path / "Données fictives é" / "entrée", fin_de_ligne="\n")
    inst = nouvelle_instance(tmp_path / "Mon Cabinet é" / "instance n°1")
    deposer_emls(inst)
    r = lancer(entree, inst, mode_test=True, strategie_isolation=strategie)
    assert r.quarantaine == 0 and r.messages_lus == 3 and r.propositions == 1
    assert cycle.verifier_audit(inst).ok
    b = cycle.brouillons(inst)[0]
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0), mode_test=True)
    cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0), mode_test=True)
    assert (inst.outbox / f"{b.id_relance}.eml").exists()
    export = cycle.exporter(inst, inst.racine / "export é.json")
    assert export.exists() and json.loads(export.read_text(encoding="utf-8"))["donnees"]["pieces"]


def test_c_cli_chemins_accentues_en_console_cp1252(tmp_path):
    entree = ecrire_entree(tmp_path / "Données é")
    instance = tmp_path / "Mon Cabinet é"
    args = [str(RACINE / "scripts" / "cycle.py"), "cycle", "--entree", str(entree), "--instance", str(instance),
            "--date", J0.isoformat(), "--mode-test"]
    r = sous_processus(args, PYTHONIOENCODING="cp1252")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert "Mon Cabinet é" in r.stdout.decode("utf-8")           # le chemin accentue est affiche sans erreur
    assert (instance / "sortie" / "resume.txt").exists()


# ---------------------------------------------------------------------------
# (d) CSV d'entree : fins de ligne CRLF et BOM UTF-8 (export Excel / Bloc-notes)
# ---------------------------------------------------------------------------


def test_d_csv_crlf_et_bom_donnent_les_memes_resultats_que_lf_sans_bom(tmp_path):
    pieces = [["P-1", "ALPHA", "orange.pdf", "Orange Pro", "2026-09-15", "59.90", "EUR", "2026-09-16", "email"]]
    propres = ecrire_entree(tmp_path / "propre", pieces=pieces)
    excel = ecrire_entree(tmp_path / "excel", pieces=pieces, fin_de_ligne="\r\n", encodage="utf-8-sig")
    for nom in ("dossiers.csv", "releve_bancaire.csv", "pieces_recues.csv"):
        brut = (excel / nom).read_bytes()
        assert brut.startswith(b"\xef\xbb\xbf") and b"\r\n" in brut                 # le piege est reel
        assert not (propres / nom).read_bytes().startswith(b"\xef\xbb\xbf")
    a, b = nouvelle_instance(tmp_path / "a"), nouvelle_instance(tmp_path / "b")
    ra, rb = lancer(propres, a), lancer(excel, b)
    assert ra.en_dict() == rb.en_dict() and ra.pieces_creees == 2 and ra.a_verifier == 0
    assert sortie_stable(a) == sortie_stable(b)


def test_d_validateurs_et_config_avec_bom_et_crlf(tmp_path):
    entree = ecrire_entree(tmp_path / "e")
    inst = nouvelle_instance(tmp_path / "i")
    inst.validateurs.write_bytes("﻿# equipe\r\nMarie Durand\r\n\r\nPaul Martin\r\n".encode("utf-8"))
    (inst.racine / "config.json").write_bytes(b"\xef\xbb\xbf" + b'{\r\n  "mode_test": true\r\n}\r\n')
    r = lancer(entree, inst)
    assert r.mode_test and cycle.exiger_validateur(inst, "marie durand") == "Marie Durand"


# Etat initial (defaut corrige, test de regression) : exclusions.py ouvrait
# exclusions_dossier.csv en `utf-8` et non `utf-8-sig`, contrairement aux trois autres lecteurs.
# Un fichier enregistre par Excel ("CSV UTF-8") ou le Bloc-notes porte un BOM : la premiere
# colonne devenait '\ufeffdossier' et le cycle etait refuse (KeyError). Ce test etait un xfail
# strict ; il est devenu rouge (XPASS) quand exclusions.py a ete corrige.
def test_d_exclusions_avec_bom_et_crlf(tmp_path):
    entree = ecrire_entree(tmp_path / "e")
    (entree / "exclusions_dossier.csv").write_bytes(
        "﻿dossier,motif_regex,motif\r\nALPHA,loxam,Location refacturee\r\n".encode("utf-8"))
    r = lancer(entree, nouvelle_instance(tmp_path / "i"))
    assert r.pieces_exclues == 0 and r.pieces_creees == 2        # LOXAM exclu : 2 pieces au lieu de 3


# ---------------------------------------------------------------------------
# (e) Aucun nom de fichier ecrit par le cycle n'est illegal sous Windows
# ---------------------------------------------------------------------------

INTERDITS = set('<>:"/\\|?*') | {chr(i) for i in range(32)}
RESERVES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def illegal_sous_windows(nom: str) -> str | None:
    """Raison pour laquelle `nom` (un composant de chemin) est illegal sous Windows, ou None."""
    if not nom or len(nom) > 255:
        return "vide ou trop long"
    if any(c in INTERDITS for c in nom):
        return "caractere interdit (: < > ? * \" | \\ / ou controle)"
    if nom[-1] in ". ":
        return "finit par un point ou une espace"
    if nom.split(".")[0].upper() in RESERVES:
        return "nom de peripherique reserve"
    return None


@pytest.mark.parametrize("nom, attendu", [
    ("rapport.csv", None), (".tmp-ab12.part", None), ("depot.sqlite-wal", None),
    ("a:b.pdf", "caractere"), ("a*.pdf", "caractere"), ('a"b', "caractere"), ("a|b", "caractere"),
    ("a?.txt", "caractere"), ("a<b>", "caractere"), ("a\x1bb", "caractere"), ("fin.", "point"),
    ("fin ", "point"), ("CON", "reserve"), ("nul.txt", "reserve"), ("Com1.eml", "reserve"), ("", "vide"),
])
def test_e_le_detecteur_de_noms_illegaux_est_juste(nom, attendu):
    raison = illegal_sous_windows(nom)
    assert (raison is None) if attendu is None else (raison is not None and attendu in raison)


def test_e_aucun_nom_ecrit_ne_derive_d_une_donnee_externe_illegale(tmp_path):
    """Donnees hostiles partout (dossier, reference, libelle, expediteur, Message-ID, noms de pieces
    jointes) : tout ce que le cycle, l'envoi, la reparation d'audit et l'export ecrivent doit avoir
    des noms legaux sous Windows et des chemins courts (marge sous MAX_PATH = 260)."""
    hostile = [["CON", "Societe : <hostile>", "nul@clients.fr", "M. \"X\"", "15", "courtois", "C-DIRECT", "", ""],
               ["AB.", "Autre | societe", "ab@autre.fr", "Y", "15", "courtois", "C-DIRECT", "", ""]]
    operations = [["CON", "2026-09-03", "CB ..\\..\\evil: <x>|?*", "120.00", "..\\..\\REF:1*"],
                  ["CON", "2026-09-04", "CB LOXAM ", "50.00", "NUL"],
                  ["AB.", "2026-09-05", "CB AUX.", "75.00", "A/B."]]
    entree = ecrire_entree(tmp_path / "e", dossiers=hostile, operations=operations)
    inst = nouvelle_instance(tmp_path / "instance")
    noms_hostiles = ('a:b*c?.pdf', '..\\..\\evil.pdf', 'con.pdf', 'fin. ', 'x"y<z>|.pdf', "\x1b]52;c;ZXZpbA==\x07.pdf")
    for k, nom in enumerate(noms_hostiles):
        ecrire_eml(inst.entrant, f"{k}.eml", expediteur="nul@clients.fr" if k % 2 else "x@gmail.com",
                   message_id=f"<{k}:a|b?*\"<>\\/@x>", corps="120,00 EUR", fichiers=((nom, b"%PDF-" + bytes([k])),))
    lancer(entree, inst)
    for b in cycle.brouillons(inst):
        cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0))
        cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0))
    (inst.entrant / "0-bombe.eml").write_bytes(b"From: a@b.fr\r\n\r\nx" + b"\r\nContent-Type: message/rfc822\r\n\r\n" * 3000)
    lancer(entree, inst, J0 + dt.timedelta(days=1))                               # alimente quarantaine.csv
    with open(inst.audit, "ab") as f:
        f.write(b'{"acteur":"sys')                                                 # coupure -> fragment d'audit
    cycle.reparer_audit(inst, RESPONSABLE, maintenant=a_10h(J0))
    cycle.exporter(inst, inst.racine / "export.json")

    fichiers = sorted(p for p in inst.racine.rglob("*"))
    assert len(fichiers) > 12
    for chemin in fichiers:
        relatif = chemin.relative_to(inst.racine)
        for composant in relatif.parts:
            assert illegal_sous_windows(composant) is None, (str(relatif), illegal_sous_windows(composant))
        assert len(str(relatif)) <= 100, str(relatif)
    assert any(p.name.startswith("audit.jsonl.fragment-") for p in fichiers)
    assert list((inst.outbox).glob("*.eml")) and list((inst.sortie / "brouillons").glob("*.txt"))
    # Les noms de pieces jointes n'ont jamais servi de nom de fichier : ils restent dans la base.
    assert not [p for p in fichiers if "evil" in p.name or "IMPORT" in p.name or p.name.startswith("a:")]


# ---------------------------------------------------------------------------
# (f) Aucun descripteur ni poignee laisse ouvert (Windows refuse de supprimer/renommer)
# ---------------------------------------------------------------------------


def ouverts_sous(racine: Path) -> list[str]:
    """Fichiers de `racine` encore ouverts par CE processus (Linux : /proc/self/fd).
    Vide ailleurs : le test de suppression du repertoire prend alors le relais."""
    base = Path("/proc/self/fd")
    if not base.exists():
        return []
    prefixe = str(racine.resolve())
    trouves = []
    for fd in base.iterdir():
        try:
            cible = os.readlink(fd)
        except OSError:
            continue
        if cible.startswith(prefixe):
            trouves.append(cible)
    return trouves


@pytest.fixture
def depots_tenus(monkeypatch):
    """Garde une reference forte sur chaque `Depot` ouvert. Sans cela, CPython ferme la
    connexion SQLite des que l'objet est libere et MASQUE un oubli de `fermer()` : or sous
    Windows la fermeture doit etre explicite, et un objet retenu par une trace d'exception
    (ou un autre fil) garde le fichier verrouille."""
    from rapprochement.depot import Depot
    tenus: list[Depot] = []
    original = Depot.__init__

    def init(self, *args, **kw):
        original(self, *args, **kw)
        tenus.append(self)

    monkeypatch.setattr(Depot, "__init__", init)
    return tenus


def test_f_aucun_descripteur_ouvert_apres_chaque_operation_et_repertoire_supprimable(tmp_path, depots_tenus):
    entree = ecrire_entree(tmp_path / "e")
    inst = nouvelle_instance(tmp_path / "instance")
    (inst.racine / "config.json").write_text('{"mode_test": true}', encoding="utf-8")   # mode fige : test
    deposer_emls(inst)

    def rien_d_ouvert(etape: str) -> None:
        gc.collect()
        assert ouverts_sous(inst.racine) == [], etape
        assert [d for d in depots_tenus if not d._ferme] == [], f"{etape} : Depot non ferme explicitement"

    with warnings.catch_warnings(record=True) as capte:
        warnings.simplefilter("always")
        lancer(entree, inst)
        rien_d_ouvert("cycle fork")
        lancer(entree, inst, J0 + dt.timedelta(days=1), mode_test=True, strategie_isolation="spawn")
        rien_d_ouvert("cycle spawn")
        lancer(entree, inst, J0 + dt.timedelta(days=2), mode_test=True, strategie_isolation="directe")
        rien_d_ouvert("cycle directe")
        brouillons = cycle.brouillons(inst)
        cycle.valider_relance(inst, brouillons[0].id_relance, VALIDEUSE, maintenant=a_10h(J0))
        cycle.envoyer_relance(inst, brouillons[0].id_relance, maintenant=a_10h(J0))
        rien_d_ouvert("valider + envoyer")
        cycle.rejeter_relance(inst, brouillons[1].id_relance, VALIDEUSE, "trop tot", maintenant=a_10h(J0))
        with pytest.raises(Exception):
            cycle.envoyer_relance(inst, brouillons[1].id_relance, maintenant=a_10h(J0))   # refus : fermeture aussi
        rien_d_ouvert("rejeter + envoi refuse")
        cycle.rattacher_piece(inst, VALIDEUSE, maintenant=a_10h(J0), dossier="ALPHA", reference="A-001",
                              id_piece="P-1")
        cycle.controler_piece(inst, "ALPHA", "A-001", VALIDEUSE, conforme=True, maintenant=a_10h(J0))
        cycle.bloquer_piece(inst, "ALPHA", "A-002", VALIDEUSE, motif="litige", maintenant=a_10h(J0))
        cycle.suivi_pieces(inst, J0)
        cycle.lister_file(inst)
        cycle.pieces(inst)
        cycle.verifier_audit(inst)
        rien_d_ouvert("decisions humaines et lectures")
        cycle.exporter(inst, inst.racine / "export.json")
        cycle.exporter(inst, inst.racine / "export.json")          # os.replace sur un fichier existant
        assert not list(inst.racine.glob("*.tmp"))
        with open(inst.audit, "ab") as f:
            f.write(b'{"acteur":"sys')
        cycle.reparer_audit(inst, RESPONSABLE, maintenant=a_10h(J0))
        rien_d_ouvert("export + reparation d'audit")
        # Panne au milieu d'un cycle : la base est fermee aussi sur le chemin d'erreur.
        with pytest.raises(RuntimeError):
            lancer(entree, inst, J0 + dt.timedelta(days=3),
                   panne=lambda point: (_ for _ in ()).throw(RuntimeError(point)) if point == "apres_pieces" else None)
        rien_d_ouvert("cycle interrompu par une panne")
        gc.collect()
    ressources = [w for w in capte if issubclass(w.category, ResourceWarning)]
    assert ressources == [], [str(w.message) for w in ressources]
    assert len(depots_tenus) >= 15 and all(d._ferme for d in depots_tenus)
    shutil.rmtree(inst.racine)                                       # sous Windows : echoue si une poignee reste
    assert not inst.racine.exists()


# ---------------------------------------------------------------------------
# Fuseaux horaires : Windows n'a pas de base `zoneinfo` (hors paquet PyPI `tzdata`)
# ---------------------------------------------------------------------------


@pytest.fixture
def sans_base_de_fuseaux(tmp_path, monkeypatch):
    """Simule Windows sans `tzdata` : chemin de zoneinfo vide, paquet `tzdata` introuvable."""
    anciens = list(zoneinfo.TZPATH)
    vide = tmp_path / "tz_vide"
    vide.mkdir()
    # Sous Windows `tzdata` est installe ET deja importe : masquer le seul paquet racine ne
    # suffit pas, les sous-paquets en cache (`tzdata.zoneinfo.Europe`) restent resolus.
    for nom in [n for n in list(sys.modules) if n == "tzdata" or n.startswith("tzdata.")] + [
            "tzdata", "tzdata.zoneinfo", "tzdata.zoneinfo.Europe"]:
        monkeypatch.setitem(sys.modules, nom, None)
    monkeypatch.setattr(cycle, "_FUSEAUX_VERIFIES", False)
    zoneinfo.reset_tzpath(to=[str(vide)])
    zoneinfo.ZoneInfo.clear_cache()
    with pytest.raises(zoneinfo.ZoneInfoNotFoundError):
        zoneinfo.ZoneInfo("Europe/Paris")                           # le probleme est reel
    yield
    zoneinfo.reset_tzpath(to=anciens)
    zoneinfo.ZoneInfo.clear_cache()


def test_fuseaux_le_repli_est_identique_a_europe_paris_sur_dix_ans(tmp_path):
    reel = ZoneInfo("Europe/Paris")
    fichier = tmp_path / "Paris"
    fichier.write_bytes(cycle._tzif_europe_paris())
    with open(fichier, "rb") as f:
        secours = ZoneInfo.from_file(f, key="Europe/Paris")
    debut = dt.datetime(2021, 1, 1, tzinfo=dt.timezone.utc)
    for heures in range(0, 24 * 365 * 10, 1):                        # toutes les heures de 2021 a 2030
        instant = debut + dt.timedelta(hours=heures)
        # Pas de `==` entre deux fuseaux : dans l'heure repetee de la bascule d'octobre, la
        # PEP 495 declare toujours inegaux deux datetimes de fuseaux differents.
        mien, vrai = instant.astimezone(secours), instant.astimezone(reel)
        assert (mien.isoformat(), mien.tzname(), mien.dst()) == (vrai.isoformat(), vrai.tzname(), vrai.dst()), instant
    for minutes in (-1, 0, 1):                                       # autour des deux bascules de 2026
        for base in (dt.datetime(2026, 3, 29, 1, 0, tzinfo=dt.timezone.utc),
                     dt.datetime(2026, 10, 25, 1, 0, tzinfo=dt.timezone.utc)):
            instant = base + dt.timedelta(minutes=minutes)
            assert instant.astimezone(secours).utcoffset() == instant.astimezone(reel).utcoffset()


def test_fuseaux_cycle_validation_et_envoi_sans_base_de_fuseaux(tmp_path, sans_base_de_fuseaux):
    """cadence.py et envoi.py appellent ZoneInfo("Europe/Paris") eux-memes : le repli installe
    par le cycle doit leur suffire (sinon tout envoi leve ZoneInfoNotFoundError)."""
    assert cycle.garantir_fuseaux() == "secours"
    assert cycle.garantir_fuseaux() == "systeme"                    # idempotent : plus rien a faire
    assert ZoneInfo("Europe/Paris").utcoffset(dt.datetime(2026, 7, 1, 12)) == dt.timedelta(hours=2)
    entree = ecrire_entree(tmp_path / "e")
    inst = nouvelle_instance(tmp_path / "i")
    lancer(entree, inst, mode_test=True)
    b = cycle.brouillons(inst)[0]
    cycle.valider_relance(inst, b.id_relance, VALIDEUSE, maintenant=a_10h(J0), mode_test=True)
    envoye = cycle.envoyer_relance(inst, b.id_relance, maintenant=a_10h(J0), mode_test=True)
    assert envoye.statut is StatutBrouillon.ENVOYEE
    # Creneau d'envoi (cadence.heure_d_envoi_valide) : 07:59 UTC = 09:59 Paris l'ete, ouvre.
    from rapprochement.cadence import heure_d_envoi_valide
    assert heure_d_envoi_valide(dt.datetime(2026, 7, 7, 6, 30, tzinfo=dt.timezone.utc))        # 08:30 Paris
    assert not heure_d_envoi_valide(dt.datetime(2026, 7, 7, 5, 30, tzinfo=dt.timezone.utc))    # 07:30 Paris


def test_fuseaux_cli_complet_sans_base_de_fuseaux(tmp_path):
    """Le CLI entier (cycle, valider, envoyer) dans un interpreteur dont zoneinfo n'a aucune base."""
    vide = tmp_path / "tz_vide"
    vide.mkdir()
    entree = ecrire_entree(tmp_path / "e")
    inst = nouvelle_instance(tmp_path / "i")
    cli = str(RACINE / "scripts" / "cycle.py")
    base = ["--instance", str(inst.racine), "--date", J0.isoformat()]
    env = {"PYTHONTZPATH": str(vide)}
    r = sous_processus([cli, "cycle", *base, "--entree", str(entree), "--mode-test"], **env)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    (b, *_) = cycle.brouillons(inst)
    r = sous_processus([cli, "valider", *base, "--mode-test", "--par", VALIDEUSE, b.id_relance], **env)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    r = sous_processus([cli, "envoyer", *base, "--mode-test", "--par", VALIDEUSE, b.id_relance], **env)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert (inst.outbox / f"{b.id_relance}.eml").exists()


# ---------------------------------------------------------------------------
# Le CLI sous `spawn` : le module principal est reimporte par chaque enfant
# ---------------------------------------------------------------------------


def test_cli_spawn_reimporte_le_script_sans_relancer_le_cycle(tmp_path):
    """`scripts/cycle.py` a sa garde `__main__` : l'enfant `spawn` le reimporte (sans PYTHONPATH,
    `src/` n'est dans `sys.path` que parce que le script l'y met) mais ne relance rien."""
    entree = ecrire_entree(tmp_path / "e")
    inst = nouvelle_instance(tmp_path / "i")
    deposer_emls(inst)
    r = sous_processus([str(RACINE / "scripts" / "cycle.py"), "cycle", "--instance", str(inst.racine),
                        "--date", J0.isoformat(), "--entree", str(entree), "--mode-test", "--isolation", "spawn"])
    sortie = r.stdout.decode("utf-8")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert "AVERTISSEMENT : isolation partielle : temps seulement, pas de limite memoire" in sortie
    assert "quarantaine                 : 0" in sortie and "messages_lus                : 3" in sortie
    assert sortie.count("Cycle du") == 1                              # un seul cycle, pas un par enfant
    assert [p.name for p in inst.sortie.glob("quarantaine.csv")] and \
        (inst.sortie / "quarantaine.csv").read_text(encoding="utf-8").count("\n") == 1
    assert JournalAudit(inst.audit).verifier().ok


# ---------------------------------------------------------------------------
# audit.py sous Windows : deux ecrivains sans `fcntl` (defaut corrige, test de regression)
# ---------------------------------------------------------------------------
#
# Etat initial (mesure avant correction) : le verrou entre processus reposait sur fcntl.flock ;
# sans `fcntl` il ne restait qu'un verrou de THREAD par objet JournalAudit. Le CLI et
# l'application web, deux processus ecrivant la meme instance, lisaient la meme derniere entree
# puis ajoutaient le meme `seq` : 5 journaux casses sur 5 (3 processus x 200 ecritures), et le
# cycle suivant refusait de demarrer (JournalNonSain) sans que `reparer-audit` puisse y remedier.
# Ce test etait un xfail strict ; il est devenu rouge (XPASS) quand audit.py a recu un verrou
# `<journal>.lock` (O_CREAT|O_EXCL) pour ce cas. Il reste comme garde-fou. Les deux ecrivains
# sont deux objets JournalAudit (donc deux verrous de thread) avec un rendez-vous force au
# meme instant de lecture : sans verrou entre eux, la chaine casse a coup sur.
# LIMITE : le chemin Windows de ce verrou (test de PID par ctypes) n'est exerce ici que sous
# Linux avec `fcntl` rendu indisponible.


def test_audit_sans_fcntl_deux_ecrivains_gardent_la_chaine_intacte(tmp_path, monkeypatch):
    import threading
    from rapprochement import audit

    monkeypatch.setattr(audit, "fcntl", None)                     # plateforme Windows
    chemin = tmp_path / "audit.jsonl"
    audit.JournalAudit(chemin).ecrire("systeme", "depart", "x")
    rendez_vous = threading.Barrier(2, timeout=2)
    lire_derniere = audit.JournalAudit._dernier

    def dernier_puis_rendez_vous(f, taille):                      # `_dernier` est une methode statique
        resultat = lire_derniere(f, taille)                       # les deux ont vu la meme derniere entree
        try:
            rendez_vous.wait()
        except threading.BrokenBarrierError:                      # un verrou les a serialises : tant mieux
            pass
        return resultat

    monkeypatch.setattr(audit.JournalAudit, "_dernier", staticmethod(dernier_puis_rendez_vous))
    erreurs: list[BaseException] = []

    def ecrire(nom: str) -> None:
        try:
            audit.JournalAudit(chemin).ecrire("systeme", "test", nom)
        except BaseException as exc:  # noqa: BLE001
            erreurs.append(exc)

    fils = [threading.Thread(target=ecrire, args=(f"processus-{n}",)) for n in range(2)]
    [f.start() for f in fils]
    [f.join() for f in fils]
    monkeypatch.undo()
    assert erreurs == []
    assert audit.JournalAudit(chemin).verifier().ok
