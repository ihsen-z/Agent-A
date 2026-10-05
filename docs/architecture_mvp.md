# Architecture du MVP : contrat entre les modules

Document de reference pour l'equipe. Il decoule du cahier des charges v1.0
(`docs/cahier_des_charges.html`). En cas de conflit entre ce document et le
cahier des charges, **ce document gagne pour le code**, et l'ecart est remonte
au chef de projet.

## 1. Principes

- **Python 3.11, bibliotheque standard uniquement.** Aucune dependance a
  l'execution (`pyproject.toml` en declare zero). `pytest` seulement pour les tests.
- **Modules purs d'abord, effets de bord ensuite.** `etats`, `routage` et
  `cadence` sont des fonctions sur des dataclasses immuables : pas d'acces
  disque, pas d'horloge cachee (`aujourdhui` / `maintenant` sont des parametres).
  Seuls `depot`, `audit`, `envoi` touchent au disque.
- **Argent : `Decimal`, jamais `float`.** Dates : `datetime.date`. Horodatages :
  `datetime.datetime` avec fuseau.
- **Style de l'existant** : noms de domaine en francais, `import datetime as dt`,
  docstrings et commentaires **sans accents** (comme `moteur.py`), annotations de
  type partout, `from __future__ import annotations`.
- **Deterministe** : memes entrees, memes sorties. Tout tri est explicite.
- **En cas de doute, ne pas trancher** : un cas ambigu part en file humaine,
  jamais en decision automatique.

## 2. Types partages

Definis dans `src/rapprochement/modeles.py`, **figes**. Un module qui a besoin
d'un champ de plus le demande au chef de projet, il ne le cree pas et ne modifie
pas `modeles.py`.

`EtatPiece`, `ETATS_TERMINAUX`, `Evenement`, `EtatPeriode`, `PieceAttendue`,
`StatutRoutage`, `MotifNonRoute`, `FichierEntrant`, `MessageEntrant`,
`DecisionRoutage`, `StatutBrouillon`, `Brouillon`, `EnvoiRelance`,
`identifiant_relance()`, `CIRCUITS`, et `Dossier.circuit / .email_relais /
.destinataire`. Lire le fichier : les docstrings y sont la specification.

`moteur.montants_compatibles(a, b)` est l'alias public de la tolerance du
rapprochement (le plus grand de 0,02 EUR et 0,5 %). Le routage l'utilise tel quel.

## 3. Propriete des fichiers

Un fichier a un seul proprietaire. **Ne jamais editer un fichier qui n'est pas le
tien**, meme pour une correction d'une ligne : signale-le dans ton rapport.

| Expert | Possede | Tests |
|---|---|---|
| Persistance et integrite | `src/rapprochement/audit.py`, `depot.py` | `tests/test_audit.py`, `tests/test_depot.py` |
| Machines d'etats | `src/rapprochement/etats.py` | `tests/test_etats.py` |
| Routage des pieces | `src/rapprochement/routage.py` | `tests/test_routage.py` |
| Cadence et relances | `src/rapprochement/cadence.py`, ajouts a `relances.py` | `tests/test_cadence.py` |
| Validation et envoi | `src/rapprochement/envoi.py` | `tests/test_envoi.py` |
| Chef de projet | `modeles.py`, `parseurs.py`, `moteur.py` (alias), `__init__.py`, `scripts/`, ce document | |

Tu ne lances **que** ta propre suite et `tests/test_rapprochement.py` :
`python3 -m pytest tests/test_<toi>.py tests/test_rapprochement.py -q`. Les autres
experts travaillent en meme temps : un test d'un voisin peut echouer, ce n'est pas
ton affaire. **Ne fais aucun `git add`, `git commit` ni `git push`.**

## 4. Contrats par module

### 4.1 `audit.py` : journal d'audit chaine

```python
@dataclass(frozen=True)
class EntreeAudit:
    seq: int; horodatage: str; acteur: str; action: str; objet: str
    details: dict; hash_precedent: str; hash: str

@dataclass(frozen=True)
class ResultatVerification:
    ok: bool; nb_entrees: int; premiere_erreur_seq: int | None; raison: str

class JournalAudit:
    def __init__(self, chemin: Path | str) -> None
    def ecrire(self, acteur: str, action: str, objet: str,
               details: Mapping[str, Any] | None = None, *,
               horodatage: dt.datetime | None = None) -> EntreeAudit
    def lire(self) -> list[EntreeAudit]
    def verifier(self) -> ResultatVerification
```

- JSONL, ajout seulement, `flush` + `os.fsync` a chaque ecriture.
- `hash = sha256(JSON canonique de l'entree sans son hash, cles triees)`, et
  `hash_precedent` = hash de l'entree precedente, `"0"*64` pour la premiere.
- `acteur` obligatoire et non vide (`ValueError`). `"systeme"` est un acteur valide.
- `details` accepte `Decimal`, `date`, `datetime`, `Enum`, `tuple`, `set` (convertis
  de facon deterministe). Tout autre type : `TypeError` explicite.
- `verifier()` detecte : entree modifiee, entree supprimee au milieu, entrees
  reordonnees, `seq` non contigu, ligne finale tronquee, JSON invalide.
  **Il ne leve jamais** : il renvoie un `ResultatVerification` qui dit ou et pourquoi.
- Verrou de fichier exclusif pendant l'ecriture si `fcntl` existe (Linux).

### 4.2 `depot.py` : persistance SQLite

```python
class Depot:
    def __init__(self, chemin: Path | str)        # ":memory:" accepte
    def transaction(self) -> ContextManager[None] # atomique, rollback sur exception
    def fermer(self) -> None

    def sauver_pieces(self, pieces: Iterable[PieceAttendue]) -> None   # upsert (dossier, reference)
    def charger_pieces(self, *, dossier: str | None = None, periode: str | None = None,
                       etats: Collection[EtatPiece] | None = None) -> list[PieceAttendue]
        # tri : (dossier, date_operation, reference)

    def marquer_vu(self, cle: str) -> bool        # True si NOUVELLE, False si deja vue. Atomique.
    def est_vu(self, cle: str) -> bool

    def mettre_en_file(self, decision: DecisionRoutage) -> None    # idempotent sur (message_id, empreinte)
    def file_humaine(self, *, resolue: bool | None = False) -> list[DecisionRoutage]
    def resoudre_file(self, message_id: str, empreinte: str, par: str, note: str = "") -> None

    def sauver_brouillon(self, b: Brouillon) -> bool       # False si l'id existe deja, n'ecrase JAMAIS
    def charger_brouillon(self, id_relance: str) -> Brouillon | None
    def lister_brouillons(self, statut: StatutBrouillon | None = None) -> list[Brouillon]
    def maj_brouillon(self, b: Brouillon) -> None          # transitions controlees, voir ci-dessous

    def enregistrer_envoi(self, e: EnvoiRelance) -> None   # idempotent sur id_relance
    def historique_envois(self, *, destinataire: str | None = None,
                          depuis: dt.date | None = None) -> list[EnvoiRelance]

    def exporter_json(self, chemin: Path | str) -> Path    # reversibilite : tout, ordre deterministe
```

- `maj_brouillon` n'accepte que : BROUILLON->VALIDEE, BROUILLON->REJETEE,
  VALIDEE->EN_COURS, VALIDEE->REJETEE, EN_COURS->ENVOYEE, EN_COURS->REJETEE,
  EN_COURS->VALIDEE (echec **certain** avant emission). Toute autre transition,
  y compris sortir de ENVOYEE : `ValueError`. Un brouillon ENVOYEE est immuable.
- Les `Decimal` sont stockes en `TEXT` (jamais `REAL`). Les dates en ISO 8601.
- `PRAGMA foreign_keys=ON`, `journal_mode=WAL` (hors `:memory:`), schema versionne
  (`PRAGMA user_version`). Ouvrir une base d'une version future : erreur claire.
- Pas de SQL construit par concatenation de donnees : requetes parametrees.
- `exporter_json` : schema documente en tete du fichier exporte (cle `schema`),
  `Decimal` en chaine, tout est relu sans perte.

### 4.3 `etats.py` : machine d'etats d'une piece attendue

```python
class TransitionInterdite(Exception): ...
PLAFOND_RELANCES = 4

def creer_depuis_rapprochement(r: Rapprochement) -> PieceAttendue
    # MANQUANT et PARTIEL seulement (ValueError sinon). periode = "AAAA-MM" de l'operation.
def appliquer(piece: PieceAttendue, evenement: Evenement, *, aujourdhui: dt.date,
              **contexte: Any) -> PieceAttendue
def etat_periode(pieces: Iterable[PieceAttendue], *, pieces_creees: bool = True,
                 cloturee: bool = False) -> EtatPeriode
```

Table de transitions (cahier des charges section 9). **Tout ce qui n'y figure pas
est interdit** et leve `TransitionInterdite` :

| Etat | Evenement | Garde (contexte) | Cible | Effet sur la piece |
|---|---|---|---|---|
| ATTENDUE | RELANCE_ENVOYEE | `valide_par` non vide et humain | DEMANDEE | `nb_relances`=1, `date_premiere_demande`=`date_derniere_relance`=aujourdhui |
| ATTENDUE | EXCLUSION_CREEE | `regle_validee=True` | HORS_PERIMETRE | |
| ATTENDUE | PIECE_RATTACHEE | `id_piece`, `confirme_par` non vide | RECUE | ajoute `id_piece` |
| DEMANDEE | RELANCE_ENVOYEE | `valide_par` | DEMANDEE | `nb_relances`+1, `date_derniere_relance`=aujourdhui. **Refuse si `nb_relances` >= `PLAFOND_RELANCES`.** |
| DEMANDEE | PIECE_RATTACHEE | idem | RECUE | |
| DEMANDEE | PROMESSE_SAISIE | `date_promesse` dans [aujourdhui, aujourdhui+15 j] | PROMISE | `date_promesse` |
| DEMANDEE | DELAI_DEPASSE | | DEMANDEE si `nb_relances` < plafond, **ESCALADEE** sinon | |
| DEMANDEE | EXCLUSION_CREEE | `regle_validee=True` | HORS_PERIMETRE | |
| PROMISE | DATE_PROMISE_DEPASSEE | `aujourdhui` > `date_promesse` | DEMANDEE | `date_promesse`=None |
| PROMISE | PIECE_RATTACHEE | idem | RECUE | |
| RECUE | CONTROLE_CONFORME | `controle_par` non vide | VALIDEE | |
| RECUE | CONTROLE_NON_CONFORME | `controle_par`, `motif` non vides | DEMANDEE | vide `pieces_rattachees` |
| ESCALADEE | ARBITRAGE_CLASSEMENT | `arbitre_par`, `motif` non vides | CLOSE_SANS_SUITE | |
| ESCALADEE | PIECE_RATTACHEE | idem | RECUE | |
| tout non terminal | BLOCAGE_SIGNALE | `motif` | meme etat | `bloquee`=True |
| tout non terminal | BLOCAGE_LEVE | | meme etat | `bloquee`=False |

- Un etat terminal n'accepte aucun evenement.
- Une piece `bloquee` n'accepte que BLOCAGE_LEVE, PIECE_RATTACHEE, EXCLUSION_CREEE
  et ARBITRAGE_CLASSEMENT.
- `valide_par` / `confirme_par` / `controle_par` / `arbitre_par` : refuser `""` et
  les acteurs automatiques (`systeme`, `auto`, `robot`, `cron`, `bot`, insensible
  a la casse). La decision humaine est une exigence du cahier des charges (D3), pas
  une politesse.
- `etat_periode` : `pieces_creees=False` -> OUVERTE. Sinon, si toutes les pieces
  sont terminales (liste vide comprise) -> COMPLETE, et CLOTUREE si `cloturee=True`.
  Sinon EN_COLLECTE. `cloturee=True` alors qu'une piece n'est pas terminale :
  `ValueError`. **L'etat de periode est calcule, jamais stocke.**

### 4.4 `routage.py` : rattacher une piece entrante

```python
def extraire_montants(texte: str) -> list[Decimal]
def lire_eml(chemin: Path | str) -> MessageEntrant
def lire_dossier_eml(repertoire: Path | str) -> list[MessageEntrant]   # tri par nom de fichier
def adresses_par_dossier(dossiers: Mapping[str, Dossier]) -> dict[str, frozenset[str]]
def cle_idempotence(message_id: str, empreinte: str) -> str

def router(message: MessageEntrant, *,
           adresses: Mapping[str, Collection[str]],
           attendues: Iterable[PieceAttendue],
           deja_vus: Collection[str] = (),
           aujourdhui: dt.date) -> list[DecisionRoutage]
```

Regles (cahier des charges section 7). **Une erreur ici expose les donnees d'un
client a un autre : c'est le seul module ou la prudence passe avant la
completude.**

1. **Idempotence.** `cle_idempotence` = SHA-256 de `message_id` + empreinte du
   fichier. Cle presente dans `deja_vus` -> `DOUBLON`, aucun effet.
2. **Dossier.** Adresse exacte de l'expediteur dans `adresses[d]` pour un seul
   dossier -> ce dossier. Pour plusieurs dossiers -> `NON_ROUTEE / DOSSIER_AMBIGU`.
   Aucune adresse -> **domaine** de l'expediteur, accepte seulement s'il
   n'appartient qu'a un seul dossier **et n'est pas un domaine de messagerie
   grand public** (gmail.com, googlemail.com, outlook.com, outlook.fr,
   hotmail.com, hotmail.fr, live.com, yahoo.com, yahoo.fr, icloud.com, orange.fr,
   wanadoo.fr, free.fr, sfr.fr, laposte.net, gmx.com, proton.me, protonmail.com).
   Un domaine grand public ne sert **jamais** de preuve : `DOSSIER_INCONNU`.
3. **Periode.** Parmi les `attendues` NON terminales du dossier : une seule
   periode -> elle. Plusieurs -> arbitrage par la date lisible dans le nom du
   fichier (`AAAA-MM`, `AAAA-MM-JJ`, `AAAAMM`) ; a defaut `PERIODE_AMBIGUE`.
4. **Operation.** Candidates = `attendues` non terminales, **du dossier resolu
   uniquement**, de la periode resolue, et dont le montant correspond selon
   `moteur.montants_compatibles`. Le montant du fichier vient de
   `extraire_montants(nom_fichier + " " + objet + " " + corps)` : **exactement un
   montant distinct** est requis (zero ou plusieurs -> `MONTANT_INCONNU`). Si le
   nom de fichier porte une date, elle doit tomber dans [date_operation - 60 j,
   date_operation + 15 j]. Exactement une candidate -> `PROPOSEE`. Zero ->
   `AUCUN_CANDIDAT`. Plusieurs -> `CANDIDATS_MULTIPLES`.
5. **Jamais de rattachement inter-dossiers**, par construction : l'ensemble des
   candidates est filtre sur le dossier *avant* toute comparaison de montant.
6. **Rien ne se perd** : un message sans piece jointe donne une decision
   `NON_ROUTEE / SANS_PIECE_JOINTE`. Une decision par fichier.
7. **Une decision `PROPOSEE` n'est jamais une confirmation.** Le rattachement
   effectif exige `confirme_par` (voir `etats.appliquer`).

`extraire_montants` : formats francais et internationaux (`1 234,56`, `1.234,56`,
`1234.56`, `89,90 EUR`, `89.90 €`, espace insecable). **Ne jamais lire un entier
nu comme un montant** (`2026`, un numero de facture, un jour). Un montant porte
toujours ses deux decimales ou un symbole de devise.

`lire_eml` : `email` de la bibliotheque standard, `policy=email.policy.default`.
Pieces jointes = parties avec `Content-Disposition: attachment` ou un nom de
fichier. `message_id` absent -> `"sha256:" + SHA-256 du message brut`.
`expediteur` = adresse seule, en minuscules. Nom de fichier : `os.path.basename`
(jamais de chemin), decodage des noms RFC 2047.

### 4.5 `cadence.py` et `relances.py` : calendrier et garde-fous

```python
def jours_feries(annee: int) -> frozenset[dt.date]        # fixes + Paques, lundi de Paques, Ascension, lundi de Pentecote
def est_ouvre(d: dt.date) -> bool                          # lundi-vendredi, hors jours feries
def ajouter_jours_ouvres(d: dt.date, n: int) -> dt.date
def jours_ouvres_entre(debut: dt.date, fin: dt.date) -> int   # jours ouvres ecoules : ]debut, fin]
def heure_d_envoi_valide(maintenant: dt.datetime, fuseau: str = "Europe/Paris") -> bool
    # jour ouvre ET 08:00 <= heure locale < 18:00 ; datetime naif -> ValueError

JALONS = (0, 3, 7)            # jours ouvres depuis la premiere demande : envois n1, n2, n3
JALON_ESCALADE = 14
FENETRE_HEBDO_JOURS = 7

@dataclass(frozen=True)
class RelancePlanifiee:
    destinataire: str; dossiers: tuple[str, ...]; references: tuple[str, ...]; niveau: int

@dataclass(frozen=True)
class Report:
    destinataire: str; references: tuple[str, ...]; raison: str
    # raison parmi : "LIMITE_HEBDOMADAIRE", "JOUR_NON_OUVRE", "DOSSIER_INCONNU"

@dataclass
class Planification:
    relances: list[RelancePlanifiee]
    reportees: list[Report]
    transitions: list[tuple[str, Evenement]]   # (reference, DELAI_DEPASSE | DATE_PROMISE_DEPASSEE)
    bloquees: list[str]                         # references a traiter par un humain

def planifier(pieces: Iterable[PieceAttendue], dossiers: Mapping[str, Dossier],
              historique: Sequence[EnvoiRelance], aujourdhui: dt.date) -> Planification

def construire_brouillon(planifiee: RelancePlanifiee,
                         pieces: Mapping[str, PieceAttendue],
                         dossiers: Mapping[str, Dossier],
                         aujourdhui: dt.date) -> Brouillon
```

Regles de `planifier` :

- Pieces eligibles : `ATTENDUE` (jamais demandee : due tout de suite) ; `DEMANDEE`
  dont `jours_ouvres_entre(date_premiere_demande, aujourdhui) >= JALONS[nb_relances]`
  tant que `nb_relances < len(JALONS)` ; `PROMISE` dont la date est depassee
  (-> `transitions`, pas de relance dans le meme cycle).
- `DEMANDEE` avec `nb_relances >= len(JALONS)` et au moins `JALON_ESCALADE` jours
  ouvres depuis la premiere demande -> `transitions` avec `DELAI_DEPASSE`. Une
  piece deja arrivee au `PLAFOND_RELANCES` y va aussi, sans attendre.
- Pieces `bloquees`, `ESCALADEE`, `RECUE`, terminales : jamais relancees.
  Les bloquees sont listees dans `bloquees`.
- **Regroupement par destinataire**, tous dossiers confondus : un seul message par
  destinataire (`Dossier.destinataire`). `niveau` = 1 + le plus grand `nb_relances`
  du groupe.
- **Limite d'un e-mail par destinataire sur 7 jours glissants** : si `historique`
  contient un envoi vers ce destinataire depuis moins de `FENETRE_HEBDO_JOURS`
  jours, tout son groupe est dans `reportees` (`LIMITE_HEBDOMADAIRE`).
  **Ce garde-fou gagne sur les jalons** (voir note ci-dessous).
- Si `aujourdhui` n'est pas un jour ouvre : tout est `reporte` (`JOUR_NON_OUVRE`).
- Dossier absent de `dossiers` : `reporte` (`DOSSIER_INCONNU`), jamais d'exception.
- Determinisme : groupes tries par destinataire, references triees.

`construire_brouillon` : reutilise le ton et les gabarits de `relances.py` pour un
seul dossier ; **plusieurs dossiers = une section par dossier** (raison sociale
en titre). Chaque ligne reclamee vient d'une `PieceAttendue` (date, montant,
libelle) : **aucune ligne sans operation bancaire source** (CA-07). L'identifiant
est `identifiant_relance(destinataire, references, niveau, aujourdhui)`. Statut
`BROUILLON`. Les fonctions existantes `construire_relance` et `construire_toutes`
restent **inchangees** (`scripts/lancer.py` les utilise).

> **Point ouvert remonte au chef de projet.** Le cahier des charges (section 10)
> combine des jalons a T+3 et T+7 avec un garde-fou « un e-mail par destinataire
> et par semaine ». Les deux se contredisent : avec le garde-fou, le jalon T+3 ne
> peut jamais s'executer. Le MVP applique la regle la plus prudente (le garde-fou
> gagne), donc en pratique : demande initiale, puis une relance au plus tot a 7
> jours glissants. A trancher avant la phase 2.

### 4.6 `envoi.py` : validation humaine et emission

```python
class EnvoiNonValide(Exception): ...
class EnvoiHorsCreneau(Exception): ...
class ErreurEnvoi(Exception): ...
class ErreurEnvoiCertaine(ErreurEnvoi): ...   # rien n'est parti : on peut reessayer

class Expediteur(Protocol):
    def envoyer(self, brouillon: Brouillon) -> str: ...   # identifiant externe

class ExpediteurDossier:                       # adaptateur de test et de demonstration
    def __init__(self, repertoire: Path | str) -> None   # ecrit <id_relance>.eml (RFC 5322)

def valider(depot: Depot, journal: JournalAudit, id_relance: str, par: str, *,
            maintenant: dt.datetime) -> Brouillon
def rejeter(depot: Depot, journal: JournalAudit, id_relance: str, par: str,
            motif: str, *, maintenant: dt.datetime) -> Brouillon
def envoyer(depot: Depot, journal: JournalAudit, expediteur: Expediteur,
            id_relance: str, *, maintenant: dt.datetime, fuseau: str = "Europe/Paris",
            creneau_ok: Callable[[dt.datetime], bool] | None = None) -> Brouillon
def envois_incertains(depot: Depot) -> list[Brouillon]     # statut EN_COURS
```

- `valider` : `par` non vide et **humain** (meme liste d'acteurs automatiques refusee
  que dans `etats`). Seul un `BROUILLON` peut etre valide. Renseigne `valide_par`,
  `valide_le`. Ecrit dans le journal d'audit.
- `envoyer` est **l'unique chemin d'emission**. Dans l'ordre :
  1. Brouillon introuvable ou statut != `VALIDEE` -> `EnvoiNonValide`
     (`valide_par` vide aussi : defense en profondeur).
  2. Creneau : `creneau_ok(maintenant)` ; si `None`, importer
     `cadence.heure_d_envoi_valide` **au moment de l'appel** (pas a l'import :
     `cadence.py` est ecrit en parallele). Hors creneau -> `EnvoiHorsCreneau`,
     le brouillon reste `VALIDEE`.
  3. Dans une transaction : `VALIDEE -> EN_COURS`, journal `envoi_demarre`.
  4. Appel de `expediteur.envoyer`.
  5. Succes : `EN_COURS -> ENVOYEE`, `envoye_le`, `depot.enregistrer_envoi(...)`,
     journal `envoi_effectue`.
  6. `ErreurEnvoiCertaine` : `EN_COURS -> VALIDEE`, journal, on relance l'exception.
  7. **Toute autre exception : le brouillon reste `EN_COURS`**, journal
     `envoi_incertain`, on relance l'exception. Un envoi dont on ne connait pas le
     resultat n'est **jamais** renvoye automatiquement : mieux vaut un e-mail
     perdu signale a un humain qu'un client relance deux fois.
- Rappeler `envoyer` sur un brouillon `ENVOYEE` ou `EN_COURS` -> `EnvoiNonValide`.
  C'est ce qui garantit CA-05 (zero doublon d'envoi).
- `ExpediteurDossier` refuse d'ecraser un `.eml` existant (`ErreurEnvoi`).

## 5. Criteres d'acceptation couverts par le code du MVP

| CA | Verifie par |
|---|---|
| CA-03 zero rattachement inter-dossiers | `tests/test_routage.py` (proprietes), revue adversariale |
| CA-05 zero doublon d'envoi | `tests/test_envoi.py`, `tests/test_cycle.py` |
| CA-06 zero envoi sans validation | `tests/test_envoi.py`, `tests/test_cycle.py` |
| CA-07 chaque ligne reliee a une operation | `tests/test_cadence.py` |
| CA-08 reproductibilite | `tests/test_cycle.py` |
| CA-10 reprise apres panne | `tests/test_cycle.py`, `tests/test_depot.py` |
| CA-11 export complet | `tests/test_depot.py` |

CA-01, CA-02, CA-04, CA-09 et CA-12 ne se prouvent pas ici : ils exigent des
donnees reelles du cabinet (CA-01, CA-02, CA-04) ou un environnement de
deploiement (CA-09, CA-12).

## 6. Hors perimetre de ce MVP

Connecteurs Gmail, Drive et Google Sheets (il faut des identifiants et un compte
de test : le MVP definit le port `Expediteur` et une source `.eml` sur disque, les
adaptateurs reseau viennent ensuite). Aucune IA. Aucune interface web. Aucun
multi-tenant.
