# Mesures de stockage backend

Le PRD DOD-08 sépare deux vérifications : le backend initial installé doit
occuper strictement moins de 500 000 000 octets, et le dimensionnement sur douze
mois doit être exercé sans suppression automatique de l’historique. Le plafond
initial ne constitue pas un quota annuel de données.

Le titulaire a fixé le pilote initial à **deux boutiques** le 24 octobre 2026,
après correction de la proposition intermédiaire d’une boutique : six appareils,
2 000 produits, 73 000 ventes et 365 000 lignes sur douze mois. La cible de dix
boutiques est reportée à une phase ultérieure ; aucun quota commercial ajouté.

## Inventaire local initial

Depuis la racine :

```powershell
./.venv/Scripts/python.exe -X utf8 tools/measure_backend.py
```

Le résultat figure dans [backend-local-size.json](../analysis/backend-local-size.json).
Il additionne les longueurs logiques des fichiers du backend, de son environnement
virtuel et de l’installation Python de base Windows. Cette dernière inclut aussi
des paquets globaux étrangers au projet. Il manque les données initiales réelles,
les photos de production, les fichiers d’exploitation et les pics temporaires.
Ce relevé incomplet ne démontre pas le budget complet sur PythonAnywhere.

## Plancher SQL sur douze mois

```powershell
./.venv/Scripts/python.exe -X utf8 tools/benchmark_backend_storage.py --output analysis/backend-storage-pilot.json --host-limit-bytes 536870912
```

La commande reconstruit le schéma courant dans une base privée jetable. Son
processus Django possède des réglages propres, sans accès aux variables de base
active, SMTP ou clés applicatives. Les données synthétiques par défaut comprennent
deux boutiques, six installations, deux mille produits, 73 000 ventes,
365 000 lignes et un journal minimal par vente. Les UUID et les contraintes SQL
réels sont conservés. Les noms sont réduits à un caractère et les dates à la
seconde ; les détails de journal sont vides.

Il s’agit d’insertions SQL directes : les paiements, mouvements de stock,
instantanés de coûts, photos, achats, retours et inventaires sont absents.
Le plancher utilise un seul propriétaire ; les comptes vendeurs et leur travail
effectif restent à inclure dans le jeu fonctionnel de qualification.
Ce jeu ne qualifie ni les parcours métier complets, ni le trafic des six
appareils, ni les temps de réponse sur l’hébergeur.

Pour limiter les réécritures des pages d’index pendant cette génération locale,
son cache de pages SQLite est réglé à 128 Mio. Les réglages de durabilité sont
conservés. Ce choix n’est pas appliqué au serveur ; la mesure ne démontre pas
la capacité mémoire de l’offre gratuite. Le rapport indique les PRAGMA utilisés.
Les lots Python de mille ventes sont insérés dans une transaction unique de
préparation du jeu, pour éviter de réécrire un journal de pages d’index à chaque
lot. Les messages intermédiaires précèdent son commit ; seul le rapport final
confirme les lignes réellement conservées et vérifiées. Ce choix ne mesure pas
le débit de ventes validées une par une par l’API.

Pour reproduire le rapport historique des dix boutiques, ajouter `--shops 10`
et choisir `analysis/backend-storage-floor.json` comme fichier de sortie.

La commande contrôle les nombres de lignes, les clés étrangères, l’intégrité
SQLite et les totaux des ventes. Le rapport distingue la taille avant/après
VACUUM et un pic de fichiers visibles échantillonné à 20 ms. Ce pic peut manquer
les fichiers temporaires SQLite sans nom ; il exclut les fichiers du runtime et
les étapes de sauvegarde/restauration. Les limites de taille et la réserve de
disque arrêtent la génération avec un état explicite, sans annoncer que la cible
est atteinte. La base de mesure est supprimée après lecture du rapport.

## Hébergement gratuit

La [documentation officielle du quota PythonAnywhere](https://help.pythonanywhere.com/pages/DiskQuota/),
consultée le 24 octobre 2026, annonce 512 Mio pour le compte gratuit,
soit 536 870 912 octets. Le quota et l’espace utilisé doivent aussi être vérifiés
sur le compte réel. La comparaison doit inclure code, dépendances, données et
fichiers temporaires comptabilisés par l’hébergeur.

`--host-limit-bytes` permet de comparer explicitement le plancher mesuré à un
quota connu. Sans ce paramètre, le rapport conserve `host_limit_unknown`.
Un plancher inférieur ou égal au quota ne prouve jamais la capacité complète.
Aucun dépassement n’autorise une offre payante, une réduction des volumes
acceptés, une suppression d’historique ou une limitation commerciale.

## Restauration

Le format de sauvegarde courant borne encore un instantané à 500 000 000 octets.
Cette borne technique doit être revue si la base annuelle mesurée la dépasse ;
elle ne doit pas transformer le budget initial en limitation fonctionnelle.
Les fichiers d’import, archives chiffrées, décompression, copie migrée et copie de
repli devront être comptés ensemble avant la qualification du parcours Unfold.
La préparation actuelle sur copie ne remplace aucune base active ; détails dans
[BACKUPS.md](BACKUPS.md).

## Pilote annuel deux boutiques — actualisation du 25 octobre 2026

Le [rapport du pilote corrigé](../analysis/backend-storage-pilot.json) atteint
deux boutiques, six installations, 2 000 produits, 73 000 ventes et
365 000 lignes. Après compactage : **200 470 528 octets**, contre 212 660 224
avant ; 48 943 pages de 4096 octets. Clés étrangères, intégrité SQLite et totaux
de toutes les ventes/lignes vérifiés valides ; base privée supprimée. Durée
totale locale de préparation et de contrôle : 162,02 secondes pendant les autres
qualifications, sans mesure du débit API. Le pic des fichiers visibles atteint
**484 208 238 octets** à 20 ms. Cette actualisation inclut le schéma 0016 des
imports et préparations de restauration, avec ses quatre tables techniques vides. Son empreinte
correspond exactement à la référence SQL reconstruite de 510 objets. La mesure
antérieure à 0013 (200 372 224 octets) reste consignée au registre.

La base seule est inférieure au quota publié, mais le pic local de préparation
approche ce quota avant ajout du runtime et des autres fichiers. Photos, comptes
vendeurs, paiements, mouvements, coûts et sauvegardes/restaurations restent
exclus. Le résultat demeure `capacity_not_demonstrated` ; aucune qualification
de l’hébergement, du budget initial complet ou du trafic réel n’en découle.
Le compactage est celui du générateur local ; ses besoins ne remplacent pas
la mesure séparée des pics de sauvegarde/restauration du serveur.

La mesure intermédiaire d’une boutique est conservée dans
[backend-storage-one-shop.json](../analysis/backend-storage-one-shop.json)
pour comparaison uniquement. Elle ne constitue pas la cible initiale acceptée.

## Référence annuelle dix boutiques — 24 octobre 2026

Le [rapport des dix boutiques](../analysis/backend-storage-floor.json) atteint
les nombres de lignes demandés. Après commit et VACUUM : **995 328 000 octets**
(1 055 002 624 avant compactage), soit 243 000 pages de 4096 octets. Clés
étrangères, intégrité SQLite et rapprochement de toutes les ventes/lignes sont
valides. La base privée a ensuite été supprimée.

Le pic des fichiers visibles échantillonné atteint **2 917 799 847 octets**,
y compris les journaux et fichiers temporaires visibles de préparation et
compactage. Il exclut le runtime, les sauvegardes/restaurations et les autres
éléments précédemment indiqués. Les allocations par table ne sont pas disponibles
sur cet essai ; aucun découpage chiffré par table n’est inventé.

Le plancher des seules tables minimales dépasse le quota gratuit publié de
536 870 912 octets. Le schéma mesuré ne tient donc pas dans ce quota au volume
annuel historique de dix boutiques ; une optimisation technique ou un arbitrage explicite du titulaire
est nécessaire. Ce résultat ne modifie ni le PRD, ni les volumes acceptés, ni
l’hébergement autorisé. Il n’établit pas non plus un dépassement du budget initial,
qui est évalué séparément. Aucun passage payant ni effacement d’historique.

## Quarantaine sur copie — limite de qualification

La commande ajoutée le 25 octobre conserve la copie préparée et produit une seconde copie de quarantaine. Son précontrôle demande deux tailles de base plus 15 Mo libres sur le volume de sortie ; il ne mesure pas le quota hébergé. Les mesures annuelles ci-dessus excluent ce travail, les fichiers de restauration et le runtime. Un parcours avec moins de copies intermédiaires et une mesure des pics sur le pilote complet restent nécessaires ; aucune capacité d’hébergement supplémentaire n’est déduite du petit scénario HTTP local.
