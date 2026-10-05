"""Persistance SQLite (contrat 4.2 de docs/architecture_mvp.md).

Principes :
- Les `Decimal` sont stockes en TEXT (jamais REAL), les dates en ISO 8601.
- Aucune requete n'est construite par concatenation de donnees : tout passe par
  des parametres (les seuls fragments de SQL dynamiques sont des constantes du
  code ou un nombre de `?`).
- Une connexion = un `Depot`. Plusieurs `Depot` (threads, processus) peuvent
  ouvrir le meme fichier : WAL, `busy_timeout`, `BEGIN IMMEDIATE`. Un meme
  `Depot` est aussi utilisable depuis plusieurs threads (verrou interne).
- Les methodes ne committent pas elles-memes dans un bloc `transaction()` :
  tout le bloc est atomique. Les `transaction()` imbriquees sont des
  SAVEPOINT.

Ajouts au contrat (rien n'est retire ni change) : `importer_json`, context
manager (`with Depot(...) as d`), `VersionSchemaInconnue`, `ErreurBase`, et
quelques garde-fous de `maj_brouillon` (voir sa docstring).

Limites connues :
- Sur systeme de fichiers reseau, WAL est peu fiable (limite de SQLite).
- `exporter_json` lit un instantane coherent mais n'est pas incremental.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
import time
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from .modeles import (
    Brouillon,
    DecisionRoutage,
    EnvoiRelance,
    EtatPiece,
    MotifNonRoute,
    PieceAttendue,
    StatutBrouillon,
    StatutRoutage,
)

SCHEMA_VERSION = 1


class VersionSchemaInconnue(RuntimeError):
    """La base a ete creee par une version plus recente du logiciel."""


class ErreurBase(RuntimeError):
    """Le fichier n'est pas une base de ce logiciel."""


_B = StatutBrouillon
TRANSITIONS_BROUILLON: frozenset[tuple[StatutBrouillon, StatutBrouillon]] = frozenset(
    {
        (_B.BROUILLON, _B.VALIDEE),
        (_B.BROUILLON, _B.REJETEE),
        (_B.VALIDEE, _B.EN_COURS),
        (_B.VALIDEE, _B.REJETEE),
        (_B.EN_COURS, _B.ENVOYEE),
        (_B.EN_COURS, _B.REJETEE),
        (_B.EN_COURS, _B.VALIDEE),  # echec CERTAIN avant emission
    }
)


def _liste_sql(valeurs: Iterable[Any]) -> str:
    return ", ".join("'" + str(v.value) + "'" for v in valeurs)


# Les enums viennent du code, jamais des donnees.
_SCHEMA = (
    f"""CREATE TABLE pieces (
        dossier TEXT NOT NULL,
        reference TEXT NOT NULL,
        periode TEXT NOT NULL,
        montant TEXT NOT NULL,
        date_operation TEXT NOT NULL,
        libelle TEXT NOT NULL,
        etat TEXT NOT NULL CHECK (etat IN ({_liste_sql(EtatPiece)})),
        nb_relances INTEGER NOT NULL,
        date_premiere_demande TEXT,
        date_derniere_relance TEXT,
        date_promesse TEXT,
        bloquee INTEGER NOT NULL CHECK (bloquee IN (0, 1)),
        motif_blocage TEXT NOT NULL,
        pieces_rattachees TEXT NOT NULL,
        devise TEXT NOT NULL DEFAULT 'EUR',
        PRIMARY KEY (dossier, reference)
    )""",
    "CREATE INDEX idx_pieces_tri ON pieces (dossier, date_operation, reference)",
    "CREATE INDEX idx_pieces_periode ON pieces (periode)",
    """CREATE TABLE vus (
        cle TEXT PRIMARY KEY,
        vu_le TEXT NOT NULL
    )""",
    f"""CREATE TABLE file_humaine (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT NOT NULL,
        empreinte TEXT NOT NULL,
        nom_fichier TEXT NOT NULL,
        statut TEXT NOT NULL CHECK (statut IN ({_liste_sql(StatutRoutage)})),
        dossier TEXT,
        periode TEXT,
        reference_operation TEXT,
        motif TEXT CHECK (motif IS NULL OR motif IN ({_liste_sql(MotifNonRoute)})),
        detail TEXT NOT NULL,
        resolue INTEGER NOT NULL DEFAULT 0 CHECK (resolue IN (0, 1)),
        resolue_par TEXT NOT NULL DEFAULT '',
        resolue_note TEXT NOT NULL DEFAULT '',
        resolue_le TEXT,
        UNIQUE (message_id, empreinte)
    )""",
    f"""CREATE TABLE brouillons (
        id_relance TEXT PRIMARY KEY,
        destinataire TEXT NOT NULL,
        objet TEXT NOT NULL,
        corps TEXT NOT NULL,
        dossiers TEXT NOT NULL,
        refs TEXT NOT NULL,
        niveau INTEGER NOT NULL,
        cree_le TEXT NOT NULL,
        statut TEXT NOT NULL CHECK (statut IN ({_liste_sql(StatutBrouillon)})),
        valide_par TEXT NOT NULL,
        valide_le TEXT,
        envoye_le TEXT
    )""",
    "CREATE INDEX idx_brouillons_statut ON brouillons (statut, cree_le, id_relance)",
    """CREATE TABLE envois (
        id_relance TEXT PRIMARY KEY,
        destinataire TEXT NOT NULL,
        destinataire_norm TEXT NOT NULL,
        date_envoi TEXT NOT NULL,
        dossiers TEXT NOT NULL,
        refs TEXT NOT NULL
    )""",
    "CREATE INDEX idx_envois_dest ON envois (destinataire_norm, date_envoi)",
    # Defense en profondeur : meme un SQL direct ne peut pas modifier un envoi
    # effectif ni un brouillon ENVOYEE.
    """CREATE TRIGGER brouillon_envoye_immuable BEFORE UPDATE ON brouillons
       WHEN OLD.statut = 'envoyee'
       BEGIN SELECT RAISE(ABORT, 'brouillon ENVOYEE immuable'); END""",
    """CREATE TRIGGER brouillon_envoye_indestructible BEFORE DELETE ON brouillons
       WHEN OLD.statut = 'envoyee'
       BEGIN SELECT RAISE(ABORT, 'brouillon ENVOYEE indestructible'); END""",
    """CREATE TRIGGER envoi_immuable BEFORE UPDATE ON envois
       BEGIN SELECT RAISE(ABORT, 'envoi immuable'); END""",
    """CREATE TRIGGER envoi_indestructible BEFORE DELETE ON envois
       BEGIN SELECT RAISE(ABORT, 'envoi indestructible'); END""",
)

# Colonnes documentees dans l'export (cle `schema`).
_SCHEMA_EXPORT = {
    "format": "rapprochement-depot",
    "version": SCHEMA_VERSION,
    "description": (
        "Export complet du depot. Decimal en chaine, dates en ISO 8601, "
        "tuples en listes, enums par leur valeur. Lignes triees de facon deterministe."
    ),
    "tables": {
        "pieces": [
            "reference", "dossier", "periode", "montant", "date_operation", "libelle",
            "etat", "nb_relances", "date_premiere_demande", "date_derniere_relance",
            "date_promesse", "bloquee", "motif_blocage", "pieces_rattachees", "devise",
        ],
        "vus": ["cle", "vu_le"],
        "file_humaine": [
            "message_id", "empreinte", "nom_fichier", "statut", "dossier", "periode",
            "reference_operation", "motif", "detail",
            "resolue", "resolue_par", "resolue_note", "resolue_le",
        ],
        "brouillons": [
            "id_relance", "destinataire", "objet", "corps", "dossiers", "references",
            "niveau", "cree_le", "statut", "valide_par", "valide_le", "envoye_le",
        ],
        "envois": ["id_relance", "destinataire", "date_envoi", "dossiers", "references"],
    },
    "ordre": {
        "pieces": "(dossier, reference)",
        "vus": "cle",
        "file_humaine": "ordre d'insertion",
        "brouillons": "id_relance",
        "envois": "id_relance",
    },
}


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


def _date_iso(d: dt.date) -> str:
    # Un datetime est un date : l'accepter ferait stocker un format different
    # et fausserait les comparaisons de chaines.
    if isinstance(d, dt.datetime) or not isinstance(d, dt.date):
        raise TypeError(f"date attendue, recu {type(d).__name__}")
    return d.isoformat()


def _date_iso_ou_none(d: dt.date | None) -> str | None:
    return None if d is None else _date_iso(d)


def _datetime_iso(x: dt.datetime | None) -> str | None:
    if x is None:
        return None
    if not isinstance(x, dt.datetime):
        raise TypeError(f"datetime attendu, recu {type(x).__name__}")
    return x.isoformat()


def _date_lue(s: str | None) -> dt.date | None:
    return None if s is None else dt.date.fromisoformat(s)


def _datetime_lu(s: str | None) -> dt.datetime | None:
    return None if s is None else dt.datetime.fromisoformat(s)


def _montant_texte(m: Decimal) -> str:
    if isinstance(m, bool) or not isinstance(m, Decimal):
        raise TypeError(f"montant : Decimal attendu, recu {type(m).__name__} (jamais float)")
    if not m.is_finite():
        raise ValueError(f"montant non fini : {m!r}")
    return str(m)


def _json_liste(t: Iterable[str]) -> str:
    valeurs = list(t)
    if not all(isinstance(v, str) for v in valeurs):
        raise TypeError("liste de chaines attendue")
    return json.dumps(valeurs, ensure_ascii=False, separators=(",", ":"))


def _tuple_lu(s: str) -> tuple[str, ...]:
    return tuple(json.loads(s))


def _norm_dest(destinataire: str) -> str:
    return destinataire.strip().lower()


def _maintenant() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _devise(d: str) -> str:
    if not isinstance(d, str) or not d.strip():
        raise ValueError("devise : chaine non vide obligatoire")
    return d


def _piece_valeurs(p: PieceAttendue) -> tuple[Any, ...]:
    return (
        p.dossier, p.reference, p.periode, _montant_texte(p.montant),
        _date_iso(p.date_operation), p.libelle, EtatPiece(p.etat).value, int(p.nb_relances),
        _date_iso_ou_none(p.date_premiere_demande), _date_iso_ou_none(p.date_derniere_relance),
        _date_iso_ou_none(p.date_promesse), 1 if p.bloquee else 0, p.motif_blocage,
        _json_liste(p.pieces_rattachees), _devise(p.devise),
    )


def _piece_lue(r: sqlite3.Row) -> PieceAttendue:
    return PieceAttendue(
        reference=r["reference"],
        dossier=r["dossier"],
        periode=r["periode"],
        montant=Decimal(r["montant"]),
        date_operation=dt.date.fromisoformat(r["date_operation"]),
        libelle=r["libelle"],
        etat=EtatPiece(r["etat"]),
        nb_relances=r["nb_relances"],
        date_premiere_demande=_date_lue(r["date_premiere_demande"]),
        date_derniere_relance=_date_lue(r["date_derniere_relance"]),
        date_promesse=_date_lue(r["date_promesse"]),
        bloquee=bool(r["bloquee"]),
        motif_blocage=r["motif_blocage"],
        pieces_rattachees=_tuple_lu(r["pieces_rattachees"]),
        devise=r["devise"],
    )


def _decision_lue(r: sqlite3.Row) -> DecisionRoutage:
    return DecisionRoutage(
        message_id=r["message_id"],
        nom_fichier=r["nom_fichier"],
        empreinte=r["empreinte"],
        statut=StatutRoutage(r["statut"]),
        dossier=r["dossier"],
        periode=r["periode"],
        reference_operation=r["reference_operation"],
        motif=None if r["motif"] is None else MotifNonRoute(r["motif"]),
        detail=r["detail"],
    )


def _brouillon_valeurs(b: Brouillon) -> tuple[Any, ...]:
    return (
        b.id_relance, b.destinataire, b.objet, b.corps, _json_liste(b.dossiers),
        _json_liste(b.references), int(b.niveau), _date_iso(b.cree_le),
        StatutBrouillon(b.statut).value, b.valide_par, _datetime_iso(b.valide_le),
        _datetime_iso(b.envoye_le),
    )


def _brouillon_lu(r: sqlite3.Row) -> Brouillon:
    return Brouillon(
        id_relance=r["id_relance"],
        destinataire=r["destinataire"],
        objet=r["objet"],
        corps=r["corps"],
        dossiers=_tuple_lu(r["dossiers"]),
        references=_tuple_lu(r["refs"]),
        niveau=r["niveau"],
        cree_le=dt.date.fromisoformat(r["cree_le"]),
        statut=StatutBrouillon(r["statut"]),
        valide_par=r["valide_par"],
        valide_le=_datetime_lu(r["valide_le"]),
        envoye_le=_datetime_lu(r["envoye_le"]),
    )


def _envoi_lu(r: sqlite3.Row) -> EnvoiRelance:
    return EnvoiRelance(
        id_relance=r["id_relance"],
        destinataire=r["destinataire"],
        date_envoi=dt.date.fromisoformat(r["date_envoi"]),
        dossiers=_tuple_lu(r["dossiers"]),
        references=_tuple_lu(r["refs"]),
    )


# ---------------------------------------------------------------------------
# Depot
# ---------------------------------------------------------------------------

_PIECES_COLONNES = (
    "dossier, reference, periode, montant, date_operation, libelle, etat, nb_relances, "
    "date_premiere_demande, date_derniere_relance, date_promesse, bloquee, "
    "motif_blocage, pieces_rattachees, devise"
)
_BROUILLONS_COLONNES = (
    "id_relance, destinataire, objet, corps, dossiers, refs, niveau, cree_le, "
    "statut, valide_par, valide_le, envoye_le"
)


class Depot:
    def __init__(self, chemin: Path | str) -> None:
        self._verrou = threading.RLock()
        self._profondeur = 0
        self._ferme = False
        memoire = str(chemin) == ":memory:"
        if not memoire:
            Path(chemin).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(chemin), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        try:
            conn.row_factory = sqlite3.Row
            self._initialiser(conn, memoire)
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    # -- ouverture ---------------------------------------------------------

    @staticmethod
    def _lire_version(conn: sqlite3.Connection) -> int:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _refuser_version_future(version: int) -> None:
        if version > SCHEMA_VERSION:
            raise VersionSchemaInconnue(
                f"base en version de schema {version}, ce logiciel ne connait que "
                f"la version {SCHEMA_VERSION} : mettre le logiciel a jour "
                "(la base n'a pas ete modifiee)"
            )

    def _initialiser(self, conn: sqlite3.Connection, memoire: bool) -> None:
        try:
            version = self._lire_version(conn)
        except sqlite3.DatabaseError as exc:
            raise ErreurBase(f"fichier illisible comme base SQLite : {exc}") from exc
        self._refuser_version_future(version)  # avant toute ecriture
        conn.execute("PRAGMA foreign_keys=ON")
        if not memoire:
            for essai in range(100):
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError:
                    if essai == 99:
                        raise
                    time.sleep(0.05)
        conn.execute("PRAGMA synchronous=FULL")
        if version == SCHEMA_VERSION:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = self._lire_version(conn)  # un autre processus a pu migrer
            self._refuser_version_future(version)
            if version == 0:
                autres = conn.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                if autres is not None:
                    raise ErreurBase(
                        "base non vide sans version de schema : ce n'est pas une base "
                        "de ce logiciel, refus de la modifier"
                    )
                for instruction in _SCHEMA:
                    conn.execute(instruction)
                conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    # -- transactions ------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Bloc atomique : tout est valide, ou rien (rollback sur toute exception,
        y compris `KeyboardInterrupt`). Imbriquable (SAVEPOINT)."""
        with self._verrou:
            if self._profondeur == 0:
                self._conn.execute("BEGIN IMMEDIATE")
                self._profondeur = 1
                try:
                    yield
                    self._conn.execute("COMMIT")
                except BaseException:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
                finally:
                    self._profondeur = 0
            else:
                nom = f"sp_{self._profondeur}"
                self._conn.execute(f"SAVEPOINT {nom}")
                self._profondeur += 1
                try:
                    yield
                    self._conn.execute(f"RELEASE {nom}")
                except BaseException:
                    self._conn.execute(f"ROLLBACK TO {nom}")
                    self._conn.execute(f"RELEASE {nom}")
                    raise
                finally:
                    self._profondeur -= 1

    @contextmanager
    def _instantane(self) -> Iterator[None]:
        """Lecture coherente multi-tables sans bloquer les ecrivains (WAL)."""
        with self._verrou:
            if self._profondeur > 0:
                yield
                return
            self._conn.execute("BEGIN")
            try:
                yield
            finally:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass

    def fermer(self) -> None:
        with self._verrou:
            if not self._ferme:
                self._ferme = True
                self._conn.close()

    def __enter__(self) -> "Depot":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.fermer()

    # -- pieces ------------------------------------------------------------

    def sauver_pieces(self, pieces: Iterable[PieceAttendue]) -> None:
        """Upsert sur (dossier, reference). Tout ou rien."""
        lignes = [_piece_valeurs(p) for p in pieces]  # valide tout avant d'ecrire
        with self.transaction():
            self._conn.executemany(
                f"""INSERT INTO pieces ({_PIECES_COLONNES})
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (dossier, reference) DO UPDATE SET
                        periode = excluded.periode, montant = excluded.montant,
                        date_operation = excluded.date_operation, libelle = excluded.libelle,
                        etat = excluded.etat, nb_relances = excluded.nb_relances,
                        date_premiere_demande = excluded.date_premiere_demande,
                        date_derniere_relance = excluded.date_derniere_relance,
                        date_promesse = excluded.date_promesse, bloquee = excluded.bloquee,
                        motif_blocage = excluded.motif_blocage,
                        pieces_rattachees = excluded.pieces_rattachees,
                        devise = excluded.devise""",
                lignes,
            )

    def charger_pieces(
        self,
        *,
        dossier: str | None = None,
        periode: str | None = None,
        etats: Collection[EtatPiece] | None = None,
    ) -> list[PieceAttendue]:
        """Tri : (dossier, date_operation, reference). `etats` vide -> aucune piece."""
        conditions: list[str] = []
        params: list[Any] = []
        if dossier is not None:
            conditions.append("dossier = ?")
            params.append(dossier)
        if periode is not None:
            conditions.append("periode = ?")
            params.append(periode)
        if etats is not None:
            valeurs = [EtatPiece(e).value for e in etats]
            if not valeurs:
                return []
            conditions.append("etat IN (" + ", ".join("?" * len(valeurs)) + ")")
            params.extend(valeurs)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        with self._verrou:
            lignes = self._conn.execute(
                f"SELECT {_PIECES_COLONNES} FROM pieces{where} "
                "ORDER BY dossier, date_operation, reference",
                params,
            ).fetchall()
        return [_piece_lue(r) for r in lignes]

    # -- idempotence des messages entrants ---------------------------------

    def marquer_vu(self, cle: str) -> bool:
        """True si la cle est NOUVELLE, False si deja vue. Atomique entre
        connexions (cle primaire + INSERT OR IGNORE)."""
        if not isinstance(cle, str) or not cle:
            raise ValueError("cle d'idempotence : chaine non vide obligatoire")
        with self._verrou:
            curseur = self._conn.execute(
                "INSERT OR IGNORE INTO vus (cle, vu_le) VALUES (?, ?)",
                (cle, _maintenant().isoformat()),
            )
            return curseur.rowcount == 1

    def est_vu(self, cle: str) -> bool:
        with self._verrou:
            return (
                self._conn.execute("SELECT 1 FROM vus WHERE cle = ?", (cle,)).fetchone()
                is not None
            )

    # -- file humaine ------------------------------------------------------

    def mettre_en_file(self, decision: DecisionRoutage) -> None:
        """Idempotent sur (message_id, empreinte) : le premier enregistrement
        gagne, une decision deja resolue n'est jamais rouverte."""
        with self._verrou:
            self._conn.execute(
                """INSERT OR IGNORE INTO file_humaine
                   (message_id, empreinte, nom_fichier, statut, dossier, periode,
                    reference_operation, motif, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.message_id, decision.empreinte, decision.nom_fichier,
                    StatutRoutage(decision.statut).value, decision.dossier, decision.periode,
                    decision.reference_operation,
                    None if decision.motif is None else MotifNonRoute(decision.motif).value,
                    decision.detail,
                ),
            )

    def file_humaine(self, *, resolue: bool | None = False) -> list[DecisionRoutage]:
        """Ordre d'insertion. `resolue=None` : toutes."""
        where, params = "", ()
        if resolue is not None:
            where, params = " WHERE resolue = ?", (1 if resolue else 0,)
        with self._verrou:
            lignes = self._conn.execute(
                f"SELECT * FROM file_humaine{where} ORDER BY id", params
            ).fetchall()
        return [_decision_lue(r) for r in lignes]

    def resoudre_file(
        self,
        message_id: str,
        empreinte: str,
        par: str,
        note: str = "",
        *,
        maintenant: dt.datetime | None = None,
    ) -> None:
        """Marque l'entree resolue (elle quitte `file_humaine()` par defaut).

        `par` non vide (`ValueError`). Entree inconnue : `KeyError`. Deja
        resolue : sans effet, la premiere resolution est conservee (la trace de
        qui a tranche n'est jamais ecrasee).
        """
        if not isinstance(par, str) or not par.strip():
            raise ValueError("resoudre_file : `par` obligatoire et non vide")
        horodatage = _datetime_iso(maintenant if maintenant is not None else _maintenant())
        with self.transaction():
            ligne = self._conn.execute(
                "SELECT resolue FROM file_humaine WHERE message_id = ? AND empreinte = ?",
                (message_id, empreinte),
            ).fetchone()
            if ligne is None:
                raise KeyError(f"file humaine : entree inconnue ({message_id!r}, {empreinte!r})")
            if ligne["resolue"]:
                return
            self._conn.execute(
                """UPDATE file_humaine SET resolue = 1, resolue_par = ?, resolue_note = ?,
                   resolue_le = ? WHERE message_id = ? AND empreinte = ? AND resolue = 0""",
                (par, note, horodatage, message_id, empreinte),
            )

    # -- brouillons --------------------------------------------------------

    def sauver_brouillon(self, b: Brouillon) -> bool:
        """True si cree, False si l'id existe deja (rien n'est modifie, JAMAIS)."""
        valeurs = _brouillon_valeurs(b)
        with self._verrou:
            curseur = self._conn.execute(
                f"""INSERT OR IGNORE INTO brouillons ({_BROUILLONS_COLONNES})
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                valeurs,
            )
            return curseur.rowcount == 1

    def charger_brouillon(self, id_relance: str) -> Brouillon | None:
        with self._verrou:
            ligne = self._conn.execute(
                f"SELECT {_BROUILLONS_COLONNES} FROM brouillons WHERE id_relance = ?",
                (id_relance,),
            ).fetchone()
        return None if ligne is None else _brouillon_lu(ligne)

    def lister_brouillons(self, statut: StatutBrouillon | None = None) -> list[Brouillon]:
        """Tri : (cree_le, id_relance)."""
        where, params = "", ()
        if statut is not None:
            where, params = " WHERE statut = ?", (StatutBrouillon(statut).value,)
        with self._verrou:
            lignes = self._conn.execute(
                f"SELECT {_BROUILLONS_COLONNES} FROM brouillons{where} "
                "ORDER BY cree_le, id_relance",
                params,
            ).fetchall()
        return [_brouillon_lu(r) for r in lignes]

    def maj_brouillon(self, b: Brouillon) -> None:
        """Applique le changement de statut de `b` au brouillon stocke.

        Seules les 7 transitions du contrat passent (`TRANSITIONS_BROUILLON`) ;
        toute autre, y compris statut inchange et toute sortie de ENVOYEE :
        `ValueError`. Brouillon inconnu : `KeyError`.

        Garde-fous AJOUTES au contrat (le texte approuve par un humain ne doit
        pas pouvoir changer apres coup) :
        - destinataire, objet, corps, dossiers, references, niveau et cree_le
          doivent etre identiques a ceux stockes ;
        - un `valide_par` / `valide_le` deja renseigne ne peut pas changer ;
        - passer a VALIDEE, EN_COURS ou ENVOYEE exige `valide_par` non vide.
        Seuls statut, valide_par, valide_le et envoye_le sont ecrits.
        """
        with self.transaction():  # IMMEDIATE : lecture et ecriture sans intercalaire
            ligne = self._conn.execute(
                f"SELECT {_BROUILLONS_COLONNES} FROM brouillons WHERE id_relance = ?",
                (b.id_relance,),
            ).fetchone()
            if ligne is None:
                raise KeyError(f"brouillon inconnu : {b.id_relance!r}")
            actuel = _brouillon_lu(ligne)
            if actuel.statut == StatutBrouillon.ENVOYEE:
                raise ValueError(f"brouillon {b.id_relance} ENVOYEE : immuable")
            cible = StatutBrouillon(b.statut)
            if (actuel.statut, cible) not in TRANSITIONS_BROUILLON:
                raise ValueError(
                    f"transition interdite {actuel.statut.value} -> {cible.value} "
                    f"(brouillon {b.id_relance})"
                )
            for champ in (
                "destinataire", "objet", "corps", "dossiers", "references", "niveau", "cree_le",
            ):
                if getattr(actuel, champ) != getattr(b, champ):
                    raise ValueError(
                        f"maj_brouillon ne peut pas modifier `{champ}` (brouillon {b.id_relance})"
                    )
            if actuel.valide_par and b.valide_par != actuel.valide_par:
                raise ValueError("valide_par deja renseigne : non modifiable")
            if actuel.valide_le is not None and b.valide_le != actuel.valide_le:
                raise ValueError("valide_le deja renseigne : non modifiable")
            if actuel.envoye_le is not None and b.envoye_le != actuel.envoye_le:
                raise ValueError("envoye_le deja renseigne : non modifiable")
            if cible in (_B.VALIDEE, _B.EN_COURS, _B.ENVOYEE) and not b.valide_par.strip():
                raise ValueError(f"passage a {cible.value} sans valide_par")
            curseur = self._conn.execute(
                """UPDATE brouillons SET statut = ?, valide_par = ?, valide_le = ?, envoye_le = ?
                   WHERE id_relance = ? AND statut = ?""",
                (
                    cible.value, b.valide_par, _datetime_iso(b.valide_le),
                    _datetime_iso(b.envoye_le), b.id_relance, actuel.statut.value,
                ),
            )
            if curseur.rowcount != 1:  # ne devrait pas arriver sous IMMEDIATE
                raise ValueError(f"brouillon {b.id_relance} modifie en parallele")

    # -- envois ------------------------------------------------------------

    def enregistrer_envoi(self, e: EnvoiRelance) -> None:
        """Idempotent sur id_relance : le premier enregistrement est conserve.
        Un envoi enregistre est immuable et indestructible (triggers SQL)."""
        with self._verrou:
            self._conn.execute(
                """INSERT OR IGNORE INTO envois
                   (id_relance, destinataire, destinataire_norm, date_envoi, dossiers, refs)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    e.id_relance, e.destinataire, _norm_dest(e.destinataire),
                    _date_iso(e.date_envoi), _json_liste(e.dossiers), _json_liste(e.references),
                ),
            )

    def historique_envois(
        self, *, destinataire: str | None = None, depuis: dt.date | None = None
    ) -> list[EnvoiRelance]:
        """`destinataire` : comparaison sans casse ni espaces de bord (plus de
        correspondances = limite hebdomadaire plus prudente). `depuis` :
        inclusif (date_envoi >= depuis). Tri : (date_envoi, id_relance)."""
        conditions: list[str] = []
        params: list[Any] = []
        if destinataire is not None:
            conditions.append("destinataire_norm = ?")
            params.append(_norm_dest(destinataire))
        if depuis is not None:
            conditions.append("date_envoi >= ?")
            params.append(_date_iso(depuis))
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        with self._verrou:
            lignes = self._conn.execute(
                f"SELECT id_relance, destinataire, date_envoi, dossiers, refs FROM envois{where} "
                "ORDER BY date_envoi, id_relance",
                params,
            ).fetchall()
        return [_envoi_lu(r) for r in lignes]

    # -- export / import ---------------------------------------------------

    def _donnees_export(self) -> dict[str, list[dict[str, Any]]]:
        with self._instantane(), self._verrou:
            c = self._conn
            pieces = [
                {
                    "reference": p.reference, "dossier": p.dossier, "periode": p.periode,
                    "montant": str(p.montant), "date_operation": p.date_operation.isoformat(),
                    "libelle": p.libelle, "etat": p.etat.value, "nb_relances": p.nb_relances,
                    "date_premiere_demande": _date_iso_ou_none(p.date_premiere_demande),
                    "date_derniere_relance": _date_iso_ou_none(p.date_derniere_relance),
                    "date_promesse": _date_iso_ou_none(p.date_promesse),
                    "bloquee": p.bloquee, "motif_blocage": p.motif_blocage,
                    "pieces_rattachees": list(p.pieces_rattachees), "devise": p.devise,
                }
                for p in (
                    _piece_lue(r)
                    for r in c.execute(
                        f"SELECT {_PIECES_COLONNES} FROM pieces ORDER BY dossier, reference"
                    )
                )
            ]
            vus = [
                {"cle": r["cle"], "vu_le": r["vu_le"]}
                for r in c.execute("SELECT cle, vu_le FROM vus ORDER BY cle")
            ]
            file_h = [
                {
                    "message_id": r["message_id"], "empreinte": r["empreinte"],
                    "nom_fichier": r["nom_fichier"], "statut": r["statut"],
                    "dossier": r["dossier"], "periode": r["periode"],
                    "reference_operation": r["reference_operation"], "motif": r["motif"],
                    "detail": r["detail"], "resolue": bool(r["resolue"]),
                    "resolue_par": r["resolue_par"], "resolue_note": r["resolue_note"],
                    "resolue_le": r["resolue_le"],
                }
                for r in c.execute("SELECT * FROM file_humaine ORDER BY id")
            ]
            brouillons = [
                {
                    "id_relance": b.id_relance, "destinataire": b.destinataire,
                    "objet": b.objet, "corps": b.corps, "dossiers": list(b.dossiers),
                    "references": list(b.references), "niveau": b.niveau,
                    "cree_le": b.cree_le.isoformat(), "statut": b.statut.value,
                    "valide_par": b.valide_par, "valide_le": _datetime_iso(b.valide_le),
                    "envoye_le": _datetime_iso(b.envoye_le),
                }
                for b in (
                    _brouillon_lu(r)
                    for r in c.execute(
                        f"SELECT {_BROUILLONS_COLONNES} FROM brouillons ORDER BY id_relance"
                    )
                )
            ]
            envois = [
                {
                    "id_relance": e.id_relance, "destinataire": e.destinataire,
                    "date_envoi": e.date_envoi.isoformat(), "dossiers": list(e.dossiers),
                    "references": list(e.references),
                }
                for e in (
                    _envoi_lu(r)
                    for r in c.execute(
                        "SELECT id_relance, destinataire, date_envoi, dossiers, refs "
                        "FROM envois ORDER BY id_relance"
                    )
                )
            ]
        return {
            "pieces": pieces, "vus": vus, "file_humaine": file_h,
            "brouillons": brouillons, "envois": envois,
        }

    def exporter_json(self, chemin: Path | str) -> Path:
        """Ecrit tout le depot en JSON (UTF-8, cles triees, indentation fixe).

        Deux exports de la meme base sont identiques octet pour octet. Le schema
        est documente dans la cle `schema`. Ecriture atomique (fichier temporaire
        + fsync + remplacement) : un export interrompu ne laisse pas de fichier
        a moitie ecrit a la place d'un export precedent.
        """
        document = {"schema": _SCHEMA_EXPORT, "donnees": self._donnees_export()}
        texte = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        cible = Path(chemin)
        cible.parent.mkdir(parents=True, exist_ok=True)
        temporaire = cible.with_name(cible.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with open(temporaire, "wb") as f:
                f.write(texte.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporaire, cible)
        finally:
            if temporaire.exists():
                temporaire.unlink()
        return cible

    def importer_json(self, chemin: Path | str) -> None:
        """Recharge un export dans un depot VIDE (`ValueError` sinon, ou si le
        fichier n'est pas un export de cette version). Tout ou rien."""
        document = json.loads(Path(chemin).read_text(encoding="utf-8"))
        schema = document.get("schema", {})
        if schema.get("format") != _SCHEMA_EXPORT["format"] or schema.get("version") != SCHEMA_VERSION:
            raise ValueError("fichier d'export inconnu ou de version differente")
        donnees = document["donnees"]
        with self.transaction():
            for table in ("pieces", "vus", "file_humaine", "brouillons", "envois"):
                if self._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                    raise ValueError("importer_json exige un depot vide")
            self.sauver_pieces(
                PieceAttendue(
                    reference=d["reference"], dossier=d["dossier"], periode=d["periode"],
                    montant=Decimal(d["montant"]),
                    date_operation=dt.date.fromisoformat(d["date_operation"]),
                    libelle=d["libelle"], etat=EtatPiece(d["etat"]),
                    nb_relances=d["nb_relances"],
                    date_premiere_demande=_date_lue(d["date_premiere_demande"]),
                    date_derniere_relance=_date_lue(d["date_derniere_relance"]),
                    date_promesse=_date_lue(d["date_promesse"]),
                    bloquee=d["bloquee"], motif_blocage=d["motif_blocage"],
                    pieces_rattachees=tuple(d["pieces_rattachees"]),
                    devise=d["devise"],
                )
                for d in donnees["pieces"]
            )
            for d in donnees["vus"]:
                self._conn.execute(
                    "INSERT INTO vus (cle, vu_le) VALUES (?, ?)", (d["cle"], d["vu_le"])
                )
            for d in donnees["file_humaine"]:
                self.mettre_en_file(
                    DecisionRoutage(
                        message_id=d["message_id"], nom_fichier=d["nom_fichier"],
                        empreinte=d["empreinte"], statut=StatutRoutage(d["statut"]),
                        dossier=d["dossier"], periode=d["periode"],
                        reference_operation=d["reference_operation"],
                        motif=None if d["motif"] is None else MotifNonRoute(d["motif"]),
                        detail=d["detail"],
                    )
                )
                self._conn.execute(
                    """UPDATE file_humaine SET resolue = ?, resolue_par = ?, resolue_note = ?,
                       resolue_le = ? WHERE message_id = ? AND empreinte = ?""",
                    (
                        1 if d["resolue"] else 0, d["resolue_par"], d["resolue_note"],
                        d["resolue_le"], d["message_id"], d["empreinte"],
                    ),
                )
            for d in donnees["brouillons"]:
                self.sauver_brouillon(
                    Brouillon(
                        id_relance=d["id_relance"], destinataire=d["destinataire"],
                        objet=d["objet"], corps=d["corps"], dossiers=tuple(d["dossiers"]),
                        references=tuple(d["references"]), niveau=d["niveau"],
                        cree_le=dt.date.fromisoformat(d["cree_le"]),
                        statut=StatutBrouillon(d["statut"]), valide_par=d["valide_par"],
                        valide_le=_datetime_lu(d["valide_le"]),
                        envoye_le=_datetime_lu(d["envoye_le"]),
                    )
                )
            for d in donnees["envois"]:
                self.enregistrer_envoi(
                    EnvoiRelance(
                        id_relance=d["id_relance"], destinataire=d["destinataire"],
                        date_envoi=dt.date.fromisoformat(d["date_envoi"]),
                        dossiers=tuple(d["dossiers"]), references=tuple(d["references"]),
                    )
                )
