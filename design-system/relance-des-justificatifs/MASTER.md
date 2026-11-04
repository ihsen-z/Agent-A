# Design System Master File

> **LOGIQUE :** pour construire un écran précis, vérifier d'abord
> `design-system/relance-des-justificatifs/pages/<ecran>.md`. S'il existe, ses
> règles **priment** sur ce fichier. Sinon, appliquer les règles ci-dessous.

---

**Projet :** Relance des justificatifs (interface locale de rapprochement bancaire)
**Établi le :** 2026-11-04, avec le skill `ui-ux-pro-max` 2.15.0
**Catégorie :** Financial Dashboard (base `products.csv`)
**Réglages :** variance 3/10 (minimal) · mouvement 5/10 (standard) · densité 7/10 (tableau de bord)
**Implémentation :** `src/rapprochement/interface/static/app.css` (jetons) et `app.js` (mouvement)

## Provenance des décisions

Le skill a été interrogé en mode `--design-system` puis par domaine. Ses
résultats ont été vérifiés contre le produit avant d'être retenus, comme son
contrat l'exige ; la sortie brute n'a pas été enregistrée telle quelle.

| Sujet | Recommandation du skill | Décision |
|---|---|---|
| Style | Minimalism & Swiss Style (`--design-system`), Data-Dense Dashboard (`products.csv` : Financial Dashboard) | Retenu |
| Motif de page | Enterprise Gateway (site marketing) | Écarté : c'est une application, pas une vitrine |
| Palette claire | Banking / Traditional Finance : marine `#0F172A`, bleu confiance `#1E3A8A`, or `#A16207` | Retenue ; textes ambre et vert assombris pour 4,5:1 |
| Palette sombre | Fond `#020617`, carte `#0E1223`, bordure `#334155`, vert `#22C55E`, rouge `#EF4444` ; « Dark bg + red/green alerts + trust blue » | Retenue, avec variantes plus claires des couleurs d'état (`color-dark-mode`) |
| « Light mode default » à éviter | — | Adapté : le thème suit le système ; sélecteur Clair / Sombre / Auto |
| Typographie | IBM Plex Sans « Financial Trust : excellent for data » (`typography.csv`) ; Fira Code/Sans (`--design-system`) | IBM Plex Sans + IBM Plex Mono, **embarquées localement** (OFL) : pas de Google Fonts, la démo reste hors connexion |
| Mouvement | Stagger List (GSAP), « pas de `back.out` sur des tableaux de données » | Mêmes principes en CSS et Web Animations API : pas de dépendance, pas de dépassement |
| Graphique part du tout | Donut, « risk:high », étiquettes directes obligatoires | Barre empilée + légende chiffrée (5 catégories, valeurs exactes) |

## Règles globales

### Couleurs (jetons sémantiques, jamais d'hexadécimal dans les composants)

Contrastes vérifiés par calcul WCAG (texte normal ≥ 4,5:1).

| Jeton | Clair | Sombre | Usage |
|---|---|---|---|
| `--fond` | `#F8FAFC` | `#020617` | Fond d'application |
| `--surface` | `#FFFFFF` | `#0E1223` | Cartes, tiroir, modale |
| `--surface-2` | `#F1F5F9` | `#131A2E` | En-têtes de tableau, zones secondaires |
| `--piste` | `#E8ECF1` | `#1A1E2F` | Rails de jauges et de barres |
| `--ligne` / `--ligne-forte` | `#E2E8F0` / `#CBD5E1` | `#1E293B` / `#334155` | Bordures |
| `--texte` | `#020617` (20,2:1) | `#F8FAFC` (17,8:1) | Texte principal |
| `--texte-2` | `#334155` (10,4:1) | `#CBD5E1` (12,5:1) | Texte secondaire |
| `--texte-3` | `#475569` (7,6:1) | `#94A3B8` (7,3:1) | Libellés, notes |
| `--marque` | `#1E3A8A` (blanc 10,4:1) | `#2563EB` (blanc 5,2:1) | Action principale |
| `--lien` | `#1D4ED8` (6,7:1) | `#93C5FD` (10,3:1) | Liens, actions secondaires |
| `--barre` | `#0F172A` | `#060B1A` | Barre latérale |
| `--accent` | `#A16207` | `#FBBF24` | Or : badge « démonstration », éléments graphiques |

| État | Texte clair | Fond clair | Texte sombre | Fond sombre | Graphique (clair / sombre) |
|---|---|---|---|---|---|
| Justifiée | `#166534` (6,5:1) | `#DCFCE7` | `#4ADE80` (8,6:1) | vert 14 % | `#16A34A` / `#22C55E` |
| Pièce manquante | `#B91C1C` (5,3:1) | `#FEE2E2` | `#F87171` (5,8:1) | rouge 16 % | `#DC2626` / `#EF4444` |
| À vérifier | `#8A5300` (5,7:1) | `#FEF3C7` | `#FBBF24` (8,9:1) | ambre 14 % | `#D97706` / `#F59E0B` |
| Paiement partiel | `#6D28D9` (6,0:1) | `#EDE9FE` | `#C4B5FD` (8,3:1) | violet 18 % | `#7C3AED` / `#8B5CF6` |
| Sans pièce nécessaire | `#475569` (6,7:1) | `#EEF2F6` | `#94A3B8` (6,1:1) | ardoise 12 % | `#94A3B8` / `#475569` |

Une couleur d'état n'est jamais seule : chaque badge porte un libellé texte
(`color-not-decorative-only`).

### Typographie

- **Texte et titres :** IBM Plex Sans 400 / 500 / 600 / 700, sous-ensemble latin (accents, œ, €, espaces fines).
- **Libellés bancaires et codes :** IBM Plex Mono 400 / 500.
- **Repli :** polices système (`Segoe UI`, `system-ui`), affichage immédiat (`font-display: swap`).
- **Échelle :** 12 · 13 · 14 · 15 · 16 · 20 · 24 · 32 · 40 px ; interligne 1,5.
- **Chiffres tabulaires** partout (`number-tabular`) : montants et compteurs ne bougent pas en s'animant.

### Espacement (densité 7/10)

`--esp-1` 4 px · `--esp-2` 8 px · `--esp-3` 12 px · `--esp-4` 16 px · `--esp-5` 20 px · `--esp-6` 24 px · `--esp-8` 32 px · `--esp-10` 40 px

### Ombres

| Jeton | Clair | Sombre |
|---|---|---|
| `--ombre-1` | `0 1px 2px rgb(2 6 23 / .05), 0 1px 3px rgb(2 6 23 / .06)` | `0 1px 2px rgb(0 0 0 / .4)` |
| `--ombre-2` | `0 4px 6px -1px rgb(2 6 23 / .08)` | `0 4px 10px rgb(0 0 0 / .45)` |
| `--ombre-3` | `0 20px 25px -5px rgb(2 6 23 / .15)` | `0 24px 48px rgb(0 0 0 / .6)` |

En sombre, la profondeur vient surtout des bordures et des surfaces
étagées ; les ombres restent discrètes.

## Thème clair et sombre

- **Auto par défaut** : suit `prefers-color-scheme`.
- **Sélecteur** Clair / Sombre / Auto dans la barre latérale, mémorisé sur le poste (`localStorage`).
- **Pas de flash au chargement** : le thème est posé par un script en tête de page, avant le premier rendu.
- **Bascule animée** par fondu enchaîné (View Transitions API quand le navigateur la propose), sans transition sur tous les éléments.

## Mouvement

Chaque animation exprime une cause et un effet (`motion-meaning`) ; une à deux
par écran (`excessive-motion`) ; uniquement `transform` et `opacity`
(`transform-performance`).

| Jeton | Valeur | Usage |
|---|---|---|
| `--duree-micro` | 120 ms | Survol, appui |
| `--duree-courte` | 180 ms | Changement d'état, sortie de tiroir et de modale |
| `--duree-moyenne` | 280 ms | Entrée de tiroir et de modale, changement d'écran |
| `--duree-longue` | 700 ms | Révélation des données (compteurs, barres) |
| `--courbe-arrivee` | `cubic-bezier(.16, 1, .3, 1)` | Ce qui arrive : décélération, sans dépassement |
| `--courbe-depart` | `cubic-bezier(.7, 0, .84, 0)` | Ce qui part : accélération |
| `--courbe-standard` | `cubic-bezier(.2, 0, 0, 1)` | Changement d'état sur place |
| `--decalage` | 40 ms | Entrée échelonnée (`stagger-sequence` : 30 à 50 ms) |

| Moment | Animation | Raison |
|---|---|---|
| Lancement du rapprochement | Étapes cochées une à une, avec les vrais chiffres | Montre le travail accompli |
| Résultat neuf (lancement, import, règle apprise) | Compteurs des indicateurs, barres qui se remplissent, cartes échelonnées | Le chiffre vient d'être calculé ; rejoué seulement quand il change |
| Retour sur un écran déjà vu | Fondu de 180 ms, sans révélation | Pas d'animation répétitive |
| Filtre des opérations | Fondu enchaîné du tableau (`fade-crossfade`) | Pas d'échelonnement ligne à ligne sur un tableau dense |
| Tiroir, modale | Glisse depuis la droite / léger zoom, sortie à 65 % de la durée d'entrée | `modal-motion`, `exit-faster-than-enter` |
| Boutons, cartes cliquables | Appui à 98 %, survol à −1 px | `scale-feedback` |

- **Mouvement réduit** (`prefers-reduced-motion`) : aucune animation non essentielle, état final immédiat, compteurs affichés directement.
- **Interruptible** : un nouveau rendu annule les compteurs en cours ; rien ne bloque la saisie ; aucun état ne dépend de la fin d'une animation.

## Composants

- **Boutons :** hauteur 38 px (46 px en grand), rayon 8 px, graisse 600 ; principal `--marque`, secondaire bordé, fantôme `--lien`.
- **Cartes :** `--surface`, bordure `--ligne`, rayon 12 px, `--ombre-1` ; une carte cliquable se soulève au survol.
- **Champs :** hauteur 36 px, bordure `--ligne-forte`, focus en anneau 3 px `--marque` à 25 %.
- **Badges d'état :** pastille + libellé, couleurs du tableau d'états.
- **Tiroir :** 500 px, depuis la droite, fond voilé ; Échap ferme ; focus rendu à l'élément d'origine.
- **Focus visible** partout : anneau 3 px couleur `--lien`.

## Anti-motifs

- ❌ Émojis en guise d'icônes : icônes SVG en ligne, même tracé partout.
- ❌ Animer `width`, `height`, `top` ou `left`.
- ❌ Dépassement (`back.out`, ressort) sur des données : il paraît approximatif.
- ❌ Animations infinies hors indicateur de chargement.
- ❌ Ressources externes (CDN, Google Fonts) : la démonstration doit marcher hors connexion.
- ❌ Couleur seule pour porter un sens.

## Liste de contrôle avant livraison

- [ ] Contrastes ≥ 4,5:1 dans les deux thèmes (script de calcul, pas à l'œil).
- [ ] Captures relues en clair et en sombre, à 1440 px et 390 px, sans défilement horizontal.
- [ ] Mouvement réduit : aucune animation, contenu complet.
- [ ] Focus visible au clavier ; Échap ferme tiroir et modale.
- [ ] Aucune ressource chargée hors du poste.
