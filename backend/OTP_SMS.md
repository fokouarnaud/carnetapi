# OTP SMS — transport et compteur locaux

État vérifié le 7 novembre 2026 : l’adaptateur Cooms, le lecteur de configuration
privée et le compteur d’admission sont testés localement. **Le raccord OTP,
les challenges, les endpoints API, l’authentification et le déploiement ne sont
pas réalisés.** Aucun SMS réel n’a été envoyé pendant cette qualification.

## Politique validée, à raccorder

Le code OTP SMS doit contenir **6 chiffres**, rester valable **10 minutes** et
autoriser **5 essais de vérification au maximum par code**. Après expiration ou
épuisement des essais, un nouveau code doit être demandé. Un renvoi consomme
le même plafond global de **10 envois par jour**, toutes boutiques, comptes et
installations confondus. Le jour du compteur est celui de `Africa/Douala`.

Ces règles de challenge et de vérification ne sont pas implémentées par
l’adaptateur HTTP. Une réponse du transport ne peut pas activer un compte ou
un téléphone. Le coût nul et la réception effective au Cameroun restent à
qualifier ; aucune facturation ni bascule payante n’est autorisée.

## Contrat Cooms qualifié localement

Le contrat fourni par le titulaire est :

| Élément | Valeur |
| --- | --- |
| Méthode | `POST` |
| Endpoint public | `https://gw-89.cooms.app/api/v1/sms/send` |
| Authentification | En-tête `x-api-key` |
| Type de contenu | `application/json` |
| Corps | `phone`, `message`, `priority` fixé à l’entier `1` |

Exemple de **structure uniquement**, sans code ni destinataire réel :

```json
{
  "phone": "+237<NEUF_CHIFFRES>",
  "message": "<TEXTE_OTP_LOCALISE>",
  "priority": 1
}
```

`identity.services.sms_gateway.SmsGateway` est le port injectable. Son
implémentation `CoomsSmsGateway` renvoie un `SmsSendResult` immuable :

| Statut | Sens et conduite |
| --- | --- |
| `accepted` | HTTP 2xx observé, motif `http_submission_only`. Aucune preuve de succès métier, livraison ou lecture. |
| `unavailable` | Configuration absente, entrée invalide, échec avant transmission ou refus HTTP 4xx hors 408. Aucun renvoi implicite. |
| `uncertain` | Transmission potentiellement commencée, délai dépassé, réponse trop grande, redirection ou autre réponse incertaine. Aucun renvoi implicite. |

La réponse JSON métier de Cooms n’est pas documentée. Le code ne déduit aucun
statut fournisseur de champs supposés tels que `success`. Une réponse 2xx
contenant un message métier négatif reste uniquement une soumission HTTP.

Le transport ne suit aucune redirection et n’effectue aucune nouvelle tentative.
Il vérifie HTTPS et le certificat avec le transport standard. La configuration
accepte un sous-domaine de `cooms.app`, le port 443 et exactement le chemin
`/api/v1/sms/send`, sans identifiants d’URL, paramètres ni fragment.

Les lectures de statut, en-têtes et corps partagent un délai d’E/S, configuré à
10 secondes par défaut et limité à 10 secondes. La résolution DNS du système
n’est pas garantie indépendamment par ce délai de socket. Le corps de réponse
est limité à 4 096 octets, plus un octet de détection, puis écarté. Les résultats
et erreurs exposent des codes fixes et éventuellement le statut HTTP, sans clé,
téléphone, texte OTP ni corps fournisseur.

## Configuration privée explicite

Le lecteur `config.cooms.cooms_settings(environ, secret_path=None)` accepte :

- un chemin de fichier JSON passé explicitement par `secret_path`, ou par la
  variable `APP_OTP_COOMS_CONFIG_FILE` ;
- les variables `COOMS_API_KEY` et `COOMS_SMS_SEND_URL`, prioritaires sur les
  valeurs du JSON.

Structure du fichier privé, **placeholders à remplacer hors du dépôt** :

```json
{
  "COOMS_API_KEY": "<CLE_PRIVEE_COOMS>",
  "COOMS_SMS_SEND_URL": "<ENDPOINT_HTTPS_COOMS_PUBLIC>"
}
```

L’endpoint attendu est celui indiqué dans le tableau du contrat. Le lecteur ne
cherche aucun fichier `.env` automatiquement et ne modifie pas l’environnement.
Sans configuration, il renvoie `None` et le transport reste indisponible. Une
configuration partielle, une URL interdite, des clés JSON inconnues ou dupliquées
sont refusées. Le fichier est limité à 16 384 octets ; les erreurs de lecture
n’exposent ni son contenu ni son chemin.

Conserver ce fichier privé hors Git et hors des journaux. Ne pas afficher la
clé, le contenu du JSON ni les variables de diagnostic. **Aucun raccord de ces
paramètres à `config/settings.py` ou à l’hébergement n’est installé par ce lot.**

## Compteur global et restauration

`identity.services.sms_budget.SmsBudgetStore` exige un chemin absolu vers un
**fichier SQLite de contrôle privé et dédié**, partagé par tous les processus
d’envoi. Ce fichier doit rester extérieur aux sauvegardes métier et à leur
restauration : restaurer la base commerciale ne doit pas remettre le compteur
d’envoi à zéro. Le choix de ce chemin et son raccord d’exploitation ne sont pas
encore déployés. Ne pas créer un compteur par boutique ou par processus.

Avant tout futur appel fournisseur, l’appelant devra valider sa demande puis
réserver un créneau avec un UUID d’opération et une empreinte canonique. Les
réservations sont persistantes et immuables. Les demandes concurrentes sont
sérialisées ; dix admissions au maximum sont accordées par jour Douala.

- `reserve()` renvoie un reçu et un booléen. Seul `True` autorise **un** appel
  fournisseur. Le même UUID et la même empreinte rendent le reçu initial avec
  `False` : aucun nouvel envoi, même après un timeout.
- Le même UUID avec une autre empreinte est refusé. Un renvoi explicite exige
  une nouvelle opération et consomme un nouveau créneau.
- Un échec fournisseur ne libère pas son créneau. Le passage au jour suivant
  n’autorise pas à réémettre une ancienne opération.
- Un contrôle absent ou non reconnu bloque l’admission ; il n’est jamais
  recréé implicitement. Un recul d’horloge détecté bloque les nouvelles admissions.

L’initialisation est une action d’exploitation explicite, avec identité
d’opérateur et motif. Elle n’écrase jamais un contrôle existant. Si l’historique
du contrôle a été perdu, `fresh_deployment=False` bloque les admissions jusqu’au
jour local suivant. `fresh_deployment=True` est réservé à un véritable premier
démarrage confirmé ; ne pas l’utiliser pour réparer ou remettre un quota à zéro.

Le compteur seul ne protège pas un appel HTTP effectué directement. **La liaison
réservation → challenge → appel unique reste à implémenter et qualifier.**

## Vérification locale sans envoi

Depuis la **racine du dépôt**, dans PowerShell avec l’environnement `.venv`
existant. Ces commandes ne lisent pas la configuration privée et ne contactent
pas Cooms. Le budget utilise uniquement des fichiers SQLite temporaires ; les
tests du transport utilisent des serveurs HTTP/TLS en boucle locale et des
secrets fictifs. Aucune base métier Django ni migration n’est nécessaire.

Les tests du budget importent le paquet `backend` depuis la racine :

```powershell
& .\.venv\Scripts\python.exe -m unittest backend.identity.tests.test_sms_budget -v
```

Les tests du lecteur et de la passerelle requièrent aussi `backend` dans
`PYTHONPATH`. Cette séquence restitue sa valeur précédente :

```powershell
$otpPreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = (Resolve-Path .\backend).Path
    & .\.venv\Scripts\python.exe -m unittest backend.config.tests.test_cooms backend.identity.tests.test_sms_gateway -v
} finally {
    $env:PYTHONPATH = $otpPreviousPythonPath
}
```

Commandes exécutées le 7 novembre 2026, code de sortie 0 : **11 tests budget** et
**22 tests configuration/transport**. Le lot pytest correspondant conserve
également 61 sous-tests de frontières. Les vérifications incluent le contrat de
requête, les redirections, les délais avec réponse lente, la réponse bornée,
TLS local, le refus d’un certificat non approuvé, les données sensibles et les
réservations concurrentes/idempotentes.

Les preuves locales sont dans `analysis/cooms-gateway-final-2026-11-07.xml`,
`analysis/sms-budget-pair-review-2026-11-07.xml` et
`analysis/cooms-gateway-qualification-2026-11-07.json` ; ces artefacts de
qualification ne constituent ni un déploiement ni une preuve de livraison SMS.
