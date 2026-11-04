#!/usr/bin/env python3
"""Lance l'interface graphique dans le navigateur.

    python scripts/interface.py

Sous Windows, un double-clic sur Demarrer_interface.bat fait la meme chose.
Par defaut l'interface repart des donnees de demonstration a chaque
lancement (les regles apprises pendant une demo precedente sont oubliees) ;
--conserver reprend l'espace de travail tel qu'il a ete laisse.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement.interface.serveur import Espace, creer_serveur  # noqa: E402


def main() -> int:
    parseur = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parseur.add_argument("--port", type=int, default=8765)
    parseur.add_argument("--hote", default="127.0.0.1",
                         help="adresse d'ecoute (defaut : ce poste uniquement)")
    parseur.add_argument("--sans-navigateur", action="store_true",
                         help="ne pas ouvrir le navigateur automatiquement")
    parseur.add_argument("--conserver", action="store_true",
                         help="reprendre l'espace de travail precedent au lieu de la demo")
    args = parseur.parse_args()

    if sys.version_info < (3, 11):
        print("Python 3.11 ou plus récent est nécessaire.", file=sys.stderr)
        return 1

    espace = Espace()
    if not args.conserver:
        espace.charger_demo()

    serveur = creer_serveur(args.hote, args.port, espace)
    url = f"http://{serveur.server_address[0]}:{serveur.server_address[1]}/"
    print()
    print("  Interface de rapprochement démarrée")
    print(f"  Adresse : {url}")
    print("  Pour arrêter : fermer cette fenêtre ou Ctrl+C")
    print()
    if not args.sans_navigateur:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        serveur.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
