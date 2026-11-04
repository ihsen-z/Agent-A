# Skills de design (tiers)

Ces sept dossiers sont une copie non modifiée du plugin **UI/UX Pro Max**,
pour que les sessions Claude Code (locales ou dans le cloud) ouvertes sur ce
dépôt en disposent sans installation.

| | |
|---|---|
| Source | https://github.com/nextlevelbuilder/ui-ux-pro-max-skill |
| Version | 2.15.0 (commit `ccce51c`) |
| Copié depuis | `.claude/skills/` du dépôt source |
| Licence | MIT (`LICENSE-ui-ux-pro-max`) ; `ui-styling/` : Apache 2.0 (`ui-styling/LICENSE.txt`) ; polices de `ui-styling/canvas-fonts/` : SIL OFL (fichiers `*-OFL.txt`) |

| Skill | Rôle |
|---|---|
| `ui-ux-pro-max` | Le principal : base locale de styles, palettes, polices, règles UX et recommandations par stack |
| `ui-styling` | shadcn/ui, Tailwind, visuels sur canvas |
| `design-system` | Jetons de design, spécifications de composants, diapositives |
| `design` | Logo, identité visuelle, bannières, icônes (certaines actions appellent des API externes avec vos propres clés) |
| `brand` | Voix et identité de marque |
| `slides` | Présentations HTML |
| `banner-design` | Bannières réseaux sociaux, publicité, web, impression |

## Utilisation depuis ce dépôt

Les `SKILL.md` écrivent les chemins sous la forme
`${CLAUDE_PLUGIN_ROOT}/.claude/skills/...`, prévue pour une installation en
plugin. Ici, `${CLAUDE_PLUGIN_ROOT}` correspond à la racine du dépôt :

```bash
python3 .claude/skills/ui-ux-pro-max/scripts/search.py "accounting dashboard" --design-system
```

Le moteur de recherche n'utilise que la bibliothèque standard de Python et lit
ses données localement, sans accès réseau.

Les recommandations sont des propositions : pour l'interface de démonstration
(`src/rapprochement/interface/`), elles se vérifient contre le contexte réel
(comptables, projection en salle de réunion, fonctionnement hors connexion).

## Fins de ligne

`.gitattributes` désactive toute conversion de fins de ligne dans ce dossier :
le skill vérifie des empreintes sha256 de ses fichiers de données, qu'une
conversion CRLF sous Windows casserait.

## Mise à jour

Recopier `.claude/skills/` d'une nouvelle version du dépôt source après l'avoir
relue (scripts compris), puis mettre à jour la version et le commit ci-dessus.
Ne pas modifier les fichiers copiés : les écarts avec la source rendraient les
mises à jour difficiles.
