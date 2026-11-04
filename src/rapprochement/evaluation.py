"""Mesure du rapprochement contre une verite terrain.

Les deux seuls chiffres qui comptent commercialement :

  - fausses alertes : une operation signalee "manquante" alors qu'elle ne l'est
    pas. C'est ce qui fait abandonner l'outil. Objectif : < 2 %.
  - manques rates   : une piece reellement manquante non signalee. C'est ce qui
    fait rater une declaration. Objectif : 0 %.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .modeles import Rapprochement, Statut

CIBLE_FAUSSES_ALERTES = 0.02


def action(statut: Statut) -> str:
    """Le comptable ne distingue pas "justifie" de "a verifier" : dans les deux
    cas il ne relance pas le client. On evalue donc sur l'action, pas sur
    l'etiquette."""
    if statut is Statut.MANQUANT:
        return "relancer"
    if statut is Statut.HORS_PERIMETRE:
        return "ignorer"
    return "traiter"


ACTION_ATTENDUE = {
    "manquant": "relancer",
    "hors_perimetre": "ignorer",
    "justifie": "traiter",
    "partiel": "traiter",
}


def lire_verite(chemin: Path | str) -> dict[str, dict[str, str]]:
    """verite_terrain.csv : reference,statut[,id_piece_rattachee,motif]."""
    with open(chemin, newline="", encoding="utf-8-sig") as f:
        return {ligne["reference"].strip(): ligne for ligne in csv.DictReader(f)}


@dataclass
class Erreur:
    rapprochement: Rapprochement
    attendu: str


@dataclass
class Evaluation:
    total: int = 0
    justes: int = 0
    fausses_alertes: int = 0
    non_relancer: int = 0
    manques_rates: int = 0
    a_relancer: int = 0
    # statut attendu -> (decisions justes, total)
    par_statut: dict[str, tuple[int, int]] = field(default_factory=dict)
    erreurs: list[Erreur] = field(default_factory=list)

    @property
    def taux_correct(self) -> float:
        return self.justes / self.total if self.total else 0.0

    @property
    def taux_fausses_alertes(self) -> float:
        return self.fausses_alertes / max(self.non_relancer, 1)

    @property
    def taux_manques_rates(self) -> float:
        return self.manques_rates / max(self.a_relancer, 1)

    @property
    def cibles_atteintes(self) -> bool:
        return (
            self.taux_fausses_alertes < CIBLE_FAUSSES_ALERTES
            and self.manques_rates == 0
        )


def evaluer(
    rapprochements: list[Rapprochement],
    verite: dict[str, dict[str, str]],
) -> Evaluation:
    matrice: Counter[tuple[str, str]] = Counter()
    par_statut_attendu: Counter[str] = Counter()
    justes_par_statut: Counter[str] = Counter()
    erreurs: list[Erreur] = []

    for r in rapprochements:
        attendu_brut = verite.get(r.operation.reference, {}).get("statut")
        if attendu_brut not in ACTION_ATTENDUE:
            continue
        attendu = ACTION_ATTENDUE[attendu_brut]
        obtenu = action(r.statut)
        matrice[(attendu, obtenu)] += 1
        par_statut_attendu[attendu_brut] += 1
        if attendu == obtenu:
            justes_par_statut[attendu_brut] += 1
        else:
            erreurs.append(Erreur(r, attendu_brut))

    total = sum(matrice.values())
    a_relancer = sum(v for (a, _), v in matrice.items() if a == "relancer")
    return Evaluation(
        total=total,
        justes=sum(v for (a, o), v in matrice.items() if a == o),
        fausses_alertes=matrice[("ignorer", "relancer")] + matrice[("traiter", "relancer")],
        non_relancer=total - a_relancer,
        manques_rates=sum(
            v for (a, o), v in matrice.items() if a == "relancer" and o != "relancer"
        ),
        a_relancer=a_relancer,
        par_statut={
            s: (justes_par_statut[s], n) for s, n in sorted(par_statut_attendu.items())
        },
        erreurs=erreurs,
    )
