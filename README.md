# Relance automatique des pièces justificatives manquantes

Système de rapprochement bancaire destiné aux cabinets de comptabilité offshore
tunisiens travaillant pour des clients français.

Entrées : le relevé bancaire d'un dossier + l'inventaire des pièces reçues.
Sorties : la liste des paiements sans justificatif, un email de relance par
dossier client, et un tableau de suivi.

## Pourquoi ce périmètre, et pas la lecture des factures

L'extraction de factures par OCR est déjà produitisée (Dext, Pennylane, Yooz),
techniquement lourde, à coût variable élevé, et porteuse de responsabilité : une
TVA mal extraite devient un problème juridique.

La chasse aux justificatifs, elle, n'est produitisée par personne, parce qu'elle
dépend du processus de chaque cabinet. Ses données d'entrée sont structurées,
donc le traitement est déterministe et reproductible. Elle ne touche pas au grand
livre, donc elle n'engage aucune responsabilité comptable.

## Le point dur n'est pas le rapprochement, c'est l'exclusion

Sur un relevé de PME française, **60 à 70 % des opérations n'exigent
légitimement aucune facture fournisseur** : salaires, URSSAF, DGFIP, frais
bancaires, échéances de prêt, virements internes, encaissements clients.

Un système qui rapproche ce qu'il peut et signale tout le reste produit des
dizaines de fausses alertes et se fait débrancher en trois jours. Le référentiel
d'exclusions (`src/rapprochement/exclusions.py`) est donc le cœur du produit, pas
un détail de filtrage. Il se décline en deux niveaux :

- des règles générales valables pour tout dossier français ;
- des règles apprises par dossier (`exclusions_dossier.csv`), enrichies chaque
  mois par le comptable. C'est ce second niveau qui rend le remplacement coûteux.

## Aucune IA dans cette version

Le rapprochement compare des montants, des dates et des libellés. Un modèle de
langage y serait plus lent, plus cher, non reproductible, et impossible à
justifier ligne par ligne devant un comptable. Chaque décision porte le nom de la
règle qui l'a tranchée (colonne `regle` du tableau de suivi), ce qui rend le
résultat auditable.

Un modèle de langage n'aura d'intérêt qu'en v2, pour trois usages précis :
identifier un fournisseur derrière un libellé abrégé inconnu, adapter le ton des
relances par dossier, et classer les réponses entrantes. Cela suppose une clé API
(un abonnement Claude Pro ne donne pas d'accès programmatique).

## Utilisation

```bash
# 1. Générer un corpus synthétique (sert à construire et mesurer sans accès cabinet)
python3 scripts/generer_corpus.py

# 2. Lancer un cycle de rapprochement
python3 scripts/lancer.py --ecrire-relances

# 3. Mesurer la qualité contre la vérité terrain
python3 scripts/evaluer.py --detail

# 4. Tests de non-régression
python3 -m pytest tests/ -q
```

## Les deux seuls indicateurs qui comptent

| Indicateur | Définition | Cible | Conséquence d'un échec |
|---|---|---|---|
| Fausses alertes | Opération signalée manquante alors qu'elle ne l'est pas | < 2 % | Le cabinet perd confiance et débranche l'outil |
| Manques ratés | Pièce réellement manquante non signalée | 0 % | Une déclaration part incomplète |

`scripts/evaluer.py` sort en code d'erreur si l'une des deux cibles est ratée.

**Sur les chiffres actuels** : le système atteint 100 % de décisions correctes
sur le corpus synthétique, à travers plusieurs graines et deux taux de pièces
manquantes (un cas à 99,5 %). Cela démontre l'absence de défaut structurel, **pas
une exactitude sur données réelles** — le corpus a été conçu par le même auteur
que le moteur. Le chiffre vendable ne pourra être annoncé qu'après mesure sur un
relevé réel accompagné de sa vérité terrain (voir
[docs/donnees_a_collecter.md](docs/donnees_a_collecter.md)).

## Cas traités

- Appariement au centime et tolérance d'arrondi (0,02 € ou 0,5 %)
- **Affectation globale** : les couples les plus sûrs sont affectés d'abord, ce
  qui évite qu'un paiement traité tôt consomme la pièce d'un autre
- Paiements groupés (une opération = 2 à 4 factures), sous conditions strictes :
  total au centime, combinaison unique, et indice de groupement dans le libellé
- Acomptes, uniquement si le libellé l'annonce
- Libellés bancaires abrégés, via `alias_fournisseurs.csv`
- Contre-exemples : le crédit-bail ressemble à une échéance d'emprunt mais exige
  bien une facture mensuelle

Les cas volontairement **non** tranchés automatiquement : une combinaison
ambiguë reste signalée comme manquante. Relancer un client qui a déjà envoyé la
pièce coûte un email ; ne pas relancer coûte une déclaration incomplète.

## Formats d'entrée

Schémas et exemples dans [`data/schemas/`](data/schemas/). Format pivot en CSV ;
chaque banque exporte différemment, la conversion se fait à l'entrée plutôt que
dans le moteur.

## Architecture

```
src/rapprochement/
  modeles.py        types du domaine
  normalisation.py  nettoyage des libellés bancaires
  exclusions.py     référentiel des opérations sans justificatif  <- le produit
  moteur.py         appariement
  relances.py       génération des emails
  rapport.py        tableau de suivi et statistiques
scripts/
  generer_corpus.py corpus synthétique + vérité terrain
  lancer.py         cycle complet
  evaluer.py        mesure contre la vérité terrain
```

## Déploiement prévu

- Interface cabinet : Google Sheets (tableau de suivi) + Gmail (relances) +
  Drive (pièces). Rien à apprendre pour le comptable.
- Déclenchement : `cron` quotidien. n8n n'apporte rien à ce stade et coûte un
  abonnement mensuel.
- Hébergement : **VPS en Union européenne obligatoire avant le premier client**.
  Les données concernent des entreprises françaises ; le cabinet est
  sous-traitant et devra produire un accord de sous-traitance nommant
  l'hébergeur.
