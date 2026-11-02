"""Configuration commune des tests.

Sous Windows, un cycle HORS mode test est refuse par construction (`IsolationIndisponible` :
la lecture isolee des e-mails exige `fork` et `resource`, Linux ou macOS). La grande majorite
des tests exerce pourtant la logique metier hors mode test ; sans adaptation ils echoueraient
tous sur Windows sans rien dire de ce qu'on veut verifier la-bas (chemins, CRLF, verrous,
SQLite, console).

La fixture ci-dessous leur donne donc, sous Windows seulement, la strategie « directe » a la
place du refus. Les tests qui verifient precisement le refus ou le choix de strategie portent
le marqueur `isolation_reelle` et gardent le vrai comportement.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "isolation_reelle: garde la vraie politique d'isolation, meme sous Windows")


@pytest.fixture(autouse=True)
def _isolation_directe_sous_windows(request, monkeypatch):
    if sys.platform != "win32" or request.node.get_closest_marker("isolation_reelle"):
        return
    from rapprochement import cycle

    reel = cycle.choisir_strategie_isolation

    def choisir(mode_test: bool = False, demandee: str | None = None) -> str:
        if demandee is None and not mode_test:
            return "directe"
        return reel(mode_test, demandee)

    monkeypatch.setattr(cycle, "choisir_strategie_isolation", choisir)
