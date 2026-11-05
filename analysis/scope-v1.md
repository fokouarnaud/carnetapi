# Périmètre de première livraison — synthèse à confirmer

Synthèse des décisions Q1–Q180 consignées dans `../AGENTS.md`. Le registre détaillé reste la référence pour les exceptions et incertitudes. **Q181 confirmée par le titulaire le 11 octobre 2026.** Cette confirmation fixe le périmètre ; elle ne prouve pas encore sa faisabilité technique ou sa couverture complète de YemX.

## Produit et budgets

Application autonome Android, écrans et navigation proches de YemX, interface et documents français/anglais dès la première livraison. Nom et identifiant définitifs à choisir plus tard. Tous les modules accessibles sans abonnement ni quotas commerciaux. Client Flutter avec SQLite local ; backend Django + SQLite, administration Unfold. APK signé distribué directement au pilote, publication Google Play ultérieure. Android minimum API 24, cible actuelle API 36 à revalider avant publication.

Pilote initial, selon l’arbitrage du titulaire du 24 octobre 2026 : **2 boutiques**, 3 appareils par boutique, soit **6 appareils actifs**, scénario avec un propriétaire et deux vendeurs par boutique. Cibles de test conservées : 100 ventes par boutique/jour, 5 lignes par vente, 1 000 produits par boutique et horizon d’un an, soit environ **73 000 ventes, 365 000 lignes et 2 000 produits**. Dix boutiques et trente appareils sont reportés à une phase ultérieure. Ces chiffres ne limitent pas les utilisateurs commercialement et n’autorisent aucune suppression automatique d’historique. APK de production au plus 50 Mo ; backend installé initialement **moins de 500 Mo décimaux**, incluant code, Python, dépendances, SQLite, photos et fichiers générés, hors OS. Ces budgets sont à mesurer et ne sont pas démontrés par les quotas d'hébergement.

## Compte, droits et synchronisation

UUID pour les entités persistantes et opérations synchronisées ; horodatages conservés en UTC, affichage et périodes convertis dans le fuseau choisi. Identité d'une opération distincte de son numéro de reçu lisible. Synchronisation idempotente sans duplication lors des reprises.

OTP SMS au Cameroun pour inscription, nouvel appareil et récupération, seulement si coût nul ; quota applicatif global 10 envois/jour incluant renvois. Fournisseur et garantie de gratuité restent à vérifier. PIN local de 6 chiffres, blocage 15 minutes après 5 erreurs, sans effacement des opérations. Le YemX observé utilise 4 chiffres : conserver la différence choisie jusqu'à décision explicite.

Accès hors connexion pendant 7 jours depuis la dernière vérification serveur des droits pour le compte, la boutique et l'installation. Expiration : lecture des données autorisées en cache, nouvelles opérations bloquées, reconnexion nécessaire sans SMS systématique. Retrait reçu : bloquer les nouvelles opérations ; conserver celles produites pendant la déconnexion et demander leur validation au responsable. Montants validés/en attente séparés ; unités en attente réservées une seule fois. Refus historisé avec motif et régularisation explicite, sans remboursement ni remise en stock automatiques.

Permissions configurables par vendeur : nouveau crédit désactivé par défaut ; encaissement de dettes activé par défaut ; consultation des dettes seule désactivée par défaut ; rappels chiffrés désactivés par défaut. Crédit, encaissement ou rappels impliquent la consultation nécessaire, sans accorder implicitement les autres droits. Coûts et marges cachés par défaut, contrôlés aussi dans l'API et les données téléchargées. Modifications d'équipe et de droits uniquement connectées et confirmées par le serveur.

## Catalogue, ventes et retours

Responsable gérant catalogue, coûts, photos et corrections historisées. Une photo facultative compressée et synchronisée par produit, cible 20 ko maximum à vérifier visuellement. Quantités à 3 décimales pour poids/volume ; entiers pour pièces. Plusieurs conditionnements nommés par le responsable (carton, casier, paquet…), conversion vers unité de base et prix propres, données facturées figées dans l'historique. Code-barres facultatif utilisable hors connexion.

Ventes comptant et à crédit, paiements fractionnés par moyen réel déclaré, sans intégration automatique MTN/Orange/banque. Remises vendeur plafonnées par boutique, défaut 0 %. Remise globale répartie au prorata et arrondis FCFA entiers stables. Conserver les ventes et encaissements réels concurrents, même si stock négatif ; le responsable régularise.

Annulations, corrections et retours partiels réservés au responsable, avec motif et historique. Retours valorisés au prix net réellement facturé ; réduire d'abord la dette de la vente, conserver les anciens encaissements, traiter explicitement l'excédent. Réintégration du stock seulement après retour confirmé vendable. Retours non vendables suivis séparément ; remboursement réel distinct de l'avoir ou de la réduction de dette.

Stock initial, inventaires et pertes historisés. Ventes possibles pendant un inventaire, avec heure de comptage par produit et rapprochement des arrivées tardives. Transferts entre boutiques du même propriétaire avec départ, transit, réceptions partielles et écarts explicites. Catalogues, clients et fournisseurs restent distincts par boutique.

Précisions du titulaire du 26 octobre 2026 pour US-16/U105 : à chaque réception
partielle, le responsable destinataire choisit explicitement une fiche produit
existante avec la même unité de base. Ce choix peut différer d'une réception
partielle à la suivante ; aucun rapprochement automatique ni correspondance
figée par la première réception. Chaque réception conserve la fiche et la
quantité reçue ; le reliquat reste suivi sur l'article d'origine du transfert.
Ces décisions ne démontrent pas encore l'implémentation des transferts.

## Clients, fournisseurs, achats et caisse

Fiche client avec nom obligatoire et téléphone facultatif ; vendeur pouvant créer et signaler une correction, responsable modifiant/fusionnant avec historique. Dettes d'ouverture, échéance facultative et relevés chronologiques. Encaissement affecté explicitement aux dettes, proposition des plus anciennes d'abord. Trop-perçu conservé et traité par le responsable. Rappels WhatsApp préparés manuellement après synchronisation, envoyés par l'utilisateur, sans preuve automatique de réception ni envoi SMS automatique.

Achats, réceptions partielles, paiements fournisseurs et retours sous contrôle du responsable. Dette selon facture validée, stock selon réception réelle, paiement et avance distincts. Soldes fournisseurs d'ouverture repris séparément, sans achat ou mouvement de caisse fictif.

Caisse commune par boutique avec attribution utilisateur/appareil et ventilation des moyens. Fonds initial, apports, retraits, dépenses et comptages physiques séparés du chiffre d'affaires. Pas de remise à zéro quotidienne. Monnaie due au client suivie comme obligation distincte. Comptage provisoire si opérations hors connexion manquantes ; rapprochement sans altération du comptage historique ni double correction.

Marge brute **estimée** sur ventes nettes moins coût moyen pondéré, sans présenter un bénéfice net. Coût inconnu signalé. Frais directs d'achat répartis au prorata, ajustables avec motif ; frais tardifs recalculant coûts et marges concernés, historique des révisions et PDF anciens conservés. Propagation aux transferts, retours et pertes à concevoir et tester.

## Documents, API et exploitation

Reçus individuels PDF sur téléphone, partage et impression Android ; impression thermique directe reportée. Rapports globaux réservés au responsable connecté avec synchronisation préalable, état des données incomplètes visible. Périodes locales converties en bornes UTC. Rapports volumineux répartis en parties numérotées, sans omission ou doublon. Consolidation des boutiques du propriétaire avec montants validés/en attente séparés.

Django REST Framework, SimpleJWT (accès 5 minutes, refresh 30 jours renouvelé par rotation, blacklist des anciens refresh), vérification serveur des droits actuels. Pagination côté base, filtrage des boutiques autorisées avant pagination, ordre stable, tailles bornées, index et totaux sur tout le filtre. UUID ne remplace pas l'autorisation. OpenAPI via drf-spectacular, Swagger UI et ReDoc, schéma et documentation de production réservés au superadministrateur technique.

PythonAnywhere gratuit au départ, renouvellement mensuel manuel du site. cron-job.org envisagé pour appels HTTP planifiés ; un appel ou reload ne garantit pas le renouvellement du site. Aucune automatisation de production configurée. SMTP Gmail de l'expéditeur demandé ; paramètres secrets encore à fournir hors dépôt.

Sauvegarde SQLite + photos cohérente, chiffrée avec clé distincte conservée hors archive, envois email par parties annotées de 15 Mo maximum et vérification d'intégrité à l'import Unfold. Automatique quotidien par défaut à 02:00 Douala, envoi à l'expéditeur lui-même, désactivable ; envoi manuel à destinataire configurable. Reprise des seules parties échouées, maximum trois tentatives supplémentaires, pas de nouvelle sauvegarde automatique tant que la précédente reste incomplète. Restauration de secours en console prévue si Django indisponible. Réconciliation après restauration par UUID, révalidation des appareils et clôture explicite de la récupération.

Réinstallation : récupérer données serveur et droits actuels après Internet/OTP, nouvelle installation. Données jamais synchronisées perdues sans sauvegarde externe. Sauvegarde locale manuelle chiffrée exportable hors de l'application, mot de passe choisi distinct du PIN, import sans duplication ni restauration de droits périmés. Codes de récupération à usage unique ; perte du numéro et des codes exige vérification exceptionnelle par le superadministrateur technique, méthode encore ouverte.

## Conditions avant architecture et livraison

Précision US-13 validée dans le chat le 12 octobre 2026 : lorsqu'une opération antérieure reçue tardivement révèle une correction d'inventaire déjà appliquée trop forte ou trop faible, proposer un ajustement complémentaire. Seul le responsable le confirme avec motif ; conserver le comptage physique, la correction initiale et chaque décision suivante. Aucune compensation automatique de la double déduction.

Précision US-30 validée par le titulaire le 12 octobre 2026 dans le chat : le cumul des retours fournisseur d'une ligne d'achat est limité aux quantités réellement reçues sur cette ligne, moins les retours déjà confirmés. La quantité commandée ou facturée seule ne permet pas un départ supplémentaire. Cette précision conserve la séparation entre départ physique, avoir réellement accepté et remboursement effectivement reçu.

Précision d'arrondi validée ensuite dans le même chat pour les retours fournisseur : arrondir au FCFA le plus proche, moitié vers le haut, la valeur du cumul retourné par ligne ; attribuer à chaque retour la différence entre ce cumul arrondi et le cumul précédent. Le retour intégral retrouve exactement le total net de la ligne d'origine. La valeur de référence et le montant d'avoir réellement accepté restent distincts et conservés avec motif.

Précision confirmée à Q182 : pour les quantités au poids/volume, arrondir le montant complet de chaque ligne au FCFA le plus proche, moitié vers le haut, avant les remises. Exemple : 0,5 kg à 249 FCFA/kg donne une ligne de 125 FCFA. Ne pas arrondir séparément chaque fraction d'unité.

Confirmer cette synthèse, comparer les parcours réels de YemX et identifier les ajouts explicites. Mesurer taille installée, charge SQLite/PythonAnywhere et espace temporaire des sauvegardes. Vérifier gratuité effective OTP, accès SMTP et planification. Concevoir et tester conflits, restauration, coût historique et isolation des boutiques. Les tests effectués dans la cible sont décrits dans `runtime/install-and-smoke-report.md` ; ils ne valident pas une réplique encore non implémentée.

## Décision US-11 — unicité des codes, 13 octobre 2026

Le titulaire a choisi dans le chat : « Refuser le doublon à la saisie ; signaler les conflits reçus hors connexion. » L'unicité est par boutique et couvre les codes d'unités et de conditionnements ensemble. Un code ne doit pas sélectionner silencieusement une autre cible. Ce choix ne définit pas encore les formats de caméra ni le protocole d'ingestion des propositions hors connexion ; ces obligations restent au périmètre.
