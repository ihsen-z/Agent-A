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
import json
import re
import subprocess
import sys
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
COLONNES_REGLE = ("dossier", "motif_regex", "motif", "nom", "auteur", "date_activation", "statut")
LONGUEUR_MAX_REGEX = 300
DELAI_SONDE_REGEX = 5.0   # secondes, pour l'ensemble des expressions a sonder


def _analyser_regex(motif: str) -> str:
    """Classe une expression reguliere : "sure", "risquee" (refusee) ou "a_sonder".

    Une expression a quantificateurs imbriques (`(a+)+$`, `(.*)*x`, `(\\w+\\s?)+$`) a un temps
    d'execution exponentiel : UNE seule, dans exclusions_dossier.csv, gele le cycle de tous
    les dossiers (le module `re` ne peut pas etre interrompu). Une alternative quantifiee
    (`(a|aa)+`) peut l'etre aussi : elle est sondee dans un processus a part, avec un delai.
    """
    pile: list[list[bool]] = []          # [contient un quantificateur, contient une alternative]
    a_sonder = False
    i, n = 0, len(motif)
    while i < n:
        c = motif[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            i += 1
            if i < n and motif[i] == "^":
                i += 1
            if i < n and motif[i] == "]":
                i += 1
            while i < n and motif[i] != "]":
                i += 2 if motif[i] == "\\" else 1
            i += 1
            if i < n and motif[i] in "+*{" and pile:
                pile[-1][0] = True
            continue
        if c == "(":
            pile.append([False, False])
        elif c == "|" and pile:
            pile[-1][1] = True
        elif c in "+*{" and pile:
            pile[-1][0] = True
        elif c == ")" and pile:
            contient_quantificateur, contient_alternative = pile.pop()
            suivant = motif[i + 1] if i + 1 < n else ""
            if suivant in ("+", "*", "{"):
                if contient_quantificateur:
                    return "risquee"
                if contient_alternative:
                    a_sonder = True
                if pile:
                    pile[-1][0] = True       # un groupe quantifie compte comme quantificateur du parent
            elif contient_quantificateur and pile:
                pile[-1][0] = True
        i += 1
    return "a_sonder" if a_sonder else "sure"


_CODE_SONDE = (
    "import re, sys, json\n"
    "motifs = json.loads(sys.stdin.read())\n"
    "essais = ['a' * 40 + '!', 'ab' * 30 + '!', ' ' * 50 + 'x', '1' * 40 + 'X', 'x' * 80]\n"
    "for i, m in enumerate(motifs):\n"
    "    r = re.compile(m)\n"
    "    for t in essais:\n"
    "        r.search(t)\n"
    "    print(i, flush=True)\n"
)


def _sonder(motifs: list[str]) -> int | None:
    """Rang de la premiere expression qui ne termine pas dans le delai, ou None."""
    try:
        subprocess.run(
            [sys.executable, "-I", "-c", _CODE_SONDE], input=json.dumps(motifs), text=True,
            capture_output=True, timeout=DELAI_SONDE_REGEX, check=True,
        )
    except subprocess.TimeoutExpired as depassement:
        sortie = depassement.stdout or ""
        if isinstance(sortie, bytes):
            sortie = sortie.decode("utf-8", "replace")
        return len(sortie.split())
    except (subprocess.CalledProcessError, OSError):
        return None
    return None


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

        Les deux premieres colonnes suffisent (format historique : toutes les regles sont
        alors VALIDEES). Lecture STRICTE : une regle PROPOSEE ne s'applique jamais, donc une
        colonne `statut` mal ecrite (`status`, `etat`) qui serait ignoree en silence ferait
        appliquer en etat terminal des regles que personne n'a validees. Toute colonne
        inconnue est donc refusee, la casse et les espaces des en-tetes sont tolerees, un
        `statut` vide est refuse, et une expression reguliere invalide ou susceptible de
        geler le cycle est refusee ICI, avec son numero de ligne.
        """
        apprises: dict[str, list[tuple[str, ...]]] = {}
        provisoires: list[dict[str, str]] = []
        if not (chemin and Path(chemin).exists()):
            return cls(apprises, provisoires)
        nom_fichier = Path(chemin).name
        with open(chemin, newline="", encoding="utf-8-sig") as f:
            lecteur = csv.reader(f)
            try:
                entete_brut = next(lecteur)
            except StopIteration:
                return cls(apprises, provisoires)
            entete = [c.strip().lower() for c in entete_brut]
            inconnues = [brut.strip() for brut, c in zip(entete_brut, entete) if c not in COLONNES_REGLE]
            if inconnues:
                raise ErreurFormat(
                    f"{nom_fichier} : colonne(s) inconnue(s) : {', '.join(repr(c) for c in inconnues)}. "
                    f"Colonnes attendues : {', '.join(COLONNES_REGLE)}. Une colonne mal nommee serait "
                    "ignoree et pourrait faire appliquer des regles non validees"
                )
            doublons = sorted({c for c in entete if entete.count(c) > 1})
            if doublons:
                raise ErreurFormat(f"{nom_fichier} : colonne(s) en double : {', '.join(doublons)}")
            manquantes = {"dossier", "motif_regex"} - set(entete)
            if manquantes:
                raise ErreurFormat(
                    f"{nom_fichier} : colonne(s) manquante(s) : {', '.join(sorted(manquantes))}"
                )
            a_sonder: list[tuple[int, str]] = []
            for numero, cellules in enumerate(lecteur, start=2):
                if not any(c.strip() for c in cellules):
                    continue
                if len(cellules) > len(entete):
                    raise ErreurFormat(f"{nom_fichier} ligne {numero} : plus de cellules que de colonnes")
                ligne = dict(zip(entete, cellules + [""] * (len(entete) - len(cellules))))
                dossier = ligne["dossier"].strip()
                motif_regex = ligne["motif_regex"].strip()
                if not dossier or not motif_regex:
                    raise ErreurFormat(
                        f"{nom_fichier} ligne {numero} : `dossier` et `motif_regex` sont obligatoires"
                    )
                if len(motif_regex) > LONGUEUR_MAX_REGEX:
                    raise ErreurFormat(
                        f"{nom_fichier} ligne {numero} : expression trop longue "
                        f"({len(motif_regex)} caracteres, maximum {LONGUEUR_MAX_REGEX})"
                    )
                try:
                    re.compile(motif_regex)
                except re.error as erreur:
                    raise ErreurFormat(
                        f"{nom_fichier} ligne {numero} : expression reguliere invalide "
                        f"« {motif_regex} » ({erreur})"
                    ) from None
                classe = _analyser_regex(motif_regex)
                if classe == "risquee":
                    raise ErreurFormat(
                        f"{nom_fichier} ligne {numero} : l'expression « {motif_regex} » contient des "
                        "quantificateurs imbriques : son temps d'execution est exponentiel et elle "
                        "gelerait le cycle de TOUS les dossiers. Reecrivez-la plus simplement"
                    )
                if classe == "a_sonder":
                    a_sonder.append((numero, motif_regex))
                if "statut" in entete:
                    statut = ligne["statut"].strip().lower()
                    if not statut:
                        raise ErreurFormat(
                            f"{nom_fichier} ligne {numero} : `statut` est vide. Indiquez "
                            f"{' ou '.join(STATUTS_REGLE)} : une cellule vide ne vaut pas validation"
                        )
                else:
                    statut = "validee"
                if statut not in STATUTS_REGLE:
                    raise ErreurFormat(
                        f"{nom_fichier} ligne {numero} : statut « {statut} » inconnu "
                        f"(attendu : {', '.join(STATUTS_REGLE)})"
                    )
                motif = ligne.get("motif", "").strip() or "Exclu par le cabinet"
                nom = ligne.get("nom", "").strip()
                if statut == "proposee":
                    provisoires.append({
                        "dossier": dossier, "motif_regex": motif_regex, "motif": motif, "nom": nom,
                        "auteur": ligne.get("auteur", "").strip(),
                        "date_activation": ligne.get("date_activation", "").strip(),
                    })
                    continue
                apprises.setdefault(dossier, []).append((motif_regex, motif, nom))
            if a_sonder:
                rang = _sonder([m for _, m in a_sonder])
                if rang is not None:
                    numero, motif_regex = a_sonder[rang]
                    raise ErreurFormat(
                        f"{nom_fichier} ligne {numero} : l'expression « {motif_regex} » ne termine pas "
                        f"en {DELAI_SONDE_REGEX:g} s sur un texte d'essai : elle gelerait le cycle de "
                        "TOUS les dossiers. Reecrivez-la plus simplement"
                    )
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
