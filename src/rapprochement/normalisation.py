"""Normalisation des libelles bancaires et des noms de fournisseurs.

Les libelles bancaires francais sont bruites de facon previsible : prefixes de
canal (CARTE, PRLV SEPA, VIR), dates collees, numeros d'autorisation, mentions
de terminal. On les retire avant toute comparaison, sinon la similarite entre
"PRLV SEPA ORANGE FRANCE 12/03 ID4471" et "Orange France" est quasi nulle.
"""

from __future__ import annotations

import re
import unicodedata

_PREFIXES = (
    r"^(carte|cb|paiement\s+cb|achat\s+cb)\b",
    r"^(prlv|prelevement)(\s+sepa)?\b",
    r"^(vir|virement)(\s+sepa)?(\s+(emis|recu|instantane))?\b",
    r"^(cheque|chq)\s*n?\s*\d*",
    r"^(retrait|dab|gab)\b",
    r"^facture\b",
)

_BRUIT = (
    r"\b\d{2}[/.]\d{2}([/.]\d{2,4})?\b",          # dates collees
    r"\bid\s?[:\-]?\s?[a-z0-9]{6,}\b",             # identifiants de mandat
    r"\bref\s?[:\-]?\s?[a-z0-9]{6,}\b",
    r"\bmandat\s?[:\-]?\s?[a-z0-9]+\b",
    r"\b\d{10,}\b",                                 # longs numeros
    r"\bnum\s?\d+\b",
    r"\bech\s?\d+\b",
    r"\bcarte\s?\d{4}\b",
    r"\b\d{4}x{4,}\d{4}\b",                         # PAN masque
    r"\b(fr|de|es|it|be|nl|lu)\b(?=\s*$)",         # pays en fin de libelle
)


def normaliser(texte: str, retirer_prefixes: bool = True) -> str:
    """Minuscule, sans accent, sans bruit technique.

    retirer_prefixes=True retire aussi le prefixe de canal (CARTE, PRLV, VIR).
    C'est ce qu'il faut pour comparer un libelle a un nom de fournisseur, mais
    PAS pour reconnaitre une regle d'exclusion : "VIREMENT INTERNE" perd son
    sens si on retire "virement". Les deux formes sont donc disponibles.
    """
    if not texte:
        return ""
    t = unicodedata.normalize("NFKD", texte)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^a-z0-9/.\s:-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    if retirer_prefixes:
        for _ in range(2):  # "achat cb prlv" : deux prefixes empiles
            for prefixe in _PREFIXES:
                t = re.sub(prefixe, " ", t).strip()

    for bruit in _BRUIT:
        t = re.sub(bruit, " ", t)

    return re.sub(r"\s+", " ", t).strip(" -:/.")


LONGUEUR_JETON_FORT = 5


_MOTS_VIDES = {
    "sarl", "sas", "sasu", "eurl", "sa", "sci", "snc", "scop", "gie",
    "france", "europe", "eu", "international", "groupe", "group",
    "societe", "ste", "cie", "et", "de", "du", "des", "la", "le", "les",
    "services", "service", "solutions", "consulting",
    "location", "locations", "materiaux", "entreprises", "entreprise",
    "pass", "sarl", "holding", "distribution",
}


def jetons_significatifs(texte: str) -> set[str]:
    """Mots porteurs de sens d'un libelle ou d'un nom de fournisseur."""
    mots = normaliser(texte).split()
    return {m for m in mots if len(m) >= 3 and m not in _MOTS_VIDES and not m.isdigit()}


def jeton_fort_commun(libelle: str, fournisseur: str) -> bool:
    """Un mot distinctif du fournisseur apparait tel quel dans le libelle.

    "LOXAM" dans "VIR SEPA LOXAM ACOMPTE 30%" est un signal decisif meme si le
    reste du nom ("Location") est absent du libelle.
    """
    jetons_l = jetons_significatifs(libelle)
    return any(
        j in jetons_l for j in jetons_significatifs(fournisseur)
        if len(j) >= LONGUEUR_JETON_FORT
    )
