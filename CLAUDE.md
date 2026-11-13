# CLAUDE.md — Réplication YemX

Le registre complet des décisions et des incertitudes se trouve dans AGENTS.md, importé ci-dessous et prioritaire en cas d'écart. Les sections GOALS, PRD et Interrogatoire y sont recopiées à l'identique ; toute modification doit être faite dans les deux fichiers.

@AGENTS.md

## GOALS

Cadrage validé par le titulaire, validation reprise dans la demande de rédaction du PRD. Cette section synthétise :
- le périmètre de première livraison confirmé à Q181 ;
- la précision Q182 ;
- les réponses stabilisées de l'interrogatoire du 11 novembre 2026 (G1–G26, détaillées dans la section « Interrogatoire du 11 novembre 2026 » après le PRD).

Elle ne remplace ni les décisions détaillées ci-dessous ni `analysis/scope-v1.md`. Les priorités ordonnent le travail ; elles ne rendent aucun module accepté facultatif. Une incompatibilité découverte doit revenir au titulaire, une question à la fois, avant toute modification du périmètre. La faisabilité et la parité restent à démontrer.

**État constaté le 11 novembre 2026 :**
- Aucune US n'est terminée : 41 sont partielles et 5 ne sont pas démontrées (`analysis/backend-us-coverage.md`).
- `backend/db.sqlite3` locale est vide et les 162 migrations n'y sont pas appliquées.
- Le client Flutter se limite au catalogue, à la vente comptant et à une outbox jamais envoyée : il n'a ni API, ni synchronisation, ni OTP/PIN.

**Ordre d'exécution (G1, G2) :**
1. Terminer, tester en suite complète, documenter puis commiter la pagination des propositions de conflit de prix, y compris le nouveau périmètre de signature du reçu.
2. Geler ensuite les nouvelles tranches backend qui ne servent pas le parcours vertical.
3. Livrer d'abord un parcours vertical de bout en bout : OTP → activation → vente hors connexion → synchronisation → affichage sur un second téléphone (US-01/05/17/36).
4. Enchaîner ensuite dans l'ordre du PRD.

**Révision du 13 novembre 2026 (titulaire) — G1 levé :** le gel des tranches backend est supprimé ; les US backend du PRD reprennent sans attendre DOD-14. Dans chaque parcours, la version connectée (en ligne) est réalisée et démontrée d'abord, puis le fonctionnement hors connexion de ce parcours. DOD-14 reste une gate de livraison, mais n'est plus un préalable aux tranches backend.

### 1. Objectifs fonctionnels prioritaires

1. **Livrer une application autonome de gestion de boutiques sur Android.**
   - Reproduire au plus près les écrans et la navigation de YemX, avec notre serveur, nos comptes et nos données.
   - Fournir tous les modules retenus sans abonnement ni quotas commerciaux.
   - Proposer le français et l'anglais dès la première livraison : interface Android, libellés métier Unfold, messages, reçus et rapports.
   - Le nom commercial reste à choisir. L'identifiant de package est figé à `cm.fokou.boutique` (G10, G11).
   - La fidélité s'apprécie comme une parité de parcours sur chaque écran YemX capturé : navigation, hiérarchie d'information, actions, palette et typographie proches. Elle est vérifiée côte à côte sur captures A207F et chaque écart est validé manuellement par le titulaire, sans seuil au pixel. Les écrans sans référence YemX suivent le même système de design (G21).
2. **Assurer les comptes, les équipes et le travail hors connexion.**
   - Distinguer propriétaire, responsable, vendeur et superadministrateur technique. Isoler les données par boutique et permettre les appartenances multiples avec permissions distinctes.
   - Réserver au propriétaire la nomination des responsables, et aux responsables les actions de gestion prévues.
   - Appliquer les permissions côté serveur et dans les données téléchargées, notamment pour les coûts et marges cachés par défaut.
   - Gérer OTP, PIN, révocation d'installation et récupération du compte.
   - Autoriser les opérations hors connexion pendant 7 jours depuis la dernière vérification serveur, pour chaque compte, boutique et installation. Après expiration, conserver les opérations et permettre la lecture autorisée du cache, mais bloquer les nouvelles opérations et les nouveaux documents. Un retrait reçu bloque aussi la consultation.
   - **Récupération exceptionnelle sans numéro ni codes de secours (G22) :** trois preuves exigées ensemble :
     - pièce d'identité vérifiée en personne ou en visio, seule sa référence étant conservée ;
     - connaissance de 3 faits du compte ;
     - confirmation par un membre actif de la boutique.

     Un délai de 72 h suit, avec notification aux sessions encore actives, avant de rattacher le nouveau numéro.
   - Le transfert de propriété d'une boutique est réservé au superadministrateur technique, avec motif, journal et les mêmes preuves. L'ancien propriétaire perd ses droits sur la boutique ; l'historique reste attribué à ses auteurs (G24).
3. **Couvrir catalogue, stock, ventes et retours.**
   - **Catalogue :** produits, catégories, photo facultative, code-barres hors connexion, unités, et plusieurs conditionnements avec facteurs et prix propres.
     - Un code-barres est unique parmi les formats actifs d'une boutique et le serveur refuse un doublon en ligne.
     - Un doublon créé hors connexion devient un conflit catalogue à résoudre par le responsable. D'ici là, le scan affiche les candidats sans rien ajouter au panier.
     - Formats lus : EAN-13, EAN-8, UPC-A, Code 128 (G14).
   - **Stock :** stock initial, alertes, inventaires pendant les ventes, corrections, pertes, archivage et suivi des retours non vendables.
   - **Ventes :** comptant et à crédit, paiements fractionnés par moyen déclaré, remises plafonnées, retours partiels valorisés au prix net facturé. Annulations, retours et corrections sont réservés au responsable, avec motif et journal.
   - **Entrées en stock :** uniquement les marchandises physiquement reçues et vendables.
   - **Refus d'une vente en attente, marchandise toujours présente (G19) :** le responsable libère explicitement la réservation, avec motif et aperçu, sans mouvement de stock. L'argent déclaré reste à régulariser séparément.
   - **Transferts entre boutiques du même propriétaire (G16) :** transit, réceptions partielles et écarts explicites.
     - Les réceptions cumulées sont plafonnées à la quantité expédiée ; un surplus physique devient un écart d'inventaire motivé à destination.
     - Annulation possible seulement avant le départ.
     - Le reliquat est clôturé par le responsable de destination, et le responsable source en est informé.
4. **Gérer clients, dettes, fournisseurs, achats et caisse.**
   - **Clients et fournisseurs :** fiches et soldes d'ouverture distincts par boutique, échéances facultatives, encaissements partiels, affectations explicites, avances, avoirs, remboursements réels et relevés chronologiques.
   - **Fusion de clients (G18) :** une opération reçue sur une fiche fusionnée B garde B comme client d'origine. Son effet est redirigé vers A, avec la mention « reçu après fusion » dans le relevé.
   - **Achats (G17) :** séparer commande, facture validée, réception et paiement fournisseur.
     - La réception peut dépasser la commande après confirmation explicite, la commande restant prévisionnelle.
     - Un achat peut avoir plusieurs factures ; la dette ne vient que des factures validées.
     - Le reliquat non reçu est clôturé explicitement par le responsable, sans effet sur la dette.
   - **Caisse :** caisse commune, moyens de paiement, fonds initial, apports, retraits, dépenses, monnaie due et comptages physiques, sans remise à zéro quotidienne ni mouvements financiers fictifs.
     - Tant que le fonds initial manque, l'attendu est marqué « incomplet » et le comptage se fait sans écart calculé. La saisie ultérieure du fonds, datée de son instant réel, recalcule l'attendu.
     - Catégories de dépenses fixes, traduites fr/en : transport, loyer, salaires, fournitures, factures, autre + libellé (G23).
   - **Rappels :** préparation des rappels WhatsApp après synchronisation, pour envoi manuel par l'utilisateur, selon ses permissions.
5. **Préserver les opérations réelles et rendre les conflits visibles.**
   - Synchroniser sans doublon.
   - Conserver les ventes et encaissements concurrents même en cas de stock négatif ou de trop-perçu, puis demander une régularisation explicite au responsable.
   - Conserver et soumettre à validation les opérations d'un vendeur retiré. Séparer montants validés et en attente, réserver les quantités en attente une seule fois et historiser les refus.
   - Résoudre explicitement les modifications concurrentes de catalogue. En cas de conflit de prix reçu, poursuivre au dernier prix commun confirmé.
   - **Antidatage (G15) :**
     - jusqu'à 30 jours, par le responsable avec motif ;
     - types permis : vente, paiement client, paiement fournisseur, réception, perte, mouvement de caisse ;
     - interdit avant un comptage validé du même périmètre, sauf révision explicite de ce comptage ;
     - le propriétaire peut dépasser les 30 jours avec un second motif.
   - Ne pas effacer l'historique ni appliquer automatiquement de remboursement ou de remise en stock.
6. **Fournir pilotage, documents et reprise de données.**
   - **Indicateurs :** chiffre d'affaires, encaissements, dettes, stock et marge brute estimée, avec leurs états provisoires ou incomplets visibles.
   - **Coûts :** coût moyen pondéré mobile, coûts de vente conservés et révisions tracées, y compris pour les frais d'achat tardifs.
   - **Tableaux de bord et rapports :** tableau de bord consolidé du propriétaire. Rapports globaux pour un responsable connecté, après synchronisation, avec périodes et fichiers numérotés complets.
   - **Reçus :** PDF générés sur le téléphone, partage et impression Android.
     - Le serveur attribue à l'activation un code d'installation unique, jamais réutilisé dans la boutique.
     - Les séries vente et paiement sont distinctes, par exemple `BT01-T03-V000001` et `BT01-T03-P000001`.
     - Le code boutique ne change plus après son premier usage (G26).
   - **Import CSV de démarrage (G20) :** sur modèles, avec prévisualisation, erreurs par ligne, confirmation et application tout ou rien par fichier et boutique.
     - Chaque modèle a une colonne `ref_import`, unique dans la boutique pour son type. Les soldes d'ouverture la référencent, et un fichier combiné est permis.
     - Encodage UTF-8 avec ou sans BOM, séparateur `;` ou `,` détecté, dates JJ/MM/AAAA heure de Douala.
     - 5 000 lignes et 2 Mo maximum par fichier.
   - **Alertes de stock bas avec l'application fermée (G25) :** synchronisation périodique WorkManager (15 minutes au mieux, réseau requis), puis notification locale. Le délai n'est pas garanti, et les réglages le disent.
7. **Permettre sauvegarde et restauration contrôlées.**
   - **Côté serveur :** sauvegardes SQLite + photos cohérentes et chiffrées, envois manuels et quotidiens, import et restauration via Unfold, et procédure de secours en console.
   - **Côté téléphone :** sauvegarde locale manuelle chiffrée, exportable hors de l'application, et import après vérification des droits actuels.
   - **Après une restauration serveur :**
     - récupérer les opérations des téléphones par identifiants stables, sans double effet ;
     - revalider responsables et vendeurs ;
     - afficher les résultats comme provisoires, puis clôturer explicitement la récupération.
   - Une clôture partielle exige motif, journal et mention persistante des données manquantes. Les arrivées tardives sont rapprochées sous contrôle du responsable.

### 2. Contraintes techniques

- **Client et distribution :**
  - Flutter avec SQLite local (Drift). Android API 24 minimum, cible API 36 à revalider avant publication.
  - APK signés de production livrés directement au pilote, avec l'identifiant `cm.fokou.boutique`.
  - La clé de signature est générée hors dépôt et conservée par le titulaire en 2 copies (G10, G11).
  - Deux APK distincts, arm64-v8a et armeabi-v7a, chacun de **50 000 000 octets au plus**. x86 est exclu (G12).
  - Stockage installé, RAM et fluidité restent à mesurer.
- **Appareils de référence (G13) :**
  - Le Samsung SM-A207F (2 Go, API 30) est l'appareil plancher réel. API 24 et API 36 sont couverts par émulateur.
  - Le titulaire fournira la liste des 6 téléphones du pilote avant la recette.
  - Les seuils mesurés sont soumis au titulaire avant la recette.
- **Schéma local (G9) :** le schéma Drift v1 est réécrit sans migration tant qu'aucun pilote n'a de données réelles. Les migrations Drift testées deviennent obligatoires dès le premier APK remis au pilote.
- **Serveur :**
  - Python, Django, SQLite, Django REST Framework et administration Django Unfold.
  - Hébergement initial PythonAnywhere gratuit, sans montée en gamme payante autorisée. Renouvellement mensuel manuel, avec rappels email 7 puis 2 jours avant expiration.
  - Budget initial du backend installé : **strictement moins de 500 000 000 octets**. Il inclut code, runtime Python, dépendances, SQLite, photos et fichiers générés, hors OS.
  - Mesurer aussi les fichiers auxiliaires et temporaires : le quota de l'hébergeur ne démontre pas ce budget.
- **Environnements (G3, G4) :**
  - Le développement se fait contre un Django local migré, puis la qualification sur PythonAnywhere, sans toucher aux données du pilote.
  - Un faux transport SMS est réservé aux réglages dev/test. Il refuse de démarrer en production et ne vaut jamais preuve DOD-09.
  - Un seul SMS Cooms réel est envoyé, en qualification.
- **Dimensionnement :**
  - Selon l'arbitrage du 24 octobre 2026 : **2 boutiques actives au pilote initial**, 3 téléphones par boutique, soit **6 appareils**. Scénario : un propriétaire et deux vendeurs par boutique.
  - Cadences conservées : 100 ventes par boutique et par jour, 5 lignes par vente en moyenne, 1 000 produits par boutique, horizon de test de 12 mois. Cela donne environ **73 000 ventes, 365 000 lignes et 2 000 produits**.
  - La cible de 10 boutiques et 30 appareils est reportée à une phase ultérieure.
  - Ce sont des cibles de charge, sans quotas commerciaux ni suppression automatique de l'historique.
  - Une photo facultative par produit, compressée à **20 ko maximum** et synchronisée ; qualité et occupation réelle à vérifier.
- **Identité et synchronisation :**
  - UUID stables pour les entités et les opérations, distincts des références lisibles ; reprises idempotentes et journal d'activité.
  - Horodatages en UTC, convertis pour l'affichage et les périodes locales, notamment Africa/Douala.
  - Sur l'appareil d'origine, conserver chaque opération synchronisée au moins 30 jours après sa synchronisation réussie, et les opérations non synchronisées ou en attente jusqu'à leur traitement.
  - Ne jamais restaurer des droits périmés depuis une archive.
- **Protocole de synchronisation (G5–G8) :**
  - **Envoi :** opération par opération, dans l'ordre des dépendances (fiche → vente → paiement). Il vise les endpoints REST par domaine existants et réutilise leurs reçus d'idempotence, sans endpoint de lot.
  - **Réception :** flux par boutique `GET /shops/{id}/changes?after=<seq>`.
    - Séquence monotone côté serveur, pages bornées, sérialisation selon les droits actuels.
    - Les retraits et archivages y figurent comme événements.
    - Le curseur local avance seulement après le commit Drift.
  - **Vendeur :** catalogue, stock disponible et clients ; dettes seulement avec la permission correspondante ; uniquement ses propres ventes et mouvements de caisse. L'activité des autres vendeurs n'arrive que sous forme d'effets agrégés.
  - **Responsable et propriétaire :** 90 jours glissants de détail, plus tout ce qui est encore ouvert (dettes non soldées, opérations en attente, transferts en transit). Les ventes plus anciennes se chargent à la demande, en ligne.
- **Calculs métier :**
  - Montants FCFA entiers, calculs exacts et arrondis déterministes qui préservent les totaux.
  - Quantités entières à la pièce, jusqu'à trois décimales pour le poids ou le volume.
  - Selon Q182, arrondir le montant complet de la ligne au FCFA le plus proche, moitié vers le haut, avant remises.
  - Répartir la remise globale au prorata des montants après remises propres, et vérifier le plafond total par ligne.
  - Figer les prix, facteurs et montants facturés nécessaires aux retours. Un coût inconnu ne vaut jamais zéro.
- **Authentification :**
  - OTP SMS au Cameroun pour les parcours retenus, uniquement à coût nul, avec un maximum global de 10 envois par jour, renvois compris. Fournisseur et gratuité effective à vérifier.
  - PIN local de 6 chiffres, bloqué 15 minutes après 5 erreurs consécutives, sans effacement.
  - SimpleJWT : accès de 5 minutes, refresh de 30 jours renouvelé par rotation avec invalidation de l'ancien refresh. Les droits et l'état de session actuels sont vérifiés côté serveur.
  - Ni les jetons ni le PIN ne renouvellent à eux seuls le délai hors connexion.
- **API :**
  - HTTPS, contrôles par compte, boutique, action et objet, et protection des champs sensibles, des photos et des exports.
  - Secrets hors dépôt et hors journaux.
  - Pagination dans les requêtes SQLite après autorisation et filtrage, avec ordre stable, tailles bornées et index adaptés. Les totaux portent sur tout le filtre.
  - Documentation via drf-spectacular : OpenAPI, Swagger UI et ReDoc. En production, schéma et interfaces sont réservés au superadministrateur technique.
- **Sauvegardes :**
  - Déclencheur externe cron-job.org appelant un endpoint HTTPS authentifié aux droits limités.
  - Sauvegarde quotidienne activée par défaut à **02:00 Africa/Douala**, configurable et désactivable.
  - SMTP Gmail `fokouvirtualone@gmail.com`, auto-envoi à l'expéditeur, et destinataire manuel configurable.
  - Archives chiffrées avec une clé séparée, découpées en parties identifiées de 15 Mo maximum. La taille encodée est vérifiée et l'intégrité contrôlée avant toute restauration.
  - Reprendre les parties échouées ou incertaines, au plus trois nouvelles tentatives espacées de 15 minutes. Suspendre toute nouvelle sauvegarde automatique tant que la précédente reste incomplète.
  - Nettoyer les fichiers temporaires après acceptation SMTP complète, en conservant manifeste et journal.
  - Le mot de passe de sauvegarde locale est distinct du PIN.
- **Choix techniques à documenter et tester sans nouvelle décision du titulaire :**
  - seuil de découpage des PDF ;
  - format et versions de la clé de sauvegarde ;
  - espace disque au pic.

  L'adresse de réception manuelle des sauvegardes reste à saisir dans Unfold.
- **Validation de livraison :**
  - Vérifier parcours métier, droits et isolation, erreurs, hors connexion, conflits, restauration, calculs historiques, deux langues, fidélité visuelle et budgets, dans des conditions représentatives.
  - Le respect du budget, la capacité SQLite/PythonAnywhere, l'OTP gratuit, SMTP et la planification restent à prouver.
  - Toute incompatibilité fonctionnelle doit être soumise au titulaire avant de réduire le périmètre.

### 3. Périmètre strictement Hors-Scope

Pour la première livraison, les exclusions confirmées sont :

- Clients web, iOS et bureau. La publication Google Play est reportée à une livraison ultérieure. L'administration web Unfold reste incluse.
- Offres Gratuit/Pro, abonnements, paiement d'abonnement et quotas commerciaux de YemX.
- Intégration automatique des paiements MTN, Orange ou bancaires. Les moyens et paiements réellement déclarés restent enregistrables.
- Intégration directe des imprimantes thermiques Bluetooth. La génération PDF et l'impression via Android restent incluses.
- Envoi automatique de rappels clients par SMS ou WhatsApp. La préparation et l'envoi manuel WhatsApp restent inclus.
- Transferts internes entre boutiques de propriétaires différents. Les transferts entre boutiques du même propriétaire sont inclus.
- Import universel de fichiers Excel et récupération automatique d'une base YemX. L'import CSV sur modèles est inclus.
- Portail public développeur et accès public à la documentation API de production.
- Notifications push serveur (Firebase/FCM). Les alertes reposent sur la synchronisation périodique et des notifications locales (G25).
- APK x86 et APK universel (G12).
- Cession de boutique par le commerçant lui-même. Seul le superadministrateur technique réattribue la propriété (G24).
- Comparaison visuelle automatique au pixel. La recette visuelle se fait par parité de parcours validée manuellement (G21).
- Envoi de vrais SMS en développement, et toute reconnaissance du faux transport SMS comme preuve de livraison OTP (G4).

Les arbitrages encore ouverts restent des décisions à résoudre dans le périmètre, et ne sont pas des exclusions. C'est le cas notamment :
- du nom commercial ;
- du fournisseur OTP effectif et de la preuve de sa gratuité ;
- des détails de réconciliation non tranchés.

Aucune des garanties suivantes n'a été acceptée, et aucune ne doit être présentée comme objectif garanti :
- bénéfice net ou comptabilité réglementaire ;
- perte maximale de 24 heures ;
- récupération de données dont aucune copie ne survit ;
- prolongation automatique du site PythonAnywhere.

## PRD — première livraison Android

### Statut, sources et règle de lecture

Version documentaire du 11 octobre 2026, révisée le 11 novembre 2026 pour intégrer les réponses G1–G26 de l'interrogatoire (section qui suit le PRD), dérivée des GOALS validés, de Q 1–Q 181 et de Q 182 ; dimensionnement initial actualisé par le titulaire le 24 octobre 2026 à deux boutiques/six appareils. Le présent PRD décrit le produit à livrer, pas l'état d'avancement. Le registre détaillé reste la source des exceptions et décisions ouvertes ; les observations REA/runtime établissent seulement les comportements effectivement observés dans YemX. Les ajouts expressément acceptés sont requis même lorsqu'ils n'ont pas été observés dans la cible.

Les User Stories ci-dessous déclinent les exigences acceptées. Le modèle logique propose les structures nécessaires pour les porter ; les noms d'entités, leur découpage physique et les états techniques sont des propositions d'architecture, pas un schéma récupéré de YemX ni de nouvelles décisions métier. Toute ambiguïté ayant un effet fonctionnel doit être résolue avec le titulaire, une question à la fois. Les conditions externes non démontrées sont des gates de livraison, pas des fonctionnalités supprimées.

### A. Problème, résultat attendu et utilisateurs

Les boutiques doivent enregistrer leur activité sur plusieurs téléphones, y compris sans réseau, puis retrouver des stocks, créances, dettes et mouvements d'argent cohérents. Les conflits ne doivent ni supprimer une opération réelle ni créer un second effet lors d'une retransmission. Les responsables doivent pouvoir expliquer les écarts et préserver les données en cas de perte d'appareil ou de restauration serveur.

Le résultat attendu est une application Android autonome, proche des parcours YemX, accompagnée de son backend et de son administration, exploitable par le pilote sans abonnement commercial. La réussite repose sur les parcours de bout en bout, l'isolation des boutiques, les calculs exacts et la récupération démontrée ; une collection d'écrans ou d'endpoints ne suffit pas.

| Acteur | Responsabilités et frontière d'accès |
| --- | --- |
| Propriétaire | Toutes les actions de responsable dans ses boutiques, nomination/retrait des responsables, consolidation de ses boutiques ; aucun accès implicite aux autres propriétaires ou aux sauvegardes globales. |
| Responsable | Catalogue, coûts, achats, réceptions, retours, corrections, inventaires, caisse, validations, vendeurs et permissions de sa boutique ; exports globaux sous condition de connexion. |
| Vendeur | Consultation utile à la vente, ventes et création de clients dans le périmètre autorisé ; crédits, encaissements, rappels et informations sensibles selon permissions. Signale les erreurs sans pouvoir modifier l'historique. |
| Superadministrateur technique | Exploitation, configuration, sauvegarde/restauration globale, confirmation des responsables après restauration, clôture de récupération et recours exceptionnel de compte. Ce rôle est distinct de la gestion commerciale. |
| Client/fournisseur | Tiers métier identifié par fiche, sans compte applicatif ou OTP imposé ; reçoit éventuellement un document ou un rappel envoyé manuellement. |

Permissions vendeur par boutique : crédit désactivé par défaut ; encaissement de dettes activé par défaut ; consultation autonome des dettes, préparation de rappels, consultation des coûts/marges et du solde global de caisse désactivées par défaut. Crédit, encaissement ou rappel accordent leur consultation nécessaire des dettes sans accorder les autres actions. Retirer cette consultation exige de présenter les permissions dépendantes et de confirmer leur retrait. Le vendeur conserve par défaut la consultation de ses propres mouvements de caisse ; cela ne représente pas le solde global.

### B. Périmètre, ordre de réalisation et parcours

Tous les modules des GOALS sont requis pour la première livraison. Depuis G1/G2, l'ordre de réalisation est : (0) terminer et commiter la tranche de pagination des propositions de conflit de prix, puis geler les nouvelles tranches backend hors parcours ; (T0) parcours vertical Android↔API de bout en bout — OTP, activation, vente hors connexion, envoi, flux descendant, affichage sur un second téléphone (US-01, US-05, US-17, US-36, US-48 ; gate DOD-14) ; puis identité/droits et données durables ; catalogue/calculs et opérations commerciales ; synchronisation/conflits ; pilotage/documents ; sauvegarde/restauration ; qualification du pilote. Les sauvegardes et la reprise doivent être conçues dès le socle, même si leur qualification intervient plus tard. Aucune tranche ne vaut livraison complète à elle seule.

Les exclusions sont exactement celles de GOALS §3 : autres clients que l'application Android, publication Google Play initiale, abonnements/quotas commerciaux, intégrations automatiques de paiement, Bluetooth thermique direct, rappels clients automatiques, transferts internes entre propriétaires distincts, import Excel universel/base YemX automatique, portail public API. L'administration web, les PDF/impressions Android, les paiements déclarés, les imports CSV sur modèles et les transferts entre boutiques du même propriétaire restent inclus.

Parcours de référence :

0. Tranche verticale T0 (G1) : OTP de développement verrouillé, activation d'une installation, vente comptant hors connexion sur le téléphone A, envoi opération par opération, réception par flux `changes` sur le téléphone B, relance des deux applications sans perte ni doublon.
1. Inscription/activation via OTP, création ou accès à une boutique, choix de langue, PIN ; invitation et permissions validées par le serveur.
2. Responsable préparant produits, conditionnements, stock et soldes d'ouverture, manuellement ou par CSV confirmé ; vendeurs retrouvant le catalogue local, par nom ou caméra/code-barres.
3. Vente comptant/crédit avec remises et moyens multiples ; écritures durables, reçu PDF et éventuelle dette ; fermeture/réouverture sans perte.
4. Encaissement client réparti entre dettes ; échéances, trop-perçu, monnaie à rendre et rappel manuel après vérifications.
5. Achat, facture, avance, réceptions partielles, retour fournisseur, avoir accepté et remboursement réel ; chaque événement a son effet propre.
6. Inventaire, pertes, retours clients, comptage de caisse et transfert avec transit ; rapprochement des opérations tardives sans correction automatique.
7. Deux appareils hors connexion enregistrant des opérations concurrentes ; synchronisation avec interruption/reprise, validation et régularisation explicites.
8. Rapport préparé depuis un instantané serveur et produit sur Android ; sauvegarde, restauration, récupération des appareils et clôture complète ou partielle.

### C. User Stories et critères d'acceptation

Les identifiants US servent à la traçabilité jusqu'aux preuves DoD. Chaque ligne est obligatoire dans la première livraison, sauf comportement explicitement conditionné par une permission ou une disponibilité système. « Responsable » comprend le propriétaire agissant dans une de ses boutiques.

#### C1. Identité, équipe, sécurité et langues — GOALS 1–2

| ID | User Story | Critères d'acceptation |
| --- | --- | --- |
| US-01 | En tant que titulaire, je veux créer mon compte et activer un téléphone pour accéder à mes boutiques. | Inscription/nouvel appareil/récupération vérifiés par OTP Cameroun ; 10 envois/jour maximum global incluant renvois ; coût nul vérifié. Échec ou quota atteint : aucune activation contournant OTP ni bascule payante. L'installation et le compte ont des UUID distincts. Hors production, seul un faux transport SMS limité aux réglages dev/test est permis ; il refuse de démarrer en production et ne vaut jamais preuve de livraison OTP (G4). Code d'installation lisible attribué par le serveur à l'activation, jamais réutilisé dans la boutique (G26). |
| US-02 | En tant qu'utilisateur, je veux déverrouiller localement l'application sans SMS quotidien. | PIN 6 chiffres ; cinquième erreur consécutive bloque 15 minutes ; compteur/blocage survivent à fermeture et redémarrage. Aucune opération effacée. PIN oublié : connexion/OTP, conservation des opérations et contrôle des droits actuels. |
| US-03 | En tant que propriétaire/responsable, je veux administrer mon équipe et ses accès. | Seul le propriétaire nomme/retire un responsable ; responsable gère vendeurs et permissions de sa boutique. Connexion et confirmation serveur requises ; auteur/motif selon action historisés. Aucune permission accordée seulement hors connexion. |
| US-04 | En tant que vendeur de plusieurs boutiques, je veux choisir ma boutique sans mélanger les données. | Permissions et baux hors connexion séparés par compte/boutique/installation ; vérifier A ne renouvelle pas B. Changer de boutique ne réattribue aucune opération. Retrait A n'enlève pas automatiquement B ni un autre appareil. |
| US-05 | En tant qu'utilisateur, je veux continuer l'activité quand le réseau manque. | Opérations autorisées pendant les 7 jours depuis vérification serveur UTC ; ensuite cache autorisé en lecture seule, date de synchronisation visible, opérations/modifications/nouveaux documents bloqués. Retrait reçu bloque aussi lecture ; opérations conservées. Recul incohérent de l'horloge impose vérification serveur, sans accusation automatique de fraude. |
| US-06 | En tant que responsable/propriétaire, je veux retirer l'accès d'un téléphone perdu. | Responsable retire une installation de vendeur dans sa boutique ; propriétaire pour les responsables. Motif/journal ; contrôle serveur même avec ancien JWT valide. Appareil hors connexion bloqué à réception du retrait ou expiration du bail, sans promesse de révocation distante instantanée. |
| US-07 | En tant qu'utilisateur, je veux récupérer mon compte et mes données après une perte. | Réinstallation : Internet/OTP, même compte, nouvelle installation, données serveur et droits actuels. Codes de secours consommables une fois pour perte du numéro puis OTP sur nouveau numéro. Sans numéro/codes : contrôle exceptionnel technique exigeant ensemble pièce d'identité vérifiée en personne ou visio (référence seule conservée), connaissance de 3 faits du compte et confirmation d'un membre actif de la boutique, puis délai de 72 h notifié aux sessions actives avant rattachement du nouveau numéro (G22) ; jamais sur simple déclaration. Données sans copie ne sont pas inventées. |
| US-08 | En tant qu'utilisateur, je veux utiliser français ou anglais et choisir la langue des documents. | Choix d'interface modifiable hors connexion ; interface/messages/libellés métier Unfold traduits. Langue proposée à génération PDF, défaut utilisateur ; données saisies inchangées, PDF existant inchangé, aucune permission nouvelle. |

#### C2. Catalogue, stock et circulation — GOALS 3

| ID | User Story | Critères d'acceptation |
| --- | --- | --- |
| US-09 | En tant que responsable, je veux gérer le catalogue de chaque boutique. | Nom, prix, coût connu/inconnu, catégorie, unité, seuil facultatif et une photo facultative ; écritures réservées au responsable. Photo ≤20 ko après compression, synchronisée et lisible sur exemples représentatifs. Copie interboutique autorisée crée nouvel UUID sans stock/historique. Archivage conserve historique et bloque nouvelles ventes dès réception. |
| US-10 | En tant que responsable, je veux plusieurs conditionnements avec un stock commun. | Facteur positif vers unité de base et prix propre par format ; 1 carton de 24 consomme 24 pièces. Quantités de pièces entières, poids/volume jusqu'à trois décimales. Facteurs/noms/prix historiques conservés lors de vente et achat. |
| US-11 | En tant que vendeur, je veux trouver une unité ou un conditionnement par scan hors connexion. | Code facultatif, caméra cherchant le catalogue local de la boutique autorisée ; carton sélectionne son format. Recherche par nom disponible. Code absent/inconnu/ambigu expliqué sans mauvaise sélection silencieuse. Formats EAN-13, EAN-8, UPC-A, Code 128 ; code unique parmi les formats actifs de la boutique, doublon refusé en ligne ; doublon né hors connexion = conflit catalogue, scan affichant les candidats sans ajout au panier jusqu'à résolution par le responsable (G14). |
| US-12 | En tant que responsable, je veux reprendre le stock existant sans achat fictif. | Quantité physique, date effective et coût connu/inconnu enregistrés comme ouverture traçable ; aucune dette fournisseur ou sortie de caisse créée. Réessai du même UUID n'ajoute pas un second stock. |
| US-13 | En tant que responsable, je veux inventorier sans arrêter les ventes. | Heure de comptage par produit ; comparaison théorique au même instant, quantité comptée conservée. Validation explicite avec motif pour correction. Mouvements postérieurs affectent le stock courant ; arrivées antérieures tardives révisent le rapprochement sans double déduction. |
| US-14 | En tant que responsable, je veux suivre pertes et retours non vendables. | Perte avec quantité/date/motif/journal, sans vente/recette/dette client automatique. Retour non vendable séparé du disponible ; remise en vente, retour fournisseur ou rebut explicites et historisés. Rapprochement avec corrections/pertes existantes empêche un second effet. |
| US-15 | En tant que responsable/propriétaire, je veux être alerté d'un stock bas. | Disponible connu = validé moins réservations en attente, sans double effet ; seuil atteint si disponible ≤ seuil. Centre d'alertes et indicateur vendeur ; notifications Android désactivables par utilisateur/boutique selon autorisation système. Une notification par franchissement connu, nouvelle possible après remontée ; aucune répétition tant que stock reste bas. Données datées et complétude signalée. Application fermée : synchronisation périodique WorkManager (15 min au mieux, réseau requis) puis notification locale ; délai non garanti signalé dans les réglages ; aucun push FCM (G25). |
| US-16 | En tant que responsable, je veux transférer physiquement entre boutiques du même propriétaire. | Départ source, transit et réceptions destination distincts, autorisés dans chaque boutique ; 5 expédiés/3 reçus laissent 2 en transit. Coût source figé au départ et intégré pour quantité reçue ; prix destination indépendant. Perte/reliquat exige régularisation avec motif ; aucun retour automatique source. Réceptions cumulées plafonnées à la quantité expédiée ; surplus physique enregistré comme écart d'inventaire motivé à destination ; annulation seulement avant départ ; reliquat clôturé par le responsable destination, responsable source informé (G16). |

#### C3. Ventes, clients, encaissements et rappels — GOALS 3–5

| ID | User Story | Critères d'acceptation |
| --- | --- | --- |
| US-17 | En tant que vendeur, je veux enregistrer une vente comptant et produire son reçu. | Panier multiligne, unité/format, quantités et tarifs conservés ; validation durable avec stock et paiement cohérents. Moyens déclarés : espèces, MTN, Orange, virement, autre nommé ; pas de transfert initié. Réessai réseau ne double aucune écriture. |
| US-18 | En tant que vendeur autorisé, je veux appliquer des remises dans la limite de la boutique. | Plafond 0 % par défaut, contrôle du cumul propre/global par ligne contre référence catalogue figée. Global réparti au prorata après remises propres, écarts stables et somme exacte. 0,5 kg ×249 donne 125 FCFA avant remises. Montants nuls, grosses valeurs et dépassements de plafond testés. |
| US-19 | En tant que vendeur autorisé, je veux vendre à crédit à un client identifié. | Permission crédit requise, fiche nom/surnom obligatoire/téléphone facultatif créée aussi hors connexion. Dette connue affichée et confirmation pour client déjà endetté ; avertissement hors connexion. Échéance facultative, sans plafond d'encours inventé ni OTP client. |
| US-20 | En tant que vendeur, je veux créer un client et signaler une correction. | UUID local stable, aucun rapprochement automatique sur nom/téléphone. Responsable corrige/fusionne avec motif/historique, conserve ventes/dettes/paiements et identifiants historiques ; opération reçue après fusion sur la fiche B conserve B comme client d'origine, effet redirigé vers A sans duplication et mention « reçu après fusion » dans le relevé (G18). |
| US-21 | En tant que vendeur autorisé, je veux encaisser et affecter un paiement entre dettes. | Permission indépendante du crédit ; dettes utiles visibles même créées par autre vendeur. Proposition anciennes d'abord, aperçu/confirmation ; dettes 3 000+7 000, paiement 5 000 → affectations 3 000+2 000. Un paiement, plusieurs allocations, montant réel compté une fois. |
| US-22 | En tant que vendeur, je veux déclarer plusieurs moyens et la monnaie effectivement rendue. | 4 000 espèces +6 000 Orange sur paiement 10 000 : caisse espèces +4 000. Conserver remis/rendu ; vente 7 500, remis 10 000, rendu 2 500 : net 7 500. Rendu nul : caisse +10 000, obligation client 2 500, fiche client requise, chiffre d'affaires 7 500. Référence externe facultative ; doublon possible signalé sans supprimer l'encaissement. |
| US-23 | En tant que responsable, je veux régler un trop-perçu ou une monnaie due explicitement. | Encaissements réels distincts conservés, dette soldée et excédent séparé ; remboursement réel ou affectation décidé avec motif/journal. Aucun nouvel encaissement lors d'une affectation, aucune régularisation automatique. |
| US-24 | En tant que responsable, je veux gérer échéances et relevés clients. | Dette d'ouverture distincte d'une vente, sans CA/marge/caisse fictifs. Modifier échéance après vente exige responsable/motif/avant-après ; retard seulement au lendemain local, jamais pour échéance absente. Relevé : solde antérieur, mouvements, allocations, références/auteur et solde après, validé/en attente séparés. |
| US-25 | En tant qu'utilisateur autorisé, je veux préparer un rappel WhatsApp exact et maîtriser son envoi. | Synchronisation réussie à la préparation, même pour copier ; paiement connu en attente/régularisation bloque rappel chiffré. Dettes en retard sélectionnées par défaut, ajout explicite d'autres dettes avec aperçu. Avertir des paiements absents d'autres appareils. Sans téléphone, copie possible. Historique « préparé », puis « envoyé déclaré » sur confirmation manuelle ; jamais livré/lu automatiquement. |
| US-26 | En tant que responsable, je veux annuler ou retourner partiellement une vente. | Motif/journal, historique original conservé ; cumul quantités et valeurs borné à l'origine. Retour au prix net facturé, notamment 3/24 d'un carton net 4 800 =600. Dette de vente réduite d'abord ; paiement ancien conservé et remboursement réel séparé. Stock ajouté seulement après confirmation vendable ; retour imputé à sa date. Réédition du reçu annulé garde référence et montre annulation/remboursement distincts. |

#### C4. Fournisseurs, achats, caisse et valorisation — GOALS 4–6

| ID | User Story | Critères d'acceptation |
| --- | --- | --- |
| US-27 | En tant que responsable, je veux suivre fournisseurs, factures et soldes d'ouverture. | Fiches et dettes/avances/avoirs séparés par boutique. Commande prévisionnelle distincte de facture validée ; seule celle-ci fonde dette. Solde antérieur ne crée achat, réception ou caisse fictifs. Aucun paiement ou compensation implicite entre boutiques. |
| US-28 | En tant que responsable, je veux confirmer les réceptions partielles d'un achat. | Achat/réception/paiement réservés au responsable ; vendeur peut signaler. 10 cartons commandés/6 reçus : seul équivalent de 6 au stock, 4 attendus. Achat déjà reçu saisissable avec confirmation ; reprise même réception sans double stock, facteurs historiques conservés. Réception au-delà de la commande permise après confirmation explicite, commande prévisionnelle ; plusieurs factures par achat, dette fondée sur les seules factures validées ; reliquat non reçu clôturé explicitement par le responsable, sans effet sur la dette (G17). |
| US-29 | En tant que responsable, je veux enregistrer avances, paiements et leurs affectations. | Paiement avant facture = avance distincte ; 30 000 affectés à facture 100 000 laissent 70 000 dus sans seconde sortie d'argent. Historique paiement conservé ; avances/avoirs distincts de dette et du cash. |
| US-30 | En tant que responsable, je veux distinguer retour fournisseur, avoir et remboursement. | Départ physique réduit stock, avoir réellement accepté réduit dette, argent reçu crée remboursement, chacun distinct. Retours partiels motivés ; avoir différent de référence conserve les deux valeurs et motif. Dette 3 000/avoir 5 000 laisse crédit fournisseur 2 000, non argent reçu, sans affectation automatique ailleurs. |
| US-31 | En tant que responsable, je veux suivre une caisse commune continue et les mouvements non commerciaux. | Fonds initial/apport/retrait/dépense avec date/auteur/appareil/motif, hors CA ; pas de remise à zéro quotidienne. Ventilation par moyen et vue personnelle vendeur ; solde global uniquement sur permission. Un paiement fournisseur/remboursement ne devient pas seconde dépense manuelle. Fonds initial absent : attendu « incomplet », comptage possible sans écart calculé ; saisie ultérieure datée de son instant réel recalcule l'attendu. Catégories de dépenses fixes traduites fr/en : transport, loyer, salaires, fournitures, factures, autre + libellé (G23). |
| US-32 | En tant que responsable, je veux rapprocher mon comptage de caisse. | Conserver attendu, physique, date et écart ; 50 000 attendus/48 000 comptés → écart −2 000, sans correction automatique. Appareils non synchronisés signalés ; opérations antérieures tardives révisent attendu/écart, jamais le physique, confirmation requise pour régulariser. |
| US-33 | En tant que responsable, je veux une marge estimée qui explique ses limites. | Ventes nettes de remises/retours moins coût moyen pondéré mobile, encaissements et dépenses séparés ; 10 unités à 1 000 +10 à 1 200 donnent coût 1 100. Coût vente figé, inconnu signalé jamais zéro ; vente au-delà stock avec dernier coût connu provisoire. Retour vendable valorisé au coût d'origine, non coût courant. |
| US-34 | En tant que responsable, je veux incorporer les frais directs et tracer leurs corrections. | Frais répartis au prorata valeur nette d'achat, prévisualisation, ajustement motivé conservant total ; valeurs nulles/inconnues exigent allocation explicite. Frais déjà payés ne créent second cash. Frais tardifs révisent coûts/marges liés avec avant/après/auteur/date ; ventes, dettes et encaissements inchangés, propagation aux transferts/retours/pertes tracée. |
| US-35 | En tant que responsable, je veux enregistrer une opération oubliée à sa date réelle. | Date effective passée saisissable par responsable avec motif, distincte de saisie et réception serveur UTC. Vendeur ne modifie pas manuellement la date. Impacts sur coût, rapports et comptages rapprochés ; aucun écrasement des observations antérieures. Antidatage limité à 30 jours pour vente, paiement client, paiement fournisseur, réception, perte et mouvement de caisse ; refusé avant un comptage validé du même périmètre sauf révision explicite de ce comptage ; au-delà de 30 jours, propriétaire seulement avec second motif (G15). Une saisie à 31 jours par un responsable est refusée. |

#### C5. Synchronisation, documents, import et exploitation — GOALS 5–7

| ID | User Story | Critères d'acceptation |
| --- | --- | --- |
| US-36 | En tant qu'utilisateur, je veux synchroniser sans perte ni duplication malgré les interruptions. | UUID stables pour entités/opérations ; accusé durable et reprise après réponse perdue. Même opération rejouée n'a qu'un effet ; même UUID avec contenu différent refusé. Dépendances fiche/vente/lignes/paiements cohérentes ; deux ventes réelles de dernière unité restent deux ventes, stock −1 signalé. |
| US-37 | En tant que responsable, je veux revoir les opérations d'un accès retiré et les conflits. | Opérations hors connexion conservées, validé/en attente distincts, réservation une fois ; validation ne redéduit pas stock. Refus motivé conserve événement et exige régularisation séparée. Deux modifications du même champ/version restent propositions ; prix de vente après conflit reçu =dernier commun confirmé jusqu'à résolution. Refus d'une vente avec marchandise toujours présente : libération explicite de la réservation par le responsable (« marchandise présente »), motif et aperçu, sans mouvement de stock ; argent déclaré régularisé séparément (G19). |
| US-38 | En tant qu'utilisateur, je veux un reçu lisible et partageable, même hors connexion. | PDF vente/paiement généré sur téléphone, référence code boutique/code téléphone/compteur stable et UUID interne séparé. Montants/moyens/date/langue et état non synchronisé/en attente visibles ; partage manuel/impression Android. Réédition conserve référence ; ancien PDF pas réécrit silencieusement. Blocage après expiration/retrait conforme US-05. Séries vente et paiement distinctes (ex. BT01-T03-V000001 / BT01-T03-P000001) ; réinstallation = nouveau code d'installation et compteur à 1 sans collision ; code boutique figé après premier usage (G26). |
| US-39 | En tant que responsable/propriétaire, je veux des tableaux de bord et rapports complets. | Ventes, stock, dettes clients/fournisseurs, caisse, marge/relevés ; consolidation seulement boutiques autorisées du propriétaire. Connexion/synchronisation et instantané serveur pour rapports globaux, PDFs sur téléphone sans stockage systématique serveur. Jour/mois/année/période incluse locale, états à date d'arrêté ; parties numérotées/synthèse complète sans omission/doublon. Validé/en attente, inconnus et données manquantes visibles. |
| US-40 | En tant que responsable, je veux démarrer par CSV sans import partiel ou double reprise. | Modèles produits/clients/fournisseurs/soldes d'ouverture, droits serveur avec connexion. Prévisualisation et erreurs par ligne ; 100 lignes dont 3 invalides →0 appliquée. Confirmation avec revalidation : tout ou rien par fichier/boutique ; reprise même UUID ne recrée pas stock/dette. Saisie manuelle maintenue. Colonne `ref_import` unique par boutique et type, référencée par les soldes d'ouverture, fichier combiné permis ; UTF-8 avec/sans BOM, séparateur `;` ou `,` détecté, dates JJ/MM/AAAA Africa/Douala ; 5 000 lignes et 2 Mo maximum par fichier (G20). |
| US-41 | En tant que superadministrateur, je veux une API bornée et documentée. | Autorisation avant filtrage/pagination SQL, ordre déterministe et agrégats sur filtre complet. Entrées/pages/lots/uploads bornés, HTTPS/secrets protégés ; JWT accès 5 min/refresh 30 jours rotatif, état session actuel contrôlé. Schéma OpenAPI/Swagger/ReDoc réservés au rôle technique en production et conformes aux réponses réelles. |
| US-42 | En tant que superadministrateur, je veux sauvegarder et suivre chaque partie envoyée. | SQLite+photos cohérentes, archive chiffrée/clé hors archive ; manuel vers adresse configurable, quotidien 02:00 Douala désactivable vers expéditeur. Parties≤15 Mo identifiées et taille email encodée contrôlée. Trois reprises max à 15 min, seulement échecs/incertains ; pas nouvelle automatique si incomplète. Acceptation SMTP distincte de livraison ; nettoyage après acceptation complète, manifeste/journal conservés. |
| US-43 | En tant que superadministrateur, je veux restaurer même si Django est indisponible. | Unfold ou console, toutes parties/intégrité/clé/compatibilité vérifiées avant remplacement ; archive tronquée, altérée ou mauvaise clé refusée sans remplacement. Rétablissement de code/runtime et procédure de secours documentés ; responsables confirmés techniquement, vendeurs revalidés ensuite avant gestion/nouvelles saisies reconnectées. |
| US-44 | En tant que superadministrateur/responsable, je veux récupérer les appareils après restauration. | Rejeu des ventes, encaissements, achats, réceptions, paiements, corrections, retours, remboursements et validations dont copie subsiste, même déjà synchronisés avant panne ; dédoublonnage UUID. Au moins 30 jours de conservation après sync origine, plus long si en attente. Liste appareils, état provisoire jusqu'à clôture technique ; partielle avec motif/mention persistante ; tardives soumises au responsable et rapprochées des ajustements. |
| US-45 | En tant qu'utilisateur, je veux sauvegarder les opérations locales avant désinstallation. | Export chiffré manuel hors stockage privé, possible hors connexion, mot de passe distinct du PIN ; intégrité et compatibilité vérifiées. Import après connexion/authentification/droits actuels conserve UUID/origines sans doublon et ne restaure pas JWT/PIN/bail comme droits. Mot de passe perdu : archive non récupérable, données serveur toujours récupérables après vérification. |
| US-46 | En tant que superadministrateur, je veux maintenir l'hébergement gratuit disponible. | Date d'expiration configurée/actualisée, rappels email J−7/J−2 et renouvellement manuel vérifiable ; déclenchement cron authentifié et statut minimal. Ni visite ni reload assimilé à prolongation. Pannes, quota/espace et dernière sauvegarde incomplète visibles ; aucune dépense ou montée en gamme automatique. |
| US-47 | En tant que superadministrateur, je veux réattribuer la propriété d'une boutique sans parcours commerçant. | Réattribution par le superadministrateur technique seulement, avec motif, journal et preuves G22 ; ancien propriétaire perd tous droits sur cette boutique, responsables à reconfirmer par le nouveau propriétaire ; historique attribué à ses auteurs d'origine ; aucune cession en libre-service (G24). |
| US-48 | En tant qu'utilisateur, je veux recevoir sur mon téléphone les changements faits ailleurs, limités à mes droits. | Envoi opération par opération dans l'ordre des dépendances vers les endpoints par domaine, reçus réutilisés, sans endpoint de lot (G5). Réception `GET /shops/{id}/changes?after=<seq>` : séquence monotone par boutique, pages bornées, sérialisation selon droits actuels, retraits/archivages comme événements, curseur avancé seulement après commit Drift (G6). Vendeur : catalogue, stock disponible, clients, dettes selon permission, ses seules ventes/mouvements de caisse, effets agrégés des autres (G7). Responsable/propriétaire : 90 jours glissants + éléments ouverts, ancien à la demande en ligne (G8). Coupure en cours de page : reprise sans trou ni doublon. |

### D. Règles transversales et états du produit

- Chaque opération durable conserve identité, boutique, auteur, installation d'origine, instant effectif, instant de saisie et réception serveur lorsque disponible. Action nécessitant motif : motif obligatoire et trace avant/après ou événement compensateur. L'historique métier n'est pas remplacé par un simple solde courant.
- Distinguer trois dimensions : livraison technique (locale/non accusée/accusée), décision métier (validée/en attente/refusée avec régularisation à suivre) et qualité des données (provisoire/incomplète/consolidée). Une synchronisation technique n'est pas une validation métier ; un refus n'efface pas l'argent reçu.
- Disponible connu : stock validé moins réservations des sorties en attente. Une intégration validée remplace sa réservation par son mouvement, sans appliquer les deux. Une opération refusée suit une décision explicite de régularisation ; les règles de libération dans chaque cas restent à préciser.
- Dette, encaissement, avance, avoir et remboursement sont des faits différents. Les allocations déplacent une affectation, pas l'argent une seconde fois. Un état de caisse reflète les moyens et mouvements réels, pas les seules ventes.
- Coût moyen mobile calculé sur événements de réception et historique pertinent ; vente hors connexion susceptible de révision tracée. Coût inconnu/provisoire doit rester visible. Ne pas appliquer le coût courant rétroactivement sans reconstitution des événements.
- Retours successifs bornés à la quantité et au montant net d'origine. Le mode exact de répartition des francs résiduels entre retours successifs doit être arrêté et partagé par Django/Flutter ; le retour total restitue exactement le montant net initial.
- Tous les écrans requis couvrent chargement, contenu, vide, erreur et actions interdites ; un échec garde données/panier/opérations en attente et propose une reprise explicable. Afficher boutique active, disponibilité réseau, date de synchronisation et réserves pertinentes. Un échec d'upload photo, d'export ou de synchronisation ne devient pas un succès silencieux.
- Périodes d'activité locales converties en intervalle UTC début inclus/fin exclue ; dettes sans échéance jamais « en retard ». Rapport d'état et rapport de mouvements sont distingués. Les PDF sont des instantanés datés, pas des vues actualisées.

### E. Modélisation logique des données

#### E1. Conventions et répartition des responsabilités

Toutes les entités persistantes/synchronisées ont un UUID stable. Les données métier d'une boutique portent son UUID et les liens sont contrôlés pour empêcher les rattachements interboutiques. Exception explicite : un transfert relie deux boutiques autorisées d'un même propriétaire. Pour une relation à plusieurs boutiques, l'accès à chaque versant est contrôlé, sans fournir tout le contenu à un acteur non autorisé.

Montants de paiement/facturation : FCFA entiers ; quantités : représentation exacte dans l'unité de base, trois décimales maximum selon unité ; coûts : représentation exacte à précision interne suffisante, valeur nullable et état connu/provisoire/inconnu. Horodatages UTC, fuseau de boutique conservé et conversion des dates saisies. Le choix physique des échelles entières doit préciser bornes/overflow et ne pas réintroduire de flottants binaires.

SQLite local conserve données autorisées, file durable, journal rejouable, pièces nécessaires et curseurs de synchronisation hors cache temporaire. Serveur porte les droits actuels, reçus d'idempotence, décisions et vue consolidée. Les secrets PIN/jetons/clés ont un stockage protégé séparé ; un export métier n'est pas une exportation de droits valides. Les projections de stock, soldes et indicateurs sont calculées ou matérialisées à partir des faits, avec leur état de complétude ; elles ne remplacent pas les événements sources.

Les attributs ci-dessous sont le minimum logique requis ; ils ne prescrivent pas une table par ligne. Les champs communs d'identité/audit précédents s'appliquent sans être répétés. Les états indiqués doivent conserver leurs transitions et contraintes ; les choix encore ouverts sont recensés en §G.

#### E2. Comptes et autorisations

| Entité logique | Attributs essentiels et relations | Contraintes |
| --- | --- | --- |
| User / Compte | Téléphone vérifié, état compte, langue ; propriétaire de 0..N boutiques, membre de 0..N boutiques. | Même UUID après récupération ; téléphone client/fournisseur n'est pas identité de compte. |
| Shop / Boutique | Propriétaire User, nom/code, fuseau, plafond remise, état. | Une boutique a 1 propriétaire courant ; droits de propriété historisés, détail de transfert de propriété ouvert. |
| Membership / Appartenance | Shop, User, rôle, permissions, état/version. | Un couple boutique/compte ; permissions distinctes par boutique, dépendances explicites. |
| Invitation / ChangementDroits | Boutique, invitant, destinataire, rôle/permissions proposés, décision serveur, dates et versions. | Pas de droit effectif avant confirmation ; responsable ne s'accorde propriété. |
| Installation | Identité téléphone installé, utilisateur rattaché, code reçu, état, activation. | Nouvelle identité après réinstallation ; codes/compteurs ne provoquent pas collision de reçu. Code lisible attribué par le serveur à l'activation, unique et jamais réutilisé dans la boutique, compteurs vente/paiement séparés (G26). |
| DeviceSession / Session | Compte, installation, refresh courant/chaîne, expiration, révocation. | Rotation atomique, ancien refresh inutilisable ; secrets non stockés en clair dans l'historique. |
| ShopDeviceAccess / AccèsAppareil | Compte/boutique/installation, permissions vérifiées/version, vérification UTC, expiration, retrait et motif. | Un bail indépendant par triplet ; tombeau de retrait non effacé par rotation ou import. |
| OTPChallenge / TentativeOTP | Finalité, destinataire, vérificateur, expiration/consommation, tentatives et résultat envoi. | Jamais code en clair dans logs ; plafond global durable, renvois inclus ; durées/limites d'essais à définir. |
| RecoveryCode / RecoursCompte | Compte, vérificateur code/consommation ou dossier exceptionnel, décisions techniques et traces. | Code usage unique atomique ; preuves exceptionnelles à définir, accès technique limité. Dossier exceptionnel : référence de pièce vérifiée, faits contrôlés, membre confirmant, début/fin du délai de 72 h et notifications (G22). |
| LocalUnlockState / Préférences | Vérificateur PIN protégé, échecs/blocage, langue et préférences notifications par utilisateur/boutique. | État local durable ; ni PIN ni préférence ne crée permission serveur. |
| OwnershipTransfer / TransfertPropriété | Boutique, ancien/nouveau propriétaire, dossier de preuves G22, superadministrateur, motif, date, responsables à reconfirmer. | Superadministrateur seul ; atomique avec changement de propriétaire et retrait des droits de l'ancien ; historique non réattribué. |

#### E3. Catalogue et stock

| Entité logique | Attributs essentiels et relations | Contraintes |
| --- | --- | --- |
| Category / Catégorie | Shop, libellé, état ;0..N produits. | Catégories propres au catalogue boutique. |
| Product / Produit | Shop, catégorie facultative, nom, unité/précision, prix, seuil nullable, coût/état, version, archivage. | Stock/vente en unité de base ; coût inconnu nullable ; historique conservé à archivage. |
| ProductPack / Conditionnement | Product, nom, facteur exact positif, prix propre, état/version. | Plusieurs formats, stock commun ; facteur pièce entier ; anciennes lignes non modifiées. |
| ProductPhoto / Photo | Product, référence fichier, format/taille/hash/version, statut de synchronisation. |0..1 photo courante,≤20 ko après compression ; copie et suppression coordonnées avec sauvegarde. |
| Barcode / CodeVente | Shop, code, cible produit ou conditionnement. | Référence locale à un format précis ; politique d'unicité/ambiguïté à résoudre avant scan fiable. Unique parmi les formats actifs de la boutique, formats EAN-13/EAN-8/UPC-A/Code 128 ; doublon hors connexion porté par BarcodeConflict (G14). |
| BarcodeConflict / ConflitCode | Boutique, code, formats candidats, opérations d'origine, résolution/auteur/motif. | Scan sans ajout automatique tant que non résolu ; résolution explicite par responsable. |
| ProductChange / ConflitCatalogue | Produit/champ, version commune, propositions/opérations, résolution/auteur/motif. | Conserver propositions concurrentes ; prix commun disponible pendant conflit reçu. |
| StockMovement / Mouvement | Produit, quantité signée base, nature, événement source, coût/état, effectif. | Un effet par événement source ; ouverture/vente/réception/retour/perte/transfert/correction distingués. |
| StockReservation / Réservation | Produit, opération en attente, quantité, statut, décision liée. | Pas de double réservation ou double stock lors de validation ; cycle de refus explicite. Libération explicite « marchandise présente » après refus : décision/motif/aperçu, sans StockMovement (G19). |
| Inventory / InventoryCount | Boutique/campagne ; lignes produit, instant de comptage, physique, théorique/écart et révisions. | Correction liée distincte ; physique historique immuable. |
| StockLoss / NonSellableReturn | Produit, origine, quantité, état, motif, décision et mouvements liés. | Non vendable exclu du disponible ; transitions/rebut sans seconde perte. |
| Transfer / TransferLine | Boutiques source/destination, produits correspondants, quantité, facteur/coût source figés, état. | Même propriétaire ; stock source/destination indépendant ; unités compatibles. |
| TransferDispatch / TransferReceipt / TransferAdjustment | Transfert/lignes, quantités réellement parties/reçues, dates, reliquat et écart motivé. | Réceptions cumulées bornées sauf décision explicite à définir ; UUID événement empêche duplication. Décision G16 : cumul reçu ≤ expédié, surplus = InventoryCount/correction à destination, annulation seulement avant départ, clôture du reliquat par la destination. |
| StockAlertState | Produit, seuil, état bas/au-dessus, dernier franchissement, notifications et préférences. | Transition connue idempotente ; autorisation Android et droit actuel vérifiés. |

#### E4. Ventes, clients et créances

| Entité logique | Attributs essentiels et relations | Contraintes |
| --- | --- | --- |
| Customer / Client | Shop, nom/surnom, téléphone facultatif, version/état. | UUID stable ; homonymes/téléphones partagés admis, pas fusion automatique. |
| CustomerChange / CustomerMerge / CorrectionSignal | Client(s), ancienne/nouvelle valeur, auteur/motif, redirections et signalements. | Fusion même boutique, conserve identités originales et rattachements ; arrivées tardives résolues. Redirection B→A persistante : opération tardive garde client d'origine, effet appliqué à la cible avec mention (G18). |
| Sale / Vente | Shop, client facultatif sauf crédit/monnaie due, référence, type, total net, état/versions. |1..N lignes ; historique vente figé, pas suppression par annulation. |
| SaleLine / LigneVente | Vente, produit/format référencés et instantanés nom/facteur/prix/version, quantité base, brut, remises, net, coût/état. | Somme nets=total ; référence remise et coût de vente conservés. |
| CustomerDebt / Dette | Client, origine vente ou ouverture, principal/ajustements, échéance seuil UTC nullable, état. | Pas de CA pour ouverture ; solde dérivé d'allocations/avoirs, dette séparée du cash. |
| CustomerPayment / PaymentComponent | Paiement réel client/vente, moyens et montants, remis/rendu, référence externe facultative. | Un paiement 1..N composantes ; somme exacte, attribution origine/auteur/appareil ; référence externe pas clé d'idempotence. |
| DebtAllocation / Affectation | Paiement/avance/avoir identifié, dette, montant, décision. | N:M via allocations ; somme affectée ne dépasse pas disponible, sans nouveau mouvement de caisse. |
| CustomerCredit / ChangeDue / Refund | Client, obligation/trop-perçu/avoir, origine, disponible ; remboursements réels et allocations liés. | Dette soldée et excédent distincts ; cash seulement remboursement confirmé, pas double obligation pour même montant. |
| SaleCancellation / CustomerReturn / ReturnLine | Vente/lignes d'origine, quantité base, valeur nette, date/motif, vendable confirmé, décision. | Cumul borné quantité/net ; avoir, dette, stock et remboursement distincts. |
| DebtDueChange / Reminder / ReminderDebt | Dette et avant/après/motif ; rappel client avec dettes/montants figés, auteur/date, statut préparé/envoyé déclaré. | Pas statut livré/lu automatique ; sélection/permission/sync et blocages contrôlés. |

#### E5. Achats, fournisseurs, caisse et coût

| Entité logique | Attributs essentiels et relations | Contraintes |
| --- | --- | --- |
| Supplier / Fournisseur | Shop, identité/coordonnées et état. | Indépendant par boutique ; mêmes coordonnées ne fusionnent pas comptes. |
| PurchaseOrder / PurchaseLine | Fournisseur, produits/formats figés, quantités commandées, valeur prévue, état. | Commande seule ni dette validée ni stock ; détail facturation partielle à arrêter. Surlivraison confirmée explicitement, reliquat clôturé explicitement, plusieurs factures par achat (G17). |
| SupplierInvoice / InvoiceLine | Achat/tiers, montant net validé, lignes/quantités facturées, date et validation. | Facture fondant dette indépendamment des réceptions ; validations explicites. |
| GoodsReceipt / GoodsReceiptLine | Achat/lignes, quantités reçues, conversion base, date/confirmation, coût/état. | Effet stock uniquement reçu ; cumul/reliquat et UUID par réception. |
| SupplierDebt / SupplierOpeningBalance | Fournisseur, facture ou origine ouverture, dette restante/avance/avoir disponibles, dates/motif. | Solde d'ouverture sans nouvel achat/réception/cash ; signes sémantiques distincts. |
| SupplierPayment / SupplierAdvance / SupplierAllocation | Paiement réel/composantes, avance disponible, affectation à facture et montant. | Allocation consomme avance sans second paiement ; jamais compensation automatique interboutique. |
| SupplierReturn / SupplierCredit / SupplierRefund | Lignes et départ physique, référence historique, avoir accepté/écart motivé, remboursement reçu. | Trois faits séparés ; surplus avoir reste crédit fournisseur avant affectation explicite. |
| PurchaseExpense / CostAllocation | Frais directs, paiement existant lié si applicable, distribution par ligne/réception, valeur et motif ajustement. | Somme allocations=frais ; pas seconde dette/caisse par simple coût ajouté. |
| CostRevision / CostDependency | Produit/événement coût, ancienne/nouvelle valeur/état, cause, auteur/date et liens vers vente/retour/transfert/perte. | Révisions auditables, propagation sans modifier montants facturés/cash/PDF passés. |
| PaymentMethod / Caisse | Moyens par boutique ; caisse espèces commune continue, fonds initial référencé. | Espèces/MTN/Orange/virement/autre séparés ; fonds initial appliqué une fois. Fonds initial nullable « non renseigné » rendant l'attendu incomplet jusqu'à sa saisie datée (G23). |
| CashMovement | Moyen, type/sens/montant net, événement réel source, bénéficiaire/motif. | Un effet de cash par source/composante ; apport/dépense/retrait hors CA ; pas compensation silencieuse. Dépense : catégorie fixe (transport, loyer, salaires, fournitures, factures, autre + libellé) (G23) ; date effective antidatable selon G15. |
| CashCount / CashReconciliation | Instant, physique, attendu, écart, complétude/appareils manquants, versions et décision. | Physique conservé ; régularisation distincte liée, réserves et historique de révision. |

#### E6. Synchronisation, documents et exploitation

| Entité logique | Attributs essentiels et relations | Contraintes |
| --- | --- | --- |
| OperationEnvelope / LocalOutbox | UUID opération, type, boutique/auteur/installation, payload/version/base, dates, état et dépendances. | Durable avant accusé ; identité distincte de reçu et référence opérateur ; pas purge d'opération en attente. |
| OperationReceipt / SyncCursor | UUID opération/hash, résultat/effets reconnus, date accusé ; progression de téléchargement par périmètre. | Rejeu identique renvoie résultat cohérent, payload différent refusé ; progression non avancée avant persistance. Curseur = dernière séquence du flux `changes` de la boutique, avancé après commit local (G6). |
| ShopChangeSequence / ChangeFeedEntry | Boutique, séquence monotone, type/UUID d'objet, nature (création, modification, retrait, archivage, effet agrégé), instant serveur. | Séquence attribuée dans la transaction d'écriture ; lecture filtrée par droits actuels au moment de la réponse ; aucune donnée sensible pour un rôle non autorisé (G6/G7). |
| LocalRetentionPolicy | Rôle, boutique, fenêtre glissante (vendeur : ses opérations ; responsable : 90 jours + ouverts), purge de cache. | Ne purge jamais une opération non accusée, en attente, ni une opération synchronisée de moins de 30 jours sur l'appareil d'origine (G8). |
| ReviewDecision / Regularization | Opérations/conflit/écart, décision, auteur/motif, liens d'ajustement et état de clôture. | Décision atomique avec effets/journal ; pas effacement de faits refusés. |
| Activity / Journal | Action, compte, boutique, installation, objet/opération, dates et motif/avant-après. | Accès filtré ; secrets absents ; révisions conservées, aucune suppression métier destructive implicite. |
| ReceiptReference / DocumentExport / ExportPart | Compteur/codes stables, opération ; export UUID, type/langue/période/arrêté/versions, parties/manifeste et réserves. | Rééditions même référence, snapshot daté ; toutes lignes une fois ; PDF généré sur téléphone. |
| CSVImport / ImportRow | Boutique, auteur, type modèle, hash/UUID fichier, validation/erreurs, correspondances, confirmation/résultat. | Revalidation à confirmation ; all-or-nothing par fichier et reprise idempotente. `ref_import` unique par boutique/type ; bornes 5 000 lignes/2 Mo ; encodage et dates G20. |
| BackupConfiguration / BackupJob / BackupPart / SendAttempt | Heure/fuseau/activation/adresses, UUID archive/manifeste/version/clé référencée, hash/numéro/taille, état SMTP et essais. | Secret/clé hors archive ; total parties vérifiable ; même backup lors reprise ; historique après nettoyage. |
| RestoreSession / RecoveryDevice / RecoveryClosure | Sauvegarde restaurée, génération de reprise, appareils attendus/état, confirmations droits, manquants/motifs et dates clôture. | Rejeu après restauration reconnu ; clôture partielle visible ; tardives rapprochées des régularisations. |
| LocalBackupManifest | Format/version, périmètre autorisé, opérations/origines/hashes, paramètres de chiffrement non secrets. | Ni PIN/JWT/bail valable importé ; authentification/droits actuels exigés. |
| HostingState / OperationalAlert | Expiration site, renouvellement constaté, rappels J−7/J−2, dernier succès sauvegarde et état bloqué. | Pas secret dans statut cron ; reload distinct de renouvellement confirmé. |

#### E7. Cardinalités, invariants et transactions

- User 1→N Shop comme propriétaire ; User N↔M Shop par Membership ; User 1→N Installation, Installation 1→N Session et N↔M Shop via ShopDeviceAccess. Le triplet compte/boutique/installation identifie l'autorisation hors connexion, pas le JWT seul.
- Shop 1→N Product/Customer/Supplier ; Product 1→N formats/mouvements ; Sale 1→N SaleLine ; Customer 1→N dettes/paiements ; paiements N↔M dettes par allocations. Tous ces liens ordinaires restent dans la même boutique.
- Purchase 1→N lignes/réceptions ; facture 1→N lignes ; chaque dette issue d'une facture référence celle-ci, avec règles de facturation partielle à arrêter. Paiement fournisseur N↔M factures via affectations. Transfer 1→N lignes/départs/réceptions et relie exactement une source/destination, avec événements de transit.
- Paiement 1→N composantes ; composante réelle→effet de caisse unique ; vente/réception/retour→mouvement stock unique par ligne/événement. Les agrégats et allocations ne sont pas de nouveaux encaissements ou réceptions.
- L'unicité des UUID, reçus d'opération et clés d'effets empêche répétition technique. Les contraintes quantitatives/financières doivent être vérifiées sous transaction courte SQLite, pas seulement dans le formulaire. Concurrence entre deux appareils sur dernière unité accepte les deux ventes réelles ; concurrence de retours ne dépasse pas l'origine.
- Pour chaque commande appliquée, écritures métier, effets, reçu d'idempotence et journal doivent être cohérents : panne avant commit → aucun effet partiel ; commit puis réponse perdue → reprise sans deuxième effet. Le protocole exact des lots/dépendances et des checkpoints de restauration doit être documenté avant implémentation.
- Index requis selon requêtes : boutique+ordre stable+UUID sur listes, événement source/UUID sur reprise, compte/boutique/installation sur accès, produit+date sur stock/coût, client/fournisseur+date sur relevés. Les indices et projections physiques sont à mesurer ; pagination avant matérialisation et autorisation avant comptage restent obligatoires.
- Flux descendant : index boutique+séquence ; l'attribution de séquence et l'effet métier sont dans la même transaction, sinon une écriture pourrait être visible sans séquence ou l'inverse. Le client n'avance son curseur qu'après commit Drift de la page (G6).

### F. Definition of Done — critères stricts de validation

#### F1. DoD d'une User Story

Une US n'est « Done » que si chacun de ses critères d'acceptation a une preuve datée, reproductible et rattachée à l'ID, sur la version livrée. Le registre de preuve indique : build/commit, environnement et données, commande ou étapes, attendu/observé, résultat, artefact et limites. Aucun test sauté, fixture remplaçant une intégration requise ou capture d'un écran statique ne prouve un parcours complet.

Pour une US traversant Android et serveur : démontrer saisie Android, persistance SQLite après redémarrage, effets API/serveur, contrôle des permissions, réception sur un autre téléphone et affichage final. Pour une US technique : démontrer les effets et l'échec contrôlé sur le composant réel concerné. Tester chemin nominal et branches pertinentes vide/erreur/réseau absent/permission refusée, avec reprise sans perte. Migrations, validations, documentation API et traductions correspondantes sont livrées ; aucune branche requise n'est un stub.

#### F2. Gates de livraison et preuves attendues

| Gate | Condition stricte pour accepter | Preuve exigée |
| --- | --- | --- |
| DOD-01 — Couverture | US-01 à US-48 satisfaites, modules/exclusions conformes aux GOALS, arbitrages affectant comportement résolus. | Matrice US→critères→preuves→statut, aucune exigence requise non couverte ; approbation explicite pour toute évolution de scope. |
| DOD-02 — APK | APK de production signé, package propre,≤50 000 000 octets ; installation/mise à jour conservent données. API 24 minimum et cible 36 retenue, aucune fonctionnalité Android requise manquante. | Hash/taille/configuration du build, installation réelle et mise à jour sur appareil pilote ; tests API 24 et versions pilotes, architectures documentées. Depuis G10–G13 : identifiant `cm.fokou.boutique`, clé hors dépôt conservée en 2 copies ; deux APK arm64-v8a et armeabi-v7a mesurés chacun ≤50 000 000 octets ; appareil plancher SM-A207F, API 24/36 par émulateur ; migration Drift testée dès le premier APK pilote (G9). |
| DOD-03 — Interface | Parcours Android proches des références YemX, retours navigation corrects ; français/anglais complets, chargement/vide/erreur/retrait/expiration visibles, textes et montants lisibles. | Captures comparées par écran/parcours et langues, validation humaine des écarts ; PIN 6 documenté comme choix explicite ; accessibilité et textes longs vérifiés. Tolérance visuelle à arrêter avant recette. Tolérance arrêtée par G21 : parité de parcours (navigation, hiérarchie, actions, palette/typographie proches) en captures côte à côte A207F, écarts validés manuellement par le titulaire, sans seuil au pixel. |
| DOD-04 — Auth/droits | Aucun appel privé, objet, fichier ou champ sensible accessible hors droits actuels ; défauts permissions et dépendances respectés. Baux 7 jours indépendants, PIN 5 erreurs/15 min, refresh 5 min/30 jours rotatif conformes. | Matrice rôles/boutiques/appareils, ancien JWT après retrait, accès UUID tiers/photos/exports/docs refusés ; tests frontières temps/recul horloge/redémarrage/rotation concurrente. |
| DOD-05 — Calculs | Aucun franc créé/perdu par arrondi ou répartition ; quantités/plafonds/retours exacts ; coûts inconnus jamais assimilés à zéro, révisions traçables. | Vecteurs partagés Django/Dart et tests limites/gros montants, panier nul, cumul remises et retours successifs ; rapprochement stock/cash/dette/marge attendu. |
| DOD-06 — Durabilité/sync | Panne/reprise n'efface ni ne double opération ; deux opérations réelles concurrentes conservées ; décisions en attente/refus n'ont pas effets fictifs. | Deux ou trois téléphones, mode avion, serveur coupé, kill appli/commit/réponse perdue, réessais concurrents, ordre inversé et payload UUID différent ; stock/dettes/caisse/journal contrôlés. |
| DOD-07 — Documents/import | Reçus corrects hors connexion autorisée ; rapports globaux connectés depuis instantané complet et découpés ; CSV tout ou rien. | PDFs français/anglais et impression Android vérifiés visuellement ; comptage/total des lignes de toutes parties comparé au filtre serveur ; cas CSV 100 lignes/3 invalides et reprise après commit sans doublon. |
| DOD-08 — Charge/espace | Dimensionnement pilote exercé sur cible ; backend initial complet<500 000 000 octets selon définition Q 2, pas seulement quota compte. Les fichiers SQLite/temporaires sont pris en compte. | Inventaire reproductible code/runtime/dépendances/base/photos/fichiers auxiliaires ; pic backup/restore relevé, espace libre démontré. Selon l’arbitrage du 24 octobre 2026, jeu pilote 12 mois≈73 000 ventes/365 000 lignes/2 000 produits et trafic 6 appareils pour 2 boutiques ; latences, verrous, erreurs et ressources rapportés. Ancienne cible 10 boutiques/30 appareils reportée à une phase ultérieure. Budget initial et horizon 12 mois évalués séparément. |
| DOD-09 — OTP et exploitation | Livraison OTP réelle à coût nul sous plafond global 10/jour ; SMTP accessible ; cron authentifié réellement déclenché ; renouvellement manuel et rappels possibles. | Tests d'intégration sur comptes autorisés, compteur et concurrence de quota, erreurs/quota saturé sans paiement ; SMTP/cron/heure Douala constatés. Pas validation par documentation fournisseur seule. Le faux transport dev/test (G4) ne compte jamais ; preuve exigée par au moins un SMS Cooms réel en qualification. |
| DOD-10 — Sauvegarde | Archive cohérente DB/photos, chiffrée, parties complètes/intègres, reprises et nettoyage conformes ; aucune clé envoyée avec archive. | Sauvegarde pendant activité, multiparties dont échec SMTP, réponse perdue et trois reprises, suspension si incomplète, destinataires manuel/auto, taille encodée réelle et état conservé ; mauvais secret cron refusé. |
| DOD-11 — Restauration/recovery | Restauration Unfold et console prouvée ; opérations antérieurement synchronisées rejouées sans doublon, droits révalidés, clôtures et retours tardifs conformes. | Exercice sauvegarde T 0/opérations T 1/restauration T 0/récupération T 1 avec plusieurs appareils, manque définitif et arrivée tardive ; DB/photos/soldes/journal comparés. Archive tronquée/altérée/mauvaise clé refusée avant remplacement. |
| DOD-12 — Secours local | Export local/import après réinstallation conserve opérations et contrôle nouveaux droits ; codes secours usage unique et recours exceptionnel défini. | Désinstallation/réinstallation puis import avec ancien UUID, deuxième import sans effet, archive altérée/mauvais mot de passe et accès révoqué ; données serveur récupérables, pertes sans copie clairement annoncées. |
| DOD-13 — Vérifications et handoff | Vérifications backend/Flutter/migrations/schema vertes sans affaiblir tests ; aucun défaut empêchant un critère accepté. Installation, exploitation et repli reproductibles. | Suites pertinentes, analyse Flutter/build, Django check/migrations et OpenAPI sans avertissement bloquant ; guide pilote, runbook sauvegarde/restauration, gestion secrets/signature, état des limites et accord de recette du titulaire. |
| DOD-14 — Tranche verticale T0 | (Préalable aux tranches backend levé le 13 novembre 2026, gate de livraison maintenue.) Avant toute nouvelle tranche backend hors parcours (G1) : OTP de développement verrouillé, activation, vente hors connexion sur A, envoi, flux `changes`, affichage sur B, relance sans perte ni doublon ; backend local migré puis qualification PythonAnywhere sans données pilote (G3). | Deux téléphones ou téléphone+émulateur, mode avion puis reprise, kill entre commit et accusé ; comparaison base serveur/Drift A/Drift B ; journal et captures datés. |

Les seuils de latence, RAM et démarrage ainsi que les modèles d'appareil et tolérances visuelles ne sont pas chiffrés dans les décisions validées. Les mesurer puis soumettre les critères au titulaire avant recette ; ne pas inventer un seuil « réussi ». Un dépassement de budget confirmé, une impossibilité d'OTP gratuit ou d'exploitation gratuite bloque la livraison conforme jusqu'à décision explicite, sans réduction silencieuse de fonctionnalité ni achat implicite.

#### F3. Scénarios adversariaux minimums

| Cas | Vérification attendue |
| --- | --- |
| Deux dernières unités connues identiques | Stock initial 1 ; deux appareils vendent chacun 1 hors connexion ; deux ventes/cash conservés, stock−1, demande de régularisation. Rejouer chaque opération ne change rien. |
| Dette encaissée deux fois réellement | Dette 10 000 ; deux paiements distincts 10 000 ; cash 20 000, dette 0, excédent 10 000 à décider. Le replay d'un des UUID ne devient pas un troisième paiement. |
| Arrondis/remises/retours | Trois lignes égales, remise 100 →34/33/33 selon ordre stable ; 0,5×249→125 avant remise ; retours successifs carton 24 net 4 800 bornés à 24 et 4 800. Cas plafond 0/5 %, ligne gratuite et gros montants couverts. |
| Retrait/expiration multiboutique | Retrait A reçu bloque lecture/saisie A, garde file ; B demeure selon propres droits. À 7 jours expirés, lecture seule sans nouveaux PDFs ; rotation JWT ne renouvelle pas le bail. |
| Achat/réception/avance | Facture 100 000, avance 30 000 affectée →dette 70 000, aucune seconde sortie ; réception 6/10 cartons →stock 6 facteurs, reste 4 ; replay et retour/avoir/remboursement vérifiés séparément. |
| Inventaire/comptage avec arrivées tardives | Physique 8 à 10:00 puis vente 1 à 10:05 →courant 7 ; vente antérieure reçue tardivement rapproche correction sans double perte. Caisse physique 48 000 inchangée quand attendu est révisé. |
| Coût/frais et propagation | Stock 10 à 1 000 puis 10 à 1 200 →CMP 1 100 ; frais tardifs corrigent marge 500→400 pour vente 1 500 avec coût 1 000→1 100, sans changer cash ; retour/transfert/perte liés révisés une fois. |
| Rappel bloqué/données incomplètes | Échec sync ou paiement connu en attente interdit texte chiffré et copie ; dette sans échéance pas en retard ; ouverture WhatsApp reste « préparé » sans déclaration manuelle. |
| Erreur au mauvais instant | Kill avant commit, après commit avant accusé, pendant upload/download/import/export et rotation ; aucun effet partiel, file durable, reprise et messages corrects. |
| Reprise après panne globale | Restaurer sauvegarde ancienne puis rejeu opérations déjà accusées, appareil manquant, clôture partielle et arrivée tardive ; aucun rôle ancien réactivé ou ajustement doublé. |
| Code-barres dupliqué hors connexion | Deux responsables attribuent le même EAN à deux formats ; après sync, conflit visible, scan propose les deux sans ajout, résolution explicite (G14). |
| Vente après fusion | Téléphone hors connexion vend à crédit au client B fusionné entre-temps dans A ; dette appliquée à A, client d'origine B conservé, mention au relevé, aucun doublon (G18). |
| Sur-réception de transfert | 5 expédiés, réception de 6 refusée ; surplus saisi comme écart d'inventaire destination ; replay sans effet (G16). |
| Antidatage hors borne | Responsable saisit un paiement à J−31 : refus ; à J−10 avant comptage validé : refus sauf révision explicite ; propriétaire à J−40 avec second motif accepté (G15). |
| Réinstallation et reçus | Réinstallation du téléphone T02 : nouveau code T03, compteur V000001 sans collision avec T02 (G26). |
| Flux interrompu | Coupure réseau au milieu d'une page `changes` et kill après commit Drift ; reprise sans trou ni doublon ; vendeur ne reçoit aucun coût ni vente d'un autre vendeur (G6/G7). |

### G. Décisions ouvertes, dépendances et risques de livraison

| Sujet restant | Travail requis sans changer le scope | Moment de résolution |
| --- | --- | --- |
| Identité, références et matériel | Nom commercial et logo ; seuils de performance mesurés sur SM-A207F à soumettre. Résolus : package `cm.fokou.boutique` (G11), APK par ABI (G12), appareil plancher (G13), codes d'installation (G26), tolérance visuelle (G21). | Avant signature, tests comparatifs et recette. |
| OTP/recours et hébergement | Fournisseur coût nul et plafond réel, durée OTP/limites essais, (preuves de recours résolues par G22, OTP de développement par G4) ; accès SMTP, expiration réelle et planification cron sur comptes autorisés. | Avant activation réelle des utilisateurs et qualification exploitation. |
| Sync et décisions | Versions, checkpoints de restauration, conflits hors ordre, clôture des régularisations, arrivées tardives. Résolus : transport et flux (G5–G8), libération après refus (G19), fusion clients (G18). | Avant figer le protocole et livrer les US dépendantes. |
| Valorisation et retours | Coût initial/stock négatif, retours successifs/arrondis, coût des réceptions avant facture, frais tardifs et propagation historique, bornes de valeurs. | Avant valider US-26/28/33/34 et les vecteurs communs. |
| Achats/transferts/CSV | Compatibilité unités, colonnes exactes des modèles CSV et revalidation. Résolus : achats (G17), transferts (G16), clés et bornes CSV (G20). | Avant écrans/mutations concernés et tests concurrence. |
| Notifications/dates/exports | Préférences multi-appareils, instantanés paginés et seuil de parties PDF (choix technique). Résolus : application fermée (G25), codes ambigus (G14), antidatage (G15). | Avant qualification des parcours concernés. |
| Sauvegarde/reprise | Format/chiffrement/version clés, compatibilité/migrations, espace au pic, sécurité des secrets, abandon manuel d'incomplète, repli restauration et preuve appareils récupérés. | Avant exercice de panne et mise en service. |

Les choix techniques résolvant ces points peuvent être documentés et testés sans redemander les décisions déjà validées. Les changements de comportement, de coût, de contrainte ou de frontière d'accès exigent une décision du titulaire. L'absence d'une copie survivante, d'un mot de passe/clé de récupération ou d'une preuve d'identité suffisante doit être expliquée sans promesse impossible.

### H. Livrables et gouvernance de recette

La traçabilité de couverture des GOALS est la suivante ; les gates transversales DOD-01, DOD-03, DOD-04 et DOD-13 s'appliquent à tous les domaines concernés.

| Objectif GOALS | User Stories associées | Modèle et gates principaux |
| --- | --- | --- |
| 1 — Produit autonome et bilingue | US-08, US-38, US-39, US-46 et ensemble des modules | E 2/E 6 ; DOD-02/03/08/09. |
| 2 — Comptes, équipes et hors connexion | US-01 à US-08, US-41, US-47, US-48 | E 2/E 6 ; DOD-04/06/09/12. |
| 3 — Catalogue, stock, ventes et retours | US-09 à US-18, US-26 | E 3/E 4 ; DOD-05/06/07. |
| 4 — Clients, achats et caisse | US-19 à US-25, US-27 à US-32, US-35 | E 4/E 5 ; DOD-05/06/07. |
| 5 — Opérations et conflits | US-23, US-36, US-37, US-44, US-48 | E 3/E 6 ; DOD-05/06/11. |
| 6 — Pilotage, documents et reprise initiale | US-24, US-33 à US-35, US-38 à US-41 | E 4/E 5/E 6 ; DOD-05/07/08. |
| 7 — Sauvegarde, restauration et récupération | US-07, US-42 à US-46 | E 2/E 6 ; DOD-09/10/11/12. |

Livrables requis : APK signés arm64-v8a et armeabi-v7a (`cm.fokou.boutique`) et procédure d'installation/mise à jour ; client Flutter et backend Django/SQLite avec migrations et configuration hors secrets ; administration métier Unfold ; schéma OpenAPI et accès documentaire protégés ; modèles CSV ; reçus/rapports PDF français et anglais ; outils/procédures sauvegarde, restauration console et reprise appareils ; scripts et données de vérification ; matrice US/DoD et rapport de mesures sur cible.

Une feature n'est annoncée livrée qu'au niveau démontré : le backend seul n'établit pas la couverture Android, une configuration cron ne prouve pas un envoi quotidien, un accusé SMTP ne prouve pas livraison email et les tests locaux ne prouvent pas capacité PythonAnywhere. La recette finale présente les preuves DOD-01 à DOD-14 et les écarts éventuels au titulaire. Aucun déploiement, publication, envoi réel ou modification de compte externe n'est autorisé par la seule rédaction de ce PRD.

## Interrogatoire du 11 novembre 2026 — questions et réponses

Analyse menée en lecture seule.
- **Points d'entrée :**
  - backend : `backend/manage.py` et `config/wsgi.py`, réglages `config.settings`, qui exigent `APP_SECRET_KEY` et `APP_JWT_SIGNING_KEY` ;
  - client : `client/lib/main.dart`, puis `bootstrap.dart` et `app.dart` (go_router) ;
  - QA : `client/tools/qa_main.dart`.
- **Bases :** `backend/db.sqlite3` locale est vide, et les 162 migrations n'y sont pas appliquées. Côté client, Drift en `schemaVersion` 1 avec 8 tables.

Questions posées une à une ; chaque réponse a été acceptée par le titulaire :

| ID | Question | Réponse retenue |
| --- | --- | --- |
| G1 | Geler l'approfondissement backend au profit d'un parcours vertical Android ↔ API ? | Oui, vertical d'abord. **Révisé le 13 novembre 2026 : gel levé ; parcours en ligne d'abord, hors connexion ensuite.** |
| G2 | Que faire du travail non commité sur la pagination des propositions de conflit de prix ? | Le finir, lancer la suite complète, documenter le nouveau périmètre de signature du reçu, puis commiter. |
| G3 | Contre quel serveur développer ? | Django local migré, puis qualification sur PythonAnywhere sans toucher aux données du pilote. |
| G4 | Comment encadrer l'OTP hors production ? | Faux transport limité à dev/test, refusé en production, jamais preuve DOD-09 ; un seul vrai SMS, en qualification. |
| G5 | Quel transport d'envoi pour la synchronisation ? | Opération par opération, vers les endpoints par domaine existants, avec leurs reçus. |
| G6 | Comment le téléphone récupère-t-il les changements ? | Flux `changes?after=<seq>` par boutique, filtré par droits, curseur avancé après le commit Drift. |
| G7 | Que télécharge un vendeur ? | Catalogue, stock, clients ; dettes selon permission ; uniquement ses propres ventes et sa caisse. |
| G8 | Quel historique sur le téléphone du responsable ou propriétaire ? | 90 jours plus tout ce qui est ouvert ; chargement à la demande en ligne au-delà. |
| G9 | Faut-il migrer ou réécrire le schéma Drift v1 ? | Réécrire sans migration jusqu'au premier APK pilote, puis migrations obligatoires. |
| G10 | Quand figer l'identifiant et la signature ? | Maintenant, avec un identifiant neutre et une clé hors dépôt conservée en 2 copies. |
| G11 | Quel identifiant exact ? | `cm.fokou.boutique`. |
| G12 | Quelle distribution par architecture ? | Un APK arm64-v8a et un APK armeabi-v7a, chacun ≤ 50 000 000 octets ; x86 exclu. |
| G13 | Quel appareil plancher ? | Samsung SM-A207F, émulateurs API 24 et 36 ; la liste des 6 téléphones sera fournie avant la recette. |
| G14 | Quelles règles d'unicité des codes-barres ? | Unique par boutique, conflit si doublon hors connexion, liste de candidats au scan ; EAN-13, EAN-8, UPC-A, Code 128. |
| G15 | Quelles bornes pour l'antidatage ? | 30 jours, liste de types fermée, pas avant un comptage validé ; le propriétaire peut aller au-delà avec un second motif. |
| G16 | Que faire d'une sur-réception de transfert ? | Plafonner à la quantité expédiée ; surplus = écart d'inventaire ; annulation avant départ ; reliquat clôturé par la destination. |
| G17 | Comment traiter les achats livrés et facturés au-delà de la commande ? | Réception libre après confirmation, la facture fait foi, plusieurs factures possibles, reliquat clôturé explicitement. |
| G18 | Où vont les opérations reçues après une fusion de clients ? | Redirection automatique tracée vers la fiche cible. |
| G19 | Que faire après le refus d'une vente si la marchandise est présente ? | Libération explicite sur constat, sans mouvement de stock. |
| G20 | Quelles clés de correspondance pour le CSV ? | Colonne `ref_import`, UTF-8, `;` ou `,`, dates JJ/MM/AAAA, 5 000 lignes et 2 Mo maximum. |
| G21 | Quel critère de fidélité visuelle ? | Parité de parcours validée manuellement, sans seuil au pixel. |
| G22 | Quelles preuves pour la récupération exceptionnelle ? | Identité vérifiée, 3 faits du compte, confirmation d'un membre actif, puis délai de 72 h. |
| G23 | Fonds de caisse absent et catégories de dépenses ? | Fonds marqué « non renseigné », attendu incomplet ; catégories fixes traduites. |
| G24 | Transfert de propriété d'une boutique ? | Superadministrateur seulement, avec les preuves G22. |
| G25 | Notifications avec l'application fermée ? | WorkManager périodique et notification locale, sans FCM. |
| G26 | Références de reçu après réinstallation ? | Code d'installation attribué par le serveur et jamais réutilisé ; séries V et P distinctes. |

