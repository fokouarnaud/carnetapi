# Backend — continuation du 30 octobre 2026

Les conflits de prix d'un produit conservent les propositions, rétablissent le
dernier prix commun et exigent aperçu puis choix motivé d'un responsable.
Ventes et reçus antérieurs restent figés ; les décisions ne créent ni stock ni
caisse. API manager et métadonnées publiques sans coûts qualifiées : **45
tests/63 sous-tests**, incluant les sept tests API/contrat et douze sous-tests,
puis parcours HTTP réel réussi. OpenAPI strict sans avertissement et référence
de restauration à **685 objets**. Les réceptions et clôtures ci-dessous n'ont
pas été rejouées dans cette tranche ; aucune US n'est déclarée complète.

Un paquet de sources serveur pour transfert manuel PythonAnywhere est qualifié
par **onze tests** et extraction réelle de **467 fichiers**, Django check isolé
et 161 migrations identiques. Il exclut bases, données, photos, clés, fichiers
d'environnement, runtime Python et client Flutter. Depuis la racine du dépôt,
`.venv/Scripts/python.exe -X utf8 tools/package_pythonanywhere.py --output dist/pythonanywhere-server-a-transferer.zip`
crée seulement un nouveau ZIP local ; ce n'est ni un déploiement ni une preuve
du budget installé. `dist/` et APK/AAB sont ignorés, sources Flutter et
`pubspec.lock` conservés. Android reste en construction, sans preuve Android
apportée par ces tests. [Contrats, preuves et limites](../analysis/backend-price-conflicts-2026-10-30.md).

Les paragraphes suivants conservent les qualifications du 29 octobre et antérieures.

Les réceptions d'achat perdues sont récupérables avec leurs UUID et preuves de
coût : une restauration réelle à sept faits a repris 6 puis 4 cartons, soit
240 unités, en conservant la dette de 70 000 FCFA et les seuls paiements réels
de 30 000 FCFA. Les corrections de catalogue compatibles sont reprises ; les
divergences restent bloquées. La clôture partielle technique dispose d'un
aperçu, d'une confirmation Unfold et d'un reçu durable, avec mentions de
manques persistantes. Les arrivées tardives restent déposées sans effet
automatique. La clôture complète, la réouverture et la couverture des 46 US
restent à démontrer. [Contrats, preuves et limites](../analysis/backend-continuation-2026-10-29.md).

Les paragraphes suivants conservent les qualifications précédentes.

Continuation : paiements/affectations fournisseurs et commandes/factures perdus
repris avec leurs UUID et valeurs historiques. Trois restaurations WSGI réelles
réussies, dont une chaîne à cinq faits : dette 70000, avance zéro, deux sorties
de caisse totalisant 30000, aucun stock/réception fictif. Contrôles de session
et d'accès relus dans les transactions d'équipe, de bail et de retrait d'appareil.
La couverture complète des 46 US reste à démontrer ; résultats et commandes :
[registre de continuation](../analysis/backend-continuation-2026-10-28.md).
Dernière requalification : **63 tests/83 sous-tests réussis**, sans avertissement,
et restauration à cinq faits réussie sur le code final. La sélection élargie
précédente a 599 tests/1232 sous-tests réussis et un échec de nettoyage de fixture,
corrigé puis requalifié ; ses six avertissements `DATABASES` et son résultat
initial restent consignés, sans prétention de suite complète entièrement verte.

US-27/36/41/44 : reprise des fiches fournisseur et des dettes, avances et avoirs
d'ouverture distincts. Aucun achat, stock, paiement ou compensation automatique.
Restauration réelle réussie à quatre événements, puis affectation et remboursement
nouveaux explicites : dette 7000, avance 2000, avoir 1500, caisse Orange +500.
Temps de création de fiche déclaré conservé sans ancienne date serveur inventée.
Régression finale après garde sur activité native étrangère : **598 tests/917
sous-tests réussis**, aucun avertissement/échec/erreur/ignoré ; restauration
fournisseur rejouée avec succès. Régression antérieure au garde : **443 tests/
910 sous-tests réussis**, trente courses de l'intégration dont quatre nouvelles.
Ambiguïté OpenAPI corrigée avec objets fournisseur fermés, sans assouplir oneOf.
Autres familles, conflits et clôtures restent requis. Aucun statut US complet ajouté.
[Contrat et preuves](../analysis/backend-recovery-supplier-dependencies-2026-10-28.md).

US-26/36/44 : annulations perdues récupérées avec leur retour compensateur,
deux origines distinctes et revues liées dans une transaction. Reliquat et
répartition dette/avoir contrôlés, aucun remboursement ni stock automatique.
Restauration réelle réussie à sept événements : deux annulations, deux retours,
trois règlements ; caisse 8500, dette 6000 et avoirs 2000/4500 après répétitions.
Régression finale : **806 tests/1318 sous-tests réussis**, zéro échec/erreur/
ignoré, six avertissements de fixtures DATABASES ; 26 courses de l'intégration
incluses, dont trois d'annulation. Aucun statut US complet ajouté,
conflits, autres familles et clôtures restent requis.
[Contrat et preuves](../analysis/backend-recovery-sale-cancellations-2026-10-28.md).

US-14/15/26/30/33/36/44 : décisions sur pool non-vendable récupérées, remise en
vente/fournisseur/rebut distincts, quantité réelle entière et coût historique
conservés. Seule la remise en vente augmente le disponible ; trace fournisseur
à zéro, aucun argent, dette, avoir ou seconde perte. **39 tests/56 sous-tests
ciblés finaux réussis**, trois courses SQLite incluses. Restauration réelle
réussie à onze événements : disponible 15 pièces, pool soldé, caisse 8500,
dette 6000 et avoirs 2000/4500, rejouée après correctif final. Régression avant
ce dernier correctif de normalisation : **669 tests/1099 sous-tests réussis**,
zéro avertissement/échec/erreur/ignoré, 23 courses de récupération incluses ;
les 39 ciblés finaux ci-dessus requalifient ce correctif. Références perdues,
conflits, annulations et clôtures restent requis. Aucun statut US complet.
[Contrat et preuves](../analysis/backend-recovery-unsellable-decisions-2026-10-28.md).

US-14/15/26/33/36/44 : réceptions physiques perdues récupérées avec coût exact
d'origine et état physique distinct. Seul le vendable augmente le disponible ;
aucune caisse/relevé client ni coût nul inventé. Références absentes ou parts
incompatibles conservées et bloquées. **32 tests/37 sous-tests ciblés réussis**,
plus correction vérifiée du format décimal des très grands coûts d'aperçu.
Régression : **576 tests/1014 sous-tests réussis**, zéro avertissement/échec/
erreur/ignoré, dont 20 tests de concurrence de récupération. Restauration réelle
réussie : huit événements perdus, dont trois
réceptions ; 14 pièces disponibles, deux non-vendables, caisse 8500 inchangée
par le physique. Objectif global actif ; coûts/révisions/dispositions perdus,
annulations, conflits et clôtures restent requis.
[Contrat et preuves](../analysis/backend-recovery-return-receipts-2026-10-28.md).

US-26/36/44 : retours commerciaux perdus récupérés sur quantités et prix nets
facturés, avec répartition dette/avoir source conservée si le contexte le permet.
Conflits conservés et bloqués ; aucun cash ni réception physique. Reçus connus
reconnus après règlement, annulation et nouvelle génération, sans ancien solde
rétabli. Restauration réelle réussie : deux retours T1 et trois règlements,
dette 6000, avoirs 2000/4500, caisse 8500. Test vente perdue → retour →
remboursement réussi. **361 tests/839 sous-tests de régression réussis**, zéro
avertissement/échec/erreur/ignoré, dont 18 tests de concurrence. Objectif des
46 US actif ; annulations/réceptions physiques perdues et clôtures requises.
[Contrat et preuves](../analysis/backend-recovery-sale-returns-2026-10-27.md).

US-23/26/31/36/44 : reprise des remboursements et affectations d'avoirs de
retour existants, avec provenance, dates source/reprise distinctes et reçus
connus sans double effet. Remboursement anonyme sans client/relevé inventé ;
aucune réception physique ni remise en stock par le règlement.
**20 tests/37 sous-tests ciblés réussis** et restauration réelle T0/T1 avec
trois règlements réussie : dette 6000, avoirs restants 2000/4500, caisse 8500.
Reprise des retours eux-mêmes perdus, insuffisances et clôtures restent requises.
Régression : **343 tests/813 sous-tests réussis**, zéro avertissement ;
16 tests/31 sous-tests de concurrence inclus. Lots recouvrants non additionnés.
[Contrat et preuves](../analysis/backend-recovery-return-settlements-2026-10-27.md).

US-21/23/24/36/44 : reprise des affectations explicites sur avance ou crédit
client de vente, montants et dettes choisis conservés, aucune nouvelle caisse.
Crédit ou dette insuffisants bloquent le lot entier ; les lignes distinctes
visant une même dette sont cumulées avant contrôle puis conservées.
**31 tests/60 sous-tests ciblés réussis** après correction du contrat de
réponse acceptant le moyen vide d'une affectation. Restauration réelle à sept
opérations réussie : affectation 400, dette 5600, crédit 100, caisse 2500.
Conflits à résoudre, avoirs de retour, autres familles et clôtures restent requis.
Régression : **328 tests/775 sous-tests réussis**, zéro avertissement, dont
13 tests de concurrence. Dépendance vente/crédit/affectation qualifiée ensuite
par un test API supplémentaire ; lots recouvrants non additionnés.
[Contrat et preuves](../analysis/backend-recovery-customer-allocations-2026-10-27.md).

US-23/31/36/44 : remboursements d'avances de paiement et de crédits de vente
récupérables explicitement, montant exact, caisse/relevé/origine atomiques et
reconnaissance entre générations sans seconde sortie. Source absente ou
insuffisante conservée et bloquée pour examen ; sa régularisation reste requise.
Une comparaison commune des valeurs nulles est corrigée, incluant l'ajout
d'une première échéance. **36 tests/54 sous-tests ciblés réussis** après
correction ; restauration complète T0/T1 à six opérations réussie : dette
6000, crédit 500, caisse nette 2500 après le seul remboursement de 500.
Régression finale : **314 tests/741 sous-tests réussis**, zéro avertissement ;
un test supplémentaire vente/crédit/remboursement passe après collecte.
[Contrat et preuves](../analysis/backend-recovery-customer-refunds-2026-10-27.md).

US-20/24/36/44 : reprise des corrections de coordonnées et des échéances
avec contrôle strict de l'avant/version, motif et origine, sans écrasement
des révisions concurrentes ni retour à un ancien solde. Retrait explicite du
téléphone ou de l'échéance pris en charge. **24 tests/41 sous-tests distincts
réussis** sur les lots ciblés et de concurrence ; restauration réelle
T0/T1 à cinq opérations réussie, avec échéance rejouée avant paiement et
dette finale 6000 FCFA, crédit 1000, caisse 3000. Résolution des conflits,
fusion, autres familles et clôtures restent requises.
Régression clients/common/récupération : **327 tests/806 sous-tests réussis**,
six avertissements connus `DATABASES` ; lots ciblés recouvrants non additionnés.
[Contrat et preuves](../analysis/backend-recovery-customer-revisions-2026-10-27.md).

US-20/24/36/44 : reprise explicite des créations de clients et dettes d'ouverture,
puis encaissements dépendants. UUID/dates initiaux conservés, aucun rapprochement
par coordonnées ni solde courant réinitialisé ; origine manquante distinguée
d'une preuve enregistrée. **205 tests/192 sous-tests réussis sur le code final**,
plus **2 tests/16 sous-tests** de projections étrangères, et parcours HTTP réel
archive T0/opérations T1/restauration T0/reprise T1
réussi sur le code final : dette 6000, non affecté 1000, caisse 3000, sans vente
ni stock fictifs. Aucun modèle/migration ajouté ; référence à 655 objets.
Une origine contradictoire produit un conflit de lecture explicite sans
identité étrangère ni montant réattribué. Régression élargie antérieure aux
derniers durcissements : 885 tests/1183 sous-tests réussis, six avertissements
connus `DATABASES` ; ne pas additionner ces lots recouvrants.
Fusion, autres familles, régularisations et clôtures restent requises.
[Contrat et preuves](../analysis/backend-recovery-customer-dependencies-2026-10-27.md).

US-21/37/44 : file d'examen des encaissements prêts, états en attente/refusé/
validé, historique et totaux source exacts séparés. Validation liée aux effets
financiers dans la même transaction ; aucun cash, dette ou remboursement
automatique lors d'un refus. **20 tests/28 sous-tests ciblés réussis**, processus
concurrents, migration et Unfold FR/EN inclus. WSGI réel du refus et restauration
jusqu'à réouverture sur le schéma à 655 objets réussis.
Régression élargie : **866 tests/1153 sous-tests réussis**, six avertissements
connus des fixtures `DATABASES` ; course complémentaire validation/refus réussie.
Régularisation financière des refus, copies bloquées, autres familles et
clôtures restent requises.
[Contrat et preuves](../analysis/backend-recovery-collection-reviews-2026-10-27.md).

US-21/23/44 : intégration explicite des encaissements récupérés, limitée aux
affectations réellement appliquées à l'origine et aux dettes encore ouvertes.
Argent reçu conservé une fois, excédent à régulariser, reçu initial distinct du
solde après remboursement ; aucun compte d'origine réactivé. **32 tests/20
sous-tests ciblés** et **3 tests/4 sous-tests concurrence/migration réussis** ;
parcours HTTP réel avec encaissement concurrent et remboursement réussi.
Régression : **849 tests/1127 sous-tests réussis**, six avertissements connus
des fixtures `DATABASES`. Restauration jusqu'à réouverture rejouée avec succès
sur le schéma à 640 objets.
La file des sources prêtes est traitée dans le complément ci-dessus ; la
régularisation des refus, les autres familles et les clôtures restent requises.
[Contrat et preuves](../analysis/backend-recovery-collections-2026-10-27.md).

US-43 : réouverture console d'une copie installée, avec preuve du bootstrap
externe, mot de passe actuel du compte neuf et décision durable sous verrou
exclusif. La récupération reste provisoire et les contrôles par boutique restent
actifs. **13 tests/25 sous-tests ciblés réussis** ; parcours HTTP à deux boutiques
réussi sur le code final, avec nouvelle écriture et réessai dans A, refus de B,
session ancienne refusée et vendeurs suspendus. Retour arrière et autres
exigences de reprise restent requis.
Régression barrière/installation/secours/droits/contrats : **88 tests/474
sous-tests réussis**, onze avertissements connus des fixtures `DATABASES`.
[Preuves et procédure](../analysis/backend-restore-activation-2026-10-27.md).

US-46, complément : envoi interrompu réparable par console technique sous
verrou exclusif, avec mot de passe actuel, motif et examen durable ; aucun mail
pendant la réparation. Arrêt réel après SMTP, classement incertain puis renvoi
explicite qualifiés par HTTP/SMTP local. **51 tests/38 sous-tests d'hébergement
réussis sur le code final**, dont la conservation du Message-ID après changement
d'expéditeur. Régression archives/restauration/contrats : **124 tests/490 sous-tests
réussis**, aucun avertissement, collectés avant ce dernier correctif.
Migration `backups.0025`, référence à 639 objets.
[Contrat et preuves](../analysis/backend-hosting-interruption-2026-10-26.md).

US-46 : expiration déclarée versionnée, rappels J−7/J−2 dans Africa/Douala,
historique SMTP, renvoi explicite et constats datés de pannes/espace disponibles
par API technique et Unfold FR/EN. Commande et déclencheur dédié protégés par
la barrière de restauration ; aucune prolongation automatique d'hébergement.
**31 tests/10 sous-tests ciblés réussis**, parcours HTTP/Unfold/SMTP loopback
réussi. Exploitation gratuite réelle, cron/Gmail et budgets restent requis.
[Contrat et preuves de la première tranche](../analysis/backend-hosting-2026-10-26.md).
Régression backups/common/config/identity : **581 tests/905 sous-tests réussis**,
onze avertissements connus des fixtures `DATABASES`. Après collecte, contrats
503 corrigés et **4 tests/214 sous-tests réussis**, sans effet métier changé.

US-16 : départs physiques, transit et réceptions partielles vendables disponibles
par API responsable. Chaque réception peut choisir une autre fiche existante
compatible, avec coûts exacts source conservés et aucun effet de caisse/dette.
Régression élargie : 361 tests/352 sous-tests réussis ; qualification finale des
durcissements : 16 tests/14 sous-tests et WSGI réel réussis. Les pertes/clôtures de transit, cas de disponible
insuffisant, non-vendables, révisions de coûts en chaîne, synchronisation et
reprise restent requis. [Preuves et contrat](../analysis/backend-transfers-2026-10-26.md).
Les résultats de la suite générale ci-dessous précèdent cette tranche.

US-15/37/44 : rapprochement explicite des réservations refusées avec une perte
ou correction négative validée du même produit, avec aperçu et motif. Aucune
nouvelle écriture physique ou financière ; quantités déjà utilisées bornées
entre copies et générations. Collision d'UUID capturé corrigée en TDD,
concurrence SQLite, conservation des origines, grandes sommes et historique
paginé vérifiés. Parcours WSGI de 500 lignes réussi, dont rapprochement et
réessai sans second effet. **Suite générale : 1113 tests réussis en 1653,30 s**,
aucun échec/erreur/ignoré, onze avertissements connus des fixtures DATABASES.
Les 46 US restent partiellement couvertes ; aucun statut global complet.
[Contrat, preuves et limites](../analysis/backend-reconciliation-2026-10-26.md).
Les qualifications suivantes restent celles de leurs tranches respectives.

US-15/37/44 : mise en attente, réservation unique et refus motivé des ventes
récupérées disponibles. L’approbation remplace sa réserve sans seconde
déduction ni nouvelle alerte ; le refus garde les faits et la réserve pour une
régularisation distincte. L’endpoint ordinaire de vente ne contourne pas cet
examen, même avec un autre UUID d’opération pour la même vente. File et
historique paginés, totaux exacts séparés par état, droits actuels et reprises
contrôlés. **414 tests/352 sous-tests de régression réussis** ; optimisation
finale de la migration qualifiée séparément par deux tests/deux sous-tests.
HTTP réel de 500 lignes en attente puis approuvées et d’un refus supplémentaire
réussi. Migrations additives 0021–0022, 80 migrations historiques conservées,
schéma SQL 578 objets. OpenAPI strict validé : 142 chemins/188 opérations ;
documentation globale encore partielle. Contrat et limites :
[BACKUPS.md](BACKUPS.md#mise-en-attente-et-refus-des-ventes-récupérées--26-octobre-2026).
Archives, audit SQL, préparation de restauration et relevés : **35 tests et
24 sous-tests réussis** après reconstruction du manifeste. La vérification
standard de référence a confirmé `schema_reference: current`.
Les paragraphes suivants conservent les qualifications précédentes.

US-44 : **intégration contrôlée des ventes récupérées** disponible après dépôt,
avec aperçu des effets, motif, droits actuels et décisions versionnées. Prix,
facteurs, remises et UUID historiques conservés ; vente déjà connue reconnue
sans second stock, paiement ou dette. L’origine reste distincte du responsable
qui contrôle, sans créer ni réactiver d’ancien compte. Douze tests API/cinq
sous-tests, quatre tests arithmétiques/neuf sous-tests, trois tests de
modèles/dix sous-tests, concurrence entre deux processus et migration remplie
réussis. Parcours HTTP réel de 500 lignes : 250000 FCFA encaissés une fois,
une seule alerte et compte source toujours retiré. Migration additive
`backups.0020`, 80 migrations historiques intactes, référence SQL 562 objets.
Régression élargie : **394 tests/330 sous-tests réussis** ; contrôle
supplémentaire entre deux générations réussi séparément. La suite générale
entière n’a pas été relancée dans cette tranche. Les 35 tests/24 sous-tests de
sauvegarde, audit SQL, préparation de restauration et relevés concernés passent
également.
OpenAPI strict validé : 139 chemins/185 opérations ; documentation globale
encore partielle. Autres familles, réservations/refus, rapprochements,
clôtures et réouverture/repli restent requis. Contrat, reproduction et limites :
[BACKUPS.md](BACKUPS.md#intégration-contrôlée-des-ventes-récupérées--25-octobre-2026).
Les paragraphes suivants conservent les preuves des tranches précédentes.

US-44 : dépôt durable des journaux par le responsable actuel, y compris pour un accès d’origine retiré. Accusé atomique, UUID et contenu reconnus entre copies, collisions refusées sans écrasement ; listes/fiches autorisées et bornées. **19 tests/34 sous-tests ciblés réussis après durcissement Unicode et pannes SQL simulées**. Régression élargie : **372 tests/472 sous-tests réussis**, onze avertissements connus de fixtures DATABASES ; cette collecte précédait les deux dernières qualifications, couvertes par les 19 ciblés. WSGI corrigé à deux boutiques réussi. Aucun effet métier ni droit créé par la capture ; intégration, validation et réservations restent à implémenter. Migration additive `backups.0019`, 80 migrations historiques conservées, référence SQL reconstruite à 550 objets. Référence et manifeste vérifiés à jour ; le worker isolé a reconstruit les mêmes 550 objets et la commande standard a confirmé current. Les échecs initiaux et leurs vérifications restent consignés dans le registre. Procédure, contrat et limites : [BACKUPS.md](BACKUPS.md#dépôt-durable-des-journaux-de-récupération--25-octobre-2026).

US-44 : les relevés clients, fournisseurs et de caisse exposent désormais le contexte de restauration actuel de la boutique autorisée, sans changer les montants ni leur arrêté `as_of`. La caisse personnelle reste limitée au vendeur. Double pagination OpenAPI corrigée sur ces trois réponses ; corps HTTP réels validés. **35 tests/34 sous-tests ciblés puis 211 tests/322 sous-tests de régression réussis**, six avertissements connus des fixtures DATABASES dans la régression ; parcours WSGI à deux boutiques réussi. Aucun modèle ni migration ajouté. OpenAPI : 133 chemins/178 opérations, 146 sans résumé/description ; documentation globale encore partielle. Détails : [BACKUPS.md](BACKUPS.md#contexte-de-restauration-des-relevés-financiers).

US-44 : inspection des appareils connus disponible en lecture seule, par API pour le responsable actuel et par listes Unfold pour le technique confirmé sous maintenance. Filtrage boutique/génération avant pagination SQL, 25 lignes par défaut/100 maximum, refus traduits et aucun droit accordé par les UUID archivés. **20 tests/28 sous-tests ciblés puis 176 tests/288 sous-tests de régression réussis**, parcours HTTP et réponses OpenAPI vérifiés. Les métadonnées partagées sont dans `backups/api/recovery_metadata.py`. Aucun modèle ni migration ajouté dans cette tranche. Rejeu, clôtures, autres rapports, réouverture et repli restent requis. Détails : [BACKUPS.md](BACKUPS.md#inspection-des-appareils-connus--api-et-unfold).

US-44, tranche précédente : registre de récupération séparé des droits, migration additive `backups.0018`. Catalogue, fiche produit et alertes exposent l’état `restoration`, par boutique et sans identité d’appareil ni coût vendeur. Un inventaire absent reste inconnu ; zéro observation ne vaut jamais récupération complète. **18 tests/12 sous-tests ciblés puis 180 tests/270 sous-tests de régression réussis**, migration 0017→0018 et parcours HTTP vérifiés. La suite générale précédente de 987 tests précède ce registre. Schéma métier courant : 535 objets ; référence et manifeste reconstruits. Détails : [BACKUPS.md](BACKUPS.md#registre-de-récupération-et-métadonnées-du-catalogue--25-octobre-2026).

Installation de restauration disponible en console sous maintenance : intention durable externe, ancien fichier et WAL/journal/SHM conservés, audit isolé de la copie en quarantaine, remplacement atomique et reprise après arrêt de processus. Les fichiers absents/illisibles sont traités explicitement ; aucune ouverture implicite. Parcours HTTP à deux boutiques réussi (200 puis 503), photo/UUID conservés et commande rejouée sans second remplacement. Suite générale de la tranche installation : **987 tests et 1053 sous-tests réussis en 1431,74 s**, dix avertissements de fixtures DATABASES. Activation/réouverture et retour arrière contrôlé restent requis. Détails : [BACKUPS.md](BACKUPS.md#installation-contrôlée-de-la-copie-en-quarantaine--25-octobre-2026).

Tranche vérifiée : 77 tests/204 sous-tests opérationnels, puis 25 tests/317 sous-tests après correction finale OpenAPI ; HTTP/SMTP local réussi. Planificateur hors HTTP protégé par la maintenance : `backend/manage.py backup_scheduler_step` exécute une étape, contrôle le compte technique actuel et ferme les connexions avant de libérer la barrière. Maintenance, barrière absente ou mauvaise base : aucune requête métier. Concurrence et attente SMTP vérifiées avec de vrais processus ; HTTP/SMTP local toujours fonctionnel. Le déclencheur OpenAPI décrit désormais ses statuts et ses deux formes de 503. Procédure et limites : [BACKUPS.md](BACKUPS.md#étape-de-sauvegarde-hors-http-et-barrière-de-maintenance).

Suite générale antérieure à la tranche du planificateur : **960 tests et 1 043 sous-tests réussis en 1080,44 s**, dix avertissements liés aux fixtures DATABASES. JUnit : 2003 cas, aucun échec/erreur/ignoré. Les parcours HTTP vendeur et récupération Unfold ainsi que la concurrence interprocessus ont réussi ; l’activation/repli et la récupération complète restent à qualifier. US-43 et objectif global toujours partiels.

Revalidation des vendeurs après restauration : API GET/POST par boutique disponible pour le responsable connecté, six permissions explicites sans reprise automatique des droits archivés, motif obligatoire et justificatif facultatif. Versions, génération et UUID protègent les reprises ; un refus coupe l’accès de la session existante sans désactiver les autres boutiques. Le rôle actuel détermine le circuit de confirmation : technique pour propriétaire/responsable, responsable métier pour vendeur. Navigation Unfold limitée et provenance des générations corrigées. Parcours HTTP privés et concurrence à deux processus vérifiés ; aucune activation de restauration ni OTP réel démontré. Détails : [BACKUPS.md](BACKUPS.md).

Contrôle des droits après restauration : modèles par compte/boutique/génération et service technique de décisions motivées disponibles, justificatif facultatif, historique et reprise conservés. Console [review_restore_access.py](../tools/review_restore_access.py) disponible sous maintenance : inspection paginée et décisions authentifiées par mot de passe technique, sans réouverture HTTP. Contrôle Unfold FR/EN disponible sous maintenance après activation explicite : preuve de bootstrap externe consommée liée à la base, version, génération et compte technique neuf ; CSRF, historique et conflits visibles. La confirmation de A ne donne pas accès à B ; un refus ultérieur de A laisse B confirmée accessible, avec contrôle dans les requêtes SQL avant pagination et dans les services. Migration additive `backups.0017`, 80 migrations historiques intactes. Détails et limites : [BACKUPS.md](BACKUPS.md).

Qualification historique Unfold : **944 tests et 1 017 sous-tests réussis en 1001,55 s**, dix avertissements de fixtures DATABASES. JUnit 1961 cas, aucun échec/erreur/ignoré. Vingt-sept tests ciblés et seize sous-tests réussis en 99,47 s ; parcours réels Unfold sous maintenance à deux boutiques et sauvegarde/secours console réussis. Le bootstrap refuse une couverture manquante ou une ancienne décision et consigne la génération actuelle avant consommation du code. API métier et autres modèles restent en 503 pendant ce contrôle. Revalidation des vendeurs, participation des traitements hors requête, remplacement/activation/repli et récupération restent requis ; US-43 et objectif global partiels. Les paragraphes suivants sont historiques.

Qualification historique de la tranche console : **928 tests et 1 001 sous-tests réussis en 954,87 s**, cinq avertissements connus de fixtures DATABASES ; JUnit 1929 cas, aucun échec, erreur ou ignoré. Console inspect/decide sous maintenance : mot de passe technique actuel, audit du schéma, liaison ORM exacte, verrou exclusif, inspection paginée et décisions motivées sans réouverture HTTP. Vingt tests ciblés et vingt-trois sous-tests réussis en 122,92 s ; parcours réel HTTP/Unfold/console enrichi réussi après bootstrap de secours. L’interface Unfold de récupération sous maintenance, la revalidation des vendeurs, l’activation/repli et la récupération restent requis. US-43 et l’objectif backend global restent partiels. Les paragraphes suivants retracent les tranches antérieures.

Qualification historique précédente : suite générale **917 tests et 976 sous-tests réussis, un échec de fixture historique**, en 884,75 s. Fixture corrigée sans retirer d’assertion ; qualification complémentaire **18 tests et 105 sous-tests réussis en 41,98 s**. Reprise finale du seul échec avec `pytest --lf` : **un test et sept sous-tests réussis en 33,95 s**. Parcours HTTP dédié à deux boutiques et parcours HTTP/Unfold/console de sauvegarde/quarantaine réussis. Le formulaire de confirmation sous maintenance, la revalidation des vendeurs, l’activation et le repli restent à réaliser. Aucun compte de production créé ni base active remplacée.

Accès technique de secours : registre de preuve externe, code à usage unique et création d'un compte technique neuf disponibles par service et console. Le registre reste hors des archives métier. Émission avant incident en mode ouvert pour le technique actuel ; création en maintenance après audit du schéma et suspension des accès archivés. Consommation et compte dans le même commit SQLite, reprise sans doublon, anciens comptes non réactivés. Commande `tools/technical_recovery.py` indépendante des paramètres web/SMTP/JWT ; secrets saisis sans écho, absents des arguments, reçus et journaux. Tests de concurrence interprocessus, arrêt forcé et retour aux anciens fichiers métier réussis. Suite générale : **901 tests et 965 sous-tests réussis en 856,11 s** ; les deux gardes de saisie ajoutés après sa collecte passent dans la régression complémentaire actuelle de **30 tests réussis en 102,81 s**, cinq avertissements de fixtures DATABASES. Procédure et limites : [BACKUPS.md](BACKUPS.md). Aucun compte ni code de production créé, aucune base active remplacée, aucune révalidation métier ou réouverture automatique.

Suite générale de la tranche précédente du 25 octobre : **884 tests et 965 sous-tests réussis en 772,20 s**, quatre avertissements liés aux fixtures DATABASES. Elle incluait la barrière et OpenAPI 503. Le cas de barrière partiellement initialisée et sa correction avaient été ajoutés après sa collecte : qualification complémentaire de **27 tests et 295 sous-tests réussis en 39,73 s**, cinq avertissements. La preuve technique à usage unique n'était pas encore implémentée lors de cette tranche.

Barrière de maintenance SQLite externe intégrée avant sessions/authentification. Onze tests ciblés de cette tranche antérieure réussis et parcours WSGI réel : accès normal, maintenance 503, abandon sur base inchangée puis accès 200. Hors configuration de tests, initialiser explicitement le contrôle après migration et avant le premier démarrage HTTP ; une barrière absente refuse les requêtes. Procédure : [BACKUPS.md](BACKUPS.md). Le code technique accepté au cadrage est désormais implémenté en console, comme décrit ci-dessus. Aucune activation de base restaurée dans cette tranche.

Restauration : collecte des parties et préparation d’une copie disponibles dans Unfold FR/EN pour le superadministrateur actuel. Préparation hors transaction longue, reprise du même UUID sans seconde copie, clé non stockée, migrations et audit SQL vérifiés sur copie privée. Migrations backups.0014–0016 ; référence déterministe de 510 objets. Dernière suite complète après quarantaine Unfold : **872 tests et 791 sous-tests réussis en 844,56 s**, aucun échec ni test ignoré ; rapport JUnit vérifié (1663 cas avec sous-tests). Parcours HTTP avec CSRF et contrôle SQLite fichier concurrent réussis. Contrôle explicite de la copie dans Unfold ajouté le 25 octobre : empreinte, manifeste, intégrité SQLite et schéma comparés au reçu enregistré, avec droits actuels et CSRF ; une altération à taille égale est détectée. Régression de cette tranche : **19 tests et 2 sous-tests réussis en 71,68 s** ; parcours HTTP réel local réussi. Activation/repli, révalidation des droits, récupération et gestion des préparations interrompues restent requis. Détails : [BACKUPS.md](BACKUPS.md).

Quarantaine des accès archivés : formulaire Unfold FR/EN avec suivi durable, motif, contrôle actuel du rôle et reprise du même UUID sans seconde copie, et outil console `tools/quarantine_server_restore.py`, sans paramètres Django, sur une nouvelle copie privée. Comptes, privilèges d’administration, appartenances, groupes/permissions attribués, installations, sessions et baux sont suspendus ; données métier et source préservées. Reçu UTC avec motif et empreintes ; aucun droit révalidé ni activation autorisée. Régression finale de cette tranche et de ses appels : **28 tests et 4 sous-tests réussis en 156,58 s**, parcours HTTP local avec préparation/inspection/quarantaine console réussi. Après intégration Unfold : douze tests et dix-neuf sous-tests réussis en 87,68 s ; contrôle SQLite fichier concurrent et parcours HTTP réel Unfold/console réussis. Accès technique de secours vérifié, confirmations et activation/repli restent requis.

Stockage : **pilote initial fixé par le titulaire à deux boutiques/six appareils le 24 octobre**, sans quota commercial et avec tous les modules métier conservés. Cible annuelle : 73 000 ventes, 365 000 lignes et 2 000 produits ; dix boutiques reportées à une phase ultérieure. Budget initial et capacité annuelle évalués séparément ; [STORAGE.md](STORAGE.md) décrit mesures, reproduction et limites. Dernière suite complète avant ce changement de défaut : **806 tests et 743 sous-tests réussis en 673,41 s**. Après adaptation TDD des défauts du modèle et de la commande : six tests ciblés et quatre sous-tests réussis en 52,85 s. La mesure annuelle deux boutiques actualisée après 0016 est terminée : base minimale 200 470 528 octets après compactage, pic visible 484 208 238 octets, intégrité/clés étrangères/totaux valides et base privée supprimée. Ce jeu exclut photos et plusieurs opérations métier ; capacité hébergée et budget complet non démontrés. Le rapport historique dix boutiques mesure 995 328 000 octets et un pic visible de 2 917 799 847 octets ; il ne qualifie pas le pilote corrigé.

Sauvegardes serveur : app technique `backups`, préparation manuelle dans Unfold FR/EN et par API réservée au superadministrateur. Instantané SQLite + photos BLOB cohérent, parties chiffrées authentifiées ≤15 000 000 octets, vérification complète et manifeste/journal conservés. Une clé dédiée distincte de Gmail/Django/JWT est requise hors SQLite ; aucune clé de production créée par l’agent. Envoi explicite d’une partie par API technique, états SMTP durables et suivi Unfold FR/EN disponibles. Les reprises liées sont bornées à trois, après 15 minutes ; nettoyage idempotent après acceptation complète avec conservation du manifeste et du journal. Abandon motivé par API/Unfold, avec journal, conservation des fichiers et blocage des nouveaux envois. Commande multiparties durable par API et formulaires Unfold FR/EN, une partie par étape, bail et reprise bornée. Création, avancement manuel, progression, attente UTC et journal filtré disponibles ; métadonnées en lecture seule. Configuration quotidienne API/Unfold FR/EN, activée à 02:00 Douala par défaut, aperçu UTC, désactivation, version et historique disponibles. Déclencheur HTTP à secret dédié et compte technique actuel, capture quotidienne actuelle puis étapes SMTP/reprises bornées, blocage sur archive incomplète et journal des jours manqués disponibles. Synthèse technique Unfold FR/EN : blocage global, plus ancienne archive incomplète, dernière acceptation SMTP complète, date et âge sans rafraîchissement artificiel par doublon tardif. Tâche externe, HTTPS hébergé et restauration active restent requis. Procédure et limites : [BACKUPS.md](BACKUPS.md).

Tranche du 19 octobre : **800 tests et 739 sous-tests réussis en 445,95 s**. Audit SQL renforcé : index manquant, contrainte supprimée, objet supplémentaire et déclencheur caché derrière un préfixe SQLite refusés. Référence de 490 objets reconstruite dans une base vide depuis le code de confiance, liée au code/runtime et vérifiée sans écrasement ; variations de présentation SQL tolérées, littéraux préservés. Anciennes bases backups.0011 et core.0080, données/photos/UUID et types de contenu conservés ; source intacte. Parcours HTTP/Unfold/SMTP local réussi avec copie migrée et auditée, empreinte de référence exacte dans le reçu, base servant l’API inchangée ; neuf soumissions locales, aucun email externe. Activation, import Unfold, révalidation des droits, remplacement contrôlé et repli restent requis. L’incident loopback Windows 10053 antérieur reste consigné au registre ; non observé dans cette qualification.

Dernière qualification, intégrité des mouvements de stock : **641 tests et 646 sous-tests réussis en 201,63 s**, cinq contrôles de concurrence ciblés et parcours HTTP catalogue réussis. Migration `stock.0002`, sans modification des migrations historiques.

Clients US-20 : indicateur `has_possible_duplicate` calculé sur les fiches connues de la boutique et route `GET .../clients/{id}/possible-duplicates/` paginée. Nom identique ou téléphone non vide identique, aucune fusion automatique ; reçus originaux conservés. Migration `customers.0002` de deux index non uniques. Vérification de session renforcée dans les transactions de création/correction/signalement, y compris les imports CSV. Dernière suite : **651 tests et 646 sous-tests**, deux contrôles de concurrence clients et parcours HTTP dédié réussis.

Organisation courante : **sept apps Django métier** (`identity`, `catalogue`, `stock`, `sales`, `customers`, `purchasing`, `cash`), chacune avec modèles, services, API/URLs et tests, plus `backups` pour l’exploitation technique. `common` porte les utilitaires partagés ; `core` conserve les migrations et une façade de compatibilité. Les anciens 89 modules racine de `core` sont répartis dans les domaines ; `core` n'en contient plus que trois. Son `models.py` de compatibilité fait 89 lignes et ne définit plus de modèles métier. Détails et commandes : [ARCHITECTURE.md](ARCHITECTURE.md).

Les 79 migrations historiques restent intactes, ainsi que la migration 0080 de libellés. La migration 0081 transfère l'état des modèles et les types de contenu en conservant tables, UUID, index, contraintes et permissions. Les sept migrations initiales de domaine adoptent cet état sans recréer les tables. Le modèle utilisateur conserve l'identité `core.User`. Les chemins et noms des 121 routes API sont préservés.

Validation finale du découpage : **636 tests et 632 sous-tests réussis** ; parcours HTTP stock/catalogue et création de boutique réussis, migrations sans divergence, OpenAPI validé sans avertissement et traductions compilées à jour. **75 tests de concurrence SQLite et un test de migration historique réussis** (76 contrôles en 666,625 s). Les contrôles emploient des bases temporaires, sans déploiement.

Stock : migration `stock.0002` ajoutant les contraintes de quantités entières en milli-unités et de coûts connus entiers en micros FCFA. Un coût inconnu reste NULL, distinct de zéro ; les quantités signées restent possibles. Une ancienne ligne invalide fait refuser la migration sans arrondi ni application partielle. Tests : `stock/tests/test_movement_storage.py` et `stock/tests/test_movement_storage_migration.py`.

SMTP : paramètres de déploiement dans `config/mail.py`. Variables `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, ou fichier JSON privé désigné par `APP_EMAIL_CONFIG_FILE`. Les variables d’environnement priment sur le fichier. En local hors tests, le fichier facultatif `%USERPROFILE%/.config/app_x/smtp.json` est chargé s’il existe ; les secrets restent hors du dépôt. Le compte SMTP fournit les adresses expéditeur par défaut. Authentification avec TLS obligatoire, délai de connexion 20 secondes, erreurs de configuration sans secret. Aucun envoi à l’import des settings. En tests, boîte mail mémoire ; `backend/config/tests` fait partie de pytest. Connexion Gmail : STARTTLS/TLS 1.3 et authentification vérifiés, sans envoi d’email. Livraison, sauvegardes, pièces jointes, planification et rappels restent à réaliser/qualifier.

Création de boutique connectée : `POST /api/v1/shops/` reçoit `id`, `operation_id`, `name` et un `reason` facultatif. Le propriétaire est le compte de la session actuelle ; aucun champ propriétaire, installation ou permission n’est accepté. Défauts : Africa/Douala, active, plafond vendeur zéro. Même opération/contenu : reçu initial 200 ; création 201 ; réutilisation incompatible 409. Auteur, installation et état créé historisés. Pas de stock, caisse, appartenance artificielle ou bail implicite ; `verify-access/` reste distinct. Deux boutiques de même nom gardent leurs UUID et leurs droits séparés. Les écritures génériques Shop dans Unfold sont refusées ; la consultation autorisée demeure. Les formulaires/actions métier Unfold dédiés et l’activation OTP restent requis. Démonstration locale : depuis backend, `../.venv/Scripts/python.exe -X utf8 -m verification.shop_creation_smoke`, sur base temporaire migrée.

Réservations refusées (US-15/Q25) : le refus seul ne libère plus le disponible connu. `stock_reserved_milli` totalise les quantités en attente et les quantités refusées dont le départ physique reste non régularisé ; `stock_pending_reserved_milli` et `stock_rejected_reserved_milli` les distinguent. Le disponible soustrait ce total une seule fois. Les réservations validées restent exclues puisque leur mouvement validé les remplace. Le centre et l’indicateur vendeur conservent l’alerte après refus, sans réarmer un épisode ni créer de mouvement physique ou financier. L’ingestion des opérations en attente, leur validation/refus et la libération explicitement rapprochée d’une régularisation restent à construire : les tests de lecture injectent ces sources, sans prétendre livrer ces parcours.

Centre boutique des coûts à examiner : `GET /api/v1/shops/{shop}/sale-cost-issues/`, filtres facultatifs `product_id` et `cost_state` (`unknown` ou `provisional`). `totals` compte les deux états sur tout le filtre, indépendamment de la page. `as_of` est conservé dans les liens paginés ; ventes et dernières versions de coût sont sélectionnées parmi les données enregistrées au plus tard à cet instant. Une nouvelle consultation sans arrêté reprend les données actuelles. Chaque ligne inclut `product_id`. La route par produit conserve sa portée et reçoit aussi ces métadonnées. `data_scope: server_known` ne prouve pas la complétude des téléphones. Droits actuels contrôlés même lors de la consultation d’un ancien arrêté.

Coûts de vente à examiner : `GET /api/v1/shops/{shop}/products/{product}/sale-cost-issues/` fournit au responsable les lignes dont la dernière version de coût est inconnue ou provisoire. Une ligne héritée sans snapshot reste visible avec `cost_state: unknown` et `cost: null` ; aucun montant nul n’est inventé. Référence vente/ligne, nom facturé, date effective, quantité de base, montant net et dernier coût sont exposés sans écriture. Les coûts connus sont exclus selon leur dernière version ; anciennes versions conservées. Pagination/filtrage SQL, ordre stable et chargement des coûts de la seule page. Session actuelle et rôle responsable exigés, même si un vendeur possède la lecture des coûts. Cette consultation ne valide pas les anciens coûts ; la méthode de confirmation reste soumise à l’arbitrage U127.

Anciens manques physiques : migration 0079, décision `StockShortageReview` réservée au responsable lorsque le stock courant est nul ou positif. `POST .../products/{product}/stock-shortage-reviews/preview/` reçoit `{}` ou un `count_id` facultatif du même produit ; confirmation sur `POST .../stock-shortage-reviews/` avec `id`, `operation_id`, `reason`, `preview_token` et le même justificatif facultatif. L'aperçu fournit quantité, valeur informative, nombre de mouvements et date ; `GET .../stock-shortage-reviews/movements/` expose les faits physiques paginés côté SQLite, dates effective/enregistrement, auteur, source et dernier coût capturé lorsqu'il existe. Liste et détail des décisions conservent le contrôle et son motif.

L'examen clôture uniquement le manque physique connu ; quantités, caisse, dette et anciens coûts restent inchangés. Un changement physique antérieur arrivé tardivement impose un nouvel examen, tandis qu'une correction de frais seule ne rouvre pas ce contrôle physique. La valorisation reste soumise à son examen distinct ; les anciens coûts provisoires ne sont pas approuvés par cette décision. Un inventaire motivé avec correction explicite doit d'abord traiter un stock encore négatif. Le réessai identique rend le reçu original après vérification des droits actuels. `current/` expose aussi `invalid_shortage_reviews_count`, compteur historique des examens incompatibles.

Stock nul : migration 0078, clôture explicite d'un écart de valorisation ouvert avec les mêmes routes `stock-valuations/`, valeur FCFA obligatoire zéro, aperçu et motif. `valuation_action` distingue `set_remaining_value` et `close_empty_gap`. La clôture conserve l'écart signé historique et tous les coûts antérieurs, sans mouvement de quantité, dette ou caisse. Des frais ou opérations antérieurs tardifs rendent la base périmée et nécessitent un nouvel examen ; le réessai conserve le reçu initial et ne referme pas automatiquement le dossier. Un stock nul sans écart ne crée pas de clôture. Le stock négatif reste refusé pour cette décision de valeur. `current/` distingue `stock_value_state`, `has_unresolved_valuation` et `has_unresolved_shortage` : une clôture de valeur ne clôture pas une ancienne survente. Un recalcul équilibrant un écart déjà capturé ne le clôture pas automatiquement ; les signalements historiques de manque des ventes et pertes restent également conservés jusqu'à leur examen physique distinct décrit ci-dessus.

Valeur restante : migration 0077 et nouvelle décision `StockValuationAdjustment`, selon l'arbitrage du titulaire. `POST .../products/{product}/stock-valuations/preview/` reçoit `value_fcfa` entier ; `POST .../stock-valuations/` confirme avec `id`, `operation_id`, même valeur, `preview_token` et motif. Le responsable confirme la valeur totale d'un stock positif à l'instant serveur, sans mouvement de quantité, de caisse, de dette ou de coût historique de vente. Le CUMP des mouvements suivants part de cette valeur. L'empreinte porte sur les sources de coût antérieures : une réception ou des frais tardifs rendent la confirmation périmée, signalée sans effacer la décision. Une nouvelle confirmation explicite peut rétablir la valeur ; aucune valeur de remplacement automatique. Réessai d'une décision déjà enregistrée : reçu initial conservé, après contrôle des droits actuels.

`GET .../stock-valuations/current/` fournit quantité/valeur connues sur le serveur, état de régularisation et date de calcul. `invalid_adjustments_count` compte les anciennes confirmations devenues incompatibles, y compris après une nouvelle confirmation valide ; ce n'est pas le nombre d'écarts encore ouverts. Liste/détail des décisions conservés et liste paginée côté SQLite. Tous ces accès exigent responsable et session actuelle. Valeurs longues en chaînes JSON. Les anciens coûts de survente restent provisoires et leur validation distincte demeure requise ; les stocks négatifs ne sont pas régularisés par une simple décision de valeur. Complétude multi-appareils, synchronisation, rapports et qualification restent requis.

Coûts d'inventaire : la confirmation motivée capture `cost` dans la transaction de correction. Un écart négatif utilise le CUMP ; un écart positif utilise le CUMP connu au comptage selon l'arbitrage du titulaire, sinon son coût reste inconnu. À instant UTC identique, les corrections suivent les autres mouvements et gardent leur ordre de validation. `GET .../products/{product}/stock-counts/{count}/adjustments/` expose les décisions paginées et leur dernière version de coût, sous droits actuels de responsable. Les frais tardifs se répercutent par le recalcul explicite existant ; les reçus initiaux, observations physiques, dettes et mouvements d'argent restent conservés. Une correction nulle a `cost: null`. Les inventaires hérités sans coût ne reçoivent un coût reconstruit qu'après recalcul explicite ; complétude multi-appareils et régularisation restent requises.

Validation courante : **626 tests et 415 sous-tests réussis en 155,90 s**. Sept tests de configuration SMTP et neuf sous-tests couvrent chargement privé/environnement, priorité, validation sans secret, intégration Django et livraison uniquement en mémoire pendant les tests. Connexion Gmail réelle : STARTTLS/TLS 1.3 et authentification acceptés, sans email envoyé. Chargement local hors tests vérifié ; zéro occurrence du mot de passe dans les fichiers non ignorés du workspace. Django check sans problème. Aucun contrôle concurrent supplémentaire pour cette configuration ; le précédent contrôle de création reste daté dans le registre (75 disponibles). Sauvegardes, rappels, livraison SMTP, planification, déploiement et qualification globale restent requis.

Retours anciens : reconstruction explicite des coûts des réceptions. Base héritée connue/inconnue/provisoire conservée et marquée, puis révision tracée au coût source approuvé ; propagation aux remises en vente et ventes suivantes. L'API responsable conserve aperçu sans écriture, contrôle de l'aperçu à la confirmation, reprises stables et historique paginé. Les incertitudes des anciennes surventes restent provisoires. La revue courante des frais se clôture après traitement de tous les produits concernés ; une correction la rouvre. Marge, transferts, régularisations et autres critères du PRD restent requis. Les sections suivantes retracent les tranches précédentes ; la matrice à jour se trouve dans `../analysis/backend-us-coverage.md`.

Coûts des pertes : `GET/POST .../products/{product}/losses/` et GET détail fournissent désormais `cost`, capturé dans la transaction à la date effective. Les GET lisent la version actuelle ; les reprises conservent le reçu initial. Inconnu, zéro et provisoire distincts ; total nano-FCFA et ratio exact en chaînes. Les frais tardifs exposent `loss_cost_before/after` dans l'aperçu de recalcul et ajoutent une version liée sans deuxième perte, paiement ou dette. Les anciennes pertes sans snapshot gardent `cost: null` jusqu'à un recalcul explicite. Autres sorties physiques et qualification des volumes restent requises.

Départs fournisseur : selon l'arbitrage du titulaire, chaque ligne sort au coût de son achat d'origine, frais affectés compris. Le coût est capturé, versionné et exposé dans `lines[].cost` ; l'avoir accepté et le remboursement restent séparés. Si ce retrait rend la valeur restante incompatible, la quantité et le coût du départ sont conservés, la valeur restante devient inconnue et l'écart signé est tracé. Stock vide avec différence de valeur : zéro unité, valeur restante zéro, différence explicitement signalée. `valuation_requires_regularization` signale les incertitudes/écarts ; les valeurs restantes positives peuvent désormais être confirmées explicitement via les routes ci-dessus, les autres clôtures restant requises. Aucune correction financière automatique. Migration 0076 pour ces indicateurs. `valuation_kind`, cause, prédécesseur et date distinguent les coûts capturés, reconstruits et révisés ; leurs anciens états ne sont pas effacés par une confirmation de valeur restante.

Routes : `GET .../products/{product}/cost-revaluations/preview/`, `GET/POST .../cost-revaluations/`, `GET .../{batch}/`, `GET .../{batch}/items/` et `GET .../{batch}/returns/`. Confirmation : `id`, `operation_id`, `reason`, `preview_token` reçu de l'aperçu. Pour reconstruire les anciennes réceptions sans snapshot : ajouter `?reconstruct_legacy_returns=true` à l'aperçu et `"reconstruct_legacy_returns": true` à sa confirmation. Mode désactivé par défaut et lié au jeton ; la réponse distingue le nombre d'anciennes réceptions et le nombre de reconstructions prévues. `origin_kind` distingue une base figée lors de la réception d'une base héritée capturée lors de la reconstruction. Un historique physique manquant reste incomplet.

Les historiques sont paginés côté SQLite ; coûts exacts et quantités longues en chaînes JSON, états inconnus/provisoires explicites. Session actuelle et responsable exigés pour toutes ces routes. Migrations 0073–0075. Démonstration HTTP reproductible depuis backend : `../.venv/Scripts/python.exe -X utf8 -m verification.sale_cost_smoke`, sur une base temporaire migrée, sans données de production.

Inventaires US-13 : **254 tests et 173 sous-tests réussis**, migrations 0043–0044. `GET/POST .../products/{product}/stock-counts/`, `GET .../{count}/`, `POST .../{count}/confirm/`, `GET .../{count}/history/`. Observation : UUID/opération, `counted_at`, `physical_milli`, note facultative. Le physique et l'attendu d'origine restent fixes ; attendu actuel calculé à l'instant du comptage, hors corrections propres à ce comptage. Confirmation : `operation_id`, `expected_milli`, `applied_adjustment_milli`, motif obligatoire ; préconditions actuelles contrôlées avant mouvement. Les arrivées tardives proposent un complément, jamais automatique, selon précision du titulaire. Toutes les décisions et le premier comptage sont conservés ; écart nul sans mouvement fictif. Vendeur refusé, session/droits revérifiés, reprises identiques 200. Coût, réservations en attente et complétude des appareils encore absents : résultat provisoire.

Qualification courante : **37 tests de concurrence réussis**. Même comptage/confirmation rejoués simultanément : un effet et mêmes reçus 201/200 ; deux décisions différentes sur la même précondition : 201/409. HTTP : physique 5000, attendu 6125, correction −1125 ; perte antérieure tardive 100, attendu 6025, complément +100 explicitement confirmé, courant 4000 après les ventes ultérieures, aucune caisse ajoutée et trois événements d'historique. Django, migrations et OpenAPI validés. Livraison complète non démontrée.

Pertes US-14 et renommage de catégorie : migrations 0040–0042. `PATCH .../categories/{category}/` reçoit `operation_id`, `base_version`, `name`, `reason` ; version incrémentée et avant/après conservés, vendeur refusé et conflit 409. `GET/POST .../products/{product}/losses/` et `GET .../losses/{loss}/` : événement UUID/date effective/motif et `quantity_milli` en unité de base. Une perte modifie uniquement le stock, sans vente, dette ou cash ; reprise 201/200 sans double effet. Pièces entières, poids/volume au milli. Perte réelle dépassant le stock connu conservée avec régularisation signalée. Le `stock_after_milli` est le snapshot exact lors de l'enregistrement, et reste celui du reçu original après d'autres événements. Coût encore null ; stock non vendable et rapprochements offline absents.

Qualification courante : **245 tests et 169 sous-tests**, **35 tests de concurrence réussis**. Renommages concurrents : une seule version validée, autre 409 ; perte simultanément rejouée : un seul mouvement physique et même reçu 201/200. HTTP complet : version catégorie 2, perte 125 milli-unités, stock 5125 et cash inchangé. Grand stock exact au-delà d'int64 vérifié, reçu original conservé après perte ultérieure. Django, fraîcheur des migrations et OpenAPI validés. Les comptes plus anciens ci-dessous décrivent les tranches précédentes ; livraison complète non démontrée.

Avoirs US-30 : **237 tests et 160 sous-tests réussis**, migrations 0037–0039. `GET/POST .../suppliers/{supplier}/credits/`, `GET .../credits/{credit}/`, `POST .../refund/`, `POST .../allocate/`, `GET .../history/`. Acceptation : événement UUID/date/motif, `return_id`, `amount_fcfa` réellement accepté. La référence conserve la différence de valeurs cumulées arrondies selon précision du titulaire. L'avoir réduit d'abord la dette de l'achat concerné, puis garde son excédent séparé. Affectation explicitement confirmée à une dette du même fournisseur/boutique ; aucun cash. Remboursement : événement et moyen/montant réellement reçu, source unique de caisse positive ; reprise identique 200 et autre contenu 409. Exemple dette 3000/avoir 5000 →crédit 2000, remboursement 1500 →crédit 500 et cash +1500. Historique SQL paginé avec auteur/motif ; vendeur refusé. Montants stockés entiers contrôlés par SQLite et API stricte ; coûts et protocoles offline restent à construire.

Qualification courante : **33 tests de concurrence SQLite fichier réussis**, dont reprises simultanées d'avoir/remboursement et protection du crédit disponible contre deux remboursements concurrents. HTTP complet réussi : référence 2000/accepté 9000, dette 7000 soldée, remboursement Orange 1500, crédit disponible 500 et historique de deux événements. Check Django, migrations et OpenAPI validés. L'objectif complet et les gates Android/externes restent ouverts.

Tranche courante : **228 tests et 154 sous-tests réussis**. Catégories US-09 (`GET/POST /api/v1/shops/{shop}/categories/`, `GET .../categories/{category}/`) et `category_id` facultatif sur création/modification produit, avec droits/isolation et journal. Les reçus antérieurs à cette option sont rejoués inchangés. Modification produit : session actuelle revérifiée sous transaction. Migration 0035.

Départ fournisseur US-30 : `GET/POST .../suppliers/{supplier}/invoices/{invoice}/returns/`, champs d'événement et `lines` (`line_id`, `quantity_milli`), migration 0036. Selon précision validée du titulaire : cumul borné aux quantités réellement reçues moins retours déjà confirmés. Le départ ajoute uniquement un mouvement physique négatif ; dette et cash inchangés, original/reprise 201/200 identiques. Avoir réellement accepté, référence valorisée, remboursement fournisseur et non-vendable restent à construire. Les comptes plus anciens ci-dessous décrivent les tranches précédentes.

Vérification courante : **31 tests de concurrence réussis**, dont catégorie rejouée simultanément sans doublon et deux départs distincts empêchés de dépasser les quantités reçues. HTTP loopback catégories/retour réussi : 201/200 identiques, catégorie visible au vendeur, départ −2500 milli-unités, dette 7000 et caisse inchangées. Check Django, migrations et OpenAPI validés. Aucune preuve de charge cible ou de livraison Android déduite de ces essais.

Le projet et `.venv` existaient à la reprise. Django et Unfold sont conservés aux versions verrouillées. `pytest` et `pytest-django` sont désormais installés ; configuration racine `pytest.ini`, dépendances de développement séparées dans `requirements-dev.txt`.

Tranche achats vérifiée : **209 tests et 148 sous-tests réussis** après les migrations 0029–0032. Routes responsables `GET/POST /api/v1/shops/{shop}/suppliers/`, puis `.../{supplier}/orders/`, `.../{supplier}/invoices/`, `.../{supplier}/debts/` et `.../{supplier}/invoices/{invoice}/receipts/`. Commande sans dette ; facture sans stock/cash ; dette d'ouverture distincte ; réception partielle modifiant seulement le stock physique. Les créations prennent un UUID d'entité et `operation_id`, une date effective UTC et un motif. Une reprise identique renvoie 200 et le reçu original ; contenu différent sous même UUID renvoie 409. Facteurs, prix et noms des lignes sont conservés. Dépassement du reliquat refusé explicitement.

Suite de concurrence courante : **27 tests réussis**, dont facture/réception simultanément rejouées avec une dette et un mouvement de stock uniques. Check Django, fraîcheur des migrations et validation OpenAPI réussis.

Paiements fournisseurs US-29 : **217 tests et 154 sous-tests réussis** après migrations 0033–0034. `GET/POST .../{supplier}/payments/`, `GET .../payments/{payment}/`, `POST .../payments/{payment}/allocate/`. Le paiement reçoit les champs d'événement (`id`, `operation_id`, `occurred_at`, `reason`) et des `parts` UUID/moyen/montant/référence facultative. Une avance se conserve sans affectation automatique ; la décision séparée reçoit `operation_id`, `reason` et `allocations` (`debt_id`, `amount_fcfa`). Paiement 30000, facture 100000, affectation 30000 →dette 70000 sans deuxième sortie. Le journal des affectations conserve auteur/motif/date. Les moyens inconnus ou mal nommés sont refusés par API et SQLite. Les dettes sont filtrées par fournisseur et boutique ; vendeur refusé même avec accès coûts/caisse. Les avoirs/remboursements fournisseurs restent à construire.

Qualification paiements : **29 tests de concurrence réussis**, avec paiement/affectation rejoués simultanément une fois et avance consommée une seule fois par deux décisions concurrentes distinctes. HTTP loopback réussi : paiement 3000 réparti 1000 espèces/2000 Orange et affectation à facture 10000 →dette 7000, deux sorties initiales seulement, reçus 201/200 identiques. Aucune capacité cible ou intégration externe déduite de ces essais locaux.

HTTP loopback achats réussi : facture 10000 FCFA, 6 sachets reçus sur 10, stock ajouté 7500 milli-unités selon facteur historique, 4 sachets attendus, dette 10000, aucun mouvement de caisse. Création/reprise 201/200 pour facture et réception. Coût des réceptions encore null, CMP et frais tardifs non implémentés ; réception avant facture et parcours offline restent à construire. Le détail des preuves et la matrice des 46 stories se trouvent dans `analysis/backend-tdd-progress.md` ; les comptes historiques ci-dessous décrivent des tranches précédentes.

Depuis la racine du dépôt :

```powershell
& ./.venv/Scripts/python.exe -m pip install -r backend/requirements-dev.txt
& ./.venv/Scripts/python.exe -m pytest -q
& ./.venv/Scripts/python.exe -m pytest backend/verification/concurrency_checks.py --ds=config.concurrency_test_settings -q
```

Résultats les plus récents : **195 tests et 145 sous-tests réussis**. Concurrence SQLite fichier : **26 tests réussis**, dont reprises simultanées d'une dépense avec une seule sortie et d'un comptage avec une seule observation. Les tests de concurrence emploient une base dédiée distincte des données métier et des connexions séparées. `pytest` est exécuté après chaque lot de modifications pendant les cycles TDD.

US-32, tranche comptages : `GET/POST .../cash-counts/` et `GET .../cash-counts/{id}/`, réservés au responsable. Physique, instant de comptage, acteur, installation, motif, réception serveur et attendu d'origine sont conservés. L'attendu est recalculé avec les mouvements réellement reçus dont la date effective est au plus celle du comptage, séparément par moyen nommé. Une opération postérieure ne change pas ce rapprochement ; une arrivée antérieure tardive révise l'attendu/l'écart, jamais le physique ou le reçu original. Aucune correction financière automatique. Migrations 0026–0027 ; l'attendu d'origine est stocké en entier JSON exact avec contrôle SQLite du type, afin de préserver un total au-delà d'int64. Résultats provisoires : la complétude des téléphones n'est pas démontrée. Décisions explicites de régularisation, historique détaillé des révisions et diagnostic des appareils non synchronisés restent à construire.

HTTP comptage vérifié : attendu 47000, physique 45000, écart −2000 ; création 201/reprise identique 200 sans mouvement de caisse. Une dépense réelle antidatée de 1000 révise l'attendu à 46000 et l'écart à −1000, avec attendu d'origine 47000 et physique 45000 conservés. Une seule nouvelle sortie correspond à cette dépense, aucune correction implicite du comptage.

US-31, tranche caisse : `GET/POST /api/v1/shops/{shop_id}/cash/`. Fonds initial, apport, retrait et dépense sont des événements réels distincts de la vente, motivés et réservés au responsable. Les dates effectives passées sont conservées avec la date de réception, l'auteur et l'installation. Reprises d'opération sans double effet, UUID réutilisé avec contenu différent refusé. Les contraintes SQLite vérifient une source et un signe cohérents, migrations 0024–0025. Le vendeur consulte ses propres mouvements ; `scope=global` exige sa permission actuelle de solde global et ne donne pas le droit d'écrire. Les totaux exacts portent sur tout le filtre autorisé, indépendamment des pages ; les moyens `other:Nom` sont séparés. Les liens figent `as_of` et `scope`, les arrivées ultérieures antidatées sont exclues de cet instantané. Aucun effacement quotidien ou mouvement de caisse pour une simple affectation. Les résultats restent provisoires tant que la complétude des téléphones n'est pas démontrée. Fournisseurs, comptages physiques et synchronisation restent à construire.

HTTP caisse vérifié après la suite de concurrence, sans augmenter le délai de dix secondes par requête : fonds initial 201, reprise 200 identique, dépense 201 ; espèces globales 55000/Orange 570, vue personnelle vendeur espèces 500/Orange 870. Le lancement parallèle précédent avait expiré sur la première requête ; cet essai séquentiel ne démontre pas la capacité cible de 30 appareils. OpenAPI, check Django et fraîcheur des migrations passent.

US-22/23, tranche monnaie due : les paiements de vente et encaissements acceptent ensemble `received_fcfa` et `returned_fcfa`, avec vérification exacte du net conservé et contraintes SQLite. Une vente de 7500 avec 10000 conservés exige un client, crée une obligation de 2500 et garde son chiffre d'affaires de 7500. `GET .../clients/{client_id}/credits/` expose ces obligations selon les droits actuels. Les décisions responsables `POST .../credits/{credit_id}/allocate/` et `refund/` sont motivées, journalisées et idempotentes ; l'affectation ne crée pas de cash, le remboursement réel crée une seule sortie. `GET .../regularizations/` expose leur historique. Les références externes identiques par boutique/moyen déclenchent un indicateur sans supprimer un paiement réel ; la réponse d'origine reste figée lors d'une reprise. Index de recherche et migrations 0020–0023. Les anciens paiements sans détail remis/rendu restent explicitement inconnus ; ces champs sont encore facultatifs pour compatibilité avec les demandes précédentes. Offline, retours/avoirs et conflits de remboursements physiques restent à terminer.

HTTP réel local vérifié : vente 7500, caisse +10000, obligation 2500 ; remboursement 2500 en 201, reprise identique en 200, une sortie −2500 et obligation soldée. Deux reprises concurrentes d'une vente avec monnaie due puis d'un remboursement produisent chaque fois un seul effet. Deux ventes réelles concurrentes portant la même référence sont conservées avec un indicateur de doublon possible.

US-03, tranche équipe : `GET /api/v1/shops/{shop_id}/team/`, `GET/PATCH .../team/{membership_id}/` et `GET .../history/`. Lecture et journal réservés aux responsables. PATCH exige UUID d'opération, `base_version`, motif et changements explicites. Propriétaire seul autorisé à nommer ou modifier un responsable ; responsable limité aux vendeurs de sa boutique. Retirer `can_read_debt` renvoie les permissions dépendantes en 409 ; une nouvelle demande avec `confirm_revoke_dependencies: true` confirme leur retrait. Coûts et solde global de caisse restent des permissions séparées ; ce dernier est faux par défaut.

Reprises contrôlées après vérification des droits actuels, ancien reçu conservé, UUID réutilisé avec contenu différent refusé, conflits de version sans écrasement. Journal avant/après atomique. Deux requêtes concurrentes de version 1 donnent un 200/un 409 ; deux reprises identiques donnent deux reçus égaux et un seul événement. Contraintes SQLite ajoutées sur les rôles acceptés et les versions positives, migrations 0008–0009. L'édition générique des appartenances dans Unfold est en lecture seule pour empêcher un changement de droits sans contrôle propriétaire/motif/journal ; le parcours métier Unfold reste à construire.

Essai indépendant `python -m verification.catalogue_smoke` depuis `backend` : HTTP réel avec base temporaire, changement/reprise 200/200, une seule entrée d'historique, droits crédit actuels dans le bail vendeur et solde global caché. Schéma OpenAPI validé sans avertissement. Ces preuves ne démontrent ni invitations/activation OTP, ni les 46 stories complètes, ni déploiement ou capacité PythonAnywhere. État détaillé et cycles : `analysis/backend-tdd-progress.md`.

Les notes historiques ci-dessous décrivent les étapes antérieures ; leurs nombres de tests ne remplacent pas les résultats courants.

US-09, tranche modifications : `PATCH /api/v1/shops/{shop_id}/products/{product_id}/` et `GET .../history/`. Nom/prix de vente/prix d'achat connu ou null/archivage, motif, UUID d'opération et version attendue. Écriture et journal réservés au responsable, stock et coût d'ouverture inchangés. Ancien reçu de création conservé même après nouveau tarif. Migration 0010. Deux mises à jour concurrentes donnent un 200/un 409 ; reprise simultanée identique : deux reçus égaux, une version et un seul journal. HTTP réel confirme nouveau tarif, coût/stock inchangés, historique et reçu original préservé. La proposition périmée est refusée sans écrasement ; sa conservation dans la future file de synchronisation et sa résolution explicite restent à construire. Catégories, photos et copie interboutique restent absentes. Nouvelles ventes comptant d'un produit archivé désormais refusées.

US-17/18/36, ventes : `GET/POST /api/v1/shops/{shop_id}/sales/` et `GET .../sales/{sale_id}/`. UUID vente/opération/lignes/paiements, quantités en milli-unités, version produit attendue. Tarifs serveur ; prix de conditionnement propre, facteur/nom/unité/prix/remises/montants facturés figés dans les lignes. `payments` utilise `cash`, `mtn`, `orange`, `bank` ou `other` nommé, montant FCFA entier et référence externe facultative. Le mode `cash` par défaut exige un total payé égal au net ; le mode `credit` admet un paiement partiel ou absent selon permission et client. Montants remis/rendus, trop-perçus à la vente et monnaie due restent à construire. Une vente de montant nul crée du stock vendu sans paiement ni mouvement de caisse fictif.

Écritures vente/lignes/stock/paiements/caisse/journal atomiques ; une reprise identique restitue le reçu original après recontrôle session et droits actuels. Même UUID avec autre demande refusé ; installation d'origine incluse dans la demande canonique. Deux ventes réelles de la dernière unité sont conservées avec stock −1 et deux paiements. Le champ `stock_requires_regularization` signale l'écart observé à l'enregistrement ; le workflow de régularisation et les alertes restent absents. Les coûts de vente sont explicitement non calculés/null à ce stade, y compris en présence d'un coût d'ouverture : aucune marge calculée ou coût zéro présenté. Coût moyen, révisions historiques et interface des reçus restent à construire. Les dates sont celles du serveur pour ce parcours connecté ; les opérations hors connexion et l'antidatage responsable nécessitent leurs futurs parcours.

US-18 : `GET/POST .../discount-policy/`, valeur `seller_discount_cap_basis_points` (500 =5 %), UUID d'opération, motif et version attendue. Responsable/propriétaire seuls modifient ; valeur zéro par défaut, anciennes factures inchangées, conflit de version sans écrasement et reprises sans second journal. Plafond et version renvoyés dans `verify-access`. Vendeur ne fournit jamais son plafond à la vente ; responsable autorisé utilise l'exemption prévue par le calculateur. Bornes de transport : 500 lignes/50 paiements par requête, sans quota de nombre de ventes. Migrations 0011–0013, totaux nets et quantités positives protégés aussi en SQLite.

Dernier essai HTTP réel temporaire : plafond 500/version 2, ligne 0,5 kg ×1200 =600, remise 30, net 570 ; espèces 200/Orange 370, stock 1250→750 ; création 201/reprise 200/lecture identique. Aucun fournisseur de paiement appelé. Schéma OpenAPI validé sans avertissement ; migrations sans divergence et check Django sans erreur. Ces preuves ne démontrent pas le protocole de synchronisation, les 30 appareils du pilote ou les budgets d'hébergement.

US-19/24 : `mode: credit`, `customer_id` obligatoire, `due_date` facultative en date locale ISO. Le crédit requiert la permission actuelle `can_grant_credit`. La dette créée est le net moins les paiements effectivement reçus ; aucun cash fictif. Aucun compte/OTP client imposé. Échéance enregistrée comme minuit local converti UTC : 31 octobre Douala →30 octobre 23:00Z, retard au lendemain local seulement. Une date impossible à représenter en UTC est refusée avant toute écriture. Pour un client déjà endetté, 409 renvoie `known_debt_fcfa` ; `confirmed_debt_fcfa` doit confirmer le montant connu actuel avant validation. Aucun plafond d'encours ajouté.

`GET .../clients/{client_id}/debts/` : dettes paginées, montants initiaux/restants, échéances UTC et retard actuel, sous permission effective de lecture des dettes. Les ventes `credit` sont exclues des téléchargements et détails lorsque cette lecture a été retirée. Les reçus d'opération rejouée gardent leur instantané original ; les soldes courants se lisent via les dettes. L'offline et les appareils retirés nécessitent encore leur protocole distinct de ce parcours connecté.

US-21/23 : `GET/POST .../clients/{client_id}/payments/` et `GET .../payment-preview/?amount_fcfa=5000`. Prévisualisation ancienne dette d'abord, montant connu sur tout le filtre du client ; au maximum 500 allocations proposées, `allocation_limit_reached` explicite si la borne est atteinte. POST : UUID paiement/opération, `parts` par moyen réel (50 max) et `allocations` explicites (500 max), chacune UUID/dette/montant. Création/prévisualisation exigent `can_collect_debt`, indépendante du crédit ; historique exige lecture effective des dettes. Transaction unique pour réception, allocations, projections de soldes, caisse et journal. Reprise du même UUID/payload après contrôle des droits restitue le reçu sans second cash.

Une allocation devenue trop grande est conservée avec montant demandé et montant réellement appliqué à la dette ; le reste apparaît en `unallocated_fcfa` et `requires_regularization`. Aucun effacement d'encaissement, remboursement ou affectation automatique à une autre dette. Deux encaissements concurrents de 10000 sur dette 10000 →caisse 20000/dette 0/montant non affecté 10000. Les actions explicites de régularisation des encaissements clients sont décrites ci-dessous ; les autres sources d'avoir/monnaie due restent à construire. Prévisualiser ne confirme ni paiement ni complétude des autres appareils.

Migrations 0014–0015. HTTP réel : vente crédit 600/payé 100/dette 500, échéance 2026-10-30T23:00:00Z ; proposition d'affectation 200 ; encaissement 201/reprise 200, une entrée de cash 200 et dette restante 300. Ces preuves ne couvrent pas les soldes d'ouverture par API, modifications d'échéance, relevés chronologiques, fusion ou rappels.

US-23, régularisation d'un encaissement existant : `POST .../clients/{client_id}/payments/{payment_id}/allocate/`, `POST .../refund/` et `GET .../regularizations/`. Responsable/propriétaire seulement, UUID décision/opération et motif obligatoires. Affectation : liste UUID/dette/montant exact, sans nouveau paiement ni cash. Remboursement déclaré réellement effectué : montant et moyen choisis, sortie de caisse négative distincte, sans réouverture de dette. L'encaissement et ses parties originaux restent conservés. Décisions historisées avec disponible avant/après, auteur et date ; soldes et journal atomiques. Un montant devenu insuffisant exige relecture (409), sans décision partielle. Aucune affectation ou restitution automatique.

Migration 0016. Reprises identiques : reçu original, sans second effet après recontrôle des droits. Deux affectations concurrentes du même disponible donnent un 201/un 409 ; remboursement rejoué simultanément donne 201/200 et une seule sortie. Historique paginé réservé au responsable, retrait du rôle pris en compte. HTTP réel : encaissement non affecté Orange 500, affectation 200 à dette, remboursement déclaré Orange 300, reprise 200, deux événements de décision ; dette finale 100, source non affectée soldée et une seule écriture −300. La conservation en avance, les avoirs/monnaies dues issus de ventes ou retours et la reprise offline restent à traiter ; ne pas déclarer US-23 entièrement livrée.

US-24, ouverture et échéance : `POST .../clients/{client_id}/debts/` avec UUID dette/opération, montant entier positif, date effective passée/présente et motif ; échéance locale facultative. Dette antérieure distincte de vente, sans stock ni cash créés. Reprise idempotente et conflits de contenu/identité refusés. `POST .../debts/{debt_id}/due-date/` exige UUID d'opération, motif, `base_version` correspondant au `due_version` actuel et `due_date` explicite (date ISO ou null pour retrait). Responsable/propriétaire seulement ; compte/session/installation/droits recontrôlés dans la transaction. Version d'échéance indépendante des encaissements ; seul le champ échéance change, avec journal avant/après. `GET .../history/` paginé, responsable uniquement, includes origine et changements d'échéance. Migration 0017.

Preuves : 5 nouveaux tests observés en échec sur POST absent (405), puis comportement implémenté ; 2 vérifications complémentaires du reçu d'ouverture et d'une dette de vente après encaissement. Concurrence : même ouverture donne 201/200 et une dette ; deux dates à version 1 donnent un 200/un 409 et une version 2. HTTP réel : ouverture 1000 effective au 1er janvier 2026, création 201/reprise 200, aucune écriture cash ajoutée ; échéance du 30 novembre stockée 2026-11-29T23:00:00Z, deux événements de journal. Avances/avoirs d'ouverture, synchronisation et qualification complète restent requis.

US-24/41, relevé : `GET .../clients/{client_id}/statement/`, responsable/propriétaire uniquement. `date_from`/`date_to` : dates locales inclusives ; `as_of` : arrêt de connaissance UTC porté par les liens de pagination ; `page_size` : 100 maximum. Autorisation puis filtre boutique/client avant comptage et pagination SQL. Capture SQL vérifiée : une page utilise LIMIT, aucune lecture du journal pour un vendeur refusé.

Journal atomique `CustomerLedgerEntry` : ouverture, vente, paiement immédiat, collecte, affectation, remboursement et changement d'échéance. Références internes UUID, auteur/motif, dates effective/de réception, allocations demandées/appliquées, solde après chaque ligne. Solde antérieur/final et dettes/non-affecté sur tout le filtre, sommes Python exactes sans overflow de SUM SQLite. Une affectation n'ajoute pas d'argent et le solde net ne solde pas les dettes automatiquement. Les états validé/en attente/refusé sont distincts à la lecture ; fixtures de lecture uniquement pour les deux derniers, sans ingestion offline déjà livrée. `provisional: true` tant que la complétude des appareils n'est pas établie.

Migrations 0018–0019 : reprise des sources financières immuables et de la réception d'origine. Test depuis schéma 0017 : ouverture 10000, collecte 5000/appliqué initial 3000 puis affectation 2000 →deltas 10000/−5000/0, crédit 0/+2000/−2000, dette finale 5000. Le test obtenait initialement zéro entrée contre trois attendues ; backfill corrigé. Ne pas dériver le crédit initial depuis le non-affecté actuel déjà régularisé. Une divergence avec une entrée existante est refusée.

Vérifications : entrée antidatée reçue après première page exclue de cet instantané, cumul exact 18446744073709551614, solde validé inchangé pour lignes en attente/refusées. HTTP réel : huit mouvements, dette/solde 1100, non affecté 0, mêmes totaux et as_of sur deux pages. Retours/avoirs, références lisibles, avances d'ouverture et protocole de sync restent requis ; les 46 stories ne sont pas entièrement couvertes.

# Backend — base en cours de construction

Django 5.2 LTS, SQLite, Django REST Framework, SimpleJWT, Unfold et drf-spectacular. Les versions effectivement installées sont verrouillées dans `requirements.txt`. Le périmètre complet est défini dans `../analysis/scope-v1.md` ; ce socle ne constitue pas encore une application livrable.

## Exécuter les vérifications locales

Depuis la racine du projet, PowerShell :

```powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
Set-Location backend
& ../.venv/Scripts/python.exe manage.py test identity catalogue stock sales customers purchasing cash backups common config.tests --settings=config.test_settings
& ../.venv/Scripts/python.exe manage.py makemigrations --check --dry-run --settings=config.test_settings
& ../.venv/Scripts/python.exe manage.py spectacular --validate --fail-on-warn --file schema.yaml --settings=config.test_settings
```

Les paramètres de test emploient des clés factices et un hash de mot de passe rapide. **Ne jamais déployer `config.test_settings`.** Les tests créent leur propre base, sans compte YemX ni requête vers son serveur.

## Configuration hors tests

Définir `APP_SECRET_KEY` et `APP_JWT_SIGNING_KEY` : deux secrets aléatoires indépendants. `APP_ALLOWED_HOSTS` reçoit la liste des hôtes autorisés séparés par virgules. `APP_DB_PATH` est facultatif (défaut `backend/db.sqlite3`). `APP_ENV` vaut `production` par défaut, avec HTTPS obligatoire et cookies sécurisés ; utiliser `development` uniquement pour un serveur local. Aucun fichier de secrets n'est créé par le projet.

Pour une première installation, utiliser `manage.py migrate` et `createsuperuser` avec `config.settings`, puis initialiser explicitement la barrière externe avec `tools/restore_maintenance.py initialize` avant le démarrage WSGI, comme décrit dans [BACKUPS.md](BACKUPS.md). Le déploiement initial sur `carnetapi.pythonanywhere.com` est réalisé : HTTPS, accès Unfold et refus des API/documentation anonymes vérifiés, connexion SMTP authentifiée. La capacité du pilote, les envois et la planification restent à qualifier. Chemins, preuves et procédure d'exploitation : [PYTHONANYWHERE.md](PYTHONANYWHERE.md). Les mises à jour d'une base existante suivent le protocole de maintenance.

## Comportements actuellement implémentés

- Comptes, boutiques et adhésions identifiés par UUID ; horodatages Django en UTC.
- `GET /api/v1/shops/` et `GET /api/v1/shops/{uuid}/` : JWT lié à une session et installation enregistrées, actives et non expirées. Boutique active appartenant au compte ou adhésion active requise, sans retrait de cette installation pour la boutique. Autorisation actuelle vérifiée avant pagination, comptage ou lecture ; un UUID externe renvoie 404.
- Pagination : 25 par défaut, paramètre `page_size`, maximum 100, ordre `created_at` puis UUID.
- `/api/v1/schema/`, `/api/v1/docs/`, `/api/v1/redoc/` : superutilisateur technique actif uniquement, via JWT ou session d'administration. Staff et propriétaire métier seuls sont refusés.
- Administration Unfold des modèles initiaux. L'administration est un outil technique ; ses formulaires ne constituent pas encore les parcours métier historisés.
- `POST /api/v1/shops/{uuid}/verify-access/` : vérification explicite des droits actuels et de cette installation pour cette boutique ; date UTC de vérification, expiration exactement 7 jours après, rôle et permissions effectives renvoyés. Ne renouvelle pas les autres boutiques. Un retrait connu n'est jamais annulé par cette route.
- `POST /api/v1/shops/{uuid}/revoke-device/` avec `installation_id`, `reason` et `operation_id` UUID : responsable pouvant retirer un appareil de vendeur dans sa boutique ; propriétaire requis pour celui d'un responsable. Journal atomique avec acteur et motif ; reprise séquentielle du même UUID/payload sans seconde écriture, UUID réutilisé avec payload différent refusé (409). Les autres boutiques et installations conservent leurs droits.
- `POST /api/v1/auth/refresh/` avec un refresh token déjà délivré à une session enregistrée : session, installation et compte actifs requis ; rotation de la chaîne, blacklist de l'ancien refresh, nouvel accès de 5 minutes, nouveau refresh et expiration serveur de 30 jours. Aucun renouvellement des permissions hors connexion ni rétablissement d'une boutique retirée. JWT sans session ou chaîne courante enregistré refusé.

## Travaux requis avant livraison

Calculs financiers préparatoires : `sales/services/pricing.py` calcule le montant d'une ligne (arrondi moitié vers le haut une seule fois), la répartition proportionnelle de remise globale après remises propres, les restes selon UUID stable et le contrôle de remise cumulée par ligne. FCFA et quantités restent entiers, sans flottants ; résultats immutables et erreurs typées. Quatorze tests de calcul passent, incluant 1728 paniers de conservation des totaux et un cas de précision au-delà des flottants. Neuf exemples partagés destinés à Django/Flutter vérifiés avec :

```powershell
& ../.venv/Scripts/python.exe -m verification.pricing_vectors
```

Ce calculateur n'enregistre aucune vente et n'autorise aucune action à lui seul. L'API devra charger les prix de référence et droits réels ; le plafond n'est pas une donnée de confiance envoyée par un vendeur. Références figées, mouvements commerciaux, encaissements/dettes et parité Flutter restent à implémenter. Suite core complète : 81 tests réussis avant l'ajout du dernier test de précision, puis 14 tests calculs réussis avec ce test inclus.

Fiches clients ajoutées : `GET/POST /api/v1/shops/{shop_uuid}/clients/`, `GET/PATCH /api/v1/shops/{shop_uuid}/clients/{client_uuid}/`, `POST .../report-correction/` et `GET .../history/`. Nom obligatoire, téléphone facultatif ; l'API reçoit un téléphone au format international `+...` et conserve null s'il est absent. Aucun OTP ou compte applicatif imposé au client. Le vendeur peut créer et signaler une correction, le responsable seul modifie et lit l'historique. Homonymes et numéros identiques conservent des UUID distincts ; fusion et avertissement de doublon restent à implémenter.

Correction : `operation_id`, `base_version`, motif et au moins un champ nom/téléphone requis. UUID inchangé, version incrémentée, valeurs avant/après historisées dans la même transaction. Une version devenue ancienne renvoie 409 sans écraser la correction validée ; reprise du même UUID/payload restitue le reçu original après recontrôle des droits actuels. Les signalements sont historisés sans modifier la fiche ; traitement/clôture et interface du responsable encore absents. Aucun effacement de fiche exposé.

État après fiches clients : **68 tests core réussis** (12 fiches clients), **5 tests de concurrence SQLite fichier réussis**, schéma OpenAPI validé sans avertissement et migrations sans divergence. Essai HTTP temporaire : vendeur crée 201 sans téléphone, responsable corrige 200 avec même UUID/version 2, historique paginé contient création et anciennes/nouvelles valeurs. Deux corrections concurrentes de version 1 : une réussite 200 et un conflit 409, une seule modification historisée. Les opérations hors connexion à reprendre après ce conflit devront rester conservées dans la future file locale ; ce mécanisme n'existe pas encore.

Conditionnements ajoutés : `GET/POST /api/v1/shops/{shop_uuid}/products/{product_uuid}/packs/`. Chaque format possède un UUID, un nom libre, `base_quantity_milli` positif et un prix de vente FCFA entier distinct du tarif unitaire. Plusieurs formats possibles pour un même produit ; aucun stock séparé ou mouvement de stock créé. Exemple : carton de 24 bouteilles = 24000 milli-unités, prix du carton 4800 FCFA indépendant de 24 × 250 FCFA. Facteur fractionnaire refusé pour les pièces, accepté jusqu'au millième pour poids/volume.

Création réservée au responsable, lecture autorisée aux membres actuels, produit et boutique vérifiés ensemble. Transaction avec reçu et journal, reprises séquentielles/simultanées sans doublons ; UUID d'opération réutilisé pour un autre produit refusé. Produit archivé : nouvelle création refusée, reçu d'une création déjà accomplie restitué si les droits sont toujours valides. Aucun endpoint de modification/désactivation de conditionnement à ce stade. Les facteurs et prix à figer dans les ventes/achats historiques seront implémentés avec ces modules.

État après conditionnements : **56 tests core** et **4 tests de concurrence SQLite fichier réussis**. Schéma OpenAPI validé sans avertissement, migrations sans divergence. Essai HTTP local étendu : création de sachet 201, reprise 200, liste vendeur 200/prix 1100 FCFA ; stock produit 1250 inchangé. Même commande `python -m verification.catalogue_smoke` ci-dessous. Ces vérifications ne démontrent pas encore une vente en conditionnement ni une réception fournisseur.

Catalogue initial ajouté : `GET/POST /api/v1/shops/{uuid}/products/` et `GET /api/v1/shops/{uuid}/products/{uuid}/`. Liste filtrée par boutique avant pagination. Création réservée au responsable, UUID produit et opération fournis par le client, journal et ouverture de stock dans une transaction. Même demande répétée : reçu original (200) ; UUID réutilisé avec demande différente : 409. Droits actuels vérifiés avant reprise.

Prix catalogue : FCFA entiers sans flottants. Quantités : milli-unités entières (1 pièce = 1000 ; 1,250 kg = 1250), pièces obligatoirement entières. Stock dérivé des mouvements ; ouverture facultative avec date passée/présente et motif. Coût connu ou explicitement inconnu, sans déduction depuis le prix d'achat catalogue. Coût initial stocké en millionièmes de FCFA entiers (`unit_cost_micros`), six décimales exactes ; l'API reçoit `unit_cost_fcfa` comme chaîne décimale ou entier, valeurs hors capacité SQLite refusées. Prix d'achat omis des réponses aux vendeurs sans permission et différé dans la requête SQL. Aucun endpoint de lecture des coûts des mouvements ou du journal encore exposé.

État actuel de vérification : **45 tests core réussis**, dont 13 catalogue ; **3 tests de concurrence** sur SQLite fichier réussis, dont création simultanée avec un seul produit et mouvement d'ouverture. Schéma OpenAPI validé sans avertissement et migrations sans divergence. Essai HTTP local avec base temporaire indépendante : création 201, reprise 200, stock 1250 inchangé, coût `600.123456` conservé, coût absent de la liste du vendeur. Pour reproduire depuis backend :

```powershell
& ../.venv/Scripts/python.exe -m verification.catalogue_smoke
```

Cet essai crée uniquement des comptes synthétiques dans une base temporaire, puis ferme le serveur et supprime cette base. Aucun compte de production ou YemX utilisé. Modification/archivage du catalogue, conditionnements, photos, autres mouvements de stock, coûts moyens et synchronisation restent à réaliser.

Inscription et émission initiale de jetons après OTP, fournisseur SMS gratuit et quota ; création sécurisée des installations et sessions, liste des appareils pour le responsable, validation après restauration ; enforcement sur Android des autorisations hors connexion de 7 jours ; API métier avec motif et journal ; configuration des permissions et retrait confirmé de leurs dépendances ; modèles ventes/stock/crédits/achats, calculs monétaires et coûts ; synchronisation idempotente, traitement des opérations retirées/refusées, restauration ; photos, rapports, imports/exports, sauvegardes et emails ; application Flutter complète français/anglais ; migrations et dimensionnement sur PythonAnywhere.

Les JWT utilisés dans les tests sont générés uniquement par les fixtures avec une claim `session_id` correspondant à leur compte. Aucun endpoint public de connexion ou de création de compte n'est fourni à ce stade, et aucun PIN/SMS factice ne remplace l'authentification de production. La révocation de session/installation globale est vérifiée dans l'authentification, mais n'a pas encore de parcours API ou UI de gestion.

Les tests couvrent accès et pagination, sessions manquantes/étrangères/retirées/expirées, droits de documentation, expiration à 7 jours, indépendance des boutiques/appareils, permissions effectives, limites vendeur/responsable/propriétaire et reprise du retrait par UUID. Les opérations commerciales hors connexion en attente ne sont pas encore implémentées et ne sont donc ni effacées ni régularisées par ce code.

Suite étendue à **32 tests** : rotation de refresh, durées effectives des jetons, chaîne liée à la même session, refus du refresh déjà consommé, comptes/installations/sessions retirés, expiration serveur, claims de session invalides/étrangères, refus d'un accès utilisé comme refresh, absence de prolongation du délai hors connexion ou de restauration des droits de boutique.

Deux vérifications complémentaires utilisent une vraie SQLite sur disque et deux connexions simultanées : un seul succès pour la même rotation, deux reçus identiques et une seule action pour le même retrait. Commande dédiée depuis backend :

```powershell
& ../.venv/Scripts/python.exe manage.py test verification.concurrency_checks --settings=config.concurrency_test_settings
```

Cette configuration crée puis supprime uniquement `backend/.concurrency-tests.sqlite3`. Le mode de transaction SQLite `IMMEDIATE` réserve le verrou d'écriture au début des blocs atomiques ; les rotations et leurs écritures de blacklist/chaîne sont transactionnelles. Ces deux cas passent localement : ils ne démontrent pas 30 appareils simultanés sur le stockage PythonAnywhere. Restauration et événements métier concurrents restent à vérifier.

Swagger UI/ReDoc utilisent actuellement les ressources CDN par défaut du package. Leur accès et le fonctionnement de la session d'administration doivent être testés dans un navigateur avant livraison. La validation du schéma ne démontre pas cette intégration navigateur.

Mesure locale : `& ./.venv/Scripts/python.exe tools/measure_backend.py` depuis la racine. Ce relevé Windows ne prouve pas le budget complet de 500 Mo du serveur cible.



### Langues du catalogue et préférence de compte — 18 octobre 2026

`Accept-Language: fr` ou `en` sélectionne les messages de la requête. Les codes machine et valeurs métier (`piece`, `manager`, etc.) restent stables ; les noms saisis ne sont pas traduits. Les erreurs simples exposent maintenant `code` dans le JSON rendu, en plus de `detail`. La couverture des traductions reste partielle : les erreurs catalogue exercées et plusieurs libellés métier sont traduits, pas encore tous les messages et champs d'administration.

`GET/PATCH /api/v1/account/language/` persiste uniquement le choix du compte authentifié. PATCH reçoit `operation_id`, `base_version` et `language` (`fr` ou `en`) ; le reçu contient `user_id`, `language` et `version`. Les reprises identiques rendent le reçu original ; contenu différent ou version périmée : 409. Aucun droit ou bail de boutique renouvelé. Le client doit encore relier son choix local et ses en-têtes à cette préférence ; aucun parcours hors connexion ou PDF n'est affirmé ici. Migration 0053, reçus indépendants de l'activité des boutiques.

Les sources de traduction UTF-8 sont `core/locale/{fr,en}/LC_MESSAGES/django.json`. Elles produisent des fichiers GNU `django.mo` standards, chargés par Django. Le compilateur utilise la bibliothèque standard Python ; ne pas éditer les `.mo` à la main. Depuis la racine du dépôt :

```powershell
./.venv/Scripts/python.exe tools/compile_messages.py
./.venv/Scripts/python.exe tools/compile_messages.py --check
./.venv/Scripts/python.exe -m pytest
```

`--check` refuse les fichiers compilés absents ou périmés sans les réécrire. Les sources acceptent aussi les formes gettext de pluriel et de contexte. Après changement des sources, recompiler avant les tests et redémarrer les processus Django persistants qui ont chargé les anciens catalogues.

Dernière observation : 348 tests et 203 sous-tests réussis ; deux contrôles de concurrence de langue réussis sur SQLite fichier (45 autres désélectionnés). Schéma OpenAPI validé sans avertissement en français et anglais, choix de rôles partagé avec le modèle. Le parcours HTTP `verification.stock_alert_smoke` vérifie aussi préférence/reprise/conflit en anglais et validation du seuil, puis photo, codes, vente et alertes. Aucun résultat de charge cible ou de livraison Android n'en est déduit. Le registre détaillé des preuves et limites reste `analysis/backend-tdd-progress.md`.


### Modifications des conditionnements — 18 octobre 2026

`GET/PATCH /api/v1/shops/{shop_id}/products/{product_id}/packs/{pack_id}/` permet de lire ou corriger un format existant. PATCH exige un responsable actuel, `operation_id`, `base_version` du conditionnement et `reason`. Champs modifiables : `name`, `base_quantity_milli`, `sale_price_fcfa`, `is_active`. Les champs de rattachement et les identifiants restent immuables. Facteur positif entier en milli-unités ; pièces entières, poids/volume au millième ; prix FCFA entier. Migrations 0054 et 0055 imposent version positive entière et stockage entier du facteur/prix dans SQLite.

Les réponses et la liste comprennent `version` du format et `product_version`. Une correction avance les deux dans une transaction, sans mouvement de stock ni modification du prix unitaire du produit. Les ventes vérifient déjà la version du produit : un ancien panier reçoit 409 au lieu d'acquérir silencieusement un facteur/prix différent. Le client doit encore conserver les opérations hors connexion et appliquer le protocole de conflit retenu ; un 409 seul n'implémente pas ce protocole.

Une reprise identique conserve son reçu original, même après une correction ultérieure. Le code-barres et sa version indépendante sont préservés ; le scan retourne les nouveaux facteur/prix/version et ne retrouve plus un format archivé. `GET .../packs/{pack_id}/history/` expose au responsable la création et les corrections avec motif et valeurs avant/après ; ordre stable et pagination SQL. Les données des ventes et factures existantes restent figées, y compris les réceptions à venir fondées sur une ancienne facture. Le prix d'achat saisi sur facture reste distinct du prix de vente du conditionnement.

Tests ciblés : `./.venv/Scripts/python.exe -m pytest backend/catalogue/tests/test_pack_changes.py` depuis la racine. Le parcours HTTP `verification.stock_alert_smoke` inclut maintenant vente d'un carton de 24, correction à 12, conservation du reçu ancien, rejet d'un panier périmé, nouvelle vente au bon facteur, historique et archivage. Aucun client Flutter ou protocole de synchronisation n'est livré par ces seules routes.


Résultat après cette tranche : **362 tests et 216 sous-tests réussis** dans la suite principale ; **49 contrôles de concurrence SQLite fichier réussis** sans désélection (274,27 s). Django check, migrations sans divergence, schéma OpenAPI sans avertissement, catalogues de traduction à jour et pip check réussis. Parcours HTTP complet réexécuté avec migration 0055 : réussi. L'objectif global reste actif, avec notamment synchronisation/conflits, Android et qualification représentative encore à traiter.


### Import CSV du catalogue — 18 octobre 2026

Tranche backend de US-40 pour les produits ; les imports clients, fournisseurs et leurs soldes restent à réaliser. Routes sous `/api/v1/shops/{shop_id}/catalogue-imports/`, réservées au responsable actuel avec session/installation actives :

- `GET template/` : CSV UTF-8 avec BOM, séparateur virgule, en-têtes stables ; contient une ligne d'exemple à remplacer par les données réelles. Téléchargement `Accept: text/csv`, messages français/anglais.
- `POST` multipart : `id` UUID choisi par le client et `file`. Persiste la source et son SHA-256, puis retourne prévisualisation et erreurs par ligne. Aucun produit ou mouvement créé à cette étape. Reprise du même UUID/source : 200 ; autre source ou boutique/auteur pour cet UUID : 409.
- `GET {id}/` : retrouve la source prévisualisée ; pour un fichier non appliqué, les catégories et codes sont revérifiés dans la boutique actuelle.
- `POST {id}/confirm/` JSON : `operation_id`, `source_sha256` de la prévisualisation et `reason`. Revalide les lignes et droits dans la transaction d'application. Toute erreur bloque le fichier entier (409 avec nouvelle prévisualisation). Reprise identique : reçu original ; autre confirmation d'un fichier déjà appliqué : 409.

Colonnes requises : `name`, `unit_kind` (`piece`, `weight`, `volume`), `unit_label`, `sale_price_fcfa`. Colonnes facultatives : `purchase_price_fcfa`, `stock_threshold_milli`, `category_id`, `barcode`, `opening_quantity_milli`, `opening_unit_cost_fcfa`, `opening_occurred_at`, `opening_reason`. En-têtes inconnus/dupliqués refusés. Catégorie existante de la même boutique ; aucun rattachement silencieux par nom. Codes conservés avec zéros initiaux, doublons du fichier ou de la boutique signalés. Photo et conditionnements se gèrent par leurs routes dédiées dans cette tranche.

Prix FCFA entiers sans séparateurs ni notation exponentielle. Quantités/seuils en milli-unités : 1000 = une pièce ou une unité de poids/volume ; pièces entières. Champ facultatif vide = absent/inconnu, sans coût zéro inventé. L'ouverture exige quantité/date effective/motif ; coût vide accepté comme inconnu, sinon décimal exact avec au plus six décimales. Exemple de date : `2026-01-01T10:00:00Z`. Les mêmes validations métier que la saisie manuelle s'appliquent.

Limites techniques par fichier : 1000000 octets et 1000 lignes de données, sans quota commercial de produits ou de fichiers. La réception multipart arrête les morceaux avant de dépasser la borne dans les fichiers temporaires Django ; un test observe les écritures réelles du handler temporaire et leur nettoyage. Cela ne mesure pas le buffering éventuel du frontal PythonAnywhere. Le contrôle de réception est actuellement appliqué à l'import CSV ; les autres uploads restent à qualifier avec la même discipline.

Une application réussie conserve UUID stables de lignes, produits et opérations, journal du fichier et des créations, origine de la confirmation, stock initial et franchissements éventuels d'alertes. Elle ne crée ni dette fournisseur ni cash. Une collision sur une ligne tardive annule aussi les premières créations et leurs événements d'alerte. Une réaffectation ultérieure de code ne rend pas invalide l'historique d'un import déjà appliqué.

Correction associée : les coûts d'ouverture dans la prévisualisation et la comparaison d'idempotence sont des chaînes décimales exactes. Pour les anciens audits contenant un flottant, la comparaison récupère le coût entier en millionièmes du mouvement d'ouverture, sans réécrire cet audit. Deux coûts proches de la borne SQLite, différant d'un millionième, restent des demandes distinctes (409 pour un UUID réutilisé).

Dernière suite principale observée : **380 tests et 223 sous-tests réussis** ; fichier import : 17 tests et 7 sous-tests. Django check, fraîcheur migrations, OpenAPI et fraîcheur des traductions réussis. Migration 0056. Parcours HTTP `verification.stock_alert_smoke` réussi, incluant modèle CSV en anglais, upload de 2 Mo refusé, prévisualisation sans création, application de deux produits et ouvertures, coût exact, reprise et fichier invalide entièrement refusé. Ce travail ne constitue pas une livraison de tous les imports US-40, du client Android ou du protocole hors connexion.


### Imports clients/fournisseurs et dettes d'ouverture — 18 octobre 2026

Nouvelles routes `/api/v1/shops/{shop_id}/customer-imports/` et `/api/v1/shops/{shop_id}/supplier-imports/`, avec les mêmes actions `POST`, `GET template/`, `GET {id}/`, `POST {id}/confirm/` que l'import produit. Responsables actuels uniquement, prévisualisation sans écritures métier et confirmation atomique avec empreinte/reprise UUID. Le modèle persistant conserve maintenant `kind` (`products`, `customers`, `suppliers`), validé en base par migration 0057. Un fichier ne peut pas être réinterprété par la route d'un autre type, même si ses octets et son UUID sont identiques.

Colonne requise : `name`. Facultatives : `phone`, `opening_debt_fcfa`, `opening_occurred_at`, `opening_reason` ; `due_date` également pour les clients. Un téléphone vide reste absent ; mêmes noms ou téléphones créent des fiches distinctes, sans rapprochement automatique et sans compte utilisateur/OTP. Les modèles contiennent un exemple à remplacer, avec noms de téléchargement distincts et exemple français/anglais.

`opening_debt_fcfa` est un entier FCFA positif pour une dette ; vide ou zéro sans autres champs d'ouverture ne crée pas de dette. Une dette non nulle exige date effective passée/présente et motif, avec échéance client facultative. Les autres champs d'ouverture sans dette positive sont signalés. Un montant négatif n'est jamais converti implicitement en avance ou avoir. Les imports d'avances/avoirs et obligations d'ouverture restent à ajouter ; cette tranche couvre les dettes positives uniquement.

La confirmation réutilise les services de saisie manuelle : fiche puis dette d'ouverture distincte, UUID stables, journal et relevé client. Aucun chiffre d'affaires, facture fournisseur, réception ou paiement fictif. Les reçus comprennent `customer_ids` ou `supplier_ids` et `opening_debt_ids`. Une erreur de ligne ou collision tardive annule tout le fichier, y compris dettes et entrées de relevé déjà préparées dans la transaction.

Le décodage CSV borné est partagé avec les produits (`common/services/csv_sources.py`) ; leurs contrôles métier et reçus restent spécifiques. Limites de réception et de lignes inchangées. La limite pendant réception est également branchée sur les photos : source <=10000000 octets, avant écriture excessive des fichiers temporaires Django ; photo finale JPEG <=20000 octets inchangée. Les buffers du frontal hébergeur et la charge pilote restent à mesurer.

Vérifications observées : **390 tests et 230 sous-tests réussis**. Deux contrôles de concurrence SQLite fichier des imports tiers réussis (51 autres désélectionnés), puis deux contrôles photo réussis après branchement de réception (51 autres désélectionnés). Ce ne sont pas 53 contrôles exécutés ensemble. Django check, migrations sans divergence, OpenAPI, traductions et pip check réussis. HTTP loopback final : deux clients homonymes, dette client de 3000, dette fournisseur de 7000, reprise sans duplication, route de mauvais type 404, vendeur 403 et aucun cash/facture créés par les ouvertures ; le même parcours vérifie toujours catalogue, photo, ventes et alertes.


### Avances et avoirs fournisseurs d'ouverture — Q171, 18 octobre 2026

`GET/POST /api/v1/shops/{shop_id}/suppliers/{supplier_id}/opening-balances/` reprend un droit fournisseur antérieur. POST exige `id`, `operation_id`, `balance_kind` (`advance` ou `credit`), `amount_fcfa` positif entier, `occurred_at` passé/présent et `reason`. Écriture et lecture réservées au responsable actuel, fournisseur de la même boutique, session/installation actives. Reprise identique : reçu original ; autre contenu sous le même UUID : 409.

La migration 0058 étend SupplierCredit avec une origine explicite : `return_credit` pour les avoirs sur retour existants, `opening_advance` ou `opening_credit` pour les reprises. Une ouverture possède boutique/fournisseur directs et aucun retour fictif ; `reference_fcfa` vaut null, car il n'existe pas de référence de marchandises retournées. Aucun montant zéro substitué à une référence absente. La contrainte d'origine impose ces variantes, et les contraintes de monnaie entière restent appliquées. Les anciens avoirs sur retour conservent leur origine et leur référence.

Les listes et détails de crédits, affectations, remboursements et historiques prennent en charge les deux origines. Une dette d'ouverture de 7000, une avance de 30000 et un avoir de 5000 restent trois soldes distincts, sans compensation automatique. Une affectation explicite de 3000 à la dette laisse 4000 dus et 27000 d'avance, sans cash. Un remboursement réellement reçu de 2000 consomme l'avoir, laisse 3000 disponibles et crée une seule entrée de caisse de 2000 au moyen déclaré. Aucun SupplierPayment, achat, réception ou mouvement de stock n'est fabriqué à l'ouverture.

Le CSV fournisseurs accepte maintenant `opening_advance_fcfa` et `opening_credit_fcfa` en plus de la dette, avec date/motif d'ouverture communs sur la ligne. Les montants sont séparés et non négatifs : vide ou zéro sans ouverture ne crée aucun droit ; chaque montant non nul est validé avant application. Le reçu ajoute `opening_balance_ids`. Une collision dans la dernière ouverture annule également fournisseur, dette et avance préparés dans le même fichier. Les colonnes clients restent conformes à Q170 (dette d'ouverture), sans nouvelle reprise de crédit client déduite de Q171.

Un UUID de confirmation égal à l'un des UUID d'opérations de ses propres lignes est refusé 409 avec rollback, au lieu de produire une erreur SQL serveur. Le contrôle porte sur le journal d'opérations sous la transaction, pas sur une fusion approximative de contenus.

Vérifications observées : 400 tests et 243 sous-tests réussis, contrôles Django/migrations/OpenAPI réussis ; parcours HTTP complet réussi. Deux contrôles de concurrence ciblés des ouvertures ont réussi : reprise 201/200 avec un seul droit ; deux affectations de 20000 sur la même avance de 30000 donnent 201/400, disponible 10000 et une seule réduction de dette. Puis les 55 contrôles de concurrence SQLite fichier ont tous réussi en 268,18 s, sans désélection. Un test de migration 0057 vers 0058 sur un avoir antérieur et son remboursement a également réussi, en conservant identités, montants, liens et reçu historique. Les autres User Stories, synchronisation, interfaces Unfold/Android et qualification des budgets restent dans l'objectif actif.


La matrice initiale des US et de leurs preuves/manques est dans `analysis/backend-us-coverage.md`. Elle est une aide à la poursuite et à l'audit, sans déclaration de livraison complète.


Après ajout du test de migration historique : suite principale finale **401 tests et 243 sous-tests réussis** (52,22 s), avec les **55 contrôles de concurrence** déjà réussis sur le même code métier. Aucun autre changement de code métier après ces vérifications.


### Relevé fournisseur chronologique — Q177, 18 octobre 2026

`GET /api/v1/shops/{shop_id}/suppliers/{supplier_id}/statement/` retourne le journal du fournisseur de la boutique autorisée, sous droits actuels de responsable/session. Filtres `date_from`, `date_to` inclusifs dans le fuseau de boutique, `as_of` UTC et pagination bornée. L'arrêté est renvoyé et repris dans les liens des pages suivantes. Un événement reçu plus tard reste exclu de l'ancien arrêté, même si sa date effective est passée. `provisional=true` reste explicite faute de checkpoints prouvant la complétude des appareils.

Le relevé distingue dette, avance et avoir disponibles : `closing_debt_fcfa`, `closing_advance_fcfa`, `closing_credit_fcfa`. Le solde net `closing_balance_fcfa` = dette − avance − avoir ; il ne compense pas ces soldes et ne représente pas la caisse. Chaque ligne expose UUID de référence, référence externe si disponible, opération, auteur, motif, dates effective/enregistrement, état et deltas des trois composantes. Les affectations et composantes des paiements de la page sont jointes ; plusieurs moyens ne font pas compter l'argent deux fois.

Factures/dettes d'ouverture augmentent la dette. Un paiement réel crée une avance et baisse le net ; son affectation réduit dette et avance sans nouvel effet net ou cash. Les droits d'ouverture gardent leur nature avance/avoir. Un départ fournisseur est tracé sans effet financier ; l'avoir accepté et le remboursement réellement reçu sont des événements distincts. Les états en attente de la période ont leurs compteurs et deltas propres, et un refus reste visible sans entrer dans les totaux validés. La production effective de ces états par la synchronisation reste à implémenter.

Le journal `SupplierLedgerEntry` est appendu dans les transactions sources (migrations 0059–0061). Reprises et rollbacks ne créent pas de second effet. La reprise initiale des événements existants utilise leurs montants d'origine, jamais leurs restants déjà consommés ; elle conserve dates et UUID déterministes et refuse une projection existante incohérente. Les affectations historiques sont lues par groupes successifs. Les totaux sont calculés en entiers Python sur tout le filtre avec lecture par morceaux, et seules les lignes de la page sont chargées avec leurs détails. Aucun flottant ou SUM SQLite débordant n'est utilisé pour ces totaux.

La contrainte d'égalité des deltas utilise la fonction SQLite `integer_balance_matches`, enregistrée sur chaque connexion Django, afin de ne pas perdre un franc par débordement d'un calcul SQL intermédiaire. Les outils externes qui évaluent les contraintes de cette table devront charger le même environnement Django/fonctions ; procédure de restauration console encore à qualifier.

Vérifications observées : 412 tests et 245 sous-tests dans la suite principale, contrôle Django/migrations/OpenAPI et HTTP loopback réussis. Deux tests de migration reprennent ouvertures/affectation/remboursement puis facture/paiement/retour/avoir/remboursement ; réexécution du premier backfill sans doublon. Les totaux supérieurs à int64, frontières de jour Douala, événements tardifs, pagination SQL et refus d'accès sont exercés. Cela ne prouve pas le traitement Dart des valeurs extrêmes, les PDF, la charge pilote ou la synchronisation ; ces exigences restent ouvertes.


Vérification complémentaire finale du relevé : les **56 contrôles de concurrence SQLite fichier ont tous réussi**, sans désélection (298,78 s). La suite principale reste **412 tests et 245 sous-tests réussis** ; aucun changement de code métier après ces vérifications.


### Vente oubliée (US-35, partielle)

POST shops/{shop}/sales/ accepte occurred_at et reason ensemble, uniquement pour le responsable/propriétaire. Motif non vide, date future refusée. La date effective UTC porte les effets stock, caisse et client ; created_at reste la saisie serveur. Sans ces champs, le contrat et les reprises existants restent compatibles. Le prix correspond à la version catalogue confirmée fournie ; aucun tarif passé n'est deviné. La date d'origine d'une future opération vendeur synchronisée relève du protocole de synchronisation encore absent.

GET shops/{shop}/sales/{sale}/history/ expose le journal paginé au responsable. Reprise identique sans double écriture ; date différente avec même UUID donne 409 ; vendeur ou ancien responsable rétrogradé ne peut pas dater manuellement. Les comptages conservent leurs physiques et révisent leurs attendus pour une vente passée reçue tardivement. L'agrégat de stock utilisé à la vente est exact même au-delà d'int64 pour la somme.

Suite principale : 421 tests, 249 sous-tests réussis ; contrôles Django, migrations, OpenAPI et fraîcheur traductions réussis. US-35 et l'objectif global restent incomplets.


Vérifications complémentaires observées : HTTP loopback sur SQLite temporaire migrée réussi, vente datée créée 201, reprise 200 identique, vendeur 403, journal responsable 200/vendeur 403. Les contrôles existants catalogue, codes-barres, photos, seuil/alertes et imports/relevé fournisseur passent dans le même parcours. Six tests de concurrence SQLite fichier portant sur les ventes ont réussi (51 désélectionnés, 79,23 s), dont la nouvelle reprise simultanée de vente datée : une seule vente, un seul mouvement stock et un seul paiement de caisse. Le lot complet de 57 contrôles concurrents n'a pas été relancé dans cette tranche ; le précédent lot complet de 56 était réussi avant ces changements. Aucun déploiement ni donnée de production modifiée.


### Limites de requêtes JSON (US-41)

Les routes DRF utilisent BoundedJSONParser, y compris les imports qui déclarent leurs parseurs explicitement. Maximum technique : 1000000 octets par corps JSON, 64 niveaux de conteneurs. Une commande trop volumineuse reçoit 413 avec code json_body_too_large et message français/anglais ; elle n'est ni tronquée ni appliquée partiellement. L'imbrication excessive reçoit 400 parse_error. Les lectures courtes sont poursuivies jusqu'à EOF ; au plus limite+1 octets sont lus par ce parseur avant rejet, indépendamment de la longueur annoncée.

La limite est en octets de l'encodage envoyé, avant décodage JSON. Les encodages JSON déjà acceptés par DRF restent acceptés. Les limites de fichiers multipart photo/CSV restent distinctes. Le buffering en amont du serveur, la mémoire totale du processus, HTTPS réel et la charge PythonAnywhere ne sont pas prouvés par ces contrôles locaux.


Résultats finaux de cette tranche : suite principale 430 tests et 249 sous-tests réussis en 72,31 s ; contrôles Django, fraîcheur migrations, OpenAPI --validate --fail-on-warn et compile_messages.py --check réussis. HTTP loopback sur SQLite temporaire migrée réussi : JSON excessif 413 avec code et message anglais attendus, puis préférences/imports/photos/codes-barres/ventes/alertes et journal de vente datée toujours fonctionnels. Aucun test concurrent supplémentaire relancé pour ce changement de parseur ; les résultats de concurrence antérieurs restent datés séparément. US-41 et l'objectif global restent actifs et incomplets.


### Retours clients et réceptions (US-14/26, partiels)

GET/POST shops/{shop}/sales/{sale}/returns/ et GET {return}/ : réservés au responsable/propriétaire. Les lignes contiennent id, sale_line_id, quantity_milli en unité de base ; commande avec id, operation_id, occurred_at et reason. Montant calculé au net facturé d'origine, par différence de cumuls arrondis au plus proche/moitié vers le haut (décision titulaire : 33/34/33 pour 100 FCFA sur 3 pièces). Historique conservé, quantité cumulée bornée, reprises idempotentes. Dette de la vente réduite d'abord ; credit_fcfa conserve le reste à régler séparément, y compris sans fiche client. Aucun paiement historique n'est effacé ni remboursement créé. Les crédits issus de retour disposent des routes de règlement dédiées décrites ci-dessous ; ils restent distincts des crédits de monnaie due CustomerCredit.

GET/POST {return}/lines/{line}/receipts/ et GET {receipt}/ : réception physique distincte avec quantity_milli, condition sellable/unsellable, identifiants/date/motif. Seule la réception vendable ajoute un mouvement customer_return ; non-vendable reste hors du disponible. Coût de vente d'origine conservé, null reste inconnu. Réceptions partielles cumulées bornées, reprises sans double stock ; produit archivé peut être reçu sans devenir vendable au catalogue. Les réceptions vendables participent à l'observation des franchissements de seuil. Les décisions ultérieures sur les non-vendables, l'annulation, les remboursements réels, les PDF et la synchronisation restent à réaliser.

Migrations 0062–0064 et tests test_sale_return_models.py, test_return_pricing.py, test_sale_returns.py, test_return_receipts.py ; deux contrôles concurrents ciblés SQLite fichier et HTTP loopback réussis. Les calculs purs sont exacts au-delà des intermédiaires int64 ; cela n'est pas une preuve de compatibilité Dart ni de coût moyen pondéré mobile.


Résultat final observé après correction OpenAPI et ajout du contrôle de franchissement US-15 : 447 tests et 269 sous-tests réussis (75,90 s). Django check sans problème, makemigrations --check --dry-run sans changement, OpenAPI --validate --fail-on-warn et fraîcheur gettext réussis. La première suite verte à 446/269 précédait le dernier test d'intégration des alertes ; le résultat 447/269 couvre ce test. Aucun contrôle de livraison Android, de charge réelle PythonAnywhere ou de restauration n'en est déduit. Objectif global conservé actif.


### Règlements explicites des crédits de retour

GET/POST shops/{shop}/sales/{sale}/returns/{return}/refunds/ et allocations/, GET {decision}/ : responsable/propriétaire seulement, droits et session actuels vérifiés sous verrou de transaction SQLite. Remboursement : id, operation_id, occurred_at, reason, amount_fcfa, method, other_name si other, external_reference facultative. Affectation : identifiants/date/motif et allocations [{id, debt_id, amount_fcfa}], uniquement aux dettes du client d'origine dans la même boutique. Un retour anonyme peut être remboursé sans création de client ; aucune association à un tiers n'est inventée pour une affectation.

Le disponible est crédit initial du retour moins tous ses règlements. Concurrence remboursement/affectation consomme le même disponible ; dépassement 409. Une affectation ne crée aucun cash. Chaque remboursement réellement déclaré crée une sortie de caisse, avec moyen et référence conservés, et reste distinct de la vente et de la réception physique. Reprises par UUID sans second effet ; date antérieure au retour (ou à la dette ciblée pour une affectation) et motif vide refusés. Modèles ReturnCreditSettlement/Allocation et source CashMovement.sale_return_refund : migration 0065. Le motif de caisse peut conserver le motif complet de 2000 caractères : TextField, migration 0066, test de full_clean après persistance.

GET du retour expose remaining_credit_fcfa actuel connu du serveur ; une réponse d'opération rejouée conserve son solde d'origine. GET shops/{shop}/clients/{client}/return-credits/ donne montant initial et restant, sous permission de consultation de dettes, sans journal privé. Le relevé client trace les affectations et remboursements ; caisse et comptages incluent les remboursements par moyen. Les crédits de retour et les autres obligations monétaires gardent leurs origines et routes distinctes, sans compensation automatique.

Preuves : tests de modèle/contraintes, API et migration test_return_credit_*.py, deux contrôles concurrents ciblés et parcours HTTP avec affectation puis remboursement. Annulation, états hors connexion, documents et décisions sur non-vendables restent incomplets.


Résultat final sur le code et schéma courants : 459 tests et 285 sous-tests réussis en 100,57 s. Contrôles Django, absence de migration manquante, OpenAPI --validate --fail-on-warn et fraîcheur gettext réussis. HTTP loopback relancé après migrations 0065–0066 et contrôle des dates : parcours complet réussi, remboursement anonyme 100 (restant 150), affectation client 100 sans cash puis remboursement 150, dette/solde 2900 et crédit zéro. Les résultats des deux contrôles de concurrence ciblés sont ceux consignés plus haut ; le lot complet de 61 n'est pas revendiqué. Aucun déploiement ou accès aux données de production. Objectif global maintenu actif.


### Stock non vendable et décisions explicites (US-14/15, partiels)

GET shops/{shop}/unsellable-stock/ liste les réceptions non vendables encore disponibles pour décision, sous droits de responsable/propriétaire ; filtre product_id et include_closed=true pour historique. Pagination en SQLite après autorisation, ordre date/id ; quantités exactes en milliunités, coût nullable/provisoire conservé. Le restant actuel est aussi visible dans GET de la réception. Les reçus rejoués gardent leur instantané d'origine.

GET/POST .../sales/{sale}/returns/{return}/lines/{line}/receipts/{receipt}/decisions/ et GET {decision}/ : id, operation_id, occurred_at, reason, quantity_milli et kind. restock exige is_sellable_confirmed=true et ajoute une seule fois au disponible, au coût d'origine de la réception. supplier_return exige supplier_id dans la boutique ; scrap n'exige aucun tiers. Ces deux derniers choix ne déduisent aucun disponible et ne créent ni caisse, ni seconde perte, ni dette ou avoir fournisseur automatique. Le départ est tracé dans le relevé fournisseur avec delta financier nul. Le rattachement d'un avoir accepté à cette nouvelle origine reste une opération financière séparée à raccorder.

Toutes les décisions partagent le même restant physique, sous transaction SQLite et permissions actuelles ; dépassement 409, changement d'une opération existante 409, date antérieure à la réception 400. Quantités à la pièce entières, poids/volume au millième. Source déjà vendable et origine étrangère refusées. La remise en vente participe à l'observation des franchissements de seuil ; elle ne réactive pas automatiquement un produit archivé.

Modèle NonSellableDecision, migrations 0067–0068 ; tests test_unsellable_decision_models.py et test_unsellable_decisions.py. Deux contrôles concurrents ciblés et HTTP réel loopback réussis. Le rapprochement avec les anciennes corrections/pertes, la valorisation, les files hors connexion et les écrans Android restent à qualifier/implémenter ; coût inconnu ne vaut pas zéro.


Résultat final sur le schéma courant : 468 tests et 304 sous-tests réussis en 95,49 s. Django check sans problème, makemigrations --check --dry-run sans changement, OpenAPI --validate --fail-on-warn et fraîcheur gettext réussis. Les contrôles de schéma en français et anglais passent dans la suite. Les deux contrôles concurrents ciblés et HTTP final sont consignés ci-dessus ; le lot complet de 63 n'est pas revendiqué. Objectif global maintenu actif.


### Annulation conservant la vente (US-26, partielle)

GET/POST shops/{shop}/sales/{sale}/cancellation/ : id, operation_id, occurred_at et reason ; responsable/propriétaire et droits actuels contrôlés en transaction. Modèle SaleCancellation, migration 0069. L'annulation conserve la vente, ses lignes et ses paiements. Elle crée un retour commercial uniquement pour les quantités non déjà couvertes par des retours validés ; s'il n'y en a plus, aucun second retour n'est créé. Dette d'origine réduite d'abord, crédit séparé pour le reste. Aucun stock ou cash automatique ; réception physique et remboursement réel restent des événements explicitement enregistrés.

GET de la vente expose state=cancelled et cancellation avec UUID/dates, en conservant UUID, montants et lignes facturés d'origine. Journal privé et motif via GET cancellation et history/, réservés au responsable. La lecture vendeur autorisée montre l'état sans ce motif. Une reprise de l'opération de création ou d'annulation garde le reçu original de cette opération ; consulter GET pour l'état courant et les futurs documents.

Une seule annulation par vente, reprises sans double effet, UUID enfants stables et rollback intégral sur collision. Date avant vente refusée ; date avant un retour commercial déjà enregistré donne 409 cancellation_timeline_conflict pour contrôle explicite de chronologie. Le traitement durable des conflits reçus hors connexion reste à réaliser. Les lignes d'un carton utilisent toujours les facteurs et nets facturés, même après changement de catalogue ou archivage.

Preuves : test_sale_cancellation_models.py, test_sale_cancellations.py et deux contrôles concurrents SQLite ciblés. Les PDF avec référence lisible et mention d'annulation/remboursement, la synchronisation et la restauration restent incomplets.


Résultat final observé : suite principale 479 tests et 307 sous-tests réussis (98,13 s), contrôle Django sans problème, fraîcheur migrations, OpenAPI --validate --fail-on-warn et gettext réussis. HTTP réel loopback sur SQLite temporaire migrée réussi : vente 500, retour préalable 250, annulation du reliquat 250, aucun cash/stock automatique ; lecture vendeur état cancelled sans motif privé, reprise de création gardant son reçu initial. Remboursement réel ultérieur 250 puis réception vendable 1000 ajoutent uniquement leurs effets propres ; journal d'annulation présent. Les anciens parcours catalogue/alertes/imports/photos/règlements/non-vendables restent réussis. Aucun déploiement ni accès aux données de production. Objectif global maintenu actif.

### Socle de valorisation US-33 — 18 octobre 2026

`stock/services/moving_average_cost.py` calcule un coût moyen sur une séquence ordonnée : montants internes en nano-FCFA entiers, ratios exacts, coût inconnu distinct de zéro, coût provisoire au-delà du stock et allocations cumulatives conservant les totaux. Après stock négatif, le stock restant prend le coût de la réception ; les anciennes ventes restent provisoires jusqu'à régularisation explicite, conformément à l'arbitrage du titulaire. Une réception connue fournit le dernier coût connu pour les nouvelles sorties, même si elle ne comble pas le déficit.

La migration 0070 ajoute les versions `StockCostSnapshot`, leurs liens d'origine et des contrôles SQL d'entiers JSON exacts. L'application Django enregistre la fonction SQLite `cost_snapshot_values_valid` à l'ouverture de ses connexions ; un outil externe écrivant directement cette table doit aussi fournir cette fonction. Ce modèle ne garantit pas à lui seul toutes les règles d'une future écriture de révision.

Depuis `backend`, démonstration sur une base fichier temporaire migrée :

```powershell
../.venv/Scripts/python.exe -X utf8 -m verification.cost_smoke
```

Cette démonstration utilise des mouvements de vérification explicites, identifiés par `demonstration: isolated_math_and_manual_snapshots`. À l'issue de ce socle initial, les services métier ne raccordaient pas encore le calcul aux opérations réelles ; la tranche suivante ci-dessous raccorde les nouvelles ventes. Marges, frais tardifs, régularisation des ruptures et propagation historique restent requis. US-33 reste partielle et US-34 non démontrée fonctionnellement.

Validation de cette tranche : 494 tests et 335 sous-tests réussis (98,89 s), dont 15 tests et 28 sous-tests ciblés coûts ; démonstration SQLite fichier, Django check, fraîcheur migrations, OpenAPI et gettext réussis. Les contrôles HTTP et concurrents de la tranche précédente n'ont pas été relancés pour ce calcul privé. L'objectif global reste actif.

### Coûts figés lors des nouvelles ventes — 18 octobre 2026

La création d'une vente enregistre désormais un `StockCostSnapshot` initial par ligne dans sa transaction stock/caisse existante. Les sources sont les ouvertures et les réceptions physiques aux montants/facteurs facturés ; un prix catalogue courant ne remplace pas ces coûts. Les réceptions exactement au même instant effectif précèdent les sorties, selon décision du titulaire. La vente antidatée exclut les réceptions futures. Les coûts connus, inconnus et provisoires restent distincts ; une réception après rupture ne réécrit pas les anciennes ventes provisoires. Une ouverture vide ne crée pas de moyenne sans unités.

Les champs de coût restent privés et les anciens reçus réseau inchangés. `SaleLine.unit_cost_micros` n'est pas remplacé par un ratio tronqué : le raccordement des retours au coût exact figé reste à faire, ainsi que marge, frais tardifs, révisions et régularisations. Aucune livraison complète d'US-33/34 déduite de cette tranche.

Parcours HTTP réel sur SQLite fichier temporaire, depuis `backend` :

```powershell
../.venv/Scripts/python.exe -X utf8 -m verification.sale_cost_smoke
```

Ce parcours enregistre des factures, réceptions et ventes par HTTP, vérifie les coûts sauvegardés, les reprises et la conservation du cash/dettes. Il complète la démonstration pure `verification.cost_smoke`, sans déploiement ni données de production.

Résultat final de la tranche : 504 tests et 337 sous-tests réussis en 125,41 s ; Django check, fraîcheur migrations, OpenAPI --validate --fail-on-warn et gettext réussis. Deux contrôles concurrents SQLite ciblés réussis (64 désélectionnés, 66 disponibles). HTTP réel : quatre ventes avec coûts figés, dont coût inconnu après ouverture nulle, reprises sans doublon, cash 36000 et dette fournisseur 28000. Le lot complet de concurrence et la charge cible restent non qualifiés pour ce changement.

### Coût exact des retours physiques — 18 octobre 2026

La migration 0071 ajoute `ReturnCostSnapshot`, lié à la réception physique et à la valeur initiale de vente lorsqu'elle existe. Le coût total est réparti par différences cumulatives conservant son total, sur toutes les réceptions de la ligne d'origine ; les quantités restent bornées aux retours acceptés. Une ancienne vente sans snapshot conserve son coût entier historique ou son inconnu, sans provenance inventée. Aucune reconstitution automatique du coût des anciens retours.

Les réceptions, décisions sur non-vendables et liste du stock non vendable exposent un objet `cost` réservé au responsable par leurs contrôles existants : référence du coût de réception, référence de vente éventuelle, état, total nano-FCFA et ratio micro-FCFA. Les entiers exacts sont des chaînes JSON. `unit_cost_micros` reste une projection entière si possible ; null peut aussi signifier un coût connu fractionnaire ou trop grand, dont `cost` fournit la représentation exacte. L'objet de la liste est le coût de la réception entière ; la quantité restante est présentée séparément. Une décision expose seulement sa part du coût de cette réception.

Les remises en vente réinjectent cette part exacte dans le calcul des nouvelles ventes. Les parts de rebut et de départ fournisseur ne sont pas ajoutées au disponible et ne réapparaissent pas dans son coût. Un coût provisoire de retour reste provisoire dans le pool concerné jusqu'à épuisement ou future révision contrôlée ; un inconnu ne devient jamais zéro. Les versions initiales de vente sont préservées.

Validation : 512 tests et 342 sous-tests réussis en 154,25 s, dont 20 tests/15 sous-tests ciblés. Deux contrôles concurrents SQLite fichier réussis (65 désélectionnés, 67 disponibles). Django check, fraîcheur migrations, OpenAPI --validate --fail-on-warn et gettext réussis. `verification.sale_cost_smoke` vérifie désormais aussi par HTTP un retour à coût 1100, sa remise en vente au milieu d'unités à 1200 et le coût de la vente suivante. Cash final 37500, dette fournisseur 28000, sans cash implicite du retour. Aucune donnée de production ni déploiement.

Restent requis : révisions/propagation après frais tardifs, coûts et marges révisés des retours, régularisation des ruptures, rapports de marge, transferts, synchronisation et qualification des budgets/charge. L'objectif global reste actif.

### Frais directs d'achat et répartition — US-34, partielle

Migration 0072 : frais distincts de la facture et du paiement, plans de répartition versionnés et lignes historiques protégées. Routes responsables sous `suppliers/{supplier}/invoices/{invoice}/charges/` : POST `preview/`, GET/POST racine, GET `{charge}/`, GET/POST `{charge}/revisions/`. Création : champs d'événement, `label`, `amount_fcfa`, `allocations` (`line_id`, `amount_fcfa`). Correction : `operation_id`, `base_version`, `reason`, `allocations`, sans changer le total des frais. Prévisualisation automatique au prorata des valeurs nettes, plus forts restes et UUID pour les égalités ; valeurs nulles/inconnues exigent une affectation explicite. Sous-ensemble de lignes possible et plan manuel prévisualisable.

Le coût des nouvelles ventes inclut les allocations courantes, avec prorata sur les quantités reçues confirmé par le titulaire. Les anciennes valeurs restent conservées. `historical_costs_pending_review` dans la version signale conservativement les coûts historiques potentiellement concernés ; leur recalcul et la propagation aux marges/retours/transferts/pertes ne sont pas encore réalisés. Ce marqueur ne vaut pas clôture de la révision historique.

Aucun paiement ou dette supplémentaire n'est créé par des frais ou leur correction. Une dépense déjà réellement payée reste indépendante. Reprises stables, contrôle de version, rôles actuels et pagination SQL s'appliquent aussi aux plans.

Vérification HTTP sur base SQLite fichier temporaire, depuis `backend` :

```powershell
../.venv/Scripts/python.exe -X utf8 -m verification.purchase_charge_smoke
```

Le parcours vérifie dépense réelle unique, plan 1000/3000 puis 2000/2000, coûts de nouvelles ventes 1100 puis 1200, ancien coût préservé, dette 40000 et cash net −1000. L'objectif global demeure actif ; frais tardifs/révisions complètes et marge restent à livrer.

Résultat final : 525 tests et 358 sous-tests réussis en 112,86 s ; Django check, fraîcheur migrations, OpenAPI --validate --fail-on-warn et gettext réussis. Deux contrôles concurrents ciblés réussis, 66 désélectionnés (68 disponibles) ; lot complet non relancé. Les réceptions 6/10 incorporant 600/1000 de frais sont couvertes par un test API distinct.


### Couverture OpenAPI vérifiée le 25 octobre 2026

Documentation interactive : `/api/v1/docs/` (Swagger UI), `/api/v1/redoc/`
(ReDoc), schéma `/api/v1/schema/`. Accès réservé au superadministrateur technique
actuel, par session ou JWT contrôlé ; propriétaire commercial ou simple staff
insuffisants. Le schéma utilise OpenAPI 3.0.3 et drf-spectacular.

Audit courant : 131 chemins, 176 opérations. Les schémas d'entrée/sortie et les règles
de sécurité générés passent la validation sans avertissement. Le 503 global de
maintenance est déclaré pour les 176 opérations avec corps bilingue,
code stable et en-têtes Retry-After/Cache-Control. Un test compare une vraie
réponse du middleware au schéma JSON et vérifie chaque opération.

La documentation reste incomplète : 149 opérations n'ont pas de résumé ou de
description propre. Le déclencheur quotidien décrit aussi son corps vide, ses statuts et les réponses 400/401/503. Les deux opérations de revalidation vendeur décrivent désormais leurs erreurs 400/401/403/404/409/415 selon la méthode ; les autres réponses 4xx restent à compléter. Il faut
compléter action par action les permissions, champs, exemples, règles métier,
conditions de concurrence/reprise et erreurs réellement renvoyées. Une validation
OpenAPI sans avertissement ne démontre pas cette complétude ; les commandes
console et formulaires Unfold sont documentés dans BACKUPS.md, hors API REST.

Revalidation vendeur : `/api/v1/shops/{shop_id}/team/{id}/restore-access/` expose un aperçu GET et une décision POST. Schémas des permissions brutes et effectives, génération, versions, reçu et historique explicités ; des réponses HTTP réelles 200/201/400/404/409/415 sont comparées au schéma JSON. Le champ `action` distingue les décisions de restauration dans l’historique de l’équipe. Procédure, exemple JSON et limites : [BACKUPS.md](BACKUPS.md#revalidation-des-vendeurs-par-le-responsable-connecté).
