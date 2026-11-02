"""Gabarits HTML en chaines de caracteres, avec echappement SYSTEMATIQUE.

Regle unique : tout texte insere dans une page passe par `echapper`. Le seul
moyen d'inserer une valeur dans un gabarit est `gabarit(modele, **valeurs)`,
qui echappe chaque valeur (sauf un fragment `Html` deja produit par ces
fonctions). Le modele est TOUJOURS une constante du code : une valeur venue de
l'exterieur (libelle bancaire, nom de piece jointe, Message-ID, motif...) n'est
jamais interpretee comme modele, donc `{{7*7}}` reste du texte.

`echapper` rend aussi visibles les caracteres de controle et de mise en forme
invisibles (U+202E, ESC...) via `cycle.affichable`, ligne par ligne : un libelle
ne peut ni injecter de balise, ni retourner visuellement un texte affiche.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from typing import Any

from ..cycle import affichable


class Html(str):
    """Fragment HTML sur : produit uniquement par `echapper`, `gabarit` ou `joindre`."""

    __slots__ = ()


def echapper(valeur: Any) -> Html:
    """LA fonction d'echappement. `None` devient une chaine vide."""
    if isinstance(valeur, Html):
        return valeur
    texte = "" if valeur is None else str(valeur)
    visible = "\n".join(affichable(ligne) for ligne in texte.split("\n"))
    return Html(html.escape(visible, quote=True))


def gabarit(modele: str, **valeurs: Any) -> Html:
    """Remplit `modele` (constante du code, champs `{nom}`) avec des valeurs echappees."""
    return Html(modele.format(**{cle: echapper(v) for cle, v in valeurs.items()}))


def joindre(morceaux: Iterable[Any], separateur: str = "") -> Html:
    """Concatene des fragments ; tout morceau qui n'est pas `Html` est echappe."""
    return Html(echapper(separateur).join(echapper(m) for m in morceaux))


__all__ = ["Html", "echapper", "gabarit", "joindre"]
