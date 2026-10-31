# BitBench - Boutique STM32

Une boutique Flask minimaliste consacrée aux cartes de développement STM32 Nucleo
et Discovery, avec SQLite, un panier, la validation de commandes de démonstration,
les commandes clients et l'administration de la boutique.

**Il s'agit d'une démonstration universitaire indépendante, et non d'une boutique officielle de STMicroelectronics.**
Les prix et les stocks sont des exemples en USD. Aucun paiement réel n'est
encaissé, aucun appareil n'est expédié et les coordonnées de livraison utilisées
pour passer commande doivent être fictives.

## Démarrer le projet

L'environnement du projet est déjà disponible si vous avez utilisé la version précédente :

```powershell
cd "C:\Users\bizidabd\OneDrive - STMicroelectronics\Desktop\Mini Projet sesame"
.\.venv\Scripts\python.exe -m flask --app main run --debug
```

Ouvrez **http://127.0.0.1:5000**. Arrêtez le serveur avec Ctrl+C. Après avoir
mis le projet à jour, redémarrez-le et actualisez le navigateur avec Ctrl+Shift+R.

Pour une nouvelle installation, utilisez Python 3.10 ou une version ultérieure :

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m flask --app main run --debug
```

L'activation de l'environnement virtuel est facultative. Ces commandes fonctionnent
même si PowerShell bloque les scripts d'activation.

## Parcours de la boutique et de l'administration

1. Parcourez les six cartes STM32 d'exemple ; recherchez par carte/MCU ou filtrez par famille STM32.
2. Ajoutez au panier les cartes disponibles. Modifiez les quantités ou supprimez des articles.
3. Créez un compte ou connectez-vous avant de passer commande. Le panier invité est conservé après la connexion.
4. Saisissez des coordonnées de livraison fictives et choisissez **Place demo order** (Passer la commande de démonstration).
5. Consultez la confirmation et la page **My orders** (Mes commandes). Les prix sont enregistrés pour chaque commande ; les modifications ultérieures des prix des produits ne changent pas les commandes passées.

La validation de commande vérifie le stock et les prix côté serveur, déduit le
stock de manière atomique et empêche qu'un même formulaire de commande crée des
commandes en double. Le panier ne réserve pas de stock. Les quantités sont limitées
à 50 unités par produit et à 20 produits différents par panier. La livraison de
démonstration est gratuite.

Créez un compte administrateur de la boutique dans un autre terminal :

```powershell
.\.venv\Scripts\python.exe -m flask --app main create-admin
```

Connectez-vous avec ce compte et ouvrez **Sales dashboard** (Tableau de bord des ventes) ou
http://127.0.0.1:5000/admin/. Les administrateurs peuvent :

- Consulter le nombre de commandes, les commandes en attente, les cartes dont le stock est faible et la valeur totale des commandes (et non les recettes encaissées).
- Créer/modifier des produits STM32, les stocks, les descriptions et les **prix en centimes USD**. Par exemple, `2500` correspond à `$25.00`.
- Décocher **Listed** (Mis en vente) pour retirer un produit sans supprimer l'historique des commandes ou des firmwares.
- Ouvrir une commande et faire avancer son statut : `pending` (en attente) -> `processing` (en traitement) -> `shipped` (expédiée).
- Consulter les enregistrements de firmware. Le téléchargement de firmware nécessite toujours un compte authentifié et autorisé.

L'administration des produits et des commandes est réservée aux rôles autorisés ;
l'inscription ne donne jamais accès aux fonctions d'administrateur. Les lignes de
commande et les totaux ne peuvent pas être modifiés dans Flask-Admin.

## SQLite et migration de l'ancien projet

La base de données reste dans `instance/bitbench.db`. Au premier démarrage de cette version :

- Les comptes existants, les mots de passe, les quantités en stock et les liens de firmware sont conservés.
- Avant la migration d'un ancien fichier SQLite, une sauvegarde est créée dans
  `instance/bitbench.before-store.db`.
- Les colonnes de prix et de mise en vente ainsi que les tables de commandes sont ajoutées sans supprimer les anciennes tables.
- Les anciennes cartes non-STM32 sont conservées, mais masquées dans la boutique.
- La collection d'exemples STM32 est ajoutée une seule fois. Les redémarrages suivants ne réinitialisent pas les prix ou les stocks et ne recréent pas les produits d'exemple retirés ou supprimés.

Pour assurer cette compatibilité, le modèle Python conserve le nom `Board` et la
table d'origine `board`. Les pages publiques désignent ces enregistrements comme
des produits ou des cartes.

Les firmwares téléversés restent dans `instance/firmware/`. Les fichiers et leurs
métadonnées sont privés au compte qui les a téléversés et aux administrateurs.
Les extensions autorisées sont `.bin`, `.elf`, `.hex` et `.uf2` ; la requête
complète de téléversement est limitée à 10 Mo. Les fichiers sont stockés uniquement
en tant que données et ne sont jamais exécutés par cette application.

## API REST

| Méthode | Route | Accès / comportement |
| --- | --- | --- |
| GET | `/api/products` | Produits STM32 publiés accessibles au public, nombre de résultats et devise USD |
| GET | `/api/products/<id>` | Produit publié accessible au public, caractéristiques, stock et `price_cents` |
| POST | `/api/products` | Administrateur ; crée un produit STM32 après validation |
| PATCH | `/api/products/<id>` | Administrateur ; modifie les champs fournis |
| DELETE | `/api/products/<id>` | Administrateur ; retire le produit de la vente tout en conservant l'historique |
| GET | `/api/orders` | Commandes du client connecté |
| GET | `/api/orders/<id>` | Propriétaire de la commande ou administrateur |
| GET | `/api/csrf` | Jeton CSRF de la session connectée |

Les routes d'origine `/api/boards` et `/api/boards/<id>` restent disponibles
comme alias. De même, `/boards/<id>` ouvre toujours la page de détails du produit
correspondant. Les appels API protégés nécessitent une connexion (`401`) et les
opérations d'écriture réservées à l'administrateur nécessitent le rôle adéquat
(`403`). Les données invalides renvoient `400`, les noms de produits en double
`409`, et les produits absents ou non publiés `404`. Les jetons CSRF manquants ou
invalides renvoient également `400` ; la vérification CSRF a lieu avant les
contrôles d'authentification de la route.

Exemple de requête de lecture :

```powershell
curl.exe http://127.0.0.1:5000/api/products
```

Exemple de JSON produit pour une requête POST d'administrateur :

```json
{
  "name": "NUCLEO-F401RE",
  "manufacturer": "STMicroelectronics",
  "mcu": "STM32F401RET6",
  "architecture": "Arm Cortex-M4",
  "cores": 1,
  "clock_mhz": 84,
  "flash_kb": 512,
  "stock": 10,
  "price_cents": 2200,
  "description": "Carte de développement STM32.",
  "is_listed": true
}
```

Les opérations d'écriture de l'API utilisent une session d'administrateur connecté
et une protection CSRF. Après connexion, récupérez `/api/csrf` avec les mêmes
cookies de session et envoyez le jeton obtenu dans l'en-tête `X-CSRFToken`. Les
montants sont toujours stockés et transmis en centimes entiers, jamais en nombres
à virgule flottante.

## Exigences du projet Flask

| Exigence du cahier des charges | Implémentation |
| --- | --- |
| Accès REST aux données de la base | Routes CRUD des produits et routes JSON privées pour les commandes |
| Fichiers statiques | CSS local et illustrations de cartes dessinées en CSS ; aucun CDN nécessaire |
| Templates Jinja2 | Pages de la boutique, des produits, du panier, de la commande, du compte, des commandes et des ventes |
| Redirections | Connexion, ajout/modification/suppression du panier, commande, téléversements, déconnexion |
| Téléversement/téléchargement de fichiers | Stockage privé et autorisé des firmwares avec Flask-Reuploaded |
| Sécurité HTTP et plusieurs mécanismes de protection | Hachage des mots de passe, CSRF, contrôles des rôles et des propriétaires, templates avec échappement, restrictions de téléversement, nonces CSP et en-têtes de sécurité |
| Sessions utilisateur | Flask-Login avec des cookies de session signés et persistants |
| Rôles et Flask-Admin | Rôles client/administrateur, vues protégées sur les stocks et commandes, tableau de bord des ventes avec statistiques et statuts |
| Base de données SQLite | Produits, comptes, firmwares, commandes et lignes de commande |

**Compatibilité avec les extensions historiques :** Flask-Reuploaded fournit
l'API `flask_uploads.UploadSet` en remplacement de Flask-Uploads, qui n'est plus
maintenue. Le cahier des charges mentionne également Flask-User ; cette ancienne
extension n'est pas utilisée avec cette pile Flask/Python moderne. Flask-Login,
associé à des contrôles explicites des rôles et des propriétaires, répond plutôt
aux exigences liées aux comptes et aux sessions. Il s'agit de remplacements
fonctionnels, et non d'une affirmation selon laquelle la dépendance Flask-User
d'origine serait installée.

## Configuration et tests

En développement local, un secret aléatoire est généré une seule fois dans
`instance/secret.key`, afin que les sessions des comptes et des paniers restent
valides après un redémarrage. Une variable d'environnement `SECRET_KEY` déjà
définie est prioritaire. Conservez la même clé entre les redémarrages du serveur ;
ne la commitez pas et ne commitez pas non plus le dossier `instance`.

Pour un déploiement HTTPS, fournissez une valeur forte pour `SECRET_KEY`, définissez
`HTTPS_ONLY=1`, désactivez le mode debug et utilisez un serveur WSGI de production
derrière HTTPS. Ne définissez pas `HTTPS_ONLY=1` pour le développement local HTTP
ordinaire. `DATABASE_URL` peut remplacer l'URL SQLAlchemy ; la migration automatique
des anciennes bases est spécifique à SQLite.

Exécutez les tests :

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Les tests utilisent des bases de données isolées et des dossiers temporaires pour
les téléversements. Ils couvrent le filtrage du catalogue, les prix en centimes
entiers, les paniers, les contrôles de stock et leur annulation en cas d'échec,
la répétition de la validation de commande, les commandes privées, les permissions
et formulaires d'administration, l'accès aux firmwares, CSRF, les en-têtes de
réponse, l'inscription et la migration répétable du schéma SQLite d'origine.

Organisation du code :

- `main.py` : routes Flask, panier et commandes, API REST et administration.
- `models.py` : modèles SQLAlchemy, migration SQLite et produits d'exemple.
- `forms.py` : validations partagées des comptes, quantités et commandes.
- `templates/` : pages Jinja et fragments réutilisables.
- `static/style.css` : design minimaliste et adaptatif de la boutique.
- `tests/test_app.py` : tests de comportement et de régression des données historiques.
