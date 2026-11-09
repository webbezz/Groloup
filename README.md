# Groloup fréquente Didi — déploiement sur le VPS

Le jeu (`static/index.html`) et le tableau d'honneur (API `/api/scores`, base SQLite) roulent dans un seul conteneur Docker.
Dossier sur le VPS : `/opt/groloup`

## 1. Installer sur le VPS

Le code vit dans un dépôt GitHub; Claude y pousse les mises à jour.

    git clone https://github.com/webbezz/Groloup.git /opt/groloup

Si le port 8076 est déjà pris : créer `/opt/groloup/.env` avec `GROLOUP_PORT=8077` (ou autre).

## 2. Démarrer

    cd /opt/groloup
    docker compose up -d --build
    curl -s http://127.0.0.1:8076/api/scores    # doit répondre []

Le conteneur écoute seulement en local (port 8076 par défaut, voir `.env` plus haut).
Il faut un reverse proxy pour l'exposer.

## 3. Reverse proxy (exemple nginx + sous-domaine)

Fichier `/etc/nginx/sites-available/groloup` :

    server {
        listen 80;
        server_name groloup.TON-DOMAINE.com;

        location / {
            proxy_pass http://127.0.0.1:8076;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }

Puis :

    ln -s /etc/nginx/sites-available/groloup /etc/nginx/sites-enabled/
    nginx -t && systemctl reload nginx
    certbot --nginx -d groloup.TON-DOMAINE.com

(Avec Caddy : `groloup.TON-DOMAINE.com { reverse_proxy 127.0.0.1:8076 }`.
Avec un tunnel Cloudflare : pointer le hostname vers `http://127.0.0.1:8076`.)

## Mises à jour automatiques

Une tâche cron de l'utilisateur debian fait un pull chaque minute :

    * * * * * git -C /opt/groloup pull --ff-only -q >> /home/debian/groloup-pull.log 2>&1

Le dossier `static/` est monté dans le conteneur : une nouvelle version du jeu est en ligne dès le pull, sans rebuild.
Le cron ne rebuild jamais. Un changement à `app.py`, `Dockerfile`, `requirements.txt` ou `docker-compose.yml`
demande un `docker compose up -d --build` fait à la main.

Les records restent dans `data/scores.db` (volume monté, ignoré par git), ils ne sont jamais perdus.

## Statistiques

Le jeu envoie quelques événements anonymes au serveur : ouverture du jeu, nouvelle partie ou reprise,
visite terminée, grande demande, finale, score enregistré. Chaque navigateur reçoit un identifiant
au hasard (`gd_vid`); aucune adresse IP n'est gardée avec ces événements.

La page `https://groloup.webbezai.org/stats` est protégée par un mot de passe défini dans `/opt/groloup/.env` :

    STATS_PASS=un-mot-de-passe

puis `docker compose up -d --build`. Sans `STATS_PASS`, la page n'existe pas (404).
Le navigateur demande un nom d'utilisateur (n'importe lequel) et le mot de passe.

En bas de la page, un bouton exclut l'appareil courant des chiffres (pour ne pas compter tes propres parties).

Tout en bas, « Remise à zéro » : un bouton efface les scores, l'autre efface les statistiques (les appareils exclus restent exclus). Avant chaque effacement, une copie de la base est faite dans `/opt/groloup/data/sauvegardes/` (les 10 plus récentes sont gardées).

## Gérer le tableau d'honneur

    sqlite3 data/scores.db "SELECT nom, points, visites FROM scores ORDER BY points DESC;"
    sqlite3 data/scores.db "DELETE FROM scores WHERE nom = 'Test';"      # enlever une entrée
    sqlite3 data/scores.db "DELETE FROM scores;"                         # tout remettre à zéro
    cp data/scores.db data/scores-$(date +%F).db                         # copie de sécurité

## Notes

- Un même appareil (IP) peut envoyer un score aux 10 secondes au maximum.
- Si l'API ne répond pas (ex. la version publiée sur claude.ai), le jeu garde les records sur l'appareil.
- Les badges restent sur chaque appareil (navigateur).
