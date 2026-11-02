"""Interface web locale du MVP (WSGI, bibliotheque standard uniquement).

Voir docs/application_web.md. Aucune logique metier ici : chaque decision appelle
la meme fonction de `rapprochement.cycle` que la ligne de commande.
"""

from __future__ import annotations

from .. import cycle as _cycle

# Sous Windows sans le paquet tzdata, `ZoneInfo("Europe/Paris")` echoue : si `cycle`
# fournit son secours (ajoute en parallele par l'expert Windows), on l'active AVANT
# d'importer les modules qui construisent ce fuseau. Sinon : `pip install tzdata`.
_garantir_fuseaux = getattr(_cycle, "garantir_fuseaux", None)
if callable(_garantir_fuseaux):
    _garantir_fuseaux()

from .application import Application, creer_application  # noqa: E402

__all__ = ["Application", "creer_application"]
