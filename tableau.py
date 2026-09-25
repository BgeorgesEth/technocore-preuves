"""Tableau de bord local des preuves Technocore.

Lance un petit serveur web sur 127.0.0.1 (jamais exposé au réseau) et un surveillant qui suit
les salons choisis : chaque message signé par un de tes DID est capturé, vérifié, archivé puis
horodaté automatiquement. Aucune clé privée n'est manipulée ici.

Usage : python tableau.py [--archive DOSSIER] [--port 8765] [--sans-navigateur]
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import re
import secrets
import sys
import threading
import time
import webbrowser
import zipfile
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import preuves as pv

HERE = Path(__file__).resolve().parent
AGENT_DIR = pv.DEFAULT_AGENT_DIR.expanduser()
ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
DID_RE = re.compile(r"^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{40,60}$")
FILE_RE = re.compile(r"^(messages|contributions)/[A-Za-z0-9_.~-]+\.json(\.ots)?$")
REPORTS = {"preuves.csv": "text/csv", "PREUVES.md": "text/markdown", "preuves.html": "text/html",
           "index.jsonl": "application/x-ndjson"}
DEFAULT_ROOMS = ["technocore", "lobby"]
READS_PER_SECOND = 5  # le serveur autorise 600 lectures/min par IP : on en garde la moitié pour le reste
EXPORT_TIME_LIMIT = 120
MAINTENANCE_EVERY = 30
CARNET_EVERY = 1800
MENTIONS_MAX = 2000  # un salon public est ouvert à tous : on borne ce qu'un flot de mentions peut écrire
STAMP_EVERY = 120
UPGRADE_EVERY = 3600


def kv_chemin(did: str) -> str:
    """Adresse de la note d'identité d'un DID : 16 premiers caractères de son SHA-256, coupés 2 / 14."""
    empreinte = hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]
    return f"/kv/did-{empreinte[:2]}/{empreinte[2:]}"


def jours_depuis(iso: str | None) -> int | None:
    try:
        quand = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - quand).total_seconds() // 86400))


def instant(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ecart_heures(debut: str | None, fin: str | None) -> float:
    a, b = instant(debut), instant(fin)
    return max((b - a) / 3600, 1 / 60) if a is not None and b is not None else 0.0


def rafale_max(horodatages: list, fenetre: float = 60.0) -> int:
    """Plus grand nombre de messages tenant dans une même fenêtre de 60 secondes."""
    points = sorted(t for t in (instant(h) for h in horodatages) if t is not None)
    meilleur = debut = 0
    for fin, valeur in enumerate(points):
        while valeur - points[debut] > fenetre:
            debut += 1
        meilleur = max(meilleur, fin - debut + 1)
    return meilleur


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.next_slot = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot)
            self.next_slot = slot + self.interval
        time.sleep(max(0.0, slot - now))


class Config:
    """DID et salons surveillés, stockés dans <archive>/config.json."""

    def __init__(self, archive: pv.Archive):
        self.path = archive.root / "config.json"
        self.lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"dids": archive.known_dids(), "salons": list(DEFAULT_ROOMS)}
        self.dids = [d for d in data.get("dids", []) if DID_RE.match(d)]
        self.salons = [r for r in data.get("salons", []) if ROOM_RE.match(r)]
        # Boîtes aux lettres : on y capture les messages des AUTRES, pas les siens.
        self.boites = [r for r in data.get("boites", []) if ROOM_RE.match(r)]
        self.boite_lue = data.get("boite_lue") or "1970-01-01T00:00:00Z"
        # Carnet d'adresses : les DID des AUTRES, avec le surnom qu'on leur donne ici.
        self.contacts = self.propres_contacts(data.get("contacts") or [])[0]
        self.save()

    @staticmethod
    def propres_contacts(contacts: list) -> tuple[list[dict], list[str]]:
        """Nettoie une liste de contacts. Renvoie (contacts retenus, entrées refusées)."""
        propres, refuses, vus = [], [], set()
        for contact in contacts:
            contact = contact if isinstance(contact, dict) else {}
            did = str(contact.get("did", "")).strip()
            surnom = " ".join(str(contact.get("surnom", "")).split())[:40]
            if not DID_RE.match(did):
                refuses.append(did or "(DID vide)")
            elif did not in vus:
                vus.add(did)
                propres.append({"did": did, "surnom": surnom})
        return propres, refuses

    def save(self) -> None:
        with self.lock:
            self.path.write_text(json.dumps({"dids": self.dids, "salons": self.salons,
                                             "boites": self.boites, "boite_lue": self.boite_lue,
                                             "contacts": self.contacts},
                                            indent=2) + "\n", encoding="utf-8")

    def update_contacts(self, contacts: list) -> list[str]:
        propres, refuses = self.propres_contacts(contacts)
        if refuses:
            return refuses
        with self.lock:
            self.contacts = propres
        self.save()
        return []

    def update(self, dids: list[str], salons: list[str], boites: list[str] | None = None) -> list[str]:
        boites = self.boites if boites is None else boites
        bad = ([d for d in dids if not DID_RE.match(d)]
               + [r for r in salons if not ROOM_RE.match(r)]
               + [r for r in boites if not ROOM_RE.match(r)])
        if bad:
            return bad
        with self.lock:
            self.dids = list(dict.fromkeys(dids))
            self.salons = list(dict.fromkeys(salons))
            self.boites = list(dict.fromkeys(boites))
        self.save()
        return []

    def marquer_lue(self) -> None:
        with self.lock:
            self.boite_lue = pv.now_iso()
        self.save()


class RoomWatcher(threading.Thread):
    """Suit un salon : rattrape l'historique conservé (export) puis lit en continu (long-poll)."""

    def __init__(self, room: str, tableau: "Tableau"):
        super().__init__(name=f"salon-{room}", daemon=True)
        self.room, self.tableau = room, tableau
        self.stop_event = threading.Event()
        self.backfill_event = threading.Event()
        self.backfill_event.set()
        self.cursor: int | None = None
        self.status = {"salon": room, "etat": "demarrage", "curseur": None, "dernier_releve": None,
                       "captures": 0, "mentions": 0, "manques": 0, "historique": "en_attente",
                       "depuis": None, "erreur": None}

    def run(self) -> None:
        backoff = 5
        while not self.stop_event.is_set():
            try:
                if self.cursor is None:
                    # Le suivi en direct part de maintenant ; l'export couvre tout ce qui précède.
                    self.tableau.limiter.acquire()
                    self.cursor = int(pv.read_room(self.room, limit=1).get("last_seq") or 0)
                    self.status.update(etat="surveillance", curseur=self.cursor)
                if self.backfill_event.is_set():
                    self.backfill_event.clear()
                    threading.Thread(target=self.backfill, name=f"export-{self.room}", daemon=True).start()
                self.poll()
                backoff = 5
            except HTTPError as error:
                wait = backoff
                if error.code == 429:
                    wait = int(error.headers.get("Retry-After") or 30)
                self.fail(f"HTTP {error.code}", wait)
                backoff = min(backoff * 2, 120)
            except (URLError, TimeoutError, OSError, ValueError, KeyError) as error:
                self.fail(str(error) or type(error).__name__, backoff)
                backoff = min(backoff * 2, 120)

    def fail(self, detail: str, wait: float) -> None:
        self.status.update(etat="erreur", erreur=f"{detail} (nouvel essai dans {wait:.0f} s)")
        self.stop_event.wait(wait)

    def backfill(self) -> None:
        """Parcourt l'export du salon (tout ce que le serveur conserve encore) à la recherche de tes DID.

        Tourne en parallèle du suivi en direct, un export à la fois : le serveur bride les exports
        simultanés depuis une même adresse.
        """
        with self.tableau.export_lock:
            dids = set(self.tableau.config.dids)
            self.status.update(historique="en_cours")
            self.tableau.limiter.acquire()
            started, first, complete = time.monotonic(), None, False
            request = Request(f"{pv.BASE_URL}/r/{self.room}/export", headers={"User-Agent": pv.USER_AGENT})
            try:
                with urlopen(request, timeout=30) as response:
                    for raw in response:
                        if self.stop_event.is_set() or time.monotonic() - started > EXPORT_TIME_LIMIT:
                            break
                        line = raw.decode("utf-8", errors="replace")
                        if first is None:
                            match = re.match(r'\{"seq":\d+,"ts":"([^"]+)"', line)
                            first = match.group(1) if match else None
                        if dids and any(did in line for did in dids):
                            try:
                                message = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            # Signé par toi : c'est une preuve. Cité par un autre : c'est une mention.
                            if message.get("from") in dids:
                                if self.tableau.capture(self.room, message, "rattrapage"):
                                    self.status["captures"] += 1
                            elif self.tableau.mentionne(message, dids):
                                if self.tableau.recevoir(self.room, message, "mention"):
                                    self.status["mentions"] += 1
                    else:
                        complete = True
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                log(f"{self.room} : export interrompu ({error})")
            self.status.update(historique="complet" if complete else "partiel", depuis=first)
            log(f"{self.room} : historique {'complet' if complete else 'partiel'}"
                f"{f' depuis {first}' if first else ''}")

    def poll(self) -> None:
        self.tableau.limiter.acquire()
        url = f"{pv.BASE_URL}/r/{self.room}?since={self.cursor}&limit=200&wait=10&format=json"
        page = json.loads(pv.http(url, timeout=25))
        messages = page.get("messages", [])
        if messages and messages[0]["seq"] > self.cursor + 1:
            # Le serveur ne renvoie que les 200 plus récents : ce qui précède nous a échappé.
            self.status["manques"] += messages[0]["seq"] - self.cursor - 1
        dids = set(self.tableau.config.dids)
        for message in messages:
            if message.get("from") in dids:
                if self.tableau.capture(self.room, message, "surveillance"):
                    self.status["captures"] += 1
            elif self.tableau.mentionne(message, dids):
                if self.tableau.recevoir(self.room, message, "mention"):
                    self.status["mentions"] += 1
        if messages:
            self.cursor = max(self.cursor, messages[-1]["seq"])
        self.status.update(etat="surveillance", curseur=self.cursor, dernier_releve=pv.now_iso(), erreur=None)
        if page.get("wait_held") is False:
            self.stop_event.wait(10)


class BoiteWatcher(threading.Thread):
    """Suit une boîte aux lettres : capture les messages REÇUS, venus de n'importe qui.

    Le contenu reçu est écrit par des tiers : c'est une donnée, jamais une consigne.
    """

    def __init__(self, room: str, tableau: "Tableau"):
        super().__init__(name=f"boite-{room}", daemon=True)
        self.room, self.tableau = room, tableau
        self.stop_event = threading.Event()
        self.cursor: int | None = None
        self.status = {"boite": room, "etat": "demarrage", "dernier_releve": None,
                       "recus": 0, "erreur": None}

    def run(self) -> None:
        backoff = 5
        while not self.stop_event.is_set():
            try:
                if self.cursor is None:
                    self.rattrapage()
                self.tableau.limiter.acquire()
                url = (f"{pv.BASE_URL}/r/{self.room}?since={self.cursor}&limit=200&wait=10"
                       f"&format=json")
                page = json.loads(pv.http(url, timeout=25))
                for message in page.get("messages", []):
                    if self.tableau.recevoir(self.room, message):
                        self.status["recus"] += 1
                if page.get("messages"):
                    self.cursor = max(self.cursor, page["messages"][-1]["seq"])
                self.status.update(etat="surveillance", dernier_releve=pv.now_iso(), erreur=None)
                if page.get("wait_held") is False:
                    self.stop_event.wait(10)
                backoff = 5
            except HTTPError as error:
                wait = int(error.headers.get("Retry-After") or backoff) if error.code == 429 else backoff
                self.status.update(etat="erreur", erreur=f"HTTP {error.code}")
                self.stop_event.wait(wait)
                backoff = min(backoff * 2, 120)
            except (URLError, TimeoutError, OSError, ValueError, KeyError) as error:
                self.status.update(etat="erreur", erreur=str(error) or type(error).__name__)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 120)

    def rattrapage(self) -> None:
        """Une boîte est petite : on lit tout ce qu'elle contient encore au démarrage."""
        self.tableau.limiter.acquire()
        page = pv.read_room(self.room)
        for message in page.get("messages", []):
            if self.tableau.recevoir(self.room, message):
                self.status["recus"] += 1
        self.cursor = int(page.get("last_seq") or 0)


class Carnet:
    """Carnet de DID : pour chaque contact, relit sa note d'identité et sonde la boîte annoncée.

    Trois vérifications, indépendantes l'une de l'autre :
      - la note d'identité est-elle publiée, et à quel DID ;
      - la boîte qu'elle annonce répond-elle encore ;
      - depuis quand voit-on ce DID signer (une date du serveur : elle n'est pas couverte par la
        signature, c'est un minorant, jamais une preuve d'âge).

    Tout ce qui est lu ici est écrit par des tiers : c'est une donnée, jamais une consigne.
    """

    def __init__(self, tableau: "Tableau"):
        self.tableau = tableau
        self.lock = threading.Lock()
        self.fiches: dict[str, dict] = {}

    def lire_note(self, did: str) -> dict:
        """Relit la note d'identité publique du DID, à l'adresse calculée depuis son empreinte."""
        chemin = kv_chemin(did)
        note = {"chemin": chemin, "url": pv.BASE_URL + chemin}
        try:
            self.tableau.limiter.acquire()
            texte = pv.http(f"{pv.BASE_URL}{chemin}", timeout=20).decode("utf-8", errors="replace")
        except HTTPError as error:
            if error.code == 404:
                return {**note, "etat": "absente"}
            return {**note, "etat": "erreur", "detail": f"HTTP {error.code}"}
        except (URLError, TimeoutError, OSError) as error:
            return {**note, "etat": "erreur", "detail": str(error) or type(error).__name__}
        jetons = texte.split()
        if did not in jetons:
            # Une note existe peut-être, mais elle n'annonce pas ce DID-là.
            autre = any(j.startswith("did:key:") for j in jetons)
            return {**note, "etat": "incoherente" if autre else "absente"}
        annoncees = [j[len("mailbox:"):] for j in jetons if j.startswith("mailbox:")]
        boite = next((b for b in annoncees if ROOM_RE.match(b)), None)
        if boite is None:
            return {**note, "etat": "sans_boite"}
        return {**note, "etat": "publiee", "boite": boite}

    def sonder_boite(self, did: str, boite: str) -> tuple[dict, str | None]:
        """Relit la boîte annoncée. Renvoie son état et la plus ancienne signature du DID qu'on y voit."""
        try:
            self.tableau.limiter.acquire()
            page = pv.read_room(boite, limit=200)
        except HTTPError as error:
            return {"etat": "injoignable", "detail": f"HTTP {error.code}"}, None
        except (URLError, TimeoutError, OSError, ValueError) as error:
            return {"etat": "injoignable", "detail": str(error) or type(error).__name__}, None
        messages = page.get("messages") or []
        # Une boîte jamais écrite a une génération nulle ; vidée par le serveur, elle garde la sienne.
        etat = "vivante" if messages else ("vide" if page.get("generation") else "inexistante")
        signes = [m for m in messages if m.get("from") == did and pv.verify_record(
            {"type": "message", "room": boite,
             **{k: m.get(k) for k in ("seq", "ts", "from", "nonce", "text", "sig")}})]
        dernier = messages[-1].get("ts") if messages else None
        # Un salon sans message pendant 7 jours est supprimé : une boîte silencieuse est en sursis.
        resume = {"etat": etat, "messages": len(messages), "de_lui": len(signes),
                  "dernier": dernier, "silence": jours_depuis(dernier)}
        return resume, min((m.get("ts") for m in signes if m.get("ts")), default=None)

    def lire_export(self, room: str, dids: set) -> tuple[dict, dict]:
        """Un seul passage sur l'export d'un salon : première signature et activité de chaque contact.

        Le même passage mesure le salon entier (médiane, textes publiés par plusieurs DID), sans quoi
        un débit n'aurait aucun point de comparaison.
        """
        premiers: dict[str, dict] = {}
        horodatages: dict[str, list] = {d: [] for d in dids}
        textes: dict[str, set] = {d: set() for d in dids}
        par_did: collections.Counter = collections.Counter()
        par_texte: dict[str, set] = {}
        bornes: list = [None, None]
        depart = time.monotonic()
        complet = False
        self.tableau.limiter.acquire()
        requete = Request(f"{pv.BASE_URL}/r/{room}/export", headers={"User-Agent": pv.USER_AGENT})
        try:
            with urlopen(requete, timeout=30) as reponse:
                for brut in reponse:
                    if time.monotonic() - depart > EXPORT_TIME_LIMIT:
                        break
                    try:
                        message = json.loads(brut.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue
                    auteur = message.get("from")
                    if not auteur:
                        continue
                    texte, ts = (message.get("text") or "").strip(), message.get("ts")
                    par_did[auteur] += 1
                    if ts:
                        bornes[0] = min(bornes[0] or ts, ts)
                        bornes[1] = max(bornes[1] or ts, ts)
                    # Un texte identique publié par beaucoup de DID trahit une flotte pilotée.
                    if 12 <= len(texte) <= 2000 and (texte in par_texte or len(par_texte) < 80000):
                        par_texte.setdefault(texte, set()).add(auteur)
                    if auteur in dids:
                        # L'export est croissant : la première occurrence est la plus ancienne conservée.
                        premiers.setdefault(auteur, message)
                        textes[auteur].add(texte)
                        horodatages[auteur].append(ts)
                else:
                    complet = True
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            log(f"carnet : export de {room} interrompu ({error})")

        comptes = sorted(par_did.values())
        heures = ecart_heures(bornes[0], bornes[1])
        resume = {"salon": room, "messages": sum(comptes), "dids": len(comptes), "complet": complet,
                  "mediane": comptes[len(comptes) // 2] if comptes else 0,
                  "fenetre_h": round(heures, 2), "depuis": bornes[0]}
        mesures = {}
        for did, message in premiers.items():
            nombre = par_did[did]
            clones, clone_texte = max(((len(par_texte.get(t, ())) - 1, t) for t in textes[did] if t),
                                      default=(0, ""))
            mesures[did] = {"salon": room, "premier": message, "premier_salon": room,
                            "messages": nombre,
                            "textes": len(textes[did]),
                            "par_heure": round(nombre / heures, 1) if heures else None,
                            "rafale": rafale_max(horodatages[did]),
                            "clones": clones, "clone_texte": clone_texte[:120] if clones >= 2 else "",
                            "mediane_h": round((resume["mediane"] or 0) / heures, 2) if heures else None,
                            "fenetre_h": resume["fenetre_h"]}
        return mesures, resume

    def analyser_salons(self, dids: set) -> dict[str, dict]:
        """Passe une fois sur chaque salon surveillé. Le coût ne dépend pas du nombre de contacts."""
        if not dids:
            return {}
        retenus: dict[str, dict] = {}
        for room in list(self.tableau.config.salons):
            with self.tableau.export_lock:
                mesures, _ = self.lire_export(room, dids)
            for did, mesure in mesures.items():
                garde = retenus.get(did)
                if garde is None:
                    retenus[did] = mesure
                    continue
                # L'activité retenue est celle du salon où il écrit le plus ; la date, la plus ancienne.
                ancien = (garde["premier"], garde["premier_salon"])
                if (mesure["premier"].get("ts") or "") >= (ancien[0].get("ts") or ""):
                    mesure["premier"], mesure["premier_salon"] = ancien
                if mesure["messages"] > garde["messages"]:
                    retenus[did] = mesure
                else:
                    garde["premier"], garde["premier_salon"] = mesure["premier"], mesure["premier_salon"]
        return {did: mesure for did, mesure in retenus.items()
                if pv.verify_record({"type": "message", "room": mesure["premier_salon"],
                                     **{k: mesure["premier"].get(k)
                                        for k in ("seq", "ts", "from", "nonce", "text", "sig")}})}

    def anciennete(self, did: str, depuis_boite: str | None, vu_salon: dict | None) -> dict:
        """La plus ancienne signature valide du DID que l'on puisse encore montrer.

        Toutes ces dates viennent du serveur : la signature ne les couvre pas. C'est donc un
        minorant — le DID peut être plus ancien —, jamais une preuve d'âge.
        """
        with self.tableau.boite_lock:
            recus = [r["ts"] for r in self.tableau.recus
                     if r.get("de") == did and r.get("signature_valide") and r.get("ts")]
        vus = {"boite": depuis_boite, "archive": min(recus, default=None),
               "salon": (vu_salon or {}).get("premier", {}).get("ts")}
        vus = {source: ts for source, ts in vus.items() if ts}
        if not vus:
            return {"etat": "inconnue"}
        source = min(vus, key=lambda k: vus[k])
        age = {"etat": "connue", "depuis": vus[source], "jours": jours_depuis(vus[source]),
               "source": source}
        if source == "salon":
            age["salon"] = vu_salon["salon"]
        return age

    @staticmethod
    def risques(activite: dict | None, partagee: list) -> list[dict]:
        """Signaux mesurés, jamais un verdict : chacun porte le chiffre qui l'a déclenché.

        Ils disent ce que le DID a fait dans la fenêtre que le serveur conserve encore. Aucun
        signal ne veut pas dire « identité propre », seulement « rien dans cette fenêtre ».
        """
        signaux = [{"code": "boite_partagee", "gravite": "haute", "qui": autre} for autre in partagee]
        if not activite:
            return signaux
        messages, textes = activite.get("messages", 0), activite.get("textes", 0)
        if activite.get("clones", 0) >= 2:
            signaux.append({"code": "flotte", "gravite": "haute", "n": activite["clones"] + 1,
                            "texte": activite.get("clone_texte", "")})
        if messages >= 10 and textes <= 2:
            signaux.append({"code": "repetition", "gravite": "haute", "n": messages, "textes": textes})
        par_heure, mediane_h = activite.get("par_heure") or 0, activite.get("mediane_h") or 0
        if par_heure >= 20 and par_heure >= 10 * max(mediane_h, 0.4):
            signaux.append({"code": "debit", "gravite": "haute" if par_heure >= 100 else "moyenne",
                            "n": par_heure,
                            "mediane": mediane_h, "salon": activite.get("salon")})
        if activite.get("rafale", 0) >= 10:
            signaux.append({"code": "rafale", "gravite": "moyenne", "n": activite["rafale"]})
        return signaux

    def verifier(self, contact: dict, vu_salon: dict | None = None,
                 partagee: list | None = None) -> dict:
        did = contact["did"]
        note = self.lire_note(did)
        boite = note.get("boite")
        etat_boite, depuis = (self.sonder_boite(did, boite) if boite
                              else ({"etat": "inconnue"}, None))
        activite = {k: v for k, v in (vu_salon or {}).items()
                    if k not in ("premier", "premier_salon")} or None
        fiche = {"did": did, "surnom": contact.get("surnom", ""), "boite": boite, "note": note,
                 "boite_etat": etat_boite, "anciennete": self.anciennete(did, depuis, vu_salon),
                 "activite": activite, "risques": self.risques(activite, partagee or []),
                 "verifie_le": pv.now_iso()}
        with self.lock:
            self.fiches[did] = fiche
        return fiche

    def boites_partagees(self, contacts: list) -> dict[str, list]:
        """Deux contacts qui annoncent la même boîte partagent une réception : souvent un seul opérateur."""
        par_boite: dict[str, list] = {}
        for contact in contacts:
            boite = (self.fiches.get(contact["did"]) or {}).get("boite")
            if boite:
                par_boite.setdefault(boite, []).append(contact)
        partagees: dict[str, list] = {}
        for voisins in par_boite.values():
            if len(voisins) > 1:
                for contact in voisins:
                    partagees[contact["did"]] = [a.get("surnom") or a["did"][:18] + "…"
                                                 for a in voisins if a["did"] != contact["did"]]
        return partagees

    def verifier_tous(self) -> int:
        contacts = list(self.tableau.config.contacts)
        vus = self.analyser_salons({c["did"] for c in contacts})
        for contact in contacts:
            self.verifier(contact, vus.get(contact["did"]))
        # La collision de boîtes ne se voit qu'une fois toutes les notes relues.
        for did, voisins in self.boites_partagees(contacts).items():
            contact = next(c for c in contacts if c["did"] == did)
            self.verifier(contact, vus.get(did), voisins)
        with self.lock:
            connus = {c["did"] for c in contacts}
            for did in [d for d in self.fiches if d not in connus]:
                self.fiches.pop(did)
        if contacts:
            log(f"carnet : {len(contacts)} contact(s) revérifié(s)")
        self.tableau.changed()
        return len(contacts)

    def liste(self) -> list[dict]:
        """Une fiche par contact, dans l'ordre du carnet, même si la vérification n'a pas encore eu lieu."""
        with self.lock:
            fiches = dict(self.fiches)
        sortie = []
        for contact in self.tableau.config.contacts:
            fiche = fiches.get(contact["did"], {"did": contact["did"], "boite": None,
                                                "note": {"etat": "en_attente"},
                                                "boite_etat": {"etat": "inconnue"},
                                                "anciennete": {"etat": "inconnue"},
                                                "activite": None, "risques": [],
                                                "verifie_le": None})
            sortie.append({**fiche, "surnom": contact.get("surnom", "")})
        return sortie


class Tableau:
    def __init__(self, archive: pv.Archive):
        self.archive = archive
        self.config = Config(archive)
        self.limiter = RateLimiter(READS_PER_SECOND)
        self.watchers: dict[str, RoomWatcher] = {}
        self.events: deque = deque(maxlen=50)
        self.dirty = threading.Event()
        self.dirty.set()
        self.version = 0
        self.jobs = {"horodatage": None, "completer": None, "carnet": None}
        self.rooms_cache: tuple[float, list] = (0.0, [])
        self.export_lock = threading.Lock()
        self.boites: dict[str, BoiteWatcher] = {}
        self.boite_lock = threading.Lock()
        self.boite_path = archive.root / "boite.jsonl"
        self.recus: list[dict] = []
        self.recus_cles: set = set()
        for ligne in (self.boite_path.read_text(encoding="utf-8").splitlines()
                      if self.boite_path.exists() else []):
            try:
                recu = json.loads(ligne)
            except json.JSONDecodeError:
                continue
            recu.setdefault("genre", "boite")
            self.recus.append(recu)
            self.recus_cles.add((recu["salon"], recu["seq"]))
        self.carnet = Carnet(self)

    # -- surveillance

    def sync_watchers(self, backfill: bool = False) -> None:
        for room in list(self.watchers):
            if room not in self.config.salons:
                self.watchers.pop(room).stop_event.set()
        for room in self.config.salons:
            watcher = self.watchers.get(room)
            if watcher is None:
                watcher = self.watchers[room] = RoomWatcher(room, self)
                watcher.start()
            elif backfill:
                watcher.backfill_event.set()

    def sync_boites(self) -> None:
        for room in list(self.boites):
            if room not in self.config.boites:
                self.boites.pop(room).stop_event.set()
        for room in self.config.boites:
            if room not in self.boites:
                self.boites[room] = BoiteWatcher(room, self)
                self.boites[room].start()

    @staticmethod
    def mentionne(message: dict, dids: set) -> bool:
        """Un tiers cite un de tes DID : reçu d'arbitre, accusé, réponse. Signé par lui, pas par toi."""
        texte = message.get("text") or ""
        return any(did in texte for did in dids)

    def recevoir(self, room: str, message: dict, genre: str = "boite") -> bool:
        """Enregistre un message écrit par un tiers. Renvoie True si c'est un nouveau.

        Boîte ou mention, c'est la même chose du point de vue de la confiance : une donnée écrite
        par quelqu'un d'autre, vérifiée mais jamais une consigne, et rangée à part des preuves.
        """
        cle = (room, message.get("seq"))
        expediteur = message.get("from", "")
        if expediteur in self.config.dids:
            return False  # vos propres messages sont déjà des preuves
        with self.boite_lock:
            if cle in self.recus_cles:
                return False
            if genre == "mention" and sum(1 for r in self.recus if r.get("genre") == "mention") >= MENTIONS_MAX:
                return False
            valide = pv.verify_record({"type": "message", "room": room,
                                       **{k: message.get(k) for k in ("seq", "ts", "from", "nonce",
                                                                      "text", "sig")}})
            recu = {"salon": room, "seq": message.get("seq"), "ts": message.get("ts"),
                    "de": expediteur, "texte": message.get("text", ""),
                    "sig": message.get("sig"), "nonce": message.get("nonce"),
                    "signature_valide": bool(valide), "genre": genre, "recu_le": pv.now_iso()}
            self.recus.append(recu)
            self.recus_cles.add(cle)
            with open(self.boite_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(recu, ensure_ascii=False) + "\n")
        self.changed()
        log(f"{'MENTION' if genre == 'mention' else 'MESSAGE REÇU'} dans {room} de {expediteur[:20]}… "
            f"({'signature valide' if valide else 'SIGNATURE INVALIDE'})")
        return True

    def capture(self, room: str, message: dict, source: str) -> bool:
        record = pv.message_record({**message, "room": room}, room, source)
        record["server_confirmation"] = {"checked_at": pv.now_iso(), "status": "confirme"}
        if not record["signature_valide"]:
            log(f"{room} #{message.get('seq')} : signature invalide, ignoré")
            return False
        path = self.archive.save(record)
        if path is None:
            return False
        self.changed()
        self.events.appendleft({"quand": pv.now_iso(), "fichier": str(path.relative_to(self.archive.root)),
                                "salon": room, "seq": record["seq"], "source": source})
        log(f"capturé : {room} #{record['seq']} ({source})")
        return True

    def changed(self) -> None:
        self.version += 1
        self.dirty.set()

    # -- tâches de fond

    def maintenance(self) -> None:
        last_stamp, last_upgrade, last_carnet = 0.0, 0.0, 0.0
        while True:
            if self.archive.reload():
                self.changed()
            if self.dirty.is_set():
                self.dirty.clear()
                try:
                    pv.build_reports(self.archive)
                except OSError as error:
                    log(f"rapport non régénéré : {error}")
            now = time.monotonic()
            if now - last_stamp > STAMP_EVERY:
                last_stamp = now
                self.stamp_pending()
            if now - last_upgrade > UPGRADE_EVERY:
                last_upgrade = now
                self.upgrade_pending()
            if now - last_carnet > CARNET_EVERY and self.config.contacts:
                last_carnet = now
                self.run_job("carnet", self.carnet.verifier_tous)
            time.sleep(MAINTENANCE_EVERY)

    def stamp_pending(self) -> int:
        done = 0
        for path, _ in self.archive.records():
            if not path.with_name(path.name + ".ots").exists():
                try:
                    pv.stamp(path)
                    done += 1
                except pv.PreuveError as error:
                    log(f"horodatage reporté : {error}")
                    break
        if done:
            log(f"{done} preuve(s) envoyée(s) à OpenTimestamps")
            self.changed()
        return done

    def upgrade_pending(self) -> int:
        done = 0
        for path, _ in self.archive.records():
            ots = path.with_name(path.name + ".ots")
            if pv.ots_status(ots) == "en_attente":
                try:
                    done += pv.upgrade(ots)
                except (pv.PreuveError, HTTPError) as error:
                    log(f"{path.name} : {error}")
        if done:
            log(f"{done} horodatage(s) ancré(s) dans Bitcoin")
            self.changed()
        return done

    def run_job(self, name: str, func) -> None:
        if self.jobs[name] and self.jobs[name].is_alive():
            return
        self.jobs[name] = threading.Thread(target=func, daemon=True)
        self.jobs[name].start()

    # -- données pour l'interface

    def rows(self) -> list[dict]:
        rows = []
        for path, record in self.archive.records():
            rel = str(path.relative_to(self.archive.root))
            ots = pv.ots_status(path.with_name(path.name + ".ots"))
            base = {"fichier": rel, "horodatage": ots, "signature_valide": bool(record.get("signature_valide")),
                    "source": record.get("source"), "capture": record.get("captured_at")}
            if record["type"] == "message":
                rows.append({**base, "type": "message", "date": record["ts"], "did": record["from"],
                             "salon": record["room"], "seq": record["seq"], "texte": record["text"],
                             "serveur": record.get("server_confirmation", {}).get("status", "non_verifie")})
            else:
                proof = record["proof"]
                rows.append({**base, "type": "contribution", "date": record["captured_at"], "did": proof["did"],
                             "url": proof["artifact_url"], "commit": proof["commit"], "texte": proof["artifact_url"]})
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows

    def state(self) -> dict:
        return {
            "version": self.version,
            "archive": str(self.archive.root),
            "agent": str(AGENT_DIR) if (AGENT_DIR / "technocore_agent.py").exists() else None,
            "dids": self.config.dids,
            "salons": [self.watchers[r].status if r in self.watchers else {"salon": r, "etat": "arret"}
                       for r in self.config.salons],
            "boites": [self.boites[r].status if r in self.boites else {"boite": r, "etat": "arret"}
                       for r in self.config.boites],
            "non_lus": sum(1 for r in self.recus if r["recu_le"] > self.config.boite_lue),
            "contacts": len(self.config.contacts),
            "evenements": list(self.events)[:20],
            "taches": {k: bool(v and v.is_alive()) for k, v in self.jobs.items()},
        }

    def public_rooms(self) -> list[dict]:
        cached_at, rooms = self.rooms_cache
        if time.monotonic() - cached_at < 60:
            return rooms
        self.limiter.acquire()
        data = json.loads(pv.http(f"{pv.BASE_URL}/rooms?format=json&limit=100", timeout=20))
        items = data.get("rooms", data) if isinstance(data, dict) else data
        rooms = [{"salon": r.get("room") or r.get("name"), "sujet": r.get("topic"), "dernier": r.get("last_seq")}
                 for r in items if isinstance(r, dict) and (r.get("room") or r.get("name"))]
        self.rooms_cache = (time.monotonic(), rooms)
        return rooms

    def zip_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive_zip:
            for path in sorted(self.archive.root.rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    archive_zip.write(path, f"Technocore-preuves/{path.relative_to(self.archive.root)}")
        return buffer.getvalue()


def make_handler(tableau: Tableau, token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    page = (HERE / "web" / "index.html").read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "technocore-tableau"

        def log_message(self, *args) -> None:  # silencieux : le terminal sert au journal des captures
            pass

        def send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def json(self, data, status: int = 200) -> None:
            self.send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def guard(self) -> bool:
            # Refuse les requêtes dont l'hôte n'est pas le nôtre (protection contre le DNS rebinding).
            if self.headers.get("Host") not in allowed_hosts:
                self.send(403, b"hote refuse", "text/plain")
                return False
            return True

        def do_GET(self) -> None:
            if not self.guard():
                return
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == "/":
                html = page.replace("__JETON__", token)
                self.send(200, html.encode(), "text/html; charset=utf-8", {
                    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                               "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                               "frame-ancestors 'none'"})
            elif url.path == "/api/etat":
                self.json(tableau.state())
            elif url.path == "/api/preuves":
                self.json(tableau.rows())
            elif url.path == "/api/boite":
                with tableau.boite_lock:
                    recus = sorted(tableau.recus, key=lambda r: (r["ts"] or ""), reverse=True)
                self.json({"messages": recus, "lue_jusqua": tableau.config.boite_lue})
            elif url.path == "/api/carnet":
                self.json({"contacts": tableau.carnet.liste()})
            elif url.path == "/api/salons-publics":
                try:
                    self.json(tableau.public_rooms())
                except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
                    self.json({"erreur": str(error)}, 502)
            elif url.path == "/api/fichier":
                name = (query.get("nom") or [""])[0]
                path = tableau.archive.root / name
                if not FILE_RE.match(name) or not path.is_file():
                    self.send(404, b"introuvable", "text/plain")
                    return
                content_type = "application/octet-stream" if name.endswith(".ots") else "application/json"
                self.send(200, path.read_bytes(), content_type,
                          {"Content-Disposition": f'attachment; filename="{path.name}"'})
            elif url.path.startswith("/api/rapport/") and url.path.rsplit("/", 1)[-1] in REPORTS:
                name = url.path.rsplit("/", 1)[-1]
                pv.build_reports(tableau.archive)
                self.send(200, (tableau.archive.root / name).read_bytes(), REPORTS[name] + "; charset=utf-8",
                          {"Content-Disposition": f'attachment; filename="{name}"'})
            elif url.path == "/api/archive.zip":
                self.send(200, tableau.zip_bytes(), "application/zip",
                          {"Content-Disposition": 'attachment; filename="Technocore-preuves.zip"'})
            else:
                self.send(404, b"introuvable", "text/plain")

        def do_POST(self) -> None:
            if not self.guard():
                return
            # Jeton exigé : un autre site ouvert dans le navigateur ne peut pas piloter le tableau de bord.
            if not secrets.compare_digest(self.headers.get("X-Jeton", ""), token):
                self.send(403, b"jeton invalide", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > 20 * 1024 * 1024:
                self.send(413, b"trop gros", "text/plain")
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self.json({"erreur": "JSON invalide"}, 400)
                return
            path = urlsplit(self.path).path
            if path == "/api/config":
                old_dids = set(tableau.config.dids)
                bad = tableau.config.update(body.get("dids", []), body.get("salons", []),
                                            body.get("boites"))
                if bad:
                    self.json({"erreur": "valeurs invalides", "details": bad}, 400)
                    return
                tableau.sync_watchers(backfill=bool(set(tableau.config.dids) - old_dids))
                tableau.sync_boites()
                self.json(tableau.state())
            elif path == "/api/importer":
                text = body.get("texte", "")
                records = [r for obj in pv.extract_json_objects(text)
                           for r in pv.records_from_object(obj, "import")]
                added, invalid = 0, 0
                for record in records:
                    if not pv.verify_record(record):
                        invalid += 1
                        continue
                    if tableau.archive.save(record):
                        added += 1
                if added:
                    tableau.changed()
                    tableau.run_job("horodatage", tableau.stamp_pending)
                self.json({"trouvees": len(records), "ajoutees": added, "invalides": invalid})
            elif path == "/api/contacts":
                bad = tableau.config.update_contacts(body.get("contacts", []))
                if bad:
                    self.json({"erreur": "valeurs invalides", "details": bad}, 400)
                    return
                tableau.run_job("carnet", tableau.carnet.verifier_tous)
                self.json({"contacts": tableau.carnet.liste()})
            elif path == "/api/carnet":
                tableau.run_job("carnet", tableau.carnet.verifier_tous)
                self.json({"ok": True})
            elif path == "/api/boite/lu":
                tableau.config.marquer_lue()
                tableau.changed()
                self.json(tableau.state())
            elif path == "/api/horodater":
                tableau.run_job("horodatage", tableau.stamp_pending)
                self.json({"ok": True})
            elif path == "/api/completer":
                tableau.run_job("completer", tableau.upgrade_pending)
                self.json({"ok": True})
            else:
                self.send(404, b"introuvable", "text/plain")

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tableau de bord local des preuves Technocore")
    parser.add_argument("--archive", type=Path, default=pv.DEFAULT_ARCHIVE)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--sans-navigateur", action="store_true")
    args = parser.parse_args(argv)

    tableau = Tableau(pv.Archive(args.archive))
    server = None
    for port in range(args.port, args.port + 20):
        try:
            token = secrets.token_urlsafe(24)
            server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(tableau, token, port))
            break
        except OSError:
            continue
    if server is None:
        print("Aucun port libre entre", args.port, "et", args.port + 19, file=sys.stderr)
        return 1
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    tableau.sync_watchers()
    tableau.sync_boites()
    if tableau.config.contacts:
        tableau.run_job("carnet", tableau.carnet.verifier_tous)
    threading.Thread(target=tableau.maintenance, name="maintenance", daemon=True).start()
    log(f"Tableau de bord : {url}")
    log(f"Archive : {tableau.archive.root}")
    log(f"DID surveillés : {len(tableau.config.dids)} · salons : {', '.join(tableau.config.salons) or 'aucun'}"
        f" · boîtes : {', '.join(tableau.config.boites) or 'aucune'}"
        f" · contacts : {len(tableau.config.contacts)}")
    log("Laisse cette fenêtre ouverte pour continuer la surveillance. Ctrl+C pour arrêter.")
    if not args.sans_navigateur:
        threading.Timer(0.8, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Arrêt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
