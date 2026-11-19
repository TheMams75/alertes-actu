#!/usr/bin/env python3
"""Envoie sur Telegram les messages déposés dans outbox/ par les tâches Claude
(revue du matin, fil 2h). Lancé par .github/workflows/envoi-telegram.yml.

Nom de fichier attendu : outbox/AAAA-MM-JJ_HHMM_<type>.md
  type = revue  -> texte brut, prêt à copier dans WhatsApp ou Slack, avec son
  type = alerte -> mise en forme Telegram, avec son
  type = fil    -> mise en forme Telegram, silencieux
  type = test   -> mise en forme Telegram, avec son

Usage : python3 envoyer_outbox.py liste_des_fichiers.txt
Variables d'environnement : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LIMITE = 4000  # Telegram refuse au-delà de 4096 caractères


def appel(params):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("ok", False)
    except urllib.error.HTTPError as e:
        print(f"  ! Telegram HTTP {e.code} : {e.read()[:200]!r}")
        return False


def morceaux(texte):
    """Découpe sur les fins de ligne pour rester sous la limite Telegram."""
    bloc, sortie = "", []
    for ligne in texte.splitlines(keepends=True):
        if len(bloc) + len(ligne) > LIMITE and bloc:
            sortie.append(bloc)
            bloc = ""
        bloc += ligne
    if bloc.strip():
        sortie.append(bloc)
    return sortie


def envoyer_fichier(chemin):
    nom = os.path.basename(chemin)
    type_ = nom.rsplit("_", 1)[-1].removesuffix(".md").removesuffix(".txt").lower()
    with open(chemin, encoding="utf-8") as f:
        texte = f.read().strip()
    if not texte:
        print(f"- {nom} : vide, ignoré")
        return True
    silencieux = type_ == "fil"
    brut = type_ == "revue"
    ok_total = True
    for part in morceaux(texte):
        params = {"chat_id": os.environ["TELEGRAM_CHAT_ID"].strip(), "text": part,
                  "disable_web_page_preview": "true",
                  "disable_notification": "true" if silencieux else "false"}
        ok = False
        if not brut:
            ok = appel({**params, "parse_mode": "Markdown"})
        if not ok:  # texte brut, ou Markdown refusé (astérisque orphelin...)
            ok = appel(params)
        ok_total &= ok
        time.sleep(1)
    print(f"- {nom} ({type_}) : {'envoyé' if ok_total else 'ÉCHEC'}")
    return ok_total


if __name__ == "__main__":
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        sys.exit("Secrets Telegram absents.")
    with open(sys.argv[1], encoding="utf-8") as f:
        motif = re.compile(r"^outbox/\d{4}-\d{2}-\d{2}_\d{4}_\w+\.(md|txt)$")
        fichiers = [l.strip() for l in f if motif.match(l.strip())]
    if not fichiers:
        print("Aucun nouveau message dans outbox/.")
    resultats = [envoyer_fichier(p) for p in sorted(fichiers) if os.path.exists(p)]
    sys.exit(0 if all(resultats) else 1)
