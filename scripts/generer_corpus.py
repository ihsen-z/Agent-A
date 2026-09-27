#!/usr/bin/env python3
"""Genere un corpus synthetique realiste : releve bancaire, pieces recues,
referentiel dossiers, et la verite terrain.

Objectif : pouvoir construire et mesurer le systeme AVANT d'avoir acces aux
donnees d'un cabinet. La composition reproduit celle d'un releve de PME
francaise, ou la majorite des lignes n'exige aucune facture fournisseur.

La verite terrain (verite_terrain.csv) est ce qui transforme une demo en
produit : elle permet d'annoncer un taux d'exactitude mesure.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import random
from decimal import Decimal
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]

DOSSIERS = [
    ("MARTIN-BTP", "Martin Batiment SARL", "compta@martin-batiment.fr", "M. Martin", 15, "direct"),
    ("LOPEZ-CONSEIL", "Lopez Conseil SAS", "s.lopez@lopez-conseil.fr", "Sophie Lopez", 24, "courtois"),
    ("ATELIER-9", "Atelier 9 SASU", "contact@atelier9.fr", "", 15, "courtois"),
]

# Fournisseurs qui emettent une facture : le libelle bancaire est bruite comme
# dans la vraie vie (prefixe de canal, date collee, identifiant de mandat).
FOURNISSEURS = [
    ("Orange France", "PRLV SEPA ORANGE FRANCE ID{id}", (39, 189)),
    ("EDF Entreprises", "PRLV SEPA EDF ENTREPRISES {date} ID{id}", (95, 640)),
    ("Amazon EU Sarl", "CARTE {date} AMZN MKTPLACE FR", (12, 480)),
    ("Total Energies", "CARTE {date} TOTAL RELAIS 4471", (45, 130)),
    ("Leroy Merlin", "CARTE {date} LEROY MERLIN LILLE", (60, 890)),
    ("OVH SAS", "PRLV SEPA OVH ID{id}", (14, 96)),
    ("Sodexo Pass France", "PRLV SEPA SODEXO PASS ID{id}", (180, 420)),
    ("Point P Materiaux", "VIR SEPA POINT P MATERIAUX {date}", (340, 4200)),
    ("Bureau Vallee", "CARTE {date} BUREAU VALLEE", (18, 215)),
    ("Assurance AXA Pro", "PRLV SEPA AXA FRANCE IARD ID{id}", (110, 380)),
    ("Loxam Location", "VIR SEPA LOXAM {date}", (150, 1800)),
    ("Societe Generale Credit-Bail", "PRLV SEPA SG CREDIT BAIL ID{id}", (390, 720)),
]

# Operations SANS facture fournisseur : c'est la majorite d'un releve reel.
HORS_PERIMETRE = [
    ("VIR SALAIRE {mois} DUPONT J", (1450, 2900), "salaires"),
    ("VIR SALAIRE {mois} BERNARD L", (1380, 2400), "salaires"),
    ("VIR VIREMENT DE SALAIRE {mois}", (1500, 3100), "salaires"),
    ("PRLV SEPA URSSAF IDF ID{id}", (890, 4100), "charges_sociales"),
    ("PRLV SEPA MALAKOFF HUMANIS ID{id}", (210, 680), "charges_sociales"),
    ("PRLV SEPA PRO BTP RETRAITE ID{id}", (340, 910), "charges_sociales"),
    ("PRLV SEPA DGFIP TVA 3310 ID{id}", (420, 5200), "impots_taxes"),
    ("VIR DGFIP ACOMPTE IS {date}", (700, 3400), "impots_taxes"),
    ("PRLV SEPA DGFIP CFE ID{id}", (310, 1250), "impots_taxes"),
    ("COTISATION CARTE AFFAIRES", (6, 14), "frais_bancaires"),
    ("FRAIS DE TENUE DE COMPTE", (12, 32), "frais_bancaires"),
    ("COMMISSION D INTERVENTION", (8, 20), "frais_bancaires"),
    ("ECHEANCE PRET N 4471029 CAPITAL+INT", (620, 1900), "emprunts"),
    ("VIREMENT INTERNE VERS COMPTE EPARGNE", (500, 5000), "mouvements_internes"),
    ("VIR COMPTE COURANT D ASSOCIE", (1000, 8000), "mouvements_internes"),
]

ENCAISSEMENTS = [
    ("VIR RECU CLIENT {ref}", (800, 12000)),
    ("REMISE DE CHEQUES {ref}", (400, 6500)),
    ("VIR SEPA RECU STRIPE PAYMENTS", (120, 3100)),
]

MOIS_FR = ("JANV", "FEVR", "MARS", "AVRIL", "MAI", "JUIN",
           "JUIL", "AOUT", "SEPT", "OCT", "NOV", "DEC")


def montant(bornes: tuple[int, int], alea: random.Random) -> Decimal:
    bas, haut = bornes
    centimes = alea.randint(bas * 100, haut * 100)
    return (Decimal(centimes) / Decimal(100)).quantize(Decimal("0.01"))


def generer(
    debut: dt.date,
    mois: int,
    taux_manquant: float,
    graine: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    alea = random.Random(graine)
    operations: list[dict] = []
    pieces: list[dict] = []
    verite: list[dict] = []
    compteur_piece = 1
    compteur_op = 1

    for code, *_ in DOSSIERS:
        for decalage_mois in range(mois):
            mois_courant = (debut.month - 1 + decalage_mois) % 12 + 1
            annee = debut.year + (debut.month - 1 + decalage_mois) // 12

            # 1. Achats fournisseurs (avec ou sans piece recue)
            for _ in range(alea.randint(9, 16)):
                fournisseur, gabarit, bornes = alea.choice(FOURNISSEURS)
                jour_facture = alea.randint(1, 26)
                date_facture = dt.date(annee, mois_courant, jour_facture)
                date_paiement = date_facture + dt.timedelta(days=alea.randint(0, 40))
                somme = montant(bornes, alea)
                reference = f"OP{compteur_op:06d}"
                compteur_op += 1

                operations.append(
                    {
                        "dossier": code,
                        "date_operation": date_paiement.isoformat(),
                        "date_valeur": date_paiement.isoformat(),
                        "libelle": gabarit.format(
                            date=date_facture.strftime("%d/%m"),
                            id=alea.randint(100000, 999999),
                        ),
                        "debit": f"{somme:.2f}",
                        "credit": "",
                        "devise": "EUR",
                        "reference": reference,
                    }
                )

                piece_recue = alea.random() > taux_manquant
                if piece_recue:
                    id_piece = f"P-{compteur_piece:05d}"
                    compteur_piece += 1
                    pieces.append(
                        {
                            "id_piece": id_piece,
                            "dossier": code,
                            "fichier": f"{date_facture:%Y-%m}-{fournisseur.lower().replace(' ', '-')}.pdf",
                            "fournisseur": fournisseur,
                            "date_facture": date_facture.isoformat(),
                            "montant_ttc": f"{somme:.2f}",
                            "devise": "EUR",
                            "date_reception": (
                                date_facture + dt.timedelta(days=alea.randint(1, 20))
                            ).isoformat(),
                            "canal": alea.choice(["email", "drive", "whatsapp"]),
                        }
                    )
                    verite.append(
                        {
                            "reference": reference,
                            "statut": "justifie",
                            "id_piece_rattachee": id_piece,
                            "motif": "",
                        }
                    )
                else:
                    verite.append(
                        {
                            "reference": reference,
                            "statut": "manquant",
                            "id_piece_rattachee": "",
                            "motif": "piece jamais transmise par le client",
                        }
                    )

            # 2. Operations hors perimetre : la majorite du releve
            for _ in range(alea.randint(11, 18)):
                gabarit, bornes, categorie = alea.choice(HORS_PERIMETRE)
                jour = alea.randint(1, 28)
                date_operation = dt.date(annee, mois_courant, jour)
                reference = f"OP{compteur_op:06d}"
                compteur_op += 1
                operations.append(
                    {
                        "dossier": code,
                        "date_operation": date_operation.isoformat(),
                        "date_valeur": date_operation.isoformat(),
                        "libelle": gabarit.format(
                            date=date_operation.strftime("%d/%m"),
                            mois=MOIS_FR[mois_courant - 1],
                            id=alea.randint(100000, 999999),
                        ),
                        "debit": f"{montant(bornes, alea):.2f}",
                        "credit": "",
                        "devise": "EUR",
                        "reference": reference,
                    }
                )
                verite.append(
                    {
                        "reference": reference,
                        "statut": "hors_perimetre",
                        "id_piece_rattachee": "",
                        "motif": categorie,
                    }
                )

            # 3. Encaissements clients
            for _ in range(alea.randint(3, 7)):
                gabarit, bornes = alea.choice(ENCAISSEMENTS)
                jour = alea.randint(1, 28)
                date_operation = dt.date(annee, mois_courant, jour)
                reference = f"OP{compteur_op:06d}"
                compteur_op += 1
                operations.append(
                    {
                        "dossier": code,
                        "date_operation": date_operation.isoformat(),
                        "date_valeur": date_operation.isoformat(),
                        "libelle": gabarit.format(ref=alea.randint(1000, 9999)),
                        "debit": "",
                        "credit": f"{montant(bornes, alea):.2f}",
                        "devise": "EUR",
                        "reference": reference,
                    }
                )
                verite.append(
                    {
                        "reference": reference,
                        "statut": "hors_perimetre",
                        "id_piece_rattachee": "",
                        "motif": "encaissements_clients",
                    }
                )

    return operations, pieces, verite


def injecter_cas_difficiles(
    operations: list[dict],
    pieces: list[dict],
    verite: list[dict],
    graine: int,
) -> None:
    """Ajoute les cas qui font echouer les outils generiques."""
    alea = random.Random(graine + 1)
    compteur = len(operations) + 10_000

    # a) Paiement groupe : un virement = 3 factures Point P
    code = "MARTIN-BTP"
    base = dt.date(2026, 4, 3)
    ids: list[str] = []
    total = Decimal("0")
    for i in range(3):
        somme = montant((200, 900), alea)
        total += somme
        id_piece = f"P-G{i:03d}"
        ids.append(id_piece)
        date_facture = base - dt.timedelta(days=12 + i * 4)
        pieces.append(
            {
                "id_piece": id_piece,
                "dossier": code,
                "fichier": f"{date_facture:%Y-%m}-point-p-{i}.pdf",
                "fournisseur": "Point P Materiaux",
                "date_facture": date_facture.isoformat(),
                "montant_ttc": f"{somme:.2f}",
                "devise": "EUR",
                "date_reception": date_facture.isoformat(),
                "canal": "email",
            }
        )
    reference = f"OP{compteur:06d}"
    compteur += 1
    operations.append(
        {
            "dossier": code,
            "date_operation": base.isoformat(),
            "date_valeur": base.isoformat(),
            "libelle": "VIR SEPA POINT P MATERIAUX REGLT GROUPE 03/04",
            "debit": f"{total:.2f}",
            "credit": "",
            "devise": "EUR",
            "reference": reference,
        }
    )
    verite.append(
        {
            "reference": reference,
            "statut": "justifie",
            "id_piece_rattachee": " + ".join(ids),
            "motif": "paiement groupe de 3 factures",
        }
    )

    # b) Acompte : facture de 4800, acompte de 1440 (30 %)
    date_facture = dt.date(2026, 4, 8)
    pieces.append(
        {
            "id_piece": "P-A001",
            "dossier": code,
            "fichier": "2026-04-loxam-chantier.pdf",
            "fournisseur": "Loxam Location",
            "date_facture": date_facture.isoformat(),
            "montant_ttc": "4800.00",
            "devise": "EUR",
            "date_reception": date_facture.isoformat(),
            "canal": "email",
        }
    )
    reference = f"OP{compteur:06d}"
    compteur += 1
    operations.append(
        {
            "dossier": code,
            "date_operation": (date_facture + dt.timedelta(days=2)).isoformat(),
            "date_valeur": (date_facture + dt.timedelta(days=2)).isoformat(),
            "libelle": "VIR SEPA LOXAM ACOMPTE 30% 10/04",
            "debit": "1440.00",
            "credit": "",
            "devise": "EUR",
            "reference": reference,
        }
    )
    verite.append(
        {
            "reference": reference,
            "statut": "partiel",
            "id_piece_rattachee": "P-A001",
            "motif": "acompte de 30 % sur facture 4800 EUR",
        }
    )

    # c) Piege : le credit-bail ressemble a une echeance de pret mais exige
    # bien une facture mensuelle.
    date_facture = dt.date(2026, 4, 5)
    pieces.append(
        {
            "id_piece": "P-CB01",
            "dossier": "LOPEZ-CONSEIL",
            "fichier": "2026-04-sg-credit-bail.pdf",
            "fournisseur": "Societe Generale Credit-Bail",
            "date_facture": date_facture.isoformat(),
            "montant_ttc": "612.44",
            "devise": "EUR",
            "date_reception": date_facture.isoformat(),
            "canal": "email",
        }
    )
    reference = f"OP{compteur:06d}"
    compteur += 1
    operations.append(
        {
            "dossier": "LOPEZ-CONSEIL",
            "date_operation": dt.date(2026, 4, 10).isoformat(),
            "date_valeur": dt.date(2026, 4, 10).isoformat(),
            "libelle": "PRLV SEPA SG CREDIT BAIL ECHEANCE 04 ID884412",
            "debit": "612.44",
            "credit": "",
            "devise": "EUR",
            "reference": reference,
        }
    )
    verite.append(
        {
            "reference": reference,
            "statut": "justifie",
            "id_piece_rattachee": "P-CB01",
            "motif": "credit-bail : facture obligatoire malgre le libelle d'echeance",
        }
    )

    # d) Arrondi de centime entre la facture et le paiement
    date_facture = dt.date(2026, 4, 12)
    pieces.append(
        {
            "id_piece": "P-R001",
            "dossier": "ATELIER-9",
            "fichier": "2026-04-ovh.pdf",
            "fournisseur": "OVH SAS",
            "date_facture": date_facture.isoformat(),
            "montant_ttc": "83.99",
            "devise": "EUR",
            "date_reception": date_facture.isoformat(),
            "canal": "email",
        }
    )
    reference = f"OP{compteur:06d}"
    operations.append(
        {
            "dossier": "ATELIER-9",
            "date_operation": dt.date(2026, 4, 14).isoformat(),
            "date_valeur": dt.date(2026, 4, 14).isoformat(),
            "libelle": "PRLV SEPA OVH ID551204",
            "debit": "84.00",
            "credit": "",
            "devise": "EUR",
            "reference": reference,
        }
    )
    verite.append(
        {
            "reference": reference,
            "statut": "justifie",
            "id_piece_rattachee": "P-R001",
            "motif": "ecart d'arrondi de 0,01 EUR",
        }
    )


def ecrire(chemin: Path, lignes: list[dict], entetes: tuple[str, ...]) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with open(chemin, "w", newline="", encoding="utf-8") as f:
        ecrivain = csv.DictWriter(f, fieldnames=list(entetes))
        ecrivain.writeheader()
        ecrivain.writerows(lignes)


def main() -> None:
    parseur = argparse.ArgumentParser(description=__doc__)
    parseur.add_argument("--sortie", default=str(RACINE / "data" / "corpus"))
    parseur.add_argument("--mois", type=int, default=3)
    parseur.add_argument("--debut", default="2026-02-01")
    parseur.add_argument(
        "--taux-manquant",
        type=float,
        default=0.18,
        help="part des achats dont la piece n'a pas ete recue (defaut 18 %%)",
    )
    parseur.add_argument("--graine", type=int, default=20260927)
    args = parseur.parse_args()

    debut = dt.date.fromisoformat(args.debut)
    operations, pieces, verite = generer(
        debut, args.mois, args.taux_manquant, args.graine
    )
    injecter_cas_difficiles(operations, pieces, verite, args.graine)

    sortie = Path(args.sortie)
    ecrire(
        sortie / "releve_bancaire.csv",
        sorted(operations, key=lambda o: (o["dossier"], o["date_operation"])),
        ("dossier", "date_operation", "date_valeur", "libelle", "debit", "credit", "devise", "reference"),
    )
    ecrire(
        sortie / "pieces_recues.csv",
        pieces,
        ("id_piece", "dossier", "fichier", "fournisseur", "date_facture", "montant_ttc", "devise", "date_reception", "canal"),
    )
    ecrire(
        sortie / "verite_terrain.csv",
        verite,
        ("reference", "statut", "id_piece_rattachee", "motif"),
    )
    ecrire(
        sortie / "dossiers.csv",
        [
            {
                "dossier": code,
                "raison_sociale": raison,
                "email_contact": email,
                "nom_contact": contact,
                "jour_echeance_tva": echeance,
                "ton_relance": ton,
            }
            for code, raison, email, contact, echeance, ton in DOSSIERS
        ],
        ("dossier", "raison_sociale", "email_contact", "nom_contact", "jour_echeance_tva", "ton_relance"),
    )

    print(f"Corpus genere dans {sortie}")
    print(f"  {len(operations)} operations bancaires")
    print(f"  {len(pieces)} pieces recues")
    print(f"  {len(verite)} lignes de verite terrain")


if __name__ == "__main__":
    main()
