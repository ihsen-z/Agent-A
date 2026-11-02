"""Sessions cote serveur et limitation des tentatives de connexion.

Aucune horloge cachee : `horloge()` (datetime avec fuseau) est injectee, ce qui
permet aux tests de controler l'expiration.

- Jeton de session : 32 octets de `secrets`, transmis seulement dans le cookie ;
  le magasin n'en garde que l'empreinte SHA-256 (une fuite memoire du magasin ne
  donne pas de jeton utilisable).
- Expiration par inactivite (30 min) ET absolue (8 h).
- Jeton CSRF propre a chaque session.
- Un nouveau jeton a chaque connexion : un jeton fourni par le client n'est jamais
  adopte (pas de fixation de session).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

Horloge = Callable[[], dt.datetime]

INACTIVITE = dt.timedelta(minutes=30)
DUREE_ABSOLUE = dt.timedelta(hours=8)
FENETRE_ECHECS = dt.timedelta(minutes=10)
SEUIL_ECHECS = 5
DUREE_BLOCAGE = dt.timedelta(minutes=10)
MAX_SESSIONS = 10_000
MAX_CLES_SUIVIES = 10_000


def _empreinte(jeton: str) -> str:
    return hashlib.sha256(jeton.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass
class Session:
    identifiant: str
    cree_le: dt.datetime
    vu_le: dt.datetime
    csrf: str
    empreinte_compte: str = ""      # change avec le mot de passe : la session meurt alors


class MagasinSessions:
    def __init__(self, horloge: Horloge, *, inactivite: dt.timedelta = INACTIVITE,
                 duree_absolue: dt.timedelta = DUREE_ABSOLUE) -> None:
        self._horloge = horloge
        self._inactivite = inactivite
        self._absolue = duree_absolue
        self._sessions: dict[str, Session] = {}
        self._verrou = threading.Lock()

    def _expiree(self, s: Session, maintenant: dt.datetime) -> bool:
        return maintenant - s.vu_le >= self._inactivite or maintenant - s.cree_le >= self._absolue

    def _purger(self, maintenant: dt.datetime) -> None:
        for cle in [c for c, s in self._sessions.items() if self._expiree(s, maintenant)]:
            del self._sessions[cle]

    def creer(self, identifiant: str, empreinte_compte: str = "") -> tuple[str, Session]:
        """Nouvelle session, NOUVEAU jeton. Renvoie (jeton a placer dans le cookie, session)."""
        maintenant = self._horloge()
        jeton = secrets.token_urlsafe(32)
        session = Session(identifiant, maintenant, maintenant, secrets.token_urlsafe(32), empreinte_compte)
        with self._verrou:
            self._purger(maintenant)
            if len(self._sessions) >= MAX_SESSIONS:
                plus_ancienne = min(self._sessions, key=lambda c: self._sessions[c].vu_le)
                del self._sessions[plus_ancienne]
            self._sessions[_empreinte(jeton)] = session
        return jeton, session

    def obtenir(self, jeton: str | None) -> Session | None:
        """Session valide pour `jeton` (et son activite est rafraichie), sinon None."""
        if not jeton:
            return None
        maintenant = self._horloge()
        cle = _empreinte(jeton)
        with self._verrou:
            s = self._sessions.get(cle)
            if s is None:
                return None
            if self._expiree(s, maintenant):
                del self._sessions[cle]
                return None
            s.vu_le = maintenant
            return s

    def detruire(self, jeton: str | None) -> None:
        if jeton:
            with self._verrou:
                self._sessions.pop(_empreinte(jeton), None)

    def detruire_utilisateur(self, identifiant: str) -> None:
        with self._verrou:
            for cle in [c for c, s in self._sessions.items() if s.identifiant == identifiant]:
                del self._sessions[cle]

    def __len__(self) -> int:
        with self._verrou:
            return len(self._sessions)


class LimiteurConnexions:
    """Blocage de `DUREE_BLOCAGE` apres `SEUIL_ECHECS` echecs en `FENETRE_ECHECS`, par cle
    (l'appelant passe une cle par identifiant ET une par adresse)."""

    def __init__(self, horloge: Horloge) -> None:
        self._horloge = horloge
        self._echecs: dict[str, list[dt.datetime]] = {}
        self._bloque_jusqu_a: dict[str, dt.datetime] = {}
        self._verrou = threading.Lock()

    def _nettoyer(self, maintenant: dt.datetime) -> None:
        for cle in [c for c, fin in self._bloque_jusqu_a.items() if fin <= maintenant]:
            del self._bloque_jusqu_a[cle]
        for cle in list(self._echecs):
            recents = [t for t in self._echecs[cle] if maintenant - t < FENETRE_ECHECS]
            if recents:
                self._echecs[cle] = recents
            else:
                del self._echecs[cle]

    def bloque(self, cles: Iterable[str]) -> bool:
        maintenant = self._horloge()
        with self._verrou:
            self._nettoyer(maintenant)
            return any(c in self._bloque_jusqu_a for c in cles)

    def echec(self, cles: Iterable[str]) -> None:
        maintenant = self._horloge()
        with self._verrou:
            self._nettoyer(maintenant)
            for c in cles:
                if c not in self._echecs and len(self._echecs) >= MAX_CLES_SUIVIES:
                    continue                      # memoire bornee : au pire, on ne compte plus
                liste = self._echecs.setdefault(c, [])
                liste.append(maintenant)
                if len(liste) >= SEUIL_ECHECS:
                    self._bloque_jusqu_a[c] = maintenant + DUREE_BLOCAGE
                    del self._echecs[c]

    def succes(self, cle: str) -> None:
        with self._verrou:
            self._echecs.pop(cle, None)


__all__ = ["LimiteurConnexions", "MagasinSessions", "Session"]
