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

## MVP : le cycle complet, avec validation humaine

Le moteur ci-dessus répond à « quelles pièces manquent ». Le MVP y ajoute tout
ce qu'il faut pour s'en servir dans un cabinet : suivi de chaque pièce dans le
temps, rattachement des pièces reçues par e-mail, relances **validées par un
humain nommé avant envoi**, journal d'audit chaîné.

```bash
# Une instance = un répertoire par cabinet (base, journal, validateurs, sorties)
mkdir -p instance && echo "Alice Durand" > instance/validateurs.txt

# 1. Un cycle : rapproche, crée les pièces attendues, route les e-mails de
#    instance/entrant/, prépare les brouillons. Il n'envoie et ne valide JAMAIS rien.
python3 scripts/cycle.py cycle --entree data/corpus --instance instance --date 2026-10-05

# 2. Un humain relit puis valide (le nom doit figurer dans validateurs.txt)
python3 scripts/cycle.py valider  --instance instance --par "Alice Durand" <id_brouillon>

# 3. Seul chemin d'émission : refuse tout brouillon non validé, refuse un renvoi
python3 scripts/cycle.py envoyer  --instance instance <id_brouillon>

# Suivi : pièces et états, file des messages non rattachés, intégrité du journal
python3 scripts/cycle.py pieces --instance instance
python3 scripts/cycle.py file   --instance instance
python3 scripts/cycle.py verifier-audit --instance instance
```

Autres commandes : `rejeter`, `trancher` (envoi dont le résultat est inconnu),
`rattacher`, `controler`, `promesse`, `arbitrer`, `bloquer`, `debloquer`,
`reparer-audit`, `exporter`. Toute décision humaine exige `--par`.

**Ce qui est garanti, et testé** : rien ne part sans validation humaine ; jamais
deux envois du même message ; un envoi incertain n'est jamais renvoyé seul ; une
pièce n'est jamais rattachée au dossier d'un autre client ; un cycle interrompu
puis relancé ne perd rien et ne duplique rien.

**Ce qui n'est pas fait** : les connecteurs Gmail, Drive et Google Sheets (il faut
des identifiants et un compte de test ; le MVP lit des `.eml` sur disque et écrit
les e-mails émis dans `outbox/`), et surtout **aucune mesure sur données réelles**.
Les chiffres de ce dépôt viennent d'un corpus synthétique. Les limites assumées
et les conditions d'un pilote sont dans
[docs/architecture_mvp.md](docs/architecture_mvp.md), sections 7 et 8.

## Architecture

```
src/rapprochement/
  modeles.py        types du domaine (contrat entre les modules)
  normalisation.py  nettoyage des libellés bancaires
  exclusions.py     référentiel des opérations sans justificatif  <- le produit
  moteur.py         appariement
  etats.py          machine d'états d'une pièce attendue (table vérifiée couple par couple)
  routage.py        rattachement des pièces reçues par e-mail, prudent par construction
  cadence.py        jours ouvrés, jalons, garde-fous de relance
  relances.py       génération des emails
  depot.py          persistance SQLite
  audit.py          journal d'audit chaîné par SHA-256
  envoi.py          validation humaine et émission, sans double envoi
  cycle.py          orchestration du cycle complet
  rapport.py        tableau de suivi et statistiques
scripts/
  generer_corpus.py corpus synthétique + vérité terrain
  lancer.py         cycle historique, sans état
  cycle.py          cycle du MVP et commandes humaines
  evaluer.py        mesure contre la vérité terrain
docs/
  cahier_des_charges.html   cahier des charges v1.0 (décisions, critères d'acceptation, feuille de route)
  architecture_mvp.md       contrat entre modules, limites connues, conditions d'un pilote
  document_commercial.html  document commercial
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
