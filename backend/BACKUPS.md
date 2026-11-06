# Sauvegardes serveur — préparation et vérification

## Mise à jour — conflits de prix de conditionnement, 6 novembre 2026

La migration additive `catalogue.0005_pack_price_conflicts` ajoute les preuves,
propositions et décisions de prix de conditionnement, protégées par six triggers
d'immutabilité. Les manifestes `backup_schema.json` et `restore_schema.json`
incluent cette migration ; la référence de restauration a été reconstruite dans
deux bases privées indépendantes, puis vérifiée courante à **710 objets**. Cette
qualification locale ne signifie pas que la migration a été appliquée au serveur
déployé ni qu'une sauvegarde réelle a été produite.

Une activité `pack_updated` effectivement reprise par `pack_update` peut servir
de preuve à un conflit de prix si son avant/après est exact et son facteur
inchangé. Le prix concurrent ne modifie ni ce facteur ni le nom/statut déjà
confirmé. L'auteur et l'installation déclarés de la source récupérée restent des
références historiques : la validation par le responsable actuel ne crée ni ne
réactive leur compte, installation ou droit.

Tant que ce conflit de prix est ouvert, une nouvelle reprise `pack_update`
contenant `sale_price_fcfa` ou `base_quantity_milli` est conservée mais bloquée,
avec l'issue typée `catalogue_pack_price_conflict` dans l'aperçu. La persistance
revérifie cette garde avant mutation ; elle ne remplace pas le prix commun par
la proposition entrante. Une révision de nom seule reste possible et invalide
l'ancien aperçu de résolution. Un reçu natif ou de reprise déjà reconnu garde
son contenu initial et ne crée aucun nouvel effet.

Le lot combiné domaine/API/gardes/régressions a **105 tests et 136 sous-tests
réussis**. La déclaration OpenAPI bornant les propositions a ensuite été
requalifiée séparément : **2 tests et 6 sous-tests réussis**. Le parcours HTTP
réel concurrent démontre prix 5 200 → commun 4 800 → résolution 5 000, facteur 24
et reçus idempotents, sans stock ou caisse créés par les décisions de prix.
Voir [la qualification du backend](README.md) et
[les tests des gardes de reprise](backups/tests/test_recovery_pack_price_conflicts.py).

Cette tranche n'ajoute aucun type de journal pour rejouer un enregistrement ou
une résolution de conflit lui-même perdu ; elle ne résout pas les conflits de
facteur. Ni synchronisation ordinaire, ni client Android, ni clôture complète
ou réouverture U44 ne sont qualifiés par ces preuves. Les autres paragraphes
conservent les qualifications et limites des tranches antérieures ; aucune US
complète ni récupération globale achevée n'en est déduite.

Extension actuelle :
[fournisseurs et ouvertures perdus](../analysis/backend-recovery-supplier-dependencies-2026-10-28.md).
`supplier_creation`, `supplier_opening_debt`, `supplier_opening_balance` reprennent
identité, dette, avance ou avoir initial par UUID, sous revue actuelle motivée.
L'aperçu sépare les trois impacts fournisseur des créances clients. Un même
contact ne fusionne aucune fiche ; fournisseur absent/étranger et dépendances
manquantes bloquent. Aucune facture, marchandise, caisse ou compensation automatique.
Les faits et premiers accusés restent distincts des soldes après règlement.
La date déclarée du journal de création de fiche ne vaut pas preuve d'une ancienne
date serveur ; les ouvertures conservent leur création source en métadonnées.
Une activité native étrangère est refusée avant chargement de ses détails privés,
comme une origine récupérée liée à une autre boutique. Cette extension ne clôt
pas la récupération ; autres familles et conflits restent requis.

Dernière extension du protocole de reprise :
[annulations de ventes et retours compensateurs](../analysis/backend-recovery-sale-cancellations-2026-10-28.md).
Le parent `sale_cancellation` contient son retour source nullable. Un nouvel
enfant exige également sa copie `sale_return` identique dans la même boutique
et génération ; un enfant déjà enregistré doit être reconnu sur ses faits.
L'acceptation motivée parent intègre ou reconnaît l'enfant atomiquement et
expose ses `related_integration_decision_ids`, avec nonce de revue enfant UUID5
déterministe. Reliquat et répartition dette/avoir doivent concorder ; aucun
remboursement ni stock physique automatique. Les données incohérentes restent
capturées et bloquées. Restauration réelle à sept événements réussie et
régression finale de 806 tests/1318 sous-tests réussie, six avertissements de
fixtures DATABASES. Cette extension ne clôt pas la récupération.

Cette tranche d'US-42 produit un instantané SQLite cohérent, le chiffre en parties
identifiées, vérifie l'ensemble puis conserve les parties et le manifeste. Les
photos courantes sont des BLOB de `ProductPhoto` dans SQLite et sont incluses dans
le même instantané, avec leurs métadonnées. Aucun fichier de clé, paramètre SMTP
ou fichier d'environnement n'est ajouté à l'archive.

L’état de préparation **« Préparée »** ne représente pas une acceptation SMTP ni une
livraison. L’API peut envoyer une partie à la demande du superadministrateur ;
son résultat SMTP et son historique sont distincts. Une commande durable API
peut désormais avancer toutes les parties par étapes bornées. La planification
quotidienne possède sa configuration, son aperçu UTC et un déclencheur HTTP limité
pour les captures, étapes et reprises. Le service externe reste à configurer et qualifier. L’abandon motivé est disponible
par API et Unfold, sans suppression des fichiers existants. Le nettoyage
après acceptation de toutes les parties est décrit plus bas. La restauration active et la récupération des
appareils/droits restent aussi requises par US-43/44. La vérification disponible
ici ne remplace jamais la base active et ne rétablit aucun droit.

## Accès et configuration

L'app technique `backups` est distincte des sept apps métier. Seul le
superadministrateur technique actif peut préparer, consulter ou télécharger une
sauvegarde globale. Être propriétaire, responsable ou membre du staff seul ne
suffit pas. Les droits et la session JWT sont recontrôlés ; Unfold peut utiliser
sa session Django protégée par CSRF.

Configuration hors SQLite et hors dépôt :

| Paramètre | Usage |
| --- | --- |
| `APP_BACKUP_ENCRYPTION_KEY` | Clé Fernet dédiée, 32 octets encodés en base64 URL-safe. Absence ou clé invalide : préparation refusée sans créer de tâche. |
| `APP_BACKUP_KEY_ID` | Identifiant public de version de clé, défaut `primary` ; 1–64 caractères ASCII alphanumériques, `_`, `-`, `.`. Utiliser un nouvel identifiant lors d'un changement de clé. |
| `APP_BACKUP_ROOT` | Répertoire privé des parties ; défaut `backend/backup-files`, ignoré par Git. Ne pas le publier par une règle de fichiers statiques ou médias. |

La clé ne doit pas être un secret Gmail, Django ou JWT : la réutilisation des
valeurs configurées est refusée. Conserver séparément une copie de récupération
de chaque clé utilisée. La clé n'est jamais jointe à un email de sauvegarde ni
stockée dans les modèles ou le manifeste.

Exemple PowerShell pour créer une nouvelle clé directement dans un fichier hors
du dépôt, sans l'afficher et sans écraser un fichier existant :

```powershell
$backupKeyPath = Join-Path $env:USERPROFILE '.config/app_x/server-backup.key'
@'
import os, sys
from pathlib import Path
from cryptography.fernet import Fernet
path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(descriptor, 'wb') as output:
    output.write(Fernet.generate_key())
    output.flush()
    os.fsync(output.fileno())
'@ | ./.venv/Scripts/python.exe - $backupKeyPath
$env:APP_BACKUP_ENCRYPTION_KEY = (Get-Content -Raw -LiteralPath $backupKeyPath).Trim()
$env:APP_BACKUP_KEY_ID = 'server-01'
```

Le mode `0600` concerne Unix ; sous Windows, restreindre aussi les ACL du fichier
à l'utilisateur d'exploitation. Ces commandes sont une procédure pour
l'opérateur ; aucune clé de production n'a été créée pendant les vérifications.

## Parcours disponibles

Dans Unfold, **Sauvegardes serveur → Préparer une sauvegarde serveur** demande
destinataire et motif. Le formulaire conserve son UUID d'opération pour les
réessais. Le suivi est en lecture seule et traduit en français/anglais.

API réservée au rôle technique :

- `POST /api/v1/backups/` avec `operation_id`, `recipient`, `reason` : 201 après préparation et vérification ; 200 pour le réessai identique déjà prêt ; autre contenu sous le même UUID : 409.
- `GET /api/v1/backups/` : historique paginé, tri UTC/id stable.
- `GET /api/v1/backups/{id}/` : métadonnées, manifeste et parties.
- `GET /api/v1/backups/{id}/parts/{number}/` : fichier chiffré, après contrôle de taille et SHA-256.

Une seule capture peut être en préparation. La transaction de réservation se
termine avant la copie SQLite et le chiffrement : une vente peut être validée
pendant le travail d'archive. La suspension de la prochaine sauvegarde
automatique lorsqu'une précédente reste incomplète devra être ajoutée avec le
parcours d'envoi ; elle ne doit pas suspendre les ventes.

Les erreurs de préparation sont enregistrées sans texte d'exception susceptible
de contenir un secret. Si les fichiers ont déjà été vérifiés mais que leur
enregistrement en base échoue, les parties et `manifest.json` restent dans le
répertoire UUID pour récupération explicite. Les fichiers de travail en clair
sont retirés après validation de la copie chiffrée ; les parties chiffrées
préparées ne sont pas supprimées puisqu'aucun envoi n'est encore accepté.

## Format et intégrité

La copie utilise l'[API SQLite de sauvegarde en ligne](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup).
Le chiffrement et l'authentification utilisent [Fernet de pyca/cryptography](https://cryptography.io/en/stable/fernet/),
par messages bornés plutôt que sur toute la base en mémoire.

Chaque fichier `.axbkp` commence par `APPXBKP1`, suivi du jeton Fernet en
représentation binaire. Son contenu authentifié contient la version du format,
l'UUID d'archive, l'identifiant public de clé, l'instant UTC, le numéro et total
de parties, les tailles, les empreintes SQLite/gzip, la liste des migrations et
le segment de données. Le fichier final complet est limité à **15 000 000 octets**.
La future composition MIME devra aussi vérifier sa taille encodée.

La vérification refuse clé incorrecte, altération, troncature, partie répétée,
manquante ou issue d'une autre archive, format inconnu et identifiants de schéma
non autorisés. Elle contrôle ensuite les empreintes, la décompression bornée,
`PRAGMA integrity_check`, les contraintes métier et les clés étrangères. Elle ne
désactive aucun CHECK pour contourner les fonctions SQLite personnalisées.

## Vérification de secours en console

Avant un incident, exporter et conserver avec la version de code le manifeste
des migrations connues :

```powershell
./.venv/Scripts/python.exe tools/backup_schema_manifest.py --output backend/backup_schema.json
./.venv/Scripts/python.exe tools/backup_schema_manifest.py --output backend/backup_schema.json --check
```

Cette exportation n'accède à aucune base. Le fichier public de la version de code
est distinct du manifeste inclus dans l'archive ; ne pas utiliser l'archive
elle-même pour affirmer la compatibilité du code de restauration.

La commande de vérification ne démarre pas Django :

```powershell
./.venv/Scripts/python.exe tools/verify_server_backup.py --key-file C:/private/server-backup.key --expected-migrations backend/backup_schema.json --output-dir C:/private/verification C:/private/parts/backup-UUID.part-0001-of-0002.axbkp C:/private/parts/backup-UUID.part-0002-of-0002.axbkp
```

Elle produit uniquement `verified-{UUID}.sqlite3`, sans écraser un fichier
existant. Son résultat indique `live_database_replaced: false` et
`migration_execution_checked: false`. Le contrôle des identifiants de migrations
est nécessaire mais ne remplace pas un essai des migrations sur copie, le mode
maintenance, le remplacement contrôlé, le repli et la révalidation des droits.
Ces étapes de restauration active ne sont pas encore livrées.

## Preuves locales et limites

Tests : `backups/tests`, contrôles SQLite fichier
`verification.backup_checks`, parcours réel `verification.server_backup_smoke`.
Le vérificateur est aussi exécuté dans un processus interdisant les imports
Django. Les essais utilisent des clés aléatoires et des bases temporaires ;
aucun email ni déploiement n'est effectué.

La cohérence, les permissions, les corruptions et les réessais sont exercés
localement. PythonAnywhere, l'espace au pic avec les données représentatives,
les temps de traitement sur l'hébergement, le débit SMTP, le planificateur et la
restauration complète restent des gates de livraison. Le petit jeu de données
de vérification ne prouve ni le budget installé de 500 Mo ni la cible de charge.


## Préparation des emails avant transport

`backups.services.email_parts.build_part_email` prépare les octets SMTP exacts,
sans ouvrir de connexion. Une partie chiffrée vérifiée devient une seule pièce
jointe ; le message indique UUID, date UTC, numéro/total et SHA-256. Le destinataire
est celui de la tâche manuelle. Un rattachement à une autre sauvegarde, une partie
corrompue et une adresse injectant des en-têtes sont refusés.

L'appelant fournit `max_message_bytes` : le composant contrôle la taille totale
après encodage MIME, en-têtes compris. Cette limite doit correspondre au transport
configuré ; le test utilisant 25 000 000 octets ne prouve aucune acceptation par
Gmail. La limite des fichiers chiffrés reste 15 000 000 octets par partie.

Le composant est exercé par les tests et le scénario HTTP de vérification. Il
est raccordé à l’API d’envoi d’une partie décrite ci-dessous. Le suivi durable
des essais et les contrôles de droits sont appliqués ; le déclencheur décrit plus
bas réutilise les reprises bornées. Construire
un message n'est ni une acceptation SMTP ni une livraison effective.


## Envoi explicite d’une partie et historique SMTP

- `POST /api/v1/backups/{uuid}/parts/{number}/send/` : `operation_id` UUID obligatoire ;
  `retry_of` UUID facultatif pour reprendre une tentative échouée ou incertaine.
- `GET /api/v1/backups/{uuid}/parts/{number}/attempts/` : historique paginé, ordre stable.

Accès technique exclusivement, avec droits et session actuels recontrôlés avant
l’enregistrement et le transport. Même UUID d’opération : même reçu, sans nouvel
envoi. Dès qu’une acceptation est connue, une nouvelle demande retourne ce reçu.
Le transport transmet exactement les octets MIME mesurés ; la limite actuelle de
message est `BACKUP_EMAIL_MAX_BYTES = 25 000 000`, une borne locale qui ne prouve
pas l’acceptation par un fournisseur. Aucune transaction SQLite ne reste ouverte
pendant SMTP.

`SendAttempt` conserve auteur, enveloppe, numéro de tentative, index de reprise,
empreinte/taille du message, dates UTC, résultat et code d’erreur sans réponse SMTP
brute. Les états sont `sending`, `accepted`, `failed`, `uncertain` ; aucun état ne
prétend prouver livraison ou lecture. Les métadonnées sont consultables en lecture
seule dans Unfold, en français et en anglais. Une préparation reste « Préparée »
après envoi : ce libellé décrit seulement l’archive, pas son état de transport.

Une reprise liée attend au moins 15 minutes après le résultat précédent, avec
indices 1, 2, 3 au maximum après l’essai initial 0. Après interruption du processus,
une tentative `sending` devient récupérable comme incertaine après 15 minutes.
La reprise d’un résultat incertain mentionne un doublon possible dans le message
et le journal, avec les mêmes UUID et numéro de partie. Une réponse tardive du
premier envoi reste conservée même si la reprise a déjà été acceptée : deux
acceptations SMTP réelles peuvent exister, sans deuxième partie ni nouvel effet
métier. Après échec persistant, une demande manuelle explicite sans `retry_of`
peut commencer une nouvelle chaîne ; aucun abandon ou renvoi global automatique.

Le service applique ces règles lorsqu’il est appelé. Aucun planificateur n’appelle
encore les reprises de lui-même. Les tests SMTP utilisent exclusivement un serveur
127.0.0.1 qui garde les messages en mémoire et ne les relaie jamais ; aucun email
externe n’est envoyé lors de la qualification.


## Nettoyage après acceptation complète

Après chaque acceptation enregistrée, `cleanup_if_complete` vérifie que toutes
les parties attendues (numéros 1 à N, sans trou) possèdent une acceptation SMTP
vers le destinataire enregistré. Tant qu’une seule partie manque, échoue ou reste
incertaine, aucun fichier de l’archive n’est supprimé, y compris les parties déjà
acceptées. Aucune preuve de livraison en boîte de réception n’est déduite.

Une fois l’ensemble accepté, seuls les fichiers chiffrés enregistrés, aux noms
générés exacts et sous le répertoire privé de cette sauvegarde, sont supprimés.
Aucune suppression récursive. Noms forgés, liens symboliques et cibles hors de
ce répertoire sont refusés. Les fichiers inconnus sont conservés. Le fichier
`manifest.json`, le manifeste en base, les parties enregistrées et le journal
SMTP restent disponibles ; aucun nettoyage d’emails externes n’est effectué.

L’état durable du nettoyage (`waiting`, `cleaning`, `failed`, `cleaned`), ses dates
UTC et son code d’erreur sont exposés dans le détail API et Unfold FR/EN. Une erreur
disque n’annule pas l’acceptation SMTP enregistrée : elle produit
`backup_cleanup_failed` sans exposer les détails du système de fichiers.

Après interruption ou suppression partielle, le réessai d’un envoi déjà accepté
reprend le nettoyage sans ouvrir une nouvelle connexion SMTP et sans recréer les
fichiers disparus. Plusieurs nettoyages concurrents peuvent terminer les mêmes
suppressions idempotentes ; une seule fin est journalisée. Les opérations sur
fichiers ont lieu hors transaction SQLite et ne bloquent pas les ventes.

Le téléchargement d’une archive marquée nettoyée renvoie **410** avec le code
`backup_archive_cleaned`, après contrôle des droits. Sa restauration exige alors
le réimport des pièces jointes sauvegardées et la clé séparée. Une suppression
partielle encore en échec reste visible ; la reprise doit être exécutée avant de
considérer le nettoyage terminé.


## Abandon explicite d’une sauvegarde incomplète

`POST /api/v1/backups/{uuid}/abandon/` reçoit un `operation_id` UUID et un `reason`
non vide (2 000 caractères maximum). Le superadministrateur technique peut aussi
ouvrir « Abandonner cette sauvegarde » dans la liste Unfold et confirmer le motif
sur le formulaire français/anglais. Les droits et la session actuels sont vérifiés
dans la transaction. Auteur, date UTC, UUID d’opération et motif restent en base
et dans le journal. Le réessai du même UUID et du même contenu conserve la même
décision ; un contenu ou une sauvegarde différents provoquent un conflit.

Une préparation encore active doit d’abord se terminer ou échouer. Une sauvegarde
dont toutes les acceptations sont déjà connues ne peut pas être abandonnée : son
nettoyage reste à terminer si nécessaire. Une sauvegarde échouée ou préparée mais
incomplète peut être abandonnée ; aucun abandon automatique n’est effectué.

Après abandon, aucune nouvelle tentative ne démarre. Un abandon enregistré entre
la création d’une tentative et le début du transport est recontrôlé et laisse une
tentative échouée `backup_abandoned`, sans connexion SMTP. Un envoi déjà engagé
peut encore être accepté : ce résultat réel reste conservé, sans annuler l’abandon
ni nettoyer automatiquement ses fichiers. Les reçus antérieurs restent lisibles
et rejouables sans nouvel envoi.

Les fichiers et le manifeste d’une archive abandonnée sont conservés. Leur durée
de rétention et leur suppression contrôlée restent à préciser ; cette action ne
les efface pas et ne crée pas une nouvelle sauvegarde. Le planificateur ignore
les sauvegardes abandonnées pour son blocage, sans réactiver leur envoi.
Aucune tâche externe n’est encore configurée.

Un ensemble partiel de pièces reçues reste refusé par le vérificateur de console,
y compris après abandon. L’abandon ne convertit jamais des parties manquantes en
archive complète ; la vérification n’effectue toujours aucun remplacement actif.


## Commande durable pour toute l’archive

- `POST /api/v1/backups/{uuid}/dispatches/` avec `operation_id` et `reason` : crée une
  commande manuelle, sans ouvrir SMTP. Un seul envoi global actif par sauvegarde.
- `GET /api/v1/backups/{uuid}/dispatches/{dispatch_uuid}/` : état courant, nombre de
  parties acceptées et prochaine échéance connue.
- `POST /api/v1/backups/{uuid}/dispatches/{dispatch_uuid}/advance/` avec `{}` : exécute
  au plus une soumission SMTP ; une partie acceptée auparavant reste ignorée.

Tous ces accès sont réservés au superadministrateur actuel. La création est
idempotente par UUID/contenu ; une relance manuelle distincte après épuisement
crée une nouvelle commande et conserve l’ancienne. Les états sont `active`,
`accepted`, `incomplete`, `abandoned`. `accepted` décrit les acceptations SMTP de
l’ensemble, jamais une livraison effective.

Un bail de quinze minutes protège une étape active des appels concurrents. Le
bail n’est pas une transaction SQLite pendant SMTP. Après arrêt du processus,
un bail expiré peut être repris ; une tentative sans réponse reste inconnue et
suit les règles de reprise existantes. Les UUID des tentatives sont dérivés de
la commande, de la partie et du prédécesseur, pour éviter une seconde tentative
à la reprise d’une même étape.

Une partie échouée qui attend ses quinze minutes ne bloque pas une autre partie
prête. Les requêtes sélectionnent une seule partie éligible dans SQLite, après
exclusion des acceptations connues. Trois reprises maximum par chaîne ; une
commande épuisée devient `incomplete`. Elle n’ouvre jamais d’elle-même une nouvelle
chaîne manuelle. Une relance après acceptation inconnue porte l’avertissement de
doublon possible même si l’utilisateur ouvre une nouvelle chaîne.

L’abandon explicite termine les commandes actives et bloque les nouvelles
soumissions ; les résultats déjà engagés restent conservés. Une commande dont
l’ensemble est accepté peut être rejouée pour terminer un nettoyage en erreur,
sans nouveau SMTP. Une erreur de construction MIME retourne 503 et libère le
bail pour permettre une reprise après correction.

Unfold fournit le suivi en lecture seule des métadonnées, ainsi que des
formulaires dédiés pour créer une commande et avancer ses étapes, en français et
en anglais. L’API reste disponible. Aucun traitement en arrière-plan ou
planificateur externe n’est activé par ces actions manuelles.


## Pilotage manuel dans Unfold

La liste des sauvegardes propose « Envoyer cette archive » lorsqu’une préparation
peut être envoyée. Si une commande existe, son lien mène à la progression. La
liste des commandes possède aussi un lien de progression. Le formulaire de
création présente UUID, destinataire, date et motif prérempli ; il crée seulement
la commande durable et n’ouvre pas SMTP. Un même formulaire rejoué conserve la
même commande.

La progression montre les parties acceptées sur le total, l’état global SMTP,
l’état du nettoyage et l’échéance UTC quand une attente est connue. « Exécuter la
prochaine étape » est un POST protégé par CSRF, suivi d’une redirection GET ; un
rafraîchissement GET n’envoie rien. Une étape soumet au plus une partie et respecte
le délai de quinze minutes, même si l’utilisateur reclique. Une commande épuisée
propose une nouvelle relance manuelle explicite ; aucune chaîne n’est réouverte
automatiquement.

L’historique des tentatives ouvre la liste Unfold filtrée sur la commande,
avec pagination Django. Après acceptation complète et nettoyage, le formulaire
d’étape disparaît. Si seul le nettoyage reste en échec, il reste possible de le
reprendre sans renvoyer d’email. Les erreurs connues sont affichées sans leurs
détails privés ; une perte du rôle technique reste un refus 403, y compris après
validation du formulaire. Propriétaire ou membre staff seul n’obtient aucun accès.

Le parcours HTTP de qualification utilise uniquement des comptes et une base
temporaires, un vrai formulaire de connexion Unfold et un SMTP loopback sans
relais. Il vérifie les jetons CSRF, les redirections, les étapes, les reçus et le
nettoyage. Cela ne constitue pas une validation visuelle Android ou une preuve de
livraison email externe. Le déclenchement quotidien est décrit ci-dessous.


## Configuration quotidienne et aperçu UTC

`GET /api/v1/backups/configuration/` et le formulaire Unfold « Configuration de la
sauvegarde quotidienne » affichent les valeurs initiales : activée, **02:00
Africa/Douala**. À cette date, l’aperçu correspondant est **01:00 UTC**. Heure
entière 0–23, minute entière 0–59, fuseau fixe Africa/Douala ; une désactivation
retourne une échéance vide. L’aperçu inclut l’instant exact s’il coïncide avec
l’horaire, sinon donne le prochain jour local lorsque l’horaire est dépassé.
Il ne constitue pas une preuve d’exécution du déclencheur externe.

La lecture des valeurs initiales ne crée aucune ligne : configuration logique de
version **0**, UUID stable. La première modification persistée crée la version 1,
puis chaque modification incrémente la version. Le singleton et les valeurs
horaires/booléennes stockées sont contrôlés dans SQLite, pas seulement dans l’API.
Le destinataire automatique vient du compte SMTP configuré ; il n’est ni une
adresse manuelle inventée ni un secret sauvegardé dans la configuration.

`PATCH /api/v1/backups/configuration/` exige `operation_id`, `expected_version`,
`enabled`, `local_hour`, `local_minute`, `reason`. Droits techniques et session
actuels recontrôlés dans la transaction. Un formulaire ou une requête portant une
ancienne version est refusé sans écraser l’horaire courant. Le réessai exact d’un
UUID retourne son reçu original, même après une autre modification ; réutiliser
cet UUID pour un contenu différent est un conflit.

Le modèle `BackupConfigurationChange` conserve auteur, motif, version précédente
et version résultante, avant/après et reçu. Configuration et journal sont écrits
ensemble : l’échec du journal annule la modification. Les secrets SMTP et de
chiffrement restent hors SQLite. L’historique est consultable en lecture seule
dans Unfold ; le CRUD générique ne permet pas de contourner cette commande.

Unfold fournit un formulaire dédié avec contrôle de version et CSRF, heure/minute
locales, fuseau, destinataire automatique et aperçu UTC. Les listes des archives
et de la configuration donnent accès au formulaire, y compris avant sa première
sauvegarde. Français et anglais disponibles ; refus 403 pour propriétaire/staff
seul et après perte du rôle technique. La désactivation ou modification de cette
préférence ne lance aucune capture ni aucun envoi manuel.

Aucune tâche cron-job.org ni automatisation Codex n’est créée par cette
configuration. La rétention après abandon et la restauration active restent
également ouvertes.

## Déclencheur quotidien limité

`POST /api/v1/backups/trigger/`, corps JSON `{}`, exige un en-tête
`Authorization: Bearer <secret dédié>`. Le secret est fourni hors dépôt et hors
base par `APP_BACKUP_SCHEDULER_TOKEN` (32 à 256 caractères ASCII sans espace).
Il doit être distinct des secrets Django, JWT, SMTP et de chiffrement. Un secret
absent ou réutilisé provoque 503 ; une authentification incorrecte provoque 401.
Ce secret donne accès uniquement à ce déclencheur : aucun accès aux archives,
aux téléchargements, aux réglages ou à l’administration. GET n’exécute rien.
Aucun destinataire, identifiant d’archive, date ou compte n’est accepté dans le
corps de la requête. La réponse expose seulement un statut opérationnel.

L’hôte doit fournir `APP_BACKUP_SCHEDULER_ACTOR_ID`, UUID d’un compte technique
actif existant. Ses droits actuels sont vérifiés à chaque appel et à chaque
opération ; aucun compte n’est créé implicitement. HTTPS est requis en production,
avec détection de la connexion sécurisée à qualifier chez l’hébergeur. La rotation
du secret ne modifie pas l’identité des captures ou des tentatives déjà stockées.

Chaque appel effectue **une capture actuelle ou au plus une soumission SMTP**.
Avant l’horaire local : `idle`. À l’horaire ou après : capture cohérente, vérifiée
et chiffrée, puis commande d’envoi durable, statut `prepared` ; aucun SMTP pendant
cet appel. Les appels suivants avancent l’envoi, avec les délais de quinze minutes
et trois reprises déjà décrits. Les appels d’attente ne renvoient rien. Une fois
toutes les parties acceptées par SMTP, nettoyage puis statut `accepted` ; un
nouvel appel le même jour est `idle`. L’acceptation SMTP ne prouve pas la livraison.
Une commande épuisée reste `incomplete` ; elle n’est pas recréée automatiquement.

Une sauvegarde non abandonnée incomplète, même manuelle, bloque une nouvelle
capture automatique (`blocked`). Un abandon explicite autorise une nouvelle
capture à partir de l’horaire du jour. La désactivation suspend les nouvelles
captures quotidiennes ; une commande déjà créée peut encore progresser. Pour
arrêter cette dernière, utiliser l’abandon explicite.

Le titulaire a validé une seule capture actuelle après plusieurs jours d’arrêt.
Les dates connues sans capture entre deux exécutions sont enregistrées avec le
nombre de jours, la première et la dernière date, dans `BackupScheduleRun` et
l’événement `daily_capture`. Aucun instantané ancien n’est inventé. Avant la
première exécution connue, aucun historique d’absence n’est affirmé. Une prise en
charge interrompue avant création de l’archive est signalée et compte parmi ces
jours ; une préparation d’archive interrompue devient un échec conservé, nécessitant
un abandon explicite avant une nouvelle capture.

Un bail global de quinze minutes évite les appels simultanés ; l’appel concurrent
reçoit `busy`. La capture, le chiffrement et SMTP s’exécutent hors transaction
SQLite. Le bail est libéré uniquement par son détenteur. L’historique quotidien
et les échéances du bail sont consultables en lecture seule dans Unfold FR/EN,
réservés au superadministrateur technique.

Le déclencheur doit être appelé régulièrement par cron-job.org pour avancer les
parties et les reprises, pas seulement une fois à 02:00. La tâche externe, son
authentification HTTPS effective et le comportement sous les limites réelles de
PythonAnywhere restent à configurer et vérifier. Aucun email externe n’est envoyé
par la qualification locale.

## Synthèse du blocage et âge de la dernière acceptation complète

La liste des sauvegardes et le formulaire de configuration quotidienne affichent
une synthèse Unfold en français ou en anglais, réservée au superadministrateur
technique actuel. Elle reste globale lorsque la liste est filtrée ou paginée :
nombre d’archives non abandonnées incomplètes, plus ancienne archive bloquante,
état, motif et lien vers son détail. Les commandes existantes de reprise et
d’abandon restent accessibles dans la liste des archives ; cette synthèse
n’exécute aucune commande.

Une préparation en cours, une préparation échouée ou une archive dont une partie
manque ou n’a pas été acceptée pour son destinataire initial bloque la nouvelle
génération automatique. L’abandon explicite retire ce blocage sans créer une
réussite. Les ventes et la synchronisation métier peuvent continuer.

La dernière acceptation complète exige toutes les parties numérotées de 1 au
nombre attendu, acceptées pour le destinataire de l’archive. Pour chaque partie,
le calcul retient sa première acceptation connue ; l’instant de complétion est
le dernier de ces instants. Une acceptation en doublon reçue plus tard ne
rajeunit pas l’archive. La préparation récente d’une archive encore incomplète
ne remplace pas la dernière acceptation complète connue.

La synthèse donne l’UUID, l’instant UTC et l’âge à l’heure du contrôle, avec rappel
que l’acceptation SMTP ne confirme pas la livraison. Le nettoyage n’est pas une
condition pour connaître cette acceptation. Sans archive complète connue, aucune
date ou durée fictive n’est affichée. Si l’acceptation est datée dans le futur,
sa date reste conservée, son âge est inconnu et le contrôle de l’horloge est
demandé. Ces données ne garantissent ni disponibilité de toutes les copies ni
restaurabilité de l’archive.

La lecture utilise les tables existantes, sans nouveau schéma. Autorisation,
compteurs et sélections SQL se font dans une courte transaction ; au plus quatre
SELECT, aucun parcours Python de tout l’historique. Les liens, rafraîchissements
et lectures ne créent ni archive, ni tentative SMTP, ni nettoyage. Un retrait du
rôle technique pendant la lecture reste un refus 403 sur les deux pages.

## Préparer une restauration sur copie — US-43 partielle

`tools/verify_server_backup.py` reste utilisable sans démarrer Django et conserve
son rôle de vérification initiale. `tools/prepare_server_restore.py` prolonge cette
vérification : toutes les parties sont authentifiées avant de lancer le worker de
migration. Il nécessite le code et le runtime Python/Django compatibles réinstallés
sur l’hôte, mais ne charge pas les réglages ordinaires de l’application et ne lit
pas sa base active. Il peut donc préparer une copie lorsque ces réglages ou cette
base sont inutilisables.

Depuis la racine du dépôt, avec des chemins privés adaptés à l’hôte :

```powershell
./.venv/Scripts/python.exe -I -X utf8 tools/prepare_server_restore.py --key-file C:/private/backup.key --expected-migrations backend/backup_schema.json --output-dir C:/private/prepared-restore-001 C:/private/parts/part-0001.axbkp C:/private/parts/part-0002.axbkp
```

Fournir toutes les parties réelles et un répertoire de sortie **nouveau**. Un
répertoire existant est refusé sans écrasement. La clé reste dans son fichier
privé ; elle n’est ni passée au worker ni ajoutée au reçu. Le processus initial
ne dépend pas du démarrage de Django pour les contrôles de format, clé, parties,
empreintes, pages SQLite, contraintes et clés étrangères.

Après cette authentification, un worker Python distinct démarre avec des réglages
de restauration dédiés. Son application graph est partagé avec le serveur, mais
son unique base est la copie temporaire. Les variables de configuration métier,
la clé de sauvegarde et les identifiants SMTP ne lui sont pas transmis. Son
backend email refuse toute construction de connexion. Il utilise les migrations
du code installé ; aucune instruction Python n’est chargée depuis l’archive.
Ce mécanisme n’est pas un bac à sable pour du code de migration non fiable : le
code de déploiement doit lui-même être connu et de confiance.

Le worker refuse un inventaire local périmé, un historique de migrations
incohérent ou un conflit du graphe. Il n’effectue que les migrations vers les
feuilles du graphe courant, sans retour arrière. Après migration et fermeture des
connexions, les tables et colonnes des modèles gérés, la présence de toutes les
migrations courantes, les pages, contraintes et clés étrangères sont contrôlées.
La durée est bornée à 120 secondes par défaut (`--timeout-seconds`, entier de 1 à
600) ; expiration ou échec ne valident pas de préparation.

Un succès produit `candidate.sqlite3` et `prepare_manifest.json`, conservés dans
le répertoire privé demandé. Le reçu contient UUID de sauvegarde, empreinte de
l’instantané authentifié, empreinte et taille de la copie migrée, migrations
avant/après et appliquées, empreinte des fichiers de migration du code utilisé,
versions Python/Django/SQLite et instant UTC. La publication emploie un lien
physique sur le même système de fichiers, sans écrasement ni seconde allocation
du fichier SQLite en clair. L’hôte doit autoriser cette opération et protéger
correctement les répertoires privés ; ces conditions restent à qualifier sur
PythonAnywhere. Les fichiers temporaires propres à la tentative sont nettoyés
sur échec ; un fichier de sortie non créé par elle est conservé.

Les drapeaux restent volontairement `activation_allowed: false`,
`permissions_revalidated: false` et `live_database_replaced: false`. Le reçu ne
constitue pas une autorisation d’activation ou une preuve de droits actuels.
L’archive peut encore contenir d’anciens rôles, sessions et baux ; **ne pas placer
manuellement cette copie à la place de la base active**. Révalidation des accès, mode maintenance, remplacement contrôlé,
repli et récupération des téléphones restent requis et non livrés par cet outil.
La capacité et le pic de stockage de cette préparation sur données représentatives
restent aussi à mesurer ; la limite du format ne prouve pas le budget installé.

## Audit des définitions SQL avant publication de la copie

Après les migrations, la copie doit correspondre au schéma de référence du code
installé. `backend/restore_schema.json` est reconstruit dans une base vide par un
worker séparé, avec le même graphe d’applications et de migrations, sans accès à
la base active ou aux réglages métier. La référence ne vient jamais de l’archive.

```powershell
./.venv/Scripts/python.exe -I -X utf8 tools/restore_schema_manifest.py --output backend/restore_schema.json
./.venv/Scripts/python.exe -I -X utf8 tools/restore_schema_manifest.py --output backend/restore_schema.json --check
```

Générer uniquement depuis une version de code connue et de confiance, avec le
runtime installé sur l’hôte cible. `--check` reconstruit une référence indépendante
et vérifie sa correspondance, sans remplacer le fichier existant. Cette étape
fait partie des contrôles avant déploiement et après modification des migrations,
du worker, du normaliseur ou du runtime. Elle n’utilise pas de données métier.

La comparaison porte sur toutes les définitions de tables, index explicites et
automatiques d’unicité, vues et déclencheurs. Les contraintes CHECK, NOT NULL,
UNIQUE, les clés étrangères, les valeurs par défaut et les autres clauses de
table/index sont conservées dans l’empreinte de leurs jetons SQL. Espaces,
commentaires et casse des mots non cités n’altèrent pas cette empreinte ; les
littéraux et les identifiants cités restent conservés. Il s’agit d’une comparaison
des définitions normalisées, pas d’un moteur général de preuve d’équivalence SQL.

Les valeurs des lignes, numéros de pages B-tree et compteurs de séquence ne font
pas partie du schéma. La définition de `sqlite_sequence` est toutefois contrôlée.
Seules les tables de statistiques SQLite connues (`sqlite_stat1` à `sqlite_stat4`)
sont facultatives ; un objet ne peut pas se masquer simplement par un nom
commençant par `sqlite_`. Un index manquant, une contrainte supprimée ou un objet
supplémentaire provoque `restore_schema_mismatch`, même si les données présentes
respectent encore toutes les règles restantes.

La référence est liée aux empreintes des fichiers de migration, du worker et du
normaliseur ainsi qu’aux versions Django, SQLite et sqlparse. Référence absente,
invalide ou périmée : préparation refusée, aucun remplacement de base. La version
actuelle contient 510 objets et environ 119 ko de métadonnées ; elle ne contient
ni photo ni valeur de ligne. Le profil est lu en mode SQLite readonly avec des
bornes sur le nombre d’objets et la taille des définitions SQL.

Le reçu de préparation ajoute `schema_audit_checked: true`, `schema_sha256` et
`schema_reference_sha256`. Ces contrôles ne confirment pas les droits actuels et
n’autorisent pas l’activation : révalidation des accès,
maintenance, remplacement et repli restent requis. La vérification des règles
métier transversales et des conditions de charge/stockage ne se réduit pas à cet
audit de schéma.

## Collecter les parties dans Unfold — US-43 partielle, 24 octobre 2026

Le superadministrateur technique connecté ouvre **Imports de restauration
serveur**, puis **Importer des parties de sauvegarde**. Le formulaire crée un
UUID d’import distinct de l’UUID de sauvegarde. Il demande un motif, la clé de
l’archive et une seule partie chiffrée, au plus 15 000 000 octets. La clé doit être
ressaisie à chaque upload ; elle ne figure ni dans les modèles, ni dans les
fichiers collectés, ni dans les réponses HTML. Le champ est masqué et marqué
sensible pour le filtrage des rapports d’erreur Django. Les variables du
formulaire, du collecteur et de ses appels sont également masquées dans ces
rapports, y compris sur le chemin d’une erreur imprévue.

Après la première partie authentifiée, l’inventaire affiche les numéros enregistrés
et manquants, les totaux globaux et une date de contrôle UTC. **Collecter une
autre partie** poursuit le même import. Les listes sont paginées en SQLite par
50 numéros ; les compteurs portent sur tout l’import. Les parties peuvent arriver
dans le désordre. Une reprise binaire identique ne crée pas un second reçu ; une
autre archive, une autre version chiffrée du même numéro ou un motif incompatible
ne remplace pas les données déjà collectées. Le premier demandeur reste l’auteur
de l’import ; un autre opérateur peut ouvrir un nouvel import.

Chaque upload vérifie l’authenticité Fernet et la structure de son en-tête,
l’identité commune de l’archive et les identifiants de migrations connus du code
installé. L’empreinte stockée porte sur les mêmes octets chiffrés que ceux inspectés.
Une archive entière n’est **pas** déclarée vérifiée par la seule présence de tous
ses numéros. Les empreintes de l’ensemble, la décompression, les pages SQLite et
l’audit/migration de la copie restent à effectuer par la préparation contrôlée.
Aucun bouton d’activation n’est fourni dans cette tranche ; les droits actuels
ne sont jamais récupérés depuis ces métadonnées.

Les contrôles du rôle technique et de la taille déclarée précèdent l’analyse du
corps du formulaire. Un gestionnaire d’upload borne aussi les octets réellement
reçus et refuse un second fichier ; CSRF reste obligatoire. L’authentification du
contenu et son écriture temporaire précèdent la transaction SQL courte publiant
le reçu. Les droits sont recontrôlés avant le commit. Les formulaires génériques
d’ajout, de modification ou de suppression des modèles sont bloqués.

Les fichiers persistés sont uniquement chiffrés, sous :

```text
BACKUP_ROOT/restore-imports/<UUID d’import>/part-00001.axbkp
```

Le nom fourni par le navigateur n’est pas utilisé. La publication crée un lien
sans écrasement sur le même volume ; le fichier temporaire est ensuite supprimé.
Un fichier existant inconnu est conservé et signalé comme conflit. Une reprise
identique peut réparer un fichier enregistré devenu absent sans doubler le reçu.
La collecte échouée retire seulement le fichier qu’elle vient de publier ; elle
ne supprime pas les parties antérieures.

La migration `backups.0013_restore_import_inventory` ajoute `RestoreImport` et
`RestoreImportPart`, leurs auteurs, dates, empreintes et contraintes d’unicité et
de taille. Les 80 migrations historiques de core sont préservées. La référence
SQL reconstruite comprenait alors 498 objets ; elle est passée à 504 après 0015, puis à 510 après 0016. Les fichiers d’import sont des
fichiers d’exploitation privés, distincts de SQLite et des photos : une sauvegarde
serveur ne les réarchive pas récursivement. Après récupération de métadonnées
anciennes, les fichiers devront donc être contrôlés avant toute préparation.

Restent requis : traitement des préparations interrompues, maintenance,
révalidation des droits, activation/repli, récupération des
appareils, gestion explicite du cycle de vie des imports et qualification des
pics d’espace au volume pilote. Ni les listes d’inventaire ni les essais locaux
ne démontrent ces étapes.

## Préparer la copie depuis l’inventaire Unfold — 25 octobre 2026

Quand toutes les parties sont enregistrées, **Préparer une copie de restauration**
ouvre le formulaire de préparation. Il indique l’UUID de sauvegarde, la date de
capture UTC et l’identifiant de clé. Le superadministrateur actuel fournit un
motif et la clé de l’archive ; un UUID d’opération distinct est porté par le
formulaire. Cette demande ne reçoit aucun fichier : corps borné à 64 Kio,
fichiers refusés avant stockage et contrôle du rôle avant analyse du corps.
CSRF, champ de clé masqué et filtrage des variables d’erreur restent actifs.

Le service vérifie les numéros enregistrés, la présence, la taille et l’empreinte
des fichiers chiffrés. Il réserve ensuite le suivi durable de l’opération et
appelle le préparateur déjà utilisé en console, hors transaction SQL longue.
Toutes les parties sont authentifiées ensemble ; les contrôles d’archive,
décompression, intégrité SQLite, migrations et définitions SQL s’appliquent à une
copie privée. Un manque d’espace connu, une mauvaise clé ou un fichier altéré
laissent un état d’échec daté, avec code borné et sans clé ou traceback enregistré.
La vérification d’espace libre du système ne prouve pas le quota du compte hébergé.

Le résultat présente l’état enregistré, l’auteur, les dates, la taille et le
SHA-256 de la copie, sous :

```text
BACKUP_ROOT/restore-prepared/<UUID de préparation>/candidate.sqlite3
BACKUP_ROOT/restore-prepared/<UUID de préparation>/prepare_manifest.json
```

La copie SQLite est volontairement conservée pour l’étape suivante. Les parties
chiffrées originales restent présentes. Rejouer le même UUID d’opération avec le
même import, auteur et motif retrouve le suivi existant et ne produit aucune
seconde copie. Après un échec terminé, **Préparer une autre copie** ouvre un nouvel
UUID ; un échec ancien n’est pas effacé ou rejoué sous un autre contenu. La clé
ne fait pas partie du reçu d’idempotence et n’est jamais persistée.

Une seule préparation est enregistrée comme active à la fois, imposée aussi par
SQLite. Une seconde commande distincte est refusée pendant ce travail ; la reprise
de la commande identique retrouve son résultat ou son état enregistré. Ce garde
technique ne limite pas commercialement boutiques, comptes ou archives. Les dix
derniers suivis figurent dans l’inventaire et l’historique complet est accessible
par la liste d’administration paginée.

Une copie achevée et auditée est conservée même si le rôle du demandeur a été
retiré pendant le calcul ; sa réponse est refusée et les droits restent requis
pour chaque consultation ultérieure. Le témoin de présence de fichier dans la
page de résultat n’effectue pas un nouvel audit d’intégrité ou de permissions.
L’état « en cours » est un état enregistré, sans preuve de processus vivant.
La reprise après interruption sans résultat enregistré, la réconciliation des
artefacts et leur cycle de nettoyage contrôlé restent à compléter ; aucun délai
seul ne libère automatiquement la préparation ou ne supprime des fichiers.

Les migrations `backups.0014` et `0015` ajoutent le suivi et ses preuves typées.
La contrainte utilise des expressions SQLite JSON déterministes. Les booléens
JSON sont distincts des chaînes « true/false » et les tailles entières des réels.
Le test à plusieurs valeurs de PYTHONHASHSEED vérifie la stabilité du DDL entre
processus, nécessaire à la référence d’audit stricte. La migration 0015 corrige
le premier générateur par migration en avant, sans modifier 0014 ni les migrations
historiques. Référence actuelle : 510 objets.

**Aucune activation n’est autorisée.** Le manifeste garde
`activation_allowed=false`, `permissions_revalidated=false` et
`live_database_replaced=false`. La préparation ne rétablit pas de droits depuis
l’archive. Maintenance, révalidation des droits, remplacement contrôlé/repli,
récupération des téléphones et clôture restent requis avant une restauration
active qualifiée. Les essais locaux décrits ne démontrent pas les ressources ou
le débit sur PythonAnywhere gratuit.

## Revérifier une copie préparée — 25 octobre 2026

Dans le résultat d’une préparation achevée, **Vérifier l’intégrité de la copie**
déclenche une lecture explicite, protégée par CSRF et les droits techniques
actuels. Un simple affichage de la page conserve seulement son indicateur de
présence et de taille ; il ne lance pas cette lecture potentiellement longue.
Le POST est limité à 64 Kio et refuse les fichiers joints avant toute inspection.

Le contrôle compare le manifeste privé au reçu enregistré, puis les octets et
le SHA-256 de la copie à leurs valeurs enregistrées. Il vérifie l’intégrité SQLite,
les clés étrangères, la liste des migrations et l’empreinte du schéma préparé,
puis relit l’empreinte du fichier avant de rendre le résultat. Les lectures de
fichier ont lieu hors transaction de la base active. Le rôle technique est
contrôlé de nouveau avant de rendre un résultat positif.

Une modification de données conservant la taille du fichier est détectée. Un
échec ne supprime ni la copie ni les parties chiffrées et ne réécrit pas l’état
historique de la préparation. Le résultat est daté dans la réponse ; il n’est
pas conservé comme une nouvelle autorisation durable. Aucun contenu de la copie,
chemin privé ou secret n’est affiché.

Ce contrôle constate la conformité à la préparation enregistrée à cet instant.
Il ne démontre pas la compatibilité avec un code modifié depuis la préparation,
ni les droits actuels contenus dans une archive. Les trois drapeaux
`activation_allowed`, `permissions_revalidated` et `live_database_replaced`
restent faux ; l’activation/repli et la récupération restent à réaliser.

## Quarantaine des accès archivés sur copie — 25 octobre 2026

La commande `tools/quarantine_server_restore.py` lit un répertoire issu du
préparateur et produit **un nouveau répertoire privé**, sans remplacer la base
active ni modifier la copie préparée. Elle fonctionne sans démarrer Django ou
charger ses paramètres applicatifs. `--help` décrit les arguments ; fournir le
répertoire préparé, `--output-dir`, un `--operation-id` UUID, `--requested-by`
UUID et un `--reason` non vide. Un répertoire de sortie existant est refusé,
même avec le même UUID : cette commande n’écrase ni ne reprend des artefacts.

L’autorisation de cette commande repose sur l’accès opérateur à l’hébergement
et aux fichiers privés. `--requested-by` consigne une attribution fournie par
l’opérateur ; ce champ ne vérifie pas à lui seul une identité ou des droits
actuels. Aucune clé de chiffrement, aucun mot de passe ou jeton ne sont demandés
par cette étape : les parties ont déjà été authentifiées par la préparation.

Avant toute écriture, la commande vérifie le reçu, la taille, l’empreinte,
l’intégrité SQLite, les clés étrangères, les migrations et le schéma exact
contre la référence de confiance installée. Une empreinte de fichier remplacée
dans le reçu ne permet pas d’introduire un déclencheur SQL inattendu. Les fichiers
SQLite auxiliaires sont refusés. La source et son reçu sont revérifiés avant
publication ; les produits, photos et identifiants métier sont conservés.

Dans la nouvelle copie uniquement, tous les comptes sont désactivés, leurs
indicateurs `is_staff`/`is_superuser` sont remis à zéro, les appartenances sont
suspendues et les attributions de groupes/permissions Django aux comptes sont
retirées. Les lignes d’appartenance et leurs rôles sont conservés pour examen ;
la copie préparée d’origine conserve les états initiaux. Les installations et
sessions sont révoquées sans remplacer les anciennes dates de révocation ; les
baux hors connexion sont retirés et les sessions web supprimées. La remise à
zéro couvre aussi un indicateur booléen stocké sous une valeur non canonique.

Le résultat contient `candidate.sqlite3` et `quarantine_manifest.json` : auteur
déclaré, opération, motif, instant UTC, sauvegarde et empreintes sources, taille,
empreinte et décomptes avant/après. `access_quarantined=true` n’autorise pas
l’activation : les trois drapeaux d’activation, de révalidation et de remplacement
restent faux. En cas d’erreur de publication gérée, seuls les fichiers créés par
cette tentative sont nettoyés ; un arrêt brutal et ses artefacts restent à
réconcilier explicitement.

**Tous les anciens comptes techniques sont eux aussi suspendus.** L’accès
technique de secours vérifié, les confirmations des responsables et propriétaires,
la révalidation des vendeurs, le suivi de récupération, la maintenance et le
remplacement/repli restent à construire avant activation. Ne pas remettre
manuellement cette copie en service ou réactiver globalement ses comptes.
La quarantaine et son suivi sont également disponibles dans Unfold, comme décrit ci-dessous.

Cette version conserve une copie préparée et une copie de quarantaine ; elle
demande préalablement un espace libre conservateur de deux tailles de copie plus
15 Mo, pour destination et journal éventuel. Ce précontrôle ne vérifie pas le
quota PythonAnywhere et ne prouve pas la capacité au volume annuel du pilote.
Une préparation intégrant directement la quarantaine et sa qualification des
pics d’espace restent nécessaires pour limiter les copies intermédiaires.

## Mettre une copie en quarantaine depuis Unfold — 25 octobre 2026

Depuis le résultat d’une préparation achevée, **Mettre la copie en quarantaine**
ouvre un formulaire avec motif obligatoire et UUID d’opération. Aucun fichier ou
secret n’est demandé. Le POST est protégé par CSRF, borné à 64 Kio et refuse les
fichiers joints ; le rôle technique actuel est contrôlé avant lecture du corps.
Le serveur attribue la demande au compte connecté, contrairement à l’attribution
déclarative de l’outil console.

La migration `backups.0016_imported_archive_quarantine_updated` ajoute
`ImportQuarantine`, lié à la préparation, avec auteur, motif, instants UTC, état,
empreinte et reçu. SQLite impose une opération unique, au plus une quarantaine
enregistrée en cours et des preuves JSON typées : quarantaine effective,
activation/révalidation/remplacement faux, taille entière et zéro accès restant
pour chaque catégorie. Le SQL de cette contrainte est contrôlé à trois graines
Python ; la référence reconstruite contient désormais 510 objets.

Le service revérifie la copie préparée, exécute la quarantaine indépendante hors
transaction de la base active, contrôle le fichier et le reçu produits, puis
enregistre le résultat. Les demandes de préparation et de quarantaine distinctes
se bloquent mutuellement pendant un travail enregistré en cours. Les ventes
ordinaires restent possibles ; ce garde n’est pas un mode maintenance.

Un POST répété avec le même UUID, auteur, préparation et motif retrouve le même
suivi, sans seconde copie, même si la réponse précédente était perdue. Un UUID
réutilisé pour un autre contenu est refusé. Après un échec achevé, une nouvelle
demande explicite utilise un nouvel UUID ; l’ancienne reste dans l’historique.
Un résultat achevé est conservé si le rôle technique est retiré pendant le
traitement, mais la réponse et les consultations ultérieures sont refusées.

Le résultat montre état, auteur, motif, dates et décomptes avant/après. La page de
préparation affiche les dix derniers suivis ; la liste d’administration permet
de parcourir l’historique complet avec pagination SQL. Les métadonnées ne sont
pas modifiables par formulaire générique. Le témoin de fichier contrôle présence
et taille, sans nouveau contrôle complet d’intégrité. L’état en cours ne prouve
pas que le processus est encore vivant.

Les fichiers sont privés sous `BACKUP_ROOT/restore-quarantined/<UUID du suivi>/`.
Aucune activation n’est proposée. Accès technique de secours vérifié,
confirmations des responsables/propriétaires et vendeurs, maintenance,
remplacement/repli et récupération des appareils restent requis. La reprise
après interruption et le nettoyage contrôlé des copies restent à compléter ;
aucun délai seul ne libère une demande ni ne supprime ses fichiers. Cette étape
ne qualifie pas les besoins de stockage du parcours complet sur l’hébergement.

## Barrière externe des requêtes WSGI — 25 octobre 2026

La barrière se trouve dans un fichier SQLite distinct de la base métier et des
archives : défaut `backend/runtime-control/restore-gate.sqlite3`, ou chemin
absolu `APP_RESTORE_GATE_PATH`. Conserver ce fichier sur un stockage local aux
processus WSGI ; la compatibilité des verrous sur PythonAnywhere reste à vérifier.
Ne pas le placer dans `BACKUP_ROOT`, dans les photos ou dans les copies
restaurées. Son répertoire est privé ; l'outil crée les nouveaux répertoires avec
mode 0700 et le fichier avec 0600, sous réserve des permissions propres à l'OS.
Il contient un journal motivé et versionné des commandes, sans mot de passe.

Après les migrations d'une installation neuve, avant son premier démarrage HTTP,
exécuter depuis la racine du dépôt (adapter les chemins et UUID) :

```powershell
./.venv/Scripts/python.exe -X utf8 tools/restore_maintenance.py initialize --gate C:/private/runtime-control/restore-gate.sqlite3 --database C:/private/business.sqlite3 --operation-id <UUID-neuf> --requested-by <UUID-operateur> --reason "Installation initiale"
./.venv/Scripts/python.exe -X utf8 tools/restore_maintenance.py status --gate C:/private/runtime-control/restore-gate.sqlite3
```

Définir `APP_DB_PATH` et `APP_RESTORE_GATE_PATH` sur ces mêmes chemins pour WSGI.
Le fichier métier doit déjà exister. `initialize` ne remplace jamais une
barrière existante et ne répare pas silencieusement un fichier endommagé. Une
barrière absente, invalide, occupée ou en maintenance renvoie HTTP 503 avant
sessions et authentification, avec absence de cache et invitation à réessayer.
Le message contient français et anglais sans lecture des préférences en base.
La configuration de tests désactive ce contrôle pour ses bases en mémoire ; les
tests dédiés et le parcours HTTP réel l'activent avec des fichiers privés.

L'entrée en maintenance prend un verrou exclusif après drainage des requêtes :

```powershell
./.venv/Scripts/python.exe -X utf8 tools/restore_maintenance.py enter --gate C:/private/runtime-control/restore-gate.sqlite3 --operation-id <UUID-neuf> --requested-by <UUID-operateur> --reason "Préparation de restauration" --expected-version 1 --timeout 10
```

Utiliser la version courante retournée par `status`. Un travail HTTP actif, y
compris le nettoyage d'un flux interrompu, conserve son bail jusqu'à fermeture
de réponse ; les connexions métier du worker sont fermées avant libération.
Expiration du délai : refus sans changement de mode. Réessayer le même UUID et
les mêmes paramètres retrouve son reçu historique sans nouvelle transition ;
ce reçu ne remplace pas la consultation de l'état courant. Changer son contenu
est refusé. L'identifiant d'opérateur sert à l'attribution du journal : il ne
constitue pas une preuve d'identité ni une création de compte.

Un abandon avant toute modification de la base est possible :

```powershell
./.venv/Scripts/python.exe -X utf8 tools/restore_maintenance.py abort --gate C:/private/runtime-control/restore-gate.sqlite3 --operation-id <UUID-neuf> --requested-by <UUID-operateur> --reason "Abandon avant remplacement" --expected-version 2
```

L'abandon compare les empreintes du fichier métier, du WAL et du journal à
l'entrée en maintenance. Un fichier modifié ou manquant reste bloqué ; cette
commande ne permet pas de rouvrir une base restaurée. Aucune commande d'activation
ou de remplacement n'est fournie à ce stade. La barrière couvre les requêtes
WSGI ; un script, une migration ou une tâche hors requête doit encore acquérir
son bail ou être arrêté avant tout futur remplacement. Il ne faut donc pas
assimiler cette étape à une procédure de restauration active achevée. Ne pas
supprimer ou réinitialiser le contrôle pour contourner une maintenance.

L'accès technique de secours retenu par le titulaire exige un code à usage
unique préparé avant l'incident, conservé par l'opérateur hors du serveur. Son
vérificateur et son état de consommation resteront hors des sauvegardes
restaurées. Le code n'est ni demandé ni affiché dans le chat. La preuve et la
création d'un compte neuf sont désormais disponibles en console, voir la section
suivante ; les comptes archivés demeurent suspendus. Confirmations métier,
activation/repli, récupération des téléphones,
interruptions, cycle de vie des copies et qualification de stockage restent
requis.

## Accès technique de secours avec code à usage unique — 25 octobre 2026

Ce mécanisme réalise la preuve supplémentaire acceptée pour Q48/U41. Il crée
un compte technique neuf après contrôle du code ; il ne réactive aucun compte
archivé, ne confirme aucun responsable/vendeur et ne rouvre pas le service HTTP.
Aucun code de production n'a été émis lors de cette qualification. Les tests
emploient des secrets synthétiques dans des répertoires privés temporaires.

### Préparer avant un incident

Conserver le registre SQLite de preuve dans un répertoire privé distinct de la
base métier et hors de `BACKUP_ROOT`, par exemple
`C:/private/runtime-control/technical-proof.sqlite3`. Le registre contient un
vérificateur salé PBKDF2-HMAC-SHA256, l'identifiant de preuve, le chemin métier,
l'auteur/motif/date d'émission, l'état disponible/consommé et le reçu de
consommation. Il ne contient pas le code en clair. Ne pas le restaurer depuis
une ancienne archive métier ni remplacer un registre consommé pour réutiliser
son code. Sa conservation pendant la restauration est une condition du secours.

Avec la base existante, une barrière initialisée en mode ouvert et un compte
technique actuellement actif, l'opérateur authentifié dans sa console privée
peut exécuter depuis la racine du dépôt, avec les chemins et UUID réels :

```powershell
./.venv/Scripts/python.exe -X utf8 tools/technical_recovery.py provision --gate C:/private/runtime-control/restore-gate.sqlite3 --vault C:/private/runtime-control/technical-proof.sqlite3 --database C:/private/business.sqlite3 --backup-root C:/private/backup-files --operation-id "UUID-EMISSION" --requested-by "UUID-TECHNIQUE-ACTUEL" --reason "Préparation du secours avant incident" --gate-version 1 --show-code
```

Les UUID de cet exemple sont des marqueurs à remplacer ; ils ne sont pas des
secrets. Sous Linux, utiliser le Python du virtualenv et les chemins privés
réels de l'hébergement. Adapter la version au résultat de
`tools/restore_maintenance.py status` ; la valeur 1 désigne ici l'installation
neuve. L'émission n'incrémente pas la version de la barrière.

`provision` exige un terminal interactif et `--show-code`. Le code aléatoire de
256 bits s'affiche une fois uniquement dans ce terminal. Le recopier dans le
stockage privé de l'opérateur hors serveur, hors chat et hors journaux ; ne pas
exécuter cette livraison dans une session enregistrée. Le reçu JSON ne contient
que l'identifiant de preuve et le constat de livraison à la console. Un fichier
existant n'est jamais écrasé et une seconde émission sur ce chemin est refusée.
Une livraison perdue n'est pas récupérable depuis le vérificateur. Le registre
et son dossier sont créés privés (0600/0700 sous POSIX ; ACL Windows à qualifier).

Le service refuse l'émission en maintenance, depuis un compte métier, avec un
technique devenu inactif, dans le répertoire des archives ou sur la base métier.
Il prend un bail exclusif sur la barrière le temps du contrôle et de l'émission.
Une activité concurrente peut provoquer un refus temporaire sans émission ; ne
pas supprimer un contrôle existant pour le contourner.

### Créer le compte technique neuf

Cette étape suppose le rétablissement du code/runtime de confiance, un
remplacement déjà contrôlé et la suspension complète des accès archivés. La
commande ne réalise pas ce remplacement et ne constitue pas une procédure
complète d'activation. Elle fonctionne avec les bibliothèques installées et des
settings dédiés, même si les paramètres de l'application web sont inutilisables.
Elle n'utilise ni secrets Django/JWT/SMTP de production, ni connexion ORM par
défaut, ni réseau ou envoi d'email.

Maintenir la barrière fermée, conserver le registre externe courant et choisir
un nom de compte neuf qui n'appartient à aucun compte archivé :

```powershell
./.venv/Scripts/python.exe -X utf8 tools/technical_recovery.py bootstrap --gate C:/private/runtime-control/restore-gate.sqlite3 --vault C:/private/runtime-control/technical-proof.sqlite3 --database C:/private/business.sqlite3 --operation-id "UUID-CREATION" --requested-by "UUID-OPERATEUR" --reason "Accès technique après incident" --gate-version 2 --username technicien-neuf --language fr
```

La version 2 est celle de l'entrée en maintenance du premier exemple ; utiliser
la version courante effective. La commande demande sans écho le code, le nouveau
mot de passe puis sa confirmation. Si le terminal ne peut pas masquer la saisie,
le repli avec affichage est refusé avant lecture du secret. Ne saisir aucune de
ces valeurs comme argument ou variable d'environnement.

Pour un processus local de confiance disposant déjà des secrets, `--stdin-json`
accepte uniquement sur un flux non interactif un objet avec `code`, `password` et
`password_confirmation`, au plus 16 Kio sur l'entrée standard. Ce mode n'est pas
un endpoint web ; son producteur doit éviter fichiers, historique de shell et
journaux contenant ces valeurs. Les messages de confirmation et le reçu de
création ne renvoient aucun secret. Les validateurs de mot de passe Django sont
les mêmes que ceux du serveur ; le compte reçoit un hash PBKDF2, pas le code de
secours comme identifiant.

Avant toute écriture, le service vérifie le mode/version/chemin de la barrière,
les migrations et le schéma exact de référence lié au code/runtime, puis la
suspension des neuf catégories d'accès archivés. Un UUID de compte neuf est créé,
avec `is_active`, `is_staff` et `is_superuser` vrais ; anciens comptes, appartenances,
attributions et sessions restent suspendus. Aucune boutique, quantité, valeur de
stock, vente, dette ou caisse n'est créée ou corrigée. Auteur, motif, date UTC,
nouvel UUID et résultat sont conservés dans le registre externe.

La consommation du code et l'insertion User partagent une transaction SQLite
sur les deux fichiers attachés. Les fichiers doivent utiliser le journal DELETE
et la synchronisation FULL ; WAL est refusé, sans modification automatique du
mode existant. Ces conditions suivent la documentation primaire
[SQLite ATTACH](https://www.sqlite.org/lang_attach.html). Une exception avant
commit annule les deux effets ; la reprise du même UUID et contenu retrouve le
reçu sans second compte. Une autre demande utilisant le code consommé est refusée.
Un mot de passe/contenu différent sous le même UUID est refusé.

Restaurer d'anciens octets métier ne restaure pas le code consommé. Si le compte
neuf a disparu de la base après un retour à une ancienne copie, même une reprise
du reçu historique ne le recrée pas. Un code manquant, perdu ou déjà utilisé ne
peut pas être remplacé automatiquement pendant la maintenance. Les éventuels
recours sans cette preuve restent à définir avec le titulaire.

Après réussite, la barrière reste en maintenance, `activation_allowed` et
`permissions_revalidated` restent faux. L'accès Unfold pendant la récupération,
les confirmations métier, le remplacement/repli, la reprise des téléphones et la
réouverture explicite restent à terminer. Ne pas utiliser `abort` pour contourner
ces étapes : une base modifiée ne satisfait plus sa condition d'abandon.

### Évidence et limites

Concurrence avec deux processus, arrêt forcé avant commit, annulation et reprise,
absence de code en clair, retour à une ancienne base, authentification réelle
Django et création/reprise console hermétique sont vérifiés. Le terminal de
livraison est simulé à sa frontière d'entrée/sortie en test ; aucune livraison
réelle de secret n'a été réalisée par l'agent. Suite générale précédente : 901 tests et 965
sous-tests réussis en 856,11 s. Les deux gardes de saisie ajoutés après sa collecte
passent dans la qualification complémentaire actuelle de 30 tests réussis en
102,81 s, cinq avertissements de fixtures DATABASES. Aucune nouvelle suite
générale postérieure à ces deux gardes n'est revendiquée.
Le parcours HTTP/Unfold/console élargi utilise uniquement une seconde cible
SQLite privée ; il ne remplace jamais celle servant l'API. Les verrous, la
résistance aux pannes d'hébergement, le stockage au volume annuel et le secours
sur PythonAnywhere restent à qualifier.

Confirmation des droits actuels : le titulaire a accepté le contrôle manuel,
boutique par boutique, par le superadministrateur, avec motif obligatoire et
référence de justificatif facultative. Ce contrôle reste distinct de l'OTP ;
confirmer une boutique ne doit pas rétablir les autres.

Le socle de ce contrôle est désormais implémenté dans
[access_models.py](backups/access_models.py) et
[access_decisions.py](backups/services/access_decisions.py), migration
`backups.0017`. Chaque mise en quarantaine crée une génération actuelle et un
contrôle en attente pour chaque couple compte/boutique archivé, propriétaires
compris. Les générations et décisions anciennes restent dans l'historique ;
elles ne confirment pas la nouvelle restauration. Les permissions archivées
restent des métadonnées, sans effet d'autorisation.

Le service `decide_responsible_access` exige un superadministrateur actuellement
actif, un UUID d'opération, la version précédente, une décision `confirmed` ou
`refused`, un motif et éventuellement une référence de justificatif. La
confirmation d'un propriétaire réactive son compte avec accès limité par les
contrôles de chaque boutique ; celle d'un responsable réactive uniquement son
appartenance correspondante, après vérification du rôle et du lien actuels. Le rôle archivé reste un
instantané : un vendeur nommé responsable par le propriétaire peut ensuite être
contrôlé techniquement comme responsable actuel ; un responsable devenu vendeur
relève de la revalidation métier décrite ci-dessous.
Aucun appareil, bail hors connexion, session ou privilège technique archivé
n'est réactivé. Un refus ultérieur est une nouvelle décision et bloque la
boutique concernée, sans fermer les autres. Les reprises identiques renvoient
le reçu initial sans répéter l'effet ; une version périmée ou un contenu changé
est refusé.

Les listes et décomptes API filtrent les contrôles en attente/refusés dans SQL
avant pagination. Les contrôles directs de permissions et le retrait d'appareil
appliquent aussi cette barrière. Un rôle actif reste nécessaire après confirmation.
Les boutiques créées après restauration suivent les règles ordinaires ; une
confirmation ne réactive pas une boutique archivée.

La réouverture contrôlée et la qualification complète du parcours vendeur restent à livrer.
Le formulaire de récupération sous maintenance est décrit ci-dessous. Le service n'ouvre pas la barrière externe
et ne remplace pas l'OTP de reconnexion. Ne pas présenter ce socle comme une
restauration active utilisable de bout en bout.

Qualification du socle de confirmation : suite générale de 917 tests et 976
sous-tests réussis, un échec de fixture historique corrigé ensuite ; cinq
avertissements de fixtures DATABASES, 884,75 s. Qualification après correction :
18 tests et 105 sous-tests réussis, 41,98 s. Parcours dédié
`python -m verification.restore_access_smoke` réussi : A confirmée/B en attente,
confirmation distincte de B, puis refus de A avec B toujours accessible, même
session et historique conservé. Session fournie par la fixture ; aucun OTP ni
activation de production exercés. Le parcours HTTP/Unfold/console existant passe
avec le nouveau schéma de 527 objets ; références régénérées depuis le code de
confiance. La mesure annuelle de stockage précédente n’a pas été rejouée après
cette migration.

Reprise finale du cas corrigé via `pytest --lf` : un test et sept sous-tests
réussis en 33,95 s, 917 désélectionnés. Le rapport de la suite générale conserve
son résultat initial ; il ne constitue pas une nouvelle exécution entièrement verte.

### Contrôles des responsables en console, pendant la maintenance

La commande [review_restore_access.py](../tools/review_restore_access.py) permet
d'inspecter les contrôles de la génération actuelle, puis d'enregistrer une
confirmation ou un refus. Elle exige le mot de passe du compte technique
actuellement actif, avec `is_staff` et `is_superuser`, et prend un verrou exclusif
sur la barrière externe. Elle refuse un mode ouvert, une version périmée, une
base différente, un schéma altéré ou une configuration ORM déjà liée à une autre
base. Elle ferme les connexions avant de rendre le verrou et ne change pas le
mode ni la version de la barrière.

Les exemples partent de la racine du dépôt. Remplacer les chemins et les valeurs
en majuscules par ceux de l'incident contrôlé. `VERSION_BARRIERE` est la version
actuelle retournée par la commande de statut de maintenance ; la commande ne
l'incrémente pas. Utiliser le compte technique neuf préparé par le bootstrap,
jamais un compte technique archivé.

```bash
python tools/review_restore_access.py inspect \
  --gate /chemin-prive/restore-gate.sqlite3 \
  --database /chemin-prive/base-restauree.sqlite3 \
  --gate-version VERSION_BARRIERE --username COMPTE_TECHNIQUE_NEUF \
  --state pending --page-size 25
```

Le mot de passe est demandé dans le terminal privé sans écho. Il ne figure dans
aucun argument, reçu ou journal. La saisie est refusée si le terminal ne permet
pas cette protection. `--stdin-json` est réservé à une entrée non interactive
protégée, bornée à 16 Kio, contenant uniquement la clé `password` ; ne pas écrire
de mot de passe dans une commande shell, dans le chat ou dans un fichier du dépôt.

L'inspection est sans écriture. Elle renvoie `generation_id`, `count`, `page`,
`page_size` et les contrôles avec identifiants, boutique, identité archivée,
rôle archivé, état et version actuels. L'identité et le rôle archivés restent des
informations à vérifier ; ils ne prouvent pas les droits actuels. Pagination de
1 à 100 contrôles par page, ordre stable et filtrage SQL avant décompte/découpe ;
`--shop-id UUID_BOUTIQUE` limite la boutique et `--page` choisit la page.

Après le contrôle humain accepté au cadrage, relever les UUID de génération et
de contrôle ainsi que sa version. Utiliser un nouvel UUID d'opération pour une
nouvelle décision ; le conserver pour une éventuelle reprise identique.

```bash
python tools/review_restore_access.py decide \
  --gate /chemin-prive/restore-gate.sqlite3 \
  --database /chemin-prive/base-restauree.sqlite3 \
  --gate-version VERSION_BARRIERE --username COMPTE_TECHNIQUE_NEUF \
  --generation-id UUID_GENERATION --review-id UUID_CONTROLE \
  --operation-id UUID_OPERATION --previous-version VERSION_CONTROLE \
  --state confirmed --reason "Identité et qualité actuelles contrôlées"
```

Ajouter `--evidence-ref REFERENCE` uniquement si un justificatif est référencé.
Pour un refus, utiliser `--state refused` et un motif adapté. La nouvelle version
est la précédente plus un. La reprise identique renvoie le reçu historique ;
elle n'annule pas une décision plus récente. Une inspection permet de vérifier
l'état courant. La commande refuse les vendeurs actuels dans ce parcours : leur
revalidation passe par le responsable et l’API métier décrite ci-dessous.

Le résultat contient `decision` et garde `activation_allowed=false`,
`gate_mode=maintenance`. Même après plusieurs confirmations, les requêtes HTTP
ordinaires restent bloquées. Cette commande ne remplace ni l'OTP de reconnexion,
ni le futur parcours Unfold de récupération, ni l'activation/repli contrôlés.


Qualification actuelle après ajout de la console : **928 tests et 1 001
sous-tests réussis en 954,87 s**, cinq avertissements connus de fixtures
DATABASES. JUnit : 1929 cas, 928 méthodes, zéro échec/erreur/ignoré. Le scénario
historique précédemment corrigé passe dans cette nouvelle suite complète.
Vingt tests ciblés et vingt-trois sous-tests réussis en 122,92 s ; vérification
supplémentaire de liaison ORM d’abord RED (25,80 s), puis GREEN (24,10 s).
Parcours réel HTTP/Unfold/console enrichi réussi : bootstrap de secours sur cible
privée, inspection, confirmation du propriétaire sans justificatif, reprise
identique ne créant qu’une décision ; ancien technique suspendu, barrière
maintenue fermée et base servant HTTP intacte. Neuf soumissions SMTP loopback,
aucun email externe. Ni activation réelle ni OTP ni formulaire de confirmation
sous maintenance démontrés par cette qualification.


### Contrôles des responsables dans Unfold sous maintenance

La vue [access_admin.py](backups/access_admin.py) propose une liste paginée,
un formulaire de contrôle et l’historique des décisions. Les libellés du
formulaire, états et rôles sont disponibles en français et en anglais. Motif
obligatoire, référence de justificatif facultative ; aucun fichier attendu.
Les modifications génériques, ajouts et suppressions du modèle sont désactivés.
Les contrôles de droits et la borne de corps de 64 Kio précèdent le parsing du
formulaire ; CSRF reste obligatoire.

Pour utiliser cette interface pendant la maintenance, rétablir d’abord le
code/runtime et obtenir le reçu du bootstrap technique décrit plus haut.
Le bootstrap vérifie désormais que la génération actuelle couvre tous les
couples compte/boutique archivés et n’a encore aucune décision : une ancienne
confirmation n’est pas une quarantaine fraîche. Le reçu externe contient
`restore_generation_id`. Un ancien reçu ne comportant pas ce lien ne permet
pas d’ouvrir cette interface ; la console reste disponible avec ses contrôles
actuels, sans réécriture automatique de la consommation historique. Une couverture manquante est refusée avant consommation
du code de secours.

Configurer dans l’environnement du processus web, puis recharger les workers :

```text
APP_RESTORE_RECOVERY_VAULT=/chemin-prive/externe/preuve.sqlite3
APP_RESTORE_RECOVERY_BOOTSTRAP_OPERATION=UUID_OPERATION_DU_BOOTSTRAP
APP_RESTORE_RECOVERY_GATE_VERSION=VERSION_BARRIERE_DU_RECU
```

Ces références ne sont ni le code de secours ni le mot de passe. Conserver la
base web, le chemin de barrière et la version correspondant au même incident.
Le registre de preuve doit rester hors des sauvegardes et hors du répertoire
d’archives configuré. Configuration entièrement absente : accès sous maintenance
désactivé. Configuration partielle ou invalide : démarrage refusé. Ne pas mettre
`RESTORE_GATE_PATH` à `None` pour accéder à la récupération ; cette valeur reste
réservée aux tests isolés.

L’accès ouvre un bail de lecture sur la barrière maintenue fermée. Avant toute
session ou authentification Django, le middleware vérifie le registre externe
consommé, l’opération de bootstrap, la base et la version, le schéma/runtime
actuels, la génération et sa couverture, ainsi que l’état actuel du compte
technique neuf. Les contrôleurs participant à cette barrière restent exclus jusqu’à la
fermeture complète de la réponse. Les erreurs libèrent le bail.

Ouvrir `/admin/` : redirection vers la liste des contrôles, avec connexion du
compte technique neuf si nécessaire. Seule cette identité technique attestée
peut se connecter à ce parcours et l’utiliser. Un compte archivé ou une autre
identité technique ne suffit pas. La connexion ne consomme pas de nouveau code
et vérifie le mot de passe actuel.

Sous maintenance, seuls les chemins suivants et leurs méthodes prévues sont
admis : accueil GET, connexion GET/POST, déconnexion POST, liste GET et contrôle
GET/POST. Les API métier, schéma API, autres modèles d’administration et méthodes
hors contrat restent en 503. Cette sélection n’ouvre pas le service commercial.
Les sessions web et leur dernier accès peuvent être écrits par Django ; ouvrir
un formulaire n’enregistre aucune décision d’accès.

Sélectionner un contrôle propriétaire/responsable de la génération actuelle,
vérifier humainement l’identité et la qualité actuelles, puis confirmer ou
refuser avec motif. Le serveur fixe l’auteur et le couple compte/boutique à
partir de la session et de l’URL. La génération et la version masquées sont
vérifiées ; un conflit rend HTTP 409 avec demande de rechargement. Une reprise
identique renvoie le reçu historique sans second effet. L’état courant et le
reçu sont séparés visuellement ; l’historique garde auteur, date, version, état,
motif et éventuelle référence, par pages de 25.

Les vendeurs restent hors de ce formulaire de décision technique : leur
revalidation par le responsable utilise l’API métier décrite ci-dessous. Les requêtes commerciales
restent bloquées même après confirmation ; activation/repli, récupération des
appareils, clôture et qualification d’hébergement restent à réaliser. L’OTP de
reconnexion et la fidélité visuelle globale ne sont pas qualifiés par ce parcours.

Vérification reproductible sur une base privée :

```bash
cd backend
python -m verification.recovery_web_smoke
```

Ce scénario démarre uniquement un serveur loopback, produit un vrai bootstrap
console avec preuve externe, teste connexion, CSRF, décision limitée à A, reprise,
version périmée en 409 et refus conservant l’historique. B reste en attente ; les
API métier et autres modèles restent en 503, barrière maintenue fermée. Aucun
compte de production ou remplacement actif n’est créé par cette vérification.


Qualification actuelle du contrôle Unfold sous maintenance : **944 tests et
1 017 sous-tests réussis en 1001,55 s**, dix avertissements de fixtures DATABASES.
JUnit vérifié : 1961 cas, 944 méthodes, zéro échec/erreur/ignoré. Qualification
ciblée : vingt-sept tests et seize sous-tests réussis en 99,47 s ; test
complémentaire d’ancienne décision refusée avant bootstrap, 23,25 s. Parcours
HTTP réel sous maintenance et parcours de sauvegarde/console réussis. Django
check, fraîcheur des migrations/gettext et validation OpenAPI réussis. Pas de
nouvelle migration dans cette tranche ; 80 migrations historiques inchangées.
L’examen visuel global, la charge annuelle et l’hébergement restent à qualifier.


### Revalidation des vendeurs par le responsable connecté

Le service [restore_seller_access.py](identity/services/restore_seller_access.py)
et l’API [restore_seller_access.py](identity/api/restore_seller_access.py) gèrent
une décision sur une appartenance vendeur actuelle, par boutique et génération.
Le propriétaire ou responsable doit avoir ses droits actuels confirmés et une
installation/session actuelles. Le superadministrateur technique seul n’acquiert
aucun droit commercial par son statut ; le vendeur ne peut pas se revalider.

```text
GET  /api/v1/shops/UUID_BOUTIQUE/team/UUID_APPARTENANCE/restore-access/
POST /api/v1/shops/UUID_BOUTIQUE/team/UUID_APPARTENANCE/restore-access/
```

Le GET retourne le contrôle et sa version, la génération, le rôle et les flags
archivés comme métadonnées, ainsi que le profil courant. Dans `membership`,
`declared_permissions` décrit les flags stockés ; `permissions` indique les
droits effectifs, tous faux si compte, appartenance ou contrôle sont suspendus.
Les flags archivés ne sont jamais les valeurs par défaut d’une confirmation.
Ce GET ne renouvelle aucun bail hors connexion.

Le POST accepte uniquement du JSON. Pour confirmer, choisir explicitement les
six booléens natifs du profil courant ; aucune valeur ancienne n’est copiée
implicitement. Exemple à adapter avec les UUID/versions reçus du GET :

```json
{
  "operation_id": "UUID_OPERATION_NEUF",
  "generation_id": "UUID_GENERATION",
  "previous_review_version": 0,
  "membership_version": 1,
  "state": "confirmed",
  "reason": "Vendeur et permissions actuels contrôlés",
  "permissions": {
    "can_grant_credit": false,
    "can_collect_debt": true,
    "can_read_debt": false,
    "can_prepare_reminder": false,
    "can_read_cost": false,
    "can_read_cash_balance": false
  }
}
```

Le profil ci-dessus autorise l’encaissement et sa consultation nécessaire des
dettes ; `permissions.can_read_debt` vaut donc vrai dans le résultat effectif,
même si le flag de consultation autonome déclaré reste faux. Motif obligatoire ;
`evidence_ref` est facultative. Un refus utilise `state=refused`, un nouvel UUID
et les versions courantes, sans champ `permissions`.

État, décision, activation de l’appartenance, profil choisi et journal
avant/après sont enregistrés dans une transaction. L’auteur, l’installation et
la session viennent du contexte authentifié, jamais du JSON. Une confirmation
peut libérer le compte métier encore suspendu par cette génération ; une
désactivation globale ultérieure, identifiable après une première confirmation,
exige un contrôle de compte distinct. Un compte privilégié inactif n’est pas
réactivé par le responsable métier. Aucun flag technique, ancien appareil,
session ou bail n’est restauré ; aucun jeton n’est émis et l’OTP reste distinct.

Le contrôle porte sur le rôle vendeur actuel. Une ancienne étiquette de
responsable ne vaut pas rôle courant ; une nomination actuelle comme responsable
par le propriétaire passe ensuite par le contrôle technique du responsable,
avec les deux rôles distincts visibles dans Unfold. Ces décisions conservent les
métadonnées archivées et les UUID d’origine.

HTTP 201 : nouvelle décision ; HTTP 200 : reçu historique identique sans second
effet. Génération/versions périmées, contenu différent pour le même UUID ou
collision d’opération : 409. Les autorisations actuelles et la session sont
revérifiées avant une reprise. Un refus bloque cette seule appartenance et son
contrôle, conserve l’ancienne confirmation et ne désactive pas le compte global
ni les autres boutiques confirmées. Rejouer un ancien reçu ne réactive rien ;
consulter le GET pour l’état courant.

L’historique d’équipe paginé inclut désormais les actions
`membership_changed` et `restore_seller_access_decided`, avec `action`, auteur,
date, motif et données avant/après. Les droits de gestion actuels de la boutique
restent obligatoires pour le consulter.

OpenAPI décrit les deux opérations et les réponses observées 200/201,
400 (erreurs de champs ou liste de messages métier), 401/403/404/409 et 415
(JSON requis). La réponse globale 503 reste présente. Le contrat est testé
contre de vraies réponses et les validations du nouveau profil sont traduites.
Les 404 Django traversant DRF sont présentées de façon générique et traduite,
sans exposer le nom interne du modèle.

Cette API métier reste en 503 pendant la maintenance globale. Le parcours HTTP
`python -m verification.restore_seller_smoke` utilise une fixture privée de droits
avec une barrière initialement ouverte et des sessions de test ; il ne constitue
pas une activation de restauration. Il démontre A revalidée/B inaccessible,
ancien appareil toujours révoqué, refus effectif avec session encore signée,
reprise historique sans réactivation, historique et versions. Deux processus
réels montrent aussi un seul effet pour un même UUID et un conflit pour deux
opérations concurrentes. OTP, activation/repli réels, récupération des appareils
et qualification hébergée restent requis.


### Étape de sauvegarde hors HTTP et barrière de maintenance

Le worker de rappels d'expiration US-46 participe désormais à cette même
frontière : `hosting_reminder_step` entre avant tout SQL et conserve le verrou
pendant SMTP et la fermeture des connexions. Son déclencheur HTTP emploie un
secret dédié distinct de celui des sauvegardes et ne donne aucun droit de
configuration. Les tests interprocessus vérifient refus en maintenance, absence
de création implicite de barrière, blocage de la maintenance pendant SMTP et
appel concurrent sans second envoi. Après restauration, les paramètres
d'hébergement doivent être confirmés dans la génération courante avant reprise
des rappels. Les tentatives interrompues `sending` peuvent être classées en
acceptation incertaine par `tools/repair_hosting_reminder.py` sous contrôle
exclusif du mode normal, avec compte technique/mot de passe actuels et examen
motivé durable. Le verrou empêche de supplanter un worker encore actif ; la
réparation ne réalise pas de SMTP. Voir la
[qualification du processus interrompu](../analysis/backend-hosting-interruption-2026-10-26.md) et le
[contrat US-46 et ses preuves](../analysis/backend-hosting-2026-10-26.md) ; aucun
renouvellement ni planificateur externe n'a été activé dans cette tranche.

Le planificateur `backups.services.scheduler.scheduler_tick` prend maintenant
un bail normal de la barrière externe avant sa première requête métier, même
sans middleware HTTP. Le bail couvre la préparation, les appels SMTP, les états
durables et le nettoyage. Les connexions ORM sont fermées avant libération,
y compris sur erreur. Une maintenance active, une barrière absente ou un lien
vers une autre base empêchent l’étape avant toute requête métier et ne créent
pas de fichier de contrôle de remplacement.

Depuis la racine du dépôt, dans la console authentifiée de l’hébergement :

```powershell
./.venv/Scripts/python.exe -X utf8 backend/manage.py backup_scheduler_step
```

La commande utilise les réglages de déploiement existants : `APP_DB_PATH`,
`APP_RESTORE_GATE_PATH`, `APP_BACKUP_SCHEDULER_ACTOR_ID`, clé de sauvegarde,
répertoire privé et SMTP. Aucun secret ni identifiant d’acteur n’est demandé
en argument. Le compte technique configuré est contrôlé dans la base actuelle.
La commande n’accorde aucun droit commercial, ne restaure pas une base et ne
crée aucun compte. Elle produit un petit JSON, par exemple `{"status":"idle"}`,
ou sort en erreur avec `backup_scheduler_unavailable`. Elle exécute une étape
puis se termine ; elle ne crée ni daemon, ni tâche planifiée, ni envoi WhatsApp.
Le déclencheur HTTPS cron-job.org existant reste disponible.

Les états `idle`, `disabled`, `blocked`, `busy`, `prepared`, `progressed`,
`accepted` et `incomplete` conservent le même sens qu’en HTTP. `accepted` indique
l’acceptation SMTP, jamais la livraison dans une boîte email. La protection
du planificateur contre deux étapes concurrentes reste distincte de la barrière
de restauration. Un second processus peut répondre `busy`, tandis que le bail
de la première étape empêche toujours la maintenance.

La frontière commune [worker_gate.py](backups/services/worker_gate.py) doit
entourer un travail synchrone complet, avant son premier SQL et hors transaction.
Elle ne doit pas rendre de queryset paresseux ni lancer de tâche détachée.
Le planificateur l’utilise effectivement ; les commandes Django génériques et
les scripts arbitraires ne sont pas automatiquement instrumentés. Avant un
remplacement réel, tous les points d’entrée et processus d’exploitation devront
être recensés et arrêtés ou participer à la barrière. Les workers de préparation
d’archives travaillent déjà sur des copies privées, sans activer la base.

Tests : [test_scheduler_worker_gate.py](backups/tests/test_scheduler_worker_gate.py)
lance de vrais processus Django/SQLite, inspecte la protection pendant chaque
requête et après fermeture ORM, vérifie refus sans SQL, erreur métier, commande
réelle, capture chiffrée, attente SMTP simulée, concurrence et nettoyage.
L’attente SMTP est une frontière externe simulée sans email réel. Le parcours
`python -m verification.server_backup_smoke` utilise aussi un vrai serveur SMTP
local pour vérifier le déclencheur HTTP, les archives, Unfold et les consoles.
Aucune activation/repli de base métier ni tâche hébergée n’est démontrée ici.

Le déclencheur `POST /api/v1/backups/trigger/` décrit désormais dans OpenAPI
le corps JSON vide, les statuts ci-dessus, les refus 400/401 et l’indisponibilité
du planificateur. Le 503 de cette opération accepte deux formes distinctes :
`{"code":"backup_scheduler_unavailable"}` après contrôle du déclencheur, ou
le corps global bilingue `service_unavailable` avant authentification. Les
en-têtes Retry-After/Cache-Control concernent la forme de maintenance. Les
réponses réelles sont comparées au schéma JSON, y compris le refus de champs
de destinataire dans l’entrée documentée.

Preuves de cette tranche : régression opérationnelle élargie 77 tests et 204
sous-tests réussis ; après correction finale de l’annotation du corps vide,
25 tests et 317 sous-tests réussis et génération OpenAPI stricte sans
avertissement. La suite générale antérieure de 960 tests n’a pas été relancée.
Activation/repli et qualification hébergée restent requis.


## Installation contrôlée de la copie en quarantaine — 25 octobre 2026

Le service [restore_installation.py](backups/services/restore_installation.py)
et la console [install_server_restore.py](../tools/install_server_restore.py)
remplacent désormais le fichier SQLite cible pendant la maintenance, sans
rouvrir HTTP. Cette étape vient après l’authentification des parties chiffrées,
la préparation/migration auditée et la quarantaine. Elle précède le compte
technique neuf, les contrôles de droits et la future activation. Les photos
BLOB restent dans la même base. Aucun remplacement de production n’a été
exécuté par l’agent ; les exercices utilisent des répertoires privés temporaires.

### Format externe et journal d’intention

Les nouvelles barrières ont `gate_format: 2`. Le `version` courant du contrôle
reste un compteur distinct, initialement 1. Le format 2 ajoute le journal
externe `gate_installations`, hors base métier et hors archive restaurée.
Une réservation lie UUID, auteur/motif, version attendue, chemins, empreintes
de la cible et de ses auxiliaires, copie vérifiée et répertoire de repli.
L’intention est commitée avant toute modification de fichiers. L’état
`reserved` persiste après une erreur ou un arrêt de processus ; les autres
contrôleurs et lectures de récupération sont refusés pendant cet état.
L’UUID du reçu final est aussi réservé contre une collision d’historique.

Le nouveau code continue à lire les anciennes barrières de format 1, mais
refuse une installation sans leur mise à niveau explicite. Après consultation
de `status`, exécuter avec les chemins et la version courants :

```powershell
./.venv/Scripts/python.exe -X utf8 tools/restore_maintenance.py upgrade --gate C:/private/runtime-control/restore-gate.sqlite3 --database C:/private/business.sqlite3 --operation-id <UUID-mise-a-niveau> --requested-by <UUID-operateur> --reason "Journal de restauration durable" --expected-version <VERSION>
```

Cette commande préserve le mode ouvert/maintenance, l’empreinte métier et
l’historique ; elle incrémente la version du contrôle. Elle ne réinitialise
pas une barrière et ne répare pas implicitement un format invalide. Réutiliser
son UUID et ses paramètres retrouve le reçu. Un ancien runtime ne prenant
en charge que le format 1 refuse ensuite cette barrière. Mettre à niveau
avant une récupération technique déjà amorcée : les preuves et l’interface
sont liées à la version exacte, et un ancien reçu n’est pas réécrit pour
contourner cette liaison.

### Installer sous maintenance

Recenser les processus exploitant la cible : HTTP et planificateur participent
à la barrière ; les commandes et scripts arbitraires doivent être arrêtés ou
protégés avant remplacement. Entrer en maintenance avec la commande `enter`
déjà décrite. Les baux actifs doivent se terminer. Vérifier la version retournée
et conserver la copie en quarantaine et son manifeste dans leur répertoire privé.

```powershell
./.venv/Scripts/python.exe -X utf8 tools/install_server_restore.py C:/private/quarantined --gate C:/private/runtime-control/restore-gate.sqlite3 --database C:/private/business.sqlite3 --operation-id <UUID-installation> --requested-by <UUID-operateur> --reason "Restauration depuis la copie vérifiée" --expected-version <VERSION-APRES-ENTER>
```

L’auteur est une attribution du journal, pas une preuve d’identité : cette
console relève de l’opérateur authentifié de l’hébergement. Elle n’accorde
aucun accès applicatif et ne remplace pas le code de secours à usage unique.
Aucun secret n’est passé en argument. Le lancement ignore les paramètres web
Django/SMTP de déploiement. Un worker isolé en réglages minimaux et base factice
vérifie en lecture seule le schéma lié au code/runtime de confiance, l’intégrité,
la génération, les contrôles de droits en attente et l’absence de compte,
permission, installation, session ou bail réactivé. Un manifeste dont la somme
a été recalculée ne suffit pas à masquer des droits actifs ou un schéma modifié.

Avant publication, la console conserve les fichiers présents dans
`restore-install-<UUID>/original.sqlite3`, avec les suffixes `-wal`, `-journal`
et `-shm` réellement présents, et en vérifie tailles/empreintes. Elle prépare
un fichier entrant sur le même système de fichiers, vide les anciens
auxiliaires après conservation puis remplace atomiquement la cible. Les
anciens WAL ne sont jamais associés à la nouvelle base. Les fichiers privés
sont synchronisés ; sur POSIX, les répertoires le sont aussi. L’original peut
être illisible : il est alors conservé comme octets sans ouverture Django.

Si le fichier métier a disparu mais que la barrière externe subsiste, un
accès HTTP ou planificateur normal est refusé avant SQLite, sans recréer de
base vide. `enter` peut enregistrer cette absence. Une installation vérifiée
crée ensuite la cible ; `previous_database_missing: true` et
`original_files[""]: null` signalent la perte, sans fabriquer de copie originale.
Un simple `abort` ne rouvre pas une base manquante. La perte simultanée du
contrôle externe et les cas sans copie survivante restent à qualifier.

### Reprise et suite du cycle

Une réservation incrémente la version (exemple : maintenance 2 → installation 3).
En cas d’interruption, rejouer **le même UUID, les mêmes paramètres et la
version attendue initiale** de l’installation. Les fichiers de travail sont
liés à cette opération ; un fichier modifié est refusé, un fichier partiel
appartenant à l’opération peut être repris. Après un arrêt juste après le
renommage, la cible reconnue par empreinte permet de terminer le journal sans
second remplacement. Le reçu final est conservé dans le contrôle externe.
Une autre opération ne prend pas implicitement la place d’une réservation.

Un succès renvoie `live_database_replaced: true`, `access_quarantined: true`,
`activation_allowed: false`, le répertoire/empreintes de repli, l’UUID de
génération et la version actuelle. `status` montre `installation.state`
`installed` et le mode reste `maintenance`. Utiliser ensuite cette version
actuelle pour le bootstrap technique et les contrôles de droits ; le `2` des
anciens exemples n’est pas la version après installation. Ces étapes de
contrôle restent distinctes de l’OTP. `abort` refuse toute installation
réservée ou terminée : l’installation ne vaut pas une activation.

La réouverture dispose désormais d'un contrôleur séparé décrit ci-dessous.
Le retour arrière contrôlé, le cycle de vie et le nettoyage des copies, la
récupération et la clôture des appareils restent requis. La présence d’une
copie de repli n’est pas une procédure de retour arrière achevée. Aucun fichier
de repli n’est supprimé automatiquement ici.

### Réouverture authentifiée — complément du 27 octobre 2026

`tools/activate_server_restore.py` prend le verrou exclusif de la barrière et
vérifie l'installation terminée, la génération courante et le bootstrap externe
du compte technique neuf. Son mot de passe actuel est saisi sans écho, ou par
flux non interactif borné avec `--stdin-json`. Le code de secours initial n'est
pas demandé à cette étape. Le processus d'audit utilise une base Django factice
et lit le métier en SQLite `mode=ro`, avec schéma/code/runtime contrôlés.

L'activation porte un nouvel UUID, un motif et la version de maintenance après
installation (3 dans l'exemple précédent). Le paramètre obligatoire
`--acknowledge-provisional` rappelle que la récupération des appareils continue.
Après le commit, `status` montre `open`, version 4. La confirmation de A laisse
B bloquée ; les vendeurs restent à revalider et les anciens appareils/sessions
ne retrouvent aucun droit. Les groupes, permissions et privilèges techniques
archivés réintroduits sont refusés avant la première réouverture.

En cas de réponse perdue, conserver le même UUID, les mêmes paramètres et la
version attendue initiale pour le réessai, après contrôle des droits techniques
actuels. L'ancien reçu d'installation reste historique avec
`activation_allowed: false` ; le reçu de réouverture décrit l'action suivante.
Une réinstallation de cette ancienne copie après réouverture est refusée et
ne remplace pas les opérations ajoutées depuis.

Les paramètres, preuves locales et limites sont détaillés dans le
[compte rendu de réouverture](../analysis/backend-restore-activation-2026-10-27.md).
Cette action ne clôture pas la génération de récupération et ne démontre pas
l'OTP, l'hébergement ou le retour arrière.
La qualification Windows couvre les arrêts de processus et les erreurs de
renommage ; une coupure électrique et le comportement hébergé restent
non démontrés, comme le pic de stockage et le budget complet.

Preuves : [test_restore_installation_journal.py](backups/tests/test_restore_installation_journal.py),
[test_restore_installation.py](backups/tests/test_restore_installation.py) et
[restore_installation_smoke.py](verification/restore_installation_smoke.py).
Le parcours WSGI/console à deux boutiques vérifie 200 avant maintenance, 503
après remplacement, original/photo/UUID conservés, anciens comptes et session
suspendus, reprise sans second effet et refus d’abandon servant d’activation.
Le WAL réel d’un processus mort reste lisible dans une copie de repli séparée.
Le contrôle Unfold existant a aussi été relancé sous le nouveau format.


## Registre de récupération et métadonnées du catalogue — 25 octobre 2026

La migration additive `backups.0018` ajoute `RestoreSession` (une par génération)
et `RecoveryDevice` (une observation par session/boutique/installation d’origine).
Ces observations sont séparées des comptes et droits. Elles conservent des UUID
d’origine et un libellé, sans créer ni activer de compte, installation, JWT, PIN
ou bail. Les générations antérieures et leurs identités sont conservées. La
migration ne fabrique pas d’inventaire pour une ancienne session de restauration.

La quarantaine crée atomiquement le registre avec la génération et les accès
suspendus. Les installations connues des comptes propriétaires/membres et les
anciens accès d’installation aux boutiques sont observés, y compris après retrait.
L’unicité évite un double compte dans une boutique ; un même téléphone peut
figurer dans plusieurs boutiques sans devenir plusieurs appareils physiques.
Il s’agit de copies potentiellement utiles connues dans l’archive, pas d’une
preuve de connexion, de récupération ni d’exhaustivité des téléphones. Les
appareils créés après la sauvegarde peuvent être absents du registre.

La date `snapshot_at` vient du champ UTC de la sauvegarde authentifiée, transmis
par la préparation ; elle reste inconnue si les anciennes métadonnées ne la
portent pas. `started_at` date la génération de restauration, et
`inventory_recorded_at` l’enregistrement de cette observation pendant la
quarantaine. Aucune de ces dates ne vaut contact récent ou fin de reprise d’un
téléphone. Le worker d’audit avant installation compare aussi les observations
au contenu connu de la copie : suppression de lignes ou absence d’enregistrement
refusées, même si le manifeste local a été recalculé.

Le catalogue et les alertes ajoutent une métadonnée `restoration` au sommet des
pages ; la fiche produit la fournit aussi. Les historiques produits paginés
portent la même métadonnée de page. Les reçus d’écriture historiques ne sont pas
réécrits. Sans génération courante, cette métadonnée vaut null. Avec une
génération ancienne sans registre, la réponse reste explicite :

```json
{
  "restoration": {
    "generation_id": "00000000-0000-4000-8000-000000000001",
    "session_id": null,
    "state": "recovering",
    "snapshot_at": null,
    "started_at": "2026-10-25T12:00:00Z",
    "inventory_recorded_at": null,
    "known_devices": null
  }
}
```

`known_devices` est limité à la boutique autorisée, calculé sur tout son
inventaire et non sur la page produit. Null signifie « inventaire non
enregistré » ; zéro signifie « aucune observation dans cet inventaire », jamais
« aucune donnée ne manque ». L’état reste `recovering`, même avec zéro appareil,
jusqu’au futur contrôle explicite de clôture technique. Identités et noms des
appareils ne sont pas exposés dans ces métadonnées ; les coûts restent masqués
pour un vendeur sans permission. La consultation ne renouvelle aucun bail.

OpenAPI décrit pages et détails, dates/UUID et valeurs inconnues. Les variantes
produit avec/sans coût se recouvrent structurellement ; leur composition utilise
maintenant anyOf. Les champs effectivement retournés restent contrôlés côté
serveur par les droits actuels, et sont comparés à des réponses réelles en test.
La documentation globale des autres opérations reste partielle.

Preuves de cette tranche : 18 tests/12 sous-tests ciblés, puis 180 tests/270
sous-tests de régression réussis ; migration de 0017 vers 0018, contraintes et
isolation, anciennes métadonnées inconnues et vrais corps OpenAPI vérifiés.
La référence de schéma a été reconstruite sur une base vide (535 objets), avec
le manifeste de migrations. Les anciennes copies préparées selon un autre
schéma doivent être préparées à nouveau depuis leurs parties authentifiées
avant quarantaine et bootstrap ; les reçus de preuve antérieurs ne sont pas
réécrits pour contourner leur liaison à la génération/version/schéma.

Les parcours WSGI de droits, installation et contrôle Unfold ont réussi sur
bases privées avec le schéma courant. La fixture du parcours de droits fournit
des sessions authentifiées et une base accessible ; elle ne démontre pas une
activation après restauration ni un OTP. La suite générale de 987 tests/1053
sous-tests était celle de la tranche installation précédente, avant 0018 ;
elle n’a pas été relancée sur tout le backend dans cette tranche.

Le registre ne reçoit pas encore les journaux d’opérations des téléphones. À
l’issue de cette tranche, le rejeu idempotent avec dépendances, le suivi des
contacts/fins de reprise, la liste détaillée autorisée, les autres rapports, la clôture complète/partielle
et les opérations tardives restent requis. La réouverture, le retour arrière
contrôlé, la conservation Android de 30 jours et la qualification hébergée
restent également à démontrer.


## Inspection des appareils connus — API et Unfold

Le responsable actuel d’une boutique peut consulter ses observations de
récupération avec :

- `GET /api/v1/shops/{shop_id}/recovery/devices/` ;
- `GET /api/v1/shops/{shop_id}/recovery/devices/{id}/`.

Ces routes ne retournent que la génération courante et la boutique autorisée.
L’état actuel du compte, de la session, de la boutique et du rôle responsable
est vérifié avant filtrage/pagination SQL, puis la réponse est construite dans
une transaction courte. Un vendeur est refusé (403) ; le rôle technique seul
ne donne pas une appartenance métier (404). Retirer une appartenance bloque
la même session signée au prochain appel. Une observation d’une autre boutique,
d’une ancienne génération ou inconnue renvoie le même 404 générique. Les
refus d’inspection sont traduits en français et en anglais.

La page contient `count`, `next`, `previous`, `results` et `restoration`.
L’ordre est `created_at`, puis UUID ; 25 observations par défaut, 100 au maximum.
Le compte et `restoration.known_devices` portent sur tout le filtre courant,
sans être réduits à la page. Une réponse vide pour un ancien inventaire
non enregistré garde `known_devices: null` et `state: recovering`.

Chaque observation expose UUID de l’observation, session de récupération,
boutique, UUID d’installation et de compte d’origine, libellé et `recorded_at`.
La fiche ajoute aussi `restoration`. Les UUID d’origine restent des données
archivées ; ils n’accordent aucun compte, session, identité ou droit actuel.
`recorded_at` est la date de copie dans le registre, pas une date de contact ou
de fin de récupération du téléphone. Les champs d’autorité proviennent de la
session et de l’URL ; POST/PATCH/DELETE ne sont pas exposés et n’appliquent
aucune modification. La lecture ne renouvelle pas le bail hors connexion.

OpenAPI décrit les entrées de pagination, les sorties et les erreurs 401/403/404,
ainsi que le 503 global de maintenance. Des réponses réelles de liste, fiche
et refus sont comparées au schéma. À ce stade, le schéma comprend 133 chemins et
178 opérations ; il reste 149 opérations sans résumé/description propre. La
documentation globale n’est donc pas complète.

### Inspection technique pendant la maintenance

Le superadministrateur technique actuel dispose des listes Unfold :

- `/admin/backups/restoresession/` ;
- `/admin/backups/recoverydevice/`.

Pendant la maintenance, ces seules listes sont admises en GET après validation
de la preuve externe consommée et de l’identité technique neuve. Les lignes
sont limitées à la génération attestée et encore courante. Un contexte de
récupération incomplet, invalide ou désignant une génération ancienne ne donne
aucune ligne ; une autre identité technique est refusée. La réponse conserve
son bail jusqu’à fermeture. POST et pages génériques d’édition restent en 503
avant Django ; API métier et autres modèles administratifs restent bloqués.

Les listes sont en lecture seule, en français/anglais, avec 25 lignes par page,
sans action de création, modification ou suppression. Le compte des
installations distinctes est séparé de celui des observations : un téléphone
observé dans A et B compte pour une installation et deux observations. Un
inventaire non enregistré n’est pas présenté comme zéro. Un avertissement
rappelle qu’une observation ne confirme ni récupération ni droits actuels.
En fonctionnement normal, le technique peut aussi inspecter l’historique des
registres ; cela n’accorde aucun accès commercial aux autres comptes.

Preuves : [test_recovery_devices_api.py](backups/tests/test_recovery_devices_api.py),
[test_recovery_devices_admin.py](backups/tests/test_recovery_devices_admin.py),
[test_recovery_web_middleware.py](backups/tests/test_recovery_web_middleware.py).
Le parcours HTTP de droits inspecte les appareils de A, refuse B en attente
puis bloque A avec le même JWT après refus. Le parcours Unfold sous maintenance
montre les deux listes après connexion technique, un téléphone/deux
observations, et le refus des écritures/éditions sans ouverture métier.

Les métadonnées partagées de pagination et leur schéma sont désormais dans
[recovery_metadata.py](backups/api/recovery_metadata.py) ; le catalogue les
utilise. Aucun modèle ni migration supplémentaire dans cette tranche.
L’inspection ne reçoit pas encore de journal téléphone et ne déclare aucun
appareil récupéré. Rejeu, contacts/fins de reprise, clôtures, tardives, autres
rapports, réouverture et repli restent requis. OTP et activation après
restauration ne sont pas qualifiés par ces fixtures privées.

Qualification de l’inspection : 20 tests et 28 sous-tests ciblés réussis
(47,95 s, six avertissements connus des fixtures DATABASES). Régression avec
catalogue, alertes, session, contrôles Unfold et OpenAPI : 176 tests et 288
sous-tests, aucun échec/erreur/ignoré, JUnit de 464 cas en 150,834 s. La limite
de 100 observations est exercée sur 104 lignes autorisées, et le nombre de
requêtes ne croît pas entre une page courte et une page de 100. Les parcours
HTTP privés de droits et de maintenance ont réussi après ces modifications.
La suite générale historique de 987 tests précède 0018 ; elle n’a pas été
réexécutée sur l’ensemble du backend dans cette tranche.

## Contexte de restauration des relevés financiers

Les réponses de ces trois lectures exposent aussi `restoration` :

| Lecture | Route GET dans `/api/v1/shops/{shop_id}/` |
| --- | --- |
| Relevé client | `clients/{client_id}/statement/` |
| Relevé fournisseur | `suppliers/{supplier_id}/statement/` |
| Journal de caisse | `cash/` |

Le compte, la session, la boutique, le tiers et les permissions actuels sont
contrôlés avant le calcul du relevé et de son contexte. Le vendeur conserve
sa vue personnelle de caisse ; le contexte de restauration ne lui accorde
ni solde global ni accès aux relevés réservés au responsable. Une boutique
en attente de confirmation et un tiers d’une autre boutique restent refusés.
Un retrait reçu bloque les lectures de la même session signée.

`restoration` vaut `null` sans restauration courante. Pendant la reprise,
`known_devices: null` signifie inventaire non enregistré ; un compte nul
d’observations reste `state: recovering`. Le compte porte sur la boutique,
sans UUID, nom d’appareil ni identité de compte d’origine. Les observations
d’une génération historique ne servent pas d’inventaire courant. La
confirmation des droits ne confirme aucune récupération de données.

Les montants, lignes, affectations, portée personnelle/globale et sommes de
tout le filtre ne changent pas. `as_of` reste l’arrêté financier repris dans
les liens de pagination. Le contexte de restauration est une information
actuelle, indépendante de cet arrêté : demander un ancien relevé n’efface
pas l’avertissement sur la restauration en cours. `provisional` reste vrai
faute de preuve de complétude des appareils, même sans restauration courante.
Ces lectures ne créent aucune écriture et ne renouvellent aucun bail.

OpenAPI décrit chaque enveloppe de relevé une seule fois. La validation de
corps HTTP réels a révélé puis corrigé une double pagination documentaire :
le générateur enveloppait auparavant une réponse déjà paginée. Le sérialiseur
de contexte partagé marque ces enveloppes comme objets uniques ; la vraie
pagination SQL reste inchangée. Les sorties avec absence de restauration,
inventaire inconnu et inventaire enregistré sont validées contre le schéma.

Preuves : [test_recovery_reports.py](backups/tests/test_recovery_reports.py),
[test_statements.py](customers/tests/test_statements.py),
[test_supplier_statements.py](purchasing/tests/test_supplier_statements.py),
[test_cash.py](cash/tests/test_cash.py) et
[restore_access_smoke.py](verification/restore_access_smoke.py).
Commande ciblée depuis la racine :

```powershell
./.venv/Scripts/python.exe -X utf8 -m pytest backend/backups/tests/test_recovery_reports.py backend/customers/tests/test_statements.py backend/purchasing/tests/test_supplier_statements.py backend/cash/tests/test_cash.py -q
```

Cycle TDD : absence du contexte observée avant implémentation, puis neuf
échecs de conformité OpenAPI avant correction de la double enveloppe.
Qualification ciblée finale : 35 tests et 34 sous-tests réussis en 52,27 s.
Régression avec catalogue, alertes, droits, maintenance et OpenAPI : 211 tests
et 322 sous-tests réussis en 109,52 s, six avertissements connus des fixtures
DATABASES ; JUnit 533 cas, aucun échec, erreur ni test ignoré.
Parcours WSGI privé à deux boutiques réussi : ancien inventaire inconnu,
observations distinctes A/B, blocage de B en attente, puis refus de A avec le
même JWT laissant B accessible. Aucun OTP ni activation après restauration
exercé. Django check, fraîcheur des migrations et validation OpenAPI réussis.
Le schéma compte toujours 133 chemins/178 opérations avec 503 ; 146 opérations
sans résumé/description et six seulement avec erreurs 4xx explicites. La
documentation API globale reste partielle.

Rejeu des journaux, contacts/fins de reprise, clôtures, données tardives,
autres rapports et tableaux de bord, réouverture et repli restent requis.
Les fixtures privées ne qualifient ni hébergement, ni capacité annuelle, ni
conservation Android des opérations. Aucun modèle ni migration ajouté.


## Dépôt durable des journaux de récupération — 25 octobre 2026

Le titulaire a accepté le dépôt du journal d’un accès retiré par un responsable
actuellement autorisé, avec motif et origine conservés, puis contrôle avant
intégration (décision complémentaire US-44 dans AGENTS.md). Le dépôt technique
est disponible. Le contrôle et le rejeu des ventes sont décrits dans la tranche
suivante ; les autres familles restent à réaliser.

| Route dans `/api/v1/shops/{shop_id}/recovery/` | Fonction |
| --- | --- |
| `POST journals/` | Conserver atomiquement un lot et son accusé durable. |
| `GET journals/` | Inspecter les dépôts de la récupération courante. |
| `GET journals/{id}/` | Relire l’accusé initial sans le modifier. |
| `GET operations/` | Inspecter les copies d’opérations conservées. |
| `GET operations/{id}/` | Lire une copie et ses dépendances déclarées. |

Le responsable doit avoir une session et une appartenance actuelles à cette
boutique, confirmées dans la génération courante. Ces contrôles précèdent la
lecture du corps et sont refaits dans la transaction d’écriture. Le vendeur
est refusé, même pour sa propre copie ; le responsable peut déposer celle du
vendeur retiré sans réactiver son compte, son installation ou ses permissions.
Le rôle technique seul n’accorde aucune appartenance commerciale. Une autre
boutique, une génération historique et un UUID inconnu restent inaccessibles
aux lectures métier. Le même JWT est refusé après retrait des droits.

### Format et accusé

L’entrée de version 1 contient `operation_id` pour le dépôt, `session_id`,
`shop_id`, `format_version: 1`, un `reason` non vide de 2000 caractères maximum
et `operations`. Chaque opération conserve son propre UUID `operation_id`,
les UUID `origin_installation_id` et `origin_user_id`, `operation_type`,
`occurred_at`, `origin_recorded_at`, `payload` et `dependencies` (liste vide
par défaut). Le responsable et l’installation qui déposent sont déterminés
par la session serveur, distincts de l’origine déclarée. Les UUID d’origine
ne créent pas de compte et ne constituent pas une preuve cryptographique.

Les dates sources exigent un fuseau explicite et sont normalisées en UTC.
Elles restent des observations déclarées : la capture ne valide pas une date
commerciale, un montant ou une quantité. Le type et le contenu source restent
opaques à ce stade ; aucune fonction métier n’est appelée selon ce texte.
Une dépendance absente du serveur peut être conservée sans objet fictif.

Limites de transport, sans quota commercial ni limite du journal total :
1 à 50 opérations par dépôt, corps JSON de 2000000 octets maximum, payload
objet de 1000000 octets UTF-8 canoniques maximum, profondeur 20 et au plus 100
UUID de dépendance par opération. Les journaux plus longs doivent être
fractionnés en dépôts distincts. Les clés JSON en double, UUID répétés dans
un lot, doublons de dépendances, versions non entières/inconnues, Unicode
invalide et champs de credentials sont refusés avant conservation. Le refus
d’un corps invalide ne fait pas perdre sa copie sur le téléphone.

L’accusé distingue l’UUID du dépôt de chaque UUID source et de son identifiant
interne de capture. Il conserve l’auteur actuel, l’installation actuelle,
le motif, la date serveur `received_at`, le compte du lot et celui des copies
nouvellement conservées. `capture_state: captured` atteste la capture
technique ; `business_effects_created: 0` décrit les effets de ce dépôt.
Cela ne signifie pas qu’une opération source est absente de la base métier :
elle peut déjà y figurer dans la sauvegarde restaurée. L’accusé historique
ne constitue ni une validation métier ni un état actuel de traitement.

### Retransmissions, collisions et isolation

Dépôt et copies sont commités dans la même transaction avant le 201. Une
retransmission du même UUID de dépôt, contenu, auteur et installation rend
le même accusé en 200. Changer le contenu ou l’origine avec cet UUID renvoie
409. Un autre UUID de dépôt peut transporter les mêmes opérations sources :
il reçoit son propre accusé et réutilise leurs identifiants de capture,
avec `new_operation_count: 0` si elles étaient toutes conservées.

L’unicité d’une opération source porte sur la session de récupération ; une
collision entre boutiques de cette session est refusée. Le hash canonique
inclut boutique, version, origines, dates, type, payload et dépendances.
Ordre des clés JSON et représentation équivalente d’un instant UTC ne
créent pas une différence de contenu. Un conflit annule le lot entier,
sans écraser l’ancienne copie ni conserver un préfixe du nouveau lot.
La reconnaissance des effets de vente par UUID est désormais disponible par
les routes d’intégration décrites ci-dessous. Les autres familles et la
qualification de restaurations successives restent requises.

Les listes appliquent autorisation et filtre boutique/génération avant
pagination SQL : 25 lignes par défaut, maximum 100, ordre création/UUID.
Le compte porte sur tout le filtre et les métadonnées `restoration` restent
présentes. Un lot de copies ne confirme aucun contact ni fin de récupération
d’un téléphone ; l’inventaire archivé des appareils reste une autre preuve.
Aucun bail hors connexion n’est renouvelé par ces routes.

### Preuves et limites

Migration additive `backups.0019_recovery_journal_capture` : deux modèles,
accusés protégés, unicité des UUID et contraintes SQLite sur version, motif,
formes JSON et longueur du hash. Passage depuis 0018 testé sur un registre
rempli, avec conservation des UUID/origines et du compte suspendu ; aucune
copie n’est inventée par la migration. Les 80 migrations historiques sont
inchangées. Manifeste des migrations et référence SQL reconstruits : 550
objets, SHA-256 `3d96f9a46d12a82eed800525125132eea4bec03c29b8112a4ae3fe986654a85b`.

Tests : [modèles](backups/tests/test_recovery_journal_models.py),
[API](backups/tests/test_recovery_journal_api.py),
[concurrence interprocessus](backups/tests/test_recovery_journal_concurrency.py),
[migration](backups/tests/test_recovery_journal_migration.py).
Qualification après durcissement Unicode : 18 tests et 32 sous-tests en
96,76 s ; qualification finale avec pannes SQL : 19 tests et 34 sous-tests
réussis en 58,42 s. Les pannes sont injectées dans les insertions et avant
la mise à jour de l’accusé, via des déclencheurs SQLite temporaires sur la
base de test : aucune partie du lot ne subsiste, et sa reprise réussit. Deux processus SQLite testent même dépôt, autre copie et contenu
contradictoire ; dépôts/copies ne sont ni doublés ni partiellement écrits.
Les pages sont exercées sur 101 copies sans requête par ligne. Les vrais
corps d’accusés, listes, fiches et erreurs sont validés contre OpenAPI.

Le [parcours WSGI privé](verification/restore_access_smoke.py), relancé après
le durcissement Unicode, démontre le
dépôt par le responsable d’une origine suspendue, l’accusé rejoué, une autre
copie sans nouveau contenu, le conflit 409, l’inspection paginée et le retrait
ultérieur du droit de dépôt. Aucun événement de vente, stock, caisse ou
compte n’est créé par la capture. Une clé Unicode invalide est refusée en
400 sans erreur serveur. Le worker de référence isolé a terminé en 110,72 s
et comparé exactement ses 550 objets et métadonnées au fichier courant.
Les deux premiers appels de vérification ont échoué ; leurs sorties
génériques ne donnent pas la cause, et aucun résultat positif n’en est
déduit. Les fixtures ne démontrent ni OTP ni
activation après restauration. Pendant la maintenance réelle, toutes ces
routes métier restent en 503 ; la réouverture contrôlée reste requise.

Le rejeu et ses dépendances métier, la validation/refus, les réservations des
sorties en attente et leur rapprochement, les contacts/fins de reprise, les
clôtures et les opérations tardives restent à réaliser. Ne pas considérer
cet accusé comme une autorisation de purger les originaux ; la conservation
Android minimale de 30 jours et des opérations en attente reste à qualifier.
La capture ne constitue pas une récupération métier complète.

OpenAPI : 137 chemins/183 opérations, 503 sur toutes les opérations ; 146
sans résumé/description propre et onze seulement avec erreurs 4xx explicites.
La documentation globale reste partielle. Django check, fraîcheur migrations
et validation du schéma réussis ; les nouvelles erreurs sont traduites FR/EN.


Régression élargie du dépôt : 372 tests et 472 sous-tests réussis en 1583,41 s,
onze avertissements connus de fixtures DATABASES ; JUnit 844 cas, aucun
échec/erreur/ignoré. Elle couvre les sauvegardes/restaurations, preuves,
installation, accès, inventaire, relevés, maintenance et revalidation vendeur.
Cette collecte précédait le durcissement Unicode et le dernier test de panne ;
les 19 tests/34 sous-tests finaux, terminés après ces changements, couvrent
l’ensemble de la capture courante. La suite générale de 987 tests est
historique, antérieure à 0018, et ne qualifie pas ce nouveau code.

Pour rejouer les vérifications ciblées depuis la racine :

```powershell
./.venv/Scripts/python.exe -X utf8 -m pytest backend/backups/tests/test_recovery_journal_models.py backend/backups/tests/test_recovery_journal_api.py backend/backups/tests/test_recovery_journal_concurrency.py backend/backups/tests/test_recovery_journal_migration.py -q
```

La commande standard `tools/restore_schema_manifest.py --output backend/restore_schema.json --check`, relancée seule après la régression, a terminé avec succès : `schema_reference: current`. La génération et la vérification portent sur une base privée neuve ; aucun schéma ou fichier métier actif n’a été remplacé.

## Intégration contrôlée des ventes récupérées — 25 octobre 2026

Le dépôt reste une capture sans effet métier. Un responsable actuel de la
boutique peut désormais examiner puis intégrer une vente conservée, avec un
motif et une décision versionnée. L’adaptateur disponible accepte uniquement
`operation_type: sale`. Les autres types renvoient 400 et gardent leur copie.

| Route sous `/api/v1/shops/{shop_id}/recovery/` | Contrat |
| --- | --- |
| `GET operations/{id}/integration-preview/` | Vérifier les faits figés, afficher effets stock/caisse/dette, obstacles, version et jeton d’aperçu ; aucune écriture métier. |
| `POST operations/{id}/integrate/` | Recevoir `operation_id` de décision, `previous_version`, `preview_token` et `reason` ; revérifier droits, intégrité et contexte avant tout effet. |

Les schémas `FrozenSalePayload`, `FrozenSaleSnapshot`,
`RecoveryIntegrationPreview` et `RecoveryIntegrationReceipt` décrivent le
format dans [le code du contrat](backups/services/recovery_sale_snapshot.py)
et [les réponses OpenAPI](backups/api/recovery_integration.py). Le payload
est un objet de version `schema_version: 1`, avec `original_request` au
format de création d’une vente et `sale` contenant ses faits figés : UUID,
mode/client, échéance UTC de la dette éventuelle, totaux, lignes et paiements.
Chaque ligne conserve nom, unité, format, facteur, version, prix, remises et
quantités. Chaque paiement conserve montant, moyen nommé si nécessaire,
référence et éventuels remis/rendu ; les deux derniers valent null lorsqu’ils
étaient absents. Le montant complet de la ligne et la répartition des remises
sont revérifiés exactement, sans consulter les tarifs actuels. Une vente à
crédit entièrement payée n’a aucune dette ni échéance de dette, même si sa
requête d’origine contenait une date. Pour une dette, la cohérence avec la
date demandée est vérifiée dans le fuseau actuel de la boutique ; un changement
historique de fuseau n’est pas encore rapproché par cet adaptateur.

L’aperçu est bloqué si une référence ou dépendance manque, si l’unité de base
a changé, si un UUID est en conflit, ou si une correction d’inventaire
enregistrée pendant cette récupération doit être rapprochée. Les lectures de
références et de corrections restent limitées à la boutique courante. Une
collision d’UUID située ailleurs ne révèle aucun reçu ou journal de cette
autre boutique. Un obstacle ne supprime pas la capture et n’applique ni
remboursement, ni correction de quantité, ni compensation automatique.

Le jeton lie l’aperçu à sa capture, son empreinte, sa version et au contexte
catalogue/stock examiné. Changer ce contexte impose un nouvel aperçu. La
transaction réserve l’écriture SQLite avant la nouvelle vérification des
droits. Vente, lignes, paiements, stock, coût calculé à la date effective,
dette impayée/excédent éventuel, relevé client, alerte, lien d’origine et
décision sont écrits ensemble. Un échec SQL annule l’ensemble des nouveaux
effets. Un stock négatif reste conservé avec son besoin de régularisation.
Les coûts des opérations plus récentes ne sont pas réécrits automatiquement :
leur révision motivée utilise le parcours de recalcul des coûts existant.
La récupération reste provisoire.

Les UUID de l’ancien compte et de l’ancienne installation peuvent être absents
de la base restaurée. Ils restent des références historiques, sans création
de compte, activation ou droit. Les clés étrangères d’écriture désignent le
responsable actuel et son installation vérifiée ; `RecoveredOperationOrigin`
relie cette écriture à sa capture protégée. Les réponses de vente, les
mouvements de caisse et les relevés clients présentent l’origine historique.
La caisse personnelle filtre également cette origine : le responsable qui
intègre ne s’attribue pas l’encaissement du vendeur. Le journal d’activité
conserve séparément le responsable, la décision motivée et l’origine déclarée.

Si l’UUID source correspond déjà à une vente dont requête, identité et faits
figés concordent, la décision `already_known` ne crée aucun second effet.
Cette reconnaissance utilise son accusé de création, même si la vente a été
annulée depuis. `business_receipt` est donc une preuve historique ; consulter
la vente pour ses retours, annulation et soldes actuels. Une première décision
renvoie 201 ; reprendre son UUID avec le même contenu/auteur/installation
renvoie son accusé initial en 200 après contrôle des droits actuels. Un nouveau
contrôle motivé utilise un nouvel UUID et la version fraîche. Un contexte,
contenu ou UUID contradictoire renvoie 409.

Migration additive `backups.0020_recovery_integration_decisions` : deux
modèles, contraintes d’unicité, version, motif, résultat, empreintes et formes
JSON. Passage 0019→0020 testé sur une capture existante : copie et identités
conservées, compte suspendu et installation révoquée inchangés, aucune vente
ou intégration inventée. Les 80 migrations historiques restent identiques.
Référence SQL reconstruite : **562 objets**, SHA-256
`f4210a6e850cf3065881f7745ec0cb5072eeb311d0c70cf66e588cc4406f19e6` ;
manifeste des migrations reconstruit également.

Les limites de capture sont passées de 64000 à 1000000 octets canoniques par
payload, et de 1000000 à 2000000 octets par corps, après un test rouge avec
les 500 lignes autorisées et leurs libellés Unicode complets. Le
[parcours HTTP réel](verification/recovery_sale_smoke.py) a intégré 500 lignes
avec une requête de 751320 octets UTF-8 : 250000 FCFA, une vente, un mouvement
de caisse, une alerte et un stock final de −497000 milli-unités, conservés
après retransmission et nouveau contrôle `already_known`. Le compte source
retiré reste inactif et son téléphone révoqué ; autre boutique et retrait
ultérieur du droit du responsable renvoient 404. Cette fixture privée fournit
une session fraîche ; elle ne qualifie ni OTP ni activation après restauration.

Preuves : [modèles](backups/tests/test_recovery_integration_models.py),
[arithmétique sans base](backups/tests/test_recovery_sale_snapshot.py),
[API et isolation](backups/tests/test_recovery_sale_integration.py),
[concurrence SQLite interprocessus](backups/tests/test_recovery_integration_concurrency.py),
[migration](backups/tests/test_recovery_integration_migration.py).
Douze tests API et cinq sous-tests ont réussi, dont annulation déjà connue,
rollback complet, origine absente, droits actuels, fuite interboutique corrigée,
caisse personnelle et vrais corps OpenAPI. Quatre tests arithmétiques/neuf
sous-tests, trois tests de modèles/dix sous-tests, le test concurrent/deux
sous-tests et le test de migration ont également réussi. Les preuves finales
de régression sont consignées dans [le registre TDD](../analysis/backend-tdd-progress.md).

Régression élargie courante : **394 tests et 330 sous-tests réussis en
321,26 s**, ventes, caisse, clients, stock et nouvelles captures/intégrations.
JUnit : 394 cas/394 méthodes, aucun échec/erreur/ignoré. Un contrôle ajouté
après cette collecte a aussi réussi en 31,70 s : une nouvelle génération de
récupération reconnaît la vente de la précédente sans second effet, avec un
seul lien d’origine. Cette preuve de service ne vaut pas qualification complète
de deux restaurations successives. La commande standard de fraîcheur de la
référence a terminé avec `schema_reference: current`.

Contrôles des sauvegardes concernés par ces nouvelles tables : **35 tests et
24 sous-tests réussis en 386,11 s**, archives chiffrées, audit SQL, préparation
des copies de restauration et métadonnées des relevés. La suite complète de
tous les modules n’a pas été relancée dans cette tranche.

OpenAPI courant : **139 chemins, 185 opérations**, chacune décrit le refus
503 sous maintenance. La validation stricte a réussi après résolution des
collisions d’énumérations, sans masquer les avertissements ; les erreurs
nouvelles sont traduites FR/EN. La documentation globale reste partielle :
146 opérations sans résumé/description et 13 avec erreurs 4xx explicites.

US-44 reste partielle : autres familles et dépendances métier, classement et
réservations en attente, refus historisés, rapprochement d’anciennes
corrections, fin de reprise par téléphone, clôtures et arrivées tardives
restent requis. La réouverture contrôlée et le repli après installation ne
sont pas réalisés : ces routes restent en 503 pendant une maintenance réelle.
La conservation Android des originaux, les budgets complets et l’hébergement
restent à qualifier. Aucun déploiement ni envoi SMTP externe dans cette tranche.

## Mise en attente et refus des ventes récupérées — 26 octobre 2026

Cette tranche prolonge US-15, US-37 et US-44. Le responsable actuel peut mettre
une vente récupérée en attente avant de l’approuver. Sa quantité est réservée
une seule fois par produit ; son montant, l’argent déclaré et son éventuelle
dette restent distincts des ventes, de la caisse et des dettes validées.
Le dépôt technique conserve son contrat sans effet métier.

| Route sous `/api/v1/shops/{shop_id}/recovery/` | Fonction |
| --- | --- |
| `GET operations/{id}/integration-preview/` | Afficher `hold_id`, `review_state`, `review_version`, `can_hold`, `can_refuse`, effets d’approbation et jeton courant. |
| `POST operations/{id}/review/` | Demander `state: pending` ou `refused`, avec UUID de commande, motif, jeton et version d’examen. |
| `GET operations/{id}/review-decisions/` | Consulter les décisions motivées et leurs accusés historiques, paginés par version/UUID. |
| `GET sales-review/?state=pending` | Consulter la file et ses totaux ; filtre facultatif `pending`, `refused` ou `validated`. |
| `POST operations/{id}/integrate/` | Approuver la vente et remplacer atomiquement sa réservation par son mouvement validé. |

Deux compteurs sont distincts : `review_version` concerne les décisions de
mise en attente/refus/validation ; `version` concerne les accusés d’intégration.
Envoyer le premier comme `previous_version` à `review/`, et le second à
`integrate/`. Les deux écritures utilisent le `preview_token` frais. Un jeton
ancien, une projection de réservation altérée ou une commande contradictoire
renvoie 409. Les droits actuels et l’intégrité de la capture sont contrôlés
avant tout réessai ; l’autorisation précède aussi la lecture du corps soumis.

Une source prête à être intégrée peut être mise en attente. Les mêmes références,
unités, limites et dépendances sont vérifiées. Les copies opaques, références
manquantes et rapprochements bloqués restent conservés, mais cet adaptateur ne
les classe pas encore en attente. L’absence de réservation pour ces copies ne
prouve aucune complétude ; la récupération reste provisoire.

`RecoverySaleHold` lie les UUID stables de l’opération **et de la vente** à une
seule réserve, même entre copies ou générations de récupération. Plusieurs
lignes du même produit sont regroupées. Changer seulement l’UUID d’opération
ne permet pas de réserver deux fois le même UUID de vente. L’endpoint ordinaire
de création de vente refuse également une identité déjà en examen ; il ne
permet pas de contourner l’approbation du responsable. Après une approbation
autorisée, son réessai habituel reconnu garde le même accusé sans second effet.

L’approbation remplace la réserve avant le calcul du disponible final, dans la
même transaction que stock, vente, paiement, dette/avoir éventuel, coût,
historique et accusés. Exemple : validé 3000, réservé 1000, disponible 2000 ;
après approbation, validé 2000, réservé 0, disponible toujours 2000. L’alerte
créée lors du franchissement connu n’est pas répétée. Un échec de l’accusé
annule aussi les effets et restaure la réservation en attente.

Un refus exige un motif, conserve la source et une décision protégée ; les
réservations deviennent `rejected` et restent déduites du disponible.
L’argent déclaré reste visible dans la catégorie `refused`, sans remboursement,
recette validée ou remise en stock automatique. Une réouverture par simple
commande `pending` ou une approbation directe est refusée. Le rapprochement et
la régularisation distincte d’un refus restent à réaliser ; aucune règle de
libération encore ouverte dans AGENTS.md n’est fixée ici.

La file applique filtre boutique/état avant pagination SQL (25 lignes par
défaut, 100 maximum, ordre création/UUID). Les totaux portent sur tout le
filtre et restent séparés par état. FCFA exacts sous forme de chaînes décimales,
y compris au-delà d’int64 ; aucune somme n’est limitée à la page affichée.
Ces valeurs sont les faits d’origine : même `validated` ne représente ni le
chiffre d’affaires courant après retours, ni un solde de caisse ou de dette.
Consulter les relevés métier pour les montants actuels. Les payloads ne sont
pas chargés par ligne, et les métadonnées de restauration restent présentes.

Les réserves et leur historique restent visibles dans la boutique après une
nouvelle génération. `origin_capture_id` identifie la copie historique ;
`current_capture_id` vaut null si aucune copie n’a encore été déposée dans la
génération actuelle. Aucun compte d’origine ni droit n’est créé ou réactivé.
La lecture et les décisions restent réservées au responsable actuel.

Migrations additives `backups.0021_recovery_sale_holds` et
`0022_recovery_sale_hold_identity` : les 80 migrations historiques sont
inchangées. Le passage conserve anciennes ventes, origines, décisions,
réservations et suspensions ; aucune classification n’est inventée.
Pour des examens 0021 existants, 0022 extrait l’UUID de vente déclaré, sans
modifier source, montants, état ou accusé. UUID invalide/doublon : arrêt atomique
pour rapprochement explicite, sans fusion ou nouvel identifiant automatique.
La migration lit uniquement l’identifiant JSON en SQL, par lots bornés.
Les accusés 0021 restent inchangés ; `source_sale_id` peut donc être absent de
ces seuls accusés historiques, comme le précise OpenAPI.

Preuves : [modèles](backups/tests/test_recovery_sale_hold_models.py),
[API](backups/tests/test_recovery_sale_hold_api.py),
[concurrence interprocessus](backups/tests/test_recovery_sale_hold_concurrency.py),
[migration des données existantes](backups/tests/test_recovery_sale_hold_migration.py),
[reprise des UUID anciens](backups/tests/test_recovery_sale_identity_migration.py).
Les tests couvrent reprise, autre génération, produits répétés, états séparés,
totaux dépassant int64, requêtes bornées, refus motivé, rollback et droits retirés.
Approbation et refus concurrents ne produisent qu’une décision finale.
Les vrais corps d’accusés, files, historiques et erreurs sont validés contre
OpenAPI ; refus et nouveaux libellés sont traduits FR/EN.

Le [parcours HTTP réel](verification/recovery_sale_smoke.py), sur SQLite privée
migrée, a mis 500 lignes en attente puis les a approuvées : 250000 FCFA,
un seul encaissement, une seule alerte et disponible −497000 milli-unités.
Un second cas refusé conserve 500 FCFA déclarés séparés et 1000 milli-unités
réservées : disponible −498000, caisse validée toujours 250000. Les copies,
réessais et nouveaux contrôles ne doublent aucun effet ; source retirée et
installation révoquée restent inchangées. Session fournie par la fixture :
OTP, Android et réouverture après restauration non qualifiés.

Schéma SQL : **578 objets**, SHA-256
`262f71ed79c6539508d5666ab9294495416df10a55bc37025abcf73806680267`.
OpenAPI strict validé : **142 chemins/188 opérations**, refus 503 décrit sur
les 188 ; 146 opérations sans résumé/description et 16 avec erreurs 4xx
explicites, donc documentation globale encore partielle.

US-37/44 restent partielles : classement des copies bloquées, autres familles
d’opérations, régularisation des refus et anciennes corrections, conflits de
catalogue, fin de reprise, clôtures, tardives, réouverture et repli restent
requis. Les écritures restent en 503 pendant une maintenance réelle. Les
résultats finaux de régression et de fraîcheur sont consignés dans
[le registre TDD](../analysis/backend-tdd-progress.md).

Qualification finale de la mise en attente : **414 tests et 352 sous-tests**
de régression métier réussis en 344,26 s, puis **35 tests/24 sous-tests**
archives, audit SQL, préparation de restauration et relevés en 400,12 s.
L’optimisation finale de la migration a ses deux tests/deux sous-tests séparés.
449 méthodes distinctes dans les rapports finaux, aucun échec/erreur/ignoré.
Le contrôle standard de référence reconstruit une base privée et confirme
`schema_reference: current`, sans charger les paramètres métier ni créer la
base leurre. La suite générale entière n’a pas été relancée.

## Rapprochement d'une réservation refusée — 26 octobre 2026

La décision complémentaire US-37/44 autorise un rapprochement explicite avec
une perte ou une correction négative d'inventaire déjà validée du même produit.
Ce lien libère seulement une quantité déjà comptabilisée et disponible pour
rapprochement. Il ne crée aucun nouveau mouvement de stock ou de caisse, et ne
valide pas la vente refusée. Ses montants déclarés restent séparés.

Pour le responsable actuel, partir de
`/api/v1/shops/{shop_id}/recovery/operations/{capture_id}/integration-preview/`
et relever `stocks[].reservation_id`. Cette lecture fournit aussi l'identifiant
lorsque le reçu historique d'examen n'en contenait pas.

Sous `operations/{capture_id}/reservations/{reservation_id}/` :

- GET `reconciliation-preview/?movement_id={movement_id}&quantity_milli=1000`
  expose les quantités d'origine, déjà rapprochées et restantes, les capacités
  du mouvement, le stock avant/après, `can_apply`, `issues` et `preview_token`.
- POST `reconciliations/` confirme `operation_id`, `movement_id`,
  `quantity_milli`, le `preview_token` exact et un `reason` obligatoire.
- GET `reconciliations/` lit les reçus d'origine : pagination SQL 25/100,
  ordre date/UUID. L'aperçu conservé dans un reçu reste historique.

La liste HTTP des mouvements physiques se trouve sous
`products/{product_id}/stock-shortage-reviews/movements/`, à la racine de la
boutique. Elle est paginée ; `source_id` identifie notamment la perte d'origine.
Parcourir les pages pour rechercher un événement absent de la première page.

Seuls les responsables actuellement autorisés peuvent lire l'aperçu, confirmer
ou lire l'historique. Les copies et UUID d'origine ne rendent aucun accès. La
génération actuelle exige une copie déposée dans cette génération, mais les
quantités déjà rapprochées dans les générations précédentes restent comptées.

Le mouvement est contrôlé contre sa perte ou sa correction validée, pas
seulement son libellé. Les corrections successives d'un même comptage partagent
sa capacité nette ; une révision positive limite la capacité restante. Les
réserves d'autres départs ne peuvent consommer une seconde fois cette capacité.
Un aperçu périmé, une quantité excessive ou une collision d'UUID retourne 409
atomiquement. Le réessai exact retrouve le reçu après contrôle des droits.
La commande ne peut utiliser l'UUID d'une opération déjà capturée.

Le disponible et les sous-totaux de réserves déduisent les liens de
rapprochement sans réécrire la réserve initiale, le refus ou le mouvement. Les
totaux restent exacts au-delà d'int64. La transaction actualise aussi l'état du
seuil : une remontée peut permettre une nouvelle alerte lors d'un franchissement
ultérieur, sans notifier à chaque rapprochement.

Migration additive `stock.0003`, données historiques et retraits conservés.
Les preuves, commandes de reproduction et limites sont dans
[le rapport de qualification](../analysis/backend-reconciliation-2026-10-26.md).
WSGI réel sur base SQLite privée migrée : vente récupérée de 500 lignes,
250000 FCFA validés, puis vente refusée à 500 FCFA déclarés hors caisse validée.
Perte distincte et rapprochement explicite de 1000 milli-unités : réserve
restante zéro, disponible −498000, aucun mouvement physique ou encaissement
ajouté par le rapprochement, aucune réactivation de l'origine. L'argent du
refus reste à régulariser ; les clôtures et réouverture/repli ne sont pas
qualifiés par ce parcours.

## Reprise des encaissements clients — 27 octobre 2026

Les routes d'aperçu et d'intégration acceptent aussi `customer_collection`.
Le payload version 1 contient `original_request` et `collection`, instantané
du reçu initial avec moyens, remis/rendu éventuels, affectations demandées et
effectivement appliquées. Un solde courant après régularisation ne remplace
pas cet instantané. Le contrat complet et les commandes sont décrits dans
[la qualification des encaissements récupérés](../analysis/backend-recovery-collections-2026-10-27.md).

L'aperçu fournit `allocation_impacts`, les entrées nettes par moyen et l'excédent
non affecté. La confirmation motivée applique chaque ligne dans son ordre,
au plus à hauteur du montant appliqué à l'origine et du solde actuel de la
dette. Une avance initialement non affectée reste à régulariser explicitement.
L'argent reçu demeure entier même si des dettes sont déjà soldées ; aucune
autre dette ni remboursement n'est traité automatiquement.

Les effets sur activité, dettes, caisse, relevé, origine et décision sont
atomiques. L'origine reste une référence historique ; le responsable courant
est le rédacteur contrôlé. Les listes d'encaissements conservent cette origine
et montrent les soldes courants. Un réessai ou une nouvelle génération
correspondante est reconnu sans effet financier supplémentaire, y compris
après une affectation récupérée réduite ou un remboursement ultérieur.

Les UUID de source capturée et de décision d'intégration ne peuvent servir à
contourner l'examen via les commandes de reçu communes. La migration additive
`backups.0026` ajoute un index de recherche d'UUID, sans effacer les captures
ou modifier les droits. Référence SQL reconstruite à 640 objets.

Preuves : 32 tests/20 sous-tests ciblés, 3 tests/4 sous-tests de concurrence et
migration réussis ; WSGI réel privé réussi avec accès retiré, boutique étrangère,
aperçu périmé, encaissement concurrent, reprise et remboursement réel.
Régression métier/common/récupération : 849 tests/1127 sous-tests réussis,
six avertissements connus des fixtures `DATABASES`. Restauration complète
jusqu'à réouverture contrôlée rejouée avec succès sur le schéma à 640 objets.
La capture brute n'est toujours pas une validation. La file financière des
sources prêtes est traitée dans le complément ci-dessous ; régularisation des
refus, autres familles, clôtures et arrivées après clôture restent requises.

## Examen des encaissements récupérés — 27 octobre 2026

Les encaissements prêts peuvent être explicitement mis en attente ou refusés
via `operations/{capture}/collection-review/`, avec l'UUID distinct de la
décision, `review_version` comme `previous_version`, l'empreinte d'aperçu et un
motif. `collections-review/` liste les examens par boutique, état/client, avec
totaux source exacts sur tout le filtre et pagination SQL. L'historique sous
`operations/{capture}/collection-review-decisions/` conserve les décisions
antérieures, y compris à travers les générations pour une même source.

Attente et refus n'écrivent ni caisse validée, ni dette, ni relevé financier,
ni stock ; un refus ne rembourse pas le client et ne règle pas le sort de
l'argent. La validation via `integrate/` lie dans la même transaction la
décision d'examen, l'intégration et tous ses effets métier. Les montants de
la file restent les faits d'origine, distincts des effets effectivement
possibles après restauration et des soldes courants. Un UUID d'encaissement
réservé ne peut être repris par une création native avec un nouvel UUID
d'opération pour contourner cet examen.

La migration additive `backups.0027` ajoute deux tables et leurs contraintes,
sans reclasser les anciens paiements, captures ou droits. Référence reconstruite
et vérifiée à 655 objets. Unfold offre une consultation technique FR/EN en
lecture seule ; aucune décision commerciale par simple édition de modèle.

Qualification ciblée : 20 tests/28 sous-tests, processus concurrents et
migration inclus ; WSGI réel refusant 5000 FCFA sans effet financier ajouté,
restauration complète jusqu'à réouverture réussie sur le nouveau schéma.
Régression élargie : 866 tests/1153 sous-tests réussis, six avertissements connus
des fixtures `DATABASES` ; course validation/refus qualifiée séparément.
Les captures bloquées, la régularisation des refus et les autres familles
de reprise restent requises. Le contrat, les commandes et les preuves sont
dans [le rapport d'examen](../analysis/backend-recovery-collection-reviews-2026-10-27.md).

## Reprise des clients et dettes d'ouverture manquants — 27 octobre 2026

Les journaux acceptent désormais les familles `customer_creation` et
`customer_opening_debt` dans les routes d'aperçu/intégration existantes. Elles
portent demande et reçu initiaux : fiche version 1/dates UTC, puis dette avec
montant restant initial entier, échéance figée et version 1, sans vente.
Le responsable contrôle et intègre chaque opération avec motif/version/token ;
il relit l'aperçu des dépendances devenues disponibles. Un encaissement bloqué
ne crée pas lui-même sa fiche ou sa dette.

Les noms/téléphones identiques ne rapprochent aucune fiche. L'intégration
connue conserve les données actuelles corrigées/encaissées et rend l'ancien
accusé ; elle ne restaure aucun ancien solde ou rôle. Les traces natives de
création client enregistrent désormais leur installation, sans changer la
demande canonique ni réécrire les reçus historiques. L'absence ancienne est
explicitement non vérifiée, et les UUID déclarés d'un nouveau journal ne
deviennent pas une preuve d'authentification. Un lien ou une activité étrangère
est contrôlé par boutique avant chargement de ses données.

Qualification finale : 205 tests/192 sous-tests clients/ventes/caisse/dépendances,
plus 2 tests/16 sous-tests sur les projections d'origine contradictoires.
Une origine incohérente avec la boutique bloque explicitement la lecture par
409 `origin_reference_conflict`, sans identité étrangère ni correction de montant.
Les contrats de ces sept opérations exposent ce code, traduit FR/EN.
Régression élargie avant les derniers durcissements : 885 tests/1183 sous-tests,
six avertissements connus `DATABASES` ; les lots recouvrants ne sont pas additionnés.
Requêtes SQL, deux processus, rollback, données modifiées après création et
contrats réels vérifiés. WSGI complet
T0/T1/restauration/reprise réussi sur le code final : 6000 FCFA de dette,
1000 non affectés, 3000 en caisse, aucun stock/vente ajouté par les dépendances.
Les opérations T1 sont des appels natifs HTTP avant remplacement ; leurs reçus
survivent dans le processus de test distinct du fichier métier restauré.
Cette preuve ne qualifie pas le journal Android ni l'OTP. Schéma inchangé à
655 objets ; aucune migration ajoutée.
Le contrat, les preuves et les limites sont dans
[le rapport des dépendances clients](../analysis/backend-recovery-customer-dependencies-2026-10-27.md).

## Reprise des corrections clients et échéances — 27 octobre 2026

Les mêmes routes d'aperçu/intégration acceptent `customer_update` et
`customer_due_change`. La première fournit demande et états avant/après
stricts de la fiche, la seconde demande et delta d'échéance/version sans
montant. Cibles et dépendances doivent exister ; faits avant et version
doivent correspondre au serveur. Les conflits restent déposés et bloqués,
sans choix automatique entre corrections. Une modification de l'état actuel
ou de l'indice de doublon impose un nouvel aperçu.

La correction garde l'UUID et ne modifie que les champs demandés. Le report,
l'ajout ou le retrait d'échéance conserve montant et solde actuels, avec ligne
de relevé nulle datée. Activité, origine et décision sont atomiques. Une
opération déjà connue rend son premier accusé sans rétablir les anciennes
coordonnées, dates ou soldes. Une ancienne correction sans installation
enregistrée reste signalée non vérifiée ; aucun compte n'est réactivé.

Qualification ciblée : 24 tests et 41 sous-tests distincts réussis, versions,
rollback, isolation, droits actuels, FR/EN et courses à deux processus inclus.
Restauration HTTP/console T0/T1 étendue à cinq opérations réussie : échéance
créée après paiement en T1 puis reprise avant lui, accusé temporaire 8000,
dette finale 6000, crédit 1000 et caisse 3000, sans seconde écriture financière.
Les reçus survivent dans le processus privé, pas dans un journal Android
qualifié ; OTP et clôture restent non démontrés par cet exercice.
Le contrat des payloads, les preuves et les limites sont dans
[le rapport des révisions clients](../analysis/backend-recovery-customer-revisions-2026-10-27.md).
Régression clients/common/récupération et audit SQL : 327 tests/806 sous-tests
réussis, six avertissements connus `DATABASES`. La collecte précède seulement
le dernier test de retrait, qualifié dans le lot final de quinze méthodes ;
aucune somme des lots recouvrants. Django/migrations/gettext et OpenAPI strict
passent, aucune migration ajoutée, six familles d'instantanés documentées.

## Remboursements clients récupérés — 27 octobre 2026

La famille `customer_refund` reprend un remboursement réel sur avance de
paiement ou crédit client de vente, avec source unique, demande/motif,
montant/moyen, avant/après initiaux et date UTC. Aperçu et décision actuels
restent requis. Un disponible modifié impose un nouvel aperçu ; le montant
réel ne subit aucun plafonnement. Absence ou insuffisance de source conserve
la copie brute et bloque l'effet pour examen, sans dette ni remboursement
fictifs. Son traitement et celui des remboursements d'avoirs de retour restent
à réaliser.

Source, débit du crédit, caisse négative, relevé, origine et décision sont
atomiques. Reçu initial et faits source restent distincts des soldes actuels,
y compris entre générations et après remboursement natif ultérieur. Les
historiques des régularisations montrent l'auteur d'origine ; un lien
contradictoire avec la boutique refuse la lecture en 409.

La comparaison commune des faits conserve maintenant une valeur nulle au
lieu d'essayer de décoder un corps HTTP vide. Le défaut avait été reproduit
pour les remboursements et l'ajout de première échéance. 36 tests/54 sous-tests
ciblés passent après correction. Le parcours complet T0/T1/restauration/reprise
à six opérations passe : dette 6000, crédit 500, caisse +3000 puis −500,
six origines/douze décisions, aucun second effet. Anciennes sessions retirées,
B non confirmée inaccessible, état provisoire conservé ; aucun OTP/SMTP réel.
Contrats, artefacts, qualification élargie et restes :
[rapport des remboursements](../analysis/backend-recovery-customer-refunds-2026-10-27.md).
Régression finale : 314 tests/741 sous-tests réussis, aucun avertissement,
échec/erreur/ignoré, courses incluses. Test ajouté après collecte : crédit
manquant recréé par vente reprise, puis remboursement, un test réussi.
Les lots se recouvrent et ne sont pas additionnés.

## Affectations explicites récupérées — 27 octobre 2026

La famille `customer_allocation` reprend une affectation d'avance de paiement
ou de crédit de vente. Payload : demande d'origine, source unique et états
historiques avant/après, montant total, date UTC et lignes ordonnées avec
UUID/dette/montant. Elle ne déplace aucun argent. L'aperçu compare crédit et
dettes actuels ; plusieurs lignes d'une même dette sont cumulées avant
contrôle, sans perdre leurs identités. Source/dette insuffisante ou absente
bloque toute l'application et conserve la copie pour examen.

Le responsable actuel contrôle token/version/motif ; soldes, lignes, activité,
relevé, origine et décision sont atomiques. Relevé net zéro, dette et crédit
diminués du même total, aucun cash/stock/vente/paiement nouveau. Reconnaissance
des reçus natifs ou repris, après paiement ultérieur ou nouvelle génération,
sans second effet. Les résolutions d'incompatibilité et règlements d'avoirs
de retour restent requis.

Qualification ciblée après correction du contrat de moyen vide d'affectation :
31 tests/60 sous-tests réussis. Restauration réelle à sept opérations réussie,
jusqu'à affectation 150 + 250 : dette 5600, crédit 100, caisse nette 2500
inchangée par l'affectation. Sept origines/quatorze décisions, deux lignes
d'affectation, aucun second effet. Droits actuels limités à A, état provisoire,
anciens accès retirés, aucun OTP/SMTP réel. Contrat, commandes et limites :
[rapport des affectations](../analysis/backend-recovery-customer-allocations-2026-10-27.md).
Régression finale : 328 tests/775 sous-tests réussis sans avertissement,
échec/erreur/ignoré, dont 13 tests/25 sous-tests de concurrence. Test ajouté
après collecte : crédit manquant recréé par vente reprise, puis affectation
sans nouvel argent, réussi séparément. Lots recouvrants non additionnés.

## Règlements d'avoirs de retour récupérés — 27 octobre 2026

`return_credit_settlement` reprend un remboursement réel ou une affectation
sur un retour commercial déjà présent, avec vente/retour/client nullable,
montants et crédit avant/après, dates effectives et d'enregistrement source,
moyen ou lignes de dette. Les droits actuels, dépendances et soldes restent
contrôlés. Absence ou insuffisance bloque tout et conserve la copie brute.
Le remboursement anonyme ne crée pas de client ; l'affectation en exige un.
Les répétitions de dette restent interdites dans ce parcours natif distinct.

Règlement, caisse éventuelle, dettes, relevé pour client identifié, origine
et décision sont atomiques. La création source reste dans le snapshot et
l'accusé de reprise enregistre sa création actuelle. Les reçus connus sont
rendus sans nouvel effet, même après autre règlement/génération. Aucune
création de retour commercial, réception physique ou stock par ces actions.
Les listes/détails natifs projettent l'auteur historique et refusent en 409
une origine contredisant leur boutique, sans identité étrangère renvoyée.

20 tests/37 sous-tests ciblés réussis et restauration réelle réussie avec
avoirs déjà à T0, puis trois règlements T1 perdus et repris. Dette 6000,
avoir identifié 2000, anonyme 4500, caisse nette 8500 ; trois mouvements de
stock T0 inchangés, aucune réception physique et aucun client anonyme créé.
Le scénario précédent garde ses assertions T0 vide ; ce nouveau mode exige
son T0 exact et zéro règlement T1 après remplacement. OTP/SMTP non exercés,
récupération provisoire. Contrat, procédure et limites :
[rapport des règlements de retour](../analysis/backend-recovery-return-settlements-2026-10-27.md).
Régression finale : 343 tests/813 sous-tests réussis, aucun avertissement,
échec/erreur/ignoré, dont 16 tests/31 sous-tests de concurrence. Les lots se
recouvrent et ne sont pas additionnés. Aucun modèle/migration ajouté ;
Django, fraîcheur migrations/manifeste/gettext et OpenAPI strict passent.

## Retours commerciaux perdus récupérés — 27 octobre 2026

La famille `sale_return` reprend un retour commercial absent de la sauvegarde,
avec vente/client nullable, montants total/dette réduite/avoir, dates et lignes
de quantités/valeurs d'origine. L'aperçu compare les lignes réellement vendues,
les quantités déjà retournées, l'arrondi cumulatif exact et la dette actuelle.
Si la répartition source dette/avoir ou la valeur cumulée ne peut être conservée,
la copie reste bloquée pour résolution explicite. Aucun tarif actuel substitué,
aucun plafonnement automatique ni seconde déduction.

Validation actuelle, retour/lignes, réduction de dette, relevé identifié,
activité/origine et décision restent atomiques. Aucune caisse ni réception
physique. Un retour anonyme ne crée aucun client/relevé. Les reçus connus
restent reconnus après règlement, annulation ou nouvelle génération sans
réinitialiser l'avoir. Date de création source conservée, date de reprise
distincte. Les listes et détails natifs montrent l'auteur source ; une origine
étrangère contradictoire refuse la lecture en 409 FR/EN.

Nouveau mode de restauration : deux ventes T0 sans retour, puis deux retours
et trois règlements HTTP T1 perdus par remplacement réel. Reprise explicite
dans l'ordre des dépendances, anciens aperçus refusés, réessais/reconnaissances
sans nouvel effet. Résultat : dette 6000, avoirs 2000/4500, caisse 8500, stock
de 10 pièces inchangé et aucune réception physique. Anciennes assertions T0
des autres modes conservées ; A seule confirmée, B 404, anciens accès retirés,
état provisoire et aucun OTP/SMTP réel. Procédure et qualifications :
[rapport des retours perdus](../analysis/backend-recovery-sale-returns-2026-10-27.md).

La reprise des annulations et réceptions physiques, résolution des conflits,
autres familles et clôtures restent requises. Régression : 361 tests/839
sous-tests réussis sans avertissement/échec/erreur/ignoré, dont 18 tests de
concurrence. Le test de dépendance vente parente/retour ajouté après collecte
réussit séparément ; lots recouvrants non additionnés.

## Réceptions physiques de retours récupérées — 28 octobre 2026

`return_receipt` conserve les quantités effectivement reçues, leur condition,
les références vente/retour/ligne/produit, les dates et le coût historique exact
initial. Quantités par ligne de retour et ligne vendue bornées ; pièces entières,
parts de coût cumulatives exactes. Les références de coûts absentes/étrangères,
parts incompatibles, parents manquants ou corrections d'inventaire ultérieures
conservent la copie et bloquent l'effet pour traitement explicite.

Seul le vendable crée son mouvement déterministe et observe le stock/les alertes
actuels. Le non-vendable alimente son pool sans disponible. Réception, coût,
stock éventuel, origine et décision sont atomiques, sans argent ni relevé client.
Le coût peut rester inconnu, provisoire, nul connu ou fractionnaire ; inconnue
et zéro ne sont jamais confondus. Les grands coûts sont des chaînes décimales.
Une ancienne absence d'instantané reste explicite, sans reconstruction implicite.

Reçus connus reconnus après rebut, révision de coût ou nouvelle génération,
ainsi qu'après reconstitution explicitement tracée d'un coût initial absent.
Le premier accusé reste distinct de la quantité non-vendable et du coût courants.
Création source conservée, création de reprise actuelle ; provenance affichée
dans les listes/détails et origine étrangère contradictoire refusée en 409.

Qualification ciblée : 32 tests/37 sous-tests réussis, deux courses SQLite
incluses. Correctif ultérieur du format des très grands coûts d'aperçu vérifié
dans la régression : **576 tests/1014 sous-tests réussis**, zéro avertissement/
échec/erreur/ignoré, dont 20 tests de concurrence de récupération. Parcours
réel réussi : T0 sans retours/réceptions, puis
huit événements HTTP T1 perdus et récupérés. Résultat : dette 6000, avoirs
2000/4500, caisse 8500, disponible 14 pièces et pool non-vendable deux.
Trois réceptions/coûts, cinq mouvements de stock, huit origines/seize décisions,
aucun double effet. Anciens accès suspendus, A seule confirmée, état provisoire,
aucun OTP/SMTP réel. Références/révisions de coût et dispositions perdues,
annulations, conflits et clôtures restent requis. Commandes et preuves :
[rapport des réceptions physiques](../analysis/backend-recovery-return-receipts-2026-10-28.md).

## Décisions sur retours non-vendables récupérées — 28 octobre 2026

`non_sellable_decision` reprend une remise en vente confirmée, un départ
fournisseur ou un rebut, sur une réception physique non-vendable appartenant
à la boutique courante. Quantité réelle et reliquat source, version de coût,
motif, dates, mouvement éventuel et fournisseur sont conservés. Le pool courant
peut différer s'il reste suffisant et si la part de coût exacte reste compatible ;
l'accusé enregistre le reliquat obtenu, sans effacer les faits source.

La version historique de coût est distinguée d'une révision courante plus
récente, et une absence ancienne d'instantané n'est pas reconstruite implicitement.
Seule la remise en vente ajoute le disponible et observe les alertes. Fournisseur
et rebut consomment le pool sans nouvelle perte disponible ; le départ fournisseur
crée seulement une trace à valeur zéro, sans dette, avoir ni paiement. Tous les
effets, l'origine et la décision sont atomiques. Insuffisance, référence absente
ou arrondi incompatible conservent la copie brute et bloquent toute application.

Les reçus connus ne consomment pas une seconde fois, après autre disposition,
révision ou génération. Listes/détails exposent l'auteur source après autorisation
et refusent une origine étrangère contradictoire. La demande d'origine suit la
normalisation native des booléens ; les confirmations figées restent strictes.

39 tests/56 sous-tests finaux ciblés réussis, trois courses SQLite incluses.
Parcours de remplacement réel à onze événements réussi : trois dispositions,
disponible 15 pièces, pool soldé, caisse 8500, dette 6000 et avoirs 2000/4500.
Une trace fournisseur à zéro, aucune dette/avoir/retour facturé/perte nouveaux.
Anciens accès suspendus, A seule confirmée, récupération provisoire, aucun
OTP/SMTP réel. Régression élargie avant la dernière normalisation : 669 tests/
1099 sous-tests réussis, zéro avertissement/échec/erreur/ignoré, 23 courses de
récupération incluses. Les 39 ciblés et le parcours réel requalifient ensuite
le code final. Références perdues, conflits et clôtures restent requis.
Commandes et preuves :
[rapport des décisions non-vendables](../analysis/backend-recovery-unsellable-decisions-2026-10-28.md).

## Documents et paiements fournisseurs récupérés — 28 octobre 2026

Les routes de dépôt, d'aperçu et d'intégration prennent aussi en charge :

- `supplier_payment` : `schema_version: 1`, `original_request` et `payment`
  contenant le reçu initial avec ses parties ordonnées, la totalité non affectée,
  les UUID du fournisseur, de l'installation et les dates UTC. La liste initiale
  des affectations est vide. Chaque partie crée une seule sortie réelle de caisse.
- `supplier_payment_allocation` : `original_request` et `allocation` contenant
  opération/fournisseur/paiement, montants exacts, avant/après source, `recorded_at`
  et lignes ordonnées id/dette/montant. Le temps effectif du journal égale ce
  `recorded_at`. Toutes les dettes choisies et l'avance courante doivent permettre
  les montants complets ; aucune réduction, substitution ou sortie de caisse.
- `purchase_order` et `purchase_invoice` : `original_request` et `purchase`,
  reçu initial natif complété de `supplier_id`. Noms/facteurs/prix/quantités/totaux
  des lignes, dates source, commande éventuelle et dette initiale sont figés.
  Une commande n'a pas de dette ; seule une facture crée sa dette, en conservant
  l'UUID fourni, et son relevé. Aucun document ne reçoit de stock ou ne crée de cash.

Les fournisseurs, produits, conditionnements et commandes nécessaires doivent
exister dans la même boutique avec des relations compatibles. Les prix et
facteurs actuels n'écrasent pas ceux de la source. Une dépendance arrivée depuis
l'aperçu impose un nouvel examen. UUID en collision, copie incohérente, parent
absent/étranger ou capacité financière insuffisante bloquent l'opération entière.
Le dépôt technique ne valide ni ne refuse une opération métier et ne rétablit
aucun droit d'origine.

Les nouveaux mouvements de paiement utilisent `uuid5(part_id,
'supplier-payment-cash')` ; une reconnaissance conserve les anciens UUID natifs
déjà présents. Une référence externe réutilisée peut signaler un doublon sans
supprimer un versement réel. L'avance reprise peut précéder sa facture et être
affectée explicitement après celle-ci. Les faits source avant/après restent
distincts des soldes réels de reprise. Aucun remboursement automatique.

Écritures métier, argent éventuel, soldes, relevé, origine et décision commettent
dans une transaction. Le reçu initial reste celui de l'opération ; consulter
l'entité pour son état courant après réceptions ou affectations ultérieures.
Les lectures de documents contrôlent leurs relations avant de charger les dettes
ou détails ; une liaison étrangère incohérente est refusée en 409. Les dates de
création source sont conservées dans `recovery.source_snapshot`, celles de la
persistance de reprise restent dans l'accusé.

Trois restaurations WSGI réelles, dont commande/facture/paiement/affectation tous
perdus, ont abouti à 70000 FCFA dus sur 100000 facturés, avance zéro et caisse
−10000 espèces/−20000 Orange, sans seconde sortie ni stock. Les clôtures,
autres familles, réceptions d'achat perdues et conflits restent requis.
Commandes et qualifications :
[registre de continuation](../analysis/backend-continuation-2026-10-28.md).

## Réceptions perdues et clôture partielle — 29 octobre 2026

Le type de journal `purchase_receipt` reprend les réceptions physiques
d'achat, séparément de la facture et du paiement. Les nouveaux reçus natifs
portent les UUID des lignes/mouvements et les preuves historiques de coût et
de frais. Une copie minimale ancienne reste déposée mais bloquée ; aucune
preuve n'est déduite du coût courant. Les réceptions 6/10 puis 4/10 ont été
reprises après une vraie restauration, sans deuxième dette ni paiement.

Les types `product_update` et `pack_update` reprennent une correction seulement
sur son état avant exact. La copie conflictuelle reste conservée. Cette reprise
ne constitue pas la résolution métier des propositions concurrentes.

Pour clôturer partiellement la récupération, le technique actuel ouvre la
session dans Unfold puis « Clôturer partiellement la récupération », choisit
les observations d'appareils définitivement indisponibles et saisit un motif.
L'aperçu présente les faits réellement observés ; la confirmation signée
conserve ces choix. Un changement depuis l'aperçu exige une nouvelle revue.
Cette action est disponible après activation normale du serveur, avec les
droits techniques actuels et la protection CSRF habituelle.

L'API technique propose aussi :

```text
GET  /api/v1/recovery/sessions/{session_id}/closure-preview/
POST /api/v1/recovery/sessions/{session_id}/closures/
```

Le POST contient `operation_id`, `restore_session_id`, `kind: "partial"`,
`preview_token`, `reason` et `missing_device_ids`. Le reçu est durable et la
répétition exacte ne produit pas une seconde clôture. `kind: "complete"` est
refusé tant qu'un bilan fiable de récupération n'est pas disponible.

Les rapports autorisés exposent `state: "closed_partial"`, `closure_id`,
`closed_at`, `missing_devices`, `historical_missing_declarations`,
`missing_data`, `late_operations` et `late_review_required`. Ils ne donnent
pas les identités des appareils d'une autre boutique. Les déclarations de
manques restent visibles après une nouvelle restauration. `late_operations`
est calculé à partir du décompte figé à la clôture, indépendamment de l'heure
serveur ; une ancienne preuve insuffisante donne `null` et demande une revue.

Après clôture, une copie tardive peut être déposée, sans effet de stock, de
caisse ou de réservation. Les nouvelles intégrations et mises en attente
restent bloquées ; le rejeu exact d'un reçu acquis reste permis. Le traitement
de la réouverture technique avant intégration tardive reste soumis à la
décision U44. Aucun droit archivé n'est rétabli par la clôture. Les écarts
restent régularisés par les actions métier explicites autorisées.

Contrats, qualification et limites :
[continuation du 29 octobre](../analysis/backend-continuation-2026-10-29.md).
