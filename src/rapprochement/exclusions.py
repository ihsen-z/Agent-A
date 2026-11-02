"""Referentiel des operations qui n'exigent aucune piece justificative fournisseur.

C'est le coeur de la valeur du systeme. Sans ce referentiel, un rapprochement
naif signale comme "piece manquante" les salaires, les charges sociales et les
impots, soit 60 a 70 % des lignes d'un releve de PME : le cabinet recoit des
dizaines de fausses alertes et abandonne l'outil.

Deux niveaux :
  - les regles generales ci-dessous, valables pour tout dossier francais ;
  - les regles apprises par dossier (exclusions_dossier.csv), alimentees par le
    comptable quand il marque une operation recurrente comme hors perimetre.
    C'est ce second niveau qui s'enrichit chaque mois et qui justifie
    l'abonnement.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .modeles import OperationBancaire, Sens
from .normalisation import normaliser
from .parseurs import ErreurFormat


def _formes(libelle: str) -> tuple[str, str]:
    """Les deux formes normalisees d'un libelle.

    Sans le prefixe de canal pour comparer a un fournisseur, avec le prefixe
    pour reconnaitre une regle : "VIREMENT INTERNE" ne veut plus rien dire si on
    retire "virement", et "VIR RECU CLIENT" non plus.
    """
    return normaliser(libelle), normaliser(libelle, retirer_prefixes=False)


@dataclass(frozen=True)
class RegleExclusion:
    nom: str
    motif: str
    motifs_regex: tuple[str, ...]
    sens: Sens | None = None          # None = les deux sens

    def correspond(self, operation: OperationBancaire) -> bool:
        if self.sens is not None and operation.sens is not self.sens:
            return False
        return any(
            re.search(p, forme)
            for forme in _formes(operation.libelle)
            for p in self.motifs_regex
        )


# Ordre volontaire : les regles les plus specifiques d'abord.
REGLES: tuple[RegleExclusion, ...] = (
    RegleExclusion(
        nom="salaires",
        motif="Salaire ou acompte de paie : justifie par le bulletin de paie",
        motifs_regex=(
            r"\bvir(ement)?\s+(de\s+)?salaire",
            r"\bsalaire[s]?\b",
            r"\bpaie\b",
            r"\bacompte\s+sur\s+salaire",
            r"\bremun(eration)?\s+(gerant|dirigeant)",
        ),
        sens=Sens.DEBIT,
    ),
    RegleExclusion(
        nom="charges_sociales",
        motif="Charge sociale : justifiee par le bordereau de l'organisme",
        motifs_regex=(
            r"\burssaf\b", r"\bmsa\b", r"\bagirc\b", r"\barrco\b",
            r"\bmalakoff\b", r"\bag2r\b", r"\bklesia\b", r"\bpro\s?btp\b",
            r"\bcipav\b", r"\bssi\b", r"\bnet\s?entreprises\b",
            r"\bmutuelle\b", r"\bprevoyance\b", r"\bretraite\b",
            r"\bcotisation[s]?\s+(sociale|retraite|prevoyance)",
        ),
        sens=Sens.DEBIT,
    ),
    RegleExclusion(
        nom="impots_taxes",
        motif="Impot ou taxe : justifie par l'avis ou la declaration",
        motifs_regex=(
            r"\bdgfip\b", r"\bd\.?g\.?f\.?i\.?p\b", r"\btresor\s+public\b",
            r"\bimpot[s]?\b", r"\bsie\b", r"\bcfe\b",
            r"\bt\.?v\.?a\.?\b", r"\btva\s+(3310|ca3|ca12)",
            r"\bacompte\s+(is|impot)", r"\btaxe\s+(fonciere|apprentissage|salaire)",
            r"\bcvae\b", r"\bcotisation\s+fonciere\b",
        ),
        sens=Sens.DEBIT,
    ),
    RegleExclusion(
        nom="frais_bancaires",
        motif="Frais bancaire : justifie par le releve lui-meme",
        motifs_regex=(
            r"\bfrais\s+(bancaire|de\s+tenue|sur\s+virement)",
            r"\bcommission[s]?\s+(d[\'e]|de\s+)?",
            r"\bagios?\b", r"\binteret[s]?\s+debiteur",
            r"\bcotisation\s+(carte|compte|convention)",
            r"\babonnement\s+(banque|carte)",
            r"\bfrais\s+d[\'e]?\s?(incident|rejet|opposition)",
        ),
    ),
    RegleExclusion(
        nom="emprunts",
        motif="Echeance d'emprunt : justifiee par le tableau d'amortissement",
        motifs_regex=(
            r"\bech(eance)?\s+(pret|emprunt|credit)",
            r"\bremboursement\s+(pret|emprunt)",
            r"\bpret\s+n\s?\d", r"\bamortissement\s+pret",
        ),
        sens=Sens.DEBIT,
    ),
    RegleExclusion(
        nom="mouvements_internes",
        motif="Mouvement interne entre comptes de l'entreprise",
        motifs_regex=(
            r"\bvirement\s+interne\b",
            r"^interne\s+vers\b",
            r"\binterne\s+vers\s+compte\b",
            r"\btransfert\s+(interne|de\s+compte)",
            r"\bvir(ement)?\s+(vers|depuis)\s+compte\s+(courant|epargne|titre)",
            r"\bcompte\s+courant\s+d[\'e]?\s?associe",
            r"\balimentation\s+compte\b",
        ),
    ),
    RegleExclusion(
        nom="encaissements_clients",
        motif="Encaissement client : suivi par la facturation de vente, pas par les achats",
        motifs_regex=(
            r"\bremise\s+(de\s+)?cheque",
            r"\bvir(ement)?\s+recu\b",
            r"^recu\s+client\b",
            r"\bencaissement\b",
            r"\bclient\s+\d{3,}\b",
            r"\bremise\s+carte\b", r"\bcredit\s+carte\s+commercant",
            r"\b(stripe|sumup|paypal|shopify|mollie)\b",
        ),
        sens=Sens.CREDIT,
    ),
    RegleExclusion(
        nom="rejets_et_annulations",
        motif="Rejet ou annulation technique : sans piece propre",
        motifs_regex=(
            r"\brejet\s+(prelevement|cheque|virement)",
            r"\bannulation\b", r"\bcontre[\s-]?passation\b",
            r"\bimpaye\b", r"\bretour\s+virement\b",
        ),
    ),
)


# Cas a NE PAS exclure malgre une apparence proche : ils exigent bien une facture.
# Le credit-bail et la location financiere emettent une facture mensuelle,
# contrairement a une echeance d'emprunt.
CONTRE_EXEMPLES: tuple[str, ...] = (
    r"\bcredit[\s-]?bail\b",
    r"\bleasing\b",
    r"\bloa\b",
    r"\blocation\s+(financiere|longue\s+duree)",
)


def _contre_exemple(operation: OperationBancaire) -> bool:
    return any(
        re.search(p, forme)
        for forme in _formes(operation.libelle)
        for p in CONTRE_EXEMPLES
    )


STATUTS_REGLE = ("validee", "proposee")


class Referentiel:
    """Regles generales + exclusions apprises par dossier.

    Une regle apprise a un cycle de vie (cahier des charges section 8) : PROPOSEE
    puis VALIDEE. Une regle PROPOSEE ne s'applique PAS : une expression reguliere
    trop large clorait des pieces en etat terminal (HORS_PERIMETRE), sans auteur ni
    date, et la piece reellement manquante ne serait plus jamais reclamee. Elle est
    seulement exposee dans `provisoires` pour que le responsable la valide.
    """

    def __init__(
        self,
        apprises: dict[str, list[tuple[str, ...]]] | None = None,
        provisoires: list[dict[str, str]] | None = None,
    ) -> None:
        # dossier -> [(motif_regex, motif_lisible[, nom])] : regles VALIDEES, appliquees
        self.apprises = apprises or {}
        # regles proposees, non appliquees : [{dossier, motif_regex, motif, nom, auteur, date_activation}]
        self.provisoires = provisoires or []

    @classmethod
    def depuis_csv(cls, chemin: Path | str | None) -> "Referentiel":
        """Charge exclusions_dossier.csv : dossier,motif_regex[,motif,nom,auteur,date_activation,statut].

        Les trois premieres colonnes suffisent (format historique : toutes les regles
        sont alors validees). `statut` vaut `validee` (defaut) ou `proposee`. Une
        expression reguliere invalide est refusee ICI, avec le numero de ligne : elle
        ferait sinon planter le cycle de tous les dossiers au moment de l'appliquer.
        """
        apprises: dict[str, list[tuple[str, ...]]] = {}
        provisoires: list[dict[str, str]] = []
        if chemin and Path(chemin).exists():
            nom_fichier = Path(chemin).name
            with open(chemin, newline="", encoding="utf-8-sig") as f:
                lecteur = csv.DictReader(f)
                manquantes = {"dossier", "motif_regex"} - set(lecteur.fieldnames or [])
                if manquantes:
                    raise ErreurFormat(
                        f"{nom_fichier} : colonne(s) manquante(s) : {', '.join(sorted(manquantes))}"
                    )
                for numero, ligne in enumerate(lecteur, start=2):
                    dossier = (ligne.get("dossier") or "").strip()
                    motif_regex = (ligne.get("motif_regex") or "").strip()
                    if not dossier or not motif_regex:
                        raise ErreurFormat(
                            f"{nom_fichier} ligne {numero} : `dossier` et `motif_regex` sont obligatoires"
                        )
                    try:
                        re.compile(motif_regex)
                    except re.error as erreur:
                        raise ErreurFormat(
                            f"{nom_fichier} ligne {numero} : expression reguliere invalide "
                            f"« {motif_regex} » ({erreur})"
                        ) from None
                    statut = (ligne.get("statut") or "validee").strip().lower()
                    if statut not in STATUTS_REGLE:
                        raise ErreurFormat(
                            f"{nom_fichier} ligne {numero} : statut « {statut} » inconnu "
                            f"(attendu : {', '.join(STATUTS_REGLE)})"
                        )
                    motif = (ligne.get("motif") or "Exclu par le cabinet").strip()
                    nom = (ligne.get("nom") or "").strip()
                    if statut == "proposee":
                        provisoires.append({
                            "dossier": dossier, "motif_regex": motif_regex, "motif": motif, "nom": nom,
                            "auteur": (ligne.get("auteur") or "").strip(),
                            "date_activation": (ligne.get("date_activation") or "").strip(),
                        })
                        continue
                    apprises.setdefault(dossier, []).append((motif_regex, motif, nom))
        return cls(apprises, provisoires)

    def exclure(self, operation: OperationBancaire) -> tuple[bool, str, str]:
        """Renvoie (exclue, motif lisible, nom de la regle)."""
        if _contre_exemple(operation):
            return False, "", ""

        formes = _formes(operation.libelle)
        for regle_apprise in self.apprises.get(operation.dossier, []):
            motif_regex, motif, *reste = regle_apprise
            if any(re.search(motif_regex, forme) for forme in formes):
                nom = reste[0] if reste and reste[0] else ""
                return True, motif, f"apprise:{operation.dossier}" + (f":{nom}" if nom else "")

        for regle in REGLES:
            if regle.correspond(operation):
                return True, regle.motif, regle.nom

        return False, "", ""
