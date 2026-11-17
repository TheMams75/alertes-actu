# Alertes actu critiques

Toutes les 10 minutes environ, GitHub Actions lit les flux Al Jazeera, Reuters et AP, repère les infos critiques et les envoie sur Telegram. Gratuit (dépôt public).

## Mise en route

1. Dans Telegram, ouvre **@BotFather**, envoie `/newbot`, choisis un nom. Il te donne un **token**.
2. Ouvre ton nouveau bot et envoie-lui `/start`.
3. Sur GitHub, dans ce dépôt : **Settings > Secrets and variables > Actions > New repository secret**
   - `TELEGRAM_BOT_TOKEN` = le token donné par BotFather
4. Onglet **Actions > Alertes actu critiques > Run workflow**, mode `chat-id`. Le bot t'envoie ton numéro de conversation.
5. Ajoute le secret `TELEGRAM_CHAT_ID` avec ce numéro.
6. Relance le workflow en mode `test`, puis en mode `run`. Le premier passage mémorise les articles sans rien envoyer et confirme l'activation.

## Réglages (`config.json`)

- `seuil_alerte` : plus il est haut, moins tu reçois d'alertes (8 par défaut).
- `mots_cles` : mots qui font monter l'indice, groupés par poids. Un mot du groupe 8 suffit à déclencher une alerte. Un `*` final accepte toutes les terminaisons (`crash*` = crashes, crashed).
- `acteurs` : pays, institutions et entreprises qui ajoutent un point.
- `exclusions` : un titre qui contient un de ces mots n'est jamais envoyé.
- `flux` : ajoute ou désactive une source (`"actif": false`).

Le mode `dry-run` montre dans les journaux ce qui serait envoyé, sans rien envoyer.

## Comment l'indice est calculé

Chaque mot-clé trouvé dans le titre compte son poids, la moitié s'il est seulement dans le résumé. Les acteurs ajoutent jusqu'à 2 points. Si au moins deux sources parlent du même événement, +2. Une même info n'est envoyée qu'une fois sur 12 heures.

## Sources

- Al Jazeera : flux officiel `aljazeera.com/xml/rss/all.xml`
- Reuters : n'a plus de flux RSS public depuis 2020, lu via une recherche Google News limitée à reuters.com
- AP : n'a plus de flux sur apnews.com, lu via son compte Flipboard officiel et Google News
