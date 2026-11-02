# Application web locale

Interface du MVP pour les personnes du cabinet : relire et valider les brouillons
de relance, les envoyer, rattacher les pièces reçues par e-mail, suivre les pièces
attendues et les escalades, vérifier le journal d'audit.

**Ce document vise un MVP de test, sur des données exclusivement fictives, sur le
poste local de l'utilisateur (Windows).** Les contraintes légales et de conformité
sont hors périmètre.

## 1. Principe

- **Aucune logique métier dans l'interface.** Chaque bouton appelle la même
  fonction de `rapprochement.cycle` que la ligne de commande `scripts/cycle.py`
  (`valider_relance`, `rejeter_relance`, `envoyer_relance`, `trancher_envoi`,
  `rattacher_piece`, `controler_piece`, `saisir_promesse`, `arbitrer_piece`,
  `bloquer_piece`, `debloquer_piece`, `verifier_audit`, `controler_envois`,
  `reparer_audit`, `exporter`), avec `par` = le **nom affiché de l'utilisateur
  connecté**, y compris pour l'envoi (voir 1.1).
- **`validateurs.txt` et `responsables.txt` restent la seule barrière.** Le cycle
  refuse toute décision d'un nom absent de `validateurs.txt` (ou de
  `responsables.txt`), et toute opération de responsable (arbitrer, trancher un
  envoi incertain, réparer le journal) d'un nom absent de `responsables.txt`, que
  la demande vienne du terminal ou du navigateur. Les comptes web tiennent ces deux
  listes à jour (section 4).

### 1.1 Envoi nominatif

« Envoyer » passe `par` = l'utilisateur connecté à `cycle.envoyer_relance` : le
journal d'audit porte `envoye_par` (qui a déclenché l'envoi) en plus de
`valide_par` (qui a validé). La fiche d'un brouillon envoyé affiche les deux, et
l'écran Audit affiche le **contrôle des envois** (`cycle.controler_envois`) : pour
chaque envoi, une validation antérieure par une personne déclarée, le même
destinataire, aucun double envoi, un `envoye_par` présent et déclaré (un envoi
sans `par` est une anomalie), un brouillon présent et ENVOYEE en base, jamais
rejeté avant l'envoi. Les **écarts hors journal** sont listés à part : `.eml` de
`outbox\` sans trace au journal, brouillon ENVOYEE en base sans entrée au journal.
- L'interface **lit** le dépôt SQLite (une connexion par requête) et n'y écrit
  jamais elle-même.
- Bibliothèque standard uniquement (`wsgiref`, `hashlib`, `secrets`...), aucun
  JavaScript, une seule feuille de style.

## 2. Installation sous Windows (PowerShell)

Prérequis : Python 3.11 (lanceur `py`).

```powershell
cd C:\chemin\vers\Agent-A
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install tzdata          # fuseaux horaires (Europe/Paris) : absents de Windows
pip install pytest          # seulement pour lancer les tests
$env:PYTHONUTF8 = 1         # E/S en UTF-8 dans cette session PowerShell
```

`tzdata` est indispensable : sans lui, `ZoneInfo("Europe/Paris")` échoue sous
Windows. `PYTHONUTF8=1` évite les erreurs d'encodage de la console ; le réglage ne
vaut que pour la session PowerShell en cours (à refaire à chaque nouvelle fenêtre,
ou à définir dans les variables d'environnement de l'utilisateur).

Vérifier l'installation :

```powershell
py -3.11 scripts\web.py --help
py -3.11 -m pytest tests\test_web.py -q
```

## 3. Préparer une instance

Une instance est un répertoire par cabinet (voir `cycle.py`) : `depot.sqlite`,
`audit.jsonl`, `validateurs.txt`, `outbox\`, `sortie\`... Le cycle (détection,
brouillons) se lance avec `scripts\cycle.py` ; **l'interface ne lance jamais le
cycle**, elle exploite ce qu'il a produit.

Pour un essai, créer `<instance>\config.json` :

```json
{"mode_test": true, "creneau_envoi_libre": true, "limite_hebdomadaire": true}
```

En mode test, chaque page porte le bandeau « MODE TEST : DONNEES FICTIVES » et la
liste des **garde-fous d'envoi coupés** (`cycle.garde_fous_mode_test`) : par défaut
le créneau d'envoi est libre (heures et jours ouvrés ignorés) ; `"limite_hebdomadaire":
false` coupe aussi la limite d'un e-mail par semaine. Tout envoi est réadressé à
l'adresse de test (objet préfixé `[TEST]`). Hors mode test, ces deux clés n'ont
aucun effet.

**Le mode est figé par instance.** Le premier cycle enregistre le mode (« test »
ou « réel ») ; il ne change plus jamais. L'en-tête de chaque page affiche
« Instance : mode test », « mode réel » ou « mode non figé (aucun cycle) » ; une
instance de test garde le bandeau même si `config.json` ne dit plus `mode_test`.
Si `config.json` demande l'autre mode, un bandeau d'alerte le signale et toute
décision est refusée par le cycle (`ModeInstanceIncompatible`), affichée comme un
refus lisible (409), rien n'étant écrit. Pour changer de mode : créer une nouvelle
instance.

Un `config.json` invalide (JSON cassé, `"mode_test": "oui"`...) est signalé par un
bandeau d'alerte sur chaque page ; le cycle refuse alors toute décision, validation
comprise (il lit le mode demandé dans ce fichier). De même, en
mode test sans `responsables.txt`, un bandeau rappelle que les opérations de
responsable sont ouvertes aux validateurs (repli journalisé `repli_roles_mode_test`).

## 4. Créer les comptes

Les comptes ne se gèrent **qu'en ligne de commande** (pas d'écran
d'administration web : moins de surface d'attaque). Le mot de passe est toujours
saisi au clavier, masqué, deux fois ; il n'est jamais passé en argument (il
finirait dans l'historique PowerShell). Au moins 10 caractères.

```powershell
py -3.11 scripts\web.py creer-utilisateur --instance C:\cabinet\demo --identifiant alice --nom "Alice Durand" --role responsable
py -3.11 scripts\web.py creer-utilisateur --instance C:\cabinet\demo --identifiant bob --nom "Bob Martin" --role comptable
py -3.11 scripts\web.py lister-utilisateurs --instance C:\cabinet\demo
py -3.11 scripts\web.py changer-mot-de-passe --instance C:\cabinet\demo --identifiant bob
py -3.11 scripts\web.py changer-role --instance C:\cabinet\demo --identifiant bob --role responsable
py -3.11 scripts\web.py supprimer-utilisateur --instance C:\cabinet\demo --identifiant bob
```

- Les comptes sont dans `<instance>\utilisateurs.json`, écrit de façon atomique
  (`os.replace`). Mot de passe haché par PBKDF2-HMAC-SHA256, sel aléatoire de
  16 octets, 600 000 itérations ; paramètres stockés avec le hachage.
- Les noms sont normalisés comme dans le cycle (`cycle.normaliser_nom` : forme
  Unicode NFC, espaces de bord retirés) : un nom saisi sous macOS (NFD) et le même
  nom dans `validateurs.txt` édité sous Windows (NFC) sont reconnus comme un seul.
- `creer-utilisateur` ajoute le **nom affiché** (une ligne, sans caractère de
  contrôle, ne commençant pas par `#`) à `validateurs.txt`, et **aussi à
  `responsables.txt` pour le rôle `responsable`**. `changer-role` met
  `responsables.txt` en accord (une rétrogradation en retire d'abord le nom) ;
  `supprimer-utilisateur` retire le nom des deux listes.
- Retirer un nom à la main de l'une des listes suffit à retirer le pouvoir
  correspondant : l'interface affiche alors le refus du cycle. **Hors mode test,
  sans `responsables.txt`, personne ne peut arbitrer, trancher ni réparer.**
- Changer un mot de passe ou supprimer un compte ferme les sessions ouvertes de ce
  compte à leur requête suivante.

## 5. Démarrer

```powershell
py -3.11 scripts\web.py servir --instance C:\cabinet\demo
```

Puis ouvrir `http://127.0.0.1:8000/`. Options :

| Option | Effet |
|---|---|
| `--port 8080` | port d'écoute (défaut 8000) |
| `--entree data\corpus` | répertoire des entrées du cycle : `dossiers.csv` sert à afficher le circuit et la raison sociale (défaut : `data\corpus`) |
| `--hote 0.0.0.0` | écoute sur le réseau. **Déconseillé** : un avertissement s'affiche, aucune connexion n'est chiffrée |
| `--hote-autorise NOM` | nom d'hôte supplémentaire accepté dans l'en-tête `Host` (avec `--hote`) |
| `--https` | ajoute `Secure` aux cookies, à n'utiliser que derrière un mandataire TLS |

Arrêt : `Ctrl+C`. Journal technique : `<instance>\logs\web.log` (accès, connexions,
refus, erreurs internes avec leur trace ; jamais de mot de passe ni de jeton).

## 6. Écrans

| Adresse | Contenu | Actions |
|---|---|---|
| `/connexion` | identifiant et mot de passe | se connecter |
| `/` | tableau de bord : brouillons à relire (et validés, envois incertains), messages non rattachés, escalades, pièces par état, état du journal d'audit | — |
| `/brouillons` | liste filtrable par statut | — |
| `/brouillons/<id>` | destinataire, circuit, dossiers, niveau, validé par, envoyé par, objet, **corps** (texte brut), pièces réclamées avec leur opération bancaire | valider, rejeter (motif), envoyer, trancher un envoi incertain |
| `/brouillons/<id>/envoyer` | **page de confirmation** (pas de JavaScript) : rien ne part avant « Confirmer l'envoi » | confirmer l'envoi |
| `/non-rattaches` | file humaine : chaque pièce reçue, son origine (expéditeur, date de réception, objet, Message-ID, quand le cycle les a conservés), sa proposition de rattachement ou la raison pour laquelle elle n'a pas été routée | confirmer la proposition, rattacher à une autre opération ; pour un message non routé, saisir dossier et référence ; forcer vers un autre dossier (responsable, motif obligatoire) |
| `/pieces` | pièces attendues, filtrables par dossier et état ; échéance et prochaine étape calculées par le cycle | contrôler (conforme / non conforme), saisir une promesse, bloquer, débloquer, classer sans suite (responsable) |
| `/escalades` | pièces escaladées et envois incertains | classer sans suite (responsable) |
| `/audit` | vérification de la chaîne, contrôle des envois (verdict par envoi), 200 dernières entrées (responsable) | exporter le dépôt en JSON (`<instance>\exports\`), réparer une dernière ligne tronquée |
| `/deconnexion` | confirmation | se déconnecter |

Un fichier d'entrée refusé par le cycle (`exclusions_dossier.csv` avec une colonne
inconnue, un statut vide ou une expression régulière imbriquée ; pièce à montant
négatif ; débit et crédit sur la même ligne ; code dossier en double) apparaît
comme un message lisible : refus 409 si une action l'a rencontré, avis sur la
fiche du brouillon quand `dossiers.csv` (option `--entree`) est refusé.

Après une action, un message confirme ce qui a été fait. Une règle du domaine qui
refuse l'action (transition interdite, validateur non déclaré, envoi hors créneau,
limite d'un e-mail par semaine, journal non sain...) s'affiche en clair dans la page,
sans trace technique.

### Droits

| Action | comptable | responsable |
|---|:-:|:-:|
| Valider, rejeter, envoyer un brouillon | oui | oui |
| Confirmer un rattachement (même dossier que la décision) | oui | oui |
| Forcer un rattachement vers un autre dossier, avec motif | — | oui |
| Contrôler, promesse, bloquer, débloquer une pièce | oui | oui |
| Classer sans suite une escalade | — | oui |
| Trancher un envoi incertain | — | oui |
| Journal d'audit : voir, vérifier, exporter, réparer | — | oui |

Une action de responsable demandée par un comptable (formulaire forgé compris)
reçoit `403`. **Défense en profondeur** : arbitrer, trancher et réparer sont
refusés aussi par le cycle si le nom n'est pas dans `responsables.txt`. Exception
assumée : `cycle.rattacher_piece` accepte `forcer` de tout validateur (comme la
ligne de commande `--forcer`) ; seule l'interface le réserve au responsable.

Un rattachement vers un dossier différent de celui de la décision de routage est
refusé par le cycle (CA-03, journal `rattachement_refuse`) : la page l'explique et
indique le dossier d'origine et le dossier saisi. Un responsable peut cocher
« Forcer » et doit donner un motif (journal `rattachement_force`). Un fichier
déjà rattaché à un autre dossier est refusé dans tous les cas, forçage compris
(un fichier n'appartient qu'à un dossier).

L'état du journal (intègre / altéré) reste visible de tous sur l'accueil.

## 7. Ce que l'application ne fait pas

- **Aucun envoi réel.** « Envoyer » dépose un fichier `<id>.eml` dans
  `<instance>\outbox\` (adaptateur `ExpediteurDossier`). Aucun connecteur e-mail,
  Gmail ou SMTP.
- Elle ne lit pas de boîte de réception : les pièces reçues arrivent par le cycle
  (fichiers `.eml` de `entrant\`).
- Elle ne lance pas le cycle, ne gère pas les comptes, ne modifie pas
  `dossiers.csv` ni les règles d'exclusion.
- Elle ne télécharge ni n'affiche les pièces jointes, et ne sert aucun fichier
  hormis sa feuille de style.

## 8. Sécurité : ce qui est fait

- **Échappement systématique** : une seule fonction (`gabarits.echapper`,
  `html.escape(..., quote=True)`) par laquelle passe toute valeur insérée dans une
  page ; les gabarits sont des constantes du code, une donnée n'est jamais
  interprétée comme gabarit (`{{7*7}}` reste du texte). Les caractères invisibles
  (retournement U+202E...) sont rendus visibles. Libellés bancaires, noms de
  pièces jointes, Message-ID, motifs : testés avec des charges hostiles.
- Un Message-ID (choisi par l'expéditeur) ne fait jamais l'aller-retour par un
  formulaire : la page n'envoie qu'une empreinte SHA-256.
- **CSRF** : jeton par session dans chaque formulaire POST, comparé en temps
  constant ; jeton signé de pré-session pour le formulaire de connexion ; cookie
  `SameSite=Strict` ; `Origin` contrôlé s'il est présent ; `Sec-Fetch-Site`
  cross-site refusé. Toute action est un POST ; un GET ne modifie jamais rien.
- **En-tête `Host` contrôlé** sur chaque requête (protection contre le
  rebinding DNS) : seuls `127.0.0.1`, `localhost`, `::1` (et `--hote`,
  `--hote-autorise`) sont acceptés.
- **Sessions** : jeton aléatoire de 32 octets, cookie `HttpOnly; SameSite=Strict;
  Path=/` (`Secure` avec `--https`), stockage côté serveur (empreinte seulement),
  expiration après 30 min d'inactivité et 8 h au plus, nouveau jeton à chaque
  connexion, déconnexion qui détruit la session côté serveur.
- **Connexion** : message unique « Identifiant ou mot de passe incorrect »,
  hachage factice pour un identifiant inconnu (même temps de réponse), blocage
  10 minutes après 5 échecs en 10 minutes, par identifiant ET par adresse.
- **En-têtes** sur toutes les réponses, erreurs et redirections comprises :
  `Content-Security-Policy: default-src 'none'; style-src 'self'; form-action 'self';
  frame-ancestors 'none'; base-uri 'none'`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
  `Cache-Control: no-store`.
- Corps de requête plafonné à 64 Kio, formulaires classiques uniquement, méthodes
  inattendues : `405`. La feuille de style est lue à un chemin fixe au démarrage.
- Écoute sur `127.0.0.1` par défaut.
- Les décisions sont sérialisées dans le processus web (un verrou d'écriture), en
  plus du verrou de fichier du journal d'audit.
- Les textes du contexte de la file (expéditeur, objet...) viennent de l'e-mail
  reçu : ils passent par le même échappement que tout le reste.

## 9. Limites de sécurité assumées (MVP de test)

- **Pas de TLS.** En local (`127.0.0.1`), le trafic ne quitte pas la machine.
  Avec `--hote`, mots de passe et cookies circulent en clair sur le réseau.
- **Comptes locaux simples** : pas de second facteur, pas de politique de
  complexité au-delà de 10 caractères, pas de renouvellement imposé.
- **Sessions en mémoire** : un redémarrage du serveur déconnecte tout le monde ;
  les compteurs de blocage sont aussi perdus.
- **Blocage par adresse** : en local, tous les utilisateurs partagent l'adresse
  `127.0.0.1`. Cinq échecs, quel que soit l'identifiant, bloquent la connexion
  de tous pendant 10 minutes. Accepté pour un poste unique.
- **`Origin: null` toléré.** Avec `Referrer-Policy: no-referrer`, un navigateur
  envoie `Origin: null` sur un POST de la même origine ; la protection repose
  alors sur le jeton CSRF et le cookie `SameSite=Strict` (et `Sec-Fetch-Site`
  quand le navigateur l'envoie).
- **Accès au disque = tous les droits.** Quiconque peut écrire dans le
  répertoire de l'instance peut modifier `utilisateurs.json`,
  `validateurs.txt` ou la base. L'instance doit rester sur un disque local, dans
  un profil Windows non partagé.
- **Rôles en deux endroits** : le rôle du compte web (`utilisateurs.json`) et
  `responsables.txt` doivent concorder ; les commandes de `scripts\web.py` les
  tiennent ensemble. Une modification à la main de l'un sans l'autre donne soit un
  `403` de l'interface, soit un refus du cycle : jamais un droit en plus.
- Le journal technique `web.log` n'est ni tourné ni chaîné.
