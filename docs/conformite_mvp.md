# Audit de conformité du MVP au cahier des charges v1.0

Auditeur indépendant, qui n'a écrit aucune ligne du projet. Ce document constate, prouve et classe. Il ne corrige rien.

## 1. Méthode

**Version auditée** : commit `ce6cd1d799f2fe0fe0d4694df7c337233b12f316` (« Lecture des e-mails isolée dans un processus borné, et documentation d'usage », 2026-10-05). L'audit a porté sur une copie figée de ce commit, extraite par `git archive HEAD`, et jamais sur le dossier de travail, que d'autres experts modifiaient pendant l'audit.

**Référence** : `docs/cahier_des_charges.html` v1.0 (texte extrait par un script qui retire les balises), puis `docs/architecture_mvp.md` (sections 4, 7 et 8), `README.md`, `docs/document_commercial.html`, le code de `src/rapprochement/*.py` et `scripts/*.py`, et les 12 fichiers de tests, assertions comprises.

**Environnement** : `python3` vaut Python 3.11.18, mais il n'a pas pytest (`python3 -m pytest` répond « No module named pytest »). Les tests ont donc été lancés avec le `pytest` installé (outil uv, Python 3.13.18). Les scripts du produit ont tourné sous Python 3.11.18. Machine à 4 cœurs.

**Ce qui a été exécuté** :

| # | Commande ou protocole | Résultat |
|---|---|---|
| E1 | `pytest -q -rx` (suite complète) | **2550 passed, 8 xfailed en 75 s**. Les 8 xfail stricts sont ceux que documente l'architecture (§7) |
| E2 | Chaque test cité comme preuve dans ce document, lancé **seul** | tous verts (détail en annexe A) |
| E3 | 40 mutations du code dans une seconde copie, avec relance des tests concernés, ou de toute la suite quand le doute le justifiait (annexe B) | 15 survivent. 3 survivent par défense en profondeur (M12 et M13 seules, M28), et 2 visaient la mauvaise fonction (M26 et M26b, remplacées par M26c, tuée). Les **10 autres révèlent un vrai trou de test** (7 comportements distincts : aucun test n'échoue alors que le code est faux) |
| E4 | `python3 scripts/generer_corpus.py` puis `python3 scripts/evaluer.py --detail` | 284 opérations, 284 décisions correctes (100,0 %), fausses alertes 0/261, manques ratés 0/23 |
| E5 | Balayage : 10 graines × 2 taux de pièces manquantes (0,18 et 0,35), puis `evaluer.py` | **2 corpus sur 20 échouent à CA-02** (graine 4 / 0,18 et graine 10 / 0,35) |
| E6 | Banc CA-09 : générateur du dépôt étendu à 20 dossiers × 250 opérations = **5 000 opérations**, 1 862 pièces, 300 e-mails, puis `scripts/cycle.py cycle` chronométré | 7,97 s, et 7,31 s pour le rejeu |
| E7 | Routage synthétique sur l'instance du banc : 200 e-mails « favorables » et 100 e-mails « pièges » (contact du dossier X, montant présent seulement chez Y) | 184/200 propositions justes, 0 fausse ; 100/100 pièges non routés ; 0 rattachement entre dossiers |
| E8 | Simulation de 30 jours sur le corpus : cycle quotidien, validation puis envoi par un humain déclaré, 3 copies d'un même e-mail entrant, puis contrôle exhaustif du journal | 9 envois, 9 `.eml`, 9 identifiants distincts, 0 envoi sans `valide`, au moins 7 jours entre deux envois |
| E9 | Interruption réelle : SIGKILL du processus `cycle` à 0,8 / 1,5 / 2,5 / 3,5 / 4,5 / 5,5 / 6,5 s, puis relance | état du dépôt identique à la référence dans 7 cas sur 7 ; journal intègre |
| E10 | Sondes ciblées sur le moteur, le routage et le journal (scripts Python courts, résultats cités là où ils servent) | voir chaque ligne |

Les scripts de E5 à E9 sont dans le scratchpad de la session d'audit (`/tmp/claude-0/-home-user-Agent-A/6aa54cdc-0faa-5e40-a323-23b4379e5206/scratchpad/outils/`). Ils ne font pas partie du dépôt.

**Règle de preuve** : un `CONFORME` n'est retenu que si le test cité existe, passe seul, et **échoue quand on casse le comportement qu'on lui attribue**, ou bien si une sonde exécutée le prouve directement. Quand un test passe pour une autre raison, il n'est pas une preuve : la ligne le dit.

**Périmètre convenu** : MVP de test sur données fictives. La section 13 (RGPD, hébergement UE, INPDP, sous-traitance, revue juridique, conservation) et CA-12 sont `NON APPLICABLE EN MODE TEST`. Les connecteurs Gmail, Drive et Sheets sont `HORS PERIMETRE MVP`.

## 2. Matrice de conformité

Statuts : `CONFORME` / `ECART` / `PARTIEL` / `HORS PERIMETRE MVP` / `NON APPLICABLE EN MODE TEST`. « Mxx » renvoie à une mutation de l'annexe B, « E » à une exécution du §1.

### §1 Objet : entrées et sorties

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| 1.1 | Relevé en CSV, OFX ou CAMT.053, jamais un PDF | PARTIEL | Seul le CSV pivot est lu (`parseurs.lire_releve`). Aucun lecteur OFX ni CAMT.053 dans `src/` ni dans `scripts/`. |
| 1.2 | Sortie : tableau de suivi, une ligne par opération, avec la règle qui l'a tranchée | CONFORME | `rapport.ENTETES_SUIVI` contient `regle`. `sortie/tableau_suivi.csv` du banc E6 compte une ligne par opération (5 000), colonne `regle` renseignée. |
| 1.3 | Sortie : brouillon de relance soumis à relecture | CONFORME | `sortie/brouillons/*.txt` au statut `brouillon` (E6 : 20 brouillons). Le groupement se fait par destinataire, pas par dossier : voir C3. |
| 1.4 | Sortie : file des pièces entrantes non routées | CONFORME | `sortie/file_humaine.csv`. E7 : 116 entrées non routées sur 300 messages. |
| 1.5 | Sortie : indicateurs du cycle et journal d'audit | CONFORME | `sortie/resume.txt`, `audit.jsonl`. `cycle.py verifier-audit` répond « OK, chaîne intacte » (E9). |

### §2 Décisions D1 à D6

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| D1 | Une pièce n'est « attendue » que si une opération bancaire l'exige | CONFORME | `etats.creer_depuis_rapprochement` refuse tout statut autre que MANQUANT. `test_etats.py::test_creer_refuse_les_autres_statuts` et `::test_corpus_les_autres_statuts_levent_value_error` (verts seuls). |
| D2.a | Aucune IA dans la décision | CONFORME | Aucun import réseau ni LLM dans `src/` et `scripts/` (grep sur `requests`, `urllib`, `http`, `socket`, `smtplib`, `anthropic`, `openai`, `imaplib` : 0 résultat). |
| D2.b | Chaque décision porte le nom de la règle | CONFORME | Champ `Rapprochement.regle`. `test_rapprochement.py::test_operations_sans_piece_justificative` vérifie le nom de la règle (7 cas), `::test_exclusion_apprise_par_dossier` aussi. |
| D2.c | Décision rejouable à l'identique | CONFORME | Voir CA-08 (§3). |
| D2.d | Une promesse du client est saisie d'un clic par le comptable, jamais détectée | CONFORME | Commande `promesse` (`cycle.saisir_promesse`). `test_cycle.py::test_promesse_puis_retour_en_demandee_trois_jours_ouvres_apres`, `::test_cli_promesse_succes`. |
| D3 | 100 % des relances validées par un humain avant envoi ; pas d'envoi automatique en v1 | CONFORME | Voir CA-06. Aucun mécanisme d'envoi automatique n'existe, ce qui est conforme à la v1. |
| D4 | Une instance isolée par cabinet, base et stockage séparés | PARTIEL | Un répertoire par instance, avec sa base et son journal (`cycle.Instance`), et des validateurs qui ne s'héritent pas (`test_adversarial.py::test_sain_instance_alternative_n_herite_pas_des_validateurs`). L'isolation physique relève du déploiement. |
| D5 | Interface Sheets / Gmail / Drive | HORS PERIMETRE MVP | CSV, `.eml` sur disque, `outbox/` (README, architecture §6). |
| D6.a | L'expéditeur est le cabinet, sous sa signature | PARTIEL | `ExpediteurDossier` émet `From: relances@cabinet.invalid` par défaut, non paramétrable depuis le CLI. Le corps du brouillon ne porte aucune signature du cabinet (brouillon du banc E6 : il finit sur « Un grand merci d'avance, »). |
| D6.b | Le circuit est un **paramètre obligatoire** du dossier | **ECART** | `parseurs.lire_dossiers`, ligne 186 : `circuit = (ligne.get("circuit") or "C-DIRECT")`. Une colonne absente ou une cellule vide donne C-DIRECT : on écrit au client final. Le `dossiers.csv` du corpus n'a pas de colonne `circuit`. Le §15 du cahier exige au contraire « C-RELAIS par défaut ». |

### §3 Périmètre

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| 3.1 | Détection des paiements sans justificatif | PARTIEL | Moteur en place : E4 trouve 23/23 manquants. Mais des manques sont ratés sur 2 corpus sur 20 (E5) et sur le banc (2/357) : voir 6.6 et B1. |
| 3.2 | Exclusion des opérations qui n'exigent aucune pièce | CONFORME | `exclusions.REGLES`. E4 : 179/179 opérations hors périmètre correctement classées. `test_operations_sans_piece_justificative`. |
| 3.3 | Génération et suivi des relances | CONFORME | `cadence.planifier`, `relances.construire_brouillon`, `etats`. E8 : 3 envois par destinataire, puis 23 pièces sur 23 escaladées au jour 30. |
| 3.4 | Routage des pièces reçues par e-mail | PARTIEL | Les `.eml` sont lus dans `entrant/`. Aucune lecture de la boîte de collecte (connecteur hors périmètre). |
| 3.5 | Tableau de suivi et journal d'audit | CONFORME | Voir 1.2 et 12.4. |
| 3.6 | Apprentissage des exclusions propres au dossier | PARTIEL | Seulement un fichier statique `exclusions_dossier.csv` (dossier, regex, motif) tenu à la main. Aucun cycle de vie : voir 8.2 à 8.4. |
| 3.7 | Hors périmètre respecté (OCR, écritures, déclarations, litiges, mobile) | CONFORME | Aucun module de ce type dans `src/`. |

### §4 Acteurs et circuits

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| 4.1 | Trois circuits ; un dossier en porte un et un seul | CONFORME | `modeles.CIRCUITS`. Un circuit inconnu est refusé, un C-RELAIS sans `email_relais` aussi (`parseurs.py` lignes 188 à 197). Sur le défaut appliqué quand le circuit manque : voir D6.b. |
| 4.2 | C-RELAIS : on écrit au cabinet donneur d'ordre | CONFORME | `Dossier.destinataire`. `test_cadence.py::test_relais_utilise_email_relais_et_regroupe_avec_les_dossiers_du_relais`. |
| 4.3 | C-INTERNE : on écrit à un collaborateur du cabinet | CONFORME | `routage.CIRCUITS_SANS_ADRESSE_CLIENT` empêche que cette adresse route une pièce. `test_routage.py::test_adresses_par_dossier_normalise_et_exclut_relais_et_interne`. |
| 4.4 | Rôles distincts : responsable de portefeuille (arbitrage, activation de l'envoi automatique) et comptable référent (valider, corriger, envoyer, **proposer une règle**) | **ECART** | Il n'y a qu'une liste plate, `validateurs.txt` : tout validateur déclaré peut arbitrer (`cycle.arbitrer_piece`). Aucune commande pour proposer une règle d'exclusion. |
| 4.5 | Le client final n'a aucun accès : il répond par e-mail | CONFORME | Il n'existe aucune interface. |

### §5 Modèle de données : les sept entités et les invariants

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| 5.1 | **Dossier** : circuit, contacts, référentiel d'exclusions propre | CONFORME | `modeles.Dossier` (circuit, email_contact, email_relais, domaines). Le référentiel propre est `exclusions_dossier.csv`, indexé par dossier. Source CSV relue à chaque cycle, sans persistance. |
| 5.2 | **Période** : mois **ou trimestre**, clé dossier + AAAA-MM, état calculé | PARTIEL | Mois seulement (`PieceAttendue.periode = "AAAA-MM"`, `etats.py` ligne 317). Aucun trimestre, aucune entité ni table. L'état est bien calculé (`etats.etat_periode`). |
| 5.3 | **OperationBancaire** : date, libellé brut, montant, sens, devise, référence stable ; clé dossier + référence | PARTIEL | La dataclass est complète et la référence de repli est stable (`test_parseurs.py`, 6 tests). Mais le moteur indexe par référence seule (xfail `test_defaut_moteur_collision_de_reference_entre_dossiers`) et le cycle refuse les collisions (limite assumée). Les opérations ne sont pas persistées : un rejeu à six mois exige d'avoir archivé les CSV. |
| 5.4 | **PieceAttendue** : son état (§9) **et le motif de sa création** | PARTIEL | Table `pieces`, clé primaire (dossier, référence), état contrôlé par une contrainte CHECK. **Aucun champ motif ni règle** : le journal `piece_creee` ne garde que la période, le montant, la devise et la date. |
| 5.5 | **PieceRecue** : origine, empreinte, date de réception, rattachement proposé ou confirmé ; clé SHA-256 | PARTIEL | Pas d'entité. La table `file_humaine` garde le message_id, l'empreinte, le nom du fichier, le statut, le dossier, la référence et qui l'a résolue. **Ni l'expéditeur ni la date de réception** ne sont persistés comme champs (grep `expediteur` et `recu_le` dans `depot.py` : 0 résultat ; l'adresse ne figure que dans le texte `detail` de certains refus). Le fichier n'est pas conservé hors de `entrant/`. Le rattachement confirmé n'est qu'une chaîne `nom#empreinte12` dans `pieces_rattachees`. |
| 5.6 | **Relance** : dossier, destinataire, pièces, statut, horodatage ; clé uuid | CONFORME | `Brouillon` et table `brouillons` (statuts brouillon, validée, en_cours, envoyée, rejetée, `valide_le`, `envoye_le`). La clé est un identifiant déterministe plutôt qu'un uuid : voir C6. |
| 5.7 | **RegleExclusion** : nom, motif en français, **auteur, date d'activation** | **ECART** | Les règles générales ont un nom et un motif (`exclusions.REGLES`). Les règles apprises n'ont **ni nom** (règle `apprise:<dossier>`), **ni auteur, ni date** (`Referentiel.depuis_csv` ne lit que dossier, motif_regex et motif). |
| 5.8 | Invariant, **porté par la base** : une pièce reçue n'est rattachée qu'à des pièces attendues d'un même dossier | **ECART** | Aucune contrainte en base (`pieces_rattachees` est un TEXT). Le rattachement humain traverse les dossiers sans alerte (sonde E10 : message proposé pour D03, rattaché par `rattacher_piece(..., dossier="D00", reference="OP000010")`, accepté, pièce passée à RECUE). |
| 5.9 | Invariant : une relance ne référence que des pièces d'un même dossier et d'un même destinataire | ECART (voulu) | Le code regroupe **plusieurs dossiers** par destinataire (`cadence.planifier`), comme l'exige le §10. Contrôle dans le code (`construire_brouillon`), pas en base. Contradiction C3. |
| 5.10 | Invariant : une opération exclue ne porte pas de pièce attendue | PARTIEL | Le cycle fait passer la pièce à HORS_PERIMETRE (`test_cycle.py::test_piece_connue_devenue_hors_perimetre_ou_justifiee`, M31 tuée). Pas en base. |
| 5.11 | Invariant : toute écriture est horodatée et attribuée à un acteur nommé | PARTIEL | Chaque entrée du journal porte un acteur et un horodatage. Les lignes de la base n'ont ni l'un ni l'autre. `vus.vu_le` lit l'horloge murale (xfail `test_defaut_depot_export_non_reproductible`). |
| 5.12 | Identifiants stables, rapprochement rejouable six mois plus tard | PARTIEL | Voir 5.3 et CA-08 : reproductible à entrées égales, mais les entrées ne sont pas conservées par le produit. |

### §6 Règles de rapprochement

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| 6.1 | Normalisation : préfixes, références et ponctuation retirés ; forme préfixée conservée | CONFORME | `test_rapprochement.py::test_normalisation_retire_le_bruit_bancaire`, `::test_normalisation_peut_conserver_le_prefixe`. « VIREMENT INTERNE » est reconnu (`test_operations_sans_piece_justificative`). |
| 6.2 | Exclusions avant tout appariement | CONFORME | `Moteur.rapprocher` appelle `_trancher_sans_piece` avant les passes. Corpus E4 : 179/179. |
| 6.3 | Règles propres au dossier d'abord, générales ensuite | CONFORME (non testé) | Sonde : une règle apprise « urssaf » l'emporte sur `charges_sociales` (`(True, 'Regle du dossier', 'apprise:T')`). **Aucun test** ne le vérifie : M08 (ordre inversé) survit à toute la suite. |
| 6.4 | Tolérance de 0,02 € ou 0,5 %, la plus grande des deux | CONFORME (mal testé) | Sonde : 1 005,00 contre une facture de 1 000 donne JUSTIFIE, 1 005,01 donne MANQUANT. **La règle des 0,5 % n'est testée nulle part** : M01c (0,5 % ramené à 0) survit à toute la suite. `test_ecart_d_arrondi_accepte` (écart de 0,01) passe avec l'une ou l'autre tolérance seule (M01 et M02 survivent chacune). |
| 6.5 | Paiements groupés : **le centime seulement** | ECART | `moteur._montants_identiques` tolère 0,02 €. Sonde : 750,02 € contre 300 + 450 donne JUSTIFIE. Non testé : M03c survit. |
| 6.6 | Affectation globale, couples les plus sûrs d'abord | PARTIEL | L'affectation est bien globale (`test_affectation_globale_prefere_le_meilleur_couple`, M10 tuée). Mais « plus sûr » ignore l'exactitude du montant, puisque `score_montant` vaut 0 ou 1. Reproduction minimale : OP-A 628,75 € (le 10/03) et OP-B 625,82 € (le 18/03), une facture de 625,82 € (le 05/03) : **la facture va à OP-A** (montant approché, date plus proche). OP-B, au montant exact, reste MANQUANT, et la pièce vraiment manquante (OP-A) n'est jamais réclamée. |
| 6.7 | Groupés de 2 à 4 : total au centime, **combinaison unique**, **indice de groupement dans le libellé** | PARTIEL | Unicité : `test_combinaison_ambigue_reste_manquante` (M06 tuée). Indice : exigé **seulement pour 2 pièces** (`if taille == 2 and not indice`). Sonde : 3 factures sans indice donnent `a_verifier`, règle `paiement_groupe_3`. M04 (indice toujours exigé) survit : aucun test n'impose la règle du cahier. |
| 6.8 | Acomptes seulement si le libellé l'annonce | CONFORME | `test_paiement_recurrent_inferieur_n_est_pas_un_acompte`, `test_acompte_reconnu_quand_le_libelle_l_annonce` (M07 tuée). |
| 6.9 | Un cas ambigu n'est **jamais tranché** ; une combinaison incertaine **reste signalée manquante** | **ECART** | (a) L'appariement 1-1 tranche : graine 10, deux paiements TOTAL de 59,58 € et une facture du 23/03. La facture est donnée au paiement du 30/03 (confiance élevée, JUSTIFIE) ; celui du 10/04, dont le libellé porte « 23/03 », reste MANQUANT. Résultat : un manque raté et une fausse alerte (E5). (b) Une décision incertaine devient A_VERIFIER, **jamais relancée** (architecture §7), au lieu de « signalée manquante ». |
| 6.10 | Ordre déterministe, sorties identiques au bit près | CONFORME | Voir CA-08 et `test_cadence.py::test_determinisme_*`, `test_routage.py::test_determinisme`. |

### §7 Routage des pièces entrantes

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| 7.1 | Idempotence sur empreinte + message ; un e-mail rejoué ne crée rien | CONFORME | `test_routage.py::test_rejeu_donne_doublon` (M17 tuée). E8 : 3 copies d'un même `.eml` donnent 1 seule entrée de file. |
| 7.2 | Dossier : adresse exacte ; à défaut, domaine rattaché à un seul dossier ; deux candidats ou plus vont en file, sans exception | CONFORME (plus strict) | `test_adresse_exacte_dans_deux_dossiers_ambigu`, `test_domaine_declare_par_deux_dossiers_ambigu`, `test_contacts_identiques_apres_suffixe_plus_ambigu` (M14 tuée). Le domaine doit être **déclaré** et ne jamais être grand public : c'est plus prudent que le cahier. |
| 7.3 | Période ouverte ; si deux, la date de la pièce arbitre ; si elle manque, file non routée | CONFORME | `test_periode_arbitree_par_le_nom`, `test_periode_ambigue`. Effet de bord mesuré : voir C7. |
| 7.4 | Opération : montant compatible dans une fenêtre de −60 à +15 jours ; exactement un candidat donne une proposition, zéro ou plusieurs vont en file | PARTIEL | `test_bornes_de_la_fenetre` (M16 tuée), `test_candidats_multiples` (M15 tuée). Mais la fenêtre ne s'applique **que si une date est lisible dans le nom du fichier** (architecture §4.4). |
| 7.5 | Jamais de rattachement inter-dossiers, non contournable | PARTIEL | Côté automatique : CONFORME. Retirer **les deux** filtres sur le dossier (M12+M13) fait échouer 18 tests, dont `test_cycle.py::test_ca03_jamais_de_rattachement_inter_dossiers` et `test_routage.py::test_expediteur_de_a_ne_vise_jamais_b_meme_seul_candidat`. E7 : 0 sur 100 pièges. Côté humain : le contournement est possible sans alerte (voir 5.8). |
| 7.6 | Aucune pièce perdue : tout ce qui n'est pas routé apparaît en file **avec son e-mail d'origine** | PARTIEL | Toute décision va en file, tout `.eml` illisible en quarantaine (`test_adversarial.py::test_sain_quarantaine_fichiers_pathologiques`). La file ne montre pas l'objet, et l'expéditeur n'apparaît que dans le texte `detail` des refus « dossier inconnu » ou « ambigu ». L'e-mail ne se retrouve que par son Message-ID dans `entrant/`. |
| 7.7 | Le rattachement reste une proposition tant qu'un humain ne l'a pas confirmé | CONFORME | La garde `confirme_par` refuse une valeur vide ou un acteur automatique (`test_etats.py::test_garde_humaine_*`, M25 tuée). Un statut NON_ROUTEE n'est jamais confirmé implicitement (`test_ca03_...`). Le « clic dans le tableau » est remplacé par la commande `rattacher`. |

### §8 Référentiel d'exclusions

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| 8.1 | Deux niveaux : règles générales, règles apprises par dossier | CONFORME | `exclusions.Referentiel`. `test_exclusion_apprise_par_dossier`. |
| 8.2 | Cycle de vie : PROPOSÉE → VALIDÉE PAR LE RESPONSABLE → ACTIVE → RÉVISÉE OU RETIRÉE | **ECART** | Absent : ni statut, ni commande, ni table. Le cycle applique toute règle du CSV avec `regle_validee=True` d'office (`cycle.py` lignes 714 à 716) : la pièce passe à HORS_PERIMETRE, état terminal. |
| 8.3 | Une règle proposée s'applique au cycle suivant mais reste signalée « provisoire » | **ECART** | Aucune notion de règle provisoire. |
| 8.4 | Toute règle porte son auteur et sa date ; une exclusion abusive se remonte à son auteur | **ECART** | Voir 5.7 : impossible. |
| 8.5 | Le référentiel contient ses contre-exemples (crédit-bail) | CONFORME | `exclusions.CONTRE_EXEMPLES`. Sonde : « ECHEANCE CREDIT BAIL VEHICULE » et « ECH CREDIT BAIL N 4471 » ne sont pas exclues, alors que la règle `emprunts` les capterait sans le contre-exemple. |
| 8.6 | **Le jeu de test couvre explicitement les contre-exemples** | **ECART** | `test_credit_bail_exige_une_facture_malgre_le_libelle_d_echeance` **passe pour une autre raison**. Son libellé (« PRLV SEPA SG CREDIT BAIL ECHEANCE 04 ») ne déclenche **aucune** règle, même sans contre-exemple (sonde : `regle_qui_matcherait_sans=[]`). M09b (contre-exemples supprimés) survit à **toute** la suite. Le libellé du corpus (« PRLV SEPA SG CREDIT BAIL ID… ») ne déclenche rien non plus. |

### §9 Machines d'états

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| 9.1 | Transitions de la pièce attendue et leurs gardes (10 lignes) | CONFORME (étendu) | `etats.TABLE`. `test_etats.py::test_la_table_de_donnees_egale_la_table_du_contrat`, `::test_ensemble_des_couples_autorises_egal_au_contrat`. Ces tests comparent au contrat d'architecture §4.3, qui **étend** le cahier : ATTENDUE→RECUE, exclusion depuis PROMISE ou ESCALADEE, ESCALADEE→RECUE. La ligne « DEMANDÉE + délai dépassé + compteur < plafond → DEMANDÉE » est assurée par `cadence.planifier` (relance suivante). |
| 9.2 | Garde « relance validée par un humain » | CONFORME | `test_garde_humaine_refuse_les_acteurs_automatiques` (M25 tuée). |
| 9.3 | Garde d'exclusion : « règle validée, applicable au dossier » | PARTIEL | La garde existe (`test_exclusion_exige_regle_validee_strictement_vraie`), mais le cycle la satisfait d'office pour toute règle du CSV (8.2). |
| 9.4 | Effet de la promesse : délai reporté à la date promise + 2 j | CONFORME | `test_cadence.py::test_promesse_borne_date_promise_plus_2_jours_ouvres`, `::test_promesse_avec_ferie_dans_le_delai`. |
| 9.5 | Effet de l'escalade : alerte au responsable | PARTIEL | Seulement `sortie/escalades.csv` réécrit à chaque cycle (`test_cycle.py::test_evolution_sur_plusieurs_jours_jusqu_a_l_escalade`). Personne n'est prévenu. |
| 9.6 | Effets « relance, ton adapté » (promesse non tenue) et « relance avec motif du rejet » (pièce non conforme) | **ECART** | `relances.construire_brouillon` ne connaît ni la promesse non tenue ni le motif du rejet : le texte est le même que pour une relance ordinaire. |
| 9.7 | « Bloqué » est un drapeau, pas un état | CONFORME | `PieceAttendue.bloquee`. `test_blocage_signale_et_leve_gardent_l_etat`. |
| 9.8 | L'état de la période est calculé, jamais saisi | CONFORME | `test_etat_periode_est_calcule_pas_stocke`. |
| 9.9 | États de période OUVERTE / EN COLLECTE / COMPLÈTE / CLÔTURÉE (clôture prononcée par le comptable) | PARTIEL | La fonction est juste (`test_periode_*`, 10 tests). Mais **aucune commande de clôture** (grep `clotur` hors `etats` et `modeles` : 0 résultat) : CLÔTURÉE est inatteignable. OUVERTE n'est jamais produite par le cycle. Une période sans pièce attendue n'apparaît pas dans `periodes.csv`. |
| 9.10 | Les délais vivent dans la configuration du dossier, source unique | PARTIEL | Ils sont bien hors de la machine d'états. Mais ce sont des **constantes de code** (`cadence.JALONS`, `JALON_ESCALADE`), pas un paramètre du dossier. Une constante morte `relances.CADENCE = (0, 3, 7, 14)` traîne en double. |

### §10 Calendrier et garde-fous

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| 10.1 | T0 : demande initiale groupée | CONFORME | `test_piece_jamais_demandee_due_tout_de_suite`. Le groupement se fait par destinataire. |
| 10.2 | T+3 j ouvrés : relance 1 | ECART (assumé) | Jamais exécutée (architecture §7). E8 : envois le 05/10, le 12/10 et le 19/10. |
| 10.3 | T+7 j ouvrés : relance 2 | PARTIEL (assumé) | Elle part à J+14 calendaires (E8). |
| 10.4 | T+14 j ouvrés : escalade | CONFORME | `test_escalade_a_14_jours_ouvres`. E8 : 23/23 escaladées. |
| 10.5 | J−5 de l'échéance TVA : relance prioritaire, ton renforcé | PARTIEL (assumé) | Le ton passe à « urgent » quand l'échéance est à 5 jours ou moins (M32 tuée par `test_brouillon_un_dossier_equivalent_a_construire_relance_niveau_2_et_urgent`). Il n'y a aucune relance prioritaire : le garde-fou hebdomadaire s'applique quand même. |
| 10.6 | Jalons paramétrables | **ECART** | Constantes de code (9.10). |
| 10.7 | Un seul e-mail par destinataire et par semaine, tous dossiers confondus | CONFORME | La règle est appliquée deux fois : à la planification (`test_limite_hebdomadaire_6_jours_reporte_7_jours_autorise`, M21 tuée) et à l'envoi (`test_limite_hebdo_deux_brouillons_meme_jour_un_seul_part`, M20 tuée). E8 : au moins 7 jours entre deux envois. |
| 10.8 | Aucun envoi hors jours et heures ouvrés **du fuseau du destinataire** | PARTIEL (assumé) | Créneau 8 h à 18 h les jours ouvrés (`test_creneau_bornes_heure_locale`, M23 tuée ; `test_jour_non_ouvre_reporte_tout_mais_garde_les_transitions`, M22 tuée). Le fuseau est fixé à Europe/Paris. |
| 10.9 | Jamais deux relances pour la même pièce dans le même cycle | CONFORME | `test_cycle.py::test_brouillon_en_attente_bloque_un_second_brouillon` (M29 tuée) ; identifiant de relance déterministe. |
| 10.10 | Plafond de 4 relances par pièce, puis escalade obligatoire | CONFORME (inatteignable) | `etats.PLAFOND_RELANCES = 4`, `test_au_plafond_la_relance_est_refusee` (M24 tuée). En pratique, au plus 3 envois (`test_jamais_plus_de_trois_envois_planifies` ; E8 : `nb_relances` max = 3). Voir C2. |

### §11 Critères d'acceptation (détail au §3)

| ID | Statut MVP de test | Preuve |
|---|---|---|
| CA-01 | Non prouvable ici (exige des données réelles) | Synthétique : 0,00 % sur le corpus par défaut |
| CA-02 | **ECART** (échoue sur le synthétique) | 2 corpus sur 20 et le banc de 5 000 opérations : manques ratés différents de 0 |
| CA-03 | CONFORME (automatique) / PARTIEL (humain) | M12+M13 tuées ; E7 : 0 inter-dossiers |
| CA-04 | Non prouvable ici (exige des données réelles) | Synthétique favorable : 92,0 % |
| CA-05 | CONFORME | Tests et E8 |
| CA-06 | CONFORME (structure) / PARTIEL (protocole) | Tests et E8 ; aucun outil de contrôle du journal |
| CA-07 | CONFORME | Tests, M30 tuée |
| CA-08 | PARTIEL | Tests verts, mais pas d'intégration continue et export non reproductible |
| CA-09 | CONFORME | E6 : 7,97 s |
| CA-10 | CONFORME | Tests, M28b tuée, E9 : 7 sur 7 |
| CA-11 | CONFORME (partie technique) | Export de 0,15 s, schéma inclus |
| CA-12 | NON APPLICABLE EN MODE TEST | |

### §12 Exigences non fonctionnelles

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| 12.1 | Idempotence par empreinte sur toute entrée : opération, e-mail, pièce | PARTIEL | E-mail : oui (7.1). Opération : upsert sur (dossier, référence), pas sur une empreinte. Inventaire des pièces : relu en entier, par `id_piece`. |
| 12.2 | Un cycle interrompu se rejoue sans effet de bord | CONFORME | `test_ca10_panne_au_milieu_du_cycle_puis_reprise` (6 points), M28b tuée, E9 : 7 sur 7. |
| 12.3 | File d'attente simple et fichier des événements non traitables | CONFORME | `file_humaine`, `sortie/quarantaine.csv` (`test_sain_quarantaine_fichiers_pathologiques`). |
| 12.4 | Horodatage et acteur sur chaque écriture | PARTIEL | Voir 5.11. |
| 12.5 | **Tests automatisés sur le moteur et le routage, exécutés à chaque modification** | **ECART** | Il n'y a **pas d'intégration continue** : pas de `.github/`, pas de configuration CI ni de hook versionné (`git ls-files` ne liste aucun fichier de CI). Les tests ne tournent qu'à la main. De plus, `python3 -m pytest`, la commande du README, échoue sous le Python 3.11 de la machine. |
| 12.6 | Instance et stockage isolés (D4) | PARTIEL | Voir D4. |
| 12.7 | Chiffrement au repos et en transit | ECART (déploiement) | `depot.sqlite` et `audit.jsonl` sont en clair ; aucune émission réseau. |
| 12.8 | Secrets hors du dépôt, dans un gestionnaire | HORS PERIMETRE MVP | Aucun secret n'existe tant que les connecteurs sont hors périmètre. |
| 12.9 | Accès nominatifs, second facteur pour l'administration | ECART (assumé) | `validateurs.txt` n'est pas authentifié (architecture §7). Une validation forgée en SQL part (xfail `test_defaut_validation_forgee_en_sql_par_un_inconnu_part`). |
| 12.10 | Journal d'audit protégé contre la modification | PARTIEL | La modification est **détectée** : `test_audit.py` (90 tests) ; M26c (contrôle de chaîne retiré dans `verifier`) est tuée par `test_entree_modifiee_avec_hash_recalcule_rompt_la_suite`. Mais rien ne l'**empêche**, et une modification de la dernière entrée suivie de la suppression de celle qui suit n'est pas détectée (sonde E10 : `ok=True`). Limite assumée, sans ancrage externe. |
| 12.11 | Sauvegardes chiffrées, restauration testée chaque mois | ECART (déploiement) | Rien. L'export et l'import JSON existent (CA-11). |

### §13 Conformité et sous-traitance

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| 13.1 | Chaîne RGPD écrite (responsable, sous-traitants, article 28) | NON APPLICABLE EN MODE TEST | Données fictives |
| 13.2 | Hébergement dans l'UE, nommé au contrat | NON APPLICABLE EN MODE TEST | |
| 13.3 | Minimisation : aucune transmission à un tiers | NON APPLICABLE EN MODE TEST | De fait respectée : aucun appel réseau (D2.a) |
| 13.4 | Durées de conservation paramétrables, suppression prouvable | NON APPLICABLE EN MODE TEST | Aucun mécanisme. À noter : `entrant/` n'est jamais purgé |
| 13.5 | INPDP et transferts internationaux | NON APPLICABLE EN MODE TEST | |
| 13.6 | Anonymisation en phase d'essai | NON APPLICABLE EN MODE TEST | Corpus fictif |
| 13.7 | Revue juridique (livrable de P1) | NON APPLICABLE EN MODE TEST | |

### §14 Mesure du ROI

| ID | Exigence | Statut | Preuve |
|---|---|---|---|
| 14.1 | Le temps économisé n'est jamais un paramètre réglable | CONFORME | Aucun paramètre de ce type (grep `roi`, `chronom`, `temps_`, `baseline` : 0 résultat). |
| 14.2 | Référence chronométrée deux semaines avant déploiement, relevés complémentaires, nouvelle mesure à 60 jours | HORS PERIMETRE MVP | Processus du cabinet sur données réelles (phase P2 : « baseline ROI gelée »). |
| 14.3 | **Le produit conserve la méthode de calcul et les valeurs d'entrée** | **ECART** | **Le MVP ne fournit rien** : ni saisie de la référence, ni stockage des valeurs d'entrée, ni calcul (grep : 0 résultat). `rapport.statistiques` calcule un `taux_automatisation`, qui n'est pas la mesure du §14. |
| 14.4 | Mesurer le nombre de dossiers suivis par collaborateur sans embauche | **ECART** | Aucun attribut « collaborateur » dans le modèle. |

### §16 Feuille de route : livrables de P1

| ID | Livrable de P1 | Statut | Preuve |
|---|---|---|---|
| 16.1 | Routage e-mail | PARTIEL | Depuis des `.eml` sur disque (3.4). |
| 16.2 | Machines d'états | CONFORME | §9 |
| 16.3 | Sheets / Gmail / Drive | HORS PERIMETRE MVP | |
| 16.4 | Journal d'audit | CONFORME | `audit.py`, 90 tests, limites assumées |
| 16.5 | VPS dans l'UE | NON APPLICABLE EN MODE TEST | |
| 16.6 | Accord de sous-traitance | NON APPLICABLE EN MODE TEST | |
| 16.7 | Revue juridique | NON APPLICABLE EN MODE TEST | |
| 16.8 | Sortie de P1 : CA-03 à CA-12 atteints | PARTIEL | Voir §3 : CA-04 non mesurable ici, CA-08 partiel, CA-12 non applicable |
| 16.9 | Sortie de P1 : un cycle hebdomadaire tourne sans intervention de l'éditeur | PARTIEL | Le cycle est rejouable par cron (README, « Déploiement prévu »), mais aucun ordonnanceur n'est fourni. |

### §17 État du code existant : affirmations vérifiées

| ID | Affirmation | Statut | Preuve |
|---|---|---|---|
| 17.1 | « Moteur d'appariement : aucun écart connu » | **ECART** | 6.6 et 6.9 (E5) |
| 17.2 | « Normalisation : alias fournisseurs » | PARTIEL | `Moteur(alias=…)` existe, mais `alias_fournisseurs.csv` n'est lu nulle part (grep `alias` dans `scripts/` et `cycle.py` : 0 résultat). |

**Décompte de la matrice** (115 lignes, sans le résumé du §11, compté par script sur ce fichier) : CONFORME 47, PARTIEL 33, ECART 21, HORS PERIMETRE MVP 4, NON APPLICABLE EN MODE TEST 10. Deux des CONFORME (6.3, 6.4) ne le sont que par sonde : aucun test ne les garde.

## 3. Les douze critères d'acceptation

« Prouvé par un test » : le test existe, passe seul, et il est tué par la mutation correspondante. « Synthétique » : chiffres exacts des exécutions du §1. Rappel du cahier (§11) : « les seuils sont mesurés sur données réelles du cabinet, **jamais sur le corpus synthétique** ; celui-ci sert à détecter les régressions, pas à prouver l'exactitude ».

| CA | Prouvé par un test automatique | Mesurable sur le corpus synthétique (chiffres exacts) | Exige des données réelles |
|---|---|---|---|
| **CA-01** Fausses alertes < 2 % | **Non.** Les tests unitaires d'exclusion (`test_operations_sans_piece_justificative`, etc.) ne mesurent pas un taux. | Corpus par défaut : **0/261 (0,00 %)**. Balayage de 20 corpus : 18 à 0, et 1 fausse alerte sur 278 (0,36 %) pour la graine 4 / 0,18 et sur 265 (0,38 %) pour la graine 10 / 0,35. Banc de 5 000 opérations : **1/4 643 (0,02 %)**. | **Oui, entièrement.** Le corpus vient du même auteur que le moteur : il ne contient que les libellés que les règles savent reconnaître. **Le synthétique ne prouve rien sur CA-01.** Il montre seulement que le moteur reconnaît ses propres gabarits. |
| **CA-02** Manques ratés = 0 | **Non.** | Corpus par défaut : 0/23. **Mais 2 corpus sur 20 échouent** : graine 4 / 0,18 donne **1 manque sur 24 (4,17 %)**, graine 10 / 0,35 donne **1 manque sur 37 (2,70 %)**, et `evaluer.py` sort avec le code 1. Banc de 5 000 opérations : **2 manques sur 357 (0,56 %)**. Cause : 6.6 et 6.9. | **Oui, entièrement.** Le synthétique ne peut pas prouver CA-02. Il prouve en revanche le contraire : **le moteur rate des manques même sur ses propres données**, ce qui est une régression détectée. |
| **CA-03** Inter-dossiers = 0 | **Oui** pour le routage automatique : `test_cycle.py::test_ca03_jamais_de_rattachement_inter_dossiers`, `test_routage.py::test_expediteur_de_a_ne_vise_jamais_b_meme_seul_candidat`, `::test_propriete_jamais_inter_dossiers`, `test_adversarial.py::test_sain_rattacher_ne_traverse_pas_les_dossiers`. M12+M13 sont tuées par 18 tests. **Non** pour la confirmation humaine : aucune garde (5.8). | E7 : **0** rattachement entre dossiers sur 184 propositions ; **100/100** pièges non routés (94 « aucun candidat », 6 « période ambiguë »). | Oui : 200 pièces réelles sur au moins 10 dossiers, avec des expéditeurs réels (adresses partagées, relais, collègues). |
| **CA-04** Routage automatique ≥ 85 % | **Non** (aucun test de taux). | E7, e-mails favorables (contact exact, montant d'une pièce attendue, date dans le nom) : **184/200 = 92,0 %**. Les 16 non routés se répartissent en 11 « aucun candidat » (date de la facture dans le mois précédant le paiement, voir C7), 4 « candidats multiples » et 1 « période ambiguë ». E6, e-mails dont le montant correspond à une opération quelconque : 21/300 (7 %), chiffre non significatif. C-RELAIS : **0 % par construction**. | **Oui, entièrement.** Le 92 % mesure des e-mails écrits pour réussir. Il ne dit rien des vrais clients (montant absent, PDF scanné, plusieurs factures, relais). |
| **CA-05** Doublons d'envoi = 0 sur 30 jours | **Oui** : `test_cycle.py::test_ca05_rejeu_meme_jour_memes_brouillons`, `::test_ca05_envoyer_deux_fois_un_seul_eml`, `test_envoi.py::test_deux_appels_successifs_un_seul_envoi`, `::test_un_brouillon_en_cours_n_est_jamais_renvoye`, `test_adversarial.py::test_sain_deux_processus_meme_brouillon_un_seul_eml`. M19, M20 et M28b sont tuées. | E8 (30 jours) : **9 envois, 9 `.eml`, 9 identifiants distincts**, 3 copies d'un e-mail entrant donnent **1** entrée, au moins **7 jours** entre deux envois vers un même destinataire. | Oui, pour le journal d'envoi réel (Gmail, hors périmètre). |
| **CA-06** Envois sans validation = 0 | **Oui** (garantie structurelle) : `test_envoi.py::test_envoyer_un_brouillon_non_valide_est_refuse`, `::test_envoyer_refuse_un_statut_validee_forge_sans_humain`, `::test_un_seul_site_d_emission_dans_le_code`, `test_adversarial.py::test_sain_cli_par_truque_et_envoi_sans_validation`. M18 et M25 sont tuées. Limite : une validation forgée en SQL part (xfail strict). | E8, contrôle exhaustif du journal par script : **9/9** `envoi_effectue` précédés d'un `valide` par un validateur déclaré, **0** exception. **Le produit ne fournit pas ce contrôle** : `verifier-audit` ne vérifie que la chaîne de hachage. | Oui : le protocole porte sur la période de recette réelle. |
| **CA-07** Lignes reliées au relevé = 100 % | **Oui** : `test_cycle.py::test_ca07_lignes_reliees_au_releve`, `test_cadence.py::test_ca07_chaque_ligne_a_une_piece_source_et_inversement` (60 cas), `::test_ca07_reference_sans_piece_source_est_refusee`, ce dernier étant le contrôle bloquant à la génération. M30 (ligne inventée) est tuée. | E6 : 20 brouillons générés sans anomalie. | Non. |
| **CA-08** Sorties identiques (empreinte), en IC | **Oui pour les sorties** : `test_cycle.py::test_ca08_deux_instances_sorties_identiques`, `test_adversarial.py::test_sain_ca08_independant_du_fuseau_de_la_locale_et_du_hash`. **Non pour l'export** (xfail `test_defaut_depot_export_non_reproductible`). **Non « en intégration continue »** : il n'y en a pas (12.5). | E6 : deux instances neuves sur le banc de 5 000 opérations donnent **29 fichiers identiques** (`sortie/`, `audit.jsonl`, empreintes SHA-256 comparées). Le rejeu sur la même instance crée 0 pièce et 0 brouillon, avec 300 doublons. | Non. |
| **CA-09** 20 dossiers / 5 000 opérations < 10 min | **Oui** : `test_adversarial.py::test_sain_ca09_5000_operations_20_dossiers_100_emails`, **8,65 s** lancé seul. Il porte sur un relevé simplifié, sans aucune opération hors périmètre. | E6, banc réaliste (générateur du dépôt, 20 dossiers × 250 opérations, 1 862 pièces, 300 e-mails) : **7,97 s** au premier cycle, **7,31 s** au rejeu, 0 anomalie. Limite : `entrant/` n'est jamais purgé (environ 34 ms par message, plus de 10 min vers 17 000 messages selon l'architecture §7). | Non (à refaire sur la machine de production). |
| **CA-10** Reprise après panne : aucune perte, aucun doublon | **Oui** : `test_cycle.py::test_ca10_panne_au_milieu_du_cycle_puis_reprise` (6 points d'injection), `::test_ca10_panne_dans_sauver_brouillon`, `::test_ca10_panne_entre_envoi_effectif_et_repercussion`, `::test_ca10_panne_pendant_la_repercussion_tout_ou_rien`. M28b (idempotence de répercussion retirée) fait échouer 14 tests. | E9, vraie interruption par SIGKILL à 7 instants : **7/7** dépôts identiques à la référence, journal intègre. Limite assumée : un crash entre COMMIT et écriture du journal laisse un effet sans trace. | Non. |
| **CA-11** Export complet < 1 h, format documenté | **Oui** : `test_cycle.py::test_ca11_export_complet_relu_sans_perte`, `test_depot.py::test_export_puis_import_reproduit_tout`, `::test_export_fichier_relu_schema_present`. | E6 : `exporter` en **0,15 s**, 435 482 octets, schéma dans la clé `schema`. Le journal d'audit n'est pas dans l'export (fichier séparé). | Oui, pour le « test de restitution trimestriel » chez le cabinet. |
| **CA-12** Données hors UE = 0 | NON APPLICABLE EN MODE TEST | | Inventaire des flux et contrat d'hébergement. |

**Ce que le corpus synthétique prouve** : la logique tient sur les cas que son auteur a imaginés, le cycle est déterministe et rejouable, la chaîne validation → envoi ne fuit pas, et la performance est large (environ 75 fois sous la cible de CA-09).

**Ce qu'il ne prouve pas** : l'exactitude. CA-01, CA-02 et CA-04 ne sont **pas démontrables sur données fictives**, et aucun chiffre de ce dépôt ne peut être présenté comme une mesure de ces critères. Le seul résultat robuste du synthétique sur CA-02 est **négatif** : le moteur rate des manques sur ses propres données.

## 4. Écarts classés

### BLOQUANT pour un MVP stable (1)

**B1 : l'appariement tranche des cas ambigus et rate des pièces manquantes (§6, CA-02).**
- **Exigence** : §6, « cas ambigu : jamais tranché automatiquement… ne pas le relancer coûte une déclaration incomplète » ; « couples les plus sûrs en premier » ; CA-02 « manques ratés = 0 », critère bloquant.
- **Ce que fait le code** : `moteur._passe_appariement` trie par score, où `score_montant` vaut 0 ou 1. Un montant exact et un montant dans la tolérance pèsent donc pareil, et la date départage. Quand deux paiements se disputent une facture, l'un des deux est JUSTIFIE avec une forte confiance, sans signaler l'ambiguïté.
- **Impact** : le produit cesse de réclamer une pièce réellement manquante. C'est observé sur 2 corpus synthétiques sur 20 et sur 2 des 357 pièces manquantes du banc. Le README parle de « 100 % … un cas à 99,5 % », et ce cas à 99,5 % est justement un manque raté.
- **Correction recommandée** :
  1. Classer l'exactitude du montant (écart nul avant écart toléré) avant la date.
  2. Quand deux opérations ont des candidats de score égal ou voisin pour une même pièce, classer les deux en MANQUANT (ou les bloquer avant toute relance), sans trancher.
  3. Ajouter à la suite un test de non-régression sur les deux reproductions (OP-A/OP-B ; TOTAL 59,58 €) et un balayage de graines de `evaluer.py`.

### A CORRIGER (18)

| # | Exigence | Ce que fait le code | Impact (une phrase) | Correction recommandée |
|---|---|---|---|---|
| A1 | D6, §15 : circuit obligatoire, C-RELAIS par défaut | Circuit absent ou vide : C-DIRECT (`parseurs.py` ligne 186) | Un dossier mal saisi écrit directement au client final, exactement ce que D6 interdit. | Exiger la colonne et la valeur (`ErreurFormat`), ou appliquer C-RELAIS par défaut. |
| A2 | §12 : tests à chaque modification ; CA-08 « en IC » | Aucune intégration continue ; `python3 -m pytest` échoue sous Python 3.11 | Avec plusieurs experts qui modifient le dépôt en même temps, une régression peut passer sans que personne ne la voie. | Workflow CI (suite complète, Python 3.11 et 3.13, plus le balayage `evaluer.py`) ; `pytest` en dépendance de développement installée. |
| A3 | §8 et §5.7 : cycle de vie, auteur, date, règle provisoire ; §9 garde « règle validée » | CSV à 3 colonnes appliqué avec `regle_validee=True` d'office | Une regex abusive clôt des pièces à HORS_PERIMETRE (état terminal) sans auteur traçable, ce qui fait un manque raté silencieux. | Colonnes `nom`, `auteur`, `date_activation`, `statut`. Une règle PROPOSÉE ne produit qu'un drapeau « provisoire » (pas d'état terminal) jusqu'à validation par le responsable. |
| A4 | §8 : contre-exemples couverts par les tests | Le test crédit-bail passe pour une autre raison (M09b survit) | La protection du contre-exemple n'est gardée par aucun test : elle peut disparaître sans alerte. | Tester « ECHEANCE CREDIT BAIL VEHICULE » (exclue sans le contre-exemple, non exclue avec) ; ajouter ce libellé au générateur de corpus. |
| A5 | §6 : règles chiffrées sous test | M01c (0,5 %), M03c (groupé au centime), M08 (ordre des exclusions), M04 (indice) survivent | Quatre règles du §6 peuvent changer sans qu'aucun test ne rougisse. | Tests de bornes : 1 005,00 / 1 005,01 sur 1 000 € ; groupé 750,00 / 750,01 ; règle apprise contre règle générale ; 3 pièces sans indice. |
| A6 | §6 : groupés au centime avec indice obligatoire | 0,02 € de tolérance ; indice exigé seulement à 2 pièces | Un total fortuit de 3 ou 4 factures peut masquer une pièce manquante (classée A_VERIFIER, jamais relancée). | Tolérance nulle et indice toujours exigé, ou amender le cahier après mesure sur réel. |
| A7 | §7 règle 5, §5 invariant 1 : jamais d'inter-dossiers | `rattacher_piece` accepte un `--dossier` différent du dossier routé (sonde E10) | Une faute de frappe d'un humain rattache la pièce d'un client au dossier d'un autre, sans alerte : c'est le cas CA-03. | Refuser si le dossier diffère de celui de la décision, sauf `--forcer` avec motif journalisé. |
| A8 | §5.5, §7.6 : PieceRecue complète, file « avec son e-mail d'origine » | Ni expéditeur, ni date de réception, ni fichier conservés | L'humain qui traite la file ne voit pas qui a envoyé la pièce, et la pièce n'existe que tant que `entrant/` n'est pas purgé. | Persister `expediteur`, `recu_le`, `objet` et une copie du fichier (clé SHA-256) ; les afficher dans `file`. |
| A9 | §5 : invariants portés par la base | Contrôles dans le code seulement ; aucune clé étrangère | Le cahier demande une garantie que le code applicatif ne puisse pas contourner. | Table de liaison `rattachements(dossier, reference, empreinte)` avec contrainte de dossier ; triggers ou clés étrangères. |
| A10 | §5.4 : motif de création de la pièce attendue | Non stocké | On ne peut pas expliquer après coup pourquoi une pièce a été réclamée sans rejouer le moteur. | Champs `regle` et `motif` dans `pieces` et dans l'audit `piece_creee`. |
| A11 | §9 : effets « ton adapté », « motif du rejet », « alerte au responsable » | Texte de relance identique dans tous les cas ; escalade en CSV seulement | Le client relancé après un rejet ne sait pas ce qui ne va pas avec sa pièce. | Passer le contexte (promesse non tenue, motif) à `construire_brouillon` ; produire un brouillon d'alerte adressé au responsable. |
| A12 | §9 : états de période, trimestre | Pas de clôture, OUVERTE jamais calculée, mois seulement | Une période ne peut jamais être déclarée close dans l'outil. | Commande `cloturer` (validateur), périodes dérivées du relevé (avec 0 pièce), périodicité par dossier. |
| A13 | §9, §10 : délais paramétrables par dossier | Constantes de code ; doublon mort `relances.CADENCE` | Il faut modifier le code pour adapter la cadence à un client. | Colonnes de cadence dans `dossiers.csv`, bornées par les garde-fous non paramétrables. |
| A14 | §4 : rôles et droits | Une seule liste plate de validateurs | Un comptable peut arbitrer et classer sans suite ce que le cahier réserve au responsable. | Deux listes (`responsables.txt`, `validateurs.txt`) ; `arbitrer` et la validation de règle réservés aux responsables. |
| A15 | §17, README, document commercial : alias fournisseurs | `alias_fournisseurs.csv` n'est lu nulle part | Une fonction annoncée n'est pas branchée, et les libellés abrégés sont moins bien appariés. | Lecteur d'alias dans `lire_entrees` et `evaluer.py`, avec test. |
| A16 | §1 : OFX et CAMT.053 | CSV pivot seul | Un cabinet sans export CSV ne peut pas démarrer. | Convertisseurs vers le pivot, ou amender le cahier (CSV pivot exigé en v1). |
| A17 | CA-06 : contrôle exhaustif du journal | `verifier-audit` ne vérifie que la chaîne | Le protocole de recette de CA-06 n'est pas outillé, et la falsification SQL (xfail) passerait la recette. | Commande `controler-envois` : chaque `envoi_effectue` doit avoir un `valide` antérieur par un validateur déclaré, avec le même destinataire. |
| A18 | D6 : signature du cabinet | Corps sans signature ; `From` non paramétrable | Le message ne porte pas l'identité du cabinet expéditeur. | Champs `signature` et `expediteur` par instance, injectés par l'adaptateur. |

### ASSUME : déjà documenté comme limite (12)

| # | Exigence | Limite documentée | Où |
|---|---|---|---|
| S1 | §10 T+3 | Jamais exécuté, le garde-fou hebdomadaire gagne | architecture §4.5 et §7 |
| S2 | §10 J−5, fuseau du destinataire | Pas de relance prioritaire ; Europe/Paris fixe | architecture §7 |
| S3 | §5 clé dossier + référence | Références uniques entre dossiers exigées, collisions refusées | architecture §7 ; xfail moteur |
| S4 | §12.9 identité | Validateurs non authentifiés ; validation forgée en SQL | architecture §7 ; xfail |
| S5 | §12.10 journal | Suppression des dernières entrées indétectable ; journal et base non atomiques | architecture §7 |
| S6 | CA-08 export | `vu_le` à l'horloge murale | xfail `test_defaut_depot_export_non_reproductible` |
| S7 | §6 « reste signalée manquante » | PARTIEL et A_VERIFIER jamais relancés | architecture §7 |
| S8 | CA-04 | Aucun routage automatique en C-RELAIS ni sur adresse grand public | architecture §7 |
| S9 | §7 fenêtre | Fenêtre −60/+15 appliquée seulement si le nom du fichier porte une date | architecture §4.4 |
| S10 | divers | Suffixe +tag incohérent, déblocage mémorisé par pièce, isolation (`ulimit -v`, descripteurs hérités), MemoryError déguisée | 5 xfail stricts ; architecture §7 |
| S11 | CA-09 dans la durée | `entrant/` jamais purgé | architecture §7 |
| S12 | §9 | ESCALADÉE sans retour vers DEMANDÉE | architecture §7 |

### HORS PERIMETRE (4)

| # | Exigence | Raison |
|---|---|---|
| H1 | D5, 3.4, 7.7 : Gmail, Drive, Sheets (boîte de collecte, clic de confirmation) | Décidé avec l'utilisateur : aucun identifiant disponible |
| H2 | §14.3 et §14.4 : conservation de la méthode et des valeurs du ROI | **Non fourni par le MVP.** Relève de P2 (« baseline ROI gelée »), mais devra exister avant tout pilote payant |
| H3 | §12.7, 12.11 et 12.6 : chiffrement, sauvegardes avec restauration testée, isolation physique | Infrastructure de déploiement (P1 sur VPS). Classement **proposé par l'auditeur**, à confirmer : l'utilisateur ne l'a pas déclaré hors périmètre |
| H4 | 16.9 : ordonnancement du cycle hebdomadaire | Une ligne de cron, à poser au déploiement |

Non comptés comme écarts : le §13, CA-12 et les livrables juridiques de P1 (VPS UE, accord de sous-traitance, revue juridique), soit 10 lignes `NON APPLICABLE EN MODE TEST`.

## 5. Contradictions

### Le cahier contre lui-même

| # | Contradiction | Qui a raison selon l'auditeur |
|---|---|---|
| C1 (connue) | §10 : relance à T+3 j ouvrés, contre « un e-mail par destinataire et par semaine ». Le jalon T+3 ne peut jamais s'exécuter. | **Le garde-fou**, non paramétrable et anti-harcèlement (risque du §15). Il faut réécrire les jalons. |
| C2 (connue) | §10 : « plafond de 4 relances » contre une cadence de 3 envois (T0, T+3, T+7) suivie d'une escalade à T+14. Le plafond est inatteignable (E8 : maximum 3). | **La cadence.** Fixer le plafond à 3 envois, ou ajouter un jalon ; sinon le plafond est une règle morte. |
| C3 | §5 invariant « une relance ne référence que des pièces d'un même dossier **et** d'un même destinataire », contre §10 « un seul e-mail par destinataire, **tous dossiers confondus** » et contre la ligne T0 « demande groupée **par dossier** ». | **Le §10.** Le code regroupe par destinataire, avec une section par dossier. Il faut réécrire l'invariant en « même destinataire ; chaque ligne rattachée à sa pièce et à son dossier ». |
| C4 | §8 « une règle **proposée** s'applique dès le cycle suivant », contre §9 « ATTENDUE → HORS PÉRIMÈTRE, garde : **règle validée** ». | **Le §9.** Un état terminal ne doit pas venir d'une règle non validée. Une règle proposée ne doit que suspendre la relance (drapeau « provisoire »). |
| C5 | §5 « PieceRecue, clé : empreinte SHA-256 », contre §7 « clé = empreinte du fichier **+ identifiant du message** ». | **Les deux, à des niveaux différents.** L'identité de la pièce se fait par l'empreinte (le même fichier dans deux messages est une seule pièce). L'idempotence de l'événement se fait par (message, empreinte). Le code n'a que la seconde. |
| C6 | §7 « la date de la pièce arbitre la période », contre la fenêtre −60/+15, qui suppose qu'une facture précède souvent son paiement d'un mois. | **La fenêtre.** E7 : 11 e-mails favorables sur 200 sont perdus (« aucun candidat ») parce que la facture date du mois précédant le paiement. Les périodes candidates devraient être celles des opérations situées dans [date de la pièce − 15 j, date de la pièce + 60 j]. |
| C7 | §15 et D6 « C-RELAIS par défaut » avec CA-04 ≥ 85 %, contre §7 (« deux dossiers candidats ou plus → file non routée, sans exception »). En C-RELAIS, une même adresse envoie les pièces de plusieurs clients : le routage automatique est nul par construction. | **Le §7 (sécurité).** CA-04 est inatteignable sur un portefeuille offshore en relais. Il faut le limiter aux circuits C-DIRECT et C-INTERNE, ou exiger du relais un marqueur de dossier (sous-adresse `+CODE`, code dans l'objet). |
| C8 | §4 donne des droits par rôle (responsable, comptable), contre §12 « RBAC fin : volontairement absent de la v1 ». | **Le §4.** Deux rôles grossiers ne sont pas un RBAC fin. Le cahier doit le dire. |
| C9 | §9 « les délais vivent dans la configuration du dossier » et §10 « Paramétrable : Oui », contre §10 « garde-fous non paramétrables », qui bornent tout. Ce n'est pas faux, mais le cahier ne dit pas qui gagne entre un jalon paramétré et un garde-fou. | **Les garde-fous**, à écrire explicitement. |

### Le cahier contre le code (au-delà des écarts du §4)

| # | Le cahier dit | Le code fait | Qui a raison |
|---|---|---|---|
| C10 | Relance : clé uuid (§5) | Identifiant déterministe `sha256(destinataire, niveau, références, jour)[:16]` | **Le code** : c'est ce qui rend le cycle rejouable sans second brouillon (CA-05). |
| C11 | Machine §9 sans ATTENDUE → REÇUE | Transition ajoutée (une pièce peut arriver avant toute relance), ainsi que l'exclusion depuis PROMISE ou ESCALADÉE et ESCALADÉE → REÇUE | **Le code** ; la table du cahier est incomplète. |
| C12 | §7 : domaine rattaché à un seul dossier | Le domaine doit être **déclaré** par le cabinet et ne jamais être grand public | **Le code** (plus prudent, en accord avec l'esprit du §7). |
| C13 | §6 : une combinaison incertaine reste signalée manquante | A_VERIFIER, mis en file humaine, jamais relancé | **Le cahier** sur le sens de l'erreur. Compromis acceptable : A_VERIFIER bloque le dossier tant qu'un humain n'a pas tranché. |
| C14 | §6 : groupés au centime, indice toujours exigé | 0,02 € ; indice seulement pour 2 pièces | **Le cahier**, jusqu'à preuve sur données réelles. |
| C15 | §17 : « Moteur d'appariement : aucun écart connu » | B1 | **Les faits.** |
| C16 | §11 CA-09 : « banc de mesure sur corpus généré » | `architecture_mvp.md` §5 : CA-09 « ne se prouve pas ici, exige un environnement de déploiement », alors qu'un test CA-09 existe | **Le cahier** : CA-09 se mesure sur corpus. L'architecture est en retard sur le code. |

### Autres documents du dépôt contre le code

- **README**, « un email de relance par dossier client » : le code fait un e-mail par destinataire, tous dossiers confondus.
- **README** et **document commercial**, « libellés abrégés via `alias_fournisseurs.csv` », « rapprochés … via les alias du dossier » : les alias ne sont pas branchés (A15).
- **Document commercial**, « Relevé CSV, OFX ou CAMT.053 » : seul le CSV est lu.
- **README**, « 100 % … à travers plusieurs graines … (un cas à 99,5 %) » : le balayage E5 donne 2 échecs de CA-02 sur 20 corpus, et le « cas à 99,5 % » est lui-même un manque raté, ce qui échoue CA-02.
- **architecture_mvp.md §1**, « `pyproject.toml` en déclare zéro » (dépendance) : `pyproject.toml` déclare `tzdata; sys_platform == 'win32'`.
- **README**, `python3 -m pytest tests/ -q` : la commande échoue sur l'interpréteur 3.11 de la machine (pytest absent).

## 6. Amendements recommandés au cahier (v1.1)

- **§10, jalons** : remplacer T0 / T+3 / T+7 / T+14 par T0, puis une relance à J+7 calendaires au plus tôt et le premier jour ouvré suivant, puis une à J+14, puis une escalade à T+15 j ouvrés. Écrire : « les garde-fous non paramétrables priment sur tout jalon paramétré ».
- **§10, plafond** : « plafond de 3 envois par pièce (demande initiale comprise), puis escalade obligatoire ». Sinon, ajouter le jalon qui rend le quatrième envoi possible.
- **§10, J−5** : préciser si la relance prioritaire peut déroger au garde-fou hebdomadaire. Recommandation : non ; elle avance seulement le ton et la priorité dans le prochain envoi autorisé.
- **§5, invariant des relances** : « une relance ne référence que des pièces attendues d'un **même destinataire** ; chaque ligne est rattachée à sa pièce et à son dossier, avec une section par dossier ».
- **§5, PieceRecue** : « identité par l'empreinte SHA-256 ; idempotence des événements par (identifiant du message, empreinte) ; l'expéditeur, la date de réception et l'objet sont conservés ».
- **§5, Relance** : « clé : identifiant déterministe (destinataire, pièces, niveau, jour), et non un uuid ».
- **§5, Période** : préciser la périodicité (mensuelle en v1, trimestrielle en option par dossier) et le fait qu'une période sans pièce attendue existe et est COMPLÈTE.
- **§6, ambiguïté** : « à montant égal ou compatible, une pièce disputée par deux opérations n'est attribuée à aucune ; un montant exact prime sur un montant toléré ».
- **§6, incertitude** : choisir explicitement entre « signalée manquante, donc relancée » et « file humaine bloquante avant le prochain envoi au dossier ».
- **§7, période** : « les périodes candidates sont celles des pièces attendues dont la date d'opération tombe dans [date de la pièce − 15 j, date de la pièce + 60 j] ».
- **§7, rattachement humain** : « une confirmation humaine vers un dossier différent du dossier routé exige un motif journalisé, et est refusée par défaut ».
- **§8 et §9** : « une règle PROPOSÉE suspend la relance (drapeau provisoire) sans produire d'état terminal ; seul l'état ACTIVE (validée par le responsable) déclenche HORS PÉRIMÈTRE ».
- **§9** : ajouter ATTENDUE → REÇUE (pièce arrivée avant toute relance), l'exclusion depuis PROMISE ou ESCALADÉE, et ESCALADÉE → REÇUE.
- **§4 et §12** : « deux rôles (responsable, comptable) sont exigés en v1 ; le RBAC fin reste hors v1 ».
- **§11, CA-04** : limiter le seuil de 85 % aux circuits C-DIRECT et C-INTERNE, ou exiger un marqueur de dossier pour C-RELAIS ; prévoir un seuil propre à C-RELAIS.
- **§11, CA-06** : « le contrôle exhaustif du journal est outillé par le produit (commande livrée) ».
- **§11, CA-08** : remplacer « en intégration continue » par « à chaque modification, en CI », et nommer les artefacts couverts (sorties, outbox, audit, export).
- **§11, règle de recette** : « le corpus synthétique est balayé sur N graines × 2 taux à chaque modification ; tout manque raté sur synthétique est une régression bloquante ».
- **§1** : « en v1, CSV pivot exigé ; OFX et CAMT.053 par convertisseurs livrés en P1 », si c'est le choix retenu.
- **§12** : préciser que « protégé contre la modification » veut dire « toute modification est détectable, avec un ancrage externe quotidien du dernier hash ».
- **§14** : décrire le format des valeurs d'entrée conservées par le produit (temps chronométré, dossiers par collaborateur, pièces par dossier, période) et le livrable qui les stocke (phase P2).
- **§17** : remplacer « Moteur d'appariement : aucun écart connu » par l'état réel, et tenir ce tableau à jour à chaque phase.

## 7. Verdict

**Le MVP n'est pas conforme à son cahier des charges, même pour un MVP de test.** Il y a **un écart bloquant** (B1), 18 écarts à corriger, 12 limites assumées et documentées, et 4 sujets hors périmètre, plus 10 lignes non applicables en mode test.

Ce qui tient, et qui est réellement prouvé (tests présents, verts seuls, tués par mutation, confirmés par exécution) :
- rien ne part sans validation humaine (CA-06) ;
- jamais deux envois (CA-05) ;
- chaque ligne réclamée vient du relevé (CA-07) ;
- reprise après panne, y compris par SIGKILL (CA-10) ;
- aucun rattachement automatique entre dossiers (CA-03, côté automatique) ;
- reproductibilité des sorties (CA-08, hors IC et hors export) ;
- performance (CA-09 : 7,97 s pour 5 000 opérations) ;
- export (CA-11).

La chaîne « relance validée par un humain nommé » est solide.

Ce qui ne tient pas : la promesse de départ, « ne rien laisser passer ». Le moteur **rate des pièces manquantes sur son propre corpus synthétique** (2 corpus sur 20, et 2/357 sur le banc), parce qu'il tranche des cas ambigus que le §6 interdit de trancher. CA-02 est un critère bloquant du cahier, et l'échec est visible sans aucune donnée réelle. Pour le reste : le référentiel d'exclusions, que le cahier appelle « le cœur », n'a ni cycle de vie ni auteur ; le test censé protéger le contre-exemple du crédit-bail ne teste rien ; aucune intégration continue ne fait tourner les 2 550 tests ; la mesure du ROI (§14) n'existe pas dans le produit.

**Ordre recommandé pour atteindre la conformité d'un MVP de test :**
1. **B1** : réviser l'affectation (montant exact avant montant toléré, pas d'attribution d'une pièce disputée), ajouter les deux reproductions en tests de non-régression et un test qui balaie `evaluer.py` sur au moins 20 graines.
2. **A2** : intégration continue (suite complète et balayage), avec pytest installable sous Python 3.11.
3. **A1 et A7** : circuit obligatoire (C-RELAIS par défaut) et garde sur le rattachement humain inter-dossiers. Ces deux points deviennent bloquants dès qu'une donnée réelle entre.
4. **A4 et A5** : combler les trous de test révélés par les mutations survivantes (contre-exemple, 0,5 %, centime, ordre des exclusions, indice).
5. **A3, A10, A17** : traçabilité (règles d'exclusion avec auteur, date et statut ; motif de création des pièces ; contrôle CA-06 outillé).
6. Les autres écarts A (A6, A8, A9, A11 à A16, A18), puis la **v1.1 du cahier** (§6), qui tranche C1 à C9 pour que code et cahier redeviennent alignés.
7. Seulement ensuite, sur données réelles (phase 0) : CA-01, CA-02, CA-03 et CA-04. Aucun chiffre synthétique de ce dépôt ne peut en tenir lieu.

---

### Annexe A : tests cités, lancés seuls (copie figée, `pytest -q <nodeid>`)

Tous verts : `test_ca03_jamais_de_rattachement_inter_dossiers`, `test_expediteur_de_a_ne_vise_jamais_b_meme_seul_candidat`, `test_ca05_rejeu_meme_jour_memes_brouillons`, `test_ca05_envoyer_deux_fois_un_seul_eml`, `test_deux_appels_successifs_un_seul_envoi`, `test_envoyer_un_brouillon_non_valide_est_refuse`, `test_envoyer_refuse_un_statut_validee_forge_sans_humain` (5 cas), `test_un_seul_site_d_emission_dans_le_code`, `test_ca07_lignes_reliees_au_releve`, `test_ca07_chaque_ligne_a_une_piece_source_et_inversement` (60 cas), `test_ca08_deux_instances_sorties_identiques`, `test_sain_ca08_independant_du_fuseau_de_la_locale_et_du_hash`, `test_ca10_panne_au_milieu_du_cycle_puis_reprise` (6 cas), `test_ca10_panne_entre_envoi_effectif_et_repercussion`, `test_ca11_export_complet_relu_sans_perte`, `test_sain_ca09_5000_operations_20_dossiers_100_emails` (8,65 s), `test_rejeu_donne_doublon`, `test_limite_hebdomadaire_6_jours_reporte_7_jours_autorise`, `test_limite_hebdo_deux_brouillons_meme_jour_un_seul_part`, `test_creneau_bornes_heure_locale` (12 cas), `test_jour_non_ouvre_reporte_tout_mais_garde_les_transitions`, `test_au_plafond_la_relance_est_refusee` (3 cas), `test_la_table_de_donnees_egale_la_table_du_contrat`, `test_etat_periode_est_calcule_pas_stocke`, `test_brouillon_en_attente_bloque_un_second_brouillon`, `test_credit_bail_exige_une_facture_malgre_le_libelle_d_echeance` (vert, **mais inopérant** : voir 8.6), `test_affectation_globale_prefere_le_meilleur_couple`, `test_combinaison_ambigue_reste_manquante`, `test_paiement_recurrent_inferieur_n_est_pas_un_acompte`, `tests/test_audit.py` (90).

### Annexe B : mutations (code muté dans une copie, tests relancés, code restauré)

« Tuée » : au moins un test échoue (premier test en échec indiqué). « Survit » : tous les tests restent verts.

| Mutation | Tests relancés | Résultat |
|---|---|---|
| M01 tolérance relative 0,5 % → 0 | rapprochement, routage, durcissement | **survit** |
| M01c idem | suite complète | **survit** (2550 passed) |
| M02 tolérance absolue 0,02 → 0 | rapprochement | **survit** |
| M01+M02 aucune tolérance | suite complète | tuée (3) : `test_ecart_d_arrondi_accepte`, `test_tolerance_du_moteur`, `test_corpus_de_reference` |
| M03 / M03c groupé : tolérance relative au lieu du centime | rapprochement / suite complète | **survit** / **survit** |
| M04 indice exigé aussi pour 3 et 4 pièces (règle du cahier) | rapprochement, états, cycle | **survit** (le code diverge du cahier sans qu'aucun test le voie) |
| M05 indice jamais exigé | rapprochement | tuée : `test_deux_factures_qui_totalisent_par_hasard_ne_sont_pas_groupees` |
| M06 combinaison non unique acceptée | rapprochement | tuée : `test_combinaison_ambigue_reste_manquante` |
| M07 acompte sans indice | rapprochement | tuée : `test_paiement_recurrent_inferieur_n_est_pas_un_acompte` |
| M08 règles générales avant règles apprises | suite complète | **survit** |
| M09 / M09b contre-exemples supprimés | rapprochement / suite complète | **survit** / **survit** |
| M10 affectation chronologique | rapprochement | tuée : `test_affectation_globale_prefere_le_meilleur_couple` |
| M11 tous les appariements 1-1 « certains » | rapprochement, durcissement | **survit** (seuil A_VERIFIER du moteur non testé) |
| M12 ou M13 seule : un seul des deux filtres sur le dossier retiré | routage | survit (défense en profondeur : l'autre filtre tient) |
| M12+M13 les deux filtres retirés | suite complète | tuée (18) : `test_ca03_…`, `test_expediteur_de_a_ne_vise_jamais_b_…`, `test_sain_casse_et_alias_…`, `test_sain_domaines_declares_temoins` |
| M14 adresse ambiguë → premier dossier | routage | tuée : `test_contacts_identiques_apres_suffixe_plus_ambigu` |
| M15 candidats multiples → premier proposé | routage | tuée : `test_candidats_multiples` |
| M16 fenêtre −60/+15 ignorée | routage | tuée : `test_bornes_de_la_fenetre` |
| M17 idempotence ignorée | routage | tuée : `test_rejeu_donne_doublon` |
| M18 statut VALIDEE non exigé | envoi | tuée : `test_le_refus_ne_depend_pas_des_controles_du_depot[brouillon]` |
| M19 renvoi d'un ENVOYEE possible | envoi | tuée : `test_le_refus_ne_depend_pas_des_controles_du_depot[envoyee]` |
| M20 limite hebdomadaire retirée à l'envoi | envoi | tuée : `test_limite_hebdo_deux_brouillons_meme_jour_un_seul_part` |
| M21 limite hebdomadaire retirée à la planification | cadence | tuée : `test_limite_hebdomadaire_6_jours_reporte_7_jours_autorise` |
| M22 envoi un jour non ouvré | cadence | tuée : `test_jour_non_ouvre_reporte_tout_mais_garde_les_transitions` |
| M23 créneau 8 h à 18 h retiré | cadence, envoi | tuée : `test_creneau_bornes_heure_locale` |
| M24 plafond 4 → 5 | états, cadence | tuée : `test_plafond_vaut_4` |
| M25 acteur automatique accepté | états, envoi | tuée : `test_garde_humaine_refuse_les_acteurs_automatiques` |
| M26c contrôle de chaîne retiré dans `JournalAudit.verifier` | audit | tuée : `test_entree_modifiee_avec_hash_recalcule_rompt_la_suite` (une première mutation de `_controler`, la fonction de réparation, survivait, mais ne visait pas le bon contrôle) |
| M27 contrôle du hash retiré dans `_controler` | audit | tuée : `test_entree_sans_saut_de_ligne_hash_faux_est_un_fragment` |
| M28 / M28b répercussion des envois sans idempotence (partielle / totale) | cycle, adversarial | survit (un contrôle redondant tient) / tuée (14) : `test_ca05_envoyer_deux_fois_un_seul_eml`, `test_ca10_…` |
| M29 brouillon concurrent recréé | cycle, adversarial | tuée : `test_brouillon_en_attente_bloque_un_second_brouillon` |
| M30 ligne inventée dans la relance | cadence, cycle | tuée : `test_ca07_chaque_ligne_a_une_piece_source_et_inversement` |
| M31 exclusion du CSV avec `regle_validee=False` | suite complète | tuée : `test_piece_connue_devenue_hors_perimetre_ou_justifiee` |
| M32 ton urgent à J−5 jamais appliqué | cadence, cycle | tuée : `test_brouillon_un_dossier_equivalent_a_construire_relance_niveau_2_et_urgent` |
