#!/usr/bin/env python3
"""Construit la version en ligne de l'interface : un seul fichier HTML.

Le fichier embarque le CSS, le JavaScript, les polices (en data: URI) et
l'analyse du jeu de demonstration, calculee par le vrai moteur au moment de la
construction. Il se publie tel quel comme page statique (par exemple un
Artifact claude.ai) : aucun serveur Python, aucune ressource externe.

C'est une vitrine en lecture seule. L'import de fichiers, les regles apprises
et les exports restent dans l'application installee (Demarrer_interface.bat).

    python3 scripts/construire_demo_en_ligne.py [--sortie chemin.html]
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import tempfile
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))

from rapprochement.interface.serveur import CORPUS, STATIQUE, Espace  # noqa: E402

NOTE_LOCALE = "Traitement 100 % local : aucune donnée ne quitte ce poste."
NOTE_EN_LIGNE = "Données de démonstration fictives (3 PME), calculées par le moteur réel."


def construire(sortie: Path, corpus: Path = CORPUS) -> Path:
    with tempfile.TemporaryDirectory() as dossier:
        analyse = Espace(Path(dossier) / "espace", corpus=corpus).analyse()

    def police(m: re.Match) -> str:
        donnees = base64.b64encode((STATIQUE / m.group(1)).read_bytes()).decode("ascii")
        return f'url("data:font/woff2;base64,{donnees}")'

    css = re.sub(r'url\("(polices/[^"]+\.woff2)"\)', police, (STATIQUE / "app.css").read_text(encoding="utf-8"))
    html = (STATIQUE / "index.html").read_text(encoding="utf-8")
    js = (STATIQUE / "app.js").read_text(encoding="utf-8")
    if "</script" in js.lower():
        raise ValueError("app.js contient « </script » : il ne peut pas etre embarque tel quel.")

    titre = re.search(r"<title>.*?</title>", html, re.S).group(0)
    script_theme = re.search(r"<script>\s*/\* Theme.*?</script>", html, re.S).group(0)
    corps = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    corps = corps.replace('<script src="/static/app.js"></script>', "").replace(NOTE_LOCALE, NOTE_EN_LIGNE)
    donnees = json.dumps(analyse, ensure_ascii=False).replace("</", "<\\/")

    # Pas de <html>, <head> ni <body> : l'hebergeur les ajoute a la publication.
    page = "\n".join([
        titre,
        '<meta name="description" content="Démonstration en lecture seule : rapprochement bancaire '
        'et relance des pièces justificatives manquantes">',
        script_theme,
        "<style>",
        css,
        "</style>",
        corps.strip(),
        f"<script>window.ANALYSE_EMBARQUEE = {donnees};</script>",
        "<script>",
        js,
        "</script>",
        "",
    ])
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(page, encoding="utf-8")
    return sortie


def main() -> int:
    parseur = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parseur.add_argument("--sortie", default=str(RACINE / "data" / "sorties" / "demo_en_ligne.html"))
    args = parseur.parse_args()
    chemin = construire(Path(args.sortie))
    print(f"Version en ligne : {chemin} ({chemin.stat().st_size // 1024} Ko)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
