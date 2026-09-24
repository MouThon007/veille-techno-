#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
veille.py - Bot de veille technologique automatisee.

Lit des flux RSS/Atom, filtre les articles par mots-cles, supprime les doublons
et publie le resultat sur Discord (webhook) et/ou Telegram (bot API).

Aucune dependance externe : uniquement la bibliotheque standard de Python 3.9+.

Usage rapide :
    python veille.py --check-feeds     # teste que tous les flux repondent
    python veille.py --init            # marque l'existant comme "deja vu" (1re fois)
    python veille.py --dry-run         # affiche ce qui serait publie, sans publier
    python veille.py                   # publie les nouveautes
    python veille.py --digest          # publie un recap groupe (veille hebdo)
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 VeilleBot/1.0")

DISCORD_COLORS = {"haut": 0xE74C3C, "moyen": 0xE67E22, "bas": 0x3498DB}


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_dotenv(path):
    """Charge un fichier .env simple (CLE=valeur) dans os.environ."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


def normalize(text):
    """Minuscules + suppression des accents, pour comparer sans se soucier
    de 'rancongiciel' vs 'rancongiciel'."""
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text.lower())


TAG_RE = re.compile(r"<[^>]+>")
ENTITY_RE = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|\w+);")


def strip_html(text):
    """Retire les balises HTML et decode les entites courantes."""
    if not text:
        return ""
    import html as _html
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text,
                  flags=re.S | re.I)
    text = TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "\u2026"


def build_pattern(keyword):
    """Construit une regex a frontieres de mots, tolerante au pluriel.
    'ransomware' matche ransomware/ransomwares mais 'conti' ne matche
    jamais 'continue'."""
    escaped = re.escape(normalize(keyword)).replace(r"\ ", r"[\s\-]+")
    return re.compile(r"\b" + escaped + r"(?:s|es|x)?\b")


# --------------------------------------------------------------------------
# Lecture des flux RSS / Atom / RDF
# --------------------------------------------------------------------------

def local_name(tag):
    return tag.split("}")[-1].lower()


def child_text(entry, *names):
    for child in entry:
        if local_name(child.tag) in names:
            if child.text and child.text.strip():
                return child.text.strip()
    return ""


def extract_link(entry):
    fallback = ""
    for child in entry:
        if local_name(child.tag) != "link":
            continue
        if child.text and child.text.strip().startswith("http"):
            return child.text.strip()
        href = child.attrib.get("href", "")
        rel = child.attrib.get("rel", "alternate")
        if href and rel == "alternate":
            return href
        if href and not fallback:
            fallback = href
    return fallback or child_text(entry, "guid", "id")


def extract_author(entry):
    """Auteur d'un article (RSS: dc:creator/author, Atom: author/name)."""
    for child in entry:
        if local_name(child.tag) in ("creator", "author"):
            if child.text and child.text.strip():
                return strip_html(child.text)
            for sub in child:
                if local_name(sub.tag) == "name" and (sub.text or "").strip():
                    return sub.text.strip()
    return ""


def parse_date(entry):
    raw = child_text(entry, "pubdate", "published", "updated", "date",
                     "created")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_feed(raw_bytes, source):
    """Retourne une liste d'articles. Gere RSS 2.0, RDF et Atom."""
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError:
        # Certains flux ont du bruit avant la declaration XML
        start = raw_bytes.find(b"<?xml")
        if start > 0:
            root = ET.fromstring(raw_bytes[start:])
        else:
            raise

    entries = [el for el in root.iter()
               if local_name(el.tag) in ("item", "entry")]
    articles = []
    for entry in entries:
        title = strip_html(child_text(entry, "title"))
        link = extract_link(entry).strip()
        if not title or not link:
            continue
        summary = strip_html(child_text(
            entry, "description", "summary", "content", "encoded"))
        articles.append({
            "titre": title,
            "lien": link,
            "resume": summary,
            "date": parse_date(entry),
            "auteur": extract_author(entry),
            "source": source,
        })
    return articles


# --------------------------------------------------------------------------
# Filtrage et scoring
# --------------------------------------------------------------------------

def analyse(article, required, bonus, exclusions):
    """Retourne (score, tags) ou (0, []) si l'article est rejete."""
    blob = normalize(f"{article['titre']} {article['resume']}")
    titre = normalize(article["titre"])

    for pattern in exclusions:
        if pattern.search(blob):
            return 0, []

    hits = {label for label, pattern in required if pattern.search(blob)}
    if not hits:
        return 0, []

    score = len(hits)
    # Un mot-cle dans le titre pese plus lourd qu'une mention en fin d'article
    if any(pattern.search(titre) for _, pattern in required):
        score += 2

    tags = []
    for categorie, items in bonus.items():
        for label, pattern in items:
            if pattern.search(blob):
                tags.append(label)
                score += 1
    return score, sorted(set(tags))


def niveau(score):
    if score >= 6:
        return "haut"
    if score >= 3:
        return "moyen"
    return "bas"


def article_id(article):
    link = article["lien"].split("?")[0].rstrip("/")
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]


def title_id(article):
    titre = normalize(article["titre"])[:70]
    return "t" + hashlib.sha1(titre.encode("utf-8")).hexdigest()[:15]


# --------------------------------------------------------------------------
# Publication
# --------------------------------------------------------------------------

def post_json(url, payload, timeout=25):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def envoyer_discord(webhook, articles, digest, titre_digest):
    """Discord accepte 10 embeds par message maximum."""
    if digest:
        lignes = []
        for art in articles:
            tags = " ".join(f"`{t}`" for t in art["tags"][:4])
            lignes.append(
                f"**[{truncate(art['titre'], 150)}]({art['lien']})**\n"
                f"{art['source']} \u2022 {art['date_txt']} {tags}"
            )
        description = truncate("\n\n".join(lignes), 4000)
        embed = {
            "title": titre_digest,
            "description": description,
            "color": DISCORD_COLORS["moyen"],
            "footer": {"text": f"{len(articles)} article(s) \u2022 veille auto"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        post_json(webhook, {"embeds": [embed]})
        return 1

    envois = 0
    for i in range(0, len(articles), 10):
        lot = articles[i:i + 10]
        embeds = []
        for art in lot:
            champs = []
            if art["tags"]:
                champs.append({
                    "name": "Tags",
                    "value": " ".join(f"`{t}`" for t in art["tags"][:8]),
                    "inline": False,
                })
            embeds.append({
                "title": truncate(art["titre"], 250),
                "url": art["lien"],
                "description": truncate(art["resume"], 350) or None,
                "color": DISCORD_COLORS[niveau(art["score"])],
                "fields": champs,
                "footer": {"text": f"{art['source']} \u2022 {art['date_txt']}"},
            })
        post_json(webhook, {"embeds": embeds})
        envois += 1
        time.sleep(1.2)  # respect du rate limit Discord
    return envois


def envoyer_telegram(token, chat_id, articles, digest, titre_digest):
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def esc(txt):
        """Telegram n'accepte que ces trois entites HTML."""
        return (txt.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))

    def send(texte, preview=False):
        post_json(url, {
            "chat_id": chat_id,
            "text": texte,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        })
        time.sleep(1.2)

    if digest:
        blocs = [f"<b>{esc(titre_digest)}</b>\n"]
        for art in articles:
            tags = " ".join(f"#{re.sub(r'[^0-9A-Za-z]', '', t)}"
                            for t in art["tags"][:4])
            blocs.append(
                f"\u2022 <a href=\"{esc(art['lien'])}\">"
                f"{esc(truncate(art['titre'], 150))}</a>\n"
                f"  <i>{esc(art['source'])} \u2022 {art['date_txt']}</i>"
                f" {tags}"
            )
        message, envois = "", 0
        for bloc in blocs:
            if len(message) + len(bloc) > 3800:
                send(message)
                envois += 1
                message = ""
            message += bloc + "\n\n"
        if message.strip():
            send(message)
            envois += 1
        return envois

    for art in articles:
        tags = " ".join(f"#{re.sub(r'[^0-9A-Za-z]', '', t)}"
                        for t in art["tags"][:6])
        texte = (
            f"<b>{esc(truncate(art['titre'], 200))}</b>\n\n"
            f"{esc(truncate(art['resume'], 400))}\n\n"
            f"<i>{esc(art['source'])} \u2022 {art['date_txt']}</i>\n"
            f"{tags}\n"
            f"{esc(art['lien'])}"
        )
        send(texte, preview=True)
    return len(articles)


def fiche_embed(art, types_source):
    """Fiche de curation pre-remplie (modele du cours). Le resume, le
    'pourquoi' et 'ce que j'ai appris' restent a rediger soi-meme."""
    info = types_source.get(art["source"], {})
    if art["date"]:
        age = (datetime.now(timezone.utc) - art["date"]).days
        actualite = (f"Publie il y a {age} jour(s)" if age > 0
                     else "Publie aujourd'hui")
    else:
        actualite = "Date inconnue : a verifier sur la page"
    pertinence = f"Niveau {niveau(art['score'])} (score {art['score']})"
    if art["tags"]:
        pertinence += " - mots detectes : " + ", ".join(art["tags"][:6])
    extrait = (truncate(art["resume"], 500)
               or "(pas d'extrait fourni par le flux)")
    piste = ("il traite de " + ", ".join(art["tags"][:3]) + "."
             if art["tags"] else "en quoi il repond a ton sujet ?")

    def champ(nom, valeur, inline=False):
        return {"name": nom, "value": truncate(valeur, 1000) or "-",
                "inline": inline}

    return {
        "title": truncate(f"Fiche - {art['titre']}", 250),
        "url": art["lien"],
        "color": DISCORD_COLORS[niveau(art["score"])],
        "fields": [
            champ("Titre", art["titre"]),
            champ("Source", art["source"], True),
            champ("Date", art["date_txt"], True),
            champ("Resume (une dizaine de lignes max)",
                  "A rediger avec tes propres mots.\n"
                  f"Extrait pour t'aider : {extrait}"),
            champ("Pourquoi cet article ?", "A completer. Piste : " + piste),
            champ("Qualite de la source", "\u200b"),
            champ("- Fiabilite", info.get("fiabilite", "A verifier"), True),
            champ("- Auteur", art.get("auteur") or "Non indique : a verifier",
                  True),
            champ("- Actualite", actualite, True),
            champ("- Objectivite", info.get("objectivite", "A verifier"), True),
            champ("- Pertinence", pertinence, True),
            champ("Ce que j'ai appris",
                  "A rediger (phrases simples, sans copier-coller)."),
        ],
        "footer": {"text": "Modele de curation - a completer"},
    }


def taille_embed(embed):
    """Nombre de caracteres compte par Discord (limite : 6000 par message)."""
    total = len(embed.get("title", "")) + len(embed.get("footer", {}).get("text", ""))
    for champ in embed.get("fields", []):
        total += len(champ["name"]) + len(champ["value"])
    return total


def envoyer_fiches(webhook, articles, types_source):
    lots, lot, taille = [], [], 0
    for art in articles:
        embed = fiche_embed(art, types_source)
        t = taille_embed(embed)
        if lot and (taille + t > 5500 or len(lot) >= 10):
            lots.append(lot)
            lot, taille = [], 0
        lot.append(embed)
        taille += t
    if lot:
        lots.append(lot)
    for embeds in lots:
        post_json(webhook, {"embeds": embeds})
        time.sleep(1.2)
    return len(lots)


def ecrire_archive(dossier, articles):
    os.makedirs(dossier, exist_ok=True)
    jour = datetime.now().strftime("%Y-%m-%d")
    chemin = os.path.join(dossier, f"{jour}.md")
    nouveau = not os.path.exists(chemin)
    with open(chemin, "a", encoding="utf-8") as f:
        if nouveau:
            f.write(f"# Veille du {jour}\n\n")
        f.write(f"<!-- run {datetime.now().strftime('%H:%M')} -->\n\n")
        for art in articles:
            f.write(f"## [{art['titre']}]({art['lien']})\n\n")
            f.write(f"*{art['source']} - {art['date_txt']}*")
            if art["tags"]:
                f.write(" - Tags : " + ", ".join(f"`{t}`" for t in art["tags"]))
            f.write("\n\n")
            if art["resume"]:
                f.write(f"> {truncate(art['resume'], 600)}\n\n")
    return chemin


# --------------------------------------------------------------------------
# Etat (anti-doublons)
# --------------------------------------------------------------------------

def charger_etat(path):
    if not os.path.exists(path):
        return {"vus": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("vus", {})
        return data
    except Exception:
        return {"vus": {}}


def sauver_etat(path, etat, retention_jours=120):
    limite = (datetime.now(timezone.utc)
              - timedelta(days=retention_jours)).isoformat()
    etat["vus"] = {k: v for k, v in etat["vus"].items() if v >= limite}
    etat["derniere_execution"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------

def check_feeds(config):
    ok, ko = 0, []
    for flux in config["flux"]:
        try:
            raw = fetch(flux["url"])
            articles = parse_feed(raw, flux["nom"])
            log(f"  OK   {flux['nom']:<32} {len(articles):>3} articles")
            ok += 1
        except Exception as exc:
            log(f"  ECHEC {flux['nom']:<32} {type(exc).__name__}: {exc}")
            ko.append(flux["nom"])
    log(f"\n{ok} flux fonctionnels, {len(ko)} en echec.")
    if ko:
        log("Retire ou corrige ces flux dans config.json : " + ", ".join(ko))
    return 0 if not ko else 1


def telegram_chatid(token):
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    with urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": UA}), timeout=20) as resp:
        data = json.load(resp)
    if not data.get("result"):
        log("Aucun message recu. Envoie un message a ton bot (ou ajoute-le au "
            "canal et poste quelque chose), puis relance cette commande.")
        return 1
    vus = set()
    for update in data["result"]:
        for cle in ("message", "channel_post", "my_chat_member"):
            chat = (update.get(cle) or {}).get("chat")
            if chat and chat["id"] not in vus:
                vus.add(chat["id"])
                log(f"  chat_id = {chat['id']}   ({chat.get('title') or chat.get('username') or chat.get('type')})")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Bot de veille technologique RSS -> Discord / Telegram")
    parser.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    parser.add_argument("--state", default=os.path.join(BASE_DIR, "state.json"))
    parser.add_argument("--dry-run", action="store_true",
                        help="affiche sans publier")
    parser.add_argument("--init", action="store_true",
                        help="marque tout l'existant comme deja vu, sans publier")
    parser.add_argument("--digest", action="store_true",
                        help="un seul message recapitulatif au lieu d'un post par article")
    parser.add_argument("--max", type=int, default=None,
                        help="nombre max d'articles publies sur cette execution")
    parser.add_argument("--jours", type=int, default=None,
                        help="anciennete maximale des articles, en jours")
    parser.add_argument("--check-feeds", action="store_true")
    parser.add_argument("--telegram-chatid", action="store_true",
                        help="affiche le chat_id de ton salon Telegram")
    parser.add_argument("--no-archive", action="store_true")
    parser.add_argument("--rejouer", action="store_true",
                        help="ignore la memoire des articles deja vus (et ne la modifie pas)")
    parser.add_argument("--fiches-seulement", action="store_true",
                        help="publie uniquement dans le salon des fiches de curation")
    args = parser.parse_args()

    load_dotenv(os.path.join(BASE_DIR, ".env"))

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)

    if args.check_feeds:
        return check_feeds(config)

    if args.telegram_chatid:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            log("TELEGRAM_BOT_TOKEN absent du .env")
            return 1
        return telegram_chatid(token)

    required = [(kw, build_pattern(kw)) for kw in config["mots_cles"]]
    bonus = {cat: [(kw, build_pattern(kw)) for kw in kws]
             for cat, kws in config.get("tags", {}).items()}
    exclusions = [build_pattern(kw) for kw in config.get("exclusions", [])]

    jours = args.jours if args.jours is not None else config.get("anciennete_max_jours", 10)
    plafond = args.max if args.max is not None else config.get("max_articles_par_execution", 12)
    limite_date = datetime.now(timezone.utc) - timedelta(days=jours)

    etat = charger_etat(args.state)
    vus = etat["vus"]

    # 1. Recuperation
    tous = []
    for flux in config["flux"]:
        try:
            raw = fetch(flux["url"])
            articles = parse_feed(raw, flux["nom"])
            tous.extend(articles)
            log(f"{flux['nom']:<32} {len(articles):>3} articles")
        except Exception as exc:
            log(f"{flux['nom']:<32} ERREUR ({type(exc).__name__})")

    # 2. Filtrage
    retenus, deja_vus_run = [], set()
    for art in tous:
        aid, tid = article_id(art), title_id(art)
        deja_connu = (aid in vus or tid in vus) and not args.rejouer
        if deja_connu or aid in deja_vus_run or tid in deja_vus_run:
            continue
        if art["date"] and art["date"] < limite_date:
            continue
        score, tags = analyse(art, required, bonus, exclusions)
        if score < config.get("score_minimum", 1):
            continue
        art["score"], art["tags"] = score, tags
        art["date_txt"] = art["date"].strftime("%d/%m/%Y") if art["date"] else "date inconnue"
        art["_ids"] = (aid, tid)
        deja_vus_run.update((aid, tid))
        retenus.append(art)

    retenus.sort(key=lambda a: (a["score"], a["date"] or limite_date), reverse=True)
    log(f"\n{len(tous)} articles lus \u2192 {len(retenus)} pertinents et nouveaux")

    if args.init:
        maintenant = datetime.now(timezone.utc).isoformat()
        for art in tous:
            vus[article_id(art)] = maintenant
            vus[title_id(art)] = maintenant
        sauver_etat(args.state, etat)
        log(f"Etat initialise : {len(vus)} entrees memorisees. "
            "Les prochaines executions ne publieront que les nouveautes.")
        return 0

    selection = retenus[:plafond]
    if not selection:
        log("Rien de neuf a publier.")
        sauver_etat(args.state, etat)
        return 0

    if args.dry_run:
        print()
        for art in selection:
            print(f"[{art['score']:>2}] {art['titre']}")
            print(f"     {art['source']} - {art['date_txt']}")
            if art["tags"]:
                print(f"     tags : {', '.join(art['tags'])}")
            print(f"     {art['lien']}\n")
        log(f"--dry-run : {len(selection)} article(s) auraient ete publies.")
        return 0

    # 3. Publication
    titre_digest = (f"Veille ransomware \u2014 "
                    f"{datetime.now().strftime('%d/%m/%Y')}")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    publie = False
    if args.fiches_seulement:
        webhook, token, chat_id = "", "", ""

    if webhook:
        try:
            n = envoyer_discord(webhook, selection, args.digest, titre_digest)
            log(f"Discord : {n} message(s) envoye(s).")
            publie = True
        except urllib.error.HTTPError as exc:
            log(f"Discord ERREUR HTTP {exc.code} : {exc.read().decode('utf-8', 'replace')[:300]}")
        except Exception as exc:
            log(f"Discord ERREUR : {exc}")

    if token and chat_id:
        try:
            n = envoyer_telegram(token, chat_id, selection, args.digest, titre_digest)
            log(f"Telegram : {n} message(s) envoye(s).")
            publie = True
        except urllib.error.HTTPError as exc:
            log(f"Telegram ERREUR HTTP {exc.code} : {exc.read().decode('utf-8', 'replace')[:300]}")
        except Exception as exc:
            log(f"Telegram ERREUR : {exc}")

    fiches = os.environ.get("DISCORD_FICHES_WEBHOOK_URL", "").strip()
    if fiches:
        try:
            n = envoyer_fiches(fiches, selection,
                               config.get("types_source", {}))
            log(f"Fiches de curation : {n} message(s) envoye(s).")
            publie = True
        except urllib.error.HTTPError as exc:
            log(f"Fiches ERREUR HTTP {exc.code} : "
                f"{exc.read().decode('utf-8', 'replace')[:300]}")
        except Exception as exc:
            log(f"Fiches ERREUR : {exc}")

    if not webhook and not fiches and not (token and chat_id):
        log("Aucune destination configuree. Renseigne DISCORD_WEBHOOK_URL ou "
            "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID dans le fichier .env "
            "(ou utilise --dry-run).")
        return 1

    if not args.no_archive and not args.rejouer and config.get("archive", True):
        chemin = ecrire_archive(os.path.join(BASE_DIR, "archives"), selection)
        log(f"Archive Markdown : {chemin}")

    if publie and not args.rejouer:
        maintenant = datetime.now(timezone.utc).isoformat()
        for art in selection:
            for ident in art["_ids"]:
                vus[ident] = maintenant
        sauver_etat(args.state, etat)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
