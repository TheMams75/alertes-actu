#!/usr/bin/env python3
"""Alertes actu critiques : lit des flux RSS (Al Jazeera, Reuters, AP),
repère les infos critiques et les envoie sur Telegram.

Usage :
    python3 alertes.py run       passage normal (lancé par GitHub Actions)
    python3 alertes.py test      envoie un message de test sur Telegram
    python3 alertes.py chat-id   renvoie ton chat_id dans la conversation avec le bot
    python3 alertes.py dry-run   affiche ce qui serait envoyé, sans rien envoyer
    python3 alertes.py test-alerte  envoie l'info la mieux notée du moment au format alerte

Variables d'environnement : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
Uniquement la bibliothèque standard de Python.
"""

import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ICI = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ICI, "config.json")
ETAT = os.environ.get("ALERTES_ETAT", os.path.join(ICI, "state.json"))
PARIS = ZoneInfo("Europe/Paris")
UA = "Mozilla/5.0 (alertes-actu; +https://github.com)"
MOTS_VIDES = set("a an the of to in on at for and or by with from as is are was be after over into new says say said le la les de des du un une et en au aux pour par sur dans avec".split())


# ---------- utilitaires ----------

def maintenant():
    return datetime.now(timezone.utc)


def charger_json(chemin, defaut):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return defaut


def sauver_json(chemin, data):
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, chemin)


def nettoyer(texte):
    texte = re.sub(r"<[^>]+>", " ", texte or "")
    texte = html.unescape(texte)
    return re.sub(r"\s+", " ", texte).strip()


def tokens(texte):
    mots = re.findall(r"[\w']+", texte.lower())
    return {m for m in mots if len(m) > 2 and m not in MOTS_VIDES}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def motif(terme):
    """Un terme qui finit par * accepte toutes les terminaisons (crash* = crashes, crashed...)."""
    t = terme.lower()
    fin = r"\w*" if t.endswith("*") else ""
    t = t.rstrip("*")
    return re.compile(r"(?<![\w])" + re.escape(t) + fin + r"(?![\w])")


# ---------- lecture des flux ----------

def telecharger(url, essais=2):
    for i in range(essais):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if i == essais - 1:
                print(f"  ! flux injoignable : {url[:80]} ({e})")
    return None


def lire_date(texte):
    if not texte:
        return None
    try:
        d = email.utils.parsedate_to_datetime(texte)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(texte.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def parser_flux(contenu, source):
    """Renvoie une liste d'articles {id, titre, resume, lien, date, source}."""
    articles = []
    try:
        racine = ET.fromstring(contenu)
    except ET.ParseError as e:
        print(f"  ! XML illisible ({source}) : {e}")
        return articles
    atom = "{http://www.w3.org/2005/Atom}"
    items = racine.findall(".//item") or racine.findall(f".//{atom}entry")
    for it in items:
        def champ(*noms):
            for n in noms:
                el = it.find(n)
                if el is not None and (el.text or "").strip():
                    return el.text.strip()
                if el is not None and el.get("href"):
                    return el.get("href")
            return ""
        titre = nettoyer(champ("title", f"{atom}title"))
        if not titre:
            continue
        # Google News ajoute " - Reuters" / " - AP News" à la fin du titre
        titre = re.sub(r"\s+-\s+(Reuters|AP News|The Associated Press|Associated Press)$", "", titre)
        lien = champ("link", f"{atom}link")
        resume = nettoyer(champ("description", f"{atom}summary", f"{atom}content"))
        if resume.startswith(titre):
            resume = resume[len(titre):].strip(" -")
        date = lire_date(champ("pubDate", f"{atom}updated", f"{atom}published",
                               "{http://purl.org/dc/elements/1.1/}date"))
        guid = champ("guid", f"{atom}id") or lien or titre
        ident = hashlib.sha1(f"{source}|{guid}".encode()).hexdigest()[:16]
        articles.append({"id": ident, "titre": titre, "resume": resume[:400],
                         "lien": lien, "date": date, "source": source})
    return articles


# ---------- score de criticité ----------

class Scoreur:
    def __init__(self, cfg):
        self.groupes = [(int(p), [(t, motif(t)) for t in termes])
                        for p, termes in cfg["mots_cles"].items()]
        self.acteurs = [(t, motif(t)) for t in cfg["acteurs"]]
        self.exclusions = [motif(t) for t in cfg["exclusions"]]
        self.exclusions_regex = [re.compile(r) for r in cfg.get("exclusions_regex", [])]
        self.declaratif = [motif(t) for t in cfg.get("declaratif", [])]
        self.poids_majeur = max(int(p) for p in cfg["mots_cles"])
        pr = cfg.get("priorites", {})
        self.bonus_max = pr.get("bonus_max", 0)
        self.priorites = {k: [(t, motif(t)) for t in v]
                          for k, v in pr.items() if isinstance(v, list)}

    def est_declaratif(self, titre):
        """Vrai si le titre rapporte une prise de parole (« X insists… », « X warns… »).
        L'attribution finale (« …, officials say ») ne compte pas : c'est un fait sourcé."""
        t = re.sub(r",[^,]*\b(say|says|said|according to|selon|reports?)\b[^,]*$", "", titre)
        debut = " ".join(t.split()[:6])
        return any(m.search(debut) for m in self.declaratif) or bool(re.match(r"^[^:]{0,40}:\s*['‘\"«“]", t))

    def score(self, art):
        titre = art["titre"].lower()
        texte = titre + " " + art["resume"].lower()
        art["declaratif"] = self.est_declaratif(titre)
        art["majeur"] = False
        if any(m.search(titre) for m in self.exclusions) or \
                any(r.search(art["titre"]) for r in self.exclusions_regex):
            return 0, ["exclu"]
        total, raisons = 0, []
        for poids, termes in self.groupes:
            touches = [t for t, m in termes if m.search(texte)]
            # un mot dans le titre compte plein, seulement dans le résumé : moitié
            pts = sum(poids if motif(t).search(titre) else poids / 2 for t in touches)
            pts = min(pts, poids * 2)  # plafond par groupe
            if touches:
                if poids == self.poids_majeur and any(motif(t).search(titre) for t in touches):
                    art["majeur"] = True
                total += pts
                raisons += [t for t in touches if t not in raisons]
        if total >= 3:
            acteurs = [t for t, m in self.acteurs if m.search(texte)]
            if acteurs:
                total += min(len(acteurs), 2)
                raisons += acteurs[:2]
            # priorités de Bob : finance, international, zones chaudes (+2 par domaine, plafonné)
            bonus = 0
            for domaine, termes in self.priorites.items():
                if any(m.search(texte) for t, m in termes):
                    bonus += 2
                    raisons.append("+" + domaine)
            total += min(bonus, self.bonus_max)
        if art["declaratif"]:
            raisons.append("déclaratif")
        return round(total, 1), raisons


def niveau(art, cfg):
    """urgent, a_suivre ou None. Une déclaration n'est jamais urgente."""
    if art["score"] >= cfg["seuil_alerte"] and not art.get("declaratif"):
        return "urgent"
    if art["score"] >= cfg.get("seuil_a_suivre", cfg["seuil_alerte"]):
        return "a_suivre"
    return None


RANG = {None: 0, "a_suivre": 1, "urgent": 2}


# ---------- Telegram ----------

def telegram(methode, **params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN manquant (secret GitHub à créer).")
    url = f"https://api.telegram.org/bot{token}/{methode}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"  ! Telegram {methode} : HTTP {e.code} {e.read()[:200]!r}")
        return {"ok": False}


def envoyer(texte, chat_id=None, silencieux=False):
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        sys.exit("TELEGRAM_CHAT_ID manquant (secret GitHub à créer).")
    rep = telegram("sendMessage", chat_id=chat_id, text=texte, parse_mode="HTML",
                   disable_web_page_preview="true",
                   disable_notification="true" if silencieux else "false")
    return rep.get("ok", False)


def formater(groupe, niv="urgent"):
    principal = groupe[0]
    sources = sorted({a["source"] for a in groupe})
    heure = (principal["date"] or maintenant()).astimezone(PARIS).strftime("%H:%M")
    if niv == "urgent":
        entete = "🚨 <b>URGENT</b>"
    else:
        entete = "🟠 <b>À SUIVRE</b>" + (" · déclaration" if principal.get("declaratif") else "")
    lignes = [f"{entete} · {html.escape(', '.join(sources))}"
              + (f" ({len(sources)} sources)" if len(sources) > 1 else ""),
              f"<b>{html.escape(principal['titre'])}</b>"]
    resume = next((a["resume"] for a in groupe if a["resume"]), "")
    if resume:
        lignes.append(html.escape(resume[:280]))
    deja = set()
    for a in groupe:
        if a["lien"] and a["source"] not in deja:
            deja.add(a["source"])
            lignes.append(f'🔗 <a href="{html.escape(a["lien"])}">{html.escape(a["source"])}</a>')
    lignes.append(f"🕒 {heure} (Paris) · indice {principal['score']}")
    return "\n".join(lignes)


# ---------- passage principal ----------

def passage(envoi=True, test=False):
    cfg = charger_json(CONFIG, None)
    if cfg is None:
        sys.exit("config.json introuvable ou invalide.")
    etat = charger_json(ETAT, {})
    premier = not etat
    vus = etat.get("vus", {})            # id -> timestamp
    alertes = etat.get("alertes", [])    # [{tokens, ts}]
    t0 = maintenant()
    limite = t0 - timedelta(hours=cfg["fenetre_heures"])
    scoreur = Scoreur(cfg)

    articles = []
    for f in cfg["flux"]:
        if not f.get("actif", True):
            continue
        contenu = telecharger(f["url"])
        if contenu:
            lus = parser_flux(contenu, f["nom"])
            print(f"- {f['nom']}: {len(lus)} articles")
            articles += lus

    nouveaux = [a for a in articles if (test or a["id"] not in vus)
                and (a["date"] is None or a["date"] >= limite)]
    for a in articles:
        vus.setdefault(a["id"], t0.timestamp())

    # regroupe les articles qui parlent du même événement
    groupes = []
    for a in nouveaux:
        a["tok"] = tokens(a["titre"])
        a["score"], a["raisons"] = scoreur.score(a)
        for g in groupes:
            if jaccard(a["tok"], g[0]["tok"]) >= cfg["similarite_doublon"]:
                g.append(a)
                break
        else:
            groupes.append([a])

    candidats = []
    for g in groupes:
        g.sort(key=lambda x: x["score"], reverse=True)
        bonus = 2 if len({a["source"] for a in g}) >= 2 else 0
        g[0]["score"] = round(g[0]["score"] + bonus, 1)
        g[0]["niveau"] = niveau(g[0], cfg)
        if g[0]["niveau"]:
            # déjà envoyé dans les 12 dernières heures à un niveau égal ou supérieur ?
            deja = max((RANG[x.get("niveau", "urgent")] for x in alertes
                        if jaccard(g[0]["tok"], set(x["tokens"])) >= cfg["similarite_doublon"]),
                       default=0)
            if RANG[g[0]["niveau"]] > deja:
                candidats.append(g)
    candidats.sort(key=lambda g: (RANG[g[0]["niveau"]], g[0]["score"]), reverse=True)
    candidats = candidats[: cfg["max_alertes_par_passage"]]

    classement = sorted(groupes, key=lambda g: g[0]["score"], reverse=True)
    print("Infos les mieux notées en ce moment :")
    for g in classement[:8]:
        src = ", ".join(sorted({a["source"] for a in g}))
        print(f"  [{g[0]['score']}] {g[0]['titre']}  ({src})  <- {', '.join(g[0]['raisons'][:6])}")

    if test:
        if not classement:
            envoyer("🧪 Test : aucun article lu dans les flux.")
            return []
        g = classement[0]
        niv = g[0].get("niveau") or "a_suivre"
        ok = envoyer("🧪 <b>TEST</b> (seuils réels : urgent " + str(cfg["seuil_alerte"]) + ", à suivre "
                     + str(cfg.get("seuil_a_suivre")) + ")\n" + formater(g, niv))
        print("Alerte de test envoyée." if ok else "Échec de l'envoi Telegram.")
        return [g]

    print(f"{len(articles)} lus, {len(nouveaux)} nouveaux, {len(candidats)} alerte(s)")
    for g in candidats:
        print(f"  {g[0]['niveau']} [{g[0]['score']}] {g[0]['titre']}  <- {', '.join(g[0]['raisons'])}")

    if premier and envoi:
        print("Premier passage : articles mémorisés, aucune alerte envoyée.")
        envoyer("✅ <b>Alertes actu activées.</b>\nTu recevras ici les infos critiques "
                "(Al Jazeera, Reuters, AP), contrôlées toutes les 10 minutes environ.")
    elif envoi:
        for g in candidats:
            niv = g[0]["niveau"]
            if envoyer(formater(g, niv), silencieux=(niv == "a_suivre")):
                alertes.append({"tokens": sorted(g[0]["tok"]), "ts": t0.timestamp(), "niveau": niv})
            time.sleep(1)

    # ménage : 7 jours pour les articles vus, 12 h pour les alertes envoyées
    vus = {k: v for k, v in vus.items() if v > t0.timestamp() - 7 * 86400}
    alertes = [x for x in alertes if x["ts"] > t0.timestamp() - 12 * 3600]
    if envoi:
        sauver_json(ETAT, {"vus": vus, "alertes": alertes, "maj": t0.isoformat()})
    return candidats


def chat_id():
    rep = telegram("getUpdates")
    if not rep.get("ok"):
        sys.exit("Telegram refuse le token : vérifie le secret TELEGRAM_BOT_TOKEN "
                 "(copie exacte du token donné par BotFather, sans espace).")
    chats = {}
    for u in rep.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        c = msg.get("chat", {})
        if c.get("id"):
            chats[c["id"]] = c.get("first_name") or c.get("title") or ""
    if not chats:
        moi = telegram("getMe").get("result", {})
        nom = moi.get("username", "?")
        sys.exit(f"Aucun message reçu par le bot @{nom}. Ouvre https://t.me/{nom} "
                 "dans Telegram, appuie sur Démarrer (ou envoie /start), puis relance.")
    for cid in chats:
        envoyer(f"Ton chat_id est : <code>{cid}</code>\nCopie-le dans le secret "
                f"GitHub TELEGRAM_CHAT_ID.", chat_id=cid)
    print(f"chat_id envoyé dans {len(chats)} conversation(s) Telegram.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "run":
        if not os.environ.get("TELEGRAM_BOT_TOKEN"):
            print("Secret TELEGRAM_BOT_TOKEN absent : passage ignoré (voir README).")
            sys.exit(0)
        if not os.environ.get("TELEGRAM_CHAT_ID"):
            print("TELEGRAM_CHAT_ID absent : envoi du chat_id dans Telegram.")
            chat_id()
            sys.exit(0)
        passage(envoi=True)
    elif mode == "dry-run":
        passage(envoi=False)
    elif mode == "test-alerte":
        passage(envoi=False, test=True)
    elif mode == "test":
        ok = envoyer("🧪 <b>Test</b> : le bot d'alertes actu fonctionne.")
        sys.exit(0 if ok else 1)
    elif mode == "chat-id":
        chat_id()
    else:
        sys.exit(__doc__)
