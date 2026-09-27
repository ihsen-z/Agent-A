# Données à collecter auprès d'un cabinet

Liste de courses, par ordre de priorité. Tant que les trois premières lignes ne
sont pas obtenues, le système tourne sur corpus synthétique.

## Les 3 documents à demander en premier

| # | Document | Format | Pourquoi |
|---|---|---|---|
| 1 | Relevé bancaire d'un mois, anonymisé | CSV, OFX ou CAMT.053 — **pas un PDF** | Entrée principale du rapprochement |
| 2 | La liste des pièces manquantes du même mois, telle qu'un comptable l'a établie | CSV ou Excel | Vérité terrain : sans elle, aucun taux d'exactitude mesurable, donc aucun argument de vente |
| 3 | Un fichier d'import qui fonctionne dans leur logiciel comptable | Le format natif, 2-3 lignes suffisent | Verrou technique de toute extension vers la saisie |

L'anonymisation acceptable : remplacer la raison sociale et l'IBAN, **garder les
libellés bancaires intacts**. Ce sont eux qui portent l'information ; un libellé
masqué rend le jeu de données inutilisable.

## Sur le processus

- Le logiciel comptable, nommé et versionné (Sage 100, Cegid Quadra/Loop, ACD,
  MyUnisoft, Pennylane, Coala, Ciel).
- **Comment ils détectent une pièce manquante aujourd'hui** : rapprochement
  manuel, liste Excel, solde du compte d'attente 471, export du grand livre ? Si
  c'est le 471, un export FEC donne les pièces manquantes sans passer par le
  relevé — chemin plus court à tester en premier.
- Le circuit d'arrivée des pièces : email dédié, Drive, Dropbox, scan papier,
  WhatsApp du dirigeant. Souvent plusieurs en parallèle.
- La nomenclature des dossiers et des fichiers : c'est ce qui permet de router
  une pièce vers le bon dossier client.
- **Qui relance qui** : le cabinet tunisien écrit-il au client français
  directement, ou passe-t-il par le cabinet français ? Question politique qui
  peut interdire le scénario.
- Le calendrier qui crée l'urgence : échéance TVA (15 ou 24), date de clôture.
- Le point de validation humaine : qui relit avant l'envoi.

## Pour construire et mesurer

- 50 à 100 pièces réelles anonymisées, choisies pour leur **diversité** et non
  leur volume : PDF texte, PDF scanné, photo de ticket, facture multi-pages,
  facture en devise, avoir, facture d'acompte, note de frais.
- 15 à 20 cas que le cabinet cite spontanément comme pénibles.
- 5 à 10 vrais échanges d'email de relance, pour calibrer le ton.
- Les opérations récurrentes propres à leurs dossiers qui n'exigent aucune
  facture (syndic, cotisations ordinales, redevances) → alimentent
  `exclusions_dossier.csv`.
- Les libellés bancaires abrégés de leurs fournisseurs habituels →
  `alias_fournisseurs.csv`.

## Pour l'argumentaire (à mesurer, pas à estimer)

- Nombre de dossiers par cabinet, pièces par mois par dossier.
- **Temps réellement passé en relance**, chronométré sur une semaine.
- Coût horaire chargé d'un comptable chez eux.
- Nombre de dossiers refusés ou retardés faute de capacité — c'est ce chiffre
  qui vend, pas les minutes économisées.

## Contraintes à vérifier avant d'écrire du code pour eux

- Utilisent-ils déjà Dext, Pennylane ou Yooz ? Si oui, on s'y branche au lieu de
  les concurrencer.
- Ont-ils le droit contractuel de sous-traiter à un tiers ? Leur contrat avec le
  cabinet français l'interdit souvent sans autorisation écrite.
- Où sont hébergées les données aujourd'hui.
