"""Sorties : tableau de suivi CSV (destine a Google Sheets) et resume console."""

from __future__ import annotations

import csv
from collections import Counter
from decimal import Decimal
from pathlib import Path

from .modeles import Rapprochement, Statut

ENTETES_SUIVI = (
    "dossier",
    "reference",
    "date_operation",
    "libelle",
    "montant",
    "devise",
    "statut",
    "confiance",
    "pieces_rattachees",
    "fournisseur",
    "motif",
    "regle",
)


def ecrire_suivi(rapprochements: list[Rapprochement], chemin: Path | str) -> Path:
    """Tableau de suivi a importer dans Google Sheets."""
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with open(chemin, "w", newline="", encoding="utf-8") as f:
        ecrivain = csv.writer(f)
        ecrivain.writerow(ENTETES_SUIVI)
        for r in sorted(
            rapprochements,
            key=lambda x: (x.operation.dossier, x.operation.date_operation),
        ):
            ecrivain.writerow(
                [
                    r.operation.dossier,
                    r.operation.reference,
                    r.operation.date_operation.isoformat(),
                    r.operation.libelle,
                    f"{r.operation.montant:.2f}",
                    r.operation.devise,
                    r.statut.value,
                    f"{r.confiance:.2f}",
                    " + ".join(p.id_piece for p in r.pieces),
                    " + ".join(p.fournisseur for p in r.pieces),
                    r.motif,
                    r.regle,
                ]
            )
    return chemin


def statistiques(rapprochements: list[Rapprochement]) -> dict[str, object]:
    compte = Counter(r.statut for r in rapprochements)
    manquants = [r for r in rapprochements if r.statut is Statut.MANQUANT]
    a_traiter = compte[Statut.MANQUANT] + compte[Statut.A_VERIFIER] + compte[Statut.PARTIEL]
    total = len(rapprochements) or 1
    return {
        "operations": len(rapprochements),
        "hors_perimetre": compte[Statut.HORS_PERIMETRE],
        "justifie": compte[Statut.JUSTIFIE],
        "manquant": compte[Statut.MANQUANT],
        "partiel": compte[Statut.PARTIEL],
        "a_verifier": compte[Statut.A_VERIFIER],
        "montant_manquant": sum((r.operation.montant for r in manquants), Decimal("0")),
        # Le chiffre qui se vend : la part du releve que le comptable
        # n'a plus a regarder.
        "taux_automatisation": round(
            100 * (compte[Statut.HORS_PERIMETRE] + compte[Statut.JUSTIFIE]) / total, 1
        ),
        "lignes_a_traiter": a_traiter,
    }


def resume(rapprochements: list[Rapprochement]) -> str:
    s = statistiques(rapprochements)
    return "\n".join(
        [
            f"Operations analysees        : {s['operations']}",
            f"  hors perimetre (sans piece): {s['hors_perimetre']}",
            f"  justifiees                 : {s['justifie']}",
            f"  a verifier                 : {s['a_verifier']}",
            f"  paiements partiels         : {s['partiel']}",
            f"  PIECES MANQUANTES          : {s['manquant']}"
            f"  ({s['montant_manquant']:.2f} EUR)",
            "",
            f"Traite sans intervention    : {s['taux_automatisation']} %",
            f"Lignes restant au comptable : {s['lignes_a_traiter']}",
        ]
    )
