# Apps Django par domaine

Le backend est organisé en sept apps Django métier enregistrées dans `config/settings.py`, accompagnées de l’app technique `backups` pour les sauvegardes globales.
Chaque app possède ses modèles, ses services, son API avec ses propres URLs, ses
tests et son répertoire de migrations. Les calculs et commandes existants sont
déplacés sans changement des règles métier.

| App | Responsabilité | Lignes du fichier modèles après découpage |
| --- | --- | ---: |
| `identity` | Comptes, boutiques, équipes, sessions, accès et préférences | 137 |
| `catalogue` | Produits, catégories, photos, conditionnements, codes et imports | 144 |
| `stock` | Stock, réservations, alertes, inventaires, pertes et valorisation | 419 |
| `sales` | Ventes, lignes facturées, moyens déclarés, annulations et retours | 148 |
| `customers` | Clients, créances, encaissements, avoirs et relevés | 234 |
| `purchasing` | Fournisseurs, achats, réceptions, paiements, retours et frais | 352 |
| `cash` | Mouvements de caisse et comptages | 76 |
| `backups` (technique) | Archives, commandes, essais SMTP, nettoyage, abandon, configuration quotidienne et Unfold | 227 |

Structure d'une app :

```text
stock/
├── apps.py
├── models.py
├── services/
├── api/
│   └── urls.py
├── tests/
└── migrations/
```

`config` reste la configuration du projet et inclut les URLs des apps. `common`
contient les champs API stricts, le contexte d'autorisation, les reçus idempotents,
les utilitaires et la base UUID abstraite. Le journal transversal `Activity` y est
défini. Les services importent les modèles depuis leur domaine, sans passer par
une collection de modèles dans `core`.

Le modèle utilisateur est défini dans `identity/models.py`, avec son identité
Django historique `core.User`. Le journal conserve aussi l'identité `core.Activity`.
Les autres 67 modèles ont le label de leur nouvelle app. Les relations utilisent
des références différées qualifiées pour éviter les imports circulaires.

## Transition des bases existantes

`core` est conservé pour les migrations historiques, les traductions partagées et
ces deux identités stables. Ses trois modules racine sont `__init__.py`, `apps.py`
et `models.py`. Le dernier est uniquement une façade d'import de compatibilité de
89 lignes ; il ne contient aucune définition de modèle métier.

Les migrations `0001` à `0079` sont conservées octet pour octet. La migration
`0080` déjà ajoutée pour les libellés bilingues de l'administration est également
conservée. Leurs empreintes sont dans `analysis/backend-migration-history.json`.

La nouvelle migration `core.0081_domain_apps` déplace l'état Django des modèles
et les labels des types de contenu, sans créer, supprimer ou renommer les tables
métier `core_*`. Les noms des index et contraintes restent ceux de l'historique.
Les IDs de types de contenu, permissions, groupes et références du journal admin
sont conservés. Une collision de types de contenu arrête la migration dans une
transaction ; elle exige une résolution explicite.

Les migrations initiales des sept apps dépendent de cette transition atomique et
ne recréent aucune table. Les prochaines migrations métier sont générées dans
l'app concernée avec les commandes Django habituelles. Ne pas remplacer cette
transition par `--fake`, supprimer des migrations ou changer `AUTH_USER_MODEL`.

Les empreintes de valorisation utilisent leurs identités historiques : déplacer
une classe n'invalide pas les aperçus et décisions déjà enregistrés. Les chemins
et noms des 121 routes API existantes sont conservés. Les URLs de l'administration
des boutiques et appartenances utilisent désormais le label `identity`.

## Intégrité du stock après la transition

`stock.0002_stockmovement_stock_movement_integer_quantity_and_more` ajoute deux
contraintes sur les mouvements : quantité canonique entière en milli-unités et
coût connu entier en micros FCFA. Les quantités signées restent possibles ; le
coût inconnu reste NULL et demeure distinct d'un coût nul. Les agrégats peuvent
toujours dépasser int64 en additionnant des lignes individuellement valides.

Les lignes historiques valides sont conservées sans modification. Si une ancienne
ligne contient une fraction, du texte ou un flottant hors capacité de stockage,
la migration échoue et sa transaction est annulée, sans arrondi, suppression ou
substitution de coût automatique. Les tests de migration de stock couvrent les
valeurs conservées et l'échec sans application partielle. Le test de transition
des apps reste fixé à 0081/0001, afin de vérifier le découpage indépendamment des
changements de schéma métier ultérieurs.

## Signalement des clients possibles doublons

`ClientSerializer.has_possible_duplicate` est un indicateur calculé dans SQL,
sans nouveau champ persistant. Il signale les autres fiches connues de la même
boutique ayant exactement le même nom enregistré ou le même téléphone non vide.
Un téléphone NULL ou vide ne crée pas de correspondance à lui seul. Aucun
rapprochement approximatif, refus de création ou fusion automatique n'est ajouté.

`GET /api/v1/shops/{shop}/clients/{client}/possible-duplicates/` retourne les
correspondances autorisées, avec pagination SQL, sans la fiche source, sans
répétition pour une fiche correspondant aux deux critères et sans données de
dettes ou d'encaissements. L'UUID et les liens des opérations restent inchangés.
Les réessais conservent leur reçu historique ; les lectures courantes recalculent
l'indicateur, notamment après une correction. Les 121 routes de compatibilité
antérieures restent inchangées ; cette nouvelle route porte le total à 122.

La migration `customers.0002_customer_customer_shop_name_match_and_more` ajoute
deux index non uniques `(shop, name)` et `(shop, phone)`. La création, correction
et demande de correction vérifient la session exacte dans leur transaction,
avant tout accès à un reçu d'opération sauvegardé. Les imports CSV transmettent
également cette identité de session. La consultation exige les droits actuels,
sans accorder de permission de crédit ou de fusion.

## Sauvegardes globales

L’app technique `backups` réserve ses API et ses formulaires Unfold au
superadministrateur technique. Les modèles `BackupJob`, `BackupPart` et
`BackupEvent` ne sont pas des données d'une boutique et ne donnent aucun accès
implicite au propriétaire. Les nouvelles tables utilisent le label `backups` ;
aucune migration historique de `core` n’est réécrite.

La préparation produit un instantané SQLite cohérent incluant les photos BLOB,
compresse et chiffre des parties bornées, vérifie l’ensemble, puis enregistre les
métadonnées. Les fichiers chiffrés sont conservés ; l’état « préparée, non
envoyée » ne vaut pas acceptation SMTP. Le format, les droits, la procédure de
clé séparée, la vérification sans Django et les critères encore ouverts sont
détaillés dans [BACKUPS.md](BACKUPS.md).

## Vérification locale

Depuis la racine du dépôt, avec les dépendances de développement installées :

```powershell
./.venv/Scripts/python.exe -X utf8 -m pytest -q --tb=short --show-capture=no
```

Depuis `backend`, les contrôles isolés utilisent des bases temporaires :

```powershell
../.venv/Scripts/python.exe manage.py check --settings=config.test_settings
../.venv/Scripts/python.exe manage.py makemigrations --check --dry-run --settings=config.test_settings
../.venv/Scripts/python.exe manage.py spectacular --validate --fail-on-warn --file schema.yaml --settings=config.test_settings
../.venv/Scripts/python.exe manage.py test verification.concurrency_checks verification.stock_valuation_migration_checks --settings=config.concurrency_test_settings --noinput
../.venv/Scripts/python.exe -m verification.stock_alert_smoke
```

Les tests de `common/tests/test_domain_migration.py` vérifient une base remplie,
le passage aller-retour, les tables et lignes, les permissions, les liens du
journal, les contraintes, la disponibilité après réservation et les empreintes
de coût. Les tests de routes comparent le contrat HTTP effectif, y compris la
liaison automatique de HEAD à GET effectuée par DRF.

Cette réorganisation ne constitue pas la livraison complète des User Stories.
Les gates Android, hors connexion, synchronisation, OTP, sauvegardes et hébergement
restent à réaliser et qualifier selon le PRD.
