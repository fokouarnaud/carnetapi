# Exploitation PythonAnywhere

## Mise à jour du 14 novembre 2026 — `5afa105` → `682b318`

- Gate locale : suite complète pytest **1 870 tests passés** (sortie 0, 1 h 05),
  migrations sans divergence, schéma OpenAPI validé, manifestes de schéma de
  sauvegarde et de restauration à jour. Requirements inchangés.
- Instantané avant déploiement : `~/app-data/pre-deploy-20261114T052224Z.sqlite3`,
  `integrity_check` ok, aucune violation de clé étrangère, 178 migrations,
  3 600 384 octets. À supprimer après confirmation de la version (budget disque).
- Répétition de la seule migration nouvelle (`customers.0008_customerreminder`,
  deux `CreateModel`) sur une copie : sortie 0, rien en attente.
- Webapp désactivée 05:26–05:28 UTC (API en 404), `git pull --ff-only`,
  migration réelle identique, `collectstatic` sans changement, `check --deploy`
  limité à W005/W021.
- Contrôles HTTP après rechargement : HTTPS 302, login admin 200, API/schéma/docs
  401, statiques 200, détail produit 401 anonyme ; OTP sans champs obligatoires
  400 (aucun envoi).
- Expiration du site relue : samedi 5 décembre 2026.

**Cooms injoignable depuis l'hébergement gratuit (constat du 14 novembre).**
Depuis la console, `curl https://gw-89.cooms.app/` échoue avec « Received HTTP
code 403 from proxy after CONNECT » (proxy `proxy.server:3128`) : le domaine
Cooms n'est pas sur la liste blanche des comptes gratuits. Aucun SMS n'a été
envoyé ni tenté ; `APP_SMS_BUDGET_PATH` et la configuration Cooms restent non
installés. Tant que ce domaine n'est pas autorisé, l'OTP réel (DOD-09) et la
connexion d'un téléphone à l'API en ligne sont impossibles sur cet hébergement ;
décision à prendre par le titulaire.

## Mise à jour du 13 novembre 2026 — `01c078f` → `5afa105`

Procédure reproductible : skill projet `.claude/skills/deploy-carnetapi`.

- Instantané avant déploiement par l'API de sauvegarde SQLite via la connexion
  Django (les contraintes appellent des fonctions SQL de l'application) :
  `~/app-data/pre-deploy-20261113T041452Z.sqlite3`, `integrity_check` ok,
  aucune violation de clé étrangère, 161 migrations, 3 207 168 octets. À
  supprimer après confirmation de la version (budget disque).
- Répétition des **17 migrations** sur une copie, depuis un `git worktree` de
  `origin/main` et un lanceur dédié forçant la base et la barrière dans `/tmp`
  (le lanceur privé écrase `APP_DB_PATH`) : sortie 0, rien en attente.
- Webapp désactivée 04:21–04:24 UTC (API en 404, appels cron-job.org en échec
  pendant cette fenêtre, sans modification du planificateur), puis `git pull
  --ff-only`, migrations réelles identiques à la répétition, `collectstatic`
  sans changement, `check --deploy` limité à W005/W021. Requirements inchangés.
- Réactivation et rechargement. Contrôles HTTP : redirection HTTPS 302, login
  admin 200, API/schéma/docs 401, statiques 200 ; nouvelles routes `changes`
  et `account/recovery-codes` en 401 anonyme ; OTP avec numéro invalide 400
  `otp_request_invalid`, vérification avec code mal formé 400. Aucun SMS
  possible : `APP_SMS_BUDGET_PATH` n'est pas configuré (OTP en 503 sinon).
- Expiration du site lue : samedi 5 décembre 2026. La reprise de la tâche de
  sauvegarde après la fenêtre d'arrêt reste à observer.

## Sauvegardes qualifiées les 7 et 8 novembre 2026

La configuration et l'essai ont été effectués dans le Browser Plugin intégré,
sur les comptes PythonAnywhere et cron-job.org déjà connectés. Le déclencheur
appelle en POST l'endpoint HTTPS privé `backups/trigger/` avec un jeton dédié,
sans droits de lecture des archives ni de restauration. La capture quotidienne
est activée à **02:00 Africa/Douala** ; les appels chaque minute permettent les
étapes d'envoi et les reprises bornées, sans nouvelle capture quotidienne.
La tâche cron est enregistrée et active ; une première exécution automatique
réussie a été observée le 8 novembre. La capture planifiée à 02:00 reste à observer.

L'essai du 7 novembre a renvoyé successivement `prepared`, `accepted`, puis
`idle`. L'instantané SQLite faisait **3 108 864 octets**, dans une partie chiffrée
de **93 649 octets**. La préparation officielle vérifie le déchiffrement, les
empreintes, les contraintes et les clés étrangères avant de rendre l'archive
prête. Un seul essai SMTP a été accepté, sans reprise, pour un message encodé
de **129 348 octets**. Les parties temporaires ont ensuite été nettoyées ; les
manifestes fichier et SQL ainsi que le journal sont conservés. Le nouvel appel
n'a créé ni seconde capture ni second envoi. L'acceptation SMTP ne prouve pas
la réception en boîte Gmail ; celle-ci reste à confirmer. Cette base ne contenait
aucune photo : cet essai n'est pas une qualification de photos représentatives.
Une copie locale de la clé séparée est conservée hors Git et hors du chat.

La mesure de quinze minutes comprend **1 791 relevés** sans erreur de lecture,
avec intervalle maximal observé de 0,503 seconde. Le pic sur le périmètre
inventorié, réserve de 32 768 octets pour l'observateur comprise, est de
**493 793 570 octets**. Le répertoire applicatif est relu à chaque relevé ; le
reste du répertoire personnel, le runtime et les journaux externes sont figés
au relevé initial. La bibliothèque SQLite externe identifiée par `ldd` est
incluse. Les parcours de fichiers ne sont pas atomiques ; un pic entre deux
relevés peut échapper à la mesure. Ce constat ne démontre ni l'ensemble des
auxiliaires externes, ni le budget à charge représentative, ni la capacité du
pilote sur douze mois.

La mise à jour du code publié après `01c078f` reste distincte de cette
configuration. Elle doit suivre une sauvegarde récente, l'arrêt temporaire
durable de la webapp et des déclencheurs, le drainage des travailleurs et la
validation de la migration additive. Un verrou `hold_control` seul ne maintient
pas cet arrêt si son processus s'interrompt. Ne pas utiliser `enter` puis `abort`
pour rouvrir une base migrée : `abort` exige les fichiers métier inchangés.

## Historique — déploiement initial du 5 novembre 2026

Le backend du commit `2c84760637af81e99d16c6cd718b729da32c7627` est déployé
sur `https://carnetapi.pythonanywhere.com`, depuis la branche `main` du dépôt
`https://github.com/fokouarnaud/carnetapi.git`. `main` et `develop` ont été
publiées avec ce même commit initial. Cette installation ne qualifie pas les
46 US, la capacité du pilote ni la synchronisation du client Android.

## Configuration observée

| Élément | Valeur |
| --- | --- |
| Compte / formule | `carnetapi` / gratuite |
| Python | 3.13.1, application web configurée en 3.13 |
| Sources Git | `/home/carnetapi/carnetapi` |
| Racine Django | `/home/carnetapi/carnetapi/backend` |
| Virtualenv | `/home/carnetapi/.virtualenvs/carnetapi` |
| WSGI | `/var/www/carnetapi_pythonanywhere_com_wsgi.py` |
| Configuration privée | `/home/carnetapi/private/env.json` |
| SMTP privé | `/home/carnetapi/private/smtp.json` |
| SQLite métier | `/home/carnetapi/app-data/db.sqlite3` |
| Contrôle de restauration | `/home/carnetapi/app-control/restore-gate.sqlite3` |
| Archives temporaires | `/home/carnetapi/app-data/backups` |
| Mapping statique | `/static/` → `backend/staticfiles` |

La configuration et SMTP sont hors Git, dans un répertoire privé `0700`, avec
des fichiers `0600`. Les clés Django et JWT, indépendantes, sont générées sur
le serveur et chargées avant l'import de Django. Le WSGI et le lanceur privé
`/home/carnetapi/private/manage.py` utilisent le même fichier de configuration.
Le WSGI initial a été conservé dans le répertoire privé avant remplacement.

Le client Flutter, les APK et les preuves locales restent exclus de ce dépôt
serveur par `.gitignore`. Aucun mapping `/media/` public n'a été ajouté : les
photos passent par les endpoints autorisés de la boutique.

## Vérifications effectivement réalisées

Le clonage a confirmé le SHA ci-dessus. L'installation des versions exactes de
`requirements.txt` et `pip check` ont réussi. Les migrations ont été appliquées,
puis la barrière externe initialisée ; le contrôle des migrations ne trouvait
plus d'opération à appliquer. `collectstatic` et le rechargement web ont réussi.

`check --deploy` a signalé seulement `security.W005` et `security.W021` : HSTS
ne couvre pas les sous-domaines et n'est pas inscrit au préchargement navigateur.
Ces avertissements n'ont pas été masqués. HTTPS est forcé par l'hébergeur ; les
réponses Django observées portent HSTS `max-age=31536000`, `nosniff` et `DENY`.

| Requête anonyme | Résultat observé |
| --- | --- |
| HTTP `/admin/login/` | 302 vers exactement la même URL en HTTPS |
| HTTPS `/admin/login/` | 200, formulaire Unfold chargé visuellement |
| HTTPS `/api/v1/shops/` | 401 JSON |
| HTTPS `/api/v1/schema/` | 401, représentation OpenAPI |
| HTTPS `/api/v1/docs/` | 401 |
| HTTPS `/static/unfold/css/styles.css` | 200 CSS |
| HTTPS `/static/unfold/fonts/inter/styles.css` | 200 CSS |

Les premiers contrôles locaux ont corrigé deux hypothèses trop strictes du
script de vérification : imposer 301 au lieu d'une redirection HTTPS valide, et
imposer JSON à la réponse d'erreur de l'endpoint OpenAPI. Le refus 401 et la
cible HTTPS sont restés strictement contrôlés.

## Premier administrateur et commandes opérateur

Le titulaire a créé le premier compte technique `fokou` dans la console dédiée,
en saisissant lui-même le mot de passe. Le succès de la commande, les trois
indicateurs actif/staff/superutilisateur et l'accès visuel à l'index Unfold sont
vérifiés. Une connexion directe sans `next` a révélé une redirection erronée vers
`/accounts/profile/` ; la session était valide et `/admin/` accessible. Le correctif
`LOGIN_REDIRECT_URL` et son test de non-régression sont qualifiés séparément avant
leur déploiement. Les mots de passe ne sont ni archivés ici ni affichés par les
commandes de vérification.

Les commandes Django utilisent le lanceur privé pour charger la même
configuration que le site :

```bash
/home/carnetapi/.virtualenvs/carnetapi/bin/python \
  /home/carnetapi/private/manage.py check --deploy
```

Ne pas exécuter un `migrate`, réinitialiser une barrière ou remplacer SQLite sur
un site actif sans le protocole de maintenance décrit dans [BACKUPS.md](BACKUPS.md).
Une mise à jour commence par l'examen du diff et des migrations de `main` ;
`git pull --ff-only` ne remplace pas cette procédure. Le lanceur privé et les
secrets ne font pas partie des sources à pousser.

## Budget et gates restant ouverts

Une première mesure, après initialisation et requêtes HTTP, a donné :

| Périmètre sans chevauchement | Octets |
| --- | ---: |
| `/home/carnetapi` entier, dont venv, sources, statiques, DB, contrôle, caches et fichiers privés | 127 916 463 |
| Bibliothèque standard Python 3.13 partagée, hors `site-packages` global | 339 401 245 |
| Bibliothèque `libpython3.13.so` résolue | 31 920 704 |
| Binaire Python résolu | 18 248 |
| Trois journaux web relevés | 15 588 |
| Sous-total, avant le WSGI et autres auxiliaires externes | **499 272 248** |

Le runtime partagé n'est donc pas compté comme zéro et les dépendances du venv
restent incluses dans le répertoire personnel. Pip, absent de `requirements.txt`,
a ensuite été retiré du seul venv. Son retrait a réussi ; les sept contrôles
HTTP ont été rejoués avec succès et le contrôle Django conserve seulement les
deux avertissements HSTS documentés. Sa remise en place se fait avec le même
interpréteur via `-m ensurepip --upgrade --default-pip`.

Le relevé suivant donne `116 537 696` octets pour le répertoire personnel,
`358` pour le WSGI et `23 320` pour les trois journaux. Avec le même runtime
partagé et le binaire, le périmètre ainsi inventorié représente **487 901 571
octets**. Ce chiffre ne démontre pas la capacité du pilote : il reste à mesurer
les autres auxiliaires externes, les fichiers temporaires et la croissance
représentative des données. La marge ne doit pas être extrapolée à douze mois.

SMTP a été transféré avec l'autorisation explicite du titulaire ; l'ouverture
authentifiée de la connexion Gmail depuis PythonAnywhere est vérifiée. Aucun
email n'est envoyé par ce contrôle. La livraison d'un email, la sauvegarde
chiffrée complète et ses reprises restent à qualifier sur cet hébergement.
Aucun jeton ni acteur de scheduler n'était actif dans la
configuration initiale. Ne pas activer cron sur une simple réponse de connexion
Unfold : il faut les secrets dédiés, un acteur technique actuel et une sauvegarde
réelle qualifiée selon [BACKUPS.md](BACKUPS.md).

La console Cooms connectée au compte retenu affichait 99 crédits restants,
issus de 100 SMS de bienvenue. Les recharges sont payantes. Ce constat ne prouve
pas la gratuité durable des OTP ; aucune activation ne doit contourner l'OTP
en cas d'échec ou d'épuisement des crédits.

Le renouvellement du site reste manuel. La page Web indiquait une désactivation
le **5 décembre 2026** ; la date doit être relue après chaque renouvellement.
La livraison des rappels 7 puis 2 jours avant expiration reste à vérifier.
