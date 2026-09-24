# Bot de veille technologique — Ransomwares

Un bot qui lit une vingtaine de flux RSS de cybersécurité, garde uniquement les
articles qui parlent de ransomwares, supprime les doublons, et les publie
automatiquement **sur un salon Discord et/ou un canal Telegram**.

Il archive aussi chaque publication dans un fichier Markdown, pratique pour
alimenter ton Padlet / Teams et prouver la régularité de ta veille.

- Aucune bibliothèque à installer : **Python 3.9+ et rien d'autre**.
- Aucun service payant, aucune limite de quota.
- Tout le filtrage est dans `config.json`, modifiable sans toucher au code.

---

## 1. Installation

Télécharge le dossier, ouvre un terminal dedans, puis :

```bash
python veille.py --check-feeds
```

Cette commande teste chaque flux et affiche `OK` ou `ECHEC`. Un site change
parfois l'adresse de son flux : supprime simplement la ligne fautive dans
`config.json`.

Ensuite :

```bash
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

et remplis le `.env` en suivant la section 2 ou 3 (ou les deux).

---

## 2. Option A — Discord

Le plus simple : pas besoin d'héberger un bot, un **webhook** suffit.

1. Sur ton serveur Discord, crée un salon `#veille-ransomware`.
2. Clic droit sur le salon → **Modifier le salon** → **Intégrations** →
   **Webhooks** → **Nouveau webhook**.
3. Donne-lui un nom (« Veille ») puis **Copier l'URL du webhook**.
4. Colle cette URL dans `.env` :

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456.../abcdef...
```

> Cette URL est un mot de passe : n'importe qui peut poster dans ton salon avec.
> Ne la mets jamais dans un dépôt public ni dans ton dossier rendu au prof.

---

## 3. Option B — Telegram

1. Dans Telegram, cherche **@BotFather**, envoie `/newbot`, choisis un nom et un
   identifiant se terminant par `bot`. BotFather te renvoie un **token**.
2. Mets ce token dans `.env` :

```
TELEGRAM_BOT_TOKEN=8123456789:AAH...
```

3. Crée ton canal (ou groupe), puis **ajoute ton bot comme administrateur**, et
   poste n'importe quel message dedans.
4. Récupère l'identifiant du canal :

```bash
python veille.py --telegram-chatid
```

5. Copie le nombre affiché dans `.env` :

```
TELEGRAM_CHAT_ID=-1001234567890
```

---

## 4. Premier lancement

```bash
python veille.py --dry-run     # montre ce qui serait publié, sans rien envoyer
python veille.py --init        # marque l'existant comme « déjà vu »
python veille.py               # publie les nouveautés
```

L'étape `--init` évite de recevoir 200 notifications d'un coup au démarrage.
À partir de là, chaque exécution ne publie que ce qui est réellement nouveau.

### Les commandes utiles

| Commande | Effet |
|---|---|
| `python veille.py` | publie chaque article dans un message séparé |
| `python veille.py --digest` | publie **un seul** message récapitulatif (idéal le vendredi) |
| `python veille.py --dry-run` | affiche sans publier, pour tester tes mots-clés |
| `python veille.py --max 5` | limite à 5 articles pour cette exécution |
| `python veille.py --jours 30` | remonte jusqu'à 30 jours en arrière |
| `python veille.py --check-feeds` | vérifie que tous les flux répondent |
| `python veille.py --init` | remet le compteur à zéro sans publier |

---

## 5. Automatisation

### Windows (Planificateur de tâches)

1. Menu Démarrer → **Planificateur de tâches** → **Créer une tâche de base**.
2. Nom : « Veille ransomware ». Déclencheur : **Chaque semaine**, vendredi, 17:30
   (ou **Chaque jour** si tu préfères un suivi quotidien).
3. Action : **Démarrer un programme**
   - Programme : `python`
   - Arguments : `veille.py --digest`
   - Commencer dans : le chemin complet du dossier, ex.
     `C:\Users\Meryem\Documents\veille-bot`
4. Coche « Exécuter même si l'utilisateur n'est pas connecté » si tu veux qu'elle
   tourne PC allumé mais session fermée.

### Linux / macOS (cron)

```bash
crontab -e
```

```
30 17 * * 5 cd /chemin/vers/veille-bot && /usr/bin/python3 veille.py --digest
0 9 * * 1-5 cd /chemin/vers/veille-bot && /usr/bin/python3 veille.py
```

### GitHub Actions — la vraie solution « 24h/24, gratuite »

Ton PC n'a pas besoin d'être allumé. Le fichier
`.github/workflows/veille.yml` est déjà prêt.

1. Crée un dépôt GitHub **privé** et pousse ce dossier dedans.
2. Dans le dépôt : **Settings** → **Secrets and variables** → **Actions** →
   **New repository secret**. Ajoute `DISCORD_WEBHOOK_URL` et/ou
   `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.
3. Onglet **Actions** → active les workflows.

Le bot tournera tous les matins en semaine, plus un récapitulatif le vendredi à
17h30, et sauvegardera lui-même son fichier `state.json` dans le dépôt.

> Dépôt **privé** obligatoire si tu commites un `.env`. En réalité le `.env` est
> ignoré par `.gitignore` et les clés passent par les Secrets : c'est fait pour.

---

## 6. Régler le filtrage

Tout se passe dans `config.json`.

- **`mots_cles`** : un article doit contenir **au moins un** de ces mots pour être
  retenu. Les accents et les pluriels sont gérés tout seuls (`rançongiciel`
  correspond à `rancongiciel`, `ransomware` attrape `ransomwares`).
  Le mot doit être entier : `conti` ne déclenche pas sur « continue ».
- **`tags`** : ces mots ne suffisent pas à retenir un article, mais ils
  l'étiquettent et augmentent son score, donc sa priorité d'affichage.
- **`exclusions`** : élimine les articles promo (« bon plan », « code promo »…)
  que publient les sites d'actu généralistes.
- **`score_minimum`** : monte-le à 4 si tu reçois trop de bruit, descends-le à 1
  si tu ne reçois presque rien. Un mot-clé présent **dans le titre** vaut 3
  points, une simple mention dans le corps de l'article n'en vaut qu'un.
- **`anciennete_max_jours`** : ignore les articles plus vieux que X jours.
- **`flux`** : ajoute n'importe quel flux RSS. Pour suivre un nouveau sujet via
  Google Actualités, construis l'URL ainsi :

```
https://news.google.com/rss/search?q=TON+SUJET&hl=fr&gl=FR&ceid=FR:fr
```

Après chaque modification, teste avec `python veille.py --dry-run`.

---

## 6 bis. Fiches de curation (second salon Discord)

Si `DISCORD_FICHES_WEBHOOK_URL` est defini, le bot publie **une fiche par article**
dans un autre salon, au format du modele de cours (Titre, Source, Date, Resume,
Pourquoi cet article ?, Qualite de la source, Ce que j'ai appris).

Pre-rempli automatiquement : titre, source, date, auteur (si le flux l'indique),
actualite, pertinence (score et mots detectes) et un premier avis sur la fiabilite
et l'objectivite de la source (`types_source` dans `config.json`).
**A rediger toi-meme** : le resume, le « pourquoi » et « ce que j'ai appris ».

Mise en place : cree un salon, un webhook, puis ajoute le secret GitHub
`DISCORD_FICHES_WEBHOOK_URL` (et la ligne dans ton `.env` en local).

---

## 7. Archives pour ton dossier

Chaque exécution écrit un fichier `archives/AAAA-MM-JJ.md` contenant les titres,
sources, dates, tags et résumés. C'est du Markdown : tu peux le coller
directement dans Teams, Padlet ou un document Word, et il constitue une preuve
datée de ta veille régulière.

---

## 8. Dépannage

| Symptôme | Cause probable |
|---|---|
| `ECHEC` sur un flux | l'adresse a changé ou le site bloque : retire la ligne |
| `Rien de neuf à publier` | normal après un `--init` ; teste avec `--jours 30` |
| Discord `ERREUR HTTP 401/404` | webhook supprimé ou URL mal copiée |
| Telegram `ERREUR HTTP 400` | mauvais `TELEGRAM_CHAT_ID`, relance `--telegram-chatid` |
| Telegram `ERREUR HTTP 403` | le bot n'est pas administrateur du canal |
| Trop de messages | baisse `max_articles_par_execution` ou utilise `--digest` |
| Articles hors sujet | monte `score_minimum` ou enrichis `exclusions` |

---

## 9. Ce que ça n'est pas

Ce bot lit des flux RSS publics et republie des titres et des liens. Il ne
contourne aucun paywall et n'exécute rien d'offensif : c'est un outil de veille
défensive, exactement comme le Feedly et les Google Alertes de ton dossier, mais
en automatique et sans abonnement.
