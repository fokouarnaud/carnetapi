# Exploitation PythonAnywhere — déploiement initial du 5 novembre 2026

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
